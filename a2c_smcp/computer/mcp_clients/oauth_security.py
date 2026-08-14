# -*- coding: utf-8 -*-
# filename: oauth_security.py
"""OAuth 安全不变量（#181）——单一权威模块。

对齐 Rust ``crates/smcp-computer/src/oauth.rs`` 的安全校验函数与常量：

- :func:`validate_secure_url` / :func:`validate_authorization_metadata`（Rust ``validate_secure_url`` /
  ``validate_authorization_metadata``）：HTTPS-only 端点 + Auth Code 路径 PKCE S256 强制。
- :func:`same_origin`（Rust ``same_origin``，scheme + host + 默认端口归一的 port）：
  PRM 准入（manager）+ redirect 守卫（transport）+ PRM resource 复核（coordinator）三处共用。
- :class:`OAuthGuardTransport`（Rust ``DiscoveryCleanupOAuthHttpClient`` 的 python 面）：
  redirect 分面（resource-origin 仅 follow 同源 redirect；跨 origin stop）+ config header
  注入分面（自定义 header 仅注入 resource-origin 请求）+ 响应体上限（OAuth 面 ≤
  ``MAX_OAUTH_RESPONSE_BYTES``，MCP 消息流豁免）。
- :func:`install_mcp_auth_log_redaction`（Rust ``SensitiveAuthClient`` 的 python 等价物）：
  ``mcp.client.auth`` 日志面剥 exc_info + token 模式脱敏，防 pydantic ``ValidationError``
  的 ``input_value``（可能含 token / code）进入宿主日志。

Rust 的 ``SensitiveAuthClient`` 用 ``spawn_blocking`` + ``NoSubscriber`` 压制 rmcp 2.2 的
token 响应日志，属 Rust/tracing 特有；python 等价目标 =「token / 授权响应不进宿主日志」，
以日志 Filter + repr 脱敏实现（#181 设计考量）。
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthError,
    OAuthProtocolError,
)

# ── 常量（对齐 Rust oauth.rs L40-43）────────────────────────────────────────────

OAUTH_HTTP_TIMEOUT = 30.0
"""OAuth HTTP 请求超时（Rust ``OAUTH_HTTP_TIMEOUT``）。python 面由 httpx client
``timeout=30s`` 承载（``http_client._AuthWatchingClient`` 默认值，与 OAuth 面共用同一
client 即天然对齐）；``mcp.client.auth.OAuthContext.timeout``（默认 300）为死参数、
无消费点，不依赖。"""

MAX_OAUTH_RESPONSE_BYTES = 1024 * 1024
"""OAuth HTTP 响应体上限（Rust ``MAX_OAUTH_RESPONSE_BYTES``）：PRM / AS discovery /
registration / token 响应均受限；MCP 消息流（``Accept: text/event-stream``）豁免。"""

_MAX_REDIRECTS = 10
"""same-origin redirect 跟随上限（对齐 Rust ``attempt.previous().len() >= 10``）。"""

CROSS_ORIGIN_REDIRECT_STOP_MARKER = "a2c_smcp_cross_origin_redirect_stopped"
"""跨 origin redirect stop 的响应扩展标记（值 = 状态码）。

mcp ``post_writer`` 吞掉 3xx 的 raise_for_status 异常（#133 同款吞没），请求侧挂起——
守卫在 stop 的响应上打本标记，``_AuthWatchingClient.stream()`` 截获后经观察者
side-channel 合成 typed error（``UpstreamRedirectStoppedError``）解出竞速。
"""

# ── 纯函数校验（对齐 Rust validate_secure_url / same_origin）─────────────────────


def is_loopback_host(parsed: Any) -> bool:
    """True if the parsed URL's host is loopback（对齐 Rust ``is_loopback_host``）。"""
    host = parsed.hostname
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def same_origin(url_a: str, url_b: str) -> bool:
    """Same-origin 判定（scheme + host + 默认端口归一的 port）。

    Rust ``same_origin`` = ``scheme + host_str + port_or_known_default``。manager 的 PRM
    准入门槛、transport 的 redirect 守卫、coordinator 的 PRM resource 复核共用此判据
    （单一权威，勿再复制）。
    """
    a, b = urlparse(url_a), urlparse(url_b)
    default_ports = {"http": 80, "https": 443}

    def port_of(parsed: Any) -> int | None:
        # 越界端口（如 challenge header 携带 :99999）触发裸 ValueError——视为不可比
        # （返回 None，任何比较都判不同源；攻击者可控的 challenge 不得打崩 start）
        try:
            port = parsed.port
        except ValueError:
            return None
        return port if port is not None else default_ports.get(parsed.scheme.lower(), 0)

    return (
        a.scheme.lower() == b.scheme.lower()
        and a.hostname == b.hostname
        and port_of(a) is not None
        and port_of(a) == port_of(b)
    )


def validate_secure_url(value: str) -> str:
    """HTTPS-only 端点校验（对齐 Rust ``validate_secure_url``）。

    https 任意主机；http 仅 loopback（localhost / loopback IP）——开发场景豁免。
    其余 scheme / 非 loopback http 一律拒绝。错误为 ``Protocol(InvalidUrl)``
    分类（静态 message，不携带 URL 本体——raw URL 不得入日志）。

    Raises:
        OAuthError: ``Protocol(InvalidUrl)``。
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        raise OAuthError.protocol(OAuthProtocolError.InvalidUrl) from None
    scheme = parsed.scheme.lower()
    # 无主机 URL（如 "https://"）同样拒绝（Rust Url::parse 行为）
    if parsed.hostname is None:
        raise OAuthError.protocol(OAuthProtocolError.InvalidUrl)
    if scheme != "https" and not (scheme == "http" and is_loopback_host(parsed)):
        raise OAuthError.protocol(OAuthProtocolError.InvalidUrl)
    return value


def validate_authorization_metadata(oauth_metadata: Any, require_pkce: bool) -> None:
    """校验 mcp discovery 产出的 AS metadata（对齐 Rust ``validate_authorization_metadata``）。

    - authorization / token / registration / issuer / JWKS 端点全部 HTTPS-only
      （loopback http 豁免）；
    - ``require_pkce``（Auth Code 路径恒 True）时 ``code_challenge_methods_supported``
      须显式含 ``S256``——缺失即 typed error（mcp 硬编码 S256 发起、但从不校验服务端
      支持与否，本校验补齐「不支持 S256 → typed error」的产出路径）。

    Raises:
        OAuthError: ``Protocol(InvalidUrl)`` / ``Protocol(PkceUnsupported)``。
    """
    validate_secure_url(str(oauth_metadata.authorization_endpoint))
    validate_secure_url(str(oauth_metadata.token_endpoint))
    if oauth_metadata.registration_endpoint is not None:
        validate_secure_url(str(oauth_metadata.registration_endpoint))
    if oauth_metadata.issuer is not None:
        validate_secure_url(str(oauth_metadata.issuer))
    # jwks_uri：mcp 1.15 的 OAuthMetadata 未建模该字段（Rust rmcp 有）；防御性
    # getattr——mcp 升级引入后校验自动生效（安全纵深），当前版本 no-op。
    jwks_uri = getattr(oauth_metadata, "jwks_uri", None)
    if jwks_uri is not None:
        validate_secure_url(str(jwks_uri))
    if require_pkce:
        methods = oauth_metadata.code_challenge_methods_supported or []
        if "S256" not in methods:
            raise OAuthError.protocol(OAuthProtocolError.PkceUnsupported)


# ── OAuth HTTP 传输守卫（不变量 5 + 6）───────────────────────────────────────────


def _redirect_request(request: httpx.Request, status_code: int, location: str) -> httpx.Request:
    """构造 redirect 请求（逐条复刻 httpx ``_redirect_method`` / ``_redirect_headers``）。

    - 303 / 302 → 除 HEAD 外全部转 GET 丢弃 body；301 → 仅 POST 转 GET；307 / 308 →
      保持 method 与 body（httpx 0.28 ``_redirect_method`` 语义）；
    - 方法变 GET 只剥 ``content-length`` / ``transfer-encoding``——**不剥 content-type**
      （httpx 语义；307 保持 method 时更须保留，否则 starlette 无法解析重发的 JSON
      body，实测 400 → mcp 重试挂起）；
    - 跨 origin 改写 ``Host`` 为目标 netloc（httpx ``_redirect_headers`` 语义）。
    """
    method = request.method
    drop_body = (
        (status_code == 303 and method != "HEAD")
        or (status_code == 302 and method != "HEAD")
        or (status_code == 301 and method == "POST")
    )
    if drop_body:
        method = "GET"
    url = urljoin(str(request.url), location)
    headers = httpx.Headers(request.headers)
    if not same_origin(str(request.url), url):
        headers["host"] = urlparse(url).netloc
        # httpx _redirect_headers：跨 origin 且非 http→https 升级时剥 Authorization
        # （当前 mcp 的 AS 面请求不带该头，属纵深——mcp 升级引入 client_secret_basic
        # 或 token Bearer 后防凭据随 redirect 外发）
        if not (request.url.scheme == "http" and urlparse(url).scheme == "https"):
            headers.pop("authorization", None)
    if drop_body:
        headers.pop("content-length", None)
        headers.pop("transfer-encoding", None)
    # Cookie 由 httpx cookie jar 按目标 origin 重推导、且中间响应的 Set-Cookie 不进 jar
    # （守卫绕过 client 层 redirect 处理）——剥除对齐 httpx _redirect_headers；OAuth 面
    # 请求当前不带 Cookie，属纵深
    headers.pop("cookie", None)
    content = None if drop_body else request.content
    return httpx.Request(method, url, headers=headers, content=content, extensions=request.extensions)


def _is_streaming_mcp_request(request: httpx.Request) -> bool:
    """MCP 消息面判定：``Accept: text/event-stream``。

    已核实 mcp ``streamable_http`` 的全部 MCP 面请求（POST 消息 + GET SSE）携带
    ``Accept: application/json, text/event-stream``（``StreamableHTTPTransport.request_headers``），
    而 OAuth 面请求（PRM / AS discovery、registration、token）均不携带——以此为
    「MCP 消息流（响应体不受 1MB 上限）」与「OAuth 面（受限）」的判别信号。
    """
    return "text/event-stream" in request.headers.get("accept", "").lower()


class _LimitedByteStream(httpx.AsyncByteStream):
    """OAuth 面响应流：累积字节数超过上限即抛错（对齐 Rust 流式累积检查）。"""

    def __init__(self, stream: httpx.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._received = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate_limited()

    async def _iterate_limited(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._received += len(chunk)
            if self._received > self._limit:
                raise httpx.RemoteProtocolError(f"OAuth HTTP response body exceeds {self._limit} bytes")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class OAuthGuardTransport(httpx.AsyncBaseTransport):
    """OAuth 安全传输（Rust ``DiscoveryCleanupOAuthHttpClient`` 的 python 面）。

    mcp 的 OAuth 流程 HTTP 与 MCP 消息共用同一个 httpx client（auth 插件经
    ``auth_flow`` yield 机制内联发请求），故守卫落在 transport 层按请求分面：

    - **redirect 分面**：client ``follow_redirects=False`` 后由本 transport 手工
      follow。resource-origin 请求（MCP 消息 / PRM discovery）仅 follow
      **same-origin** redirect（≤ ``_MAX_REDIRECTS`` 次）；跨 origin 立即 stop
      （返回 3xx 响应，不泄漏任何 header）——对齐 Rust
      ``protected_resource_redirects`` 的 custom policy。非 resource-origin 请求
      （AS 面：authorization / token / registration）按标准规则自由 follow。
    - **header 注入分面**：config headers（如 ``X-Tenant-Id``）仅注入
      resource-origin 请求；AS 面请求一律剥离——对齐 Rust
      ``protected_resource_headers``（自定义 header 绝不随 AS 请求外发）。
    - **响应体上限**：OAuth 面响应 ≤ ``MAX_OAUTH_RESPONSE_BYTES``；MCP 消息流
      （event-stream accept）豁免（工具调用结果可大于 1MB）。

    注意：httpx 在跨 origin redirect 时仅剥离 ``Authorization`` / ``Cookie`` 等
    敏感 header，自定义 header 会跟随泄漏（GHSA-9g45-5xwm-f3wc 面）——故依赖
    httpx 内置行为不足以保证本不变量，必须本层 stop。
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        *,
        protected_resource_url: str,
        config_header_names: frozenset[str] = frozenset(),
        on_redirect_stop: Callable[[int, httpx.Request], None] | None = None,
    ) -> None:
        self._transport = transport
        self._protected_resource = protected_resource_url
        # 统一小写比对（httpx.Headers 大小写不敏感，按名字检索用原始 key 即可）
        self._config_header_names = frozenset(name.lower() for name in config_header_names)
        # #181：跨 origin stop 通知回调（observer 双通道写入）。auth 管道内请求
        # （discovery / DCR / token）走 httpx 内部 _send_handling_auth 路径、不经
        # client.stream() override——唯一可靠截获点在 transport 层
        self._on_redirect_stop = on_redirect_stop

    def _is_protected_resource_request(self, url: httpx.URL) -> bool:
        return same_origin(str(url), self._protected_resource)

    def _strip_config_headers(self, request: httpx.Request) -> None:
        for name in list(request.headers):
            if name.lower() in self._config_header_names:
                request.headers.pop(name)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._is_protected_resource_request(request.url):
            # AS 面：自定义 header 绝不外发（对齐 Rust protected_resource_headers 注入面）
            self._strip_config_headers(request)

        # ``response.request`` 在 transport 层不可依赖（httpx 在 transport 返回后才
        # 设置；MockTransport 亦不保证）——redirect 循环用自维护的 current
        current = request
        response = await self._transport.handle_async_request(current)
        for _ in range(_MAX_REDIRECTS):
            # 仅 follow httpx 的 redirect 状态码集合 {301,302,303,307,308}
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location")
            if location is None:
                break
            if self._is_protected_resource_request(current.url):
                target = urljoin(str(current.url), location)
                if not same_origin(target, self._protected_resource):
                    # 跨 origin redirect：stop（返回 3xx 给上层）。mcp 会把 3xx 的
                    # raise_for_status 异常吞掉致请求侧挂起（#133 同款）——打标记 +
                    # 通知回调（observer 双通道：per-rpc + connect）；不排干 body——
                    # 上层仍需读取该响应
                    response.extensions[CROSS_ORIGIN_REDIRECT_STOP_MARKER] = response.status_code
                    if self._on_redirect_stop is not None:
                        self._on_redirect_stop(response.status_code, current)
                    break
            # follow 前排干中间响应 body，归还连接池（httpx _send_handling_redirects
            # 同款语义；不排干则真实 transport 每条 redirect 烧掉一条连接）。
            # 排干同样受 1MB 上限约束（恶意/失陷 AS 可连发多跳大 body 放大内存——
            # 中间响应走 OAuth 面判别，非 event-stream 即受限）
            if isinstance(response.stream, httpx.AsyncByteStream):
                response.stream = _LimitedByteStream(response.stream, MAX_OAUTH_RESPONSE_BYTES)
            await response.aread()
            current = _redirect_request(current, response.status_code, location)
            response = await self._transport.handle_async_request(current)

        if not _is_streaming_mcp_request(current):
            stream = response.stream
            # AsyncClient 路径恒 async（client 层对 sync stream 先行断言），此处仅收窄类型
            if isinstance(stream, httpx.AsyncByteStream):
                response.stream = _LimitedByteStream(stream, MAX_OAUTH_RESPONSE_BYTES)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


# ── mcp.client.auth 日志脱敏（不变量 4，Rust SensitiveAuthClient 的 python 等价物）─

_MCP_AUTH_LOGGER_NAME = "mcp.client.auth"
_INSTALLED_MARKER = "_a2c_smcp_oauth_redaction_installed"

# token / code / state 的 key=value 形态（含 pydantic repr 形态）
_SECRET_KEY_VALUE_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|client_secret|code|state)\b"
    r"(\s*[=:]\s*)(?P<value>[^,}\s\"']+)"
)


def _redact(text: str) -> str:
    """把 token 类 key=value 的 value 替换为 ``[REDACTED]``。"""
    return _SECRET_KEY_VALUE_RE.sub(r"\g<1>\g<2>[REDACTED]", text)


class _OAuthSecretRedactionFilter(logging.Filter):
    """``mcp.client.auth`` 的日志脱敏 Filter。

    - 剥离 ``exc_info``：mcp 的 ``logger.exception("Invalid refresh response")`` /
      ``logger.exception("OAuth flow error")`` 携带的 pydantic ``ValidationError`` 内嵌
      ``input_value``（可能含 token / code）——mcp 的 message 均为静态文案，剥离后
      记录只剩诊断结论（对齐 Rust ``SensitiveAuthClient`` 的 NoSubscriber 压制语义）。
    - message 做 key=value 模式脱敏（纵深）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_info = None
            record.exc_text = None
        if record.msg and isinstance(record.msg, str):
            # getMessage() 渲染后替换，清空 args 防二次渲染
            record.msg = _redact(record.getMessage())
            record.args = ()
        return True


def install_mcp_auth_log_redaction() -> None:
    """给 ``mcp.client.auth`` 挂脱敏 Filter（幂等）。

    SDK 启用 OAuth（coordinator 构造 provider）时调用；仅影响 mcp 库自身的 OAuth
    日志面，不动宿主日志配置。
    """
    logger = logging.getLogger(_MCP_AUTH_LOGGER_NAME)
    if getattr(logger, _INSTALLED_MARKER, False):
        return
    logger.addFilter(_OAuthSecretRedactionFilter())
    setattr(logger, _INSTALLED_MARKER, True)
