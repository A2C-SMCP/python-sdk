# -*- coding: utf-8 -*-
# filename: installer.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Plugin 显式生命周期：install / uninstall / enable / disable（#63；#123 起对齐协议 v0.3.0 §2.4 install ⊥ enable）
Plugin explicit lifecycle: install / uninstall / enable / disable (aligned to protocol v0.3.0 §2.4 since #123).

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §3.3 / §4.3 / §6.2 / §7.2 / §10.6；
                   协议 v0.3.0 runtime-contract §2.3（config-first）/ §2.4（三态生命周期）；#123 裁决记录见 #120。

**v0.3.0 语义（破坏性，#123）**：install ≠ activate。两个正交声明式意图——
``installedPlugins``（**全局**安装事实，固定落 **user scope** settings.json；读取走 merged 并集）×
``enabledPlugins``（per-scope 启用意图，**仅显式 ``true`` 激活**，absent/false 均不活跃）。
``install`` 只 config-first 写 ``installedPlugins`` + 物化（:func:`materialize_plugin`：clone/账本/manifest
校验/冲突预检），**不激活**（不 stage SKILL、不写 ``enabledPlugins``）→ ``installed_disabled``；
``enable`` 才把 skills 与 bundled server **原子**并入投影（挂载失败回滚 ``installed_disabled``）；
``uninstall`` 删意图 + 清 ``enabledPlugins`` 条目 + teardown。物化账本 ``installed_plugins.json``
是可从 ``installedPlugins`` 重建的**纯派生缓存**（boot 重物化见 recovery，§4.9 删除无损）。

本模块是 :mod:`~a2c_smcp.computer.settings.reconciler` 的**兄弟**——reconciler 做 additive-only 对账 + 孤儿
gc/prune，本模块做**显式单 plugin** 增删启停，写 ``installed_plugins.json``（正是 :func:`gc_plugins` 读取的账本，
#62 decision A 留的接缝）。复用 :func:`~a2c_smcp.computer.skills.staging.stage_marketplace_skills`（skill 注册）
+ :func:`~a2c_smcp.computer.skills.staging.locate_plugin_root`（plugin 根定位/clone）+
:mod:`~a2c_smcp.computer.skills.manifest`（marketplace.json / plugin.json / mcp-servers 解析）。

**循环导入**：本模块 import ``skills.staging``（其顶层 import ``settings.schema``），故**刻意不在**
``settings/__init__`` re-export（与 reconciler 同——见 #62 教训）；消费方直接
``from a2c_smcp.computer.settings.installer import install_plugin`` 等。

**MCP 注入**：与 :func:`gc_plugins` 的 ``mcp_teardown`` 同款——MCP server 的存在性查询 / 注册 / 摘除经注入
回调（:data:`ExistingBundleIds` / :data:`RegisterServer` / :data:`RemoveServer`），由 #69 CLI 层的
:func:`~a2c_smcp.computer.cli.commands.build_mcp_callbacks` 用 ``Computer.amount_server`` /
``Computer.aunmount_server_by_id`` / ``mcp_manager.server_configs`` 包装；回调 ``None`` = ledger-only
（单测 / 无 server 场景）。**enable 注册经 Computer 路径渲染 ``${input:}``**（§9.3，inputs 池消歧归 #65）；
v0.3.0 起 install 不再收挂载回调（不激活）。

**plugin ↔ MCP Server = 依赖关系，非所有关系**（协议 runtime-contract §2.5 / §4.9.1，#153/D3+F1）：plugin 以
``bundle_id`` **声明依赖**，账本 ``mcpServers`` 记的是依赖声明。由此三条贯穿本模块：
  1. **install/enable 不因「同 bundle_id 已有」拒绝**——那是「依赖已满足」，提示并复用既有实例（§2.5-1）；
  2. **uninstall/disable 不无条件收回**——过 §4.9.1-2 判据「无其他 plugin 依赖 ∧ 非用户声明」才回收；
  3. **身份一律 bundle_id**——display name 只给用户看。

边界（文档化，非缺陷）/ Documented boundaries：
- 本模块操作 **live session**；跨重启恢复（活跃集 = installed ∧ enabled）由治理启动恢复承担（#117/#123）：
  bundled SKILL 经 ``Computer.boot_up`` 内 :mod:`~a2c_smcp.computer.settings.recovery`（**intent 驱动**）
  自动重建；bundled MCP server 重挂由 client 显式经 ``Computer.reconcile_governance(hooks)`` 触发
  （#93 client owns MCP config；CLI 启动序列即参考接线）。
- disable 的 skill orphan 为**内存态**（Registry 不落盘），同会话廉价复原；跨重启由治理恢复按
  ``enabledPlugins`` 门控重建（仅显式 true 激活）。**uninstall 连孤儿一并注销**（#257：``include_orphans=True``），
  :func:`_unregister_marketplace_skills`（marketplace remove / gc / prune）同样遍历含孤儿的全量条目——
  否则残留孤儿会被注册点跨 source 守卫当作「先到者」，令换源 **enable** 在本进程内永久失败
  （install 侧另有 :func:`_bare_name_conflicts` 的裸名预检，与本条无关）。
  enable 失败回滚以含孤儿的快照为基线：调用前已存在的孤儿回孤儿态，本次新增者才注销。
- :func:`enable_plugin` 不校验 ``installedPlugins`` 意图成员资格（以账本记录为"可启用"依据）：手编移除意图
  而账本未 gc 的窗口里 live enable 仍可点亮，但重启后 boot 门控（installed ∧ enabled）不会恢复，交由
  reconcile 收敛。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.raw import (
    SettingsFileCorruptError,
    list_field_or_empty,
    mapping_field_or_empty,
    read_raw_object,
)
from a2c_smcp.computer.settings.reconciler import (
    InstallPathRemovalError,
    NonPluginBundleIds,
    declared_installed_plugin_ids,
    is_external_plugin_install_path,
    ledger_entry_fully_materialized,
    ledger_mcp_deps_of,
    ledger_record_materialized,
    other_plugin_mcp_deps,
    reclaimable_mcp_deps,
    remove_plugin_install_path,
)
from a2c_smcp.computer.settings.schema import SettingsScope, is_valid_enabled_plugin_key
from a2c_smcp.computer.settings.scope import (
    DELETE,
    apply_write,
    load_settings_file,
    resolve_settings,
    user_settings_path,
    workdir_local_settings_path,
    workdir_project_settings_path,
)
from a2c_smcp.computer.settings.store import (
    InstalledPluginRecord,
    InstalledPluginsFile,
    atomic_write_json,
    file_lock,
    load_installed_plugins,
    load_known_marketplaces,
    update_installed_plugins,
)
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.manifest import (
    PluginManifestError,
    check_strict_conflict,
    entry_is_strict,
    find_plugin_entry,
    load_bundled_servers,
    manifest_declared_components,
    plugin_root_base,
    read_marketplace_manifest,
    read_plugin_metadata,
    resolve_plugin_version,
    resolve_skill_override_dirs,
)
from a2c_smcp.computer.skills.registry import SkillRegistry, SourceConflict
from a2c_smcp.computer.skills.sources import SkillSourceError
from a2c_smcp.computer.skills.staging import (
    DEFAULT_GIT_TIMEOUT,
    SKILL_MD,
    SKILLS_SUBDIR,
    SkillStagingError,
    locate_plugin_root,
    stage_marketplace_skills,
)
from a2c_smcp.utils.bundle_id import resolve_bundle_id
from a2c_smcp.utils.git_url import sanitize_text_for_diagnostics
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 异常 / Exceptions（模块内本地异常，仿 ToolNameDuplicatedError / SkillStagingError 惯例）
# ---------------------------------------------------------------------------
class PluginInstallError(Exception):
    """
    plugin install / enable / uninstall 编排失败。

    - install/enable 前置失败：marketplace 未添加 / catalog 未 clone / entry 缺失 / 非法 scope；
    - uninstall 删除**独占外部 clone** 失败（软链重定向 / resolve 异常 / rmtree 失败）——此时账本与安装意图
      保持不变，残留 clone 仍有索引可重试；调用方（CLI / marketplace_remove）必须按失败上报，不得当作 no-op。

    **结构化类别**（#257）：可选 ``error_code``（机读类别，如跨 marketplace 同名冲突的 ``"validation"``）与
    ``details``（附加 JSON 字段，如 ``{"conflictWith": "alpha@mp-a"}``）。缺省均为空 → CLI 输出形态与既有
    契约一致（``{"error": "error", ...}``）。与 ``InputResolutionError.error_code`` 同姿态：只在已能判定
    类别的位置填值，不在异常体系上另立新机制。
    """

    def __init__(self, message: str, *, error_code: str | None = None, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# ---------------------------------------------------------------------------
# MCP 注入接口 / Injected MCP interface（沿用 gc_plugins 的回调注入风格）
#
# **身份一律 bundle_id**（#153/D3）：账本记 bundle_id（§4.9.1-1）⇒ 依赖预检与停摘链全部收 bundle_id。
# 历史上本接缝收 display name，与「name 给用户看、bundle_id 给代码」判据相悖，且令「同名异 id」误判。
# ---------------------------------------------------------------------------
# 当前运行期已注册 server 的 **bundle_id** 集（同步）。数据源 MUST 是**运行期权威配置集**
# （CLI 包 ``{resolve_bundle_id(cfg) for cfg in comp.mcp_manager.server_configs()}``），
# MUST NOT 读构造期快照（协议 §2.5-4：快照对运行期挂载不可见，会把「依赖已满足」误判为「未满足」）。
ExistingBundleIds = Callable[[], set[str]]
# 运行期挂载一个 plugin 声明依赖的 server（异步；#137 ③ 起 CLI 包 transient ``Computer.amount_server``，含
# ``${input:}`` 渲染；治理投影不回写 mcp.json——依赖声明的真相在 ledger）。
RegisterServer = Callable[[MCPServerConfig], Awaitable[None]]
# 运行期停摘一个 server（异步，入参 **bundle_id**；CLI 包 transient ``Computer.aunmount_server_by_id``，
# 停进程不删声明）。仅对经 §4.9.1-2 回收判据判定**可回收**者调用。
RemoveServer = Callable[[str], Awaitable[None]]
# ``NonPluginBundleIds``（§4.9.1-2 回收判据「X 非用户声明」项的数据源，#164）定义在 :mod:`.reconciler`
# ——判据本体 ``reclaimable_mcp_deps`` 的所在地，且本模块**导入** reconciler（反向导入会成环）。此处仅转导出，
# 使四个回调别名在 installer 的公开面齐整 / re-exported here so the callback aliases read together.
# 注入 plugin-scoped inputs 入池（异步；入参 plugin_root；CLI 包 ``load_plugin_inputs`` → ``Computer.add_or_update_input``）。
# 在 register（→render bundled server 的 ${input:}）之前调，使裸 id 可经 D2 前缀回退解析（#69 Group A，§9.3 D2）。
InjectInputs = Callable[[Path], Awaitable[None]]


# ---------------------------------------------------------------------------
# 内部辅助 / Internal helpers
# ---------------------------------------------------------------------------
def _split_plugin_id(plugin_id: str) -> tuple[str, str]:
    """``<plugin>@<marketplace>`` → ``(plugin, marketplace)``；非法 key → :class:`PluginInstallError`。"""
    if not is_valid_enabled_plugin_key(plugin_id):
        raise PluginInstallError(f"invalid plugin id {plugin_id!r} (expect '<plugin>@<marketplace>', strict kebab, each ≤64)")
    plugin, _, marketplace = plugin_id.partition("@")
    return plugin, marketplace


def _bare_name_conflicts(
    plugin_id: str,
    *,
    facts: _InstallFacts,
    allow_if_installed: bool = False,
) -> list[str]:
    """
    跨 marketplace 裸名唯一性扫描（skill.md §1.2 note / §1.5；#257）/ Cross-marketplace bare-name scan.

    Scans merged ``installedPlugins`` (user/project/local via :func:`resolve_settings`) ∪ materialization-ledger
    pids and returns the installed pids sharing the bare name from **another** marketplace (sorted, self excluded).

    扫描面 = merged ``installedPlugins``（user/project/local，经 :func:`resolve_settings`）∪ 物化账本
    ``installed_plugins.json`` 的 pid 键；返回**同名不同 marketplace** 的已安装 pid（升序、排除自身）。

    ``allow_if_installed``（install 用）：目标 pid 自身已在**物化账本**时直接放行——同一 pid 重装不改安装集、
    不新增第二个 ``<plugin>``，属决策 1 的幂等情形（存量双装 home 里也须可重装/重钉版本）。carve-out
    **只认账本，不认 merged 声明集**：project/local 的纯声明不是安装事实（否则声明一个从未安装的 pid 就能
    把同名第二个装进来）。enable 侧保持 fail-closed（决策 2）。

    ``drop_legacy=False`` 是**承重墙**：账本默认视图会整条丢弃旧格式（``bundledMcpServers``）记录，令
    v0.2.x 存量 home 的 ``alpha@mp-a`` 对检查隐身——安装随后才由 :func:`migrate_legacy_installs` 从账本
    回填意图，正好形成「先绕过、后出现」的旁路。故此处与迁移路径读**同一视图**（只读 pid 键，不碰旧格式值）。

    ``facts`` **必填**：预检与 :func:`_bare_name_conflict_error` 的诊断分档必须共用同一次读取，避免并发
    窗口里「判定用旧快照、文案用新快照」的漂移（#257 复审 🟡3）。不自读 fallback——调用方先算一次再传。
    """
    # 幂等 carve-out 只认**账本记录**（真重装 / 存量双装）。project/local 的纯声明不是安装事实——拿 merged
    # 意图放行等于「声明一个从未安装的 `<plugin>@<mp>` 就能把同名第二个装进来」（#257 复审 🔴3）。
    if allow_if_installed and plugin_id in facts.ledger_pids:
        return []
    return _bare_name_clash_pids(plugin_id, facts.installed_pids)


def _bare_name_clash_pids(plugin_id: str, pids: Iterable[str]) -> list[str]:
    """
    给定 pid 集合，取与 ``plugin_id`` **同裸名、不同 marketplace** 的 pid（升序、排除自身）/
    Bare-name clashes of ``plugin_id`` within ``pids``.

    预检与 in-lock 复核的**唯一判据**（#257 复审 🟡1：两处曾各自实现，锁内漏了账本 carve-out）。
    The single clash predicate shared by the unlocked pre-check and the in-lock recheck.
    """
    plugin, _marketplace = _split_plugin_id(plugin_id)
    return sorted(pid for pid in pids if pid != plugin_id and pid.partition("@")[0] == plugin)


def _bare_name_conflict_error(
    plugin_id: str,
    conflicts: list[str],
    *,
    action: str,
    ledger_pids: frozenset[str],
) -> PluginInstallError:
    """
    构造跨 marketplace 同名冲突的结构化错误（``validation`` + 可判诊断；#257 决策 4/6）/
    Build the structured conflict error.

    **补救按账本分档**（#257 架构师审查 🟡2）：「已安装」以**账本**为准——有物化记录 → ``plugin uninstall``
    有效；无物化记录（project/local 声明、残留意图，或并发窗口里刚写入的意向）→ 删对应 settings 条目 /
    等其完成，``plugin uninstall`` 对这类条目恒为 no-op。
    Remedies are split by ledger presence; a pid without a materialization record cannot be cleared by uninstall.

    :param ledger_pids: 调用方已算出的账本 pid 快照，**必填**——与判定冲突的 `facts` 同源，避免分档时
        重读导致漂移（并发窗口）与持锁期间多余 I/O（#257 复审 🟡3）。不自读 fallback。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    materialized = [pid for pid in conflicts if pid in ledger_pids]
    declared_only = [pid for pid in conflicts if pid not in ledger_pids]
    existing = ", ".join(repr(pid) for pid in conflicts)
    remedies: list[str] = []
    if materialized:
        uninstall = " / ".join(f"'plugin uninstall {pid}'" for pid in materialized)
        remedies.append(f"Uninstall the materialized plugin(s) first ({uninstall})")
    if declared_only:
        declared = " / ".join(repr(pid) for pid in declared_only)
        remedies.append(
            f"{declared} has no materialization record (a user-scope stale intent, a project/local settings "
            f"declaration, or an install still in flight) — remove that settings entry (or retry once the other "
            f"install completes); 'plugin uninstall' cannot clear a declaration"
        )
    message = (
        f"plugin {plugin!r} is already installed or declared from another marketplace ({existing}); a plugin name "
        f"MUST be unique across marketplaces (skill.md §1.5), so {action} of {plugin_id!r} is refused. "
        f"{'; '.join(remedies)}. Or rename the plugin in marketplace {marketplace!r}."
    )
    return PluginInstallError(message, error_code="validation", details={"conflictWith": conflicts[0]})


def _settings_path_for_scope(scope: str, project_path: str | None, env: Mapping[str, str] | None) -> tuple[Path, SettingsScope]:
    """
    解析 enable/disable 写入的 settings.json 路径与 scope 枚举 / Resolve the settings.json path + scope enum to write。

    可写 scope：``user``（默认）/ ``project`` / ``local``（后两者需 ``project_path`` = active workdir）。
    ``managed`` / ``policy`` / 未知 → :class:`PluginInstallError`（policy 为只读治理层，§5.1）。
    """
    if scope == "user":
        return user_settings_path(env), SettingsScope.USER
    if scope in ("project", "local"):
        if not project_path:
            raise PluginInstallError(f"scope {scope!r} requires project_path (active workdir)")
        wd = Path(project_path)
        if scope == "project":
            return workdir_project_settings_path(wd), SettingsScope.PROJECT
        return workdir_local_settings_path(wd), SettingsScope.LOCAL
    raise PluginInstallError(f"cannot write enabledPlugins to scope {scope!r} (writable: user|project|local)")


def _write_enabled_plugin(plugin_id: str, value: bool | None, scope: str, project_path: str | None, env: Mapping[str, str] | None) -> None:
    """
    写 ``enabledPlugins[<plugin_id>] = value`` 到指定 scope 的 settings.json（持锁原子 RMW）/ Write the enabled flag。

    ``value=None`` = 删除该条目（enable 失败回滚恢复 absent / uninstall 清条目，v0.3.0 §2.4）；条目本不存在 → no-op。

    复用 store 原语自组织：:func:`file_lock`（旁车 ``.lock``，建父目录）+ :func:`read_raw_object`（**raw 保真**，
    不经校验过滤视图）+ :func:`apply_write`（递归进 ``enabledPlugins`` 仅改该 key、不毁兄弟；:data:`DELETE` 删 key）+
    :func:`atomic_write_json`（``header=None``——settings.json 是人编意图层，无写保护头 / version 字段，区别于物化文件）。

    **#246**：读取 raw 原始 JSON，使 project 的 ``readRoots`` / ``landingRoot`` / ``enabledMcpjsonServers``、
    policy-only 字段、非法 ``permissions`` 条目等被校验器过滤的字段不再被整文件回写抹掉。目标文件 corrupt /
    ``enabledPlugins`` 非对象 → fail-closed（抛 :class:`PluginInstallError`，原文件逐字节不动）。
    Raw-fidelity RMW; corrupt target or non-object ``enabledPlugins`` fails closed.
    """
    path, _scope_enum = _settings_path_for_scope(scope, project_path, env)
    try:
        with file_lock(path):
            existing = read_raw_object(path) or {}
            # 形状检查**两条分支都要过**：upsert 分支若跳过，apply_write 会把非映射值当「类型切换」整体替换，
            # 等同静默修复（丢掉用户写的畸形内容）——同样违反 raw 保真。
            enabled = mapping_field_or_empty(existing, "enabledPlugins", path=path)
            if value is None:
                if plugin_id not in enabled:
                    return  # 无条目可删（apply_write 对缺失父键会原样写入哨兵，须在此短路）
                updated = apply_write(existing, {"enabledPlugins": {plugin_id: DELETE}})
            else:
                updated = apply_write(existing, {"enabledPlugins": {plugin_id: value}})
            atomic_write_json(path, updated)
    except SettingsFileCorruptError as e:
        raise PluginInstallError(str(e)) from e


def _clear_enabled_entries_visible_layers(plugin_id: str, project_paths: set[str], env: Mapping[str, str] | None) -> None:
    """
    清理 ``enabledPlugins[plugin_id]`` 的全部**可见层**条目：user 恒清；project/local 对「账本 projectPath ∪ cwd」
    逐一清（#125 任务 1：账本重物化归一后丢 projectPath，仅遍历账本会漏掉 cwd 可见层的残留 ``true``——
    卸载后残留会让重装立即激活，违反 install 不激活）/ Clear enabled entries across visible layers。

    project/local 写调用前**先查 settings 文件存在**：:func:`~a2c_smcp.computer.settings.store.file_lock`
    会 ``mkdir(parents=True)`` 建父目录 + 建 ``.lock``，而 :func:`_write_enabled_plugin` 的 no-op 短路在锁后——
    无守卫会在无 ``.tfrobot/`` 的 cwd 制造垃圾目录。managed/policy 只读层不触碰；清失败 WARN 不阻断（best-effort）。

    **user 层也在 best-effort 之内**（#246 独立审查 🔴B）：本函数在 uninstall **已删树/注销 skill/摘 server/删账本
    之后**才被调用，user 层若抛 :class:`PluginInstallError` 会把整个卸载打断在半途，留下 project/local 残留。
    故三层同姿态——失败只 WARN。真正的「目标键畸形」已由 :func:`_precheck_settings_writes` 在任何破坏性动作
    **之前**拦下，这里只是纵深防御。
    """
    targets = {str(Path(p)) for p in project_paths if p}
    targets.add(str(Path.cwd()))
    layers: list[tuple[str, str | None, Path]] = [("user", None, user_settings_path(env))]
    for project_path in sorted(targets):
        for s in ("project", "local"):
            layers.append((s, project_path, _settings_path_for_scope(s, project_path, env)[0]))
    for scope_name, anchor_path, layer_path in layers:
        try:
            if scope_name != "user" and not layer_path.exists():  # 存在性守卫：不为清理凭空建 .tfrobot/ + .lock
                continue
            _write_enabled_plugin(plugin_id, None, scope_name, anchor_path, env)
        except PluginInstallError as e:  # best-effort：清条目失败不阻断
            logger.warning(
                "failed to clear enabledPlugins[%s] in %s scope%s: %s",
                plugin_id,
                scope_name,
                f" at {anchor_path}" if anchor_path else "",
                e,
            )


def _precheck_settings_writes(project_paths: set[str], env: Mapping[str, str] | None) -> None:
    """
    uninstall 的**破坏性动作之前**预检所有待写 settings 文件的目板键形状（#246 独立审查 🟡1）
    Validate every settings file that uninstall will write **before** the irreversible steps.

    为什么必须前置：uninstall 的顺序是「删树 → 注销 skill → 摘 server → 删账本 → 写意图」。若意图写在**最后**
    才因目标键畸形 fail-closed，此时安装已被不可逆删除，而重试又会因账本已空走 no-op——**永远清不干净**。
    故在任何删除之前把**本次真的会写**的目标键形状验一遍：畸形即 :class:`PluginInstallError` 且**零副作用**。

    **检查范围按层收窄**（勿放宽）：``_write_installed_plugin`` 只写 **user** 层，project/local 只经
    :func:`_write_enabled_plugin` 写 ``enabledPlugins``。project/local 里的 ``installedPlugins`` 残留声明是
    只读诊断面（``prune_plugin_intent`` 只 WARN、**绝不改写**），拿它来挡 uninstall 属误伤——与本操作无关的
    畸形字段不该阻塞删除。

    :raises PluginInstallError: 任一**待写**目标键畸形 / 文件 corrupt / 根非对象。
    """
    checks: list[tuple[Path, tuple[str, ...]]] = [(user_settings_path(env), ("installedPlugins", "enabledPlugins"))]
    targets = {str(Path(p)) for p in project_paths if p}
    targets.add(str(Path.cwd()))
    for project_path in sorted(targets):
        for s in ("project", "local"):
            layer_path, _layer_scope = _settings_path_for_scope(s, project_path, env)
            if layer_path.exists():  # 与清理同款存在性守卫：不存在的层不会被写
                checks.append((layer_path, ("enabledPlugins",)))
    try:
        for check_path, keys in checks:
            existing = read_raw_object(check_path) or {}
            for key in keys:
                if key == "installedPlugins":
                    list_field_or_empty(existing, key, path=check_path)
                else:
                    mapping_field_or_empty(existing, key, path=check_path)
    except SettingsFileCorruptError as e:
        raise PluginInstallError(str(e)) from e


# ---------------------------------------------------------------------------
# enable 内容预检（#259）/ Enable content preflight
# ---------------------------------------------------------------------------
def _enable_recovery_hint(plugin_id: str) -> str:
    """
    #259 失败消息的恢复指引 / Recovery guidance for #259 failures。

    措辞须与真实行为一致（审查 🟡3）：``marketplace refresh`` 只做 ``git pull``（甚至不还原本地已删文件）；
    ``plugin install`` 复用既有 clone（``refresh=False``）；boot 重物化仅在记录的 ``installPath`` **根**缺失 /
    不可解析时触发，且同样复用 clone——三者都修不好「根在、文件被删」。故首选是**从干净副本重建**：
    外部独占 clone 走 uninstall+install（uninstall 删树 → install 重 clone）；catalog 相对源走
    marketplace remove+add（重 clone catalog）。
    """
    _plugin, marketplace = _split_plugin_id(plugin_id)
    return (
        f"Recovery: re-materialize from a clean copy, then retry. Plugin fetched into its own clone: run "
        f"'plugin uninstall {plugin_id}' then 'plugin install {plugin_id}'. Plugin shipped by the marketplace "
        f"catalog: run 'marketplace remove {marketplace}', 'marketplace add <source>', then 'plugin install "
        f"{plugin_id}'. 'marketplace refresh {marketplace}' only helps when the upstream repo still has the files; "
        f"a restart only re-materializes records whose recorded install root is missing or unparsable (it reuses "
        f"the existing clone, so deleted files are not restored)."
    )


def _describe_dead_record(rec: object) -> str:
    """失效账本记录 → 「点名路径 + 原因」短语（判定权威是 reconciler 判据，此处只做分档展示）/ Dead-record detail。"""
    if not isinstance(rec, Mapping):
        return f"ledger record is not an object ({type(rec).__name__})"
    scope = rec.get("scope")
    scope_hint = f" (scope={scope})" if isinstance(scope, str) and scope else ""
    install_path = rec.get("installPath")
    if not (isinstance(install_path, str) and install_path):
        return f"ledger record has no installPath{scope_hint}"
    if not Path(install_path).is_dir():
        return f"installPath {install_path}{scope_hint}: directory does not exist"
    # 判据已排除「installPath 缺失」与「目录不存在」两个分支 ⇒ 剩余唯一原因是 bundled JSON 不可解析。
    return f"installPath {install_path}{scope_hint}: bundled server JSON is unparsable"


def _marketplace_entry_for(marketplace: str, plugin: str, home: Path) -> Mapping[str, Any] | None:
    """读 marketplace 条目；catalog 清单缺失/损坏 → ``None``（声明面不可判定）。

    注意（审查 🟡3/🟡7）：stage **不会**因 catalog 清单损坏而抛错——:func:`stage_marketplace_skills` 对
    marketplace 级失败同样走「记 ERROR + 返回空」的静默降级。声明面不可判定时只跳过声明态判据；上述
    SKILL 容器（约定 ``skills/`` 或声明覆写）内确有 ``SKILL.md`` 时，仍由 stage 返回值为空的零注册兜底
    （③.6）捕获——容器之外散落的 ``SKILL.md`` 两个判据都不扫描（与装载同源）。
    """
    try:
        manifest = read_marketplace_manifest(marketplace_skill_dir(home, marketplace))
    except PluginManifestError as e:
        logger.warning(
            "enable preflight: marketplace %r manifest unreadable, content declaration unknown: %s", marketplace, e,
        )
        return None
    return find_plugin_entry(manifest, plugin)


def _json_truthy(value: object) -> bool:
    """JSON 真值判据（对齐 rust ``json_truthy``）：``null``/``false``/``0``/``""``/``[]``/``{}`` 为假，其余为真。

    审查 🟡2：声明态判据必须按**原始 JSON 真值**判，而不是按「能归一出可用路径列表」判——否则
    ``skills: [123]`` / ``{"x": 1}`` 在 rust 侧算「已声明且零投影 → 失败」，Python 侧却算「未声明 → 放行」；
    ``skills: ""`` 则相反。畸形声明同样属于「声明了但不可投影」，应走失败路径。
    """
    if value is None or value is False:
        return False
    if isinstance(value, bool):  # True 在此处恒真（False 已返回）
        return True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (str, list, tuple, dict)):
        return len(value) > 0
    return True


def _declares_skills(entry: Mapping[str, Any] | None, plugin_manifest: Mapping[str, Any]) -> bool:
    """该 plugin 是否**声明**了 SKILL 内容（声明态判据，#259 架构师裁决）。

    声明源 = ``entry.skills``（恒取）+ ``plugin.json.skills``（仅 strict=true），**按 JSON 真值**判
    （见 :func:`_json_truthy`，与 rust ``entry_declares_component`` 同口径）。

    **约定 ``skills/`` 容器存在不算声明**（审查 🔴1）：MCP-only plugin 可能顺带提交 ``skills/README.md`` /
    ``.gitkeep`` / 空目录，把「容器在」当声明会让它在旧版本可启用、新版本永久不可启用且无命令可修。

    **已知边界**（审查 🟡3，与 rust 对齐）：convention-only plugin（entry / plugin.json 均无 skills 声明、
    只有约定 ``skills/`` 目录）在 SKILL.md 被全部删除后 **enable 仍成功**——无从判定「曾经有过」。
    「容器在但零 SKILL.md」不由此判据抓，而由 stage 返回值零注册的兜底（③.6）在**有文件被装载跳过**时抓。
    """
    strict = entry_is_strict(entry) if entry is not None else True
    if _json_truthy((entry or {}).get("skills")):
        return True
    return strict and _json_truthy(plugin_manifest.get("skills"))


def _projectable_skill_count(
    plugin_root: Path, entry: Mapping[str, Any] | None, plugin_manifest: Mapping[str, Any],
) -> int:
    """可投影 SKILL 包数：容器（约定 ``skills/`` + 声明覆写）内 ``<skill>/SKILL.md`` 的个数。

    与装载同源（:func:`~a2c_smcp.computer.skills.manifest.resolve_skill_override_dirs` + 一级目录扫描）。
    任何文件系统异常（权限 / 软链环 / 非法路径）→ 记 WARNING 并计 0（fail-closed，不静默放行；审查 🟡9）。
    """
    strict = entry_is_strict(entry) if entry is not None else True
    try:
        containers = [
            plugin_root / SKILLS_SUBDIR,
            *resolve_skill_override_dirs(entry or {}, plugin_manifest, plugin_root, strict=strict),
        ]
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("enable preflight: cannot resolve SKILL containers under %s, counted as empty: %s", plugin_root, e)
        return 0
    seen: set[Path] = set()
    total = 0
    for container in containers:
        try:
            resolved = container.resolve()
            if resolved in seen or not container.is_dir():
                continue
            seen.add(resolved)
            total += sum(1 for child in container.iterdir() if child.is_dir() and (child / SKILL_MD).is_file())
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("enable preflight: cannot list SKILL container %s, counted as empty: %s", container, e)
    return total


@dataclass(frozen=True)
class _EnablePreflight:
    """enable 内容预检结果 / Enable content preflight outcome。"""

    servers: list[MCPServerConfig]
    # installPath 内确实存在 ``<skill>/SKILL.md`` 包 → stage 后必须为该 plugin 注册到非空技能集
    # （真实装载还会因 frontmatter / 名字 / catalog 清单问题跳过，见 #259 审查 🔴2）。
    expects_skills: bool


def _enable_content_error(plugin_id: str, problems: Sequence[str]) -> str:
    """组装 #259 的公开诊断（点名死路径 + 零变更事实 + 恢复指引）/ Public diagnostic message。

    措辞不得断言 ``installed_disabled``（审查 🟡7）：``enabledPlugins`` 可能原本就是显式 true，本命令只是
    **未改动**它——如实陈述「no changes」并给出当原值为 true 时的补救。
    """
    details = "\n  - ".join(problems)
    return (
        f"cannot enable plugin {plugin_id!r}: its installed content is missing or unusable, so its declared skills / "
        f"bundled servers cannot be projected. This command made no changes (enabledPlugins is unchanged; if it is "
        f"already true for this scope, run 'plugin disable {plugin_id}' first).\n  - {details}\n"
        f"{_enable_recovery_hint(plugin_id)}"
    )


def _preflight_enable_content(
    plugin_id: str,
    plugin: str,
    marketplace: str,
    home: Path,
    records: Sequence[Mapping[str, Any]],
) -> _EnablePreflight:
    """
    enable 内容预检（#259）：账本记录必须仍有效物化，且**声明的内容**必须完整可投影 / Enable content preflight。

    为什么必须前置：旧实现从记录 ``installPath`` 裸读 bundled servers（缺失目录 → 空集）、随后 re-stage 的
    失败又被 :func:`~a2c_smcp.computer.skills.staging.stage_marketplace_skills` 的逐 plugin 降级吞掉，于是写出
    ``enabledPlugins=true`` 而零技能零 server——协议 runtime-contract §2.4 明令禁止的「enabled 但不可用」半态。
    预检在**任何持久化变更之前**失败，且本命令不写任何状态；本函数**不**重物化（重物化归重装 / boot）。

    三条判据：
    1. **物化**：复用 :func:`~a2c_smcp.computer.settings.reconciler.ledger_record_materialized`
       （``installPath`` 目录存在 ∧ bundled JSON 可解析）；多 scope 取 **∀**
       （:func:`~a2c_smcp.computer.settings.reconciler.ledger_entry_fully_materialized`）——与 boot 重物化判据
       同源，任一记录失效即整体失败，不启用健康子集。
    2. **声明面一致**：记录 ``mcpServers``（声明依赖的 bundle_id，经
       :func:`~a2c_smcp.computer.settings.reconciler.ledger_mcp_deps_of` 取值域守卫）必须全部可从该
       ``installPath`` 重新解析，防「根在、部分 bundled 文件被删」静默少挂。
    3. **声明态可投影**（#259 架构师裁决）：manifest/entry 声明了 SKILL 内容（见 :func:`_declares_skills`）
       却零 ``<skill>/SKILL.md`` → 失败；**真正未声明任何内容**的空 plugin 仍允许启用（正对照）。

    预检只做「文件存在」层判定；装载层的更严判据（frontmatter / 重名 / catalog 清单）由 stage 后的
    零注册兜底收口（``_EnablePreflight.expects_skills`` → :func:`enable_plugin` ③.6）。

    :return: 聚合的 bundled servers（供依赖预检与挂载复用，避免二次解析）+ 是否期望 SKILL 投影。
    :raises PluginInstallError: 任一记录不可物化 / 声明 server 不可解析 / 声明 SKILL 零投影。

    .. note::
       声明面取自**活 catalog**（``entry``），内容取自账本 pin 的 ``installPath``。对 sha pin 的外部源，若 catalog
       的 ``skills`` 声明已变而克隆仍钉旧提交，预检会要求先 ``plugin install`` 重装对齐——这是刻意的 fail-fast，
       不是误报（审查 🟢11 记边界）。
    """
    problems: list[str] = []
    try:
        fully_materialized = ledger_entry_fully_materialized(records)
    except OSError as e:
        # 权限等文件系统异常不得裸逸出（审查 🟡7）：归一为结构化错误。
        raise PluginInstallError(
            _enable_content_error(plugin_id, [f"cannot inspect recorded install paths: {e}"]),
        ) from e
    if not fully_materialized:
        for rec in records:
            try:
                if not ledger_record_materialized(rec):
                    problems.append(_describe_dead_record(rec))
            except OSError as e:
                problems.append(f"cannot inspect recorded install path ({rec!r}): {e}")
        if not problems:  # 防御：判据说失效却逐条都通过（不应发生）→ 仍须失败，不得静默放行
            problems.append("no usable ledger record")
        raise PluginInstallError(_enable_content_error(plugin_id, problems))

    entry = _marketplace_entry_for(marketplace, plugin, home)
    # bundle_id → (安装根, 配置)（首次出现顺序）。多 scope **同根同配置**记录重复声明同一 server 时去重；
    # 异根 / 异配置 = identity collapse，硬错（对齐 rust#274；审查 🟡1）。
    by_id: dict[str, tuple[Path, MCPServerConfig]] = {}
    identity_conflicts: list[str] = []
    expects_skills = False
    for rec in records:
        install_path = Path(str(rec.get("installPath")))
        try:
            parsed = load_bundled_servers(install_path)
        except PluginManifestError as e:  # 判据刚通过后文件又被改坏（TOCTOU）→ 仍按失败处理
            problems.append(f"installPath {install_path}: bundled server JSON is unparsable ({e})")
            continue
        except OSError as e:  # iterdir 权限等（审查 🟡7）
            problems.append(f"installPath {install_path}: bundled servers cannot be read ({e})")
            continue
        missing = sorted(ledger_mcp_deps_of([rec]) - {resolve_bundle_id(cfg) for cfg in parsed})
        if missing:
            problems.append(
                f"installPath {install_path}: declared bundled server(s) {', '.join(repr(m) for m in missing)} "
                "cannot be resolved (the install tree is stale; see the recovery steps below)"
            )
        plugin_manifest = read_plugin_metadata(install_path)
        try:
            if entry is not None:
                # strict=false + plugin.json 声明组件 → staging 会**整体拒绝**该 plugin（审查 🟡6）；预检先转结构化错误。
                check_strict_conflict(entry, plugin_manifest)
            declares_skills = _declares_skills(entry, plugin_manifest)
            skill_count = _projectable_skill_count(install_path, entry, plugin_manifest)
        except PluginManifestError as e:
            problems.append(f"installPath {install_path}: plugin manifests conflict ({e})")
            continue
        except (OSError, RuntimeError, ValueError) as e:
            problems.append(f"installPath {install_path}: cannot inspect SKILL packages: {e}")
            continue
        expects_skills = expects_skills or skill_count > 0
        if declares_skills and skill_count == 0:
            problems.append(
                f"installPath {install_path}: declares SKILL packages but none are projectable "
                f"(no <skill>/{SKILL_MD} under the conventional {SKILLS_SUBDIR}/ dir or declared overrides)"
            )
        # 声明了 bundled servers（账本 mcpServers，或 strict plugin.json 的 mcpServers 组件）却零投影 → 失败
        # （架构师审查 🔴1，与 rust#274 `declared_components.contains("mcpServers")` 同口径）。
        strict = entry_is_strict(entry) if entry is not None else True
        declares_servers = bool(ledger_mcp_deps_of([rec])) or (
            strict and "mcpServers" in manifest_declared_components(plugin_manifest)
        )
        if declares_servers and not parsed:
            problems.append(
                f"installPath {install_path}: declares bundled MCP servers but none are projectable "
                f"(no mcp-servers/*.json under the install path)"
            )
        for cfg in parsed:
            bundle_id = resolve_bundle_id(cfg)
            existing = by_id.get(bundle_id)
            if existing is None:
                by_id[bundle_id] = (install_path, cfg)
            elif existing[0] != install_path or existing[1] != cfg:
                identity_conflicts.append(bundle_id)
                problems.append(
                    f"bundle_id {bundle_id!r} collapses distinct identities ({existing[0]} vs {install_path}); "
                    f"refusing to enable an ambiguous projection"
                )
    if problems:
        raise PluginInstallError(
            _enable_content_error(plugin_id, problems),
            # 身份坍缩属可判定的配置冲突类别（对齐既有 validation / conflictWith 约定，审查 🟡1）。
            error_code="validation" if identity_conflicts else None,
            details={"conflictWith": identity_conflicts[0]} if identity_conflicts else None,
        )
    return _EnablePreflight(servers=[cfg for _root, cfg in by_id.values()], expects_skills=expects_skills)


def _write_installed_plugin(
    plugin_id: str,
    present: bool,
    env: Mapping[str, str] | None,
    *,
    home: Path | None = None,
    guard_bare_name: bool = False,
) -> bool:
    """
    user scope ``installedPlugins`` 数组的持锁 RMW（增/删一个条目）；返回**写前**是否已含该条目 / RMW the install intent。

    安装是全局一次的事实（协议 v0.3.0 §2.1/§2.4「plugin_installation」）→ 意图固定落 **user** settings.json
    （#123 决策：不随 ``--scope`` 泄漏进可提交的 project 文件）；读取侧由 :func:`resolve_settings` merged
    并集承担（project/local 声明亦被认可，支持声明式复现）。数组写回整体替换（§5.4 写语义），保序去重。

    **锁内裸名复核**（``guard_bare_name=True``，install 专用；#257 架构师审查 🔴1）：install 的预检
    （:func:`_bare_name_conflicts`）是无锁读，与本次写入之间隔着迁移等步骤——两个进程可各自通过预检后再
    先后持锁追加，造出「同名双装」。故在**同一把锁内**、``append`` 之前用刚读到的 ``entries``（已含并发
    进程刚写入的 pid）∪ 账本 pid 复核一次；冲突则抛 :class:`PluginInstallError`（文件未写）。
    **与预检同款 carve-out**：目标 pid 已在账本 = 真重装（不改变安装集），整段复核跳过——勿把它当漏检「修掉」，
    否则会误拒预检刻意放行的重装（#257 复审 🟡1）。
    **不得在外层再包一层 ``file_lock``**：锁基于旁车文件的新 fd，同进程二次获取会退避后抛 ``SettingsLockError``。

    :raises ValueError: ``guard_bare_name=True`` 但未传 ``home``（无法读账本做跨进程兜底；安全守卫不得静默
        fail-open，生产调用点恒传 ``home``，此错仅防未来误用）。

    **#246**：raw 保真 RMW——只改 ``installedPlugins``，其余字段（含 policy-only 越权字段 / 未知键）逐字保留。
    目标文件 corrupt / ``installedPlugins`` 非数组 → fail-closed（抛 :class:`PluginInstallError`，原文件不动）。
    """
    path = user_settings_path(env)
    try:
        with file_lock(path):
            existing = read_raw_object(path) or {}
            entries: list[Any] = list_field_or_empty(existing, "installedPlugins", path=path)
            was_present = plugin_id in entries
            if present and not was_present:
                if guard_bare_name:
                    if home is None:
                        # 安全守卫不得静默 fail-open（#257 复审 🟡6）：缺 home 就无法做跨进程兜底，直接报错。
                        raise ValueError("_write_installed_plugin(guard_bare_name=True) requires home for the ledger recheck")
                    ledger = frozenset(_ledger_pids(home=home, env=env))
                    # 与预检**同一 carve-out**（#257 复审 🟡1）：目标 pid 已有物化记录 = 真重装（不改安装集）。
                    # 抢跑窗口里目标 pid 尚未入账本，复核照旧触发，不削弱并发闭合。
                    if plugin_id not in ledger:
                        candidates = {e for e in entries if isinstance(e, str) and is_valid_enabled_plugin_key(e)}
                        clashes = _bare_name_clash_pids(plugin_id, candidates | ledger)
                        if clashes:
                            raise _bare_name_conflict_error(plugin_id, clashes, action="install", ledger_pids=ledger)
                entries.append(plugin_id)
            elif not present and was_present:
                entries = [e for e in entries if e != plugin_id]
            atomic_write_json(path, apply_write(existing, {"installedPlugins": entries}))
            return was_present
    except SettingsFileCorruptError as e:
        raise PluginInstallError(str(e)) from e


def _ledger_pids(*, home: Path, env: Mapping[str, str] | None) -> set[str]:
    """物化账本中的 pid 键 / Materialization-ledger plugin ids（``drop_legacy=False``：旧格式记录也是安装事实）。"""
    return {
        pid
        for pid in load_installed_plugins(home=home, env=env, drop_legacy=False).get("plugins", {})
        if is_valid_enabled_plugin_key(pid)
    }


@dataclass(frozen=True, slots=True)
class _InstallFacts:
    """安装事实集 / Install facts：merged 意图 pid 与物化账本 pid（#257 架构师审查 🟡4 的单一权威）。"""

    declared_pids: frozenset[str]
    ledger_pids: frozenset[str]

    @property
    def installed_pids(self) -> frozenset[str]:
        """安装集 = 声明意图 ∪ 账本 / Declared intents ∪ ledger."""
        return self.declared_pids | self.ledger_pids


def _install_facts(*, home: Path, env: Mapping[str, str] | None) -> _InstallFacts:
    """
    安装事实集的**单一权威**：merged ``installedPlugins``（user/project/local）∪ 物化账本 pid /
    Single authority for install facts.

    ``drop_legacy=False``（见 :func:`_ledger_pids`）：旧格式记录也是安装事实，默认视图会让 v0.2.x 存量
    在检查时隐身。install/enable 的预检与冲突诊断分档共用本函数，避免两处各自拼同一并集而漂移。

    .. note::
       install 的**锁内复核**刻意不走本函数：它必须用锁内刚读到的 ``entries`` 快照（含并发进程刚写入的
       user 意图）∪ 账本 pid，而非另一时刻的 merged 视图——两处共享的是 :func:`_bare_name_clash_pids`
       判据与账本 carve-out，不是同一份快照。
    """
    declared = resolve_settings(env=env).settings
    return _InstallFacts(
        declared_pids=frozenset(declared_installed_plugin_ids(declared)),
        ledger_pids=frozenset(_ledger_pids(home=home, env=env)),
    )


def _plugin_skill_state(registry: SkillRegistry, marketplace: str, plugin: str) -> dict[str, bool]:
    """
    本 plugin 名下条目的 ``name → orphaned`` 快照（**含孤儿**）/ Snapshot name→orphaned including orphans.

    Rollback baseline for a failed enable (#257 review 🟡5): an active-only baseline would treat a
    *pre-existing* orphan as "newly added" and ``unregister`` it, so the failed command would not be a no-op.

    enable 失败回滚的基线（#257 复审 🟡5）：只用活跃集做基线会把「调用前就存在的孤儿」当成本次新增而
    ``unregister``，令失败命令的净效果不为零（少一条既有条目）。
    """
    want_source = f"{SOURCE_MARKETPLACE}:{marketplace}"
    prefix = f"{plugin}:"
    out: dict[str, bool] = {}
    for ref, orphaned in registry.all_refs():
        name = ref.get("name")
        if isinstance(name, str) and ref.get("source") == want_source and name.startswith(prefix):
            out[name] = orphaned
    return out


def _plugin_skill_names(registry: SkillRegistry, marketplace: str, plugin: str, *, include_orphans: bool = False) -> list[str]:
    """
    某 plugin 名下的 marketplace SKILL name / A plugin's marketplace SKILL names。

    过滤 ``A2CSkillRef.source == "marketplace:<mp>"`` 且 ``name`` 以 ``<plugin>:`` 起（marketplace name = ``<plugin>:<skill>``）。
    默认仅枚举活跃集（:meth:`SkillRegistry.active_refs` 排除孤儿）；``include_orphans=True`` 时连孤儿一并返回
    （uninstall 用：孤儿残留会被跨 source 守卫当作「先到者」永久拒绝新 source，必须一并清掉，#257 审查 🔴2）。
    """
    want_source = f"{SOURCE_MARKETPLACE}:{marketplace}"
    prefix = f"{plugin}:"
    refs = [ref for ref, _orphaned in registry.all_refs()] if include_orphans else registry.active_refs()
    names: list[str] = []
    for ref in refs:
        if ref.get("source") != want_source:
            continue
        name = ref.get("name")
        if isinstance(name, str) and name.startswith(prefix):
            names.append(name)
    return names


def _source_conflict_error(
    plugin_id: str,
    conflicts: Sequence[SourceConflict],
    *,
    home: Path,
    env: Mapping[str, str] | None,
) -> PluginInstallError:
    """
    把「本次 staging 真正被拒的跨 source ref」转成结构化错误（#257 复审 🔴2）/ Turn refused refs into a structured error.

    ``plugin uninstall`` only works on an **installed** conflicting plugin; when enable reaches here the
    same-name installed pid has already been rejected by the pre-check, so the owner is usually **not
    installed** (an in-memory registration left by ``marketplace add``'s full staging) — the remedy must be
    chosen accordingly, otherwise it is a permanent no-op.

    ``plugin uninstall`` 只对**已安装**的冲突方有效；而 enable 走到本分支时，同名的已安装 pid 已被前置检查
    拦下，冲突方通常是**未安装**的（``marketplace add`` 全量 stage 留下的内存注册）——文案必须据此分档，
    否则给出的是永远 no-op 的补救。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    source_prefix = f"{SOURCE_MARKETPLACE}:"
    owners = sorted(
        {
            src[len(source_prefix):]
            for conflict in conflicts
            for src in (conflict.existing_source,)
            if isinstance(src, str) and src.startswith(source_prefix)
        }
    )
    names = ", ".join(repr(conflict.name) for conflict in conflicts)
    if not owners:
        message = (
            f"skills of plugin {plugin!r} ({names}) are registered by another source; refusing to enable {plugin_id!r}. "
            f"Remove the shadowing source registration (or restart the Computer), then retry."
        )
        return PluginInstallError(message, error_code="validation")
    installed = _install_facts(home=home, env=env).installed_pids
    hints: list[str] = []
    for owner in owners:
        pid = f"{plugin}@{owner}"
        if pid in installed:
            hints.append(f"'plugin uninstall {pid}'")
        else:
            hints.append(
                f"'marketplace remove {owner}' (removes that marketplace's other plugins too), or restart the "
                f"Computer (stale in-memory registrations of a marketplace that is not installed)"
            )
    message = (
        f"skills of plugin {plugin!r} ({names}) are currently registered from another marketplace "
        f"({', '.join(repr(owner) for owner in owners)}), refusing to enable {plugin_id!r}. Resolve by: "
        f"{'; '.join(hints)}. Then retry 'plugin enable {plugin_id}' to adopt the copy from {marketplace!r}."
    )
    return PluginInstallError(message, error_code="validation", details={"conflictWith": f"{plugin}@{owners[0]}"})


def _resolve_marketplace_source(marketplace: str, home: Path, env: Mapping[str, str] | None) -> tuple[Mapping[str, Any], str | None]:
    """从 ``known_marketplaces.json`` 取 marketplace 的 git source + commitSha；未添加 → :class:`PluginInstallError`。"""
    known = load_known_marketplaces(home=home, env=env)
    record = known.get("marketplaces", {}).get(marketplace)
    if record is None:
        raise PluginInstallError(f"marketplace {marketplace!r} not added (run 'marketplace add' first)")
    source = record.get("source")
    if not isinstance(source, Mapping):
        raise PluginInstallError(f"marketplace {marketplace!r} has no valid source record")
    commit_sha = record.get("commitSha")
    return source, commit_sha if isinstance(commit_sha, str) else None


def _log_satisfied_deps(servers: list[MCPServerConfig], existing: set[str]) -> None:
    """
    依赖预检（协议 §2.5-1，**只提示不拒绝**）/ Dependency precheck: report, never reject。

    同 ``bundle_id`` 本地已有 = **依赖已满足** → 复用既有实例、按来源优先序 reconcile、卸载本 plugin 不移除它。
    **MUST NOT 拒绝安装**——plugin 与 MCP Server 是**依赖关系而非所有关系**（§2.5），历史的
    ``MCPServerNameConflictError``「外来同名硬抛、name 即身份」已被协议正面推翻（Discussion #23 / D3），
    连同 ``owned``（所有权白名单）概念一并退役：依赖声明没有所有权，故无需区分「自有」与「外来」。
    display name 相同、``bundle_id`` 不同是**合法共存**（§5.6），MUST NOT 视为冲突。

    **只记日志、无返回值**：面向用户的提示由 CLI 层的 ``_satisfied_deps`` / ``_print_satisfied_deps`` 承担
    （enable 路径 MUST 在挂载**之前**算——挂载会改变活跃集，事后算分不清「本来就有」与「刚被自己挂上」）。
    """
    for cfg in servers:
        bid = resolve_bundle_id(cfg)
        if bid in existing:
            logger.info(
                "dependency satisfied: MCP server %r already exists locally; reusing it rather than creating a new one "
                "(configs reconcile by scope precedence; uninstalling this plugin will not remove it)",
                bid,
            )


def _require_existing_ids_guard(
    existing_bundle_ids: ExistingBundleIds | None,
    register_server: RegisterServer | None,
) -> None:
    """
    护栏：给了 ``register_server`` 就**必须**给 ``existing_bundle_ids`` / Guard: register implies existing-ids。

    D3 后 ``existing`` 不再是「拒绝闸门」而是 **skip-or-register 的判据**（§2.5-1「依赖已满足 → 复用既有实例」）：
    缺它则以 ``existing=∅`` 运行 ⇒ 全量 register ⇒ 把用户既有的同 bundle_id server **静默覆盖**（manager 对同
    bundle_id 是覆盖更新、不抛）。三注入回调应「齐备或全无」，此处强制 register→existing。
    """
    if register_server is not None and existing_bundle_ids is None:
        raise PluginInstallError(
            "existing_bundle_ids is required when register_server is given "
            "(else a dependency-satisfied server would be silently overwritten instead of reused; protocol §2.5-1)",
        )


# ---------------------------------------------------------------------------
# install / uninstall / enable / disable
# ---------------------------------------------------------------------------
async def materialize_plugin(
    plugin_id: str,
    home: Path,
    *,
    scope: str = "user",
    project_path: str | None = None,
    version: str | None = None,
    refresh: bool = False,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    existing_bundle_ids: ExistingBundleIds | None = None,
) -> InstalledPluginRecord:
    """
    物化单个 plugin（clone/定位 + manifest 校验 + 依赖预检 + 写账本；**零激活**）/ Materialize one plugin。

    协议 v0.3.0 §2.3「Fetch 资产：意图 → 物化账本 → 克隆缓存 → 活跃集」中**物化账本**一环的执行体：
    供 :func:`install_plugin`（显式安装）与治理启动恢复（账本删除后由 ``installedPlugins`` 意图重建，
    §4.9「删除无损」）复用。不 stage SKILL、不挂 server、不写任何 settings 意图。

    顺序（§10.6「预检-先于-变更」）/ Order:
    1. 校验 ``plugin_id`` → ``(plugin, mp)``；从 ``known_marketplaces.json`` 取 mp source（未添加→抛）。
    2. 要求 catalog clone 已存在（``marketplace add`` 前置；缺失→抛）；读 marketplace.json 定位 entry。
    3. :func:`locate_plugin_root` 定位 plugin 根（必要时 clone 外部 plugin；失败→抛，零变更）。
    4. strict 冲突检测（§4.4，#80）+ :func:`load_bundled_servers` 全量解析（任一畸形→抛，写账本前）。
    5. **依赖预检**（协议 §2.5-1）：同 ``bundle_id`` 已存在 = 依赖已满足 → **提示、不拒绝**（历史的外来同名
       硬抛已被 D3 推翻，见 :func:`_log_satisfied_deps`）；``existing_bundle_ids=None``（boot 重物化等无 live
       manager 场景）→ 跳过预检。**本步骤零副作用**——install ⊥ activate，挂载归 :func:`enable_plugin`。
    6. 写 ``installed_plugins.json``（仅全成功）：scope / installPath / version / commitSha /
       installedAt / lastUpdated / **mcpServers（bundle_id 数组，§4.9.1-1）**。

    :param scope: 物化记录 scope（``managed|user|project|local``，默认 ``user``）。
    :param version: 记录版本覆盖（``--version``）；缺省按 entry > plugin.json > commitSha 解析。
    :return: 写入的 :class:`InstalledPluginRecord`。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    _source, commit_sha = _resolve_marketplace_source(marketplace, home, env)

    catalog_dir = marketplace_skill_dir(home, marketplace)
    if not catalog_dir.is_dir():
        raise PluginInstallError(f"marketplace {marketplace!r} catalog not cloned at {catalog_dir} (run 'marketplace add/refresh' first)")

    manifest = read_marketplace_manifest(catalog_dir)
    entry = find_plugin_entry(manifest, plugin)
    if entry is None:
        raise PluginInstallError(f"plugin {plugin!r} not found in marketplace {marketplace!r} manifest")

    # 3-4：定位 + 解析（写账本前，畸形即抛 → 原子前置）
    plugin_root, version_fallback = await locate_plugin_root(
        marketplace,
        plugin,
        entry,
        catalog_dir,
        plugin_root_base(manifest),
        home,
        commit_sha,
        refresh=refresh,
        timeout=timeout,
        env=env,
    )
    # plugin.json 读一次复用（strict 检测 + version 解析共用，与 staging._stage_one_plugin 姿态对齐，避免重复读盘）。
    plugin_manifest = read_plugin_metadata(plugin_root)
    try:
        check_strict_conflict(entry, plugin_manifest)
    except PluginManifestError as e:
        raise PluginInstallError(str(e)) from e
    servers = load_bundled_servers(plugin_root)

    # 5：依赖预检（§2.5-1，只提示不拒绝；零变更）
    existing = existing_bundle_ids() if existing_bundle_ids is not None else set()
    _log_satisfied_deps(servers, existing)

    # 6：写账本（仅全成功）
    mcp_deps = [resolve_bundle_id(cfg) for cfg in servers]
    resolved_version = version if version else resolve_plugin_version(entry, plugin_manifest, version_fallback)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    record: InstalledPluginRecord = {"scope": scope, "installPath": str(plugin_root)}
    if project_path:
        record["projectPath"] = str(Path(project_path))
    if resolved_version:
        record["version"] = resolved_version
    if version_fallback:
        record["commitSha"] = version_fallback
    record["installedAt"] = now
    record["lastUpdated"] = now
    # 无条件写（空则 ``[]`）：格式明确、与 rust-sdk#139 磁盘形态逐字节一致，且「有该键」= 新格式正向标志。
    record["mcpServers"] = mcp_deps

    def _put(data: InstalledPluginsFile, _rec: InstalledPluginRecord = record, _pid: str = plugin_id) -> None:
        plugins = data["plugins"]
        # 多 scope 数组：替换同 scope 记录、保留其它 scope（v0.2.1 常见单元素）。
        kept = [r for r in plugins.get(_pid, []) if r.get("scope") != _rec["scope"]]
        kept.append(_rec)
        plugins[_pid] = kept

    update_installed_plugins(_put, home=home, env=env)
    return record


async def install_plugin(
    plugin_id: str,
    registry: SkillRegistry,  # noqa: ARG001 - 签名与 uninstall/enable/disable 对称；v0.3.0 install 不再触碰 Registry
    home: Path,
    *,
    scope: str = "user",
    project_path: str | None = None,
    version: str | None = None,
    refresh: bool = False,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    existing_bundle_ids: ExistingBundleIds | None = None,
) -> InstalledPluginRecord:
    """
    显式安装单个 plugin = config-first 写意图 + 物化；**不激活**（v0.3.0 §2.4 install 行）/ Install ≠ activate。

    顺序 / Order:
    1. cheap 预检（零变更）：``plugin_id`` 形态、marketplace 已添加、catalog 已 clone、manifest entry 存在。
    2. **config-first**（§2.3）：把 ``plugin_id`` 写入 **user scope** ``installedPlugins``（全局安装意图）。
    3. :func:`materialize_plugin` 物化（clone/strict/bundled 解析/依赖预检/写账本）。
    4. 物化失败 → **原子回滚**：撤销第 2 步新写的意图条目（重装场景原已在 → 保留）→ 上抛（#123 决策 #3：
       「失败 = 零变更」；协议对 install 失败未置可否，本 SDK 取更强不变量，rust-sdk#103 镜像）。

    结果态 ``installed_disabled``：**不** stage SKILL、**不**写 ``enabledPlugins``、**不**挂 bundled server——
    显式 :func:`enable_plugin` 才原子点亮（skills 与 server 一并）。

    :param registry: 与其余三动词对称保留；install 自 v0.3.0 起不触碰 Registry。
    :param scope: 物化记录 scope（``managed|user|project|local``，默认 ``user``）——只影响账本记录归档，
        **不**影响意图落点（安装是全局一次的事实，意图恒写 user scope）。
    :param existing_bundle_ids: 可选依赖预检输入（``None`` = 跳过预检）。预检**只提示不拒绝**（§2.5-1），
        故此处缺省不影响正确性；真正消费 ``existing`` 做 skip-or-register 决策的是 :func:`enable_plugin`。
    :return: 写入的 :class:`InstalledPluginRecord`。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)

    # 1：cheap 预检（不写任何状态；深校验交给 materialize）——保持 fail-fast 零副作用
    _resolve_marketplace_source(marketplace, home, env)
    catalog_dir = marketplace_skill_dir(home, marketplace)
    if not catalog_dir.is_dir():
        raise PluginInstallError(f"marketplace {marketplace!r} catalog not cloned at {catalog_dir} (run 'marketplace add/refresh' first)")
    try:
        manifest = read_marketplace_manifest(catalog_dir)
    except PluginManifestError as e:
        # #263 四轮：catalog 的 marketplace.json 损坏/缺失同属本地配置问题 → 结构化错误（不 traceback）。
        raise PluginInstallError(
            f"marketplace {marketplace!r} manifest invalid: "
            f"{sanitize_text_for_diagnostics(str(e))}"
        ) from None
    if find_plugin_entry(manifest, plugin) is None:
        raise PluginInstallError(f"plugin {plugin!r} not found in marketplace {marketplace!r} manifest")

    # 1.4：跨 marketplace 裸名唯一性（skill.md §1.5；#257 / conformance §5）。必须早于下面任何写盘——
    #      含 :func:`migrate_legacy_installs` 的迁移标记键（那时才从账本回填意图的存量插件才会「冒出来」）。
    facts = _install_facts(home=home, env=env)
    conflicts = _bare_name_conflicts(plugin_id, facts=facts, allow_if_installed=True)
    if conflicts:
        raise _bare_name_conflict_error(plugin_id, conflicts, action="install", ledger_pids=facts.ledger_pids)

    # 1.5：预检过后、写意图键之前，先跑一次性迁移（幂等、标记键短路）。install 会写 installedPlugins 键——
    #      若迁移尚未发生（如升级后未经 boot 的 headless CLI install），先写键会把迁移标记误置为"已迁移"，
    #      永久丢弃 v0.2.x 存量活跃态（隔离审查 🔴#2 + N1：置于预检后保 fail-fast 无副作用）。
    migrate_legacy_installs(home, env=env)

    # 2：config-first 写全局安装意图（快照写前状态供原子回滚：重装时原已在 → 失败不误删）
    was_present = _write_installed_plugin(plugin_id, True, env, home=home, guard_bare_name=True)

    # 3-4：物化；失败回滚意图（best-effort，不掩盖原异常）
    try:
        record = await materialize_plugin(
            plugin_id,
            home,
            scope=scope,
            project_path=project_path,
            version=version,
            refresh=refresh,
            timeout=timeout,
            env=env,
            existing_bundle_ids=existing_bundle_ids,
        )
    except Exception as e:
        if not was_present:
            try:
                _write_installed_plugin(plugin_id, False, env)
            except Exception as rollback_exc:  # noqa: BLE001 - 回滚 best-effort
                logger.warning("install rollback: failed to revert installedPlugins entry %r: %s", plugin_id, rollback_exc)
        # #263：source / staging / manifest 非法属本地配置问题（非崩溃）→ 结构化错误上抛，
        # CLI 经 _plugin_error 输出（无 traceback）。消息只取各异常**已脱敏**的文本，不在此重新拼接原始值。
        if isinstance(e, SkillSourceError):
            raise PluginInstallError(f"marketplace {marketplace!r} source invalid: {e}") from None
        if isinstance(e, SkillStagingError):
            raise PluginInstallError(
                f"marketplace {marketplace!r} plugin staging failed: "
                f"{sanitize_text_for_diagnostics(str(e))}"
            ) from None
        if isinstance(e, PluginManifestError):
            raise PluginInstallError(
                f"marketplace {marketplace!r} plugin manifest invalid: "
                f"{sanitize_text_for_diagnostics(str(e))}"
            ) from None
        raise

    logger.info(
        "installed plugin %r (MCP deps=%s; installed_disabled, run 'plugin enable' to activate)",
        plugin_id,
        record.get("mcpServers") or "none",
    )
    return record


async def uninstall_plugin(
    plugin_id: str,
    registry: SkillRegistry,
    home: Path,
    *,
    non_plugin_bundle_ids: NonPluginBundleIds,
    scope: str | None = None,
    keep_servers: bool = False,
    env: Mapping[str, str] | None = None,
    remove_server: RemoveServer | None = None,
) -> bool:
    """
    卸载单个 plugin（删 plugin 独占外部 clone + 注销 skills + **按判据回收** MCP 依赖 + 删账本记录）/ Uninstall。

    **MCP 依赖回收**（协议 §4.9.1-2，#153/D3+F1）：plugin 声明依赖的 server **不是**它的所有物，故卸载
    **不无条件收回**——逐个过 :func:`~a2c_smcp.computer.settings.reconciler.reclaimable_mcp_deps`：
    **回收 X ⟺ 无其他 plugin 声明依赖 X ∧ X 非用户声明**。由此「用户自有 server 永不连坐」与「多 plugin 共享
    依赖不被提前摘除、最后一个依赖者卸载时回收（无泄漏）」两条同时成立。``--keep-servers`` 跳过整个回收环节。


    **v0.3.0 §2.4 uninstall 行**：当该 pid 的账本记录**全部**移除（回 ``available``）时，同步删除 user scope
    ``installedPlugins`` 意图条目 + 清 ``enabledPlugins`` 条目（user 必清；给了 ``project_path`` 时 project/local
    一并 best-effort——managed/policy 只读层不触碰）。指定 ``scope`` 仅删该 scope 记录且其余 scope 仍在 →
    意图与 enabled 条目保留（安装事实未消失）。

    :func:`gc_plugins` 的显式单 plugin 对应物。``--keep-servers`` 跳过 server 摘除（保留 config）。
    ``scope=None`` 删该 id 全部记录；指定 scope 仅删该 scope 记录（其余 scope 保留）。未安装 → ``False``（no-op）。

    :raises PluginInstallError: 独占外部 clone 删除失败（软链重定向 / OSError）——账本与安装意图保持不变，
        残留 clone 仍有索引可重试；调用方 MUST NOT 当作 no-op。

    .. note::
       **文件归属（#258 方案 A，对齐 rust-sdk#271）**：只有 **plugin 独占的外部 clone**
       ``<home>/marketplace/.plugins/<mp>/<plugin>`` 会被删除（:func:`remove_plugin_install_path`；``git-subdir``
       删整个 clone 根），且仅在**没有 retained 记录**（指定 scope 之外的其余 scope）的 ``installPath`` 仍引用该
       clone 时删除。相对源（``source: "./plugins/x"``）的 ``installPath`` 位于**共享 catalog clone**
       ``<home>/marketplace/<mp>/…``——该树是 marketplace 级缓存，兄弟 plugin、剩余 scope 与重装都依赖它，
       MUST NOT 在此删除；归 ``marketplace remove`` / ``prune``（reconcile 仅在 ``sourceChanged`` 整树重 clone 时
       清理）清理。``marketplace refresh`` **不**保证
       还原工作树内被删文件（上游未变时 ``git pull --ff-only`` 不重建已删路径），恢复须 ``marketplace remove`` +
       ``add``。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    # ``drop_legacy=False``（#257 审查 🔴3）：v0.2.x 旧格式账本记录在默认视图里被整条丢弃 → 早返回 no-op，
    # 令冲突诊断给出的 ``plugin uninstall`` 补救在 headless CLI 上永远无效（migrate 在早返回之后）。
    installed = load_installed_plugins(home=home, env=env, drop_legacy=False)
    records = installed.get("plugins", {}).get(plugin_id)
    if not records:
        logger.info("uninstall: plugin %r not installed (no-op)", plugin_id)
        return False

    targeted = [r for r in records if scope is None or r.get("scope") == scope]
    if not targeted:
        logger.info("uninstall: plugin %r has no record in scope %r (no-op)", plugin_id, scope)
        return False

    # no-op 早返回之后、任何变更之前，先跑一次性迁移（幂等）：uninstall 也写 installedPlugins 键
    # （同 install 的标记误置风险，防迁移被抢跑关闭；置于此处保「未安装 = 真 no-op 零写盘」，审查 N1）。
    migrate_legacy_installs(home, env=env)

    # ① 停摘候选 + 回收判定，**先于任何删除**取得（§4.9.1-3：名单 MUST 在账本记录移除之前取得，且 MUST 仅依赖
    #    账本自身字段——与 installPath 指向的树是否删除无关；#258 起 catalog 内的树不再被本函数删除）。
    #    ``retained``：scoped uninstall 时本 pid 其余 scope 的记录仍声明依赖 ⇒ 仍算依赖者，不可回收。
    deps = sorted(ledger_mcp_deps_of(targeted))
    retained = [r for r in records if r not in targeted]
    ledger_plugins = installed.get("plugins", {})
    reclaim = reclaimable_mcp_deps(
        deps,
        other_deps=other_plugin_mcp_deps(ledger_plugins, exclude_pid=plugin_id, retained_records=retained),
        user_declared=non_plugin_bundle_ids(),
    )

    # ①.5 破坏性动作之前预检待写 settings 的目标键形状（#246 审查 🟡1）：畸形 → 零副作用 fail-closed，
    #      否则会在「已删树」之后才抛错，而重试因账本已空走 no-op → 永远清不干净。
    _precheck_settings_writes(
        {p for r in targeted if isinstance(p := r.get("projectPath"), str) and p},
        env,
    )

    # #258：按「引用」判定而非记录数——仅当**没有 retained 记录**的 installPath 落在该 plugin 独占外部 clone
    # 内时才删除。跨 scope 源类型变化（user=Git 源 / project=相对源）按记录数门控会在先卸 user 时误保留 clone，
    # 随后卸 project 又不再命中 → 永久泄漏（对齐 rust-sdk#271 复审终版；多 scope 的 skills/servers 拆除另案 #262）。
    # 口径与 rust 终版一致：缺 / 空 / 非 str / 损坏或相对路径的 retain 记录**不**算引用（删除侧另有 canonicalize
    # + Home 守卫兜底）；归属判定本身纯词法，不解析 installPath 链上的软链。
    ext_clone_still_referenced = any(
        is_external_plugin_install_path(Path(ip), home, marketplace, plugin)
        for r in retained
        if isinstance(ip := r.get("installPath"), str) and ip
    )

    # ② 删树 → ③ 注销 skills → ④ 回收依赖 → ⑤ 删账本记录（⑤ 恒在末位，勿前移，见 §4.9.1-3）
    if not ext_clone_still_referenced:
        for rec in targeted:
            install_path = rec.get("installPath")
            if isinstance(install_path, str) and install_path:
                # 只删 plugin 独占外部 clone；共享 catalog 内的相对源落点返回 None（按设计保留）。
                try:
                    remove_plugin_install_path(Path(install_path), home, marketplace, plugin)
                except InstallPathRemovalError as e:
                    # 删除失败（软链重定向 / resolve 异常 / rmtree OSError）→ 抛错并保持账本与意图不变；
                    # 布尔 ``False`` 的公开契约仍只表示「未安装 no-op」，避免调用方误报 not installed。
                    raise PluginInstallError(
                        f"failed to remove exclusive clone for {plugin_id!r}: {e}; ledger and intent preserved",
                    ) from e

    # 孤儿一并注销（#257 审查 🔴2）：``disable → uninstall`` 的残留孤儿会被跨 source 守卫当作「先到者」，
    # 令后续换源 install/enable 永久失败且本进程内无命令可清。uninstall 是「该 plugin 彻底消失」，含孤儿才闭环。
    for name in _plugin_skill_names(registry, marketplace, plugin, include_orphans=True):
        registry.unregister(name)

    if not keep_servers and remove_server is not None:
        for bundle_id in reclaim:
            await remove_server(bundle_id)

    def _drop(data: InstalledPluginsFile, _pid: str = plugin_id, _scope: str | None = scope) -> None:
        plugins = data["plugins"]
        if _scope is None:
            plugins.pop(_pid, None)
            return
        remaining = [r for r in plugins.get(_pid, []) if r.get("scope") != _scope]
        if remaining:
            plugins[_pid] = remaining
        else:
            plugins.pop(_pid, None)

    update_installed_plugins(_drop, home=home, env=env)

    # v0.3.0：账本记录清空（回 available）→ 删全局安装意图 + 清 enabledPlugins 条目（意图是唯一权威，§2.3）。
    # project/local 落点 = 被删记录的 projectPath ∪ cwd（#125 任务 1：归一记录丢 projectPath 时 cwd 可见层兜底）。
    remaining = load_installed_plugins(home=home, env=env).get("plugins", {}).get(plugin_id)
    if not remaining:
        _write_installed_plugin(plugin_id, False, env)
        project_paths = {p for r in targeted if isinstance(p := r.get("projectPath"), str) and p}
        _clear_enabled_entries_visible_layers(plugin_id, project_paths, env)
    logger.info(
        "uninstalled plugin %r (scope=%s, MCP deps declared=%s, reclaimed=%s)",
        plugin_id,
        scope or "all",
        deps or "none",
        "kept" if keep_servers else (reclaim or "none"),
    )
    return True


def prune_plugin_intent(plugin_id: str, home: Path, *, env: Mapping[str, str] | None = None) -> bool:
    """
    prune 单个**悬挂安装意图**（``installedPlugins`` 声明 ∧ 无有效物化 ∧ 静态不可达，#125 任务 2）/ Prune intent。

    :func:`~a2c_smcp.computer.settings.reconciler.list_dangling_plugin_intents` 的执行对应物——针对悬挂意图的
    uninstall 等价物（无 MCP/SKILL 面：悬挂 pid 从未物化成功，无已挂 server、无已 stage skill）。删的是
    **权威意图**（§2.3），调用方（CLI ``plugin gc``）须过 confirm 门 / 显式 ``--prune-dangling``
    （§4.8.4 删除走显式路径）。settings 意图写权归 installer（与 install/uninstall 同边界）。

    顺序 / Order:
    1. 一次性迁移先行（写 ``installedPlugins`` 键前防迁移标记误置，同 install/uninstall 铁律）；
    2. 删 user 层安装意图；3. 清可见层 ``enabledPlugins`` 条目（user + 账本 projectPath ∪ cwd）；
    4. 弹出该 pid 账本残骸记录（若有）；5. pid 仍见于 cwd 可见 project/local 层 ``installedPlugins``
       声明 → WARN 指明文件路径、**不改写**（committable 团队声明不由本地 gc 静默动，下轮 gc 会再次列出）。

    :return: ``True`` = 意图已彻底移除；``False`` = cwd 可见 project/local 层仍有 committable 声明残留
        （merged 意图仍含该 pid，下轮 gc 会再次列为悬挂——调用方不得将其计入「已 prune」，隔离审查 🟡#1）。
    """
    _split_plugin_id(plugin_id)  # 形态校验（非法 → PluginInstallError，零变更）
    migrate_legacy_installs(home, env=env)
    records = load_installed_plugins(home=home, env=env).get("plugins", {}).get(plugin_id, [])
    project_paths = {p for r in records if isinstance(p := r.get("projectPath"), str) and p}
    _write_installed_plugin(plugin_id, False, env)
    _clear_enabled_entries_visible_layers(plugin_id, project_paths, env)

    def _drop(data: InstalledPluginsFile, _pid: str = plugin_id) -> None:
        data["plugins"].pop(_pid, None)

    update_installed_plugins(_drop, home=home, env=env)

    cwd = Path.cwd()
    residual_layers = (
        ("project", workdir_project_settings_path(cwd), SettingsScope.PROJECT),
        ("local", workdir_local_settings_path(cwd), SettingsScope.LOCAL),
    )
    fully_pruned = True
    for scope_name, path, scope_enum in residual_layers:
        if not path.exists():
            continue
        data, _errors = load_settings_file(path, scope_enum)
        declared = data.get("installedPlugins")
        if isinstance(declared, list) and plugin_id in declared:
            fully_pruned = False
            logger.warning(
                "prune: %r still declared in %s scope settings %s; not rewriting committable declaration (remove manually)",
                plugin_id,
                scope_name,
                path,
            )
    logger.info("pruned dangling plugin intent %r (fully_pruned=%s)", plugin_id, fully_pruned)
    return fully_pruned


async def disable_plugin(
    plugin_id: str,
    registry: SkillRegistry,
    home: Path,
    *,
    non_plugin_bundle_ids: NonPluginBundleIds,
    scope: str = "user",
    project_path: str | None = None,
    env: Mapping[str, str] | None = None,
    remove_server: RemoveServer | None = None,
) -> None:
    """
    禁用单个 plugin = 整 plugin 下线（§4.3 决策 #6）/ Disable = take the whole plugin offline。

    ① 写 ``enabledPlugins[id]=false``；② **按 §4.9.1-2 判据回收**其声明依赖的 MCP server（判据与 uninstall 同源
    ——协议明列 disable / uninstall 二者共用；用户自有 / 他 plugin 仍依赖者 MUST NOT 被摘）；③ 隐藏 skills
    （:meth:`SkillRegistry.mark_orphan`，**物化层不动**——clone 树 / installed 记录保留，:func:`enable_plugin`
    廉价复原）。区别于 :func:`uninstall_plugin`：disable 留 installed 记录、可一键回滚。

    .. note::
       disable **不删账本记录**，故判据的「其他 plugin」须排除本 pid **全部**记录（``exclude_pid``，无 retained）
       ——否则自己的记录会把自己的依赖判成「他人仍依赖」而永不回收。

       **已知驻留行为**（协议字面推论，与 rust-sdk#139 同口径）：「其他 plugin」= 账本 **installed** 记录
       （**含 disabled**，§4.9.1-2 字面为「无其他 plugin **声明依赖**」= 账本有记录）。故 A、B 均声明 X 时，
       **二者都 disable 后 X 仍驻留**（各自的账本记录互相替对方挡住回收），须等 uninstall 才回收。这是保守侧
       ——X 多活一会儿，但不泄漏（最后一个依赖者 uninstall 时回收）。守卫见
       ``test_disable_both_dependents_keeps_dep_resident``。


    ⚠️ **scope 契约**：``scope`` 须与该 plugin 的**安装 scope 一致**（调用方 / #69 CLI 从上下文传）——否则把
    ``enabledPlugins[id]=false`` 写到错误层，更高优先级层意图未动 → 六层合并后可能仍 enabled、下次 reconcile 复活，
    而 live 态已摘 server / orphan skill，造成背离。
    ⚠️ **非原子**：先写 settings 再摘 server / orphan skill；``remove_server`` 抛错会留半态（settings 已 false 但
    skill 未隐藏）。与 install/enable 的「预检/写在变更前」不对称——靠 reconcile 收敛兜底（#63 文档化边界）。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    _write_enabled_plugin(plugin_id, False, scope, project_path, env)
    reclaim: list[str] = []
    if remove_server is not None:
        # 账本单次读：自己的声明与「其他 plugin 的声明」取自**同一快照**，消 TOCTOU（与 uninstall/gc 对称）。
        ledger_plugins = load_installed_plugins(home=home, env=env).get("plugins", {})
        reclaim = reclaimable_mcp_deps(
            sorted(ledger_mcp_deps_of(ledger_plugins.get(plugin_id, []))),
            other_deps=other_plugin_mcp_deps(ledger_plugins, exclude_pid=plugin_id),
            user_declared=non_plugin_bundle_ids(),
        )
        for bundle_id in reclaim:
            await remove_server(bundle_id)
    for name in _plugin_skill_names(registry, marketplace, plugin):
        registry.mark_orphan(name)
    logger.info("disabled plugin %r (scope=%s, MCP deps reclaimed=%s, skills orphaned)", plugin_id, scope, reclaim or "none")


async def enable_plugin(
    plugin_id: str,
    registry: SkillRegistry,
    home: Path,
    *,
    scope: str = "user",
    project_path: str | None = None,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    existing_bundle_ids: ExistingBundleIds | None = None,
    register_server: RegisterServer | None = None,
    remove_server: RemoveServer | None = None,
) -> None:
    """
    启用单个 plugin（**原子激活**：skills 与 bundled server 一并入投影，失败回滚 ``installed_disabled``）/ Enable。

    **依赖已满足 ⇒ 复用既有实例**（协议 §2.5-1，#153/D3）：声明的 ``bundle_id`` 若已在运行期活跃集里，
    **跳过 register**——既有实例（用户 mcp.json 声明的，或他 plugin 带入的）胜出，本 plugin 复用它而非覆盖。
    与 :meth:`Computer.reconcile_governance` 的「existing wins → skip」同姿态。
    完整来源优先序见 :data:`~a2c_smcp.computer.settings.schema.SCOPE_ORDER`（**唯一权威**，勿在此复述字面量
    ——两处手写序漂移正是 #154 的根因）。此处「existing wins」是「``origin=plugin`` 恒最低」的**可观测等价**：
    plugin 声明**不进任一 resolve**（结构性缺席，见 ``SCOPE_ORDER`` 的说明），其「输给用户侧」由本处 +
    :meth:`Computer.reconcile_governance` 的 skip 保证。
    ⚠️ **等价的边界**：保证的是「plugin 输给任何**已挂载**的 server」，而非「输给任何**声明**」——若某用户声明
    被审批门拦下（DISABLED / PENDING 未批）而未挂，其 bundle_id 空出，plugin 那份仍会挂上。这与 rust
    ``collect_enabled_bundled_servers``（plugin 集内 first-wins、不与用户声明比对）**同姿态**，parity 保持；
    且协议 §5 item 10 明定 plugin 声明 MUST NOT 进审批门，故门控结果本就不该反向决定 plugin 基线。

    v0.3.0 §2.4「enable 原子性」：顺序 ① **内容预检 + 重解析 bundled servers**
    （:func:`_preflight_enable_content`，#259：记录必须仍有效物化、声明内容必须可完整投影，任一不满足即在
    **零持久化变更**前抛 :class:`PluginInstallError`）→ **依赖预检（先于 settings 写）**；② 快照该 scope
    ``enabledPlugins`` 原值 + 本 plugin 已活跃 skills →
    写 ``enabledPlugins[id]=true``；③ 复活 skills——re-stage（:func:`stage_marketplace_skills` 的
    :meth:`register_or_update` 把孤儿同名翻 ``orphaned=False``，``refresh=False`` 复用既有 clone）；
    ④ 重挂 servers。③/④ 任一失败 → **回滚**：注销本次新增 skill、摘除本次新增 server（经 ``remove_server``）、
    ``enabledPlugins`` 恢复原值（原 absent → 删条目）→ 上抛，净效果回 ``installed_disabled``。
    未安装 → :class:`PluginInstallError`（须先 install）。

    :raises PluginInstallError: 未安装；或 **内容预检失败**（#259——账本记录 ``installPath`` 缺失 / bundled JSON
        不可解析 / 声明 server 不可解析 / 声明 SKILL 零投影）。后者在**任何持久化变更之前**抛出，消息点名死路径
        并给出恢复指引，净效果保持 ``installed_disabled``（不重物化、不重 clone）。

    ⚠️ **scope 契约**：同 :func:`disable_plugin`——``scope`` 须与安装 scope 一致（调用方 / #69 从上下文传），否则
    ``enabledPlugins[id]=true`` 写错层、与 live 态背离。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    _require_existing_ids_guard(existing_bundle_ids, register_server)
    installed = load_installed_plugins(home=home, env=env)
    records = installed.get("plugins", {}).get(plugin_id)
    if not records:
        raise PluginInstallError(f"plugin {plugin_id!r} not installed; cannot enable (run 'plugin install' first)")

    # 0：跨 marketplace 裸名防御（#257 决策 2）——先于 enabledPlugins 写 / stage / 挂 server。存量双装
    #    （升级前两 marketplace 均已 install）升级后只能先 uninstall 被遮蔽的一方，不得靠 enable 触发覆盖。
    facts = _install_facts(home=home, env=env)
    conflicts = _bare_name_conflicts(plugin_id, facts=facts)
    if conflicts:
        raise _bare_name_conflict_error(plugin_id, conflicts, action="enable", ledger_pids=facts.ledger_pids)

    # ① 内容预检（#259，零持久化变更前）：记录仍有效物化 + 声明内容可完整投影 + 拒绝半态
    #    （判据复用 reconciler；不重物化、不重 clone）→ 聚合 bundled servers 供依赖预检与挂载复用。
    preflight = _preflight_enable_content(plugin_id, plugin, marketplace, home, records)
    servers = preflight.servers
    existing = existing_bundle_ids() if existing_bundle_ids is not None else set()
    _log_satisfied_deps(servers, existing)

    # ② 快照回滚基线（该 scope 文件的原值：True/False/None=absent；本 plugin 已活跃 skill 集）→ 写 true
    settings_path, scope_enum = _settings_path_for_scope(scope, project_path, env)
    prior_settings, _errors = load_settings_file(settings_path, scope_enum)
    prior_enabled = prior_settings.get("enabledPlugins")
    prev_value: bool | None = prior_enabled.get(plugin_id) if isinstance(prior_enabled, Mapping) else None
    skills_before = _plugin_skill_state(registry, marketplace, plugin)
    _write_enabled_plugin(plugin_id, True, scope, project_path, env)

    registered: list[str] = []
    try:
        # ③ 复活 skills（re-stage：register_or_update 翻活孤儿；复用既有 clone）
        registry.drain_source_conflicts()  # 清掉此前无关流程（如 marketplace add）留下的跨源记录
        source, _commit_sha = _resolve_marketplace_source(marketplace, home, env)
        staged = await stage_marketplace_skills(
            marketplace,
            source,
            registry,
            home,
            plugin_filter={plugin},
            refresh=False,
            timeout=timeout,
            env=env,
        )
        # ③.5 跨 source 复核（#257 复审 🔴1/🔴2）：staging 对同名异源条目的拒绝是静默的（记 ERROR、不抛），
        #     若不复核，enable 会「假成功」——enabledPlugins=true、server 已挂，但 get_skills 里是别的
        #     marketplace 的技能。**只认本次 staging 真正被拒的 name**（drain 记录）——按 plugin 前缀猜会把
        #     「同 plugin、skill 名不同」的正常注册误判成冲突（把可用路径打死）。转结构化错误 → 走既有回滚。
        want_source = f"{SOURCE_MARKETPLACE}:{marketplace}"
        refused = [
            conflict
            for conflict in registry.drain_source_conflicts()
            if conflict.name.startswith(f"{plugin}:") and conflict.refused_source == want_source
        ]
        if refused:
            raise _source_conflict_error(plugin_id, refused, home=home, env=env)
        # ③.6 stage 零注册兜底（#259 审查 🔴2 / 架构师复审 🟡2）：预检只按「SKILL.md 存在」判可投影，而真实
        #     装载还会因 frontmatter 缺 description / 名字非法 / catalog 清单不可读而**静默跳过**（staging 的逐
        #     plugin 降级不抛）。判据必须取**本次** stage 的返回值（`register_or_update` 对既有同源条目也返回 True，
        #     `staged` 非空即「本次装载工作正常」）——查 registry 里的旧活跃集会被旧投影掩盖本次失败。
        #     installPath 内确有 SKILL.md 包、却零注册 → 不得报「启用成功」；转结构化错误走既有回滚。
        #     **不改** staging 的全局降级（recovery/reconciler 依赖它），只收口 enable 调用方。
        if preflight.expects_skills and not staged:
            raise PluginInstallError(
                f"plugin {plugin_id!r} ships SKILL packages but staging registered none (invalid SKILL.md "
                f"frontmatter, duplicate SKILL names, or an unreadable marketplace manifest); enabledPlugins was "
                f"restored to its previous value. {_enable_recovery_hint(plugin_id)}"
            )
        # ④ 挂载依赖：仅挂**尚未满足**者；已满足 → 复用既有实例，不覆盖（§2.5-1）
        if register_server is not None:
            for cfg in servers:
                bundle_id = resolve_bundle_id(cfg)
                if bundle_id in existing:
                    continue
                await register_server(cfg)
                registered.append(bundle_id)  # 身份 = bundle_id：按 name 记会在「同名异 id」时回滚摘错对象
    except Exception:
        # 回滚（逆序，best-effort 不掩盖原异常）：撤销本次新增 skill / server，enabledPlugins 恢复原值。
        # ``registered`` 只含本次真正挂上的（依赖已满足者已在 ④ skip），故此处无需再判「是否本来就在」。
        for name in _plugin_skill_names(registry, marketplace, plugin, include_orphans=True):
            if name not in skills_before:
                registry.unregister(name)
            elif skills_before[name]:
                registry.mark_orphan(name)  # 调用前就存在的孤儿：恢复孤儿态，不是本次新增
        if remove_server is not None:
            for bundle_id in registered:
                try:
                    await remove_server(bundle_id)
                except Exception as e:  # noqa: BLE001 - 回滚 best-effort
                    logger.warning("enable rollback: remove_server(%r) failed: %s", bundle_id, e)
        try:
            _write_enabled_plugin(plugin_id, prev_value, scope, project_path, env)
        except Exception as e:  # noqa: BLE001 - 回滚 best-effort
            logger.warning("enable rollback: failed to restore enabledPlugins[%s]=%r: %s", plugin_id, prev_value, e)
        raise
    logger.info("enabled plugin %r (scope=%s, skills recovered, servers remounted)", plugin_id, scope)


# ---------------------------------------------------------------------------
# v0.2.x → v0.3.0 一次性状态迁移 / One-time state migration
# ---------------------------------------------------------------------------
def migrate_legacy_installs(home: Path, *, env: Mapping[str, str] | None = None) -> list[str]:
    """
    v0.2.x「装即活跃」存量 → v0.3.0 双意图的一次性迁移（迁移指南「保住既有用户现状」）/ One-time legacy migration。

    **标记 = user settings.json 是否含 ``installedPlugins`` 键**（#123 决策 #2）：键在（哪怕空数组）→ 已迁移，
    严格 no-op——防「v0.3.0 后手动删意图、账本未 gc」被迁回复活（意图是唯一权威，§2.3）。键缺失（pre-v0.3.0
    世界）→ 把账本全部合法 pid 写入 ``installedPlugins``，并对 merged ``enabledPlugins`` **无条目**的 pid 在
    user scope 补 ``enabledPlugins=true``（v0.2.x 下 absent=active，翻转后会熄灯，须保住；显式 false 本就
    禁用 → 不写，保持 ``installed_disabled``）。首跑**必写**标记键（空账本也写 ``[]``）。

    合并视图经 :func:`resolve_settings`（user/project/local；无 flag/policy——boot 语境，与
    ``Computer._resolve_declared_settings`` 已知限制一致）。由 ``Computer.boot_up`` 在治理恢复前调用（失败
    隔离）；幂等、只跑一次。

    :param home: SKILL Home 绝对根（账本读取）。
    :param env: 环境映射（settings 路径解析），默认 ``os.environ``。
    :return: 本次迁入 ``installedPlugins`` 的 pid 列表（已迁移 no-op → 空）。
    """
    path = user_settings_path(env)
    try:
        return _migrate_installs_locked(path, home, env)
    except SettingsFileCorruptError as e:
        raise PluginInstallError(str(e)) from e


def _migrate_installs_locked(path: Path, home: Path, env: Mapping[str, str] | None) -> list[str]:
    """
    :func:`migrate_legacy_installs` 的锁内主体（拆出以保持 raw 错误翻译集中在调用处）
    The locked body of :func:`migrate_legacy_installs`, split out so raw-error translation stays at the call site.

    **#246**：标记键判据读 **raw**（``"installedPlugins" in existing``）——过去读校验视图，类型错的标记键会被
    过滤掉 ⇒ 被误判「未迁移」而重复迁入。写回同样只改目标键，其余字段（含 policy-only 越权字段 / 未知键）保留。
    """
    with file_lock(path):
        existing = read_raw_object(path) or {}
        if "installedPlugins" in existing:
            return []  # 已迁移（标记键在，哪怕类型错）→ 严格 no-op

        # 容器形状守卫：整字段非对象时 fail-closed——否则 apply_write 会按「类型切换」整体替换，
        # 等于把用户写的畸形内容静默换掉（独立审查 🔴3）。
        enabled_raw = mapping_field_or_empty(existing, "enabledPlugins", path=path)

        # ``drop_legacy=False`` 是**必须**的（#153）：v0.2.x 账本记录一律是旧格式（``bundledMcpServers``），
        # 默认读法会把它们整条丢弃 ⇒ 带 MCP server 的 plugin 连 pid 一起消失 ⇒ 意图无从回填 ⇒ 升级后
        # plugin 静默全丢。此处**只读 key**（pid），不碰旧格式的字段值，故安全；账本记录本身随后由
        # recovery 从这里回填出的意图重建（协议 §4.9.1-4「丢弃 + 从 installedPlugins 意图重建」的前提）。
        raw_ledger = load_installed_plugins(home=home, env=env, drop_legacy=False)
        ledger_pids = [pid for pid in raw_ledger.get("plugins", {}) if is_valid_enabled_plugin_key(pid)]
        merged_enabled = resolve_settings(env=env).settings.get("enabledPlugins")
        merged_view: Mapping[str, Any] = merged_enabled if isinstance(merged_enabled, Mapping) else {}

        updates: dict[str, Any] = {"installedPlugins": ledger_pids}
        # **只补缺失**：``merged_view`` 是校验过滤后的视图，畸形值（如 ``"yes"``）会被过滤掉而被误判「无条目」；
        # 故再用 raw 容器兜一层——该 pid 只要在**原始** ``enabledPlugins`` 里存在（哪怕值畸形）就不覆盖它。
        # 对齐 rust ``set_enabled_true_if_absent`` 的 ``entry().or_insert(true)``（独立审查 🔴3）。
        enabled_updates = {pid: True for pid in ledger_pids if pid not in merged_view and pid not in enabled_raw}
        if enabled_updates:
            updates["enabledPlugins"] = enabled_updates
        atomic_write_json(path, apply_write(existing, updates))
        if ledger_pids:
            logger.info(
                "migrated %d legacy plugin install(s) to installedPlugins (v0.3.0): %s (enabled=true backfilled for %d)",
                len(ledger_pids),
                ledger_pids,
                len(enabled_updates),
            )
        return ledger_pids
