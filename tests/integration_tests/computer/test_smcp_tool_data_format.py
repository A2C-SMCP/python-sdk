# -*- coding: utf-8 -*-
"""
集成测试：验证 Computer.aget_available_tools() 返回的 SMCPTool 数据结构。
此数据即为通过 Socket.IO 发送到 Agent 端 on_tools_received 回调中 tools 参数的内容。

Integration test: Verify the SMCPTool data structure returned by Computer.aget_available_tools().
This data is what the Agent side receives in on_tools_received callback via Socket.IO (GetToolsRet.tools).

用途：Agent 端开发者可以参考此测试中的 print 输出作为 SMCPTool 数据结构的标准格式。
Usage: Agent-side developers can refer to the printed output in this test as the standard SMCPTool format.
"""

import json

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig, ToolMeta


@pytest.mark.anyio
async def test_smcp_tool_data_without_default_tool_meta(stdio_params) -> None:
    """
    场景：模拟 server add 时 default_tool_meta=null 的情况（如 iterm-mcp）。
    Scenario: Simulate server add with default_tool_meta=null (e.g. iterm-mcp).

    此时工具的 meta 不应包含 a2c_tool_meta 字段。
    In this case, tool's meta should NOT contain a2c_tool_meta field.
    """
    cfg_dict = {
        "name": "iterm-mcp",
        "type": "stdio",
        "disabled": False,
        "forbidden_tools": [],
        "tool_meta": {},
        "default_tool_meta": None,
        "vrl": None,
        "server_parameters": {
            "command": stdio_params.command,
            "args": list(stdio_params.args),
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
    }

    computer = Computer(name="test_no_meta", auto_connect=True)
    await computer.aadd_or_aupdate_server(cfg_dict)

    tools = await computer.aget_available_tools()

    print("\n" + "=" * 72)
    print("GetToolsRet.tools (default_tool_meta=null, 如 iterm-mcp)")
    print("此数据即 Agent 端 on_tools_received(computer, tools, sio) 中的 tools")
    print("=" * 72)
    print(json.dumps(tools, indent=2, ensure_ascii=False))
    print("=" * 72)

    assert tools, "工具列表不应为空"
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "params_schema" in tool
        assert "return_schema" in tool
        # default_tool_meta=null 时，meta 中不应有 a2c_tool_meta
        meta = tool.get("meta", {})
        assert "a2c_tool_meta" not in meta, (
            f"default_tool_meta=null 时不应注入 a2c_tool_meta，实际 meta={meta}"
        )

    await computer.shutdown()


@pytest.mark.anyio
async def test_smcp_tool_data_with_default_tool_meta_tags(stdio_params) -> None:
    """
    场景：模拟 server add 时 default_tool_meta 含 tags 的情况（如 playwright）。
    Scenario: Simulate server add with default_tool_meta containing tags (e.g. playwright).

    验证 tags 和 auto_apply 字段在 SMCPTool.meta 中的序列化格式：
    - a2c_tool_meta 的值是 JSON **字符串**（非 dict），因为 SMCPTool.meta 的类型约束为
      Attributes = Mapping[str, AttributeValue]，其中 AttributeValue 仅支持简单类型。
    - Agent 端需要对 meta["a2c_tool_meta"] 执行 json.loads() 才能解析出 tags 等字段。

    Verify tags and auto_apply serialization format in SMCPTool.meta:
    - a2c_tool_meta value is a JSON **string** (not dict), because SMCPTool.meta type is
      Attributes = Mapping[str, AttributeValue], where AttributeValue only supports primitives.
    - Agent side must call json.loads(meta["a2c_tool_meta"]) to parse tags and other fields.
    """
    cfg_dict = {
        "name": "playwright",
        "type": "stdio",
        "disabled": False,
        "forbidden_tools": [],
        "tool_meta": {},
        "default_tool_meta": {"tags": ["browser"], "auto_apply": True},
        "vrl": None,
        "server_parameters": {
            "command": stdio_params.command,
            "args": list(stdio_params.args),
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
    }

    computer = Computer(name="test_with_tags", auto_connect=True)
    await computer.aadd_or_aupdate_server(cfg_dict)

    tools = await computer.aget_available_tools()

    print("\n" + "=" * 72)
    print("GetToolsRet.tools (default_tool_meta={tags:[browser],auto_apply:true})")
    print("此数据即 Agent 端 on_tools_received(computer, tools, sio) 中的 tools")
    print("=" * 72)
    print(json.dumps(tools, indent=2, ensure_ascii=False))
    print("=" * 72)

    assert tools, "工具列表不应为空"
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "params_schema" in tool
        assert "return_schema" in tool

        meta = tool.get("meta", {})
        assert "a2c_tool_meta" in meta, (
            f"配置了 default_tool_meta 后 meta 中应包含 a2c_tool_meta，实际 meta={meta}"
        )

        # a2c_tool_meta 是 JSON 字符串，不是 dict
        raw_value = meta["a2c_tool_meta"]
        assert isinstance(raw_value, str), (
            f"a2c_tool_meta 应为 JSON 字符串，实际类型={type(raw_value).__name__}，值={raw_value}"
        )

        # Agent 端需要 json.loads 才能使用
        parsed = json.loads(raw_value)
        assert isinstance(parsed, dict), f"json.loads 后应为 dict，实际={type(parsed)}"
        assert parsed.get("tags") == ["browser"], f"tags 应为 ['browser']，实际={parsed.get('tags')}"
        assert parsed.get("auto_apply") is True, f"auto_apply 应为 True，实际={parsed.get('auto_apply')}"

    await computer.shutdown()


@pytest.mark.anyio
async def test_smcp_tool_data_with_per_tool_meta(stdio_params) -> None:
    """
    场景：同时配置 default_tool_meta 和 per-tool tool_meta 时的合并行为。
    Scenario: Merge behavior when both default_tool_meta and per-tool tool_meta are configured.

    per-tool 配置优先于 default，但缺失的字段会从 default 回落。
    Per-tool config takes priority over default, but missing fields fall back to default.
    """
    cfg = StdioServerConfig(
        name="merged_meta_server",
        server_parameters=stdio_params,
        default_tool_meta=ToolMeta(tags=["default_tag"], auto_apply=False),
        tool_meta={
            # hello 工具有专门的 per-tool 配置：覆盖 auto_apply，tags 从 default 回落
            "hello": ToolMeta(auto_apply=True),
        },
    )

    computer = Computer(name="test_merged", mcp_servers={cfg})
    await computer.boot_up()

    tools = await computer.aget_available_tools()

    print("\n" + "=" * 72)
    print("GetToolsRet.tools (default_tool_meta + per-tool tool_meta 合并)")
    print("此数据即 Agent 端 on_tools_received(computer, tools, sio) 中的 tools")
    print("=" * 72)
    print(json.dumps(tools, indent=2, ensure_ascii=False))
    print("=" * 72)

    assert tools, "工具列表不应为空"

    # 找到 hello 工具验证合并结果
    hello_tool = next((t for t in tools if t["name"] == "hello"), None)
    assert hello_tool is not None, "应存在 hello 工具"

    meta = hello_tool.get("meta", {})
    assert "a2c_tool_meta" in meta

    parsed = json.loads(meta["a2c_tool_meta"])
    # auto_apply 应被 per-tool 覆盖为 True
    assert parsed.get("auto_apply") is True, f"per-tool auto_apply 应覆盖为 True，实际={parsed}"
    # tags 应从 default 回落得到 ["default_tag"]
    assert parsed.get("tags") == ["default_tag"], f"tags 应从 default 回落为 ['default_tag']，实际={parsed}"

    await computer.shutdown()
