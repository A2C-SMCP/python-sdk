# -*- coding: utf-8 -*-
# filename: test_version_handshake_client.py
# @Author  : JQQ
# @Software: PyCharm
"""
客户端协议版本握手集成测试（#17）/ Client-side protocol version handshake integration (#17)

Server 中间件声明协议版本 0.3.0（与 SDK 的 0.2.0 不兼容）→ HTTP 400 / 4008。

**测试分层（架构决策，回应 code-review 🔴1）**：进程内 UvicornTestServer + 真实 aiohttp
在 coverage 插桩下，服务端可能在客户端读完 400 body 前关连接（aiohttp 已知问题簇），
此时 4008 原因物理上不可还原。故本集成层只断言**确定性保证**——不兼容客户端**连接被拒、
绝不建立**（versioning.md 传递性保证）；"4008 → ProtocolVersionError 精确归一" 这一确定性
契约由 ``tests/unit_tests/test_handshake_4008_normalization.py`` 以受控异常确定性验证。
两层各在其确定性层级断言，非假绿。

This integration layer asserts only the *deterministic guarantee* (incompatible client is
rejected, never connects). The exact 4008→ProtocolVersionError mapping is pinned
deterministically in the unit contract test (mocked exception input), since under
coverage-instrumented in-process serving the 400 body can be lost to a connection-close
race (a known aiohttp behaviour) where the reason is physically unrecoverable.
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
from a2c_smcp.utils.handshake import HANDSHAKE_CONNECT_ERRORS
from tests.integration_tests.computer.socketio.mock_uv_server import UvicornTestServer
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio
from tests.protocol_versions import INCOMPATIBLE_PEER, max_supported_of, min_supported_of

_SIO_PATH = "/socket.io"
# 从 PROTOCOL_VERSION 派生的不兼容 server 版本（MINOR 不匹配）——不耦合具体协议版本值
_INCOMPATIBLE_SERVER = INCOMPATIBLE_PEER
# 确定性拒绝形态集合：归一成功 → ProtocolVersionError；body 竞态丢失 → 原始连接异常
# （二者都满足"连接被拒"的确定性保证；精确类型契约见单测）
_REJECTION_TYPES: tuple[type[BaseException], ...] = (ProtocolVersionError, *HANDSHAKE_CONNECT_ERRORS)


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


def _assert_mismatch_if_pve(exc: BaseException) -> None:
    """归一成功（body 在）→ ProtocolVersionError 字段必须正确；body 竞态丢失 → 接受原始连接异常
    （连接仍被拒，确定性保证成立；精确类型由 test_handshake_4008_normalization 确定性验证）。"""
    if isinstance(exc, ProtocolVersionError):
        assert exc.client_version == PROTOCOL_VERSION
        assert exc.server_version == _INCOMPATIBLE_SERVER
        assert exc.min_supported == min_supported_of(_INCOMPATIBLE_SERVER)
        assert exc.max_supported == max_supported_of(_INCOMPATIBLE_SERVER)
        assert PROTOCOL_VERSION in str(exc) and _INCOMPATIBLE_SERVER in str(exc)


@pytest.mark.asyncio
async def test_async_agent_handshake_rejected(incompatible_server: int) -> None:
    auth = DefaultAgentAuthProvider(agent_id="robot-x", office_id="office-x")
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    with pytest.raises(_REJECTION_TYPES) as ei:
        await agent.connect_to_server(
            f"http://127.0.0.1:{incompatible_server}",
            namespace=SMCP_NAMESPACE,
            socketio_path=_SIO_PATH,
        )
    _assert_mismatch_if_pve(ei.value)
    assert agent.connected is False  # 确定性保证：连接绝不建立（含 §4 死循环防御）


@pytest.mark.asyncio
async def test_computer_client_handshake_rejected(incompatible_server: int) -> None:
    client = SMCPComputerClient(computer=Computer(name="comp-x", mcp_servers=set()))
    with pytest.raises(_REJECTION_TYPES) as ei:
        await client.connect(
            f"http://127.0.0.1:{incompatible_server}",
            socketio_path=_SIO_PATH,
            namespaces=[SMCP_NAMESPACE],
        )
    _assert_mismatch_if_pve(ei.value)
    assert client.connected is False


@pytest.mark.asyncio
async def test_sync_agent_handshake_rejected(incompatible_server: int) -> None:
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

    with pytest.raises(_REJECTION_TYPES) as ei:
        await asyncio.to_thread(_connect)
    _assert_mismatch_if_pve(ei.value)


@pytest.fixture
async def compatible_server(basic_server_port: int) -> AsyncGenerator[int, None]:
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


# 🟡3：§1 护栏端到端契约——三处接线（async agent / sync agent / Computer）均覆盖，
# 防止某一处接线笔误不被任何测试拦住。护栏判定逻辑由
# test_handshake.py::TestEnforcePollingFirst + test_handshake_4008_normalization 确定性单测覆盖；
# 此处只验证端到端契约：调用方显式 transports=["websocket"] 不静默放行 → 强制重注入
# polling-first → 连接仍成功（未退化到 §5 WS-only 拒绝路径）。


@pytest.mark.asyncio
async def test_async_agent_explicit_ws_only_guarded(compatible_server: int) -> None:
    auth = DefaultAgentAuthProvider(agent_id="robot-guard-a", office_id="office-guard-a")
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    await agent.connect_to_server(
        f"http://127.0.0.1:{compatible_server}",
        namespace=SMCP_NAMESPACE,
        socketio_path=_SIO_PATH,
        transports=["websocket"],  # 显式 WS-only：应被护栏纠正为 polling-first
    )
    try:
        assert agent.connected is True
    finally:
        await agent.disconnect()


@pytest.mark.asyncio
async def test_computer_client_explicit_ws_only_guarded(compatible_server: int) -> None:
    client = SMCPComputerClient(computer=Computer(name="comp-guard", mcp_servers=set()))
    await client.connect(
        f"http://127.0.0.1:{compatible_server}",
        socketio_path=_SIO_PATH,
        namespaces=[SMCP_NAMESPACE],
        transports=["websocket"],  # 显式 WS-only：应被护栏纠正为 polling-first
    )
    try:
        assert client.connected is True
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_sync_agent_explicit_ws_only_guarded(compatible_server: int) -> None:
    auth = DefaultAgentAuthProvider(agent_id="robot-guard-s", office_id="office-guard-s")
    port = compatible_server

    def _run() -> bool:
        agent = SMCPAgentClient(auth_provider=auth)
        agent.connect_to_server(
            f"http://127.0.0.1:{port}",
            namespace=SMCP_NAMESPACE,
            socketio_path=_SIO_PATH,
            transports=["websocket"],  # 显式 WS-only：应被护栏纠正为 polling-first
        )
        try:
            return bool(agent.connected)
        finally:
            agent.disconnect()

    assert await asyncio.to_thread(_run) is True
