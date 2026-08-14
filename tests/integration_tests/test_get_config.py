# -*- coding: utf-8 -*-
# filename: test_get_config.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：``client:get_config`` 端到端集成测试（#149）。

    真实全链路：真实 Computer（CLI 式空集构造 + ``amount_server`` 运行期挂载）→ SMCPComputerClient
    → 真实 SMCPNamespace → 真实 AsyncSMCPAgentClient.get_config_from_computer。

    验收（#149）：
      - 走**真实构造路径**（F7：``Computer(mcp_servers=set())`` + ``amount_server``，不依赖 ``_FakeComputer``）；
        死快照下 get_config 恒空，修复后 Agent 端到端能看到该 server。
      - servers 键 = **bundle_id**（≠ display name → 取值分叉）；entry 携 display name。
      - body 占位符 ``${env:X}`` **字面保留**（raw 未渲染 → 绝不把已解析 secret 发上 wire）。

English: End-to-end integration test for ``client:get_config`` (#149) — real Computer + Agent round-trip.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncGenerator

import pytest
from mcp import StdioServerParameters
from socketio import ASGIApp

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, SMCP_NAMESPACE
from a2c_smcp.testing import UvicornTestServer
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio


@pytest.fixture
def basic_server_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def socketio_server(basic_server_port: int) -> AsyncGenerator[None, None]:
    """基于真实 SMCPNamespace 的测试服务器 / Test server backed by the real SMCPNamespace."""
    sio = create_computer_test_socketio()
    sio.eio.start_service_task = False
    asgi_app = ASGIApp(sio, socketio_path="/socket.io")
    server = UvicornTestServer(asgi_app, port=basic_server_port)
    await server.up()
    try:
        yield
    finally:
        await server.down(force=True)


async def _agent_join(agent: AsyncSMCPAgentClient, office_id: str, name: str) -> None:
    await agent.emit(
        JOIN_OFFICE_EVENT,
        {"role": "agent", "office_id": office_id, "name": name},
        namespace=SMCP_NAMESPACE,
    )


@pytest.mark.asyncio
async def test_agent_get_config_roundtrip_sees_runtime_mounted_server(
    socketio_server: None,
    basic_server_port: int,
    monkeypatch,
) -> None:
    """真实构造路径 + Agent 端到端：``amount_server`` 挂载的 server 经 ``get_config_from_computer`` 可见（#149）。"""
    monkeypatch.setenv("A2C_GETCONFIG_E2E_SECRET", "leaked-e2e-value")
    office_id = "office-cfg-1"
    # display name 含 '.' → bundle_id 'my_e2e_srv'（取值分叉）；env 带 ${env:} 占位（raw 保真探针）。
    cfg = StdioServerConfig(
        name="my.e2e.srv",
        server_parameters=StdioServerParameters(
            command="bash",
            args=["-lc", "echo hi"],
            env={"TOKEN": "${env:A2C_GETCONFIG_E2E_SECRET}"},
        ),
    )
    # CLI 式：空集构造 + auto_connect=False（config-only 挂载，不起子进程） + 运行期 amount_server。
    computer = Computer(name="comp-cfg-1", mcp_servers=set(), auto_connect=False)
    await computer.amount_server(cfg)

    comp_client = SMCPComputerClient(computer=computer)
    await comp_client.connect(
        f"http://localhost:{basic_server_port}",
        socketio_path="/socket.io",
        headers={"mock_header": "mock_value"},
        auth={"mock_header": "mock_value"},
        namespaces=[SMCP_NAMESPACE],
    )
    await comp_client.join_office(office_id)

    auth = DefaultAgentAuthProvider(agent_id="robot-cfg-1", office_id=office_id)
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    await agent.connect_to_server(
        f"http://localhost:{basic_server_port}",
        namespace=SMCP_NAMESPACE,
        socketio_path="/socket.io",
    )
    await _agent_join(agent, office_id, "robot-cfg-1")

    try:
        ret = await agent.get_config_from_computer(computer="comp-cfg-1")
        servers = ret["servers"]
        # 死快照下此处恒空；修复后运行期挂载项可见。
        assert "my_e2e_srv" in servers, "Agent 端到端必须看到运行期挂载的 server（#149 死快照回归）"
        assert "my.e2e.srv" not in servers  # display name 不做键
        entry = servers["my_e2e_srv"]
        assert entry["name"] == "my.e2e.srv"
        assert entry["bundle_id"] == "my_e2e_srv"
        # body 占位符字面保留（raw）——已解析 secret 绝不上 wire。
        assert entry["server_parameters"]["env"]["TOKEN"] == "${env:A2C_GETCONFIG_E2E_SECRET}"
        assert "leaked-e2e-value" not in str(ret)
    finally:
        await agent.disconnect()
        await comp_client.disconnect()
        if computer.mcp_manager is not None:
            await computer.mcp_manager.aclose()
