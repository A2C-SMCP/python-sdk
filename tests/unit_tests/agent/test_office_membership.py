# -*- coding: utf-8 -*-
"""
#203 Office 成员关系状态机（Agent 侧单元测试，async + sync 双实现）。

Agent 侧的自动回房在 rust-sdk **没有对应实现可对拍**（rust 只修了 Computer 侧，见 rust-sdk@05dfdc7），
证据负担全在 Python 测试上，故与 Computer 侧镜像覆盖：断连 reason 判据表、`__disconnect_final`、
回房调度与 payload、generation 守卫、被拒/异常清空、显式 join/leave 的意图记录与清空。

Unit coverage for the Agent-side (#203) membership state machine — mirrored across the async and
sync implementations, since rust-sdk has no counterpart to compare against for this side.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from socketio.exceptions import BadNamespaceError

from a2c_smcp.agent import client as client_mod
from a2c_smcp.agent import sync_client as sync_client_mod
from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, SMCP_NAMESPACE
from a2c_smcp.utils import office as office_mod
from a2c_smcp.utils.office import OFFICE_JOIN_TIMEOUT

_TRANSPORT_ERROR = "transport error"
_CLIENT_DISCONNECT = "client disconnect"
_SERVER_DISCONNECT = "server disconnect"
_OFFICE = "officeA"
_AGENT_NAME = "agent-1"


def _auth() -> DefaultAgentAuthProvider:
    return DefaultAgentAuthProvider(agent_id=_AGENT_NAME, office_id=_OFFICE)


def _make_async_client(**kwargs: Any) -> AsyncSMCPAgentClient:
    return AsyncSMCPAgentClient(auth_provider=_auth(), **kwargs)


def _make_sync_client(**kwargs: Any) -> SMCPAgentClient:
    return SMCPAgentClient(auth_provider=_auth(), **kwargs)


def _register_namespace(client: Any, sid: str = "fake-sid") -> None:
    """模拟"namespace 在册"（回房守卫判据，非 ``connected``）/ simulate a registered namespace."""
    client.namespaces[SMCP_NAMESPACE] = sid


# ── async：断连 reason 判据表 / async: disconnect-reason table ────────────────


@pytest.mark.asyncio
async def test_async_transport_error_retains_desired_office() -> None:
    """传输中断且会自动重连 → 保留入房意图（等重连后重放）。"""
    client = _make_async_client(reconnection=True)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    client._on_namespace_disconnect(_TRANSPORT_ERROR)

    assert client._desired_office == (_OFFICE, _AGENT_NAME)


@pytest.mark.asyncio
async def test_async_transport_error_without_reconnection_clears() -> None:
    """不会重连 → 没有恢复路径，清空意图。"""
    client = _make_async_client(reconnection=False)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    client._on_namespace_disconnect(_TRANSPORT_ERROR)

    assert client._desired_office is None


@pytest.mark.parametrize("reason", [_CLIENT_DISCONNECT, _SERVER_DISCONNECT])
@pytest.mark.asyncio
async def test_async_manual_and_server_disconnect_clear(reason: str) -> None:
    """手工断开 / 服务端踢出 → 清空意图。"""
    client = _make_async_client(reconnection=True)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    client._on_namespace_disconnect(reason)

    assert client._desired_office is None


@pytest.mark.asyncio
async def test_async_disconnect_final_clears() -> None:
    """重连彻底放弃 → 清空意图。"""
    client = _make_async_client(reconnection=True)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    client._on_namespace_disconnect_final()

    assert client._desired_office is None


# ── async：回房调度与载荷 / async: replay scheduling & payload ────────────────


@pytest.mark.asyncio
async def test_async_connect_without_desired_schedules_nothing() -> None:
    """首连（无意图）不得调度回房。"""
    client = _make_async_client()
    assert client._desired_office is None

    client._on_namespace_connect()

    assert client._office_rejoin_task is None


@pytest.mark.asyncio
async def test_async_connect_with_desired_schedules_replay() -> None:
    """重连（意图仍在）必须重放 server:join_office（有界超时）。"""
    client = _make_async_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    calls: list[dict] = []

    async def fake_call(event: str, data: Any = None, namespace: str | None = None, **kwargs: Any):
        calls.append({"event": event, "data": dict(data or {}), "namespace": namespace, "timeout": kwargs.get("timeout")})
        return None

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    task = client._office_rejoin_task
    assert task is not None
    await task

    assert len(calls) == 1
    assert calls[0]["event"] == JOIN_OFFICE_EVENT
    assert calls[0]["data"] == {"office_id": _OFFICE, "role": "agent", "name": _AGENT_NAME}
    assert calls[0]["namespace"] == SMCP_NAMESPACE
    assert calls[0]["timeout"] == OFFICE_JOIN_TIMEOUT, "回房必须带 OFFICE_JOIN_TIMEOUT 有界超时"
    assert client._desired_office == (_OFFICE, _AGENT_NAME)


@pytest.mark.asyncio
async def test_async_stale_generation_replay_dropped() -> None:
    """generation 过期 → 不发包、不碰状态。"""
    client = _make_async_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 5
    called = False

    async def fake_call(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        return None

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 4)

    assert called is False
    assert client._desired_office == (_OFFICE, _AGENT_NAME)


@pytest.mark.asyncio
async def test_async_rejected_replay_clears_desired_office() -> None:
    """回房被拒（如"Agent already in room"）→ 清空意图，状态不得撒谎。

    预算是**退化值**（``0``）：本用例钉的是「预算耗尽/关闭」后的终态，与 #203/#218 的既有效应一致
    ——重试分支本身由本文件末尾的 `#212` 小节覆盖。/ The retry budget is degenerate here on purpose.
    """
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.0
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 7

    async def fake_call(*args: Any, **kwargs: Any):
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 7)

    assert client._desired_office is None


@pytest.mark.asyncio
async def test_async_replay_exception_clears_desired_office() -> None:
    """回房抛异常（断链时的 BadNamespaceError 等）→ 同样清空。"""
    client = _make_async_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 3

    async def fake_call(*args: Any, **kwargs: Any):
        raise RuntimeError("/smcp is not a connected namespace.")

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 3)

    assert client._desired_office is None


@pytest.mark.asyncio
async def test_async_stale_replay_failure_does_not_clobber_new_intent() -> None:
    """回房在途时用户显式换房 → 陈旧回房的失败不得清掉新意图。"""
    client = _make_async_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def fake_call(*args: Any, **kwargs: Any):
        in_flight.set()
        await release.wait()
        raise RuntimeError("boom")

    client.call = fake_call  # type: ignore[method-assign]

    task = asyncio.create_task(client._arejoin_office((_OFFICE, _AGENT_NAME), 1))
    await asyncio.wait_for(in_flight.wait(), timeout=5)

    client._office_generation = 2
    client._desired_office = ("officeB", _AGENT_NAME)

    release.set()
    await task

    assert client._desired_office == ("officeB", _AGENT_NAME)


# ── async：显式 join / leave 的意图记录 / explicit join & leave ───────────────


@pytest.mark.asyncio
async def test_async_join_records_intent_and_leave_clears_it() -> None:
    """显式 join 记录意图（供重连重放）+ 落账已确认成员关系；leave 清空两半。

    #218：join 已由无 ack 的 emit 改为**等 ACK 的 call** ⇒ 夹具必须打桩 ``call``（打桩 ``emit``
    会让真 ``call`` 吃满 OFFICE_JOIN_TIMEOUT 后超时）。
    """
    client = _make_async_client()
    calls: list[tuple[str, Any]] = []

    async def fake_call(event: str, data: Any = None, namespace: str | None = None, **kwargs: Any) -> Any:
        calls.append((event, data))
        return None  # 空 ack = 成功

    async def fake_emit(event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        calls.append((event, data))

    client.call = fake_call  # type: ignore[method-assign]
    client.emit = fake_emit  # type: ignore[method-assign]

    await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
    assert calls[-1][0] == JOIN_OFFICE_EVENT
    assert calls[-1][1]["office_id"] == _OFFICE
    assert client._desired_office == (_OFFICE, _AGENT_NAME)
    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)

    await client.leave_office(_OFFICE, namespace=SMCP_NAMESPACE)
    assert client._desired_office is None
    assert client._confirmed_office is None, "退房后已确认成员关系随之作废"


# ── sync：镜像覆盖 / sync mirror ─────────────────────────────────────────────


def _sync_wait_thread(client: SMCPAgentClient, timeout: float = 5.0) -> None:
    """等回房线程收尾（同步侧无 await 可锚定）/ join the replay thread (no awaitable to anchor on).

    **先断言线程引用存在**：若实现意外把引用清空（如失败路径回退到 ``_drop_desired_office``），
    本助手会静默不等待 ⇒ 后续断言在「线程尚未跑完」的状态下假绿。
    """
    thread = client._office_rejoin_thread
    assert thread is not None, "回房线程未被记录：等待失去锚点（可能静默假绿）"
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "回房线程未在期限内收尾"


def test_sync_transport_error_retains_desired_office() -> None:
    client = _make_sync_client(reconnection=True)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    client._on_namespace_disconnect(_TRANSPORT_ERROR)

    assert client._desired_office == (_OFFICE, _AGENT_NAME)


def test_sync_manual_disconnect_clears_desired_office() -> None:
    client = _make_sync_client(reconnection=True)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    client._on_namespace_disconnect(_CLIENT_DISCONNECT)

    assert client._desired_office is None


def test_sync_disconnect_final_clears_desired_office() -> None:
    client = _make_sync_client(reconnection=True)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    client._on_namespace_disconnect_final()

    assert client._desired_office is None


def test_sync_connect_with_desired_schedules_replay_in_thread() -> None:
    """重连（意图仍在）→ 起 daemon 线程重放入房（有界超时）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    calls: list[dict] = []

    def fake_call(event: str, data: Any = None, namespace: str | None = None, timeout: int = 60) -> Any:
        calls.append({"event": event, "data": dict(data or {}), "namespace": namespace, "timeout": timeout})
        return None

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    _sync_wait_thread(client)

    assert len(calls) == 1
    assert calls[0]["event"] == JOIN_OFFICE_EVENT
    assert calls[0]["data"] == {"office_id": _OFFICE, "role": "agent", "name": _AGENT_NAME}
    assert calls[0]["namespace"] == SMCP_NAMESPACE
    assert calls[0]["timeout"] == OFFICE_JOIN_TIMEOUT, "回房必须带 OFFICE_JOIN_TIMEOUT 有界超时"
    assert client._desired_office == (_OFFICE, _AGENT_NAME)


def test_sync_connect_without_desired_schedules_nothing() -> None:
    client = _make_sync_client()
    _register_namespace(client)

    client._on_namespace_connect()

    assert client._office_rejoin_thread is None


def test_sync_stale_generation_replay_dropped() -> None:
    """generation 过期 → 线程不发包、不碰状态。"""
    client = _make_sync_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 5
    called: list[int] = []

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        called.append(1)
        return None

    client.call = fake_call  # type: ignore[method-assign]

    client._rejoin_office((_OFFICE, _AGENT_NAME), 4)

    assert called == []
    assert client._desired_office == (_OFFICE, _AGENT_NAME)


def test_sync_rejected_replay_clears_desired_office() -> None:
    """回房被拒 → 清空意图（预算是**退化值** ``0``：本用例钉的是退避关闭/耗尽后的终态）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.0
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 7

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]

    client._rejoin_office((_OFFICE, _AGENT_NAME), 7)

    assert client._desired_office is None


def test_sync_budget_zero_degrades_to_single_attempt() -> None:
    """``office_rejoin_retry_budget <= 0`` ⇒ 退化回单次尝试（配置逃生舱，镜像 #203 的旧口径）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.0
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    _sync_wait_thread(client)
    time.sleep(0.05)

    assert attempts == [1], "预算为 0 ⇒ 不得重试"


def test_sync_join_records_intent_and_leave_clears_it() -> None:
    """显式 join/leave 记录/清空意图 + 已确认成员关系（同步侧，镜像异步）。"""
    client = _make_sync_client()
    calls: list[tuple[str, Any]] = []

    def fake_call(event: str, data: Any = None, namespace: str | None = None, timeout: int = 60) -> Any:
        calls.append((event, data))
        return None  # 空 ack = 成功

    def fake_emit(event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        calls.append((event, data))

    client.call = fake_call  # type: ignore[method-assign]
    client.emit = fake_emit  # type: ignore[method-assign]

    client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
    assert calls[-1][0] == JOIN_OFFICE_EVENT
    assert calls[-1][1]["office_id"] == _OFFICE
    assert client._desired_office == (_OFFICE, _AGENT_NAME)
    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)

    client.leave_office(_OFFICE, namespace=SMCP_NAMESPACE)
    assert client._desired_office is None
    assert client._confirmed_office is None, "退房后已确认成员关系随之作废"


# ── #218 C1：显式 join 等 ACK（async）/ explicit join waits for the ack ────────
#
# 效应表矩阵本身在 tests/unit_tests/utils/test_office.py 直接覆盖；本节覆盖**接线**：
# 异常形态（可感 + `.code` 可分流）、状态归属（desired / confirmed 的去留）、
# 会话纪元与抢占守卫、以及锁语义（取锁后被抢占 ⇒ 不发包）。


def _rejecting_async_call(payload: dict[str, Any]) -> Any:
    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        return payload

    return fake_call


@pytest.mark.asyncio
async def test_async_join_success_records_confirmed_office() -> None:
    """成功（空 ack）⇒ 意图与**已确认**成员关系同时落账。"""
    client = _make_async_client()
    client.call = _rejecting_async_call(None)  # type: ignore[method-assign]

    await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert client._desired_office == (_OFFICE, _AGENT_NAME)
    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)


@pytest.mark.asyncio
async def test_async_rejected_switch_restores_confirmed_office() -> None:
    """S1：房 A 已在房 → 显式 `join(B)` 被拒 ⇒ 回退到**已确认**的房 A（不清空）。

    若一律清空，重连后将**静默掉出**仍然有效的房 A —— 这就是 `_confirmed_office` 的存在理由。
    """
    client = _make_async_client()
    client._confirmed_office = (_OFFICE, _AGENT_NAME)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client.call = _rejecting_async_call({"code": 4106, "message": "Agent already in another room"})  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        await client.join_office("officeB", _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert ei.value.code == 4106
    assert client._desired_office == (_OFFICE, _AGENT_NAME)
    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)


@pytest.mark.asyncio
async def test_async_rejected_first_join_without_confirmed_clears() -> None:
    """S9：从未成功入房（`confirmed is None`）⇒ 首次被拒即清空（首撞 `4101` = 永久冲突）。"""
    client = _make_async_client()
    client.call = _rejecting_async_call({"code": 4101, "message": "Room already has an agent"})  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert ei.value.code == 4101
    assert client._desired_office is None
    assert client._confirmed_office is None


@pytest.mark.asyncio
async def test_async_unknown_code_still_raises() -> None:
    """未知码（未来协议新增）**仍**可感：不得因码表白名单而静默放过。"""
    client = _make_async_client()
    client.call = _rejecting_async_call({"code": 4199, "message": "future code"})  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert ei.value.code == 4199


@pytest.mark.asyncio
async def test_async_unparsable_code_is_indeterminate_but_raises() -> None:
    """码不可解析 ⇒ 「未获裁决」：`.code == -1`（省略 code 键）且**不** TypeError。"""
    client = _make_async_client()
    client._confirmed_office = (_OFFICE, _AGENT_NAME)
    client.call = _rejecting_async_call({"code": "not-a-number", "message": "weird"})  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert ei.value.code == -1
    assert ei.value.error_message == "weird"
    assert client._confirmed_office is None, "未获裁决 ⇒ 双清空（请求已发出，旧确认不再可信）"


@pytest.mark.asyncio
async def test_async_403_appends_identity_hint_and_rolls_back_both_halves() -> None:
    """S3：同连接改名被拒（403）⇒ 名字半随房号半一起回滚 + 异常带身份提示。"""
    client = _make_async_client()
    client._confirmed_office = (_OFFICE, "alice")
    client._desired_office = ("officeB", "bob")
    client.call = _rejecting_async_call(  # type: ignore[method-assign]
        {"code": 403, "message": "Role or name mismatch with existing session"}
    )

    with pytest.raises(SMCPProtocolError) as ei:
        await client.join_office("officeB", "bob", namespace=SMCP_NAMESPACE)

    assert ei.value.code == 403
    assert "alice" in ei.value.error_message
    assert "重新建立连接" in ei.value.error_message
    assert client._desired_office == (_OFFICE, "alice"), "新名不得留存（否则重连前持续静默分叉）"
    assert client._confirmed_office == (_OFFICE, "alice")


@pytest.mark.asyncio
async def test_async_rejection_logs_via_shared_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式路径的拒绝同样经**共享产出者**（同态口径 ②），否则 #219 的变异会留空档。"""
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)
    client = _make_async_client()
    client.call = _rejecting_async_call({"code": 4101, "message": "Room already has an agent"})  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError):
        await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    fake_logger.error.assert_called_once()
    text = fake_logger.error.call_args[0][0]
    assert "4101" in text and _OFFICE in text


@pytest.mark.asyncio
async def test_async_transport_failure_propagates_and_clears_both() -> None:
    """S2：传输层失败（无裁决）⇒ 原样传播**原异常** + 双清空（不臆断服务端状态）。"""
    client = _make_async_client()

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert client._desired_office is None
    assert client._confirmed_office is None


@pytest.mark.asyncio
async def test_async_pre_send_failure_clears_intent() -> None:
    """发送前失败（未连接 / 未注册 namespace）⇒ 同表双清空（请求根本没出去，不保留待重放意图）。"""
    client = _make_async_client()

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        raise BadNamespaceError("/smcp is not a connected namespace.")

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(BadNamespaceError):
        await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert client._desired_office is None


@pytest.mark.asyncio
async def test_async_transport_failure_after_disconnect_keeps_intent() -> None:
    """S5：传输层失败**撞上断连钩子**（generation 已推进）⇒ 效应不施加、意图保留待重放。

    这一行的守卫是承重条款：写成无条件清空会静默杀死 #203 建立的重放意图。
    """
    client = _make_async_client(reconnection=True)
    _register_namespace(client)

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        client._on_namespace_disconnect(_TRANSPORT_ERROR)  # 断连钩子推进 generation
        raise RuntimeError("/smcp is not a connected namespace.")

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert client._desired_office == (_OFFICE, _AGENT_NAME)
    assert client._confirmed_office is None


@pytest.mark.asyncio
async def test_async_superseded_join_skips_send() -> None:
    """取锁窗口内被更新的声明抢占 ⇒ **不发包**（wire 顺序必须与最后声明一致）。"""
    client = _make_async_client()
    sent: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        sent.append(1)
        return None

    client.call = fake_call  # type: ignore[method-assign]
    await client._office_op_lock.acquire()
    task = asyncio.create_task(client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE))
    await asyncio.sleep(0.05)  # 让 join 走到「等锁」处
    client._bump_office_generation()  # 模拟并发 leave / 换房抢占
    client._office_op_lock.release()
    await task

    assert sent == [], "被抢占的 join 不得把 JOIN 发上线"


@pytest.mark.asyncio
async def test_async_join_in_flight_then_leave_clears_both() -> None:
    """S4：join 在途 + 立刻 leave ⇒ 双清空（依赖 leave 的清账排在锁后，否则留下幽灵房）。"""
    client = _make_async_client()
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        await release.wait()
        return None

    async def fake_emit(*args: Any, **kwargs: Any) -> None:
        return None

    client.call = fake_call  # type: ignore[method-assign]
    client.emit = fake_emit  # type: ignore[method-assign]

    join_task = asyncio.create_task(client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE))
    await asyncio.wait_for(in_flight.wait(), timeout=5)

    leave_task = asyncio.create_task(client.leave_office(_OFFICE, namespace=SMCP_NAMESPACE))
    await asyncio.sleep(0.05)  # leave 入口已清 desired，正等 op-lock

    release.set()
    await join_task  # 在途 join 成功 ⇒ 落账 C
    await leave_task  # 随后 LEAVE 发出 ⇒ 清 C（清账在锁内、LEAVE 之后）

    assert client._desired_office is None
    assert client._confirmed_office is None


@pytest.mark.asyncio
async def test_async_leave_clears_confirmed_even_when_emit_fails() -> None:
    """LEAVE 发包失败（namespace 已断）⇒ `confirmed` 仍须作废：请求可能已发出，旧确认不再可信。

    若清账写在 emit **之后**且无 finally，异常路径会留下 `confirmed` ⇒ 后续任一次拒绝把意图回退到
    用户刚退掉的房 ⇒ 下次重连自动回房到幽灵房。
    """
    client = _make_async_client()
    client._confirmed_office = (_OFFICE, _AGENT_NAME)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    async def fake_emit(*args: Any, **kwargs: Any) -> None:
        raise BadNamespaceError("/smcp is not a connected namespace.")

    client.emit = fake_emit  # type: ignore[method-assign]

    with pytest.raises(BadNamespaceError):
        await client.leave_office(_OFFICE, namespace=SMCP_NAMESPACE)

    assert client._desired_office is None
    assert client._confirmed_office is None


@pytest.mark.asyncio
async def test_async_cancelled_join_keeps_declared_intent() -> None:
    """S10：显式 join 被取消（`CancelledError`，非 `Exception`）⇒ 停在入口预写值（有意）。"""
    client = _make_async_client()
    in_flight = asyncio.Event()

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        await asyncio.sleep(30)
        return None

    client.call = fake_call  # type: ignore[method-assign]

    task = asyncio.create_task(client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE))
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client._desired_office == (_OFFICE, _AGENT_NAME), "取消不改状态：预写意图保留（撤销须显式 leave）"
    assert client._confirmed_office is None


@pytest.mark.asyncio
async def test_async_stale_success_blocked_by_session_epoch() -> None:
    """S11：手工断连（会话边界）与在途 join 的成功落账撞车 ⇒ 陈旧成功**不得**落账。"""
    client = _make_async_client(reconnection=False)
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        await release.wait()
        return None

    client.call = fake_call  # type: ignore[method-assign]

    task = asyncio.create_task(client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE))
    await asyncio.wait_for(in_flight.wait(), timeout=5)

    client._on_namespace_disconnect(_CLIENT_DISCONNECT)  # 会话边界：推进 session/generation
    release.set()
    await task

    assert client._confirmed_office is None, "已随会话作废的成功不得落账（会话纪元守卫）"
    assert client._desired_office is None


@pytest.mark.asyncio
async def test_async_replay_success_records_confirmed_office() -> None:
    """回放成功 ⇒ 写 `_confirmed_office`（白名单里的第二个写入者），供之后的拒绝回退使用。"""
    client = _make_async_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 3
    client.call = _rejecting_async_call(None)  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 3)

    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)


@pytest.mark.asyncio
async def test_async_superseded_replay_success_still_records_confirmed() -> None:
    """成功是关于服务端**事实**的陈述 ⇒ 被抢占的回放成功仍落账（#213），但不再改其它状态。

    若按失败效应表的 `generation == current` 条件逐行套用，这里会**吞掉成功落账**，其反例正是
    `_confirmed_office` 的存在理由。
    """
    client = _make_async_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        await release.wait()
        return None

    client.call = fake_call  # type: ignore[method-assign]

    task = asyncio.create_task(client._arejoin_office((_OFFICE, _AGENT_NAME), 1))
    await asyncio.wait_for(in_flight.wait(), timeout=5)

    # 用户显式换房（抢占这次回放）
    client._bump_office_generation()
    client._desired_office = ("officeB", _AGENT_NAME)

    release.set()
    await task

    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)
    assert client._desired_office == ("officeB", _AGENT_NAME)


# ── #218 C1：sync 镜像 / sync mirror ─────────────────────────────────────────


def test_sync_rejected_switch_restores_confirmed_office() -> None:
    """S1（同步）：换房被拒 ⇒ 回退到已确认房（不清空）。"""
    client = _make_sync_client(reconnection=False)
    client._confirmed_office = (_OFFICE, _AGENT_NAME)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4106, "message": "Agent already in another room"}

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        client.join_office("officeB", _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert ei.value.code == 4106
    assert client._desired_office == (_OFFICE, _AGENT_NAME)
    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)


def test_sync_rejected_first_join_clears_both() -> None:
    """S9（同步）：从未成功入房 ⇒ 首次被拒即双清空。"""
    client = _make_sync_client()

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert ei.value.code == 4101
    assert client._desired_office is None
    assert client._confirmed_office is None


def test_sync_403_appends_identity_hint_and_rolls_back_both_halves() -> None:
    """S3（同步）：改名被拒 ⇒ 名字半随房号半回滚 + 异常带身份提示（双路径同文案）。"""
    client = _make_sync_client(reconnection=False)
    client._confirmed_office = (_OFFICE, "alice")
    client._desired_office = ("officeB", "bob")

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        return {"code": 403, "message": "Role or name mismatch with existing session"}

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as ei:
        client.join_office("officeB", "bob", namespace=SMCP_NAMESPACE)

    assert ei.value.code == 403
    assert "alice" in ei.value.error_message
    assert "重新建立连接" in ei.value.error_message
    assert client._desired_office == (_OFFICE, "alice")
    assert client._confirmed_office == (_OFFICE, "alice")


def test_sync_transport_failure_propagates_and_clears_both() -> None:
    """S2（同步）：传输层失败 ⇒ 原样传播 + 双清空。"""
    client = _make_sync_client()

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert client._desired_office is None
    assert client._confirmed_office is None


def test_sync_leave_clears_confirmed_even_when_emit_fails() -> None:
    """LEAVE 发包失败 ⇒ `confirmed` 仍须作废（异常安全，与异步同构）。"""
    client = _make_sync_client()
    client._confirmed_office = (_OFFICE, _AGENT_NAME)
    client._desired_office = (_OFFICE, _AGENT_NAME)

    def fake_emit(*args: Any, **kwargs: Any) -> None:
        raise BadNamespaceError("/smcp is not a connected namespace.")

    client.emit = fake_emit  # type: ignore[method-assign]

    with pytest.raises(BadNamespaceError):
        client.leave_office(_OFFICE, namespace=SMCP_NAMESPACE)

    assert client._desired_office is None
    assert client._confirmed_office is None


def test_sync_join_in_flight_then_leave_clears_both() -> None:
    """S4（同步）：join 在途 + 立刻 leave ⇒ 双清空（op-lock 保证 LEAVE 排在在途 JOIN 之后落地）。"""
    client = _make_sync_client()
    in_flight = threading.Event()
    release = threading.Event()

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        release.wait(timeout=5)
        return None

    def fake_emit(*args: Any, **kwargs: Any) -> None:
        return None

    client.call = fake_call  # type: ignore[method-assign]
    client.emit = fake_emit  # type: ignore[method-assign]

    join_thread = threading.Thread(target=lambda: client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE))
    join_thread.start()
    assert in_flight.wait(timeout=5), "join 未跑到发包处"

    leave_thread = threading.Thread(target=lambda: client.leave_office(_OFFICE, namespace=SMCP_NAMESPACE))
    leave_thread.start()
    time.sleep(0.05)  # leave 入口已清 desired、正等 op-lock

    release.set()
    join_thread.join(timeout=5)
    leave_thread.join(timeout=5)
    assert not join_thread.is_alive() and not leave_thread.is_alive()

    assert client._desired_office is None
    assert client._confirmed_office is None


def test_sync_superseded_join_skips_send() -> None:
    """取锁窗口内被抢占 ⇒ **不发包**（`threading.Lock` 不公平，锁内重查是必需的）。"""
    client = _make_sync_client()
    sent: list[int] = []

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        sent.append(1)
        return None

    client.call = fake_call  # type: ignore[method-assign]
    client._office_op_lock.acquire()
    thread = threading.Thread(target=lambda: client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE))
    thread.start()
    time.sleep(0.05)  # join 走到「等锁」处
    client._bump_office_generation()
    client._office_op_lock.release()
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert sent == [], "被抢占的 join 不得把 JOIN 发上线"


def test_sync_replay_success_records_confirmed_office() -> None:
    """回放成功 ⇒ 写 `_confirmed_office`（同步侧）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 3

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        return None

    client.call = fake_call  # type: ignore[method-assign]

    client._rejoin_office((_OFFICE, _AGENT_NAME), 3)

    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)


def test_sync_replay_failure_does_not_bump_generation() -> None:
    """回放失败**不**额外推进 generation —— 与异步侧逐字同构（旧实现的 `_drop_desired_office()` 会）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.0  # 只看失败效应，不跑退避
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 7

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]

    client._rejoin_office((_OFFICE, _AGENT_NAME), 7)

    assert client._desired_office is None
    assert client._office_generation == 7, "失败效应不得推进 generation（两侧同构）"


class _ArmingLock:
    """注入交错的 ``threading.Lock`` 替身：**armed 后**的首次获取前先执行注入动作。

    用来在单线程测试里确定性地复现「另一线程的入口段刚好落在『判据已读、效应未写』窗口内」——
    GIL 抢占窗口无法用 sleep 稳定复现，故改为在锁获取点注入。/ Deterministically injects a
    competing operation's entry write at the lock-acquisition point (a GIL window cannot be raced
    reliably with sleeps).
    """

    def __init__(self, real: threading.Lock, is_armed: Any, inject: Any) -> None:
        self._real = real
        self._is_armed = is_armed
        self._inject = inject
        self.fired = False

    def __enter__(self) -> _ArmingLock:
        if self._is_armed() and not self.fired:
            self.fired = True  # 先行置位：注入动作自身也会获取本锁
            self._inject()
        self._real.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._real.release()


def test_sync_transport_failure_effect_cannot_clobber_concurrent_join() -> None:
    """陈旧失败效应不得清掉**并发 join(B)** 刚写下的意图：守卫与效应必须同一次锁获取。

    交错（隔离审查报出）：U 的 `join(A)` 传输层失败 → 判据已读为「仍最新」→ 并发 V 的 `join(B)`
    **入口段**（bump generation + 写 desired）落在这条窗口里 → 修复前 U 按陈旧判据双清空 ⇒ V 随后
    成功入房、`_desired_office` 却永久为 None ⇒ 该连接此后任何重连都不再回放 B。
    """
    client = _make_sync_client()
    armed = False

    def raising_call(*args: Any, **kwargs: Any) -> Any:
        nonlocal armed
        armed = True  # 失败已发生：此后任意一次锁获取都可能撞上并发入口段
        raise RuntimeError("boom")

    def inject_concurrent_entry() -> None:
        # 与真实实现同序：bump generation → 状态锁内写 desired
        client._bump_office_generation()
        with client._office_state_lock:
            client._desired_office = ("officeB", _AGENT_NAME)

    client.call = raising_call  # type: ignore[method-assign]
    lock = _ArmingLock(threading.Lock(), lambda: armed, inject_concurrent_entry)
    client._office_state_lock = lock  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

    assert lock.fired, "交错注入未生效 —— 夹具失效即假绿"
    assert client._desired_office == ("officeB", _AGENT_NAME), "陈旧效应不得清掉更新操作的意图"


def test_sync_replay_failure_effect_cannot_clobber_concurrent_join() -> None:
    """回放线程的陈旧失败效应同样不得清掉并发 join 的意图与已确认成员关系（同窗口，另一调用点）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    client._confirmed_office = (_OFFICE, "alice")
    armed = False

    def raising_call(*args: Any, **kwargs: Any) -> Any:
        nonlocal armed
        armed = True
        raise RuntimeError("boom")

    def inject_concurrent_entry() -> None:
        client._bump_office_generation()
        with client._office_state_lock:
            client._desired_office = ("officeB", _AGENT_NAME)
            client._confirmed_office = ("officeB", _AGENT_NAME)

    client.call = raising_call  # type: ignore[method-assign]
    lock = _ArmingLock(threading.Lock(), lambda: armed, inject_concurrent_entry)
    client._office_state_lock = lock  # type: ignore[assignment]

    client._rejoin_office((_OFFICE, _AGENT_NAME), 1)

    assert lock.fired, "交错注入未生效 —— 夹具失效即假绿"
    assert client._desired_office == ("officeB", _AGENT_NAME), "陈旧回放不得清掉新意图"
    assert client._confirmed_office == ("officeB", _AGENT_NAME), "陈旧回放不得清掉新已确认房"


def test_sync_stale_success_blocked_by_session_epoch() -> None:
    """S11（同步）：会话边界与回放的成功落账撞车 ⇒ 陈旧成功不得落账。"""
    client = _make_sync_client(reconnection=False)
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    in_flight = threading.Event()
    release = threading.Event()

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        release.wait(timeout=5)
        return None

    client.call = fake_call  # type: ignore[method-assign]

    thread = threading.Thread(target=lambda: client._rejoin_office((_OFFICE, _AGENT_NAME), 1))
    thread.start()
    assert in_flight.wait(timeout=5)

    client._on_namespace_disconnect(_CLIENT_DISCONNECT)  # 会话边界：推进 session/generation
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert client._confirmed_office is None, "已随会话作废的成功不得落账"


# ── #212：重连回房的瞬态冲突有界退避（async + sync）─────────────────────────────────
#
# 协议依据：error-handling.md:486 —— 4101/4105「仅当本端刚经历传输层重连」时可做**有界**退避重试；
# room-model.md:216-218 —— 服务端回收窗口可达数十秒，任何有限短窗都覆盖不了它（故默认预算 45s）。
# **只有回放路径重试**：显式 join_office 的语义在 #218 已定（不重试），其用例见上文「#218 C1」小节。
#
# 测试里的预算一律取**极小值**（退避被 ``min(曲线, 剩余预算)`` 夹取 ⇒ 不牺牲确定性也无需真等 45s）。


class _RecordingAsyncLock:
    """记录 enter/exit 序列的 ``asyncio.Lock`` 替身 —— 证明**退避期间不持锁**（#217 补正 §三）。

    「预算内逐次取/放锁」的可观测形态：每次尝试各形成一对 enter/exit；若把 sleep 挪进临界区
    （整体持锁），序列里就不会出现「每尝试一对」的交替。/ Records lock enter/exit pairs so a
    whole-budget critical section cannot pass as per-attempt locking.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.events: list[str] = []

    async def __aenter__(self) -> _RecordingAsyncLock:
        self.events.append("enter")
        await self._real.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._real.release()
        self.events.append("exit")

    def locked(self) -> bool:
        return bool(self._real.locked())


class _RecordingLock:
    """``threading.Lock`` 版本（sync 侧）/ the threading counterpart."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.events: list[str] = []

    def __enter__(self) -> _RecordingLock:
        self.events.append("enter")
        self._real.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._real.release()
        self.events.append("exit")


_ROOM_FULL = {"code": 4101, "message": "Room already has an agent"}
_NAME_TAKEN = {"code": 4105, "message": "Name already taken in room"}

#: 异步探针的**限时取锁**超时：必须严格小于被测用例的退避窗口（`office_rejoin_retry_budget`），
#: 否则「退避期间锁被持有」不再可观测 —— 用例会退化成恒真（各用例内另有 `assert` 自检）。
PROBE_TIMEOUT = 0.5


async def _await_until(predicate: Any, *, timeout: float = 5.0) -> None:
    """有界等待断言（**不得无界忙等**：``poe test`` 无 ``--timeout`` 护栏，挂死会拖住整个套件）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"等待超时（{timeout}s）：夹具或实现未按预期推进")
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_async_replay_self_heals_across_a_transient_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """回房撞旧会话（4101）→ 退避重试 → 服务端回收后成功 ⇒ 落账，且**不落 ERROR**（抖动自愈）。

    这是本单的目标形态：静默断线后用户无需手工重入，日志里只有一条成功 INFO。
    """
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.3
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 3
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL) if len(attempts) == 1 else None  # 第 2 次：旧会话已被回收

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 3)

    assert len(attempts) == 2, "瞬态冲突必须重试"
    assert client._confirmed_office == (_OFFICE, _AGENT_NAME), "重试成功后落账"
    fake_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_async_replay_gives_up_when_budget_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算耗尽 ⇒ 落失败效应 + **共享 ERROR 产出者**（文案与显式路径同源，#219 同态口径）。"""
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.25
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_NAME_TAKEN)

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 1)

    assert len(attempts) >= 2, "预算内必须重试"
    assert client._desired_office is None, "预算耗尽 ⇒ 落失败效应（回放侧 confirmed 为空 ⇒ 双清空）"
    fake_logger.error.assert_called_once()
    text = fake_logger.error.call_args[0][0]
    assert "4105" in text and _OFFICE in text, "共享 ERROR 文案不得因重试而改变"


@pytest.mark.asyncio
async def test_async_replay_marks_exhausted_retries_in_own_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """「重试过几次」走**本模块**的 WARNING，不污染共享 ERROR 文案（#219 同态口径）。"""
    fake_logger = MagicMock()
    monkeypatch.setattr(client_mod, "logger", fake_logger)
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.2
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 1)

    warnings = [c.args[0] for c in fake_logger.warning.call_args_list]
    assert any("重试耗尽" in w for w in warnings), f"须有一条重试耗尽 WARNING，实得：{warnings}"


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 4106, "message": "Agent already in another room"},
        {"code": 500, "message": "Internal server error"},
        {"code": "not-a-number", "message": "weird"},
        {"message": "boom"},
    ],
)
@pytest.mark.asyncio
async def test_async_replay_never_retries_non_transient_outcomes(payload: dict) -> None:
    """一次即终：``4106``（非瞬态）/ ``500``（可晚于提交）/ 码不可解析 / 无码，都不重试。

    与 `test_async_replay_retries_name_conflict_too` 构成正反对照：``4101`` 与 ``4105`` 才重试。
    """
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 5.0  # 预算充足：断言「不重试」必须是判据使然，而非预算耗尽
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(payload)

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 1)

    assert attempts == [1], "非瞬态码 / 无码一律一次即终"


@pytest.mark.asyncio
async def test_async_replay_retries_name_conflict_too() -> None:
    """``4105`` 与 ``4101`` 同属瞬态冲突（协议把二者并列为同一类），同样重试。"""
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.3
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_NAME_TAKEN) if len(attempts) == 1 else None

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office((_OFFICE, _AGENT_NAME), 1)

    assert len(attempts) == 2
    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)


@pytest.mark.asyncio
async def test_async_replay_releases_op_lock_between_attempts() -> None:
    """退避在**锁外**（#217 补正 §三裁决）：每次尝试各取一次 op-lock，否则一次恢复会占满 office 互斥。

    两条断言各自独立取证：① enter/exit 逐次交替（结构）；② 退避窗口内**限时**取到锁（行为）。

    ② 必须是**限时**取锁而非「取到后看计数」：把 sleep 挪进同一次临界区时，锁释放后 asyncio.Lock 的
    FIFO 会把下一次获取优先交给等待中的探针 ⇒ 探针照样「取到且计数为 1」，计数型断言杀不死它（实测）。
    限时取锁把「退避期间锁被持有」变成可观测事实：持有整个退避 ⇒ 0.5s 内拿不到。
    """
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 1.0  # 退避窗口 1.0s（曲线被剩余预算夹取）> 探针 0.5s 超时
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]
    # 夹具自检（防「探针变盲」的假绿）：限时取锁只在「退避窗口 > 探针超时」时才有分辨力。
    assert PROBE_TIMEOUT < client.office_rejoin_retry_budget, "探针超时须小于退避窗口，否则断言退化成恒真"
    real_lock = client._office_op_lock  # 探针用**真锁**：两条断言各用各的仪器，互不污染
    lock = _RecordingAsyncLock(real_lock)
    client._office_op_lock = lock  # type: ignore[assignment]

    task = asyncio.create_task(client._arejoin_office((_OFFICE, _AGENT_NAME), 1))
    # 让回放跑完第一次尝试并进入退避
    await _await_until(lambda: len(attempts) >= 1)

    probe_took_lock_at: list[int] = []

    async def probe() -> None:
        await asyncio.wait_for(real_lock.acquire(), timeout=PROBE_TIMEOUT)  # 超时 ⇒ TimeoutError ⇒ 用例红
        probe_took_lock_at.append(len(attempts))
        real_lock.release()

    await probe()
    await task

    assert probe_took_lock_at == [1], "探针须在**退避窗口内**取到锁（回放结束后才取到不算）"
    assert lock.events == ["enter", "exit"] * len(attempts), (
        f"每次尝试须是独立的临界区；实得 {lock.events}（尝试 {len(attempts)} 次）"
    )


@pytest.mark.asyncio
async def test_async_replay_superseded_during_backoff_sends_nothing_more() -> None:
    """退避期间被更新的操作抢占（generation 前进）⇒ 中止：不再发包、不得改动新意图。"""
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.4
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]

    task = asyncio.create_task(client._arejoin_office((_OFFICE, _AGENT_NAME), 1))
    await _await_until(lambda: len(attempts) >= 1)

    # 退避窗口内用户显式换房（入口段：推进 generation + 写新意图）
    client._office_generation = 2
    client._desired_office = ("officeB", _AGENT_NAME)
    await task

    assert attempts == [1], "被抢占后不得再发包"
    assert client._desired_office == ("officeB", _AGENT_NAME), "陈旧回放不得改新意图"


@pytest.mark.asyncio
async def test_async_replay_cancelled_during_backoff_keeps_intent() -> None:
    """退避期间被取消（会话边界 / shutdown）⇒ 静默收尾：不改状态、意图保留待下次重放。"""
    client = _make_async_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.4
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]

    task = asyncio.create_task(client._arejoin_office((_OFFICE, _AGENT_NAME), 1))
    await _await_until(lambda: len(attempts) >= 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == [1], "取消后不得再发包"
    assert client._desired_office == (_OFFICE, _AGENT_NAME), "取消不是失败效应：意图保留（交给下一次重放）"


# ── sync 镜像 ────────────────────────────────────────────────────────────────


def test_sync_replay_self_heals_across_a_transient_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步侧自愈（镜像异步）：撞旧会话 → 退避重试 → 成功落账，不落 ERROR。"""
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.3
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL) if len(attempts) == 1 else None

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    _sync_wait_thread(client, timeout=5.0)

    assert len(attempts) == 2
    assert client._confirmed_office == (_OFFICE, _AGENT_NAME)
    fake_logger.error.assert_not_called()


def test_sync_replay_gives_up_when_budget_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步侧预算耗尽（镜像异步）：落效应 + 共享 ERROR + 重试耗尽 WARNING。"""
    fake_office_logger = MagicMock()
    fake_client_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_office_logger)
    monkeypatch.setattr(sync_client_mod, "logger", fake_client_logger)
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.25
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_NAME_TAKEN)

    client.call = fake_call  # type: ignore[method-assign]

    started = time.monotonic()
    client._on_namespace_connect()
    _sync_wait_thread(client, timeout=5.0)
    elapsed = time.monotonic() - started

    assert len(attempts) >= 2
    assert client._desired_office is None
    assert elapsed < 0.25 + 0.3, f"预算须是**墙钟**上界（退避被剩余预算夹取）；实耗 {elapsed:.2f}s"
    fake_office_logger.error.assert_called_once()
    warnings = [c.args[0] for c in fake_client_logger.warning.call_args_list]
    assert any("重试耗尽" in w for w in warnings), f"须有一条重试耗尽 WARNING，实得：{warnings}"


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 4106, "message": "Agent already in another room"},
        {"code": 500, "message": "Internal server error"},
        {"message": "boom"},
    ],
)
def test_sync_replay_never_retries_non_transient_outcomes(payload: dict) -> None:
    """同步侧一次即终：``4106`` / ``500`` / 无码都不重试（预算充足 ⇒ 判据使然）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 5.0
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(payload)

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    _sync_wait_thread(client, timeout=5.0)

    assert attempts == [1], "非瞬态码 / 无码一律一次即终"


def test_sync_replay_releases_op_lock_between_attempts() -> None:
    """同步侧退避在**两把锁之外**：① enter/exit 逐次交替（结构）② 退避窗口内**取得锁**（行为）。

    只有 ① 不够：把 ``time.sleep`` 挪进**同一次**临界区时 enter/exit **对数不变**（每次尝试仍各一对），
    结构断言照样全绿（隔离审查以变异实证：整段缩进进 ``with`` 后 69 例全绿）。② 才是「退避不占满
    office 互斥」的行为证据 —— 与异步侧的探针同构。/ Counting pairs alone cannot falsify "slept inside
    the critical section"; the probe is the behavioural evidence.
    """
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 1.0  # 退避窗口 = 1.0s（曲线被剩余预算夹取）⇒ 探针有充足窗口
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []
    first_attempt = threading.Event()

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        first_attempt.set()
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]
    # 夹具自检（防「探针变盲」的假绿）：限时取锁只在「退避窗口 > 探针超时」时才有分辨力。
    assert PROBE_TIMEOUT < client.office_rejoin_retry_budget, "探针超时须小于退避窗口，否则断言退化成恒真"
    real_lock = client._office_op_lock  # 探针用**真锁**：两条断言各用各的仪器，互不污染
    lock = _RecordingLock(real_lock)
    client._office_op_lock = lock  # type: ignore[assignment]

    client._on_namespace_connect()
    assert first_attempt.wait(timeout=5), "回放线程未发起首次尝试（夹具失效即假绿）"

    # 退避窗口内（整段预算 1.0s）另一次 office 操作必须能拿到锁；整体持锁则 0.5s 内必然拿不到
    probe_got_lock = real_lock.acquire(timeout=PROBE_TIMEOUT)
    probe_attempts = len(attempts)
    if probe_got_lock:
        real_lock.release()
    _sync_wait_thread(client, timeout=5.0)

    assert probe_got_lock, "退避期间 op-lock 必须空闲（拿不到 ⇒ 一次恢复把 office 互斥占满）"
    assert probe_attempts == 1, "探针须在**退避窗口内**取到锁（回放结束后才取到不算）"
    assert len(attempts) >= 2, "预算内必须重试（否则本断言退化成单次路径）"
    assert lock.events == ["enter", "exit"] * len(attempts), (
        f"每次尝试须是独立的临界区；实得 {lock.events}（尝试 {len(attempts)} 次）"
    )


def test_sync_replay_superseded_during_backoff_sends_nothing_more() -> None:
    """同步侧：退避期间被抢占 ⇒ 醒来即在守卫处退出（不再发包、不改新意图）。

    同步线程**不可取消**（``_cancel_office_rejoin`` 只摘引用）⇒ 孤儿线程最多多睡一轮退避，
    本用例钉的正是「睡完不发包、不碰状态」。
    """
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.5
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []
    first_attempt = threading.Event()

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        first_attempt.set()
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    assert first_attempt.wait(timeout=5), "回放线程未发起首次尝试（夹具失效即假绿）"

    # 退避窗口内用户显式换房（入口段：推进 generation + 写新意图）
    client._office_generation = 2
    client._desired_office = ("officeB", _AGENT_NAME)
    _sync_wait_thread(client, timeout=5.0)

    assert attempts == [1], "被抢占后不得再发包"
    assert client._desired_office == ("officeB", _AGENT_NAME), "陈旧回放不得改新意图"


def test_sync_give_up_effect_cannot_clobber_concurrent_join() -> None:
    """**退避耗尽**这条效应路径同样受「判据与写同一次状态锁获取」保护（#217 补正 §三后的新增调用点）。

    与既有两条 ``_ArmingLock`` 用例同构，只是注入点换到「预算耗尽 ⇒ 落效应」这一次临界区。

    **注入值必须与回退目标不同值**：``4101`` 属校验类 ⇒ 陈旧回放的效应是 ``desired := confirmed``。
    若把注入的 ``confirmed`` 也写成新意图，无论守卫在不在，两条断言都成立 —— 那是**假绿**（本用例第一
    版即踩此坑，隔离审查以变异实证：删掉守卫后本用例照样通过）。故注入 ``confirmed = officeC``，
    与新意图 ``desired = officeB`` 分列两侧。/ Inject a *different* confirmed value: with 4101 the stale
    effect assigns ``desired := confirmed``, so equal values would make the assertions vacuously true.
    """
    client = _make_sync_client()
    _register_namespace(client)
    client.office_rejoin_retry_budget = 0.0  # 首次失败即耗尽 ⇒ 落效应
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    client._confirmed_office = (_OFFICE, "alice")

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        return dict(_ROOM_FULL)

    def inject_concurrent_entry() -> None:
        client._bump_office_generation()
        with client._office_state_lock:  # _ArmingLock 已先行置位 fired ⇒ 不会递归触发注入
            client._desired_office = ("officeB", _AGENT_NAME)
            client._confirmed_office = ("officeC", "alice")

    client.call = fake_call  # type: ignore[method-assign]
    lock = _ArmingLock(client._office_state_lock, lambda: True, inject_concurrent_entry)
    client._office_state_lock = lock  # type: ignore[assignment]

    client._rejoin_office((_OFFICE, _AGENT_NAME), 1)

    assert lock.fired, "交错注入未生效 —— 夹具失效即假绿"
    assert client._desired_office == ("officeB", _AGENT_NAME), "陈旧的回退不得清掉新意图"
    assert client._confirmed_office == ("officeC", "alice"), "陈旧的回退不得清掉新已确认房"
