# -*- coding: utf-8 -*-
# filename: mutable_resources_stdio_server.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文: phase 受控、运行期可变资源集的 MCP Stdio 服务器，用于 #210 的集成测试。
英文: A phase-controlled, runtime-mutable resource-set MCP Stdio server for the #210 integration tests.

为什么需要它 / Why this fixture exists:
    #210 的缺陷是「``resources/list_changed`` 之后该 server 永久失能」—— 判据是**触发通知的那次工具调用
    是否返回**，而现有的 ``notifications_stdio_server.py`` 的 ``list_resources()`` 恒为 ``[]``，
    任何只断言「窗口/SKILL 缓存变了」的写法修前修后都绿（假绿）。故本 fixture 提供**真的会变**的资源集：

      - ``set_phase(phase)``     ：改 ``window://`` 集合 → 发 ``resources/list_changed``
      - ``set_skill_body(text)`` ：改 ``SKILL.md`` 内容（URI 集合**不变**）→ 发 ``resources/updated``

    后者专门覆盖「内容级更新」这条路径：它的 URI 集合不变，若与集合路径共用同一个脏位，重物化会被
    集合比对挡掉（静默丢更新）。

资源形状 / Resource shapes:
    - ``window://itest.mutable.res/main`` 等：随 phase 增删（window 集合比对路径）
    - ``skill://itest.mutable.res/demo``  ：``_meta.source=resources`` 可注册根（形状同 fastmcp 夹具）
      └ ``.../demo/SKILL.md``             ：子资源，内容由 ``set_skill_body`` 驱动

启动 / Run: python mutable_resources_stdio_server.py  （仅 stdin/stdout，不监听端口）
"""

from __future__ import annotations

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

# 进程内状态（由工具调用驱动）/ in-process state driven by tool calls
_state: dict[str, object] = {"phase": 0, "skill_body": "initial body"}

_HOST = "itest.mutable.res"

# phase → window:// URI 集合 / phase → window:// URI set
_PHASE_WINDOWS: dict[int, list[str]] = {
    0: [],
    1: [f"window://{_HOST}/main"],
    2: [f"window://{_HOST}/main", f"window://{_HOST}/side"],
}

SKILL_ROOT = f"skill://{_HOST}/demo"
SKILL_MD_URI = f"{SKILL_ROOT}/SKILL.md"

_SET_PHASE = types.Tool(
    name="set_phase",
    description="Set the current phase (int) and fire resources/list_changed",
    inputSchema={"type": "object", "properties": {"phase": {"type": "integer"}}, "required": ["phase"]},
)

_SET_SKILL_BODY = types.Tool(
    name="set_skill_body",
    description="Rewrite the SKILL.md body (URI set unchanged) and fire resources/updated",
    inputSchema={"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]},
)


def _skill_md() -> str:
    """当前 ``SKILL.md`` 内容（frontmatter 必需，body 由 ``set_skill_body`` 驱动）。"""
    # frontmatter ``name`` 须与 URI 叶子一致（skill.md §11.6 最佳实践）——注册名取前者、落盘目录取后者。
    return (
        "---\n"
        "name: demo\n"
        "description: runtime-mutable resource fixture skill\n"
        "license: MIT\n"
        "---\n"
        f"# Mutable Demo\n\n{_state['skill_body']}\n"
    )


def build_resources() -> list[types.Resource]:
    """按当前 phase 计算 window:// 集合，并附上可注册的 skill:// 根与其 SKILL.md 子资源。"""
    windows = [
        types.Resource.model_validate(
            {
                "uri": uri,
                "name": uri.rsplit("/", 1)[-1],
                "mimeType": "text/markdown",
                "annotations": {"priority": 0.5, "audience": ["assistant"]},
            },
        )
        for uri in _PHASE_WINDOWS[int(_state["phase"])]  # type: ignore[arg-type]
    ]
    skill = [
        types.Resource.model_validate(
            {
                "uri": SKILL_ROOT,
                "name": "demo",
                "mimeType": "inode/directory",
                "_meta": {"source": "resources"},
            },
        ),
        types.Resource.model_validate({"uri": SKILL_MD_URI, "name": "SKILL.md", "mimeType": "text/markdown"}),
    ]
    return windows + skill


async def run() -> None:
    """中文: 启动服务器 / 英文: Start the server."""
    server = Server(name="mutable-resources-server", version="0.0.1", instructions="itest-mutable-resources")

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return build_resources()

    @server.read_resource()
    async def _read_resource(uri: types.AnyUrl) -> str:
        key = str(uri)
        if key == SKILL_MD_URI:
            return _skill_md()
        if key.startswith(f"window://{_HOST}/"):
            return f"# {key}\n\n窗口内容 / window content for {key}\n"
        raise FileNotFoundError(f"resource not found: {key}")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [_SET_PHASE, _SET_SKILL_BODY]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        ctx = server.request_context
        args = arguments or {}
        if name == "set_phase":
            _state["phase"] = int(args.get("phase", 0))
            # 运行期改变 window:// 集合后立即广播（纯 resources/list_changed，不伴随 config 变更）
            await ctx.session.send_resource_list_changed()
            return [types.TextContent(type="text", text=f"phase={_state['phase']}")]
        if name == "set_skill_body":
            _state["skill_body"] = str(args.get("body", ""))
            # 内容级更新：URI 集合不变，只发 resources/updated（③ 路径的驱动）
            await ctx.session.send_resource_updated(uri=SKILL_MD_URI)  # type: ignore[arg-type]
            return [types.TextContent(type="text", text="skill body updated")]
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        init_opts = server.create_initialization_options(
            notification_options=NotificationOptions(resources_changed=True),
        )
        await server.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    anyio.run(run)
