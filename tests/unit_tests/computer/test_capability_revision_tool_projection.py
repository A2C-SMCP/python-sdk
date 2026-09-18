# -*- coding: utf-8 -*-
# filename: test_capability_revision_tool_projection.py
# @Author  : JQQ
# @Software: PyCharm
"""
单元测试（#197）：``tools/list_changed`` 后工具投影**真实变化**须推进 ``Computer.capability_revision``。

Unit tests for #197: a real tool-projection change after ``tools/list_changed`` must advance
``Computer.capability_revision``; a duplicate notification with an unchanged projection must not.

背景 / Background: ``_on_manager_change`` 运行在 MCP ``ClientSession`` 的**接收循环内联上下文**
（``mcp/shared/session.py`` → ``await self._message_handler(req)``），其中 await 任何对**同一会话**的
请求会造成会话级重入死锁（#127 实证 ``TimeoutError``；#197 复验：window 刷新内联 RPC 会使连通话一并挂起）。
故本修复**不在 handler 内联刷新**，而是调度后台任务消费 ``manager.arefresh_tools()`` 的
「投影是否变化」结论——与 Rust PR #199 的 ``projection_changed`` 语义对齐。

``_on_manager_change`` is an MCP receive-loop inline callback; awaiting a same-session request there
deadlocks (#127). The fix therefore schedules a background refresh instead of inlining it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import ToolListChangedNotification

from a2c_smcp.computer.computer import Computer


class _RecordingClient:
    """记录 ``emit_update_tool_list`` 调用次数的伪 Socket.IO 客户端 / Fake Socket.IO client counting emits."""

    def __init__(self) -> None:
        self.update_called = 0

    async def emit_update_tool_list(self) -> None:
        self.update_called += 1


def _tool_list_changed() -> Any:
    """构造 MCP ``notifications/tools/list_changed`` 通知替身 / Build a ToolListChangedNotification stand-in."""
    return SimpleNamespace(root=ToolListChangedNotification())


async def _booted_computer() -> Computer:
    """boot 后返回 Computer（``mcp_manager`` 已就绪但无活跃 client —— auto_connect=False）。"""
    computer = Computer(name="test", auto_connect=False, auto_reconnect=False)
    await computer.boot_up()
    assert computer.mcp_manager is not None
    return computer


def _stub_refresh(
    computer: Computer,
    *,
    changed: bool,
    calls: list[int] | None = None,
    error: Exception | None = None,
) -> None:
    """把 manager 的投影刷新替换为受控桩：返回 ``changed``，或抛 ``error``。

    Replace the manager's projection refresh with a controlled stub (returns ``changed`` / raises ``error``).
    """
    assert computer.mcp_manager is not None

    async def fake() -> bool:
        if calls is not None:
            calls.append(1)
        if error is not None:
            raise error
        return changed

    computer.mcp_manager.arefresh_tools = fake  # type: ignore[method-assign]


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    """轮询等待 predicate 为真（避免对后台任务调度做固定 sleep 假设）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等待条件超时 / timed out waiting for condition")
        await asyncio.sleep(0.005)


async def _settle(computer: Computer) -> None:
    """等待工具投影后台刷新任务结算（未调度则直接返回，便于红灯期断言真实缺口）。"""
    task = getattr(computer, "_tool_refresh_task", None)
    if task is not None:
        await task


class TestToolProjectionRevision:
    """#197：工具投影变化 → capability_revision 推进（与 clear_oauth 共用同一能力轴）。"""

    @pytest.mark.asyncio
    async def test_projection_change_bumps_revision(self) -> None:
        """投影真实变化 → revision 0→1，且既有 emit 链保持 / Real change must bump and keep emitting."""
        computer = await _booted_computer()
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]
        _stub_refresh(computer, changed=True)
        assert computer.capability_revision == 0

        await computer._on_manager_change(_tool_list_changed())
        await _settle(computer)

        assert computer.capability_revision == 1
        assert client.update_called == 1, "notify:update_tool_list 链路须保持（Agent 重拉依赖）"

    @pytest.mark.asyncio
    async def test_unchanged_projection_does_not_bump(self) -> None:
        """阴性对照：重复通知而投影未变 → 不得产生虚假 revision（对齐 Rust 阴性对照 3/3）。"""
        computer = await _booted_computer()
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]
        _stub_refresh(computer, changed=False)

        await computer._on_manager_change(_tool_list_changed())
        await _settle(computer)

        assert computer.capability_revision == 0, "投影未变时不得推进 revision"
        assert client.update_called == 1, "emit 行为保持现状（无条件广播，幂等由 Agent 重拉兜住）"

    @pytest.mark.asyncio
    async def test_bumps_even_without_socketio_client(self) -> None:
        """本地状态先行：无 Socket.IO 连接仍推进 revision（对齐 clear_oauth 的 bump 先于早退姿态）。"""
        computer = await _booted_computer()
        _stub_refresh(computer, changed=True)
        assert computer.socketio_client is None

        await computer._on_manager_change(_tool_list_changed())
        await _settle(computer)

        assert computer.capability_revision == 1, "未加入 office 不代表本地能力轴不推进"

    @pytest.mark.asyncio
    async def test_refresh_error_does_not_bump_and_is_swallowed(self) -> None:
        """刷新失败 → 不推进（不确定即不推进）、不向接收循环冒泡。"""
        computer = await _booted_computer()
        _stub_refresh(computer, changed=False, error=RuntimeError("list_tools boom"))

        await computer._on_manager_change(_tool_list_changed())
        await _settle(computer)

        assert computer.capability_revision == 0

    @pytest.mark.asyncio
    async def test_notification_during_inflight_refresh_is_not_dropped(self) -> None:
        """在途刷新期间到达的通知不得被丢弃 → 结算后补跑一轮。"""
        computer = await _booted_computer()
        calls: list[int] = []
        gate = asyncio.Event()
        assert computer.mcp_manager is not None

        async def fake() -> bool:
            calls.append(1)
            if len(calls) == 1:
                await gate.wait()  # 第一轮阻塞，制造「在途」窗口 / hold first pass to open the in-flight window
            return True

        computer.mcp_manager.arefresh_tools = fake  # type: ignore[method-assign]

        await computer._on_manager_change(_tool_list_changed())
        await _wait_until(lambda: len(calls) == 1)  # 第一轮已进入
        await computer._on_manager_change(_tool_list_changed())  # 在途 → 须标脏
        gate.set()
        await _settle(computer)

        assert len(calls) == 2, "在途通知必须补跑一轮，不得被丢弃"

    @pytest.mark.asyncio
    async def test_shutdown_cancels_inflight_refresh_and_stops_rescheduling(self) -> None:
        """停机：在途刷新被 **cancel 并结算**；``aclose()`` 的 await 窗口内到达的通知不得复活任务。

        两条断言分别钉死两处实现属性（仅断言终态会漏）：① 在途句柄须 `done() and cancelled()` ——
        只 cancel 不 await 或整段摘除 cancel 都会让任务滞留；② 停机窗口内**投递通知**时不得新建任务 ——
        只有「先摘 ``mcp_manager`` 引用、再 ``aclose``」才能挡住（顺序反转则窗口重新敞开）。
        """
        computer = await _booted_computer()
        gate = asyncio.Event()
        assert computer.mcp_manager is not None
        manager = computer.mcp_manager

        async def fake() -> bool:
            await gate.wait()  # 阻塞在途刷新，制造停机窗口
            return True

        manager.arefresh_tools = fake  # type: ignore[method-assign]

        # aclose 窗口内投递一条通知：须因 mcp_manager 已摘除而不新建任务（记录「置 None」这一事实）
        window_saw_no_task: list[bool] = []

        async def aclose_spy() -> None:
            await computer._on_manager_change(_tool_list_changed())
            window_saw_no_task.append(computer._tool_refresh_task is None)

        manager.aclose = aclose_spy  # type: ignore[method-assign]

        await computer._on_manager_change(_tool_list_changed())
        inflight = computer._tool_refresh_task
        await _wait_until(lambda: inflight is not None and not inflight.done())

        await computer.shutdown()

        assert inflight is not None and inflight.done() and inflight.cancelled(), "在途刷新须被 cancel 并结算"
        assert window_saw_no_task == [True], "停机窗口内到达的通知不得新建任务（摘引用须先于 aclose）"
        assert computer.capability_revision == 0, "被取消的刷新不得推进能力轴"
        assert computer.mcp_manager is None, "停机须摘除 manager 引用"

        # 停机后到达的通知 → 不得复活任务
        await computer._on_manager_change(_tool_list_changed())
        assert computer._tool_refresh_task is None, "停机后不得再调度工具投影刷新"
