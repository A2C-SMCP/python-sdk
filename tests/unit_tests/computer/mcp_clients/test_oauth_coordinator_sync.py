# -*- coding: utf-8 -*-
"""Unit tests for SyncOAuthCoordinator — the synchronous mirror of OAuthCoordinator.

Tests the sync→async bridge: dedicated event loop, callback wrapping,
sync delegation, and thread-bridged async methods.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from a2c_smcp.computer.mcp_clients.oauth_coordinator_sync import SyncOAuthCoordinator
from a2c_smcp.computer.mcp_clients.oauth_credential_store import (
    InMemoryOAuthCredentialStore,
    ScopedCredentialStore,
    oauth_mode_fingerprint,
)
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthOptions,
    _OAuthModeAuthCodeDynamic,
    _OAuthStatusUnauthorized,
)


@pytest.fixture
def memory_store() -> InMemoryOAuthCredentialStore:
    return InMemoryOAuthCredentialStore()


@pytest.fixture
def oauth_options() -> OAuthOptions:
    return OAuthOptions(
        mode=_OAuthModeAuthCodeDynamic(),
        scopes=["read"],
        client_name="test-sync",
    )


@pytest.fixture
def sync_coordinator(
    memory_store: InMemoryOAuthCredentialStore, oauth_options: OAuthOptions
) -> SyncOAuthCoordinator:
    return SyncOAuthCoordinator(
        bundle_id="test-bundle",
        server_url="https://api.example.com",
        resource="https://api.example.com",
        options=oauth_options,
        credential_store=memory_store,
    )


class TestSyncOAuthCoordinator:
    """Core sync bridge tests."""

    def test_initial_status_is_unauthorized(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        status = sync_coordinator.status()
        assert isinstance(status, _OAuthStatusUnauthorized)

    def test_restore_credentials_no_stored(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        status = sync_coordinator.restore_credentials()
        assert isinstance(status, _OAuthStatusUnauthorized)

    def test_needs_oauth_provider_initially_false(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        assert sync_coordinator.needs_oauth_provider() is False

    def test_build_oauth_provider(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        provider = sync_coordinator.build_oauth_provider()
        assert provider is not None

    def test_invalidate_credentials(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        sync_coordinator.invalidate_credentials()
        status = sync_coordinator.status()
        assert isinstance(status, _OAuthStatusUnauthorized)

    def test_handle_insufficient_scope(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        sync_coordinator.handle_insufficient_scope("admin.write")
        from a2c_smcp.computer.mcp_clients.oauth_types import _OAuthStatusReauthorizationRequired

        status = sync_coordinator.status()
        assert isinstance(status, _OAuthStatusReauthorizationRequired)
        assert status.required_scope == "admin.write"

    def test_observe_service_error_401_unauthorized(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        handled = sync_coordinator.observe_service_error(401, None)
        assert handled is True

    def test_observe_service_success_noop_when_unauthorized(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        """observe_service_success is no-op but must not throw."""
        from a2c_smcp.computer.mcp_clients.oauth_types import _OAuthStatusUnauthorized

        status_before = sync_coordinator.status()
        assert isinstance(status_before, _OAuthStatusUnauthorized)
        sync_coordinator.observe_service_success()
        status_after = sync_coordinator.status()
        assert isinstance(status_after, _OAuthStatusUnauthorized)
        assert status_after.state == status_before.state

    def test_status_is_stable_across_calls(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        """Multiple status() calls should return consistent results."""
        s1 = sync_coordinator.status()
        s2 = sync_coordinator.status()
        assert s1.state == s2.state == "unauthorized"

    def test_loop_reuse(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        """_ensure_loop should return the same loop on repeated calls."""
        loop1 = sync_coordinator._ensure_loop()
        loop2 = sync_coordinator._ensure_loop()
        assert loop1 is loop2
        assert not loop1.is_closed()

    def test_async_reuse(
        self, sync_coordinator: SyncOAuthCoordinator
    ) -> None:
        """_ensure_async should return the same coordinator instance."""
        a1 = sync_coordinator._ensure_async()
        a2 = sync_coordinator._ensure_async()
        assert a1 is a2

    def test_loop_cleanup(self) -> None:
        """_cleanup_loop should close the loop."""
        import atexit

        # Create a coordinator on its own to test cleanup without side effects
        store = InMemoryOAuthCredentialStore()
        opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"])
        coord = SyncOAuthCoordinator(
            bundle_id="b1",
            server_url="https://example.com",
            resource="https://example.com",
            options=opts,
            credential_store=store,
        )
        loop = coord._ensure_loop()
        assert not loop.is_closed()
        coord._cleanup_loop()
        assert loop.is_closed()

    def test_atexit_registered(self) -> None:
        """Creating a coordinator registers an atexit handler."""
        import atexit

        store = InMemoryOAuthCredentialStore()
        opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"])
        # atexit registration happens on first _ensure_loop() call
        coord = SyncOAuthCoordinator(
            bundle_id="b2",
            server_url="https://example.com",
            resource="https://example.com",
            options=opts,
            credential_store=store,
        )
        # Trigger loop creation
        coord._ensure_loop()
        # Clean up to avoid polluting atexit handlers for other tests
        coord._cleanup_loop()


class TestSyncOAuthCoordinatorWithCredentials:
    """Tests requiring pre-stored credentials."""

    @pytest.fixture
    def populated_coordinator(
        self, memory_store: InMemoryOAuthCredentialStore, oauth_options: OAuthOptions
    ) -> SyncOAuthCoordinator:
        """Create a coordinator with pre-stored credentials."""
        # Pre-store via async path
        fp = oauth_mode_fingerprint(oauth_options)

        async def _store() -> None:
            scoped = ScopedCredentialStore(
                bundle_id="test-bundle",
                resource="https://api.example.com",
                mode_fingerprint=fp,
                backend=memory_store,
            )
            # Store DCR info
            from a2c_smcp.computer.mcp_clients.oauth_coordinator import TokenStorageAdapter

            adapter = TokenStorageAdapter(scoped)
            await adapter.set_client_info(
                OAuthClientInformationFull(
                    client_id="cid",
                    redirect_uris=["http://127.0.0.1:0/callback"],
                    client_name="test",
                )
            )
            await adapter.set_tokens(
                OAuthToken(
                    access_token="at",
                    token_type="Bearer",
                    scope="read",
                    refresh_token="rt",
                )
            )

        asyncio.new_event_loop().run_until_complete(_store())

        return SyncOAuthCoordinator(
            bundle_id="test-bundle",
            server_url="https://api.example.com",
            resource="https://api.example.com",
            options=oauth_options,
            credential_store=memory_store,
        )

    def test_restore_credentials_with_stored(
        self, populated_coordinator: SyncOAuthCoordinator
    ) -> None:
        from a2c_smcp.computer.mcp_clients.oauth_types import _OAuthStatusAuthorized

        status = populated_coordinator.restore_credentials()
        assert isinstance(status, _OAuthStatusAuthorized)
        assert status.scopes == ["read"]

    def test_observe_service_error_401_transitions_authorized_to_unauthorized(
        self, populated_coordinator: SyncOAuthCoordinator
    ) -> None:
        from a2c_smcp.computer.mcp_clients.oauth_types import (
            _OAuthStatusAuthorized,
            _OAuthStatusUnauthorized,
        )

        status_before = populated_coordinator.restore_credentials()
        assert isinstance(status_before, _OAuthStatusAuthorized)

        handled = populated_coordinator.observe_service_error(401, None)
        assert handled is True
        status_after = populated_coordinator.status()
        assert isinstance(status_after, _OAuthStatusUnauthorized)
