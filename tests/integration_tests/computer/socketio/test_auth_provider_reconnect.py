# -*- coding: utf-8 -*-
"""
#200 动态 auth provider 集成测试 / #200 dynamic auth provider integration test (real wire).

真实 wire 验证（方案 C 原生透传，验收标准 4）：真实 ASGI Socket.IO 服务端 + 真实
``SMCPComputerClient``——provider 首连返回 Token A；模拟真实断网（直接掐断底层 WebSocket
传输）触发客户端自动重连，重连握手前 provider 被**重新求值**并携带新返回的 Token B。
provider 每次握手恰调用一次，首连与重连的 CONNECT auth 均为调用时最新值。

Real-wire proof of plan C (acceptance criterion 4): a real ASGI Socket.IO server plus a real
``SMCPComputerClient`` — the provider returns Token A on first connect; a simulated network
drop (abrupt kill of the underlying WebSocket transport) triggers the client auto-reconnect,
and the provider is **re-evaluated** before the reconnect handshake, carrying the freshly
returned Token B. The provider is called exactly once per handshake, and each CONNECT auth
is the value returned at call time.

断网模拟选型 / Why kill the transport (upstream behavior, verified in source):
  - 服务端主动 CLOSE（如 ``sio.disconnect(sid)``）→ engineio 客户端 ``disconnect()`` 在触发
    'disconnect' 事件前置 ``state='disconnecting'`` → socketio 层 ``will_reconnect=False``，
    **不触发**自动重连；
  - 传输层突然断开（读循环在 ``state=='connected'`` 时退出）→ ``TRANSPORT_ERROR`` →
    ``_handle_reconnect`` 以 ``auth=self.connection_auth``（即 provider）重新建连。
  - Server-initiated CLOSE sets eio ``state='disconnecting'`` before the 'disconnect' event,
    so socketio's ``will_reconnect`` is False — no auto-reconnect;
  - an abrupt transport drop (read loop exits while ``state=='connected'``) → ``TRANSPORT_ERROR``
    → ``_handle_reconnect`` reconnects with ``auth=self.connection_auth`` (the provider).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest
from socketio import ASGIApp, AsyncServer

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.testing import UvicornTestServer
from tests.integration_tests.mock_socketio_server import MockComputerServerNamespace

_CONNECT_TIMEOUT = 10.0


class _AuthRecordingNamespace(MockComputerServerNamespace):
    """记录每次 CONNECT 收到的 auth payload（按到达顺序）/ records each CONNECT auth payload in arrival order."""

    def __init__(self) -> None:
        super().__init__()
        self.auth_record: list[dict] = []
        self.new_connect: asyncio.Event | None = None

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        result = await super().on_connect(sid, environ, auth)
        self.auth_record.append(dict(auth) if isinstance(auth, dict) else {})
        if self.new_connect is not None:
            self.new_connect.set()
        return result


@pytest.fixture
async def auth_server(basic_server_port: int) -> AsyncGenerator[_AuthRecordingNamespace, None]:
    """真实 ASGI Socket.IO 服务端（记录 auth 的命名空间）/ real ASGI Socket.IO server with the auth-recording namespace."""
    sio = AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        ping_timeout=10,
        ping_interval=10,
        async_handlers=True,
    )
    # 与共享 fixture 一致：关闭后台服务任务，避免关闭时后台任务异常 / consistent with the shared
    # fixture: disable the background service task to avoid background task issues on shutdown
    sio.eio.start_service_task = False
    ns = _AuthRecordingNamespace()
    sio.register_namespace(ns)
    asgi_app = ASGIApp(sio, socketio_path="/socket.io")
    server = UvicornTestServer(asgi_app, port=basic_server_port)
    await server.up()
    try:
        yield ns
    finally:
        await server.down(force=True)


@pytest.mark.asyncio
async def test_auth_provider_token_rotation_on_auto_reconnect(auth_server: _AuthRecordingNamespace, basic_server_port: int):
    """首连 Token A → 模拟断网 → 自动重连携带 Token B（验收标准 4）。

    English: first connect carries Token A → simulated network drop → auto-reconnect carries Token B (criterion 4).
    """
    provider_calls: list[int] = []

    async def provider() -> dict[str, str]:
        provider_calls.append(1)
        return {"token": "A" if len(provider_calls) == 1 else "B"}

    computer = MagicMock(spec=Computer)
    client = SMCPComputerClient(
        computer=computer,
        # 测试提速：默认值即可工作，此处收紧重连节奏 / faster reconnect cadence for tests
        reconnection=True,
        reconnection_attempts=3,
        reconnection_delay=0.05,
        reconnection_delay_max=0.2,
    )

    first_connect = asyncio.Event()
    auth_server.new_connect = first_connect
    try:
        await client.connect(
            f"http://localhost:{basic_server_port}",
            auth_provider=provider,
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        await asyncio.wait_for(first_connect.wait(), timeout=_CONNECT_TIMEOUT)
        assert auth_server.auth_record[-1] == {"token": "A"}, "首连 CONNECT auth 必须是 provider 首调返回值 Token A"

        # 模拟真实断网：掐断底层 WebSocket 传输（不发送 CLOSE 包），走 TRANSPORT_ERROR → 自动重连路径
        # Simulate a real network drop: abruptly kill the WebSocket transport (no CLOSE packet),
        # which is the TRANSPORT_ERROR → auto-reconnect path.
        second_connect = asyncio.Event()
        auth_server.new_connect = second_connect
        first_sid = client.namespaces[SMCP_NAMESPACE]
        ws = client.eio.ws
        assert ws is not None  # 握手后已升级 websocket / transport upgraded to websocket post-handshake
        await ws.close()

        await asyncio.wait_for(second_connect.wait(), timeout=_CONNECT_TIMEOUT)
        assert auth_server.auth_record[-1] == {"token": "B"}, "重连 CONNECT auth 必须是 provider 重新求值的 Token B"

        # 恰两次握手、provider 恰调用两次（每次握手重新求值，无并行/竞态）
        assert auth_server.auth_record == [{"token": "A"}, {"token": "B"}]
        assert len(provider_calls) == 2

        # 服务端已收到重连 CONNECT；等客户端处理 ack 重建命名空间（ack 比服务端事件晚一个 loop tick）
        # The server has received the reconnect CONNECT; wait for the client to process the ack
        # and re-establish the namespace (the ack lands one loop tick after the server-side event).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CONNECT_TIMEOUT
        while SMCP_NAMESPACE not in client.namespaces:
            if loop.time() > deadline:
                raise AssertionError("重连后命名空间未在期限内重建 / namespace not re-established after reconnect")
            await asyncio.sleep(0.01)

        # 重连后命名空间持新 socketio sid（≠ 首连 sid），客户端处于已连接态 / namespace re-established
        # with a fresh socketio-level sid after reconnect; client is connected again.
        assert client.namespaces[SMCP_NAMESPACE] != first_sid
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_auth_provider_transient_failure_self_heals(auth_server: _AuthRecordingNamespace, basic_server_port: int):
    """验收标准 5（真实 wire）：provider 瞬时失败 → 该次握手失败、socketio 有界重试自愈。

    首连成功（Token A）；断网后首个重连握手的 provider 求值抛异常 → 该次握手不产生 CONNECT
    （服务端无记录）→ socketio 有界重试（``reconnection_attempts``）→ 下一次求值返回 Token B
    → 自愈。已知上游缺陷（provider 异常被吞、错误信息为空，跟踪 #201）不改变本路径的可观测
    结果：服务端 auth 终值正确、provider 调用序恰为 3 次（首连 + 失败尝试 + 自愈尝试）。

    Acceptance criterion 5 on the real wire: a transient provider failure fails that handshake
    and self-heals via socketio's bounded retry. First connect succeeds (Token A); after the
    network drop the first reconnect handshake raises in the provider → no CONNECT reaches the
    server → the bounded retry (``reconnection_attempts``) re-evaluates and connects with Token B.
    The known upstream defects (swallowed provider exceptions, empty error message — tracked in
    #201) do not change the observable outcome: the server's auth record ends correct and the
    provider is called exactly 3 times (first connect + failed attempt + self-healed attempt).
    """
    provider_calls: list[int] = []

    async def flaky_provider() -> dict[str, str]:
        provider_calls.append(1)
        if len(provider_calls) == 1:
            return {"token": "A"}
        if len(provider_calls) == 2:
            raise RuntimeError("transient credential refresh failure")  # 瞬时失败 / transient failure
        return {"token": "B"}

    computer = MagicMock(spec=Computer)
    client = SMCPComputerClient(
        computer=computer,
        reconnection=True,
        reconnection_attempts=3,
        reconnection_delay=0.05,
        reconnection_delay_max=0.2,
    )

    first_connect = asyncio.Event()
    auth_server.new_connect = first_connect
    try:
        await client.connect(
            f"http://localhost:{basic_server_port}",
            auth_provider=flaky_provider,
            socketio_path="/socket.io",
            namespaces=[SMCP_NAMESPACE],
        )
        await asyncio.wait_for(first_connect.wait(), timeout=_CONNECT_TIMEOUT)
        assert auth_server.auth_record[-1] == {"token": "A"}, "首连 CONNECT auth 必须是 Token A"

        second_connect = asyncio.Event()
        auth_server.new_connect = second_connect
        ws = client.eio.ws
        assert ws is not None
        await ws.close()

        # 首个重连尝试 provider 抛异常（CONNECT 未发出，服务端无记录）→ 下一次尝试自愈携带 Token B
        # First reconnect attempt raises in the provider (no CONNECT sent, nothing recorded
        # server-side) → the next attempt self-heals carrying Token B.
        await asyncio.wait_for(second_connect.wait(), timeout=_CONNECT_TIMEOUT)
        assert auth_server.auth_record[-1] == {"token": "B"}, "自愈后 CONNECT auth 必须是 Token B"
        assert auth_server.auth_record == [{"token": "A"}, {"token": "B"}], "失败尝试不得产生服务端 CONNECT 记录"
        assert len(provider_calls) == 3, "provider 调用序必须恰为 3 次（首连 + 失败尝试 + 自愈尝试）"

        # 等客户端处理 ack 重建命名空间（同主用例的 ack 时序）
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CONNECT_TIMEOUT
        while SMCP_NAMESPACE not in client.namespaces:
            if loop.time() > deadline:
                raise AssertionError("自愈后命名空间未在期限内重建 / namespace not re-established after self-heal")
            await asyncio.sleep(0.01)
    finally:
        await client.disconnect()
