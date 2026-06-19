# -*- coding: utf-8 -*-
"""
中文：#112(AS-38) 连接面鉴权端到端契约集成测试。
English: #112(AS-38) connection-auth end-to-end contract integration test.

对标 rust-sdk ``crates/smcp-server-core/tests/auth_connection_test.rs`` /
``crates/smcp-computer/tests/auth_dict_injection_test.rs``：

验证 **客户端 AuthProvider 注入的 auth dict** 恰好能被 **服务端 on_connect → Default provider** 验签通过，
即四处对齐的 ``token`` 字段契约在 SDK 内端到端闭环（client 产出 → server 消费）。HTTP header 不再参与鉴权。
"""

from __future__ import annotations

import pytest
from socketio import AsyncServer, Server

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.server.auth import DefaultAuthenticationProvider
from a2c_smcp.server.namespace import SMCPNamespace
from a2c_smcp.server.sync_auth import DefaultSyncAuthenticationProvider
from a2c_smcp.server.sync_namespace import SyncSMCPNamespace

SHARED_SECRET = "shared-secret-123"


def _client_auth_dict(api_key: str | None) -> dict | None:
    """客户端 AuthProvider 实际产出的 auth dict / the auth dict actually produced by the client provider."""
    return DefaultAgentAuthProvider(agent_id="agent-1", office_id="office-1", api_key=api_key).get_connection_auth()


@pytest.mark.asyncio
async def test_async_client_auth_dict_round_trips_to_server_accept_reject() -> None:
    # 客户端注入凭据 → auth dict 字段名恰为 token / client injects credential under the token field
    auth = _client_auth_dict(SHARED_SECRET)
    assert auth == {"token": SHARED_SECRET}

    sio = AsyncServer(async_mode="asgi")
    ns = SMCPNamespace(auth_provider=DefaultAuthenticationProvider(SHARED_SECRET))
    sio.register_namespace(ns)

    # 客户端真实产出的 auth dict 被服务端接受 / the client-produced auth dict is accepted
    assert await ns.on_connect("sid-ok", {}, auth) is True

    # 错误凭据 / 缺失 auth dict / 旧 header 路径 → 拒连
    with pytest.raises(ConnectionRefusedError):
        await ns.on_connect("sid-wrong", {}, {"token": "nope"})
    with pytest.raises(ConnectionRefusedError):
        await ns.on_connect("sid-none", {}, None)
    with pytest.raises(ConnectionRefusedError):
        await ns.on_connect("sid-header", {"asgi.scope": {"headers": [(b"access_token", SHARED_SECRET.encode())]}}, None)


def test_sync_client_auth_dict_round_trips_to_server_accept_reject() -> None:
    auth = _client_auth_dict(SHARED_SECRET)
    assert auth == {"token": SHARED_SECRET}

    sio = Server()
    ns = SyncSMCPNamespace(auth_provider=DefaultSyncAuthenticationProvider(SHARED_SECRET))
    sio.register_namespace(ns)

    assert ns.on_connect("sid-ok", {}, auth) is True

    with pytest.raises(ConnectionRefusedError):
        ns.on_connect("sid-wrong", {}, {"token": "nope"})
    with pytest.raises(ConnectionRefusedError):
        ns.on_connect("sid-none", {}, None)
    with pytest.raises(ConnectionRefusedError):
        ns.on_connect("sid-header", {"asgi.scope": {"headers": [(b"access_token", SHARED_SECRET.encode())]}}, None)


def test_client_no_credential_yields_none_auth_rejected_by_server() -> None:
    # 无凭据时客户端 auth dict 为 None；服务端据此拒连
    assert _client_auth_dict(None) is None
