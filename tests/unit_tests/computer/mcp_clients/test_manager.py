# -*- coding: utf-8 -*-
# filename: test_manager.py
# @Time    : 2025/8/18 14:59
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import StdioServerParameters, Tool
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters
from mcp.types import CallToolResult

from a2c_smcp.computer.mcp_clients.manager import MCPServerManager, _ServerDeclaredToolMeta
from a2c_smcp.computer.mcp_clients.model import (
    MCPClientProtocol,
    MCPServerActivationState,
    MCPServerConfig,
    MCPServerConnectionState,
    MCPServerRuntimeStatus,
    SseServerConfig,
    StdioServerConfig,
    StreamableHttpServerConfig,
    ToolMeta,
)

# 模拟类型定义
TOOL_NAME = str
SERVER_NAME = str


# 模拟BaseMCPClient
class MockMCPClient:
    def __init__(self, tools: list[Tool] = None, ret_meta: dict | None = None, message_handler=None):
        self.tools = tools or []
        self.aconnect = AsyncMock()
        self.adisconnect = AsyncMock()
        self.list_tools = AsyncMock(return_value=tools)
        call_ret = MagicMock(spec=CallToolResult)
        call_ret.result = None
        call_ret.meta = ret_meta
        self.call_tool = AsyncMock(return_value=call_ret)
        self.state = "connected"
        # #179：connect-phase challenge 事件（bounded connect 竞速的 mock 面）。
        # 永不触发 → manager 的竞速以 aconnect 完成收尾（mock 语义 = 匿名连通）。
        self._challenge_event: asyncio.Event | None = None

        def connect_challenge_event() -> asyncio.Event:
            if self._challenge_event is None:
                self._challenge_event = asyncio.Event()
            return self._challenge_event

        self.connect_challenge_event = connect_challenge_event
        # #181：connect-phase redirect stop 事件（同款 mock 面——永不触发）
        self._redirect_event: asyncio.Event | None = None

        def connect_redirect_event() -> asyncio.Event:
            if self._redirect_event is None:
                self._redirect_event = asyncio.Event()
            return self._redirect_event

        self.connect_redirect_event = connect_redirect_event

        def take_connect_redirect_stop() -> None:
            return None

        self.take_connect_redirect_stop = take_connect_redirect_stop
        # 保存透传进来的 message_handler，便于测试断言
        self.message_handler = message_handler


def create_mock_tool(name: str, meta: dict | None = None) -> Tool:
    # 使用真实 Tool（pydantic 模型）而非 MagicMock：available_tools 经 model_copy 改写暴露名（#106），
    # 而 MagicMock 的 model_copy 不具备 pydantic 语义（返回 mock，.name 非真实字符串）会失真。
    # Use a real Tool so model_copy (used to rename the exposed tool, #106) behaves faithfully.
    return Tool(name=name, inputSchema={"type": "object"}, _meta=meta)


# #184：测试用 fail-connect 名单 / Test-only fail-connect server names
_FAIL_CONNECT_SERVERS: set[str] = set()
# #199：test-only server-declared tools 注册表（沿用 _FAIL_CONNECT_SERVERS try/finally 模式）——
# 按 config.name 命中时，该 server 的 list_tools 返回注册的工具（Tool._meta 携带 Server 声明）。
_DECLARED_TOOLS: dict[str, list[Tool]] = {}


# 模拟client_factory函数
def mock_client_factory(config: MCPServerConfig, message_handler=None) -> MockMCPClient:
    # 简化处理：根据配置名称返回不同的工具列表
    # #184：测试 fail-connect 场景
    client: MockMCPClient
    if config.name in _FAIL_CONNECT_SERVERS:
        client = MockMCPClient(message_handler=message_handler)
        client.aconnect = AsyncMock(side_effect=RuntimeError("Simulated connect failure"))
        return client
    if config.name in _DECLARED_TOOLS:
        return MockMCPClient(_DECLARED_TOOLS[config.name], message_handler=message_handler)
    if "server1" in config.name:
        return MockMCPClient([create_mock_tool("tool1", meta={"test": "meta"}), create_mock_tool("tool2")], message_handler=message_handler)
    elif "server2" in config.name:
        return MockMCPClient(
            [create_mock_tool("tool3"), create_mock_tool("tool4")],
            ret_meta={"test": "ret_meta"},
            message_handler=message_handler,
        )
    elif "alias_server" in config.name:
        return MockMCPClient([create_mock_tool("tool5")], message_handler=message_handler)
    elif "duplicate_server" in config.name:
        return MockMCPClient([create_mock_tool("duplicate_tool")], message_handler=message_handler)
    return MockMCPClient(message_handler=message_handler)


# Monkey patch客户端工厂函数
@pytest.fixture(autouse=True)
def patch_client_factory(monkeypatch):
    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", mock_client_factory)


# 创建示例服务器配置
def create_server_config(
    name: str,
    disabled: bool = False,
    forbidden_tools: list = None,
    tool_meta: dict = None,
    default_tool_meta: ToolMeta | None = None,
) -> MCPServerConfig:
    forbidden_tools = forbidden_tools or []
    tool_meta = tool_meta or {}
    if "sse" in name:
        return SseServerConfig(
            name=name,
            disabled=disabled,
            forbidden_tools=forbidden_tools,
            tool_meta=tool_meta,
            default_tool_meta=default_tool_meta,
            server_parameters=MagicMock(spec=SseServerParameters),
        )
    elif "http" in name:
        return StreamableHttpServerConfig(
            name=name,
            disabled=disabled,
            forbidden_tools=forbidden_tools,
            tool_meta=tool_meta,
            default_tool_meta=default_tool_meta,
            # 真实模型（非 MagicMock）：#179 起 manager 对 streamable config 读取
            # server_parameters.url 做 OAuth 通道判定——MagicMock(spec=pydantic 模型)
            # 无法解析字段（pydantic v2 字段不在类属性上），属 wire-invalid 夹具。
            server_parameters=StreamableHttpParameters(url="http://localhost:8000/mcp"),
        )
    else:
        return StdioServerConfig(
            name=name,
            disabled=disabled,
            forbidden_tools=forbidden_tools,
            tool_meta=tool_meta,
            default_tool_meta=default_tool_meta,
            server_parameters=MagicMock(spec=StdioServerParameters),
        )


@pytest.fixture
async def manager() -> MCPServerManager:
    manager = MCPServerManager()
    await manager.enable_auto_reconnect()  # 启用自动重连便于测试
    return manager


@pytest.mark.asyncio
async def test_initialize_with_servers(manager):
    """测试初始化和服务器启动"""
    servers = [create_server_config("server1"), create_server_config("server2", disabled=True), create_server_config("sse_server")]

    await manager.ainitialize(servers)

    # 初始化后不会自动启动所有服务。验证活动客户端
    assert "server1" not in manager._active_clients
    assert "sse_server" not in manager._active_clients
    assert "server2" not in manager._active_clients

    # 调用start_all
    await manager.astart_all()

    # 验证启动
    assert "server1" in manager._active_clients
    assert "sse_server" in manager._active_clients
    assert "server2" not in manager._active_clients

    # 验证 ExposedToolMapping（exposed = {bundle_id}__{原始名}；此处 bundle_id == name）
    assert manager._exposed_tools["server1__tool1"] == ("server1", "tool1")
    assert manager._exposed_tools["server1__tool2"] == ("server1", "tool2")
    assert "server2__tool3" not in manager._exposed_tools  # 禁用的服务器

    # 验证状态检查（#166：返回 (bundle_id, display_name, active, state)；#184：state 改为 connection value）
    statuses = manager.get_server_status()
    assert ("server1", "server1", True, "connected") in statuses
    assert ("server2", "server2", False, "disconnected") in statuses
    assert ("sse_server", "sse_server", True, "connected") in statuses


@pytest.mark.asyncio
async def test_get_server_status_returns_display_name(manager):
    """#166：display name 与 bundle_id 分叉时，status 返回正确的 display name。

    `normalize_name` 折叠 `.`→`_` 但**不折叠 `-`** ⇒ 夹具名 `my.server` → bundle_id `my_server`
    → name ≠ bundle_id，因此断言可鉴别 display name 被正确传递（同值致盲第六例）。
    """
    cfg = create_server_config("my.server")
    await manager.ainitialize([cfg])
    await manager.astart_all()
    statuses = manager.get_server_status()
    assert ("my_server", "my.server", True, "connected") in statuses


@pytest.mark.asyncio
async def test_tool_execution(manager):
    """测试工具执行流程"""
    servers = [create_server_config("server1")]
    await manager.ainitialize(servers)

    await manager.astart_all()

    # 执行工具
    params = {"key": "value"}
    await manager.aexecute_tool("server1__tool1", params)

    # 验证调用（透传原始名 tool1）
    client = manager._active_clients["server1"]
    client.call_tool.assert_awaited_once_with("tool1", params)

    with pytest.raises(Exception):
        await manager.aexecute_tool("server1__tool5", params)


@pytest.mark.asyncio
async def test_tool_execution_with_ret_meta(manager):
    """测试工具执行流程"""
    servers = [create_server_config("server2", tool_meta={"tool3": ToolMeta(ret_meta={"test": "ret_meta"})})]
    await manager.ainitialize(servers)

    await manager.astart_all()

    # 执行工具
    params = {"key": "value"}
    ret = await manager.aexecute_tool("server2__tool3", params)
    assert ret.meta["test"] == "ret_meta"

    # 验证调用
    client = manager._active_clients["server2"]
    client.call_tool.assert_awaited_once_with("tool3", params)


@pytest.mark.asyncio
async def test_tool_with_alias(manager):
    """测试别名映射功能"""
    tool_meta = {"tool5": ToolMeta(alias="aliased_tool")}
    servers = [create_server_config("alias_server", tool_meta=tool_meta)]
    await manager.ainitialize(servers)
    await manager.astart_all()

    # 验证 ExposedToolMapping：alias 仅替换工具名部分，仍带 {bundle_id}__ 前缀
    assert manager._exposed_tools["alias_server__aliased_tool"] == ("alias_server", "tool5")
    assert "alias_server__tool5" not in manager._exposed_tools

    # 执行别名工具（以 exposed 名寻址，解析回原始名 tool5）
    await manager.aexecute_tool("alias_server__aliased_tool", {})
    client = manager._active_clients["alias_server"]
    print("Call args list:", client.call_tool.call_args_list)
    client.call_tool.assert_awaited_once_with("tool5", {})


@pytest.mark.asyncio
async def test_disabled_tool(manager):
    """测试禁用工具处理"""
    servers = [create_server_config("server1", forbidden_tools=["tool2"])]
    await manager.ainitialize(servers)
    await manager.astart_all()

    # forbidden 工具不进 ExposedToolMapping（不可见不可调用）
    assert "server1__tool2" not in manager._exposed_tools

    # 尝试执行禁用工具 → 未命中 ExposedToolMapping → ValueError（上层映射 4001）
    with pytest.raises(ValueError):
        await manager.aexecute_tool("server1__tool2", {})

    # #106 契约：禁用工具不应再出现在对外暴露面（不可见且不可调用）
    names = [tool.name async for _bid, tool in manager.available_tools()]
    assert "server1__tool2" not in names
    assert "server1__tool1" in names


@pytest.mark.asyncio
async def test_cross_server_same_name_coexist_via_bundle_prefix(manager):
    """BundleID：跨 server 同名工具经 ``{bundle_id}__`` 前缀天然共存，不再抛 ToolNameDuplicatedError。"""
    servers = [
        create_server_config("server1"),
        # duplicate_server 的 duplicate_tool 别名为 tool1；与 server1 的 tool1 因 bundle 前缀不同而共存
        create_server_config("duplicate_server", tool_meta={"duplicate_tool": ToolMeta(alias="tool1")}),
    ]
    await manager.ainitialize(servers)
    await manager.astart_all()

    assert len(manager._active_clients) == 2
    # 两个 "tool1" 借 bundle 前缀共存于暴露面，互不冲突
    assert manager._exposed_tools["server1__tool1"] == ("server1", "tool1")
    assert manager._exposed_tools["duplicate_server__tool1"] == ("duplicate_server", "duplicate_tool")


@pytest.mark.asyncio
async def test_dynamic_server_management(manager):
    """测试动态添加/移除服务器"""
    # 初始配置
    servers = [create_server_config("server1")]
    await manager.ainitialize(servers)
    await manager.astart_all()
    assert "server1" in manager._active_clients

    # 添加新服务器
    new_server = create_server_config("http_server")
    await manager.aadd_or_aupdate_server(new_server)

    # 验证新服务器启动
    assert "http_server" not in manager._active_clients
    await manager._astart_client("http_server")
    assert "http_server" in manager._active_clients

    # 更新服务器配置（启用自动重连）
    updated_server = create_server_config("server1", forbidden_tools=["tool1"])
    # 验证服务器重启
    old_client = manager._active_clients["server1"]  # 要提示保存旧客户端的引用，因为add_or_update_server会销毁旧客户端
    await manager.aadd_or_aupdate_server(updated_server)
    await asyncio.sleep(0.1)  # 等待自动重连 需要释放一次协程才能触发协程任务的执行与调用。

    old_client.adisconnect.assert_awaited()

    # 验证更新应用：forbid tool1 后不再暴露 server1__tool1
    assert "server1__tool1" not in manager._exposed_tools

    # 移除服务器
    await manager.aremove_server("http_server")
    assert "http_server" not in manager._active_clients
    assert "http_server" not in manager._servers_config


@pytest.mark.asyncio
async def test_auto_reconnect_disabled(manager):
    """测试禁用自动重连时更新配置"""
    await manager.disable_auto_reconnect()

    servers = [create_server_config("server1")]
    await manager.ainitialize(servers)
    await manager.astart_all()

    # 尝试更新活动服务器的配置
    updated_config = create_server_config("server1", forbidden_tools=["tool1"])

    with pytest.raises(RuntimeError):
        await manager.aadd_or_aupdate_server(updated_config)


@pytest.mark.asyncio
async def test_get_available_tools(manager):
    """测试获取可用工具"""
    tool_meta = {"tool1": ToolMeta(auto_apply=True)}
    servers = [create_server_config("server1", tool_meta=tool_meta)]
    await manager.ainitialize(servers)
    await manager.astart_all()

    # 获取工具
    tools = []
    async for _bid, tool in manager.available_tools():
        tools.append(tool)

    assert len(tools) == 2
    tool1 = next(t for t in tools if t.name == "server1__tool1")
    assert tool1.meta["a2c_tool_meta"].auto_apply


@pytest.mark.asyncio
async def test_default_tool_meta_applies_when_missing_per_tool(manager):
    """当未提供 per-tool 配置时，应回落使用 default_tool_meta。
    When per-tool meta is missing, default_tool_meta should be applied.
    """
    servers = [create_server_config("server1", default_tool_meta=ToolMeta(auto_apply=True))]
    await manager.ainitialize(servers)
    await manager.astart_all()

    # 检查 available_tools 注入
    tools = []
    async for _bid, tool in manager.available_tools():
        tools.append(tool)
    t1 = next(t for t in tools if t.name == "server1__tool1")
    assert t1.meta["a2c_tool_meta"].auto_apply is True

    # 检查 aexecute_tool 返回元数据注入
    ret = await manager.aexecute_tool("server1__tool1", {})
    assert ret.meta["a2c_tool_meta"].auto_apply is True


@pytest.mark.asyncio
async def test_per_tool_overrides_default(manager):
    """per-tool 配置应覆盖 default_tool_meta 的根级字段。
    Per-tool meta should override default_tool_meta root-level fields.
    """
    servers = [
        create_server_config(
            "server1",
            tool_meta={"tool1": ToolMeta(auto_apply=False)},
            default_tool_meta=ToolMeta(auto_apply=True),
        ),
    ]
    await manager.ainitialize(servers)
    await manager.astart_all()

    ret = await manager.aexecute_tool("server1__tool1", {})
    assert ret.meta["a2c_tool_meta"].auto_apply is False


@pytest.mark.asyncio
async def test_default_tool_meta_alias_ignored_no_collapse(manager, monkeypatch):
    """#151 R1'：``default_tool_meta.alias`` 天生病态（alias 是 per-tool 改名）→ 被忽略，工具各以原始名暴露、一个不丢。

    English: an ``alias`` in ``default_tool_meta`` is inherently ill-formed (alias renames a single tool); it MUST be
    ignored so every tool of the server stays exposed under its own name — never collapsed.

    现码红（塌名 Bug）：default alias 回落到 server1 的 tool1/tool2 → 全塌成 ``server1__custom`` → first-wins →
    tool2 对 Agent 静默不可见/不可调用。修后绿：alias 不从 default 继承 → ``server1__tool1`` 与 ``server1__tool2`` 都暴露；
    且打一次响亮配置诊断（方案 d，与 no-double-open「不静默丢 + 配置诊断」同姿态）。
    """
    import a2c_smcp.computer.mcp_clients.manager as mgr_mod

    # 项目自定义 logger 不向 caplog 传播 → 直接 spy 模块 logger.warning 断言诊断（须在 astart_all 触发刷新前设置）。
    warns: list[str] = []
    monkeypatch.setattr(mgr_mod.logger, "warning", lambda msg, *a, **k: warns.append(str(msg)))

    servers = [create_server_config("server1", default_tool_meta=ToolMeta(alias="custom"))]
    await manager.ainitialize(servers)
    await manager.astart_all()

    names = {tool.name async for _bid, tool in manager.available_tools()}
    # 塌名 Bug：现码只剩一个 'server1__custom'；修后两工具各以原始名暴露、无丢失。
    assert names == {"server1__tool1", "server1__tool2"}, f"default alias 不应塌名/丢工具，实际: {names}"
    # a2c_tool_meta.alias 亦为 None（default 位 alias 被忽略，输出与命名一致、不误导 Agent）。
    tools = {t.name: t async for _bid, t in manager.available_tools()}
    assert tools["server1__tool1"].meta["a2c_tool_meta"].alias is None
    # 响亮配置诊断：命中被忽略的 default alias 时 WARN 一次（方案 d）。
    assert any("default_tool_meta.alias" in w and "custom" in w for w in warns), (
        f"被忽略的 default_tool_meta.alias 必须打配置诊断 WARN，实际 WARN: {warns}"
    )


@pytest.mark.asyncio
async def test_per_tool_alias_wins_over_default_alias_no_collapse(manager):
    """#151 R1' 分支 D：server 配 ``default_tool_meta.alias`` 且 tool1 另配 per-tool alias → per-tool alias 生效、
    default alias 被弃、无塌名。锁死 ``_merged_tool_meta`` 合并分支的 ``merged["alias"] = specific.alias`` 语义。

    English: per-tool alias wins; default alias is discarded; no collapse (locks the both-present merge branch).
    """
    servers = [
        create_server_config(
            "server1",
            tool_meta={"tool1": ToolMeta(alias="renamed1")},
            default_tool_meta=ToolMeta(alias="custom"),
        ),
    ]
    await manager.ainitialize(servers)
    await manager.astart_all()

    names = {tool.name async for _bid, tool in manager.available_tools()}
    # tool1 用 per-tool alias 'renamed1'；tool2 无 per-tool → default alias 被弃 → 原始名 'tool2'。无塌名、无丢失。
    assert names == {"server1__renamed1", "server1__tool2"}, f"实际: {names}"
    # 路由回原始名可解析（per-tool alias 生效但路由目标仍是 tool1）。
    assert manager._exposed_tools["server1__renamed1"] == ("server1", "tool1")


@pytest.mark.asyncio
async def test_error_handling(manager):
    """测试错误处理"""
    # 模拟客户端连接错误
    bad_server = create_server_config("error_server")
    bad_client = MockMCPClient()
    bad_client.list_tools.side_effect = Exception("Connection failed")
    manager.client_factory = lambda _: bad_client

    await manager.aadd_or_aupdate_server(bad_server)

    # 验证状态
    assert "error_server" not in manager._active_clients

    # 工具执行错误处理
    servers = [create_server_config("server1")]
    await manager.ainitialize(servers)
    await manager.astart_all()

    client = manager._active_clients["server1"]
    client.call_tool.side_effect = TimeoutError("Execution timed out")

    with pytest.raises(TimeoutError):
        await manager.aexecute_tool("server1__tool1", {}, timeout=0.1)


@pytest.mark.asyncio
async def test_meta_data_injection(manager):
    """测试工具元数据注入"""
    tool_meta = {"tool1": ToolMeta(ret_object_mapper={"result": "data"})}
    servers = [create_server_config("server1", tool_meta=tool_meta)]
    await manager.ainitialize(servers)
    await manager.astart_all()

    # 执行工具
    result = await manager.aexecute_tool("server1__tool1", {})

    # 验证元数据注入
    assert "a2c_tool_meta" in result.meta
    assert result.meta["a2c_tool_meta"].ret_object_mapper == {"result": "data"}


@pytest.mark.asyncio
async def test_manager_propagates_message_handler_to_clients():
    """验证 Manager 能将 message_handler 透传到具体 Client。
    Verify Manager forwards message_handler to concrete clients.
    """

    # 定义一个占位回调
    async def dummy_handler(*args, **kwargs):
        return None

    mgr = MCPServerManager(message_handler=dummy_handler)
    await mgr.enable_auto_reconnect()

    servers = [create_server_config("server1"), create_server_config("sse_server")]
    await mgr.ainitialize(servers)
    await mgr.astart_all()

    # 校验每个激活客户端都收到了相同的回调实例
    for name, client in mgr._active_clients.items():
        assert getattr(client, "message_handler", None) is dummy_handler, f"Client {name} did not receive message_handler"


@pytest.mark.asyncio
async def test_manager_message_handler_none_results_in_none_on_clients():
    """当未提供 message_handler 时，客户端应为 None。"""
    mgr = MCPServerManager()  # 不传入 handler
    await mgr.enable_auto_reconnect()

    servers = [create_server_config("server1")]
    await mgr.ainitialize(servers)
    await mgr.astart_all()

    client = mgr._active_clients["server1"]
    assert getattr(client, "message_handler", None) is None


# 覆盖 _add_server_config 的 RuntimeError 分支
# Test _add_server_config RuntimeError branch
@pytest.mark.asyncio
async def test_update_active_server_without_reconnect(manager):
    """
    测试：当 auto_reconnect=False 且尝试更新已激活的服务器配置时抛出 RuntimeError。
    Test: Raise RuntimeError when updating active config with auto_reconnect=False.
    """
    config = create_server_config("server1")
    await manager.disable_auto_reconnect()
    manager._servers_config[config.name] = config
    manager._active_clients[config.name] = cast(MCPClientProtocol, MagicMock(spec=MCPClientProtocol))
    with pytest.raises(RuntimeError):
        await manager._add_or_update_server_config(config)


# 覆盖 astart_client 的 ValueError/RuntimeError 分支
# Test astart_client ValueError/RuntimeError branches
@pytest.mark.asyncio
async def test_astart_client_invalid_cases(manager):
    """
    测试：启动未知服务器/禁用服务器时报错。
    Test: Raise ValueError/RuntimeError for unknown or disabled server.
    """
    # 未知服务器
    with pytest.raises(ValueError):
        await manager._astart_client("not_exist")
    # 禁用服务器
    config = create_server_config("server2", disabled=True)
    manager._servers_config[config.name] = config
    with pytest.raises(RuntimeError):
        await manager._astart_client(config.name)


# 覆盖 aexecute_tool 的 PermissionError/ValueError/RuntimeError 分支
# Test aexecute_tool PermissionError/ValueError/RuntimeError branches
@pytest.mark.asyncio
async def test_aexecute_tool_invalid_cases(manager):
    """
    测试：执行被禁用工具/未注册工具/服务器未激活时报错。
    Test: Raise PermissionError/ValueError/RuntimeError for disabled tool, missing tool, or inactive server.
    """
    # 未命中 ExposedToolMapping（含被 forbidden / 未注册工具）→ ValueError（上层映射 4001）
    with pytest.raises(ValueError):
        await manager.aexecute_tool("srv__no_tool", {})
    # 映射命中但服务器未激活 → RuntimeError
    manager._exposed_tools["serverY__toolY"] = ("serverY", "toolY")
    manager._active_clients.clear()
    manager._servers_config["serverY"] = create_server_config("serverY")
    with pytest.raises(RuntimeError):
        await manager.aexecute_tool("serverY__toolY", {})


# 覆盖 aexecute_tool 的 TimeoutError/Exception 分支
# Test aexecute_tool TimeoutError/Exception branches
@pytest.mark.asyncio
async def test_aexecute_tool_timeout_and_exception(manager, monkeypatch):
    """
    测试：执行工具时超时或抛出异常。
    Test: Raise TimeoutError/RuntimeError when tool execution times out or raises.
    """
    config = create_server_config("server3")
    manager._servers_config["server3"] = config
    manager._active_clients["server3"] = cast(MCPClientProtocol, MagicMock(spec=MCPClientProtocol))
    manager._exposed_tools["server3__toolZ"] = ("server3", "toolZ")
    mock_client = manager._active_clients["server3"]
    # 超时
    mock_client.call_tool = AsyncMock(side_effect=asyncio.TimeoutError)
    with pytest.raises(TimeoutError):
        await manager.aexecute_tool("server3__toolZ", {}, timeout=0.01)
    # 其它异常
    mock_client.call_tool = AsyncMock(side_effect=Exception("fail"))
    with pytest.raises(RuntimeError):
        await manager.aexecute_tool("server3__toolZ", {})


# 覆盖 _arefresh_tool_mapping 的 ToolNameDuplicatedError 分支
# Test _arefresh_tool_mapping ToolNameDuplicatedError branch
@pytest.mark.asyncio
async def test_arefresh_builds_exposed_mapping_coexist_same_tool(manager, monkeypatch):
    """BundleID：两 server 同名 duplicate_tool 经 ``{bundle_id}__`` 前缀共存于 ExposedToolMapping，不再抛异常。"""
    config1 = create_server_config("duplicate_server1")
    config2 = create_server_config("duplicate_server2")

    def always_duplicate_tool(_, message_handler=None):
        return MockMCPClient([create_mock_tool("duplicate_tool")], message_handler=message_handler)

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", always_duplicate_tool)
    manager._servers_config = {"duplicate_server1": config1, "duplicate_server2": config2}
    manager._active_clients = {
        "duplicate_server1": always_duplicate_tool(config1),
        "duplicate_server2": always_duplicate_tool(config2),
    }
    await manager._arefresh_tool_mapping()
    assert manager._exposed_tools["duplicate_server1__duplicate_tool"] == ("duplicate_server1", "duplicate_tool")
    assert manager._exposed_tools["duplicate_server2__duplicate_tool"] == ("duplicate_server2", "duplicate_tool")


@pytest.mark.asyncio
async def test_astart_client_same_tool_coexist(manager, monkeypatch):
    """BundleID：两 server 同名工具可同时启动共存（bundle 前缀隔离），不再抛 ToolNameDuplicatedError。"""
    config1 = create_server_config("duplicate_server1")
    config2 = create_server_config("duplicate_server2")

    def always_duplicate_tool(_, message_handler=None):
        return MockMCPClient([create_mock_tool("duplicate_tool")], message_handler=message_handler)

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", always_duplicate_tool)
    await manager.aadd_or_aupdate_server(config1)
    await manager.aadd_or_aupdate_server(config2)
    assert not manager._active_clients
    await manager.astart_client("duplicate_server1")
    assert len(manager._active_clients) == 1
    await manager.astart_client("duplicate_server2")  # 不再冲突，共存
    assert len(manager._active_clients) == 2
    assert "duplicate_server1__duplicate_tool" in manager._exposed_tools
    assert "duplicate_server2__duplicate_tool" in manager._exposed_tools


# 覆盖 aremove_server 的删除不存在服务器分支
# Test aremove_server deleting non-existent server
@pytest.mark.asyncio
async def test_aremove_server_not_exist(manager):
    """
    测试：移除不存在的服务器配置应抛出 KeyError。
    Test: Raise KeyError when removing a non-existent server config.
    """
    with pytest.raises(KeyError):
        await manager.aremove_server("not_exist")


@pytest.mark.asyncio
async def test_astart_all_same_tool_coexist(manager, monkeypatch):
    """BundleID：astart_all 启动两个同名工具 server，经 bundle 前缀共存，不再抛异常。"""
    await manager.aclose()
    config1 = create_server_config("dup_server1")
    config2 = create_server_config("dup_server2")

    def always_duplicate_tool(_, message_handler=None):
        return MockMCPClient([create_mock_tool("duplicate_tool")], message_handler=message_handler)

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", always_duplicate_tool)
    await manager.ainitialize([config1, config2])
    await manager.astart_all()
    assert len(manager._active_clients) == 2
    assert "dup_server1__duplicate_tool" in manager._exposed_tools
    assert "dup_server2__duplicate_tool" in manager._exposed_tools


@pytest.mark.asyncio
async def test_aadd_or_aupdate_server_same_tool_coexist(manager, monkeypatch):
    """BundleID：add/update 遇同名工具不再抛 ToolNameDuplicatedError / 回滚——经 ``{bundle_id}__`` 前缀共存。"""
    await manager.enable_auto_connect()
    config1 = create_server_config("server1")
    await manager.ainitialize([config1])
    await manager.astart_all()

    def duplicate_tool_factory(config: MCPServerConfig, message_handler=None) -> Any:
        return MockMCPClient([create_mock_tool("tool1")], message_handler=message_handler)  # 两 server 都返回 tool1

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", duplicate_tool_factory)

    # 添加新服务器：不再冲突/回滚，直接共存
    config2 = create_server_config("server2")
    await manager.aadd_or_aupdate_server(config2)
    assert "server2" in manager._servers_config
    assert "server2" in manager._active_clients

    # server1 原客户端由 mock_client_factory 建（tool1/tool2），server2 由 duplicate_tool_factory 建（tool1）
    names = {t.name async for _bid, t in manager.available_tools()}
    assert {"server1__tool1", "server1__tool2", "server2__tool1"} <= names

    # 借 alias 把 server2 的 tool1 改名（仍带 bundle 前缀）
    config2_aliased = create_server_config("server2", tool_meta={"tool1": ToolMeta(alias="renamed")})
    await manager.aadd_or_aupdate_server(config2_aliased)
    names2 = {t.name async for _bid, t in manager.available_tools()}
    assert "server2__renamed" in names2
    assert "server2__tool1" not in names2


@pytest.mark.asyncio
async def test_acall_tool_with_vrl_context_injection(manager, monkeypatch):
    """
    测试：acall_tool在VRL转换时注入tool_name和parameters
    Test: acall_tool injects tool_name and parameters during VRL transformation
    """
    import json

    from mcp.types import TextContent

    from a2c_smcp.computer.mcp_clients.model import A2C_VRL_TRANSFORMED

    # 中文: 创建带VRL脚本的配置，脚本会提取tool_name和parameters
    # English: Create config with VRL script that extracts tool_name and parameters
    vrl_script = """
    .context = {
        "tool": .tool_name,
        "params": .parameters
    }
    """

    # 中文: 创建mock客户端，返回带内容的结果
    # English: Create mock client that returns result with content
    def vrl_test_factory(config: MCPServerConfig, message_handler=None) -> Any:
        mock_result = CallToolResult(
            content=[TextContent(text="test result", type="text")],
            isError=False,
        )
        client = MockMCPClient([create_mock_tool("test_tool")], message_handler=message_handler)
        client.call_tool = AsyncMock(return_value=mock_result)
        return client

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", vrl_test_factory)

    # 中文: 创建配置并初始化
    # English: Create config and initialize
    config = StdioServerConfig(
        name="vrl_test_server",
        disabled=False,
        forbidden_tools=[],
        tool_meta={},
        vrl=vrl_script,
        server_parameters=MagicMock(spec=StdioServerParameters),
    )

    await manager.enable_auto_connect()
    await manager.ainitialize([config])
    await manager.astart_all()

    # 中文: 调用工具，传入参数
    # English: Call tool with parameters
    test_params = {"query": "test query", "limit": 10}
    result = await manager.acall_tool("vrl_test_server", "test_tool", test_params)

    # 中文: 验证VRL转换结果包含tool_name和parameters
    # English: Verify VRL transformation result contains tool_name and parameters
    assert result.meta is not None
    assert A2C_VRL_TRANSFORMED in result.meta

    transformed = json.loads(result.meta[A2C_VRL_TRANSFORMED])
    assert "context" in transformed
    assert transformed["context"]["tool"] == "test_tool"
    assert transformed["context"]["params"] == test_params


@pytest.mark.asyncio
async def test_acall_tool_vrl_conditional_on_tool_name(manager, monkeypatch):
    """
    测试：VRL脚本基于tool_name执行条件逻辑
    Test: VRL script executes conditional logic based on tool_name
    """
    import json

    from mcp.types import TextContent

    from a2c_smcp.computer.mcp_clients.model import A2C_VRL_TRANSFORMED

    # 中文: VRL脚本根据tool_name设置不同的result_type
    # English: VRL script sets different result_type based on tool_name
    vrl_script = """
    if .tool_name == "search" {
        .result_type = "search_result"
        .query = .parameters.query
    } else if .tool_name == "execute" {
        .result_type = "execution_result"
        .command = .parameters.cmd
    } else {
        .result_type = "unknown"
    }
    """

    def conditional_vrl_factory(config: MCPServerConfig, message_handler=None) -> Any:
        mock_result = CallToolResult(
            content=[TextContent(text="result", type="text")],
            isError=False,
        )
        client = MockMCPClient(
            [create_mock_tool("search"), create_mock_tool("execute"), create_mock_tool("other")],
            message_handler=message_handler,
        )
        client.call_tool = AsyncMock(return_value=mock_result)
        return client

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", conditional_vrl_factory)

    config = StdioServerConfig(
        name="conditional_server",
        disabled=False,
        forbidden_tools=[],
        tool_meta={},
        vrl=vrl_script,
        server_parameters=MagicMock(spec=StdioServerParameters),
    )

    await manager.enable_auto_connect()
    await manager.ainitialize([config])
    await manager.astart_all()

    # 中文: 测试search工具
    # English: Test search tool
    result1 = await manager.acall_tool("conditional_server", "search", {"query": "test"})
    transformed1 = json.loads(result1.meta[A2C_VRL_TRANSFORMED])
    assert transformed1["result_type"] == "search_result"
    assert transformed1["query"] == "test"

    # 中文: 测试execute工具
    # English: Test execute tool
    result2 = await manager.acall_tool("conditional_server", "execute", {"cmd": "ls"})
    transformed2 = json.loads(result2.meta[A2C_VRL_TRANSFORMED])
    assert transformed2["result_type"] == "execution_result"
    assert transformed2["command"] == "ls"

    # 中文: 测试其他工具
    # English: Test other tool
    result3 = await manager.acall_tool("conditional_server", "other", {})
    transformed3 = json.loads(result3.meta[A2C_VRL_TRANSFORMED])
    assert transformed3["result_type"] == "unknown"


@pytest.mark.asyncio
async def test_acall_tool_vrl_with_nested_parameters(manager, monkeypatch):
    """
    测试：VRL脚本访问嵌套的parameters字段
    Test: VRL script accesses nested parameters fields
    """
    import json

    from mcp.types import TextContent

    from a2c_smcp.computer.mcp_clients.model import A2C_VRL_TRANSFORMED

    # 中文: VRL脚本访问嵌套的parameters
    # English: VRL script accesses nested parameters
    vrl_script = """
    .user_info = {
        "id": .parameters.user.id,
        "name": .parameters.user.name
    }
    .options = .parameters.options
    """

    def nested_params_factory(config: MCPServerConfig, message_handler=None) -> Any:
        mock_result = CallToolResult(
            content=[TextContent(text="result", type="text")],
            isError=False,
        )
        client = MockMCPClient([create_mock_tool("nested_tool")], message_handler=message_handler)
        client.call_tool = AsyncMock(return_value=mock_result)
        return client

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", nested_params_factory)

    config = StdioServerConfig(
        name="nested_server",
        disabled=False,
        forbidden_tools=[],
        tool_meta={},
        vrl=vrl_script,
        server_parameters=MagicMock(spec=StdioServerParameters),
    )

    await manager.enable_auto_connect()
    await manager.ainitialize([config])
    await manager.astart_all()

    # 中文: 调用工具，传入嵌套参数
    # English: Call tool with nested parameters
    nested_params = {"user": {"id": 123, "name": "Alice"}, "options": {"enabled": True, "timeout": 30}}

    result = await manager.acall_tool("nested_server", "nested_tool", nested_params)
    transformed = json.loads(result.meta[A2C_VRL_TRANSFORMED])

    assert transformed["user_info"]["id"] == 123
    assert transformed["user_info"]["name"] == "Alice"
    assert transformed["options"]["enabled"] is True
    assert transformed["options"]["timeout"] == 30


# ── #106 回归：alias 必须反映到对外暴露的工具名 / alias must surface as the exposed tool name ──
@pytest.mark.asyncio
async def test_available_tools_exposes_alias_as_name(manager):
    """#106 复现：配置 alias 后，available_tools 暴露的 Tool.name 应为 display_name（alias），而非原始名。

    Repro for #106: once an alias is configured, available_tools must expose the display_name (alias)
    as Tool.name, not the original name. Downstream agents address tools by this name, so含连字符 /
    冲突的原始名才能借 alias 适配下游命名约束。
    """
    tool_meta = {"tool5": ToolMeta(alias="aliased_tool")}
    servers = [create_server_config("alias_server", tool_meta=tool_meta)]
    await manager.ainitialize(servers)
    await manager.astart_all()

    names = [tool.name async for _bid, tool in manager.available_tools()]
    # 暴露面应为 {bundle_id}__{alias}，原始名不得出现 / exposed name = {bundle_id}__{alias}, never the original
    assert "alias_server__aliased_tool" in names
    assert "alias_server__tool5" not in names


@pytest.mark.asyncio
async def test_forbidden_tool_excluded_from_duplicate_detection(manager, monkeypatch):
    """#106 附带缺陷复现：被 forbid 的工具不应再参与跨 server 重名冲突检测。

    Repro for #106 secondary defect: a forbidden tool must not participate in cross-server duplicate
    detection. 两个 server 都原生暴露同名 ``tool1``，其中一侧 forbid 之后，应能消除 ToolNameDuplicatedError。
    """

    def both_expose_tool1(config: MCPServerConfig, message_handler=None) -> Any:
        return MockMCPClient([create_mock_tool("tool1")], message_handler=message_handler)

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", both_expose_tool1)

    # server1 forbid tool1，server2 正常暴露 tool1 → 不应再冲突
    servers = [
        create_server_config("server1", forbidden_tools=["tool1"]),
        create_server_config("server2"),
    ]
    await manager.ainitialize(servers)
    await manager.astart_all()

    # server1 forbid tool1（不进表），server2 正常暴露 → 仅 server2__tool1
    assert "server1__tool1" not in manager._exposed_tools
    assert manager._exposed_tools["server2__tool1"] == ("server2", "tool1")
    bundle_id, original = await manager.avalidate_tool_call("server2__tool1", {})
    assert (bundle_id, original) == ("server2", "tool1")
    # 暴露面只出现一次 server2__tool1（server1 那份被 forbid、不暴露）
    names = [tool.name async for _bid, tool in manager.available_tools()]
    assert names.count("server2__tool1") == 1
    assert "server1__tool1" not in names


@pytest.mark.asyncio
async def test_forbidden_original_name_suppresses_alias(manager):
    """#106 边界：对原始名 forbid 时，即便该工具配了 alias，alias 也不应暴露（forbid 优先于 alias）。

    Boundary (#106): forbidding by the original name suppresses an aliased exposure too — forbid wins over alias,
    so an aliased tool cannot be used to bypass a forbid on its underlying tool.
    """
    tool_meta = {"tool5": ToolMeta(alias="aliased_tool")}
    servers = [create_server_config("alias_server", tool_meta=tool_meta, forbidden_tools=["tool5"])]
    await manager.ainitialize(servers)
    await manager.astart_all()

    # alias 与原始名都不暴露、不路由 / neither the alias nor the original is exposed/routed
    names = [tool.name async for _bid, tool in manager.available_tools()]
    assert "alias_server__aliased_tool" not in names
    assert "alias_server__tool5" not in names
    assert "alias_server__aliased_tool" not in manager._exposed_tools
    assert "alias_server__tool5" not in manager._exposed_tools


@pytest.mark.asyncio
async def test_forbidden_tool_not_exposed_and_uncallable(manager):
    """forbid 工具后：不进 ExposedToolMapping、不可调用（未命中 → ValueError，上层映射 4001）。"""
    servers = [create_server_config("server1", forbidden_tools=["tool2"])]
    await manager.ainitialize(servers)
    await manager.astart_all()

    assert "server1__tool2" not in manager._exposed_tools
    with pytest.raises(ValueError):
        await manager.aexecute_tool("server1__tool2", {})


# ── #184 启动/连接状态正交化 测试 / Activation/connection orthogonality tests ──


@pytest.mark.asyncio
async def test_activation_intent_persists_on_connect_failure(manager):
    """start 失败后 activation intent 仍保留，连接状态为 error。"""
    _FAIL_CONNECT_SERVERS.add("server1")
    try:
        servers = [create_server_config("server1")]
        await manager.ainitialize(servers)

        with pytest.raises(Exception):
            await manager.astart_client("server1")

        # activation intent 已记录
        assert "server1" in manager._activation_intents
        # 但 active_clients 中没有
        assert "server1" not in manager._active_clients
        # connection state 为 error
        assert manager._connection_states["server1"] == MCPServerConnectionState.ERROR
    finally:
        _FAIL_CONNECT_SERVERS.discard("server1")


@pytest.mark.asyncio
async def test_stop_clears_activation_intent(manager):
    """stop 同时清除 activation intent + connection state。"""
    servers = [create_server_config("server1")]
    await manager.ainitialize(servers)
    await manager.astart_client("server1")

    assert "server1" in manager._activation_intents
    assert "server1" in manager._active_clients

    await manager.astop_client("server1")

    assert "server1" not in manager._activation_intents
    assert "server1" not in manager._active_clients
    assert "server1" not in manager._connection_states


@pytest.mark.asyncio
async def test_stop_all_includes_activation_only(manager):
    """stop_all 覆盖未连接但已启动的 server（activation-only）。"""
    _FAIL_CONNECT_SERVERS.add("server1")
    try:
        servers = [create_server_config("server1"), create_server_config("server2")]
        await manager.ainitialize(servers)

        # server1: start 失败，但有 activation intent
        with pytest.raises(Exception):
            await manager.astart_client("server1")
        # server2: 正常启动
        await manager.astart_client("server2")

        assert "server1" in manager._activation_intents
        assert "server2" in manager._activation_intents

        await manager.astop_all()

        # 两者都清除了
        assert "server1" not in manager._activation_intents
        assert "server2" not in manager._activation_intents
    finally:
        _FAIL_CONNECT_SERVERS.discard("server1")


@pytest.mark.asyncio
async def test_get_server_runtime_statuses_activation_started(manager):
    """get_server_runtime_statuses 正确反映 activation STARTED 状态。"""
    servers = [create_server_config("server1")]
    await manager.ainitialize(servers)
    await manager.astart_client("server1")

    statuses = manager.get_server_runtime_statuses()
    assert len(statuses) == 1
    s = statuses[0]
    assert s.bundle_id == "server1"
    assert s.name == "server1"
    assert s.activation == MCPServerActivationState.STARTED
    assert s.connection == MCPServerConnectionState.CONNECTED
    assert s.is_started() is True
    assert s.is_connected() is True


@pytest.mark.asyncio
async def test_get_server_runtime_statuses_stopped(manager):
    """未启动的 server 显示 STOPPED + DISCONNECTED。"""
    servers = [create_server_config("server1")]
    await manager.ainitialize(servers)

    statuses = manager.get_server_runtime_statuses()
    assert len(statuses) == 1
    s = statuses[0]
    assert s.activation == MCPServerActivationState.STOPPED
    assert s.connection == MCPServerConnectionState.DISCONNECTED
    assert s.is_started() is False
    assert s.is_connected() is False


@pytest.mark.asyncio
async def test_get_server_runtime_statuses_connect_failure(manager):
    """连接失败后显示 STARTED + ERROR，activation intent 保留。"""
    _FAIL_CONNECT_SERVERS.add("server1")
    try:
        servers = [create_server_config("server1")]
        await manager.ainitialize(servers)

        with pytest.raises(Exception):
            await manager.astart_client("server1")

        statuses = manager.get_server_runtime_statuses()
        s = statuses[0]
        assert s.activation == MCPServerActivationState.STARTED
        assert s.connection == MCPServerConnectionState.ERROR
        assert s.is_started() is True
        assert s.is_connected() is False
    finally:
        _FAIL_CONNECT_SERVERS.discard("server1")


def test_get_server_status_backward_compat(manager):
    """旧 get_server_status() 返回格式不变：4-tuple (bundle_id, name, bool, str)。"""
    servers = [create_server_config("server1")]
    manager._servers_config = {s.name: s for s in servers}

    statuses = manager.get_server_status()
    assert len(statuses) == 1
    bid, name, is_started, state = statuses[0]
    assert bid == "server1"
    assert name == "server1"
    assert is_started is False  # 未启动
    assert state == "disconnected"

    # 添加激活意图后
    manager._activation_intents.add("server1")
    manager._connection_states["server1"] = MCPServerConnectionState.CONNECTING
    statuses = manager.get_server_status()
    _, _, is_started, state = statuses[0]
    assert is_started is True
    assert state == "connecting"


def test_clear_all_clears_new_fields():
    """_clear_all 清空 activation_intents 和 connection_states。"""
    manager = MCPServerManager()
    manager._activation_intents.add("test")
    manager._connection_states["test"] = MCPServerConnectionState.CONNECTING
    manager._exposed_tools["k"] = ("test", "t")

    manager._clear_all()

    assert len(manager._activation_intents) == 0
    assert len(manager._connection_states) == 0
    assert len(manager._exposed_tools) == 0


def test_mcpserver_runtime_status_helpers():
    """MCPServerRuntimeStatus.is_started / is_connected 行为正确。"""
    started_connected = MCPServerRuntimeStatus(
        bundle_id="a", name="A",
        activation=MCPServerActivationState.STARTED,
        connection=MCPServerConnectionState.CONNECTED,
    )
    assert started_connected.is_started() is True
    assert started_connected.is_connected() is True

    stopped = MCPServerRuntimeStatus(
        bundle_id="b", name="B",
        activation=MCPServerActivationState.STOPPED,
        connection=MCPServerConnectionState.DISCONNECTED,
    )
    assert stopped.is_started() is False
    assert stopped.is_connected() is False

    started_auth = MCPServerRuntimeStatus(
        bundle_id="c", name="C",
        activation=MCPServerActivationState.STARTED,
        connection=MCPServerConnectionState.AUTHORIZATION_REQUIRED,
    )
    assert started_auth.is_started() is True
    assert started_auth.is_connected() is False


def test_mcpserver_runtime_status_immutable():
    """frozen dataclass 不可变。"""
    s = MCPServerRuntimeStatus(
        bundle_id="a", name="A",
        activation=MCPServerActivationState.STARTED,
        connection=MCPServerConnectionState.CONNECTED,
    )
    with pytest.raises(Exception):
        s.activation = MCPServerActivationState.STOPPED  # type: ignore[misc]


# ==============================================================================
# #199：ToolMeta tags 三层合并（Server 声明层）——protocol#51 / PR#57 裁决已定；wire 契约见
# a2c-smcp-protocol develop data-structures.md §ToolMeta 三层合并规则；对拍 rust-sdk#200（V9）。
# ==============================================================================

# conformance fixture 形态：Tool._meta["a2c_tool_meta"] 声明（原生 dict，白名单仅 tags）。
# Server 自声明 auto_apply=true 必须经字段级过滤后彻底消失（提权向量，裁决不变量 2）。
_DECLARED_TOOL_META: dict[str, Any] = {"custom_key": "v", "a2c_tool_meta": {"tags": ["read"], "auto_apply": True}}


def _canonical(tool: Tool) -> dict:
    """reconcile 后 ``a2c_tool_meta`` 的 wire canonical 形式（全字段含 null，key 序不参与判定）。"""
    return tool.meta["a2c_tool_meta"].model_dump(mode="json")


def _declared_tool_meta() -> Any:
    """V1/V6/V8 共用的合法声明 fixture（即 conformance §2.6 fixture server 形态）。"""
    return dict(_DECLARED_TOOL_META)


def test_merged_tool_meta_three_layer_server_tags_fallback():
    """#199 V2/V3/V4/V10：三层合并语义（``_merged_tool_meta`` 直测，免 manager 启动）。

    ``tool_meta[tool] > default_tool_meta > Server 声明``；tags 按字段整体替换不 union；
    缺失/null 继承下一层、``[]`` 显式清除（裁决④，无需三态）。
    """
    server = _ServerDeclaredToolMeta(tags=["read"])

    # V2: default 覆盖 Server 声明
    config = create_server_config("server1", default_tool_meta=ToolMeta(tags=["default"]))
    assert MCPServerManager._merged_tool_meta(config, "tool1", server_declared=server).tags == ["default"]

    # V3: per-tool 覆盖 default（Server 声明恒最低层）
    config = create_server_config(
        "server1",
        tool_meta={"tool1": ToolMeta(tags=["specific"])},
        default_tool_meta=ToolMeta(tags=["default"]),
    )
    assert MCPServerManager._merged_tool_meta(config, "tool1", server_declared=server).tags == ["specific"]

    # V4: per-tool [] 显式清除所有下层值（不回落 Server 声明）
    config = create_server_config(
        "server1",
        tool_meta={"tool1": ToolMeta(tags=[])},
        default_tool_meta=ToolMeta(tags=["default"]),
    )
    assert MCPServerManager._merged_tool_meta(config, "tool1", server_declared=server).tags == []

    # V10: per-tool 显式 null 与缺失等价 → 继承 default（Server 声明仍最低层）
    config = create_server_config(
        "server1",
        tool_meta={"tool1": ToolMeta(tags=None)},
        default_tool_meta=ToolMeta(tags=["default"]),
    )
    assert MCPServerManager._merged_tool_meta(config, "tool1", server_declared=server).tags == ["default"]

    # V5: 配置只含 auto_apply（无 tags）→ tags 仍回落 Server 声明；auto_apply 来自配置
    config = create_server_config("server1", tool_meta={"tool1": ToolMeta(auto_apply=True)})
    merged = MCPServerManager._merged_tool_meta(config, "tool1", server_declared=server)
    assert merged.tags == ["read"]
    assert merged.auto_apply is True


def test_merged_tool_meta_no_config_tags_only_canonical():
    """#199 合法声明 + 无配置 → tags-only canonical（全字段含 null）；无声明 → None（现状不变）。

    V9 对拍锚点：canonical 出线形式钉死（全字段含 null，双 SDK 同形）。
    """
    config = create_server_config("server1")
    merged = MCPServerManager._merged_tool_meta(config, "tool1", server_declared=_ServerDeclaredToolMeta(tags=["read"]))
    assert merged is not None
    assert merged.model_dump(mode="json") == {
        "auto_apply": None,
        "alias": None,
        "tags": ["read"],
        "ret_object_mapper": None,
    }
    # server_declared=None（默认参数）→ config-only 语义不变（裁决③：acall_tool / get_tool_meta /
    # _arefresh_tool_mapping 三个消费点零行为差）
    assert MCPServerManager._merged_tool_meta(config, "tool1") is None


@pytest.mark.asyncio
async def test_available_tools_server_declared_tags_vector1(manager):
    """#199 conformance §2.6 V1：无任何配置 + Server 声明 tags → tags-only canonical 覆写；
    白名单外字段（auto_apply=true）字段级过滤**不进入终值**（裁决不变量 2）；原生 ``_meta`` key 原样保留。
    """
    _DECLARED_TOOLS["decl_server"] = [create_mock_tool("safe_read", meta=_declared_tool_meta())]
    try:
        servers = [create_server_config("decl_server")]
        await manager.ainitialize(servers)
        await manager.astart_all()

        tools = {t.name: t async for _bid, t in manager.available_tools()}
        assert "decl_server__safe_read" in tools
        tool = tools["decl_server__safe_read"]
        assert tool.meta["custom_key"] == "v"
        assert _canonical(tool) == {
            "auto_apply": None,
            "alias": None,
            "tags": ["read"],
            "ret_object_mapper": None,
        }
    finally:
        _DECLARED_TOOLS.pop("decl_server", None)


@pytest.mark.asyncio
async def test_available_tools_declared_auto_apply_config_wins(manager):
    """#199 V6：声明含 auto_apply=true（任意配置）→ 终值 auto_apply 恒来自配置，声明值不进入终值。"""
    _DECLARED_TOOLS["decl_server"] = [create_mock_tool("safe_read", meta=_declared_tool_meta())]
    try:
        servers = [create_server_config("decl_server", tool_meta={"safe_read": ToolMeta(auto_apply=False)})]
        await manager.ainitialize(servers)
        await manager.astart_all()

        tools = {t.name: t async for _bid, t in manager.available_tools()}
        assert _canonical(tools["decl_server__safe_read"])["auto_apply"] is False
    finally:
        _DECLARED_TOOLS.pop("decl_server", None)


@pytest.mark.asyncio
async def test_available_tools_native_meta_preserved_canonical_replaced(manager):
    """#199 V8：声明合法 + 配置覆盖 → 原生 ``_meta`` key（custom_key）原样保留；
    原 ``a2c_tool_meta`` 被 canonical **整体覆写**（不字段级残留——声明 auto_apply 消失）。"""
    _DECLARED_TOOLS["decl_server"] = [create_mock_tool("safe_read", meta=_declared_tool_meta())]
    try:
        servers = [create_server_config(
            "decl_server",
            tool_meta={"safe_read": ToolMeta(tags=["specific"], auto_apply=False)},
        )]
        await manager.ainitialize(servers)
        await manager.astart_all()

        tools = {t.name: t async for _bid, t in manager.available_tools()}
        tool = tools["decl_server__safe_read"]
        assert tool.meta["custom_key"] == "v"
        assert _canonical(tool) == {
            "auto_apply": False,
            "alias": None,
            "tags": ["specific"],
            "ret_object_mapper": None,
        }
    finally:
        _DECLARED_TOOLS.pop("decl_server", None)


@pytest.mark.asyncio
async def test_available_tools_malformed_declaration_discarded_no_config(manager, monkeypatch):
    """#199 V7a：畸形声明（tags 非 list[str] / 声明非对象）→ 丢弃 + 诊断；tools/list 正常、
    工具可用；无配置终值 → ``a2c_tool_meta`` 键**删除**；同 server 多畸形工具诊断**至多一次**（防刷屏）。"""
    import a2c_smcp.computer.mcp_clients.manager as mgr_mod

    warns: list[str] = []
    monkeypatch.setattr(mgr_mod.logger, "warning", lambda msg, *a, **k: warns.append(str(msg)))

    _DECLARED_TOOLS["malformed_server"] = [
        create_mock_tool("bad_tags", meta={"a2c_tool_meta": {"tags": "read", "auto_apply": True}}),
        create_mock_tool("bad_shape", meta={"a2c_tool_meta": "not-an-object"}),
        create_mock_tool("good_tool"),
    ]
    try:
        servers = [create_server_config("malformed_server")]
        await manager.ainitialize(servers)
        await manager.astart_all()

        tools = {t.name: t async for _bid, t in manager.available_tools()}
        assert set(tools) == {
            "malformed_server__bad_tags",
            "malformed_server__bad_shape",
            "malformed_server__good_tool",
        }
        assert "a2c_tool_meta" not in tools["malformed_server__bad_tags"].meta
        assert "a2c_tool_meta" not in tools["malformed_server__bad_shape"].meta
        assert "a2c_tool_meta" not in tools["malformed_server__good_tool"].meta
        # 每 server 每次 tools/list 刷新至多一次诊断（#151 R1' 防刷屏先例）
        malformed_warns = [w for w in warns if "畸形 ToolMeta Server 声明" in w]
        assert len(malformed_warns) == 1, f"畸形声明诊断应恰一次，实际: {malformed_warns}"
        assert "malformed_server" in malformed_warns[0]
    finally:
        _DECLARED_TOOLS.pop("malformed_server", None)


@pytest.mark.asyncio
async def test_available_tools_malformed_declaration_config_canonical_kept(manager, monkeypatch):
    """#199 V7b：畸形声明 + 有配置终值 → 维持配置 canonical（现状）；声明值（含 auto_apply）不进入。"""
    import a2c_smcp.computer.mcp_clients.manager as mgr_mod

    warns: list[str] = []
    monkeypatch.setattr(mgr_mod.logger, "warning", lambda msg, *a, **k: warns.append(str(msg)))

    _DECLARED_TOOLS["malformed_server"] = [
        create_mock_tool("bad_tags", meta={"a2c_tool_meta": {"tags": "read", "auto_apply": True}}),
    ]
    try:
        servers = [create_server_config("malformed_server", tool_meta={"bad_tags": ToolMeta(tags=["cfg"])})]
        await manager.ainitialize(servers)
        await manager.astart_all()

        tools = {t.name: t async for _bid, t in manager.available_tools()}
        tool = tools["malformed_server__bad_tags"]
        assert _canonical(tool)["tags"] == ["cfg"]
        assert _canonical(tool)["auto_apply"] is None
        assert len([w for w in warns if "畸形 ToolMeta Server 声明" in w]) == 1
    finally:
        _DECLARED_TOOLS.pop("malformed_server", None)


@pytest.mark.asyncio
async def test_available_tools_null_declaration_discarded(manager, monkeypatch):
    """#199 显式 null 声明（``a2c_tool_meta: null``）属「声明非对象」畸形判据 → 丢弃 + 诊断
    （canonical-final：该 key 最终值恒为 Computer 写入，Server null 不得原样出线）；
    无配置终值 → key **删除**；有配置终值 → 配置 canonical。
    """
    import a2c_smcp.computer.mcp_clients.manager as mgr_mod

    warns: list[str] = []
    monkeypatch.setattr(mgr_mod.logger, "warning", lambda msg, *a, **k: warns.append(str(msg)))

    _DECLARED_TOOLS["null_server"] = [
        create_mock_tool("null_no_config", meta={"a2c_tool_meta": None}),
        create_mock_tool("null_with_config", meta={"a2c_tool_meta": None}),
    ]
    try:
        servers = [create_server_config("null_server", tool_meta={"null_with_config": ToolMeta(tags=["cfg"])})]
        await manager.ainitialize(servers)
        await manager.astart_all()

        tools = {t.name: t async for _bid, t in manager.available_tools()}
        assert set(tools) == {"null_server__null_no_config", "null_server__null_with_config"}
        # 无配置 → key 删除（不得以 "null" 字符串原样出线）
        assert "a2c_tool_meta" not in tools["null_server__null_no_config"].meta
        # 有配置 → 配置 canonical
        assert _canonical(tools["null_server__null_with_config"])["tags"] == ["cfg"]
        # 同 server 两畸形声明 → 诊断恰一次
        assert len([w for w in warns if "畸形 ToolMeta Server 声明" in w]) == 1
    finally:
        _DECLARED_TOOLS.pop("null_server", None)


@pytest.mark.asyncio
async def test_available_tools_malformed_tags_with_non_str_element(manager, monkeypatch):
    """#199 畸形判据 2 子路径：``tags`` 为 list 但含非 ``str`` 元素（``all(isinstance(t, str))`` False）→ 丢弃 + 诊断。"""
    import a2c_smcp.computer.mcp_clients.manager as mgr_mod

    warns: list[str] = []
    monkeypatch.setattr(mgr_mod.logger, "warning", lambda msg, *a, **k: warns.append(str(msg)))

    _DECLARED_TOOLS["malformed_server"] = [
        create_mock_tool("bad_elem", meta={"a2c_tool_meta": {"tags": ["read", 1]}}),
    ]
    try:
        servers = [create_server_config("malformed_server")]
        await manager.ainitialize(servers)
        await manager.astart_all()

        tools = {t.name: t async for _bid, t in manager.available_tools()}
        assert "malformed_server__bad_elem" in tools
        assert "a2c_tool_meta" not in tools["malformed_server__bad_elem"].meta
        assert len([w for w in warns if "畸形 ToolMeta Server 声明" in w]) == 1
    finally:
        _DECLARED_TOOLS.pop("malformed_server", None)


@pytest.mark.asyncio
async def test_available_tools_declared_null_tags_canonical(manager):
    """#199 声明层 ``tags: null``（合法，与缺失等价，继承下一层）→ 无配置时恒写 tags-only canonical
    （全字段 null）——声明 dict 连同白名单外字段（auto_apply）整体被 canonical 覆写、无残留。"""
    _DECLARED_TOOLS["decl_server"] = [
        create_mock_tool("safe_read", meta={"a2c_tool_meta": {"tags": None, "auto_apply": True}}),
    ]
    try:
        servers = [create_server_config("decl_server")]
        await manager.ainitialize(servers)
        await manager.astart_all()

        tools = {t.name: t async for _bid, t in manager.available_tools()}
        assert _canonical(tools["decl_server__safe_read"]) == {
            "auto_apply": None,
            "alias": None,
            "tags": None,
            "ret_object_mapper": None,
        }
    finally:
        _DECLARED_TOOLS.pop("decl_server", None)


@pytest.mark.asyncio
async def test_available_tools_malformed_warns_per_refresh(manager, monkeypatch):
    """#199 诊断「每 server 每次 tools/list 刷新至多一次」：两次 ``available_tools()`` 各打一次（跨刷新不累计抑制）。"""
    import a2c_smcp.computer.mcp_clients.manager as mgr_mod

    warns: list[str] = []
    monkeypatch.setattr(mgr_mod.logger, "warning", lambda msg, *a, **k: warns.append(str(msg)))

    _DECLARED_TOOLS["malformed_server"] = [
        create_mock_tool("bad_tags", meta={"a2c_tool_meta": {"tags": "read"}}),
        create_mock_tool("bad_shape", meta={"a2c_tool_meta": "not-an-object"}),
    ]
    try:
        servers = [create_server_config("malformed_server")]
        await manager.ainitialize(servers)
        await manager.astart_all()

        for _ in range(2):
            {t.name async for _bid, t in manager.available_tools()}
        malformed_warns = [w for w in warns if "畸形 ToolMeta Server 声明" in w]
        assert len(malformed_warns) == 2, f"两次刷新应各 1 条诊断，实际: {malformed_warns}"
    finally:
        _DECLARED_TOOLS.pop("malformed_server", None)
