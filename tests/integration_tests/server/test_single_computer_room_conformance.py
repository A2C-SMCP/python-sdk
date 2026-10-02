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
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    build_computer_not_found_error,
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
    """一个线上会话 + 它收到的成员通知（按到达顺序）。"""

    def __init__(self) -> None:
        self.client = AsyncClient()
        self.notices: list[tuple[str, dict[str, Any]]] = []

        @self.client.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
        async def _on_enter(data: dict[str, Any]) -> None:
            self.notices.append(("enter", data))

        @self.client.on(LEAVE_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
        async def _on_leave(data: dict[str, Any]) -> None:
            self.notices.append(("leave", data))

    async def join(self, role: str, name: str, office_id: str) -> Any:
        return await self.client.call(
            JOIN_OFFICE_EVENT, {"role": role, "name": name, "office_id": office_id}, namespace=SMCP_NAMESPACE, timeout=10
        )

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
    assert a.notices == [("enter", {"office_id": "R1", "computer": "C1"})], a.notices


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
        ("leave", {"office_id": "R1", "computer": "C1"}),
        ("enter", {"office_id": "R1", "computer": "C2"}),
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
    assert a.notices == [("leave", {"office_id": "R1", "computer": "C1"})], a.notices
    assert watcher_r2.notices == [("enter", {"office_id": "R2", "computer": "C1"})], watcher_r2.notices
    # 旧房席位已释放：另一台 Computer 可进 R1
    c2 = await peers()
    assert await c2.join("computer", "C2", "R1") is None
