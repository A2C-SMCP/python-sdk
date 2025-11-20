# -*- coding: utf-8 -*-

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import StdioServerParameters
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters

from a2c_smcp.computer.mcp_clients.model import SseServerConfig, StdioServerConfig, StreamableHttpServerConfig, ToolMeta
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import SMCP_NAMESPACE, UPDATE_CONFIG_EVENT


@pytest.mark.asyncio
async def test_emit_disallows_notify_and_client_events():
    """
    中文：验证 emit 校验逻辑，禁止 notify:* 与 client:* 事件
    English: Verify emit validation blocks notify:* and client:* events
    """
    client = SMCPComputerClient(computer=MagicMock())

    with pytest.raises(ValueError):
        await client.emit("notify:something", {})

    with pytest.raises(ValueError):
        await client.emit("client:something", {})


@pytest.mark.asyncio
async def test_emit_update_config_only_when_in_office(monkeypatch):
    """
    中文：仅当已加入房间（有 office_id）时触发 UPDATE_MCP_CONFIG_EVENT；未加入时不触发
    English: Fire UPDATE_MCP_CONFIG_EVENT only when office_id set; otherwise do nothing
    """
    client = SMCPComputerClient(computer=MagicMock())

    sent = []

    async def fake_emit(self, event, data=None, namespace=None, callback=None):
        sent.append((event, data, namespace))

    # 注入必要上下文（无需真实连接）/ Inject minimal context (no real connection)
    client.namespaces[SMCP_NAMESPACE] = "sid-123"

    # 场景1：未加入房间，不应发送
    monkeypatch.setattr(SMCPComputerClient, "emit", fake_emit, raising=False)
    client.office_id = None
    await SMCPComputerClient.emit_update_config(client)
    assert not sent

    # 场景2：已加入房间，应发送 UPDATE_MCP_CONFIG_EVENT
    client.office_id = "office-1"
    await SMCPComputerClient.emit_update_config(client)
    assert len(sent) == 1
    assert sent[0][0] == UPDATE_CONFIG_EVENT
    assert sent[0][1] == {"computer": "sid-123"}


@pytest.mark.asyncio
async def test_on_tool_call_error_handling():
    """
    中文：当 aexecute_tool 抛出异常时，应返回 CallToolResult 且 isError=True
    English: If aexecute_tool raises, return CallToolResult with isError=True
    """
    computer = MagicMock()
    computer.aexecute_tool = AsyncMock(side_effect=RuntimeError("boom"))

    client = SMCPComputerClient(computer=computer)
    client.office_id = "office-1"
    client.namespaces[SMCP_NAMESPACE] = "sid-abc"

    req = {
        "computer": "sid-abc",
        "robot_id": "office-1",
        "req_id": "r1",
        "tool_name": "t1",
        "params": {"k": "v"},
        "timeout": 1,
    }

    ret = await client.on_tool_call(req)
    assert ret["isError"] is True
    assert ret["structuredContent"] is not None
    assert ret["structuredContent"].get("error_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_on_get_config_serialization_three_types():
    """
    中文：验证 on_get_config 对 stdio/sse/streamable_http 三种配置的序列化与强校验
    English: Verify on_get_config serialization and strict validation for stdio/sse/streamable_http types
    """
    # 构造三种类型配置 / Build three types of server configs
    stdio_cfg = StdioServerConfig(
        name="stdio-srv",
        server_parameters=StdioServerParameters(command="bash", args=["-lc", "echo hi"], env={}),
        forbidden_tools=["ban1"],
        tool_meta={"toolA": ToolMeta(auto_apply=True)},
    )
    sse_cfg = SseServerConfig(
        name="sse-srv",
        server_parameters=SseServerParameters(url="http://localhost:18080/sse"),
        forbidden_tools=[],
        tool_meta={},
    )
    http_cfg = StreamableHttpServerConfig(
        name="http-srv",
        server_parameters=StreamableHttpParameters(url="http://localhost:18081"),
        forbidden_tools=[],
        tool_meta={},
    )

    class _FakeComputer:
        def __init__(self):
            self._mcp_servers = (stdio_cfg, sse_cfg, http_cfg)
            self._inputs = []

        @property
        def inputs(self) -> list:
            return self._inputs

        @property
        def mcp_servers(self):
            return self._mcp_servers

    client = SMCPComputerClient(computer=_FakeComputer())
    client.office_id = "office-1"
    client.namespaces[SMCP_NAMESPACE] = "sid-xyz"

    req = {"computer": "sid-xyz", "robot_id": "office-1", "req_id": "mock_req"}
    ret = await client.on_get_config(req)

    # 结构校验 / Structure checks
    assert "servers" in ret
    servers = ret["servers"]
    assert set(servers.keys()) == {"stdio-srv", "sse-srv", "http-srv"}

    assert servers["stdio-srv"]["type"] == "stdio"
    assert servers["sse-srv"]["type"] == "sse"
    assert servers["http-srv"]["type"] == "streamable"

    # 基础字段校验 / Base fields
    assert servers["stdio-srv"]["disabled"] is False
    assert servers["stdio-srv"]["forbidden_tools"] == ["ban1"]
    assert "toolA" in servers["stdio-srv"]["tool_meta"]

    # server_parameters 应为可序列化结构 / server_parameters should be JSON-like
    assert isinstance(servers["stdio-srv"]["server_parameters"], dict)
    assert isinstance(servers["sse-srv"]["server_parameters"], dict)
    assert isinstance(servers["http-srv"]["server_parameters"], dict)
