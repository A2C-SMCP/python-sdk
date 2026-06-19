# -*- coding: utf-8 -*-
"""
中文：Agent 客户端握手配置测试（对齐 Rust handshake_config_test 精神）。
English: Handshake config tests for the Agent-side client.

#112(AS-38)：连接面鉴权改走 Socket.IO ``auth`` dict（字段默认 ``token``）；api_key 注入 auth dict、不再进 header。

覆盖场景 / Covered scenarios:
  1. ``DEFAULT_AUTH_FIELD_NAME`` 常量对外暴露为 ``token``
  2. ``DefaultAgentAuthProvider`` 默认把 API key 注入 ``auth`` dict 的 ``token`` 字段（不进 header）
  3. 自定义 ``auth_field_name`` 能覆盖默认名
  4. Async / Sync Agent 客户端默认 namespace 为 ``/smcp``，自定义 namespace 会贯穿事件注册
  5. getter 返回构造器传入的真实值
"""

from __future__ import annotations

from a2c_smcp.agent import DEFAULT_AUTH_FIELD_NAME, DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import (
    ENTER_OFFICE_NOTIFICATION,
    LEAVE_OFFICE_NOTIFICATION,
    SMCP_NAMESPACE,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
    UPDATE_SKILLS_NOTIFICATION,
)

EXPECTED_NOTIFY_EVENTS = {
    ENTER_OFFICE_NOTIFICATION,
    LEAVE_OFFICE_NOTIFICATION,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
    UPDATE_SKILLS_NOTIFICATION,
}


def test_agent_default_auth_field_name_is_token() -> None:
    """Agent 模块导出的默认鉴权字段名为 ``token`` / exported default auth field is ``token``"""
    assert DEFAULT_AUTH_FIELD_NAME == "token"


def test_default_agent_auth_provider_injects_token_by_default() -> None:
    """``DefaultAgentAuthProvider`` 默认把 API key 注入 ``auth`` dict 的 ``token`` 字段（不进 header）"""
    provider = DefaultAgentAuthProvider(
        agent_id="agent-1",
        office_id="office-1",
        api_key="secret",
    )

    assert provider.get_connection_auth() == {"token": "secret"}
    # api_key 不再进 header；header 仅承载路由
    assert provider.get_connection_headers() == {}


def test_default_agent_auth_provider_custom_field_overrides_default() -> None:
    """自定义 ``auth_field_name`` 能覆盖默认名 / custom auth_field_name overrides the default"""
    provider = DefaultAgentAuthProvider(
        agent_id="agent-2",
        office_id="office-2",
        api_key="secret",
        auth_field_name="x-tf-token",
    )

    connection_auth = provider.get_connection_auth()
    assert connection_auth is not None
    assert connection_auth == {"x-tf-token": "secret"}
    assert "token" not in connection_auth


def test_async_agent_client_default_namespace() -> None:
    """Async Agent 客户端默认 namespace 为 ``/smcp`` / async agent default namespace is ``/smcp``"""
    provider = DefaultAgentAuthProvider(agent_id="a", office_id="o")
    client = AsyncSMCPAgentClient(auth_provider=provider)

    assert client.namespace == SMCP_NAMESPACE
    assert SMCP_NAMESPACE in client.handlers
    assert set(client.handlers[SMCP_NAMESPACE].keys()) == EXPECTED_NOTIFY_EVENTS


def test_async_agent_client_custom_namespace_registers_all_handlers() -> None:
    """Async Agent 客户端自定义 namespace 贯穿事件处理器注册 / custom namespace applies to every handler registration"""
    custom_ns = "/tf-smcp"
    provider = DefaultAgentAuthProvider(agent_id="a", office_id="o")
    client = AsyncSMCPAgentClient(auth_provider=provider, namespace=custom_ns)

    assert client.namespace == custom_ns
    assert custom_ns in client.handlers
    assert SMCP_NAMESPACE not in client.handlers
    assert set(client.handlers[custom_ns].keys()) == EXPECTED_NOTIFY_EVENTS


def test_sync_agent_client_default_namespace() -> None:
    """Sync Agent 客户端默认 namespace 为 ``/smcp`` / sync agent default namespace is ``/smcp``"""
    provider = DefaultAgentAuthProvider(agent_id="a", office_id="o")
    client = SMCPAgentClient(auth_provider=provider)

    assert client.namespace == SMCP_NAMESPACE
    assert SMCP_NAMESPACE in client.handlers
    assert set(client.handlers[SMCP_NAMESPACE].keys()) == EXPECTED_NOTIFY_EVENTS


def test_sync_agent_client_custom_namespace_registers_all_handlers() -> None:
    """Sync Agent 客户端自定义 namespace 同样贯穿事件处理器注册"""
    custom_ns = "/tf-smcp"
    provider = DefaultAgentAuthProvider(agent_id="a", office_id="o")
    client = SMCPAgentClient(auth_provider=provider, namespace=custom_ns)

    assert client.namespace == custom_ns
    assert custom_ns in client.handlers
    assert SMCP_NAMESPACE not in client.handlers
    assert set(client.handlers[custom_ns].keys()) == EXPECTED_NOTIFY_EVENTS
