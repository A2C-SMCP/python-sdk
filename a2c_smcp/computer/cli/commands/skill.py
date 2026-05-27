# -*- coding: utf-8 -*-
# filename: skill.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``skill`` 命令 handler（跨源扁平视图：list / info 两个只读动词）/ Skill command handlers (read-only list/info)。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.4（S15，#68）。

skill **没有** install/uninstall/enable/disable —— 这些经 plugin 层（marketplace 源）/ server add-rm（mcp 源）/
文件操作（user 源）完成。CLI 只暴露 list / info（§4.4）。list 跨三源扁平展示 name / source / enabled / orphan，
``--source mp|mcp|user`` 过滤。REPL 直读 ``Computer`` 活跃 registry；Typer 非交互在新进程重建 registry
（从 ``known_marketplaces.json`` 离线重扫 marketplace + user DropIn；mcp 源需 live 服务器 → 非交互不列、需 REPL）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from a2c_smcp.computer.cli.commands import flag_value
from a2c_smcp.computer.cli.utils import console
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, SOURCE_MCP, SOURCE_USER
from a2c_smcp.computer.skills.registry import SkillRegistry

# CLI --source 短名 → A2CSkillRef.source 前缀段 / CLI short name → source prefix segment。
_SOURCE_ALIAS: dict[str, str] = {"mp": SOURCE_MARKETPLACE, "mcp": SOURCE_MCP, "user": SOURCE_USER}


def _source_segment(src: str) -> str:
    """取 ``A2CSkillRef.source`` 的来源段（``marketplace:<mp>`` → ``marketplace``）/ Source kind segment。"""
    return src.split(":", 1)[0]


def skill_list(registry: SkillRegistry, *, source: str | None = None, json_output: bool = False) -> int:
    """跨源列出当前可见 SKILL（name / source / enabled / orphan），``source`` ∈ {mp,mcp,user} 过滤 / List skills。"""
    want = _SOURCE_ALIAS.get(source) if source else None
    if source and want is None:
        console.print(f"[yellow]unknown --source {source!r} (expected mp|mcp|user)[/yellow]")
        return 1

    rows: list[dict[str, Any]] = []
    for ref, orphaned in registry.all_refs():
        src = str(ref.get("source", ""))
        if want is not None and _source_segment(src) != want:
            continue
        rows.append({"name": ref.get("name"), "source": src, "enabled": not orphaned, "orphan": orphaned})
    rows.sort(key=lambda r: str(r["name"]))

    if json_output:
        console.print_json(data=rows)
        return 0
    if not rows:
        console.print("[dim]No skills visible.[/dim]")
        return 0
    from rich.table import Table

    table = Table(title="Skills", header_style="bold magenta")
    for col in ("Name", "Source", "Enabled", "Orphan"):
        table.add_column(col)
    for r in rows:
        table.add_row(str(r["name"]), r["source"], "✓" if r["enabled"] else "—", "⚠" if r["orphan"] else "—")
    console.print(table)
    return 0


def skill_info(registry: SkillRegistry, name: str, *, json_output: bool = False) -> int:
    """SKILL 详情：source / 归属 / version / frontmatter / SKILL.md 路径（含孤儿）/ Skill detail (incl. orphan)。"""
    found: dict[str, Any] | None = None
    orphaned = False
    for ref, is_orphan in registry.all_refs():
        if ref.get("name") == name:
            found, orphaned = dict(ref), is_orphan
            break
    if found is None:
        if json_output:
            console.print_json(data={"error": f"unknown skill: {name}"})
        else:
            console.print(f"[red]✗ unknown skill: {name!r}[/red]")
        return 1
    found["orphan"] = orphaned
    found["enabled"] = not orphaned
    if json_output:
        console.print_json(data=found)
        return 0
    from rich.table import Table

    table = Table(title=f"Skill · {name}", show_header=False)
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")
    for key in ("source", "version", "description", "path", "license", "compatibility", "enabled", "orphan"):
        if key in found:
            table.add_row(key, str(found[key]))
    console.print(table)
    return 0


async def rebuild_registry(home: Path, env: Mapping[str, str] | None) -> SkillRegistry:
    """
    非交互重建 registry：从 ``known_marketplaces.json`` 离线重扫 marketplace clone + user DropIn / Offline rebuild。

    新进程的 registry 是空的；据已物化的 marketplace clone（``refresh=False`` 复用本地树、不触网）+ user 源
    就地发现重新注册。**mcp 源不在此**（需运行中 MCP 服务器，仅 REPL 可见）。
    """
    from a2c_smcp.computer.settings.store import load_known_marketplaces
    from a2c_smcp.computer.skills.staging import stage_marketplace_skills, stage_user_skills

    reg = SkillRegistry()
    mps = load_known_marketplaces(home, env).get("marketplaces") or {}
    for nm, rec in mps.items():
        await stage_marketplace_skills(
            nm, rec.get("source") or {}, reg, home, auto_update=bool(rec.get("autoUpdate", False)), refresh=False, env=env
        )
    stage_user_skills(reg, home)
    return reg


# ── REPL dispatcher / REPL adapter ────────────────────────────────────────────
def repl_dispatch(comp: Any, parts: list[str]) -> None:
    """``skill list|info ...`` REPL 行解析 → 调 handler（直读 Computer 活跃 registry）/ Parse a REPL skill line。"""
    sub = parts[1].lower()
    args = parts[2:]
    json_output = "--json" in args
    pos = [a for a in args if not a.startswith("--")]
    registry: SkillRegistry = comp.skill_registry

    if sub == "list":
        skill_list(registry, source=flag_value(args, "--source"), json_output=json_output)
    elif sub == "info":
        if not pos:
            console.print("[yellow]usage: skill info <name>[/yellow]")
            return
        skill_info(registry, pos[0], json_output=json_output)
    else:
        console.print(f"[yellow]unknown skill subcommand: {sub}[/yellow]")
