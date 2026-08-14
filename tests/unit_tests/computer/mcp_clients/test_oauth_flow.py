# -*- coding: utf-8 -*-
# filename: test_oauth_flow.py
# @Time    : 2026/08/13
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
OAuthFlow handle 测试（#179）：构造无 I/O、launch 等 401 触发、complete/cancel/
cancel_callback 全 Rust 对齐、repr 脱敏、cancel-before-launch 语义。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from a2c_smcp.computer.mcp_clients.oauth_coordinator import OAuthCoordinator
from a2c_smcp.computer.mcp_clients.oauth_credential_store import InMemoryOAuthCredentialStore
from a2c_smcp.computer.mcp_clients.oauth_flow import OAuthFlow
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
    OAuthError,
    OAuthErrorCode,
    OAuthOptions,
    _OAuthModeAuthCodeDynamic,
)

# ============================================================================
# Fixtures
# ============================================================================


class _FakeManager:
    """最小 manager 桩：OAuthFlow 只依赖这三个接缝（Phase 3 由真 manager 实现）。"""

    def __init__(self, coordinator: OAuthCoordinator | None) -> None:
        self._oauth_coordinators: dict[str, OAuthCoordinator] = {}
        if coordinator is not None:
            self._oauth_coordinators["b1"] = coordinator
        self.kicked: list[str] = []
        self.discarded: list[str] = []

    async def _ensure_oauth_coordinator(self, bundle_id: str) -> OAuthCoordinator:
        coordinator = self._oauth_coordinators.get(bundle_id)
        if coordinator is None:
            raise OAuthError(OAuthErrorCode.NotConfigured, "OAuth has not been admitted for this server")
        return coordinator

    def _ensure_oauth_connect_task(self, bundle_id: str, coordinator: OAuthCoordinator) -> None:
        self.kicked.append(bundle_id)

    def _discard_oauth_flow(self, bundle_id: str) -> None:
        self.discarded.append(bundle_id)


@pytest.fixture
def oauth_options() -> OAuthOptions:
    return OAuthOptions(
        mode=_OAuthModeAuthCodeDynamic(),
        scopes=["read", "write"],
        client_name="test-client",
    )


@pytest.fixture
def coordinator(oauth_options: OAuthOptions) -> OAuthCoordinator:
    return OAuthCoordinator(
        bundle_id="b1",
        server_url="https://api.example.com",
        resource="https://api.example.com",
        options=oauth_options,
        credential_store=InMemoryOAuthCredentialStore(),
    )


@pytest.fixture
def begin_request() -> OAuthBeginRequest:
    return OAuthBeginRequest(redirect_uri="https://host.example/callback")


def _make_flow(manager: Any, req: OAuthBeginRequest) -> OAuthFlow:
    return OAuthFlow(manager=manager, bundle_id="b1", request=req)


async def _drive_redirect(coordinator: OAuthCoordinator, state: str = "st1") -> None:
    """直驱 redirect_handler（模拟 401 challenge 触发 provider 发布 URL）。"""
    handler = coordinator._make_redirect_handler()
    await handler(
        "https://auth.example/authorize?state="
        + state
        + "&redirect_uri=https%3A%2F%2Fhost.example%2Fcallback"
    )


# ============================================================================
# Tests
# ============================================================================


class TestOAuthFlowHandle:
    def test_repr_redacts_redirect_uri(self, begin_request: OAuthBeginRequest) -> None:
        flow = _make_flow(_FakeManager(None), begin_request)
        text = repr(flow)
        assert "b1" in text
        assert begin_request.redirect_uri not in text  # 宿主 callback 不入 repr/日志

    @pytest.mark.asyncio
    async def test_complete_without_coordinator_state_mismatch(
        self, begin_request: OAuthBeginRequest
    ) -> None:
        flow = _make_flow(_FakeManager(None), begin_request)
        with pytest.raises(OAuthError) as exc:
            await flow.complete(OAuthCallback(code="c1", state="st1"))
        assert exc.value.code == OAuthErrorCode.StateMismatch

    @pytest.mark.asyncio
    async def test_cancel_invalid_reason(self, begin_request: OAuthBeginRequest) -> None:
        flow = _make_flow(_FakeManager(None), begin_request)
        with pytest.raises(OAuthError) as exc:
            await flow.cancel(OAuthCancellationReason.AccessDenied)
        assert exc.value.code == OAuthErrorCode.InvalidCancellationReason

    @pytest.mark.asyncio
    async def test_cancel_before_launch_terminates_and_discards(
        self, begin_request: OAuthBeginRequest
    ) -> None:
        """pre-admission / pre-launch cancel → Terminated(Cancelled)，并从 manager 撤下 handle。"""
        manager = _FakeManager(None)
        flow = _make_flow(manager, begin_request)
        outcome = await flow.cancel(OAuthCancellationReason.Cancelled)
        assert outcome.outcome == "terminated"
        assert outcome.reason == OAuthCancellationReason.Cancelled
        assert manager.discarded == ["b1"]

    @pytest.mark.asyncio
    async def test_cancel_pending_after_launch(
        self, coordinator: OAuthCoordinator, begin_request: OAuthBeginRequest
    ) -> None:
        """launch 后 cancel(Cancelled)：状态由 SDK 从 pending flow 内部解析。"""
        await coordinator.register(begin_request)
        await _drive_redirect(coordinator)
        flow = _make_flow(_FakeManager(coordinator), begin_request)
        outcome = await flow.cancel(OAuthCancellationReason.Cancelled)
        assert outcome.outcome == "terminated"
        assert outcome.reason == OAuthCancellationReason.Cancelled

    @pytest.mark.asyncio
    async def test_cancel_callback_delegates_to_coordinator(
        self, coordinator: OAuthCoordinator, begin_request: OAuthBeginRequest
    ) -> None:
        await coordinator.register(begin_request)
        await _drive_redirect(coordinator)
        flow = _make_flow(_FakeManager(coordinator), begin_request)
        outcome = await flow.cancel_callback(
            OAuthCancellation(state="st1", reason=OAuthCancellationReason.AccessDenied)
        )
        assert outcome.outcome == "terminated"
        assert outcome.reason == OAuthCancellationReason.AccessDenied

    @pytest.mark.asyncio
    async def test_cancel_compat_dispatch(
        self, coordinator: OAuthCoordinator, begin_request: OAuthBeginRequest
    ) -> None:
        """_cancel_compat：Cancelled/Timeout → cancel 路径；AccessDenied/AuthorizationError → cancel_callback。"""
        await coordinator.register(begin_request)
        await _drive_redirect(coordinator, state="stA")
        flow = _make_flow(_FakeManager(coordinator), begin_request)
        outcome = await flow._cancel_compat(
            OAuthCancellation(state="stA", reason=OAuthCancellationReason.Timeout)
        )
        assert outcome.outcome == "terminated"
        assert outcome.reason == OAuthCancellationReason.Timeout

        # 第二个 flow 走 provider 路径
        await coordinator.register(begin_request)
        await _drive_redirect(coordinator, state="stB")
        outcome = await flow._cancel_compat(
            OAuthCancellation(state="stB", reason=OAuthCancellationReason.AuthorizationError)
        )
        assert outcome.outcome == "terminated"
        assert outcome.reason == OAuthCancellationReason.AuthorizationError

    @pytest.mark.asyncio
    async def test_launch_returns_url_and_kicks_connect(
        self, coordinator: OAuthCoordinator, begin_request: OAuthBeginRequest
    ) -> None:
        """launch()：确保 coordinator → register → 确保 connect 任务在途 → 等 URL。"""
        manager = _FakeManager(coordinator)
        flow = _make_flow(manager, begin_request)
        launch_task = asyncio.create_task(flow.launch())
        await asyncio.sleep(0)  # 让 launch 内 register 先落地
        await _drive_redirect(coordinator)
        launch = await asyncio.wait_for(launch_task, timeout=5)
        assert launch.state == "st1"
        assert manager.kicked == ["b1"]  # connect 尝试已确保在途

    @pytest.mark.asyncio
    async def test_stale_handle_cannot_cancel_new_flow(
        self, coordinator: OAuthCoordinator, begin_request: OAuthBeginRequest
    ) -> None:
        """🟡2 handle 代际绑定：旧 flow 的 handle 不得取消新 flow。"""
        manager = _FakeManager(coordinator)
        flow_a = _make_flow(manager, begin_request)
        launch_a_task = asyncio.create_task(flow_a.launch())
        await asyncio.sleep(0)  # 让 register 落地（wait_launch 等待中）
        await _drive_redirect(coordinator, state="stA")
        await asyncio.wait_for(launch_a_task, timeout=5)
        # flow A 终态（cancel）
        outcome = await flow_a.cancel(OAuthCancellationReason.Cancelled)
        assert outcome.outcome == "terminated"

        # flow B（同请求、新代际）注册并 PENDING
        flow_b = _make_flow(manager, begin_request)
        launch_b_task = asyncio.create_task(flow_b.launch())
        await asyncio.sleep(0)
        await _drive_redirect(coordinator, state="stB")
        await asyncio.wait_for(launch_b_task, timeout=5)

        # 旧 handle A cancel → StateMismatch（stale handle），flow B 存活
        with pytest.raises(OAuthError) as exc:
            await flow_a.cancel(OAuthCancellationReason.Cancelled)
        assert exc.value.code == OAuthErrorCode.StateMismatch
        assert coordinator._flow.phase.name == "PENDING"  # flow B 未被误杀

    @pytest.mark.asyncio
    async def test_complete_after_launch(
        self, coordinator: OAuthCoordinator, begin_request: OAuthBeginRequest
    ) -> None:
        """handle.complete 委托 coordinator.complete（全链在 Phase 3 fake AS 中覆盖）。"""
        await coordinator.register(begin_request)
        await _drive_redirect(coordinator)
        flow = _make_flow(_FakeManager(coordinator), begin_request)
        complete_task = asyncio.create_task(flow.complete(OAuthCallback(code="c1", state="st1")))
        await asyncio.sleep(0)
        from mcp.shared.auth import OAuthToken

        await coordinator._token_storage.set_tokens(
            OAuthToken(access_token="at1", token_type="Bearer", scope="read write")
        )
        outcome = await asyncio.wait_for(complete_task, timeout=5)
        assert outcome.outcome == "authorized"
