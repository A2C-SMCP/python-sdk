# -*- coding: utf-8 -*-
"""
#203 自动重连后恢复 Office 成员关系（Computer 侧，真实 wire）。

Socket.IO 房间成员关系属于**会话**：传输层断线自动重连后 namespace 换新 SID，服务端
``on_disconnect`` 已销毁旧会话的房间成员关系（``server/base.py:87-109``）。本文件用真实 ASGI
Socket.IO 服务端 + 真实 ``SMCPComputerClient`` 验证：自动重连完成后客户端**重放**
``server:join_office``，重新成为该 Office 的成员；无法恢复时必须清空本地 ``office_id``，
不得保留表面有效的旧值。

Real-wire proof for #203: Socket.IO room membership is **session-scoped**; after an
auto-reconnect the namespace gets a fresh SID while the old session's membership is gone.
These tests drive a real ASGI Socket.IO server plus a real ``SMCPComputerClient`` and assert
that (a) the client replays ``server:join_office`` on reconnect so the new SID is back in the
room, and (b) when the replay cannot succeed the local ``office_id`` is cleared instead of
being kept as a stale, plausible-looking value.

断线模拟选型见 ``test_auth_provider_reconnect.py`` 的文件头说明：服务端主动 CLOSE 会让
socketio ``will_reconnect=False``（不触发自动重连），只有传输层突然断开才走 ``TRANSPORT_ERROR``
→ 自动重连路径。/ Drop simulation rationale: see ``test_auth_provider_reconnect.py``.

断言口径（关键）/ Assertion contract (critical): 核心用例断言的是**服务端**收到了新 SID 的
``server:join_office``（跨 SID 的 append 记录），而非仅客户端 ``office_id`` 仍为旧值——后者在
"重连后根本没回房"时也会通过（假绿）。/ The core test asserts the **server** saw the replay from
the new SID; asserting only the client-side ``office_id`` would pass even when the replay never
happened.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from socketio import ASGIApp, AsyncClient, AsyncServer

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, SMCP_NAMESPACE, UPDATE_CONFIG_NOTIFICATION
from a2c_smcp.testing import UvicornTestServer
from tests.integration_tests.mock_socketio_server import MockComputerServerNamespace

_CONNECT_TIMEOUT = 10.0
_WAIT_INTERVAL = 0.01
# 负例的静默窗口：新 SID 建立后再等这么久仍无 join，才可判定"没有自动回房"
# Quiescence window for negative cases: a replay (if wrongly implemented) lands well within it.
_QUIESCENCE = 0.3


class _OfficeRecordingNamespace(MockComputerServerNamespace):
    """按到达顺序记录 ``server:join_office``（跨 SID，故用 append 而非 sid 覆盖写）。

    Records ``server:join_office`` in arrival order across SIDs (the shared mock records by SID,
    which overwrites and therefore cannot count replays after a reconnect).
    """

    def __init__(self) -> None:
        super().__init__()
        self.join_record: list[tuple[str, dict]] = []
        # 每次 join 的服务端裁决（含被拒原因），供"真实服务端拒绝路径"用例断言
        # Per-join server verdict, so the real-rejection case can assert on the genuine ack.
        # #214：成功 = None（空 ack）；失败 = flat ErrorPayload（顶层含 code）
        self.join_results: list[Any] = []
        self.joined = asyncio.Event()
        # 自第 N 次（1-based）join 起一律拒绝，复现「旧会话尚未回收」的瞬态拒绝
        # Reject from the Nth (1-based) join onwards — reproduces the transient rejection window
        # where the server has not yet reaped the stale session.
        self.reject_from: int | None = None
        # 置位后：服务端**推迟回收旧会话**（模拟静默断线——真实服务端要等 ping 超时才会 on_disconnect），
        # 让重放的 join 撞上真实的同名冲突检查。
        # When set, the server defers reaping the stale session (as it does on a silent drop until
        # its ping timeout), so the replay hits the real duplicate-name check.
        self.stall_disconnect: asyncio.Event | None = None
        # #223：按到达顺序的**事件序日志**（``("join", sid)`` 成功重放 / ``("join-rejected", sid)`` /
        # ``("update_config", sid)``）——用于断言「补发发生在成功重放**之后**」，比两个计数各自断言强。
        # Arrival-ordered log so the flush can be tied to "after the successful replay".
        self.event_log: list[tuple[str, str]] = []

    async def on_disconnect(self, sid: str) -> None:
        if self.stall_disconnect is not None:
            await self.stall_disconnect.wait()
        await super().on_disconnect(sid)

    async def on_server_join_office(self, sid: str, data):  # type: ignore[override]
        if self.reject_from is not None and len(self.join_record) + 1 >= self.reject_from:
            payload = dict(data)
            self.join_record.append((sid, payload))
            self.join_results.append(
                {"code": 4105, "message": "Name already taken in room"},
            )
            self.event_log.append(("join-rejected", sid))
            self.joined.set()
            return self.join_results[-1]
        result = await super().on_server_join_office(sid, data)
        self.join_record.append((sid, dict(data)))
        self.event_log.append(("join", sid))
        # v0.5.0（#214）：成功 = 空 ack（None）；失败 = flat ErrorPayload
        self.join_results.append(result)
        self.joined.set()
        return result

    async def on_server_update_config(self, sid: str, data):  # type: ignore[override]
        """#223：记录到达（含被服务端丢弃的那些）后交给真实 handler（房间广播/拒收都在父类里）。"""
        self.event_log.append(("update_config", sid))
        return await super().on_server_update_config(sid, data)


@pytest.fixture
async def office_server(basic_server_port: int) -> AsyncGenerator[_OfficeRecordingNamespace, None]:
    """真实 ASGI Socket.IO 服务端（记录 join 的命名空间）/ real ASGI server with the join-recording namespace."""
    sio = AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        ping_timeout=10,
        ping_interval=10,
        async_handlers=True,
    )
    sio.eio.start_service_task = False
    ns = _OfficeRecordingNamespace()
    sio.register_namespace(ns)
    asgi_app = ASGIApp(sio, socketio_path="/socket.io")
    server = UvicornTestServer(asgi_app, port=basic_server_port)
    await server.up()
    try:
        yield ns
    finally:
        await server.down(force=True)


def _make_client() -> SMCPComputerClient:
    """构造测试用客户端（收紧重连节奏以提速，语义不变）/ build the client under test."""
    computer = MagicMock(spec=Computer)
    computer.name = "office-rejoin-computer"
    return SMCPComputerClient(
        computer=computer,
        reconnection=True,
        reconnection_attempts=10,
        reconnection_delay=0.1,
        reconnection_delay_max=0.3,
    )


async def _wait_for_new_sid(client: SMCPComputerClient, first_sid: str) -> str:
    """轮询等服务端 ACK 重建 namespace 并返回新 SID / poll until the namespace is re-established."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CONNECT_TIMEOUT
    while True:
        sid = client.namespaces.get(SMCP_NAMESPACE)
        if sid is not None and sid != first_sid:
            return sid
        if loop.time() > deadline:
            raise AssertionError("重连后命名空间未在期限内重建 / namespace not re-established after reconnect")
        await asyncio.sleep(_WAIT_INTERVAL)


@pytest.mark.asyncio
async def test_office_rejoined_after_auto_reconnect(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """断线自动重连后，新 SID 必须重新成为 Office 成员（服务端可见）。

    English: after an auto-reconnect the new SID must be back in the Office, observable server-side.
    """
    client = _make_client()
    try:
        await client.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        first_sid = client.namespaces[SMCP_NAMESPACE]
        await client.join_office("rejoin-office")
        assert len(office_server.join_record) == 1
        assert office_server.join_record[0][0] == first_sid
        assert client.office_id == "rejoin-office"

        # 掐断底层 WebSocket（不发 CLOSE 包）→ TRANSPORT_ERROR → 自动重连
        office_server.joined.clear()
        ws = client.eio.ws
        assert ws is not None
        await ws.close()
        new_sid = await _wait_for_new_sid(client, first_sid)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)

        # 服务端必须看到**新 SID** 的 join（客户端 office_id 单独断言会假绿）
        assert len(office_server.join_record) == 2, f"重连后未重放 join：{office_server.join_record}"
        sid, payload = office_server.join_record[-1]
        assert sid == new_sid, "重放的 join 必须来自重连后的新 SID"
        assert payload["office_id"] == "rejoin-office"
        assert payload["role"] == "computer"
        assert payload["name"] == "office-rejoin-computer"
        assert client.office_id == "rejoin-office"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_rejoin_rejected_by_real_server_clears_office_state(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """**真实服务端**拒绝路径：旧会话未回收 → 同名冲突 → 退避重试到**预算耗尽**后清空状态。

    与 ``reject_from`` 合成开关不同，本用例让服务端**推迟回收旧会话**（静默断线时服务端要等 ping
    超时才 ``on_disconnect``），使重放的 join 真实撞上 ``enter_room`` 的同名检查——覆盖生产主线上
    真实的 ack 文案与清空行为。

    English: the genuine server-side rejection path — the stale session is not yet reaped, so the
    replay hits ``enter_room``'s duplicate-name check and comes back with the real ack text.
    """
    client = _make_client()
    # #212：预算压到极小 ⇒ 真实 4105 上重试几次后立即认输（默认 45s 会让本用例白跑满预算）。
    client.office_rejoin_retry_budget = 0.3
    try:
        await client.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        first_sid = client.namespaces[SMCP_NAMESPACE]
        await client.join_office("rejoin-office")
        assert client.office_id == "rejoin-office"

        # 令服务端延迟回收旧会话（模拟静默断线的 ping 超时窗口）
        office_server.stall_disconnect = asyncio.Event()
        office_server.joined.clear()
        ws = client.eio.ws
        assert ws is not None
        await ws.close()

        await _wait_for_new_sid(client, first_sid)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)

        ack = office_server.join_results[-1]
        assert isinstance(ack, dict) and ack["code"] == 4105, "旧会话未回收时，真实服务端应拒绝同名重放"
        assert ack["message"] == "Name already taken in room", f"应为协议规范文案（不再含对端 sid），实际：{ack!r}"

        # 状态必须清空（不得保留表面有效的旧值）——#212 起清空发生在**预算耗尽**之后
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CONNECT_TIMEOUT
        while client.office_id is not None:
            if loop.time() > deadline:
                raise AssertionError("真实拒绝后 office_id 未清空 / office_id not cleared after a real rejection")
            await asyncio.sleep(_WAIT_INTERVAL)
        # #212：真实 4105 是瞬态冲突 ⇒ 预算内必须重试（行为变更：此前一次即终）
        assert len(office_server.join_record) >= 3, "瞬态冲突必须重试（至少多打一次）"
    finally:
        if office_server.stall_disconnect is not None:
            office_server.stall_disconnect.set()  # 放行旧会话回收，避免拖住 teardown
        await client.disconnect()


@pytest.mark.asyncio
async def test_rejoin_self_heals_once_the_server_reclaims_the_session(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """**本单的目标形态**（真服务端闸门 + 真回收窗口）：退避重试撞上回收完成 ⇒ 自动回房、无需人工。

    本用例是全库里**唯一**用真闸门 + 真回收窗口兑现该路径的一条：``stall_disconnect`` 推迟真实
    ``on_disconnect`` ⇒ 重放真的撞上 ``enter_room`` 的同名检查；放行后旧会话真的被回收 ⇒ 重试真的入房。
    Agent 侧两条自愈用例的「拒绝」出自替身开关，不构成真闸门证据。

    与前一条用例同一装置，只是**放行**被推迟的 ``on_disconnect``：服务端回收旧会话后，下一次重试
    真的入房成功 ⇒ ``office_id`` 保持、``_confirmed_office_id`` 落账、不落 ERROR。
    """
    client = _make_client()
    client.office_rejoin_retry_budget = 5.0  # 预算充足：本用例要的是「重试后成功」，不是预算耗尽
    try:
        await client.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        first_sid = client.namespaces[SMCP_NAMESPACE]
        await client.join_office("rejoin-office")
        assert client.office_id == "rejoin-office"

        office_server.stall_disconnect = asyncio.Event()
        office_server.joined.clear()
        ws = client.eio.ws
        assert ws is not None
        await ws.close()

        await _wait_for_new_sid(client, first_sid)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)
        # 前置断言用 `>=`：退避窗口内计数不再静止（同 Agent 侧）。
        assert len(office_server.join_record) >= 2, "前置：重放确已被真服务端拒一次"

        # 服务端完成回收：放行被推迟的 on_disconnect ⇒ 房内不再有旧会话 ⇒ 下次重试应成功
        office_server.stall_disconnect.set()
        office_server.joined.clear()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CONNECT_TIMEOUT
        while client._confirmed_office_id != "rejoin-office":
            if loop.time() > deadline:
                raise AssertionError("重试未自愈 / the retry did not restore the office membership")
            await asyncio.sleep(_WAIT_INTERVAL)

        assert client.office_id == "rejoin-office", "自愈后 desired 保持"
        assert len(office_server.join_record) >= 3, "自愈必须靠一次真实重放"
        assert office_server.join_record[-1][0] == client.namespaces[SMCP_NAMESPACE], "重放须来自当前 SID"
    finally:
        if office_server.stall_disconnect is not None:
            office_server.stall_disconnect.set()
        await client.disconnect()


@pytest.mark.asyncio
async def test_rejected_rejoin_clears_office_state(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """回房被拒（旧会话未回收等瞬态）时，本地成员状态必须清空，不得保留表面有效的旧值。

    English: when the replay is rejected, the local membership state must be cleared.
    """
    office_server.reject_from = 2  # 第二次（重连后）的 join 一律拒绝
    client = _make_client()
    client.office_rejoin_retry_budget = 0.3  # #212：极小预算 ⇒ 重试几次后立即认输
    try:
        await client.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        first_sid = client.namespaces[SMCP_NAMESPACE]
        await client.join_office("rejoin-office")
        assert client.office_id == "rejoin-office"

        office_server.joined.clear()
        ws = client.eio.ws
        assert ws is not None
        await ws.close()
        await _wait_for_new_sid(client, first_sid)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)

        # 确实尝试过重放（被拒），且客户端状态回到"未加入"
        assert len(office_server.join_record) >= 2  # `>=`：退避窗口内计数不再静止（#212）
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CONNECT_TIMEOUT
        while client.office_id is not None:
            if loop.time() > deadline:
                raise AssertionError("回房被拒后 office_id 未清空 / office_id not cleared after a rejected replay")
            await asyncio.sleep(_WAIT_INTERVAL)

        # #212：瞬态冲突（4105）在预算内必须重试（行为变更：此前一次即终）
        assert len(office_server.join_record) >= 3, "瞬态冲突必须重试（至少多打一次）"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_manual_disconnect_drops_membership_intent(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """手工断开清空回房意图：再次 connect 不得静默回旧房间。

    English: a manual disconnect clears the membership intent — a later connect must not silently
    rejoin the previous office.
    """
    client = _make_client()
    try:
        await client.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        await client.join_office("rejoin-office")
        assert len(office_server.join_record) == 1

        await client.disconnect()
        assert client.office_id is None, "手工断开后 office_id 必须清空"

        # 重新连接：不得自动回房（office_id 已是 None ⇒ 无 desired）
        await client.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CONNECT_TIMEOUT
        while SMCP_NAMESPACE not in client.namespaces:
            if loop.time() > deadline:
                raise AssertionError("重新连接后命名空间未建立 / namespace not established on reconnect")
            await asyncio.sleep(_WAIT_INTERVAL)
        await asyncio.sleep(_QUIESCENCE)
        assert len(office_server.join_record) == 1, "手工断开后的连接不得自动回房"
        assert client.office_id is None
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_leave_office_then_reconnect_does_not_rejoin(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """显式 leave 后断线重连不回房（leave 已清空意图）。

    English: an explicit leave clears the intent, so a later auto-reconnect must not rejoin.
    """
    client = _make_client()
    try:
        await client.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        first_sid = client.namespaces[SMCP_NAMESPACE]
        await client.join_office("rejoin-office")
        await client.leave_office("rejoin-office")
        assert client.office_id is None

        ws = client.eio.ws
        assert ws is not None
        await ws.close()
        await _wait_for_new_sid(client, first_sid)
        await asyncio.sleep(_QUIESCENCE)

        assert len(office_server.join_record) == 1, "显式 leave 后重连不得回房"
        assert client.office_id is None
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_deferred_config_update_is_flushed_after_the_replay_succeeds(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """#223：回放在途窗口内 ``emit_update_config`` **不发包**；回房成功后**恰补发一次**，房内真收到广播。

    协议依据（computer.md §2.2）：Computer SHOULD 在**成功加入 Office 后**才发送 ``server:update_*``；
    未加入 Office 时「本地变化可以被记录或合并，但不应产生跨房间可见通知」。窗口内发包会被服务端
    ``require_office_id`` 拒收——而这些事件是 fire-and-forget（协议禁止为其新增 ack）⇒ 发送端**无感**。

    本用例在**真 wire** 上同时钉住两半：

      ① 窗口内：服务端**零** ``server:update_config``、房内**零** ``notify:update_config``（不白发包）；
      ② 回房成功后：**恰一包** + 房内 Agent **恰一条** ``notify:update_config``，且**发生在成功重放之后**
         （事件序日志断言，防「早于 / 绕过回放」的坏实现同样满足计数）。

    真闸门：``stall_disconnect`` 推迟真实 ``on_disconnect`` ⇒ 重放真的撞 ``enter_room`` 的同名检查。
    """
    office_id = "rejoin-office"
    observer = AsyncClient()
    client = _make_client()
    client.office_rejoin_retry_budget = 5.0  # 预算充足：本用例要的是「重试后成功并补发」
    received: list[dict] = []

    @observer.on(UPDATE_CONFIG_NOTIFICATION, namespace=SMCP_NAMESPACE)
    async def _on_notify(data: dict) -> None:
        received.append(data)

    try:
        # 房内观察者（Agent 角色）：先入场，断线重连期间它一直留在房里
        await observer.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        await observer.call(
            JOIN_OFFICE_EVENT,
            {"office_id": office_id, "role": "agent", "name": "observer-agent"},
            namespace=SMCP_NAMESPACE,
        )

        await client.connect(
            f"http://localhost:{basic_server_port}",
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        first_sid = client.namespaces[SMCP_NAMESPACE]
        await client.join_office(office_id)
        assert client._confirmed_office_id == office_id, "前置：已确认在房（可上报态）"

        # 静默断线：服务端推迟回收旧会话 ⇒ 重放的 join 撞真同名检查并在预算内重试
        office_server.stall_disconnect = asyncio.Event()
        office_server.joined.clear()
        office_server.event_log.clear()
        ws = client.eio.ws
        assert ws is not None
        await ws.close()
        await _wait_for_new_sid(client, first_sid)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)
        assert client._confirmed_office_id is None, "前置：回放在途 ⇒ 成员关系尚未确立"

        # ① 窗口内变更：不得发包、不得广播（负例需要静默窗口）
        await client.emit_update_config()
        await asyncio.sleep(_QUIESCENCE)
        assert not any(kind == "update_config" for kind, _ in office_server.event_log), (
            f"窗口内不得发 server:update_config（发了也会被 require_office_id 丢弃）：{office_server.event_log}"
        )
        assert received == [], "窗口内房内不得收到 notify:update_config"

        # 放行回收 ⇒ 下一次重试真的入房成功 ⇒ 补发
        office_server.stall_disconnect.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CONNECT_TIMEOUT
        while not received:
            if loop.time() > deadline:
                raise AssertionError(f"回房成功后未补发：{office_server.event_log}")
            await asyncio.sleep(_WAIT_INTERVAL)

        # ② 恰一包 + 恰一条，且都在**成功重放之后**
        current_sid = client.namespaces[SMCP_NAMESPACE]
        assert [kind for kind, _ in office_server.event_log].count("update_config") == 1, (
            f"补发必须恰一次：{office_server.event_log}"
        )
        flush_at = office_server.event_log.index(("update_config", current_sid))
        joined_at = max(
            i for i, (kind, sid) in enumerate(office_server.event_log) if kind == "join" and sid == current_sid
        )
        assert flush_at > joined_at, "补发必须发生在成功重放之后（不得早于/绕过回放）"
        assert len(received) == 1, f"房内 Agent 恰收到一条 notify:update_config：{received}"
        assert received[0]["computer"] == "office-rejoin-computer"
        assert client._confirmed_office_id == office_id, "自愈后成员关系落账"
    finally:
        if office_server.stall_disconnect is not None:
            office_server.stall_disconnect.set()
        await client.disconnect()
        await observer.disconnect()
