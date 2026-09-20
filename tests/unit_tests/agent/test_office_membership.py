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
import time
from typing import Any

import pytest

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, SMCP_NAMESPACE
from a2c_smcp.utils.office import OFFICE_REJOIN_TIMEOUT

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
    assert calls[0]["timeout"] == OFFICE_REJOIN_TIMEOUT, "回房必须带 OFFICE_REJOIN_TIMEOUT 有界超时"
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
    """回房被拒（如"Agent already in room"）→ 清空意图，状态不得撒谎。"""
    client = _make_async_client()
    _register_namespace(client)
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
    """显式 join 记录意图（供重连重放），leave 清空。"""
    client = _make_async_client()
    emitted: list[tuple[str, Any]] = []

    async def fake_emit(event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        emitted.append((event, data))

    client.emit = fake_emit  # type: ignore[method-assign]

    await client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
    assert client._desired_office == (_OFFICE, _AGENT_NAME)
    assert emitted[-1][1]["office_id"] == _OFFICE

    await client.leave_office(_OFFICE, namespace=SMCP_NAMESPACE)
    assert client._desired_office is None


# ── sync：镜像覆盖 / sync mirror ─────────────────────────────────────────────


def _sync_wait_thread(client: SMCPAgentClient, timeout: float = 5.0) -> None:
    """等回房线程收尾（同步侧无 await 可锚定）/ join the replay thread (no awaitable to anchor on)."""
    thread = client._office_rejoin_thread
    if thread is not None:
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
    assert calls[0]["timeout"] == OFFICE_REJOIN_TIMEOUT, "回房必须带 OFFICE_REJOIN_TIMEOUT 有界超时"
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
    """回房被拒 → 清空意图（单次尝试）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 7

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]

    client._rejoin_office((_OFFICE, _AGENT_NAME), 7)

    assert client._desired_office is None


def test_sync_rejected_replay_not_retried() -> None:
    """单次尝试：被拒后不得重试（镜像 rust）。"""
    client = _make_sync_client()
    _register_namespace(client)
    client._desired_office = (_OFFICE, _AGENT_NAME)
    client._office_generation = 1
    attempts: list[int] = []

    def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    _sync_wait_thread(client)
    time.sleep(0.1)

    assert attempts == [1], "被拒后不得重试"


def test_sync_join_records_intent_and_leave_clears_it() -> None:
    """显式 join/leave 记录/清空意图（同步侧）。"""
    client = _make_sync_client()
    emitted: list[tuple[str, Any]] = []

    def fake_emit(event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        emitted.append((event, data))

    client.emit = fake_emit  # type: ignore[method-assign]

    client.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
    assert client._desired_office == (_OFFICE, _AGENT_NAME)
    assert emitted[-1][1]["office_id"] == _OFFICE

    client.leave_office(_OFFICE, namespace=SMCP_NAMESPACE)
    assert client._desired_office is None
