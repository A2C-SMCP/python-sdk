# -*- coding: utf-8 -*-
"""
UAT seed: MCP stdio Server — binary image tool_call sideband.

Axis: MC-BIN-* (tool_call binary sideband)
Mode: tools (returns binary image content via call_tool)

提供两个工具：
  - ``big_image``: 返回超 inline_budget 的确定性 PNG 字节（应铸 a2c_blob_handle）
  - ``small_image``: 返回极小 PNG 字节（应原样内联，对照组）

启动：python binary_image_tool_server.py （stdio transport）

协议依据：blob-transfer.md §5（MCP 不可变结构旁路 / _meta.a2c_blob_handle）
"""

import base64

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

BIG_LEN = 32768  # base64 → 43692 bytes > inline_budget (32768), triggers blob handle
SMALL_LEN = 64   # base64 → 88 bytes < inline_budget, stays inline


def det_bytes(n: int) -> bytes:
    """Deterministic byte sequence of length n."""
    return bytes((i * 37 + 11) % 256 for i in range(n))


async def run() -> None:
    server = Server(
        name="uat-seed-binary-image",
        version="0.0.1",
        instructions="MC-BIN binary image tool server for blob-transfer B-04",
    )

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        empty_schema = {"type": "object", "properties": {}}
        return [
            types.Tool(
                name="big_image",
                description="Return an oversize PNG image (> inline_budget)",
                inputSchema=empty_schema,
            ),
            types.Tool(
                name="small_image",
                description="Return a tiny PNG image (<= inline_budget)",
                inputSchema=empty_schema,
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None):
        if name == "big_image":
            data = base64.b64encode(det_bytes(BIG_LEN)).decode("ascii")
            return [types.ImageContent(type="image", data=data, mimeType="image/png")]
        if name == "small_image":
            data = base64.b64encode(det_bytes(SMALL_LEN)).decode("ascii")
            return [types.ImageContent(type="image", data=data, mimeType="image/png")]
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    anyio.run(run)
