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

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型，避免运行时循环导入 / type-only to dodge runtime import cycle
    from a2c_smcp.computer.computer import Computer
    from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
    from a2c_smcp.computer.settings.installer import ExistingServerNames, RegisterServer, RemoveServer


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
    - ``register_server``：注册 / 更新一个 ``MCPServerConfig``（enable / install remount 用）；
    - ``remove_server``：按 name 停并摘除 server（disable / uninstall teardown 用）。

    设计 §12.2：marketplace remove 的级联卸载（→ :func:`installer.uninstall_plugin`）与 #69 的 plugin
    enable/disable/install/uninstall 共用此接缝，避免各处重复装配。
    """

    def _existing() -> set[str]:
        return {cfg.name for cfg in comp.mcp_servers}

    async def _register(cfg: MCPServerConfig) -> None:
        await comp.aadd_or_aupdate_server(cfg)

    async def _remove(name: str) -> None:
        await comp.aremove_server(name)

    return McpCallbacks(existing_server_names=_existing, register_server=_register, remove_server=_remove)
