# -*- coding: utf-8 -*-
"""
* 文件名: test_single_computer_room_conformance
* 描述: #230 / protocol#66 —— room-model.md §一致性测试场景 **全部 9 条**，线上（真实 Socket.IO 服务端）
        对 **async**（``SMCPNamespace``，ASGI）与 **sync**（``SyncSMCPNamespace``，WSGI 子进程）两种实现各跑一遍。

        记号同协议：``A`` = Agent，``C1`` / ``C2`` = 两台不同 Computer，``R1`` / ``R2`` = 两个房间。
        客户端一律用 ``socketio.AsyncClient``——线上契约与客户端是否同步无关，同一份用例即可对拍两种服务端。

        「无广播」类断言需要一段静默窗口（:data:`QUIET`）：广播是异步投递的，立刻断言「没收到」恒真。
        并发场景（#8）只在线上验证「恰一者成功」的**结果**；交错的确定性复现见
        ``tests/unit_tests/server/test_registry_atomicity.py::TestAtomicSeatClaim``。

Over-the-wire conformance for the 9 room-model scenarios of protocol#66, against both server flavours.
"""

from __future__ import annotations

import asyncio
import itertools
import multiprocessing
import socket
from collections.abc import AsyncGenerator
from multiprocessing import synchronize
from typing import Any

import pytest
from socketio import ASGIApp, AsyncClient
from werkzeug.serving import make_server

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.smcp import (
    ENTER_OFFICE_NOTIFICATION,
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_EVENT,
    LEAVE_OFFICE_NOTIFICATION,
    LIST_ROOM_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    ErrorCode,
    build_bad_request_error,
    build_computer_not_found_error,
    build_room_rejection_error,
)
from a2c_smcp.testing import UvicornTestServer, create_local_sync_server
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio

#: 「没有收到广播」的静默观察窗口（秒）/ quiet window before asserting that nothing was broadcast
QUIET = 0.3


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run_sync_server(port: int, ready: synchronize.Event) -> None:
    sio, _ns, wsgi_app = create_local_sync_server()
    sio.eio.start_service_task = False
    server = make_server("localhost", port, wsgi_app, threaded=True)
    ready.set()
    server.serve_forever()


@pytest.fixture(params=["async", "sync"])
async def server_url(request: pytest.FixtureRequest) -> AsyncGenerator[str, None]:
    """依次起 async（同进程 uvicorn）与 sync（WSGI 子进程）服务端，交出带 ``a2c_version`` 的连接 URL。"""
    port = _free_port()
    url = f"http://localhost:{port}?a2c_version={PROTOCOL_VERSION}"
    if request.param == "async":
        sio = create_computer_test_socketio()
        sio.eio.start_service_task = False
        server = UvicornTestServer(ASGIApp(sio, socketio_path="/socket.io"), port=port)
        await server.up()
        try:
            yield url
        finally:
            await server.down(force=True)
        return
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(target=_run_sync_server, args=(port, ready), daemon=True)
    proc.start()
    if not ready.wait(timeout=5):
        proc.kill()
        pytest.fail("同步服务端进程启动超时")
    try:
        yield url
    finally:
        proc.terminate()
        proc.join(timeout=3)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=1)


class _Peer:
    """一个线上会话 + 它收到的成员通知（按到达顺序）。

    v0.5.0（protocol#64）：每个 Computer 会话默认携带一个**唯一且合法**的 ``computer_id``
    （规范 UUID、非 nil/max）；同一 peer 的重复 join / 换房沿用同值（身份在连接内不可变）。
    """
    #: 进程内 Computer 身份计数器（构造合法 UUID v4 文本；不使用 nil/max）
    _computer_seq = itertools.count(1)

    def __init__(self) -> None:
        self.client = AsyncClient()
        self.computer_id = f"00000000-0000-4000-8000-{next(self._computer_seq):012d}"
        self.notices: list[tuple[str, dict[str, Any]]] = []

        @self.client.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
        async def _on_enter(data: dict[str, Any]) -> None:
            self.notices.append(("enter", data))

        @self.client.on(LEAVE_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
        async def _on_leave(data: dict[str, Any]) -> None:
            self.notices.append(("leave", data))

    async def join(self, role: str, name: str, office_id: str, computer_id: str | None = None) -> Any:
        payload: dict[str, Any] = {"role": role, "name": name, "office_id": office_id}
        if role == "computer":
            payload["computer_id"] = computer_id if computer_id is not None else self.computer_id
        return await self.client.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE, timeout=10)

    async def join_raw(self, payload: dict[str, Any]) -> Any:
        """原样发送（用于构造 400 场景：缺失 / 非法 / Agent 携带 computer_id）。"""
        return await self.client.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE, timeout=10)

    async def leave(self, office_id: str) -> Any:
        return await self.client.call(LEAVE_OFFICE_EVENT, {"office_id": office_id}, namespace=SMCP_NAMESPACE, timeout=10)


@pytest.fixture
async def peers(server_url: str) -> AsyncGenerator[Any, None]:
    """``await make()`` ⇒ 已连接的 :class:`_Peer`；用例结束统一断开。"""
    made: list[_Peer] = []

    async def make() -> _Peer:
        peer = _Peer()
        await peer.client.connect(server_url, namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
        made.append(peer)
        return peer

    yield make
    for peer in made:
        if peer.client.connected:
            await peer.client.disconnect()


def _room_full(office_id: str, role: str) -> dict[str, Any]:
    """协议 4101 载荷（error-handling.md §Room Full 的字面示例）。"""
    message = "Room already has an agent" if role == "agent" else "Room already has a computer"
    return {"code": 4101, "message": message, "details": {"office_id": office_id, "role": role}}


async def _quiet() -> None:
    await asyncio.sleep(QUIET)


async def _await_notices(peer: _Peer, count: int, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(peer.notices) < count:
        if loop.time() >= deadline:
            raise AssertionError(f"等待通知超时：期望 {count} 条，实得 {peer.notices!r}")
        await asyncio.sleep(0.01)


async def test_1_second_computer_is_4101_and_nothing_broadcast(peers: Any) -> None:
    """#1：A、C1 在 R1；C2 join R1 ⇒ 4101 {role: computer}；R1 成员不变，无任何 notify:*。"""
    a, c1, c2 = await peers(), await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c1.join("computer", "C1", "R1") is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c2.join("computer", "C2", "R1") == _room_full("R1", "computer")

    await _quiet()
    assert a.notices == [] and c1.notices == [], (a.notices, c1.notices)


async def test_2_same_name_second_computer_is_4101_not_4105(peers: Any) -> None:
    """#2：同 #1，且 C2 与 C1 **同名** ⇒ 同样 4101（不得回 4105）。"""
    a, c1, c2 = await peers(), await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c1.join("computer", "PC", "R1") is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c2.join("computer", "PC", "R1") == _room_full("R1", "computer")

    await _quiet()
    assert a.notices == [], a.notices


async def test_3_idempotent_rejoin_does_not_rebroadcast(peers: Any) -> None:
    """#3：C1 在 R1；同一会话再次 join R1 ⇒ 空 ack，不重复广播 notify:enter_office。"""
    a, c1 = await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c1.join("computer", "C1", "R1") is None
    await _await_notices(a, 1)

    assert await c1.join("computer", "C1", "R1") is None

    await _quiet()
    assert a.notices == [
        ("enter", {"office_id": "R1", "computer": "C1", "computer_id": c1.computer_id}),
    ], a.notices


async def test_4_rejected_switch_keeps_old_room_untouched(peers: Any) -> None:
    """#4：C1 在 R1、C2 在 R2；C2 join R1 ⇒ 4101；C2 仍在 R2，R2 **未**收到 notify:leave_office。"""
    watcher_r2, c1, c2 = await peers(), await peers(), await peers()
    assert await watcher_r2.join("agent", "A2", "R2") is None
    assert await c1.join("computer", "C1", "R1") is None
    assert await c2.join("computer", "C2", "R2") is None
    await _await_notices(watcher_r2, 1)
    watcher_r2.notices.clear()

    assert await c2.join("computer", "C2", "R1") == _room_full("R1", "computer")

    await _quiet()
    assert watcher_r2.notices == [], f"校验先于副作用：R2 不得收到 leave：{watcher_r2.notices!r}"
    # C2 仍在 R2：R2 的 Computer 席位仍归它 ⇒ 另一台 Computer 进 R2 仍被拒
    c3 = await peers()
    assert await c3.join("computer", "C3", "R2") == _room_full("R2", "computer")

    # 且成员关系仍在（未被静默摘房）：R2 的 Agent 仍可路由到 C2
    @c2.client.on(TOOL_CALL_EVENT, namespace=SMCP_NAMESPACE)
    async def _on_tool_call(data: dict[str, Any]) -> dict[str, Any]:
        return {"isError": False, "content": [{"type": "text", "text": "still C2"}]}

    call = {"agent": "A2", "computer": "C2", "tool_name": "echo", "params": {}, "req_id": "r", "timeout": 5}
    res = await watcher_r2.client.call(TOOL_CALL_EVENT, call, namespace=SMCP_NAMESPACE, timeout=10)
    assert res["content"][0]["text"] == "still C2", res


async def test_5_rebind_is_leave_then_enter_and_routes_to_new_computer(peers: Any) -> None:
    """#5：A、C1 在 R1；C1 leave → C2 join ⇒ A 依次收到 leave(C1)、enter(C2)；C2 可路由、C1 回 404。"""
    a, c1, c2 = await peers(), await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c1.join("computer", "C1", "R1") is None
    await _await_notices(a, 1)
    a.notices.clear()

    @c2.client.on(TOOL_CALL_EVENT, namespace=SMCP_NAMESPACE)
    async def _on_tool_call(data: dict[str, Any]) -> dict[str, Any]:
        return {"isError": False, "content": [{"type": "text", "text": "from C2"}]}

    assert await c1.leave("R1") is None
    assert await c2.join("computer", "C2", "R1") is None

    await _await_notices(a, 2)
    assert a.notices == [
        ("leave", {"office_id": "R1", "computer": "C1", "computer_id": c1.computer_id}),
        ("enter", {"office_id": "R1", "computer": "C2", "computer_id": c2.computer_id}),
    ], a.notices

    def _call(computer: str) -> dict[str, Any]:
        return {"agent": "A", "computer": computer, "tool_name": "echo", "params": {}, "req_id": "r", "timeout": 5}

    routed = await a.client.call(TOOL_CALL_EVENT, _call("C2"), namespace=SMCP_NAMESPACE, timeout=10)
    assert routed["content"][0]["text"] == "from C2", routed
    gone = await a.client.call(TOOL_CALL_EVENT, _call("C1"), namespace=SMCP_NAMESPACE, timeout=10)
    assert gone == build_computer_not_found_error("C1"), gone


async def test_6_switch_into_occupied_room_is_4101_and_stays(peers: Any) -> None:
    """#6：A、C1 在 R1，C2 在 R2；C1 join R2 ⇒ 4101（R2 已有 C2）；C1 仍在 R1（A 未收到 leave）。"""
    a, c1, c2 = await peers(), await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c1.join("computer", "C1", "R1") is None
    assert await c2.join("computer", "C2", "R2") is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c1.join("computer", "C1", "R2") == _room_full("R2", "computer")

    await _quiet()
    assert a.notices == [], a.notices
    # C1 仍在 R1 且可路由（未被静默摘房）

    @c1.client.on(TOOL_CALL_EVENT, namespace=SMCP_NAMESPACE)
    async def _on_tool_call(data: dict[str, Any]) -> dict[str, Any]:
        return {"isError": False, "content": [{"type": "text", "text": "still C1"}]}

    call = {"agent": "A", "computer": "C1", "tool_name": "echo", "params": {}, "req_id": "r", "timeout": 5}
    res = await a.client.call(TOOL_CALL_EVENT, call, namespace=SMCP_NAMESPACE, timeout=10)
    assert res["content"][0]["text"] == "still C1", res


async def test_7_second_agent_is_4101_with_agent_role(peers: Any) -> None:
    """#7：A 在 R1；另一 Agent join R1 ⇒ 4101 {office_id: R1, role: agent}。"""
    a, b = await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None

    assert await b.join("agent", "B", "R1") == _room_full("R1", "agent")


async def test_8_concurrent_join_into_empty_room_has_exactly_one_winner(peers: Any) -> None:
    """#8：R1 为空，C1、C2 并发 join ⇒ 恰一者空 ack、另一者 4101 {role: computer}；R1 至多一台 Computer。"""
    c1, c2 = await peers(), await peers()

    acks = await asyncio.gather(c1.join("computer", "C1", "R1"), c2.join("computer", "C2", "R1"))

    assert sorted(acks, key=lambda x: x is not None) == [None, _room_full("R1", "computer")], acks
    # 结果层复核：再来一台同样被拒（房内恰有一台 Computer）
    c3 = await peers()
    assert await c3.join("computer", "C3", "R1") == _room_full("R1", "computer")


async def test_9_positive_auto_switch_broadcasts_both_rooms(peers: Any) -> None:
    """#9：A、C1 在 R1，R2 为空；C1 join R2 ⇒ 空 ack；R1 收到 leave(C1)，R2 收到 enter(C1)。"""
    a, watcher_r2, c1 = await peers(), await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await watcher_r2.join("agent", "A2", "R2") is None
    assert await c1.join("computer", "C1", "R1") is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c1.join("computer", "C1", "R2") is None

    await _await_notices(a, 1)
    await _await_notices(watcher_r2, 1)
    assert a.notices == [
        ("leave", {"office_id": "R1", "computer": "C1", "computer_id": c1.computer_id}),
    ], a.notices
    assert watcher_r2.notices == [
        ("enter", {"office_id": "R2", "computer": "C1", "computer_id": c1.computer_id}),
    ], watcher_r2.notices
    # 旧房席位已释放：另一台 Computer 可进 R1
    c2 = await peers()
    assert await c2.join("computer", "C2", "R1") is None


# ===========================================================================
# v0.5.0（protocol#64）：``computer_id`` 场景 #10–#21
# ===========================================================================
#: 两个合法且不同的 ``computer_id``（记法同协议：X / Y）。刻意含 a-f：``X.upper()`` 才能构造大小写差异。
X = "abcdef01-2345-4678-8abc-def012345678"
Y = "abcdef02-2345-4678-8abc-def012345679"


def _bad_request() -> dict[str, Any]:
    return build_bad_request_error()


def _forbidden() -> dict[str, Any]:
    return build_room_rejection_error(ErrorCode.FORBIDDEN)


async def _list_room(peer: _Peer, office_id: str, agent: str = "A") -> dict[str, Any]:
    """以 ``peer``（房内 Agent）身份拉 ``server:list_room``。"""
    return await peer.client.call(
        LIST_ROOM_EVENT,
        {"agent": agent, "req_id": "lr", "office_id": office_id},
        namespace=SMCP_NAMESPACE,
        timeout=10,
    )


def _session_by_name(room: dict[str, Any], name: str) -> dict[str, Any]:
    for session in room["sessions"]:
        if session.get("name") == name:
            return session
    raise AssertionError(f"{name!r} 不在 SessionInfo 列表中: {room!r}")


async def test_10_computer_id_missing_or_malformed_is_400(peers: Any) -> None:
    """#10：缺失 / 大写 / 无连字符 / 非 UUID / nil / max ⇒ 400；规范形态但版本位非 4/7 ⇒ 接受。

    400 先于任何副作用：用一个房内 watcher 证明失败请求不产生 enter 广播，随后同一 peer 以合法值
    入房成功（席位仍空 ⇒ 失败请求确实没入过房）。
    """
    watcher, c = await peers(), await peers()
    assert await watcher.join("agent", "W", "R1") is None

    bad_values: list[Any] = [
        "<missing>",
        X.upper(),
        X.replace("-", ""),
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",  # nil
        "ffffffff-ffff-ffff-ffff-ffffffffffff",  # max
        # protocol#64 / PR #74：整串 + 恰 36 字符；空白与换行一律 400
        X + "\n",
        " " + X,
        X + " ",
    ]
    for bad in bad_values:
        payload: dict[str, Any] = {"role": "computer", "name": "C1", "office_id": "R1"}
        if bad != "<missing>":
            payload["computer_id"] = bad
        assert await c.join_raw(payload) == _bad_request(), f"computer_id={bad!r} 应回 400"

    await _quiet()
    assert watcher.notices == [], f"400 不得产生任何广播: {watcher.notices!r}"

    # 正向：规范形态但版本位非 4/7（opaque 接收）必须接受
    accepted = "12345678-1234-9234-8234-123456789abc"
    assert await c.join("computer", "C1", "R1", computer_id=accepted) is None
    await _await_notices(watcher, 1)
    assert watcher.notices == [("enter", {"office_id": "R1", "computer": "C1", "computer_id": accepted})]


async def test_11_agent_carrying_computer_id_is_400(peers: Any) -> None:
    """#11：Agent 携带 ``computer_id``（任意值，含 ``null``）⇒ 400；A 未入房。

    R1 按场景保持为空（Agent 席位唯一 ⇒ 无法同时放观察者）。「未入房」用**正对照**证明：
    去掉该键后同一会话可正常入房——若前两次 400 已产生副作用，这里会撞 4101。
    """
    a = await peers()

    assert await a.join_raw({"role": "agent", "name": "A", "office_id": "R1", "computer_id": X}) == _bad_request()
    # ``null``：TypedDict 的 ``NotRequired[str]`` 在校验阶段即拒绝 ⇒ 400
    assert await a.join_raw({"role": "agent", "name": "A", "office_id": "R1", "computer_id": None}) == _bad_request()

    # A 未入房：不携带该键即可正常入房（若已入房会撞 4101 / 4106）
    assert await a.join("agent", "A", "R1") is None


async def test_12_echo_in_notice_and_session_info(peers: Any) -> None:
    """#12：A 在 R1；C1 以 X 入房 ⇒ enter 带 X；SessionInfo 中 C1 有 X，A **无** ``computer_id`` 键。"""
    a, c = await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    await _await_notices(a, 1)
    assert a.notices == [("enter", {"office_id": "R1", "computer": "C1", "computer_id": X})], a.notices

    room = await _list_room(a, "R1")
    c1_info = _session_by_name(room, "C1")
    a_info = _session_by_name(room, "A")
    assert c1_info["computer_id"] == X, c1_info
    assert "computer_id" not in a_info, f"Agent 的 SessionInfo MUST 省略该键: {a_info!r}"


async def test_13_same_sid_changing_computer_id_is_403(peers: Any) -> None:
    """#13：C1 在 R1（X）；同一会话以 Y 再次 join ⇒ 403；仍在 R1、记录仍为 X、无广播。"""
    a, c = await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c.join("computer", "C1", "R1", computer_id=Y) == _forbidden()

    await _quiet()
    assert a.notices == [], f"403 不得产生广播: {a.notices!r}"
    room = await _list_room(a, "R1")
    assert _session_by_name(room, "C1")["computer_id"] == X, "记录不得被改声明覆盖"


async def test_14_cloned_computer_id_in_another_room_is_accepted(peers: Any) -> None:
    """#14：C1 在 R1（X）；另一会话 C2 以同一 X join R2（克隆）⇒ 空 ack（协议不做冲突检测）。"""
    c1, c2 = await peers(), await peers()
    assert await c1.join("computer", "C1", "R1", computer_id=X) is None
    assert await c2.join("computer", "C2", "R2", computer_id=X) is None


async def test_15_leave_and_disconnect_notices_carry_computer_id(peers: Any) -> None:
    """#15：C1 显式 leave / 断连 ⇒ A 收到带 X 的 ``notify:leave_office``。"""
    a, c = await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c.leave("R1") is None
    await _await_notices(a, 1)
    assert a.notices == [("leave", {"office_id": "R1", "computer": "C1", "computer_id": X})], a.notices

    # 断连路径：重新入房后直接断开
    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    await _await_notices(a, 2)
    a.notices.clear()
    await c.client.disconnect()
    await _await_notices(a, 1)
    assert a.notices == [("leave", {"office_id": "R1", "computer": "C1", "computer_id": X})], a.notices


async def test_16_auto_switch_notices_carry_computer_id(peers: Any) -> None:
    """#16：C1 从 R1 自动换房到 R2 ⇒ R1 收到 leave(X)，R2 收到 enter(X)。"""
    a_r1, a_r2, c = await peers(), await peers(), await peers()
    assert await a_r1.join("agent", "A1", "R1") is None
    assert await a_r2.join("agent", "A2", "R2") is None
    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    await _await_notices(a_r1, 1)
    a_r1.notices.clear()

    assert await c.join("computer", "C1", "R2", computer_id=X) is None

    await _await_notices(a_r1, 1)
    await _await_notices(a_r2, 1)
    assert a_r1.notices == [("leave", {"office_id": "R1", "computer": "C1", "computer_id": X})], a_r1.notices
    assert a_r2.notices == [("enter", {"office_id": "R2", "computer": "C1", "computer_id": X})], a_r2.notices


async def test_17_same_identity_rejoin_is_idempotent(peers: Any) -> None:
    """#17：C1 在 R1（X）；同一会话以 X 再次 join ⇒ 空 ack，无广播（#13 的正对照）。"""
    a, c = await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    await _quiet()
    assert a.notices == [], a.notices


async def test_18_record_survives_leave_so_new_id_is_403(peers: Any) -> None:
    """#18：C1 在 R1（X）；leave 后同一会话以 Y join ⇒ 403；记录不因 leave 清除。"""
    a, c = await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c.leave("R1") is None
    await _await_notices(a, 1)
    a.notices.clear()

    assert await c.join("computer", "C1", "R1", computer_id=Y) == _forbidden()
    room = await _list_room(a, "R1")
    assert all(session.get("name") != "C1" for session in room["sessions"]), f"C1 不得在房: {room!r}"

    # 记录仍为 X：同一会话以 X 入房即可（正对照）
    assert await c.join("computer", "C1", "R1", computer_id=X) is None


async def test_19_changing_id_while_switching_room_is_403_not_4101(peers: Any) -> None:
    """#19：C1 在 R1（X）、C2 在 R2；C1 以 Y join R2 ⇒ 403（**非** 4101）；C1 仍在 R1、R1 无 leave。"""
    a_r1, c1, c2 = await peers(), await peers(), await peers()
    assert await a_r1.join("agent", "A1", "R1") is None
    assert await c1.join("computer", "C1", "R1", computer_id=X) is None
    assert await c2.join("computer", "C2", "R2", computer_id=Y) is None
    await _await_notices(a_r1, 1)
    a_r1.notices.clear()

    ack = await c1.join("computer", "C1", "R2", computer_id=Y)
    assert ack == _forbidden(), f"身份校验必须先于席位（403 而非 4101）: {ack!r}"

    await _quiet()
    assert a_r1.notices == [], f"C1 仍在 R1，不得收到 leave: {a_r1.notices!r}"
    room = await _list_room(a_r1, "R1")
    assert _session_by_name(room, "C1")["computer_id"] == X


async def test_20_agent_switching_room_with_computer_id_is_400_not_4106(peers: Any) -> None:
    """#20：A 在 R1；A join R2 并携带 ``computer_id`` ⇒ 400（**非** 4106）；A 仍在 R1。"""
    a, watcher = await peers(), await peers()
    assert await a.join("agent", "A", "R1") is None
    assert await watcher.join("agent", "W", "R2") is None
    watcher.notices.clear()

    ack = await a.join_raw({"role": "agent", "name": "A", "office_id": "R2", "computer_id": X})
    assert ack == _bad_request(), f"取值校验必须先于房间语义（400 而非 4106）: {ack!r}"

    await _quiet()
    assert watcher.notices == [], watcher.notices
    room = await _list_room(a, "R1")
    assert _session_by_name(room, "A")["name"] == "A"


async def test_21_agent_enter_notice_omits_computer_id(peers: Any) -> None:
    """#21：C1 在 R1（X）；A 入房 ⇒ C1 收到的 enter(agent) **无** ``computer_id`` 键（不得为 null）。"""
    c, a = await peers(), await peers()
    assert await c.join("computer", "C1", "R1", computer_id=X) is None
    assert await a.join("agent", "A", "R1") is None

    await _await_notices(c, 1)
    assert c.notices == [("enter", {"office_id": "R1", "agent": "A"})], c.notices
    payload = c.notices[0][1]
    assert "computer_id" not in payload, f"Agent 通知 MUST 省略该键（不得为 null）: {payload!r}"
