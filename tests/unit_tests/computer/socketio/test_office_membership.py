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
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

import a2c_smcp.computer.socketio.client as computer_client_mod
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import (
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_EVENT,
    SMCP_NAMESPACE,
    UPDATE_CONFIG_EVENT,
    UPDATE_DESKTOP_EVENT,
    UPDATE_SKILLS_EVENT,
    UPDATE_TOOL_LIST_EVENT,
)
from a2c_smcp.utils.office import OFFICE_JOIN_TIMEOUT

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
        return None

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
    assert calls[0]["timeout"] == OFFICE_JOIN_TIMEOUT, "回房必须带 OFFICE_JOIN_TIMEOUT 有界超时"
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
        return None

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
        return None

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
        return {"code": 4105, "message": "Name already taken in room"}

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
        return None

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
async def test_double_rejected_join_never_restores_an_unconfirmed_office() -> None:
    """两次并发 join 均被服务端拒绝 ⇒ 终态必须是「无房」。

    join 入口会**预写** office_id（避免广播时序竞争，见实现注释）——该值是**意图**，未经服务端确认。
    若后到 join 的快照恰好读到前一次「在途且未确认」的预写值，一旦自己也被拒，就会把那个**从未加入过**
    的房号钉回状态：守卫 ``_in_office`` 随之放行 ``server:update_*``，而服务端会话里并无该房（#213）。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    in_flight = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_call(event: str, data: Any = None, namespace: str | None = None, **kwargs: Any) -> Any:
        calls.append(dict(data or {}).get("office_id", ""))
        if len(calls) == 1:
            in_flight.set()
            await release.wait()
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]

    first = asyncio.create_task(client.join_office("officeB"))
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    second = asyncio.create_task(client.join_office("officeC"))
    await asyncio.sleep(0)  # 让 second 完成入口段（bump + 预写 office_id）

    release.set()
    with pytest.raises(RuntimeError):
        await first
    with pytest.raises(RuntimeError):
        await second

    assert calls == ["officeB", "officeC"]
    assert client.office_id is None, "两次都被拒 ⇒ 不得钉在从未被确认的房号上"


@pytest.mark.asyncio
async def test_superseded_success_still_records_confirmed() -> None:
    """被**抢占的成功**也要落账：那次成员变更真实发生过，否则后续被拒会回退到更旧、已失效的房号。

    序列：基线 A → join(B) 在途且服务端**接受**（真实成员关系 = B）→ join(C) 抢占并**被拒**。
    终态必须回退到 B（事实），而不是 A（更旧）。/ A superseded success is still a server-side fact.
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    in_flight = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_call(event: str, data: Any = None, namespace: str | None = None, **kwargs: Any) -> Any:
        room = dict(data or {}).get("office_id", "")
        calls.append(room)
        if room == "officeA":
            return None
        if room == "officeB":
            in_flight.set()
            await release.wait()
            return None  # 服务端接受，但已被 J2 抢占
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = fake_call  # type: ignore[method-assign]
    await client.join_office("officeA")

    first = asyncio.create_task(client.join_office("officeB"))
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    second = asyncio.create_task(client.join_office("officeC"))
    await asyncio.sleep(0)  # 让 second 完成入口段（bump + 预写）
    release.set()
    await first
    with pytest.raises(RuntimeError, match="加入房间失败"):
        await second

    assert calls == ["officeA", "officeB", "officeC"]
    assert client.office_id == "officeB", "应回退到真实发生的 B，而非更旧的 A"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [_TRANSPORT_ERROR, _SERVER_DISCONNECT])
async def test_disconnect_then_rejected_join_reports_no_office(reason: str) -> None:
    """断连后**已确认**房号随之作废（房间成员关系属于会话）⇒ 之后被拒的 join 不得回退宣称旧房。

    传输中断时 desired 仍保留（待回房重放），但那只是「意图」：重连后的新 SID 从未加入过该房。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)

    async def accept(*args: Any, **kwargs: Any) -> Any:
        return None

    client.call = accept  # type: ignore[method-assign]
    await client.join_office("officeA")

    client._on_namespace_disconnect(reason)
    if reason == _TRANSPORT_ERROR:
        assert client.office_id == "officeA", "传输中断保留 desired（#203 口径）"

    async def reject(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = reject  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeB")

    assert client.office_id is None, "新 SID 从未加入过的房间不得被回退逻辑复活"


@pytest.mark.asyncio
async def test_superseded_replay_success_still_records_confirmed() -> None:
    """被抢占的**回房成功**同样落账（同 ``join_office`` 的口径）。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    client._office_generation = 5
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        await release.wait()
        return None

    client.call = fake_call  # type: ignore[method-assign]
    replay = asyncio.create_task(client._arejoin_office("officeA", 5))
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    client._office_generation = 6  # 被更新的操作抢占 / superseded mid-flight
    release.set()
    await replay

    async def reject(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = reject  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeB")

    assert client.office_id == "officeA", "被抢占的回房成功也必须落账"


@pytest.mark.asyncio
async def test_replay_rejection_clears_office_id() -> None:
    """回房被拒且 generation 仍新鲜（无人接管）→ **预算耗尽后**清空 office_id（状态不得撒谎）。

    预算是退化值 ``0``：本用例钉的是 #203 既有的终态；重试分支本身由文末 `#212` 小节覆盖。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 0.0
    client.office_id = "officeA"
    client._office_generation = 7

    async def fake_call(*args: Any, **kwargs: Any):
        return {"code": 4105, "message": "Name already taken in room"}

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


# ── #213 显式 join 失败时的房号去留 / office id on an explicit join failure ────


@pytest.mark.asyncio
async def test_rejected_switch_restores_previous_office() -> None:
    """换房被**服务端明确拒绝** ⇒ 回退到已确认的旧房号（它仍在旧房，不得对外宣称"无房"）。

    服务端按协议「校验先于副作用」不改动既有成员关系（#213）：拒绝换房时该 Computer 仍留在旧房，
    客户端若清空 desired 就等于对宿主撒谎（守卫 ``_in_office`` 随之静默失效）。
    English: an explicitly rejected switch restores the last server-confirmed office — the server
    leaves the existing membership untouched, so clearing it would contradict the real state.
    """
    client = _make_client()
    _mark_namespace_registered(client)

    async def accept(*args: Any, **kwargs: Any) -> Any:
        return None

    async def reject(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4105, "message": "Name already taken in room"}

    client.call = accept  # type: ignore[method-assign]
    await client.join_office("officeA")
    assert client.office_id == "officeA"

    client.call = reject  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeB")

    assert client.office_id == "officeA", "被拒的换房不得抹掉仍生效的旧房号"


@pytest.mark.asyncio
async def test_leave_then_rejected_join_does_not_resurrect_old_office() -> None:
    """退房后已确认房号随之作废 ⇒ 之后被拒的 join 不得把旧房号回退回来。"""
    client = _make_client()
    _mark_namespace_registered(client)

    async def accept(*args: Any, **kwargs: Any) -> Any:
        return None

    emitted: list[Any] = []

    async def fake_emit(*args: Any, **kwargs: Any) -> None:
        emitted.append(args)

    client.emit = fake_emit  # type: ignore[method-assign]
    client.call = accept  # type: ignore[method-assign]
    await client.join_office("officeA")
    await client.leave_office("officeA")
    assert client.office_id is None

    async def reject(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = reject  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeB")

    assert client.office_id is None, "已退掉的房间不得被回退逻辑复活"


@pytest.mark.asyncio
async def test_rejected_fresh_join_keeps_no_office() -> None:
    """**正对照**：此前无房时被拒 ⇒ 仍为 ``None``（回退不得凭空造出房号）。"""
    client = _make_client()
    _mark_namespace_registered(client)
    assert client.office_id is None

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4105, "message": "Name already taken in room"}

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeB")

    assert client.office_id is None


@pytest.mark.asyncio
async def test_switch_transport_failure_still_clears_desired() -> None:
    """传输层失败（无法判定服务端是否已生效）⇒ 维持清空语义，且**已确认房号一并不再可信**。

    English: a transport failure cannot be adjudicated client-side, so the client must not claim a
    room it may no longer be in — neither as desired nor as the confirmed fallback.
    """
    client = _make_client()
    _mark_namespace_registered(client)

    async def accept(*args: Any, **kwargs: Any) -> Any:
        return None

    client.call = accept  # type: ignore[method-assign]
    await client.join_office("officeA")  # 建立已确认房号 / establish the confirmed office

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("namespace is not a connected namespace.")

    client.call = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await client.join_office("officeB")

    assert client.office_id is None

    # 未知结局已使旧房号失效 ⇒ 之后被拒的 join 不得回退到它
    async def reject(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = reject  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeC")

    assert client.office_id is None, "未知结局后的被拒 join 不得回退到可能已失效的旧房"


@pytest.mark.asyncio
async def test_disconnect_final_then_rejected_join_reports_no_office() -> None:
    """重连彻底放弃后**已确认房号一并作废** ⇒ 被拒的 join 不得复活旧房。

    刻意与传输中断分支相反：那里 desired 保留（待重放），这里 desired 与 confirmed 都清——两者在传输
    分支上分道，故 ``test_disconnect_final_clears_desired_office``（只钉 desired）覆盖不到本行。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)

    async def accept(*args: Any, **kwargs: Any) -> Any:
        return None

    client.call = accept  # type: ignore[method-assign]
    await client.join_office("officeA")

    client._on_namespace_disconnect_final()
    assert client.office_id is None, "彻底放弃重连 ⇒ desired 清空"

    async def reject(*args: Any, **kwargs: Any) -> Any:
        return {"code": 4101, "message": "Room already has an agent"}

    client.call = reject  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeB")

    assert client.office_id is None, "新连接不得复活一个从未加入的旧房"


@pytest.mark.asyncio
async def test_switch_empty_ack_is_success() -> None:
    """v0.5.0（#214）：空 ack（``None``）= **成功**裁决 ⇒ 落账已确认房号，不再抛错。

    回归守卫：旧模型（v0.5.0 前）把 ``None`` 当「未获裁决」。若解析器未随协议更新，本用例会因
    抛 RuntimeError 而红——这正是 #213 挂账的「把拒绝/空响应当成功」缺陷的镜像面。
    """
    client = _make_client()
    _mark_namespace_registered(client)
    client.office_id = "officeA"

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        return None

    client.call = fake_call  # type: ignore[method-assign]

    await client.join_office("officeB")

    assert client.office_id == "officeB"
    assert client._confirmed_office_id == "officeB"


@pytest.mark.asyncio
async def test_switch_indeterminate_ack_clears_desired() -> None:
    """形状不认识的响应（未获裁决）⇒ 抛错并清空：它不是「拒绝」，不能据此回退房号。

    此处以**已废除的元组形态**代表「未获裁决」——解析器刻意不再兼容它（见
    ``a2c_smcp/utils/office.py``），故它必须落进「无法判定」而不是静默成功。
    """
    client = _make_client()
    _mark_namespace_registered(client)
    client.office_id = "officeA"

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        return [True, None]

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeB")

    assert client.office_id is None


# ── emit 守卫 / emit guards ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["emit_update_config", "emit_update_tool_list", "emit_refresh_desktop", "emit_update_skills"],
)
async def test_emit_update_guards_require_registered_namespace(method_name: str) -> None:
    """office_id 有值但 namespace 不在册（重连窗口/已掉线）→ no-op，不得发无效包。

    #223：emit 判据是**两半**——「已确认成员关系」∧「namespace 在册」（另见
    ``test_update_emitters_do_not_send_when_namespace_unregistered_even_if_confirmed`` 对后者的
    独立负例）。本用例只覆盖「不在册」这一半（此时 confirmed 恰也为空）。

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


# ── #212：重连回房的瞬态冲突有界退避 ──────────────────────────────────────────────
#
# 协议依据：error-handling.md:486（4101/4105 条件可选重试，仅限传输层重连后的恢复路径）、
# room-model.md:216-218（回收窗口可达数十秒 ⇒ 默认预算须覆盖它）。**只有回放路径重试**：显式
# join_office 不重试（语义归 #213/#214，见上文「显式 join 失败时的房号去留」小节）。
# 预算一律取极小值（被 ``min(曲线, 剩余预算)`` 夹取 ⇒ 不牺牲确定性也无需真等 45s）。

_ROOM_FULL = {"code": 4101, "message": "Room already has an agent"}

#: 限时取锁超时（见 agent 侧同名常量说明）：必须严格小于退避窗口。
PROBE_TIMEOUT = 0.5


class _RecordingAsyncLock:
    """记录 enter/exit 序列的 ``asyncio.Lock`` 替身 —— 证明**退避期间不持锁**（#217 补正 §三）。"""

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


async def _await_until(predicate: Any, *, timeout: float = 5.0) -> None:
    """有界等待断言（``poe test`` 无 ``--timeout`` 护栏，无界忙等会拖住整个套件）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"等待超时（{timeout}s）：夹具或实现未按预期推进")
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_replay_self_heals_across_a_transient_conflict() -> None:
    """回房撞旧会话（4101）→ 退避重试 → 服务端回收后成功 ⇒ 落账（Computer 侧抖动自愈）。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 0.3
    client.office_id = "officeA"
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL) if len(attempts) == 1 else None

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 1)

    assert len(attempts) == 2, "瞬态冲突必须重试"
    assert client.office_id == "officeA"
    assert client._confirmed_office_id == "officeA", "重试成功后落账"


@pytest.mark.asyncio
async def test_replay_gives_up_when_budget_is_exhausted() -> None:
    """预算耗尽 ⇒ 清空 desired + 已确认房号 + ERROR（文案带码，与状态清空同步）。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 0.25
    client.office_id = "officeA"
    client._confirmed_office_id = "officeA"
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 1)

    assert len(attempts) >= 2, "预算内必须重试"
    assert client.office_id is None
    assert client._confirmed_office_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"code": 4106, "message": "Agent already in another room"},
        {"code": 500, "message": "Internal server error"},
        {"message": "boom"},
    ],
)
async def test_replay_never_retries_non_transient_outcomes(payload: dict) -> None:
    """一次即终：``4106``（非瞬态）/ ``500``（可晚于提交）/ 无码都不重试（预算充足 ⇒ 判据使然）。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 5.0
    client.office_id = "officeA"
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(payload)

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 1)

    assert attempts == [1], "非瞬态码 / 无码一律一次即终"


@pytest.mark.asyncio
async def test_replay_releases_op_lock_between_attempts() -> None:
    """退避在**锁外**：① enter/exit 逐次交替（结构）② 退避窗口内**限时**取到锁（行为，杀「sleep 在锁内」）。

    只有 ① 不够：把 sleep 挪进同一次临界区时 enter/exit 对数不变 ⇒ 结构断言照样绿（隔离审查实证）。
    ② 必须是**限时**取锁：asyncio.Lock 的 FIFO 会把释放后的首次获取优先交给等待中的探针 ⇒「取到后看
    计数」同样杀不死该变异（实测）。限时取锁把「退避期间锁被持有」变成可观测事实。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 1.0  # 退避窗口 1.0s > 探针 0.5s 超时
    client.office_id = "officeA"
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]
    # 夹具自检（防「探针变盲」的假绿）：限时取锁只在「退避窗口 > 探针超时」时才有分辨力。
    assert PROBE_TIMEOUT < client.office_rejoin_retry_budget, "探针超时须小于退避窗口，否则断言退化成恒真"
    real_lock = client._office_op_lock  # 探针用**真锁**：两条断言各用各的仪器
    lock = _RecordingAsyncLock(real_lock)
    client._office_op_lock = lock  # type: ignore[assignment]

    task = asyncio.create_task(client._arejoin_office("officeA", 1))
    await _await_until(lambda: len(attempts) >= 1)

    probe_took_lock_at: list[int] = []

    async def probe() -> None:
        await asyncio.wait_for(real_lock.acquire(), timeout=PROBE_TIMEOUT)  # 超时 ⇒ TimeoutError ⇒ 用例红
        probe_took_lock_at.append(len(attempts))
        real_lock.release()

    await probe()
    await task

    assert probe_took_lock_at == [1], "探针须在**退避窗口内**取到锁（回放结束后才取到不算）"
    assert len(attempts) >= 2, "预算内必须重试（否则本断言退化成单次路径）"
    assert lock.events == ["enter", "exit"] * len(attempts), (
        f"每次尝试须是独立的临界区；实得 {lock.events}（尝试 {len(attempts)} 次）"
    )


@pytest.mark.asyncio
async def test_replay_declares_the_same_identity_across_attempts() -> None:
    """同一次回放的 N 次尝试必须声明**同一身份**：退避窗口内就地改名不得让同一 sid 先后声明两个名字。

    身份在一条连接内不可变（协议 events.md）：第二次尝试若重读 ``computer.name``，改名后会撞 ``403``，
    把一次本可成功的恢复打成失败。/ Identity is immutable per session, so the request is built once.
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 0.3
    client.office_id = "officeA"
    client._office_generation = 1
    seen: list[Any] = []

    async def fake_call(event: Any, data: Any = None, **kwargs: Any) -> Any:
        seen.append(data)
        if len(seen) == 1:
            client.computer.name = "renamed-mid-backoff"  # CLI 就地改名（interactive_impl）
            return dict(_ROOM_FULL)
        return None

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 1)

    assert len(seen) == 2, "须发生一次重试（否则本断言退化成单次路径）"
    assert seen[0]["name"] == seen[1]["name"] == "test_computer", "N 次尝试必须共用同一次身份声明"


@pytest.mark.asyncio
async def test_replay_superseded_during_backoff_sends_nothing_more() -> None:
    """退避期间被更新的操作抢占（generation 前进）⇒ 中止：不再发包、不得改动新意图。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 0.4
    client.office_id = "officeA"
    client._office_generation = 1
    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL)

    client.call = fake_call  # type: ignore[method-assign]

    replay = asyncio.create_task(client._arejoin_office("officeA", 1))
    await _await_until(lambda: len(attempts) >= 1)

    client._office_generation += 1
    client.office_id = "officeB"
    await replay

    assert attempts == [1], "被抢占后不得再发包"
    assert client.office_id == "officeB", "陈旧回放不得改新房号"


# ── #212 裁决 4：显式 join 的失败判据由「有没有码」收敛为「是否校验类」─────────────────


@pytest.mark.asyncio
async def test_post_commit_code_clears_desired_but_keeps_confirmed() -> None:
    """``500`` 可发生在成员关系**已提交之后** ⇒ 不得回退到旧房（会宣称仍在房），落到本侧「非校验」支。

    与本侧既有的「无码」分支同形：清 desired、**保留** ``_confirmed_office_id``（与 Agent 的「双清空」
    刻意分叉，见 ``utils/office.py`` 的 scope note）。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    client._confirmed_office_id = "officeA"
    client._office_generation = 7

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        return {"code": 500, "message": "Internal server error"}

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("officeB")

    assert client.office_id is None, "500 不得回退到旧房（可能已随提交点作废）"
    assert client._confirmed_office_id == "officeA", "本侧非校验支保留已确认记忆（与 Agent 刻意分叉）"


# ── #223：回放在途窗口内的 server:update_*（判据对齐 + 合并补发）────────────────────
#
# 协议依据（computer.md §2.2）：「Computer SHOULD 在**成功加入 Office 后**才发送 server:update_*。
# 未加入 Office 时，本地变化**可以被记录或合并**，但不应产生跨房间可见通知。」
# 回放在途窗口内 desired 仍在、namespace 已重新在册，但服务端新会话尚无 office_id ⇒ 服务端
# require_office_id 拒收，而这些事件是 fire-and-forget（无 ack）⇒ 发送端无感。故判据改为
# 「有入房意图 ∧ 服务端已确认成员关系 ∧ 在册」；窗口内变更记入待补发集合，成员关系确立
# （回放成功 / 显式入房成功）后逐条补发。
#
# 该缺陷是**活性/质量缺陷，不是合规违规**（丢包本身合规，协议还禁止给这些事件加 ack）；客户端判据
# 与协议前置不一致 + 变更未记录，才是要修的。客户端谓词也**不是隔离守卫**——广播目标由服务端会话
# 决定（events.md:27），客户端只决定发不发。

#: 四类上报（含无守卫的孪生 ``update_config``：同 payload、同判据、同补发）。
_UPDATE_EMITTERS: list[tuple[str, str]] = [
    ("emit_update_config", UPDATE_CONFIG_EVENT),
    ("update_config", UPDATE_CONFIG_EVENT),
    ("emit_update_tool_list", UPDATE_TOOL_LIST_EVENT),
    ("emit_refresh_desktop", UPDATE_DESKTOP_EVENT),
    ("emit_update_skills", UPDATE_SKILLS_EVENT),
]


def _record_emits(client: SMCPComputerClient) -> list[tuple[str, Any]]:
    """把 ``emit`` 换成记录器（返回 ``[(event, data), ...]``），避免触碰真实 socketio 连接。"""
    sent: list[tuple[str, Any]] = []

    async def fake_emit(event: str, data: Any = None, *args: Any, **kwargs: Any) -> None:
        sent.append((event, data))

    client.emit = fake_emit  # type: ignore[method-assign]
    return sent


def _confirm_office(client: SMCPComputerClient, office_id: str = "officeA") -> None:
    """构造稳定态：有入房意图 ∧ 服务端已确认成员关系 ∧ namespace 在册。"""
    client.office_id = office_id
    client._confirmed_office_id = office_id
    _mark_namespace_registered(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name,event", _UPDATE_EMITTERS)
async def test_update_emitters_send_only_when_confirmed_and_live(method_name: str, event: str) -> None:
    """**正对照**：已确认成员关系 + 在册 ⇒ 恰发一条（防「一律不发」的过度收紧）。"""
    client = _make_client(reconnection=True)
    _confirm_office(client)
    sent = _record_emits(client)

    await getattr(client, method_name)()

    assert sent == [(event, {"computer": "test_computer"})], f"{method_name} 在稳定态必须发包"
    assert client._deferred_office_updates == set(), "稳定态直接发，不留待补发"


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name,event", _UPDATE_EMITTERS)
async def test_update_emitters_do_not_send_on_desired_only(method_name: str, event: str) -> None:
    """只有 desired（服务端从未确认）⇒ 不发包 + 记入待补发 —— 回放在途窗口的主体判据。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    assert client._confirmed_office_id is None, "前置：服务端尚未确认成员关系"
    sent = _record_emits(client)

    await getattr(client, method_name)()

    assert sent == [], f"{method_name} 在「未确认在房」时不得发包（会被服务端静默丢弃）"
    assert event in client._deferred_office_updates, "窗口内的变更必须被记录，否则永久丢失"


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name,event", _UPDATE_EMITTERS)
async def test_update_emitters_do_not_send_when_namespace_unregistered_even_if_confirmed(
    method_name: str, event: str
) -> None:
    """确认过但在册已失效（连接又断）⇒ 不发包（判据的「在册」半是承重的，独立负例）。"""
    client = _make_client(reconnection=True)
    client.office_id = "officeA"
    client._confirmed_office_id = "officeA"
    assert SMCP_NAMESPACE not in client.namespaces
    sent = _record_emits(client)

    await getattr(client, method_name)()

    assert sent == [], f"{method_name} 在 namespace 不在册时不得发包"
    assert event in client._deferred_office_updates


@pytest.mark.asyncio
async def test_no_emit_or_deferral_before_the_first_join() -> None:
    """从未入房（既无意图也无确认）⇒ 不发包**也不记录**：房内无旧状态可陈旧（协议只允许记录，未要求）。"""
    client = _make_client()
    _mark_namespace_registered(client)
    assert client.office_id is None
    sent = _record_emits(client)

    await client.emit_update_config()

    assert sent == []
    assert client._deferred_office_updates == set(), "无入房意图时不该攒待补发（首次入房后对面本就会重拉）"


@pytest.mark.asyncio
async def test_no_emit_when_confirmed_survives_without_intent() -> None:
    """非校验类拒绝后的可达态（desired 已清、confirmed 保留）⇒ 不得发包（意图半不可省）。

    本态由 ``join_office`` 的非校验支产生（``500`` 可晚于成员关系提交 ⇒ 服务端会话通常已无 office_id）。
    放行会**新增**一批注定被丢的包 + 服务端 ERROR 噪音，故判据必须带意图半。
    """
    client = _make_client()
    _mark_namespace_registered(client)
    client.office_id = None
    client._confirmed_office_id = "officeA"
    sent = _record_emits(client)

    await client.emit_update_config()

    assert sent == [], "意图已清 ⇒ 不做任何上报（与今天行为一致，不得新增发包）"
    assert client._deferred_office_updates == set()


@pytest.mark.asyncio
async def test_deferral_survives_without_a_live_namespace() -> None:
    """断线窗口（已有意图、namespace 不在册）也要记录 —— 这是本缺陷最早、最广的窗口。"""
    client = _make_client(reconnection=True)
    client.office_id = "officeA"
    assert SMCP_NAMESPACE not in client.namespaces
    sent = _record_emits(client)

    await client.emit_update_skills()

    assert sent == []
    assert client._deferred_office_updates == {UPDATE_SKILLS_EVENT}


@pytest.mark.asyncio
async def test_replay_confirmation_flushes_deferred_updates() -> None:
    """回放（先被 4101 拒、退避后成功）那一刻逐条补发窗口内的变更 —— 本单的核心行为。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 0.3
    client.office_id = "officeA"
    client._office_generation = 1
    sent = _record_emits(client)

    await client.emit_update_skills()
    await client.emit_update_config()
    assert sent == [], "窗口内不得发包"
    assert client._deferred_office_updates == {UPDATE_CONFIG_EVENT, UPDATE_SKILLS_EVENT}

    attempts: list[int] = []

    async def fake_call(*args: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        return dict(_ROOM_FULL) if len(attempts) == 1 else None

    client.call = fake_call  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 1)

    assert len(attempts) == 2, "前置：回放须先被拒再成功（考的是成功那一刻的补发）"
    assert [event for event, _ in sent] == [UPDATE_CONFIG_EVENT, UPDATE_SKILLS_EVENT], (
        "成员关系确立后按事件名序逐条补发"
    )
    assert all(data == {"computer": "test_computer"} for _, data in sent)
    assert client._deferred_office_updates == set(), "补发成功后条目必须移除"


@pytest.mark.asyncio
async def test_explicit_join_confirmation_flushes_deferred_updates() -> None:
    """显式入房成功同样补发，且补发发生在**落账之后**（emit 时已能读到新房号）。"""
    client = _make_client()
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    await client.emit_update_config()
    assert client._deferred_office_updates == {UPDATE_CONFIG_EVENT}

    seen: list[tuple[str, str | None]] = []

    async def fake_emit(event: str, data: Any = None, *args: Any, **kwargs: Any) -> None:
        seen.append((event, client._confirmed_office_id))

    client.emit = fake_emit  # type: ignore[method-assign]

    async def accept(*args: Any, **kwargs: Any) -> Any:
        return None

    client.call = accept  # type: ignore[method-assign]

    await client.join_office("officeB")

    assert seen == [(UPDATE_CONFIG_EVENT, "officeB")], "补发须在 confirmed 落账之后发生"
    assert client._deferred_office_updates == set()


@pytest.mark.asyncio
async def test_flush_does_not_resend_after_success() -> None:
    """补发成功后条目必须移除：重复补发会放大对面的重拉（虽然幂等，但没必要）。"""
    client = _make_client()
    _confirm_office(client)
    client._deferred_office_updates.add(UPDATE_CONFIG_EVENT)
    sent = _record_emits(client)

    await client._flush_deferred_office_updates()
    await client._flush_deferred_office_updates()

    assert [event for event, _ in sent] == [UPDATE_CONFIG_EVENT]


@pytest.mark.asyncio
async def test_flush_rechecks_the_predicate_before_each_entry() -> None:
    """逐条复检判据：第一条发出后连接即断 ⇒ 后续条目不得再发（否则又是一批发给服务端被丢的包）。"""
    client = _make_client(reconnection=True)
    _confirm_office(client)
    client._deferred_office_updates.update({UPDATE_CONFIG_EVENT, UPDATE_SKILLS_EVENT})
    sent: list[str] = []

    async def fake_emit(event: str, data: Any = None, *args: Any, **kwargs: Any) -> None:
        sent.append(event)
        client._on_namespace_disconnect(_TRANSPORT_ERROR)  # 首条后连接即断（成员关系随会话作废）

    client.emit = fake_emit  # type: ignore[method-assign]

    await client._flush_deferred_office_updates()

    assert sent == [UPDATE_CONFIG_EVENT], "断连后不得继续补发"
    assert client._deferred_office_updates == {UPDATE_SKILLS_EVENT}, "未发出的条目留待下次确立"


@pytest.mark.asyncio
async def test_replay_flush_runs_outside_the_op_lock() -> None:
    """补发在 office 操作锁**之外**（补发含 await emit，不得占着锁 —— #222 的「状态锁不跨 RPC」同款）。

    探针必须是**限时取锁**：补发此刻正阻塞在 ``emit`` 里，若实现把它挪进临界区或长屏障，这里会超时。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_rejoin_retry_budget = 0.0  # 单次尝试：窗口里没有退避噪声
    client.office_id = "officeA"
    client._office_generation = 1
    await client.emit_update_config()  # 窗口内变更 ⇒ 记入待补发（谓词假，不发包）
    assert client._deferred_office_updates == {UPDATE_CONFIG_EVENT}

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_emit(event: str, data: Any = None, *args: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()

    client.emit = blocking_emit  # type: ignore[method-assign]

    async def succeed(*args: Any, **kwargs: Any) -> Any:
        return None

    client.call = succeed  # type: ignore[method-assign]
    real_lock = client._office_op_lock
    lock = _RecordingAsyncLock(real_lock)
    client._office_op_lock = lock  # type: ignore[assignment]

    replay = asyncio.create_task(client._arejoin_office("officeA", 1))
    try:
        await asyncio.wait_for(entered.wait(), timeout=PROBE_TIMEOUT)  # 补发已在飞（阻塞在 emit 内）
        await asyncio.wait_for(real_lock.acquire(), timeout=PROBE_TIMEOUT)  # 超时 ⇒ 补发占了锁 ⇒ 红
        real_lock.release()
    finally:
        release.set()
        await replay

    assert lock.events == ["enter", "exit"], f"回放只取一次锁，且补发全程在锁外；实得 {lock.events}"


@pytest.mark.asyncio
async def test_flush_retains_the_failed_entry_and_continues_the_others(caplog: pytest.LogCaptureFixture) -> None:
    """一条失败不得饿死其余类别（队首阻塞），失败条目保留待下次；ERROR 只由补发方产出。"""
    client = _make_client()
    _confirm_office(client)
    client._deferred_office_updates.update({UPDATE_CONFIG_EVENT, UPDATE_SKILLS_EVENT})
    sent: list[str] = []

    async def fake_emit(event: str, data: Any = None, *args: Any, **kwargs: Any) -> None:
        if event == UPDATE_CONFIG_EVENT:
            raise RuntimeError("transport broke")
        sent.append(event)

    client.emit = fake_emit  # type: ignore[method-assign]

    computer_client_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR)
    try:
        await client._flush_deferred_office_updates()  # 不得抛出
    finally:
        computer_client_mod.logger.removeHandler(caplog.handler)

    assert sent == [UPDATE_SKILLS_EVENT], "config 失败后仍须尝试 skills"
    assert client._deferred_office_updates == {UPDATE_CONFIG_EVENT}
    assert any(r.levelno == logging.ERROR for r in caplog.records), "失败须有 ERROR 记录（唯一产出者在这里）"


@pytest.mark.asyncio
async def test_flush_failure_does_not_escape_the_replay_task() -> None:
    """补发异常不得逃出回放任务（该任务无人 await ⇒ 会变成没人取回的 Task 异常）。"""
    client = _make_client(reconnection=True)
    _confirm_office(client)
    client._office_generation = 1
    client.office_rejoin_retry_budget = 0.0
    client._deferred_office_updates.add(UPDATE_CONFIG_EVENT)

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("transport broke")

    client.emit = explode  # type: ignore[method-assign]

    async def succeed(*args: Any, **kwargs: Any) -> Any:
        return None

    client.call = succeed  # type: ignore[method-assign]

    await client._arejoin_office("officeA", 1)  # 不得抛出

    assert client._deferred_office_updates == {UPDATE_CONFIG_EVENT}, "未送达的条目保留"


@pytest.mark.asyncio
async def test_flush_does_not_swallow_cancellation() -> None:
    """取消必须照旧透传（补发只捕 ``Exception``）：停机收敛不得被吞掉。"""
    client = _make_client()
    _confirm_office(client)
    client._deferred_office_updates.add(UPDATE_CONFIG_EVENT)
    entered = asyncio.Event()

    async def blocking_emit(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    client.emit = blocking_emit  # type: ignore[method-assign]

    task = asyncio.create_task(client._flush_deferred_office_updates())
    await asyncio.wait_for(entered.wait(), timeout=PROBE_TIMEOUT)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client._deferred_office_updates == {UPDATE_CONFIG_EVENT}, "取消（未送达）的条目保留"


@pytest.mark.asyncio
async def test_deferred_set_is_not_cleared_by_leave() -> None:
    """退房不清待补发：下一次入房补发对对面无害且有益（新房的 Agent 也不会主动重拉 skills/desktop）。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    await client.emit_update_skills()
    assert client._deferred_office_updates == {UPDATE_SKILLS_EVENT}

    sent = _record_emits(client)
    await client.leave_office("officeA")

    assert [event for event, _ in sent] == [LEAVE_OFFICE_EVENT]
    assert client.office_id is None
    assert client._deferred_office_updates == {UPDATE_SKILLS_EVENT}, "退房不清（集合域恒为 4 个事件名，不会膨胀）"


@pytest.mark.asyncio
async def test_leave_office_without_a_live_namespace_clears_intent_and_does_not_raise() -> None:
    """#223：断线（namespace 不在册）时退房 ⇒ 不得抛 ``BadNamespaceError``，且本地意图**必须**被清掉。

    此前是「先 emit 后清」：``emit`` 抛异常 ⇒ 清理语句根本执行不到 ⇒ 用户（或 CLI ``socket leave``）在
    断线期间退的房仍在本地意图里，重连后自动回房把刚退的房又回了一遍。无连接时服务端会话已随连接销毁
    （没有可退的成员关系），故不发包即可。
    """
    client = _make_client(reconnection=True)
    client.office_id = "officeA"
    client._confirmed_office_id = "officeA"
    client._office_generation = 3
    stale_replay = asyncio.create_task(asyncio.sleep(60))
    client._office_rejoin_task = stale_replay
    sent = _record_emits(client)
    assert SMCP_NAMESPACE not in client.namespaces

    await client.leave_office("officeA")  # 不得抛

    assert sent == [], "无连接时不发包（服务端会话已随连接销毁）"
    assert client.office_id is None, "本地意图必须清掉（否则重连后又会被自动拉回）"
    assert client._confirmed_office_id is None
    assert client._office_rejoin_task is None, "在途回房必须被作废"
    assert client._office_generation == 4, "退房推进世代（作废在途操作的结果）"

    stale_replay.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale_replay


@pytest.mark.asyncio
async def test_leave_office_during_inflight_replay_still_notifies_server() -> None:
    """在途窗口内退房 ⇒ 仍须发 ``server:leave_office``（CLI 的 ``socket leave`` 就活在这个窗口里）。

    窗口内 namespace **已在册**（否则 CLI 走的是「未连接」分支），故「不在册才跳过」的短路不会误伤它。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    assert client._confirmed_office_id is None, "前置：回放在途 ⇒ 服务端尚未确认"
    sent = _record_emits(client)

    await client.leave_office("officeA")

    assert [event for event, _ in sent] == [LEAVE_OFFICE_EVENT], "在途窗口退房必须通知服务端"
    assert client.office_id is None


# ── #223 隔离审查：幻影「已确认房」的两个可达路径（回归锁）─────────────────────────────
#
# 背景：``_confirmed_office_id`` 的成功落账**刻意不受 supersession 守卫约束**（#213：成功是关于服务端
# 事实的陈述）。该豁免成立的**前提**是「本任务必是 ``_office_rejoin_task``」——每条抢占路径都在同一同步段
# 里先 ``_cancel_office_rejoin()`` 再改状态，被抢占的回放在 ``await call`` 处即收到 ``CancelledError``
# （BaseException，逃过成功/失败两个 ``except``），走不到落账行。
#   · 回放路径：靠上面的**取消**成立（下面第一条用例锁住它）；
#   · 显式 join 路径：**没有**可取消的任务（join 不是你……不是 ``_office_rejoin_task``）⇒ 它的落账会落在
#     退房清账**之后** ⇒ 必须由 ``leave_office``「退房是最后一句陈述」的**临界区后再清一次**兜住
#     （下面第二条用例锁住它）。
# 幻影态的后果正是 #223 的缺陷形态：服务端会话已无房，而本地 ``_can_emit_office_update()`` 为真 ⇒
# 上报白发一批（被 ``require_office_id`` 丢弃）且**不记入待补发** ⇒ 变更真实丢失。


@pytest.mark.asyncio
async def test_leave_during_inflight_replay_leaves_no_phantom_confirmed() -> None:
    """退房抢占「已持锁、正等 ACK」的**自动回房** ⇒ 落定后 ``confirmed`` 必须为 None（取消使然）。

    生产形态：回放任务登记在 ``_office_rejoin_task`` 上（``_on_namespace_connect`` 的唯一赋值点），
    故 ``leave_office`` 的 ``_cancel_office_rejoin()`` 能真正取消它 ⇒ 陈旧成功写不进去。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    client._confirmed_office_id = "officeA"
    client._office_generation = 1
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_call(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return None  # 服务端 ACCEPT（空 ack）——若未被取消，成功落账会把 officeA 写回

    client.call = blocking_call  # type: ignore[method-assign]
    replay = asyncio.create_task(client._arejoin_office("officeA", 1))
    client._office_rejoin_task = replay  # 生产形态：connect 钩子登记在途回放
    await asyncio.wait_for(entered.wait(), timeout=PROBE_TIMEOUT)

    sent = _record_emits(client)
    leave = asyncio.create_task(client.leave_office("officeA"))
    await asyncio.sleep(0)  # 让退房走到「等锁」（此刻回放仍持锁等 ACK）
    assert client.office_id is None, "前置：退房已清本地意图"
    release.set()
    await asyncio.gather(replay, leave, return_exceptions=True)

    assert [event for event, _ in sent] == [LEAVE_OFFICE_EVENT]
    # **机制断言**（终态断言会失明：临界区后那次再清同样兜得住这条路径）——被抢占的回放必须真的被取消，
    # 即 `:579` 那条「不受 supersession 约束的成功落账」所依赖的前提不变量。
    assert replay.cancelled(), "退房必须取消在途回放（否则陈旧成功会写回已退掉的房）"
    assert client._confirmed_office_id is None, (
        "退房是最后一句陈述：被抢占的在途回放不得把已退掉的房写回已确认房号（否则 #223 判据会放行注定被丢的上报）"
    )
    assert not client._can_emit_office_update()


@pytest.mark.asyncio
async def test_leave_while_explicit_join_awaits_ack_leaves_no_phantom_confirmed() -> None:
    """退房抢占「等 ACK 中的**显式** join」⇒ 落定后 ``confirmed`` 必须为 None（临界区后再清一次使然）。

    显式 ``join_office`` 的落账在**锁外**（且它不是 ``_office_rejoin_task`` ⇒ 取消链管不到它）：退房只等
    锁，等它释放锁后它才落账 ⇒ 线序是 JOIN→LEAVE（服务端最后为**无房**）而本地会留下「已确认在房」的
    幻影。修法是 ``leave_office`` 在临界区之后再清一次——退房比任何在途操作都更新。
    """
    client = _make_client()
    _mark_namespace_registered(client)
    client._office_generation = 1
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_call(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return None  # 服务端 ACCEPT（空 ack）

    client.call = blocking_call  # type: ignore[method-assign]
    sent = _record_emits(client)
    join = asyncio.create_task(client.join_office("officeA"))
    await asyncio.wait_for(entered.wait(), timeout=PROBE_TIMEOUT)

    leave = asyncio.create_task(client.leave_office("officeA"))
    await asyncio.sleep(0)  # 让退房走到「等锁」（此刻 join 仍持锁等 ACK）
    assert client.office_id is None, "前置：退房已清本地意图（join 尚未落账）"
    release.set()
    await asyncio.gather(join, leave, return_exceptions=True)

    assert [event for event, _ in sent] == [LEAVE_OFFICE_EVENT]
    assert client._confirmed_office_id is None, "在途 join 的成功落账不得复活已退掉的房"
    assert not client._can_emit_office_update()


@pytest.mark.asyncio
async def test_join_success_after_a_session_boundary_is_not_recorded() -> None:
    """#223：显式 ``join_office`` 的 ACK 在**会话作废之后**才落账 ⇒ 不得写回 ``_confirmed_office_id``。

    断连钩子是**内联同步**触发的（engineio 特性，见 client 的 hooks 注释）⇒「ACK 已就绪、join 任务尚未
    恢复」与「钩子已清账」可以同拍发生。成员关系属于会话 ⇒ 那条「成功」陈述的是一个**已销毁**的会话，
    落账会造出幻影态：服务端会话无房，而 #223 的上报判据为真 ⇒ 发包被丢弃且不记待补发（变更永久丢失）。
    判据必须是**会话纪元**：``generation`` 兼顾不了——它同时被用户操作推进，用它守卫会连「同一会话内被
    抢占的成功」（#213 明令必须落账）一起否掉。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client._office_generation = 1
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_call(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return None  # 服务端 ACCEPT（空 ack）

    client.call = blocking_call  # type: ignore[method-assign]
    join = asyncio.create_task(client.join_office("officeA"))
    await asyncio.wait_for(entered.wait(), timeout=PROBE_TIMEOUT)

    client._on_namespace_disconnect(_TRANSPORT_ERROR)  # 会话作废（传输中断、会自动重连 ⇒ desired 保留）
    assert client._confirmed_office_id is None, "前置：钩子已按「成员关系属于会话」清账"

    release.set()
    await asyncio.gather(join, return_exceptions=True)

    assert client._confirmed_office_id is None, "跨会话的成功不得落账（否则上报判据会放行注定被丢的包）"
    assert not client._can_emit_office_update(), "判据不得在「服务端无房」时为真"
    sent = _record_emits(client)
    await client.emit_update_config()
    assert sent == [], "幻影态下不得发包"
    assert client._deferred_office_updates == {UPDATE_CONFIG_EVENT}, (
        "窗口内的变更必须被记录（回房成功后补发）——这正是本单要保住的语义"
    )


@pytest.mark.asyncio
async def test_leave_tail_clear_runs_even_when_the_notification_fails() -> None:
    """退房通知失败（等锁期间断线 ⇒ ``emit`` 抛）时，「退房是最后一句陈述」的清理**不得被跳过**。

    尾清若放在 ``async with`` 之后的裸语句里，异常会把它整段跳过 ⇒ 在途 join 的落账留存 ⇒ 幻影态
    （同 ``test_leave_while_explicit_join_awaits_ack_leaves_no_phantom_confirmed``）。异常照旧透传，不吞。
    """
    client = _make_client()
    _mark_namespace_registered(client)
    client._office_generation = 1
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_call(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return None

    client.call = blocking_call  # type: ignore[method-assign]
    join = asyncio.create_task(client.join_office("officeA"))
    await asyncio.wait_for(entered.wait(), timeout=PROBE_TIMEOUT)

    async def failing_emit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("namespace is not a connected namespace.")

    client.emit = failing_emit  # type: ignore[method-assign]
    leave = asyncio.create_task(client.leave_office("officeA"))
    await asyncio.sleep(0)  # 让退房走到「等锁」
    assert client.office_id is None, "前置：退房已清本地意图"
    release.set()  # join 在锁外落账（若尾清被跳过就留存）
    results = await asyncio.gather(join, leave, return_exceptions=True)

    assert isinstance(results[1], RuntimeError), f"退房通知失败必须透传（不得吞）：{results!r}"
    assert client._confirmed_office_id is None, "通知失败不得让「退房是最后一句陈述」失守"
    assert not client._can_emit_office_update()


@pytest.mark.asyncio
async def test_replay_success_after_a_session_boundary_is_not_recorded() -> None:
    """**纵深防御用例（人为破坏不变量）**：回放的成功落账同样受**会话纪元**约束。

    生产上本状态**不可达**：``_arejoin_office`` 的唯一创建点（``_on_namespace_connect``）必然把任务登记到
    ``_office_rejoin_task``，而每条抢占路径都在同一同步段里先取消它 ⇒ 被抢占的回放在 ``await call`` 处即收
    到 ``CancelledError``（见 ``test_leave_during_inflight_replay_leaves_no_phantom_confirmed``）。本用例
    **刻意不登记**该任务（绕过取消链）以钉住守卫本身——否则落账处那句「即便该不变量被破坏，会话纪元也会挡住
    跨会话那一半」只是散文（隔离审查实测：删掉该守卫时 0 个用例变红）。它不声称该状态可达，只声称守卫有效。
    """
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
    client.office_id = "officeA"
    client._office_generation = 1
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_call(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return None  # 服务端 ACCEPT（空 ack）

    client.call = blocking_call  # type: ignore[method-assign]
    replay = asyncio.create_task(client._arejoin_office("officeA", 1))
    # 刻意**不**写 client._office_rejoin_task —— 人为破坏「在途回放必被登记」的不变量
    await asyncio.wait_for(entered.wait(), timeout=PROBE_TIMEOUT)

    client._on_namespace_disconnect(_TRANSPORT_ERROR)  # 会话作废并推进纪元
    release.set()
    await asyncio.gather(replay, return_exceptions=True)

    assert client._confirmed_office_id is None, "跨会话的成功落账必须被会话纪元否掉（纵深防御）"
    assert not client._can_emit_office_update()
