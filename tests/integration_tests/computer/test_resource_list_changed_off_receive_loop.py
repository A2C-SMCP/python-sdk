# -*- coding: utf-8 -*-
# filename: test_resource_list_changed_off_receive_loop.py
# @Author  : JQQ
# @Software: PyCharm
"""
集成测试（#210）：``resources/list_changed`` 之后该 MCP server 不得失能（真实 stdio 链路）。

Integration tests for #210: after ``resources/list_changed`` the MCP server must stay usable over a real
stdio session — no mocks on the computer/manager boundary.

判据 / Criterion: 触发通知的那次 ``aexecute_tool`` 必须**在有界时间内返回**。缺陷形态下它永久挂起 ——
内联刷新发出的 ``resources/list`` 请求，其响应只能由正阻塞在回调里的接收循环读取（自阻塞）。

驱动器 / Driver: ``mutable_resources_stdio_server.py`` 的 ``set_phase`` 改 ``window://`` 集合后发
``resources/list_changed``、``set_skill_body`` 改 ``SKILL.md`` 内容（URI 集合**不变**）后发
``resources/updated`` —— 后者覆盖「内容级更新不能被集合比对挡掉」这条独立路径。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig, ToolMeta

_SERVER = "res-srv"
_FIXTURE = Path(__file__).parent / "mcp_servers" / "mutable_resources_stdio_server.py"
_HOST = "itest.mutable.res"
_SKILL_NAME = f"mcp:{_SERVER}:demo"

# 工具调用上界：缺陷形态下接收循环永久自阻塞，此上界把「挂死」变成一次可读的失败。
# 修复后单次调用是毫秒级，10s 仅为 CI 抖动留裕量。
_TOOL_TIMEOUT = 10.0


class _RecordingClient:
    """记录 Socket.IO emit 次数的伪客户端 / Fake Socket.IO client counting emits."""

    def __init__(self) -> None:
        self.desktop_refreshes = 0
        self.skills_emits = 0

    async def emit_refresh_desktop(self) -> None:
        self.desktop_refreshes += 1

    async def emit_update_skills(self) -> None:
        self.skills_emits += 1


def _config() -> StdioServerConfig:
    """构造 stdio MCP Server 配置（auto_apply 跳过二次确认）。"""
    return StdioServerConfig(
        name=_SERVER,
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(_FIXTURE)]),
        default_tool_meta=ToolMeta(auto_apply=True),
    )


async def _drive_tool(computer: Computer, tool: str, args: dict[str, Any], timeout: float = _TOOL_TIMEOUT) -> None:
    """调用一个会触发资源通知的工具，**要求它在有界时间内返回**（#210 红灯判据）。"""
    assert computer.mcp_manager is not None
    await asyncio.wait_for(computer.mcp_manager.aexecute_tool(f"{_SERVER}__{tool}", args), timeout=timeout)


async def _drain_resource_refresh(computer: Computer, timeout: float = 10.0) -> None:
    """等待资源刷新后台任务结算（轮询任务句柄——通知异步到达后才创建任务）。

    同 #197 集成侧的 ``_drain_tool_refresh``：工具调用返回前 server 已先写通知、再写响应，接收循环按序
    处理 → 返回时任务必已调度；但刷新也可能在返回前就结算，故「在途则 await、已结算则返回」两种都要覆盖。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        task = computer._resource_refresh_task
        if task is not None and not task.done():
            await task  # 结算在途刷新（结算后可能又起新任务 → 再查一轮）
            continue
        if task is not None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待资源刷新任务结算超时")


@pytest.mark.anyio
async def test_resources_list_changed_does_not_disable_server(tmp_path: Path) -> None:
    """#210：``resources/list_changed`` 后该 server 仍可用，且窗口缓存真的推进、桌面刷新真的上报。"""
    computer = Computer(
        name="comp-210",
        mcp_servers={_config()},
        skill_home=tmp_path / "home",
    )
    await computer.boot_up()
    client = _RecordingClient()
    computer.socketio_client = client  # type: ignore[assignment]
    try:
        assert computer._windows_cache == set()  # boot 不播种窗口缓存（首次通知必然「变化」）

        # 修复前：此调用在 10s 内不返回（接收循环被内联的 list_windows RPC 自阻塞）→ 红灯
        await _drive_tool(computer, "set_phase", {"phase": 1})
        # 反证「server 未失能」：能收下一条通知 + 工具响应 —— 缺陷形态下第二条同样挂起
        await _drive_tool(computer, "set_phase", {"phase": 2})
        await _drain_resource_refresh(computer)

        assert computer._windows_cache == {f"window://{_HOST}/main", f"window://{_HOST}/side"}, (
            "window:// 集合须经真实 list_resources 重枚举后提交（空断言修前修后都绿，故须断言具体集合）"
        )
        assert client.desktop_refreshes >= 1, "集合变化须上报桌面刷新"
    finally:
        await computer.shutdown()


@pytest.mark.anyio
async def test_resources_updated_skill_content_rematerializes(tmp_path: Path) -> None:
    """#210 ③ 路径：``resources/updated(skill://…)``（URI 集合**不变**）仍须重物化并上报。

    这条路径若复用集合脏位，会被 ``new_skills == _skills_cache`` 挡掉 → Registry 永远停在旧内容。
    """
    computer = Computer(
        name="comp-210-skill",
        mcp_servers={_config()},
        skill_home=tmp_path / "home",
    )
    await computer.boot_up()
    client = _RecordingClient()
    computer.socketio_client = client  # type: ignore[assignment]
    try:
        ref = computer.get_skill_ref(_SKILL_NAME)
        assert ref is not None, "boot 期应已物化可注册形状的 skill:// 根"
        staged = Path(ref["path"]) / "SKILL.md"
        assert staged.is_file()
        before = staged.read_text(encoding="utf-8")

        # 集合不变、内容变 —— 修复前此调用在接收循环内联重物化时同样会挂起 → 红灯
        await _drive_tool(computer, "set_skill_body", {"body": "UPDATED-BY-210"})
        await _drain_resource_refresh(computer)
        await computer._skill_debouncer.aflush()  # 结算去抖窗口 → emit

        assert "UPDATED-BY-210" in staged.read_text(encoding="utf-8"), "内容级更新必须无条件重物化"
        assert "UPDATED-BY-210" not in before
        assert client.skills_emits >= 1, "重物化后须经去抖器上报 SKILL 更新"
    finally:
        await computer.shutdown()


@pytest.mark.anyio
async def test_resources_list_changed_without_office_still_updates_local_state(tmp_path: Path) -> None:
    """本地状态先行（#210 语义变更）：未加入 office 也重物化 SKILL，仅跳过上报。"""
    computer = Computer(
        name="comp-210-offline",
        mcp_servers={_config()},
        skill_home=tmp_path / "home",
    )
    await computer.boot_up()
    assert computer.socketio_client is None
    try:
        await _drive_tool(computer, "set_phase", {"phase": 2})
        await _drain_resource_refresh(computer)

        assert computer._windows_cache == {f"window://{_HOST}/main", f"window://{_HOST}/side"}, (
            "未入房间不代表本地窗口缓存应停滞（对齐 #197 tools 路径的「本地状态先行」）"
        )
    finally:
        await computer.shutdown()
