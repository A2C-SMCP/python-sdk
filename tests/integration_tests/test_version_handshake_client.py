# -*- coding: utf-8 -*-
# filename: test_version_handshake_client.py
# @Author  : JQQ
# @Software: PyCharm
"""
客户端协议版本握手集成测试（#17）/ Client-side protocol version handshake integration (#17)

Server 中间件声明协议版本 0.3.0（与 SDK 的 0.2.0 不兼容）→ HTTP 400 / 4008。验证：
Server middleware declares 0.3.0 (incompatible with the SDK's 0.2.0) → HTTP 400 / 4008. Verify:
- AsyncSMCPAgentClient / SMCPAgentClient(sync) / SMCPComputerClient 均抛 ProtocolVersionError
- 异常字段（client/server/min/max）正确填充
- 4008 死循环防御：开启自动重连 + Server 持续 4008，SDK 抛错后不再连接（connected=False）
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest
from socketio import ASGIApp

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.exceptions import ProtocolVersionError
from a2c_smcp.server.middleware import A2CProtocolVersionASGIMiddleware
from a2c_smcp.smcp import SMCP_NAMESPACE
from tests.integration_tests.computer.socketio.mock_uv_server import UvicornTestServer
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio

_SIO_PATH = "/socket.io"
_INCOMPATIBLE_SERVER = "0.3.0"  # 与 SDK PROTOCOL_VERSION(0.2.0) MINOR 不匹配


@pytest.fixture
async def incompatible_server(basic_server_port: int) -> AsyncGenerator[int, None]:
    sio = create_computer_test_socketio()
    sio.eio.start_service_task = False
    app = A2CProtocolVersionASGIMiddleware(
        ASGIApp(sio, socketio_path=_SIO_PATH),
        socketio_path=_SIO_PATH,
        server_version=_INCOMPATIBLE_SERVER,
    )
    server = UvicornTestServer(app, port=basic_server_port)
    await server.up()
    try:
        yield basic_server_port
    finally:
        await server.down(force=True)


def _assert_mismatch(exc: ProtocolVersionError) -> None:
    assert exc.client_version == PROTOCOL_VERSION
    assert exc.server_version == _INCOMPATIBLE_SERVER
    assert exc.min_supported == "0.3.0"
    assert exc.max_supported == "0.3.999"
    # __str__ 含 client/server 对比，便于排查
    assert PROTOCOL_VERSION in str(exc) and _INCOMPATIBLE_SERVER in str(exc)


@pytest.mark.asyncio
async def test_async_agent_raises_protocol_version_error(incompatible_server: int) -> None:
    auth = DefaultAgentAuthProvider(agent_id="robot-x", office_id="office-x")
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    with pytest.raises(ProtocolVersionError) as ei:
        await agent.connect_to_server(
            f"http://127.0.0.1:{incompatible_server}",
            namespace=SMCP_NAMESPACE,
            socketio_path=_SIO_PATH,
        )
    _assert_mismatch(ei.value)
    # 4008 死循环防御：抛错后不得处于已连接 / 重连状态
    assert agent.connected is False


@pytest.mark.asyncio
async def test_computer_client_raises_protocol_version_error(incompatible_server: int) -> None:
    computer = Computer(name="comp-x", mcp_servers=set())
    client = SMCPComputerClient(computer=computer)
    with pytest.raises(ProtocolVersionError) as ei:
        await client.connect(
            f"http://127.0.0.1:{incompatible_server}",
            socketio_path=_SIO_PATH,
            namespaces=[SMCP_NAMESPACE],
        )
    _assert_mismatch(ei.value)
    assert client.connected is False


@pytest.mark.asyncio
async def test_sync_agent_raises_protocol_version_error(incompatible_server: int) -> None:
    auth = DefaultAgentAuthProvider(agent_id="robot-sync", office_id="office-sync")

    def _connect() -> None:
        agent = SMCPAgentClient(auth_provider=auth)
        try:
            agent.connect_to_server(
                f"http://127.0.0.1:{incompatible_server}",
                namespace=SMCP_NAMESPACE,
                socketio_path=_SIO_PATH,
            )
        finally:
            assert agent.connected is False

    with pytest.raises(ProtocolVersionError) as ei:
        await asyncio.to_thread(_connect)
    _assert_mismatch(ei.value)
