# -*- coding: utf-8 -*-
"""
#203 Office 成员关系状态机（Computer 侧单元测试）。

覆盖真实 wire 用例难以确定性触发的分支：
  - 断连 reason → desired（office_id）保留/清空的判据表；
  - 「重连彻底放弃」（``__disconnect_final``）清空；
  - 回房的 generation 守卫：陈旧结果不得落状态（含"回房在途时用户显式换房"的竞态）；
  - 回房被拒 → compare-and-clear；
  - 未实际在 namespace 中时 ``emit_update_*`` 为 no-op（desired 保留窗口内不得放行）。

Unit coverage for the #203 Computer-side membership state machine: the disconnect-reason table,
final-disconnect clearing, generation-guarded replay application, rejection semantics, and the
tightened emit guards.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, SMCP_NAMESPACE
from a2c_smcp.utils.office import OFFICE_REJOIN_TIMEOUT

_TRANSPORT_ERROR = "transport error"
_CLIENT_DISCONNECT = "client disconnect"
_SERVER_DISCONNECT = "server disconnect"


def _make_client(**kwargs: Any) -> SMCPComputerClient:
    computer = MagicMock()
    computer.name = "test_computer"
    return SMCPComputerClient(computer=computer, **kwargs)


def _mark_namespace_registered(client: SMCPComputerClient, sid: str = "fake-sid") -> None:
    """模拟"namespace 在册"（回房守卫的判据，**不是** ``connected``，见实现注释）。

    Simulate a registered namespace — the replay guard, deliberately not ``connected``.
    """
    client.namespaces[SMCP_NAMESPACE] = sid


# ── 断连 reason 判据表 / disconnect-reason table ──────────────────────────────


@pytest.mark.asyncio
async def test_transport_error_retains_desired_office_while_reconnecting() -> None:
    """传输中断且会自动重连 → 保留 desired（等重连后回房）。

    English: a transport error with auto-reconnect enabled retains the desired office.
    """
    client = _make_client(reconnection=True)
    client.office_id = "officeA"

    client._on_namespace_disconnect(_TRANSPORT_ERROR)

    assert client.office_id == "officeA"


@pytest.mark.asyncio
async def test_transport_error_without_reconnection_clears_desired_office() -> None:
    """不会重连（``reconnection=False``）→ 没有恢复路径，必须清空。

    English: without reconnect there is no path back, so the desired office must be dropped.
    """
    client = _make_client(reconnection=False)
    client.office_id = "officeA"

    client._on_namespace_disconnect(_TRANSPORT_ERROR)

    assert client.office_id is None


@pytest.mark.parametrize("reason", [_CLIENT_DISCONNECT, _SERVER_DISCONNECT])
@pytest.mark.asyncio
async def test_manual_and_server_disconnect_clear_desired_office(reason: str) -> None:
    """手工断开 / 服务端踢出 → 清空意图（下次连接不得静默回旧房间）。

    English: a manual disconnect or a server kick clears the intent.
    """
    client = _make_client(reconnection=True)
    client.office_id = "officeA"

    client._on_namespace_disconnect(reason)

    assert client.office_id is None


@pytest.mark.asyncio
async def test_disconnect_final_clears_desired_office() -> None:
    """重连彻底放弃（``__disconnect_final``）→ 清空，避免状态长期撒谎。

    English: when socketio gives up reconnecting, the desired office is cleared.
    """
    client = _make_client(reconnection=True)
    client.office_id = "officeA"

    client._on_namespace_disconnect_final()

    assert client.office_id is None


# ── connect 钩子 / connect hook ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_without_desired_office_schedules_no_replay() -> None:
    """首连（未加入过房间）不得调度回房。"""
    client = _make_client(reconnection=True)
    assert client.office_id is None

    client._on_namespace_connect()

    assert client._office_rejoin_task is None


@pytest.mark.asyncio
async def test_connect_with_desired_office_schedules_replay() -> None:
    """重连（desired 仍在）必须调度回房并真正发出 server:join_office。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    calls: list[dict] = []

    async def fake_call(event: str, data: Any = None, namespace: str | None = None, **kwargs: Any):
        calls.append({"event": event, "data": dict(data or {}), "namespace": namespace, "timeout": kwargs.get("timeout")})
        return [True, None]

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    task = client._office_rejoin_task
    assert task is not None
    await task

    assert len(calls) == 1
    assert calls[0]["event"] == JOIN_OFFICE_EVENT
    assert calls[0]["data"]["office_id"] == "officeA"
    assert calls[0]["data"]["role"] == "computer"
    assert calls[0]["data"]["name"] == "test_computer"
    assert calls[0]["namespace"] == SMCP_NAMESPACE
    # 回房必须是**有界**等待：socketio 的 call() 在连接已断时不快速失败，会吃满默认 60s
    assert calls[0]["timeout"] == OFFICE_REJOIN_TIMEOUT, "回房必须带 OFFICE_REJOIN_TIMEOUT 有界超时"
    assert client.office_id == "officeA"


@pytest.mark.asyncio
async def test_connect_during_inflight_replay_reschedules_cleanly() -> None:
    """回房在途时再次断连再重连：旧回房作废、由新的 connect 钩子重新调度，状态不被陈旧结果污染。

    English: a second connect while a replay is in flight must supersede it and schedule a fresh
    replay — the stale attempt must not touch state.
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    attempts: list[int] = []
    first_in_flight = asyncio.Event()
    release = asyncio.Event()

    async def fake_call(*args: Any, **kwargs: Any):
        attempts.append(1)
        if len(attempts) == 1:
            first_in_flight.set()
            await release.wait()
            raise RuntimeError("namespace is not a connected namespace.")  # 第二次断链
        return [True, None]

    client.call = fake_call  # type: ignore[method-assign]

    client._on_namespace_connect()
    first_task = client._office_rejoin_task
    assert first_task is not None
    await asyncio.wait_for(first_in_flight.wait(), timeout=5)

    # 再次断连 + 重连：新 connect 钩子应作废在途回房并发起新一次
    client._on_namespace_disconnect("transport error")
    assert client.office_id == "officeA", "传输中断且会重连时 desired 必须保留"
    client._on_namespace_connect()
    second_task = client._office_rejoin_task
    assert second_task is not None and second_task is not first_task

    release.set()
    await asyncio.wait_for(second_task, timeout=5)
    with pytest.raises(asyncio.CancelledError):
        await first_task

    assert len(attempts) == 2, "第二次连接必须重新发起回房"
    assert client.office_id == "officeA", "陈旧回房的异常不得污染状态"


# ── generation 守卫 / generation guards ──────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_generation_replay_is_dropped() -> None:
    """generation 过期 → 不发包、不碰状态（陈旧回房整体作废）。"""
    client = _make_client(reconnection=True)
    client.office_id = "officeB"
    client._office_generation = 5
    called = False

    async def fake_call(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        return [True, None]

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 4)

    assert called is False
    assert client.office_id == "officeB"


@pytest.mark.asyncio
async def test_stale_replay_rejection_does_not_clobber_newer_office() -> None:
    """回房在途期间用户显式换了房 → 陈旧回房的**失败结果**不得清掉新房号。

    这是 #203 实现里最容易踩的竞态：``join_office`` 会先写 ``office_id``，若陈旧回房的失败路径
    无条件清空，就会把用户刚设好的房间号抹掉（四个 emit 守卫随之全部静默失效）。

    English: if the user switches office while a replay is in flight, the stale replay's failure
    must not clear the newer office id.
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    client._office_generation = 1
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def fake_call(*args: Any, **kwargs: Any):
        in_flight.set()
        await release.wait()
        return [False, "Internal server error: already exists in room"]

    client.call = fake_call  # type: ignore[method-assign]

    task = asyncio.create_task(client._arejoin_office("officeA", 1))
    await asyncio.wait_for(in_flight.wait(), timeout=5)

    # 用户在回房在途时显式换房（generation 前进 + office_id 覆写）
    client._office_generation = 2
    client.office_id = "officeB"

    release.set()
    await task

    assert client.office_id == "officeB", "陈旧回房的失败结果不得清掉新房号"


@pytest.mark.asyncio
async def test_failed_join_does_not_clear_newer_office() -> None:
    """先前 join 失败时必须 compare-and-clear：后到 join 写入的新房号不得被抹掉。

    English: a failing join must not clear an office id written by a newer (later) join —
    otherwise the later join succeeds server-side while the local state stays None and the four
    emit guards go silently dead.
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    in_flight = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_call(event: str, data: Any = None, namespace: str | None = None, **kwargs: Any):
        calls.append(dict(data or {}).get("office_id", ""))
        if len(calls) == 1:
            in_flight.set()
            await release.wait()
            raise RuntimeError("加入房间失败 / Failed to join office: boom")
        return [True, None]

    client.call = fake_call  # type: ignore[method-assign]

    first = asyncio.create_task(client.join_office("officeA"))
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    # 后到 join：先推进 generation 写入 officeB，再排队等 office 操作锁
    second = asyncio.create_task(client.join_office("officeB"))
    await asyncio.sleep(0)  # 让 second 完成入口段（bump + 写 office_id）

    release.set()
    with pytest.raises(RuntimeError):
        await first
    await second

    assert calls == ["officeA", "officeB"]
    assert client.office_id == "officeB", "先前 join 的失败不得清掉后到 join 写入的房号"


@pytest.mark.asyncio
async def test_replay_rejection_clears_office_id() -> None:
    """回房被拒且 generation 仍新鲜（无人接管）→ 清空 office_id（状态不得撒谎）。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    client._office_generation = 7

    async def fake_call(*args: Any, **kwargs: Any):
        return [False, "Internal server error: already exists in room"]

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 7)

    assert client.office_id is None


@pytest.mark.asyncio
async def test_replay_exception_clears_office_id() -> None:
    """回房抛异常（如连断时的 BadNamespaceError）→ 同样清空。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    client._office_generation = 3

    async def fake_call(*args: Any, **kwargs: Any):
        raise RuntimeError("namespace is not a connected namespace.")

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 3)

    assert client.office_id is None


# ── emit 守卫 / emit guards ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["emit_update_config", "emit_update_tool_list", "emit_refresh_desktop", "emit_update_skills"],
)
async def test_emit_update_guards_require_registered_namespace(method_name: str) -> None:
    """office_id 有值但 namespace 不在册（重连窗口/已掉线）→ no-op，不得发无效包。

    English: with a retained desired office but no registered namespace, the update emitters must
    stay no-ops instead of raising ``BadNamespaceError``.
    """
    client = _make_client(reconnection=True)
    client.office_id = "officeA"
    assert SMCP_NAMESPACE not in client.namespaces
    emitted: list[Any] = []

    async def fake_emit(*args: Any, **kwargs: Any) -> None:
        emitted.append(args)

    client.emit = fake_emit  # type: ignore[method-assign]

    await getattr(client, method_name)()

    assert emitted == [], f"{method_name} 在未实际入房时不得发包"
