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
    """回房被拒且 generation 仍新鲜（无人接管）→ 清空 office_id（状态不得撒谎）。"""
    client = _make_client(reconnection=True)
    _mark_namespace_registered(client)
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
