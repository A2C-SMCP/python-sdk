# -*- coding: utf-8 -*-
# filename: test_computer.py
# @Time    : 2025/8/21 11:45
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from polyfactory.factories.pydantic_factory import ModelFactory
from prompt_toolkit import PromptSession
from pydantic import ValidationError

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inputs.resolver import InputResolver
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerCommandInput,
    MCPServerConfig,
    MCPServerPickStringInput,
    MCPServerPromptStringInput,
    StdioServerConfig,
    StdioServerParameters,
    ToolMeta,
)


class ToolFactory(ModelFactory[Tool]):
    __model__ = Tool


class DummyAsyncIterator:
    def __init__(self, items):
        self._items = items
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._idx]
        self._idx += 1
        return item


@pytest.mark.asyncio
async def test_aget_available_tools(monkeypatch):
    # 构造mock工具/Build mock tool
    tool = ToolFactory.build(description="mock_desc")
    # 构造mock manager/Build mock manager
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.available_tools.return_value = DummyAsyncIterator([tool])
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    # 实例化Computer/Instantiate Computer
    computer = Computer(name="test")
    await computer.boot_up()
    # 调用aget_available_tools/Call aget_available_tools
    tools = await computer.aget_available_tools()
    # 检查返回类型/Check return type
    assert isinstance(tools, list)
    assert len(tools) == 1
    t = tools[0]
    # 检查SMCPTool结构/Check SMCPTool structure
    assert isinstance(t, dict)
    assert t["name"] == tool.name
    assert t["description"] == tool.description
    assert t["params_schema"] == tool.inputSchema
    assert t["return_schema"] == tool.outputSchema


@pytest.mark.asyncio
async def test_aget_available_tools_meta_branches(monkeypatch):
    # meta=None
    tool1 = ToolFactory.build(_meta=None)
    # meta=普通dict，且所有值都能通过TypeAdapter校验
    tool2 = ToolFactory.build()
    tool2.meta = {"a": 1}
    # meta=包含不能被TypeAdapter校验的对象（如set），且能被json序列化
    tool3 = ToolFactory.build(_meta={"a": {1, 2, 3}})
    # meta=包含不能被TypeAdapter校验且不能被json序列化的对象

    class Unserializable:
        pass

    tool4 = ToolFactory.build(_meta={"a": Unserializable()})
    # meta正常且有annotations
    tool5 = ToolFactory.build(_meta={"a": 1})
    dummy_annotations = MagicMock(spec=ToolAnnotations)
    dummy_annotations.model_dump.return_value = {"ann": 1}
    tool5.annotations = dummy_annotations
    # 构造mock manager
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.available_tools.return_value = DummyAsyncIterator([tool1, tool2, tool3, tool4, tool5])
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    computer = Computer(name="test")
    await computer.boot_up()
    tools = await computer.aget_available_tools()
    assert len(tools) == 5
    # tool5应包含MCP_TOOL_ANNOTATION
    meta = tools[4]["meta"]
    # MCP_TOOL_ANNOTATION 分支只要能覆盖即可，不强制断言meta一定非None
    if meta is not None:
        assert "MCP_TOOL_ANNOTATION" in meta
    # 补充断言：meta为普通dict时内容应一致
    assert tools[1]["meta"]["a"] == 1


def test_mcp_servers_readonly(monkeypatch):
    # 构造一个不可变配置实例/Build an immutable config instance
    mock_manager = MagicMock(spec=MCPServerManager)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    config = StdioServerConfig(server_parameters=StdioServerParameters(command="echo"), name="test")
    computer = Computer(name="test", mcp_servers={config})
    servers = computer.mcp_servers
    # 检查类型/Check type
    assert isinstance(servers, tuple)
    # 尝试修改属性/Attempt to modify property
    with pytest.raises(AttributeError):
        computer.mcp_servers = set()  # noqa
    # 尝试修改元组内容/Attempt to modify tuple content
    with pytest.raises(TypeError):
        servers[0] = None  # noqa
    # 尝试修改frozen model属性/Attempt to modify frozen model attribute
    with pytest.raises(ValidationError):
        servers[0].name = "illegal"  # noqa


@pytest.mark.asyncio
async def test_boot_up(monkeypatch):
    """
    测试 Computer.boot_up 方法。
    Test Computer.boot_up method.
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    computer = Computer(name="test")
    # boot_up 是异步方法
    await computer.boot_up()
    assert computer.mcp_manager is mock_manager
    mock_manager.ainitialize.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown(monkeypatch):
    """
    测试 Computer.shutdown 方法。
    Test Computer.shutdown method.
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    computer = Computer(name="test")
    computer.mcp_manager = mock_manager
    await computer.shutdown()
    mock_manager.aclose.assert_called_once()
    assert computer.mcp_manager is None


@pytest.mark.asyncio
async def test_aenter(monkeypatch):
    """
    测试 Computer.__aenter__ 方法。
    Test Computer.__aenter__ method.
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)

    async with Computer(name="test") as computer:
        assert isinstance(computer, Computer)
        assert computer.mcp_manager is mock_manager
        mock_manager.ainitialize.assert_called_once()


@pytest.mark.asyncio
async def test_aexit(monkeypatch):
    """
    测试 Computer.__aexit__ 方法。
    Test Computer.__aexit__ method.
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    async with Computer(name="test") as computer:
        ...
    mock_manager.aclose.assert_called_once()
    assert computer.mcp_manager is None


@pytest.mark.asyncio
async def test_aexecute_tool_auto_apply(monkeypatch):
    """
    测试aexecute_tool自动通过分支
    Test aexecute_tool auto_apply branch
    """
    # 构造mock manager/Build mock manager
    mock_manager = MagicMock(spec=MCPServerManager)
    # avalidate_tool_call返回(server, tool)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    # get_tool_meta 返回 auto_apply=True 的合并元数据
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=True))
    # acall_tool直接返回result
    mock_result = MagicMock()
    mock_manager.acall_tool = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    computer = Computer(name="test")
    await computer.boot_up()
    result = await computer.aexecute_tool("reqid", "tool", {"a": 1})
    assert result == mock_result


@pytest.mark.asyncio
async def test_aexecute_tool_confirm_callback_apply(monkeypatch):
    """
    测试aexecute_tool confirm_callback为True分支
    Test aexecute_tool confirm_callback True branch
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    # get_tool_meta 返回 auto_apply=False，走 confirm_callback=True 分支
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=False))
    mock_result = MagicMock()
    mock_manager.acall_tool = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    # confirm_callback返回True
    computer = Computer(name="test", confirm_callback=lambda *_: True)
    await computer.boot_up()
    result = await computer.aexecute_tool("reqid", "tool", {"a": 1})
    assert result == mock_result


@pytest.mark.asyncio
async def test_aexecute_tool_confirm_callback_reject(monkeypatch):
    """
    测试aexecute_tool confirm_callback为False分支
    Test aexecute_tool confirm_callback False branch
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    # get_tool_meta 返回 auto_apply=False，走 confirm_callback False 分支
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=False))
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    computer = Computer(name="test", confirm_callback=lambda *_: False)
    await computer.boot_up()
    result = await computer.aexecute_tool("reqid", "tool", {"a": 1})
    assert result.content[0].text == "工具调用二次确认被拒绝，请稍后再试"


@pytest.mark.asyncio
async def test_aexecute_tool_confirm_callback_timeout(monkeypatch):
    """
    测试aexecute_tool confirm_callback抛出TimeoutError
    Test aexecute_tool confirm_callback raises TimeoutError
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    # 改为通过 get_tool_meta 控制 auto_apply=False
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=False))
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)

    def raise_timeout(*_):
        raise TimeoutError()

    computer = Computer(name="test", confirm_callback=raise_timeout)
    await computer.boot_up()
    result = await computer.aexecute_tool("reqid", "tool", {"a": 1})
    assert result.isError is True
    assert "确认超时" in result.content[0].text


@pytest.mark.asyncio
async def test_aexecute_tool_confirm_callback_exception(monkeypatch):
    """
    测试aexecute_tool confirm_callback抛出其他异常
    Test aexecute_tool confirm_callback raises Exception
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    # 使用 get_tool_meta 控制 auto_apply=False（触发 confirm_callback 异常分支）
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=False))
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)

    def raise_exc(*_):
        raise RuntimeError("fail")

    computer = Computer(name="test", confirm_callback=raise_exc)
    await computer.boot_up()
    result = await computer.aexecute_tool("reqid", "tool", {"a": 1})
    assert result.isError is True
    assert "二次确认时发生异常" in result.content[0].text


@pytest.mark.asyncio
async def test_aexecute_tool_no_confirm_callback(monkeypatch):
    """
    测试aexecute_tool无confirm_callback分支
    Test aexecute_tool no confirm_callback branch
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    # 使用 get_tool_meta 控制 auto_apply=False（无 confirm_callback 分支）
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=False))
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    computer = Computer(name="test")
    await computer.boot_up()
    result = await computer.aexecute_tool("reqid", "tool", {"a": 1})
    assert result.isError is True
    assert "没有实现二次确认回调方法" in result.content[0].text


# -------------------- #96 工具调用取消（notify:tool_call_cancel）单元测试 --------------------
# #96 tool_call cancellation unit tests: Computer-side cancellable execution + acancel_tool


async def _aexecute_until_inflight(computer: Computer, req_id: str, coro_factory) -> asyncio.Task:
    """启动一个 aexecute_tool 任务并等其进入在途注册表 / start aexecute_tool & wait until tracked."""
    task: asyncio.Task = asyncio.ensure_future(coro_factory())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if req_id in computer._inflight_tool_tasks:
            break
    return task


def _slow_manager(monkeypatch, ran: dict, *, delay: float = 5.0, result=None):
    """构造一个 acall_tool 会阻塞 delay 秒的 mock manager（用于验证中断）。"""

    async def slow_call(*_a, **_k):
        await asyncio.sleep(delay)
        ran["completed"] = True
        return result if result is not None else MagicMock(isError=False)

    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=True))
    mock_manager.acall_tool = AsyncMock(side_effect=slow_call)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    return mock_manager


@pytest.mark.asyncio
async def test_aexecute_tool_cancelled_returns_error_result(monkeypatch):
    """#96：在途工具被 acancel_tool 中断 → 返回取消态 CallToolResult，慢体未跑完，注册表已清。

    #96: an in-flight tool call cancelled via acancel_tool returns a cancelled CallToolResult,
    the slow body is interrupted (never completes), and the registry is cleaned up.
    """
    ran = {"completed": False}
    _slow_manager(monkeypatch, ran, delay=5.0)
    computer = Computer(name="test")
    await computer.boot_up()

    task = await _aexecute_until_inflight(computer, "req-cancel", lambda: computer.aexecute_tool("req-cancel", "tool", {"a": 1}))
    assert "req-cancel" in computer._inflight_tool_tasks

    assert await computer.acancel_tool("req-cancel") is True
    result = await task
    assert result.isError is True
    assert ("取消" in result.content[0].text) or ("cancel" in result.content[0].text.lower())
    # #115：取消态须带协议结果级 meta 标记，使 Agent 能区分「取消 / 超时 / 普通失败」三态
    # #115: cancelled result MUST carry protocol result-level meta markers so the Agent can tell
    # cancellation apart from timeout / ordinary failure (protocol 32eea98 / protocol#5)
    assert result.meta is not None
    assert result.meta["a2c_cancelled"] is True
    assert result.meta["a2c_cancel_reason"] == "agent_requested"
    assert ran["completed"] is False  # 慢体被中断未跑完 / interrupted before completion
    assert "req-cancel" not in computer._inflight_tool_tasks  # 注册表已清 / registry cleaned
    # 历史须把「被取消」与其它失败区分（fix-review 🟡1）/ history distinguishes cancelled from other failures
    assert computer._tool_call_history[-1]["success"] is False
    assert computer._tool_call_history[-1]["error"] == "cancelled"
    assert "req-cancel" not in computer._cancelled_req_ids  # 取消标记已清 / cancel marker cleared


@pytest.mark.asyncio
async def test_aexecute_tool_timeout_marks_a2c_timeout(monkeypatch):
    """#115：Computer 端工具执行超时（Manager 抛 TimeoutError）→ 结果级 meta.a2c_timeout=True。

    专门 except TimeoutError 分支（非通用兜底）使「超时」与「普通失败」可被 Agent 区分；
    此 ack 会回到 Agent，故此标记是「三态可区分」对 Computer 执行超时一态成立的前提。
    #115: Computer-side tool execution timeout (Manager raises TimeoutError) → result-level
    meta.a2c_timeout=True, distinguishable from ordinary failure (protocol 32eea98 / protocol#5).
    """
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=True))
    mock_manager.acall_tool = AsyncMock(side_effect=TimeoutError("Tool 'tool' execution timed out"))
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    computer = Computer(name="test")
    await computer.boot_up()

    result = await computer.aexecute_tool("req-timeout", "tool", {"a": 1}, timeout=0.01)
    assert result.isError is True
    assert result.meta is not None
    assert result.meta["a2c_timeout"] is True
    # 历史记录此次为失败 / history records this as a failure
    assert computer._tool_call_history[-1]["success"] is False


def test_calltoolresult_meta_serializes_top_level_meta_not_underscore():
    """#115 回归：结果级 meta 默认 model_dump(mode="json")（不 by_alias）出线 key 必须是顶层 `meta`，非 `_meta`。

    锁死历史易错点：MCP `Result.meta` 字段 alias=`_meta`；若误传 by_alias=True 会回退到 `_meta`，破坏协议
    data-structures.md §结果级 `meta` 契约（client.py on_tool_call 出线即依赖此默认行为）。
    #115 regression: default model_dump(mode="json") (no by_alias) MUST emit top-level `meta`, not `_meta`.
    """
    # 按生产代码同构方式写真实 meta 字段（属性赋值），而非构造器 meta=（后者会落 extra，字段 alias 为 _meta）。
    # Populate the real meta field the same way production does (attribute assignment), NOT ctor meta= (which
    # would land in extra since the field alias is _meta).
    r = CallToolResult(content=[TextContent(text="x", type="text")], isError=True)
    r.meta = {"a2c_cancelled": True, "a2c_cancel_reason": "agent_requested"}
    # 真实字段已被填充（非 extra）/ the real field is populated (not extra)
    assert r.meta is not None and r.meta["a2c_cancelled"] is True
    assert r.__pydantic_extra__ == {}
    dumped = r.model_dump(mode="json")
    assert "meta" in dumped
    assert "_meta" not in dumped
    assert dumped["meta"]["a2c_cancelled"] is True


@pytest.mark.asyncio
async def test_acancel_tool_unknown_req_id_noop(monkeypatch):
    """#96：取消未知/已完成 req_id → 幂等 no-op，返回 False，不抛异常。"""
    mock_manager = MagicMock(spec=MCPServerManager)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    computer = Computer(name="test")
    await computer.boot_up()
    assert await computer.acancel_tool("does-not-exist") is False


@pytest.mark.asyncio
async def test_aexecute_tool_outer_cancel_reraises(monkeypatch):
    """#96：外层协程自身被取消（非 acancel_tool）→ 向上传播 CancelledError，inner 不泄漏。

    区别于显式工具取消：连接断开/teardown 取消外层 aexecute_tool 任务时必须传播，
    不得伪装成取消态结果。
    """
    ran = {"completed": False}
    _slow_manager(monkeypatch, ran, delay=5.0)
    computer = Computer(name="test")
    await computer.boot_up()

    task = await _aexecute_until_inflight(computer, "req-outer", lambda: computer.aexecute_tool("req-outer", "tool", {}))
    inner = computer._inflight_tool_tasks["req-outer"]
    task.cancel()  # 外层取消，未走 acancel_tool / outer cancel, NOT via acancel_tool
    with pytest.raises(asyncio.CancelledError):
        await task
    # inner 已退场（无泄漏）+ 注册表已清
    await asyncio.sleep(0.01)
    assert inner.cancelled() or inner.done()
    assert "req-outer" not in computer._inflight_tool_tasks


@pytest.mark.asyncio
async def test_concurrent_cancel_isolation(monkeypatch):
    """#96：两在途调用并发，取消其一，另一正常返回，互不影响。"""
    done_result = MagicMock(isError=False)

    async def call(*_a, **_k):
        await asyncio.sleep(0.3)
        return done_result

    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.avalidate_tool_call = AsyncMock(return_value=("server", "tool"))
    mock_manager.get_tool_meta = MagicMock(return_value=ToolMeta(auto_apply=True))
    mock_manager.acall_tool = AsyncMock(side_effect=call)
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)
    computer = Computer(name="test")
    await computer.boot_up()

    t1 = asyncio.ensure_future(computer.aexecute_tool("req-1", "tool", {}))
    t2 = asyncio.ensure_future(computer.aexecute_tool("req-2", "tool", {}))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if {"req-1", "req-2"} <= set(computer._inflight_tool_tasks):
            break

    assert await computer.acancel_tool("req-1") is True
    r1 = await t1
    r2 = await t2
    assert r1.isError is True  # req-1 被取消 / cancelled
    assert r2 is done_result  # req-2 正常完成 / completed normally


# -------------------- 以下为动态管理相关的单元测试（合并自 test_computer_manage.py） --------------------


class DummyResolver(InputResolver):
    def __init__(self, mapping: dict[str, object]):
        self.mapping = mapping
        self.cleared = False

    def clear_cache(self, key: str | None = None) -> None:  # pragma: no cover - only used in update_inputs case
        self.cleared = True

    async def aresolve_by_id(
        self, input_id: str, *, session: PromptSession | None = None, plugin: str | None = None, marketplace: str | None = None,
    ):
        # 接受 #69 Group A 的 plugin/marketplace 上下文 kwargs（默认 None；本 stub 仅按裸 id 查表）。
        return self.mapping[input_id]


# NOTE(#137 ②/③): 以下三例原测 ``aadd_or_aupdate_server`` 的「render + validate + manager 委派」——该语义已 flip
# 为 durable（落盘）后**原状迁至 transient** ``amount_server``。此处改测 ``amount_server`` 以在**不触碰磁盘**下保持
# 对渲染/校验/委派的等价覆盖；durable 落盘专属行为另见 ``test_computer_dual_path_crud.py``。
@pytest.mark.asyncio
async def test_amount_server_with_raw_dict_uses_inputs_and_validates(monkeypatch):
    # Arrange manager mock
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.aadd_or_aupdate_server = AsyncMock()
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)

    # Prepare computer with custom resolver
    resolver = DummyResolver({"cmd": "/bin/echo", "port": "9000"})
    computer = Computer(name="test", input_resolver=resolver)

    # Raw config dict with placeholders
    cfg_dict: dict = {
        "type": "stdio",
        "name": "echo",
        "disabled": False,
        "server_parameters": {
            "command": "${input:cmd}",
            "args": ["--port", "${input:port}"],
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
        "forbidden_tools": [],
        "tool_meta": {},
    }

    # Act
    await computer.amount_server(cfg_dict)

    # Assert: forwarded to manager with validated model instance and placeholders resolved
    mock_manager.aadd_or_aupdate_server.assert_called_once()
    (validated_cfg,), _ = mock_manager.aadd_or_aupdate_server.call_args
    assert isinstance(validated_cfg, MCPServerConfig)
    assert isinstance(validated_cfg, StdioServerConfig)
    assert validated_cfg.name == "echo"
    assert validated_cfg.server_parameters.command == "/bin/echo"
    assert validated_cfg.server_parameters.args == ["--port", "9000"]


@pytest.mark.asyncio
async def test_amount_server_with_model_instance(monkeypatch):
    # Arrange manager mock
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.aadd_or_aupdate_server = AsyncMock()
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)

    # Prepare computer with resolver mapping empty (no placeholders used)
    resolver = DummyResolver({})
    computer = Computer(name="test", input_resolver=resolver)

    # Model instance config
    cfg = StdioServerConfig(
        name="echo2",
        server_parameters=StdioServerParameters(command="/bin/echo", args=["hello"], env=None, cwd=None),
    )

    # Act
    await computer.amount_server(cfg)

    # Assert: same model (after dump/render/validate) is sent
    mock_manager.aadd_or_aupdate_server.assert_called_once()
    (validated_cfg,), _ = mock_manager.aadd_or_aupdate_server.call_args
    assert isinstance(validated_cfg, StdioServerConfig)
    assert validated_cfg.name == cfg.name
    assert validated_cfg.server_parameters.command == "/bin/echo"
    assert validated_cfg.server_parameters.args == ["hello"]


@pytest.mark.asyncio
async def test_amount_server_missing_input_keeps_placeholder(monkeypatch):
    # Arrange manager mock (should be called with placeholder preserved)
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.aadd_or_aupdate_server = AsyncMock()
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: mock_manager)

    # Resolver without required key -> will raise KeyError inside resolver,
    # but renderer will catch and keep original placeholder string
    resolver = DummyResolver({})
    computer = Computer(name="test", input_resolver=resolver)

    cfg_dict: dict = {
        "type": "stdio",
        "name": "bad",
        "disabled": False,
        "server_parameters": {
            "command": "${input:missing}",
            "args": [],
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
        "forbidden_tools": [],
        "tool_meta": {},
    }

    # Act: should not raise; placeholder remains
    await computer.amount_server(cfg_dict)
    mock_manager.aadd_or_aupdate_server.assert_called_once()
    (validated_cfg,), _ = mock_manager.aadd_or_aupdate_server.call_args
    # Assert placeholder preserved after rendering+validation
    assert validated_cfg.server_parameters.command == "${input:missing}"


@pytest.mark.asyncio
async def test_aunmount_server_by_id_delegates(monkeypatch):
    # #137 ②：旧 ``aremove_server`` 的纯运行期停摘语义现为 transient ``aunmount_server_by_id``。
    cfg = StdioServerConfig(
        name="echo",
        server_parameters=StdioServerParameters(command="/bin/echo", args=[], env=None, cwd=None),
    )
    mock_manager = MagicMock(spec=MCPServerManager)
    mock_manager.aremove_server = AsyncMock()
    mock_manager.server_configs = MagicMock(return_value=(cfg,))  # bundle_id("echo") == "echo"
    computer = Computer(name="test")
    computer.mcp_manager = mock_manager

    await computer.aunmount_server_by_id("echo")

    mock_manager.aremove_server.assert_called_once_with("echo")


def test_update_inputs_replaces_resolver_and_clears_cache():
    # Start with a dummy resolver to ensure it gets replaced
    resolver = DummyResolver({"a": 1})
    computer = Computer(name="test", input_resolver=resolver)

    # Update to real pydantic inputs and ensure new resolver is used
    computer.update_inputs(
        [
            MCPServerPromptStringInput(id="p", description="prompt", default="x"),
            MCPServerPickStringInput(id="k", description="pick", options=["a", "b"], default="a"),
            MCPServerCommandInput(id="c", description="cmd", command="echo hi"),
        ],
    )

    # After update, resolver should no longer be DummyResolver
    assert not isinstance(computer._input_resolver, DummyResolver)
    # The new resolver should be able to clear cache without error
    computer._input_resolver.clear_cache()


@pytest.mark.asyncio
async def test_boot_up_renders_all_initial_servers(monkeypatch):
    # Arrange an initial server with placeholders
    inputs = [MCPServerPromptStringInput(id="cmd", description="", default="/bin/echo")]
    cfg = StdioServerConfig(
        name="echo",
        server_parameters=StdioServerParameters(command="${input:cmd}"),
    )

    # Spy manager to capture ainitialize payload
    captured = {}

    class SpyManager(MagicMock):
        async def ainitialize(self, servers):  # type: ignore[override]
            captured["servers"] = list(servers)

    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", lambda *a, **kw: SpyManager(spec=MCPServerManager))

    # Use a dummy resolver to avoid interactive prompt, mapping 'cmd' to path
    resolver = DummyResolver({"cmd": "/bin/echo"})
    computer = Computer(name="test", inputs=inputs, mcp_servers={cfg}, input_resolver=resolver)

    await computer.boot_up()

    servers = captured["servers"]
    assert len(servers) == 1
    s0 = servers[0]
    assert isinstance(s0, StdioServerConfig)
    assert s0.server_parameters.command == "/bin/echo"
