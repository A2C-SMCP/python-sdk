# -*- coding: utf-8 -*-
# filename: help.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
分组 help 渲染 + 命令分类法（REPL help / completer 共用单一事实源）/ Grouped help + command taxonomy。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.1 / §10.2（S15，#68）。

``render_help`` 默认列 namespace（折叠），``help <ns>`` 列该 namespace 命令。命令分类法（:data:`NAMESPACES` /
:data:`NAMESPACE_COMMANDS` / :data:`SUBCOMMANDS` / :data:`FLAGS`）是 REPL help 与 :mod:`completer` 的**单一事实源**。
plugin / settings namespace 作多 namespace 可扩展骨架先行登记（命令体由 #69 落地）。
"""

from __future__ import annotations

from a2c_smcp.computer.cli.utils import console

# namespace → 一行描述（折叠 help 用，顺序即展示序）/ namespace → one-line description (ordered)。
NAMESPACES: dict[str, str] = {
    "server": "MCP server lifecycle (add / rm / start / stop)",
    "inputs": "Input definitions and values",
    "marketplace": "SKILL marketplaces (git sources)",
    "plugin": "Plugins — skill+mcp bundles (see #69)",
    "skill": "Skills cross-source query (list / info)",
    "socket": "Socket.IO connection control",
    "notify": "Send notifications to Agent",
    "settings": "Edit settings.json files (see #69)",
    "utility": "status / tools / mcp / desktop / render / tc / history",
}

# namespace → [(命令样式, 描述)] 完整 help 行 / namespace → full help rows。
NAMESPACE_COMMANDS: dict[str, list[tuple[str, str]]] = {
    "server": [
        ("server add <json|@file>", "添加或更新 MCP 配置 / add or update config"),
        ("server rm <name>", "移除 MCP 配置 / remove config"),
        ("start <name>|all", "启动客户端 / start client(s)"),
        ("stop <name>|all", "停止客户端 / stop client(s)"),
    ],
    "inputs": [
        ("inputs load <@file>", "从文件加载 inputs 定义 / load inputs"),
        ("inputs list", "查看当前 inputs 定义 / show inputs"),
        ("inputs value list|get|set|rm|clear", "inputs 缓存值增删改查 / CRUD cached input values"),
    ],
    "marketplace": [
        ("marketplace add <git-url> [--name N] [--trust] [--auto-update] [--no-clone]", "添加 marketplace（首次 y/N trust）/ add"),
        ("marketplace list [--json]", "列出已知 marketplace / list known marketplaces"),
        ("marketplace info <name> [--json]", "marketplace 详情 / marketplace detail"),
        ("marketplace remove <name> [--keep-plugins]", "移除（默认级联卸载 plugin）/ remove (cascade)"),
        ("marketplace refresh [<name>|all]", "git pull / 重 clone + 对账 / refresh"),
        ("marketplace set <name> auto-update=<bool>", "设置 per-source auto-update / set flag"),
    ],
    "plugin": [
        ("plugin install|uninstall|enable|disable|list|info", "（#69 落地）/ implemented in #69"),
    ],
    "skill": [
        ("skill list [--source mp|mcp|user] [--json]", "跨源列出可见 SKILL / list skills"),
        ("skill info <name> [--json]", "SKILL 详情 / skill detail"),
    ],
    "socket": [
        ("socket connect [<url>]", "连接 Socket.IO / connect"),
        ("socket join <office_id> <computer_name>", "加入房间 / join office"),
        ("socket leave", "离开房间 / leave office"),
    ],
    "notify": [("notify update", "触发配置更新通知 / emit config updated notification")],
    "settings": [("settings show|edit|get|set", "（#69 落地）/ implemented in #69")],
    "utility": [
        ("status", "查看服务器状态 / show server status"),
        ("tools", "列出可用工具 / list tools"),
        ("mcp", "显示当前 MCP 配置 / show current MCP config"),
        ("desktop [size] [window_uri]", "获取当前桌面窗口组合 / get desktop"),
        ("render <json|@file>", "测试渲染（占位符解析）/ test rendering"),
        ("tc <json|@file>", "调试工具调用 / debug tool call"),
        ("history [n]", "最近工具调用历史 / recent tool call history"),
        ("help [<namespace>]", "查看命令 / show commands"),
        ("quit | exit", "退出 / quit"),
    ],
}

# completer 用：namespace → 子命令词 / subcommand words per namespace (for completion)。
SUBCOMMANDS: dict[str, list[str]] = {
    "server": ["add", "rm", "remove"],
    "inputs": ["load", "add", "update", "rm", "remove", "get", "list", "value"],
    "marketplace": ["add", "list", "info", "remove", "refresh", "set"],
    "plugin": ["install", "uninstall", "enable", "disable", "list", "info"],
    "skill": ["list", "info"],
    "socket": ["connect", "join", "leave"],
    "notify": ["update"],
    "settings": ["show", "edit", "get", "set"],
}

# 行首可补全词：namespace + 顶层命令 / root completions: namespaces + top-level commands。
ROOT_WORDS: list[str] = [
    *SUBCOMMANDS.keys(),
    "start", "stop", "status", "tools", "mcp", "desktop", "render", "tc", "history", "help", "quit", "exit",
]

# (namespace, subcommand) → flag 集 / flags per command (for completion)。
FLAGS: dict[tuple[str, str], list[str]] = {
    ("marketplace", "add"): ["--name", "--trust", "--auto-update", "--no-clone", "--json"],
    ("marketplace", "list"): ["--json"],
    ("marketplace", "info"): ["--json"],
    ("marketplace", "remove"): ["--keep-plugins", "--json"],
    ("marketplace", "refresh"): ["--json"],
    ("marketplace", "set"): ["--json"],
    ("skill", "list"): ["--source", "--json"],
    ("skill", "info"): ["--json"],
}


def render_help(namespace: str | None = None) -> None:
    """折叠 namespace 列表（``namespace=None``）或某 namespace 命令详情（§10.2）/ Render grouped help。"""
    from rich.table import Table

    if namespace is None:
        table = Table(title="Namespaces (type 'help <name>' for details)", header_style="bold magenta")
        table.add_column("Namespace", style="bold cyan", no_wrap=True)
        table.add_column("Description")
        for name, desc in NAMESPACES.items():
            table.add_row(name, desc)
        console.print(table)
        console.print("[dim]提示: help <namespace> 查看该组命令；? 重看本表。[/dim]")
        return

    ns = namespace.lower()
    rows = NAMESPACE_COMMANDS.get(ns)
    if rows is None:
        console.print(f"[yellow]unknown namespace: {namespace!r} (try 'help')[/yellow]")
        return
    table = Table(title=f"{ns} commands", header_style="bold magenta")
    table.add_column("Command", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    for cmd, desc in rows:
        table.add_row(cmd, desc)
    console.print(table)
