# -*- coding: utf-8 -*-
# filename: test_oauth_types.py
# @Time    : 2026/08/11
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
测试 OAuth 领域类型的序列化 / 反序列化、camelCase 输出、StreamableHttpServerConfig 集成、
Rust 对照语义对齐。
"""
from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from a2c_smcp.computer.mcp_clients.model import StreamableHttpServerConfig
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
    OAuthClientMode,
    OAuthClientRegistration,
    OAuthError,
    OAuthErrorCode,
    OAuthFlowOutcome,
    OAuthLaunch,
    OAuthOptions,
    OAuthProtocolError,
    OAuthStatus,
    _OAuthModeAuthCodeCIMD,
    _OAuthModeAuthCodeDynamic,
    _OAuthModeAuthCodePreregistered,
    _OAuthModeCCPrivateKeyJwt,
    _OAuthModeCCSecret,
    _OAuthOutcomeAuthorized,
    _OAuthOutcomeTerminated,
    _OAuthRegClientMetadataDocument,
    _OAuthRegDynamic,
    _OAuthRegPreregistered,
    _OAuthStatusAuthorizationPending,
    _OAuthStatusAuthorized,
    _OAuthStatusError,
    _OAuthStatusReauthorizationRequired,
    _OAuthStatusUnauthorized,
)

# ============================================================================
# Helpers
# ============================================================================

_CLIENT_MODE_ADAPTER = TypeAdapter(OAuthClientMode)
_CLIENT_REG_ADAPTER = TypeAdapter(OAuthClientRegistration)
_STATUS_ADAPTER = TypeAdapter(OAuthStatus)
_OUTCOME_ADAPTER = TypeAdapter(OAuthFlowOutcome)


def _roundtrip(adapter: TypeAdapter, model: object) -> object:
    """JSON 序列化 → 反序列化 roundtrip，返回反序列化后的实例。"""
    json_str = adapter.dump_json(model)
    return adapter.validate_json(json_str)


def _assert_camel_keys(json_str: str | bytes, *expected_keys: str) -> None:
    """解析 JSON 并断言所有期望的 camelCase key 存在。"""
    data = json.loads(json_str)
    for key in expected_keys:
        assert key in data, f"Expected camelCase key {key!r} not found in {data}"


# ============================================================================
# OAuthClientRegistration — 3 variants round-trip
# ============================================================================


class TestOAuthClientRegistration:
    def test_dynamic_roundtrip(self) -> None:
        reg = _OAuthRegDynamic()
        back = _roundtrip(_CLIENT_REG_ADAPTER, reg)
        assert back == reg
        assert isinstance(back, _OAuthRegDynamic)
        assert back.registration == "dynamic"

    def test_dynamic_camel_keys(self) -> None:
        reg = _OAuthRegDynamic()
        data = json.loads(_CLIENT_REG_ADAPTER.dump_json(reg))
        assert data == {"registration": "dynamic"}

    def test_preregistered_roundtrip(self) -> None:
        reg = _OAuthRegPreregistered(client_id="client-1", client_secret_input="sec-inp-1")
        back = _roundtrip(_CLIENT_REG_ADAPTER, reg)
        assert back == reg
        assert isinstance(back, _OAuthRegPreregistered)
        assert back.client_id == "client-1"
        assert back.client_secret_input == "sec-inp-1"

    def test_preregistered_minimal_roundtrip(self) -> None:
        """无 client_secret_input（public client）。"""
        reg = _OAuthRegPreregistered(client_id="public-client")
        back = _roundtrip(_CLIENT_REG_ADAPTER, reg)
        assert back == reg
        assert back.client_secret_input is None

    def test_preregistered_camel_keys(self) -> None:
        reg = _OAuthRegPreregistered(client_id="c1", client_secret_input="s1")
        data = json.loads(_CLIENT_REG_ADAPTER.dump_json(reg))
        assert data["clientId"] == "c1"

    def test_cimd_roundtrip(self) -> None:
        reg = _OAuthRegClientMetadataDocument(url="https://example.com/.well-known/oauth-client-metadata")
        back = _roundtrip(_CLIENT_REG_ADAPTER, reg)
        assert back == reg
        assert isinstance(back, _OAuthRegClientMetadataDocument)
        assert back.url == "https://example.com/.well-known/oauth-client-metadata"

    def test_cimd_camel_keys(self) -> None:
        reg = _OAuthRegClientMetadataDocument(url="https://example.com/meta")
        _assert_camel_keys(
            _CLIENT_REG_ADAPTER.dump_json(reg),
            "registration", "url",
        )

    def test_reject_invalid_variant(self) -> None:
        """未知 registration 值应被拒绝。"""
        with pytest.raises(ValidationError):
            _CLIENT_REG_ADAPTER.validate_json(json.dumps({"registration": "unknown"}))


# ============================================================================
# OAuthClientMode — 5 variants round-trip
# ============================================================================


class TestOAuthClientMode:
    def test_auth_code_dynamic_roundtrip(self) -> None:
        mode = _OAuthModeAuthCodeDynamic()
        back = _roundtrip(_CLIENT_MODE_ADAPTER, mode)
        assert back == mode
        assert isinstance(back, _OAuthModeAuthCodeDynamic)

    def test_auth_code_dynamic_json(self) -> None:
        """验证 JSON 形态与 Rust 一致。"""
        mode = _OAuthModeAuthCodeDynamic()
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        assert data == {"type": "authorizationCode", "registration": "dynamic"}

    def test_auth_code_preregistered_roundtrip(self) -> None:
        mode = _OAuthModeAuthCodePreregistered(
            client_id="acme-client", client_secret_input="secret-input-42",
        )
        back = _roundtrip(_CLIENT_MODE_ADAPTER, mode)
        assert back == mode
        assert isinstance(back, _OAuthModeAuthCodePreregistered)
        assert back.client_id == "acme-client"
        assert back.client_secret_input == "secret-input-42"

    def test_auth_code_preregistered_json(self) -> None:
        mode = _OAuthModeAuthCodePreregistered(client_id="acme-client", client_secret_input="si-1")
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        assert data == {
            "type": "authorizationCode",
            "registration": "preregistered",
            "clientId": "acme-client",
            "clientSecretInput": "si-1",
        }

    def test_auth_code_cimd_roundtrip(self) -> None:
        mode = _OAuthModeAuthCodeCIMD(url="https://idp.example.com/cimd")
        back = _roundtrip(_CLIENT_MODE_ADAPTER, mode)
        assert back == mode
        assert isinstance(back, _OAuthModeAuthCodeCIMD)

    def test_auth_code_cimd_json(self) -> None:
        mode = _OAuthModeAuthCodeCIMD(url="https://idp.example.com/cimd")
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        assert data == {
            "type": "authorizationCode",
            "registration": "clientMetadataDocument",
            "url": "https://idp.example.com/cimd",
        }

    def test_cc_secret_roundtrip(self) -> None:
        mode = _OAuthModeCCSecret(client_id="svc-account", client_secret_input="cc-secret-inp")
        back = _roundtrip(_CLIENT_MODE_ADAPTER, mode)
        assert back == mode
        assert isinstance(back, _OAuthModeCCSecret)

    def test_cc_secret_json(self) -> None:
        mode = _OAuthModeCCSecret(client_id="svc", client_secret_input="si")
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        assert data == {
            "type": "clientCredentialsSecret",
            "clientId": "svc",
            "clientSecretInput": "si",
        }

    def test_cc_private_key_jwt_roundtrip(self) -> None:
        mode = _OAuthModeCCPrivateKeyJwt(
            client_id="jwt-client",
            private_key_input="pk-input",
            algorithm="ES256",
            token_endpoint_audience="https://api.example.com",
        )
        back = _roundtrip(_CLIENT_MODE_ADAPTER, mode)
        assert back == mode
        assert isinstance(back, _OAuthModeCCPrivateKeyJwt)

    def test_cc_private_key_jwt_defaults(self) -> None:
        """默认 algorithm=RS256，token_endpoint_audience=None。"""
        mode = _OAuthModeCCPrivateKeyJwt(client_id="jwt-client", private_key_input="pk-input")
        assert mode.algorithm == "RS256"
        assert mode.token_endpoint_audience is None
        back = _roundtrip(_CLIENT_MODE_ADAPTER, mode)
        assert back == mode

    def test_cc_private_key_jwt_json(self) -> None:
        mode = _OAuthModeCCPrivateKeyJwt(client_id="jwt-c", private_key_input="pk")
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        assert data["type"] == "clientCredentialsPrivateKeyJwt"
        assert data["clientId"] == "jwt-c"
        assert data["privateKeyInput"] == "pk"
        assert data["algorithm"] == "RS256"

    def test_disambiguate_auth_code_variants(self) -> None:
        """同一 discriminator 值 ``authorizationCode`` 的三变体必须精确区分。

        ``extra="forbid"`` 是关键——无此标志 Dynamic 会吞掉 Preregistered 的额外字段。
        """
        cases = [
            '{"type":"authorizationCode","registration":"dynamic"}',
            '{"type":"authorizationCode","registration":"preregistered","clientId":"c1"}',
            '{"type":"authorizationCode","registration":"clientMetadataDocument","url":"https://x.com"}',
        ]
        for json_str in cases:
            result = _CLIENT_MODE_ADAPTER.validate_json(json_str)
            # exclude_none 与 Rust serde skip_serializing_if=None 对齐
            back_json = json.loads(_CLIENT_MODE_ADAPTER.dump_json(result, exclude_none=True))
            assert json.loads(json_str) == back_json, f"Round-trip mismatch for {json_str}"

    def test_union_order_robustness(self) -> None:
        """倒序 / 乱序 Union 不破坏反序列化（Literal 约束 + extra="forbid" 保证鲁棒性）。

        当前正确顺序（Dynamic 先）的守卫非功能依赖——各变体的 Literal discriminator
        （``type`` + ``registration``）已提供充分区分。本测试验证：即使把 Dynamic 置末，
        三歧义变体仍正确分类。
        """
        from pydantic import TypeAdapter as _TA

        # 倒序：Dynamic 置末（最宽变体在最后——worst case for extra="ignore"）
        _reordered = (
            _OAuthModeAuthCodeCIMD
            | _OAuthModeAuthCodePreregistered
            | _OAuthModeAuthCodeDynamic
            | _OAuthModeCCSecret
            | _OAuthModeCCPrivateKeyJwt
        )
        _adapter = _TA(_reordered)

        cases = [
            ('{"type":"authorizationCode","registration":"dynamic"}', _OAuthModeAuthCodeDynamic),
            (
                '{"type":"authorizationCode","registration":"preregistered","clientId":"c1","clientSecretInput":"s1"}',
                _OAuthModeAuthCodePreregistered,
            ),
            (
                '{"type":"authorizationCode","registration":"clientMetadataDocument","url":"https://x.com"}',
                _OAuthModeAuthCodeCIMD,
            ),
            (
                '{"type":"clientCredentialsSecret","clientId":"cc","clientSecretInput":"s"}',
                _OAuthModeCCSecret,
            ),
            (
                '{"type":"clientCredentialsPrivateKeyJwt","clientId":"jwt","privateKeyInput":"pk"}',
                _OAuthModeCCPrivateKeyJwt,
            ),
        ]
        for json_str, expected_cls in cases:
            result = _adapter.validate_json(json_str)
            assert isinstance(result, expected_cls), (
                f"Reordered union: expected {expected_cls.__name__} for {json_str}, "
                f"got {type(result).__name__}"
            )
            # 序列化 → 反序列化闭环
            back = _adapter.validate_json(_adapter.dump_json(result, exclude_none=True))
            assert type(back) is type(result), (
                f"Reordered union roundtrip type mismatch: "
                f"{type(result).__name__} → {type(back).__name__}"
            )


# ============================================================================
# OAuthOptions
# ============================================================================


class TestOAuthOptions:
    def test_minimal_roundtrip(self) -> None:
        opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic())
        back = _roundtrip(TypeAdapter(OAuthOptions), opts)
        assert back == opts
        assert back.resource is None
        assert back.scopes == []
        assert back.client_name is None

    def test_full_roundtrip(self) -> None:
        opts = OAuthOptions(
            resource="https://api.example.com",
            scopes=["files:read", "files:write"],
            client_name="My MCP Client",
            mode=_OAuthModeCCSecret(client_id="svc", client_secret_input="inp-1"),
        )
        back = _roundtrip(TypeAdapter(OAuthOptions), opts)
        assert back == opts
        assert back.scopes == ["files:read", "files:write"]
        assert isinstance(back.mode, _OAuthModeCCSecret)

    def test_camel_keys(self) -> None:
        opts = OAuthOptions(
            resource="https://rs.example.com",
            scopes=["read"],
            client_name="Test",
            mode=_OAuthModeAuthCodeDynamic(),
        )
        adapter = TypeAdapter(OAuthOptions)
        data = json.loads(adapter.dump_json(opts))
        assert data["resource"] == "https://rs.example.com"
        assert data["mode"]["type"] == "authorizationCode"

    def test_exclude_none_omits_optionals(self) -> None:
        """serialize with exclude_none 应与 Rust skip_serializing_if=None 行为一致。"""
        opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic())
        data = json.loads(TypeAdapter(OAuthOptions).dump_json(opts, exclude_none=True))
        # resource / client_name 均为 None → 不出现在 JSON 中
        assert "resource" not in data
        assert "clientName" not in data
        assert data["mode"]["type"] == "authorizationCode"
        # scopes 是 []（非 None）→ 应出现
        assert data["scopes"] == []


# ============================================================================
# OAuthBeginRequest
# ============================================================================


class TestOAuthBeginRequest:
    def test_roundtrip(self) -> None:
        req = OAuthBeginRequest(redirect_uri="https://app.example.com/callback", required_scope="files:read")
        adapter = TypeAdapter(OAuthBeginRequest)
        back = adapter.validate_json(adapter.dump_json(req))
        assert back == req
        assert back.redirect_uri == "https://app.example.com/callback"
        assert back.required_scope == "files:read"

    def test_minimal(self) -> None:
        req = OAuthBeginRequest(redirect_uri="com.example.app:/callback")
        adapter = TypeAdapter(OAuthBeginRequest)
        back = adapter.validate_json(adapter.dump_json(req))
        assert back == req
        assert back.required_scope is None

    def test_camel_keys(self) -> None:
        req = OAuthBeginRequest(redirect_uri="https://cb.example.com")
        adapter = TypeAdapter(OAuthBeginRequest)
        _assert_camel_keys(adapter.dump_json(req), "redirectUri")


# ============================================================================
# OAuthLaunch — repr redaction
# ============================================================================


class TestOAuthLaunch:
    def test_roundtrip(self) -> None:
        launch = OAuthLaunch(
            authorization_url="https://auth.example.com/authorize?client_id=x&state=abc",
            state="opaque-state-123",
        )
        adapter = TypeAdapter(OAuthLaunch)
        back = adapter.validate_json(adapter.dump_json(launch))
        assert back == launch
        assert back.authorization_url == launch.authorization_url
        assert back.state == launch.state

    def test_camel_keys(self) -> None:
        launch = OAuthLaunch(authorization_url="https://a.example.com", state="s1")
        adapter = TypeAdapter(OAuthLaunch)
        _assert_camel_keys(adapter.dump_json(launch), "authorizationUrl", "state")

    def test_repr_redacts_sensitive_fields(self) -> None:
        launch = OAuthLaunch(authorization_url="https://secret.example.com?token=abc", state="super-secret")
        r = repr(launch)
        assert "[REDACTED]" in r
        assert "secret.example.com" not in r
        assert "super-secret" not in r
        assert "abc" not in r


# ============================================================================
# OAuthCallback — repr redaction
# ============================================================================


class TestOAuthCallback:
    def test_roundtrip(self) -> None:
        cb = OAuthCallback(code="auth-code-xyz", state="state-123", issuer="https://as.example.com")
        adapter = TypeAdapter(OAuthCallback)
        back = adapter.validate_json(adapter.dump_json(cb))
        assert back == cb
        assert back.code == "auth-code-xyz"
        assert back.issuer == "https://as.example.com"

    def test_minimal(self) -> None:
        cb = OAuthCallback(code="code-1", state="state-1")
        adapter = TypeAdapter(OAuthCallback)
        back = adapter.validate_json(adapter.dump_json(cb))
        assert back == cb
        assert back.issuer is None

    def test_camel_keys(self) -> None:
        cb = OAuthCallback(code="c1", state="s1", issuer="https://iss.example.com")
        adapter = TypeAdapter(OAuthCallback)
        _assert_camel_keys(adapter.dump_json(cb), "code", "state", "issuer")

    def test_repr_redacts_sensitive_fields(self) -> None:
        cb = OAuthCallback(code="secret-code-999", state="secret-state-888", issuer="https://is.example.com")
        r = repr(cb)
        assert "[REDACTED]" in r
        assert "secret-code-999" not in r
        assert "secret-state-888" not in r
        # issuer is NOT redacted
        assert "is.example.com" in r


# ============================================================================
# OAuthCancellation + OAuthCancellationReason
# ============================================================================


class TestOAuthCancellation:
    def test_roundtrip(self) -> None:
        cancel = OAuthCancellation(
            state="state-abc", issuer="https://as.example.com", reason=OAuthCancellationReason.Cancelled,
        )
        adapter = TypeAdapter(OAuthCancellation)
        back = adapter.validate_json(adapter.dump_json(cancel))
        assert back == cancel
        assert back.reason == OAuthCancellationReason.Cancelled

    def test_camel_keys(self) -> None:
        cancel = OAuthCancellation(state="s1", reason=OAuthCancellationReason.Timeout)
        adapter = TypeAdapter(OAuthCancellation)
        _assert_camel_keys(adapter.dump_json(cancel), "state", "reason")

    def test_reason_values(self) -> None:
        """验证 OAuthCancellationReason JSON 序列化值与 Rust 一致。"""
        assert OAuthCancellationReason.AccessDenied.value == "accessDenied"
        assert OAuthCancellationReason.AuthorizationError.value == "authorizationError"
        assert OAuthCancellationReason.Cancelled.value == "cancelled"
        assert OAuthCancellationReason.Timeout.value == "timeout"

    def test_repr_redacts_state(self) -> None:
        """state 与 OAuthLaunch.state 为同一 CSRF token，须脱敏。"""
        cancel = OAuthCancellation(
            state="super-secret-csrf-token",
            issuer="https://as.example.com",
            reason=OAuthCancellationReason.AuthorizationError,
        )
        r = repr(cancel)
        assert "[REDACTED]" in r
        assert "super-secret-csrf-token" not in r
        # issuer 不脱敏
        assert "as.example.com" in r


# ============================================================================
# OAuthStatus — 5 variants discriminated union
# ============================================================================


class TestOAuthStatus:
    def test_unauthorized_roundtrip(self) -> None:
        s = _OAuthStatusUnauthorized()
        back = _roundtrip(_STATUS_ADAPTER, s)
        assert back == s
        assert isinstance(back, _OAuthStatusUnauthorized)

    def test_unauthorized_json(self) -> None:
        s = _OAuthStatusUnauthorized()
        data = json.loads(_STATUS_ADAPTER.dump_json(s))
        assert data == {"state": "unauthorized"}

    def test_authorization_pending_roundtrip(self) -> None:
        s = _OAuthStatusAuthorizationPending()
        back = _roundtrip(_STATUS_ADAPTER, s)
        assert back == s
        assert isinstance(back, _OAuthStatusAuthorizationPending)

    def test_authorized_roundtrip(self) -> None:
        s = _OAuthStatusAuthorized(scopes=["files:read", "profile"])
        back = _roundtrip(_STATUS_ADAPTER, s)
        assert back == s
        assert isinstance(back, _OAuthStatusAuthorized)
        assert back.scopes == ["files:read", "profile"]

    def test_authorized_json(self) -> None:
        s = _OAuthStatusAuthorized(scopes=["read", "write"])
        data = json.loads(_STATUS_ADAPTER.dump_json(s))
        assert data == {"state": "authorized", "scopes": ["read", "write"]}

    def test_reauthorization_required_roundtrip(self) -> None:
        s = _OAuthStatusReauthorizationRequired(required_scope="admin:write")
        back = _roundtrip(_STATUS_ADAPTER, s)
        assert back == s
        assert isinstance(back, _OAuthStatusReauthorizationRequired)

    def test_reauthorization_required_json(self) -> None:
        s = _OAuthStatusReauthorizationRequired(required_scope="admin")
        data = json.loads(_STATUS_ADAPTER.dump_json(s))
        assert data == {"state": "reauthorizationRequired", "requiredScope": "admin"}

    def test_error_roundtrip(self) -> None:
        s = _OAuthStatusError(message="token exchange failed")
        back = _roundtrip(_STATUS_ADAPTER, s)
        assert back == s
        assert isinstance(back, _OAuthStatusError)

    def test_error_json(self) -> None:
        s = _OAuthStatusError(message="provider error")
        data = json.loads(_STATUS_ADAPTER.dump_json(s))
        assert data == {"state": "error", "message": "provider error"}

    def test_discriminated_deserialization(self) -> None:
        """标准 discriminator 正确还原各变体。"""
        cases = [
            ('{"state":"unauthorized"}', _OAuthStatusUnauthorized),
            ('{"state":"authorizationPending"}', _OAuthStatusAuthorizationPending),
            ('{"state":"authorized","scopes":["read"]}', _OAuthStatusAuthorized),
            ('{"state":"reauthorizationRequired","requiredScope":"admin"}', _OAuthStatusReauthorizationRequired),
            ('{"state":"error","message":"fail"}', _OAuthStatusError),
        ]
        for json_str, expected_cls in cases:
            result = _STATUS_ADAPTER.validate_json(json_str)
            assert isinstance(result, expected_cls), f"Expected {expected_cls.__name__} for {json_str}"


# ============================================================================
# OAuthFlowOutcome — 2 variants discriminated union
# ============================================================================


class TestOAuthFlowOutcome:
    def test_authorized_roundtrip(self) -> None:
        o = _OAuthOutcomeAuthorized(scopes=["files:read", "profile"])
        back = _roundtrip(_OUTCOME_ADAPTER, o)
        assert back == o
        assert isinstance(back, _OAuthOutcomeAuthorized)
        assert back.scopes == ["files:read", "profile"]

    def test_authorized_json(self) -> None:
        o = _OAuthOutcomeAuthorized(scopes=["read"])
        data = json.loads(_OUTCOME_ADAPTER.dump_json(o))
        assert data == {"outcome": "authorized", "scopes": ["read"]}

    def test_terminated_roundtrip(self) -> None:
        o = _OAuthOutcomeTerminated(
            reason=OAuthCancellationReason.Cancelled,
            status=_OAuthStatusUnauthorized(),
        )
        back = _roundtrip(_OUTCOME_ADAPTER, o)
        assert back == o
        assert isinstance(back, _OAuthOutcomeTerminated)
        assert back.reason == OAuthCancellationReason.Cancelled
        assert isinstance(back.status, _OAuthStatusUnauthorized)

    def test_terminated_json(self) -> None:
        o = _OAuthOutcomeTerminated(
            reason=OAuthCancellationReason.Timeout,
            status=_OAuthStatusError(message="expired"),
        )
        data = json.loads(_OUTCOME_ADAPTER.dump_json(o))
        assert data["outcome"] == "terminated"
        assert data["reason"] == "timeout"
        assert data["status"]["state"] == "error"
        assert data["status"]["message"] == "expired"

    def test_discriminated_deserialization(self) -> None:
        authorized_json = json.dumps({"outcome": "authorized", "scopes": ["read"]})
        result = _OUTCOME_ADAPTER.validate_json(authorized_json)
        assert isinstance(result, _OAuthOutcomeAuthorized)

        terminated_json = json.dumps({
            "outcome": "terminated",
            "reason": "cancelled",
            "status": {"state": "unauthorized"},
        })
        result = _OUTCOME_ADAPTER.validate_json(terminated_json)
        assert isinstance(result, _OAuthOutcomeTerminated)


# ============================================================================
# StreamableHttpServerConfig 集成（oauth 字段可选）
# ============================================================================


class TestStreamableHttpServerConfigOAuth:
    """验证 StreamableHttpServerConfig 正确承载可选 oauth 字段。"""

    def test_without_oauth_backward_compatible(self) -> None:
        """无 oauth 字段的旧配置应正常通过验证。"""
        cfg = StreamableHttpServerConfig(
            name="http-srv",
            server_parameters={"url": "https://mcp.example.com"},
        )
        assert cfg.oauth is None

        # JSON round-trip
        back = StreamableHttpServerConfig.model_validate_json(cfg.model_dump_json())
        assert back == cfg
        assert back.oauth is None

    def test_with_oauth_roundtrip(self) -> None:
        """含 oauth 字段的配置 round-trip 正确。"""
        oauth_opts = OAuthOptions(
            scopes=["files:read"],
            mode=_OAuthModeCCSecret(client_id="svc", client_secret_input="inp-1"),
        )
        cfg = StreamableHttpServerConfig(
            name="http-srv",
            bundle_id="http-srv",
            server_parameters={"url": "https://mcp.example.com"},
            oauth=oauth_opts,
        )
        # round-trip via JSON
        back = StreamableHttpServerConfig.model_validate_json(cfg.model_dump_json())
        assert back == cfg
        assert back.oauth is not None
        assert back.oauth.scopes == ["files:read"]
        assert isinstance(back.oauth.mode, _OAuthModeCCSecret)

    def test_oauth_field_in_json_output(self) -> None:
        """oauth 字段正确出现/不出现在 JSON 输出中。

        BaseMCPServerConfig.model_config 不含 serialize_by_alias，故 model_dump_json
        输出 Python 字段名（非 alias），且 None 值默认包含。验证 oauth null 值的 round-trip 正确性。
        """
        # 不含 oauth
        cfg_no_oauth = StreamableHttpServerConfig(
            name="no-oauth",
            server_parameters={"url": "https://mcp.example.com"},
        )
        data = json.loads(cfg_no_oauth.model_dump_json())
        assert data["oauth"] is None  # None 字段默认包含

        # 含 oauth
        oauth_opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"])
        cfg_with_oauth = StreamableHttpServerConfig(
            name="with-oauth",
            server_parameters={"url": "https://mcp.example.com"},
            oauth=oauth_opts,
        )
        data = json.loads(cfg_with_oauth.model_dump_json())
        assert "oauth" in data
        assert data["oauth"] is not None
        # JSON 中 oauth.mode 里不应出现 Python snake_case 键
        assert "type" in data["oauth"]["mode"]  # camelCase via alias_generator

    def test_validate_from_dict_with_oauth(self) -> None:
        """从含 oauth 的 dict 直接验证（模拟 mcp.json 加载）。"""
        body = {
            "name": "protected-srv",
            "type": "streamable",
            "server_parameters": {"url": "https://protected.example.com"},
            "oauth": {
                "scopes": ["files:read"],
                "mode": {
                    "type": "clientCredentialsSecret",
                    "clientId": "cc-client",
                    "clientSecretInput": "inp-cc",
                },
            },
        }
        cfg = StreamableHttpServerConfig.model_validate(body)
        assert cfg.oauth is not None
        assert isinstance(cfg.oauth.mode, _OAuthModeCCSecret)
        assert cfg.oauth.mode.client_id == "cc-client"


# ============================================================================
# Rust 对照测试向量
# ============================================================================


class TestRustAlignmentVectors:
    """同一组 OAuth 配置在 Python 序列化后与 Rust 已知输出逐字对比。

    向量与 rust-sdk crates/smcp-computer/src/oauth.rs 的测试对齐。
    """

    def test_mode_dynamic_matches_rust(self) -> None:
        """AuthorizationCode + Dynamic → Rust flatten 输出。"""
        mode = _OAuthModeAuthCodeDynamic()
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        # Rust: {"type": "authorizationCode", "registration": "dynamic"}
        assert data == {"type": "authorizationCode", "registration": "dynamic"}

    def test_mode_preregistered_matches_rust(self) -> None:
        mode = _OAuthModeAuthCodePreregistered(client_id="rust-client", client_secret_input="secret-inp")
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        assert data["type"] == "authorizationCode"
        assert data["registration"] == "preregistered"
        assert data["clientId"] == "rust-client"
        assert data["clientSecretInput"] == "secret-inp"

    def test_mode_cc_secret_matches_rust(self) -> None:
        mode = _OAuthModeCCSecret(client_id="m2m-client", client_secret_input="cc-input")
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        assert data == {
            "type": "clientCredentialsSecret",
            "clientId": "m2m-client",
            "clientSecretInput": "cc-input",
        }

    def test_mode_cc_private_key_jwt_matches_rust(self) -> None:
        mode = _OAuthModeCCPrivateKeyJwt(
            client_id="jwt-client",
            private_key_input="pk-input",
            algorithm="RS256",
            token_endpoint_audience="https://aud.example.com",
        )
        data = json.loads(_CLIENT_MODE_ADAPTER.dump_json(mode))
        assert data["type"] == "clientCredentialsPrivateKeyJwt"
        assert data["clientId"] == "jwt-client"
        assert data["privateKeyInput"] == "pk-input"
        assert data["algorithm"] == "RS256"
        assert data["tokenEndpointAudience"] == "https://aud.example.com"

    def test_status_authorized_matches_rust(self) -> None:
        s = _OAuthStatusAuthorized(scopes=["files:read", "profile"])
        data = json.loads(_STATUS_ADAPTER.dump_json(s))
        assert data == {"state": "authorized", "scopes": ["files:read", "profile"]}

    def test_outcome_terminated_matches_rust(self) -> None:
        o = _OAuthOutcomeTerminated(
            reason=OAuthCancellationReason.Cancelled,
            status=_OAuthStatusUnauthorized(),
        )
        data = json.loads(_OUTCOME_ADAPTER.dump_json(o))
        assert data["outcome"] == "terminated"
        assert data["reason"] == "cancelled"
        assert data["status"] == {"state": "unauthorized"}


# ============================================================================
# 错误枚举值验证
# ============================================================================


class TestErrorEnums:
    """验证错误枚举值与 Rust 变体名一致。"""

    def test_protocol_error_values(self) -> None:
        assert OAuthProtocolError.AuthorizationRequired.value == "authorizationRequired"
        assert OAuthProtocolError.TokenExchangeFailed.value == "tokenExchangeFailed"
        assert OAuthProtocolError.PkceUnsupported.value == "pkceUnsupported"
        assert OAuthProtocolError.InsufficientScope.value == "insufficientScope"
        assert OAuthProtocolError.IssuerMismatch.value == "issuerMismatch"
        assert OAuthProtocolError.JwtSigning.value == "jwtSigning"

    def test_oauth_error_code_values(self) -> None:
        assert OAuthErrorCode.NotConfigured.value == "notConfigured"
        assert OAuthErrorCode.StateMismatch.value == "stateMismatch"
        assert OAuthErrorCode.AuthorizationExpired.value == "authorizationExpired"
        assert OAuthErrorCode.MissingSecret.value == "missingSecret"
        assert OAuthErrorCode.UnsupportedSigningAlgorithm.value == "unsupportedSigningAlgorithm"
        assert OAuthErrorCode.InvalidRedirectUri.value == "invalidRedirectUri"
        assert OAuthErrorCode.ConflictingAuthorizationHeader.value == "conflictingAuthorizationHeader"
        assert OAuthErrorCode.Protocol.value == "protocol"


class TestOAuthError:
    """公共 OAuthError 异常（#179 facade 的 typed error 契约）。"""

    def test_code_and_message(self) -> None:
        err = OAuthError(OAuthErrorCode.NotConfigured, "OAuth has not been admitted for this server")
        assert err.code is OAuthErrorCode.NotConfigured
        assert err.message == "OAuth has not been admitted for this server"
        assert isinstance(err, Exception)

    def test_protocol_classmethod(self) -> None:
        err = OAuthError.protocol(OAuthProtocolError.Metadata)
        assert err.code is OAuthErrorCode.Protocol
        assert OAuthProtocolError.Metadata.value in str(err)

    def test_str_contains_code_value(self) -> None:
        err = OAuthError(OAuthErrorCode.StateMismatch, "Callback state does not match pending flow")
        assert OAuthErrorCode.StateMismatch.value in str(err)
        assert "Callback state" in str(err)

    def test_coordinator_error_alias(self) -> None:
        # #178 遗留的内部名在 #179 收敛为公共类型（back-compat alias）
        from a2c_smcp.computer.mcp_clients.oauth_coordinator import _OAuthCoordinatorError

        assert _OAuthCoordinatorError is OAuthError


# ============================================================================
# #181 secret 脱敏：str() / f-string 不得绕过 __repr__（pydantic __str__ 绕过实测）
# ============================================================================


class TestSecretRedactedStr:
    """#181 不变量 4：pydantic v2 的 ``__str__`` 走 ``__repr_str__``、绕过子类
    ``__repr__``——f-string / ``str()`` / ``logging %s`` 曾实测泄露 secret。
    """

    def _assert_redacted(self, obj: object, *secrets: str) -> None:
        rendered = str(obj)
        for secret in secrets:
            assert secret not in rendered, f"secret {secret!r} leaked via str(): {rendered}"
        # 双向断言：脱敏标记存在（不是空串假绿）
        assert "[REDACTED]" in rendered

    def test_launch_str_redacted(self) -> None:
        launch = OAuthLaunch(
            authorization_url="https://as.example/authorize?code=abc&state=xyz",
            state="super-secret-state",
        )
        self._assert_redacted(launch, "https://as.example/authorize", "super-secret-state", "abc")

    def test_callback_str_redacted(self) -> None:
        callback = OAuthCallback(code="auth-code-123", state="state-456", issuer="https://as.example")
        self._assert_redacted(callback, "auth-code-123", "state-456")
        # issuer 非 secret（显式保留，对齐 repr 契约）
        assert "https://as.example" in str(callback)

    def test_cancellation_str_redacted(self) -> None:
        cancellation = OAuthCancellation(
            state="state-789",
            issuer=None,
            reason=OAuthCancellationReason.Cancelled,
        )
        self._assert_redacted(cancellation, "state-789")

    def test_begin_request_str_redacted(self) -> None:
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback", required_scope=None)
        self._assert_redacted(request, "https://host.example/callback")

    def test_fstring_uses_str(self) -> None:
        # 最常见的泄露形态：f-string / logging "%s"
        launch = OAuthLaunch(authorization_url="https://as.example/authorize?code=abc", state="xyz")
        rendered = f"{launch}"
        assert "abc" not in rendered and "xyz" not in rendered
