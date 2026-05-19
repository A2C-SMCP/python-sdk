# -*- coding: utf-8 -*-
# filename: test_version_handshake_server.py
# @Author  : JQQ
# @Software: PyCharm
"""
服务端协议版本握手集成测试（#18）/ Server-side protocol version handshake integration (#18)

真实 uvicorn 起 ASGIApp + A2CProtocolVersionASGIMiddleware，用裸 HTTP 断言握手层行为：
Real uvicorn serving ASGIApp wrapped by the middleware; raw HTTP asserts the handshake layer:
- 不兼容 → 400 + 4008 flat body + X-A2C-Error-Code header
- 缺失 / 非法 a2c_version → 400（无 4008 header）
- 兼容 → 透传给 socketio（非 400）
- 路径作用域：非 socketio_path 路由不受影响
- server:list_room 返回的 SessionInfo 含 a2c_version
"""

from collections.abc import AsyncGenerator

import httpx
import pytest
import socketio
from socketio import ASGIApp
from socketio.exceptions import ConnectionError as SioConnectionError

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.server.middleware import A2CProtocolVersionASGIMiddleware
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, LIST_ROOM_EVENT, SMCP_NAMESPACE
from tests.integration_tests.computer.socketio.mock_uv_server import UvicornTestServer
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio

_SIO_PATH = "/socket.io"


@pytest.fixture
async def mw_server(basic_server_port: int) -> AsyncGenerator[int, None]:
    """Server 协议版本 = SDK PROTOCOL_VERSION（0.2.0）。"""
    sio = create_computer_test_socketio()
    sio.eio.start_service_task = False
    app = A2CProtocolVersionASGIMiddleware(
        ASGIApp(sio, socketio_path=_SIO_PATH),
        socketio_path=_SIO_PATH,
        server_version=PROTOCOL_VERSION,
    )
    server = UvicornTestServer(app, port=basic_server_port)
    await server.up()
    try:
        yield basic_server_port
    finally:
        await server.down(force=True)


def _poll_url(port: int, query: str) -> str:
    return f"http://127.0.0.1:{port}{_SIO_PATH}/?EIO=4&transport=polling&{query}"


@pytest.mark.asyncio
async def test_incompatible_returns_400_4008_with_header(mw_server: int) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.get(_poll_url(mw_server, "a2c_version=0.3.0"))
    assert r.status_code == 400
    assert r.headers.get("X-A2C-Error-Code") == "4008"
    body = r.json()
    assert body["code"] == 4008
    assert body["server_version"] == PROTOCOL_VERSION
    assert body["client_version"] == "0.3.0"
    assert body["min_supported"] == "0.2.0" and body["max_supported"] == "0.2.999"


@pytest.mark.asyncio
async def test_missing_version_returns_400_no_4008_header(mw_server: int) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{mw_server}{_SIO_PATH}/?EIO=4&transport=polling")
    assert r.status_code == 400
    assert "X-A2C-Error-Code" not in r.headers
    assert r.json() == {"code": 400, "message": "Missing a2c_version query parameter"}


@pytest.mark.asyncio
async def test_invalid_version_returns_400(mw_server: int) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.get(_poll_url(mw_server, "a2c_version=not-semver"))
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == 400 and "Invalid a2c_version" in body["message"]


@pytest.mark.asyncio
async def test_compatible_passes_through_to_socketio(mw_server: int) -> None:
    # PATCH 自由：0.2.999 与 server 0.2.0 兼容；中间件透传，socketio 返回 EIO open（非 400）
    async with httpx.AsyncClient() as c:
        r = await c.get(_poll_url(mw_server, "a2c_version=0.2.999"))
    assert r.status_code == 200
    assert "X-A2C-Error-Code" not in r.headers


@pytest.mark.asyncio
async def test_path_scope_non_socketio_route_unaffected(mw_server: int) -> None:
    # 非 socketio_path：中间件必须透传（即使无 a2c_version），不得返回我们的握手 400 body
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{mw_server}/health")
    assert "X-A2C-Error-Code" not in r.headers
    if r.status_code == 400:
        # 若下游 socketio 偶然 400，也必须不是我们的版本握手 body
        assert r.json().get("message") != "Missing a2c_version query parameter"


@pytest.mark.asyncio
async def test_list_room_session_info_contains_a2c_version(mw_server: int) -> None:
    """SDK 客户端自动携带 a2c_version → on_connect 记录 → server:list_room 带出。"""
    office_id = "office-handshake-ver"
    auth = DefaultAgentAuthProvider(agent_id="robot-ver", office_id=office_id)
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    await agent.connect_to_server(
        f"http://127.0.0.1:{mw_server}",
        namespace=SMCP_NAMESPACE,
        socketio_path=_SIO_PATH,
    )
    try:
        await agent.emit(
            JOIN_OFFICE_EVENT,
            {"role": "agent", "office_id": office_id, "name": "robot-ver"},
            namespace=SMCP_NAMESPACE,
        )
        result = await agent.call(
            LIST_ROOM_EVENT,
            {"agent": "robot-ver", "req_id": "lr-1", "office_id": office_id},
            namespace=SMCP_NAMESPACE,
        )
        agents = [s for s in result["sessions"] if s["role"] == "agent"]
        assert agents, "房间内应有 agent 会话"
        assert all(s.get("a2c_version") == PROTOCOL_VERSION for s in agents)
    finally:
        await agent.disconnect()


@pytest.fixture
async def incompatible_mw_server(basic_server_port: int) -> AsyncGenerator[int, None]:
    """Server 协议版本 0.3.0，与 SDK 0.2.0 不兼容（用于 WS-only 拒绝端到端）。"""
    sio = create_computer_test_socketio()
    sio.eio.start_service_task = False
    app = A2CProtocolVersionASGIMiddleware(
        ASGIApp(sio, socketio_path=_SIO_PATH),
        socketio_path=_SIO_PATH,
        server_version="0.3.0",
    )
    server = UvicornTestServer(app, port=basic_server_port)
    await server.up()
    try:
        yield basic_server_port
    finally:
        await server.down(force=True)


@pytest.mark.asyncio
async def test_ws_only_compatible_connects_end_to_end(mw_server: int) -> None:
    """§测试建议 row 652：WS-only 直连 + 版本兼容 → websocket scope 被校验后放行，连接成功。"""
    raw = socketio.AsyncClient()  # 裸客户端：绕过 SDK §1 护栏，真实走 websocket scope
    try:
        await raw.connect(
            f"http://127.0.0.1:{mw_server}?a2c_version={PROTOCOL_VERSION}",
            socketio_path=_SIO_PATH,
            transports=["websocket"],
            namespaces=[SMCP_NAMESPACE],
        )
        assert raw.connected is True
    finally:
        await raw.disconnect()


@pytest.mark.asyncio
async def test_ws_only_incompatible_rejected_end_to_end(incompatible_mw_server: int) -> None:
    """缺口闭合端到端反例：WS-only 直连 + 版本不兼容 → 中间件 websocket scope 校验拒绝，
    连接绝不建立（此前 buggy 实现会放行 → 击穿传递性保证）。

    边界说明（🟡4 / 已知限制，回应 code-review）：本 e2e 只断言**确定性保证**——连接被拒
    （SioConnectionError + not connected）。"denial-response 4008 body 字节一致"无法在此
    over-the-wire 断言，因 python-socketio 不向应用暴露 WS 关闭码/响应体；该字节一致性由
    test_middleware.py::test_ws_denial_response_byte_identical_4008 在 ASGI 可调用层确定性
    验证。与 versioning.md §5 R4（客户端收 4900 改 polling 重连取 4008，受 python-socketio
    能力限制未实现）属同源已知限制。
    """
    raw = socketio.AsyncClient()
    # client a2c_version=0.2.0 vs server 0.3.0 → MINOR 不匹配（不兼容）
    try:
        with pytest.raises(SioConnectionError):
            await raw.connect(
                f"http://127.0.0.1:{incompatible_mw_server}?a2c_version={PROTOCOL_VERSION}",
                socketio_path=_SIO_PATH,
                transports=["websocket"],
                namespaces=[SMCP_NAMESPACE],
            )
        assert raw.connected is False
    finally:
        if raw.connected:  # 防御：万一意外连上也要清理，避免污染同进程后续用例
            await raw.disconnect()
