# -*- coding: utf-8 -*-
"""
中文：Agent 客户端握手配置测试（对齐 Rust handshake_config_test 精神）。
English: Handshake config tests for the Agent-side client.

覆盖场景 / Covered scenarios:
  1. ``DEFAULT_AUTH_HEADER_NAME`` 常量对外暴露为 ``access_token``
  2. ``DefaultAgentAuthProvider`` 默认把 API key 写到 ``access_token`` header
  3. 自定义 ``api_key_header`` 能覆盖默认名
  4. Async / Sync Agent 客户端默认 namespace 为 ``/smcp``，自定义 namespace 会贯穿事件注册
  5. getter 返回构造器传入的真实值
"""

from __future__ import annotations

from a2c_smcp.agent import DEFAULT_AUTH_HEADER_NAME, DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import (
    ENTER_OFFICE_NOTIFICATION,
    LEAVE_OFFICE_NOTIFICATION,
    SMCP_NAMESPACE,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
)

EXPECTED_NOTIFY_EVENTS = {
    ENTER_OFFICE_NOTIFICATION,
    LEAVE_OFFICE_NOTIFICATION,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
}


def test_agent_default_auth_header_name_is_access_token() -> None:
    """Agent 模块导出的默认 header 名为 ``access_token`` / exported default header is ``access_token``"""
    assert DEFAULT_AUTH_HEADER_NAME == "access_token"


def test_default_agent_auth_provider_uses_access_token_by_default() -> None:
    """``DefaultAgentAuthProvider`` 默认把 API key 写到 ``access_token`` header"""
    provider = DefaultAgentAuthProvider(
        agent_id="agent-1",
        office_id="office-1",
        api_key="secret",
    )

    headers = provider.get_connection_headers()
    assert headers["access_token"] == "secret"
    assert "x-api-key" not in headers


def test_default_agent_auth_provider_custom_header_overrides_default() -> None:
    """自定义 ``api_key_header`` 能覆盖默认名 / custom api_key_header overrides the default"""
    provider = DefaultAgentAuthProvider(
        agent_id="agent-2",
        office_id="office-2",
        api_key="secret",
        api_key_header="x-tf-token",
    )

    headers = provider.get_connection_headers()
    assert headers["x-tf-token"] == "secret"
    assert "access_token" not in headers


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
