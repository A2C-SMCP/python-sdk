"""
* 文件名: sync_auth
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: None
* 描述: 同步版本的认证接口与默认实现 / Synchronous authentication interfaces and default implementation
"""

from abc import ABC, abstractmethod

from socketio import Server

from a2c_smcp.server.auth import DEFAULT_AUTH_FIELD_NAME


class SyncAuthenticationProvider(ABC):
    """
    同步认证提供者抽象基类，用于处理Socket.IO连接的认证逻辑
    Synchronous base class for authentication providers for Socket.IO authentication
    """

    @abstractmethod
    def authenticate(self, sio: Server, environ: dict, auth: dict | None, headers: list) -> bool:
        """
        认证连接请求
        Authenticate connection request

        Args:
            sio (Server): Socket.IO服务器实例 / Socket.IO server instance
            environ (dict): 请求环境变量 / Request environment variables
            auth (dict | None): 原始认证数据 / Raw authentication data
            headers (list): 原始请求头列表 / Raw request headers list

        Returns:
            bool: 认证是否成功 / Whether authentication succeeded
        """
        raise NotImplementedError


class DefaultSyncAuthenticationProvider(SyncAuthenticationProvider):
    """
    同步版本默认认证提供者
    Default sync authentication provider
    """

    def __init__(self, admin_secret: str | None = None, api_key_name: str = DEFAULT_AUTH_FIELD_NAME) -> None:
        """
        初始化默认同步认证提供者
        Initialize default sync authentication provider

        Args:
            admin_secret (str | None): 管理员密钥 / Admin secret
            api_key_name (str): auth dict 内密钥字段名，默认 ``token`` / Key field name within the
                Socket.IO CONNECT ``auth`` dict, defaults to ``token``
        """
        self.admin_secret = admin_secret
        self.api_key_name = api_key_name

    def authenticate(self, sio: Server, environ: dict, auth: dict | None, headers: list) -> bool:
        """
        默认认证逻辑：从 Socket.IO CONNECT ``auth`` dict 提取密钥进行认证。
        Default authentication logic: extract the API key from the Socket.IO CONNECT ``auth`` dict.

        #112(AS-38)：连接面鉴权走 ``auth`` dict（字段 ``api_key_name``，默认 ``token``）；HTTP header 不再
        参与鉴权（路由 header 仍由传输层透传，与鉴权无关）。
        #112(AS-38): connection auth reads the ``auth`` dict; HTTP headers no longer authenticate.
        """
        # 从 auth dict 提取密钥（字段 api_key_name，默认 token）
        # Extract the API key from the auth dict (field api_key_name, default token)
        api_key = auth.get(self.api_key_name) if isinstance(auth, dict) else None
        if not api_key:
            return False

        # 检查管理员权限：与配置的管理员密钥比较
        # Check admin permission: compare with configured admin secret
        if self.admin_secret is not None and api_key == self.admin_secret:
            return True

        # 这里可以添加其他认证逻辑，如数据库验证等
        # Additional authentication logic can be added here, such as database validation
        return False
