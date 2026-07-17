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
gc。MCP 注入回调（``existing_bundle_ids`` / ``register_server`` / ``remove_server`` / ``inject_inputs``）由
REPL dispatcher 从活跃 ``Computer`` 装配后注入；Typer 非交互无 live Computer → 传 ``None``。

**v0.3.0 语义（#123）**：``plugin install`` 只安装**不激活**（不挂 server、不投影 skills → ``installed_disabled``；
仅传 ``existing_bundle_ids`` 做依赖预检）；``plugin enable`` 才原子点亮 skills + bundled server（挂载失败
回滚 ``installed_disabled``，故 enable 也注入 ``remove_server``）。``plugin list`` 默认列出**全部已安装**
（enabled 列呈现两态；install 后必须可见）。

**plugin 实时挂载 D2 上下文渲染（#69 Group A，§9.3 D2）**：plugin 的 ``mcp-servers/inputs.json`` 入池时 id
前缀化为 ``<plugin>@<marketplace>/<id>``；enable 挂载 bundled server 时，``_plugin_register_cb`` 携
plugin/marketplace 上下文调 transient :meth:`Computer.amount_server`（#137 ③：治理投影不回写 mcp.json），使
server config 的裸 ``${input:id}`` 经 resolver D2 前缀回退解析。``_plugin_inject_inputs_cb`` 负责在 register 前把
inputs 注入 Computer 池。

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

from a2c_smcp.computer.cli.commands import (
    build_mcp_callbacks,
    flag_value,
    format_settings_errors,
    resolved_settings,
    resolved_settings_with_errors,
)
from a2c_smcp.computer.cli.progress import clone_spinner
from a2c_smcp.computer.cli.utils import console
from a2c_smcp.computer.inputs.plugin_pool import load_plugin_inputs
from a2c_smcp.computer.mcp_clients.model import MCPServerInput
from a2c_smcp.computer.settings.installer import (
    ExistingBundleIds,
    InjectInputs,
    NonPluginBundleIds,
    PluginInstallError,
    RegisterServer,
    RemoveServer,
    disable_plugin,
    enable_plugin,
    install_plugin,
    prune_plugin_intent,
    uninstall_plugin,
)
from a2c_smcp.computer.settings.mcp_config import (
    McpApprovalStatus,
    approve_all_project_mcp,
    approve_mcp_server,
    deny_mcp_server,
    gate_mcp_servers,
)
from a2c_smcp.computer.settings.reconciler import (
    DANGLING_CATALOG_MISSING,
    DANGLING_ENTRY_MISSING,
    DANGLING_MANIFEST_UNREADABLE,
    DANGLING_MARKETPLACE_NOT_ADDED,
    declared_installed_plugin_ids,
    gc_plugins,
    ledger_entry_materialized,
    list_dangling_plugin_intents,
    list_orphan_plugins,
)
from a2c_smcp.computer.settings.schema import SettingsScope
from a2c_smcp.computer.skills.manifest import MCP_INPUTS_FILENAME, MCP_SERVERS_SUBDIR
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.utils.bundle_id import resolve_bundle_id
from a2c_smcp.utils.env_segment import detect_env_name_collisions
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

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
    """携 plugin/marketplace 上下文的 register 回调：bundled server 的裸 ``${input:id}`` 经此解析到带前缀池条目。

    #137 ③：bundled 挂载 = 治理投影（ledger 是真相），走 transient :meth:`Computer.amount_server`，**不回写** mcp.json。
    """

    async def _register(cfg: Any) -> None:
        await comp.amount_server(cfg, plugin=plugin, marketplace=marketplace)

    return _register


def _inject_inputs_collision_safe(comp: Any, inputs: list[MCPServerInput], *, source: str) -> None:
    """把一批 input 注入池，**跳过**会造成 env 名坍缩的条目并红字呈现（DRY 单点，#155）。

    分层依据（本仓既有边界，#136）：**写层 fail-fast、读层容错**。库层
    :meth:`BaseInputResolver.__init__` 对坍缩是硬错误（协议 §「环境变量命名规则」MUST）；CLI 是**读层**，
    对声明面容错——肇事条目跳过 + 呈现，不连坐掉整条挂载管线（与 ``load_plugin_inputs`` 对畸形条目
    「WARN 跳过、不抛」同款）。

    为何须**批量前检**而非依赖逐条注入时抛：``add_or_update_input`` 自 #155 起会抛，裸循环撞名即
    中途 abort ⇒ 前面的已注入、后面的被静默丢弃（部分注入）。前检使注入要么全成、要么只少肇事项。

    肇事组内**每一个** id 都跳过（无一方有优先权，符合「两者均已跳过」的呈现语义）；已在池内的既有
    id 不动（无法追溯撤回，且既有池自身恒无坍缩）。
    """
    existing = {i.id for i in comp.list_inputs()}
    collisions = detect_env_name_collisions(existing | {i.id for i in inputs})
    skipped = {i for ids in collisions.values() for i in ids}
    for name, ids in sorted(collisions.items()):
        joined = ", ".join(repr(i) for i in ids)
        console.print(
            f"[red]❌ {source}: input id {joined} 均映射到同一环境变量名 {name!r}，两者已跳过，请改 id 消歧 / "
            f"input ids collide on one env var name, all skipped; rename to disambiguate[/red]",
        )
    for inp in inputs:
        if inp.id in skipped:
            continue
        comp.add_or_update_input(inp)


def _inject_plugin_inputs(comp: Any, plugin_root: Path, plugin: str, marketplace: str) -> None:
    """从 ``<plugin_root>/mcp-servers/inputs.json`` 读 plugin-scoped inputs、前缀化、注入池（DRY 单点）。"""
    inputs_json = plugin_root / MCP_SERVERS_SUBDIR / MCP_INPUTS_FILENAME
    _inject_inputs_collision_safe(comp, load_plugin_inputs(inputs_json, plugin, marketplace), source=f"plugin {plugin}@{marketplace}")


def _plugin_inject_inputs_cb(comp: Any, plugin: str, marketplace: str) -> InjectInputs:
    """注入 plugin-scoped inputs 入池（install/enable 路径回调；闭包携 plugin/marketplace 上下文）。"""

    async def _inject(plugin_root: Path) -> None:
        _inject_plugin_inputs(comp, plugin_root, plugin, marketplace)

    return _inject


async def run_governance_remount(comp: Any, *, settings_flag_path: Path | None = None) -> None:
    """
    启动期治理重挂（#117 设计 Y 的 client 接线参考实现）/ Boot-time governance remount (reference client wiring)。

    boot_up 已恢复 bundled SKILL（skills-only，§4.8 #93 边界：SDK 不擅自拉 MCP 进程）；此处 CLI 作为
    参考 client 经**公共 API** ``Computer.reconcile_governance(hooks)`` 显式重挂 enabled bundled MCP
    server（外部 client / 未来 GUI 照抄本函数）。语义要点：

    - declared 传 flag-aware 合并视图——注意其"补上 flag scope"**仅及于阶段二 server 重挂**（boot 从不挂
      server）；bundled SKILL 已在 boot 期按无 flag 视图恢复且 additive-only 不撤销，flag-scope 的
      ``enabledPlugins=false`` 对 skill 跨 boot 不生效（可靠 disable 请写 user scope）；
    - inputs 注入先于 register（bundled server 的 ``${input:}`` 经 D2 前缀回退解析，与 install/enable 流一致）；
    - ``existing_bundle_ids`` **必传**（#153：取 :func:`build_mcp_callbacks` 装配的**运行期权威**集，
      **不是** ``comp.mcp_servers`` 构造期快照——后者 CLI 下恒空，会把已挂的用户 server 判成不存在、
      令 bundled server 覆盖它）：同 bundle_id 已有 = 依赖已满足 → reconcile_governance 内部 skip（用户配置胜）；
      bundled **免批准**（§5.10 不走 project 信任门），单点失败不阻断。
    """
    env = os.environ
    declared = resolved_settings(env, flag_path=settings_flag_path)

    async def _register(cfg: Any, record: Any) -> None:
        # #137 ③：治理重挂 = 投影（ledger 是真相），走 transient amount_server，不回写 mcp.json。
        await comp.amount_server(cfg, plugin=record.plugin, marketplace=record.marketplace)
        console.print(f"[green]✓ restored bundled MCP server {cfg.name!r} (plugin {record.plugin_id})[/green]")

    async def _inject(record: Any) -> None:
        _inject_plugin_inputs(comp, record.install_path, record.plugin, record.marketplace)

    report = await comp.reconcile_governance(
        existing_bundle_ids=build_mcp_callbacks(comp).existing_bundle_ids,
        register_server=_register,
        inject_inputs=_inject,
        declared=declared,
    )
    for marketplace in report.failed_marketplaces:
        console.print(f"[yellow]⚠ marketplace {marketplace!r} degraded during governance recovery (skills/servers not restored)[/yellow]")


def _satisfied_deps(deps: list[str], existing_bundle_ids: ExistingBundleIds | None) -> list[str]:
    """本次声明的依赖里**已满足**（同 bundle_id 本地已有）的部分（协议 §2.5-1）/ Already-satisfied deps。"""
    if existing_bundle_ids is None:
        return []
    existing = existing_bundle_ids()
    return [d for d in deps if d in existing]


def _print_satisfied_deps(satisfied: list[str]) -> None:
    """
    提示「依赖已满足」（协议 §2.5-1 SHOULD 提示）/ Report dependency-satisfied to the user。

    D3 前此处是**硬失败**（``MCPServerNameConflictError``，退出码 1，「name 即身份、无逃生口」）——该裁决已被
    协议推翻：plugin 与 MCP Server 是依赖关系，同 bundle_id 已有 = 依赖已满足，MUST NOT 拒绝。
    """
    for bundle_id in satisfied:
        console.print(
            f"[cyan]ℹ dependency satisfied: MCP server {bundle_id!r} already exists locally; this plugin reuses it "
            f"rather than creating a new one. Configs reconcile by scope precedence. Uninstalling this plugin will "
            f"not remove it.[/cyan]",
        )


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
    existing_bundle_ids: ExistingBundleIds | None = None,
    json_output: bool = False,
) -> int:
    """安装单个 plugin（**不激活**：写 installedPlugins + 物化 → ``installed_disabled``，v0.3.0 §2.4）/ Install。

    外来 MCP 同名硬抛、原子失败（§10.6 预检保留）；skills/bundled server 由 ``plugin enable`` 原子点亮。
    """
    if _split_plugin_id(plugin_id) is None:
        return _err(f"invalid plugin id {plugin_id!r} (expected '<plugin>@<marketplace>')", json_output=json_output)
    try:
        with clone_spinner(f"Installing {plugin_id}...", enabled=not json_output):
            record = await install_plugin(
                plugin_id, registry, home,
                scope=scope, project_path=project_path, version=version, env=env,
                existing_bundle_ids=existing_bundle_ids,
            )
    except PluginInstallError as e:
        return _err(str(e), json_output=json_output)

    deps = record.get("mcpServers") or []
    if json_output:
        console.print_json(
            data={"installed": plugin_id, "scope": record.get("scope"), "state": "installed_disabled", "mcpServers": deps},
        )
        return EXIT_OK
    # 依赖已满足提示（协议 §2.5-1）：install 不挂载 ⇒ 活跃集不变 ⇒ 事后算与事前算等价。
    _print_satisfied_deps(_satisfied_deps(deps, existing_bundle_ids))
    detail = f" ({len(deps)} MCP server(s) declared as dependencies, not mounted)" if deps else ""
    return _ok(f"installed {plugin_id!r}{detail} (disabled; run 'plugin enable {plugin_id}' to activate)")


async def plugin_uninstall(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    plugin_id: str,
    *,
    non_plugin_bundle_ids: NonPluginBundleIds,
    keep_servers: bool = False,
    remove_server: RemoveServer | None = None,
    json_output: bool = False,
) -> int:
    """卸载单个 plugin（删 installPath 树 + 注销 skills + **按判据**回收 bundled server + 删账本）/ Uninstall a plugin。"""
    ok = await uninstall_plugin(
        plugin_id,
        registry,
        home,
        non_plugin_bundle_ids=non_plugin_bundle_ids,
        env=env,
        keep_servers=keep_servers,
        remove_server=remove_server,
    )
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
    existing_bundle_ids: ExistingBundleIds | None = None,
    register_server: RegisterServer | None = None,
    remove_server: RemoveServer | None = None,
    inject_inputs: InjectInputs | None = None,
    json_output: bool = False,
) -> int:
    """启用单个 plugin（**原子激活**：skills 与 bundled server 一并点亮，失败回滚 installed_disabled）/ Enable。

    **scope 契约**（installer §4.3）：从 ledger 逐 scope 读安装 scope 传入，绝不默认 ``user``，否则写错层、与 live
    态背离。多 scope 记录 → 逐 scope enable。挂载前先经 ``inject_inputs`` 把 plugin-scoped inputs 注入池（#69 Group A）；
    ``remove_server`` 供挂载失败时回滚摘除本次新增 server（v0.3.0 §2.4 enable 原子性）。

    「依赖已满足」提示 **MUST 在 enable 之前算**（协议 §2.5-1）：enable 会挂载未满足的依赖、改变活跃集，
    事后再算就分不清「本来就有」与「刚被自己挂上」——这与 install 路径（不挂载、事后算等价）不同。
    """
    records = _installed_records(home, env, plugin_id)
    if not records:
        return _err(f"plugin {plugin_id!r} not installed (install it first)", json_output=json_output)
    satisfied = _satisfied_deps(sorted({s for r in records for s in (r.get("mcpServers") or [])}), existing_bundle_ids)
    try:
        for rec in records:
            install_path = rec.get("installPath")
            if inject_inputs is not None and isinstance(install_path, str) and install_path:
                await inject_inputs(Path(install_path))
            await enable_plugin(
                plugin_id, registry, home,
                scope=str(rec.get("scope", "user")), project_path=rec.get("projectPath"), env=env,
                existing_bundle_ids=existing_bundle_ids, register_server=register_server, remove_server=remove_server,
            )
    except PluginInstallError as e:
        return _err(str(e), json_output=json_output)
    if json_output:
        console.print_json(data={"enabled": plugin_id, "scopes": [r.get("scope") for r in records]})
        return EXIT_OK
    _print_satisfied_deps(satisfied)  # 用 enable 前的快照：此刻活跃集已含本次挂上的，事后算会失真
    return _ok(f"enabled {plugin_id!r}")


async def plugin_disable(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    plugin_id: str,
    *,
    non_plugin_bundle_ids: NonPluginBundleIds,
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
            non_plugin_bundle_ids=non_plugin_bundle_ids,
            scope=str(rec.get("scope", "user")), project_path=rec.get("projectPath"), env=env, remove_server=remove_server,
        )
    if json_output:
        console.print_json(data={"disabled": plugin_id, "scopes": [r.get("scope") for r in records]})
        return EXIT_OK
    return _ok(f"disabled {plugin_id!r} (whole plugin offline; enable to restore)")


def _enabled_plugins_view(home: Path, env: Mapping[str, str] | None) -> dict[str, Any]:
    """合并视图的 ``enabledPlugins`` 映射（``id → bool``；v0.3.0 仅显式 ``true`` 为启用）/ Merged enabledPlugins map。"""
    ep = resolved_settings(env).get("enabledPlugins")
    return dict(ep) if isinstance(ep, Mapping) else {}


def plugin_list(
    home: Path,
    env: Mapping[str, str] | None,
    *,
    available: bool = False,
    json_output: bool = False,
) -> int:
    """列出全部 installed plugin（enabled 列呈现两态；install-only 的 ``installed_disabled`` 必须可见）/ List。

    v0.3.0（#123）：默认即列全部已安装——``--available`` 保留为兼容 no-op（旧语义"含 disabled"已成默认），
    **已弃用、计划移除**（#125 任务 3）：非 JSON 模式打弃用提示；JSON 模式走 logger（stdout 保持纯 JSON）。
    """
    from a2c_smcp.computer.settings.store import load_installed_plugins

    if available:  # 弃用提示（deprecation notice）——行为不变，仅提示
        msg = "--available is deprecated (listing all installed plugins is the default since v0.3.0) and will be removed"
        if json_output:
            logger.warning("plugin list: %s", msg)
        else:
            console.print(f"[yellow]⚠ {msg} / --available 已弃用（v0.3.0 起默认列全部已安装），将在后续版本移除[/yellow]")

    installed = load_installed_plugins(home=home, env=env).get("plugins", {})
    enabled_map = _enabled_plugins_view(home, env)

    rows: list[dict[str, Any]] = []
    for pid, records in installed.items():
        enabled = enabled_map.get(pid) is True  # 仅显式 true = 启用（缺省翻转，v0.3.0 §2.4）
        scopes = sorted({str(r.get("scope")) for r in records})
        deps = sorted({s for r in records for s in (r.get("mcpServers") or [])})
        rows.append({"id": pid, "enabled": enabled, "scopes": scopes, "mcpServers": deps})
    rows.sort(key=lambda r: str(r["id"]))

    if json_output:
        console.print_json(data=rows)
        return EXIT_OK
    if not rows:
        console.print("[dim]No plugins installed. Install one: plugin install <plugin>@<marketplace>[/dim]")
        return EXIT_OK
    table = Table(title="Plugins", header_style="bold magenta")
    for col in ("Plugin", "Enabled", "Scopes", "MCP deps (bundle_id)"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["id"]),
            "✓" if r["enabled"] else "—",
            ", ".join(r["scopes"]) or "—",
            ", ".join(r["mcpServers"]) or "—",
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
    """plugin 详情：scope / installPath / version / commitSha / enabled / mcpServers / installedAt。"""
    records = _installed_records(home, env, plugin_id)
    if not records:
        return _err(f"plugin {plugin_id!r} not installed", json_output=json_output)
    enabled = _enabled_plugins_view(home, env).get(plugin_id) is True  # 仅显式 true（缺省翻转，v0.3.0）
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
        for k in ("scope", "installPath", "version", "commitSha", "installedAt", "mcpServers"):
            if k in rec:
                table.add_row(f"{prefix}{k}", str(rec[k]))
        if idx < len(records) - 1:
            table.add_row("", "")
    console.print(table)
    return EXIT_OK


# 悬挂意图 reason → 分档修复提示（catalog-missing 可能只是断网后 clone 未建，裁量留给用户）/ per-reason hints.
_DANGLING_HINTS: dict[str, str] = {
    DANGLING_MARKETPLACE_NOT_ADDED: "marketplace unknown — no self-heal path",
    DANGLING_CATALOG_MISSING: "boot/'marketplace refresh' may re-clone; prune only if the source is gone for good",
    DANGLING_MANIFEST_UNREADABLE: "try 'marketplace refresh' first; prune only if it stays broken",
    DANGLING_ENTRY_MISSING: "prune only if upstream removed the plugin",
}


def _recoverable_intents(
    home: Path,
    declared: Mapping[str, Any],
    dangling: list[tuple[str, str]],
    env: Mapping[str, str] | None,
) -> list[str]:
    """「静态可达但未物化」的意图 pid（下次 boot 由 recovery 重物化自愈——只提示、绝不 prune）/ Recoverable。"""
    from a2c_smcp.computer.settings.store import load_installed_plugins

    ledger = load_installed_plugins(home=home, env=env).get("plugins", {})
    dangling_ids = {pid for pid, _ in dangling}
    return sorted(
        pid
        for pid in declared_installed_plugin_ids(declared)
        if pid not in dangling_ids and not ledger_entry_materialized(ledger.get(pid))
    )


async def plugin_gc(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    *,
    non_plugin_bundle_ids: NonPluginBundleIds,
    mcp_teardown: Callable[[list[str]], Awaitable[None]] | None = None,
    confirm: Callable[[list[str]], Awaitable[bool]] | None = None,
    prune_dangling: bool = False,
    json_output: bool = False,
) -> int:
    """
    清理孤儿 plugin（账本 ∖ 意图）+ 诊断/prune 悬挂意图（意图 ∖ 账本 ∧ 静态不可达，#125 任务 2）/ GC + prune。

    权威性不对称安全阀：孤儿删除的是**派生缓存**（恒安全）→ 无 confirm 也自动执行（Typer 现状不变）；
    悬挂 prune 删的是**权威意图**（§2.3）→ 须 confirm 门（REPL）或显式 ``--prune-dangling``
    （Typer 非交互缺省只诊断）。「静态可达未物化」→ ``recoverable``：仅提示（下次 boot 自愈）、绝不删。
    JSON 契约：``removed``（不变）+ ``dangling``（诊断 ``{id, reason}``）+ ``prunedIntents``（实际删）
    + ``recoverable``。
    """
    declared = resolved_settings(env)
    orphans = list_orphan_plugins(home, declared, env=env)
    dangling = list_dangling_plugin_intents(home, declared, env=env)
    recoverable = _recoverable_intents(home, declared, dangling, env)
    prunable = dangling if prune_dangling else []

    if not orphans and not dangling and not recoverable:
        if json_output:
            console.print_json(data={"removed": [], "dangling": [], "prunedIntents": [], "recoverable": []})
        else:
            console.print("[dim]No orphan plugins.[/dim]")
        return EXIT_OK

    if confirm is not None and (orphans or prunable):
        items = list(orphans) + [f"{pid} (dangling intent: {reason})" for pid, reason in prunable]
        if not await confirm(items):
            return _err("aborted by user", json_output=json_output)

    removed = (
        await gc_plugins(
            orphans, registry, home, non_plugin_bundle_ids=non_plugin_bundle_ids, env=env, mcp_teardown=mcp_teardown,
        )
        if orphans
        else []
    )
    pruned: list[str] = []
    residual: list[str] = []
    for pid, _reason in prunable:
        try:
            # False = cwd 可见 project/local 层仍有 committable 声明残留（merged 意图仍含 pid）——
            # 不计入 prunedIntents，否则自动化「prune 到干净」永不收敛（隔离审查 🟡#1）。
            if prune_plugin_intent(pid, home, env=env):
                pruned.append(pid)
            else:
                residual.append(pid)
        except Exception as e:  # noqa: BLE001 - 单条失败降级不阻断其余（OSError/锁竞争等，隔离审查 🟡#2）
            if not json_output:
                console.print(f"[yellow]⚠ prune {pid!r} failed: {e}[/yellow]")
            logger.warning("plugin gc: prune %r failed: %s", pid, e)

    if json_output:
        console.print_json(
            data={
                "removed": removed,
                "dangling": [{"id": pid, "reason": reason} for pid, reason in dangling],
                "prunedIntents": pruned,
                "residualDeclarations": residual,
                "recoverable": recoverable,
            },
        )
        return EXIT_OK
    if removed or not (dangling or recoverable):
        _ok(f"gc removed {len(removed)} orphan plugin(s): {', '.join(removed) or '—'}")
    for pid, reason in dangling:
        if pid in pruned:
            status = "pruned"
        elif pid in residual:
            status = "intent declaration remains in project/local settings (remove manually)"
        else:
            status = "diagnosed only (confirm in REPL or pass --prune-dangling)"
        console.print(f"[yellow]⚠ dangling intent {pid} [{reason}] — {status}; {_DANGLING_HINTS.get(reason, '')}[/yellow]")
    for pid in recoverable:
        console.print(f"[dim]recoverable: {pid} (not materialized; next boot will re-materialize)[/dim]")
    return EXIT_OK


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
    settings_flag_path: Path | None = None,
) -> None:
    """启动期解析 mcp.json 声明面 + 批准门控 + 收敛挂载态（§9.2）/ Boot-time MCP approval + mount reconciliation。

    - user/embed/flag/policy origin server（trusted）→ 门控判 ENABLED → 直挂（免批准框）；
    - DISABLED（企业拒绝/不在白名单/显式 disabled）→ **确保停摘**；
    - PENDING（工作区共享未决）→ TTY 弹 y/a/n 写 local scope；非 TTY → skip+WARN，``approve_all`` 全批（仅本次不落盘）。

    ``--approve-all-mcp`` 在**非交互**仅本次挂载、**不落盘**；TTY ``[a]`` 才写 enableAllProjectMcpServers（用户显式决定）。

    **循环契约 = `ENABLED ⇒ 确保已挂 / DISABLED ⇒ 确保停摘 / PENDING ⇒ 问`**（#164）。「确保」而非「挂载」是
    因为 embed 层（``Computer(mcp_servers=...)``）在 ``boot_up`` 已被**无门**挂起：
    - ENABLED 时若 bundle_id 已活跃则跳过——重复 ``amount_server`` 会 restart 客户端，``auto_reconnect=False``
      时更直接抛 ``RuntimeError``（见 ``manager._add_or_update_server_config``）。文件来源的 server 此刻恒未活跃，
      故行为不变。
    - DISABLED 时**必须真的摘掉**，否则 policy 拒绝名单对 embed 只是装饰品、协议「用户/管理员保留最终关停权」
      落空。对从未挂过的文件来源 server，``aunmount_server_by_id`` 是幂等 no-op，行为不变。

    **两个 flag 文件分工**（协议 §2.5-3 的 flag scope **文件对**）：``settings_flag_path`` = ``--settings <file>``，
    是 **settings.json** flag 层（喂 :func:`resolved_settings` 的 ``flag_path``——``{enabledPlugins/MCP 门控字段…}``）；
    flag 层 **mcp.json** = ``--mcp-config <file>``，经 :class:`Computer` 注入、由 ``comp.resolve_mcp_declarations()``
    解析（含其 ``inputs`` 段）。二者 schema 不同、**不可互喂**。旧参数名 ``flag_config`` 已更名——它从来是
    settings.json，旧名主动误导（#154）。
    """
    env = os.environ
    # 声明面 = durable scopes + flag（`--mcp-config`）+ embed（构造入参），均携 origin（§2.5-5）。两个 flag 文件
    # 分工见 docstring：settings_flag_path 是 settings.json，**不**喂 resolve_mcp_config。
    # #116：project/local 层由 resolve_mcp_config 内部锚定进程 cwd。
    resolved = comp.resolve_mcp_declarations(env=env)

    # 被 drop 的畸形 server/input 必须呈现（§5.6 / mcp_config 容错不静默，#69 Group B 风险 3）。
    for e in resolved.errors:
        console.print(f"[yellow]⚠ mcp.json: {e}[/yellow]")

    if not resolved.servers:
        return

    # #157：scope 越权被过滤的字段必须有解释——否则用户只看到莫名的批准框（协议 §2.1「响亮失败」）。
    # 典型：仓库的 project settings.json 携 enableAllProjectMcpServers → 被过滤 → 此处告知该挪去 local/user。
    resolved_st = resolved_settings_with_errors(env, flag_path=settings_flag_path)
    for line in format_settings_errors(resolved_st.errors):
        console.print(f"[yellow]{line}[/yellow]")
    settings = resolved_st.settings
    # #148/F8：审批门 MUST NOT 依赖账本 bundled 名集；plugin 声明**结构性**不入 resolve（无 plugin 入参 +
    # SettingsScope 无 PLUGIN 成员，见 schema.SCOPE_ORDER）⇒ 迭代物理上遇不到 plugin origin，无需过滤器
    # ——写一个就是永假守卫（死代码），且是档④「进门后豁免」形状复活的诱因（详见 mcp_server_status）。
    statuses = gate_mcp_servers(resolved, settings)

    # mcp.json 定义的 input 入池（无前缀），供 server config 的裸 ${input:} 解析（#69 Group B 风险 3）。
    # #155：坍缩条目跳过 + 红字呈现，合法 input / server 照常——一个 id 笔误不该让整面 server 消失。
    _inject_inputs_collision_safe(comp, list(resolved.inputs), source="mcp.json")

    approved_all_session = approve_all  # 非交互 --approve-all-mcp，或 TTY 选过一次 [a]

    def _active_bundle_ids() -> set[str]:
        """运行期活跃集的 bundle_id（embed 层已被 ``boot_up`` 无门挂起 ⇒ 挂载前须查）/ Active bundle_ids。"""
        if getattr(comp, "mcp_manager", None) is None:
            return set()
        return {resolve_bundle_id(cfg) for cfg in comp.mcp_manager.server_configs()}

    async def _ensure_mounted(name: str) -> None:
        try:
            srv = resolved.servers[name]
            # **只在「已挂的那份就是胜出者」时跳过**（隔离审查 🔴1）。``boot_up`` **只**预挂 embed 层
            # （``_mcp_servers``），故该条件精确等价于「胜出者的 origin 是 embed」：
            #   - 胜出者 = embed ∧ 已活跃 ⇒ boot_up 挂的正是它 ⇒ 无事可做（重挂只会 restart 客户端，
            #     ``auto_reconnect=False`` 时更抛 RuntimeError）；
            #   - 胜出者 = flag/policy/local/user ⇒ **必须挂**，否则 resolved 里那份更高层的胜出配置永不
            #     生效 ⇒ 运行期层序相对 resolve 层被反转（``local < embed < flag < policy`` 名存实亡）。
            # 文件来源此刻恒未活跃 ⇒ 走 mount 分支，行为不变。
            #
            # ⚠️ **勿改成比较 config**（两种都试过、都错）：``mcp_manager.server_configs()`` 存**渲染后**配置、
            # 而声明面恒为 **raw**（D1）⇒ 任何带 ``${input:}`` 的 config 恒不相等 ⇒ 每 boot 重挂（restart /
            # RuntimeError）；改用 raw-对-raw 又受 embed 层 ``model_dump`` 往返规整影响，同样脆。
            # origin 判据直接表达意图，不受二者影响。守卫见
            # ``test_higher_layer_beats_embed_at_mount_time`` + ``test_embed_with_placeholder_is_not_remounted...``。
            if srv.origin is SettingsScope.EMBED and resolve_bundle_id(srv.config) in _active_bundle_ids():
                console.print(f"[dim]· MCP server {name!r} already mounted from the embed layer (winner), left as is[/dim]")
                return
            # #137 ③：boot 读**已声明** mcp.json 挂载 = 投影（盘上已是真相），走 transient amount_server，不回写
            # （否则每 boot 重复回写用户声明层 / scope 漂移，见 #138）。
            await comp.amount_server(_mount_dict(srv), session=session)
            console.print(f"[green]✓ mounted MCP server {name!r}[/green]")
        except Exception as exc:  # 单个 server 挂载失败不阻断其余 / one failure must not block the rest
            console.print(f"[red]✗ failed to mount MCP server {name!r}: {exc}[/red]")

    async def _ensure_unmounted(name: str) -> None:
        """DISABLED ⇒ 真的摘掉：embed 已被 boot_up 无门挂起，只打印不摘会让 policy 拒绝名单沦为装饰品。"""
        try:
            bid = resolve_bundle_id(resolved.servers[name].config)
            if bid not in _active_bundle_ids():
                return  # 从未挂过（文件来源恒走此分支）→ 幂等 no-op，行为不变
            await comp.aunmount_server_by_id(bid)
            console.print(f"[dim]· MCP server {name!r} disabled (policy/denied) → unmounted[/dim]")
        except Exception as exc:
            console.print(f"[red]✗ failed to unmount disabled MCP server {name!r}: {exc}[/red]")

    for name, status in statuses.items():
        if status == McpApprovalStatus.ENABLED:
            await _ensure_mounted(name)
        elif status == McpApprovalStatus.DISABLED:
            console.print(f"[dim]· MCP server {name!r} disabled (policy/denied), skipped[/dim]")
            await _ensure_unmounted(name)
        else:  # PENDING
            if approved_all_session:
                await _ensure_mounted(name)
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
                await _ensure_mounted(name)
            elif ans in {"y", "yes"}:
                approve_mcp_server(name)
                await _ensure_mounted(name)
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
        if _split_plugin_id(plugin_id) is None:
            console.print("[yellow]invalid plugin id (expected '<plugin>@<marketplace>')[/yellow]")
            return
        scope = flag_value(args, "--scope") or "user"
        # v0.3.0（#123）：install 不激活——不注入 register/inject 回调、不 mark_skills_dirty（skills 无变化）；
        # 仅传 existing_bundle_ids 供依赖预检（只提示不拒绝）。激活走 `plugin enable`。
        await plugin_install(
            registry, home, env, plugin_id,
            scope=scope, project_path=project_path if scope in ("project", "local") else None,
            version=flag_value(args, "--version"),
            existing_bundle_ids=cbs.existing_bundle_ids,
            json_output=json_output,
        )
    elif sub == "uninstall":
        if not pos:
            console.print("[yellow]usage: plugin uninstall <plugin>@<marketplace> [--keep-servers][/yellow]")
            return
        code = await plugin_uninstall(
            registry, home, env, pos[0],
            non_plugin_bundle_ids=cbs.non_plugin_bundle_ids,
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
            existing_bundle_ids=cbs.existing_bundle_ids,
            register_server=_plugin_register_cb(comp, plugin, marketplace),
            remove_server=cbs.remove_server,  # enable 失败回滚摘除本次新增 server（v0.3.0 原子性）
            inject_inputs=_plugin_inject_inputs_cb(comp, plugin, marketplace),
            json_output=json_output,
        )
        if code == EXIT_OK:
            comp.mark_skills_dirty()
    elif sub == "disable":
        if not pos:
            console.print("[yellow]usage: plugin disable <plugin>@<marketplace>[/yellow]")
            return
        code = await plugin_disable(
            registry, home, env, pos[0],
            non_plugin_bundle_ids=cbs.non_plugin_bundle_ids, remove_server=cbs.remove_server, json_output=json_output,
        )
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

        async def _confirm(items: list[str]) -> bool:
            ans = (await session.prompt_async(f"GC {len(items)} item(s): {', '.join(items)}? [y/N]: ")).strip().lower()
            return ans in {"y", "yes"}

        # REPL 有 confirm 门 → 悬挂意图 prune 一并纳入确认（#125 任务 2；Typer 非交互须显式 --prune-dangling）
        code = await plugin_gc(
            registry, home, env,
            non_plugin_bundle_ids=cbs.non_plugin_bundle_ids,
            mcp_teardown=_batch_teardown(cbs.remove_server), confirm=_confirm, prune_dangling=True, json_output=json_output,
        )
        if code == EXIT_OK:
            comp.mark_skills_dirty()
    else:
        console.print(f"[yellow]unknown plugin subcommand: {sub}[/yellow]")


def _batch_teardown(remove_server: RemoveServer) -> Callable[[list[str]], Awaitable[None]]:
    """把单个 ``remove_server`` 包成批量 teardown 回调（gc_plugins 的 ``mcp_teardown`` 入参为 **bundle_id** 列表，
    且已在 gc 内过完 §4.9.1-2 回收判据）/ Adapt single-remove into gc's batch teardown。"""

    async def _teardown(bundle_ids: list[str]) -> None:
        for bundle_id in bundle_ids:
            await remove_server(bundle_id)

    return _teardown
