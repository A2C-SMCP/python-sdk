# -*- coding: utf-8 -*-
# filename: __init__.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
CLI 命令核心（REPL 与 Typer 非交互共用）/ CLI command core shared by the REPL and non-interactive Typer surfaces。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.2 / §4.4 / §12（S15，#68）。

本包把 marketplace / skill 命令的业务逻辑（包裹 staging / reconciler / store / installer / registry 既有后端）
抽成纯函数式 handler，签名取**显式资源**（``registry`` / ``home`` / ``env`` + flags）而非整个 Computer，便于
隔离单测；REPL 经各模块的 ``repl_dispatch`` 适配器把 ``Computer`` 的活跃 registry / home / session 绑定进去，
Typer 子命令则构造轻量上下文（不 boot Computer、不连 socket）。

本模块只放 **跨命令共享的接缝**：:func:`build_mcp_callbacks`——从 ``Computer`` 装配 installer / 卸载级联所需的
MCP 注入回调（``existing_server_names`` / ``register_server`` / ``remove_server``）。plugin / settings 命令（#69）
将复用本接缝。
This package holds the command business logic as pure-ish handlers (explicit resources, not the whole Computer)
so they unit-test in isolation. Only the cross-command seam lives here: :func:`build_mcp_callbacks`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型，避免运行时循环导入 / type-only to dodge runtime import cycle
    from a2c_smcp.computer.computer import Computer
    from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
    from a2c_smcp.computer.settings.installer import ExistingServerNames, RegisterServer, RemoveServer
    from a2c_smcp.computer.settings.schema import SettingsValidationError
    from a2c_smcp.computer.settings.scope import ResolvedSettings


@dataclass(frozen=True, slots=True)
class McpCallbacks:
    """installer / 卸载级联所需的三个 MCP 注入回调 / The three MCP injection callbacks installer/cascade needs。"""

    existing_server_names: ExistingServerNames
    register_server: RegisterServer
    remove_server: RemoveServer


def build_mcp_callbacks(comp: Computer) -> McpCallbacks:
    """
    从 ``Computer`` 装配 installer / uninstall 级联所需的 MCP 注入回调 / Wire MCP callbacks from a live Computer。

    - ``existing_server_names``：当前已注册 MCP server 名集合（冲突预检用）；
    - ``register_server``：**运行期挂载**一个 ``MCPServerConfig``（enable / install remount 用）；
    - ``remove_server``：按 name **运行期停摘** server（disable / uninstall teardown 用）。

    设计 §12.2：marketplace remove 的级联卸载（→ :func:`installer.uninstall_plugin`）与 #69 的 plugin
    enable/disable/install/uninstall 共用此接缝，避免各处重复装配。

    **#137 ③ transient 分流**：本接缝**唯一**消费方是 plugin enable/disable/install/uninstall 与 marketplace
    级联卸载——皆**治理投影**（bundled 真相在 ledger，非用户此刻声明），故走 transient
    :meth:`Computer.amount_server` / :meth:`Computer.aunmount_server`，**不回写** mcp.json（否则 disable 后
    复活 + scope 漂移，见 #138）。用户显式 ``server add``/``rm`` 是另一条（REPL）durable 路径，与此无关。
    """

    def _existing() -> set[str]:
        return {cfg.name for cfg in comp.mcp_servers}

    async def _register(cfg: MCPServerConfig) -> None:
        await comp.amount_server(cfg)

    async def _remove(name: str) -> None:
        await comp.aunmount_server(name)

    return McpCallbacks(existing_server_names=_existing, register_server=_register, remove_server=_remove)


# ── 跨命令解析 / 视图接缝（marketplace / skill / plugin / settings 共用）/ shared parse & view seams ──
def flag_value(args: list[str], flag: str) -> str | None:
    """取 ``--flag value`` 形态的值（**不支持** ``--flag=value``，REPL 简化）/ Extract a ``--flag value`` pair。

    四个命令模块（marketplace / skill / plugin / settings）的 REPL dispatcher 共用，避免 4 处分叉。
    """
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            return args[idx + 1]
    return None


def resolved_settings_with_errors(
    env: Mapping[str, str] | None,
    *,
    flag_path: Path | None = None,
) -> ResolvedSettings:
    """五层合并 settings **连同校验错误**（含 policy first-source-wins；#116 锚 cwd）/ Merged settings **with errors**。

    :func:`resolved_settings` 是本函数的薄包装（只取 ``.settings``）。需要向用户**呈现**越权/畸形字段的
    调用方（boot 批准流程、``settings show``）用本函数拿完整 :class:`ResolvedSettings`。
    Callers that must surface filtered/malformed fields use this; :func:`resolved_settings` wraps it.

    函数内 lazy import 沿用本仓 dodge-cycle 范式（settings.scope / settings.policy 不反向依赖 cli，无环，
    仅避免 ``import cli.commands`` 拉重）。
    """
    from a2c_smcp.computer.settings.policy import resolve_policy_settings
    from a2c_smcp.computer.settings.scope import resolve_settings

    return resolve_settings(
        env=env,
        flag_settings_path=flag_path,
        policy_settings=resolve_policy_settings(env=env),
    )


def resolved_settings(
    env: Mapping[str, str] | None,
    *,
    flag_path: Path | None = None,
) -> dict[str, Any]:
    """五层合并 settings（含 policy first-source-wins；#116 project/local 锚定进程 cwd）/ Merged settings incl. policy。

    plugin（``enabledPlugins`` / gc 声明视图）与 settings（merged show / get）共用。policy 层承载企业
    allowed/deniedMcpServers（POLICY_ONLY 字段，批准门控须读到），故统一注入。

    **丢弃校验错误**——只在「不向用户呈现诊断」的声明视图用；要呈现的走
    :func:`resolved_settings_with_errors` + :func:`format_settings_errors`（#157）。
    Drops validation errors; use the ``_with_errors`` variant where diagnostics must surface.
    """
    return resolved_settings_with_errors(env, flag_path=flag_path).settings


def format_settings_errors(errors: Sequence[SettingsValidationError]) -> list[str]:
    """把 settings 校验错误格式化为人读警示行（**纯函数**，供 boot 批准流程与 ``settings show`` 共用）/ format。

    #157：scope 越权过滤（policy-only / 审批门 enable 方向判据）**静默丢弃字段**——若连错误也不呈现，用户
    只会看到「我的 settings 莫名不生效」（协议 §2.1 要求**响亮失败**，``SettingsValidationError`` 的契约亦
    自称「经 ``settings show`` / 诊断命令呈现」）。抽为纯函数以便**单测文案与 scope/field 拼装**，杜绝未来
    重构把「呈现」半程静默回退成吞错误——呈现行为在 ``run_mcp_approval`` 这类 ``Session``-泛型异步副作用
    函数里无法直接断言。对拍 rust ``cli/commands/mod.rs::format_settings_errors``。
    Pure function so the wording/assembly is unit-testable; the call sites are thin by design.
    """
    return [f"⚠ settings.json[{e.scope.value}]: {e.field} — {e.reason}" for e in errors]
