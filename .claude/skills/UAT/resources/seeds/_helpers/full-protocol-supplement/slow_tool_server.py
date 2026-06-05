#!/usr/bin/env python3
"""Slow tool MCP server for F-12 tool_call_cancel test."""
import anyio, mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
import asyncio
import sys

async def run() -> None:
    server = Server(name="uat-slow-tool", version="0.0.1", instructions="Slow tool for cancel test")

    @server.list_tools()
    async def _():
        return [types.Tool(name="slow_echo", description="Sleeps then echoes",
                           inputSchema={"type":"object","properties":{"msg":{"type":"string"}},"required":["msg"]})]

    @server.call_tool()
    async def _(name: str, arguments: dict | None):
        if name == "slow_echo":
            # 收到 MCP notifications/cancelled 时，server 端 cancel scope 会中断此 sleep；
            # 记录中断日志（满足 F-12「Computer 日志显示工具执行被中断」），并向上传播取消。
            try:
                await asyncio.sleep(10)
            except anyio.get_cancelled_exc_class():
                print("SLOW_ECHO_INTERRUPTED: tool execution cancelled / 工具执行被中断", file=sys.stderr, flush=True)
                raise
            return [types.TextContent(type="text", text=f"done: {arguments.get('msg','')}")]
        return [types.TextContent(type="text", text=f"unknown: {name}")]

    async with stdio_server() as (rs, ws):
        await server.run(rs, ws, server.create_initialization_options())

if __name__ == "__main__":
    anyio.run(run)
