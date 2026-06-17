# -*- coding: utf-8 -*-
# filename: test_fastmcp_skills_integration.py
"""
AS-40 集成测试：FastMCP-style Skills Provider 经真 stdio MCP server 的可注册性。
Integration test for AS-40: registrability of a FastMCP-style Skills Provider over a real stdio MCP server.

依据 / Basis: AS-40 comment 13849 —— 「FastMCP 资源已能被 smcp-computer 注册，前提是 provider 用
可注册形状（``_meta.source=resources`` 根 + 子资源）；裸 ``skill://<name>/SKILL.md`` 由 provider 侧适配解决。」
本测试**不改 src**，只新增覆盖：真 stdio server → boot manager → ``_restage_mcp_skills`` → ``Computer.get_skills()``。

为什么落在集成测试而非 CLI/tmux UAT / Why integration test, not CLI UAT:
    ``_restage_mcp_skills`` 仅在 ``boot_up`` 触发，而 MCP server 的批准/连接发生在 boot 之后——
    非交互 CLI 无法观测 live MCP skill 注册（见 scenarios/mcp-fastmcp-skill.md 的 CLI 局限说明）。
    故可注册性的自动化验证落在此 gated 集成测试。

覆盖 / Coverage:
    - MF-01: 可注册形状被 ``get_skills()`` 收集为 ``mcp:fastmcp-skill-test:fastmcp-demo``
    - MF-02: 裸 ``skill://bare-demo/SKILL.md`` **不**注册（当前契约），且不误报 invalid/skipped
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig

_FIXTURE = Path(__file__).resolve().parents[1] / "mcp_servers" / "fastmcp_skill_stdio_server.py"
_SERVER_NAME = "fastmcp-skill-test"
_EXPECTED_SKILL = "mcp:fastmcp-skill-test:fastmcp-demo"


@asynccontextmanager
async def _booted_computer(tmp_path: Path) -> AsyncIterator[Computer]:
    """启动真 stdio fixture 的 manager，注入 Computer，重物化后产出可查询 get_skills 的 Computer。"""
    assert _FIXTURE.exists(), f"fixture missing: {_FIXTURE}"
    params = StdioServerParameters(command=sys.executable, args=[str(_FIXTURE)])
    cfg = StdioServerConfig(name=_SERVER_NAME, server_parameters=params)

    manager = MCPServerManager(auto_connect=False)
    await manager.ainitialize([cfg])
    await manager.astart_all()

    comp = Computer(name="comp-fastmcp-itest", skill_home=tmp_path / "home", auto_connect=False, auto_reconnect=False)
    comp.mcp_manager = manager  # type: ignore[assignment]
    comp._skill_home = tmp_path / "home"
    comp._skill_home.mkdir(parents=True, exist_ok=True)
    try:
        yield comp
    finally:
        await manager.astop_all()


@pytest.mark.anyio
async def test_mf01_registrable_shape_collected_by_get_skills(tmp_path: Path) -> None:
    """MF-01: 可注册形状（_meta.source=resources 根 + SKILL.md/reference.md 子资源）→ 入 get_skills。"""
    async with _booted_computer(tmp_path) as comp:
        registered = await comp._restage_mcp_skills()
        assert _EXPECTED_SKILL in registered

        names = [s["name"] for s in comp.get_skills()]
        assert _EXPECTED_SKILL in names

        # 物化产物落盘：包根含 SKILL.md 与 reference.md（子资源被逐个 read 还原）
        ref = comp.get_skill_ref(_EXPECTED_SKILL)
        assert ref is not None
        pkg = Path(ref["path"])
        assert (pkg / "SKILL.md").is_file()
        assert (pkg / "reference.md").is_file()
        assert ref["source"] == f"mcp:{_SERVER_NAME}"
        assert ref["version"] == "1.0.0"


@pytest.mark.anyio
async def test_mf02_bare_layout_not_registered(tmp_path: Path) -> None:
    """MF-02: 裸 skill://bare-demo/SKILL.md（无 _meta.source 根）当前不注册——provider 侧适配解决。

    同时守护「不误报」：裸布局只是静默跳过，不应污染可注册形状的注册结果。
    """
    async with _booted_computer(tmp_path) as comp:
        registered = await comp._restage_mcp_skills()
        # 裸布局任何命名变体都不应出现
        assert not any("bare-demo" in n for n in registered)
        names = [s["name"] for s in comp.get_skills()]
        assert not any("bare-demo" in n for n in names)
        # 可注册形状仍正常入册（裸布局未连累）
        assert _EXPECTED_SKILL in names
