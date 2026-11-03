"""
中文: phase 受控、运行期可变工具集的 MCP Stdio 服务器，用于 #127 的集成 / e2e 测试。
英文: A phase-controlled, runtime-mutable-toolset MCP Stdio server for #127 integration / e2e tests.

文件名: mutable_tools_stdio_server.py
作者: JQQ
版权: 2023 JQQ. All rights reserved.
依赖: anyio, mcp
描述:
  暴露一个恒存的控制工具 ``set_phase(phase:int)``——每次调用会把内部 phase 置为入参、随即发出
  ``notifications/tools/list_changed``，从而在运行期改变 ``list_tools()`` 暴露的工具集：

    - phase 1 : [set_phase, dynamic_tool(schemaA: 属性 ``alpha``)]   —— 新增
    - phase 2 : [set_phase, dynamic_tool(schemaB: 属性 ``beta``)]    —— 同名换 schema
    - 其它    : [set_phase]                                          —— 移除

  由此可用一个真实 MCP 子进程驱动「新增 / 同名换 schema / 移除」三类运行期工具变化，且**不伴随任何
  config 变更**（纯 tools/list_changed），用于验证 #127 的全链路刷新。
  #227 可选 --resource-notifications：同时在工具响应前发送资源通知，验证接收回调不会重入死锁。
"""

import argparse
from pathlib import Path

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

# 进程内 phase 状态（由 set_phase 工具调用驱动）/ in-process phase state driven by set_phase calls
_state = {"phase": 0}

_SET_PHASE = types.Tool(
    name="set_phase",
    description="Set the current phase (int) and fire tools/list_changed",
    inputSchema={"type": "object", "properties": {"phase": {"type": "integer"}}, "required": ["phase"]},
)


def _tools_for_phase(phase: int) -> list[types.Tool]:
    """按当前 phase 计算暴露的工具集 / compute the exposed tool set for the given phase."""
    if phase == 1:
        return [
            _SET_PHASE,
            types.Tool(
                name="dynamic_tool",
                description="dynamic tool schema A",
                inputSchema={"type": "object", "properties": {"alpha": {"type": "string"}}},
            ),
        ]
    if phase == 2:
        return [
            _SET_PHASE,
            types.Tool(
                name="dynamic_tool",
                description="dynamic tool schema B",
                inputSchema={"type": "object", "properties": {"beta": {"type": "number"}}},
            ),
        ]
    return [_SET_PHASE]


async def run() -> None:
    """中文: 启动服务器 / 英文: Start the server."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-notifications", choices=["unbound", "list", "content"])
    parser.add_argument("--skill-dir", type=Path)
    options = parser.parse_args()

    def write_skill(phase: int) -> None:
        options.skill_dir.mkdir(parents=True, exist_ok=True)
        (options.skill_dir / "SKILL.md").write_text(
            f"---\nname: demo\ndescription: phase {phase}\n---\n# Demo phase {phase}\n",
            encoding="utf-8",
        )

    if options.resource_notifications:
        write_skill(0)

    server = Server(name="mutable-tools-server", version="0.0.1", instructions="itest-mutable-tools")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return _tools_for_phase(_state["phase"])

    if options.resource_notifications:

        @server.list_resources()
        async def handle_list_resources() -> list[types.Resource]:
            resources: list[types.Resource] = []
            if _state["phase"] or options.resource_notifications == "content":
                resources.append(
                    types.Resource(
                        uri="skill://mutable-srv/demo",
                        name="demo",
                        _meta={"source": "mounted", "mount_dir": str(options.skill_dir)},
                    )
                )
            if _state["phase"]:
                resources.append(types.Resource(uri="window://dynamic", name="dynamic"))
            return resources

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None):
        ctx = server.request_context
        if name == "set_phase":
            _state["phase"] = int((arguments or {}).get("phase", 0))
            # 运行期改变工具集后立即广播列表变更（纯 tools/list_changed，不伴随 config 变更）
            # Broadcast list-changed right after mutating the tool set (pure tools/list_changed, no config change)
            await ctx.session.send_tool_list_changed()
            if options.resource_notifications:
                write_skill(_state["phase"])
                if options.resource_notifications == "content":
                    await ctx.session.send_resource_updated(types.AnyUrl("skill://mutable-srv/demo"))
                else:
                    await ctx.session.send_resource_list_changed()
            return [types.TextContent(type="text", text=f"phase={_state['phase']}")]
        if name == "dynamic_tool" and _state["phase"] in (1, 2):
            return [types.TextContent(type="text", text=f"dynamic phase={_state['phase']}")]
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=True, resources_changed=bool(options.resource_notifications)),
        )
        await server.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    anyio.run(run)
