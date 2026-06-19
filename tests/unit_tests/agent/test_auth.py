"""
* 文件名: test_auth
* 作者: JQQ
* 创建日期: 2025/9/30
* 最后修改日期: 2026/6/19
* 版权: 2023 JQQ. All rights reserved.
* 依赖: pytest
* 描述: Agent认证模块测试用例 / Agent authentication module test cases

  #112(AS-38)：连接面鉴权改走 Socket.IO ``auth`` dict（字段默认 ``token``）；api_key 注入 auth dict、
  不再进 HTTP header；header 仅承载路由。
"""

import pytest

from a2c_smcp.agent.auth import DEFAULT_AUTH_FIELD_NAME, AgentAuthProvider, DefaultAgentAuthProvider
from a2c_smcp.agent.types import AgentConfig


def test_default_auth_field_name_is_token() -> None:
    """#112(AS-38)：默认鉴权字段名为 ``token`` / default auth field is ``token``."""
    assert DEFAULT_AUTH_FIELD_NAME == "token"


class TestAgentAuthProvider:
    """测试Agent认证提供者抽象基类 / Test Agent authentication provider abstract base class"""

    def test_abstract_methods(self) -> None:
        """测试抽象方法不能直接实例化 / Test abstract methods cannot be instantiated directly"""
        with pytest.raises(TypeError):
            AgentAuthProvider()  # type: ignore


class TestDefaultAgentAuthProvider:
    """测试默认Agent认证提供者 / Test default Agent authentication provider"""

    def test_init_with_minimal_params(self) -> None:
        """测试使用最小参数初始化 / Test initialization with minimal parameters"""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent",
            office_id="test_office",
        )

        assert provider.get_agent_id() == "test_agent"

        config = provider.get_agent_config()
        assert config["agent"] == "test_agent"
        assert config["office_id"] == "test_office"

    def test_init_with_full_params(self) -> None:
        """测试使用完整参数初始化 / Test initialization with full parameters"""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent",
            office_id="test_office",
            api_key="test_key",
            extra_headers={"Custom-Header": "custom_value"},
            auth_data={"tenant_id": "t1"},
        )

        assert provider.get_agent_id() == "test_agent"

        # #112(AS-38)：api_key 注入 auth dict 的 token 字段，与 auth_data 合并
        # api_key is injected into the auth dict's token field, merged with auth_data
        auth_data = provider.get_connection_auth()
        assert auth_data == {"tenant_id": "t1", "token": "test_key"}

        # #112(AS-38)：header 仅承载路由，不含 api_key
        # headers carry routing only, no api_key
        headers = provider.get_connection_headers()
        assert headers == {"Custom-Header": "custom_value"}
        assert "test_key" not in headers.values()

    def test_get_connection_auth_empty(self) -> None:
        """测试空认证数据 / Test empty authentication data"""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent",
            office_id="test_office",
        )

        auth_data = provider.get_connection_auth()
        assert auth_data is None

    def test_api_key_injected_into_auth_dict_token(self) -> None:
        """#112(AS-38)：仅 api_key 时 → auth dict 含 token / api_key only → auth dict has token."""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent",
            office_id="test_office",
            api_key="secret_key",
        )

        assert provider.get_connection_auth() == {"token": "secret_key"}

    def test_get_connection_headers_routing_only(self) -> None:
        """#112(AS-38)：api_key 不再进 header，header 仅路由 / api_key never in headers."""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent",
            office_id="test_office",
            api_key="secret_key",
            extra_headers={"X-TF-Route": "r1"},
        )

        headers = provider.get_connection_headers()
        assert headers == {"X-TF-Route": "r1"}
        assert "access_token" not in headers
        assert "secret_key" not in headers.values()

    def test_get_connection_headers_without_api_key(self) -> None:
        """测试不带API密钥的请求头 / Test headers without API key"""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent",
            office_id="test_office",
        )

        headers = provider.get_connection_headers()
        assert headers == {}

    def test_get_agent_config(self) -> None:
        """测试获取Agent配置 / Test get Agent configuration"""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent_123",
            office_id="test_office_456",
        )

        config = provider.get_agent_config()
        expected_config: AgentConfig = {
            "agent": "test_agent_123",
            "office_id": "test_office_456",
        }
        assert config == expected_config

    def test_custom_auth_field_name(self) -> None:
        """测试自定义 auth dict 密钥字段名 / Test custom auth-dict field name"""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent",
            office_id="test_office",
            api_key="test_key",
            auth_field_name="x-custom-auth",
        )

        assert provider.get_connection_auth() == {"x-custom-auth": "test_key"}
        # 默认 token 字段在自定义字段名下不存在
        assert provider.get_connection_headers() == {}

    def test_api_key_overrides_auth_data_token(self) -> None:
        """显式 api_key 在 token 字段上优先于 auth_data / explicit api_key wins on the token field."""
        provider = DefaultAgentAuthProvider(
            agent_id="test_agent",
            office_id="test_office",
            api_key="new_token",
            auth_data={"token": "old_token", "tenant_id": "t1"},
        )

        assert provider.get_connection_auth() == {"token": "new_token", "tenant_id": "t1"}
