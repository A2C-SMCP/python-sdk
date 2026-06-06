# -*- coding: utf-8 -*-
"""
UAT seed: MCP stdio Server — tools-only, NO resources capability declared.

Axis: MC-NO-RES
Purpose: trigger ErrorCode 4015 (MCP_CAPABILITY_NOT_SUPPORTED) when Agent
         sends client:get_resources targeting this server.
"""

from __future__ import annotations

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

SERVER_NAME = "no-resources-server"


async def run() -> None:
    server = Server(
        name=SERVER_NAME, version="0.0.1",
        instructions="MC-NO-RES: tools-only, no resources capability",
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="ping",
                description="Returns pong",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        return [types.TextContent(type="text", text="pong")]

    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    anyio.run(run)
