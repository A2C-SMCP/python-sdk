# -*- coding: utf-8 -*-
# filename: test_request_builders.py
# @Time    : 2026/05/19
# @Author  : A2C-SMCP
# @Email   : qa@a2c-smcp.local
# @Software: PyTest
"""
中文：Agent 共享 request-builder 纯函数单元测试。
    覆盖 4 个 build_*_request 的返回结构、可选键（cursor/size/window）、
    req_id 唯一性与 agent 透传，以及 validate_emit_event 的拒绝规则。
English: Unit tests for the shared Agent request-builder pure functions.
    Covers the return structure of all 4 build_*_request helpers, optional
    keys (cursor/size/window), req_id uniqueness / agent passthrough, and the
    validate_emit_event rejection rules.
"""

import pytest

from a2c_smcp.agent._request_builders import (
    build_get_blob_request,
    build_get_desktop_request,
    build_get_resources_request,
    build_get_skill_request,
    build_get_skills_request,
    build_get_tools_request,
    build_tool_call_request,
    validate_emit_event,
)
from a2c_smcp.agent.types import AgentConfig

# 中文：构造一个最小 AgentConfig；纯函数不触碰 auth_provider，仅读取 ["agent"]。
# English: Minimal AgentConfig; pure functions never touch auth_provider, only read ["agent"].
_CFG: AgentConfig = {"agent": "agent-1", "office_id": "office-1"}


def test_build_tool_call_request_structure() -> None:
    req = build_tool_call_request(_CFG, "comp-1", "tool.x", {"k": "v"}, timeout=42)
    assert req["computer"] == "comp-1"
    assert req["tool_name"] == "tool.x"
    assert req["params"] == {"k": "v"}
    assert req["agent"] == "agent-1"
    assert req["timeout"] == 42
    assert isinstance(req["req_id"], str) and req["req_id"]


def test_build_get_tools_request_structure() -> None:
    req = build_get_tools_request(_CFG, "comp-1")
    assert req["computer"] == "comp-1"
    assert req["agent"] == "agent-1"
    assert isinstance(req["req_id"], str) and req["req_id"]


def test_build_get_resources_request_omits_cursor_by_default() -> None:
    # 中文：首次请求不带 cursor 键；English: first page must not carry the cursor key.
    req = build_get_resources_request(_CFG, "comp-1", "mcp-srv")
    assert req["computer"] == "comp-1"
    assert req["mcp_server"] == "mcp-srv"
    assert req["agent"] == "agent-1"
    assert "cursor" not in req


def test_build_get_resources_request_includes_cursor_when_given() -> None:
    req = build_get_resources_request(_CFG, "comp-1", "mcp-srv", cursor="page-2")
    assert req["cursor"] == "page-2"


def test_build_get_desktop_request_optional_keys() -> None:
    bare = build_get_desktop_request(_CFG, "comp-1")
    assert "desktop_size" not in bare
    assert "window" not in bare

    full = build_get_desktop_request(_CFG, "comp-1", size=3, window="window://x")
    assert full["desktop_size"] == 3
    assert full["window"] == "window://x"


def test_req_id_is_unique_per_call() -> None:
    # 中文：每次调用 uuid.uuid4().hex 应产生不同 req_id；English: each call yields a fresh req_id.
    a = build_get_tools_request(_CFG, "comp-1")
    b = build_get_tools_request(_CFG, "comp-1")
    assert a["req_id"] != b["req_id"]


@pytest.mark.parametrize("event", ["notify:enter_office", "agent:do_something"])
def test_validate_emit_event_rejects_forbidden_prefixes(event: str) -> None:
    with pytest.raises(ValueError):
        validate_emit_event(event)


@pytest.mark.parametrize("event", ["server:join_office", "client:tool_call", "tool_call"])
def test_validate_emit_event_allows_valid_events(event: str) -> None:
    # 中文：合法事件应静默通过（无返回值、无异常）。
    # English: valid events pass silently (no return value, no exception).
    assert validate_emit_event(event) is None


# ── v0.2.1 新增 / v0.2.1 additions ────────────────────────────────────────


def test_build_get_skills_request_structure() -> None:
    req = build_get_skills_request(_CFG, "comp-1")
    assert req["computer"] == "comp-1"
    assert req["agent"] == "agent-1"
    assert isinstance(req["req_id"], str) and req["req_id"]


def test_build_get_skill_request_omits_rel_path_by_default() -> None:
    """rel_path 缺省时由 Computer 端解析为 SKILL.md / Default rel_path resolved server-side to SKILL.md."""
    req = build_get_skill_request(_CFG, "comp-1", "user:x:y")
    assert req["computer"] == "comp-1"
    assert req["name"] == "user:x:y"
    assert "rel_path" not in req


def test_build_get_skill_request_includes_rel_path_when_given() -> None:
    req = build_get_skill_request(_CFG, "comp-1", "user:x:y", rel_path="docs/intro.md")
    assert req["rel_path"] == "docs/intro.md"


def test_build_get_blob_request_omits_optionals_by_default() -> None:
    """chunk_offset / max_chunk_bytes 缺省时由 Computer 端 clamp / Defaults clamped server-side."""
    req = build_get_blob_request(_CFG, "comp-1", "opaque-handle")
    assert req["blob_handle"] == "opaque-handle"
    assert "chunk_offset" not in req
    assert "max_chunk_bytes" not in req


def test_build_get_blob_request_with_offset_and_max() -> None:
    req = build_get_blob_request(_CFG, "comp-1", "opaque-handle", chunk_offset=65536, max_chunk_bytes=4096)
    assert req["chunk_offset"] == 65536
    assert req["max_chunk_bytes"] == 4096
