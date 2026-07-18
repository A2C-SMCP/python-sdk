# -*- coding: utf-8 -*-
# filename: test_auth_error.py
# @Time    : 2026/07/18
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
上游 MCP 工具授权错误分类 + 结果构造单测（#133，协议 error-handling.md §4006/4007 + security.md；镜像 rust build_auth_error_result）。

Unit tests for upstream MCP tool authorization error classification + result building (#133).

测试意图 / Test intentions:
- 分类器按协议决策表把上游失败映射到 4006/4007：401→4006、403→4007、OAuthTokenError→4007、
  OAuthRegistrationError/OAuthFlowError→4006；非授权失败（普通异常）→ None（走通用路径）；
- 异常链穿透：包在 ``ExceptionGroup`` / 嵌 ``__cause__`` 内仍能命中（传输层常见包裹形态）；
- 构造器产出 ``CallToolResult(isError=True)``，``meta`` 三键（error_code / mcp_server / auth_hint）齐备，
  wire 出线 key 为顶层 ``meta``（非 ``_meta``）；
- 安全边界：``auth_hint`` / 文案为静态非敏感值，构造器不接收也不外泄任何异常原文。
"""

from __future__ import annotations

import httpx
import pytest
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

from a2c_smcp.computer.mcp_clients.auth_error import (
    META_AUTH_HINT_KEY,
    META_ERROR_CODE_KEY,
    META_MCP_SERVER_KEY,
    build_auth_error_result,
    classify_auth_error,
)
from a2c_smcp.smcp import ErrorCode


def _http_status_error(status: int, *, detail: str = "boom") -> httpx.HTTPStatusError:
    """构造一个携指定 HTTP 状态码的 httpx.HTTPStatusError（模拟上游 401/403）。"""
    req = httpx.Request("POST", "https://example.com/mcp")
    resp = httpx.Response(status, request=req, text=detail)
    return httpx.HTTPStatusError(detail, request=req, response=resp)


# ── 分类器：HTTP 状态码映射 ───────────────────────────────────────────────────
def test_classify_http_401_is_authorization_required() -> None:
    """上游 401 Unauthorized → 4006（未授权 / 需首次或重新授权）。"""
    assert classify_auth_error(_http_status_error(401)) == ErrorCode.TOOL_AUTHORIZATION_REQUIRED


def test_classify_http_403_is_authorization_failed() -> None:
    """上游 403 Forbidden → 4007（已登录但权限/scope 不足）。"""
    assert classify_auth_error(_http_status_error(403)) == ErrorCode.TOOL_AUTHORIZATION_FAILED


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_classify_other_http_status_is_not_auth(status: int) -> None:
    """非 401/403 的 HTTP 错误不属授权语义 → None（走通用工具失败路径）。"""
    assert classify_auth_error(_http_status_error(status)) is None


# ── 分类器：MCP OAuth 异常映射 ────────────────────────────────────────────────
def test_classify_oauth_token_error_is_failed() -> None:
    """OAuthTokenError（token 交换/刷新失败，曾授权失效）→ 4007。"""
    assert classify_auth_error(OAuthTokenError("refresh failed")) == ErrorCode.TOOL_AUTHORIZATION_FAILED


def test_classify_oauth_registration_error_is_required() -> None:
    """OAuthRegistrationError（从未配置授权）→ 4006。"""
    assert classify_auth_error(OAuthRegistrationError("register failed")) == ErrorCode.TOOL_AUTHORIZATION_REQUIRED


def test_classify_oauth_flow_error_defaults_to_required() -> None:
    """其它 OAuthFlowError（无法可靠判别）→ 4006（协议：无法判别时倾向 4006 稳妥兜底）。"""
    assert classify_auth_error(OAuthFlowError("flow broke")) == ErrorCode.TOOL_AUTHORIZATION_REQUIRED


# ── 分类器：异常链穿透（传输层包裹形态） ─────────────────────────────────────
def test_classify_unwraps_exception_group() -> None:
    """授权错误被包在 ExceptionGroup（anyio task group 常见）内仍能命中。"""
    eg = ExceptionGroup("transport", [ValueError("noise"), _http_status_error(403)])
    assert classify_auth_error(eg) == ErrorCode.TOOL_AUTHORIZATION_FAILED


def test_classify_walks_cause_chain() -> None:
    """授权错误经 ``raise ... from`` 嵌在 __cause__ 内仍能命中。"""
    try:
        try:
            raise _http_status_error(401)
        except httpx.HTTPStatusError as inner:
            raise RuntimeError("wrapped by acall_tool-style layer") from inner
    except RuntimeError as e:
        assert classify_auth_error(e) == ErrorCode.TOOL_AUTHORIZATION_REQUIRED


def test_classify_generic_exception_is_not_auth() -> None:
    """普通异常（工具坏了/参数错）→ None，绝不误判为授权失败。"""
    assert classify_auth_error(ValueError("tool blew up")) is None
    assert classify_auth_error(RuntimeError("Tool execution failed: something")) is None


def test_classify_ignores_implicit_context_chain() -> None:
    """刻意不穿透 ``__context__``（隐式链）：无关的、已处理过的 401 挂在 __context__ 上**不得**被误判为需授权。

    防 false-positive：``__context__`` 是「在处理 A 时又抛了 B」自动挂接，可能与本次失败无因果关系。宁可漏判
    （退化通用 isError = 现状）也不误判（主动误导用户走无谓授权）。
    """
    try:
        try:
            raise _http_status_error(401)
        except httpx.HTTPStatusError:
            raise RuntimeError("unrelated non-auth failure")  # noqa: B904 - 刻意裸 raise 造纯 __context__（__cause__=None）
    except RuntimeError as e:
        assert e.__context__ is not None and e.__cause__ is None, "前提：纯 __context__ 链（无 __cause__）"
        assert classify_auth_error(e) is None, "隐式 __context__ 链上的无关 401 不得触发误分类"


# ── 构造器：结果形状 + wire 契约 ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("code", "expected"),
    [(ErrorCode.TOOL_AUTHORIZATION_REQUIRED, 4006), (ErrorCode.TOOL_AUTHORIZATION_FAILED, 4007)],
)
def test_build_auth_error_result_shape(code: ErrorCode, expected: int) -> None:
    """构造 isError 结果，meta 三键齐备，mcp_server = 传入 bundle_id，error_code 为整数。"""
    result = build_auth_error_result("gh-bundle", code)
    assert result.isError is True
    assert result.content, "SHOULD 携人类可读文案（协议示例 content=[TextContent(...)]）"
    assert result.meta is not None
    assert result.meta[META_ERROR_CODE_KEY] == expected
    assert result.meta[META_MCP_SERVER_KEY] == "gh-bundle"
    assert isinstance(result.meta[META_AUTH_HINT_KEY], dict)
    assert result.meta[META_AUTH_HINT_KEY].get("message"), "auth_hint.message SHOULD 提供最小可用 UX"


def test_build_auth_error_result_wires_meta_not_underscore_meta() -> None:
    """wire 出线：``model_dump(mode='json')``（on_tool_call 用法，无 by_alias）令结果级 key 为顶层 ``meta``。"""
    result = build_auth_error_result("gh-bundle", ErrorCode.TOOL_AUTHORIZATION_REQUIRED)
    wire = result.model_dump(mode="json")
    assert "meta" in wire and "_meta" not in wire, "结果级 meta 出线 key MUST 为 meta（协议 data-structures.md §结果级 meta）"
    assert wire["meta"][META_MCP_SERVER_KEY] == "gh-bundle"
    assert wire["meta"][META_ERROR_CODE_KEY] == 4006


def test_build_auth_error_result_carries_only_static_hint_fields() -> None:
    """安全边界（§auth_hint）：构造器不接收异常/凭证，仅产**静态** ``{action, message}``——无任何凭证承载子字段。

    结构性证明「不外泄」：构造器签名只有 bundle_id + code，异常原文根本无从进入；auth_hint 键集锁死为
    ``{action, message}``，杜绝将来误加可藏凭证的子字段。动态异常原文不外泄由 ``test_manager_auth_error`` 的
    注入-secret 用例覆盖（异常经生产路径流过、断言 secret 不出现）。
    """
    result = build_auth_error_result("gh-bundle", ErrorCode.TOOL_AUTHORIZATION_FAILED)
    assert result.meta is not None
    hint = result.meta[META_AUTH_HINT_KEY]
    assert set(hint.keys()) == {"action", "message"}, "auth_hint 仅承载 action/message，杜绝凭证承载子字段"
    assert hint["action"] == "token_refresh_required"
    # mcp_server 仅为传入的 bundle_id（非凭证），且 error_code 为纯整数码。
    assert result.meta[META_MCP_SERVER_KEY] == "gh-bundle"
    assert result.meta[META_ERROR_CODE_KEY] == 4007
