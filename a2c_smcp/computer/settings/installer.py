# -*- coding: utf-8 -*-
# filename: installer.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Plugin 显式生命周期：install / uninstall / enable / disable（v0.2.1 #63）
Plugin explicit lifecycle: install / uninstall / enable / disable (v0.2.1 #63).

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §3.3 / §4.3 / §6.2 / §7.2 / §10.6；
                   父工单 #53 决策 #6（disable=整 plugin 下线）/ #18（MCP 外来同名硬抛、无逃生口）/ #19（uninstall 级联）。

本模块是 :mod:`~a2c_smcp.computer.settings.reconciler` 的**兄弟**——reconciler 做 additive-only 对账 + 孤儿
gc/prune，本模块做**显式单 plugin** 增删启停，写 ``installed_plugins.json``（正是 :func:`gc_plugins` 读取的账本，
#62 decision A 留的接缝）。复用 :func:`~a2c_smcp.computer.skills.staging.stage_marketplace_skills`（skill 注册）
+ :func:`~a2c_smcp.computer.skills.staging.locate_plugin_root`（plugin 根定位/clone）+
:mod:`~a2c_smcp.computer.skills.manifest`（marketplace.json / plugin.json / mcp-servers 解析）。

**循环导入**：本模块 import ``skills.staging``（其顶层 import ``settings.schema``），故**刻意不在**
``settings/__init__`` re-export（与 reconciler 同——见 #62 教训）；消费方直接
``from a2c_smcp.computer.settings.installer import install_plugin`` 等。

**MCP 注入**：与 :func:`gc_plugins` 的 ``mcp_teardown`` 同款——bundled MCP server 的存在性查询 / 注册 / 摘除经
注入回调（:data:`ExistingServerNames` / :data:`RegisterServer` / :data:`RemoveServer`），由 #69 CLI 层用
``Computer.aadd_or_aupdate_server`` / ``Computer.aremove_server`` / ``mcp_manager.get_server_status`` 包装；
回调 ``None`` = ledger-only（单测 / 无 server 场景）。**install/enable 注册经 Computer 路径渲染 ``${input:}``**
（§9.3，inputs 池消歧归 #65）。

边界（文档化，非缺陷）/ Documented boundaries：
- 本模块操作 **live session**（Computer.boot_up 当前不调 reconcile）：跨重启重挂 bundled server +
  ``installed × enabled`` 交集归 reconcile 接线层（#68/#69）。
- disable 的 skill orphan 为**内存态**（Registry 不落盘），同会话廉价复原；跨重启靠 reconcile 按
  ``enabledPlugins`` 重建。uninstall 仅注销**活跃** SKILL（与 :func:`_unregister_marketplace_skills` 同限）；
  先 disable（orphan）再 uninstall 会残留内存孤儿条目，进程重启即清。
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.schema import SettingsScope, is_valid_enabled_plugin_key
from a2c_smcp.computer.settings.scope import (
    apply_write,
    load_settings_file,
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
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import is_within

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 异常 / Exceptions（模块内本地异常，仿 ToolNameDuplicatedError / SkillStagingError 惯例）
# ---------------------------------------------------------------------------
class PluginInstallError(Exception):
    """plugin install/enable 前置失败（marketplace 未添加 / catalog 未 clone / entry 缺失 / 非法 scope）。"""


class MCPServerNameConflictError(Exception):
    """
    bundled MCP server 与**外来**同名 server 冲突（§7.2/§10.6）/ Foreign MCP server name conflict。

    **硬抛、原子失败、无 rename/force 逃生口**（name 即身份）。判定排除 plugin 自有
    （命中其 ``bundledMcpServers`` 记录）→ 幂等再物化不算冲突。
    """


# ---------------------------------------------------------------------------
# MCP 注入接口 / Injected MCP interface（沿用 gc_plugins 的回调注入风格）
# ---------------------------------------------------------------------------
# 当前已注册 server 名集合（同步；CLI 包 ``{r[0] for r in mcp_manager.get_server_status()}``）。
ExistingServerNames = Callable[[], set[str]]
# 注册 / 更新一个 server（异步；CLI 包 ``Computer.aadd_or_aupdate_server``，含 ``${input:}`` 渲染）。
RegisterServer = Callable[[MCPServerConfig], Awaitable[None]]
# 停止并移除一个 server（异步；CLI 包 ``Computer.aremove_server``）。
RemoveServer = Callable[[str], Awaitable[None]]


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


def _write_enabled_plugin(plugin_id: str, value: bool, scope: str, project_path: str | None, env: Mapping[str, str] | None) -> None:
    """
    写 ``enabledPlugins[<plugin_id>] = value`` 到指定 scope 的 settings.json（持锁原子 RMW）/ Write the enabled flag。

    复用 store 原语自组织：:func:`file_lock`（旁车 ``.lock``，建父目录）+ :func:`load_settings_file`（容错）+
    :func:`apply_write`（递归进 ``enabledPlugins`` 仅改该 key、不毁兄弟）+ :func:`atomic_write_json`（``header=None``——
    settings.json 是人编意图层，无写保护头 / version 字段，区别于物化文件）。
    """
    path, scope_enum = _settings_path_for_scope(scope, project_path, env)
    with file_lock(path):
        existing, _errors = load_settings_file(path, scope_enum)
        updated = apply_write(existing, {"enabledPlugins": {plugin_id: value}})
        atomic_write_json(path, updated)


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


def _bundled_servers_of(home: Path, plugin_id: str, env: Mapping[str, str] | None) -> set[str]:
    """该 plugin 物化记录里全部 ``bundledMcpServers`` 名（跨 scope 记录并集）/ All recorded bundled server names。"""
    installed = load_installed_plugins(home=home, env=env)
    out: set[str] = set()
    for rec in installed.get("plugins", {}).get(plugin_id, []):
        servers = rec.get("bundledMcpServers")
        if isinstance(servers, list):
            out.update(s for s in servers if isinstance(s, str))
    return out


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


def _conflict_check(servers: list[MCPServerConfig], existing: set[str], owned: set[str]) -> None:
    """
    bundled server 同名冲突预检（§7.2/§10.6）/ Bundled-server name-conflict precheck。

    外来同名（``cfg.name in existing`` 且 ``not in owned``）→ :class:`MCPServerNameConflictError`（硬抛、零变更）；
    plugin 自有（命中 ``owned`` = 其 ``bundledMcpServers`` 记录）→ 幂等放行（overwrite 自有 server）。
    """
    for cfg in servers:
        if cfg.name in existing and cfg.name not in owned:
            raise MCPServerNameConflictError(
                f"MCP server name conflict: {cfg.name!r} already exists and is not owned by this plugin. "
                "Resolve by 'server rm' the existing server, or rename it in the plugin's own manifest "
                "(no --rename / --force-override escape hatch: name is identity).",
            )


def _require_existing_names_guard(
    existing_server_names: ExistingServerNames | None,
    register_server: RegisterServer | None,
) -> None:
    """
    护栏：给了 ``register_server`` 就**必须**给 ``existing_server_names`` / Guard: register implies existing-names。

    否则冲突闸门以 ``existing=∅`` 运行被静默旁路——外来同名 server 会被 MCP manager **静默覆盖**（manager 对
    同名是覆盖更新、不抛），违 §10.6「外来同名硬抛」铁律。三注入回调应「齐备或全无」，此处强制 register→existing。
    """
    if register_server is not None and existing_server_names is None:
        raise PluginInstallError(
            "existing_server_names is required when register_server is given "
            "(else the MCP name-conflict gate is silently bypassed; design §10.6)",
        )


# ---------------------------------------------------------------------------
# install / uninstall / enable / disable
# ---------------------------------------------------------------------------
async def install_plugin(
    plugin_id: str,
    registry: SkillRegistry,
    home: Path,
    *,
    scope: str = "user",
    project_path: str | None = None,
    version: str | None = None,
    refresh: bool = False,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    existing_server_names: ExistingServerNames | None = None,
    register_server: RegisterServer | None = None,
    remove_server: RemoveServer | None = None,
) -> InstalledPluginRecord:
    """
    显式安装单个 plugin（**原子失败：冲突即抛、不留半装**）/ Install one plugin atomically。

    顺序（§10.6「预检-先于-变更」）/ Order:
    1. 校验 ``plugin_id`` → ``(plugin, mp)``；从 ``known_marketplaces.json`` 取 mp source（未添加→抛）。
    2. 要求 catalog clone 已存在（``marketplace add`` 前置；缺失→抛）；读 marketplace.json 定位 entry。
    3. :func:`locate_plugin_root` 定位 plugin 根（必要时 clone 外部 plugin；失败→抛，零变更）。
    4. :func:`load_bundled_servers` 全量解析 ``mcp-servers/<n>.json``（任一畸形→抛，注册前）。
    5. **★冲突闸门**：外来同名→:class:`MCPServerNameConflictError`（零变更）；自有同名→幂等放行。
    6-7. 过闸后变更：注册 bundled servers → :func:`stage_marketplace_skills` 注册 skills（复用既有 clone）。
       任一失败 → **精确补偿回滚**（仅注销本次新增的 skill、仅移除本次新增的 server——重装失败不误删此前已有）
       → 上抛，**不写账本**。
    8. **★最后**写 ``installed_plugins.json``（仅全成功）：scope / installPath / version / commitSha /
       installedAt / lastUpdated / bundledMcpServers。

    :param scope: 物化记录 scope（``managed|user|project|local``，默认 ``user``）。
    :param version: 记录版本覆盖（``--version``）；缺省按 entry > plugin.json > commitSha 解析。v0.2.1 git-ref
        锁版本由 entry source 的 ref 治理（version 仅作记录）。
    :param existing_server_names / register_server / remove_server: MCP 注入回调（``None`` = ledger-only）。
        **契约**：给了 ``register_server`` 就必须给 ``existing_server_names``（否则冲突闸门被静默旁路，违 §10.6）。
    :return: 写入的 :class:`InstalledPluginRecord`。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    _require_existing_names_guard(existing_server_names, register_server)
    source, commit_sha = _resolve_marketplace_source(marketplace, home, env)

    catalog_dir = marketplace_skill_dir(home, marketplace)
    if not catalog_dir.is_dir():
        raise PluginInstallError(f"marketplace {marketplace!r} catalog not cloned at {catalog_dir} (run 'marketplace add/refresh' first)")

    manifest = read_marketplace_manifest(catalog_dir)
    entry = find_plugin_entry(manifest, plugin)
    if entry is None:
        raise PluginInstallError(f"plugin {plugin!r} not found in marketplace {marketplace!r} manifest")

    # 1-4：定位 + 解析（注册前，畸形即抛 → 原子前置）
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
    servers = load_bundled_servers(plugin_root)

    # 5：★冲突闸门（零变更）。owned = 自有同名白名单（其上次记录的 bundledMcpServers）。
    owned = _bundled_servers_of(home, plugin_id, env)
    existing = existing_server_names() if existing_server_names is not None else set()
    _conflict_check(servers, existing, owned)

    # —— 过闸：开始变更，失败补偿回滚 ——
    # 回滚前快照本 plugin 已活跃的 skill：使补偿只撤销**本次新增**——重装（plugin 已装、skill 已活跃、owned
    # server 已挂）中途失败时，不误删此前就存在的 skill / 摘除原本工作的 server（精确回滚，避免 live 态破坏）。
    skills_before = set(_plugin_skill_names(registry, marketplace, plugin))
    registered: list[str] = []
    try:
        if register_server is not None:
            for cfg in servers:
                await register_server(cfg)
                registered.append(cfg.name)
        # skills 注册：复用 #61（catalog 已 clone、plugin 已定位 → refresh=False 不重 clone）
        await stage_marketplace_skills(
            marketplace,
            source,
            registry,
            home,
            plugin_filter={plugin},
            refresh=refresh,
            timeout=timeout,
            env=env,
        )
    except Exception:
        # 精确回滚（逆序）：仅注销本次新增的 skill（不在 skills_before）、仅移除本次新增的 server（不在 owned，
        # 即非自有/非重装前已挂）；再上抛（账本未写）。
        for name in _plugin_skill_names(registry, marketplace, plugin):
            if name not in skills_before:
                registry.unregister(name)
        if remove_server is not None:
            for sname in registered:
                if sname in owned:
                    continue  # 自有 server（重装前已挂）：保留，不误摘
                try:
                    await remove_server(sname)
                except Exception as e:  # 回滚 best-effort，不掩盖原异常
                    logger.warning("install rollback: remove_server(%r) failed: %s", sname, e)
        raise

    # 8：★最后写账本（仅全成功）
    bundled_names = [cfg.name for cfg in servers]
    resolved_version = version if version else resolve_plugin_version(entry, read_plugin_metadata(plugin_root), version_fallback)
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
    if bundled_names:
        record["bundledMcpServers"] = bundled_names

    def _put(data: InstalledPluginsFile, _rec: InstalledPluginRecord = record, _pid: str = plugin_id) -> None:
        plugins = data["plugins"]
        # 多 scope 数组：替换同 scope 记录、保留其它 scope（v0.2.1 常见单元素）。
        kept = [r for r in plugins.get(_pid, []) if r.get("scope") != _rec["scope"]]
        kept.append(_rec)
        plugins[_pid] = kept

    update_installed_plugins(_put, home=home, env=env)
    logger.info("installed plugin %r (servers=%s, skills staged)", plugin_id, bundled_names or "none")
    return record


async def uninstall_plugin(
    plugin_id: str,
    registry: SkillRegistry,
    home: Path,
    *,
    scope: str | None = None,
    keep_servers: bool = False,
    env: Mapping[str, str] | None = None,
    remove_server: RemoveServer | None = None,
) -> bool:
    """
    卸载单个 plugin（删 installPath 树 + 注销 skills + 级联 stop+remove bundled server + 删账本记录）/ Uninstall。

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

    bundled: list[str] = []
    for rec in targeted:
        install_path = rec.get("installPath")
        if isinstance(install_path, str) and install_path:
            _safe_rmtree(Path(install_path), home)
        servers = rec.get("bundledMcpServers")
        if isinstance(servers, list):
            bundled.extend(s for s in servers if isinstance(s, str))

    for name in _plugin_skill_names(registry, marketplace, plugin):
        registry.unregister(name)

    if not keep_servers and remove_server is not None:
        for sname in bundled:
            await remove_server(sname)

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
    logger.info(
        "uninstalled plugin %r (scope=%s, servers=%s)",
        plugin_id,
        scope or "all",
        "kept" if keep_servers else (bundled or "none"),
    )
    return True


async def disable_plugin(
    plugin_id: str,
    registry: SkillRegistry,
    home: Path,
    *,
    scope: str = "user",
    project_path: str | None = None,
    env: Mapping[str, str] | None = None,
    remove_server: RemoveServer | None = None,
) -> None:
    """
    禁用单个 plugin = 整 plugin 下线（§4.3 决策 #6）/ Disable = take the whole plugin offline。

    ① 写 ``enabledPlugins[id]=false``；② 停并摘除其 bundled MCP server（读物化记录的 ``bundledMcpServers``）；
    ③ 隐藏 skills（:meth:`SkillRegistry.mark_orphan`，**物化层不动**——clone 树 / installed 记录保留，
    :func:`enable_plugin` 廉价复原）。区别于 :func:`uninstall_plugin`：disable 留 installed 记录、可一键回滚。

    ⚠️ **scope 契约**：``scope`` 须与该 plugin 的**安装 scope 一致**（调用方 / #69 CLI 从上下文传）——否则把
    ``enabledPlugins[id]=false`` 写到错误层，更高优先级层意图未动 → 六层合并后可能仍 enabled、下次 reconcile 复活，
    而 live 态已摘 server / orphan skill，造成背离。
    ⚠️ **非原子**：先写 settings 再摘 server / orphan skill；``remove_server`` 抛错会留半态（settings 已 false 但
    skill 未隐藏）。与 install/enable 的「预检/写在变更前」不对称——靠 reconcile 收敛兜底（#63 文档化边界）。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    _write_enabled_plugin(plugin_id, False, scope, project_path, env)
    if remove_server is not None:
        for sname in sorted(_bundled_servers_of(home, plugin_id, env)):
            await remove_server(sname)
    for name in _plugin_skill_names(registry, marketplace, plugin):
        registry.mark_orphan(name)
    logger.info("disabled plugin %r (scope=%s, servers detached, skills orphaned)", plugin_id, scope)


async def enable_plugin(
    plugin_id: str,
    registry: SkillRegistry,
    home: Path,
    *,
    scope: str = "user",
    project_path: str | None = None,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    existing_server_names: ExistingServerNames | None = None,
    register_server: RegisterServer | None = None,
) -> None:
    """
    启用单个 plugin（廉价复原，**无需重 clone/重装**）/ Enable = cheap restore (no re-clone)。

    顺序：① 从物化记录的 ``installPath`` 重解析 bundled servers → **★冲突预检（先于 settings 写 → enable 原子）**；
    ② 写 ``enabledPlugins[id]=true``；③ 复活 skills——re-stage（:func:`stage_marketplace_skills` 的
    :meth:`register_or_update` 把孤儿同名翻 ``orphaned=False``，``refresh=False`` 复用既有 clone）；④ 重挂 servers。
    未安装 → :class:`PluginInstallError`（须先 install）。

    ⚠️ **scope 契约**：同 :func:`disable_plugin`——``scope`` 须与安装 scope 一致（调用方 / #69 从上下文传），否则
    ``enabledPlugins[id]=true`` 写错层、与 live 态背离。
    """
    plugin, marketplace = _split_plugin_id(plugin_id)
    _require_existing_names_guard(existing_server_names, register_server)
    installed = load_installed_plugins(home=home, env=env)
    records = installed.get("plugins", {}).get(plugin_id)
    if not records:
        raise PluginInstallError(f"plugin {plugin_id!r} not installed; cannot enable (run 'plugin install' first)")

    # ① 重解析 bundled servers（不重 clone，从记录 installPath 读）+ 冲突预检（零持久化变更前）
    servers: list[MCPServerConfig] = []
    for rec in records:
        install_path = rec.get("installPath")
        if isinstance(install_path, str) and install_path:
            servers.extend(load_bundled_servers(Path(install_path)))
    owned = _bundled_servers_of(home, plugin_id, env)
    existing = existing_server_names() if existing_server_names is not None else set()
    _conflict_check(servers, existing, owned)

    # ② 写 enabledPlugins[id]=true（冲突预检通过后）
    _write_enabled_plugin(plugin_id, True, scope, project_path, env)

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

    # ④ 重挂 servers
    if register_server is not None:
        for cfg in servers:
            await register_server(cfg)
    logger.info("enabled plugin %r (scope=%s, skills recovered, servers remounted)", plugin_id, scope)
