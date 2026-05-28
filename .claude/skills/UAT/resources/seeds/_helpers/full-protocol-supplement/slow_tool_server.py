#!/usr/bin/env python3
"""Slow tool MCP server for F-12 tool_call_cancel test."""
import anyio, mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
import asyncio

async def run() -> None:
    server = Server(name="uat-slow-tool", version="0.0.1", instructions="Slow tool for cancel test")

    @server.list_tools()
    async def _():
        return [types.Tool(name="slow_echo", description="Sleeps then echoes",
                           inputSchema={"type":"object","properties":{"msg":{"type":"string"}},"required":["msg"]})]

    @server.call_tool()
    async def _(name: str, arguments: dict | None):
        if name == "slow_echo":
            await asyncio.sleep(10)
            return [types.TextContent(type="text", text=f"done: {arguments.get('msg','')}")]
        return [types.TextContent(type="text", text=f"unknown: {name}")]

    async with stdio_server() as (rs, ws):
        await server.run(rs, ws, server.create_initialization_options())

if __name__ == "__main__":
    anyio.run(run)
