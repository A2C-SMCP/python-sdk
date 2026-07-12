# -*- coding: utf-8 -*-
# filename: test_bundle_id.py
# @Time    : 2026/07/11
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
BundleID 生成 / 校验 / TLV 摘要单测（协议 0.3.0 BundleID 模型，a2c-smcp-protocol#15）。

⚠️ 断言策略 / Assertion policy：fallback 摘要的**精确字节**由协议仓一致性测试向量「定死」（rust-sdk#117
首版交付、python 对拍），向量落库前**不**硬编码具体 hex——本文件断言的是**性质**（确定性 / 稳定性 /
stdio≠http / env 序无关 / args 序相关 / 格式），逐字节对拍留待向量到达（见 test_bundle_id.py 的
``test_fallback_*`` 与 bundle_id.py 模块 docstring 的 ASSUMPTION[1..3]）。
"""

from __future__ import annotations

import re

import pytest
from mcp import StdioServerParameters
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters

from a2c_smcp.computer.mcp_clients.model import SseServerConfig, StdioServerConfig, StreamableHttpServerConfig
from a2c_smcp.utils.bundle_id import (
    connection_identity_tlv,
    generate_bundle_id,
    is_valid_bundle_id,
    normalize_name,
    resolve_bundle_id,
    validate_explicit_bundle_id,
)

_FALLBACK_RE = re.compile(r"^bundle_[0-9a-f]{16}$")


def _stdio(name: str, command: str = "node", args: list[str] | None = None, env: dict[str, str] | None = None) -> StdioServerConfig:
    return StdioServerConfig(name=name, server_parameters=StdioServerParameters(command=command, args=args or [], env=env))


def _sse(name: str, url: str = "https://example.com/sse", headers: dict[str, str] | None = None) -> SseServerConfig:
    return SseServerConfig(name=name, server_parameters=SseServerParameters(url=url, headers=headers))


def _http(name: str, url: str = "https://example.com/mcp", headers: dict[str, str] | None = None) -> StreamableHttpServerConfig:
    return StreamableHttpServerConfig(name=name, server_parameters=StreamableHttpParameters(url=url, headers=headers))


# ————————————————————————————————— normalize_name (Step 1) —————————————————————————————————


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 非 [A-Za-z0-9_-] → _
        ("my server", "my_server"),
        ("a.b.c", "a_b_c"),
        # `-` 保留、不折叠、不替换
        ("my-server", "my-server"),
        ("a--b", "a--b"),
        # `_` 折叠（含原文 __）
        ("my_server", "my_server"),
        ("a__b", "a_b"),
        ("a___b", "a_b"),
        # 裁首尾 [_-]
        ("__lead", "lead"),
        ("trail__", "trail"),
        ("-x-", "x"),
        ("_-x-_", "x"),
        # 不做大小写折叠
        ("MyServer", "MyServer"),
        ("everything", "everything"),
        # 非 ASCII → _（随后可能被裁）
        ("café", "caf"),
        ("服务器-1", "1"),
        # 规范化为空 → 触发 fallback
        ("", ""),
        ("你好", ""),
        ("!!!", ""),
        ("___", ""),
        ("---", ""),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_normalize_name_non_injective() -> None:
    """规范化非单射：`my server` / `my-server` 不同，但 `my server` / `my_server` 撞（规范注）。"""
    assert normalize_name("my server") == normalize_name("my_server") == "my_server"
    assert normalize_name("my-server") != normalize_name("my_server")


# ————————————————————————————————— 显式 bundle_id 校验 —————————————————————————————————


@pytest.mark.parametrize("valid", ["playwright", "playwright_isolated", "a-b_c", "A1", "bundle_0123456789abcdef"])
def test_validate_explicit_bundle_id_ok(valid: str) -> None:
    assert validate_explicit_bundle_id(valid) == valid
    assert is_valid_bundle_id(valid) is True


@pytest.mark.parametrize("invalid", ["", "a__b", "__", "a.b", "a/b", "a b", "café"])
def test_validate_explicit_bundle_id_reject(invalid: str) -> None:
    assert is_valid_bundle_id(invalid) is False
    with pytest.raises(ValueError):
        validate_explicit_bundle_id(invalid)


# ————————————————————————————————— generate_bundle_id：规范化路径 (Step 2) —————————————————————————————————


def test_generate_uses_normalized_name_when_non_empty() -> None:
    assert generate_bundle_id(_stdio("My Server")) == "My_Server"
    assert generate_bundle_id(_sse("everything")) == "everything"
    # 连接身份对非空规范化路径无影响（不进 fallback）
    assert generate_bundle_id(_stdio("everything", command="node")) == generate_bundle_id(_stdio("everything", command="python"))


# ————————————————————————————————— generate_bundle_id：fallback 路径 (Step 3) —————————————————————————————————


@pytest.mark.parametrize("empty_name", ["", "你好", "!!!", "___"])
def test_fallback_format(empty_name: str) -> None:
    """空规范化 → `bundle_` + 16 小写 hex。"""
    bid = generate_bundle_id(_stdio(empty_name))
    assert _FALLBACK_RE.match(bid), bid


def test_fallback_deterministic() -> None:
    """同 (name, 连接身份) → 同 bundle_id（确定性，重复调用稳定）。"""
    a = generate_bundle_id(_stdio("你好", command="node", args=["x"], env={"A": "1"}))
    b = generate_bundle_id(_stdio("你好", command="node", args=["x"], env={"A": "1"}))
    assert a == b


def test_fallback_stdio_differs_from_http() -> None:
    """同为空名，stdio 与 http 连接身份不同 → bundle_id 不同。"""
    assert generate_bundle_id(_stdio("你好")) != generate_bundle_id(_http("你好"))


def test_fallback_env_order_independent() -> None:
    """env 按 key 排序纳入 → 插入序不影响摘要。"""
    a = generate_bundle_id(_stdio("你好", env={"A": "1", "B": "2"}))
    b = generate_bundle_id(_stdio("你好", env={"B": "2", "A": "1"}))
    assert a == b


def test_fallback_args_order_dependent() -> None:
    """args 保序纳入 → 顺序改变摘要。"""
    a = generate_bundle_id(_stdio("你好", args=["a", "b"]))
    b = generate_bundle_id(_stdio("你好", args=["b", "a"]))
    assert a != b


def test_fallback_headers_order_independent() -> None:
    a = generate_bundle_id(_http("你好", headers={"X-A": "1", "X-B": "2"}))
    b = generate_bundle_id(_http("你好", headers={"X-B": "2", "X-A": "1"}))
    assert a == b


def test_fallback_command_sensitive() -> None:
    assert generate_bundle_id(_stdio("你好", command="node")) != generate_bundle_id(_stdio("你好", command="python"))


# ————————————————————————————————— connection_identity_tlv 结构不变量 —————————————————————————————————


def test_tlv_excludes_non_connection_fields() -> None:
    """TLV 排除 disabled/forbidden_tools 等非连接字段 → 仅连接身份变化才改摘要。"""
    base = _stdio("你好", command="node", args=["s"], env={"A": "1"})
    variant = StdioServerConfig(
        name="你好",
        server_parameters=StdioServerParameters(command="node", args=["s"], env={"A": "1"}),
        disabled=True,
        forbidden_tools=["t"],
    )
    assert connection_identity_tlv(base) == connection_identity_tlv(variant)


def test_tlv_empty_collections_are_zero_count() -> None:
    """空 args/env → 计数 0，不报错，产合法 fallback。"""
    bid = generate_bundle_id(_stdio("你好", args=[], env=None))
    assert _FALLBACK_RE.match(bid)


# ————————————————————————————————— resolve_bundle_id —————————————————————————————————


def test_resolve_generates_when_omitted() -> None:
    """当前 DTO 尚无 bundle_id 字段（Phase 2 加）→ getattr None → 走生成。"""
    cfg = _stdio("My Server")
    assert resolve_bundle_id(cfg) == "My_Server"


def test_resolve_prefers_explicit() -> None:
    """显式 bundle_id 优先且经校验（用 duck-typed 对象模拟 Phase 2 后的字段）。"""
    import types as _types

    cfg = _types.SimpleNamespace(bundle_id="playwright_isolated")
    assert resolve_bundle_id(cfg) == "playwright_isolated"  # type: ignore[arg-type]

    bad = _types.SimpleNamespace(bundle_id="bad__id")
    with pytest.raises(ValueError):
        resolve_bundle_id(bad)  # type: ignore[arg-type]
