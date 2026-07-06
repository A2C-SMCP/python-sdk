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
- **与治理启动恢复的分工（#117）**：本模块是**声明式**对账（declared 驱动，恢复不了"装即活跃"、未写
  ``enabledPlugins`` 的命令式安装）；boot 恢复走 :mod:`~a2c_smcp.computer.settings.recovery`
  （**ledger 驱动**：``installed_plugins.json`` 为"已安装"事实源 + ``enabledPlugins`` 仅作禁用门控），
  两者 additive-only 语义一致、职责不同。

并发 / Concurrency：:func:`reconcile` **串行** stage 各 marketplace（不 ``asyncio.gather``），遵守
:func:`stage_marketplace_skills` 文档化的同步 ``file_lock`` 阻塞约束（store.py 同步设计的固有约束）。
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

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


def declared_plugin_ids(declared: Mapping[str, object]) -> set[str]:
    """
    所有**声明过**的 plugin id（``<plugin>@<mp>``，含 ``false`` 禁用项）/ All declared plugin ids。

    用于 :func:`list_orphan_plugins` 的孤儿判定——``false`` = 声明禁用（**非**孤儿），仅 key 完全缺失才算孤儿。
    """
    raw = declared.get("enabledPlugins")
    if not isinstance(raw, Mapping):
        return set()
    return {k for k in raw if is_valid_enabled_plugin_key(k)}


def _enabled_plugin_names_for(marketplace: str, declared: Mapping[str, object]) -> set[str]:
    """
    某 marketplace 下**启用**（``true``）的 plugin 名集合 / Enabled (``true``) plugin names for a marketplace。

    ``enabledPlugins`` key 形如 ``<plugin>@<mp>``；返回去掉 ``@<mp>`` 后缀的 ``<plugin>`` 集合，作
    :func:`stage_marketplace_skills` 的 ``plugin_filter``（缺启用项 → 空集 → 仅 clone catalog、不注册 SKILL）。
    """
    raw = declared.get("enabledPlugins")
    if not isinstance(raw, Mapping):
        return set()
    suffix = f"@{marketplace}"
    names: set[str] = set()
    for key, enabled in raw.items():
        if enabled is True and is_valid_enabled_plugin_key(key) and key.endswith(suffix):
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

    每个 declared marketplace 物化其 ``enabledPlugins`` 中**启用**的 plugin 的 SKILL（``plugin_filter``）；
    禁用 / 未声明 plugin 不注册。失败降级：stage 吞错返回空 → 本函数据 clone 树是否存在判 ``failed``。

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
    列出"所有 scope 都不再声明"的孤儿 plugin（installed 有、enabledPlugins 无此 key）/ List orphan plugins。

    ``false`` = 声明禁用（key 仍在）→ **非**孤儿；仅 key 完全缺失才算孤儿（见 :func:`declared_plugin_ids`）。

    :param declared: 单一声明视图（取 ``enabledPlugins`` 全部 key——含 ``false``——作"仍声明"集）。
    :return: ``installed_plugins.json`` 中 plugin id ∉ 声明集 的列表（保持物化顺序）。
    """
    declared_ids = declared_plugin_ids(declared)
    installed = load_installed_plugins(home=home, env=env)
    return [pid for pid in installed.get("plugins", {}) if pid not in declared_ids]


async def gc_plugins(
    plugin_ids: list[str],
    registry: SkillRegistry,
    home: Path,
    *,
    env: Mapping[str, str] | None = None,
    mcp_teardown: Callable[[list[str]], Awaitable[None]] | None = None,
) -> list[str]:
    """
    清理孤儿 plugin（installPath 树 + installed_plugins.json 条目 + Registry SKILL + bundled MCP）/ GC plugins。

    y/N 确认交 CLI 层（#68）；``plugin_ids`` 应为 :func:`list_orphan_plugins` 的子集（用户确认后传入）。
    每条记录的 ``installPath`` 仅在词法位于 SKILL Home 内才删（:func:`_safe_rmtree`）；``bundledMcpServers``
    汇总后经 ``mcp_teardown`` 回调停/摘（真正接线由 computer.py 集成承担，#62 只触发回调）。

    :param mcp_teardown: 可选异步回调，入参为本次清理涉及的全部 bundled MCP server name；``None`` = 不处理。
    :return: 实际清理的 plugin id 列表。
    """
    installed = load_installed_plugins(home=home, env=env)
    plugins = installed.get("plugins", {})
    removed: list[str] = []
    for pid in plugin_ids:
        records = plugins.get(pid)
        if records is None:
            continue
        bundled: list[str] = []
        for rec in records:
            install_path = rec.get("installPath")
            if isinstance(install_path, str) and install_path:
                _safe_rmtree(Path(install_path), home)
            servers = rec.get("bundledMcpServers")
            if isinstance(servers, list):
                bundled.extend(s for s in servers if isinstance(s, str))

        plugin, _, marketplace = pid.partition("@")
        if marketplace:
            _unregister_marketplace_skills(registry, marketplace, plugin=plugin)

        if mcp_teardown is not None and bundled:
            await mcp_teardown(bundled)

        def _drop(data: InstalledPluginsFile, _p: str = pid) -> None:
            data.get("plugins", {}).pop(_p, None)

        update_installed_plugins(_drop, home=home, env=env)
        removed.append(pid)
        logger.info("gc: removed orphan plugin %r (bundled MCP: %s)", pid, bundled or "none")
    return removed
