# -*- coding: utf-8 -*-
# filename: test_oauth_manager_facade.py
# @Time    : 2026/08/13
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
MCPServerManager OAuth facade 全链路测试（#179）。

httpx.MockTransport 假 AS 全家桶（#133 先例：真实 AS 子进程在 CI 不稳，组件测试在
进程内稳定复现）——覆盖 bounded connect（anonymous-first → 准入 → 恢复/交互）、
callback 校验（重放/错 state/错 issuer）、取消、clear_oauth、并发幂等。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mcp.client.session_group import StreamableHttpParameters
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from a2c_smcp.computer.mcp_clients.base_client import BaseMCPClient
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager, _is_oauth_required_error
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerConnectionState,
    StreamableHttpServerConfig,
)
from a2c_smcp.computer.mcp_clients.oauth_coordinator import OAuthCoordinator
from a2c_smcp.computer.mcp_clients.oauth_credential_store import InMemoryOAuthCredentialStore
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
    OAuthError,
    OAuthErrorCode,
    OAuthOptions,
    _OAuthModeAuthCodeDynamic,
    _OAuthStatusAuthorized,
    _OAuthStatusUnauthorized,
)
from a2c_smcp.computer.mcp_clients.utils import client_factory

MCP_URL = "https://mcp.example.com/mcp"
PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource"
AS_ISSUER = "https://as.example"
REDIRECT_URI = "https://host.example/callback"

LATEST_PROTOCOL_VERSION = "2025-06-18"

# ============================================================================
# Fake AS（httpx.MockTransport 单处理器路由）
# ============================================================================


def _jsonrpc_response(req_id: Any, result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": req_id, "result": result},
        headers={"content-type": "application/json"},
    )


def make_fake_as_handler(extra: dict[str, Any] | None = None) -> tuple[Callable, dict[str, Any]]:
    """构造假 AS handler + 观测字典（请求计数 / 收到的 Authorization 头等）。

    路由：
    - POST MCP 端点：无 Authorization → 401 + Bearer resource_metadata（可经
      ``challenge_header`` 覆盖 / ``None`` 关闭）；带 Bearer → JSON-RPC 响应
      （initialize / tools/list）；无 id（notification）→ 202。
    - GET PRM / AS metadata → 元数据 JSON；POST DCR / token 端点 → 注册/令牌 JSON。
    """
    stats: dict[str, Any] = {
        "prm_fetches": 0,
        "dcr_posts": 0,
        "token_posts": 0,
        "bearer_seen": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url == MCP_URL:
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer "):
                stats["bearer_seen"] += 1
                if (extra or {}).get("reject_bearer"):
                    # 服务端拒绝恢复的凭据（死凭据场景：restore→401 循环探测）
                    return httpx.Response(
                        401,
                        headers={"www-authenticate": 'Bearer error="invalid_token"'},
                    )
                body = json.loads(request.content)
                req_id = body.get("id")
                if req_id is None:
                    return httpx.Response(202)
                method = body.get("method", "")
                if method == "initialize":
                    return _jsonrpc_response(
                        req_id,
                        {
                            "protocolVersion": LATEST_PROTOCOL_VERSION,
                            "capabilities": {},
                            "serverInfo": {"name": "fake-as", "version": "1.0"},
                        },
                    )
                if method == "tools/list":
                    return _jsonrpc_response(req_id, {"tools": []})
                return _jsonrpc_response(req_id, {})
            # 匿名：401 + Bearer challenge
            challenge = (extra or {}).get("challenge_header")
            if challenge is None:
                challenge = f'Bearer resource_metadata="{PRM_URL}"'
            return httpx.Response(
                401,
                headers={"www-authenticate": challenge},
            )
        if request.method == "GET" and url == PRM_URL:
            stats["prm_fetches"] += 1
            return httpx.Response(
                200,
                json={
                    "resource": MCP_URL,
                    "authorization_servers": [AS_ISSUER],
                },
                headers={"content-type": "application/json"},
            )
        if request.method == "GET" and url.startswith(f"{AS_ISSUER}/.well-known/oauth-authorization-server"):
            return httpx.Response(
                200,
                json={
                    "issuer": AS_ISSUER,
                    "authorization_endpoint": f"{AS_ISSUER}/authorize",
                    "token_endpoint": f"{AS_ISSUER}/token",
                    "registration_endpoint": f"{AS_ISSUER}/register",
                    "response_types_supported": ["code"],
                    "scopes_supported": ["read", "write"],
                },
                headers={"content-type": "application/json"},
            )
        if request.method == "POST" and url == f"{AS_ISSUER}/register":
            stats["dcr_posts"] += 1
            return httpx.Response(
                201,
                json={
                    "client_id": "fake-client-1",
                    "client_name": "A2C Computer",
                    "redirect_uris": [REDIRECT_URI],
                },
                headers={"content-type": "application/json"},
            )
        if request.method == "POST" and url == f"{AS_ISSUER}/token":
            stats["token_posts"] += 1
            return httpx.Response(
                200,
                json={"access_token": "fake-at", "token_type": "Bearer", "scope": "read write"},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    return handler, stats


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def oauth_config() -> StreamableHttpServerConfig:
    return StreamableHttpServerConfig(
        name="oauth-server",
        server_parameters={"url": MCP_URL},
        oauth=OAuthOptions(
            mode=_OAuthModeAuthCodeDynamic(),
            scopes=["read", "write"],
            client_name="test-client",
        ),
    )


@pytest.fixture
def fake_as(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    """把 manager 的 client_factory 替换为注入 MockTransport 的包装。"""
    handler, stats = make_fake_as_handler()

    def factory(
        config: Any,
        message_handler: Any = None,
        oauth_coordinator: OAuthCoordinator | None = None,
    ) -> BaseMCPClient:
        return client_factory(
            config,
            message_handler=message_handler,
            oauth_coordinator=oauth_coordinator,
            httpx_transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)
    return handler, stats


async def _make_manager(fake_as: tuple[dict[str, Any], dict[str, Any]], config: StreamableHttpServerConfig) -> MCPServerManager:
    _handler, _stats = fake_as
    manager = MCPServerManager(auto_connect=False)
    await manager.ainitialize([config])
    return manager


def _request() -> OAuthBeginRequest:
    return OAuthBeginRequest(redirect_uri=REDIRECT_URI)


def _connection_state(manager: MCPServerManager, bundle_id: str) -> MCPServerConnectionState:
    for status in manager.get_server_runtime_statuses():
        if status.bundle_id == bundle_id:
            return status.connection
    raise AssertionError(f"bundle {bundle_id!r} not found")


async def _seed_credentials(manager: MCPServerManager, config: StreamableHttpServerConfig) -> None:
    """向 manager 注入的 store 预置凭据（模拟此前已完成的授权）。"""
    from a2c_smcp.computer.mcp_clients.oauth_credential_store import (
        ScopedCredentialStore,
        oauth_mode_fingerprint,
    )

    bundle_id = "oauth-server"
    assert config.oauth is not None
    scoped = ScopedCredentialStore(
        bundle_id=bundle_id,
        resource=MCP_URL,
        mode_fingerprint=oauth_mode_fingerprint(config.oauth),
        backend=manager._oauth_credential_store,  # type: ignore[attr-defined]
    )
    await scoped.set_issuer(AS_ISSUER)
    from a2c_smcp.computer.mcp_clients.oauth_coordinator import TokenStorageAdapter

    adapter = TokenStorageAdapter(scoped)
    await adapter.set_tokens(OAuthToken(access_token="fake-at", token_type="Bearer", scope="read write"))
    await adapter.set_client_info(
        OAuthClientInformationFull(
            client_id="fake-client-1",
            client_secret=None,
            redirect_uris=[REDIRECT_URI],
            client_name="A2C Computer",
        )
    )


# ============================================================================
# Tests
# ============================================================================


class TestBoundedConnect:
    @pytest.mark.asyncio
    async def test_oauth_required_without_registered_flow(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """匿名 initialize → 401+metadata → 准入 → restore 未授权 → 无注册 flow → OAuthRequired；
        activation 保留（STARTED + AUTHORIZATION_REQUIRED）。"""
        manager = await _make_manager(fake_as, oauth_config)
        with pytest.raises(OAuthError) as exc:
            await manager.astart_client("oauth-server")
        assert exc.value.code == OAuthErrorCode.Protocol
        assert "authorizationRequired" in exc.value.message

        state = _connection_state(manager, "oauth-server")
        assert state == MCPServerConnectionState.AUTHORIZATION_REQUIRED
        # activation 保留：显式 stop 才会清除（#184 语义）
        statuses = manager.get_server_runtime_statuses()
        status = next(s for s in statuses if s.bundle_id == "oauth-server")
        assert status.activation.value == "started"

    @pytest.mark.asyncio
    async def test_astart_all_swallows_oauth_required(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """boot 批量启动：OAuthRequired 不打断整体（吞掉 + Started+AuthorizationRequired）。"""
        manager = await _make_manager(fake_as, oauth_config)
        await manager.astart_all()  # 不应抛
        state = _connection_state(manager, "oauth-server")
        assert state == MCPServerConnectionState.AUTHORIZATION_REQUIRED

    @pytest.mark.asyncio
    async def test_bearer_without_metadata_no_admission(
        self, monkeypatch: pytest.MonkeyPatch, oauth_config: StreamableHttpServerConfig
    ) -> None:
        """Bearer 无 resource_metadata → 不准入 → ERROR + OAuthError 精确分类（#185，Rust
        ``BearerWithoutMetadata`` → ``OAuthDiscoveryFailed`` 语义：Protocol + 判别文案，
        与 authorizationRequired 文案区分 → 不被 auto 路径吞掉）。"""
        handler, stats = make_fake_as_handler(extra={"challenge_header": "Bearer"})

        def factory(
            config: Any,
            message_handler: Any = None,
            oauth_coordinator: OAuthCoordinator | None = None,
        ) -> BaseMCPClient:
            return client_factory(
                config,
                message_handler=message_handler,
                oauth_coordinator=oauth_coordinator,
                httpx_transport=httpx.MockTransport(handler),
            )

        monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)
        manager = MCPServerManager(auto_connect=False)
        await manager.ainitialize([oauth_config])
        with pytest.raises(OAuthError) as exc:
            await manager.astart_client("oauth-server")
        assert exc.value.code == OAuthErrorCode.Protocol
        assert "resource metadata missing" in exc.value.message
        # 精确分类 ≠ authorizationRequired：auto 路径**不得**吞掉本错误
        assert not _is_oauth_required_error(exc.value)
        assert _connection_state(manager, "oauth-server") == MCPServerConnectionState.ERROR

    @pytest.mark.asyncio
    async def test_cross_origin_metadata_no_admission(
        self, monkeypatch: pytest.MonkeyPatch, oauth_config: StreamableHttpServerConfig
    ) -> None:
        """cross-origin resource_metadata → 不准入（same-origin 门槛）→ OAuthError 精确分类
        （#185，Rust ``ChallengeAdmission::Unsupported`` → ``UnsupportedChallenge`` 语义）。"""
        handler, stats = make_fake_as_handler(
            extra={"challenge_header": 'Bearer resource_metadata="https://evil.example/.well-known/oauth-protected-resource"'}
        )

        def factory(
            config: Any,
            message_handler: Any = None,
            oauth_coordinator: OAuthCoordinator | None = None,
        ) -> BaseMCPClient:
            return client_factory(
                config,
                message_handler=message_handler,
                oauth_coordinator=oauth_coordinator,
                httpx_transport=httpx.MockTransport(handler),
            )

        monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)
        manager = MCPServerManager(auto_connect=False)
        await manager.ainitialize([oauth_config])
        with pytest.raises(OAuthError) as exc:
            await manager.astart_client("oauth-server")
        assert exc.value.code == OAuthErrorCode.Protocol
        assert "Unsupported authentication challenge" in exc.value.message
        # 精确分类 ≠ authorizationRequired：auto 路径**不得**吞掉本错误
        assert not _is_oauth_required_error(exc.value)

    @pytest.mark.asyncio
    async def test_restore_authorized_connects_directly(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """预置凭据（token + DCR + issuer index）→ restore → 带 Bearer 直连 CONNECTED。"""
        _handler, stats = fake_as
        manager = await _make_manager(fake_as, oauth_config)
        await _seed_credentials(manager, oauth_config)

        await manager.astart_client("oauth-server")
        assert _connection_state(manager, "oauth-server") == MCPServerConnectionState.CONNECTED
        assert stats["bearer_seen"] >= 1
        status = await manager.oauth_status("oauth-server")
        assert isinstance(status, _OAuthStatusAuthorized)


class TestInteractiveFlow:
    @pytest.mark.asyncio
    async def test_full_launch_complete_flow(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """create_oauth_flow → launch（URL+state）→ complete → authorized + CONNECTED。"""
        _handler, stats = fake_as
        manager = await _make_manager(fake_as, oauth_config)

        flow = manager.create_oauth_flow("oauth-server", _request())
        assert flow is manager.create_oauth_flow("oauth-server", _request())  # 幂等：同一 handle

        launch = await asyncio.wait_for(flow.launch(), timeout=10)
        assert launch.state
        assert launch.authorization_url.startswith(f"{AS_ISSUER}/authorize")

        # 等 connect 任务推进到 callback 等待（DCR + URL 已发生）
        await asyncio.sleep(0.05)
        assert stats["prm_fetches"] == 1
        assert stats["dcr_posts"] == 1

        outcome = await asyncio.wait_for(
            flow.complete(OAuthCallback(code="fake-code", state=launch.state, issuer=AS_ISSUER)),
            timeout=10,
        )
        assert outcome.outcome == "authorized"
        assert stats["token_posts"] == 1

        # 交互式 connect 任务在交换后重试 initialize 并 commit
        for _ in range(50):
            if _connection_state(manager, "oauth-server") == MCPServerConnectionState.CONNECTED:
                break
            await asyncio.sleep(0.1)
        assert _connection_state(manager, "oauth-server") == MCPServerConnectionState.CONNECTED

        status = await manager.oauth_status("oauth-server")
        assert isinstance(status, _OAuthStatusAuthorized)

    @pytest.mark.asyncio
    async def test_callback_replay_rejected(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """终态后重放 callback → StateMismatch（不重复消费）。"""
        manager = await _make_manager(fake_as, oauth_config)
        flow = manager.create_oauth_flow("oauth-server", _request())
        launch = await asyncio.wait_for(flow.launch(), timeout=10)
        await asyncio.sleep(0.05)
        outcome = await asyncio.wait_for(
            flow.complete(OAuthCallback(code="fake-code", state=launch.state, issuer=AS_ISSUER)),
            timeout=10,
        )
        assert outcome.outcome == "authorized"
        with pytest.raises(OAuthError) as exc:
            await manager.complete_oauth(
                "oauth-server",
                OAuthCallback(code="replay", state=launch.state, issuer=AS_ISSUER),
            )
        assert exc.value.code == OAuthErrorCode.StateMismatch

    @pytest.mark.asyncio
    async def test_wrong_state_rejected(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        manager = await _make_manager(fake_as, oauth_config)
        flow = manager.create_oauth_flow("oauth-server", _request())
        launch = await asyncio.wait_for(flow.launch(), timeout=10)
        await asyncio.sleep(0.05)
        with pytest.raises(OAuthError) as exc:
            await flow.complete(OAuthCallback(code="fake-code", state="wrong-state", issuer=AS_ISSUER))
        assert exc.value.code == OAuthErrorCode.StateMismatch
        # 有效 flow 未被消费——正确 state 仍可完成
        outcome = await asyncio.wait_for(
            flow.complete(OAuthCallback(code="fake-code", state=launch.state, issuer=AS_ISSUER)),
            timeout=10,
        )
        assert outcome.outcome == "authorized"

    @pytest.mark.asyncio
    async def test_issuer_mismatch_rejected(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        manager = await _make_manager(fake_as, oauth_config)
        flow = manager.create_oauth_flow("oauth-server", _request())
        launch = await asyncio.wait_for(flow.launch(), timeout=10)
        await asyncio.sleep(0.05)
        with pytest.raises(OAuthError) as exc:
            await flow.complete(OAuthCallback(code="fake-code", state=launch.state, issuer="https://other.example"))
        assert exc.value.code == OAuthErrorCode.IssuerMismatch
        # 有效 flow 未被消费——正确 issuer 仍可完成
        outcome = await asyncio.wait_for(
            flow.complete(OAuthCallback(code="fake-code", state=launch.state, issuer=AS_ISSUER)),
            timeout=10,
        )
        assert outcome.outcome == "authorized"

    @pytest.mark.asyncio
    async def test_cancel_timeout_terminates(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """handle.cancel(Timeout) → Terminated + status unauthorized + 可开新 flow。"""
        manager = await _make_manager(fake_as, oauth_config)
        flow = manager.create_oauth_flow("oauth-server", _request())
        await asyncio.wait_for(flow.launch(), timeout=10)
        await asyncio.sleep(0.05)

        outcome = await flow.cancel(OAuthCancellationReason.Timeout)
        assert outcome.outcome == "terminated"
        assert outcome.reason == OAuthCancellationReason.Timeout

        status = await manager.oauth_status("oauth-server")
        assert isinstance(status, _OAuthStatusUnauthorized)

        # 终态后新请求**替换**注册表槽（Rust !is_terminal 过滤）：新 handle，可正常 launch
        flow2 = manager.create_oauth_flow("oauth-server", _request())
        assert flow2 is not flow
        launch2 = await asyncio.wait_for(flow2.launch(), timeout=10)
        assert launch2.state

    @pytest.mark.asyncio
    async def test_concurrent_launch_single_connect_task(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """双 launch 并发 → 至多一次 connect 尝试在途（PRM 抓取计数 == 1）。"""
        _handler, stats = fake_as
        manager = await _make_manager(fake_as, oauth_config)
        flow = manager.create_oauth_flow("oauth-server", _request())

        results = await asyncio.gather(
            asyncio.wait_for(flow.launch(), timeout=10),
            asyncio.wait_for(flow.launch(), timeout=10),
        )
        assert results[0].state == results[1].state
        await asyncio.sleep(0.05)
        assert stats["prm_fetches"] == 1  # 单 connect 任务 → 单次 discovery


class TestReviewFixes:
    """#179 隔离审查修复的回归用例（终态替换 / pre-challenge cancel / ghost flow / 死凭据 / transport 切换）。"""

    @pytest.mark.asyncio
    async def test_terminal_flow_replaced_by_different_request(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """complete 终态后，不同 redirect_uri（loopback retry 换端口模式）可开新 flow。"""
        manager = await _make_manager(fake_as, oauth_config)
        flow = manager.create_oauth_flow("oauth-server", _request())
        launch = await asyncio.wait_for(flow.launch(), timeout=10)
        await asyncio.sleep(0.05)
        outcome = await asyncio.wait_for(
            flow.complete(OAuthCallback(code="c1", state=launch.state, issuer=AS_ISSUER)),
            timeout=10,
        )
        assert outcome.outcome == "authorized"

        # 终态：不同 redirect_uri → 新 handle（非 AlreadyPending）
        flow2 = manager.create_oauth_flow(
            "oauth-server",
            OAuthBeginRequest(redirect_uri="https://host.example/cb2"),
        )
        assert flow2 is not flow
        launch2 = await asyncio.wait_for(flow2.launch(), timeout=10)
        assert launch2.state

    @pytest.mark.asyncio
    async def test_pre_challenge_cancel_resolves_concurrent_launch(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """launch 在途时 pre-challenge cancel → launch 以 typed error 返回（非挂起），status 回落。"""
        manager = await _make_manager(fake_as, oauth_config)
        coordinator = OAuthCoordinator(
            bundle_id="oauth-server",
            server_url=MCP_URL,
            resource=MCP_URL,
            options=oauth_config.oauth,
            credential_store=manager._oauth_credential_store,
        )
        manager._oauth_coordinators["oauth-server"] = coordinator
        flow = manager.create_oauth_flow("oauth-server", _request())

        launch_task = asyncio.create_task(flow.launch())
        await asyncio.sleep(0)  # 让 register 落地（launch 等 wait_launch）
        outcome = await asyncio.wait_for(flow.cancel(OAuthCancellationReason.Cancelled), timeout=5)
        assert outcome.outcome == "terminated"
        # launch 以 typed error 返回，绝不挂起
        with pytest.raises(OAuthError) as exc:
            await asyncio.wait_for(launch_task, timeout=5)
        assert exc.value.code == OAuthErrorCode.AuthorizationCancelled
        status = await manager.oauth_status("oauth-server")
        assert isinstance(status, _OAuthStatusUnauthorized)

    @pytest.mark.asyncio
    async def test_expired_flow_clears_registration_no_ghost(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """flow 过期收敛后 registered 清除——重 start 不再派发 ghost flow。"""
        manager = await _make_manager(fake_as, oauth_config)
        flow = manager.create_oauth_flow("oauth-server", _request())
        await asyncio.wait_for(flow.launch(), timeout=10)
        # 等 connect 任务推进到 challenge → redirect_handler 发布 PENDING（时序敏感：
        # 未 PENDING 时 bump generation 不构成陈旧判据）
        from a2c_smcp.computer.mcp_clients.oauth_coordinator import _FlowPhase

        coordinator = manager._oauth_coordinators["oauth-server"]
        for _ in range(50):
            if coordinator._flow.phase == _FlowPhase.PENDING:
                break
            await asyncio.sleep(0.05)
        assert coordinator._flow.phase == _FlowPhase.PENDING
        # 强制陈旧（等价于 pending 期间发生凭据 save/refresh 的 generation bump）
        coordinator._generation += 1
        # 重 start：expire 路径收敛（OAuthRequired 属预期），此后注册应为空
        with pytest.raises(OAuthError):
            await manager.astart_client("oauth-server")
        assert not coordinator.has_registered_request()
        assert not coordinator.has_active_flow()
        # 新 flow 可注册（无 AlreadyPending / 无 ghost 重发布）
        flow2 = manager.create_oauth_flow("oauth-server", _request())
        assert flow2 is not flow

    @pytest.mark.asyncio
    async def test_restored_credentials_rejected_then_cleared(
        self, monkeypatch: pytest.MonkeyPatch, oauth_config: StreamableHttpServerConfig
    ) -> None:
        """restore→Bearer 401（服务端拒绝恢复的凭据）→ 凭据槽被清 + OAuthRequired（防死循环）。"""
        handler, stats = make_fake_as_handler(extra={"reject_bearer": True})

        def factory(
            config: Any,
            message_handler: Any = None,
            oauth_coordinator: OAuthCoordinator | None = None,
        ) -> BaseMCPClient:
            return client_factory(
                config,
                message_handler=message_handler,
                oauth_coordinator=oauth_coordinator,
                httpx_transport=httpx.MockTransport(handler),
            )

        monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)
        manager = MCPServerManager(auto_connect=False)
        await manager.ainitialize([oauth_config])
        await _seed_credentials(manager, oauth_config)

        with pytest.raises(OAuthError):
            await manager.astart_client("oauth-server")
        assert _connection_state(manager, "oauth-server") == MCPServerConnectionState.AUTHORIZATION_REQUIRED
        status = await manager.oauth_status("oauth-server")
        assert isinstance(status, _OAuthStatusUnauthorized)
        # 凭据槽已清（下一次匿名 attempt 不会再次 restore→Authorized）
        coordinator = manager._oauth_coordinators["oauth-server"]
        assert await coordinator._token_storage.get_tokens() is None

    @pytest.mark.asyncio
    async def test_transport_switch_retires_stale_coordinator(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """同 bundle_id 运行期 streamable→stdio 切换：不崩溃、不沿用陈旧 coordinator。"""
        manager = await _make_manager(fake_as, oauth_config)
        with pytest.raises(OAuthError):
            await manager.astart_client("oauth-server")  # 准入（授权未完成）
        assert "oauth-server" in manager._oauth_coordinators

        from a2c_smcp.computer.mcp_clients.model import StdioServerConfig

        stdio_cfg = StdioServerConfig(
            name="oauth-server",
            server_parameters={"command": "true"},
        )
        # auto_connect=False：仅换配置——transport 类型切换即刻退役 OAuth 运行时态
        await manager.aadd_or_aupdate_server(stdio_cfg)
        assert "oauth-server" not in manager._oauth_coordinators
        assert "oauth-server" not in manager._oauth_flows
        # 退役已断言（更新时即刻发生）。启动路径的 plain connect 属既有 stdio 行为
        # （进程即退、异常形态非本测试关注点），防御分支不触 OAuth 面即可。
        assert manager._oauth_spec(stdio_cfg) is None


class TestFacadeGuards:
    @pytest.mark.asyncio
    async def test_oauth_status_pre_admission_not_configured(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        manager = await _make_manager(fake_as, oauth_config)
        with pytest.raises(OAuthError) as exc:
            await manager.oauth_status("oauth-server")
        assert exc.value.code == OAuthErrorCode.NotConfigured

    @pytest.mark.asyncio
    async def test_oauth_status_unknown_bundle(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        manager = await _make_manager(fake_as, oauth_config)
        with pytest.raises(OAuthError) as exc:
            await manager.oauth_status("unknown-bundle")
        assert exc.value.code == OAuthErrorCode.NotConfigured

    @pytest.mark.asyncio
    async def test_oauth_status_stdio_unsupported_transport(self, fake_as: tuple[dict[str, Any], dict[str, Any]]) -> None:
        from a2c_smcp.computer.mcp_clients.model import StdioServerConfig

        manager = MCPServerManager(auto_connect=False)
        await manager.ainitialize([
            StdioServerConfig(
                name="stdio-srv",
                server_parameters={"command": "python3", "args": ["-c", "pass"]},
            )
        ])
        with pytest.raises(OAuthError) as exc:
            await manager.oauth_status("stdio-srv")
        assert exc.value.code == OAuthErrorCode.UnsupportedTransport

    def test_create_oauth_flow_conflicting_request(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        manager = MCPServerManager(auto_connect=False)
        # create_oauth_flow 为同步方法——直接同步构造 manager 状态（不经 ainitialize 的锁）
        manager._servers_config["oauth-server"] = oauth_config
        manager.create_oauth_flow("oauth-server", _request())
        with pytest.raises(OAuthError) as exc:
            manager.create_oauth_flow(
                "oauth-server",
                OAuthBeginRequest(redirect_uri="https://other.example/cb"),
            )
        assert exc.value.code == OAuthErrorCode.AuthorizationAlreadyPending


class TestClearOAuth:
    @pytest.mark.asyncio
    async def test_clear_oauth_full_reset(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        """授权完成后 clear_oauth：凭据清空 + status unauthorized + 连接 AUTHORIZATION_REQUIRED
        （Started 保留）+ client 退役；二次 clear 幂等不抛。"""
        manager = await _make_manager(fake_as, oauth_config)
        flow = manager.create_oauth_flow("oauth-server", _request())
        launch = await asyncio.wait_for(flow.launch(), timeout=10)
        await asyncio.sleep(0.05)
        outcome = await asyncio.wait_for(
            flow.complete(OAuthCallback(code="fake-code", state=launch.state, issuer=AS_ISSUER)),
            timeout=10,
        )
        assert outcome.outcome == "authorized"
        for _ in range(50):
            if _connection_state(manager, "oauth-server") == MCPServerConnectionState.CONNECTED:
                break
            await asyncio.sleep(0.1)
        assert _connection_state(manager, "oauth-server") == MCPServerConnectionState.CONNECTED

        await manager.clear_oauth("oauth-server")

        status = await manager.oauth_status("oauth-server")
        assert isinstance(status, _OAuthStatusUnauthorized)
        state = _connection_state(manager, "oauth-server")
        assert state == MCPServerConnectionState.AUTHORIZATION_REQUIRED
        assert "oauth-server" not in manager._active_clients

        # 二次 clear 幂等（凭据已空，无错误）
        await manager.clear_oauth("oauth-server")

    @pytest.mark.asyncio
    async def test_clear_oauth_resolves_in_flight_launch(
        self, monkeypatch: pytest.MonkeyPatch, oauth_config: StreamableHttpServerConfig
    ) -> None:
        """在途 launch waiter 遇 clear_oauth → 以 typed error 返回（不挂起；teardown 升格）。

        确定性构造：第 1 个 MCP POST 返回 401 challenge（准入），第 2 个（交互式
        connect 的 initialize）挂起响应体——waiter 必在 clear 时仍 PENDING，
        不依赖「URL 先发布还是 clear 先到」的竞速。
        """

        async def _hang_body():  # noqa: ANN202
            await asyncio.Event().wait()  # 永不返回：aconnect 挂起于响应体读取
            yield b""  # pragma: no cover — 不可达

        post_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if request.method == "POST" and url == MCP_URL:
                post_count["n"] += 1
                if post_count["n"] == 1:
                    return httpx.Response(
                        401,
                        headers={"www-authenticate": f'Bearer resource_metadata="{PRM_URL}"'},
                    )
                return httpx.Response(
                    200,
                    content=_hang_body(),
                    headers={"content-type": "application/json"},
                )
            if request.method == "GET" and url == PRM_URL:
                return httpx.Response(
                    200,
                    json={"resource": MCP_URL, "authorization_servers": [AS_ISSUER]},
                    headers={"content-type": "application/json"},
                )
            if request.method == "GET" and url.startswith(f"{AS_ISSUER}/.well-known/oauth-authorization-server"):
                return httpx.Response(
                    200,
                    json={
                        "issuer": AS_ISSUER,
                        "authorization_endpoint": f"{AS_ISSUER}/authorize",
                        "token_endpoint": f"{AS_ISSUER}/token",
                        "registration_endpoint": f"{AS_ISSUER}/register",
                        "response_types_supported": ["code"],
                        "scopes_supported": ["read", "write"],
                    },
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(404)

        def factory(
            config: Any,
            message_handler: Any = None,
            oauth_coordinator: OAuthCoordinator | None = None,
        ) -> BaseMCPClient:
            return client_factory(
                config,
                message_handler=message_handler,
                oauth_coordinator=oauth_coordinator,
                httpx_transport=httpx.MockTransport(handler),
            )

        monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)
        manager = MCPServerManager(auto_connect=False)
        await manager.ainitialize([oauth_config])
        flow = manager.create_oauth_flow("oauth-server", _request())
        launch_task = asyncio.create_task(flow.launch())
        # 等 launch 内 ensure 完成准入 + register 落位（时序敏感：准入含匿名 connect 竞速）
        for _ in range(50):
            coordinator = manager._oauth_coordinators.get("oauth-server")
            if coordinator is not None and coordinator.has_registered_request():
                break
            await asyncio.sleep(0.05)
        assert manager._oauth_coordinators["oauth-server"].has_registered_request()
        await manager.clear_oauth("oauth-server")
        with pytest.raises(OAuthError) as exc:
            await asyncio.wait_for(launch_task, timeout=5)
        assert exc.value.code == OAuthErrorCode.AuthorizationCancelled

    @pytest.mark.asyncio
    async def test_clear_oauth_not_configured(
        self, fake_as: tuple[dict[str, Any], dict[str, Any]], oauth_config: StreamableHttpServerConfig
    ) -> None:
        manager = await _make_manager(fake_as, oauth_config)
        with pytest.raises(OAuthError) as exc:
            await manager.clear_oauth("oauth-server")
        assert exc.value.code == OAuthErrorCode.NotConfigured
