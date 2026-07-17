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
  ``enabledPlugins`` 门控重建（仅显式 true 激活）。uninstall 仅注销**活跃** SKILL（与
  :func:`_unregister_marketplace_skills` 同限）；先 disable（orphan）再 uninstall 会残留内存孤儿条目，
  进程重启即清。
- :func:`enable_plugin` 不校验 ``installedPlugins`` 意图成员资格（以账本记录为"可启用"依据）：手编移除意图
  而账本未 gc 的窗口里 live enable 仍可点亮，但重启后 boot 门控（installed ∧ enabled）不会恢复，交由
  reconcile 收敛。
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.reconciler import (
    NonPluginBundleIds,
    ledger_mcp_deps_of,
    other_plugin_mcp_deps,
    reclaimable_mcp_deps,
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
    find_plugin_entry,
    load_bundled_servers,
    plugin_root_base,
    read_marketplace_manifest,
    read_plugin_metadata,
    resolve_plugin_version,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import (
    DEFAULT_GIT_TIMEOUT,
    locate_plugin_root,
    stage_marketplace_skills,
)
from a2c_smcp.utils.bundle_id import resolve_bundle_id
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import is_within

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 异常 / Exceptions（模块内本地异常，仿 ToolNameDuplicatedError / SkillStagingError 惯例）
# ---------------------------------------------------------------------------
class PluginInstallError(Exception):
    """plugin install/enable 前置失败（marketplace 未添加 / catalog 未 clone / entry 缺失 / 非法 scope）。"""


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


def _safe_rmtree(path: Path, home: Path) -> None:
    """仅当 ``path`` 词法位于 SKILL Home 内才递归删除（防越权删盘外目录）/ Guarded rmtree within SKILL Home（同 reconciler 语义）。"""
    rp = path.resolve()
    if not is_within(rp, home.resolve()):
        logger.error("installer: refusing to remove %s outside SKILL Home %s", rp, home)
        return
    if not rp.exists():
        return
    try:
        shutil.rmtree(rp)
    except OSError as e:  # 失败降级：删除受阻不阻断后续
        logger.warning("installer: failed to remove %s: %s", rp, e)


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

    复用 store 原语自组织：:func:`file_lock`（旁车 ``.lock``，建父目录）+ :func:`load_settings_file`（容错）+
    :func:`apply_write`（递归进 ``enabledPlugins`` 仅改该 key、不毁兄弟；:data:`DELETE` 删 key）+
    :func:`atomic_write_json`（``header=None``——settings.json 是人编意图层，无写保护头 / version 字段，区别于物化文件）。
    """
    path, scope_enum = _settings_path_for_scope(scope, project_path, env)
    with file_lock(path):
        existing, _errors = load_settings_file(path, scope_enum)
        if value is None:
            enabled = existing.get("enabledPlugins")
            if not isinstance(enabled, Mapping) or plugin_id not in enabled:
                return  # 无条目可删（apply_write 对缺失父键会原样写入哨兵，须在此短路）
            updated = apply_write(existing, {"enabledPlugins": {plugin_id: DELETE}})
        else:
            updated = apply_write(existing, {"enabledPlugins": {plugin_id: value}})
        atomic_write_json(path, updated)


def _clear_enabled_entries_visible_layers(plugin_id: str, project_paths: set[str], env: Mapping[str, str] | None) -> None:
    """
    清理 ``enabledPlugins[plugin_id]`` 的全部**可见层**条目：user 恒清；project/local 对「账本 projectPath ∪ cwd」
    逐一清（#125 任务 1：账本重物化归一后丢 projectPath，仅遍历账本会漏掉 cwd 可见层的残留 ``true``——
    卸载后残留会让重装立即激活，违反 install 不激活）/ Clear enabled entries across visible layers。

    project/local 写调用前**先查 settings 文件存在**：:func:`~a2c_smcp.computer.settings.store.file_lock`
    会 ``mkdir(parents=True)`` 建父目录 + 建 ``.lock``，而 :func:`_write_enabled_plugin` 的 no-op 短路在锁后——
    无守卫会在无 ``.tfrobot/`` 的 cwd 制造垃圾目录。managed/policy 只读层不触碰；清失败 WARN 不阻断（best-effort）。
    """
    _write_enabled_plugin(plugin_id, None, "user", None, env)
    targets = {str(Path(p)) for p in project_paths if p}
    targets.add(str(Path.cwd()))
    for pp in sorted(targets):
        for s in ("project", "local"):
            try:
                path, _scope = _settings_path_for_scope(s, pp, env)
                if not path.exists():  # 存在性守卫：不为清理凭空建 .tfrobot/ + .lock
                    continue
                _write_enabled_plugin(plugin_id, None, s, pp, env)
            except PluginInstallError as e:  # best-effort：清条目失败不阻断
                logger.warning("failed to clear enabledPlugins[%s] in %s scope at %s: %s", plugin_id, s, pp, e)


def _write_installed_plugin(plugin_id: str, present: bool, env: Mapping[str, str] | None) -> bool:
    """
    user scope ``installedPlugins`` 数组的持锁 RMW（增/删一个条目）；返回**写前**是否已含该条目 / RMW the install intent。

    安装是全局一次的事实（协议 v0.3.0 §2.1/§2.4「plugin_installation」）→ 意图固定落 **user** settings.json
    （#123 决策：不随 ``--scope`` 泄漏进可提交的 project 文件）；读取侧由 :func:`resolve_settings` merged
    并集承担（project/local 声明亦被认可，支持声明式复现）。数组写回整体替换（§5.4 写语义），保序去重。
    """
    path = user_settings_path(env)
    with file_lock(path):
        existing, _errors = load_settings_file(path, SettingsScope.USER)
        current = existing.get("installedPlugins")
        entries: list[str] = list(current) if isinstance(current, list) else []
        was_present = plugin_id in entries
        if present and not was_present:
            entries.append(plugin_id)
        elif not present and was_present:
            entries = [e for e in entries if e != plugin_id]
        atomic_write_json(path, apply_write(existing, {"installedPlugins": entries}))
        return was_present


def _plugin_skill_names(registry: SkillRegistry, marketplace: str, plugin: str) -> list[str]:
    """
    某 plugin 当前**活跃**的 marketplace SKILL name / A plugin's currently-active marketplace SKILL names。

    过滤 ``A2CSkillRef.source == "marketplace:<mp>"`` 且 ``name`` 以 ``<plugin>:`` 起（marketplace name = ``<plugin>:<skill>``）。
    仅枚举活跃集（:meth:`SkillRegistry.active_refs` 排除孤儿）——同 :func:`_unregister_marketplace_skills`。
    """
    want_source = f"{SOURCE_MARKETPLACE}:{marketplace}"
    prefix = f"{plugin}:"
    names: list[str] = []
    for ref in registry.active_refs():
        if ref.get("source") != want_source:
            continue
        name = ref.get("name")
        if isinstance(name, str) and name.startswith(prefix):
            names.append(name)
    return names


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
    if find_plugin_entry(read_marketplace_manifest(catalog_dir), plugin) is None:
        raise PluginInstallError(f"plugin {plugin!r} not found in marketplace {marketplace!r} manifest")

    # 1.5：预检过后、写意图键之前，先跑一次性迁移（幂等、标记键短路）。install 会写 installedPlugins 键——
    #      若迁移尚未发生（如升级后未经 boot 的 headless CLI install），先写键会把迁移标记误置为"已迁移"，
    #      永久丢弃 v0.2.x 存量活跃态（隔离审查 🔴#2 + N1：置于预检后保 fail-fast 无副作用）。
    migrate_legacy_installs(home, env=env)

    # 2：config-first 写全局安装意图（快照写前状态供原子回滚：重装时原已在 → 失败不误删）
    was_present = _write_installed_plugin(plugin_id, True, env)

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
    except Exception:
        if not was_present:
            try:
                _write_installed_plugin(plugin_id, False, env)
            except Exception as e:  # noqa: BLE001 - 回滚 best-effort
                logger.warning("install rollback: failed to revert installedPlugins entry %r: %s", plugin_id, e)
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
    卸载单个 plugin（删 installPath 树 + 注销 skills + **按判据回收** MCP 依赖 + 删账本记录）/ Uninstall。

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

    .. note::
       **相对源 plugin**（``source`` 为相对路径）的 ``installPath`` 位于**共享 catalog clone 内**
       （``<home>/marketplace/<mp>/plugins/<plugin>``），:func:`_safe_rmtree` 删的是该 marketplace 共享 git
       工作树的子目录——此行为与兄弟 :func:`gc_plugins` 一致（同一 ``_safe_rmtree`` 语义、非本工单新引入）；
       后续 ``marketplace refresh`` 遇脏树会 fallback 全量重 clone 干净恢复。「仅删外部 ``.plugins/`` 树、跳过
       catalog 内子树」的行为收敛是 installer + gc 的**跨切 follow-up**（须两处一致，避免与 #62 的 gc 分叉）。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    installed = load_installed_plugins(home=home, env=env)
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
    #    账本自身字段——``installPath`` 指向的树在下面就被删了，「从 install_path 重解析」在本路径恒不成立）。
    #    ``retained``：scoped uninstall 时本 pid 其余 scope 的记录仍声明依赖 ⇒ 仍算依赖者，不可回收。
    deps = sorted(ledger_mcp_deps_of(targeted))
    retained = [r for r in records if r not in targeted]
    ledger_plugins = installed.get("plugins", {})
    reclaim = reclaimable_mcp_deps(
        deps,
        other_deps=other_plugin_mcp_deps(ledger_plugins, exclude_pid=plugin_id, retained_records=retained),
        user_declared=non_plugin_bundle_ids(),
    )

    # ② 删树 → ③ 注销 skills → ④ 回收依赖 → ⑤ 删账本记录（⑤ 恒在末位，勿前移，见 §4.9.1-3）
    for rec in targeted:
        install_path = rec.get("installPath")
        if isinstance(install_path, str) and install_path:
            _safe_rmtree(Path(install_path), home)

    for name in _plugin_skill_names(registry, marketplace, plugin):
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

    v0.3.0 §2.4「enable 原子性」：顺序 ① 从物化记录的 ``installPath`` 重解析 bundled servers →
    **依赖预检（先于 settings 写）**；② 快照该 scope ``enabledPlugins`` 原值 + 本 plugin 已活跃 skills →
    写 ``enabledPlugins[id]=true``；③ 复活 skills——re-stage（:func:`stage_marketplace_skills` 的
    :meth:`register_or_update` 把孤儿同名翻 ``orphaned=False``，``refresh=False`` 复用既有 clone）；
    ④ 重挂 servers。③/④ 任一失败 → **回滚**：注销本次新增 skill、摘除本次新增 server（经 ``remove_server``）、
    ``enabledPlugins`` 恢复原值（原 absent → 删条目）→ 上抛，净效果回 ``installed_disabled``。
    未安装 → :class:`PluginInstallError`（须先 install）。

    ⚠️ **scope 契约**：同 :func:`disable_plugin`——``scope`` 须与安装 scope 一致（调用方 / #69 从上下文传），否则
    ``enabledPlugins[id]=true`` 写错层、与 live 态背离。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    _require_existing_ids_guard(existing_bundle_ids, register_server)
    installed = load_installed_plugins(home=home, env=env)
    records = installed.get("plugins", {}).get(plugin_id)
    if not records:
        raise PluginInstallError(f"plugin {plugin_id!r} not installed; cannot enable (run 'plugin install' first)")

    # ① 重解析 bundled servers（不重 clone，从记录 installPath 读）+ 依赖预检（零持久化变更前）
    servers: list[MCPServerConfig] = []
    for rec in records:
        install_path = rec.get("installPath")
        if isinstance(install_path, str) and install_path:
            servers.extend(load_bundled_servers(Path(install_path)))
    existing = existing_bundle_ids() if existing_bundle_ids is not None else set()
    _log_satisfied_deps(servers, existing)

    # ② 快照回滚基线（该 scope 文件的原值：True/False/None=absent；本 plugin 已活跃 skill 集）→ 写 true
    settings_path, scope_enum = _settings_path_for_scope(scope, project_path, env)
    prior_settings, _errors = load_settings_file(settings_path, scope_enum)
    prior_enabled = prior_settings.get("enabledPlugins")
    prev_value: bool | None = prior_enabled.get(plugin_id) if isinstance(prior_enabled, Mapping) else None
    skills_before = set(_plugin_skill_names(registry, marketplace, plugin))
    _write_enabled_plugin(plugin_id, True, scope, project_path, env)

    registered: list[str] = []
    try:
        # ③ 复活 skills（re-stage：register_or_update 翻活孤儿；复用既有 clone）
        source, _commit_sha = _resolve_marketplace_source(marketplace, home, env)
        await stage_marketplace_skills(
            marketplace,
            source,
            registry,
            home,
            plugin_filter={plugin},
            refresh=False,
            timeout=timeout,
            env=env,
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
        for name in _plugin_skill_names(registry, marketplace, plugin):
            if name not in skills_before:
                registry.unregister(name)
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
    with file_lock(path):
        existing, _errors = load_settings_file(path, SettingsScope.USER)
        if "installedPlugins" in existing:
            return []  # 已迁移（标记键在）→ 严格 no-op

        # ``drop_legacy=False`` 是**必须**的（#153）：v0.2.x 账本记录一律是旧格式（``bundledMcpServers``），
        # 默认读法会把它们整条丢弃 ⇒ 带 MCP server 的 plugin 连 pid 一起消失 ⇒ 意图无从回填 ⇒ 升级后
        # plugin 静默全丢。此处**只读 key**（pid），不碰旧格式的字段值，故安全；账本记录本身随后由
        # recovery 从这里回填出的意图重建（协议 §4.9.1-4「丢弃 + 从 installedPlugins 意图重建」的前提）。
        raw_ledger = load_installed_plugins(home=home, env=env, drop_legacy=False)
        ledger_pids = [pid for pid in raw_ledger.get("plugins", {}) if is_valid_enabled_plugin_key(pid)]
        merged_enabled = resolve_settings(env=env).settings.get("enabledPlugins")
        merged_view: Mapping[str, Any] = merged_enabled if isinstance(merged_enabled, Mapping) else {}

        updates: dict[str, Any] = {"installedPlugins": ledger_pids}
        enabled_updates = {pid: True for pid in ledger_pids if pid not in merged_view}
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
