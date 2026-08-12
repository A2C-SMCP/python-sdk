# -*- coding: utf-8 -*-
# filename: oauth_coordinator_sync.py
# @Time    : 2026/08/11
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
同步 OAuthCoordinator 镜像，内部用 asyncio 线程桥接运行 async OAuthCoordinator。

模式对齐 :class:`a2c_smcp.agent.sync_client.SMCPAgentClient`（``new_event_loop`` +
``run_until_complete``）。调用方须注意：CLI / REPL 等单线程场景下可用，多线程环境
需自备同步（``SyncOAuthCoordinator`` 本身非线程安全）。

协议归属：SDK 层（不涉及 A2C-SMCP 协议变更）。
父 Epic：#176；本 Sub：#178（Auth Code + PKCE + DCR 流程）。
"""
from __future__ import annotations

import asyncio
import atexit
from collections.abc import Callable
from typing import TYPE_CHECKING

from a2c_smcp.computer.mcp_clients.oauth_coordinator import OAuthCoordinator
from a2c_smcp.computer.mcp_clients.oauth_credential_store import OAuthCredentialStore
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthOptions,
)

if TYPE_CHECKING:
    from mcp.client.auth import OAuthClientProvider

    from a2c_smcp.computer.mcp_clients.oauth_types import (
        OAuthFlowOutcome,
        OAuthLaunch,
        OAuthStatus,
    )


class SyncOAuthCoordinator:
    """Synchronous mirror of :class:`OAuthCoordinator`.

    Wraps an async ``OAuthCoordinator`` instance, running async methods on a
    dedicated event loop. Sync methods (``needs_oauth_provider``,
    ``build_oauth_provider``) are delegated directly.

    Not thread-safe — caller must serialize access.
    """

    def __init__(
        self,
        *,
        bundle_id: str,
        server_url: str,
        resource: str,
        options: OAuthOptions,
        credential_store: OAuthCredentialStore,
        redirect_handler: Callable[[str], None] | None = None,
        callback_handler: Callable[[], tuple[str, str | None]] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._bundle_id = bundle_id
        self._server_url = server_url
        self._resource = resource
        self._options = options
        self._credential_store = credential_store
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async: OAuthCoordinator | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            atexit.register(self._cleanup_loop)
        return self._loop

    def _cleanup_loop(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()

    def close(self) -> None:
        """显式关闭，注销 atexit handler / Explicit close, unregister atexit handler.

        调用此方法后实例不可再用。若实例被 GC 前未调用 close()，
        atexit handler 仍作为 fallback 清理 loop。
        """
        if self._async is not None:
            self._async = None
        if self._loop is not None:
            try:
                atexit.unregister(self._cleanup_loop)
            except Exception:
                pass  # not registered (already called? never registered?)
            self._cleanup_loop()
            self._loop = None

    def _ensure_async(self) -> OAuthCoordinator:
        if self._async is None:
            # Wrap sync callbacks as coroutines
            async_redirect = None
            if self._redirect_handler is not None:
                sync_redir = self._redirect_handler

                async def _redir(url: str) -> None:
                    sync_redir(url)

                async_redirect = _redir

            async_callback = None
            if self._callback_handler is not None:
                sync_cb = self._callback_handler

                async def _cb() -> tuple[str, str | None]:
                    return sync_cb()

                async_callback = _cb

            self._async = OAuthCoordinator(
                bundle_id=self._bundle_id,
                server_url=self._server_url,
                resource=self._resource,
                options=self._options,
                credential_store=self._credential_store,
                redirect_handler=async_redirect,  # type: ignore[arg-type]
                callback_handler=async_callback,  # type: ignore[arg-type]
                timeout=self._timeout,
            )
        return self._async

    # -- Public API (mirrors OAuthCoordinator) --------------------------------

    # Async → sync bridge methods
    def status(self) -> OAuthStatus:
        return self._ensure_loop().run_until_complete(self._ensure_async().status())

    def begin(self, request: OAuthBeginRequest) -> OAuthLaunch:
        return self._ensure_loop().run_until_complete(self._ensure_async().begin(request))

    def complete(self, callback: OAuthCallback) -> OAuthFlowOutcome:
        return self._ensure_loop().run_until_complete(self._ensure_async().complete(callback))

    def cancel(self, cancellation: OAuthCancellation) -> OAuthFlowOutcome:
        return self._ensure_loop().run_until_complete(self._ensure_async().cancel(cancellation))

    def cancel_callback(self, cancellation: OAuthCancellation) -> OAuthFlowOutcome:
        return self._ensure_loop().run_until_complete(self._ensure_async().cancel_callback(cancellation))

    def restore_credentials(self) -> OAuthStatus:
        return self._ensure_loop().run_until_complete(self._ensure_async().restore_credentials())

    def handle_insufficient_scope(self, required_scope: str) -> None:
        self._ensure_loop().run_until_complete(
            self._ensure_async().handle_insufficient_scope(required_scope)
        )

    def observe_service_success(self) -> None:
        self._ensure_loop().run_until_complete(self._ensure_async().observe_service_success())

    def observe_service_error(self, status_code: int, www_authenticate: str | None) -> bool:
        return self._ensure_loop().run_until_complete(
            self._ensure_async().observe_service_error(status_code, www_authenticate)
        )

    def invalidate_credentials(self) -> None:
        self._ensure_loop().run_until_complete(self._ensure_async().invalidate_credentials())

    # Sync delegation (these are sync on OAuthCoordinator)
    def needs_oauth_provider(self) -> bool:
        return self._ensure_async().needs_oauth_provider()

    def build_oauth_provider(self) -> OAuthClientProvider:
        return self._ensure_async().build_oauth_provider()


__all__ = ["SyncOAuthCoordinator"]
