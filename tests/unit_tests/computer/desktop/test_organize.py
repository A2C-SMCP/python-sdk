# -*- coding: utf-8 -*-
# filename: test_organize.py
# @Time    : 2025/10/02 16:27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
组织策略单元测试 / Unit tests for desktop organizing policy

仅测试 organize_desktop 的行为，不依赖 Computer。
Only verify organize_desktop behavior, decoupled from Computer.

v0.2 协议指南 §6.2 / §6.4：
- priority 来自 ``Resource.annotations.priority``（float [0.0, 1.0]，缺省 0.0）
- fullscreen 来自 ``Resource._meta['fullscreen']``（bool，缺省 False）
- URI query 不再承载排序信息
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from mcp.types import (
    Annotations,
    BlobResourceContents,
    ReadResourceResult,
    Resource,
    Role,
    TextResourceContents,
)
from pydantic import AnyUrl

import a2c_smcp.computer.desktop.organize as organize_mod
from a2c_smcp.computer.desktop.organize import organize_desktop
from a2c_smcp.computer.types import ToolCallRecord


# 防御 test_logger.py 调用 logging.Logger.manager.loggerDict.clear() 后影响 caplog 捕获
# Defend against test_logger.py clearing loggerDict so caplog still captures source-module logs
@pytest.fixture(autouse=True)
def attach_project_logger_to_caplog(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    src_logger = organize_mod.logger
    prev_level = src_logger.level
    prev_propagate = src_logger.propagate
    src_logger.setLevel(logging.DEBUG)
    src_logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        try:
            src_logger.removeHandler(caplog.handler)
        except Exception:
            pass
        src_logger.setLevel(prev_level)
        src_logger.propagate = prev_propagate


# -------------------------
# 工具函数 / Helpers
# -------------------------
def _mk_resource(
    uri: str,
    *,
    priority: float | None = None,
    fullscreen: bool | None = None,
    audience: list[str] | None = None,
) -> Resource:
    """
    构建测试用 Window Resource，支持设置 annotations.priority / annotations.audience / _meta.fullscreen。
    Build a test Window Resource with optional annotations.priority / annotations.audience / _meta.fullscreen.
    """
    annotations: Annotations | None = None
    if priority is not None or audience is not None:
        audience_typed = cast(list[Role], audience) if audience is not None else None
        annotations = Annotations(priority=priority, audience=audience_typed)
    extra: dict[str, Any] = {}
    if fullscreen is not None:
        extra["_meta"] = {"fullscreen": fullscreen}
    return Resource(uri=AnyUrl(uri), name=uri.rsplit("/", 1)[-1], annotations=annotations, **extra)


def _mk_detail(uri: str, text: str) -> ReadResourceResult:
    return ReadResourceResult(contents=[TextResourceContents(text=text, uri=AnyUrl(uri))])


def _mk_history(*servers: str) -> tuple[ToolCallRecord, ...]:
    """构造最小可用的工具调用历史 / build minimal tool-call history."""
    return tuple(
        ToolCallRecord(
            timestamp="2026-04-28T00:00:00Z",
            req_id=f"req-{i}",
            server=s,
            tool="t",
            parameters={},
            timeout=None,
            success=True,
            error=None,
        )
        for i, s in enumerate(servers)
    )


# -------------------------
# 核心组织规则 / Core ordering rules
# -------------------------
@pytest.mark.asyncio
async def test_priority_within_server_and_size_cap() -> None:
    """同一服务器内按 priority 降序，全局按 size 截断。"""
    uris = ["window://srv/w1", "window://srv/w2", "window://srv/w3"]
    res1 = _mk_resource(uris[0], priority=0.1)
    res2 = _mk_resource(uris[1], priority=0.9)
    res3 = _mk_resource(uris[2])  # 缺省 0.0

    windows = [
        ("srv", res1, _mk_detail(uris[0], "w1-text")),
        ("srv", res2, _mk_detail(uris[1], "w2-text")),
        ("srv", res3, _mk_detail(uris[2], "w3-text")),
    ]
    ret = await organize_desktop(windows=windows, size=2, history=tuple())
    assert ret[0].startswith(uris[1]) and "w2-text" in ret[0]
    assert ret[1].startswith(uris[0]) and "w1-text" in ret[1]
    assert len(ret) == 2


@pytest.mark.asyncio
async def test_fullscreen_one_per_server_then_next_server() -> None:
    """fullscreen=True 时该 Server 仅推第一个 fullscreen 窗口，再进入下一个 Server。"""
    a1 = _mk_resource("window://A/a1", priority=0.5)
    a2 = _mk_resource("window://A/a2", priority=0.1, fullscreen=True)
    a3 = _mk_resource("window://A/a3", priority=0.9)
    b1 = _mk_resource("window://B/b1", priority=0.05)
    windows = [
        ("A", a1, _mk_detail("window://A/a1", "a1")),
        ("A", a2, _mk_detail("window://A/a2", "a2-full")),
        ("A", a3, _mk_detail("window://A/a3", "a3")),
        ("B", b1, _mk_detail("window://B/b1", "b1")),
    ]
    ret = await organize_desktop(windows=windows, size=None, history=_mk_history("A"))
    assert ret[0].startswith("window://A/a2") and "a2-full" in ret[0]
    assert any(x.startswith("window://B/b1") and "b1" in x for x in ret)


@pytest.mark.asyncio
async def test_server_order_by_recent_history() -> None:
    """服务器顺序按最近历史倒序优先。"""
    res_a = _mk_resource("window://A/a1", priority=0.01)
    res_b = _mk_resource("window://B/b1", priority=0.01)
    res_c = _mk_resource("window://C/c1", priority=0.01)
    windows = [
        ("A", res_a, _mk_detail("window://A/a1", "a")),
        ("B", res_b, _mk_detail("window://B/b1", "b")),
        ("C", res_c, _mk_detail("window://C/c1", "c")),
    ]
    # 最近顺序：A then C（C 最近）；B 未在历史中 -> 名字升序追加
    history = _mk_history("A", "C")
    ret = await organize_desktop(windows=windows, size=None, history=history)
    assert ret[0].startswith("window://C/c1")
    assert ret[1].startswith("window://A/a1")
    assert ret[2].startswith("window://B/b1")


@pytest.mark.asyncio
async def test_size_zero_returns_empty() -> None:
    ret = await organize_desktop(windows=[], size=0, history=tuple())
    assert ret == []


# -------------------------
# §6.2 缺失值同义 / Missing-value equivalence
# -------------------------
@pytest.mark.asyncio
async def test_missing_annotations_equivalent_to_zero_priority() -> None:
    """annotations is None 与 annotations.priority is None 等价处理为 0.0。"""
    res_high = _mk_resource("window://srv/high", priority=0.8)
    res_no_anno = _mk_resource("window://srv/no_anno")  # 完全无 annotations
    res_no_prio = _mk_resource("window://srv/no_prio", audience=["assistant"])  # 仅 audience，无 priority
    windows = [
        ("srv", res_no_anno, _mk_detail("window://srv/no_anno", "no_anno")),
        ("srv", res_high, _mk_detail("window://srv/high", "high")),
        ("srv", res_no_prio, _mk_detail("window://srv/no_prio", "no_prio")),
    ]
    ret = await organize_desktop(windows=windows, size=None, history=tuple())
    # high 应排第一；其余两个均为 0.0，按稳定排序保持原始顺序
    assert ret[0].startswith("window://srv/high")
    assert {ret[1].split("\n")[0], ret[2].split("\n")[0]} == {"window://srv/no_anno", "window://srv/no_prio"}


@pytest.mark.asyncio
async def test_missing_meta_equivalent_to_no_fullscreen() -> None:
    """_meta is None 与 _meta.fullscreen is None 等价处理为 False。"""
    a = _mk_resource("window://srv/a", priority=0.5)  # 无 _meta
    b = _mk_resource("window://srv/b", priority=0.9)  # 无 _meta
    windows = [
        ("srv", a, _mk_detail("window://srv/a", "a")),
        ("srv", b, _mk_detail("window://srv/b", "b")),
    ]
    ret = await organize_desktop(windows=windows, size=None, history=tuple())
    # 无 fullscreen，按 priority 降序
    assert ret[0].startswith("window://srv/b")
    assert ret[1].startswith("window://srv/a")


# -------------------------
# §6.4 越界与非法值 / Out-of-range and invalid values
# -------------------------
@pytest.mark.asyncio
async def test_out_of_range_priority_treated_as_zero_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """priority 越界 [0.0, 1.0] 时记录 WARN，按 0.0 处理（绕过 Annotations 自带的范围校验测试 Computer 的兜底逻辑）。"""
    # Annotations 构造期会强制 priority ∈ [0,1]；这里使用 model_construct 绕过校验，
    # 模拟来自不规范 MCP Server 的越界值，验证 Computer 的兜底分支。
    bad_anno = Annotations.model_construct(priority=2.0, audience=None)
    over = Resource(uri=AnyUrl("window://srv/over"), name="over", annotations=bad_anno)
    normal = _mk_resource("window://srv/normal", priority=0.5)
    windows = [
        ("srv", over, _mk_detail("window://srv/over", "over")),
        ("srv", normal, _mk_detail("window://srv/normal", "normal")),
    ]
    with caplog.at_level(logging.WARNING):
        ret = await organize_desktop(windows=windows, size=None, history=tuple())
    # over 被 clamp 为 0.0，normal=0.5 在前
    assert ret[0].startswith("window://srv/normal")
    assert ret[1].startswith("window://srv/over")
    assert any("priority" in m and ("越界" in m or "out-of-range" in m) for m in caplog.messages)


@pytest.mark.asyncio
async def test_non_bool_fullscreen_treated_as_false_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """fullscreen 非布尔类型记录 WARN，按 False 处理。"""
    bad = _mk_resource("window://srv/bad", priority=0.9)
    bad.meta = {"fullscreen": "yes"}
    other = _mk_resource("window://srv/other", priority=0.1)
    windows = [
        ("srv", bad, _mk_detail("window://srv/bad", "bad")),
        ("srv", other, _mk_detail("window://srv/other", "other")),
    ]
    with caplog.at_level(logging.WARNING):
        ret = await organize_desktop(windows=windows, size=None, history=tuple())
    # bad 不被识别为 fullscreen；按 priority 降序：bad(0.9) 在前
    assert ret[0].startswith("window://srv/bad")
    assert ret[1].startswith("window://srv/other")
    assert any("fullscreen" in m and ("非布尔" in m or "non-bool" in m) for m in caplog.messages)


@pytest.mark.asyncio
async def test_audience_user_logs_warning_but_still_included(caplog: pytest.LogCaptureFixture) -> None:
    """annotations.audience 不含 'assistant' 时记录 WARN 但仍纳入聚合（v0.2 行为）。"""
    user_only = _mk_resource("window://srv/user", priority=0.5, audience=["user"])
    windows = [("srv", user_only, _mk_detail("window://srv/user", "u"))]
    with caplog.at_level(logging.WARNING):
        ret = await organize_desktop(windows=windows, size=None, history=tuple())
    assert len(ret) == 1 and ret[0].startswith("window://srv/user")
    assert any("audience" in m and "assistant" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_uri_query_no_longer_drives_ordering() -> None:
    """v0.2 起，URI 中的 ?priority= / ?fullscreen= 不再影响排序（一律视为 0.0/False）。"""
    a = _mk_resource("window://srv/a", priority=0.0)
    a.uri = AnyUrl("window://srv/a?priority=99")
    b = _mk_resource("window://srv/b", priority=0.7)
    windows = [
        ("srv", a, _mk_detail("window://srv/a?priority=99", "a")),
        ("srv", b, _mk_detail("window://srv/b", "b")),
    ]
    ret = await organize_desktop(windows=windows, size=None, history=tuple())
    # b 来自 annotations.priority=0.7 应排第一；a 的 URI query 被忽略
    assert ret[0].startswith("window://srv/b")
    assert ret[1].startswith("window://srv/a")


# -------------------------
# 容错分支 / Robustness branches
# -------------------------
@pytest.mark.asyncio
async def test_skip_empty_contents_and_detail_exception() -> None:
    """空内容与 detail.contents 访问异常会被跳过。"""
    empty_detail = ReadResourceResult(contents=[])

    class BadDetail:
        def __getattribute__(self, name: str) -> Any:
            if name == "contents":
                raise RuntimeError("boom")
            return super().__getattribute__(name)

    ok_detail = _mk_detail("window://S/ok", "ok")

    windows: list[tuple[str, Resource, ReadResourceResult]] = [
        ("S", _mk_resource("window://S/empty"), empty_detail),
        ("S", _mk_resource("window://S/bad"), BadDetail()),  # type: ignore[arg-type]
        ("S", _mk_resource("window://S/ok"), ok_detail),
    ]
    ret = await organize_desktop(windows=windows, size=None, history=tuple())
    assert len(ret) == 1 and ret[0].startswith("window://S/ok") and "ok" in ret[0]


@pytest.mark.asyncio
async def test_invalid_window_uri_is_skipped() -> None:
    """非 window:// 协议的 URI 会被跳过。"""
    good_detail = _mk_detail("window://G/good", "good")
    bad_res = _mk_resource("custom-scheme://G/bad", priority=0.5)
    bad_detail = _mk_detail("custom-scheme://G/bad", "bad")
    good_res = _mk_resource("window://G/good", priority=0.5)

    windows = [
        ("G", bad_res, bad_detail),
        ("G", good_res, good_detail),
    ]
    ret = await organize_desktop(windows=windows, size=None, history=tuple())
    assert len(ret) == 1 and ret[0].startswith("window://G/good") and "good" in ret[0]


@pytest.mark.asyncio
async def test_render_blob_and_unknown_and_render_exception() -> None:
    """渲染分支：Blob 被记录但不拼接文本；未知类型记录 error；渲染异常回退为 URI。"""
    detail_blob = ReadResourceResult(
        contents=[
            BlobResourceContents(uri=AnyUrl("window://R/blob"), mimeType="application/octet-stream", blob="eHg="),
            TextResourceContents(uri=AnyUrl("window://R/blob"), text="t1"),
        ],
    )
    detail_unknown = _mk_detail("window://R/u", "t2")
    detail_unknown.contents = [object(), TextResourceContents(uri=AnyUrl("window://R/u"), text="t2")]  # type: ignore[list-item]
    detail_exception = _mk_detail("window://R/ex", "ex")
    detail_exception.contents = 123  # type: ignore[assignment]

    windows = [
        ("R", _mk_resource("window://R/blob"), detail_blob),
        ("R", _mk_resource("window://R/u"), detail_unknown),
        ("R", _mk_resource("window://R/ex"), detail_exception),
    ]
    ret = await organize_desktop(windows=windows, size=None, history=tuple())
    assert any(x.startswith("window://R/blob") and "t1" in x for x in ret)
    assert any(x.startswith("window://R/u") and "t2" in x for x in ret)
    assert any(x == "window://R/ex" for x in ret)


@pytest.mark.asyncio
async def test_server_level_cap_breaks_iteration() -> None:
    """当第一个服务器已满足 size 上限时，后续服务器在服务器层级立即中断。"""
    a = _mk_resource("window://A/a", priority=0.1)
    b = _mk_resource("window://B/b", priority=0.1)
    windows = [
        ("A", a, _mk_detail("window://A/a", "a")),
        ("B", b, _mk_detail("window://B/b", "b")),
    ]
    ret = await organize_desktop(windows=windows, size=1, history=_mk_history("A"))
    assert len(ret) == 1 and ret[0].startswith("window://A/a")
