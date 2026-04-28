# -*- coding: utf-8 -*-
# filename: test_dpe_uri.py
# @Time    : 2026/04/28 14:00
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
DPEURI 单元测试（v0.2 协议）/ Unit tests for DPEURI (v0.2 protocol)

测试意图 / Test intentions（覆盖协议指南 §6.1 测试矩阵）：
- 解析：host、doc-ref（单段 / 分段路径）、URL 编码 / 解码
- 拒绝：dpe://host（无 doc-ref，对应 4012）、非 dpe scheme、缺 host
- 容错：URI 含 query / fragment 时 WARN + 丢弃，不抛异常
- 反向域名 SHOULD lint：不规范 host 仅 WARN，不阻塞
- 构造层（Agent SDK）：build 严格，拒绝空 host / 空 doc_ref
- Pydantic v2 集成：字符串校验与序列化
- is_dpe_uri：含 query 仍判定为 valid
"""

import logging
from collections.abc import Iterator

import pytest
from pydantic import BaseModel

import a2c_smcp.utils.dpe_uri as dpe_uri_mod
from a2c_smcp.utils import DPEURI, is_dpe_uri


@pytest.fixture(autouse=True)
def attach_project_logger_to_caplog(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """
    捕获 dpe_uri 模块的 WARN 输出。
    Capture WARN output from the dpe_uri module.

    背景 / Background：项目 logger "a2c_smcp" 关闭了 propagate；
    且 tests/unit_tests/computer/utils/test_logger.py 的 reset 钩子会
    `logging.Logger.manager.loggerDict.clear()`，可能让 dpe_uri 里
    模块级 logger 变量与 getLogger("a2c_smcp.utils.dpe_uri") 指向不同对象。
    所以这里直接把 caplog.handler 挂到源模块当前持有的 logger 引用上。

    "a2c_smcp" disables propagation; another test's reset hook calls
    loggerDict.clear(), which can detach dpe_uri's module-level logger.
    Attach caplog.handler directly to the source module's current logger reference.
    """
    src_logger = dpe_uri_mod.logger
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


def test_parse_minimal_doc_ref() -> None:
    """单段 doc-ref / Single-segment doc-ref"""
    u = DPEURI("dpe://com.example.docs/rpt-2026")
    assert u.host == "com.example.docs"
    assert u.doc_ref == "rpt-2026"
    assert u.doc_ref_parts == ["rpt-2026"]


def test_parse_multi_segment_doc_ref() -> None:
    """多段 doc-ref / Multi-segment doc-ref (协议 §6.1)"""
    u = DPEURI("dpe://com.example.docs/reports/2026/annual")
    assert u.host == "com.example.docs"
    assert u.doc_ref == "reports/2026/annual"
    assert u.doc_ref_parts == ["reports", "2026", "annual"]


def test_parse_doc_ref_with_extension() -> None:
    """含扩展名 / With file extension (协议 §4.2)"""
    u = DPEURI("dpe://com.example.code/src/main/java/Foo.java")
    assert u.host == "com.example.code"
    assert u.doc_ref == "src/main/java/Foo.java"


def test_parse_url_encoded_segment_kept_single_part() -> None:
    """单段 URL 编码保持为一段 / URL-encoded slash within a segment stays one part (§6.1)"""
    u = DPEURI("dpe://com.example.docs/a%2Fb")
    assert u.doc_ref_parts == ["a/b"]
    # doc_ref 重组后包含解码后的斜杠（单段中的斜杠）
    assert u.doc_ref == "a/b"


def test_str_roundtrip_canonical() -> None:
    """str(uri) 返回标准字符串形式 / Canonical string form"""
    u = DPEURI("dpe://com.example.docs/reports/2026/annual")
    assert str(u) == "dpe://com.example.docs/reports/2026/annual"


# ------------------------------
# v0.2 容错：URI 含 query / fragment
# ------------------------------


def test_parse_with_query_drops_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """含 query 的 URI 解析成功；query 被丢弃；记录 WARN（协议 §6.1 / §F4）

    URIs carrying query parse successfully; query is dropped; a WARN log is emitted.
    """
    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        u = DPEURI("dpe://com.example.docs/doc?format=md")

    assert u.host == "com.example.docs"
    assert u.doc_ref == "doc"

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("query" in msg.lower() for msg in warn_msgs)


def test_parse_with_fragment_drops_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """含 fragment 的 URI 解析成功；fragment 被丢弃；记录 WARN

    URIs carrying fragment parse successfully; fragment is dropped; a WARN log is emitted.
    """
    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        u = DPEURI("dpe://com.example.docs/doc#section-1")

    assert u.host == "com.example.docs"
    assert u.doc_ref == "doc"

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("fragment" in msg.lower() for msg in warn_msgs)


def test_parse_no_warn_on_clean_uri(caplog: pytest.LogCaptureFixture) -> None:
    """无 query / fragment 的标准 URI 不产生 WARN / No WARN on clean URI"""
    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        DPEURI("dpe://com.example.docs/doc")

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # 不应有 query/fragment 相关的 WARN
    assert not any("query" in msg.lower() or "fragment" in msg.lower() for msg in warn_msgs)


# ------------------------------
# host 反向域名 SHOULD lint / Reverse-DNS host lint
# ------------------------------


def test_reverse_domain_compliant_host_no_warn(caplog: pytest.LogCaptureFixture) -> None:
    """合规反向域名 host 不产生 lint WARN / Compliant reverse-DNS host emits no lint WARN"""
    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        DPEURI("dpe://com.example.docs/doc")

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("反向域名" in msg or "reverse-DNS" in msg for msg in warn_msgs)


def test_non_reverse_domain_host_warns_but_parses(caplog: pytest.LogCaptureFixture) -> None:
    """单段 host 不阻塞解析，仅产生 SHOULD WARN / Single-segment host parses with SHOULD WARN"""
    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        u = DPEURI("dpe://docs/some-doc")

    assert u.host == "docs"
    assert u.doc_ref == "some-doc"
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("反向域名" in msg or "reverse-DNS" in msg for msg in warn_msgs)


def test_uppercase_host_warns(caplog: pytest.LogCaptureFixture) -> None:
    """大写 host 不规范，产生 WARN（注意 yarl 可能将 host 标准化为小写）/ Uppercase host warns"""
    # 用 underscore 代替 uppercase 触发 lint WARN（yarl 会自动小写化 host，因此用 _ 测试）
    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        DPEURI("dpe://docs_server/doc")

    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("反向域名" in msg or "reverse-DNS" in msg for msg in warn_msgs)


# ------------------------------
# 异常分支 / Error cases (协议 §6.1)
# ------------------------------


def test_invalid_scheme_raises() -> None:
    """非 dpe scheme 抛 ValueError / Non-dpe scheme raises"""
    with pytest.raises(ValueError):
        DPEURI("http://com.example.docs/doc")
    with pytest.raises(ValueError):
        DPEURI("window://com.example.docs/doc")


def test_missing_host_raises() -> None:
    """缺 host 抛 IndexError / Missing host raises (§6.1 dpe:///doc-ref)"""
    with pytest.raises(IndexError):
        DPEURI("dpe:///doc-ref")


def test_missing_doc_ref_raises_for_4012() -> None:
    """无 doc-ref（dpe://host）抛 ValueError，对应错误码 4012（§6.1）

    Empty doc-ref raises ValueError mapping to error code 4012.
    """
    with pytest.raises(ValueError, match="4012"):
        DPEURI("dpe://com.example.docs")


def test_empty_doc_ref_with_trailing_slash_raises() -> None:
    """带尾部斜杠但无 doc-ref（dpe://host/）也应被拒绝 / Trailing slash without doc-ref also rejected"""
    with pytest.raises(ValueError, match="4012"):
        DPEURI("dpe://com.example.docs/")


# ------------------------------
# 构造函数 / build() tests
# ------------------------------


def test_build_with_str_doc_ref_basic() -> None:
    """str doc-ref 构造 / build with str doc_ref"""
    u = DPEURI.build(host="com.example.docs", doc_ref="rpt-2026")
    assert str(u) == "dpe://com.example.docs/rpt-2026"
    assert u.doc_ref == "rpt-2026"


def test_build_with_str_doc_ref_multi_segment() -> None:
    """str doc-ref 自动按 '/' 分段 / Slash-split str doc_ref"""
    u = DPEURI.build(host="com.example.docs", doc_ref="reports/2026/annual")
    assert str(u) == "dpe://com.example.docs/reports/2026/annual"
    assert u.doc_ref_parts == ["reports", "2026", "annual"]


def test_build_with_iterable_doc_ref_keeps_segment_with_slash() -> None:
    """Iterable doc-ref 保持段内斜杠为编码（"c/d" -> "c%2Fd" 单段）

    Iterable doc_ref keeps in-segment slash encoded (single segment).
    """
    u = DPEURI.build(host="com.example.docs", doc_ref=["a", "c/d"])
    s = str(u)
    assert "c%2Fd" in s
    u2 = DPEURI(s)
    assert u2.doc_ref_parts == ["a", "c/d"]


def test_build_with_special_chars_url_encoded() -> None:
    """构造时特殊字符自动 URL 编码 / Special chars auto URL-encoded"""
    u = DPEURI.build(host="com.example.docs", doc_ref="A B")
    s = str(u)
    assert "A%20B" in s
    u2 = DPEURI(s)
    assert u2.doc_ref == "A B"


def test_build_empty_host_rejected() -> None:
    """空 host 抛 ValueError / Empty host raises"""
    with pytest.raises(ValueError):
        DPEURI.build(host="", doc_ref="doc")


def test_build_empty_doc_ref_rejected() -> None:
    """空 doc_ref 抛 ValueError（构造层严格 §F4）/ Empty doc_ref rejected (Agent SDK strict)"""
    with pytest.raises(ValueError):
        DPEURI.build(host="com.example.docs", doc_ref="")
    with pytest.raises(ValueError):
        DPEURI.build(host="com.example.docs", doc_ref=[])
    # 仅含斜杠也视为空
    with pytest.raises(ValueError):
        DPEURI.build(host="com.example.docs", doc_ref="/")


def test_build_roundtrip() -> None:
    """构造后再解析，属性一致 / Build then parse roundtrip"""
    built = DPEURI.build(host="com.example.docs", doc_ref="reports/2026/annual")
    parsed = DPEURI(str(built))
    assert parsed.host == "com.example.docs"
    assert parsed.doc_ref_parts == ["reports", "2026", "annual"]


# ------------------------------
# is_dpe_uri 行为 / is_dpe_uri behavior
# ------------------------------


def test_is_dpe_uri_true_cases() -> None:
    """合法 URI 返回 True；含 query 也算合法 / Valid cases incl. query"""
    assert is_dpe_uri("dpe://com.example.docs/doc") is True
    assert is_dpe_uri("dpe://com.example.docs/reports/2026/annual") is True
    # v0.2：含 query 不再判定为非法
    assert is_dpe_uri("dpe://com.example.docs/doc?format=md") is True


def test_is_dpe_uri_false_cases() -> None:
    """非法 URI 返回 False / Invalid cases"""
    assert is_dpe_uri("http://com.example.docs/doc") is False
    assert is_dpe_uri("dpe:///doc") is False
    assert is_dpe_uri("dpe://com.example.docs") is False  # 无 doc-ref
    assert is_dpe_uri("dpe://com.example.docs/") is False  # 空 doc-ref
    assert is_dpe_uri("not a uri") is False


# ------------------------------
# Pydantic v2 集成 / Pydantic integration
# ------------------------------


def test_pydantic_integration_validate_and_serialize() -> None:
    """Pydantic 字段类型为 DPEURI / Pydantic field of DPEURI"""

    class M(BaseModel):
        uri: DPEURI

    m = M.model_validate({"uri": "dpe://com.example.docs/reports/2026"})
    assert isinstance(m.uri, DPEURI)
    assert m.uri.host == "com.example.docs"
    assert m.uri.doc_ref_parts == ["reports", "2026"]

    data = m.model_dump()
    assert isinstance(data["uri"], str)
    assert data["uri"] == "dpe://com.example.docs/reports/2026"


def test_pydantic_integration_rejects_invalid() -> None:
    """非法字符串 Pydantic 校验失败 / Invalid string fails Pydantic validation"""

    class M(BaseModel):
        uri: DPEURI

    with pytest.raises(ValueError):
        M.model_validate({"uri": "dpe://com.example.docs"})  # 无 doc-ref
    with pytest.raises(ValueError):
        M.model_validate({"uri": "http://com.example.docs/doc"})  # 错 scheme


def test_pydantic_integration_tolerates_query_with_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Pydantic 校验同样走 v0.2 容错：含 query 不抛错，仅 WARN

    Pydantic shares v0.2 tolerance (query dropped + WARN, no exception).
    """

    class M(BaseModel):
        uri: DPEURI

    with caplog.at_level(logging.WARNING, logger="a2c_smcp"):
        m = M.model_validate({"uri": "dpe://com.example.docs/doc?format=md"})

    assert m.uri.doc_ref == "doc"
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("query" in msg.lower() for msg in warn_msgs)
