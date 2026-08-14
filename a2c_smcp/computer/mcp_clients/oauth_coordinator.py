# -*- coding: utf-8 -*-
# filename: oauth_coordinator.py
# @Time    : 2026/08/11
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
OAuth 流程协调器，逐字段对齐 rust-sdk ``crates/smcp-computer/src/oauth.rs``。

复用 ``mcp.client.auth.OAuthClientProvider`` 处理 OAuth 协议栈（discovery / DCR / PKCE S256 /
authorization / token exchange / refresh），本模块提供 state machine、bounded transaction、
PKCE/CSRF TTL 管理与 TokenStorage→ScopedCredentialStore 桥接。

协议归属：SDK 层（不涉及 A2C-SMCP 协议变更）。
父 Epic：#176；本 Sub：#178（Auth Code + PKCE + DCR 流程）。
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from a2c_smcp.computer.mcp_clients.oauth_credential_store import (
    OAuthCredentialKey,
    OAuthCredentialRecordKind,
    OAuthCredentialStore,
    OAuthCredentialStoreError,
    ScopedCredentialStore,
    oauth_mode_fingerprint,
)
from a2c_smcp.computer.mcp_clients.oauth_security import (
    install_mcp_auth_log_redaction,
    is_loopback_host,
    same_origin,
    validate_authorization_metadata,
    validate_secure_url,
)
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
    OAuthError,
    OAuthErrorCode,
    OAuthFlowOutcome,
    OAuthLaunch,
    OAuthOptions,
    OAuthProtocolError,
    OAuthStatus,
    _OAuthOutcomeAuthorized,
    _OAuthOutcomeTerminated,
    _OAuthStatusAuthorizationPending,
    _OAuthStatusAuthorized,
    _OAuthStatusReauthorizationRequired,
    _OAuthStatusUnauthorized,
)

# ============================================================================
# AuthorizationFlowState
# ============================================================================


class _FlowPhase(Enum):
    IDLE = auto()
    PENDING = auto()
    EXPIRED = auto()


@dataclass
class _PendingAuthorization:
    """Pending authorization flow state (aligns with Rust ``PendingAuthorization``)."""

    launch: OAuthLaunch
    request: OAuthBeginRequest
    requested_scopes: list[str]
    generation: int
    issuer: str
    staged_credentials: str | None = None  # serialized OAuthToken after exchange


@dataclass
class _AuthorizationFlowState:
    """Internal flow state machine (aligns with Rust ``AuthorizationFlowState``)."""

    phase: _FlowPhase = _FlowPhase.IDLE
    pending: _PendingAuthorization | None = None
    expired_state: str | None = None  # PKCE state when phase==EXPIRED (Rust Expired{state})


# ============================================================================
# ExpiringStateStore — PKCE/CSRF state with TTL
# ============================================================================


@dataclass
class _ExpiringAuthorizationState:
    """PKCE/CSRF state entry with expiry (aligns with Rust ``ExpiringAuthorizationState``)."""

    pkce_verifier: str
    issuer: str | None
    redirect_uri: str
    scopes: list[str]
    created_at: float = field(default_factory=time.monotonic)
    claimed: bool = False  # True → claimed for exchange, survives expiry during handoff


class ExpiringStateStore:
    """PKCE/CSRF state store with TTL-based expiry (aligns with Rust ``ExpiringStateStore``).

    10-minute TTL; ``claim_for_exchange`` atomically marks a state as claimed
    to prevent double-use during code exchange.

    Not a general-purpose KV store — only holds authorization state during
    the browser round-trip window.
    """

    def __init__(self, ttl: float = 600.0) -> None:
        self._states: dict[str, _ExpiringAuthorizationState] = {}
        self._ttl = ttl

    # -- helpers ---------------------------------------------------------------

    def _is_expired(self, entry: _ExpiringAuthorizationState) -> bool:
        if entry.claimed:
            return False  # claimed → survives expiry during handoff
        return (time.monotonic() - entry.created_at) > self._ttl

    def expire_stale(self) -> None:
        """Remove expired (non-claimed) entries."""
        stale = [s for s, e in self._states.items() if self._is_expired(e) and not e.claimed]
        for s in stale:
            del self._states[s]

    # -- public API ------------------------------------------------------------

    def store(
        self,
        state: str,
        *,
        pkce_verifier: str,
        issuer: str | None = None,
        redirect_uri: str = "",
        scopes: list[str] | None = None,
    ) -> None:
        """Store a new PKCE/CSRF state."""
        self._states[state] = _ExpiringAuthorizationState(
            pkce_verifier=pkce_verifier,
            issuer=issuer,
            redirect_uri=redirect_uri,
            scopes=scopes or [],
        )

    def claim_for_exchange(self, state: str) -> _ExpiringAuthorizationState | None:
        """Atomically claim a state for code exchange (prevents double-use).

        Returns None if the state does not exist or has expired.
        After successful claim, the state is NOT deleted — the caller must
        call ``release_exchange_claim`` or ``finalize_exchange``.
        """
        entry = self._states.get(state)
        if entry is None:
            return None
        if self._is_expired(entry):
            del self._states[state]
            return None
        if entry.claimed:
            return None  # already claimed by another exchange
        entry.claimed = True
        return entry

    def release_exchange_claim(self, state: str) -> None:
        """Release a claim (caller rejected the callback — e.g. issuer mismatch).

        Restores normal TTL-based expiry so the state can be reclaimed
        by garbage collection.
        """
        entry = self._states.get(state)
        if entry is not None and entry.claimed:
            entry.claimed = False

    def finalize_exchange(self, state: str) -> None:
        """Remove the state after successful exchange."""
        self._states.pop(state, None)

    def lookup(self, state: str) -> _ExpiringAuthorizationState | None:
        """Look up a state without claiming."""
        entry = self._states.get(state)
        if entry is None:
            return None
        if self._is_expired(entry):
            del self._states[state]
            return None
        return entry


# 注册阶段 TTL（🟡3）：宿主注册后从未收到 challenge 的 flow，超时视为过期——
# 否则可无限期残留、以 AuthorizationAlreadyPending 阻塞一切新 flow。与 PKCE/CSRF
# state TTL 同界（10 分钟，Rust AUTHORIZATION_STATE_TTL）。
_REGISTERED_TTL = 600.0


# ============================================================================
# TokenStorageAdapter — bridges mcp.client.auth.TokenStorage → ScopedCredentialStore
# ============================================================================


class TokenStorageAdapter:
    """Adapts ``mcp.client.auth.TokenStorage`` Protocol to ``ScopedCredentialStore``.

    ``OAuthClientProvider`` stores tokens and DCR registration separately
    via this adapter. Both are persisted through the ``ScopedCredentialStore``
    backend using distinct ``OAuthCredentialKey`` entries.

    Scope preservation (aligns with Rust): when a refresh response omits
    ``scope``, the previous scopes are preserved in the stored token.
    """

    def __init__(self, store: ScopedCredentialStore) -> None:
        self._store = store
        self._last_token: OAuthToken | None = None
        self._on_token_saved: Callable[[], None] | None = None
        # #179：load 抑制——新交互式 flow 注册后置 True，provider 视作 tokenless
        # （401 → 完整 auth flow 发布新 URL）；**store 保留旧凭据**（Rust staged
        # 语义的 python 等价：新 flow 未 commit 前旧凭据不动）。仅抑制 load，
        # set_tokens 照常持久化。终态 / 清凭据时复位。
        self._suppress_load: bool = False

    def set_on_token_saved(self, callback: Callable[[], None] | None) -> None:
        """Register a callback invoked after tokens are persisted."""
        self._on_token_saved = callback

    def set_token_load_suppressed(self, suppressed: bool) -> None:
        """抑制/恢复 get_tokens 的 store 读取（#179 新 flow 挑战强制机制）。"""
        self._suppress_load = suppressed

    # -- token keys -----------------------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        """Load OAuthToken from scoped credential store.

        ``try_load_credentials`` returns the raw credential content stored
        by ``save_credentials`` (the token JSON string).
        """
        if self._suppress_load:
            return None
        try:
            raw = await self._store.try_load_credentials()
        except OAuthCredentialStoreError:
            return None
        if raw is None:
            return None
        try:
            token = OAuthToken.model_validate_json(raw)
            self._last_token = token
            return token
        except Exception:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist OAuthToken via scoped credential store.

        Scope preservation: if the new token omits ``scope`` and we have
        a previously stored token with scopes, preserve those scopes.
        """
        # Scope preservation (aligns with Rust ScopedCredentialStore::save)
        if not tokens.scope and self._last_token and self._last_token.scope:
            tokens = tokens.model_copy(update={"scope": self._last_token.scope})

        self._last_token = tokens
        token_json = tokens.model_dump_json(by_alias=True)
        await self._store.save_credentials(token_json)
        # Notify coordinator that token exchange completed
        if self._on_token_saved is not None:
            self._on_token_saved()

    # -- client-info keys ------------------------------------------------------

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Load DCR registration info from scoped credential store."""
        raw = await self._load_client_info_raw()
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(raw)
        except Exception:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Persist DCR registration info via scoped credential store."""
        await self._save_client_info_raw(client_info.model_dump_json(by_alias=True))

    async def _load_client_info_raw(self) -> str | None:
        """Load raw client-info JSON from scoped store."""
        key = self._client_info_key()
        try:
            return await self._store.load_raw(key)
        except OAuthCredentialStoreError:
            return None

    async def _save_client_info_raw(self, value: str) -> None:
        """Save raw client-info JSON via scoped store."""
        key = self._client_info_key()
        await self._store.save_raw(key, value)

    def _client_info_key(self) -> OAuthCredentialKey:
        """Derive a stable key for client-info storage (distinct from token credentials)."""
        return self._store.make_key(OAuthCredentialRecordKind.ClientRegistration)


# ============================================================================
# OAuthCoordinator
# ============================================================================


class OAuthCoordinator:
    """OAuth flow state machine wrapping ``mcp.client.auth.OAuthClientProvider``.

    Aligns with Rust ``OAuthCoordinator``:
    - Bounded transaction: credentials restored in constructor
    - State machine: Unauthorized / AuthorizationPending / Authorized /
      ReauthorizationRequired / Error
    - Generation-gated transitions
    - Host callback contract via redirect/callback handlers

    The ``OAuthClientProvider`` handles protocol mechanics (discovery, DCR,
    PKCE S256, token exchange, refresh). This coordinator provides the
    state machine, PKCE TTL, and host API (begin/complete/cancel/status).

    **Important**: This coordinator does NOT open a browser or bind a port.
    The host provides ``redirect_handler`` and ``callback_handler``.
    """

    def __init__(
        self,
        *,
        bundle_id: str,
        server_url: str,
        resource: str,
        options: OAuthOptions,
        credential_store: OAuthCredentialStore,
        redirect_handler: Callable[[str], asyncio.Future[None]] | None = None,
        callback_handler: Callable[[], asyncio.Future[tuple[str, str | None]]] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._bundle_id = bundle_id
        self._server_url = server_url
        self._resource = resource
        self._options = options
        self._credential_store = credential_store
        self._timeout = timeout

        self._mode_fingerprint = oauth_mode_fingerprint(options)

        # Scoped credential store (from Sub #180)
        self._store = ScopedCredentialStore(
            bundle_id=bundle_id,
            resource=resource,
            mode_fingerprint=self._mode_fingerprint,
            backend=credential_store,
        )
        # TokenStorage adapter for mcp.client.auth
        self._token_storage = TokenStorageAdapter(self._store)

        # State machine
        self._lock = asyncio.Lock()
        self._state_store = ExpiringStateStore(ttl=600.0)  # 10 min TTL
        self._flow: _AuthorizationFlowState = _AuthorizationFlowState()
        self._status: OAuthStatus = _OAuthStatusUnauthorized()
        self._generation: int = 0
        self._granted_scopes: list[str] = []
        self._issuer: str | None = None
        # #179 staged flow：宿主在 challenge 之前注册的请求（无 I/O），redirect_handler
        # 据此发布 pending（而非 URL 反构）。challenge-only 路径（无注册）不得伪造 pending。
        self._registered_request: OAuthBeginRequest | None = None
        # 注册时间戳（🟡3：register-only 阶段 TTL 判定；teardown 清 None）
        self._registered_at: float | None = None

        # Callback bridge (host-provided or set later)
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler

        # OAuthClientProvider (lazily created)
        self._provider: OAuthClientProvider | None = None
        self._provider_needs_rebuild: bool = True

        # Pending callback awaitable (bridges async_auth_flow ↔ host complete/cancel)
        self._callback_future: asyncio.Future[tuple[str, str | None]] | None = None
        self._launch_future: asyncio.Future[OAuthLaunch] | None = None
        # flow 终止/失败信号（cancel / expire / clear / fail_launch 置位）：_aoauth_connect
        # 据此取消挂起的 aconnect（provider 流程死后 mcp 请求侧挂起，#133 实证——
        # 不取消则 connect 任务泄漏、终态后新 flow 的 re-kick 永不触发）。
        self._flow_aborted: asyncio.Event = asyncio.Event()
        # Token exchange completion signal (synchronizes complete() with provider's save_credentials)
        self._token_exchange_done: asyncio.Event | None = None

    # =========================================================================
    # Public API
    # =========================================================================

    async def status(self) -> OAuthStatus:
        """Return current authorization status.

        On ``Authorized``, validates the token is still usable (triggers
        refresh if expired). On ``AuthorizationPending``, expires stale flows.
        """
        async with self._lock:
            self._state_store.expire_stale()
            if isinstance(self._status, _OAuthStatusAuthorizationPending):
                await self._expire_invalid_authorization_flow()
            return self._status

    # ── #179 staged flow：register（无 I/O）vs launch（等 401 触发的 URL） ──

    async def register(self, request: OAuthBeginRequest) -> None:
        """Stage a host authorization flow — validation + dedup/conflict, **no provider I/O**.

        宿主在 Bearer challenge 之前即可调用（``create_oauth_flow`` 语义）。相同请求幂等；
        与已注册 / 在途 pending 不同的请求 → :class:`OAuthError` ``AuthorizationAlreadyPending``
        （Rust ``begin_with_cancellation`` 语义）。实际的 discovery / DCR / PKCE / URL 生成
        由 401 challenge 触发的 mcp inline auth 流程完成，URL 经 :meth:`wait_launch` 交付。

        Raises:
            OAuthError: ``UnsupportedTransport`` / ``InvalidRedirectUri`` /
                ``AuthorizationAlreadyPending``。
        """
        async with self._lock:
            await self._register_under_lock(request)

    async def _register_under_lock(self, request: OAuthBeginRequest) -> None:
        """Core registration logic under ``self._lock``."""
        self._state_store.expire_stale()
        await self._expire_invalid_authorization_flow()

        # Validate mode — only Auth Code supported（automatic-only，#180）
        mode_dict = self._options.mode.model_dump(by_alias=False)
        if mode_dict.get("type") not in ("authorizationCode",):
            raise OAuthError(
                OAuthErrorCode.UnsupportedTransport,
                f"OAuth mode {mode_dict.get('type')} not supported for interactive flow",
            )

        # 宿主 redirect_uri 权威（Rust 宿主契约：redirect URI 不得入持久化配置，运行时提供）
        _validate_redirect_uri(request.redirect_uri)

        # EXPIRED flow 被新注册取代（旧 state 的 late callback 此后按新 flow 判 StateMismatch）
        if self._flow.phase == _FlowPhase.EXPIRED:
            self._flow = _AuthorizationFlowState()

        # 注册过期放行（🟡3）：registered-only 且超时 → 清注册，新请求可开新 flow。
        # （EXPIRED 相已在前面取代；PENDING 相由 PKCE state TTL 另行收敛。）
        if (
            self._registered_request is not None
            and self._flow.phase != _FlowPhase.PENDING
            and self._registered_at is not None
            and (time.monotonic() - self._registered_at) > _REGISTERED_TTL
        ):
            # 过期放行：清注册 + 解除旧 launch 等待者（若有——注册后 launch 在途且
            # 600s 无 challenge 的极端窗口；纵深防御，与 teardown 升格同原则）
            self._registered_request = None
            self._registered_at = None
            fut = self._launch_future
            if fut is not None and not fut.done():
                fut.set_exception(
                    _OAuthCoordinatorError(
                        OAuthErrorCode.AuthorizationExpired,
                        "Authorization flow has expired",
                    )
                )
            self._launch_future = None

        # Dedup / conflict（先在途 pending，后已注册）
        if self._flow.phase == _FlowPhase.PENDING and self._flow.pending is not None:
            if self._flow.pending.request == request:
                return  # 幂等：同一 flow，launch future 已在途
            raise OAuthError(
                OAuthErrorCode.AuthorizationAlreadyPending,
                "A different authorization flow is already pending",
            )
        if self._registered_request is not None:
            if self._registered_request == request:
                return
            raise OAuthError(
                OAuthErrorCode.AuthorizationAlreadyPending,
                "A different authorization flow is already pending",
            )

        # 注册：provider 带宿主 redirect_uri 重建（defer 到 challenge / build 时刻）
        self._registered_request = request
        self._provider_needs_rebuild = True
        # generation 递增：mint 新 pending 的 generation（Rust 在 begin 捕获当前值、
        # python 在注册即 mint——净效果同构：任何更早的在途 flow 视为陈旧）。
        self._generation += 1
        # 注册时间戳（🟡3 注册 TTL：从未被 challenge 的注册可无限期残留阻塞新 flow）
        self._registered_at = time.monotonic()
        # 新 flow 视作 tokenless（store 保留旧凭据）：否则 provider 会用旧 token 直连、
        # 401 永不发生 → launch 拿不到 URL（终态后重授权场景）。上游 mcp/client/auth.py
        # ``async_auth_flow`` 的 inline 全流程只在 401 时触发（无已知上游 issue）。
        self._token_storage.set_token_load_suppressed(True)
        self._flow_aborted.clear()
        self._status = _OAuthStatusAuthorizationPending()

        # Launch/callback futures：**无条件重建**（fresh 注册必经此块——dedup/冲突
        # 已提前 return；不依赖任何终结路径正确置空，杜绝陈旧 future 复用整类问题）
        loop = asyncio.get_running_loop()
        self._launch_future = loop.create_future()
        self._callback_future = loop.create_future()

        # Wire token exchange completion callback
        self._token_storage.set_on_token_saved(self._on_token_saved)

    async def begin(self, request: OAuthBeginRequest) -> OAuthLaunch:
        """Start an interactive authorization flow（#179 起为 compat facade）。

        = :meth:`register` + :meth:`wait_launch`。返回 ``OAuthLaunch``——宿主据其
        URL 打开浏览器；URL 由 401 challenge 触发的 provider 流程生成（transport-coupled，
        Rust 独立 discovery 的 Python 等价面）。

        Raises:
            OAuthError: 与 :meth:`register` 同；challenge-only 路径（无注册请求）时
                redirect_handler 以 ``Protocol(authorizationRequired)`` 快速失败。
        """
        await self.register(request)
        return await self.wait_launch()

    async def wait_launch(self) -> OAuthLaunch:
        """等待 401 challenge 触发的 redirect_handler 发布授权 URL。

        Raises:
            OAuthError: ``Protocol`` —— launch future 未建立（未注册即调用）。
        """
        fut = self._launch_future
        if fut is None:
            raise OAuthError(OAuthErrorCode.Protocol, "Launch future not set")
        return await fut

    def fail_launch(self, error: OAuthError) -> None:
        """Connect 任务失败路径：以异常解除 ``wait_launch()`` 的等待者（幂等，已解则跳过）。

        #179 隔离审查 🔴2：**完整拆解**——仅设异常会留下半拆解槽（registered 保持、
        phase IDLE），宿主以相同 request 重试时 register 幂等早退、wait_launch 永远
        重抛陈旧异常，新 connect 任务的 URL 发布因 future 已 done 被跳过。
        故设异常后走统一 teardown + status 回落（失败 launch 无任何 commit）。

        锁语义：本方法为**同步原子路径**（体内无 await 点），单事件循环下与持锁
        路径天然串行——teardown 的「须持 _lock」在此免锁成立（🔵 审查注释）。
        """
        self._flow_aborted.set()
        fut = self._launch_future
        if fut is not None and not fut.done():
            fut.set_exception(error)
        self._teardown_flow_slot()
        self._status = _OAuthStatusUnauthorized()

    def has_registered_request(self) -> bool:
        """是否已注册宿主 flow（manager 判「challenge 后是否直接跑 OAuth initialize」）。"""
        return self._registered_request is not None

    def current_generation(self) -> int:
        """当前凭据代际（🟡2 handle 绑定：launch 记录、cancel 校验 stale handle）。"""
        return self._generation

    def flow_aborted_event(self) -> asyncio.Event:
        """flow 终止/失败信号（manager._aoauth_connect 竞速用；新注册时 clear）。"""
        return self._flow_aborted

    def launch_awaiting(self) -> bool:
        """是否有注册 flow 仍在等 URL（wait_launch 等待者未解）——connect 任务续派发判据。"""
        return (
            self._registered_request is not None
            and self._launch_future is not None
            and not self._launch_future.done()
        )

    def has_active_flow(self) -> bool:
        """是否有**非终态且未过期** flow（注册未 challenge 或 pending 在途）。

        manager ``create_oauth_flow`` 冲突判定用：终态后（registered 已清 ∧ 非 PENDING）
        新请求**替换**注册表槽（Rust ``!flow.is_terminal()`` 过滤语义），而非冲突报错。
        注册超过 ``_REGISTERED_TTL`` 仍未 challenge 视为过期（🟡3：不得无限阻塞新 flow）。
        """
        if self._flow.phase == _FlowPhase.PENDING:
            return True
        if self._registered_request is None:
            return False
        if self._registered_at is None:
            return True  # pragma: no cover — 防御分支
        return (time.monotonic() - self._registered_at) <= _REGISTERED_TTL

    async def complete(self, callback: OAuthCallback) -> OAuthFlowOutcome:
        """Submit the browser callback and wait for the terminal outcome.

        Validates state + issuer + generation. The provider exchanges the
        code and persists credentials.
        """
        async with self._lock:
            # Handle Expired flow — late callback after PKCE state expiry
            self._reject_expired_flow(callback.state)

            if self._flow.phase != _FlowPhase.PENDING or self._flow.pending is None:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.StateMismatch,
                    "No pending authorization flow",
                )

            pending = self._flow.pending

            # Validate generation
            if callback.state != pending.launch.state:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.StateMismatch,
                    "Callback state does not match pending flow",
                )

            # Validate generation（验收 4；Rust complete_with_cancellation）——pending 期间
            # 若发生凭据 save/refresh（_on_token_saved 递增 generation），本 flow 已陈旧：
            # 收敛 EXPIRED 后报 AuthorizationExpired（镜像 Rust oauth.rs:2559-2572）。
            if pending.generation != self._generation:
                await self._expire_invalid_authorization_flow()
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.AuthorizationExpired,
                    "Authorization flow has expired",
                )

            # Validate issuer (if provided)——两侧规范化比较（AnyHttpUrl 尾部斜杠 vs 裸 issuer）
            normalized_cb_issuer = _normalize_issuer(callback.issuer)
            if normalized_cb_issuer is not None and pending.issuer != normalized_cb_issuer:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.IssuerMismatch,
                    "Callback issuer does not match pending flow",
                )

            cb_future = self._callback_future
            if cb_future is None:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.StateMismatch,
                    "No callback future — flow may have expired",
                )

        # Claim PKCE state for exchange (prevents double-use).
        claimed = self._state_store.claim_for_exchange(callback.state)
        if claimed is None:
            raise _OAuthCoordinatorError(
                OAuthErrorCode.AuthorizationExpired,
                "PKCE state expired or already claimed",
            )

        # Release lock before waiting for provider to complete exchange.
        # Signal the callback handler to return (code, state) to the provider,
        # then wait for the provider to finish token exchange + persistence.
        exchange_done = asyncio.Event()
        self._token_exchange_done = exchange_done
        cb_future.set_result((callback.code, callback.state))

        # Wait for token exchange to complete (with 5-minute timeout).
        # exchange_done.wait() yields to the event loop, letting the provider's
        # async_auth_flow run token exchange. set_tokens() fires _on_token_saved
        # → exchange_done.set() when the exchange completes.
        try:
            await asyncio.wait_for(exchange_done.wait(), timeout=300.0)
        except TimeoutError as exc:
            self._state_store.release_exchange_claim(callback.state)
            raise _OAuthCoordinatorError(
                OAuthErrorCode.Protocol,
                "Token exchange timed out",
            ) from exc

        async with self._lock:
            # 终态：恢复 store 读取（交换已由 set_tokens 落盘；抑制仅针对 flow 期间）
            self._token_storage.set_token_load_suppressed(False)
            outcome: OAuthFlowOutcome
            token = await self._token_storage.get_tokens()
            if token is not None:
                self._state_store.finalize_exchange(callback.state)
                self._status = _OAuthStatusAuthorized(scopes=list(self._granted_scopes))
                self._flow = _AuthorizationFlowState()
                outcome = _OAuthOutcomeAuthorized(scopes=list(self._granted_scopes))
            else:
                self._state_store.release_exchange_claim(callback.state)
                self._flow_aborted.set()
                outcome = _OAuthOutcomeTerminated(
                    reason=OAuthCancellationReason.AuthorizationError,
                    status=self._status,
                )
            self._token_exchange_done = None
            self._teardown_flow_slot()
            return outcome

    async def cancel(self, cancellation: OAuthCancellation) -> OAuthFlowOutcome:
        """Cancel a pending authorization flow.

        Only ``Cancelled`` or ``Timeout`` reasons are accepted for host-initiated
        cancellation. Provider errors must use ``cancel_callback``.
        """
        if cancellation.reason not in (
            OAuthCancellationReason.Cancelled,
            OAuthCancellationReason.Timeout,
        ):
            raise _OAuthCoordinatorError(
                OAuthErrorCode.InvalidCancellationReason,
                f"Host cancellation only accepts Cancelled/Timeout, got {cancellation.reason}",
            )

        async with self._lock:
            # Handle Expired flow — late cancellation after expiry
            self._reject_expired_flow(cancellation.state)

            if self._flow.phase != _FlowPhase.PENDING or self._flow.pending is None:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.StateMismatch,
                    "No pending authorization flow to cancel",
                )

            pending = self._flow.pending
            if cancellation.state != pending.launch.state:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.StateMismatch,
                    "Cancellation state does not match pending flow",
                )

            # 校验 generation（与 complete 同判据；pending 期间凭据 save/refresh 后陈旧）
            if pending.generation != self._generation:
                await self._expire_invalid_authorization_flow()
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.AuthorizationExpired,
                    "Authorization flow has expired",
                )

            # #179：宿主路径同样校验 issuer（与 cancel_callback 对齐，Rust validate_callback_issuer）；
            # 两侧规范化比较（AnyHttpUrl 尾部斜杠 vs 裸 issuer）
            normalized_cancel_issuer = _normalize_issuer(cancellation.issuer)
            if normalized_cancel_issuer is not None and pending.issuer != normalized_cancel_issuer:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.IssuerMismatch,
                    "Cancellation issuer does not match pending flow",
                )

            return await self._terminate_pending_flow(
                cancellation.state, cancellation.reason
            )

    async def cancel_pending(
        self,
        reason: OAuthCancellationReason,
        expected_generation: int | None = None,
        expected_request: OAuthBeginRequest | None = None,
    ) -> OAuthFlowOutcome:
        """Handle 级宿主取消：状态由 SDK 从 pending flow 内部解析（Rust ``OAuthFlow::cancel``）。

        仅 ``Cancelled`` / ``Timeout``。launch 前（已注册未 challenge）→ 清注册即终态
        （Rust flow.cancel before launch 语义）；已终态（无 pending 无注册）→ ``StateMismatch``。

        #179 隔离审查 🟡2 stale handle 绑定：``expected_generation`` / ``expected_request``
        由 handle 携带——PENDING 相校验 pending 代际、registered-only 相校验代际（或
        请求）——旧 flow 的 handle 不得取消新 flow（Rust handle 绑定自身状态）。
        """
        if reason not in (
            OAuthCancellationReason.Cancelled,
            OAuthCancellationReason.Timeout,
        ):
            raise _OAuthCoordinatorError(
                OAuthErrorCode.InvalidCancellationReason,
                f"Host cancellation only accepts Cancelled/Timeout, got {reason}",
            )

        async with self._lock:
            if self._flow.phase == _FlowPhase.PENDING and self._flow.pending is not None:
                pending = self._flow.pending
                if (
                    expected_generation is not None
                    and pending.generation != expected_generation
                ):
                    raise _OAuthCoordinatorError(
                        OAuthErrorCode.StateMismatch,
                        "Authorization flow handle is stale",
                    )
                state = pending.launch.state
                return await self._terminate_pending_flow(state, reason)

            if self._registered_request is not None:
                # stale handle 守卫（registered-only 相）
                if expected_generation is not None:
                    if self._generation != expected_generation:
                        raise _OAuthCoordinatorError(
                            OAuthErrorCode.StateMismatch,
                            "Authorization flow handle is stale",
                        )
                elif (
                    expected_request is not None
                    and self._registered_request != expected_request
                ):
                    raise _OAuthCoordinatorError(
                        OAuthErrorCode.StateMismatch,
                        "Authorization flow handle is stale",
                    )
                # pre-challenge cancel：注册即终止，无任何 I/O 曾发生。status 从 store
                # 恢复（旧凭据仍可用 → Authorized）；wait_launch 等待者由 teardown
                # 统一以 AuthorizationCancelled 解除。
                self._flow_aborted.set()
                self._token_storage.set_token_load_suppressed(False)
                status = await self._restore_status_from_store()
                self._status = status
                self._teardown_flow_slot()
                return _OAuthOutcomeTerminated(reason=reason, status=status)
            raise _OAuthCoordinatorError(
                OAuthErrorCode.StateMismatch,
                "No pending authorization flow to cancel",
            )

    async def cancel_callback(self, cancellation: OAuthCancellation) -> OAuthFlowOutcome:
        """Submit a provider OAuth error callback (AccessDenied / AuthorizationError).

        Validates state and issuer before cancelling.
        """
        if cancellation.reason not in (
            OAuthCancellationReason.AccessDenied,
            OAuthCancellationReason.AuthorizationError,
        ):
            raise _OAuthCoordinatorError(
                OAuthErrorCode.InvalidCancellationReason,
                f"Provider callback only accepts AccessDenied/AuthorizationError, got {cancellation.reason}",
            )

        async with self._lock:
            # Handle Expired flow — late provider error after expiry
            self._reject_expired_flow(cancellation.state)

            if self._flow.phase != _FlowPhase.PENDING or self._flow.pending is None:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.StateMismatch,
                    "No pending authorization flow",
                )

            pending = self._flow.pending
            if cancellation.state != pending.launch.state:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.StateMismatch,
                    "Cancellation state does not match pending flow",
                )

            # 校验 generation（与 complete/cancel 同判据）
            if pending.generation != self._generation:
                await self._expire_invalid_authorization_flow()
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.AuthorizationExpired,
                    "Authorization flow has expired",
                )

            normalized_cancel_issuer = _normalize_issuer(cancellation.issuer)
            if normalized_cancel_issuer is not None and pending.issuer != normalized_cancel_issuer:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.IssuerMismatch,
                    "Cancellation issuer does not match pending flow",
                )

            return await self._terminate_pending_flow(
                cancellation.state, cancellation.reason
            )

    async def _terminate_pending_flow(
        self, state: str, reason: OAuthCancellationReason
    ) -> OAuthFlowOutcome:
        """锁内终结 pending flow（状态已校验匹配），返回 Terminated。

        #179：status 取**终结后**真实状态（Rust ``restore_status_after_termination``）——
        scope-upgrade flow 取消后旧凭据仍可用 → ``Authorized``，而非一律 ``Unauthorized``。
        """
        # Clean up PKCE state
        self._state_store.finalize_exchange(state)
        self._flow = _AuthorizationFlowState()
        self._flow_aborted.set()

        # Cancel the pending callback（teardown 随后统一置空双 futures）
        cb = self._callback_future
        if cb and not cb.done():
            cb.cancel()

        # 终态：恢复 store 读取后探测（Rust restore_status_after_termination——
        # 取消后旧凭据仍可用 → Authorized）
        self._token_storage.set_token_load_suppressed(False)
        status = await self._restore_status_from_store()
        self._status = status

        self._teardown_flow_slot()
        return _OAuthOutcomeTerminated(reason=reason, status=status)

    async def _restore_status_from_store(self) -> OAuthStatus:
        """从 store 探测终结后状态（Rust ``restore_status_after_termination``；须持 ``_lock``）。

        scope-upgrade 取消 / pre-challenge 取消后旧凭据仍可用 → ``Authorized``。
        """
        token = await self._token_storage.get_tokens()
        if token is not None:
            if token.scope:
                self._granted_scopes = [
                    s.strip() for s in token.scope.split(" ") if s.strip()
                ]
            return _OAuthStatusAuthorized(scopes=list(self._granted_scopes))
        return _OAuthStatusUnauthorized()

    def _teardown_flow_slot(self) -> None:
        """终态收敛（complete/cancel/expire/fail_launch/clear 出口，须持 ``_lock``）。

        ① 对未决 ``_launch_future`` 以 ``AuthorizationCancelled`` 解除（任何终结路径都
        不得遗留挂起的 ``wait_launch`` 等待者——否则外层取消 / 过期 / clear 后宿主
        launch 无上界挂起）；② 清 registered + 双 futures，使后续相同请求的
        ``create_oauth_flow`` 可开新 flow（#179 隔离审查 🔴1/🔴2 的根因收敛点）。
        """
        fut = self._launch_future
        if fut is not None and not fut.done():
            fut.set_exception(
                _OAuthCoordinatorError(
                    OAuthErrorCode.AuthorizationCancelled,
                    "Authorization was cancelled",
                )
            )
        self._registered_request = None
        self._registered_at = None
        self._launch_future = None
        self._callback_future = None

    # =========================================================================
    # Bounded transaction: restore credentials
    # =========================================================================

    async def restore_credentials(self) -> OAuthStatus:
        """Attempt to restore previously stored credentials.

        Returns the restored status (Authorized if credentials exist and
        are valid, Unauthorized otherwise).

        This is the pre-connect step of the bounded transaction:
        1. Load credentials from store
        2. Load DCR registration from store
        3. If both exist, build provider with restored state
        4. Validate the token (triggers refresh if expired)
        """
        async with self._lock:
            # #179：先采纳持久化 issuer（否则 index.active.issuer != self._issuer，
            # 非 None issuer 的凭据永远恢复失败——#178 缺口）
            adopted = await self._store.adopt_persisted_issuer()
            if adopted is not None:
                self._issuer = adopted
            try:
                token = await self._token_storage.get_tokens()
                client_info = await self._token_storage.get_client_info()
            except Exception:
                token = None
                client_info = None

            if token is None or client_info is None:
                if self.has_active_flow():
                    # flow 在途（registered / PENDING）：不覆盖其状态——Pending 语义须
                    # 保留（否则 status() 的 expire 判据失效，僵尸注册无法收敛）。
                    return self._status
                self._status = _OAuthStatusUnauthorized()
                return self._status

            # Restore granted scopes from token
            if token.scope:
                self._granted_scopes = [s.strip() for s in token.scope.split(" ") if s.strip()]

            # Build provider with restored state (OAuthClientProvider._initialize
            # will load tokens + client_info from storage)
            self._provider_needs_rebuild = True
            self._status = _OAuthStatusAuthorized(scopes=list(self._granted_scopes))
            return self._status

    # =========================================================================
    # Bearer challenge detection
    # =========================================================================

    def needs_oauth_provider(self) -> bool:
        """Return True if an OAuthClientProvider should be injected into the HTTP client.

        True when credentials are restored and the provider can handle auth,
        **or** a host flow is registered（#179 交互式路径：401 须触发完整 auth flow
        发布 URL，而非裸 401 拆连接）。
        """
        return (
            self._provider is not None
            or self._status.state == "authorized"
            or self._registered_request is not None
        )

    def build_oauth_provider(self) -> OAuthClientProvider:
        """Build (or rebuild) the ``OAuthClientProvider`` for httpx injection."""
        self._rebuild_provider_if_needed()
        assert self._provider is not None, "OAuthClientProvider not built"
        return self._provider

    def _rebuild_provider_if_needed(self) -> None:
        """Rebuild OAuthClientProvider if configuration or state changed.

        #179：redirect_uri 权威来源 = 宿主 ``OAuthBeginRequest.redirect_uri``
        （Rust 宿主契约：运行时提供、不得入持久化配置）。无注册请求时用占位
        loopback（仅合法 URI；challenge-only 路径实际不可达——redirect_handler
        在无注册时快速失败，见 :meth:`_make_redirect_handler`）。
        """
        if not self._provider_needs_rebuild and self._provider is not None:
            return

        redirect_uri_value = (
            self._registered_request.redirect_uri
            if self._registered_request is not None
            else "http://127.0.0.1:0/callback"
        )
        redirect_uri = AnyUrl(redirect_uri_value)
        client_metadata = OAuthClientMetadata(
            redirect_uris=[redirect_uri],
            client_name=self._options.client_name or "A2C Computer",
            scope=" ".join(self._options.scopes) if self._options.scopes else None,
        )

        self._provider = OAuthClientProvider(
            server_url=self._server_url,
            client_metadata=client_metadata,
            storage=self._token_storage,
            redirect_handler=self._make_redirect_handler(),
            callback_handler=self._make_callback_handler(),
            timeout=self._timeout,
        )
        self._provider_needs_rebuild = False
        # #181：provider 存在 = OAuth 启用 → mcp.client.auth 日志脱敏（幂等）。
        # mcp 的 logger.exception 携带 pydantic ValidationError 的 input_value（可能含
        # token / code），Rust 以 SensitiveAuthClient 的 NoSubscriber 压制；python 等价物
        # 为日志 Filter（见 oauth_security.install_mcp_auth_log_redaction）。
        install_mcp_auth_log_redaction()

    # =========================================================================
    # Redirect / callback handler factories
    # =========================================================================

    def _make_redirect_handler(self) -> Any:
        """Create the redirect_handler for OAuthClientProvider.

        Called by the provider when the authorization URL is ready.
        Stores the launch info and resolves _launch_future.

        #179 契约：仅当宿主已注册 flow（:meth:`register`）时发布 pending——
        pending 携带**宿主 request 对象**（非 URL 反构，幂等/冲突判定依据）；
        challenge-only 路径（无注册）抛 ``Protocol(authorizationRequired)``
        让 mcp inline auth 流程快速失败，不得伪造 pending。
        """
        coordinator = self

        async def redirect_handler(url: str) -> None:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            state = params.get("state", [None])[0] or ""
            redirect_uri_from_url = params.get("redirect_uri", [""])[0] or ""

            launch = OAuthLaunch(authorization_url=url, state=state)

            # 捕获 provider 发现的 AS issuer（mcp 公共属性，防御性读取）。
            # 注意：issuer 是 AnyHttpUrl（非 str，自带尾部斜杠）→ str + 规范化。
            issuer: str | None = None
            provider = coordinator._provider
            if provider is not None:
                oauth_metadata = getattr(provider.context, "oauth_metadata", None)
                candidate = getattr(oauth_metadata, "issuer", None) if oauth_metadata is not None else None
                if candidate is not None:
                    issuer = _normalize_issuer(str(candidate))
                # #181：authorization 前校验 mcp discovery 产物（HTTPS-only 端点 / PKCE
                # S256 / PRM resource 同源）——失败置 aborted + 抛 Protocol，provider
                # 流程死亡、manager 竞速通道收敛（#133 挂起模式防御）
                coordinator._validate_discovered_metadata()

            async with coordinator._lock:
                # 锁内复核注册（竞态：检查与持锁之间 flow 可能已被 cancel/complete 终态清除）
                if coordinator._registered_request is None:
                    # challenge-only 路径（无注册 flow）：provider 全流程将随此抛错死亡、
                    # 请求侧挂起（#133）——置 aborted 供 manager 的 authorized 竞速取消之
                    coordinator._flow_aborted.set()
                    raise OAuthError.protocol(OAuthProtocolError.AuthorizationRequired)
                if issuer is not None:
                    coordinator._issuer = issuer
                    await coordinator._store.set_issuer(issuer)
                # Persist PKCE state for TTL + double-use prevention
                # (pkce_verifier is managed internally by OAuthClientProvider)
                coordinator._state_store.store(
                    state,
                    pkce_verifier="",  # provider-managed
                    issuer=issuer,
                    redirect_uri=redirect_uri_from_url,
                    scopes=list(coordinator._options.scopes),
                )
                coordinator._flow = _AuthorizationFlowState(
                    phase=_FlowPhase.PENDING,
                    pending=_PendingAuthorization(
                        launch=launch,
                        request=coordinator._registered_request,
                        requested_scopes=list(coordinator._options.scopes),
                        generation=coordinator._generation,
                        issuer=issuer or "",
                    ),
                )
                coordinator._status = _OAuthStatusAuthorizationPending()

                # Resolve launch future
                if coordinator._launch_future and not coordinator._launch_future.done():
                    coordinator._launch_future.set_result(launch)

        return redirect_handler

    def _validate_discovered_metadata(self) -> None:
        """#181：authorization 前校验 mcp discovery 产物（Rust ``validate_authorization_metadata``
        + ``observe_admitted_resource_metadata`` 的 python 面）。

        mcp 不校验 AS metadata 端点的 HTTPS-only / PKCE S256 支持，PRM document 的
        ``resource`` 字段也不与 server_url 复核——本方法兜底（``redirect_handler`` 是
        flow 必经点，discovery 已全部完成）。失败置 aborted（manager 竞速通道）+
        抛 ``Protocol`` 分类错误（静态 message，不携带 URL / metadata 本体）。
        """
        provider = self._provider
        if provider is None:  # pragma: no cover — 防御分支（仅 redirect_handler 调用）
            return
        context = provider.context
        try:
            # protected resource 端点 HTTPS-only（纵深：manager 准入已校验，外部构造
            # coordinator 的路径亦被覆盖）
            validate_secure_url(self._server_url)
            # PRM resource 复核：mcp 自行从 401 challenge 提取 metadata URL 并请求，
            # manager 准入的 same-origin 校验不直接约束 mcp 的第二次请求——按 PRM
            # document 的 resource 字段与 server_url 复核（Rust admitted resource 匹配）
            prm = context.protected_resource_metadata
            if prm is not None and prm.resource is not None:
                resource = str(prm.resource)
                if not same_origin(resource, self._server_url):
                    raise OAuthError.protocol(OAuthProtocolError.Metadata)
                validate_secure_url(resource)
            metadata = context.oauth_metadata
            if metadata is None:
                # mcp 在 discovery 全失败时 fallback {base}/authorize；Rust 自动路径
                # 无此分支（admission 已证明 challenge 携带 resource_metadata，PRM
                # discovery 必须产出 metadata）——按 Rust 语义拒绝
                raise OAuthError.protocol(OAuthProtocolError.Metadata)
            # require_pkce 恒 True：_register_under_lock 已 gate 仅 authorizationCode
            # 模式（#180 automatic-only）
            validate_authorization_metadata(metadata, require_pkce=True)
        except OAuthError as exc:
            # #181 隔离审查 🔴1：校验失败必须**当场收敛** launch 等待者——fail_launch
            # 为同步原子路径（体内无 await，单事件循环下与持锁路径天然串行，免锁
            # 成立，见其 docstring），置 aborted + 以 typed error 解 wait_launch +
            # 完整拆解 flow slot。若仅置 aborted 等 manager 分支收敛，manager 的
            # aborted 分支只写连接状态 ERROR、不触 fail_launch → wait_launch 永久
            # 挂起（#133「授权失败 MUST NOT 表现为挂起」）。
            self.fail_launch(exc)
            raise

    def _make_callback_handler(self) -> Any:
        """Create the callback_handler for OAuthClientProvider.

        Called by the provider to wait for the host to submit the browser
        callback. Blocks until complete() or cancel() is called.
        """
        coordinator = self

        async def callback_handler() -> tuple[str, str | None]:
            if coordinator._callback_future is None:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.StateMismatch,
                    "No pending callback future",
                )
            try:
                code, state = await coordinator._callback_future
                return code, state
            except asyncio.CancelledError:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.AuthorizationCancelled,
                    "Authorization was cancelled",
                ) from None

        return callback_handler

    # =========================================================================
    # Insufficient scope step-up (aligns with Rust handle_insufficient_scope)
    # =========================================================================

    async def handle_insufficient_scope(self, required_scope: str) -> None:
        """Handle a 403 insufficient_scope response from the protected resource.

        Sets status to ``ReauthorizationRequired`` for interactive flows.
        The host should initiate a new authorization flow with the required scope.

        For Auth Code flows (this Sub #178): the token stays provisional until
        a subsequent MCP request succeeds (confirmed by observe_service_success).

        Caller must hold ``_lock``.
        """
        # Expire stale flows before proceeding (aligns with Rust handle_insufficient_scope)
        await self._expire_invalid_authorization_flow()
        # If a pending flow is still active, don't override its status
        if self._flow.phase == _FlowPhase.PENDING:
            return
        self._set_status(_OAuthStatusReauthorizationRequired(required_scope=required_scope))

    def _set_status(self, status: OAuthStatus) -> None:
        """Set status without acquiring lock (caller holds lock)."""
        self._status = status

    async def observe_service_success(self) -> None:
        """Observe a successful MCP request — confirms provisional token.

        If in ``ReauthorizationRequired`` state, transitions to ``Authorized``.
        Aligns with Rust ``observe_service_success``.
        """
        async with self._lock:
            if isinstance(self._status, _OAuthStatusReauthorizationRequired):
                self._status = _OAuthStatusAuthorized(scopes=list(self._granted_scopes))

    async def observe_service_error(
        self, status_code: int, www_authenticate: str | None
    ) -> bool:
        """Observe a transport error from an MCP request.

        Returns True if the coordinator handled the error (e.g. insufficient_scope).
        Aligns with Rust ``observe_streamable_error`` / ``observe_service_error``.

        Generation-gated: stale observations (from before the current generation)
        are silently ignored.
        """
        async with self._lock:
            # Parse insufficient_scope from WWW-Authenticate
            if status_code == 403 and www_authenticate:
                required_scope = _parse_insufficient_scope(www_authenticate)
                if required_scope:
                    await self.handle_insufficient_scope(required_scope)
                    return True

            # 401: invalidate credentials
            if status_code == 401:
                self._status = _OAuthStatusUnauthorized()
                return True

        return False

    def _on_token_saved(self) -> None:
        """Callback invoked by TokenStorageAdapter after tokens are persisted.

        Signals the token exchange completion event (if any) to unblock
        ``complete()`` waiting for the provider's ``set_tokens`` call.

        #179：同时递增 generation（对齐 Rust save/commit bump）——exchange 完成的
        那一刻起旧 pending 即视为陈旧，``_expire_invalid_authorization_flow`` 会
        立即从 store 恢复真实状态（Authorized），而非挂到 TTL 才收敛。
        """
        self._generation += 1
        if self._token_exchange_done is not None:
            self._token_exchange_done.set()

    async def invalidate_credentials(self) -> None:
        """Clear stored credentials (generation-gated)."""
        async with self._lock:
            self._token_storage.set_token_load_suppressed(False)
            self._generation += 1
            self._status = _OAuthStatusUnauthorized()
            self._provider_needs_rebuild = True
            self._granted_scopes.clear()
            try:
                await self._store.clear()
            except OAuthCredentialStoreError:
                pass

    async def clear(self) -> None:
        """清除整个 OAuth slot 的运行时态（#179 ``clear_oauth`` 用）：

        pending flow 终结（cancel 在途 callback → 交互式 connect 任务随之结束）+
        注册/ futures 清空 + 凭据槽清空（:meth:`invalidate_credentials`）。
        """
        async with self._lock:
            if self._flow.phase == _FlowPhase.PENDING and self._flow.pending is not None:
                state = self._flow.pending.launch.state
                self._state_store.finalize_exchange(state)
                self._flow = _AuthorizationFlowState()
                cb = self._callback_future
                if cb and not cb.done():
                    cb.cancel()
            else:
                self._flow = _AuthorizationFlowState()
            self._flow_aborted.set()
            self._teardown_flow_slot()
        await self.invalidate_credentials()

    def _reject_expired_flow(self, state: str) -> None:
        """Classify a late callback against an EXPIRED flow.

        Must be called under ``_lock``. No-op when the flow is not EXPIRED.

        Raises:
            _OAuthCoordinatorError: ``StateMismatch`` when the state doesn't
                match the expired flow's retained state (the flow stays
                EXPIRED for a later matching callback); ``AuthorizationExpired``
                when it matches (the flow is cleared back to Idle).
        """
        if self._flow.phase != _FlowPhase.EXPIRED:
            return
        if self._flow.expired_state != state:
            raise _OAuthCoordinatorError(
                OAuthErrorCode.StateMismatch,
                "State does not match expired flow",
            )
        self._flow = _AuthorizationFlowState()  # clear to Idle
        self._teardown_flow_slot()  # 同 🔴1：过期终态不留陈旧 launch future
        raise _OAuthCoordinatorError(
            OAuthErrorCode.AuthorizationExpired,
            "Authorization flow has expired",
        )

    async def _expire_invalid_authorization_flow(self) -> bool:
        """Expire the pending authorization flow if the PKCE state or generation is stale.

        Aligns with Rust ``expire_invalid_authorization_flow`` (oauth.rs:2418).

        Must be called under ``_lock``. Returns ``True`` if the flow was expired,
        ``False`` if no-op (no pending flow, or flow still valid).

        On expiry, mirrors Rust ``restore_status_after_termination``: probes the
        credential store and restores ``Authorized`` when still-valid credentials
        exist (e.g. a scope-upgrade flow expiring while the previous grant remains
        usable), ``Unauthorized`` otherwise.
        """
        if self._flow.phase != _FlowPhase.PENDING or self._flow.pending is None:
            return False

        pending = self._flow.pending
        state = pending.launch.state

        # Valid if generation matches AND PKCE state still in store
        state_is_valid = (
            pending.generation == self._generation
            and self._state_store.lookup(state) is not None
        )
        if state_is_valid:
            return False

        # Flow is stale — remove PKCE state, transition to EXPIRED
        self._state_store.finalize_exchange(state)
        self._flow = _AuthorizationFlowState(
            phase=_FlowPhase.EXPIRED, expired_state=state
        )
        # #179：取消在途 callback future —— provider 的 callback 等待随之中断，
        # 交互式 connect 任务（detached）得以结束而非泄漏到 TTL 之外。
        cb = self._callback_future
        if cb and not cb.done():
            cb.cancel()
        self._callback_future = None
        self._flow_aborted.set()
        # 收敛（#179 隔离审查 🔴1）：一并清 registered + 双 futures——尤其
        # _launch_future 若不置空，过期后新注册的 wait_launch 会立即返回**旧 flow**
        # 的授权 URL（陈旧 URL 的 callback 永远到不了新 flow）。
        self._teardown_flow_slot()
        # Restore status from the credential store (aligns with Rust
        # restore_status_after_termination → restore_authorization_status)。
        # 先复位 load 抑制：register 曾置 True（新 flow 视作 tokenless），expiry 探测
        # 须读旧凭据——否则恒判 Unauthorized，且 suppress 残留致后续 restore_credentials
        # 永远读不到旧凭据（#179 隔离复核 🟡b）。
        self._token_storage.set_token_load_suppressed(False)
        token = await self._token_storage.get_tokens()
        if token is not None:
            if token.scope:
                self._granted_scopes = [
                    s.strip() for s in token.scope.split(" ") if s.strip()
                ]
            self._status = _OAuthStatusAuthorized(scopes=list(self._granted_scopes))
        else:
            self._status = _OAuthStatusUnauthorized()
        return True


# ============================================================================
# Bearer challenge parsing
# ============================================================================


def _parse_insufficient_scope(www_authenticate: str | None) -> str | None:
    """Parse ``WWW-Authenticate: Bearer error="insufficient_scope" scope="..."``.

    Returns the required scope string, or None if not an insufficient_scope challenge.
    Aligns with Rust ``bearer_insufficient_scope``.
    """
    if www_authenticate is None:
        return None
    if not www_authenticate:
        return None

    # Format: Bearer error="insufficient_scope", scope="read write"
    # Match: Bearer ... error="insufficient_scope" ... scope="<value>"
    match = re.search(
        r'Bearer\s+.*?error\s*=\s*"insufficient_scope".*?scope\s*=\s*"([^"]+)"',
        www_authenticate,
        re.IGNORECASE,
    )
    if not match:
        # Try scope before error
        match = re.search(
            r'Bearer\s+.*?scope\s*=\s*"([^"]+)".*?error\s*=\s*"insufficient_scope"',
            www_authenticate,
            re.IGNORECASE,
        )
    if match:
        return match.group(1)
    return None


def parse_bearer_resource_metadata(www_authenticate: str | None) -> str | None:
    """Parse ``WWW-Authenticate: Bearer resource_metadata="<url>"`` (RFC 9728).

    Returns the resource metadata URL, or None if not found.
    Aligns with Rust ``DiscoveryCleanupOAuthHttpClient`` auto-admission check.
    """
    if not www_authenticate:
        return None

    # quoted
    match = re.search(r'resource_metadata\s*=\s*"([^"]+)"', www_authenticate, re.IGNORECASE)
    if match:
        return match.group(1)
    # unquoted
    match = re.search(r'resource_metadata\s*=\s*([^\s,]+)', www_authenticate, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


# ============================================================================
# Redirect URI validation（对齐 Rust validate_redirect_uri，oauth.rs:2976）
# ============================================================================


def _normalize_issuer(value: str | None) -> str | None:
    """规范化 issuer（对齐 Rust ``canonical_issuer``）：trim + 去尾部斜杠。

    mcp 的 ``OAuthMetadata.issuer`` 是 ``AnyHttpUrl``（自带尾部斜杠），host 回调
    通常给裸 issuer——比较前两侧统一规范化，否则合法 callback 必误报 IssuerMismatch。
    """
    if value is None:
        return None
    normalized = str(value).strip().rstrip("/")
    return normalized or None


def _validate_redirect_uri(uri: str) -> None:
    """Validate a host-provided redirect URI（Rust 宿主契约的三种合法形态）。

    - HTTPS
    - loopback HTTP（``localhost`` / loopback IP，如 ``http://127.0.0.1:8080/cb``）
    - RFC 8252 反向域名 private-use URI（``com.example.app:/oauth/callback``：
      scheme ≥2 个合法 DNS 标签、无 authority、非根单斜杠路径）

    带 fragment 一律拒绝。**错误文案为静态字符串，绝不携带 URI 本体**
    （Rust 宿主契约：raw callback URI 不得入日志）。

    Raises:
        OAuthError: ``InvalidRedirectUri``。
    """
    parsed = urlparse(uri)
    if not parsed.scheme:
        raise OAuthError(OAuthErrorCode.InvalidRedirectUri, "redirect URI is invalid")
    if parsed.fragment:
        raise OAuthError(
            OAuthErrorCode.InvalidRedirectUri,
            "redirect URI must not contain a fragment",
        )

    scheme = parsed.scheme.lower()
    if scheme == "https":
        return
    if scheme == "http":
        if is_loopback_host(parsed):
            return
        raise OAuthError(
            OAuthErrorCode.InvalidRedirectUri,
            "redirect URI must use HTTPS, loopback HTTP, or a reverse-domain private-use scheme",
        )

    # RFC 8252 private-use scheme（非 http/https）
    labels = scheme.split(".")
    reverse_domain = len(labels) >= 2 and all(
        label
        and not label.startswith("-")
        and not label.endswith("-")
        and all(char.isascii() and (char.isalnum() or char == "-") for char in label)
        for label in labels
    )
    path = parsed.path
    if (
        reverse_domain
        and parsed.hostname is None
        and uri.startswith(f"{scheme}:/")
        and not uri.startswith(f"{scheme}://")
        and path.startswith("/")
        and len(path) > 1
    ):
        return
    raise OAuthError(
        OAuthErrorCode.InvalidRedirectUri,
        "redirect URI must use HTTPS, loopback HTTP, or a reverse-domain private-use scheme",
    )


# ============================================================================
# OAuthCoordinatorError → 公共 OAuthError（#179 收敛）
# ============================================================================

# #179：内部错误类型收敛为公共 OAuthError（oauth_types.py）。back-compat alias 保留
# #178 时代的引用（测试与既有 import），语义不变：携带 OAuthErrorCode + 静态 message。
_OAuthCoordinatorError = OAuthError


__all__ = [
    "ExpiringStateStore",
    "OAuthCoordinator",
    "TokenStorageAdapter",
    "parse_bearer_resource_metadata",
]
