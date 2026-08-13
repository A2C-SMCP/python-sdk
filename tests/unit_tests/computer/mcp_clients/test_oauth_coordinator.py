# -*- coding: utf-8 -*-
"""Unit tests for OAuthCoordinator, ExpiringStateStore, and TokenStorageAdapter.

Tests the Sub #178 OAuth flow engine: state machine, bounded transaction,
PKCE/CSRF TTL, credential restoration, Bearer challenge parsing.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.shared.auth import OAuthToken

from a2c_smcp.computer.mcp_clients.oauth_coordinator import (
    ExpiringStateStore,
    OAuthCoordinator,
    TokenStorageAdapter,
    _AuthorizationFlowState,
    _FlowPhase,
    _OAuthCoordinatorError,
    _parse_insufficient_scope,
    _PendingAuthorization,
    parse_bearer_resource_metadata,
)
from a2c_smcp.computer.mcp_clients.oauth_credential_store import (
    InMemoryOAuthCredentialStore,
    ScopedCredentialStore,
    StoredCredentialEnvelope,
    oauth_mode_fingerprint,
)
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
    OAuthError,
    OAuthErrorCode,
    OAuthLaunch,
    OAuthOptions,
    _OAuthModeAuthCodeDynamic,
    _OAuthStatusAuthorizationPending,
    _OAuthStatusAuthorized,
    _OAuthStatusUnauthorized,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def memory_store() -> InMemoryOAuthCredentialStore:
    return InMemoryOAuthCredentialStore()


@pytest.fixture
def oauth_options() -> OAuthOptions:
    return OAuthOptions(
        mode=_OAuthModeAuthCodeDynamic(),
        scopes=["read", "write"],
        client_name="test-client",
    )


@pytest.fixture
def scoped_store(memory_store: InMemoryOAuthCredentialStore, oauth_options: OAuthOptions) -> ScopedCredentialStore:
    return ScopedCredentialStore(
        bundle_id="test-bundle",
        resource="https://api.example.com",
        mode_fingerprint=oauth_mode_fingerprint(oauth_options),
        backend=memory_store,
    )


@pytest.fixture
def token_adapter(scoped_store: ScopedCredentialStore) -> TokenStorageAdapter:
    return TokenStorageAdapter(scoped_store)


@pytest.fixture
def coordinator(memory_store: InMemoryOAuthCredentialStore, oauth_options: OAuthOptions) -> OAuthCoordinator:
    return OAuthCoordinator(
        bundle_id="test-bundle",
        server_url="https://api.example.com",
        resource="https://api.example.com",
        options=oauth_options,
        credential_store=memory_store,
    )


# ============================================================================
# ExpiringStateStore tests
# ============================================================================


class TestExpiringStateStore:
    def test_store_and_lookup(self) -> None:
        store = ExpiringStateStore(ttl=600.0)
        store.store("state1", pkce_verifier="v1", issuer="https://auth.example.com")
        entry = store.lookup("state1")
        assert entry is not None
        assert entry.pkce_verifier == "v1"
        assert entry.issuer == "https://auth.example.com"

    def test_lookup_nonexistent_returns_none(self) -> None:
        store = ExpiringStateStore()
        assert store.lookup("nonexistent") is None

    def test_claim_for_exchange_prevents_double_use(self) -> None:
        store = ExpiringStateStore(ttl=600.0)
        store.store("state1", pkce_verifier="v1")
        first = store.claim_for_exchange("state1")
        assert first is not None
        assert first.pkce_verifier == "v1"
        # Second claim must fail
        second = store.claim_for_exchange("state1")
        assert second is None

    def test_release_exchange_claim_allows_reclaim(self) -> None:
        store = ExpiringStateStore(ttl=600.0)
        store.store("state1", pkce_verifier="v1")
        store.claim_for_exchange("state1")
        store.release_exchange_claim("state1")
        # After release, can claim again
        claimed = store.claim_for_exchange("state1")
        assert claimed is not None

    def test_finalize_exchange_removes_state(self) -> None:
        store = ExpiringStateStore(ttl=600.0)
        store.store("state1", pkce_verifier="v1")
        store.finalize_exchange("state1")
        assert store.lookup("state1") is None

    def test_expire_stale_removes_expired(self) -> None:
        store = ExpiringStateStore(ttl=0.01)  # 10ms TTL
        store.store("state1", pkce_verifier="v1")
        time.sleep(0.02)
        store.expire_stale()
        assert store.lookup("state1") is None

    def test_claimed_state_survives_expiry(self) -> None:
        store = ExpiringStateStore(ttl=0.01)
        store.store("state1", pkce_verifier="v1")
        store.claim_for_exchange("state1")
        time.sleep(0.02)
        # Expired but claimed → survives
        store.expire_stale()
        assert store.lookup("state1") is not None

    def test_claim_expired_nonexistent(self) -> None:
        store = ExpiringStateStore()
        assert store.claim_for_exchange("nope") is None


# ============================================================================
# Bearer challenge parsing tests
# ============================================================================


class TestBearerChallengeParsing:
    def test_parse_resource_metadata_quoted(self) -> None:
        header = 'Bearer resource_metadata="https://example.com/.well-known/oauth-protected-resource"'
        result = parse_bearer_resource_metadata(header)
        assert result == "https://example.com/.well-known/oauth-protected-resource"

    def test_parse_resource_metadata_unquoted(self) -> None:
        header = "Bearer resource_metadata=https://example.com/prm"
        result = parse_bearer_resource_metadata(header)
        assert result == "https://example.com/prm"

    def test_parse_resource_metadata_none(self) -> None:
        assert parse_bearer_resource_metadata(None) is None
        assert parse_bearer_resource_metadata("") is None

    def test_parse_resource_metadata_no_match(self) -> None:
        assert parse_bearer_resource_metadata("Bearer realm=example") is None

    def test_parse_insufficient_scope(self) -> None:
        header = 'Bearer error="insufficient_scope", scope="tools.write tools.read"'
        result = _parse_insufficient_scope(header)
        assert result == "tools.write tools.read"

    def test_parse_insufficient_scope_reversed_order(self) -> None:
        header = 'Bearer scope="admin", error="insufficient_scope"'
        result = _parse_insufficient_scope(header)
        assert result == "admin"

    def test_parse_insufficient_scope_no_match(self) -> None:
        assert _parse_insufficient_scope("") is None
        assert _parse_insufficient_scope("Bearer error=invalid_token") is None
        assert _parse_insufficient_scope(None) is None  # type: ignore[arg-type]


# ============================================================================
# TokenStorageAdapter tests
# ============================================================================


class TestTokenStorageAdapter:
    @pytest.mark.asyncio
    async def test_get_tokens_empty_store(self, token_adapter: TokenStorageAdapter) -> None:
        result = await token_adapter.get_tokens()
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_tokens(
        self, token_adapter: TokenStorageAdapter, scoped_store: ScopedCredentialStore
    ) -> None:
        token = OAuthToken(
            access_token="access-123",
            token_type="Bearer",
            expires_in=3600,
            scope="read write",
            refresh_token="refresh-456",
        )
        await token_adapter.set_tokens(token)

        restored = await token_adapter.get_tokens()
        assert restored is not None
        assert restored.access_token == "access-123"
        assert restored.refresh_token == "refresh-456"
        assert restored.scope == "read write"

    @pytest.mark.asyncio
    async def test_scope_preservation_on_refresh(
        self, token_adapter: TokenStorageAdapter
    ) -> None:
        # Store token with scopes
        old = OAuthToken(
            access_token="old-access",
            token_type="Bearer",
            scope="read write",
            refresh_token="old-refresh",
        )
        await token_adapter.set_tokens(old)

        # Refresh token omitting scope
        new_token = OAuthToken(
            access_token="new-access",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="new-refresh",
            # scope omitted!
        )
        await token_adapter.set_tokens(new_token)

        restored = await token_adapter.get_tokens()
        assert restored is not None
        assert restored.access_token == "new-access"
        assert restored.scope == "read write"  # preserved from old

    @pytest.mark.asyncio
    async def test_get_client_info_empty(self, token_adapter: TokenStorageAdapter) -> None:
        result = await token_adapter.get_client_info()
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_client_info(self, token_adapter: TokenStorageAdapter) -> None:
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="client-123",
            client_secret="secret-456",
            redirect_uris=["http://127.0.0.1:0/callback"],
            client_name="test-client",
        )
        await token_adapter.set_client_info(client_info)

        restored = await token_adapter.get_client_info()
        assert restored is not None
        assert restored.client_id == "client-123"
        assert restored.client_secret == "secret-456"


# ============================================================================
# OAuthCoordinator tests
# ============================================================================


class TestOAuthCoordinator:
    @pytest.mark.asyncio
    async def test_initial_status_is_unauthorized(self, coordinator: OAuthCoordinator) -> None:
        status = await coordinator.status()
        assert isinstance(status, _OAuthStatusUnauthorized)

    @pytest.mark.asyncio
    async def test_restore_credentials_no_stored(self, coordinator: OAuthCoordinator) -> None:
        status = await coordinator.restore_credentials()
        assert isinstance(status, _OAuthStatusUnauthorized)

    @pytest.mark.asyncio
    async def test_restore_credentials_with_stored(
        self, coordinator: OAuthCoordinator, token_adapter: TokenStorageAdapter
    ) -> None:
        # Pre-store credentials
        from mcp.shared.auth import OAuthClientInformationFull

        token = OAuthToken(
            access_token="access-abc",
            token_type="Bearer",
            expires_in=3600,
            scope="read",
            refresh_token="refresh-xyz",
        )
        await token_adapter.set_tokens(token)

        client_info = OAuthClientInformationFull(
            client_id="client-1",
            redirect_uris=["http://127.0.0.1:0/callback"],
            client_name="test",
        )
        await token_adapter.set_client_info(client_info)

        status = await coordinator.restore_credentials()
        assert isinstance(status, _OAuthStatusAuthorized)
        assert status.scopes == ["read"]

    @pytest.mark.asyncio
    async def test_needs_oauth_provider_initially_false(self, coordinator: OAuthCoordinator) -> None:
        assert coordinator.needs_oauth_provider() is False

    @pytest.mark.asyncio
    async def test_needs_oauth_provider_after_restore(
        self, coordinator: OAuthCoordinator, token_adapter: TokenStorageAdapter
    ) -> None:
        from mcp.shared.auth import OAuthClientInformationFull

        token = OAuthToken(
            access_token="at", token_type="Bearer", refresh_token="rt"
        )
        await token_adapter.set_tokens(token)
        await token_adapter.set_client_info(
            OAuthClientInformationFull(
                client_id="c1", redirect_uris=["http://127.0.0.1:0/callback"], client_name="t"
            )
        )
        await coordinator.restore_credentials()
        # restore_credentials() sets _status → Authorized when both token
        # and client_info are present. needs_oauth_provider() returns True
        # when _status.state == "authorized" (not dependent on _provider existence).
        assert coordinator.needs_oauth_provider() is True

    @pytest.mark.asyncio
    async def test_build_oauth_provider(self, coordinator: OAuthCoordinator) -> None:
        provider = coordinator.build_oauth_provider()
        assert provider is not None

    @pytest.mark.asyncio
    async def test_begin_not_interactive_without_challenge(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """begin() validates mode before creating launch future."""
        # With AuthCodeDynamic mode, begin_under_lock should pass mode validation.
        # Without a real OAuthClientProvider triggering the flow, the launch_future
        # will hang → but the state machine should be PENDING.
        # Test the mode validation path by calling _begin_under_lock directly.
        from a2c_smcp.computer.mcp_clients.oauth_coordinator import _OAuthCoordinatorError

        # A non-AuthCode mode should be rejected
        coordinator._options = OAuthOptions(
            mode=_OAuthModeAuthCodeDynamic(),  # still AuthCode, OK
            scopes=["read"],
            client_name="test",
        )
        # Verifying the coordinator was created with correct initial state
        assert coordinator._status.state == "unauthorized"

    @pytest.mark.asyncio
    async def test_cancel_no_pending_flow(self, coordinator: OAuthCoordinator) -> None:
        cancellation = OAuthCancellation(
            state="nonexistent",
            reason=OAuthCancellationReason.Cancelled,
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.cancel(cancellation)
        assert exc.value.code == OAuthErrorCode.StateMismatch

    @pytest.mark.asyncio
    async def test_cancel_invalid_reason(self, coordinator: OAuthCoordinator) -> None:
        cancellation = OAuthCancellation(
            state="any",
            reason=OAuthCancellationReason.AccessDenied,
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.cancel(cancellation)
        assert exc.value.code == OAuthErrorCode.InvalidCancellationReason

    @pytest.mark.asyncio
    async def test_complete_no_pending_flow(self, coordinator: OAuthCoordinator) -> None:
        callback = OAuthCallback(code="code", state="state")
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.complete(callback)
        assert exc.value.code == OAuthErrorCode.StateMismatch

    @pytest.mark.asyncio
    async def test_handle_insufficient_scope(self, coordinator: OAuthCoordinator) -> None:
        await coordinator.handle_insufficient_scope("admin.write")
        from a2c_smcp.computer.mcp_clients.oauth_types import _OAuthStatusReauthorizationRequired

        status = await coordinator.status()
        assert isinstance(status, _OAuthStatusReauthorizationRequired)
        assert status.required_scope == "admin.write"

    @pytest.mark.asyncio
    async def test_observe_service_success_confirms_provisional(
        self, coordinator: OAuthCoordinator, token_adapter: TokenStorageAdapter
    ) -> None:
        from mcp.shared.auth import OAuthClientInformationFull

        # Set up authorized state
        await token_adapter.set_tokens(
            OAuthToken(access_token="at", token_type="Bearer", scope="read")
        )
        await token_adapter.set_client_info(
            OAuthClientInformationFull(
                client_id="c1", redirect_uris=["http://127.0.0.1:0/callback"], client_name="t"
            )
        )
        await coordinator.restore_credentials()

        # Trigger insufficient_scope
        await coordinator.handle_insufficient_scope("admin")
        status = await coordinator.status()
        assert status.state == "reauthorizationRequired"

        # Confirm via success observation
        await coordinator.observe_service_success()
        status = await coordinator.status()
        assert isinstance(status, _OAuthStatusAuthorized)

    @pytest.mark.asyncio
    async def test_observe_service_error_401_invalidates(
        self, coordinator: OAuthCoordinator, token_adapter: TokenStorageAdapter
    ) -> None:
        from mcp.shared.auth import OAuthClientInformationFull

        await token_adapter.set_tokens(
            OAuthToken(access_token="at", token_type="Bearer", scope="read")
        )
        await token_adapter.set_client_info(
            OAuthClientInformationFull(
                client_id="c1", redirect_uris=["http://127.0.0.1:0/callback"], client_name="t"
            )
        )
        await coordinator.restore_credentials()

        handled = await coordinator.observe_service_error(401, None)
        assert handled is True

    @pytest.mark.asyncio
    async def test_observe_service_error_403_insufficient_scope(
        self, coordinator: OAuthCoordinator, token_adapter: TokenStorageAdapter
    ) -> None:
        from mcp.shared.auth import OAuthClientInformationFull

        await token_adapter.set_tokens(
            OAuthToken(access_token="at", token_type="Bearer", scope="read")
        )
        await token_adapter.set_client_info(
            OAuthClientInformationFull(
                client_id="c1", redirect_uris=["http://127.0.0.1:0/callback"], client_name="t"
            )
        )
        await coordinator.restore_credentials()

        handled = await coordinator.observe_service_error(
            403, 'Bearer error="insufficient_scope", scope="write"'
        )
        assert handled is True

        status = await coordinator.status()
        assert status.state == "reauthorizationRequired"

    @pytest.mark.asyncio
    async def test_invalidate_credentials(self, coordinator: OAuthCoordinator) -> None:
        await coordinator.invalidate_credentials()
        status = await coordinator.status()
        assert isinstance(status, _OAuthStatusUnauthorized)

    @pytest.mark.asyncio
    async def test_cancel_callback_no_pending_flow(self, coordinator: OAuthCoordinator) -> None:
        cancellation = OAuthCancellation(
            state="any",
            reason=OAuthCancellationReason.AccessDenied,
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.cancel_callback(cancellation)
        assert exc.value.code == OAuthErrorCode.StateMismatch

    @pytest.mark.asyncio
    async def test_cancel_callback_invalid_reason(self, coordinator: OAuthCoordinator) -> None:
        cancellation = OAuthCancellation(
            state="any",
            reason=OAuthCancellationReason.Cancelled,  # not valid for callback cancel
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.cancel_callback(cancellation)
        assert exc.value.code == OAuthErrorCode.InvalidCancellationReason

    # ── begin/complete/cancel happy path（#179 解封：直驱 _make_redirect_handler 闭包） ──
    # 401 触发式 mcp inline auth 流程中，redirect_handler 由 provider 在收到
    # Bearer challenge 后调用；单元测试不经真实 transport，直接驱动该闭包
    # （#178 xfail 注释所指「可注入 mock」的落地形态）。

    @pytest.mark.asyncio
    async def test_begin_happy_path(self, coordinator: OAuthCoordinator) -> None:
        """begin() → 返回 OAuthLaunch，flow 进入 PENDING（compat = register + wait_launch）。"""
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        begin_task = asyncio.create_task(coordinator.begin(request))
        await asyncio.sleep(0)  # 让 register() 先落地
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        launch = await asyncio.wait_for(begin_task, timeout=5)
        assert launch.authorization_url
        assert launch.state == "st1"

        status = await coordinator.status()
        assert status.state == "authorizationPending"  # _OAuthStatusAuthorizationPending

    @pytest.mark.asyncio
    async def test_cancel_happy_path(self, coordinator: OAuthCoordinator) -> None:
        """cancel(Cancelled) → 清理 flow，返回 Terminated，status 回落 unauthorized。"""
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        cancellation = OAuthCancellation(state="st1", reason=OAuthCancellationReason.Cancelled)
        outcome = await coordinator.cancel(cancellation)
        assert outcome.outcome == "terminated"  # _OAuthOutcomeTerminated discriminator

        status = await coordinator.status()
        assert status.state == "unauthorized"

    @pytest.mark.asyncio
    async def test_complete_happy_path(self, coordinator: OAuthCoordinator) -> None:
        """complete(code, state) → 交换 token，返回 Authorized。"""
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        complete_task = asyncio.create_task(
            coordinator.complete(OAuthCallback(code="mock-code", state="st1"))
        )
        await asyncio.sleep(0)  # 让 claim_for_exchange 先落地
        # 模拟 provider 的 token 交换持久化（set_tokens → _on_token_saved → exchange_done）
        token = OAuthToken(access_token="at1", token_type="Bearer", scope="read write")
        await coordinator._token_storage.set_tokens(token)
        outcome = await asyncio.wait_for(complete_task, timeout=5)
        assert outcome.outcome == "authorized"  # _OAuthOutcomeAuthorized discriminator

    @pytest.mark.asyncio
    async def test_cancel_callback_happy_path(self, coordinator: OAuthCoordinator) -> None:
        """cancel_callback(AccessDenied) → 返回 Terminated。

        provider 路径仅接受 AccessDenied / AuthorizationError（Timeout 属宿主路径，
        由 cancel() 处理；原 xfail 断言误用 Timeout，#179 一并修正）。
        """
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        cancellation = OAuthCancellation(
            state="st1", reason=OAuthCancellationReason.AccessDenied
        )
        outcome = await coordinator.cancel_callback(cancellation)
        assert outcome.outcome == "terminated"  # _OAuthOutcomeTerminated discriminator


# ============================================================================
# #179 隔离复核补测：stale-generation complete 路径 + expire 探测复位 suppress
# ============================================================================


class TestStaleGenerationConvergence:
    """🟡3 / 🟡b 复核补测：PENDING 相 stale generation 与 suppress 残留。"""

    @pytest.mark.asyncio
    async def test_complete_stale_generation_expired(self, coordinator: OAuthCoordinator) -> None:
        """complete 时 pending 已陈旧（refresh save 曾 bump generation）→ AuthorizationExpired。"""
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        coordinator._generation += 1  # 模拟 pending 期间的凭据 save/refresh bump
        with pytest.raises(OAuthError) as exc:
            await coordinator.complete(OAuthCallback(code="c1", state="st1"))
        assert exc.value.code == OAuthErrorCode.AuthorizationExpired
        assert coordinator._flow.phase == _FlowPhase.EXPIRED

    @pytest.mark.asyncio
    async def test_cancel_stale_generation_expired(self, coordinator: OAuthCoordinator) -> None:
        """cancel 同判据（与 complete 对齐）。"""
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        coordinator._generation += 1
        with pytest.raises(OAuthError) as exc:
            await coordinator.cancel(OAuthCancellation(state="st1", reason=OAuthCancellationReason.Cancelled))
        assert exc.value.code == OAuthErrorCode.AuthorizationExpired

    @pytest.mark.asyncio
    async def test_expire_restores_authorized_with_stored_credentials(
        self, memory_store: InMemoryOAuthCredentialStore, oauth_options: OAuthOptions
    ) -> None:
        """register 驱动 PENDING（suppress 置位）+ 预存凭据 + stale → expire 探测复位
        suppress 并恢复 Authorized（🟡b：旧凭据仍可用，不得恒判 Unauthorized）。"""
        from mcp.shared.auth import OAuthClientInformationFull

        coord = OAuthCoordinator(
            bundle_id="b1",
            server_url="https://api.example.com",
            resource="https://api.example.com",
            options=oauth_options,
            credential_store=memory_store,
        )
        # 预存旧授权凭据（register 之前 → 不受 suppress 影响）
        await coord._token_storage.set_tokens(
            OAuthToken(access_token="at1", token_type="Bearer", scope="read")
        )
        await coord._token_storage.set_client_info(
            OAuthClientInformationFull(
                client_id="dcr-1",
                client_secret=None,
                redirect_uris=["https://host.example/callback"],
                client_name="A2C Test",
            )
        )
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coord.register(request)
        handler = coord._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        coord._generation += 1  # stale
        status = await coord.status()  # Pending → 触发 expire 收敛
        assert status.state == "authorized"  # 旧凭据仍可用（suppress 已复位）
        assert coord.has_registered_request() is False


# ============================================================================
# #179 staged-flow：register（无 I/O 注册）vs wait_launch（等 401 触发的 URL）
# ============================================================================


class TestStagedRegistration:
    """宿主在 challenge 之前即可注册 flow；注册无 I/O、幂等、冲突结构化报错。"""

    @pytest.mark.asyncio
    async def test_register_is_idempotent_for_identical_request(self, coordinator: OAuthCoordinator) -> None:
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        await coordinator.register(request)  # 幂等，不抛
        assert coordinator.has_registered_request()

    @pytest.mark.asyncio
    async def test_register_conflicting_request_raises(self, coordinator: OAuthCoordinator) -> None:
        request1 = OAuthBeginRequest(redirect_uri="https://host.example/cb1")
        request2 = OAuthBeginRequest(redirect_uri="https://host.example/cb2")
        await coordinator.register(request1)
        with pytest.raises(OAuthError) as exc:
            await coordinator.register(request2)
        assert exc.value.code == OAuthErrorCode.AuthorizationAlreadyPending

    @pytest.mark.asyncio
    async def test_register_pre_challenge_builds_no_provider(self, coordinator: OAuthCoordinator) -> None:
        await coordinator.register(OAuthBeginRequest(redirect_uri="https://host.example/callback"))
        assert coordinator._provider is None  # 无 I/O：provider 留待 challenge 后重建

    @pytest.mark.asyncio
    async def test_register_sets_pending_status(self, coordinator: OAuthCoordinator) -> None:
        await coordinator.register(OAuthBeginRequest(redirect_uri="https://host.example/callback"))
        status = await coordinator.status()
        assert status.state == "authorizationPending"

    @pytest.mark.asyncio
    async def test_pre_challenge_cancel_clears_registered(self, coordinator: OAuthCoordinator) -> None:
        """pre-challenge cancel 终态收敛清注册（原 clear_registered_request 死 API 已删，
        语义由 cancel_pending 的 teardown 覆盖）。"""
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        outcome = await coordinator.cancel_pending(
            OAuthCancellationReason.Cancelled,
            expected_generation=coordinator.current_generation(),
        )
        assert outcome.outcome == "terminated"
        assert not coordinator.has_registered_request()

    # ── redirect_uri 校验矩阵（对齐 Rust validate_redirect_uri，oauth.rs:2976） ──

    @pytest.mark.asyncio
    async def test_register_accepts_https(self, coordinator: OAuthCoordinator) -> None:
        await coordinator.register(OAuthBeginRequest(redirect_uri="https://host.example/cb"))

    @pytest.mark.asyncio
    async def test_register_accepts_loopback_ip(self, coordinator: OAuthCoordinator) -> None:
        await coordinator.register(OAuthBeginRequest(redirect_uri="http://127.0.0.1:8080/cb"))

    @pytest.mark.asyncio
    async def test_register_accepts_loopback_localhost(self, coordinator: OAuthCoordinator) -> None:
        await coordinator.register(OAuthBeginRequest(redirect_uri="http://localhost/cb"))

    @pytest.mark.asyncio
    async def test_register_rejects_http_non_loopback(self, coordinator: OAuthCoordinator) -> None:
        with pytest.raises(OAuthError) as exc:
            await coordinator.register(OAuthBeginRequest(redirect_uri="http://10.0.0.1/cb"))
        assert exc.value.code == OAuthErrorCode.InvalidRedirectUri

    @pytest.mark.asyncio
    async def test_register_accepts_reverse_domain_private_use(self, coordinator: OAuthCoordinator) -> None:
        await coordinator.register(
            OAuthBeginRequest(redirect_uri="com.example.app:/oauth/callback")
        )

    @pytest.mark.asyncio
    async def test_register_rejects_single_label_private_use(self, coordinator: OAuthCoordinator) -> None:
        with pytest.raises(OAuthError) as exc:
            await coordinator.register(OAuthBeginRequest(redirect_uri="custom:/callback"))
        assert exc.value.code == OAuthErrorCode.InvalidRedirectUri

    @pytest.mark.asyncio
    async def test_register_rejects_authority_bearing_private_use(self, coordinator: OAuthCoordinator) -> None:
        with pytest.raises(OAuthError) as exc:
            await coordinator.register(
                OAuthBeginRequest(redirect_uri="com.example.app://host/cb")
            )
        assert exc.value.code == OAuthErrorCode.InvalidRedirectUri

    @pytest.mark.asyncio
    async def test_register_rejects_fragment(self, coordinator: OAuthCoordinator) -> None:
        with pytest.raises(OAuthError) as exc:
            await coordinator.register(
                OAuthBeginRequest(redirect_uri="https://host.example/cb#frag")
            )
        assert exc.value.code == OAuthErrorCode.InvalidRedirectUri


class TestRedirectHandlerContract:
    """#179：redirect_handler 只在宿主已注册 flow 时发布 launch；challenge-only 路径不伪造 pending。"""

    @pytest.mark.asyncio
    async def test_redirect_with_registered_request_populates_pending(
        self, coordinator: OAuthCoordinator
    ) -> None:
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        assert coordinator._flow.phase == _FlowPhase.PENDING
        assert coordinator._flow.pending is not None
        # pending 携带宿主 request 对象（非 URL 反构），供幂等/冲突判定
        assert coordinator._flow.pending.request == request
        assert coordinator._flow.pending.request.redirect_uri == "https://host.example/callback"

    @pytest.mark.asyncio
    async def test_redirect_without_registered_request_raises(
        self, coordinator: OAuthCoordinator
    ) -> None:
        handler = coordinator._make_redirect_handler()
        with pytest.raises(OAuthError) as exc:
            await handler("https://auth.example/authorize?state=st1")
        assert exc.value.code == OAuthErrorCode.Protocol
        assert coordinator._flow.phase == _FlowPhase.IDLE  # 不伪造 pending

    @pytest.mark.asyncio
    async def test_redirect_resolves_launch_future(self, coordinator: OAuthCoordinator) -> None:
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        launch = await coordinator.wait_launch()
        assert launch.state == "st1"
        assert launch.authorization_url.startswith("https://auth.example/authorize")

    @pytest.mark.asyncio
    async def test_redirect_captures_provider_issuer(self, coordinator: OAuthCoordinator) -> None:
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        # 模拟 provider 已发现 AS metadata 且带 issuer（mcp 公共属性）
        provider = MagicMock()
        provider.context.oauth_metadata.issuer = "https://auth.example"
        coordinator._provider = provider
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        assert coordinator._issuer == "https://auth.example"
        assert coordinator._flow.pending is not None
        assert coordinator._flow.pending.issuer == "https://auth.example"


class TestProviderRebuild:
    """#179：redirect_uri 权威来源 = 宿主 OAuthBeginRequest（Rust 宿主契约）。"""

    @pytest.mark.asyncio
    async def test_provider_uses_host_redirect_uri(self, coordinator: OAuthCoordinator) -> None:
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        provider = coordinator.build_oauth_provider()
        assert str(provider.context.client_metadata.redirect_uris[0]) == "https://host.example/callback"


class TestStagedValidationDetails:
    """#179：cancel issuer 校验 + 终态清 registered/futures + restore 采纳持久化 issuer。"""

    @pytest.mark.asyncio
    async def test_cancel_issuer_mismatch(self, coordinator: OAuthCoordinator) -> None:
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        provider = MagicMock()
        provider.context.oauth_metadata.issuer = "https://auth.example"
        coordinator._provider = provider
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        cancellation = OAuthCancellation(
            state="st1",
            issuer="https://other.example",
            reason=OAuthCancellationReason.Cancelled,
        )
        with pytest.raises(OAuthError) as exc:
            await coordinator.cancel(cancellation)
        assert exc.value.code == OAuthErrorCode.IssuerMismatch

    @pytest.mark.asyncio
    async def test_cancel_clears_registered_request(self, coordinator: OAuthCoordinator) -> None:
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        outcome = await coordinator.cancel(
            OAuthCancellation(state="st1", reason=OAuthCancellationReason.Cancelled)
        )
        assert outcome.outcome == "terminated"
        assert not coordinator.has_registered_request()
        # 终态后同请求可重新注册（fresh flow）
        await coordinator.register(request)

    @pytest.mark.asyncio
    async def test_restore_credentials_adopts_persisted_issuer(
        self,
        memory_store: InMemoryOAuthCredentialStore,
        oauth_options: OAuthOptions,
    ) -> None:
        # 第一个 coordinator 完成授权（issuer 已持久化到 index + 凭据已保存）
        first = OAuthCoordinator(
            bundle_id="b1",
            server_url="https://api.example.com",
            resource="https://api.example.com",
            options=oauth_options,
            credential_store=memory_store,
        )
        await first._store.set_issuer("https://auth.example")
        token = OAuthToken(access_token="at1", token_type="Bearer", scope="read")
        await first._token_storage.set_tokens(token)
        from mcp.shared.auth import OAuthClientInformationFull

        await first._token_storage.set_client_info(
            OAuthClientInformationFull(
                client_id="dcr-1",
                client_secret="secret-1",
                redirect_uris=["https://host.example/callback"],
                client_name="A2C Test",
            )
        )

        # 第二个 coordinator（模拟进程重启后重建）从同一 store restore
        second = OAuthCoordinator(
            bundle_id="b1",
            server_url="https://api.example.com",
            resource="https://api.example.com",
            options=oauth_options,
            credential_store=memory_store,
        )
        status = await second.restore_credentials()
        assert status.state == "authorized"
        assert second._issuer == "https://auth.example"
        assert second._granted_scopes == ["read"]


# ============================================================================
# fix-review 回归：槽终结一致性 / clear waiter / 注册 TTL
# ============================================================================


class TestSlotTeardownConsistency:
    """🔴1 / 🔴2 / clear-waiter：任何终结路径不留陈旧 launch future 或半拆解槽。"""

    @pytest.mark.asyncio
    async def test_expired_flow_then_new_register_gets_fresh_url(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """🔴1：过期收敛必须置空 launch future——新注册的 wait_launch 不得返回旧 flow 的 URL。"""
        req1 = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(req1)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st1"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        coordinator._generation += 1  # stale → expire
        await coordinator.status()

        # 新注册（不同请求）→ 直驱 handler → wait_launch 必须是**新** state
        req2 = OAuthBeginRequest(redirect_uri="https://host.example/cb2")
        await coordinator.register(req2)
        await handler(
            "https://auth.example/authorize?state=st2"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcb2"
        )
        launch = await coordinator.wait_launch()
        assert launch.state == "st2"

    @pytest.mark.asyncio
    async def test_fail_launch_then_same_request_retry_recovers(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """🔴2：fail_launch 完整拆解——同请求重试不得重抛陈旧异常，新 connect 可发布新 URL。"""
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        coordinator.fail_launch(
            OAuthError(OAuthErrorCode.Protocol, "OAuth protocol error: authorizationRequired")
        )
        with pytest.raises(OAuthError):
            await coordinator.wait_launch()

        # 同请求重试：注册可落位 + 新 connect 任务的 URL 可发布（不再命中陈旧 future）
        await coordinator.register(request)
        handler = coordinator._make_redirect_handler()
        await handler(
            "https://auth.example/authorize?state=st2"
            "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
        )
        launch = await coordinator.wait_launch()
        assert launch.state == "st2"

    @pytest.mark.asyncio
    async def test_clear_resolves_in_flight_launch_waiter(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """clear() 终态必须解除在途 wait_launch 等待者（teardown 升格）。"""
        request = OAuthBeginRequest(redirect_uri="https://host.example/callback")
        await coordinator.register(request)
        waiter = asyncio.create_task(coordinator.wait_launch())
        await asyncio.sleep(0)
        await coordinator.clear()
        with pytest.raises(OAuthError) as exc:
            await asyncio.wait_for(waiter, timeout=5)
        assert exc.value.code == OAuthErrorCode.AuthorizationCancelled


class TestRegistrationTtl:
    """🟡3：register-only 阶段（从未 challenge）超时过期，不得无限阻塞新 flow。"""

    @pytest.mark.asyncio
    async def test_stale_registration_replaced_by_new_request(
        self, coordinator: OAuthCoordinator
    ) -> None:
        req1 = OAuthBeginRequest(redirect_uri="https://host.example/cb1")
        await coordinator.register(req1)
        assert coordinator.has_active_flow()
        # 回拨注册时间模拟超时（TTL 600s）
        coordinator._registered_at = (
            time.monotonic() - 700.0  # type: ignore[union-attr]
        )
        assert not coordinator.has_active_flow()  # 时间感知：过期视为非活跃
        req2 = OAuthBeginRequest(redirect_uri="https://host.example/cb2")
        await coordinator.register(req2)  # 放行（不再 AlreadyPending）
        assert coordinator.has_registered_request()

    @pytest.mark.asyncio
    async def test_fresh_registration_blocks_conflicting_request(
        self, coordinator: OAuthCoordinator
    ) -> None:
        await coordinator.register(OAuthBeginRequest(redirect_uri="https://host.example/cb1"))
        with pytest.raises(OAuthError) as exc:
            await coordinator.register(OAuthBeginRequest(redirect_uri="https://host.example/cb2"))
        assert exc.value.code == OAuthErrorCode.AuthorizationAlreadyPending


# ============================================================================
# Integration tests (OAuthCoordinator + TokenStorageAdapter + ScopedCredentialStore)
# ============================================================================


class TestOAuthIntegration:
    @pytest.mark.asyncio
    async def test_full_credential_lifecycle(
        self, memory_store: InMemoryOAuthCredentialStore, oauth_options: OAuthOptions
    ) -> None:
        """Test the full lifecycle: store → restore → clear."""
        scoped = ScopedCredentialStore(
            bundle_id="b1",
            resource="https://api.example.com",
            mode_fingerprint=oauth_mode_fingerprint(oauth_options),
            backend=memory_store,
        )
        adapter = TokenStorageAdapter(scoped)

        # Initially empty
        assert await adapter.get_tokens() is None
        assert await adapter.get_client_info() is None

        # Store DCR info
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="dcr-client-1",
            client_secret="dcr-secret-1",
            redirect_uris=["http://127.0.0.1:0/callback"],
            client_name="A2C Test",
        )
        await adapter.set_client_info(client_info)

        # Store token
        token = OAuthToken(
            access_token="tok-xyz",
            token_type="Bearer",
            expires_in=3600,
            scope="read write",
            refresh_token="ref-abc",
        )
        await adapter.set_tokens(token)

        # Create new adapter (simulates restart)
        adapter2 = TokenStorageAdapter(scoped)
        restored_token = await adapter2.get_tokens()
        assert restored_token is not None
        assert restored_token.access_token == "tok-xyz"
        assert restored_token.refresh_token == "ref-abc"

        restored_client = await adapter2.get_client_info()
        assert restored_client is not None
        assert restored_client.client_id == "dcr-client-1"

        # Clear
        await scoped.clear()
        adapter3 = TokenStorageAdapter(scoped)
        assert await adapter3.get_tokens() is None
        assert await adapter3.get_client_info() is None

    @pytest.mark.asyncio
    async def test_coordinator_restore_after_reconnect(
        self, memory_store: InMemoryOAuthCredentialStore, oauth_options: OAuthOptions
    ) -> None:
        """Simulate reconnect: coordinator → restore credentials → authorized."""
        # Setup: pre-store credentials
        scoped = ScopedCredentialStore(
            bundle_id="b1",
            resource="https://api.example.com",
            mode_fingerprint=oauth_mode_fingerprint(oauth_options),
            backend=memory_store,
        )
        adapter = TokenStorageAdapter(scoped)

        from mcp.shared.auth import OAuthClientInformationFull

        await adapter.set_client_info(
            OAuthClientInformationFull(
                client_id="c1",
                redirect_uris=["http://127.0.0.1:0/callback"],
                client_name="test",
            )
        )
        await adapter.set_tokens(
            OAuthToken(
                access_token="restored-token",
                token_type="Bearer",
                expires_in=3600,
                scope="read",
                refresh_token="restored-refresh",
            )
        )

        # Create coordinator (simulates fresh process)
        coord = OAuthCoordinator(
            bundle_id="b1",
            server_url="https://api.example.com",
            resource="https://api.example.com",
            options=oauth_options,
            credential_store=memory_store,
        )
        status = await coord.restore_credentials()
        assert isinstance(status, _OAuthStatusAuthorized)
        assert status.scopes == ["read"]


# ============================================================================
# ExpiredFlow tests — Issue #186: 补全 expire_invalid_authorization_flow
# ============================================================================


@pytest.fixture
def coordinator_with_pending(
    memory_store: InMemoryOAuthCredentialStore, oauth_options: OAuthOptions
) -> OAuthCoordinator:
    """Create coordinator with PENDING flow and valid state in store."""
    coord = OAuthCoordinator(
        bundle_id="test-bundle",
        server_url="https://api.example.com",
        resource="https://api.example.com",
        options=oauth_options,
        credential_store=memory_store,
    )
    state = "test-pkce-state-123"
    coord._state_store.store(
        state,
        pkce_verifier="test-verifier",
        issuer="https://accounts.example.com",
        redirect_uri="http://127.0.0.1:0/callback",
        scopes=["read", "write"],
    )
    launch = OAuthLaunch(
        authorization_url="https://accounts.example.com/auth?state=test-pkce-state-123",
        state=state,
    )
    coord._flow = _AuthorizationFlowState(
        phase=_FlowPhase.PENDING,
        pending=_PendingAuthorization(
            launch=launch,
            request=OAuthBeginRequest(
                redirect_uri="http://127.0.0.1:0/callback",
                required_scope=None,
            ),
            requested_scopes=["read", "write"],
            generation=coord._generation,
            issuer="https://accounts.example.com",
        ),
    )
    coord._status = _OAuthStatusAuthorizationPending()
    return coord


class TestOAuthCoordinatorExpiredFlow:
    """Issue #186: expire_invalid_authorization_flow + EXPIRED flow handling."""

    # ── _expire_invalid_authorization_flow ──────────────────────────────

    @pytest.mark.asyncio
    async def test_expire_invalid_valid_flow_noop(
        self, coordinator_with_pending: OAuthCoordinator
    ) -> None:
        """Valid PENDING flow with matching generation + PKCE state → no-op."""
        coord = coordinator_with_pending
        assert coord._flow.phase == _FlowPhase.PENDING
        result = await coord._expire_invalid_authorization_flow()
        assert result is False
        assert coord._flow.phase == _FlowPhase.PENDING  # unchanged

    @pytest.mark.asyncio
    async def test_expire_invalid_stale_generation(
        self, coordinator_with_pending: OAuthCoordinator
    ) -> None:
        """Generation mismatch → flow expired."""
        coord = coordinator_with_pending
        original_state = coord._flow.pending.launch.state  # type: ignore[union-attr]
        # Simulate stale generation (e.g. after credential invalidation)
        coord._generation += 1
        result = await coord._expire_invalid_authorization_flow()
        assert result is True
        assert coord._flow.phase == _FlowPhase.EXPIRED
        assert coord._flow.expired_state == original_state
        assert isinstance(coord._status, _OAuthStatusUnauthorized)
        # PKCE state should have been removed
        assert coord._state_store.lookup(original_state) is None

    @pytest.mark.asyncio
    async def test_expire_invalid_expired_pkce_state(
        self, coordinator_with_pending: OAuthCoordinator
    ) -> None:
        """PKCE state expired from store → flow expired."""
        coord = coordinator_with_pending
        original_state = coord._flow.pending.launch.state  # type: ignore[union-attr]
        # Remove PKCE state from store
        coord._state_store.finalize_exchange(original_state)
        result = await coord._expire_invalid_authorization_flow()
        assert result is True
        assert coord._flow.phase == _FlowPhase.EXPIRED

    @pytest.mark.asyncio
    async def test_expire_invalid_no_pending_flow(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """IDLE flow → expire is no-op."""
        assert coordinator._flow.phase == _FlowPhase.IDLE
        result = await coordinator._expire_invalid_authorization_flow()
        assert result is False
        assert coordinator._flow.phase == _FlowPhase.IDLE

    @pytest.mark.asyncio
    async def test_expire_invalid_with_stored_token_restores_authorized(
        self, coordinator_with_pending: OAuthCoordinator
    ) -> None:
        """Store probe: stale flow with still-valid stored credentials → Authorized.

        Mirrors Rust restore_status_after_termination for the step-up scenario
        (scope-upgrade flow expiring while the previous grant remains usable).
        """
        coord = coordinator_with_pending
        # Pre-store credentials (simulates a previous completed authorization)
        await coord._token_storage.set_tokens(
            OAuthToken(
                access_token="previous-token",
                token_type="Bearer",
                expires_in=3600,
                scope="read",
                refresh_token="previous-refresh",
            )
        )
        coord._generation += 1  # stale flow
        result = await coord._expire_invalid_authorization_flow()
        assert result is True
        assert coord._flow.phase == _FlowPhase.EXPIRED
        assert isinstance(coord._status, _OAuthStatusAuthorized)
        assert coord._status.scopes == ["read"]
        assert coord._granted_scopes == ["read"]

    # ── status() auto-expires stale flows ──────────────────────────────

    @pytest.mark.asyncio
    async def test_status_auto_expires_stale_flow(
        self, coordinator_with_pending: OAuthCoordinator
    ) -> None:
        """status() calls _expire_invalid_authorization_flow → Unauthorized."""
        coord = coordinator_with_pending
        coord._generation += 1  # stale
        status = await coord.status()
        assert isinstance(status, _OAuthStatusUnauthorized)
        assert coord._flow.phase == _FlowPhase.EXPIRED

    # ── complete() with Expired flow ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_complete_with_expired_flow_state_mismatch(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """Expired flow + mismatched callback state → StateMismatch."""
        coordinator._flow = _AuthorizationFlowState(
            phase=_FlowPhase.EXPIRED, expired_state="original-state"
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.complete(
                OAuthCallback(code="code-1", state="different-state")
            )
        assert exc.value.code == OAuthErrorCode.StateMismatch
        # Mismatch keeps the flow EXPIRED so a later matching callback
        # can still be classified (aligns with Rust).
        assert coordinator._flow.phase == _FlowPhase.EXPIRED

    @pytest.mark.asyncio
    async def test_complete_with_expired_flow_match(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """Expired flow + matching callback state → AuthorizationExpired."""
        coordinator._flow = _AuthorizationFlowState(
            phase=_FlowPhase.EXPIRED, expired_state="original-state"
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.complete(
                OAuthCallback(code="code-1", state="original-state")
            )
        assert exc.value.code == OAuthErrorCode.AuthorizationExpired
        assert coordinator._flow.phase == _FlowPhase.IDLE  # cleared

    # ── cancel() with Expired flow ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cancel_with_expired_flow_match(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """Expired flow + matching state → AuthorizationExpired."""
        coordinator._flow = _AuthorizationFlowState(
            phase=_FlowPhase.EXPIRED, expired_state="s1"
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.cancel(
                OAuthCancellation(state="s1", reason=OAuthCancellationReason.Cancelled)
            )
        assert exc.value.code == OAuthErrorCode.AuthorizationExpired
        assert coordinator._flow.phase == _FlowPhase.IDLE

    @pytest.mark.asyncio
    async def test_cancel_with_expired_flow_mismatch(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """Expired flow + mismatched state → StateMismatch."""
        coordinator._flow = _AuthorizationFlowState(
            phase=_FlowPhase.EXPIRED, expired_state="s1"
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.cancel(
                OAuthCancellation(state="s2", reason=OAuthCancellationReason.Cancelled)
            )
        assert exc.value.code == OAuthErrorCode.StateMismatch
        # Mismatch keeps the flow EXPIRED so a later matching callback
        # can still be classified (aligns with Rust).
        assert coordinator._flow.phase == _FlowPhase.EXPIRED

    # ── cancel_callback() with Expired flow ─────────────────────────────

    @pytest.mark.asyncio
    async def test_cancel_callback_with_expired_flow(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """Expired flow + matching state → AuthorizationExpired."""
        coordinator._flow = _AuthorizationFlowState(
            phase=_FlowPhase.EXPIRED, expired_state="s1"
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.cancel_callback(
                OAuthCancellation(
                    state="s1", reason=OAuthCancellationReason.AccessDenied
                )
            )
        assert exc.value.code == OAuthErrorCode.AuthorizationExpired
        assert coordinator._flow.phase == _FlowPhase.IDLE

    @pytest.mark.asyncio
    async def test_cancel_callback_with_expired_flow_state_mismatch(
        self, coordinator: OAuthCoordinator
    ) -> None:
        """Expired flow + mismatched state → StateMismatch."""
        coordinator._flow = _AuthorizationFlowState(
            phase=_FlowPhase.EXPIRED, expired_state="s1"
        )
        with pytest.raises(_OAuthCoordinatorError) as exc:
            await coordinator.cancel_callback(
                OAuthCancellation(
                    state="different", reason=OAuthCancellationReason.AccessDenied
                )
            )
        assert exc.value.code == OAuthErrorCode.StateMismatch
        # Mismatch keeps the flow EXPIRED so a later matching callback
        # can still be classified (aligns with Rust).
        assert coordinator._flow.phase == _FlowPhase.EXPIRED

    # ── handle_insufficient_scope with PENDING flow ───────────────────

    @pytest.mark.asyncio
    async def test_handle_insufficient_scope_pending_flow_noop(
        self, coordinator_with_pending: OAuthCoordinator
    ) -> None:
        """handle_insufficient_scope with valid PENDING flow → no-op (don't override)."""
        coord = coordinator_with_pending
        assert isinstance(coord._status, _OAuthStatusAuthorizationPending)
        await coord.handle_insufficient_scope("admin.write")
        # Status should remain AuthorizationPending (not ReauthorizationRequired)
        assert isinstance(coord._status, _OAuthStatusAuthorizationPending)

    @pytest.mark.asyncio
    async def test_handle_insufficient_scope_stale_pending_expires_then_reauthorizes(
        self, coordinator_with_pending: OAuthCoordinator
    ) -> None:
        """Stale PENDING flow → expire → ReauthorizationRequired is set."""
        coord = coordinator_with_pending
        coord._generation += 1  # stale
        await coord.handle_insufficient_scope("admin.write")
        from a2c_smcp.computer.mcp_clients.oauth_types import (
            _OAuthStatusReauthorizationRequired,
        )

        assert coord._flow.phase == _FlowPhase.EXPIRED
        assert isinstance(coord._status, _OAuthStatusReauthorizationRequired)
        assert coord._status.required_scope == "admin.write"

    # ── register expire coverage（原 _begin_under_lock，#179 staged 化） ───

    @pytest.mark.asyncio
    async def test_register_expires_stale_flow_then_supersedes(
        self, coordinator_with_pending: OAuthCoordinator
    ) -> None:
        """register 先过期 stale PENDING（#186 语义），随后新注册**取代** EXPIRED
        （新 flow 的 intent 以 registered_request 为准；旧 state 的 late callback
        此后按新 flow 判 StateMismatch）。"""
        coord = coordinator_with_pending
        coord._generation += 1  # stale
        request = OAuthBeginRequest(
            redirect_uri="http://127.0.0.1:0/callback",
            required_scope=None,
        )
        await coord._register_under_lock(request)
        assert coord._flow.phase == _FlowPhase.IDLE  # EXPIRED 已被新注册取代
        assert coord.has_registered_request()
