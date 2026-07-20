# -*- coding: utf-8 -*-
# filename: auth_error.py
# @Time    : 2026/07/18
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
上游 MCP 工具授权错误：分类 + 结果构造（#133，协议 error-handling.md §4006/4007 + security.md）。

Upstream MCP tool authorization error: classification + result building (#133).

Computer 调用 MCP 工具因**上游授权**（OAuth 2.0 等）失败时，协议 **MUST** 以 ``CallToolResult`` 返回，并在
**结果级 ``meta``** 携带 ``error_code`` / ``mcp_server``（失败 server 的 **bundle_id**）/ ``auth_hint``。A2C 不介入
OAuth 握手（由 MCP 库 / 宿主负责），仅在调用失败时**反应式**分类上游失败信号并 surface。镜像 rust-sdk
``build_auth_error_result``（rust-sdk#120）；wire 契约双端一致（同一 #18 语义：``mcp_server`` == bundle_id）。

安全边界（协议 §auth_hint 安全边界）：``auth_hint`` 仅承载**静态非敏感**提示，本模块产出**绝不**嵌入异常原文
（可能含 token / URL / cookie），防止凭证经此通道泄漏给 Agent。
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
from mcp.client.auth import OAuthFlowError, OAuthTokenError
from mcp.types import CallToolResult, TextContent

from a2c_smcp.smcp import ErrorCode

__all__ = [
    "META_AUTH_HINT_KEY",
    "META_ERROR_CODE_KEY",
    "META_MCP_SERVER_KEY",
    "UpstreamAuthError",
    "build_auth_error_result",
    "classify_auth_error",
]


class UpstreamAuthError(Exception):
    """传输层已观测到上游授权失败信号（401/403）但 MCP 库拆连接致 ``call_tool`` 挂起时，由 HTTP client 兜底抛出。

    A typed signal raised by the HTTP client when it observed an upstream auth-failure (401/403) at the transport
    layer but the MCP library tore down the connection so ``call_tool`` would hang (协议 error-handling.md §可观测判据：
    授权失败 MUST NOT 表现为挂起至超时；Computer MUST 自身层面兜底)。

    携带**结构化**的 HTTP 状态码（及可选 ``WWW-Authenticate`` 响应头），供 :func:`classify_auth_error` 精确映射，
    不依赖字符串匹配。``www_authenticate_header`` 仅为将来更精确的 ``auth_hint`` 预留通道，**当前不外泄**（见
    build_auth_error_result：auth_hint 为静态非敏感值）。
    """

    def __init__(self, status_code: int, www_authenticate_header: str | None = None) -> None:
        self.status_code = status_code
        self.www_authenticate_header = www_authenticate_header
        super().__init__(f"upstream auth failure: HTTP {status_code}")


# 结果级 meta 字段键（协议字面键，非 A2C_ 前缀）/ result-level meta keys (protocol literal keys).
META_ERROR_CODE_KEY = "error_code"
META_MCP_SERVER_KEY = "mcp_server"
META_AUTH_HINT_KEY = "auth_hint"

# 每个错误码的静态 auth_hint（action 机器可读 + message 用户可读一句话）。action 为开放枚举（协议 MAY），
# 解析方须容忍未知值；message 为最小可用 UX。二者均非敏感、与具体异常无关。
_AUTH_HINT_BY_CODE: dict[ErrorCode, dict[str, str]] = {
    ErrorCode.TOOL_AUTHORIZATION_REQUIRED: {
        "action": "user_authorization_required",
        "message": "此工具需要授权后方可使用，请在 Computer 宿主环境完成授权后重试。",
    },
    ErrorCode.TOOL_AUTHORIZATION_FAILED: {
        "action": "token_refresh_required",
        "message": "工具授权已失效或权限不足，请重新完成授权或检查权限后重试。",
    },
}


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """遍历异常链：自身 + ``__cause__``（显式 ``raise ... from``）+ 拆 ``BaseExceptionGroup`` 子异常（去重防环）。

    传输层（anyio task group / streamable_http）把底层 httpx / OAuth 错误包在 ``ExceptionGroup``，或经
    ``raise ... from`` 嵌入 ``__cause__``，故分类须穿透这两类**显式 / 结构化**链而非只看顶层。

    **刻意不走 ``__context__``**（隐式链「在处理 A 时又抛了 B」自动挂接）：``__context__`` 可能挂上与本次失败
    **无因果关系**、恰好已被处理过的异常；若其中残留一个无关的 httpx 401/403，会把「工具坏了」误分类为
    「需授权」，误导用户走无谓授权。宁可漏判（退化为通用 isError = 现状，安全）也不误判（主动误导）。
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)


def classify_auth_error(exc: BaseException) -> ErrorCode | None:
    """把上游失败异常按协议决策表分类为 4006/4007；非授权失败返回 ``None``（走通用路径）。

    Classify an upstream failure to 4006/4007 per the protocol decision table; ``None`` if not an auth failure.

    映射（error-handling.md §4006/4007 判定决策表）：

    - HTTP 401 Unauthorized → **4006**（凭证缺失 / 非法 / 未提供，需首次或重新授权）
    - HTTP 403 Forbidden → **4007**（已登录但权限 / scope 不足）
    - ``OAuthTokenError``（token 交换 / 刷新失败，曾授权失效）→ **4007**
    - 其它 ``OAuthFlowError``（含 ``OAuthRegistrationError``：从未配置授权；或流程未完成）→ **4006**
      （协议：Computer 无法可靠判别时倾向 4006 稳妥兜底）

    仅在有**正面授权信号**时分类；无信号（stdio / 通用工具异常 / 其它 HTTP 状态）→ ``None``，绝不把普通失败
    误判为「需授权」。
    """
    for e in _iter_exception_chain(exc):
        # 传输层兜底信号（HTTP client 观测到 401/403 但 MCP 库拆连接致 call_tool 挂起 → 兜底抛此）：结构化状态码，
        # 优先判定、无字符串歧义。见协议 §可观测判据「自身层面兜底」。
        if isinstance(e, UpstreamAuthError):
            if e.status_code == httpx.codes.UNAUTHORIZED:  # 401
                return ErrorCode.TOOL_AUTHORIZATION_REQUIRED
            if e.status_code == httpx.codes.FORBIDDEN:  # 403
                return ErrorCode.TOOL_AUTHORIZATION_FAILED
            # 已判定属授权类但状态码非 401/403（理论不达：捕获点只收 401/403）→ 协议 §降级语义兜底 4006。
            return ErrorCode.TOOL_AUTHORIZATION_REQUIRED
        if isinstance(e, httpx.HTTPStatusError):
            status = e.response.status_code
            if status == httpx.codes.UNAUTHORIZED:  # 401
                return ErrorCode.TOOL_AUTHORIZATION_REQUIRED
            if status == httpx.codes.FORBIDDEN:  # 403
                return ErrorCode.TOOL_AUTHORIZATION_FAILED
        elif isinstance(e, OAuthTokenError):  # OAuthFlowError 子类，须先于基类判定
            return ErrorCode.TOOL_AUTHORIZATION_FAILED
        elif isinstance(e, OAuthFlowError):  # 含 OAuthRegistrationError 及其它流程错
            return ErrorCode.TOOL_AUTHORIZATION_REQUIRED
    return None


def build_auth_error_result(bundle_id: str, error_code: ErrorCode) -> CallToolResult:
    """构造上游授权失败的 ``CallToolResult``（协议 error-handling.md §授权失败的响应结构）。

    Build the authorization-failure ``CallToolResult`` carrying result-level ``meta``.

    :param bundle_id: 触发授权错误的 MCP Server 的 **bundle_id**（写入 ``meta.mcp_server``，供 Agent correlate）。
    :param error_code: :attr:`ErrorCode.TOOL_AUTHORIZATION_REQUIRED` (4006) 或 ``TOOL_AUTHORIZATION_FAILED`` (4007)。

    ``meta`` 经**属性赋值**写入（ctor ``meta=`` 会落入 pydantic extra 而非真实字段，导致出线 key 失真；参见
    ``Computer.aexecute_tool`` 取消结果同款 ``.meta =`` 赋值范式）。``auth_hint`` 为静态非敏感值，不含任何异常原文。
    """
    hint = _AUTH_HINT_BY_CODE[error_code]
    result = CallToolResult(isError=True, content=[TextContent(type="text", text=hint["message"])])
    result.meta = {
        META_ERROR_CODE_KEY: int(error_code),
        META_MCP_SERVER_KEY: bundle_id,
        META_AUTH_HINT_KEY: dict(hint),
    }
    return result
