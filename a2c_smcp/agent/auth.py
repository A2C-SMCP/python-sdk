"""
* 文件名: auth
* 作者: JQQ
* 创建日期: 2025/9/30
* 最后修改日期: 2025/9/30
* 版权: 2023 JQQ. All rights reserved.
* 依赖: None
* 描述: Agent端认证接口抽象定义 / Agent-side authentication interface abstract definition
"""

from abc import ABC, abstractmethod
from typing import Any

from a2c_smcp.agent.types import AgentConfig

# 默认鉴权字段名（Socket.IO CONNECT ``auth`` dict 内的键）/ Default auth field name within the
# Socket.IO CONNECT ``auth`` dict.
#
# #112(AS-38)：连接面鉴权统一走 Socket.IO ``auth`` dict（不再用 HTTP header）。A2C-SMCP auth-agnostic，
# 默认 ``token``（对齐 server 默认与 TFRS token-exchange 契约），可由调用方覆盖。
# #112(AS-38): connection auth lives in the Socket.IO ``auth`` dict (no HTTP header); defaults to ``token``.
DEFAULT_AUTH_FIELD_NAME = "token"


class AgentAuthProvider(ABC):
    """
    Agent认证提供者抽象基类，用于处理Agent连接的认证逻辑
    Abstract base class for Agent authentication providers, handles Agent connection authentication logic
    """

    @abstractmethod
    def get_agent_id(self) -> str:
        """
        获取agent_id，由用户实现具体逻辑
        Get agent_id, implemented by user with specific logic

        Returns:
            str: agent_id
        """
        pass

    @abstractmethod
    def get_connection_auth(self) -> dict[str, Any] | None:
        """
        获取连接认证信息，用于Socket.IO连接时的auth参数
        Get connection authentication info, used for Socket.IO connection auth parameter

        Returns:
            dict[str, Any] | None: 认证信息字典 / Authentication info dictionary
        """
        pass

    @abstractmethod
    def get_connection_headers(self) -> dict[str, str]:
        """
        获取连接请求头，用于Socket.IO连接时的headers参数
        Get connection headers, used for Socket.IO connection headers parameter

        Returns:
            dict[str, str]: 请求头字典 / Headers dictionary
        """
        pass

    @abstractmethod
    def get_agent_config(self) -> AgentConfig:
        """
        获取Agent配置信息
        Get Agent configuration information

        Returns:
            AgentConfig: Agent配置 / Agent configuration
        """
        pass


class DefaultAgentAuthProvider(AgentAuthProvider):
    """
    默认Agent认证提供者，提供基础的认证逻辑实现
    Default Agent authentication provider, provides basic authentication logic implementation
    """

    def __init__(
        self,
        agent_id: str,
        office_id: str,
        api_key: str | None = None,
        auth_field_name: str = DEFAULT_AUTH_FIELD_NAME,
        extra_headers: dict[str, str] | None = None,
        auth_data: dict[str, Any] | None = None,
    ) -> None:
        """
        初始化默认Agent认证提供者
        Initialize default Agent authentication provider

        Args:
            agent_id (str): Agent唯一标识 / Agent unique identifier
            office_id (str): 办公室ID / Office ID
            api_key (str | None): API密钥；#112(AS-38) 注入 Socket.IO ``auth`` dict 的 ``auth_field_name``
                字段（默认 ``token``）/ API key; injected into the Socket.IO ``auth`` dict under
                ``auth_field_name`` (default ``token``)
            auth_field_name (str): auth dict 内密钥字段名，默认 ``token`` / Key field name within the
                ``auth`` dict, defaults to ``token``
            extra_headers (dict[str, str] | None): 额外（路由）请求头，#112(AS-38) 起不再承载鉴权凭据 /
                Extra (routing) headers; no longer carry auth credentials since #112(AS-38)
            auth_data (dict[str, Any] | None): 额外认证数据，与 api_key 合并进 ``auth`` dict /
                Extra auth data, merged with api_key into the ``auth`` dict
        """
        self._agent_id = agent_id
        self._office_id = office_id
        self._api_key = api_key
        self._auth_field_name = auth_field_name
        self._extra_headers = extra_headers or {}
        self._auth_data = auth_data or {}

    def get_agent_id(self) -> str:
        """
        获取Agent ID
        Get Agent ID
        """
        return self._agent_id

    def get_connection_auth(self) -> dict[str, Any] | None:
        """
        获取连接认证信息（Socket.IO ``auth`` dict）
        Get connection authentication info (Socket.IO ``auth`` dict)

        #112(AS-38)：连接面鉴权走 ``auth`` dict —— 把 ``api_key`` 注入 ``auth_field_name`` 字段
        （默认 ``token``），并与用户传入的 ``auth_data`` 合并。无任何字段时返回 ``None``。
        #112(AS-38): connection auth lives in the ``auth`` dict — the api_key is injected under
        ``auth_field_name`` (default ``token``) and merged with the user-supplied ``auth_data``.
        """
        auth: dict[str, Any] = self._auth_data.copy()

        # 把 API 密钥注入 auth dict（默认字段 token）
        # Inject the API key into the auth dict (default field token)
        if self._api_key:
            auth[self._auth_field_name] = self._api_key

        return auth or None

    def get_connection_headers(self) -> dict[str, str]:
        """
        获取连接请求头（仅路由，#112(AS-38) 起不再承载鉴权凭据）
        Get connection (routing-only) headers; no auth credentials since #112(AS-38)
        """
        return self._extra_headers.copy()

    def get_agent_config(self) -> AgentConfig:
        """
        获取Agent配置信息
        Get Agent configuration information
        """
        return AgentConfig(
            agent=self._agent_id,
            office_id=self._office_id,
        )
