# -*- coding: utf-8 -*-
# filename: test_capability_revision_tool_projection.py
# @Author  : JQQ
# @Software: PyCharm
"""
集成测试（#197）：运行期 MCP 工具投影变化须推进 ``Computer.capability_revision``（真实 stdio 链路）。

Integration tests for #197: runtime tool-projection changes must advance ``Computer.capability_revision``
over a real stdio MCP server — no mocks on the computer/manager boundary.

驱动器 / Driver: ``mutable_tools_stdio_server.py`` 的 ``set_phase(phase:int)`` 运行期改变工具集并**无条件**
发 ``tools/list_changed`` —— 故「重复同一 phase」构成天然阴性对照：通知照发、投影未变、revision 不得推进。
The driver fires ``tools/list_changed`` unconditionally, so re-setting the same phase is a built-in
negative control: the notification arrives, the projection is unchanged, the revision must not advance.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig, ToolMeta

_MUTABLE_SRV = Path(__file__).parent / "mcp_servers" / "mutable_tools_stdio_server.py"


def _mutable_cfg(name: str = "mutable-srv") -> StdioServerConfig:
    """构造可变工具集 stdio MCP Server 配置（auto_apply 跳过二次确认）。"""
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(_MUTABLE_SRV)]),
        default_tool_meta=ToolMeta(auto_apply=True),
    )


async def _drain_tool_refresh(computer: Computer, timeout: float = 10.0) -> None:
    """等待工具投影后台刷新结算（#197）：轮询任务句柄——通知异步到达后才创建任务。

    ``aexecute_tool`` 返回前，server 已先写通知、再写响应，接收循环按序处理 → 返回时任务必已调度；
    但刷新可能在返回前就已结算，故「已在途则 await、已结算则直接返回」两种情形都要覆盖。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        task = computer._tool_refresh_task
        if task is not None and not task.done():
            await task  # 结算在途刷新（结算后可能又起新任务 → 再查一轮）
            continue
        if task is not None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待工具投影刷新任务结算超时")


async def _drive_phase(computer: Computer, phase: int) -> None:
    """调用 ``set_phase`` 触发工具变化 + ``tools/list_changed``，并等待后台投影刷新结算。"""
    assert computer.mcp_manager is not None
    await computer.mcp_manager.aexecute_tool("mutable-srv__set_phase", {"phase": phase})
    await _drain_tool_refresh(computer)


@pytest.mark.anyio
async def test_tools_list_changed_advances_capability_revision() -> None:
    """新增 / 同名换 schema / 移除 三类运行期变化各推进一次；重复同一投影不推进（#197）。"""
    computer = Computer(name="comp-197", mcp_servers={_mutable_cfg()})
    await computer.boot_up()
    try:
        assert computer.capability_revision == 0

        await _drive_phase(computer, 1)  # phase 1：新增 dynamic_tool(schemaA: alpha)
        assert computer.capability_revision == 1, "运行期新增工具须推进能力版本"

        await _drive_phase(computer, 2)  # phase 2：同名 dynamic_tool 换 schema（schemaB: beta）
        assert computer.capability_revision == 2, "同名换 schema 亦属能力变化（拒绝 name-only 捷径）"

        await _drive_phase(computer, 2)  # 重复同一 phase：通知照发、投影未变
        assert computer.capability_revision == 2, "重复通知而投影未变不得产生虚假 revision（阴性对照）"

        await _drive_phase(computer, 0)  # phase 0：移除 dynamic_tool
        assert computer.capability_revision == 3, "运行期移除工具须推进能力版本"
    finally:
        await computer.shutdown()
