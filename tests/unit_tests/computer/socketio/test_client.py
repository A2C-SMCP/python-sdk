# -*- coding: utf-8 -*-

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import StdioServerParameters
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters
from mcp.types import Resource

from a2c_smcp.computer.mcp_clients.model import SseServerConfig, StdioServerConfig, StreamableHttpServerConfig, ToolMeta
from a2c_smcp.computer.socketio.client import SMCPComputerClient, _to_a2c_resource
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
    client.computer.name = "sid-123"

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
    client.computer.name = "comp-abc"

    req = {
        "computer": "comp-abc",
        "agent": "office-1",
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
        def __init__(self, name: str):
            self.name = name
            self._mcp_servers = (stdio_cfg, sse_cfg, http_cfg)
            self._inputs = []

        @property
        def inputs(self) -> list:
            return self._inputs

        @property
        def mcp_servers(self):
            return self._mcp_servers

    client = SMCPComputerClient(computer=_FakeComputer(name="sid-xyz"))
    client.office_id = "office-1"

    req = {"computer": "sid-xyz", "agent": "office-1", "req_id": "mock_req"}
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


@pytest.mark.asyncio
async def test_join_office_success():
    """
    测试成功加入房间：服务器返回 (True, None)
    Test successful join office: server returns (True, None)
    """
    client = SMCPComputerClient(computer=MagicMock())
    client.computer.name = "test_computer"

    # Mock call 方法返回成功结果
    # Mock call method to return success result
    client.call = AsyncMock(return_value=[True, None])

    # 应该成功加入，不抛出异常
    # Should succeed without exception
    await client.join_office("office_123")

    # 验证 office_id 被设置
    # Verify office_id is set
    assert client.office_id == "office_123"


@pytest.mark.asyncio
async def test_join_office_duplicate_name_raises_error():
    """
    测试加入房间失败（重名）：服务器返回 (False, error_msg)，应抛出 RuntimeError
    Test join office fails (duplicate name): server returns (False, error_msg), should raise RuntimeError
    """
    client = SMCPComputerClient(computer=MagicMock())
    client.computer.name = "duplicate_name"

    # Mock call 方法返回失败结果
    # Mock call method to return failure result
    error_msg = "Computer with name 'duplicate_name' already exists in room 'office_123'"
    client.call = AsyncMock(return_value=[False, error_msg])

    # 应该抛出 RuntimeError
    # Should raise RuntimeError
    with pytest.raises(RuntimeError, match="加入房间失败"):
        await client.join_office("office_123")

    # office_id 不应该被设置
    # office_id should not be set
    assert client.office_id is None


@pytest.mark.asyncio
async def test_join_office_no_response_raises_error():
    """
    测试加入房间失败（无响应）：服务器返回 None 或空值，应抛出 RuntimeError
    Test join office fails (no response): server returns None or empty, should raise RuntimeError
    """
    client = SMCPComputerClient(computer=MagicMock())
    client.computer.name = "test_computer"

    # Mock call 方法返回 None
    # Mock call method to return None
    client.call = AsyncMock(return_value=None)

    # 应该抛出 RuntimeError
    # Should raise RuntimeError
    with pytest.raises(RuntimeError, match="服务器未返回结果"):
        await client.join_office("office_123")

    # office_id 不应该被设置
    # office_id should not be set
    assert client.office_id is None


# ======================================================================
# _to_a2c_resource —— MCP Resource → A2CResource snake_case 映射
# _to_a2c_resource — MCP Resource → A2CResource snake_case mapping
#
# 回归护栏 / Regression guard for issue #32（PR #29 follow-up）：
#   `_to_a2c_resource` 的整个 annotations 分支此前零测试覆盖；本组用例锁定
#   audience/priority/last_modified 透传契约（含 forward-compat：MCP `Annotations`
#   model_config=extra='allow'，未来 `lastModified` 字段必须经防御式 getattr 捕获）。
#   The annotations branch of `_to_a2c_resource` was previously untested; these
#   cases lock the audience/priority/last_modified passthrough contract, incl.
#   the forward-compat path (MCP `Annotations` extra='allow' → defensive getattr).
# 协议依据 / Protocol: a2c-smcp-protocol data-structures.md#A2CResource（透传 MCP annotations）
# ======================================================================


def test_to_a2c_resource_maps_last_modified_forward_compat() -> None:
    """
    中文：MCP 服务端透传 `lastModified`（MCP `Annotations` extra='allow' 的 forward-compat
    字段）时，必须规整为 `annotations.last_modified`，且不得残留 camelCase 原键。
    English: When the MCP server passes through `lastModified` (a forward-compat
    field via MCP `Annotations` extra='allow'), it must be normalized to
    `annotations.last_modified` with no leftover camelCase key.
    """
    res = Resource.model_validate(
        {
            "uri": "window://example.desktop/p1",
            "name": "P1",
            "annotations": {
                "audience": ["user", "assistant"],
                "priority": 0.7,
                "lastModified": "2026-05-19T08:00:00Z",
            },
        },
    )

    mapped = _to_a2c_resource(res)

    assert mapped["uri"] == str(res.uri)
    assert mapped["name"] == "P1"
    assert mapped["annotations"] == {
        "audience": ["user", "assistant"],
        "priority": 0.7,
        "last_modified": "2026-05-19T08:00:00Z",
    }
    # camelCase 原键不得泄漏进协议层 / no camelCase key may leak into protocol layer
    assert "lastModified" not in mapped["annotations"]


def test_to_a2c_resource_full_field_mapping() -> None:
    """
    中文：覆盖完整字段分支——基础字段、camelCase→snake_case（mimeType→mime_type）、
    `_meta` 透传、annotations（audience/priority）一并正确映射。
    English: Cover the full field branch — base fields, camelCase→snake_case
    (mimeType→mime_type), `_meta` passthrough and annotations (audience/priority).
    """
    res = Resource.model_validate(
        {
            "uri": "file://tmp/doc.md",
            "name": "Doc",
            "description": "a doc",
            "mimeType": "text/markdown",
            "size": 123,
            "_meta": {"fullscreen": True},
            "annotations": {"audience": ["user"], "priority": 0.3},
        },
    )

    mapped = _to_a2c_resource(res)

    assert mapped == {
        "uri": "file://tmp/doc.md",
        "name": "Doc",
        "description": "a doc",
        "mime_type": "text/markdown",
        "size": 123,
        "annotations": {"audience": ["user"], "priority": 0.3},
        "_meta": {"fullscreen": True},
    }
    # MCP 原 camelCase 键不得出现 / original MCP camelCase key must be absent
    assert "mimeType" not in mapped


def test_to_a2c_resource_no_annotations_omits_optional_keys() -> None:
    """
    中文：可选字段缺失时不得注入空键——无 annotations 则不出现 `annotations` 键，
    无 size/description/_meta 同理（避免下游误判"字段存在但为空"）。
    English: Missing optional fields must not inject empty keys — no `annotations`
    key when absent; same for size/description/_meta (avoid downstream
    "present-but-empty" misreads).
    """
    res = Resource.model_validate({"uri": "window://h/x", "name": "X"})

    mapped = _to_a2c_resource(res)

    assert mapped == {"uri": "window://h/x", "name": "X"}
    assert "annotations" not in mapped
    assert "size" not in mapped
    assert "description" not in mapped
    assert "_meta" not in mapped
