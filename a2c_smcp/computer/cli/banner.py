# -*- coding: utf-8 -*-
# filename: banner.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
REPL 启动 banner（zero-state 引导）/ REPL startup banner with zero-state guidance（S15，#68）。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §10.1。

触发条件：``installed plugins == 0 AND servers == 0``（marketplace 数量不计入）→ 引导框；否则一行摘要。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.panel import Panel

from a2c_smcp.computer.cli.utils import console
from a2c_smcp.computer.settings.store import load_installed_plugins
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)


def _installed_plugin_count(home: Path | None, env: Mapping[str, str] | None) -> int:
    if home is None:
        return 0
    try:
        return len(load_installed_plugins(home, env).get("plugins") or {})
    except Exception as e:  # banner 不应因物化读失败而打断启动；记 DEBUG 便于排障（含编程错）/ never break boot
        logger.debug("banner: installed-plugin count failed (%s), treating as 0", e)
        return 0


def render_banner(comp: Any, home: Path | None, env: Mapping[str, str] | None) -> None:
    """zero-state（0 plugins AND 0 servers）→ 引导框；否则一行摘要 / Zero-state box or one-line summary。"""
    plugins = _installed_plugin_count(home, env)
    servers = len(getattr(comp, "mcp_servers", ()) or ())

    if plugins == 0 and servers == 0:
        body = (
            "[bold]A2C Computer[/bold] — ready (0 plugins, 0 servers)\n\n"
            "[bold]Next steps:[/bold]\n"
            "  • [cyan]marketplace add <git-url>[/cyan]   add a SKILL marketplace\n"
            "  • [cyan]server add @<file>[/cyan]          add an MCP server\n"
            "  • [cyan]help[/cyan]                        full command list"
        )
        console.print(Panel(body, expand=False, border_style="cyan"))
        return
    console.print(f"[bold]A2C Computer[/bold]  ·  {plugins} plugins  ·  {servers} servers")
