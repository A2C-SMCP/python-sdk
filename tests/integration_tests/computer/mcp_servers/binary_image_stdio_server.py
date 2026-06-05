"""
中文: 返回二进制图像内容的最小 MCP Stdio 服务器，用于 tool_call 二进制旁路 e2e。
英文: Minimal MCP Stdio server returning binary image content, for the tool_call binary-sideband e2e.

文件名: binary_image_stdio_server.py / filename: binary_image_stdio_server.py
作者: JQQ / author: JQQ
依赖: anyio, mcp / dependencies: anyio, mcp
描述:
  暴露两个工具：``big_image`` 返回超内联预算的确定性 PNG 字节（应走 ``_meta.a2c_blob_handle`` 旁路），
  ``small_image`` 返回极小 PNG 字节（应原样内联，作对照）。字节由确定性序列生成，便于 e2e sha256 断言。
  Exposes ``big_image`` (oversize deterministic PNG bytes → must go via the sideband handle) and
  ``small_image`` (tiny PNG bytes → stays inline) for deterministic sha256 assertions.
"""

import base64

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

# 确定性字节序列长度 / Deterministic payload sizes（big 远超常见 inline_budget，small 远小于）。
BIG_LEN = 4096
SMALL_LEN = 64


def det_bytes(n: int) -> bytes:
    """中文: 生成长度为 n 的确定性字节序列（测试两侧共用以计算期望 sha256）。
    English: Deterministic byte sequence of length n; shared by the test to compute expected sha256.
    """
    return bytes((i * 37 + 11) % 256 for i in range(n))


async def run() -> None:
    server = Server(name="binary-image-itest-server", version="0.0.1", instructions="itest-binary-image")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        empty_schema = {"type": "object", "properties": {}}
        return [
            types.Tool(name="big_image", description="Return an oversize PNG image", inputSchema=empty_schema),
            types.Tool(name="small_image", description="Return a tiny PNG image", inputSchema=empty_schema),
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
