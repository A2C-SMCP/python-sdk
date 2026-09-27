# -*- coding: utf-8 -*-
"""
v0.5.0 整 milestone 跨节点审查 — Agent 侧回归锁（async + sync 双实现）。

- 🔴3：断链后工具调用等满超时 ⇒ 超时臂的取消广播 ``emit`` 抛 ``BadNamespaceError``；该异常在 ``except``
  子句**内**抛出、不被同级 ``except Exception`` 接住 ⇒ 逃出 ``emit_tool_call``。夹具用**真实**未连接客户端
  （不桩 ``emit``）复现真实失败形态。
- 🔴4：sync ``join_office`` 入口段两次取状态锁 ⇒ 断连钩子落在两次之间时已清空的意图被复活。
- Y5：``namespace=None`` 被上游落到 ``/`` ⇒ 文档标准写法 ``join_office(office, name)`` 抛 ``BadNamespaceError``。
- Y4：``server:leave_office`` 有 ack（协议 events.md:173）⇒ 被拒必须可感，且本地意图照样清空。
- Y6：sync office 操作锁先到先得。
- Y7：超时元组从 ``a2c_smcp.agent`` 公开导出。
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from socketio import AsyncClient, Client
from socketio.exceptions import TimeoutError as SioTimeoutError

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.base import _FifoLock
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, LEAVE_OFFICE_EVENT, SMCP_NAMESPACE

_OFFICE = "officeA"
_NAME = "agent-1"


def _auth() -> DefaultAgentAuthProvider:
    return DefaultAgentAuthProvider(agent_id=_NAME, office_id=_OFFICE)


# ── 🔴3 超时臂的取消广播 best-effort ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_timeout_after_link_loss_still_returns_timeout_result() -> None:
    client = AsyncSMCPAgentClient(auth_provider=_auth())
    client.call = AsyncMock(side_effect=SioTimeoutError("timeout"))  # type: ignore[method-assign]
    assert SMCP_NAMESPACE not in client.namespaces, "前置：真实未连接 ⇒ 取消广播的 emit 会抛 BadNamespaceError"

    res = await client.emit_tool_call("comp-1", "echo", {}, timeout=1)  # 不得抛

    assert res.isError
    assert res.meta == {"a2c_timeout": True}, "超时标记不得随广播失败丢失"


def test_sync_timeout_after_link_loss_still_returns_timeout_result() -> None:
    client = SMCPAgentClient(auth_provider=_auth())
    client.call = MagicMock(side_effect=SioTimeoutError("timeout"))  # type: ignore[method-assign]
    assert SMCP_NAMESPACE not in client.namespaces

    res = client.emit_tool_call("comp-1", "echo", {}, timeout=1)

    assert res.isError
    assert res.meta == {"a2c_timeout": True}


# ── 🔴4 sync join 入口段单次取锁 ────────────────────────────────────────────────


class _InterleavingLock:
    """包一把真锁：**第一次**释放后立即（在调用线程里）执行注入回调，模拟另一线程恰好抢进这条窗口。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.inject: Any = None

    def __enter__(self) -> Any:
        return self._inner.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        result = self._inner.__exit__(*exc)
        cb, self.inject = self.inject, None
        if cb is not None:
            cb()
        return result


def test_sync_join_entry_is_one_critical_section() -> None:
    """断连钩子（手工断开 ⇒ drop 意图）落在入口段内部时，意图**不得**被复活。

    旧实现：``bump``（第一次取锁）→【钩子：清意图】→ 写 desired（第二次取锁）⇒ 已清空的意图被写回，
    而 generation 已前进 ⇒ join 静默不发包 ⇒ 状态是「有意图但没人会去兑现」。
    """
    client = SMCPAgentClient(auth_provider=_auth())
    sent: list[str] = []
    client.call = lambda event, *a, **k: sent.append(event)  # type: ignore[assignment,method-assign]
    lock = _InterleavingLock(client._office_state_lock)
    client._office_state_lock = lock  # type: ignore[assignment]
    lock.inject = lambda: client._begin_office_session(drop_desired=True)

    client.join_office(_OFFICE, _NAME, namespace=SMCP_NAMESPACE)

    # 注入点 = 第一次释放状态锁：修复后它就是「整个入口段」之后 ⇒ 钩子的 drop 是**更新的声明** ⇒ 意图为空、
    # 且 generation 已被钩子推进 ⇒ 不发包（与异步侧同态）。
    assert client._desired_office is None, f"已被手工断开清空的意图不得被入口段复活：{client._desired_office!r}"
    assert sent == [], f"被会话边界抢占 ⇒ 不发包，实得 {sent!r}"
    assert JOIN_OFFICE_EVENT not in sent


# ── Y5 namespace 缺省 = 实例命名空间 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_join_without_namespace_uses_instance_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_call(self: Any, event: str, data: Any = None, namespace: Any = None, timeout: Any = 60) -> Any:
        seen.append(namespace)
        return None  # 空 ack = 成功

    monkeypatch.setattr(AsyncClient, "call", fake_call)
    client = AsyncSMCPAgentClient(auth_provider=_auth())

    await client.join_office(_OFFICE, _NAME)  # 文档标准写法（不传 namespace）

    assert seen == [SMCP_NAMESPACE], f"namespace=None 不得落到根命名空间 '/'，实得 {seen!r}"
    assert client._confirmed_office == (_OFFICE, _NAME)


def test_sync_join_without_namespace_uses_instance_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_call(self: Any, event: str, data: Any = None, namespace: Any = None, timeout: Any = 60) -> Any:
        seen.append(namespace)
        return None

    monkeypatch.setattr(Client, "call", fake_call)
    client = SMCPAgentClient(auth_provider=_auth())

    client.join_office(_OFFICE, _NAME)

    assert seen == [SMCP_NAMESPACE]
    assert client._confirmed_office == (_OFFICE, _NAME)


@pytest.mark.asyncio
async def test_async_emit_without_namespace_uses_instance_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_emit(self: Any, event: str, data: Any = None, namespace: Any = None, callback: Any = None) -> None:
        seen.append(namespace)

    monkeypatch.setattr(AsyncClient, "emit", fake_emit)
    client = AsyncSMCPAgentClient(auth_provider=_auth())
    await client.emit(LEAVE_OFFICE_EVENT, {"office_id": _OFFICE})
    assert seen == [SMCP_NAMESPACE]


# ── Y4 leave 等 ack ────────────────────────────────────────────────────────────


_LEAVE_REJECTED = {"code": 500, "message": "Internal error"}


@pytest.mark.asyncio
async def test_async_leave_rejection_raises_and_still_clears_local_state() -> None:
    client = AsyncSMCPAgentClient(auth_provider=_auth())
    client._desired_office = client._confirmed_office = (_OFFICE, _NAME)
    calls: list[tuple[str, Any]] = []

    async def fake_call(event: str, data: Any = None, namespace: Any = None, timeout: Any = 60) -> Any:
        calls.append((event, timeout))
        return dict(_LEAVE_REJECTED)

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        await client.leave_office(_OFFICE)

    assert ei.value.code == 500
    assert [event for event, _ in calls] == [LEAVE_OFFICE_EVENT], "leave 必须走 call（等 ack），不再 fire-and-forget"
    assert calls[0][1] == 10, "leave 的 ack 等待须有界（OFFICE_JOIN_TIMEOUT）"
    assert client._desired_office is None and client._confirmed_office is None, "退房被拒也不回滚本地清账（#223）"


@pytest.mark.asyncio
async def test_async_leave_empty_ack_is_success() -> None:
    client = AsyncSMCPAgentClient(auth_provider=_auth())
    client._desired_office = client._confirmed_office = (_OFFICE, _NAME)
    client.call = AsyncMock(return_value=None)  # type: ignore[method-assign]
    await client.leave_office(_OFFICE)
    assert client._confirmed_office is None


def test_sync_leave_rejection_raises_and_still_clears_local_state() -> None:
    client = SMCPAgentClient(auth_provider=_auth())
    client._desired_office = client._confirmed_office = (_OFFICE, _NAME)
    client.call = MagicMock(return_value=dict(_LEAVE_REJECTED))  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        client.leave_office(_OFFICE)

    assert ei.value.code == 500
    assert client.call.call_args.args[0] == LEAVE_OFFICE_EVENT
    assert client._desired_office is None and client._confirmed_office is None


# ── Y6 sync office 操作锁先到先得 ───────────────────────────────────────────────


def _wait_queued(lock: _FifoLock, n: int) -> None:
    deadline = time.monotonic() + 2
    while lock._next_ticket < n:
        assert time.monotonic() < deadline
        time.sleep(0.001)


def test_fifo_lock_serves_waiters_in_arrival_order() -> None:
    for _ in range(20):
        lock = _FifoLock()
        order: list[str] = []
        lock.acquire()

        def worker(tag: str, lock: _FifoLock = lock, order: list[str] = order) -> None:
            with lock:
                order.append(tag)

        threads = []
        for i, tag in enumerate("ABCD"):
            t = threading.Thread(target=worker, args=(tag,))
            t.start()
            _wait_queued(lock, i + 2)  # 持有者占 1 号票，第 i 个等待者拿到 i+1 号
            threads.append(t)
        lock.release()
        for t in threads:
            t.join(2)
        assert order == list("ABCD")


def test_fifo_lock_abandoned_ticket_does_not_block_later_waiters() -> None:
    """超时放弃的等待者票号必须被跳过——否则其后排队者永远等不到。"""
    lock = _FifoLock()
    lock.acquire()
    assert lock.acquire(timeout=0.01) is False  # 放弃 1 号票
    got = threading.Event()

    def worker() -> None:
        with lock:
            got.set()

    t = threading.Thread(target=worker)
    t.start()
    _wait_queued(lock, 3)
    lock.release()
    assert got.wait(2), "放弃的票号未被跳过 ⇒ 后续等待者死锁"
    t.join(2)
    assert not lock.locked()


def test_sync_client_office_op_lock_is_fifo() -> None:
    assert isinstance(SMCPAgentClient(auth_provider=_auth())._office_op_lock, _FifoLock)


# ── Y7 公开导出 ─────────────────────────────────────────────────────────────────


def test_timeout_tuples_are_exported() -> None:
    import a2c_smcp.agent as agent_pkg

    assert "OFFICE_ACK_TIMEOUT_ERRORS" in agent_pkg.__all__
    assert "TOOL_CALL_TIMEOUT_ERRORS" in agent_pkg.__all__
    assert SioTimeoutError in agent_pkg.OFFICE_ACK_TIMEOUT_ERRORS
