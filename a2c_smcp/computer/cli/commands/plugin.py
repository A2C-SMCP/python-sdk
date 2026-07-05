# -*- coding: utf-8 -*-
# filename: plugin.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``plugin`` 命令 handler（install / uninstall / enable / disable / list / info / gc）+ 启动期 MCP 批准框。
Plugin command handlers + boot-time MCP approval box。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.3 / §9.2 / §10.6 / §12（S16，#69）。

与 marketplace.py 同范式：handler 取**显式资源**（``registry`` / ``home`` / ``env``）+ flags，返回退出码
（0 成功 / 1 用户错 / 2 网络错），包裹 :mod:`...settings.installer` 四动词 + :mod:`...settings.reconciler`
gc。MCP 注入回调（``existing_server_names`` / ``register_server`` / ``remove_server`` / ``inject_inputs``）由
REPL dispatcher 从活跃 ``Computer`` 装配后注入；Typer 非交互无 live Computer → 传 ``None``（ledger-only，
MCP 挂载延到下次 REPL boot 经批准框落地）。

**plugin 实时挂载 D2 上下文渲染（#69 Group A，§9.3 D2）**：plugin 的 ``mcp-servers/inputs.json`` 入池时 id
前缀化为 ``<plugin>@<marketplace>/<id>``；挂载 bundled server 时，``_plugin_register_cb`` 携 plugin/marketplace
上下文调 :meth:`Computer.aadd_or_aupdate_server`，使 server config 的裸 ``${input:id}`` 经 resolver D2 前缀回退
解析。``_plugin_inject_inputs_cb`` 负责在 register 前把 inputs 注入 Computer 池。

**MCP 批准框（#69 Group B，§9.2）**：:func:`run_mcp_approval` 在 REPL 启动期跑——解析 ``.tfrobot/mcp.json``
定义层、套门控、对 ``PENDING`` server 弹 y/a/n（写 local scope）；非交互无 TTY → skip+WARN（``--approve-all-mcp``
可全批、仅本次不落盘）。bundled / user-flag-policy origin 免批准（门控已判 ENABLED）。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from rich.table import Table

from a2c_smcp.computer.cli.commands import build_mcp_callbacks, flag_value, resolved_settings
from a2c_smcp.computer.cli.progress import clone_spinner
from a2c_smcp.computer.cli.utils import console
from a2c_smcp.computer.inputs.plugin_pool import load_plugin_inputs
from a2c_smcp.computer.settings.installer import (
    ExistingServerNames,
    InjectInputs,
    MCPServerNameConflictError,
    PluginInstallError,
    RegisterServer,
    RemoveServer,
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from a2c_smcp.computer.settings.mcp_config import (
    McpApprovalStatus,
    approve_all_project_mcp,
    approve_mcp_server,
    bundled_mcp_server_names,
    deny_mcp_server,
    gate_mcp_servers,
    resolve_mcp_config,
)
from a2c_smcp.computer.settings.reconciler import gc_plugins, list_orphan_plugins
from a2c_smcp.computer.skills.manifest import MCP_INPUTS_FILENAME, MCP_SERVERS_SUBDIR
from a2c_smcp.computer.skills.registry import SkillRegistry

# 退出码语义（§4.6）/ Exit code semantics（与 marketplace.py 对齐）。
EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_NETWORK_ERROR = 2


# ── 输出辅助 / output helpers ─────────────────────────────────────────────────
def _err(msg: str, *, json_output: bool, code: int = EXIT_USER_ERROR, error_code: str | None = None, **extra: Any) -> int:
    if json_output:
        payload: dict[str, Any] = {"error": error_code or "error", "message": msg, **extra}
        console.print_json(data=payload)
    else:
        console.print(f"[red]✗ {msg}[/red]")
    return code


def _ok(msg: str) -> int:
    console.print(f"[green]✓ {msg}[/green]")
    return EXIT_OK


def _installed_records(home: Path, env: Mapping[str, str] | None, plugin_id: str) -> list[dict[str, Any]]:
    """读 ``installed_plugins.json`` 中某 plugin 的全部 scope 记录（plain dict）/ All install records of a plugin。"""
    from a2c_smcp.computer.settings.store import load_installed_plugins

    return [dict(r) for r in load_installed_plugins(home=home, env=env).get("plugins", {}).get(plugin_id, [])]


def _split_plugin_id(plugin_id: str) -> tuple[str, str] | None:
    """``<plugin>@<marketplace>`` → ``(plugin, marketplace)``；格式非法 → ``None``。"""
    plugin, _, marketplace = plugin_id.partition("@")
    if not plugin or not marketplace:
        return None
    return plugin, marketplace


# ── plugin 实时挂载 D2 上下文渲染回调（#69 Group A）/ plugin-mount D2 context callbacks ──
def _plugin_register_cb(comp: Any, plugin: str, marketplace: str) -> RegisterServer:
    """携 plugin/marketplace 上下文的 register 回调：bundled server 的裸 ``${input:id}`` 经此解析到带前缀池条目。"""

    async def _register(cfg: Any) -> None:
        await comp.aadd_or_aupdate_server(cfg, plugin=plugin, marketplace=marketplace)

    return _register


def _plugin_inject_inputs_cb(comp: Any, plugin: str, marketplace: str) -> InjectInputs:
    """注入 plugin-scoped inputs 入池：从 ``<plugin_root>/mcp-servers/inputs.json`` 读、前缀化、逐条 add_or_update_input。"""

    async def _inject(plugin_root: Path) -> None:
        inputs_json = plugin_root / MCP_SERVERS_SUBDIR / MCP_INPUTS_FILENAME
        for inp in load_plugin_inputs(inputs_json, plugin, marketplace):
            comp.add_or_update_input(inp)

    return _inject


# ── handlers ──────────────────────────────────────────────────────────────────
async def plugin_install(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    plugin_id: str,
    *,
    scope: str = "user",
    project_path: str | None = None,
    version: str | None = None,
    existing_server_names: ExistingServerNames | None = None,
    register_server: RegisterServer | None = None,
    inject_inputs: InjectInputs | None = None,
    json_output: bool = False,
) -> int:
    """安装单个 plugin（外来 MCP 同名硬抛、原子失败，§10.6）/ Install a plugin (foreign name conflict hard-throws)。"""
    if _split_plugin_id(plugin_id) is None:
        return _err(f"invalid plugin id {plugin_id!r} (expected '<plugin>@<marketplace>')", json_output=json_output)
    try:
        with clone_spinner(f"Installing {plugin_id}...", enabled=not json_output):
            record = await install_plugin(
                plugin_id, registry, home,
                scope=scope, project_path=project_path, version=version, env=env,
                existing_server_names=existing_server_names, register_server=register_server, inject_inputs=inject_inputs,
            )
    except MCPServerNameConflictError as e:
        # §10.6 硬抛、退出码 1 + JSON error（无 rename/force 逃生口）。
        return _err(str(e), json_output=json_output, error_code="mcp_server_name_conflict")
    except PluginInstallError as e:
        return _err(str(e), json_output=json_output)

    servers = record.get("bundledMcpServers") or []
    if json_output:
        console.print_json(data={"installed": plugin_id, "scope": record.get("scope"), "bundledMcpServers": servers})
        return EXIT_OK
    detail = f" ({len(servers)} MCP server(s) mounted)" if servers else ""
    return _ok(f"installed {plugin_id!r}{detail}")


async def plugin_uninstall(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    plugin_id: str,
    *,
    keep_servers: bool = False,
    remove_server: RemoveServer | None = None,
    json_output: bool = False,
) -> int:
    """卸载单个 plugin（删 installPath 树 + 注销 skills + 级联停摘 bundled server + 删账本）/ Uninstall a plugin。"""
    ok = await uninstall_plugin(plugin_id, registry, home, env=env, keep_servers=keep_servers, remove_server=remove_server)
    if not ok:
        return _err(f"plugin {plugin_id!r} not installed (no-op)", json_output=json_output)
    if json_output:
        console.print_json(data={"uninstalled": plugin_id, "keptServers": keep_servers})
        return EXIT_OK
    return _ok(f"uninstalled {plugin_id!r}" + (" (servers kept)" if keep_servers else ""))


async def plugin_enable(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    plugin_id: str,
    *,
    existing_server_names: ExistingServerNames | None = None,
    register_server: RegisterServer | None = None,
    inject_inputs: InjectInputs | None = None,
    json_output: bool = False,
) -> int:
    """启用单个 plugin（廉价复原：重挂 server + 复活 skills）/ Enable a plugin (cheap restore)。

    **scope 契约**（installer §4.3）：从 ledger 逐 scope 读安装 scope 传入，绝不默认 ``user``，否则写错层、与 live
    态背离。多 scope 记录 → 逐 scope enable。挂载前先经 ``inject_inputs`` 把 plugin-scoped inputs 注入池（#69 Group A）。
    """
    records = _installed_records(home, env, plugin_id)
    if not records:
        return _err(f"plugin {plugin_id!r} not installed (install it first)", json_output=json_output)
    try:
        for rec in records:
            install_path = rec.get("installPath")
            if inject_inputs is not None and isinstance(install_path, str) and install_path:
                await inject_inputs(Path(install_path))
            await enable_plugin(
                plugin_id, registry, home,
                scope=str(rec.get("scope", "user")), project_path=rec.get("projectPath"), env=env,
                existing_server_names=existing_server_names, register_server=register_server,
            )
    except MCPServerNameConflictError as e:
        return _err(str(e), json_output=json_output, error_code="mcp_server_name_conflict")
    except PluginInstallError as e:
        return _err(str(e), json_output=json_output)
    if json_output:
        console.print_json(data={"enabled": plugin_id, "scopes": [r.get("scope") for r in records]})
        return EXIT_OK
    return _ok(f"enabled {plugin_id!r}")


async def plugin_disable(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    plugin_id: str,
    *,
    remove_server: RemoveServer | None = None,
    json_output: bool = False,
) -> int:
    """禁用单个 plugin = 整 plugin 下线（停摘 bundled server + 隐藏 skills；物化层保留可一键复原）/ Disable a plugin。

    **scope 契约**：同 :func:`plugin_enable`，从 ledger 逐 scope 读。
    """
    records = _installed_records(home, env, plugin_id)
    if not records:
        return _err(f"plugin {plugin_id!r} not installed (no-op)", json_output=json_output)
    for rec in records:
        await disable_plugin(
            plugin_id, registry, home,
            scope=str(rec.get("scope", "user")), project_path=rec.get("projectPath"), env=env, remove_server=remove_server,
        )
    if json_output:
        console.print_json(data={"disabled": plugin_id, "scopes": [r.get("scope") for r in records]})
        return EXIT_OK
    return _ok(f"disabled {plugin_id!r} (whole plugin offline; enable to restore)")


def _enabled_plugins_view(home: Path, env: Mapping[str, str] | None) -> dict[str, Any]:
    """合并视图的 ``enabledPlugins`` 映射（``id → bool``，缺省视为启用）/ Merged enabledPlugins map。"""
    ep = resolved_settings(env).get("enabledPlugins")
    return dict(ep) if isinstance(ep, Mapping) else {}


def plugin_list(
    home: Path,
    env: Mapping[str, str] | None,
    *,
    available: bool = False,
    json_output: bool = False,
) -> int:
    """列出 installed plugin（默认 enabled；``--available`` 含 disabled）/ List installed plugins。"""
    from a2c_smcp.computer.settings.store import load_installed_plugins

    installed = load_installed_plugins(home=home, env=env).get("plugins", {})
    enabled_map = _enabled_plugins_view(home, env)

    rows: list[dict[str, Any]] = []
    for pid, records in installed.items():
        enabled = enabled_map.get(pid) is not False  # 缺省 / true = 启用；显式 false = 禁用
        if not enabled and not available:
            continue
        scopes = sorted({str(r.get("scope")) for r in records})
        bundled = sorted({s for r in records for s in (r.get("bundledMcpServers") or [])})
        rows.append({"id": pid, "enabled": enabled, "scopes": scopes, "bundledMcpServers": bundled})
    rows.sort(key=lambda r: str(r["id"]))

    if json_output:
        console.print_json(data=rows)
        return EXIT_OK
    if not rows:
        console.print("[dim]No plugins installed. Install one: plugin install <plugin>@<marketplace>[/dim]")
        return EXIT_OK
    table = Table(title="Plugins", header_style="bold magenta")
    for col in ("Plugin", "Enabled", "Scopes", "MCP servers"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["id"]),
            "✓" if r["enabled"] else "—",
            ", ".join(r["scopes"]) or "—",
            ", ".join(r["bundledMcpServers"]) or "—",
        )
    console.print(table)
    return EXIT_OK


def plugin_info(
    home: Path,
    env: Mapping[str, str] | None,
    plugin_id: str,
    *,
    json_output: bool = False,
) -> int:
    """plugin 详情：scope / installPath / version / commitSha / enabled / bundledMcpServers / installedAt。"""
    records = _installed_records(home, env, plugin_id)
    if not records:
        return _err(f"plugin {plugin_id!r} not installed", json_output=json_output)
    enabled = _enabled_plugins_view(home, env).get(plugin_id) is not False
    info: dict[str, Any] = {"id": plugin_id, "enabled": enabled, "records": records}
    if json_output:
        console.print_json(data=info)
        return EXIT_OK
    table = Table(title=f"Plugin · {plugin_id}", show_header=False)
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")
    table.add_row("enabled", "✓" if enabled else "—")
    for idx, rec in enumerate(records):
        prefix = f"[{rec.get('scope')}] " if len(records) > 1 else ""
        for k in ("scope", "installPath", "version", "commitSha", "installedAt", "bundledMcpServers"):
            if k in rec:
                table.add_row(f"{prefix}{k}", str(rec[k]))
        if idx < len(records) - 1:
            table.add_row("", "")
    console.print(table)
    return EXIT_OK


async def plugin_gc(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    *,
    mcp_teardown: Callable[[list[str]], Awaitable[None]] | None = None,
    confirm: Callable[[list[str]], Awaitable[bool]] | None = None,
    json_output: bool = False,
) -> int:
    """清理孤儿 plugin（所有 scope 都不再声明 enabledPlugins[id]）/ GC orphan plugins。"""
    declared = resolved_settings(env)
    orphans = list_orphan_plugins(home, declared, env=env)
    if not orphans:
        if json_output:
            console.print_json(data={"removed": []})
        else:
            console.print("[dim]No orphan plugins.[/dim]")
        return EXIT_OK
    if confirm is not None and not await confirm(orphans):
        return _err("aborted by user", json_output=json_output)
    removed = await gc_plugins(orphans, registry, home, env=env, mcp_teardown=mcp_teardown)
    if json_output:
        console.print_json(data={"removed": removed})
        return EXIT_OK
    return _ok(f"gc removed {len(removed)} orphan plugin(s): {', '.join(removed) or '—'}")


# ── MCP 批准框（启动期，§9.2，#69 Group B）/ MCP approval box (boot-time) ─────────
def _mount_dict(srv: Any) -> dict[str, Any]:
    """合并 resolved server 的 ``config``（含占位符）+ ``ext``（剥离的 envFile 等扩展字段）为挂载用 dict。

    ★ 必须合回 ``ext``，否则 spawn 时 ``_apply_env_file`` 看不到 ``envFile`` → 静默丢失（#69 Group B 风险 2）。
    """
    return {**srv.config.model_dump(mode="json"), **dict(srv.ext)}


async def run_mcp_approval(
    comp: Any,
    session: Any | None,
    *,
    approve_all: bool = False,
    flag_config: Path | None = None,
) -> None:
    """启动期解析 ``.tfrobot/mcp.json`` 定义层 + 批准门控 + 挂载 ENABLED server（§9.2）/ Boot-time MCP approval + mount。

    - bundled / user-flag-policy origin server → 门控判 ENABLED → 直挂（免批准框）；
    - DISABLED（企业拒绝/不在白名单/显式 disabled）→ 跳过；
    - PENDING（工作区共享未决）→ TTY 弹 y/a/n 写 local scope；非 TTY → skip+WARN，``approve_all`` 全批（仅本次不落盘）。

    ``--approve-all-mcp`` 在**非交互**仅本次挂载、**不落盘**；TTY ``[a]`` 才写 enableAllProjectMcpServers（用户显式决定）。

    **flag 层 schema 区分**（fix-review #1）：``flag_config`` 源自 ``--settings <file>``，是 **settings.json** flag 层
    （喂 :func:`resolved_settings` 的 ``flag_path``——``{enabledPlugins/MCP 门控字段…}``）。它**不是 mcp.json**，故
    **不**喂 ``resolve_mcp_config(flag_config_path=)``（后者期望 ``{servers,inputs}``、schema 不符）；flag-scope
    **mcp.json 当前无 CLI 入口**（旧 ``--config`` 仅 ``_run_impl`` server 预加载、从不喂门控解析）。
    """
    env = os.environ
    # flag_config 是 settings.json（见 docstring）→ resolve_mcp_config 不收（避免 settings.json 当 mcp.json 误读）。
    # #116：project/local 层由 resolve_mcp_config 内部锚定进程 cwd。
    resolved = resolve_mcp_config(env=env, flag_config_path=None)

    # 被 drop 的畸形 server/input 必须呈现（§5.6 / mcp_config 容错不静默，#69 Group B 风险 3）。
    for e in resolved.errors:
        console.print(f"[yellow]⚠ mcp.json: {e}[/yellow]")

    if not resolved.servers:
        return

    settings = resolved_settings(env, flag_path=flag_config)
    bundled = bundled_mcp_server_names(comp.skill_home, env)
    statuses = gate_mcp_servers(resolved, settings, bundled)

    # mcp.json 定义的 input 入池（无前缀），供 server config 的裸 ${input:} 解析（#69 Group B 风险 3）。
    for inp in resolved.inputs:
        comp.add_or_update_input(inp)

    approved_all_session = approve_all  # 非交互 --approve-all-mcp，或 TTY 选过一次 [a]

    async def _mount(name: str) -> None:
        try:
            await comp.aadd_or_aupdate_server(_mount_dict(resolved.servers[name]), session=session)
            console.print(f"[green]✓ mounted MCP server {name!r}[/green]")
        except Exception as exc:  # 单个 server 挂载失败不阻断其余 / one failure must not block the rest
            console.print(f"[red]✗ failed to mount MCP server {name!r}: {exc}[/red]")

    for name, status in statuses.items():
        if status == McpApprovalStatus.ENABLED:
            await _mount(name)
        elif status == McpApprovalStatus.DISABLED:
            console.print(f"[dim]· MCP server {name!r} disabled (policy/denied), skipped[/dim]")
        else:  # PENDING
            if approved_all_session:
                await _mount(name)
                continue
            if session is None:  # 非 TTY 且未 --approve-all-mcp → skip + WARN
                console.print(
                    f"[yellow]⚠ skipped pending MCP server {name!r} (no TTY); approve in REPL or pass --approve-all-mcp[/yellow]",
                )
                continue
            srv = resolved.servers[name]
            prompt = (
                f"⚠ Unapproved workspace MCP server '{name}' (origin={srv.origin})\n"
                "  [y]es this server · [a]ll project servers · [n]o (deny): "
            )
            ans = (await session.prompt_async(prompt)).strip().lower()
            if ans in {"a", "all"}:
                approve_all_project_mcp()
                approved_all_session = True
                await _mount(name)
            elif ans in {"y", "yes"}:
                approve_mcp_server(name)
                await _mount(name)
            else:
                deny_mcp_server(name)
                console.print(f"[dim]· denied MCP server {name!r} (written to local scope)[/dim]")


# ── REPL dispatcher（绑定 Computer 的活跃 registry / home / session）/ REPL adapter ──
async def repl_dispatch(comp: Any, parts: list[str], *, session: Any) -> None:
    """把 ``plugin ...`` REPL 行解析为 flags → 调 handler；变更后触发去抖 emit / Parse a REPL line and dispatch。"""
    sub = parts[1].lower()
    args = parts[2:]
    registry = comp.skill_registry
    home = comp.skill_home
    env = os.environ
    json_output = "--json" in args
    pos = [a for a in args if not a.startswith("--")]
    cbs = build_mcp_callbacks(comp)
    # #116：project/local scope 的 project_path 锚定进程 cwd（仅 install 分支消费；enable/disable 从 ledger 读 projectPath）。
    project_path = os.getcwd()

    if sub == "install":
        if not pos:
            console.print("[yellow]usage: plugin install <plugin>@<marketplace> [--version V] [--scope user|project|local][/yellow]")
            return
        plugin_id = pos[0]
        split = _split_plugin_id(plugin_id)
        if split is None:
            console.print("[yellow]invalid plugin id (expected '<plugin>@<marketplace>')[/yellow]")
            return
        plugin, marketplace = split
        scope = flag_value(args, "--scope") or "user"
        code = await plugin_install(
            registry, home, env, plugin_id,
            scope=scope, project_path=project_path if scope in ("project", "local") else None,
            version=flag_value(args, "--version"),
            existing_server_names=cbs.existing_server_names,
            register_server=_plugin_register_cb(comp, plugin, marketplace),
            inject_inputs=_plugin_inject_inputs_cb(comp, plugin, marketplace),
            json_output=json_output,
        )
        if code == EXIT_OK:
            comp.mark_skills_dirty()
    elif sub == "uninstall":
        if not pos:
            console.print("[yellow]usage: plugin uninstall <plugin>@<marketplace> [--keep-servers][/yellow]")
            return
        code = await plugin_uninstall(
            registry, home, env, pos[0],
            keep_servers="--keep-servers" in args, remove_server=cbs.remove_server, json_output=json_output,
        )
        if code == EXIT_OK:
            comp.mark_skills_dirty()
    elif sub == "enable":
        if not pos:
            console.print("[yellow]usage: plugin enable <plugin>@<marketplace>[/yellow]")
            return
        split = _split_plugin_id(pos[0])
        if split is None:
            console.print("[yellow]invalid plugin id (expected '<plugin>@<marketplace>')[/yellow]")
            return
        plugin, marketplace = split
        code = await plugin_enable(
            registry, home, env, pos[0],
            existing_server_names=cbs.existing_server_names,
            register_server=_plugin_register_cb(comp, plugin, marketplace),
            inject_inputs=_plugin_inject_inputs_cb(comp, plugin, marketplace),
            json_output=json_output,
        )
        if code == EXIT_OK:
            comp.mark_skills_dirty()
    elif sub == "disable":
        if not pos:
            console.print("[yellow]usage: plugin disable <plugin>@<marketplace>[/yellow]")
            return
        code = await plugin_disable(registry, home, env, pos[0], remove_server=cbs.remove_server, json_output=json_output)
        if code == EXIT_OK:
            comp.mark_skills_dirty()
    elif sub == "list":
        plugin_list(home, env, available="--available" in args, json_output=json_output)
    elif sub == "info":
        if not pos:
            console.print("[yellow]usage: plugin info <plugin>@<marketplace>[/yellow]")
            return
        plugin_info(home, env, pos[0], json_output=json_output)
    elif sub == "gc":

        async def _confirm(orphans: list[str]) -> bool:
            ans = (await session.prompt_async(f"GC {len(orphans)} orphan plugin(s): {', '.join(orphans)}? [y/N]: ")).strip().lower()
            return ans in {"y", "yes"}

        code = await plugin_gc(
            registry, home, env,
            mcp_teardown=_batch_teardown(cbs.remove_server), confirm=_confirm, json_output=json_output,
        )
        if code == EXIT_OK:
            comp.mark_skills_dirty()
    else:
        console.print(f"[yellow]unknown plugin subcommand: {sub}[/yellow]")


def _batch_teardown(remove_server: RemoveServer) -> Callable[[list[str]], Awaitable[None]]:
    """把单个 ``remove_server`` 包成批量 teardown 回调（gc_plugins 的 ``mcp_teardown`` 入参为 name 列表）。"""

    async def _teardown(names: list[str]) -> None:
        for name in names:
            await remove_server(name)

    return _teardown
