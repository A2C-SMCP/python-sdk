# -*- coding: utf-8 -*-
# filename: test_window_uri.py
# @Time    : 2025/10/01 19:46
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
WindowURI 单元测试（v0.2 协议）/ Unit tests for WindowURI (v0.2 protocol)

测试意图 / Test intentions:
- v0.2 纯标识符形态：解析 host、路径段、URL 编码 / 解码
- v0.2 容错层：URI 含 query 时不抛异常，仅记录 WARN 并丢弃 query
- 兼容旧调用点：priority / fullscreen 属性永远返回 None
- 构造层（Agent SDK）：build_uri 不再接受 priority / fullscreen 参数
- 异常分支：非法 scheme、缺失 host
- Pydantic v2 集成校验与序列化
- is_window_uri 行为：含 query 仍判定为 valid
"""

import logging
from collections.abc import Iterator

import pytest
from pydantic import BaseModel

import a2c_smcp.utils.window_uri as window_uri_mod
from a2c_smcp.utils import WindowURI, is_window_uri


@pytest.fixture(autouse=True)
def attach_project_logger_to_caplog(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """
    捕获 window_uri 模块的 WARN 输出。
    Capture WARN output from the window_uri module.

    背景 / Background：项目 logger "a2c_smcp" 关闭了 propagate；
    且 tests/unit_tests/computer/utils/test_logger.py 的 reset 钩子会
    `logging.Logger.manager.loggerDict.clear()`，可能让 window_uri 里
    模块级 logger 变量与 getLogger("a2c_smcp.utils.window_uri") 指向不同对象。
    所以这里直接把 caplog.handler 挂到源模块当前持有的 logger 引用上。

    "a2c_smcp" disables propagation; and another test's reset hook calls
    loggerDict.clear(), which can detach window_uri's module-level logger.
    Attach caplog.handler directly to the source module's current logger reference.
    """
    src_logger = window_uri_mod.logger
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


# ------------------------------
# 解析相关 / Parsing tests
# ------------------------------


def test_parse_minimal() -> None:
    """最小 URI：仅 host，无路径与参数 / Minimal URI with only host"""
    u = WindowURI("window://com.example.mcp")
    assert u.mcp_id == "com.example.mcp"
    assert u.windows == []
    assert u.priority is None
    assert u.fullscreen is None


def test_parse_with_paths() -> None:
    """带多个路径段 / Multiple path segments"""
    u = WindowURI("window://com.example.mcp/dashboard/main")
    assert u.mcp_id == "com.example.mcp"
    assert u.windows == ["dashboard", "main"]


def test_parse_path_segment_url_decoding() -> None:
    """单段 URL 编码保持为一段 / URL-encoded slash within a segment stays one part"""
    u = WindowURI("window://h/a%2Fb")
    assert u.windows == ["a/b"]


# ------------------------------
# v0.2 容错：URI 含 query 时丢弃 + WARN
# ------------------------------


def test_parse_with_query_drops_query_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """含 query 的 URI 解析成功；query 部分被丢弃；priority/fullscreen 返回 None；记录 WARN

    URIs carrying query parse successfully; query is dropped; priority/fullscreen return None;
    a WARN log is emitted (Computer parser layer tolerance, protocol guide §4.1).
    """
    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        u = WindowURI("window://com.example.mcp/page?priority=90&fullscreen=true")

    assert u.mcp_id == "com.example.mcp"
    assert u.windows == ["page"]
    # v0.2：旧字段统一返回 None
    assert u.priority is None
    assert u.fullscreen is None

    # 至少一条 WARN 提到 query 被丢弃
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("query" in msg.lower() for msg in warn_msgs)


def test_parse_with_query_no_warn_on_empty(caplog: pytest.LogCaptureFixture) -> None:
    """无 query 的 URI 解析时不产生 WARN，避免日志风暴 / No WARN when query is absent"""
    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        WindowURI("window://com.example.mcp/page")

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("query" in msg.lower() for msg in warn_msgs)


def test_parse_with_invalid_query_value_does_not_raise() -> None:
    """v0.2：含非法 priority/fullscreen 值的 URI 不再抛异常 / v0.2 no longer raises on invalid query values"""
    # v0.1 时这些会抛 ValueError；v0.2 改为容错丢弃
    WindowURI("window://x?priority=-1")
    WindowURI("window://x?priority=999")
    WindowURI("window://x?priority=abc")
    WindowURI("window://x?fullscreen=maybe")


# ------------------------------
# 旧字段向后兼容 / Legacy attribute back-compat
# ------------------------------


def test_priority_and_fullscreen_always_none() -> None:
    """priority / fullscreen 属性永远返回 None（兼容旧调用点；由 sub-issue #4 移除）

    Attributes always return None for legacy callers (removed by sub-issue #4).
    """
    cases = [
        "window://h",
        "window://h/p",
        "window://h/p?priority=80",
        "window://h/p?priority=0&fullscreen=true",
        "window://h/p?fullscreen=false",
    ]
    for uri in cases:
        u = WindowURI(uri)
        assert u.priority is None
        assert u.fullscreen is None


# ------------------------------
# 构造函数 / Build tests
# ------------------------------


def test_build_uri_basic_and_roundtrip() -> None:
    """构造基本 URI 并可解析回属性 / Build and roundtrip parse"""
    u = WindowURI.build_uri(host="com.example.mcp", windows=["dashboard", "main"])
    s = str(u)
    assert s == "window://com.example.mcp/dashboard/main"

    u2 = WindowURI(s)
    assert u2.mcp_id == "com.example.mcp"
    assert u2.windows == ["dashboard", "main"]


def test_build_uri_encoding() -> None:
    """路径段自动 URL 编码，但解析后的 parts 为原始值 / Encoded on build, decoded on parse"""
    u = WindowURI.build_uri(host="h", windows=["A B", "c/d"])
    s = str(u)
    assert "A%20B" in s and "c%2Fd" in s
    u2 = WindowURI(s)
    assert u2.windows == ["A B", "c/d"]


def test_build_uri_no_path() -> None:
    """无 windows 时仅生成 host 形式 / Host-only when windows is empty"""
    u = WindowURI.build_uri(host="h", windows=[])
    assert str(u) == "window://h"


def test_build_uri_empty_host_rejected() -> None:
    """空 host 抛 ValueError / Empty host raises"""
    with pytest.raises(ValueError):
        WindowURI.build_uri(host="", windows=["p"])


def test_build_uri_rejects_legacy_priority_kwarg() -> None:
    """v0.2：build_uri 不再接受 priority / fullscreen 参数

    v0.2: build_uri no longer accepts priority / fullscreen kwargs (Agent SDK
    builder layer is strict — protocol guide §F4).
    """
    with pytest.raises(TypeError):
        WindowURI.build_uri(host="h", windows=["p"], priority=80)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        WindowURI.build_uri(host="h", windows=["p"], fullscreen=True)  # type: ignore[call-arg]


# ------------------------------
# 异常场景 / Error cases
# ------------------------------


def test_invalid_scheme_raises() -> None:
    """非法 scheme 抛 ValueError / Non-window scheme raises ValueError"""
    with pytest.raises(ValueError):
        WindowURI("http://x")


def test_missing_host_raises() -> None:
    """缺失 host 抛 IndexError / Missing host raises IndexError"""
    with pytest.raises(IndexError):
        WindowURI("window://")


# ------------------------------
# is_window_uri 行为 / is_window_uri behavior
# ------------------------------


def test_is_window_uri_true_cases() -> None:
    """合法 URI 返回 True；含 query 也算合法（仅记录 WARN） / Valid cases incl. query"""
    assert is_window_uri("window://h") is True
    assert is_window_uri("window://h/p1/p2") is True
    # v0.2：含 query 不再判定为非法
    assert is_window_uri("window://h?priority=80") is True


def test_is_window_uri_false_cases() -> None:
    """非法 URI 返回 False / Invalid cases"""
    assert is_window_uri("http://h") is False
    assert is_window_uri("window://") is False
    assert is_window_uri("not a uri") is False


# ------------------------------
# Pydantic v2 集成 / Pydantic integration
# ------------------------------


def test_pydantic_integration_validate_and_serialize() -> None:
    """Pydantic 字段类型为 WindowURI，验证字符串输入与序列化输出 / Pydantic field of WindowURI"""

    class M(BaseModel):
        uri: WindowURI

    m = M.model_validate({"uri": "window://com.example/p1/p2"})
    assert isinstance(m.uri, WindowURI)
    assert m.uri.mcp_id == "com.example"
    assert m.uri.windows == ["p1", "p2"]

    data = m.model_dump()
    assert isinstance(data["uri"], str)
    assert data["uri"] == "window://com.example/p1/p2"


def test_pydantic_integration_tolerates_query_with_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Pydantic 校验同样走 v0.2 容错：含 query 不抛错，仅 WARN / Pydantic shares v0.2 tolerance"""

    class M(BaseModel):
        uri: WindowURI

    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        m = M.model_validate({"uri": "window://com.example/p?priority=20"})

    assert m.uri.mcp_id == "com.example"
    assert m.uri.priority is None
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("query" in msg.lower() for msg in warn_msgs)
