# -*- coding: utf-8 -*-
# filename: test_auth.py
# @Time    : 2025/9/30 23:00
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文：Agent 认证提供者（AgentAuthProvider）的集成测试。
English: Integration tests for AgentAuthProvider.

#112(AS-38)：连接面鉴权改走 Socket.IO ``auth`` dict（字段默认 ``token``）；api_key 注入 auth dict、
不再进 HTTP header；header 仅承载路由。
"""

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.types import AgentConfig


def test_default_agent_auth_provider_basic():
    """
    中文：验证默认认证提供者的基本功能（api_key 进 auth dict token 字段）。
    English: Verify basic functionality of DefaultAgentAuthProvider (api_key → auth dict token field).
    """
    agent_id = "test-agent-123"
    office_id = "test-office-456"
    api_key = "test-api-key"

    auth = DefaultAgentAuthProvider(
        agent_id=agent_id,
        office_id=office_id,
        api_key=api_key,
    )

    # 验证 agent_id
    assert auth.get_agent_id() == agent_id

    # #112(AS-38)：api_key 进 auth dict 的 token 字段
    auth_data = auth.get_connection_auth()
    assert auth_data == {"token": api_key}

    # #112(AS-38)：header 仅承载路由，不含凭据
    headers = auth.get_connection_headers()
    assert headers == {}

    # 验证 Agent 配置
    config = auth.get_agent_config()
    assert config["agent"] == agent_id
    assert config["office_id"] == office_id


def test_default_agent_auth_provider_with_custom_headers():
    """
    中文：验证默认认证提供者支持自定义（路由）请求头，且凭据走 auth dict 而非 header。
    English: Verify custom (routing) headers are supported while credential goes to the auth dict.
    """
    agent_id = "test-agent-custom"
    office_id = "test-office-custom"
    api_key = "custom-api-key"
    extra_headers = {
        "X-Custom-Header": "custom-value",
        "X-TF-Route": "route-1",
    }

    auth = DefaultAgentAuthProvider(
        agent_id=agent_id,
        office_id=office_id,
        api_key=api_key,
        extra_headers=extra_headers,
    )

    # #112(AS-38)：凭据在 auth dict
    assert auth.get_connection_auth() == {"token": api_key}

    # header 仅承载路由头，且不含凭据
    headers = auth.get_connection_headers()
    assert headers == extra_headers
    assert "access_token" not in headers
    assert api_key not in headers.values()


def test_default_agent_auth_provider_with_custom_field_name():
    """
    中文：验证默认认证提供者支持自定义 auth dict 密钥字段名。
    English: Verify DefaultAgentAuthProvider supports a custom auth-dict field name.
    """
    agent_id = "test-agent-header"
    office_id = "test-office-header"
    api_key = "field-api-key"
    custom_field_name = "x-api-token"

    auth = DefaultAgentAuthProvider(
        agent_id=agent_id,
        office_id=office_id,
        api_key=api_key,
        auth_field_name=custom_field_name,
    )

    connection_auth = auth.get_connection_auth()
    assert connection_auth is not None
    assert connection_auth == {custom_field_name: api_key}
    assert "token" not in connection_auth
    assert auth.get_connection_headers() == {}


def test_default_agent_auth_provider_with_auth_data():
    """
    中文：验证默认认证提供者支持额外认证数据（无 api_key 时原样承载）。
    English: Verify DefaultAgentAuthProvider supports extra auth data (carried as-is without api_key).
    """
    agent_id = "test-agent-auth"
    office_id = "test-office-auth"
    auth_data = {
        "username": "testuser",
        "password": "testpass",
        "token": "auth-token-123",
    }

    auth = DefaultAgentAuthProvider(
        agent_id=agent_id,
        office_id=office_id,
        auth_data=auth_data,
    )

    connection_auth = auth.get_connection_auth()

    # 验证认证数据
    assert connection_auth is not None
    assert connection_auth == auth_data
    assert connection_auth["username"] == "testuser"
    assert connection_auth["password"] == "testpass"
    assert connection_auth["token"] == "auth-token-123"


def test_default_agent_auth_provider_no_api_key():
    """
    中文：验证默认认证提供者在没有 API 密钥时的行为。
    English: Verify DefaultAgentAuthProvider behavior without API key.
    """
    agent_id = "test-agent-no-key"
    office_id = "test-office-no-key"

    auth = DefaultAgentAuthProvider(
        agent_id=agent_id,
        office_id=office_id,
        # 不提供 api_key
    )

    # 无 api_key、无 auth_data → auth dict 为 None，header 为空
    assert auth.get_connection_auth() is None
    headers = auth.get_connection_headers()
    assert "access_token" not in headers
    assert len(headers) == 0


def test_agent_config_type_validation():
    """
    中文：验证 AgentConfig 类型定义的正确性。
    English: Verify AgentConfig type definition correctness.
    """
    # 创建有效的 AgentConfig
    config: AgentConfig = {
        "agent_id": "test-agent",
        "office_id": "test-office",
    }

    assert config["agent_id"] == "test-agent"
    assert config["office_id"] == "test-office"

    # 验证必需字段
    assert "agent_id" in config
    assert "office_id" in config


def test_default_agent_auth_provider_immutability():
    """
    中文：验证默认认证提供者返回的数据不会相互影响。
    English: Verify DefaultAgentAuthProvider returns immutable data.
    """
    agent_id = "test-agent-immutable"
    office_id = "test-office-immutable"
    auth_data = {"key": "value"}
    extra_headers = {"X-Test": "test"}

    auth = DefaultAgentAuthProvider(
        agent_id=agent_id,
        office_id=office_id,
        auth_data=auth_data,
        extra_headers=extra_headers,
    )

    # 获取数据并修改
    headers1 = auth.get_connection_headers()
    headers1["Modified"] = "modified"

    auth_data1 = auth.get_connection_auth()
    if auth_data1:
        auth_data1["modified"] = "modified"

    # 再次获取数据，验证没有被修改
    headers2 = auth.get_connection_headers()
    auth_data2 = auth.get_connection_auth()

    assert "Modified" not in headers2
    assert auth_data2 and "modified" not in auth_data2
