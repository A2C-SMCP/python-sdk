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
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
    OAuthErrorCode,
    OAuthFlowOutcome,
    OAuthLaunch,
    OAuthOptions,
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

    def set_on_token_saved(self, callback: Callable[[], None] | None) -> None:
        """Register a callback invoked after tokens are persisted."""
        self._on_token_saved = callback

    # -- token keys -----------------------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        """Load OAuthToken from scoped credential store.

        ``try_load_credentials`` returns the raw credential content stored
        by ``save_credentials`` (the token JSON string).
        """
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

        # Callback bridge (host-provided or set later)
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler

        # OAuthClientProvider (lazily created)
        self._provider: OAuthClientProvider | None = None
        self._provider_needs_rebuild: bool = True

        # Pending callback awaitable (bridges async_auth_flow ↔ host complete/cancel)
        self._callback_future: asyncio.Future[tuple[str, str | None]] | None = None
        self._launch_future: asyncio.Future[OAuthLaunch] | None = None
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
                if self._flow.phase == _FlowPhase.EXPIRED:
                    self._status = _OAuthStatusUnauthorized()
                    self._flow = _AuthorizationFlowState()
            return self._status

    async def begin(self, request: OAuthBeginRequest) -> OAuthLaunch:
        """Start an interactive authorization flow.

        Performs discovery → DCR → PKCE S256 → generates authorization URL.
        Returns ``OAuthLaunch`` with the URL the host must open.

        Raises:
            OAuthError: If a different flow is already pending, or if
                the mode is not Authorization Code.
        """
        async with self._lock:
            await self._begin_under_lock(request)

        # Wait for the provider to generate the launch URL via redirect_handler
        if self._launch_future is None:
            raise _OAuthCoordinatorError(OAuthErrorCode.Protocol, "Launch future not set")
        return await self._launch_future

    async def _begin_under_lock(self, request: OAuthBeginRequest) -> None:
        """Core begin logic under self._lock."""
        self._state_store.expire_stale()

        # Validate mode — only Auth Code supported in Sub #178 (automatic-only)
        mode_dict = self._options.mode.model_dump(by_alias=False)
        if mode_dict.get("type") not in ("authorizationCode",):
            raise _OAuthCoordinatorError(
                OAuthErrorCode.UnsupportedTransport,
                f"OAuth mode {mode_dict.get('type')} not supported for interactive flow",
            )

        # Check for existing pending flow
        if self._flow.phase == _FlowPhase.PENDING and self._flow.pending is not None:
            existing = self._flow.pending
            if existing.request == request:
                # Same request → dedup, return existing launch
                self._launch_future = asyncio.get_event_loop().create_future()
                self._launch_future.set_result(existing.launch)
                return
            raise _OAuthCoordinatorError(
                OAuthErrorCode.AuthorizationAlreadyPending,
                "A different authorization flow is already pending",
            )

        # Build OAuthClientProvider if needed
        self._rebuild_provider_if_needed()

        # Create launch future — will be resolved by redirect_handler.
        # _begin_under_lock is async def → always inside a running loop; get_running_loop()
        # can never raise RuntimeError here.
        loop = asyncio.get_running_loop()
        self._launch_future = loop.create_future()
        self._callback_future = loop.create_future()

        # Wire token exchange completion callback
        self._token_storage.set_on_token_saved(self._on_token_saved)

        # Set pending state
        self._status = _OAuthStatusAuthorizationPending()
        self._generation += 1

        # Defer the actual OAuth flow to the provider's async_auth_flow
        # which is triggered when httpx receives a 401 response.
        # The redirect_handler will be called with the authorization URL.

    async def complete(self, callback: OAuthCallback) -> OAuthFlowOutcome:
        """Submit the browser callback and wait for the terminal outcome.

        Validates state + issuer + generation. The provider exchanges the
        code and persists credentials.
        """
        async with self._lock:
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

            # Validate issuer (if provided)
            if callback.issuer is not None and pending.issuer != callback.issuer:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.IssuerMismatch,
                    f"Callback issuer {callback.issuer!r} != {pending.issuer!r}",
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
            outcome: OAuthFlowOutcome
            token = await self._token_storage.get_tokens()
            if token is not None:
                self._state_store.finalize_exchange(callback.state)
                self._status = _OAuthStatusAuthorized(scopes=list(self._granted_scopes))
                self._flow = _AuthorizationFlowState()
                outcome = _OAuthOutcomeAuthorized(scopes=list(self._granted_scopes))
            else:
                self._state_store.release_exchange_claim(callback.state)
                outcome = _OAuthOutcomeTerminated(
                    reason=OAuthCancellationReason.AuthorizationError,
                    status=self._status,
                )
            self._token_exchange_done = None
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

            previous_status = self._status
            self._status = _OAuthStatusUnauthorized()
            self._flow = _AuthorizationFlowState()

            # Clean up PKCE state
            self._state_store.finalize_exchange(cancellation.state)

            # Cancel the pending callback
            cb = self._callback_future
            self._callback_future = None
            if cb and not cb.done():
                cb.cancel()

            return _OAuthOutcomeTerminated(
                reason=cancellation.reason,
                status=previous_status,
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

            if cancellation.issuer is not None and pending.issuer != cancellation.issuer:
                raise _OAuthCoordinatorError(
                    OAuthErrorCode.IssuerMismatch,
                    f"Cancellation issuer {cancellation.issuer!r} != {pending.issuer!r}",
                )

            previous_status = self._status
            self._status = _OAuthStatusUnauthorized()
            self._flow = _AuthorizationFlowState()

            # Clean up PKCE state
            self._state_store.finalize_exchange(cancellation.state)

            cb = self._callback_future
            self._callback_future = None
            if cb and not cb.done():
                cb.cancel()

            return _OAuthOutcomeTerminated(
                reason=cancellation.reason,
                status=previous_status,
            )

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
            try:
                token = await self._token_storage.get_tokens()
                client_info = await self._token_storage.get_client_info()
            except Exception:
                token = None
                client_info = None

            if token is None or client_info is None:
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

        True when credentials are restored and the provider can handle auth.
        """
        return self._provider is not None or self._status.state == "authorized"

    def build_oauth_provider(self) -> OAuthClientProvider:
        """Build (or rebuild) the ``OAuthClientProvider`` for httpx injection."""
        self._rebuild_provider_if_needed()
        assert self._provider is not None, "OAuthClientProvider not built"
        return self._provider

    def _rebuild_provider_if_needed(self) -> None:
        """Rebuild OAuthClientProvider if configuration or state changed."""
        if not self._provider_needs_rebuild and self._provider is not None:
            return

        redirect_uri = AnyUrl("http://127.0.0.1:0/callback")
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

    # =========================================================================
    # Redirect / callback handler factories
    # =========================================================================

    def _make_redirect_handler(self) -> Any:
        """Create the redirect_handler for OAuthClientProvider.

        Called by the provider when the authorization URL is ready.
        Stores the launch info and resolves _launch_future.
        """
        coordinator = self

        async def redirect_handler(url: str) -> None:
            # Extract state + redirect_uri from authorization URL
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            state = params.get("state", [None])[0] or ""
            redirect_uri_from_url = params.get("redirect_uri", [""])[0] or ""

            launch = OAuthLaunch(authorization_url=url, state=state)

            async with coordinator._lock:
                # Persist PKCE state for TTL + double-use prevention
                # (pkce_verifier is managed internally by OAuthClientProvider)
                coordinator._state_store.store(
                    state,
                    pkce_verifier="",  # provider-managed
                    issuer=coordinator._issuer,
                    redirect_uri=redirect_uri_from_url,
                    scopes=list(coordinator._options.scopes),
                )
                coordinator._flow = _AuthorizationFlowState(
                    phase=_FlowPhase.PENDING,
                    pending=_PendingAuthorization(
                        launch=launch,
                        request=OAuthBeginRequest(
                            redirect_uri=redirect_uri_from_url,
                            required_scope=None,
                        ),
                        requested_scopes=list(coordinator._options.scopes),
                        generation=coordinator._generation,
                        issuer=coordinator._issuer or "",
                    ),
                )
                coordinator._status = _OAuthStatusAuthorizationPending()

                # Resolve launch future
                if coordinator._launch_future and not coordinator._launch_future.done():
                    coordinator._launch_future.set_result(launch)

        return redirect_handler

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
        """
        if self._token_exchange_done is not None:
            self._token_exchange_done.set()

    async def invalidate_credentials(self) -> None:
        """Clear stored credentials (generation-gated)."""
        async with self._lock:
            self._generation += 1
            self._status = _OAuthStatusUnauthorized()
            self._provider_needs_rebuild = True
            self._granted_scopes.clear()
            try:
                await self._store.clear()
            except OAuthCredentialStoreError:
                pass


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
# OAuthCoordinatorError (internal)
# ============================================================================


class _OAuthCoordinatorError(Exception):
    """Internal coordinator error (not exposed on wire)."""

    def __init__(self, code: OAuthErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = [
    "ExpiringStateStore",
    "OAuthCoordinator",
    "TokenStorageAdapter",
    "parse_bearer_resource_metadata",
]
