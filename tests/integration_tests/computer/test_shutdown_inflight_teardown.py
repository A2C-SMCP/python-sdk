# -*- coding: utf-8 -*-
# filename: test_shutdown_inflight_teardown.py
# @Author  : JQQ
# @Software: PyCharm
"""
集成测试（#211）：``Computer.shutdown()`` 停机收敛 + 外部取消信号还原。

缺陷 / Defect
    ``_keep_alive_task`` 的 ``finally`` 把关闭信号 ``_async_session_closed_event.set()`` 与
    ``await self._aexit_stack.aclose()`` 平铺在同层：服务端消息落在拆除窗口时 ``aclose()`` 抛
    ``ExceptionGroup(BrokenResourceError)`` → 跳过信号 → ``on_enter_disconnected`` 永久等待 →
    ``astop_all`` 持 ``manager._lock`` 卡死 → ``shutdown()`` 不返回。

触发条件 / Trigger（实测修正，非「有请求在途」本身）
    **服务端消息落在拆除窗口内**。已完成请求、以及响应远在拆除之后才到的未决请求都不触发；
    而 ``shutdown()`` 取消在途刷新任务后，该请求虽被放弃、**服务端仍会回包**，回包正好撞上拆除 ——
    故 ``test_shutdown_converges_with_inflight_refresh`` 刻意「不 drain 刷新直接停机」。

取消信号 / Cancellation
    ``transitions.AsyncMachine.process_context`` 按设计吞掉状态机回调内抛出的 ``CancelledError``
    （服务于库自身的 ``cancel_running_transitions``），``_close_task`` 亦有一处同类吞点 ——
    两者都会让 ``asyncio.wait_for(computer.shutdown(), n)`` **返回正常值而非抛 TimeoutError**。
    下面两个用例分别钉死这两条吞点上的信号还原。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import MethodType
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients import base_client as base_client_mod
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig, ToolMeta

_SERVER = "res-srv"
_FIXTURE = Path(__file__).parent / "mcp_servers" / "mutable_resources_stdio_server.py"

# 停机预算：修复后实测 ~0.003s（在途刷新被取消后拆除即结束），2s 仅为 CI 抖动留裕量。
# 缺陷形态下 elapsed 必然顶到预算上界（撤 F1 时表现为 wait_for 超时抛错，全撤三处时表现为「正常返回」——
# 两者都判红，故判据只认 elapsed，不绑定某一种失败形态）。
_SHUTDOWN_BUDGET = 5.0
_CONVERGED_LIMIT = 2.0

# 工具调用上界：integration job 不带 --timeout，无界 await 一旦挂起会挂死整套 CI（而非 fast-fail）。
# 缺陷形态下该调用可能永久挂起，故必须显式加界（同 test_resource_list_changed_off_receive_loop.py 约定）。
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


def _pid_alive(pid: int) -> bool:
    """子进程是否仍存活（僵尸进程在 waitpid 前仍算存活）/ liveness probe for a spawned child."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 权限边界 / permission boundary
        return True
    return True


def _config() -> StdioServerConfig:
    """构造 stdio MCP Server 配置（auto_apply 跳过二次确认）。"""
    return StdioServerConfig(
        name=_SERVER,
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(_FIXTURE)]),
        default_tool_meta=ToolMeta(auto_apply=True),
    )


async def _boot(tmp_path: Path, name: str) -> tuple[Computer, Any]:
    """启动一台 Computer 并返回 (computer, 首个活跃 client)。"""
    computer = Computer(name=name, mcp_servers={_config()}, skill_home=tmp_path / "home")
    await computer.boot_up()
    assert computer.mcp_manager is not None
    client = next(iter(computer.mcp_manager._active_clients.values()))
    return computer, client


async def _force_teardown(client: Any) -> None:
    """兜底清理：放行 keep-alive 任务并强杀子进程（用例中途断言失败时不留残留）。"""
    client._close_event.set()
    task = client._session_keep_alive_task
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except BaseException:  # noqa: BLE001 - 兜底清理：拆除异常（含 anyio 的 ExceptionGroup）不得掩盖用例结论
            task.cancel()
    try:
        await client._aforce_kill()
    except Exception:  # noqa: BLE001 - best-effort 清理
        pass


async def _wait_refresh_inflight(computer: Computer, timeout: float = 5.0) -> Any:
    """轮询直到资源刷新任务**在途未决**（把「触发器已上膛」变成显式前置条件，而非运气）。

    本用例的缺陷触发条件是「服务端响应落在拆除窗口内」：`shutdown()` 取消在途刷新后该请求虽被放弃、
    服务端仍会回包。若刷新在停机前已自行结算，红灯将不再成立 —— 故此处断言在途，避免绿灯变得不可归因
    （实测：0.002s 的「收敛」若缺此证据，无法区分「修好了」与「压根没触发」）。
    """
    assert computer.mcp_manager is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        task = computer._resource_refresh_task
        if task is not None and not task.done():
            return task
        await asyncio.sleep(0.005)
    raise AssertionError("资源刷新任务未能在停机前进入在途状态 —— 触发器未上膛，红灯判据不成立")


@pytest.mark.anyio
async def test_shutdown_converges_with_inflight_refresh(tmp_path: Path) -> None:
    """#211 验收：存在在途 MCP 请求时 ``shutdown()`` 仍须在预算内收敛，且关闭信号置位。

    刻意**不** drain ``_resource_refresh_task``：在途刷新被 ``shutdown()`` 取消后服务端仍会回包，
    回包落在拆除窗口即触发缺陷（issue 原样复现）。修复前实测 elapsed 顶到预算上界。
    """
    computer, client = await _boot(tmp_path, "comp-211-inflight")
    computer.socketio_client = _RecordingClient()  # type: ignore[assignment]
    try:
        assert computer.mcp_manager is not None
        await asyncio.wait_for(
            computer.mcp_manager.aexecute_tool(f"{_SERVER}__set_phase", {"phase": 1}), timeout=_TOOL_TIMEOUT
        )

        # 前置（防空过）：触发器已上膛 + 会话确实活着 + 信号尚未置位
        inflight = await _wait_refresh_inflight(computer)
        assert not client._async_session_closed_event.is_set()
        assert client._async_session is not None
        captured_pids = set(client._child_pids)
        assert captured_pids, "应捕获到 stdio 子进程 PID"

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(computer.shutdown(), timeout=_SHUTDOWN_BUDGET)
        elapsed = loop.time() - started

        # 收敛上界远低于 _TEARDOWN_TIMEOUT（10s）：若收敛其实来自 F2 兜底超时，本断言必红 →
        # 绿灯可归因于 F1（信号无条件置位），而非被兜底掩盖。
        assert elapsed < _CONVERGED_LIMIT, f"停机必须在预算内收敛，实测 {elapsed:.2f}s"
        assert client._async_session_closed_event.is_set(), "停机后会话关闭信号必须已置位"
        assert client._async_session is None
        assert inflight.done(), "在途刷新技术上应随停机结算"
        # 拆除真的跑完（而非被兜底跳过）：子进程应已回收
        for _ in range(30):
            if all(not _pid_alive(pid) for pid in captured_pids):
                break
            await asyncio.sleep(0.1)
        assert all(not _pid_alive(pid) for pid in captured_pids), f"stdio 子进程未回收: {captured_pids}"
    finally:
        await _force_teardown(client)


@pytest.mark.anyio
async def test_shutdown_restores_cancel_swallowed_settling_inflight(tmp_path: Path) -> None:
    """#211：取消落在 ``shutdown()`` **自身**的 ``contextlib.suppress`` 结算窗口内时，同样须还原。

    这是 ``Computer.shutdown()`` 上装饰器**不可省**的证据：该吞点位于 ``manager.aclose()`` 之前，不在后者
    的覆盖范围内 —— 且取消发生在 ``aclose()`` 取快照**之前**，故内层装饰器只会把该计数当作入口既有值而
    放过（快照式判据的固有边界：谁先起跑谁负责还原）。

    前提说明（归因诚实）：``shutdown`` 体内还有第二个同任务吞点 —— 技能去抖器的
    ``await task`` + ``except CancelledError: pass``（``skills/debouncer.py``）。本用例的 ``skill_home``
    指向 tmp 且无 watcher 事件，去抖器空闲、不会挂起，故取消必然落在上述 ``contextlib.suppress`` 上；
    若将来 ``shutdown`` 前面多出别的 await，本用例的归因会失效（届时需补「去抖器空闲」前置断言）。
    """
    computer, client = await _boot(tmp_path, "comp-211-shutdown-suppress")

    started = asyncio.Event()

    async def _slow_to_settle() -> None:
        """被取消后仍需一段时间才结算的在途任务（模拟真实拆除 / 网络等待）。"""
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(1.0)
            raise

    inflight = asyncio.create_task(_slow_to_settle())
    # 必须等它真正跑起来再停机：向**尚未启动**的协程投递取消会直接落在协程开头、绕过内部 try/except，
    # 于是它瞬间结算 —— 结算窗口不存在，用例便失去判别力（本用例首版即栽在此处）。
    await asyncio.wait_for(started.wait(), timeout=_SHUTDOWN_BUDGET)
    computer._resource_refresh_task = inflight
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(computer.shutdown(), timeout=0.2)
    finally:
        inflight.cancel()
        await _force_teardown(client)


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["astop_all", "astop_client"])
async def test_stop_entries_restore_swallowed_cancellation(tmp_path: Path, entry: str) -> None:
    """#211：CLI ``stop`` 命令直接 await 的两个公开入口同样须还原取消（私有 ``_astop_*`` 保持裸奔）。"""
    computer, client = await _boot(tmp_path, f"comp-211-{entry}")
    assert computer.mcp_manager is not None
    manager = computer.mcp_manager
    bundle_id = next(iter(manager._active_clients))

    async def _slow_on_enter(_self: Any, _event: Any) -> None:
        await asyncio.sleep(1.0)

    client.on_enter_disconnected = MethodType(_slow_on_enter, client)  # type: ignore[method-assign]
    try:
        call = manager.astop_all() if entry == "astop_all" else manager.astop_client(bundle_id)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(call, timeout=0.2)
    finally:
        await _force_teardown(client)


@pytest.mark.anyio
@pytest.mark.parametrize("entry", ["astart_all", "astart_client"])
async def test_start_entries_restore_swallowed_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """#211：CLI ``start`` 命令直接 await 的两个公开入口同样须还原取消（吞点同经 client 状态机）。"""
    from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient

    async def _slow_after_connect(_self: Any, _event: Any) -> None:
        await asyncio.sleep(1.0)

    monkeypatch.setattr(StdioMCPClient, "aafter_connect", _slow_after_connect)

    # auto_connect=False：boot 不连，交给 astart_* 连 —— 取消才会落在 connect 的状态机回调上
    computer = Computer(
        name=f"comp-211-{entry}", mcp_servers={_config()}, skill_home=tmp_path / "home", auto_connect=False
    )
    await computer.boot_up()
    assert computer.mcp_manager is not None
    manager = computer.mcp_manager
    registered = next(iter(manager.server_configs()), None)
    assert registered is not None, "boot 后应能取到已登记的 server"
    bundle_id = registered.bundle_id
    try:
        call = manager.astart_all() if entry == "astart_all" else manager.astart_client(bundle_id)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(call, timeout=0.2)
    finally:
        for client in list(manager._active_clients.values()):
            await _force_teardown(client)


@pytest.mark.asyncio
async def test_concurrent_batch_converges_children_before_restoring_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#208：**并发**批量启动路径同样须还原被吞的取消，且**先收敛子任务**再上抛。

    并发路径用 ``asyncio.wait``（**非** ``gather``：后者会把取消级联进子任务并弃置未 await 的子任务，
    子任务可能停在半开的 transport 上）。故本用例同时钉两件事：
    ① 最外层入口仍抛 ``CancelledError``（``wait_for`` 转 ``TimeoutError``）——取消可观测；
    ② 取消后**没有孤儿子任务**（``asyncio.all_tasks()`` 不残留批量启动体）——收敛而非弃置。

    若把驱动换成 ``gather``，「可观测」仍可能通过而「无孤儿」失真；若去掉收敛重抛，则①失真。
    """
    from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient

    async def _slow_after_connect(_self: Any, _event: Any) -> None:
        await asyncio.sleep(1.0)

    monkeypatch.setattr(StdioMCPClient, "aafter_connect", _slow_after_connect)

    def _named(suffix: str) -> StdioServerConfig:
        return StdioServerConfig(
            name=f"{_SERVER}-{suffix}",
            server_parameters=StdioServerParameters(command=sys.executable, args=[str(_FIXTURE)]),
            default_tool_meta=ToolMeta(auto_apply=True),
        )

    computer = Computer(
        name="comp-208-concurrent-cancel",
        mcp_servers={_named("a"), _named("b"), _named("c")},
        skill_home=tmp_path / "home",
        auto_connect=False,
    )
    await computer.boot_up()
    assert computer.mcp_manager is not None
    manager = computer.mcp_manager
    # 配置并发上限 ⇒ 走结构化并发路径（未配置是串行路径，由 #211 既有用例覆盖）
    manager.with_mcp_start_concurrency(3)
    ids = manager.enabled_bundle_ids()
    assert len(ids) == 3, "三台 server 均应已登记"

    before = asyncio.all_tasks()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(manager.astart_clients_batch(ids), timeout=0.2)
    finally:
        for client in list(manager._active_clients.values()):
            await _force_teardown(client)

    # ② 无孤儿：取消后不得残留批量启动子任务（收敛 = 等子任务结算后才上抛）
    leaked = [t for t in asyncio.all_tasks() - before if not t.done() and t is not asyncio.current_task()]
    assert not leaked, f"取消后残留未结算子任务：{leaked}"


@pytest.mark.anyio
async def test_boot_up_restores_swallowed_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#211：``boot_up()`` 内部经 client 状态机（其 ``process_context`` 按设计吞取消）→ 须还原取消信号。"""
    from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient

    async def _slow_after_connect(_self: Any, _event: Any) -> None:
        await asyncio.sleep(1.0)

    # 挂在 transitions 回调上：取消落点被状态机吞掉，boot_up 会继续跑完 —— 正是需要还原的形态
    monkeypatch.setattr(StdioMCPClient, "aafter_connect", _slow_after_connect)

    computer = Computer(name="comp-211-boot", mcp_servers={_config()}, skill_home=tmp_path / "home")
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(computer.boot_up(), timeout=0.2)
    finally:
        manager = computer.mcp_manager
        if manager is not None:
            for client in list(manager._active_clients.values()):
                await _force_teardown(client)


@pytest.mark.anyio
async def test_manager_aclose_restores_swallowed_cancellation(tmp_path: Path) -> None:
    """#211：``MCPServerManager.aclose()`` 同样经过 client 状态机的吞点 → 须还原取消信号。"""
    computer, client = await _boot(tmp_path, "comp-211-mgr-aclose")

    async def _slow_on_enter(_self: Any, _event: Any) -> None:
        await asyncio.sleep(1.0)

    client.on_enter_disconnected = MethodType(_slow_on_enter, client)  # type: ignore[method-assign]
    try:
        assert computer.mcp_manager is not None
        manager = computer.mcp_manager
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(manager.aclose(), timeout=0.2)

        # 承重判据：补抛必须发生在**最外层**（aclose）收尾**之后** —— 否则内层 astop_all 先抛会跳过
        # `_clear_all()`，等于用「取消可观测」换「状态没清干净」。只断言「抛了 TimeoutError」看不出区别。
        assert not manager._servers_config and not manager._active_clients, (
            "最外层入口补抛前必须完成 _clear_all 收尾"
        )
    finally:
        await _force_teardown(client)


@pytest.mark.anyio
async def test_healthy_shutdown_never_force_kills(tmp_path: Path) -> None:
    """阴性对照：正常停机**不得**触发兜底强杀 —— F2 若在健康路径误触发，会把健康的 stdio 子进程 SIGKILL 掉。"""
    computer, client = await _boot(tmp_path, "comp-211-healthy")
    kill_spy = AsyncMock()
    client._aforce_kill = kill_spy  # type: ignore[method-assign]
    try:
        await asyncio.wait_for(computer.shutdown(), timeout=_SHUTDOWN_BUDGET)

        assert kill_spy.await_count == 0, "健康路径不得走到兜底强杀（信号本就该就地置位）"
        assert client._async_session_closed_event.is_set()
    finally:
        await _force_teardown(client)


@pytest.mark.anyio
async def test_shutdown_propagates_direct_task_cancel(tmp_path: Path) -> None:
    """#211：宿主**直接** ``task.cancel()``（非 ``wait_for``）时，``shutdown()`` 须以 ``CancelledError`` 退出。

    与 ``wait_for`` 形态的区别在于退出方式：本形态下调用方期待任务呈「已取消」，而非「正常返回」。
    """
    computer, client = await _boot(tmp_path, "comp-211-direct-cancel")
    entered = asyncio.Event()

    async def _slow_on_enter(_self: Any, _event: Any) -> None:
        entered.set()
        await asyncio.sleep(1.0)

    client.on_enter_disconnected = MethodType(_slow_on_enter, client)  # type: ignore[method-assign]
    try:
        task = asyncio.create_task(computer.shutdown())
        # 前置：确认取消必定落在被吞窗口内（状态机回调中），否则本用例失去判别力
        await asyncio.wait_for(entered.wait(), timeout=_SHUTDOWN_BUDGET)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_SHUTDOWN_BUDGET)
    finally:
        await _force_teardown(client)


@pytest.mark.anyio
async def test_shutdown_restores_cancellation_swallowed_by_state_machine(tmp_path: Path) -> None:
    """#211：取消落在状态机回调里（被 transitions 吞掉）时，``shutdown()`` 必须在收尾处还原取消信号。

    吞点：``transitions.AsyncMachine.process_context`` 的 ``except CancelledError``（库按设计实现，
    服务于 ``cancel_running_transitions``）。此处用一个慢 ``on_enter_disconnected`` 把取消引导到该回调内。
    """
    computer, client = await _boot(tmp_path, "comp-211-transitions")

    async def _slow_on_enter(_self: Any, _event: Any) -> None:
        await asyncio.sleep(1.0)

    client.on_enter_disconnected = MethodType(_slow_on_enter, client)  # type: ignore[method-assign]
    try:
        # 缺陷形态：取消被吞 → 协程「正常返回」→ wait_for 不抛 TimeoutError，宿主拿不到超时信号
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(computer.shutdown(), timeout=0.2)
    finally:
        await _force_teardown(client)


@pytest.mark.anyio
async def test_shutdown_restores_cancellation_swallowed_in_close_task(tmp_path: Path) -> None:
    """#211：取消落在 ``_close_task`` 的等待上（被其 ``except CancelledError: return`` 吞掉）时同样须还原。

    用「卡死的 keep-alive 任务」把 ``_close_task`` 钉在限时等待里，取消必定落在该等待上。
    """
    computer, client = await _boot(tmp_path, "comp-211-close-task")
    monkeypatch_timeout = 2.0

    async def _stuck() -> None:
        await asyncio.Event().wait()

    stuck = asyncio.create_task(_stuck())
    client._session_keep_alive_task = stuck
    original_timeout = base_client_mod._TEARDOWN_TIMEOUT
    base_client_mod._TEARDOWN_TIMEOUT = monkeypatch_timeout
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(computer.shutdown(), timeout=0.2)
    finally:
        base_client_mod._TEARDOWN_TIMEOUT = original_timeout
        stuck.cancel()
        await _force_teardown(client)
