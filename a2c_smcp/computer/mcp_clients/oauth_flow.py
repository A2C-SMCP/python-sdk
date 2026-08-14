# -*- coding: utf-8 -*-
# filename: oauth_flow.py
# @Time    : 2026/08/13
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
OAuthFlow handle（#179）：宿主持有的单次交互授权句柄，对齐 rust-sdk ``oauth/flow.rs``。

构造**无 I/O**；``launch()`` 才触发 discovery → DCR → PKCE → 授权 URL 生成
（Python 为 transport-coupled：URL 由 401 challenge 触发的 mcp inline auth 流程
产生，launch 确保一次带 provider 的 connect 尝试在途并等 URL）。终态原子性由
:class:`~a2c_smcp.computer.mcp_clients.oauth_coordinator.OAuthCoordinator` 的 flow
state + futures 在锁下保证，handle 自身不再加状态机。

协议归属：SDK 层。父 Epic：#176；本 Sub：#179（公共 facade + 宿主契约）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
    OAuthError,
    OAuthErrorCode,
    OAuthFlowOutcome,
    OAuthLaunch,
    _OAuthOutcomeTerminated,
    _OAuthStatusUnauthorized,
)

if TYPE_CHECKING:
    from a2c_smcp.computer.mcp_clients.oauth_coordinator import OAuthCoordinator


class OAuthFlow:
    """Host-owned handle for one interactive OAuth flow（Rust ``OAuthFlow`` parity）。

    相同 ``create_oauth_flow`` 请求返回**同一 handle**（manager 注册表 dedup），
    宿主无需自行管理「克隆」；不同请求 → ``AuthorizationAlreadyPending``。
    """

    def __init__(self, manager: Any, bundle_id: str, request: OAuthBeginRequest) -> None:
        self._manager = manager
        self._bundle_id = bundle_id
        self._request = request
        # 代际绑定（🟡2）：launch 记录本次注册 mint 的 generation——cancel 携带校验，
        # 旧 flow 的 stale handle 不得取消新 flow（Rust handle 绑定自身状态）。
        self._generation: int | None = None

    async def launch(self) -> OAuthLaunch:
        """确保 coordinator 已准入 → 注册本 flow → 确保 connect 尝试在途 → 等授权 URL。

        Discovery / client setup / PKCE / CSRF state 由 401 challenge 触发的
        provider 流程在 URL 发布前完成（Rust ``launch()`` 的同构面）。SDK 全程
        不打开浏览器、不绑端口、不等待回调。
        """
        coordinator = cast(
            "OAuthCoordinator",
            await self._manager._ensure_oauth_coordinator(self._bundle_id),
        )
        await coordinator.register(self._request)
        self._generation = coordinator.current_generation()
        self._manager._ensure_oauth_connect_task(self._bundle_id, coordinator)
        return await coordinator.wait_launch()

    async def complete(self, callback: OAuthCallback) -> OAuthFlowOutcome:
        """提交宿主浏览器回调（code + state + 可选 issuer），等待终态结果。

        Raises:
            OAuthError: 无 pending flow（含未准入）→ ``StateMismatch``；其余由
                coordinator 按 Rust 语义抛（``AuthorizationExpired`` / ``IssuerMismatch``）。
        """
        coordinator = await self._coordinator()
        return await coordinator.complete(callback)

    async def cancel(self, reason: OAuthCancellationReason) -> OAuthFlowOutcome:
        """宿主生命周期取消（仅 ``Cancelled`` / ``Timeout``）。

        状态由 SDK 从 pending flow 内部解析（Rust ``OAuthFlow::cancel`` 语义，宿主
        无需持有 state）。launch 之前取消（未准入 / 未 challenge）→ 撤下 handle 并
        返回 ``Terminated(Cancelled)``。
        """
        if reason not in (
            OAuthCancellationReason.Cancelled,
            OAuthCancellationReason.Timeout,
        ):
            raise OAuthError(
                OAuthErrorCode.InvalidCancellationReason,
                f"Host cancellation only accepts Cancelled/Timeout, got {reason}",
            )
        coordinator = await self._try_coordinator()
        if coordinator is None:
            # pre-admission cancel：flow 从未起 I/O，撤下注册 handle 即终态
            self._manager._discard_oauth_flow(self._bundle_id)
            return _OAuthOutcomeTerminated(
                reason=reason,
                status=_OAuthStatusUnauthorized(),
            )
        return await coordinator.cancel_pending(
            reason,
            expected_generation=self._generation,
            expected_request=self._request,
        )

    async def cancel_callback(self, cancellation: OAuthCancellation) -> OAuthFlowOutcome:
        """提交 provider OAuth 错误回调（仅 ``AccessDenied`` / ``AuthorizationError``）。"""
        coordinator = await self._coordinator()
        return await coordinator.cancel_callback(cancellation)

    async def _cancel_compat(self, cancellation: OAuthCancellation) -> OAuthFlowOutcome:
        """Rust ``cancel_compat`` 按 reason 分派：Cancelled/Timeout → 宿主取消路径；
        其余（AccessDenied/AuthorizationError）→ provider 回调路径。manager.cancel_oauth 用。
        """
        if cancellation.reason in (
            OAuthCancellationReason.Cancelled,
            OAuthCancellationReason.Timeout,
        ):
            coordinator = await self._try_coordinator()
            if coordinator is None:
                self._manager._discard_oauth_flow(self._bundle_id)
                return _OAuthOutcomeTerminated(
                    reason=cancellation.reason,
                    status=_OAuthStatusUnauthorized(),
                )
            return await coordinator.cancel(cancellation)
        return await (await self._coordinator()).cancel_callback(cancellation)

    # -- 内部 -----------------------------------------------------------------

    async def _try_coordinator(self) -> OAuthCoordinator | None:
        """当前 bundle 的 coordinator（无则 None，不抛）。"""
        return cast(
            "OAuthCoordinator | None",
            self._manager._oauth_coordinators.get(self._bundle_id),
        )

    async def _coordinator(self) -> OAuthCoordinator:
        coordinator = await self._try_coordinator()
        if coordinator is None:
            raise OAuthError(
                OAuthErrorCode.StateMismatch,
                "No pending authorization flow",
            )
        return coordinator

    def __repr__(self) -> str:
        """脱敏 repr：仅 bundle_id——绝不携带 redirect_uri / state / code（Rust Debug 同款）。"""
        return f"OAuthFlow(bundle_id={self._bundle_id!r})"
