# -*- coding: utf-8 -*-
"""
UAT seed: MCP stdio Server — resources mode, with window:// scheme resources.

Axis: MC-RES-WIN
Mode: resources
Purpose: serve window:// resources with annotations (priority, audience, fullscreen)
         to exercise client:get_resources discovery, camelCase→snake_case, and
         error codes 4014 / 4015.
"""

from __future__ import annotations

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

SERVER_NAME = "window-resource-server"


def build_resources() -> list[types.Resource]:
    return [
        types.Resource.model_validate({
            "uri": "window://main-editor",
            "name": "main-editor",
            "description": "Primary code editor window",
            "mimeType": "text/plain",
            "annotations": {
                "priority": 0.9,
                "audience": ["assistant"],
                "lastModified": "2026-05-28T12:00:00Z",
            },
            "_meta": {"fullscreen": True},
        }),
        types.Resource.model_validate({
            "uri": "window://terminal",
            "name": "terminal",
            "description": "Terminal output window",
            "mimeType": "text/plain",
            "annotations": {
                "priority": 0.5,
                "audience": ["user", "assistant"],
                "lastModified": "2026-05-28T11:30:00Z",
            },
            "_meta": {"fullscreen": False},
        }),
        types.Resource.model_validate({
            "uri": "config://app-settings",
            "name": "app-settings",
            "description": "Application configuration",
            "mimeType": "application/json",
        }),
    ]


async def run() -> None:
    server = Server(
        name=SERVER_NAME, version="0.0.1",
        instructions="MC-RES-WIN: window resources with annotations",
    )

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return build_resources()

    @server.read_resource()
    async def _read_resource(uri: types.AnyUrl) -> str:
        uri_str = str(uri)
        if uri_str == "window://main-editor":
            return "Main editor content"
        if uri_str == "window://terminal":
            return "Terminal output content"
        if uri_str == "config://app-settings":
            return '{"theme": "dark"}'
        raise FileNotFoundError(f"resource not found: {uri}")

    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    anyio.run(run)
