# -*- coding: utf-8 -*-
# filename: test_base_client.py
# @Time    : 2025/8/18 19:26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
import asyncio
import contextlib
import logging
from asyncio import Queue
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from mcp import ClientSession
from mcp.types import (
    AnyUrl,
    Implementation,
    InitializeResult,
    ListResourcesResult,
    ListToolsResult,
    Resource,
    ResourcesCapability,
    ServerCapabilities,
)
from pydantic import BaseModel
from transitions.core import MachineError

from a2c_smcp.computer.mcp_clients import base_client as base_client_mod
from a2c_smcp.computer.mcp_clients.base_client import STATES, BaseMCPClient


# 创建测试用的具体客户端实现
class MockMCPClient(BaseMCPClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._async_session = None
        self.prepare_called = False
        self.can_called = False
        self.before_called = False
        self.after_called = False
        self.on_enter_called = False
        self.disabled = False
        self.forbidden_tools = []
        self.tool_meta = {}
        self.aexit_stack = AsyncMock(spec=AsyncExitStack)

    async def _create_async_session(self) -> ClientSession:
        return MagicMock(spec=ClientSession)

    async def aprepare_connect(self, event):
        await super().aprepare_connect(event)
        self.prepare_called = True

    async def acan_connect(self, event):
        await super().acan_connect(event)
        self.can_called = True
        return True

    async def abefore_connect(self, event):
        await super().abefore_connect(event)
        self.before_called = True

    async def on_enter_connected(self, event):
        await super().on_enter_connected(event)

    async def aafter_connect(self, event):
        await super().aafter_connect(event)
        self.after_called = True

    async def on_enter_disconnected(self, event):
        self.on_enter_called = True
        await super().on_enter_disconnected(event)


# ------------------------------
# InitializeResult 相关的辅助类 / Helpers for InitializeResult related tests
# ------------------------------


class DummyParams(BaseModel):
    """
    中文: 伪造的参数模型，用于构造客户端实例。
    英文: Dummy parameter model used to construct client instance.
    """

    value: int = 1


class FakeClientSession:
    """
    中文: 伪造的 ClientSession，仅实现 initialize() 用于返回固定的 InitializeResult。
    英文: Fake ClientSession implementing initialize() to return a fixed InitializeResult.
    """

    def __init__(self) -> None:
        self.initialized = False

    async def initialize(self) -> InitializeResult:
        self.initialized = True
        return InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=ServerCapabilities(),
            serverInfo=Implementation(name="fake", version="1.0.0"),
            instructions="test",
        )


class InitializeResultTestClient(BaseMCPClient):
    """
    中文: 用于测试的 BaseMCPClient 子类，返回 FakeClientSession，避免与现有 TestMCPClient 命名冲突。
    英文: Test subclass of BaseMCPClient returning a FakeClientSession, avoiding name conflict with TestMCPClient.
    """

    async def _create_async_session(self):
        # 这里不做额外上下文，仅返回伪造 session / No extra context, just return fake session
        return FakeClientSession()


# pytest fixture 创建测试客户端
@pytest.fixture
async def client():
    """创建一个测试用的 MCP 客户端实例"""
    state_changes = Queue()

    # 状态变化回调收集器
    async def state_change_callback(from_state, to_state):
        await state_changes.put((from_state, to_state))

    # 创建并返回客户端和状态变化队列
    client = MockMCPClient(params=MagicMock(), state_change_callback=state_change_callback)
    return client, state_changes


@pytest.fixture
async def connected_client(client):
    """返回已连接状态的客户端"""
    client, state_changes = client
    await client.aconnect()
    return client, state_changes


# 辅助函数用于从队列中获取状态变化
async def get_next_state_change(state_changes):
    """从状态变化队列中获取下一个变化，最多等待1秒"""
    return await asyncio.wait_for(state_changes.get(), timeout=1.0)


# 测试用例
@pytest.mark.asyncio
async def test_initial_state(client):
    """测试初始状态是否正确"""
    client_instance, _ = client
    assert client_instance.state == STATES.initialized


@pytest.mark.asyncio
async def test_successful_connection_flow(client):
    """测试完整的连接流程"""
    client_instance, state_changes = client

    # 触发连接
    await client_instance.aconnect()

    # 验证状态转换
    assert client_instance.state == STATES.connected

    # 验证回调函数被调用
    from_state, to_state = await get_next_state_change(state_changes)
    assert from_state == STATES.initialized.value
    assert to_state == STATES.connected.value

    # 验证各阶段回调被调用
    assert client_instance.prepare_called
    assert client_instance.can_called
    assert client_instance.before_called
    assert client_instance.after_called


@pytest.mark.asyncio
async def test_disconnection_flow(connected_client):
    """测试断开连接流程"""
    client_instance, state_changes = connected_client
    await state_changes.get()  # 清除初始连接状态变化

    # 触发断开连接
    await client_instance.adisconnect()

    # 验证状态转换
    assert client_instance.state == STATES.disconnected

    # 验证回调函数被调用
    from_state, to_state = await get_next_state_change(state_changes)
    assert from_state == STATES.connected
    assert to_state == STATES.disconnected

    # 验证断开时的资源清理
    assert client_instance._async_session is None
    assert client_instance.on_enter_called


@pytest.mark.asyncio
async def test_disconnect_from_disconnected_state(connected_client):
    """测试从断开状态断开连接"""
    client_instance, _ = connected_client
    await client_instance.adisconnect()
    assert client_instance.state == STATES.disconnected
    with pytest.raises(MachineError):
        await client_instance.adisconnect()


@pytest.mark.asyncio
async def test_error_transition(client):
    """测试错误状态转换"""
    client_instance, state_changes = client

    # 触发错误
    await client_instance.aerror()

    # 验证状态转换
    assert client_instance.state == STATES.error

    # 验证回调函数被调用
    from_state, to_state = await get_next_state_change(state_changes)
    assert from_state == STATES.initialized
    assert to_state == STATES.error


@pytest.mark.asyncio
async def test_async_session_property(client):
    """测试async_session属性"""
    client_instance, _ = client

    # 初始状态下会话为空
    assert client_instance._async_session is None

    # 访问属性触发连接
    session = await client_instance.async_session

    # 验证会话已创建
    assert session is not None

    # 验证状态转换
    assert client_instance.state == STATES.connected


@pytest.mark.asyncio
async def test_list_tools_success(connected_client):
    """测试list_tools成功场景"""
    client_instance, _ = connected_client
    # 模拟会话对象
    mock_session = client_instance._async_session
    ret_mock = MagicMock(spec=ListToolsResult)
    ret_mock.tools = []
    ret_mock.nextCursor = None
    mock_session.list_tools = AsyncMock(return_value=ret_mock)

    # 调用方法
    await client_instance.list_tools()

    # 验证正确调用
    mock_session.list_tools.assert_called()


@pytest.mark.asyncio
async def test_list_tools_error(client):
    """测试list_tools在未连接时抛出异常"""
    client_instance, _ = client
    with pytest.raises(ConnectionError, match="Not connected to server"):
        await client_instance.list_tools()


@pytest.mark.asyncio
async def test_call_tool_success(connected_client):
    """测试call_tool成功场景"""
    client_instance, _ = connected_client
    # 模拟会话对象
    mock_session = client_instance._async_session
    mock_session.call_tool = AsyncMock()

    # 调用方法
    await client_instance.call_tool("test_tool", {"param": "value"})

    # 验证正确调用
    mock_session.call_tool.assert_called_with("test_tool", {"param": "value"})


@pytest.mark.asyncio
async def test_call_tool_error(client):
    """测试call_tool在未连接时抛出异常"""
    client_instance, _ = client
    with pytest.raises(ConnectionError, match="Not connected to server"):
        await client_instance.call_tool("test_tool", {})


@pytest.mark.asyncio
async def test_failed_connection(client, mocker):
    """测试连接失败场景"""
    client_instance, state_changes = client

    # 模拟连接条件检查失败
    mocker.patch.object(MockMCPClient, "acan_connect", AsyncMock(return_value=False))

    # 触发连接
    await client_instance.aconnect()

    # 验证状态未变化
    assert client_instance.state == STATES.initialized

    # 验证没有状态变化回调
    assert state_changes.empty()


@pytest.mark.asyncio
async def test_transition_to_initialized(connected_client):
    """测试初始化状态转换"""
    client_instance, state_changes = connected_client
    await state_changes.get()  # 清除初始连接状态变化

    # 触发初始化
    await client_instance.ainitialize()

    # 验证状态转换
    assert client_instance.state == STATES.initialized

    # 验证回调函数被调用
    from_state, to_state = await get_next_state_change(state_changes)
    assert from_state == STATES.connected
    assert to_state == STATES.initialized

    # 验证资源被清理
    assert client_instance._async_session is None


@pytest.mark.asyncio
async def test_enter_error_state_releases_resources(client):
    """测试进入错误状态时释放资源"""
    client_instance, state_changes = client

    # 设置会话对象存在
    mock_session = AsyncMock()
    client_instance._async_session = mock_session

    # 触发错误状态
    await client_instance.aerror()

    # 确保进入错误状态时的清理被调用
    assert client_instance._async_session is not None
    # 目前 aerror 经过优化与迭代，不再显式调用 aclose 方法，而是根据目前的任务状态动态判断调用。
    client_instance.aexit_stack.aclose.assert_not_called()
    assert client_instance.state == STATES.error


# ------------------------------
# InitializeResult 相关测试 / Tests for InitializeResult logic
# ------------------------------


@pytest.mark.asyncio
async def test_initialize_result_set_and_cleared_on_disconnect():
    """
    中文: 连接后应设置 initialize_result；断开后应清理为 None。
    英文: After connect, initialize_result should be set; after disconnect, it should be cleared to None.
    """
    client = InitializeResultTestClient(DummyParams())

    # 连接 / connect
    await client.aconnect()

    # 等待会话创建成功 / wait for session creation
    await client._create_session_success_event.wait()

    # 验证初始化结果存在 / ensure initialize_result is present
    assert client.initialize_result is not None
    assert client.initialize_result.serverInfo.name == "fake"

    # 断开 / disconnect
    await client.adisconnect()

    # 等待会话真正关闭 / wait for actual session teardown
    await client._async_session_closed_event.wait()

    # 验证初始化结果被清理 / ensure initialize_result is cleared
    assert client.initialize_result is None


@pytest.mark.asyncio
async def test_initialize_result_cleared_on_error():
    """
    中文: 出错流程中也应清理 initialize_result。
    英文: initialize_result should be cleared in error path as well.
    """
    client = InitializeResultTestClient(DummyParams())

    # 连接 / connect
    await client.aconnect()
    await client._create_session_success_event.wait()
    assert client.initialize_result is not None

    # 触发错误 / trigger error (will cancel keep-alive and teardown session)
    await client.aerror()

    # 等待会话真正关闭 / wait for actual session teardown
    await client._async_session_closed_event.wait()

    # 应被清理 / should be cleared
    assert client.initialize_result is None


# ------------------------------
# list_windows 相关测试 / Tests for list_windows logic
# ------------------------------


@pytest.mark.asyncio
async def test_list_windows_without_subscribe_still_returns_resources(connected_client):
    """
    中文: 当 capabilities.resources 存在但 subscribe=False 时，list_windows 仍应正常列出 window:// 资源。
    英文: When capabilities.resources exists but subscribe=False, list_windows should still list window:// resources.

    Bug: list_windows 误将 resources.subscribe 作为前置门禁，subscribe=False 时直接返回空列表。
    """
    client_instance, _ = connected_client
    mock_session = client_instance._async_session

    # 设置 initialize_result，resources 存在但 subscribe=False
    client_instance._initialize_result = InitializeResult(
        protocolVersion="2025-06-18",
        capabilities=ServerCapabilities(
            resources=ResourcesCapability(subscribe=False, listChanged=False),
        ),
        serverInfo=Implementation(name="test", version="1.0.0"),
    )

    # mock list_resources 返回 window:// 资源
    mock_session.list_resources = AsyncMock(
        return_value=ListResourcesResult(
            resources=[
                Resource(uri=AnyUrl("window://test.host/main?priority=80"), name="Main Window"),
                Resource(uri=AnyUrl("window://test.host/secondary?priority=20"), name="Secondary"),
                Resource(uri=AnyUrl("https://example.com/not-a-window"), name="Not Window"),
            ]
        )
    )

    windows = await client_instance.list_windows()

    # 应返回 2 个 window:// 资源，而非空列表
    assert len(windows) == 2
    uris = [str(r.uri) for r in windows]
    assert all(u.startswith("window://") for u in uris)
    # 按 priority 降序排列
    assert "/main" in uris[0]
    assert "/secondary" in uris[1]
    # subscribe=False 时不应调用 subscribe_resource
    mock_session.subscribe_resource.assert_not_called() if hasattr(mock_session, "subscribe_resource") else None


# -------------------- #96 取消最后一公里：call_tool 取消时补发 MCP notifications/cancelled --------------------
# #96 cancel last-mile: call_tool emits MCP notifications/cancelled to the remote on cancellation


class _BlockingCancelSession:
    """伪造 ClientSession：call_tool 阻塞直到被取消；记录 send_notification 调用（含捕获的 request_id）。"""

    def __init__(self, request_id: int) -> None:
        self._request_id = request_id  # base_client.call_tool 会在调用前同步读取它作为本次请求 id
        self.send_notification = AsyncMock()
        self.started = asyncio.Event()

    async def call_tool(self, tool_name: str, params: dict):  # noqa: ANN001, ARG002
        self.started.set()
        await asyncio.sleep(30)  # 阻塞直到被取消 / block until cancelled
        raise AssertionError("call_tool 不应正常返回（应被取消）/ should be cancelled, not return")


@pytest.mark.asyncio
async def test_call_tool_cancel_emits_mcp_cancelled(connected_client):
    """#96：call_tool 被取消时向远端补发 notifications/cancelled（requestId == 捕获 id），并仍抛 CancelledError。"""
    client_instance, _ = connected_client
    fake = _BlockingCancelSession(request_id=42)
    client_instance._async_session = fake  # 直接注入受控 session（已连接态）

    task = asyncio.ensure_future(client_instance.call_tool("slow", {"x": 1}))
    await asyncio.wait_for(fake.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake.send_notification.await_count == 1, "取消时应补发一次 notifications/cancelled"
    notif = fake.send_notification.await_args.args[0]
    assert notif.root.method == "notifications/cancelled"
    assert notif.root.params.requestId == 42  # 即 base_client 捕获的本次 MCP request_id
    assert notif.root.params.reason == "A2C tool_call cancelled"


@pytest.mark.asyncio
async def test_emit_mcp_cancelled_builds_client_notification(client):
    """#96：_emit_mcp_cancelled 以 ClientNotification(CancelledNotification(requestId=...)) 调 send_notification，异常全吞。"""
    client_instance, _ = client
    session = MagicMock(spec=ClientSession)
    session.send_notification = AsyncMock()

    await client_instance._emit_mcp_cancelled(session, 99)

    assert session.send_notification.await_count == 1
    notif = session.send_notification.await_args.args[0]
    assert notif.root.params.requestId == 99
    # send_notification 抛错也不应外泄（best-effort）/ failures are swallowed
    session.send_notification = AsyncMock(side_effect=RuntimeError("boom"))
    await client_instance._emit_mcp_cancelled(session, 100)  # 不抛异常即通过


# ------------------------------
# #211 停机收敛：关闭信号是无条件承诺 + 断开等待有上界
# ------------------------------


class _ExplodingExitStack:
    """替身 exit stack：``aclose()`` 抛 ``ExceptionGroup(BrokenResourceError)``。

    即 #211 的真实拆除异常形态 —— 拆除窗口内服务端消息到达时，mcp stdio 的 ``stdout_reader``
    向已关闭的读流 ``send`` → anyio task group 聚合为该 ExceptionGroup。
    """

    def __init__(self) -> None:
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        raise ExceptionGroup("unhandled errors in a TaskGroup", [anyio.BrokenResourceError()])


async def _connected_client() -> InitializeResultTestClient:
    """连上假会话的 client（供 #211 用例复用）。"""
    client = InitializeResultTestClient(DummyParams())
    await client.aconnect()
    await client._create_session_success_event.wait()
    return client


@pytest.mark.asyncio
async def test_closed_signal_survives_teardown_exception():
    """#211：``aclose()`` 抛异常不得让关闭信号与状态清理缺席 —— 否则 on_enter_disconnected 永久等待。"""
    client = await _connected_client()
    # 前置非空断言：确有需要被清理的状态（防空过）/ preconditions must be non-vacuous
    assert client.initialize_result is not None
    assert not client._async_session_closed_event.is_set()

    # 播种非空，令「已清空」断言非空过 / seed non-empty so the cleared-assertion is not vacuous
    client._subscribed_window_uris.add("window://seed.example/main")
    exploding = _ExplodingExitStack()
    client._aexit_stack = exploding  # type: ignore[assignment]

    # 缺陷形态：拆除异常跳过 finally 后半段 → 信号缺席 → 只能在 wait_for 超时处「正常返回」
    await asyncio.wait_for(client.adisconnect(), timeout=2.0)

    assert exploding.aclose_calls == 1, "拆除必须真的发生过（否则后续断言整体空过）"
    assert client._async_session_closed_event.is_set(), "aclose() 抛异常时关闭信号仍须照常置位"
    assert client._async_session is None, "会话引用必须被清理"
    assert client.initialize_result is None, "初始化结果必须被清理"
    assert client._subscribed_window_uris == set(), "会话级订阅集合必须随会话拆除清空"


@pytest.mark.asyncio
async def test_closed_signal_and_cleanup_on_normal_teardown():
    """阴性对照：正常拆除路径同样置位并清理（防上条断言只在异常路径下成立）。"""
    client = await _connected_client()
    assert client.initialize_result is not None
    client._subscribed_window_uris.add("window://seed.example/main")

    await asyncio.wait_for(client.adisconnect(), timeout=2.0)

    assert client._async_session_closed_event.is_set()
    assert client._async_session is None
    assert client.initialize_result is None
    assert client._subscribed_window_uris == set()


@pytest.mark.asyncio
async def test_disconnect_wait_is_bounded_when_signal_never_arrives(monkeypatch):
    """#211 兜底：``_close_task`` 的早退分支可能永不置位信号 → 断开等待必须有上界，不得永久等待。"""
    monkeypatch.setattr(base_client_mod, "_TEARDOWN_TIMEOUT", 0.3)
    client = await _connected_client()

    async def _never_signals() -> None:
        """桩：既不拆除也不置位关闭信号（等价于 _close_task 的早退分支）"""

    client._close_task = _never_signals  # type: ignore[method-assign]
    kill_spy = AsyncMock()
    client._aforce_kill = kill_spy  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(client.adisconnect(), timeout=2.0)
    elapsed = loop.time() - started

    assert elapsed < 1.0, f"断开必须在有界时间内返回，实测 {elapsed:.2f}s"
    assert client.state == STATES.disconnected
    assert kill_spy.await_count == 1, "信号缺席 = 拆除状态未知 → 兜底须强杀子进程树"

    # 清理：_close_task 被桩掉 → keep-alive 任务仍在等关闭信号
    client._close_event.set()
    task = client._session_keep_alive_task
    if task is not None and not task.done():
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)


@pytest.mark.asyncio
async def test_teardown_error_is_logged_as_warning_not_error():
    """#211：拆除异常属非致命（信号已置位、停机已收敛）→ 日志级别须为 WARNING，不得暗示「启动失败」。"""
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    # a2c_smcp 包 logger propagate=False（不向根传播），故直接挂到子 logger 上捕获
    target = logging.getLogger("a2c_smcp.computer")
    handler = _Collect()
    target.addHandler(handler)
    try:
        client = await _connected_client()
        client._aexit_stack = _ExplodingExitStack()  # type: ignore[assignment]
        await asyncio.wait_for(client.adisconnect(), timeout=2.0)
    finally:
        target.removeHandler(handler)

    teardown = [r for r in records if "teardown error" in r.getMessage()]
    assert teardown, "拆除异常必须留下日志记录（否则本断言空过）"
    assert [r.levelname for r in teardown] == ["WARNING"] * len(teardown), [r.levelname for r in teardown]


# ------------------------------
# #247 传输失败后的状态机收敛与半断开 teardown 幂等
# #247 state-machine convergence after transport failure + half-broken teardown idempotency
# ------------------------------


async def _wait_for_state(client: BaseMCPClient, expected: STATES, timeout: float = 2.0) -> None:
    """有界等待状态机到达 expected（避免对事件循环调度顺序做时序假设）。"""

    async def _poll() -> None:
        while client.state != expected:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_unexpected_keepalive_exit_converges_and_disconnect_is_idempotent() -> None:
    """#247：keep-alive 宿主任务被 anyio 取消（非 _close_task 发起）后，状态机须收敛到 error。

    复现真实形态：取消被 ``except CancelledError`` 吞掉，``finally`` 的 ``aclose()`` 抛
    ``ExceptionGroup`` 并清空会话；状态机不得滞留 connected，且半断开（error）状态下
    ``adisconnect`` 不抛。
    """
    client = await _connected_client()
    assert client.state == STATES.connected
    assert client._async_session is not None
    client._aexit_stack = _ExplodingExitStack()  # type: ignore[assignment]
    task = client._session_keep_alive_task
    assert task is not None

    task.cancel()  # 模拟 anyio 取消宿主任务（不经 _close_task ⇒ _close_event 未置位）
    await _wait_for_state(client, STATES.error, timeout=2.0)

    assert client._async_session is None
    assert client._async_session_closed_event.is_set()
    # 前置形态断言（非红/绿判据）：任务确以 ExceptionGroup 结束，证明夹具复现了拆除异常形态
    with pytest.raises(ExceptionGroup):
        await task

    # 半断开 → 断开：不得抛 MachineError
    await asyncio.wait_for(client.adisconnect(), timeout=2.0)
    assert client.state == STATES.disconnected


@pytest.mark.asyncio
async def test_disconnect_with_dead_session_does_not_reconnect() -> None:
    """#247 根因②：``acan_disconnect`` 不得再经惰性 ``async_session`` 触发 ``aconnect``。"""
    client = await _connected_client()
    client._async_session = None  # 半断开：状态 connected 但会话已清空（传输死亡后的形态）
    await asyncio.wait_for(client.adisconnect(), timeout=2.0)
    assert client.state == STATES.disconnected


@pytest.mark.asyncio
async def test_keepalive_done_callback_guards() -> None:
    """#247：收敛回调的守卫矩阵——非「本代任务 + 非优雅关闭 + connected + 已就绪」一律不收敛。"""
    sentinel = asyncio.create_task(asyncio.sleep(0))
    await sentinel

    # ① 已被新连接替换（identity 不符）
    c1 = await _connected_client()
    c1_task = c1._session_keep_alive_task
    c1._on_keep_alive_task_done(sentinel)
    assert c1._converge_task is None
    assert c1.state == STATES.connected

    # ② 优雅关闭（_close_event 已置位）
    c2 = await _connected_client()
    c2_task = c2._session_keep_alive_task
    c2._close_event.set()
    c2._session_keep_alive_task = sentinel
    c2._session_ready = True
    c2._on_keep_alive_task_done(sentinel)
    assert c2._converge_task is None
    assert c2.state == STATES.connected

    # ③ 状态非 connected
    c3 = await _connected_client()
    c3_task = c3._session_keep_alive_task
    await c3.adisconnect()
    c3._session_keep_alive_task = sentinel
    c3._session_ready = True
    c3._on_keep_alive_task_done(sentinel)
    assert c3._converge_task is None
    assert c3.state == STATES.disconnected

    # ④ connect 阶段（initialize 未完成）
    c4 = await _connected_client()
    c4_task = c4._session_keep_alive_task
    c4._session_ready = False
    c4._session_keep_alive_task = sentinel
    c4._on_keep_alive_task_done(sentinel)
    assert c4._converge_task is None
    assert c4.state == STATES.connected

    # 清理：放行仍存活的真实任务并等待其退出（避免悬挂任务/跨用例泄漏）
    c1._close_event.set()
    c4._close_event.set()
    originals = [t for t in (c1_task, c2_task, c3_task, c4_task) if t is not None]
    await asyncio.wait_for(asyncio.gather(*originals, return_exceptions=True), timeout=2.0)


@pytest.mark.asyncio
async def test_convergence_runs_in_clean_transitions_context(monkeypatch) -> None:  # noqa: PT019
    """#247：收敛事件必须以干净 transitions 上下文运行（不得继承 aconnect 的 current_context）。

    继承会让 ``process_context`` 走 nested 分支：收敛事件自身不可取消、却能取消其它在途
    transition——生产上表现为 shutdown 与死亡并发时行为依赖调度顺序。
    """
    client = await _connected_client()
    assert client._session_ready is True
    seen: list[object] = []
    original = client.aerror

    async def _probe(*args, **kwargs):  # noqa: ANN002, ANN003
        seen.append(base_client_mod.A2CAsyncMachine.current_context.get())
        return await original(*args, **kwargs)

    monkeypatch.setattr(client, "aerror", _probe)
    client._aexit_stack = _ExplodingExitStack()  # type: ignore[assignment]
    task = client._session_keep_alive_task
    assert task is not None

    task.cancel()
    await _wait_for_state(client, STATES.error, timeout=2.0)
    assert seen == [None], f"收敛上下文必须干净，实测 {seen!r}"


@pytest.mark.asyncio
async def test_convergence_does_not_tear_down_reconnected_session() -> None:
    """#247 TOCTOU：收敛任务执行前新连接已接管 ⇒ 不得把新会话打成 error。"""
    client = await _connected_client()
    old = asyncio.create_task(asyncio.sleep(0))
    await old

    await asyncio.wait_for(client._aconverge_unexpected_exit(old), timeout=1.0)
    assert client.state == STATES.connected

    await asyncio.wait_for(client.adisconnect(), timeout=2.0)


@pytest.mark.asyncio
async def test_graceful_teardown_is_not_treated_as_unexpected_exit() -> None:
    """#247 负对照：优雅拆除（状态已离开 connected）不得收敛为 error。

    本路径同时满足 state 守卫；``_close_event`` 守卫由
    ``test_keepalive_done_callback_guards`` ② 独立钉住。
    """
    client = await _connected_client()
    client._aexit_stack = _ExplodingExitStack()  # type: ignore[assignment]
    await asyncio.wait_for(client.adisconnect(), timeout=2.0)
    await asyncio.sleep(0.05)
    assert client.state == STATES.disconnected


@pytest.mark.asyncio
async def test_convergence_gives_up_when_transition_never_finishes(monkeypatch) -> None:  # noqa: PT019
    """#247 🟡：在途 transition 超时未结束时，收敛有界放弃并告警（不能立即放弃）。"""
    monkeypatch.setattr(base_client_mod, "_CONVERGE_INFLIGHT_TIMEOUT", 0.2)
    client = await _connected_client()
    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger("a2c_smcp.computer")
    handler = _Collect()
    target.addHandler(handler)
    blocker = asyncio.create_task(asyncio.sleep(30))
    tasks = client.machine.async_tasks.setdefault(id(client), [])
    tasks.append(blocker)
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(client._aconverge_unexpected_exit(keep_alive), timeout=2.0)
        elapsed = loop.time() - started
        assert client.state == STATES.connected, "有在途 transition 且超时 → 不得强行 aerror"
        assert elapsed >= 0.15, f"必须先等 deadline 再放弃，实测 {elapsed:.2f}s"
        assert any("gave up waiting for in-flight transitions" in r.getMessage() for r in records), (
            "超时放弃必须留下 WARNING"
        )
    finally:
        target.removeHandler(handler)
        tasks.remove(blocker)
        if not tasks:
            client.machine.async_tasks.pop(id(client), None)
        blocker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await blocker
        await asyncio.wait_for(client.adisconnect(), timeout=2.0)


class _GatedInitSession:
    """initialize() 由测试显式放行/失败的假会话（#247 connect 窗口用例）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail

    async def initialize(self) -> InitializeResult:
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("initialize failed")
        return InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=ServerCapabilities(),
            serverInfo=Implementation(name="gated", version="1.0.0"),
        )


class _GatedInitClient(BaseMCPClient):
    """返回可被测试卡住/失败的 ``initialize()`` 会话。"""

    def __init__(self, params: BaseModel, *, fail_initialize: bool = False) -> None:
        super().__init__(params)  # type: ignore[arg-type]
        self.session = _GatedInitSession(fail=fail_initialize)

    async def _create_async_session(self) -> ClientSession:  # type: ignore[override]
        return self.session  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_keepalive_death_during_initialize_fails_connect_and_converges() -> None:
    """#247 🔴：initialize() 窗口内 keep-alive 死亡 ⇒ aconnect 必须失败且状态收敛 error。

    旧实现：``self.async_session`` 惰性属性在 ``_async_session`` 被 finally 清空后触发
    ``aconnect`` from ``connected`` → ``MachineError``，且会话已死却被当作连接成功。
    """
    client = _GatedInitClient(DummyParams())
    connect_task = asyncio.create_task(client.aconnect())
    await asyncio.wait_for(client.session.started.wait(), timeout=2.0)

    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None
    keep_alive.cancel()  # 模拟 anyio 取消宿主任务
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(keep_alive), timeout=2.0)

    client.session.release.set()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(connect_task, timeout=2.0)

    await _wait_for_state(client, STATES.error, timeout=2.0)
    assert client._async_session is None
    assert client._initialize_result is None, "失败连接不得留下 stale initialize_result"
    assert client._async_session_closed_event.is_set()


@pytest.mark.asyncio
async def test_initialize_failure_tears_down_session_and_converges() -> None:
    """#247 🟡：``initialize()`` 失败必须回收 keep-alive/会话，并以 error 收口（不泄漏）。"""
    client = _GatedInitClient(DummyParams(), fail_initialize=True)
    connect_task = asyncio.create_task(client.aconnect())
    await asyncio.wait_for(client.session.started.wait(), timeout=2.0)
    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None

    client.session.release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(connect_task, timeout=2.0)

    await _wait_for_state(client, STATES.error, timeout=2.0)
    assert keep_alive.done(), "initialize() 失败后 keep-alive 任务必须被回收"
    assert client._async_session is None
    assert client._initialize_result is None, "失败连接不得留下 stale initialize_result"
    assert client._async_session_closed_event.is_set()


class _FailingCreateClient(BaseMCPClient):
    """``_create_async_session`` 直接失败（模拟 stdio 命令不存在 / 传输在创建期被取消）。"""

    async def _create_async_session(self) -> ClientSession:  # type: ignore[override]
        raise FileNotFoundError("mcp command not found")


class _GatedCreateClient(BaseMCPClient):
    """``_create_async_session`` 由测试显式放行（模拟 stdio spawn / sse 建连进行中）。"""

    def __init__(self, params: BaseModel) -> None:
        super().__init__(params)  # type: ignore[arg-type]
        self.create_started = asyncio.Event()
        self.create_release = asyncio.Event()

    async def _create_async_session(self) -> ClientSession:  # type: ignore[override]
        self.create_started.set()
        await self.create_release.wait()
        return FakeClientSession()  # type: ignore[return-value]


class _HangingInitSession:
    """``initialize()` 永不返回（mcp 无读超时的传输黑洞形态）。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def initialize(self) -> InitializeResult:
        self.started.set()
        await asyncio.Event().wait()


class _HangingInitClient(BaseMCPClient):
    """返回 ``initialize()`` 永不返回的会话。"""

    def __init__(self, params: BaseModel) -> None:
        super().__init__(params)  # type: ignore[arg-type]
        self.session = _HangingInitSession()

    async def _create_async_session(self) -> ClientSession:  # type: ignore[override]
        return self.session  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_session_creation_failure_fails_connect_fast() -> None:
    """#247 🔴：会话创建失败不得让 aconnect 永久等待 success 事件（stdio/sse 无 bounded connect）。"""
    client = _FailingCreateClient(DummyParams())
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ConnectionError) as ei:
        await asyncio.wait_for(client.aconnect(), timeout=2.0)
    assert isinstance(ei.value.__cause__, FileNotFoundError), "创建失败根因必须以 __cause__ 保留"
    assert loop.time() - started < 1.0, "创建失败必须快速失败，不得挂到外层 timeout"
    await _wait_for_state(client, STATES.error, timeout=2.0)
    assert client._async_session is None


@pytest.mark.asyncio
async def test_cancel_during_session_acquisition_tears_down_keepalive() -> None:
    """#247 🟡：connect 在会话获取阶段被取消 ⇒ keep-alive/会话必须收口，不得成为孤儿。"""
    client = _GatedCreateClient(DummyParams())
    connect_task = asyncio.create_task(client.aconnect())
    await asyncio.wait_for(client.create_started.wait(), timeout=2.0)
    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None

    connect_task.cancel()
    client.create_release.set()  # 放行创建，使收口在有界时间内可完成
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(connect_task, timeout=2.0)

    await _wait_for_state(client, STATES.error, timeout=2.0)
    assert keep_alive.done(), "取消后 keep-alive 任务必须被回收"
    assert client._async_session is None
    assert client._async_session_closed_event.is_set()


@pytest.mark.asyncio
async def test_initialize_hang_watchdog_fails_connect_fast() -> None:
    """#247 🔴：initialize() 永不返回 + keep-alive 死亡 ⇒ aconnect 必须快返（mcp 无读超时）。"""
    client = _HangingInitClient(DummyParams())
    connect_task = asyncio.create_task(client.aconnect())
    await asyncio.wait_for(client.session.started.wait(), timeout=2.0)

    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None
    keep_alive.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(keep_alive), timeout=2.0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(connect_task, timeout=2.0)
    assert loop.time() - started < 1.0, "initialize 黑洞必须被看门狗快速打断"

    await _wait_for_state(client, STATES.error, timeout=2.0)
    assert client._async_session is None
    assert client._async_session_closed_event.is_set()


@pytest.mark.asyncio
async def test_cancel_during_initialize_tears_down_keepalive() -> None:
    """#247 🔴：initialize 挂起期间取消 aconnect ⇒ keep-alive/会话必须收口（不能只剩裸 raise）。"""
    client = _GatedInitClient(DummyParams())
    connect_task = asyncio.create_task(client.aconnect())
    await asyncio.wait_for(client.session.started.wait(), timeout=2.0)
    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None

    connect_task.cancel()
    client.session.release.set()  # 放行 initialize，使收口有界
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(connect_task, timeout=2.0)

    await _wait_for_state(client, STATES.error, timeout=2.0)
    assert keep_alive.done(), "取消后 keep-alive 任务必须被回收"
    assert client._async_session is None
    assert client._async_session_closed_event.is_set()


@pytest.mark.asyncio
async def test_initialize_handshake_deadline_fails_connect_fast(monkeypatch) -> None:  # noqa: PT019
    """#247 🔴：initialize 永不返回且 keep-alive 仍存活 ⇒ 握手 deadline 必须失败（防 gate.drain 拖死）。"""
    monkeypatch.setattr(base_client_mod, "_CONNECT_HANDSHAKE_TIMEOUT", 0.2)
    client = _HangingInitClient(DummyParams())
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(client.aconnect(), timeout=2.0)
    assert loop.time() - started < 1.0, "握手 deadline 必须快速生效"
    assert client.state == STATES.error
    assert client._async_session is None
    assert client._session_keep_alive_task is not None
    assert client._session_keep_alive_task.done()


@pytest.mark.asyncio
async def test_afail_connect_clears_stale_state_and_force_kills() -> None:
    """#247 🔴2：_afail_connect 必须清 stale 初始化态，并对存活任务 best-effort 强杀（机制断言）。"""
    client = await _connected_client()
    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None
    client._initialize_result = InitializeResult(
        protocolVersion="2025-06-18",
        capabilities=ServerCapabilities(),
        serverInfo=Implementation(name="stale", version="1.0.0"),
    )
    client._subscribed_window_uris.add("window://seed.example/main")
    kill_spy = AsyncMock()
    client._aforce_kill = kill_spy  # type: ignore[method-assign]
    original_close_task = client._close_task

    async def _signal_only() -> None:
        client._close_event.set()

    client._close_task = _signal_only  # type: ignore[method-assign]
    try:
        await client._afail_connect(keep_alive, "unit-test")
    finally:
        client._close_task = original_close_task  # type: ignore[method-assign]

    assert client._initialize_result is None, "stale initialize_result 必须被清理"
    assert client._subscribed_window_uris == set(), "stale 订阅集合必须被清理"
    assert kill_spy.await_count == 1, "存活任务的拆除短路必须走 best-effort 强杀"

    # 清理：放行真实 keep-alive（close_event 已置位）并结算收敛任务
    await asyncio.wait_for(asyncio.shield(keep_alive), timeout=2.0)
    if client._converge_task is not None:
        await asyncio.wait_for(asyncio.shield(client._converge_task), timeout=2.0)


@pytest.mark.asyncio
async def test_afail_connect_ignores_stale_generation(monkeypatch) -> None:  # noqa: PT019
    """#247 🟡6：迟到的旧代 connect 失败不得拆/清新代（包括 _close_task 不得拆新代任务）。"""
    monkeypatch.setattr(base_client_mod, "_TEARDOWN_TIMEOUT", 0.1)
    client = await _connected_client()
    old_task = client._session_keep_alive_task
    assert old_task is not None
    new_task = asyncio.create_task(asyncio.sleep(30))  # 新代任务仍在运行
    await asyncio.sleep(0)
    client._session_keep_alive_task = new_task
    sentinel = InitializeResult(
        protocolVersion="2025-06-18",
        capabilities=ServerCapabilities(),
        serverInfo=Implementation(name="new-gen", version="1.0.0"),
    )
    client._initialize_result = sentinel
    client._subscribed_window_uris.add("window://seed.example/main")
    kill_spy = AsyncMock()
    original_force_kill = client._aforce_kill
    client._aforce_kill = kill_spy  # type: ignore[method-assign]
    try:
        await asyncio.wait_for(client._afail_connect(old_task, "stale"), timeout=2.0)
        assert client._initialize_result is sentinel
        assert client._subscribed_window_uris == {"window://seed.example/main"}
        assert kill_spy.await_count == 0
        assert not new_task.done(), "旧代收口不得拆除新代 keep-alive 任务"
    finally:
        client._aforce_kill = original_force_kill  # type: ignore[method-assign]
        new_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await new_task
        client._session_keep_alive_task = old_task
        await asyncio.wait_for(client.adisconnect(), timeout=2.0)


@pytest.mark.asyncio
async def test_convergence_defers_for_async_state_callback() -> None:
    """#247 🔴：aafter_connect 挂起在 async state_change_callback 时，收敛须延后而非永久放弃。"""
    cb_started = asyncio.Event()
    cb_release = asyncio.Event()

    async def _callback(from_state: str, to_state: str) -> None:
        if to_state == STATES.connected:
            cb_started.set()
            await cb_release.wait()

    client = InitializeResultTestClient(DummyParams(), state_change_callback=_callback)
    connect_task = asyncio.create_task(client.aconnect())
    await asyncio.wait_for(cb_started.wait(), timeout=2.0)

    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None
    keep_alive.cancel()  # 传输在 aafter_connect 的 callback 挂起点死亡
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(keep_alive), timeout=2.0)

    cb_release.set()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(connect_task, timeout=2.0)

    await _wait_for_state(client, STATES.error, timeout=3.0)
    assert client._async_session is None


@pytest.mark.asyncio
async def test_disconnect_from_error_without_keepalive_task_is_fast() -> None:
    """#247：从未连接过的 error client（无 keep-alive 任务）断开不得空等 ``_TEARDOWN_TIMEOUT``。"""
    client = InitializeResultTestClient(DummyParams())
    await client.aerror()
    assert client.state == STATES.error
    assert client._session_keep_alive_task is None

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(client.adisconnect(), timeout=2.0)
    elapsed = loop.time() - started
    assert client.state == STATES.disconnected
    assert elapsed < 1.0, f"无任务的 error client 断开应即时返回，实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_disconnect_after_reinitialize_error_is_fast() -> None:
    """#247 🟡：任务已 done 且 ainitialize 清掉关闭事件后，error 态断开不得空等 10s。"""
    client = _FailingCreateClient(DummyParams())
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(client.aconnect(), timeout=2.0)
    await _wait_for_state(client, STATES.error, timeout=2.0)

    await client.ainitialize()  # 清空 events；_close_task 因任务已 done 早退
    assert client._session_keep_alive_task is not None
    assert client._session_keep_alive_task.done()
    # 前置非空断言：关闭事件确实已被 on_enter_initialized 清掉（否则本用例空过）
    assert not client._async_session_closed_event.is_set()

    await client.aerror()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(client.adisconnect(), timeout=2.0)
    elapsed = loop.time() - started
    assert client.state == STATES.disconnected
    assert elapsed < 1.0, f"任务已 done 的 error client 断开不得空等，实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_disconnect_from_error_with_live_keepalive_task() -> None:
    """#247：error 态下 keep-alive 任务仍存活时，``adisconnect`` 必须真正等待其收尾。"""
    client = await _connected_client()
    keep_alive = client._session_keep_alive_task
    assert keep_alive is not None
    original_close_task = client._close_task

    async def _signal_only() -> None:
        client._close_event.set()

    client._close_task = _signal_only  # type: ignore[method-assign]
    await client.aerror()  # 置 error 但不等待 keep-alive（模拟错误路径的早退形态）
    client._close_task = original_close_task  # type: ignore[method-assign]
    assert client.state == STATES.error
    # 前置依赖基类不变量：aerror 的回调链在 set_state 前零 await ⇒ 任务此刻必然仍存活。
    assert not keep_alive.done(), "前置：任务在 adisconnect 入口仍应存活"

    await asyncio.wait_for(client.adisconnect(), timeout=2.0)
    assert client.state == STATES.disconnected
    assert client._async_session is None
    assert client._async_session_closed_event.is_set()
