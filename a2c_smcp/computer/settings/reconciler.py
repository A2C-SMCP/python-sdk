# -*- coding: utf-8 -*-
# filename: reconciler.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Reconciler：启动对账（additive-only 只增不删）+ 孤儿清理（marketplace prune / plugin gc）（v0.2.1 #62）
Reconciler: startup reconcile (additive-only) + orphan cleanup (v0.2.1 #62).

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §7.1（启动流程）/ §7.2（失败降级）/
                   §7.3（显式 sync 与孤儿清理）；父工单 #53「reconciler additive-only」决策。

核心契约 / Core contract（§7.1 校正自 CC 问询）：
- **先合并单一声明视图再对账**（不是逐 scope）：调用方经
  :func:`a2c_smcp.computer.settings.scope.resolve_settings` 把所有 scope 合成单一视图后传入
  :func:`reconcile` 的 ``declared``（``enabledPlugins`` / ``extraKnownMarketplaces`` 两键即对账输入）。
- **additive-only 只增不删**：CC 的 reconcile 返回只有 ``{installed, updated, upToDate, failed, skipped}``、
  **无 removed**。"声明没、物化有"的条目**完全不动**（绝不自动清理）；孤儿留待 §7.3 显式
  :func:`prune_marketplaces` / :func:`gc_plugins` 清。
- **失败降级铁律**（§7.2）：git clone/pull 失败 → 记 ERROR、不阻断其余、对 Agent 不可见（不入
  Registry）；本模块**不**向调用方抛 git/IO 异常（沿用 :func:`stage_marketplace_skills` 的吞错降级）。

边界（#62 / #63 接缝）/ Boundary:
- 本模块**只**维护 ``known_marketplaces.json``（经 :func:`stage_marketplace_skills`）并按
  ``enabledPlugins`` 注册启用 plugin 的 SKILL；**不写** ``installed_plugins.json``——plugin install /
  uninstall / enable / disable 账本归 #63。:func:`gc_plugins` **读** ``installed_plugins.json`` 找孤儿
  （生产由 #63 写入、本工单测试直接 seed）。
- bundled MCP server 的起停经 :func:`gc_plugins` 的 ``mcp_teardown`` 回调注入；真正接线（MCP manager）
  由 computer.py 集成承担，不在 #62。
- **与治理启动恢复的分工（#117/#123）**：本模块是**marketplace 声明式**对账（``extraKnownMarketplaces``
  驱动的 catalog 物化 + 启用 plugin 的 SKILL 注册）；boot 恢复走 :mod:`~a2c_smcp.computer.settings.recovery`
  （**intent 驱动**：``installedPlugins`` 为安装事实源、活跃集 = installed ∧ ``enabledPlugins[id] is True``
  （v0.3.0 缺省翻转）、账本缺失时重物化），两者 additive-only 语义一致、职责不同。

并发 / Concurrency：:func:`reconcile` **串行** stage 各 marketplace（不 ``asyncio.gather``），遵守
:func:`stage_marketplace_skills` 文档化的同步 ``file_lock`` 阻塞约束（store.py 同步设计的固有约束）。
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from a2c_smcp.computer.settings.mcp_config import mcp_json_declared_bundle_ids
from a2c_smcp.computer.settings.schema import is_valid_enabled_plugin_key, is_valid_marketplace_name
from a2c_smcp.computer.settings.store import (
    InstalledPluginsFile,
    KnownMarketplacesFile,
    load_installed_plugins,
    load_known_marketplaces,
    update_installed_plugins,
    update_known_marketplaces,
)
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.manifest import (
    PluginManifestError,
    find_plugin_entry,
    load_bundled_servers,
    read_marketplace_manifest,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import (
    _EXTERNAL_PLUGINS_NS,
    DEFAULT_GIT_TIMEOUT,
    stage_marketplace_skills,
)
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import is_within

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 对账报告 / Reconcile report
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """
    一次 :func:`reconcile` 的结果（镜像 CC reconcile 返回值；**无 removed** —— additive-only）。
    The result of one :func:`reconcile` run (mirrors CC's return shape; **no removed** — additive-only)。

    各 marketplace 名互斥地归入 installed / updated / up_to_date / failed / skipped 之一；
    ``registered_skills`` 汇总本次成功注册 / 刷新的全部 SKILL 名（供 watcher / emit diff）。
    """

    installed: list[str] = field(default_factory=list)  # 本次新 clone 的 marketplace
    updated: list[str] = field(default_factory=list)  # autoUpdate pull / sourceChanged 重 clone
    up_to_date: list[str] = field(default_factory=list)  # 声明且已在、未刷新
    failed: list[str] = field(default_factory=list)  # 声明但 stage 后 clone 树仍缺失（clone 失败，已降级）
    skipped: list[str] = field(default_factory=list)  # 预留（trust 门控落 #68 / strict 延后 #80）
    registered_skills: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 声明视图提取 / Declared-view extraction
# ---------------------------------------------------------------------------
def _declared_marketplaces(declared: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """
    从单一声明视图取合法 marketplace 声明 / Extract valid marketplace declarations。

    跳过非法名（非 strict-kebab）/ 非对象条目（记 WARN）；返回 ``name → {source, autoUpdate?}``。
    """
    raw = declared.get("extraKnownMarketplaces")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Mapping[str, object]] = {}
    for name, decl in raw.items():
        if not is_valid_marketplace_name(name):
            logger.warning("reconcile: invalid marketplace name %r in extraKnownMarketplaces, skipped", name)
            continue
        if not isinstance(decl, Mapping):
            logger.warning("reconcile: marketplace %r declaration is not an object, skipped: %r", name, decl)
            continue
        out[name] = decl
    return out


def declared_marketplace_names(declared: Mapping[str, object]) -> set[str]:
    """单一声明视图里全部合法 marketplace 名 / All valid declared marketplace names（供孤儿判定）。"""
    return set(_declared_marketplaces(declared))


def declared_installed_plugin_ids(declared: Mapping[str, object]) -> set[str]:
    """
    merged ``installedPlugins``（全局安装意图，协议 v0.3.0 §2.4）中的合法 pid 集合 / Declared install intent。

    用于 :func:`list_orphan_plugins` 的孤儿判定与「活跃集 = installed ∧ enabled」门控（#123）：
    ``installed_disabled``（已安装未启用）是合法静止态、**非**孤儿；仅账本 pid ∉ 安装意图才算孤儿。
    """
    raw = declared.get("installedPlugins")
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str) and is_valid_enabled_plugin_key(item)}


def _enabled_plugin_names_for(marketplace: str, declared: Mapping[str, object]) -> set[str]:
    """
    某 marketplace 下**活跃**（installed ∧ ``enabledPlugins[id] is True``，v0.3.0 §4.8.1）的 plugin 名集合。

    ``enabledPlugins`` key 形如 ``<plugin>@<mp>``；返回去掉 ``@<mp>`` 后缀的 ``<plugin>`` 集合，作
    :func:`stage_marketplace_skills` 的 ``plugin_filter``（缺启用项/未安装 → 空集 → 仅 clone catalog、不注册 SKILL）。
    """
    raw = declared.get("enabledPlugins")
    if not isinstance(raw, Mapping):
        return set()
    installed = declared_installed_plugin_ids(declared)
    suffix = f"@{marketplace}"
    names: set[str] = set()
    for key, enabled in raw.items():
        if enabled is True and key in installed and key.endswith(suffix):
            names.add(key[: -len(suffix)])
    return names


# ---------------------------------------------------------------------------
# 安全删除 / Guarded removal
# ---------------------------------------------------------------------------
def _safe_rmtree(path: Path, home: Path) -> None:
    """
    仅当 ``path`` 词法位于 SKILL Home 内才递归删除（防越权删盘外目录）/ rmtree only within SKILL Home。

    ``path`` 不存在 → no-op；越界 → 记 ERROR + 拒删（不抛）。删除失败 → 记 WARN（不阻断后续清理）。
    """
    rp = path.resolve()
    if not is_within(rp, home.resolve()):
        logger.error("reconcile: refusing to remove %s outside SKILL Home %s", rp, home)
        return
    if not rp.exists():
        return
    try:
        shutil.rmtree(rp)
    except OSError as e:  # 失败降级：删除受阻不阻断其余清理
        logger.warning("reconcile: failed to remove %s: %s", rp, e)


def _unregister_marketplace_skills(registry: SkillRegistry, marketplace: str, *, plugin: str | None = None) -> None:
    """
    从 Registry 注销某 marketplace（可选限定单 plugin）的 SKILL / Unregister a marketplace's SKILLs。

    按 ``A2CSkillRef.source == "marketplace:<mp>"`` 反查（marketplace SKILL name 形如 ``<plugin>:<skill>``，
    ``plugin`` 给定时再按 ``<plugin>:`` 前缀过滤）。additive-only 模型下 marketplace clone 树不上 watcher，
    其 SKILL 在 prune/gc 前恒活跃，故经 :meth:`SkillRegistry.active_refs` 枚举即可。
    """
    want_source = f"{SOURCE_MARKETPLACE}:{marketplace}"
    prefix = f"{plugin}:" if plugin is not None else None
    for ref in registry.active_refs():
        if ref.get("source") != want_source:
            continue
        name = ref.get("name")
        if not name:
            continue
        if prefix is not None and not name.startswith(prefix):
            continue
        registry.unregister(name)


# ---------------------------------------------------------------------------
# 启动对账（additive-only）/ Startup reconcile (additive-only)
# ---------------------------------------------------------------------------
async def reconcile(
    registry: SkillRegistry,
    home: Path,
    declared: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
    refresh: bool = False,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> ReconcileReport:
    """
    对账声明的 marketplace 与物化 clone 树（additive-only）/ Reconcile declared marketplaces vs materialized clones。

    四分支（§7.1 step 3）/ Four branches:
    - **missing**（declared∖materialized）→ clone（``stage_marketplace_skills`` 缺失即 clone）。
    - **sourceChanged**（``record.source != decl.source``）→ 先 ``rmtree`` 旧 clone 树（含外部 plugin 树）
      再重 clone（**reconciler 兜底**：:func:`stage_marketplace_skills` 仅 pull 不换库）。
    - **autoUpdate**（``decl.autoUpdate==True`` 或显式 ``refresh=True``）→ ``git pull``。
    - **orphan**（materialized∖declared）→ **完全不动**（不进循环；靠 :func:`prune_marketplaces` 显式清）。

    每个 declared marketplace 物化其**活跃**（installed ∧ ``enabledPlugins[id] is True``，v0.3.0）plugin 的
    SKILL（``plugin_filter``）；未安装 / 禁用 / 未声明 plugin 不注册。失败降级：stage 吞错返回空 →
    本函数据 clone 树是否存在判 ``failed``。

    :param registry: 目标 :class:`SkillRegistry`。
    :param home: SKILL Home 绝对根（调用方保证存在）。
    :param declared: 单一声明视图（:func:`...scope.resolve_settings` 的 ``.settings``），取
        ``extraKnownMarketplaces`` / ``enabledPlugins`` 两键。
    :param env: git 子进程 / 物化文件路径解析环境（默认 ``os.environ``）。
    :param refresh: ``True`` = 显式 sync（``/plugin sync`` / ``/marketplace refresh``）：对**全部**已存在
        clone 走 pull（不止 ``autoUpdate`` 项）；仍 additive-only。
    :param timeout: 单次 git 操作超时（秒），透传 :func:`stage_marketplace_skills`。
    :return: :class:`ReconcileReport`。
    """
    declared_mps = _declared_marketplaces(declared)
    # 对账前快照物化状态（stage 会在循环内回写 known_marketplaces.json，故先取基线分类）。
    known = load_known_marketplaces(home=home, env=env)
    materialized = known.get("marketplaces", {})

    installed: list[str] = []
    updated: list[str] = []
    up_to_date: list[str] = []
    failed: list[str] = []
    registered_skills: list[str] = []

    ext_root = home / SOURCE_MARKETPLACE / _EXTERNAL_PLUGINS_NS
    for name, decl in declared_mps.items():
        source = decl.get("source")
        record = materialized.get(name)
        source_changed = record is not None and record.get("source") != source
        clone_dir = marketplace_skill_dir(home, name)

        if source_changed:
            # 源已变更：旧 clone 是「别的仓库」，必须 wipe 后重 clone（stage 只 pull 不换库）。
            logger.info("reconcile: marketplace %r source changed, wiping stale clone for re-clone", name)
            _unregister_marketplace_skills(registry, name)
            _safe_rmtree(clone_dir, home)
            _safe_rmtree(ext_root / name, home)

        present_before = clone_dir.exists()
        do_refresh = bool(decl.get("autoUpdate")) or refresh
        plugin_filter = _enabled_plugin_names_for(name, declared)

        names = await stage_marketplace_skills(
            name,
            source if isinstance(source, Mapping) else {},
            registry,
            home,
            plugin_filter=plugin_filter,
            auto_update=bool(decl.get("autoUpdate")),
            refresh=do_refresh,
            timeout=timeout,
            env=env,
        )
        registered_skills.extend(names)

        if not clone_dir.exists():
            failed.append(name)
        elif source_changed:
            updated.append(name)
        elif not present_before:
            installed.append(name)
        elif do_refresh:
            updated.append(name)
        else:
            up_to_date.append(name)

    return ReconcileReport(
        installed=installed,
        updated=updated,
        up_to_date=up_to_date,
        failed=failed,
        registered_skills=registered_skills,
    )


# ---------------------------------------------------------------------------
# 孤儿清理 / Orphan cleanup（§7.3，additive-only 唯一删除入口）
# ---------------------------------------------------------------------------
def list_orphan_marketplaces(
    home: Path,
    declared: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """
    列出"所有 scope 都不再声明"的孤儿 marketplace（物化有、声明无）/ List orphan marketplaces。

    :param declared: 单一声明视图（取 ``extraKnownMarketplaces`` key 作"仍声明"集）。
    :return: ``known_marketplaces.json`` 中 key ∉ 声明集 的 marketplace 名（保持物化顺序）。
    """
    declared_names = declared_marketplace_names(declared)
    known = load_known_marketplaces(home=home, env=env)
    return [name for name in known.get("marketplaces", {}) if name not in declared_names]


def prune_marketplaces(
    names: list[str],
    registry: SkillRegistry,
    home: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """
    清理孤儿 marketplace（clone 树 + 外部 plugin 树 + known_marketplaces.json 条目 + Registry SKILL）/ Prune。

    y/N 确认交 CLI 层（#68）；本函数只**执行**清理（``names`` 应为 :func:`list_orphan_marketplaces` 的子集，
    经用户确认后传入）。非法名跳过；删除越界守卫见 :func:`_safe_rmtree`。**不**触碰 ``installed_plugins.json``
    （plugin 账本归 #63 / :func:`gc_plugins`）。

    :return: 实际清理的 marketplace 名列表。
    """
    ext_root = home / SOURCE_MARKETPLACE / _EXTERNAL_PLUGINS_NS
    removed: list[str] = []
    for name in names:
        if not is_valid_marketplace_name(name):
            logger.warning("prune: invalid marketplace name %r, skipped", name)
            continue
        _unregister_marketplace_skills(registry, name)
        _safe_rmtree(marketplace_skill_dir(home, name), home)
        _safe_rmtree(ext_root / name, home)

        def _drop(data: KnownMarketplacesFile, _n: str = name) -> None:
            data.get("marketplaces", {}).pop(_n, None)

        update_known_marketplaces(_drop, home=home, env=env)
        removed.append(name)
        logger.info("prune: removed orphan marketplace %r", name)
    return removed


def list_orphan_plugins(
    home: Path,
    declared: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """
    列出孤儿 plugin：账本有记录、``installedPlugins`` 安装意图不再包含（v0.3.0 §2.3 账本=派生缓存）/ Orphans。

    ``installed_disabled``（意图在、未启用）是合法静止态 → **非**孤儿；``enabledPlugins``（含 ``false``）
    不参与孤儿判定（enablement 与 installation 正交，§2.4）。

    :param declared: 单一声明视图（取 ``installedPlugins`` 合法条目作"仍安装"集，见
        :func:`declared_installed_plugin_ids`）。
    :return: ``installed_plugins.json`` 中 plugin id ∉ 安装意图 的列表（保持物化顺序）。
    """
    declared_ids = declared_installed_plugin_ids(declared)
    installed = load_installed_plugins(home=home, env=env)
    return [pid for pid in installed.get("plugins", {}) if pid not in declared_ids]


def ledger_record_materialized(rec: object) -> bool:
    """
    单条账本记录是否仍有效物化：「``installPath`` 目录存在 ∧ bundled JSON 可解析」/ Per-record live check。

    v0.3.0 §5.8（安装路径非权威，boot MUST 重新校验）+ #125 任务 4：仅查目录存在会漏掉「目录在、bundled JSON
    事后损坏」——stage 后 skill 亮而 :func:`~a2c_smcp.computer.settings.recovery.collect_enabled_bundled_servers`
    WARN-skip，即 rust-sdk#102 同型半态。判据升级为可解析：:class:`PluginManifestError` → 该记录失效 →
    触发重物化（catalog 完好则修复指回；不可修复则整体保持 ``installed_disabled``，skill 不单独亮）。
    记录级单点：entry 级 any/all 变体与 recovery 死记录清扫共用（判据对称，隔离审查 🟡#4）。
    """
    install_path = rec.get("installPath") if isinstance(rec, Mapping) else None
    if not (isinstance(install_path, str) and install_path and Path(install_path).is_dir()):
        return False
    try:
        load_bundled_servers(Path(install_path))
    except PluginManifestError as e:
        logger.warning("ledger record %s has corrupt bundled server JSON, treated as unmaterialized: %s", install_path, e)
        return False
    return True


def ledger_entry_materialized(records: object) -> bool:
    """
    某 pid 是否**存在**有效物化记录（∃ 语义）/ Whether any record is live。

    原 ``recovery._ledger_materialized`` 迁入公开化（#125 任务 2）：作 :func:`list_dangling_plugin_intents`
    的悬挂判据——只要还有一条活记录就不是「意图 ∖ 账本」悬挂（prune 对象须是零有效物化）。
    boot 重物化触发请用 :func:`ledger_entry_fully_materialized`（∀ 语义——混合健康度也要修复）。
    """
    if not isinstance(records, list):
        return False
    return any(ledger_record_materialized(rec) for rec in records)


def ledger_entry_fully_materialized(records: object) -> bool:
    """
    某 pid 的账本记录是否**全部**有效物化（∀ 语义，非空）/ Whether every record is live。

    boot 重物化触发判据（#125 隔离审查 🟡#4）：∃ 语义会让「一条健康 + 一条损坏」的混合健康度 pid 永不进
    ``needs_materialize``——损坏 scope 记录每次 boot 被 collect WARN-skip、又不被清扫，即窄化半态回归口。
    ∀ 语义下混合健康度触发重物化：健康 scope 幂等重建、损坏残留由 sweep（同记录级判据）清扫。
    """
    if not isinstance(records, list) or not records:
        return False
    return all(ledger_record_materialized(rec) for rec in records)


# ---------------------------------------------------------------------------
# 账本 MCP 依赖 + 回收判据（协议 runtime-contract §4.9.1，#153/D3+F1）/ Ledger MCP deps & reclaim criterion
# ---------------------------------------------------------------------------
def ledger_mcp_deps_of(records: object) -> set[str]:
    """
    某 pid 全部账本记录声明依赖的 MCP Server bundle_id 并集（§4.9.1-1）/ Declared MCP deps (bundle_ids) of a pid。

    值域是 **bundle_id**（身份），非 display name——账本 MUST NOT 记 name（§4.9.1-1）。跨 scope 记录取并集。
    """
    out: set[str] = set()
    if not isinstance(records, list):
        return out
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        deps = rec.get("mcpServers")
        if isinstance(deps, list):
            out.update(d for d in deps if isinstance(d, str))
    return out


def other_plugin_mcp_deps(
    plugins: Mapping[str, object],
    *,
    exclude_pid: str,
    retained_records: Sequence[object] = (),
) -> set[str]:
    """
    **本次操作后**仍有 plugin 声明依赖的 bundle_id 并集（回收判据第一项的数据源，#153）/ Deps still declared by others。

    :param plugins: 账本 ``plugins`` 映射（**移除本次记录之前**的视图——§4.9.1-3 要求停摘名单在账本移除前取得）。
    :param exclude_pid: 正在 disable/uninstall/gc 的 plugin id（其记录不算「其他 plugin」）。
    :param retained_records: ``exclude_pid`` 本次**未被移除**的记录（scoped uninstall 时的其余 scope）——它们
        仍声明依赖，故仍算依赖者。整 pid 移除（disable / 全 scope uninstall / gc）时传空。

    「其他 plugin」= 账本中的 **installed** 记录（**含 disabled**），不按 enabled 过滤：协议 §4.9.1-2 字面为
    「无其他 plugin **声明依赖** X」（声明 = 账本有记录），``conformance-tests.md`` §285「**最后一个依赖者卸载**
    时回收」亦印证判据看记录存在性而非启用态。后果是 installed-but-disabled 的依赖者会令 X 保留（保守），但其
    卸载时仍回收 ⇒ **不泄漏**。与 rust-sdk#139 同字面。
    """
    out: set[str] = set()
    for pid, records in plugins.items():
        if pid == exclude_pid:
            continue
        out |= ledger_mcp_deps_of(records)
    out |= ledger_mcp_deps_of(list(retained_records))
    return out


def reclaimable_mcp_deps(
    deps: Iterable[str],
    *,
    other_deps: set[str],
    user_declared: set[str],
) -> list[str]:
    """
    §4.9.1-2 **回收判据**（纯函数、零落盘状态、零 IO）/ The reclaim criterion。

    **回收 X ⟺ 无其他 plugin 声明依赖 X ∧ X 非用户声明**。disable / uninstall / gc **三个消费者全部委托本函数**，
    MUST NOT 各写副本（判据分叉 = 用户 server 被连坐或 server 泄漏；#142 `is_valid_bundle_id` 同款教训）。

    D5 措辞已由「只收回**自己带入**的」正式重写为「只收回**无人再依赖 ∧ 非用户声明**的」：前者把**时点快照**
    （安装时谁带入）写进了**长期判据**，正是传递性泄漏之源——A 引入 X、B 装时已在、卸 A 后 A 的记录连同
    「X 由 A 引入」这一事实一并消失 ⇒ 卸 B 时无人认领 X ⇒ **永久泄漏**。故账本 MUST NOT 存 provenance
    （§4.9.1-1），判据只用**现时**事实。

    :param deps: 本次 disable/uninstall 的 plugin 所声明的依赖（``ledger_mcp_deps_of`` 产出）。
    :param other_deps: :func:`other_plugin_mcp_deps` 产出。
    :param user_declared: :func:`~a2c_smcp.computer.settings.mcp_config.mcp_json_declared_bundle_ids` 产出。
        ⚠️ **已知未覆盖面**（#153 隔离审查 🔴，方案待三仓 Discussion 定案）：协议把本项的数据源写作「**运行期
        权威配置集**中 ``origin != plugin`` 的条目」，而本 SDK 的 manager **不存 origin**——用户经
        ``--config @file`` / SDK 内嵌 ``Computer(mcp_servers={...})`` 挂载的 server 走 transient
        ``amount_server``、**不落 mcp.json**，与「plugin 自己挂的 server」在可观测信息上**完全同形**。
        故若该 server 同时被某 plugin 声明依赖，卸载该 plugin 时仍会回收它。详见
        :func:`~a2c_smcp.computer.settings.mcp_config.mcp_json_declared_bundle_ids`。
    :return: 可回收的 bundle_id（保 ``deps`` 迭代序）。
    """
    return [d for d in deps if d not in other_deps and d not in user_declared]


# 悬挂意图 reason 分档（#125 任务 2；wire 值入 CLI JSON 输出，rust 镜像同字面）/ dangling reason tiers.
DANGLING_MARKETPLACE_NOT_ADDED = "marketplace-not-added"
DANGLING_CATALOG_MISSING = "catalog-missing"
DANGLING_MANIFEST_UNREADABLE = "manifest-unreadable"
DANGLING_ENTRY_MISSING = "entry-missing"


def list_dangling_plugin_intents(
    home: Path,
    declared: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """
    列出悬挂安装意图：``installedPlugins`` 声明 ∧ 账本无有效物化 ∧ **静态不可达**（#125 任务 2）/ Dangling intents。

    与 :func:`list_orphan_plugins` 互为反向：孤儿 = 账本 ∖ 意图（删派生缓存，恒安全）；悬挂 = 意图 ∖ 账本且
    离线判定无法重物化（prune 删的是**权威意图**，须 confirm / 显式 flag——§4.8.4 删除走显式路径）。
    「静态可达但未物化」**不**列入——下次 boot 由 recovery 重物化自愈，非 prune 对象。

    纯本地零网络。reason 四档供 CLI 分档提示（``catalog-missing`` 可能只是临时断网后 clone 未建，裁量留给调用方）：

    - :data:`DANGLING_MARKETPLACE_NOT_ADDED`：known_marketplaces 无记录（无自愈路径，最强 prune 信号）；
    - :data:`DANGLING_CATALOG_MISSING`：known 在、catalog clone 缺失（boot/refresh 会重试 clone）;
    - :data:`DANGLING_MANIFEST_UNREADABLE`：clone 在、marketplace.json 损坏/缺失（先 ``marketplace refresh``）;
    - :data:`DANGLING_ENTRY_MISSING`：manifest 合法但无该 plugin entry（上游已移除才 prune）。

    :return: ``[(pid, reason)]``，按 pid 排序稳定输出。
    """
    ledger = load_installed_plugins(home=home, env=env).get("plugins", {})
    known = load_known_marketplaces(home=home, env=env).get("marketplaces", {})
    out: list[tuple[str, str]] = []
    for pid in sorted(declared_installed_plugin_ids(declared)):
        if ledger_entry_materialized(ledger.get(pid)):
            continue
        plugin, _, marketplace = pid.partition("@")
        record = known.get(marketplace)
        if not isinstance(record, Mapping) or not isinstance(record.get("source"), Mapping):
            out.append((pid, DANGLING_MARKETPLACE_NOT_ADDED))
            continue
        catalog_dir = marketplace_skill_dir(home, marketplace)
        if not catalog_dir.is_dir():
            out.append((pid, DANGLING_CATALOG_MISSING))
            continue
        try:
            manifest = read_marketplace_manifest(catalog_dir)
        except PluginManifestError:
            out.append((pid, DANGLING_MANIFEST_UNREADABLE))
            continue
        if find_plugin_entry(manifest, plugin) is None:
            out.append((pid, DANGLING_ENTRY_MISSING))
        # else：静态可达（known ∧ clone ∧ entry）→ recoverable，boot 自愈，不列
    return out


async def gc_plugins(
    plugin_ids: list[str],
    registry: SkillRegistry,
    home: Path,
    *,
    env: Mapping[str, str] | None = None,
    mcp_teardown: Callable[[list[str]], Awaitable[None]] | None = None,
) -> list[str]:
    """
    清理孤儿 plugin（installPath 树 + installed_plugins.json 条目 + Registry SKILL + MCP 依赖回收）/ GC plugins。

    y/N 确认交 CLI 层（#68）；``plugin_ids`` 应为 :func:`list_orphan_plugins` 的子集（用户确认后传入）。
    每条记录的 ``installPath`` 仅在词法位于 SKILL Home 内才删（:func:`_safe_rmtree`）；其声明依赖的 MCP Server
    经 **§4.9.1-2 回收判据**（:func:`reclaimable_mcp_deps`）过滤后才交 ``mcp_teardown`` 停/摘——gc 是 uninstall
    的批量形态，同样 **MUST NOT 连坐用户自有 server**（#153）。

    逐 pid 处理且账本随之逐个删除 ⇒ 同批两 plugin 共享同一依赖时，先处理者因后者仍在账本而保留、后处理者
    （前者已删）回收 —— 自动满足「最后一个依赖者回收」。

    :param mcp_teardown: 可选异步回调，入参为本次**判定可回收**的 MCP Server **bundle_id** 列表；``None`` = 不处理。
    :return: 实际清理的 plugin id 列表。
    """
    installed = load_installed_plugins(home=home, env=env)
    plugins = installed.get("plugins", {})
    removed: list[str] = []
    for pid in plugin_ids:
        records = plugins.get(pid)
        if records is None:
            continue
        # 停摘候选（账本字段自足，§4.9.1-3）须在删树/删账本前取得。
        deps = ledger_mcp_deps_of(records)
        reclaim = reclaimable_mcp_deps(
            sorted(deps),
            other_deps=other_plugin_mcp_deps(plugins, exclude_pid=pid),
            user_declared=mcp_json_declared_bundle_ids(env=env),
        )
        for rec in records:
            install_path = rec.get("installPath")
            if isinstance(install_path, str) and install_path:
                _safe_rmtree(Path(install_path), home)

        plugin, _, marketplace = pid.partition("@")
        if marketplace:
            _unregister_marketplace_skills(registry, marketplace, plugin=plugin)

        if mcp_teardown is not None and reclaim:
            await mcp_teardown(reclaim)

        def _drop(data: InstalledPluginsFile, _p: str = pid) -> None:
            data.get("plugins", {}).pop(_p, None)

        update_installed_plugins(_drop, home=home, env=env)
        # 内存视图与磁盘同步：否则后续 pid 的 other_plugin_mcp_deps 会把**已 gc 的 pid** 误算作依赖者，
        # 令同批共享的依赖永不回收（泄漏）。
        plugins.pop(pid, None)
        removed.append(pid)
        logger.info("gc: removed orphan plugin %r (MCP deps reclaimed: %s)", pid, reclaim or "none")
    return removed
