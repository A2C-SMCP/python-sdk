# -*- coding: utf-8 -*-
# filename: fastmcp_skill_stdio_server.py
"""
中文: 最小 stdio MCP server fixture，模拟 FastMCP-style Skills Provider，用于 AS-40 集成测试。
英文: Minimal stdio MCP server fixture emulating a FastMCP-style Skills Provider, for the AS-40
      integration test.

背景 / Background (AS-40 comment 13849):
    FastMCP Skills Provider 暴露 ``skill://<name>/SKILL.md`` / ``_manifest`` / ``reference.md``。
    其中**裸布局** ``skill://<name>/SKILL.md``（无 ``_meta.source`` 根）当前 SDK 不收（设计如此，
    由 provider 侧适配解决）；只要 provider 暴露**可注册形状**（``_meta.source=resources`` 根 +
    子资源），current SDK **无需改码**即可物化注册。本 fixture 同时暴露两者，用一台 server 守护
    两个契约：

      ┌ MF-01 可注册形状（registrable）──────────────────────────────────────┐
      │ skill://fastmcp.demo.example/fastmcp-demo            _meta.source=resources │
      │ skill://fastmcp.demo.example/fastmcp-demo/SKILL.md        （子资源）        │
      │ skill://fastmcp.demo.example/fastmcp-demo/reference.md    （子资源）        │
      └──────────────────────────────────────────────────────────────────────────┘
      ┌ MF-02 裸 FastMCP 布局（bare，当前不注册）──────────────────────────────────┐
      │ skill://bare-demo/SKILL.md                          无 _meta.source 根       │
      └──────────────────────────────────────────────────────────────────────────┘
      ┌ MF-03 畸形段（#188，theseus-kit 修复前形态）──────────────────────────────┐
      │ skill://fastmcp.demo.example/fastmcp-demo/notes/extra.md                  │
      │                    子资源误打 _meta.source=resources ——SDK 按 URI 前缀归属排除│
      └──────────────────────────────────────────────────────────────────────────┘

启动 / Run: python fastmcp_skill_stdio_server.py  （仅 stdin/stdout，不监听端口）
"""

from __future__ import annotations

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

# ── MF-01 可注册形状 / registrable shape ─────────────────────────────────────
SKILL_HOST = "fastmcp.demo.example"
REG_ROOT = f"skill://{SKILL_HOST}/fastmcp-demo"

_REG_SKILL_MD = (
    "---\n"
    "name: fastmcp-demo\n"
    "description: FastMCP-style demo skill exposed as registrable resources\n"
    "license: MIT\n"
    "---\n"
    "# FastMCP Demo\n\n"
    "中文: FastMCP 可注册形状演示 SKILL。英文: registrable-shape demo skill.\n"
)
_REG_REFERENCE_MD = "# Reference\n\n中文: 附带参考文件。英文: supporting reference file.\n"

# ── MF-02 裸 FastMCP 布局 / bare layout（无 _meta.source 根，当前 SDK 跳过）─────
BARE_ROOT = "skill://bare-demo"
_BARE_SKILL_MD = "---\nname: bare-demo\ndescription: bare FastMCP layout without a _meta.source root\n---\n# Bare Demo\n"

# ── MF-03 畸形段 / malformed shape（#188：子资源误打 _meta.source=resources）────
_REG_NOTES_MD = "# Notes\n\n中文: 被根前缀覆盖、却误带 source 的子资源。英文: covered sub-resource carrying source.\n"

# uri -> resources/read 文本内容 / uri -> read content
_CONTENT: dict[str, str] = {
    f"{REG_ROOT}/SKILL.md": _REG_SKILL_MD,
    f"{REG_ROOT}/reference.md": _REG_REFERENCE_MD,
    f"{REG_ROOT}/notes/extra.md": _REG_NOTES_MD,
    f"{BARE_ROOT}/SKILL.md": _BARE_SKILL_MD,
}


def build_resources() -> list[types.Resource]:
    """枚举 MF-01 根+子资源、MF-03 畸形子资源 与 MF-02 裸 SKILL.md。"""
    return [
        # MF-01 根（带 _meta.source=resources）/ registrable root
        types.Resource.model_validate(
            {
                "uri": REG_ROOT,
                "name": "fastmcp-demo",
                "mimeType": "inode/directory",
                "_meta": {"source": "resources", "version": "1.0.0"},
            },
        ),
        # MF-01 子资源（不带 _meta.source）/ sub-resources
        types.Resource.model_validate({"uri": f"{REG_ROOT}/SKILL.md", "name": "SKILL.md", "mimeType": "text/markdown"}),
        types.Resource.model_validate(
            {"uri": f"{REG_ROOT}/reference.md", "name": "reference.md", "mimeType": "text/markdown"},
        ),
        # MF-03 畸形子资源（#188）：被根前缀覆盖却误带 _meta.source。
        # 刻意放在真根**之后**——pre-fix 它会被当独立根物化，与真根同 leaf → _reset_dir
        # 删掉已注册真根包，令落盘断言确定性失败。
        types.Resource.model_validate(
            {
                "uri": f"{REG_ROOT}/notes/extra.md",
                "name": "extra.md",
                "mimeType": "text/markdown",
                "_meta": {"source": "resources"},
            },
        ),
        # MF-02 裸 FastMCP 入口（无根、无 _meta.source）/ bare entry
        types.Resource.model_validate({"uri": f"{BARE_ROOT}/SKILL.md", "name": "SKILL.md", "mimeType": "text/markdown"}),
    ]


async def run() -> None:
    server = Server(name="fastmcp-skill-test", version="0.0.1", instructions="AS-40 fastmcp skills fixture")

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return build_resources()

    @server.read_resource()
    async def _read_resource(uri: types.AnyUrl) -> str:
        key = str(uri)
        if key not in _CONTENT:
            raise FileNotFoundError(f"resource not found: {key}")
        return _CONTENT[key]

    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    anyio.run(run)
