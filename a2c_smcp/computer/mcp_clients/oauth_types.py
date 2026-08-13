# -*- coding: utf-8 -*-
# filename: oauth_types.py
# @Time    : 2026/08/11
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
OAuth 领域类型定义，逐字段对齐 rust-sdk ``crates/smcp-computer/src/oauth.rs``。

Python 端以 Pydantic 模型承载，camelCase 序列化与 Rust serde ``rename_all = "camelCase"``
逐字一致。本模块仅定义类型，OAuth 流程逻辑由 Sub 2-5 实现（复用 mcp.client.auth）。

协议归属：SDK 层（不涉及 A2C-SMCP 协议变更）。
父 Epic：#176；本 Sub：#177（领域类型 + 配置 schema 基座）。

.. note::
    ``OAuthProtocolError`` / ``OAuthErrorCode`` 为内部错误分类枚举，不进 JSON wire。
    ``OAuthLaunch`` / ``OAuthCallback`` 的 ``__repr__`` 脱敏敏感字段。
    ``extra="forbid"`` 令 Union 反序列化时可精确区分各变体（字段集不同）。
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# ============================================================================
# 基类
# ============================================================================


class _OAuthBaseModel(BaseModel):
    """camelCase 序列化基类（wire 对齐 Rust serde ``rename_all = "camelCase"``），Python 侧 snake_case。

    ``extra="forbid"``：Union 反序列化依赖此标志精确区分变体（字段集唯一性）。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        frozen=True,
        extra="forbid",
    )


# ============================================================================
# OAuthCancellationReason
# ============================================================================


class OAuthCancellationReason(StrEnum):
    """宿主报告的待定浏览器授权终止原因（对齐 Rust）。"""

    AccessDenied = "accessDenied"
    """AS 返回 ``access_denied``。"""
    AuthorizationError = "authorizationError"
    """AS 返回其他 OAuth 错误。"""
    Cancelled = "cancelled"
    """用户或宿主取消流程。"""
    Timeout = "timeout"
    """宿主回调截止时间耗尽。"""


# ============================================================================
# OAuthClientRegistration（3 变体，discriminator "registration"）
# ============================================================================


class _OAuthRegDynamic(_OAuthBaseModel):
    """动态客户端注册（DCR），无需预先提供 client_id。"""

    registration: Literal["dynamic"] = "dynamic"


class _OAuthRegPreregistered(_OAuthBaseModel):
    """预注册客户端，已持有 client_id，可选的 client_secret 经 input-ID 解析。"""

    registration: Literal["preregistered"] = "preregistered"
    client_id: str
    client_secret_input: str | None = None


class _OAuthRegClientMetadataDocument(_OAuthBaseModel):
    """通过客户端元数据文档 URL（CIMD）标识。低频场景。"""

    registration: Literal["clientMetadataDocument"] = "clientMetadataDocument"
    url: str


OAuthClientRegistration = (
    _OAuthRegDynamic
    | _OAuthRegPreregistered
    | _OAuthRegClientMetadataDocument
)
"""授权码客户端注册方式（3 变体，Rust serde ``tag = "registration"``）。

- ``Dynamic`` → ``{"registration": "dynamic"}``
- ``Preregistered`` → ``{"registration": "preregistered", "clientId": …, "clientSecretInput": …}``
- ``ClientMetadataDocument`` → ``{"registration": "clientMetadataDocument", "url": …}``

.. note::
    不含 ``Annotated[..., Field(discriminator=)]``，因 Rust ``#[serde(flatten)]`` 会将
    ``registration`` 字段 inline 到父级 ``OAuthClientMode`` JSON 同层。标准 Pydantic discriminator
    无法处理同 discriminator 值多个外层变体。实际区分由 ``extra="forbid"`` + 字段集唯一性实现。
"""


# ============================================================================
# OAuthClientMode（5 变体，因 Rust flatten 无法使用标准 discriminator）
# ============================================================================


class _OAuthModeAuthCodeDynamic(_OAuthBaseModel):
    """授权码模式 — 动态注册。"""

    type: Literal["authorizationCode"] = "authorizationCode"
    registration: Literal["dynamic"] = "dynamic"


class _OAuthModeAuthCodePreregistered(_OAuthBaseModel):
    """授权码模式 — 预注册（public / confidential）。"""

    type: Literal["authorizationCode"] = "authorizationCode"
    registration: Literal["preregistered"] = "preregistered"
    client_id: str
    client_secret_input: str | None = None


class _OAuthModeAuthCodeCIMD(_OAuthBaseModel):
    """授权码模式 — 客户端元数据文档。"""

    type: Literal["authorizationCode"] = "authorizationCode"
    registration: Literal["clientMetadataDocument"] = "clientMetadataDocument"
    url: str


class _OAuthModeCCSecret(_OAuthBaseModel):
    """客户端凭据模式 — client_secret（basic / post）。"""

    type: Literal["clientCredentialsSecret"] = "clientCredentialsSecret"
    client_id: str
    client_secret_input: str


class _OAuthModeCCPrivateKeyJwt(_OAuthBaseModel):
    """客户端凭据模式 — private_key_jwt（RS256/384/512、ES256/384）。"""

    type: Literal["clientCredentialsPrivateKeyJwt"] = "clientCredentialsPrivateKeyJwt"
    client_id: str
    private_key_input: str
    algorithm: str = "RS256"
    token_endpoint_audience: str | None = None


# 注意：变体顺序决定反序列化从左到右的尝试顺序，不可随意调整。
OAuthClientMode = (
    _OAuthModeAuthCodeDynamic
    | _OAuthModeAuthCodePreregistered
    | _OAuthModeAuthCodeCIMD
    | _OAuthModeCCSecret
    | _OAuthModeCCPrivateKeyJwt
)
"""OAuth 交互 / M2M 流程模式（5 变体，Rust serde ``tag = "type"``）。

Rust ``AuthorizationCode { #[serde(flatten)] registration }`` 将 ``OAuthClientRegistration`` 字段
inline 到父级 JSON 同层 → 3 个授权码子变体 + 2 个客户端凭据变体 = 5 变体。Union **未用标准
discriminator**（3 个授权码变体共享 ``type="authorizationCode"``），区分由 ``extra="forbid"``
+ 各变体字段集唯一性保证。
"""


# ============================================================================
# OAuthOptions
# ============================================================================


class OAuthOptions(_OAuthBaseModel):
    """OAuth 配置选项（对齐 Rust ``HttpServerConfig.oauth``）。"""

    resource: str | None = None
    """RFC 8707 规范资源标识。省略时回退为 Streamable HTTP MCP 端点。"""
    scopes: list[str] = Field(default_factory=list)
    """请求的 OAuth scope 列表。"""
    client_name: str | None = None
    """客户端展示名。"""
    mode: OAuthClientMode
    """OAuth 流程模式（授权码 / 客户端凭据）。"""


# ============================================================================
# OAuthBeginRequest
# ============================================================================


class OAuthBeginRequest(_OAuthBaseModel):
    """宿主发起授权时提供的参数。"""

    redirect_uri: str
    """宿主拥有的回调目标：HTTPS / loopback HTTP / 私有用途 URI（如 ``com.example.app:/oauth/callback``）。"""
    required_scope: str | None = None
    """可选的最小 scope 要求。"""

    def __repr__(self) -> str:
        """脱敏 repr（验收 6）：宿主 callback URI 不得入日志/普通 repr。"""
        return (
            f"OAuthBeginRequest(redirect_uri={_REDACTED!r}, "
            f"required_scope={self.required_scope!r})"
        )


# ============================================================================
# OAuthLaunch（repr 脱敏）
# ============================================================================

_REDACTED = "[REDACTED]"


class OAuthLaunch(_OAuthBaseModel):
    """浏览器启动信息。SDK **绝不自行打开浏览器**。"""

    authorization_url: str
    """授权 URL。"""
    state: str
    """不透明 state 参数（防 CSRF）。"""

    def __repr__(self) -> str:
        """脱敏 repr：避免 authorization_url / state 进入日志。"""
        return f"OAuthLaunch(authorization_url={_REDACTED!r}, state={_REDACTED!r})"


# ============================================================================
# OAuthCallback（repr 脱敏）
# ============================================================================


class OAuthCallback(_OAuthBaseModel):
    """宿主回调监听器解析出的值。"""

    code: str
    """授权码。"""
    state: str
    """回传的 state 参数。"""
    issuer: str | None = None
    """AS issuer（供校验）。"""

    def __repr__(self) -> str:
        """脱敏 repr：避免 code / state 进入日志。"""
        return f"OAuthCallback(code={_REDACTED!r}, state={_REDACTED!r}, issuer={self.issuer!r})"


# ============================================================================
# OAuthCancellation
# ============================================================================


class OAuthCancellation(_OAuthBaseModel):
    """结构化取消输入（对齐 Rust）。"""

    state: str
    """与 ``OAuthLaunch.state`` 严格一致。"""
    issuer: str | None = None
    """AS issuer（供校验）。"""
    reason: OAuthCancellationReason
    """取消原因。"""

    def __repr__(self) -> str:
        """脱敏 repr：state 与 OAuthLaunch.state 为同一 CSRF token，须脱敏。"""
        return (
            f"OAuthCancellation(state={_REDACTED!r}, "
            f"issuer={self.issuer!r}, reason={self.reason!r})"
        )


# ============================================================================
# OAuthStatus（5 变体，discriminator "state"）
# ============================================================================


class _OAuthStatusUnauthorized(_OAuthBaseModel):
    """未授权。"""

    state: Literal["unauthorized"] = "unauthorized"


class _OAuthStatusAuthorizationPending(_OAuthBaseModel):
    """授权进行中。"""

    state: Literal["authorizationPending"] = "authorizationPending"


class _OAuthStatusAuthorized(_OAuthBaseModel):
    """已授权。"""

    state: Literal["authorized"] = "authorized"
    scopes: list[str] = Field(default_factory=list)


class _OAuthStatusReauthorizationRequired(_OAuthBaseModel):
    """需重新授权（scope 升级）。"""

    state: Literal["reauthorizationRequired"] = "reauthorizationRequired"
    required_scope: str


class _OAuthStatusError(_OAuthBaseModel):
    """授权错误。"""

    state: Literal["error"] = "error"
    message: str


_OAuthStatusUnion = (
    _OAuthStatusUnauthorized
    | _OAuthStatusAuthorizationPending
    | _OAuthStatusAuthorized
    | _OAuthStatusReauthorizationRequired
    | _OAuthStatusError
)

OAuthStatus = Annotated[_OAuthStatusUnion, Field(discriminator="state")]
"""可观测授权状态（5 变体，Rust serde ``tag = "state"``）。"""


# ============================================================================
# OAuthFlowOutcome（2 变体，discriminator "outcome"）
# ============================================================================


class _OAuthOutcomeAuthorized(_OAuthBaseModel):
    """授权码已交换、凭据已存储。"""

    outcome: Literal["authorized"] = "authorized"
    scopes: list[str] = Field(default_factory=list)


class _OAuthOutcomeTerminated(_OAuthBaseModel):
    """宿主或 AS 终止流程（未替换凭据）。

    ``status`` 可为 ``Authorized``（scope 升级取消后原凭据仍可用）。
    """

    outcome: Literal["terminated"] = "terminated"
    reason: OAuthCancellationReason
    status: OAuthStatus


OAuthFlowOutcome = Annotated[
    _OAuthOutcomeAuthorized | _OAuthOutcomeTerminated,
    Field(discriminator="outcome"),
]
"""交互式授权流程的结构化结果（2 变体，Rust serde ``tag = "outcome"``）。"""


# ============================================================================
# OAuthProtocolError（内部错误分类，不进 wire）
# ============================================================================


class OAuthProtocolError(StrEnum):
    """OAuth 协议栈失败的稳定、非敏感分类。

    对齐 Rust ``OAuthProtocolError``（``#[non_exhaustive]``）。Provider 响应体 / URL / token /
    client 标识等**刻意丢弃在 mcp 边界**——宿主可用此分类做控制流与诊断，不会意外暴露 AS 数据。
    """

    AuthorizationRequired = "authorizationRequired"
    AuthorizationFailed = "authorizationFailed"
    TokenExchangeFailed = "tokenExchangeFailed"
    TokenRefreshFailed = "tokenRefreshFailed"
    Http = "http"
    Provider = "provider"
    Metadata = "metadata"
    PkceUnsupported = "pkceUnsupported"
    InvalidUrl = "invalidUrl"
    NoAuthorizationSupport = "noAuthorizationSupport"
    Internal = "internal"
    InvalidTokenType = "invalidTokenType"
    TokenExpired = "tokenExpired"
    InvalidScope = "invalidScope"
    RegistrationFailed = "registrationFailed"
    InsufficientScope = "insufficientScope"
    IssuerMismatch = "issuerMismatch"
    ClientCredentials = "clientCredentials"
    JwtSigning = "jwtSigning"
    Other = "other"


# ============================================================================
# OAuthErrorCode（内部错误码，不进 wire；带数据变体由 Sub 2+ 丰富）
# ============================================================================


class OAuthErrorCode(StrEnum):
    """OAuth 错误变体代码（无关联数据，对齐 Rust ``OAuthError`` 变体名）。

    带数据变体（MissingSecret / UnsupportedSigningAlgorithm / InvalidRedirectUri / Protocol）
    的关联数据由 Sub 2-5 的异常类承载；本 Enum 仅声明代码集。
    """

    NotConfigured = "notConfigured"
    UnsupportedTransport = "unsupportedTransport"
    StateMismatch = "stateMismatch"
    IssuerMismatch = "issuerMismatch"
    AuthorizationExpired = "authorizationExpired"
    AuthorizationCancelled = "authorizationCancelled"
    DrainTimeout = "drainTimeout"
    AuthorizationAlreadyPending = "authorizationAlreadyPending"
    InvalidCancellationReason = "invalidCancellationReason"
    MissingSecret = "missingSecret"
    UnsupportedSigningAlgorithm = "unsupportedSigningAlgorithm"
    InvalidRedirectUri = "invalidRedirectUri"
    ConflictingAuthorizationHeader = "conflictingAuthorizationHeader"
    ExplicitPolicyRequiresOptions = "explicitPolicyRequiresOptions"
    DisabledPolicyWithOptions = "disabledPolicyWithOptions"
    Protocol = "protocol"


# ============================================================================
# OAuthError（公共异常，对齐 Rust ``OAuthError`` 枚举）
# ============================================================================


class OAuthError(Exception):
    """OAuth 错误（#179 facade 的 typed error 契约）。

    对齐 Rust ``OAuthError`` 枚举变体（``NotConfigured`` / ``StateMismatch`` /
    ``IssuerMismatch`` / ``AuthorizationExpired`` / ``AuthorizationAlreadyPending`` /
    ``Protocol(OAuthProtocolError)`` 等），变体经 :class:`OAuthErrorCode` 承载。

    **安全约定**：``message`` 必须为静态文案，绝不携带 provider 响应体 / token /
    authorization URL / code / state / ``error_description``（Rust 宿主契约的日志脱敏规则）。
    """

    def __init__(self, code: OAuthErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code: OAuthErrorCode = code
        self.message: str = message

    @classmethod
    def protocol(cls, category: OAuthProtocolError) -> OAuthError:
        """构造 ``Protocol`` 分类错误（对齐 Rust ``OAuthError::Protocol(OAuthProtocolError)``）。

        仅携带稳定、非敏感的分类名，宿主可据此做控制流与诊断。
        """
        return cls(OAuthErrorCode.Protocol, f"OAuth protocol error: {category.value}")


def default_oauth_options() -> OAuthOptions:
    """automatic-only（Rust #180）默认选项：Auth Code + DCR、无预设 scopes。

    无显式 ``oauth`` 配置时由 manager 用于 challenge 准入（scopes / resource 从
    metadata 派生）。私有变体类型（``_OAuthModeAuthCodeDynamic``）不跨界导出。
    """
    return OAuthOptions(mode=_OAuthModeAuthCodeDynamic())


__all__ = [
    "OAuthBeginRequest",
    "OAuthCallback",
    "OAuthCancellation",
    "OAuthCancellationReason",
    "OAuthClientMode",
    "OAuthClientRegistration",
    "OAuthError",
    "OAuthErrorCode",
    "default_oauth_options",
    "OAuthFlowOutcome",
    "OAuthLaunch",
    "OAuthOptions",
    "OAuthProtocolError",
    "OAuthStatus",
]
