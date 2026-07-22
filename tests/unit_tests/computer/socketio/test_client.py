# -*- coding: utf-8 -*-

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import StdioServerParameters
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters
from mcp.types import Resource

from a2c_smcp.computer.computer import Computer
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
    # 构造三种类型配置 / Build three types of server configs。
    # #150 R5①：display name **取值分叉**——含空格/括号，normalize_name 后 bundle_id 与 name 逐字不同
    # （"stdio srv (display)" → "stdio_srv_display"）⇒ 键断言从此能鉴别「按 name 取」还是「按 bundle_id 取」，
    # 令本序列化用例升级为真正的泄漏守卫（原名 "stdio-srv" 规范化后逐字等于自身，零鉴别力）。
    stdio_cfg = StdioServerConfig(
        name="stdio srv (display)",
        server_parameters=StdioServerParameters(command="bash", args=["-lc", "echo hi"], env={}),
        forbidden_tools=["ban1"],
        tool_meta={"toolA": ToolMeta(auto_apply=True)},
    )
    sse_cfg = SseServerConfig(
        name="sse srv (display)",
        server_parameters=SseServerParameters(url="http://localhost:18080/sse"),
        forbidden_tools=[],
        tool_meta={},
    )
    http_cfg = StreamableHttpServerConfig(
        name="http srv (display)",
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

        def active_server_configs(self):
            # #149：on_get_config 现从运行期活跃集的 raw 投影取数；本纯序列化 fake 直接回等价配置集。
            return self._mcp_servers

    client = SMCPComputerClient(computer=_FakeComputer(name="sid-xyz"))
    client.office_id = "office-1"

    req = {"computer": "sid-xyz", "agent": "office-1", "req_id": "mock_req"}
    ret = await client.on_get_config(req)

    # 结构校验 / Structure checks
    assert "servers" in ret
    servers = ret["servers"]
    # #150：键 = **bundle_id**（与 display name 分叉）；若实现误按 name 取键此断言即红（泄漏守卫）。
    assert set(servers.keys()) == {"stdio_srv_display", "sse_srv_display", "http_srv_display"}
    # entry 同时携原始 display name 与解析后 bundle_id（name≠key，证明二者是不同维度）。
    assert servers["stdio_srv_display"]["name"] == "stdio srv (display)"
    assert servers["stdio_srv_display"]["bundle_id"] == "stdio_srv_display"
    assert servers["sse_srv_display"]["name"] == "sse srv (display)"
    assert servers["http_srv_display"]["name"] == "http srv (display)"

    assert servers["stdio_srv_display"]["type"] == "stdio"
    assert servers["sse_srv_display"]["type"] == "sse"
    assert servers["http_srv_display"]["type"] == "streamable"

    # 基础字段校验 / Base fields
    assert servers["stdio_srv_display"]["disabled"] is False
    assert servers["stdio_srv_display"]["forbidden_tools"] == ["ban1"]
    assert "toolA" in servers["stdio_srv_display"]["tool_meta"]

    # server_parameters 应为可序列化结构 / server_parameters should be JSON-like
    assert isinstance(servers["stdio_srv_display"]["server_parameters"], dict)
    assert isinstance(servers["sse_srv_display"]["server_parameters"], dict)
    assert isinstance(servers["http_srv_display"]["server_parameters"], dict)


@pytest.mark.asyncio
async def test_on_get_config_reads_runtime_active_set_not_dead_snapshot(monkeypatch):
    """#149 P0 回归：真实 ``Computer(mcp_servers=set())`` + ``amount_server`` 挂载 → ``on_get_config`` 必须返回该 server。

    English: #149 P0 regression — a server mounted at runtime (not at construction) MUST appear in ``on_get_config``.

    现码红（死快照）：``on_get_config`` 迭代构造期死快照 ``self.computer.mcp_servers``（CLI 空集构造 → 恒空）→
    ``servers == {}``；修复后绿：迭代运行期活跃集（``manager.server_configs()`` 权威）的 **raw 投影**。
    走真实构造路径（F7：不依赖 ``_FakeComputer``），并附带三项断言：
      ① ``servers`` 键 = **bundle_id**（≠ display name → 取值分叉，证明键不是 name）；
      ② ``entry["name"]`` = 原始 display name；
      ③ body 占位符 ``${env:X}`` **字面保留**（raw 未渲染 → 绝不把已解析 secret 发上 wire）。
    """
    # ${env:X} 会在挂载（render）阶段被解析为真实值存入 manager；raw 投影须仍保留占位符字面量。
    monkeypatch.setenv("A2C_GETCONFIG_SECRET", "leaked-secret-value")
    # display name 含 '.' → normalize_name 为 bundle_id 'my_srv'（取值分叉）；env 带 ${env:} 占位（raw 保真探针）。
    cfg = StdioServerConfig(
        name="my.srv",
        server_parameters=StdioServerParameters(
            command="bash",
            args=["-lc", "echo hi"],
            env={"TOKEN": "${env:A2C_GETCONFIG_SECRET}"},
        ),
    )
    # auto_connect=False：config-only 挂载（不起子进程），单测轻量确定。
    comp = Computer(name="comp-xyz", mcp_servers=set(), auto_connect=False)
    try:
        await comp.amount_server(cfg)  # 纯运行期挂载（transient；不触碰构造期 _mcp_servers）

        client = SMCPComputerClient(computer=comp)
        client.office_id = "office-1"
        ret = await client.on_get_config({"computer": "comp-xyz", "agent": "office-1", "req_id": "r1"})

        servers = ret["servers"]
        # ① 键 = bundle_id（≠ display name）；死快照下此断言红（servers 恒空）。
        assert "my_srv" in servers, "运行期活跃集的 server 必须出现在 get_config（#149 死快照回归）"
        assert "my.srv" not in servers  # display name 不做键
        entry = servers["my_srv"]
        # ② entry 携 display name + 解析后 bundle_id。
        assert entry["name"] == "my.srv"
        assert entry["bundle_id"] == "my_srv"
        # ③ body 占位符字面保留（raw 未渲染）——绝不把已解析 secret 发上 wire。
        token = entry["server_parameters"]["env"]["TOKEN"]
        assert token == "${env:A2C_GETCONFIG_SECRET}", f"wire body 必须 raw；泄漏了已渲染值: {token!r}"
        assert "leaked-secret-value" not in str(ret), "已解析 secret 绝不得出现在 get_config wire 返回中"
    finally:
        if comp.mcp_manager is not None:
            await comp.mcp_manager.aclose()


@pytest.mark.asyncio
async def test_on_get_config_includes_durably_added_server(monkeypatch, tmp_path):
    """#149：durable ``aadd_or_aupdate_server``（= REPL ``server add`` 路径）挂载的 server 也须出现在 get_config。

    English: a durably-added server (the REPL ``server add`` path) must also appear in ``on_get_config``.

    与 transient ``amount_server`` 共用 ``_amount_rendered`` 漏斗，二者共同覆盖验收「REPL add 可见 / plugin bundled 可见」。
    chdir tmp_path：durable 落盘 ``.tfrobot/mcp.local.json`` 隔离到临时目录，杜绝污染真实仓库（#137 陷阱）。
    """
    monkeypatch.chdir(tmp_path)
    cfg = StdioServerConfig(
        name="durable.srv",
        server_parameters=StdioServerParameters(command="bash", args=["-lc", "echo hi"], env={}),
    )
    comp = Computer(name="comp-dur", mcp_servers=set(), auto_connect=False)
    try:
        await comp.aadd_or_aupdate_server(cfg)  # durable：落盘 + 运行期物化（经同一 _amount_rendered 漏斗登记 raw）

        client = SMCPComputerClient(computer=comp)
        client.office_id = "office-1"
        ret = await client.on_get_config({"computer": "comp-dur", "agent": "office-1", "req_id": "r1"})

        assert "durable_srv" in ret["servers"], "durable 声明的 server 必须出现在 get_config"
        assert ret["servers"]["durable_srv"]["name"] == "durable.srv"
    finally:
        if comp.mcp_manager is not None:
            await comp.mcp_manager.aclose()


@pytest.mark.asyncio
async def test_on_get_config_fails_closed_when_raw_record_missing(monkeypatch):
    """#149 安全纵深：运行期活跃 server 缺 raw 记录（不变式被违反）时，get_config **省略**该 server 而非回退 rendered。

    English: fail-closed — if a runtime-active server lacks a raw record, omit it from the wire (never leak the rendered secret).

    直接 fabricate 不变式违反（挂载后清空 ``_active_raw`` 缓存、manager 仍持该 server），断言 fail-closed：该 server
    从 wire 省略、且已解析 secret **绝不外泄**（守住用户拍板的 §9.1 raw-only wire 约束的防御纵深）。
    """
    monkeypatch.setenv("A2C_GETCONFIG_SECRET2", "leaked-secret-2")
    cfg = StdioServerConfig(
        name="ghost.srv",
        server_parameters=StdioServerParameters(
            command="bash", args=["-lc", "echo hi"], env={"TOKEN": "${env:A2C_GETCONFIG_SECRET2}"},
        ),
    )
    comp = Computer(name="comp-fc", mcp_servers=set(), auto_connect=False)
    try:
        await comp.amount_server(cfg)
        comp._active_raw.clear()  # 模拟：某挂载漏斗漏登记 raw（manager 仍持 rendered config）

        client = SMCPComputerClient(computer=comp)
        client.office_id = "office-1"
        ret = await client.on_get_config({"computer": "comp-fc", "agent": "office-1", "req_id": "r1"})

        # fail-closed：省略该 server（不回退 rendered），且已解析 secret 绝不上 wire。
        assert "ghost_srv" not in ret["servers"], "缺 raw 记录时必须省略该 server（fail-closed）"
        assert "leaked-secret-2" not in str(ret), "绝不得回退渲染值以致 secret 外泄"
    finally:
        if comp.mcp_manager is not None:
            await comp.mcp_manager.aclose()


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


# -------------------- #96 notify:tool_call_cancel 接收处理器 --------------------
# #96 Computer-side receiver for notify:tool_call_cancel


@pytest.mark.asyncio
async def test_on_tool_call_cancel_delegates_to_computer():
    """#96：handler 按 req_id 委托 computer.acancel_tool，notify:* 无 ack 故返回 None。"""
    computer = MagicMock()
    computer.acancel_tool = AsyncMock(return_value=True)
    client = SMCPComputerClient(computer=computer)

    ret = await client.on_tool_call_cancel({"agent": "agt", "req_id": "req-1"})

    assert ret is None  # notify:* 处理器不回执 / no ack for notify:*
    computer.acancel_tool.assert_awaited_once_with("req-1")


@pytest.mark.asyncio
async def test_on_tool_call_cancel_unknown_req_id_no_raise():
    """#96：未知/已完成 req_id（acancel_tool 返回 False）→ 无害 no-op，不抛异常。"""
    computer = MagicMock()
    computer.acancel_tool = AsyncMock(return_value=False)
    client = SMCPComputerClient(computer=computer)

    ret = await client.on_tool_call_cancel({"agent": "agt", "req_id": "nope"})

    assert ret is None
    computer.acancel_tool.assert_awaited_once_with("nope")
