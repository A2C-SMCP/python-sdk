# -*- coding: utf-8 -*-
# filename: test_tool_call_cancel.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：#209 Agent 侧在途工具调用取消入口 + 三态分类入口的单元测试。

覆盖协议 `events.md §server:tool_call_cancel`「Agent 侧主动取消的终态来源」三条裁决：
(a) 仅投递信号、继续等原 ack（不本地提早返回）；
(b) 不冒充 Computer（不本地合成 ``a2c_cancelled``）；
(c) 三态如实透出（按 ack 结果级 ``meta`` 分类，不依据「本端已发出取消信号」改写终态）。

English: Unit tests for the Agent-side cancel entry (#209) and the tri-state outcome classifier.
"""

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import CallToolResult, TextContent
from socketio.exceptions import TimeoutError as SioTimeoutError

from a2c_smcp.agent import (
    CancelSignal,
    ToolCallOutcome,
    classify_tool_call_outcome,
)
from a2c_smcp.agent._cancel import (
    CANCEL_WATCHER_THREAD_NAME,
    AsyncCancelWatcher,
    CancelSendGate,
    SyncCancelWatcher,
)
from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import CANCEL_TOOL_CALL_EVENT, SMCP_NAMESPACE

# 直接引用**源常量**（勿硬编码字面量：改名会让泄漏断言静默永真）。
# Reference the source constant: a hardcoded literal would make the leak assertions vacuously true.
CANCEL_THREAD_NAME = CANCEL_WATCHER_THREAD_NAME


# ────────────────────────── 测试替身 / Test doubles ──────────────────────────


class _IsSetOnly:
    """**仅**实现 ``is_set()`` 的取消信号 —— 协议硬约束的形状。

    宿主（如 TFRS 内核 ``CancellationToken``）只能提供 ``is_set()``：不得假定存在可 await
    的 ``wait``、也不得假定存在 ``set`` / ``is_cancelled``。本类**故意**不定义任何其它成员，
    故实现中任何越界访问都会立即 ``AttributeError``，成为该硬约束的回归锁。

    / A cancel signal exposing **only** ``is_set()`` — the protocol-mandated shape.
    """

    def __init__(self) -> None:
        self._flag = False

    def is_set(self) -> bool:
        return self._flag

    def fire(self) -> None:
        """测试侧置位（宿主侧对应 ``CancellationToken.cancel()``）/ flip the flag from the test."""
        self._flag = True


def _wire(
    *,
    is_error: bool = False,
    meta: dict[str, Any] | None = None,
    container: str = "meta",
    omit_is_error: bool = False,
) -> dict[str, Any]:
    """构造 wire 形态的 ``client:tool_call`` ack / build a wire-shaped ack payload."""
    payload: dict[str, Any] = {"content": [{"type": "text", "text": "x"}]}
    if not omit_is_error:
        payload["isError"] = is_error
    if meta is not None:
        payload[container] = meta
    return payload


CANCELLED_WIRE = _wire(is_error=True, meta={"a2c_cancelled": True, "a2c_cancel_reason": "agent_requested"})


def _cancel_threads() -> set[threading.Thread]:
    """当前存活的取消 watcher 线程集合 / currently-alive cancel watcher threads."""
    return {t for t in threading.enumerate() if t.name == CANCEL_THREAD_NAME}


@pytest.fixture
def auth_provider() -> DefaultAgentAuthProvider:
    return DefaultAgentAuthProvider(agent_id="test_agent", office_id="test_office", api_key="k")


@pytest.fixture
def async_client(auth_provider: DefaultAgentAuthProvider) -> AsyncSMCPAgentClient:
    return AsyncSMCPAgentClient(auth_provider=auth_provider, event_handler=None)


@pytest.fixture
def sync_client(auth_provider: DefaultAgentAuthProvider) -> SMCPAgentClient:
    return SMCPAgentClient(auth_provider=auth_provider, event_handler=None)


# ────────────────────────── 1. 三态分类入口 / Classifier ──────────────────────────


class TestToolCallOutcomeContract:
    """跨 SDK 契约：枚举字面量集合 / cross-SDK contract on the enum literals."""

    def test_values_are_the_cross_sdk_agreed_literals(self) -> None:
        """#209 / rust-sdk#218 约定的四个 snake_case 字面量，rust 侧按此对齐。

        注意：rust 现无任何字符串序列化（无 serde / rename_all / Display / as_str），
        python 的 StrEnum 是**首个具体序列化** —— 本断言即该契约的锚点。
        """
        assert {m.value for m in ToolCallOutcome} == {"completed", "timed_out", "cancelled", "failed"}

    def test_is_str_enum(self) -> None:
        """StrEnum：取值即字符串，宿主日志 / 轨迹字段可直接落库。"""
        assert ToolCallOutcome.CANCELLED == "cancelled"
        assert isinstance(ToolCallOutcome.TIMED_OUT, str)


class TestClassifyToolCallOutcome:
    """分类器：标记先于 isError、meta/_meta 双 key 宽松、仅布尔 true 计真。"""

    def test_completed(self) -> None:
        assert classify_tool_call_outcome(_wire()) is ToolCallOutcome.COMPLETED

    def test_is_error_key_absent_counts_as_completed(self) -> None:
        """``isError`` 缺席 → Completed（与 rust ``unwrap_or(false)`` 同判）。"""
        assert classify_tool_call_outcome(_wire(omit_is_error=True)) is ToolCallOutcome.COMPLETED

    def test_plain_failure(self) -> None:
        assert classify_tool_call_outcome(_wire(is_error=True)) is ToolCallOutcome.FAILED

    def test_cancelled_marker(self) -> None:
        assert (
            classify_tool_call_outcome(_wire(is_error=True, meta={"a2c_cancelled": True}))
            is ToolCallOutcome.CANCELLED
        )

    def test_timed_out_marker(self) -> None:
        assert (
            classify_tool_call_outcome(_wire(is_error=True, meta={"a2c_timeout": True})) is ToolCallOutcome.TIMED_OUT
        )

    def test_marker_wins_over_is_error(self) -> None:
        """取消 / 超时是对 ``isError`` 的语义细化 ⇒ **先判标记再判 isError**。

        若顺序写反，取消态会被误归为普通失败（协议 error-handling.md §取消语义）。
        """
        assert (
            classify_tool_call_outcome(_wire(is_error=False, meta={"a2c_cancelled": True}))
            is ToolCallOutcome.CANCELLED
        )

    def test_cancelled_takes_precedence_over_timeout(self) -> None:
        """两标记理论上不并存，但优先级必须确定：取消 > 超时（与 rust 同序）。"""
        assert (
            classify_tool_call_outcome(_wire(is_error=True, meta={"a2c_cancelled": True, "a2c_timeout": True}))
            is ToolCallOutcome.CANCELLED
        )

    def test_lenient_underscore_meta_key(self) -> None:
        """协议 data-structures.md：Consumer **SHOULD** 对 ``meta`` / ``_meta`` 双 key 宽松读取。"""
        for container in ("meta", "_meta"):
            assert (
                classify_tool_call_outcome(
                    _wire(is_error=True, meta={"a2c_cancelled": True}, container=container)
                )
                is ToolCallOutcome.CANCELLED
            )
            assert (
                classify_tool_call_outcome(_wire(is_error=True, meta={"a2c_timeout": True}, container=container))
                is ToolCallOutcome.TIMED_OUT
            )

    @pytest.mark.parametrize("falsy", ["true", 1, "1", 0, None, []])
    def test_only_boolean_true_counts(self, falsy: Any) -> None:
        """仅**布尔** ``true`` 计真（非真值其它类型不算）——与 rust ``as_bool()`` 同判。"""
        assert classify_tool_call_outcome(_wire(is_error=True, meta={"a2c_cancelled": falsy})) is ToolCallOutcome.FAILED

    def test_accepts_call_tool_result_object(self) -> None:
        """``CallToolResult`` 形态（Agent 经 ``by_name=True`` 解析后的实际入参）同判。"""
        assert classify_tool_call_outcome(CallToolResult.model_validate(CANCELLED_WIRE, by_name=True)) is (
            ToolCallOutcome.CANCELLED
        )
        assert classify_tool_call_outcome(CallToolResult.model_validate(_wire(), by_name=True)) is (
            ToolCallOutcome.COMPLETED
        )
        assert classify_tool_call_outcome(CallToolResult.model_validate(_wire(is_error=True), by_name=True)) is (
            ToolCallOutcome.FAILED
        )

    def test_object_with_real_meta_field(self) -> None:
        """结果级 ``meta`` 经**属性赋值**写入真实字段时同样命中（生产代码同 idiom）。"""
        result = CallToolResult(content=[TextContent(text="x", type="text")], isError=True)
        result.meta = {"a2c_timeout": True}
        assert classify_tool_call_outcome(result) is ToolCallOutcome.TIMED_OUT

    def test_object_built_with_ctor_meta_kwarg_is_still_classified(self) -> None:
        """构造器传 ``meta=`` 会落 ``model_extra``（字段 alias 是 ``_meta``）——须兜底读取。

        不兜底则这类结果被**静默误判**为普通失败（``FAILED``），宿主拿不到「取消」终态。
        """
        result = CallToolResult(
            content=[TextContent(text="x", type="text")],
            isError=True,
            meta={"a2c_cancelled": True},  # type: ignore[call-arg] - 刻意的陷阱写法
        )
        assert result.meta is None  # 真实字段确实为空（陷阱成立）/ the trap is real
        assert classify_tool_call_outcome(result) is ToolCallOutcome.CANCELLED


# ────────────────────────── 2. 入口形状 / Entry-point shape ──────────────────────────


class TestCancelSignalProtocol:
    def test_is_set_only_object_satisfies_protocol(self) -> None:
        """只要求 ``is_set()``：仅实现它的对象必须被接受（不得要求 wait/set/...）。"""
        assert isinstance(_IsSetOnly(), CancelSignal)

    def test_object_without_is_set_rejected(self) -> None:
        assert not isinstance(object(), CancelSignal)

    def test_stdlib_events_satisfy_protocol(self) -> None:
        """标准库事件天然满足最小契约（宿主可直接传，无需适配器）。"""
        assert isinstance(threading.Event(), CancelSignal)
        assert isinstance(asyncio.Event(), CancelSignal)


# ────────────────────────── 3. 异步路径 / Async path ──────────────────────────


class TestAsyncEmitToolCallCancel:
    @pytest.mark.asyncio
    async def test_signal_delivers_cancel_and_ack_stays_terminal(
        self, async_client: AsyncSMCPAgentClient
    ) -> None:
        """信号置位 → 投递一次 ``server:tool_call_cancel``；终态**完全**取自 ack，Agent 不改写。"""
        signal = _IsSetOnly()
        poll = 0.05

        async def _ack_after_signal(*args: Any, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0.02)
            signal.fire()
            # 留足 ≥3 个轮询周期给 watcher 发现并投递 / allow ≥3 poll cycles for the watcher
            await asyncio.sleep(3 * poll + 0.15)
            return CANCELLED_WIRE

        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock) as mock_emit,
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=_ack_after_signal) as mock_call,
        ):
            result = await async_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        assert mock_call.await_count == 1
        call_args, _ = mock_call.call_args
        req_id = call_args[2]["req_id"]

        # 恰好一次取消广播；载荷 {agent, req_id}，req_id == 本次调用所铸（协议唯一定位键）
        assert mock_emit.await_count == 1, "信号触发应恰好投递一次取消 / exactly one cancel broadcast expected"
        emit_args, emit_kwargs = mock_emit.call_args
        assert emit_args[1] == CANCEL_TOOL_CALL_EVENT
        assert emit_args[3] == SMCP_NAMESPACE
        assert emit_args[2]["agent"] == "test_agent"
        assert emit_args[2]["req_id"] == req_id

        # 终态如实取自 ack，Agent 未改写
        assert result.isError is True
        assert result.meta == {"a2c_cancelled": True, "a2c_cancel_reason": "agent_requested"}
        assert classify_tool_call_outcome(result) is ToolCallOutcome.CANCELLED

    @pytest.mark.asyncio
    async def test_signal_never_set_broadcasts_nothing(self, async_client: AsyncSMCPAgentClient) -> None:
        """```cancel``` 传入但永不置位 → 零广播、结果原样（「未中断时不产生取消标记」）。"""
        signal = _IsSetOnly()

        async def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(3 * 0.05 + 0.15)
            return _wire(meta={"a2c_cancelled": True})  # ack 自带标记（模拟 Computer 侧产出）也应原样透出

        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock) as mock_emit,
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=_ok),
        ):
            result = await async_client.emit_tool_call("comp-1", "fast", {}, 5, cancel=signal)

        assert mock_emit.await_count == 0, "信号未置位不得广播取消 / no cancel broadcast when signal never set"
        assert result.meta == {"a2c_cancelled": True}

    @pytest.mark.asyncio
    async def test_cancel_delivered_never_rewrites_a_clean_ack(self, async_client: AsyncSMCPAgentClient) -> None:
        """(b)+(c) 裁决：取消**确已投递**，但 ack 是干净的成功结果 ⇒ 终态**照实**为 COMPLETED。

        这是「已发出取消信号 **不是** 已取消的充分条件」的最锐利表达 —— 若实现按「本端发过取消」
        改写终态或本地合成 ``a2c_cancelled``，本用例立即变红。
        """
        signal = _IsSetOnly()
        poll = 0.05

        async def _clean_ack_after_signal(*args: Any, **kwargs: Any) -> dict[str, Any]:
            signal.fire()
            await asyncio.sleep(3 * poll + 0.15)  # 让 watcher 完成广播 / let the watcher broadcast
            return _wire()  # 无任何标记的成功结果 / a clean, unmarked success

        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock) as mock_emit,
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=_clean_ack_after_signal),
        ):
            result = await async_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        assert mock_emit.await_count == 1, "取消确应已投递 / the cancel must have been broadcast"
        assert result.meta is None, "Agent MUST NOT 改写终态 / Agent must not rewrite the terminal state"
        assert classify_tool_call_outcome(result) is ToolCallOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_no_cancel_argument_keeps_existing_behaviour(self, async_client: AsyncSMCPAgentClient) -> None:
        """``cancel`` 缺省 = 现有调用零改动：无 broadcast、无 watcher。"""
        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock) as mock_emit,
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, return_value=_wire()) as mock_call,
        ):
            result = await async_client.emit_tool_call("comp-1", "echo", {}, 5)

        assert mock_call.await_count == 1
        assert mock_emit.await_count == 0
        assert classify_tool_call_outcome(result) is ToolCallOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_timeout_after_signal_broadcasts_exactly_once(self, async_client: AsyncSMCPAgentClient) -> None:
        """幂等：信号已触发广播后，自身超时分支**不得**重复广播。"""
        signal = _IsSetOnly()
        poll = 0.05

        async def _timeout_after_signal(*args: Any, **kwargs: Any) -> Any:
            signal.fire()
            await asyncio.sleep(3 * poll + 0.15)  # 确保 watcher 先广播 / let the watcher broadcast first
            raise SioTimeoutError("Timeout")

        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock) as mock_emit,
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=_timeout_after_signal),
        ):
            result = await async_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        assert mock_emit.await_count == 1, "取消广播必须幂等 / cancel broadcast must be idempotent"
        # 自身超时路径如实标 a2c_timeout，**不**冒充 Computer 的 a2c_cancelled
        assert result.meta == {"a2c_timeout": True}
        assert classify_tool_call_outcome(result) is ToolCallOutcome.TIMED_OUT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc_type", [TimeoutError, SioTimeoutError], ids=["builtin", "socketio"])
    async def test_ack_wait_timeout_reaches_timeout_arm(
        self, async_client: AsyncSMCPAgentClient, exc_type: type[BaseException]
    ) -> None:
        """两种超时类型都必须落进超时兜底臂（广播取消 + 标 ``a2c_timeout``）。

        ⚠️ 回归锁：python-socketio 的 ``TimeoutError`` **不是** builtin ``TimeoutError`` 的子类，
        真实链路上抛的是它。只写 ``except TimeoutError`` 会让本臂在生产上**永不可达** ——
        自身超时被 ``except Exception`` 接走，既不广播取消也不标超时（协议 error-handling.md
        §Agent 端超时要求两者都做）。
        """
        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock) as mock_emit,
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=exc_type("Timeout")),
        ):
            result = await async_client.emit_tool_call("comp-1", "slow", {}, 5)

        assert mock_emit.await_count == 1, "超时必须广播取消 / timeout must broadcast a cancel"
        assert result.meta == {"a2c_timeout": True}
        assert classify_tool_call_outcome(result) is ToolCallOutcome.TIMED_OUT

    @pytest.mark.asyncio
    async def test_broadcast_failure_is_swallowed(self, async_client: AsyncSMCPAgentClient) -> None:
        """异步广播失败（如连接已断）必须被吞掉——**绝不**影响原调用的返回值。"""
        signal = _IsSetOnly()
        poll = 0.05

        async def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            signal.fire()
            await asyncio.sleep(3 * poll + 0.15)
            return _wire()

        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock, side_effect=RuntimeError("socket down")) as emit_mock,
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=_ok),
        ):
            result = await async_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        # 正对照：广播分支**确被走到**（否则「失败被吞」可能只是压根没广播过——空转断言）。
        assert emit_mock.await_count == 1, "广播分支确被走到 / the broadcast branch must have been reached"
        assert classify_tool_call_outcome(result) is ToolCallOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_external_cancellation_is_not_swallowed(self, async_client: AsyncSMCPAgentClient) -> None:
        """收尾 ``await watcher`` 时**不得**吞掉外部取消（本仓 #211 同族前科）。

        ``asyncio.wait_for``（3.12 起基于 ``asyncio.timeout``）只在 ``__aexit__`` **看到**
        ``CancelledError`` 时才抛 ``TimeoutError``；若被 await 的协程把取消吞掉并**正常返回**，
        超时信号整体消失、调用方拿到静默成功。

        ⚠️ 夹具必须**模拟吞点**（catch ``CancelledError`` 后正常返回）才能上锁 —— 若下游忠实传播
        ``CancelledError``，它本就会穿过 ``finally`` 继续传播，与 ``restore_swallowed_cancel``
        无关，删掉还原断言**依然成立**（该锁形同虚设）。吞点形态正是 #211 记录的那族。
        """
        signal = _IsSetOnly()

        async def _swallow_cancel(*args: Any, **kwargs: Any) -> dict[str, Any]:
            # 模拟 #211 族吞点：下游把取消吞掉并**正常返回** / simulate the #211-family swallow point.
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return _wire()
            return _wire()  # pragma: no cover - unreachable

        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock),
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=_swallow_cancel),
        ):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    async_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal),
                    timeout=0.3,
                )

    @pytest.mark.asyncio
    async def test_is_set_only_signal_cross_thread_no_attribute_error(
        self, async_client: AsyncSMCPAgentClient
    ) -> None:
        """宿主在**另一线程**置位（真实形态）→ 两条路径都不得 ``AttributeError``。"""
        signal = _IsSetOnly()
        poll = 0.05
        timer = threading.Timer(0.05, signal.fire)

        async def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(3 * poll + 0.15)
            return _wire(is_error=True, meta={"a2c_cancelled": True})

        timer.start()
        try:
            with (
                patch("socketio.AsyncClient.emit", new_callable=AsyncMock) as mock_emit,
                patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=_ok),
            ):
                result = await async_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)
        finally:
            timer.cancel()

        assert mock_emit.await_count == 1
        assert classify_tool_call_outcome(result) is ToolCallOutcome.CANCELLED

    @pytest.mark.asyncio
    async def test_rejects_signal_without_is_set(self, async_client: AsyncSMCPAgentClient) -> None:
        """fail-fast：无 ``is_set()`` 的对象在入口即抛 ``TypeError``（优于后台静默失能）。"""
        with patch("socketio.AsyncClient.call", new_callable=AsyncMock) as mock_call:
            with pytest.raises(TypeError, match="is_set"):
                await async_client.emit_tool_call("comp-1", "echo", {}, 5, cancel=object())  # type: ignore[arg-type]
        mock_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_watcher_task_does_not_survive_return(self, async_client: AsyncSMCPAgentClient) -> None:
        """返回后 watcher 必须收口：再置位信号不得产生迟到广播。"""
        signal = _IsSetOnly()

        async def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return _wire()

        with (
            patch("socketio.AsyncClient.emit", new_callable=AsyncMock) as mock_emit,
            patch("socketio.AsyncClient.call", new_callable=AsyncMock, side_effect=_ok),
        ):
            await async_client.emit_tool_call("comp-1", "fast", {}, 5, cancel=signal)

        signal.fire()
        await asyncio.sleep(3 * 0.05 + 0.15)
        assert mock_emit.await_count == 0, "已返回后不得再有迟到广播 / no late broadcast after return"


# ────────────────────────── 4. 同步路径 / Sync path ──────────────────────────


class TestSyncEmitToolCallCancel:
    def test_signal_delivers_cancel_and_ack_stays_terminal(self, sync_client: SMCPAgentClient) -> None:
        """同步镜像：信号置位 → 投递一次；终态取自 ack。"""
        signal = _IsSetOnly()
        poll = 0.05

        def _ack_after_signal(*args: Any, **kwargs: Any) -> dict[str, Any]:
            time.sleep(0.02)
            signal.fire()
            time.sleep(3 * poll + 0.15)
            return CANCELLED_WIRE

        with (
            patch("socketio.Client.emit") as mock_emit,
            patch("socketio.Client.call", side_effect=_ack_after_signal) as mock_call,
        ):
            result = sync_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        assert mock_call.call_count == 1
        call_args, _ = mock_call.call_args
        req_id = call_args[2]["req_id"]

        assert mock_emit.call_count == 1, "信号触发应恰好投递一次取消 / exactly one cancel broadcast expected"
        emit_args, _ = mock_emit.call_args
        assert emit_args[1] == CANCEL_TOOL_CALL_EVENT
        assert emit_args[3] == SMCP_NAMESPACE
        assert emit_args[2]["agent"] == "test_agent"
        assert emit_args[2]["req_id"] == req_id

        assert result.isError is True
        assert result.meta == {"a2c_cancelled": True, "a2c_cancel_reason": "agent_requested"}
        assert classify_tool_call_outcome(result) is ToolCallOutcome.CANCELLED

    def test_signal_set_from_another_thread(self, sync_client: SMCPAgentClient) -> None:
        """真实形态：宿主持信号在**另一线程**置位（协议硬约束只给 is_set()）。"""
        signal = _IsSetOnly()
        poll = 0.05
        timer = threading.Timer(0.05, signal.fire)

        def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            time.sleep(3 * poll + 0.15)
            return _wire(is_error=True, meta={"a2c_cancelled": True})

        timer.start()
        try:
            with (
                patch("socketio.Client.emit") as mock_emit,
                patch("socketio.Client.call", side_effect=_ok),
            ):
                result = sync_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)
        finally:
            timer.cancel()

        assert mock_emit.call_count == 1
        assert classify_tool_call_outcome(result) is ToolCallOutcome.CANCELLED

    def test_cancel_delivered_never_rewrites_a_clean_ack(self, sync_client: SMCPAgentClient) -> None:
        """同步镜像：取消已投递 + ack 干净成功 ⇒ 终态仍为 COMPLETED，Agent 不改写。"""
        signal = _IsSetOnly()
        poll = 0.05

        def _clean_ack_after_signal(*args: Any, **kwargs: Any) -> dict[str, Any]:
            signal.fire()
            time.sleep(3 * poll + 0.15)
            return _wire()

        with (
            patch("socketio.Client.emit") as mock_emit,
            patch("socketio.Client.call", side_effect=_clean_ack_after_signal),
        ):
            result = sync_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        assert mock_emit.call_count == 1, "取消确应已投递 / the cancel must have been broadcast"
        assert result.meta is None, "Agent MUST NOT 改写终态 / Agent must not rewrite the terminal state"
        assert classify_tool_call_outcome(result) is ToolCallOutcome.COMPLETED

    def test_signal_never_set_broadcasts_nothing(self, sync_client: SMCPAgentClient) -> None:
        signal = _IsSetOnly()

        def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            time.sleep(3 * 0.05 + 0.15)
            return _wire()

        with (
            patch("socketio.Client.emit") as mock_emit,
            patch("socketio.Client.call", side_effect=_ok),
        ):
            result = sync_client.emit_tool_call("comp-1", "fast", {}, 5, cancel=signal)

        assert mock_emit.call_count == 0
        assert classify_tool_call_outcome(result) is ToolCallOutcome.COMPLETED

    def test_no_cancel_argument_keeps_existing_behaviour(self, sync_client: SMCPAgentClient) -> None:
        def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            assert _cancel_threads() == set(), "cancel=None 时不得新建 watcher 线程 / no watcher thread when cancel is None"
            return _wire()

        with (
            patch("socketio.Client.emit") as mock_emit,
            patch("socketio.Client.call", side_effect=_ok),
        ):
            result = sync_client.emit_tool_call("comp-1", "echo", {}, 5)

        assert mock_emit.call_count == 0
        assert classify_tool_call_outcome(result) is ToolCallOutcome.COMPLETED

    def test_timeout_after_signal_broadcasts_exactly_once(self, sync_client: SMCPAgentClient) -> None:
        """幂等：watcher 已广播后，自身超时分支不得重复广播；且如实标 ``a2c_timeout``。"""
        signal = _IsSetOnly()
        poll = 0.05

        def _timeout_after_signal(*args: Any, **kwargs: Any) -> Any:
            signal.fire()
            time.sleep(3 * poll + 0.15)
            raise SioTimeoutError("Timeout")

        with (
            patch("socketio.Client.emit") as mock_emit,
            patch("socketio.Client.call", side_effect=_timeout_after_signal),
        ):
            result = sync_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        assert mock_emit.call_count == 1, "取消广播必须幂等 / cancel broadcast must be idempotent"
        assert result.meta == {"a2c_timeout": True}
        assert classify_tool_call_outcome(result) is ToolCallOutcome.TIMED_OUT

    @pytest.mark.parametrize("exc_type", [TimeoutError, SioTimeoutError], ids=["builtin", "socketio"])
    def test_ack_wait_timeout_reaches_timeout_arm(
        self, sync_client: SMCPAgentClient, exc_type: type[BaseException]
    ) -> None:
        """同步镜像：两种超时类型都必须落进超时兜底臂（见 async 同名用例的说明）。"""
        with (
            patch("socketio.Client.emit") as mock_emit,
            patch("socketio.Client.call", side_effect=exc_type("Timeout")),
        ):
            result = sync_client.emit_tool_call("comp-1", "slow", {}, 5)

        assert mock_emit.call_count == 1, "超时必须广播取消 / timeout must broadcast a cancel"
        assert result.meta == {"a2c_timeout": True}
        assert classify_tool_call_outcome(result) is ToolCallOutcome.TIMED_OUT

    def test_rejects_signal_without_is_set(self, sync_client: SMCPAgentClient) -> None:
        with patch("socketio.Client.call") as mock_call:
            with pytest.raises(TypeError, match="is_set"):
                sync_client.emit_tool_call("comp-1", "echo", {}, 5, cancel=object())  # type: ignore[arg-type]
        mock_call.assert_not_called()

    @pytest.mark.parametrize("outcome", ["return", "timeout", "error"])
    def test_watcher_thread_converges(self, sync_client: SMCPAgentClient, outcome: str) -> None:
        """线程收口：正常返回 / 超时 / 任意异常三条路径后均无线程泄漏。"""
        signal = _IsSetOnly()
        before = _cancel_threads()

        def _side_effect(*args: Any, **kwargs: Any) -> Any:
            if outcome == "timeout":
                raise SioTimeoutError("Timeout")
            if outcome == "error":
                raise RuntimeError("boom")
            return _wire()

        with (
            patch("socketio.Client.emit"),
            patch("socketio.Client.call", side_effect=_side_effect),
        ):
            result = sync_client.emit_tool_call("comp-1", "echo", {}, 5, cancel=signal)

        assert _cancel_threads() == before, "watcher 线程未收口 / watcher thread leaked"
        # 无中断 ⇒ Agent **不**改写终态：超时路径如实标 a2c_timeout，任何路径都不得出现取消标记。
        assert "a2c_cancelled" not in (result.meta or {})

    def test_watcher_thread_converges_when_signal_fired(self, sync_client: SMCPAgentClient) -> None:
        """信号真触发（线程跑过一轮以上）后同样收口。"""
        signal = _IsSetOnly()
        before = _cancel_threads()

        def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            time.sleep(3 * 0.05 + 0.15)
            return _wire()

        with (
            patch("socketio.Client.emit"),
            patch("socketio.Client.call", side_effect=_ok),
        ):
            sync_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        signal.fire()
        time.sleep(3 * 0.05 + 0.1)
        assert _cancel_threads() == before, "watcher 线程未收口 / watcher thread leaked"

    def test_late_signal_after_return_broadcasts_nothing(self, sync_client: SMCPAgentClient) -> None:
        """返回后置位信号 → 不得产生迟到广播（``finally`` 内先「关门」）。"""
        signal = _IsSetOnly()

        with (
            patch("socketio.Client.emit") as mock_emit,
            patch("socketio.Client.call", return_value=_wire()),
        ):
            sync_client.emit_tool_call("comp-1", "fast", {}, 5, cancel=signal)

        signal.fire()
        time.sleep(3 * 0.05 + 0.15)
        assert mock_emit.call_count == 0, "已返回后不得再有迟到广播 / no late broadcast after return"


class TestWatcherSealsGateOnStop:
    """收口必须**先关门**：``stop()`` 抢占广播权后，任何迟到置位都不可能再广播（承重不变量）。"""

    def test_sync_stop_seals_gate(self) -> None:
        gate = CancelSendGate()
        signal = _IsSetOnly()
        broadcasts: list[str] = []
        watcher = SyncCancelWatcher(signal, gate, "req-1", broadcasts.append, interval=0.01)

        watcher.start()
        watcher.stop()

        signal.fire()  # 迟到置位 / late flip
        time.sleep(0.1)

        assert broadcasts == [], "stop() 后不得再有广播 / no broadcast after stop()"
        assert gate.try_claim() is False, "stop() 必须已关门 / stop() must have sealed the gate"

    @pytest.mark.asyncio
    async def test_async_stop_seals_gate(self) -> None:
        gate = CancelSendGate()
        signal = _IsSetOnly()
        broadcasts: list[str] = []

        async def _record(req_id: str) -> None:
            broadcasts.append(req_id)

        watcher = AsyncCancelWatcher(signal, gate, "req-1", _record, interval=0.01)
        watcher.start()
        await watcher.stop()

        signal.fire()
        await asyncio.sleep(0.1)

        assert broadcasts == [], "stop() 后不得再有广播 / no broadcast after stop()"
        assert gate.try_claim() is False, "stop() 必须已关门 / stop() must have sealed the gate"

    def test_gate_claims_exactly_once(self) -> None:
        gate = CancelSendGate()
        assert gate.try_claim() is True
        assert gate.try_claim() is False
        assert gate.try_claim() is False

    def test_gate_claim_is_thread_safe(self) -> None:
        """并发抢占下恰好一个线程获胜 / under concurrency exactly one claimer wins."""
        gate = CancelSendGate()
        winners: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def _race() -> None:
            barrier.wait()
            got = gate.try_claim()
            with lock:
                winners.append(got)

        threads = [threading.Thread(target=_race) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert winners.count(True) == 1, f"闸门必须恰好放行一次 / exactly one claim expected, got {winners.count(True)}"


class TestWatcherThreadNeverLeaksOnEmitFailure:
    def test_broadcast_failure_is_swallowed(self, sync_client: SMCPAgentClient) -> None:
        """取消广播失败（如连接已断）必须被吞掉并记日志——**绝不**影响原调用。"""
        signal = _IsSetOnly()

        def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
            signal.fire()
            time.sleep(3 * 0.05 + 0.15)
            return _wire()

        with (
            patch("socketio.Client.emit", side_effect=RuntimeError("socket down")) as emit_mock,
            patch("socketio.Client.call", side_effect=_ok),
        ):
            result = sync_client.emit_tool_call("comp-1", "slow", {}, 5, cancel=signal)

        # 正对照：广播分支**确被走到**（否则「失败被吞」可能只是压根没广播过——空转断言）。
        assert emit_mock.call_count == 1, "广播分支确被走到 / the broadcast branch must have been reached"
        assert classify_tool_call_outcome(result) is ToolCallOutcome.COMPLETED
        assert _cancel_threads() == set()
