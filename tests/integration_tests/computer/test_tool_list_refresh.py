# -*- coding: utf-8 -*-
# filename: test_tool_list_refresh.py
# @Author  : JQQ
# @Software: PyCharm
"""
集成测试（#127 Bug B）：MCP 运行期工具集变化后，Computer 端 client:get_tools 视图须刷新。

  根因：manager 的 ``_tool_mapping`` 在 boot 期构建、``tools/list_changed`` 时不刷新，而 ``available_tools()``
  迭代该映射键——运行期**新增**的工具不在映射中永远被漏掉。修复：``Computer.aget_available_tools()`` 在服务
  ``client:get_tools`` 前调用 ``manager.arefresh_tools()``（socketio 安全上下文），使 新增 / 同名换 schema /
  移除 三类变化均反映。

Integration tests for #127 Bug B: the Computer's ``client:get_tools`` view must reflect runtime tool-set changes.
  Root cause: ``_tool_mapping`` is built at boot and not refreshed on ``tools/list_changed``; ``available_tools()``
  iterates its keys, so runtime-added tools are missed. Fix: refresh in ``aget_available_tools`` before serving.

驱动器 / Driver: ``mutable_tools_stdio_server.py`` 的 ``set_phase(phase:int)`` 工具在运行期改变工具集并发
  ``tools/list_changed``（纯工具热更新，不伴随 config 变更）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig, ToolMeta

_MUTABLE_SRV = Path(__file__).parent / "mcp_servers" / "mutable_tools_stdio_server.py"


def _mutable_cfg(name: str = "mutable-srv") -> StdioServerConfig:
    """构造可变工具集 stdio MCP Server 配置（auto_apply 跳过二次确认）。"""
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(_MUTABLE_SRV)]),
        default_tool_meta=ToolMeta(auto_apply=True),
    )


def _names(tools: list) -> set[str]:
    return {t["name"] for t in tools}


@pytest.mark.anyio
async def test_get_tools_reflects_runtime_add_schema_change_remove() -> None:
    """Computer.aget_available_tools 反映运行期 新增 / 同名换 schema / 移除（#127 Bug B 修复）。"""
    computer = Computer(name="comp-127", mcp_servers={_mutable_cfg()})
    await computer.boot_up()
    try:
        # 基线：仅控制工具 / baseline: only the control tool
        base = await computer.aget_available_tools()
        assert _names(base) == {"mutable-srv__set_phase"}

        # —— 新增：phase 1 → dynamic_tool(schemaA: alpha) ——
        await computer.mcp_manager.aexecute_tool("mutable-srv__set_phase", {"phase": 1})
        added = await computer.aget_available_tools()
        assert "mutable-srv__dynamic_tool" in _names(added), "运行期新增工具须出现在 get_tools / runtime-added tool must surface"
        dyn_a = next(t for t in added if t["name"] == "mutable-srv__dynamic_tool")
        assert "alpha" in dyn_a["params_schema"].get("properties", {})

        # —— 同名换 schema：phase 2 → dynamic_tool(schemaB: beta，无 alpha) ——
        await computer.mcp_manager.aexecute_tool("mutable-srv__set_phase", {"phase": 2})
        changed = await computer.aget_available_tools()
        dyn_b = next(t for t in changed if t["name"] == "mutable-srv__dynamic_tool")
        props = dyn_b["params_schema"].get("properties", {})
        assert "beta" in props and "alpha" not in props, "同名工具须换到新 schema、无旧残留 / new schema, no stale"

        # —— 移除：phase 0 → dynamic_tool 消失 ——
        await computer.mcp_manager.aexecute_tool("mutable-srv__set_phase", {"phase": 0})
        removed = await computer.aget_available_tools()
        assert "mutable-srv__dynamic_tool" not in _names(removed), "移除的工具须从 get_tools 消失 / removed tool must disappear"
        assert _names(removed) == {"mutable-srv__set_phase"}
    finally:
        await computer.shutdown()


@pytest.mark.anyio
async def test_manager_arefresh_tools_surfaces_added_tool() -> None:
    """锁 Bug B 根因：不刷新则新增工具不在 _tool_mapping 中被漏掉；arefresh_tools() 后可见。"""
    manager = MCPServerManager(auto_connect=False)
    await manager.ainitialize([_mutable_cfg()])
    await manager.astart_all()
    try:
        base = {t.name async for t in manager.available_tools()}
        assert base == {"mutable-srv__set_phase"}

        # 运行期新增工具但**不**刷新映射 → available_tools 仍漏掉（复现 Bug B）
        await manager.aexecute_tool("mutable-srv__set_phase", {"phase": 1})
        stale = {t.name async for t in manager.available_tools()}
        assert "mutable-srv__dynamic_tool" not in stale, "未刷新时 _tool_mapping 陈旧、新增工具被漏（Bug B 现象）"

        # 显式刷新（Computer 服务 get_tools 时所做）→ 新增工具浮现
        await manager.arefresh_tools()
        fresh = {t.name async for t in manager.available_tools()}
        assert "mutable-srv__dynamic_tool" in fresh, "arefresh_tools() 后新增工具须可见（#127 修复）"
    finally:
        await manager.aclose()
