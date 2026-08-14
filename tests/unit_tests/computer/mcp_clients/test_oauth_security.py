# -*- coding: utf-8 -*-
# filename: test_oauth_security.py
# @Time    : 2026/08/14
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
#181：OAuth 安全不变量——URL 校验 / PKCE S256 强制 / same-origin redirect 守卫 /
响应体上限 / 日志脱敏。

向量对齐 Rust ``oauth.rs`` 内联测试：
- ``secure_url_policy_accepts_native_private_use_redirects_only``（4114）
- ``authorization_code_metadata_requires_explicit_s256_pkce``（4142）
- ``protected_resource_headers_do_not_follow_cross_origin_redirects``（4280）

关键判据双向断言（#181 验收）：不仅断言「错误路径未发生」，同时断言「正确路径
已发生」（请求计数）。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.shared.auth import OAuthMetadata

from a2c_smcp.computer.mcp_clients.oauth_security import (
    MAX_OAUTH_RESPONSE_BYTES,
    OAuthGuardTransport,
    _redact,
    install_mcp_auth_log_redaction,
    same_origin,
    validate_authorization_metadata,
    validate_secure_url,
)
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthError,
    OAuthErrorCode,
)

# ── same_origin ────────────────────────────────────────────────────────────────


class TestSameOrigin:
    def test_default_ports_normalize(self) -> None:
        assert same_origin("https://example.com/mcp", "https://example.com:443/other")
        assert same_origin("http://example.com/mcp", "http://example.com:80/other")

    def test_scheme_host_port_differ(self) -> None:
        assert not same_origin("https://example.com/mcp", "http://example.com/mcp")
        assert not same_origin("https://example.com/mcp", "https://other.com/mcp")
        assert not same_origin("https://example.com/mcp", "https://example.com:8443/mcp")

    def test_out_of_range_port_treated_as_different_origin(self) -> None:
        # 攻击者可控的 challenge header 携带越界端口不得打崩 start（ValueError 面）
        assert not same_origin("https://evil.example:99999/x", "https://evil.example/x")
        assert not same_origin("https://evil.example:99999/a", "https://evil.example:99999/b")

    def test_nondefault_port_matches_only_itself(self) -> None:
        assert same_origin("https://example.com:8443/a", "https://example.com:8443/b")
        assert not same_origin("https://example.com:8443/a", "https://example.com:9443/a")


# ── validate_secure_url（对齐 rust 4114-4118）───────────────────────────────────


class TestValidateSecureUrl:
    def test_https_accepts(self) -> None:
        assert validate_secure_url("https://resource.example/mcp") == "https://resource.example/mcp"

    @pytest.mark.parametrize(
        "url",
        ["http://127.0.0.1:8080/mcp", "http://localhost:8080/mcp", "http://[::1]:8080/mcp"],
    )
    def test_loopback_http_accepts(self, url: str) -> None:
        assert validate_secure_url(url) == url

    @pytest.mark.parametrize(
        "url",
        ["http://resource.example/mcp", "ftp://resource.example/mcp", "not-a-url", "https://"],
    )
    def test_non_https_rejects(self, url: str) -> None:
        with pytest.raises(OAuthError) as exc_info:
            validate_secure_url(url)
        assert exc_info.value.code == OAuthErrorCode.Protocol
        # 静态 message：URL 本体绝不入错误文案
        assert url not in str(exc_info.value)


# ── validate_authorization_metadata（对齐 rust 4142-4154）──────────────────────


def _metadata(**overrides: Any) -> OAuthMetadata:
    defaults: dict[str, Any] = {
        "issuer": "https://issuer.example",
        "authorization_endpoint": "https://issuer.example/authorize",
        "token_endpoint": "https://issuer.example/token",
        "code_challenge_methods_supported": ["S256"],
    }
    defaults.update(overrides)
    return OAuthMetadata.model_validate(defaults)


class TestValidateAuthorizationMetadata:
    def test_requires_explicit_s256(self) -> None:
        # 无 methods → err（rust 4147）
        with pytest.raises(OAuthError) as exc_info:
            validate_authorization_metadata(_metadata(code_challenge_methods_supported=None), True)
        assert exc_info.value.message == "OAuth protocol error: pkceUnsupported"
        # [plain] → err（rust 4149-4150）
        with pytest.raises(OAuthError) as exc_info:
            validate_authorization_metadata(_metadata(code_challenge_methods_supported=["plain"]), True)
        assert exc_info.value.message == "OAuth protocol error: pkceUnsupported"
        # [S256] → ok（rust 4152-4153）
        validate_authorization_metadata(_metadata(code_challenge_methods_supported=["S256"]), True)

    def test_non_auth_code_skips_pkce(self) -> None:
        # require_pkce=False：无 S256 声明不拒绝（client-credentials 面）
        validate_authorization_metadata(_metadata(code_challenge_methods_supported=None), False)

    @pytest.mark.parametrize(
        "field",
        ["issuer", "authorization_endpoint", "token_endpoint"],
    )
    def test_insecure_endpoint_rejects(self, field: str) -> None:
        with pytest.raises(OAuthError) as exc_info:
            validate_authorization_metadata(_metadata(**{field: "http://issuer.example/x"}), False)
        assert exc_info.value.code == OAuthErrorCode.Protocol

    def test_registration_endpoint_rejects(self) -> None:
        with pytest.raises(OAuthError):
            validate_authorization_metadata(_metadata(registration_endpoint="http://issuer.example/register"), False)

    def test_jwks_uri_rejects_when_present(self) -> None:
        # mcp 1.15 的 OAuthMetadata 未建模 jwks_uri（Rust rmcp 有）——防御性 getattr 分支：
        # 以带该属性的对象直测（mcp 升级引入字段后校验自动生效）
        metadata = SimpleNamespace(
            authorization_endpoint="https://issuer.example/authorize",
            token_endpoint="https://issuer.example/token",
            registration_endpoint=None,
            issuer="https://issuer.example",
            jwks_uri="http://issuer.example/jwks",
            code_challenge_methods_supported=["S256"],
        )
        with pytest.raises(OAuthError):
            validate_authorization_metadata(metadata, False)

    def test_loopback_http_endpoints_accept(self) -> None:
        # 开发场景：loopback http 端点放行（对齐 Rust is_loopback_host 豁免）
        validate_authorization_metadata(
            _metadata(
                authorization_endpoint="http://localhost:8080/authorize",
                token_endpoint="http://127.0.0.1:8080/token",
            ),
            True,
        )


# ── OAuthGuardTransport（不变量 5+6，对齐 rust 4280+）───────────────────────────

_RESOURCE = "https://resource.example/mcp"


def _mock_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class TestOAuthGuardTransport:
    @pytest.mark.asyncio
    async def test_cross_origin_redirect_stops_without_leaking_headers(self) -> None:
        # 对齐 rust 4280：cross-origin server 收到 0 请求、自定义 header 不泄漏。
        # 按 host 分派：若守卫失守 follow 了跨 origin redirect，cross 计数即 >0（双向断言）。
        cross_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "attacker.example":
                cross_requests.append(request)
                return httpx.Response(404, request=request)
            return httpx.Response(
                302,
                headers={"location": "https://attacker.example/capture"},
                request=request,
            )

        guard = OAuthGuardTransport(
            _mock_transport(handler),
            protected_resource_url=_RESOURCE,
            config_header_names=frozenset(["x-tenant-id"]),
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.get(_RESOURCE, headers={"x-tenant-id": "tenant-157"})
        assert response.status_code == 302
        assert cross_requests == []

    @pytest.mark.asyncio
    async def test_same_origin_redirect_follows(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/mcp":
                return httpx.Response(302, headers={"location": "/mcp/real"}, request=request)
            return httpx.Response(200, json={"ok": True}, request=request)

        guard = OAuthGuardTransport(_mock_transport(handler), protected_resource_url=_RESOURCE)
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.get(_RESOURCE)
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    @pytest.mark.asyncio
    async def test_redirect_follow_capped_at_ten(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(302, headers={"location": f"/hop{len(calls)}"}, request=request)

        guard = OAuthGuardTransport(_mock_transport(handler), protected_resource_url=_RESOURCE)
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.get(_RESOURCE)
        # 初始 + 10 次 follow；第 10 次 follow 的 302 不再跟（stop）
        assert response.status_code == 302
        assert len(calls) == 11

    @pytest.mark.asyncio
    async def test_config_headers_not_sent_to_as_requests(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={}, request=request)

        guard = OAuthGuardTransport(
            _mock_transport(handler),
            protected_resource_url=_RESOURCE,
            config_header_names=frozenset(["x-tenant-id"]),
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            # resource-origin：config header 保留
            await client.post(_RESOURCE, headers={"x-tenant-id": "tenant-157"}, content=b"{}")
            # AS 面：config header 剥离
            await client.post("https://as.example/token", headers={"x-tenant-id": "tenant-157"})
        assert seen[0].headers.get("x-tenant-id") == "tenant-157"
        assert "x-tenant-id" not in seen[1].headers

    @pytest.mark.asyncio
    async def test_oauth_response_body_capped_at_1mb(self) -> None:
        # 注意：httpx.Response(content=...) 预置 _content 会短路 aread() 的流迭代（假绿陷阱）——
        # 必须用 stream= 构造让读取路径真正走 _LimitedByteStream（真实网络响应即为流式）。
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=httpx.ByteStream(b"x" * (MAX_OAUTH_RESPONSE_BYTES + 1)),
                request=request,
            )

        guard = OAuthGuardTransport(_mock_transport(handler), protected_resource_url=_RESOURCE)
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            with pytest.raises(httpx.RemoteProtocolError, match="exceeds"):
                await client.post("https://as.example/token", content=b"{}")

    @pytest.mark.asyncio
    async def test_intermediate_redirect_body_also_capped(self) -> None:
        # 二轮审查 🟡3：redirect 中间响应的排干同样受 1MB 上限（防恶意多跳大 body
        # 放大内存）——超限抛错、不 follow
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/mcp":
                return httpx.Response(
                    302,
                    headers={"location": "/mcp/"},
                    stream=httpx.ByteStream(b"x" * (MAX_OAUTH_RESPONSE_BYTES + 1)),
                    request=request,
                )
            return httpx.Response(200, request=request)

        guard = OAuthGuardTransport(
            _mock_transport(handler), protected_resource_url=_RESOURCE
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            with pytest.raises(httpx.RemoteProtocolError, match="exceeds"):
                await client.get(_RESOURCE)

    @pytest.mark.asyncio
    async def test_mcp_streaming_response_exempt_from_body_cap(self) -> None:
        # MCP 消息面（event-stream accept）豁免：工具结果可超 1MB
        big = b"x" * (MAX_OAUTH_RESPONSE_BYTES + 1)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=httpx.ByteStream(big), request=request)

        guard = OAuthGuardTransport(_mock_transport(handler), protected_resource_url=_RESOURCE)
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.post(_RESOURCE, headers={"accept": "application/json, text/event-stream"}, content=b"{}")
        assert response.content == big

    @pytest.mark.asyncio
    async def test_302_redirects_any_method_to_get(self) -> None:
        # httpx _redirect_method：302 除 HEAD 外**所有方法**转 GET（浏览器语义，不只 POST）
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/mcp":
                return httpx.Response(302, headers={"location": "/mcp/"}, request=request)
            return httpx.Response(200, request=request)

        guard = OAuthGuardTransport(
            _mock_transport(handler), protected_resource_url=_RESOURCE
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.put(_RESOURCE, content=b"payload")
        assert response.status_code == 200
        assert seen[1].method == "GET"
        assert seen[1].content in (b"", None)

    @pytest.mark.asyncio
    async def test_301_post_redirects_to_get(self) -> None:
        # httpx _redirect_method：301 仅 POST 转 GET
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/mcp":
                return httpx.Response(301, headers={"location": "/mcp/"}, request=request)
            return httpx.Response(200, request=request)

        guard = OAuthGuardTransport(
            _mock_transport(handler), protected_resource_url=_RESOURCE
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.post(_RESOURCE, content=b"payload")
        assert response.status_code == 200
        assert seen[1].method == "GET"
        assert seen[1].content in (b"", None)

    @pytest.mark.asyncio
    async def test_302_head_method_preserved(self) -> None:
        # httpx _redirect_method：302 对 HEAD 不转 GET（HEAD 保留）
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/mcp":
                return httpx.Response(302, headers={"location": "/mcp/"}, request=request)
            return httpx.Response(200, request=request)

        guard = OAuthGuardTransport(
            _mock_transport(handler), protected_resource_url=_RESOURCE
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.head(_RESOURCE)
        assert response.status_code == 200
        assert seen[1].method == "HEAD"

    @pytest.mark.asyncio
    async def test_as_face_cross_origin_redirect_strips_authorization(self) -> None:
        # httpx _redirect_headers：跨 origin 且非 http→https 升级时剥 Authorization
        # （纵深——mcp 升级引入 token Bearer 后防凭据随 redirect 外发）
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "as1.example":
                return httpx.Response(
                    302, headers={"location": "https://as2.example/token"}, request=request
                )
            return httpx.Response(200, request=request)

        guard = OAuthGuardTransport(
            _mock_transport(handler), protected_resource_url=_RESOURCE
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.get(
                "https://as1.example/token", headers={"authorization": "Bearer secret-token"}
            )
        assert response.status_code == 200
        assert "authorization" not in seen[1].headers

    @pytest.mark.asyncio
    async def test_as_face_cross_origin_redirect_rewrites_host(self) -> None:
        # AS 面（非 resource-origin）自由 follow + 跨 origin 改写 Host（httpx
        # _redirect_headers 语义——虚拟主机路由正确性）
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "as1.example":
                return httpx.Response(
                    302, headers={"location": "https://as2.example/token"}, request=request
                )
            return httpx.Response(200, request=request)

        guard = OAuthGuardTransport(
            _mock_transport(handler), protected_resource_url=_RESOURCE
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.get("https://as1.example/token")
        assert response.status_code == 200
        assert seen[1].url.host == "as2.example"
        assert seen[1].headers.get("host") == "as2.example"

    @pytest.mark.asyncio
    async def test_300_multiple_choice_is_not_followed(self) -> None:
        # 仅 follow httpx 的 redirect 集合 {301,302,303,307,308}；300/304/305/306 原样返回。
        # 双向断言：请求计数 == 1——若守卫误 follow，循环后最终响应仍是 300（断言
        # status_code 会假绿），计数才能区分
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(
                300, headers={"location": "/elsewhere"}, request=request
            )

        guard = OAuthGuardTransport(
            _mock_transport(handler), protected_resource_url=_RESOURCE
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.get(_RESOURCE)
        assert response.status_code == 300
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_307_redirect_preserves_content_type_and_body(self) -> None:
        # 回归（#181 实测挂起）：starlette 对 mount 路由返回 307 → 重发必须保留
        # content-type / content-length，否则 server 无法解析 JSON body（400 → mcp
        # 重试挂起）。307 语义 = 方法 + body 全保留（httpx _redirect_headers 对齐）。
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/mcp":
                return httpx.Response(307, headers={"location": "/mcp/"}, request=request)
            return httpx.Response(200, request=request)

        guard = OAuthGuardTransport(
            _mock_transport(handler), protected_resource_url=_RESOURCE
        )
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.post(
                _RESOURCE, json={"probe": True}, headers={"content-type": "application/json"}
            )
        assert response.status_code == 200
        # 双向断言：重发请求保留 method / content-type / body
        assert seen[1].method == "POST"
        assert seen[1].headers.get("content-type") == "application/json"
        assert seen[1].content == seen[0].content

    @pytest.mark.asyncio
    async def test_303_post_redirect_drops_body(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/mcp":
                return httpx.Response(303, headers={"location": "/moved"}, request=request)
            return httpx.Response(200, request=request)

        guard = OAuthGuardTransport(_mock_transport(handler), protected_resource_url=_RESOURCE)
        async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
            response = await client.post(_RESOURCE, content=b"payload")
        assert response.status_code == 200
        assert seen[1].method == "GET"
        assert seen[1].content in (b"", None)
        # httpx 语义：方法变 GET 只剥 content-length / transfer-encoding，不剥 content-type


# ── 日志脱敏（不变量 4，Rust SensitiveAuthClient 的 python 等价物）──────────────


class TestMcpAuthLogRedaction:
    def test_validation_error_exc_info_stripped(self, caplog: pytest.LogCaptureFixture) -> None:
        install_mcp_auth_log_redaction()
        logger = logging.getLogger("mcp.client.auth")
        with caplog.at_level(logging.ERROR, logger="mcp.client.auth"):
            try:
                OAuthMetadata.model_validate({"issuer": "secret-token-value-123", "authorization_endpoint": "x"})
            except Exception:  # pragma: no cover - 触发 exc_info 路径
                logger.exception("OAuth flow error")
                logger.error("raw leak: access_token=secret-token-value-123")
            else:  # pragma: no cover - 防假绿：校验必须真的抛错（异常未抛则 filter 路径未走到）
                pytest.fail("OAuthMetadata.model_validate 未抛异常——测试前置失真")
        records = caplog.records
        assert records, "mcp.client.auth 日志未被记录（filter 误杀全部）"
        rendered = "\n".join(r.getMessage() for r in records)
        assert "secret-token-value-123" not in rendered
        # 双向断言：exc_info 被剥离（ValidationError input_value 不可达）且静态文案仍在
        assert all(r.exc_info is None for r in records)
        assert "OAuth flow error" in rendered
        assert "access_token=[REDACTED]" in rendered

    def test_redact_handles_various_shapes(self) -> None:
        assert _redact("access_token=abc123") == "access_token=[REDACTED]"
        assert _redact("refresh_token: xyz") == "refresh_token: [REDACTED]"
        assert _redact("Bearer access_token=abc, other=1") == "Bearer access_token=[REDACTED], other=1"
        assert _redact("no secrets here") == "no secrets here"

    def test_install_is_idempotent(self) -> None:
        install_mcp_auth_log_redaction()
        logger = logging.getLogger("mcp.client.auth")
        count = sum(1 for f in logger.filters if type(f).__name__ == "_OAuthSecretRedactionFilter")
        assert count == 1
