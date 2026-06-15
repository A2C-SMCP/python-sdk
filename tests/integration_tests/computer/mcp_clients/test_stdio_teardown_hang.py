# -*- coding: utf-8 -*-
# filename: test_stdio_teardown_hang.py
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
"""集成回归测试：stdio MCP 子进程拆除挂死（#105 调查 / E2E flaky-hang 根因）。

中文：根因——旧实现以 ``task.cancel()`` 触发 keep-alive 收尾，``_aexit_stack.aclose()`` 在
CancelledError 抢占下跳过 mcp SDK 的子进程强杀，导致 stdio 子进程残留、其 ThreadedChildWatcher
线程的 ``os.waitpid`` 永久阻塞（慢 CI 偶发整套 e2e 挂死）。

本测试三条：
  1. 拆除时 ``_aexit_stack.aclose()`` 在**非取消**上下文运行（``task.cancelling()==0``）——根因修复机制。
  2. 正常 disconnect 后 stdio 子进程被回收（PID 捕获生效 + 子进程已死）。
  3. 「兑底强杀」：收尾任务卡死时，``_close_task`` 在限时内按 PID 强杀子进程、补发 closed 事件并返回。

English: integration regression for the stdio-child teardown hang — proves aclose runs cancellation-free
(root fix), the child is reaped on disconnect, and the force-kill fallback bounds a wedged teardown.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.mcp_clients import base_client as base_client_mod
from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient

pytestmark = pytest.mark.asyncio

_MINIMAL_SRV = Path(__file__).resolve().parents[2] / "computer" / "mcp_servers" / "minimal_stdio_server.py"


def _params() -> StdioServerParameters:
    assert _MINIMAL_SRV.exists(), f"server script not found: {_MINIMAL_SRV}"
    return StdioServerParameters(command=sys.executable, args=[str(_MINIMAL_SRV)])


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 权限边界 / permission boundary
        return True
    return True


class _CancellationProbe(AbstractAsyncContextManager):
    """压入 keep-alive 的 AsyncExitStack；在 __aexit__（即 aclose 期间）记录是否处于取消态。

    Pushed onto the keep-alive exit stack to record whether aclose() runs under cancellation.
    """

    def __init__(self) -> None:
        self.cancelling_during_aexit: int | None = None

    async def __aenter__(self) -> _CancellationProbe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        task = asyncio.current_task()
        # Python 3.11+：task.cancelling() 返回未 uncancel 的取消请求数；旧 cancel 路径下 >=1。
        self.cancelling_during_aexit = task.cancelling() if task is not None else None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only stdio child semantics")
async def test_teardown_runs_aclose_without_cancellation() -> None:
    """根因修复机制：拆除时 aclose() 在非取消上下文运行（cancelling()==0）。"""
    client = StdioMCPClient(_params())
    await client.aconnect()
    await client._create_session_success_event.wait()

    # 压入探针：LIFO，先于 mcp 上下文被 aclose，且在同一收尾流程内执行
    probe = _CancellationProbe()
    await client._aexit_stack.enter_async_context(probe)

    await client.adisconnect()
    await client._async_session_closed_event.wait()

    assert probe.cancelling_during_aexit == 0, (
        f"aclose() 必须在非取消上下文运行（根因修复）；实测 cancelling()={probe.cancelling_during_aexit}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only stdio child semantics")
async def test_stdio_child_reaped_on_disconnect() -> None:
    """正常 disconnect 后：捕获到子进程 PID 且子进程已被回收。"""
    client = StdioMCPClient(_params())
    await client.aconnect()
    await client._create_session_success_event.wait()

    captured = set(client._child_pids)
    assert captured, "应捕获到本 client 启动的 stdio 子进程 PID / child PID capture must work"

    await client.adisconnect()
    await client._async_session_closed_event.wait()

    # 子进程应已退出 / child must be gone (allow brief reap delay)
    for _ in range(30):
        if all(not _pid_alive(pid) for pid in captured):
            break
        await asyncio.sleep(0.1)
    assert all(not _pid_alive(pid) for pid in captured), f"stdio 子进程未回收 / child still alive: {captured}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only force-kill fallback")
async def test_close_task_force_kills_wedged_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """兑底强杀：收尾任务卡死时，_close_task 限时内按 PID 强杀子进程、补发 closed 事件并返回。"""
    monkeypatch.setattr(base_client_mod, "_TEARDOWN_TIMEOUT", 0.3)

    client = StdioMCPClient(_params())

    # 模拟一个永不响应 _close_event 的卡死收尾任务 / a teardown task that never finishes
    async def _stuck() -> None:
        await asyncio.Event().wait()

    stuck_task = asyncio.create_task(_stuck())
    await asyncio.sleep(0)  # 让任务进入挂起 / let it suspend
    client._session_keep_alive_task = stuck_task

    # 真实「卡死子进程」：独立 session 以便 killpg / a real wedged child in its own session
    wedged = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    client._child_pids = {wedged.pid}

    try:
        # 限时内必须返回（兑底），不得挂死 / must return bounded, never hang
        await asyncio.wait_for(client._close_task(), timeout=5)
        # 子进程被强杀 / child force-killed
        wedged.wait(timeout=5)
        assert wedged.returncode is not None
        # 补发 closed 事件，避免 on_enter_disconnected 的 closed 等待挂死
        assert client._async_session_closed_event.is_set()
    finally:
        if wedged.poll() is None:  # pragma: no cover - 安全网 / safety net
            wedged.kill()
            wedged.wait()
        stuck_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stuck_task
