# -*- coding: utf-8 -*-
"""
UAT seed: MCP stdio Server — resources mode, FAILURE: no sub-resources.

Axis: MC-RES-01 (failure-axes.md)
Mode: resources
违规点: 声明 _meta.source="resources" 但 resources/list 不返回任何子资源
       → 期望 `staging.py:_materialize_resources` 抛
         SkillStagingError("resources-mode SKILL has no sub-resources under ...")
"""

from __future__ import annotations

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

SKILL_HOST = "seed.mc-res-01.example.com"
SKILL_LEAF = "valid-skill-pkg"


def build_resources() -> list[types.Resource]:
    """根带 _meta.source=resources，但**没有**任何子资源。"""
    root_uri = f"skill://{SKILL_HOST}/{SKILL_LEAF}"
    return [
        types.Resource.model_validate({
            "uri": root_uri,
            "name": SKILL_LEAF,
            "mimeType": "inode/directory",
            "_meta": {"source": "resources", "version": "1.0.0"},
        }),
        # 故意不附加子资源 —— 触发 _materialize_resources 的空集合分支
    ]


async def run() -> None:
    server = Server(name="uat-seed-resources-no-subs", version="0.0.1", instructions="MC-RES-01")

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return build_resources()

    @server.read_resource()
    async def _read_resource(uri: types.AnyUrl) -> str:
        # 不应被调用（根资源不会被 read；子资源根本不存在）
        raise FileNotFoundError(f"no sub-resources available (axis MC-RES-01); attempted: {uri}")

    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    anyio.run(run)
