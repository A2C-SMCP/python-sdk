# -*- coding: utf-8 -*-
# filename: test_manager_skill_resources.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
MCPServerManager.list_skill_resources / read_resource 单元测试（v0.2.1 #59）

测试意图 / Test intentions:
- list_skill_resources：完整消费 cursor 翻页（多页）；仅返回 skill:// + 附 server 归属；非 skill:// 排除
- 某 server 枚举抛错（如未声明 resources 能力）→ 跳过该 server、不阻断其余
- read_resource：委托 client 通用读取；未注册 server → MCPServerNotFoundError
"""

import pytest
from mcp.types import ReadResourceResult, Resource, TextResourceContents

from a2c_smcp.computer.mcp_clients.base_client import MCPServerNotFoundError
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager


class _PagedClient:
    """按 cursor 返回多页 resources/list 的假客户端 / fake client paging resources/list by cursor。"""

    def __init__(self, pages: list[tuple[list[Resource], str | None]]) -> None:
        self._pages = pages
        self.cursors_seen: list[str | None] = []

    async def list_resources_page(self, cursor: str | None = None) -> tuple[list[Resource], str | None]:
        self.cursors_seen.append(cursor)
        idx = 0 if cursor is None else int(cursor)
        return self._pages[idx]


class _RaisingClient:
    async def list_resources_page(self, cursor: str | None = None) -> tuple[list[Resource], str | None]:
        raise RuntimeError("no resources capability")


class _ReadClient:
    def __init__(self, result: ReadResourceResult) -> None:
        self._result = result
        self.read_uris: list[str] = []

    async def get_window_detail(self, uri: str) -> ReadResourceResult:
        self.read_uris.append(str(uri))
        return self._result


def _res(uri: str) -> Resource:
    return Resource(uri=uri, name=uri.rsplit("/", 1)[-1])


async def test_list_skill_resources_exhausts_cursor_and_filters() -> None:
    page0 = ([_res("skill://h/a"), _res("http://h/not-skill"), _res("skill://h/b")], "1")
    page1 = ([_res("skill://h/c")], None)
    client = _PagedClient([page0, page1])
    mgr = MCPServerManager()
    mgr._active_clients = {"srv": client}  # type: ignore[assignment]

    out = await mgr.list_skill_resources()
    names = [str(r.uri) for _, r in out]
    assert names == ["skill://h/a", "skill://h/b", "skill://h/c"]  # 跨页、滤掉 http://
    assert all(s == "srv" for s, _ in out)  # server 归属
    assert client.cursors_seen == [None, "1"]  # 完整消费翻页直至末页


async def test_list_skill_resources_skips_erroring_server() -> None:
    good = _PagedClient([([_res("skill://h/a")], None)])
    bad = _RaisingClient()
    mgr = MCPServerManager()
    mgr._active_clients = {"good": good, "bad": bad}  # type: ignore[assignment]

    out = await mgr.list_skill_resources()
    assert [(s, str(r.uri)) for s, r in out] == [("good", "skill://h/a")]  # bad 被跳过、good 不受影响


async def test_list_skill_resources_server_filter() -> None:
    c1 = _PagedClient([([_res("skill://h/a")], None)])
    c2 = _PagedClient([([_res("skill://h/b")], None)])
    mgr = MCPServerManager()
    mgr._active_clients = {"s1": c1, "s2": c2}  # type: ignore[assignment]

    out = await mgr.list_skill_resources(server_name="s2")
    assert [(s, str(r.uri)) for s, r in out] == [("s2", "skill://h/b")]


async def test_read_resource_delegates_and_unknown_server_raises() -> None:
    result = ReadResourceResult(contents=[TextResourceContents(uri="skill://h/a/SKILL.md", text="hi")])
    client = _ReadClient(result)
    mgr = MCPServerManager()
    mgr._active_clients = {"srv": client}  # type: ignore[assignment]

    got = await mgr.read_resource("srv", "skill://h/a/SKILL.md")
    assert got is result
    assert client.read_uris == ["skill://h/a/SKILL.md"]

    with pytest.raises(MCPServerNotFoundError):
        await mgr.read_resource("absent", "skill://h/a/SKILL.md")
