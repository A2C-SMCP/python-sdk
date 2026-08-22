# -*- coding: utf-8 -*-
# filename: __init__.py.py
# @Time    : 2025/8/15 11:47
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.oauth_credential_store import (  # noqa: F401 — #179 宿主注入面
    InMemoryOAuthCredentialStore,
    OAuthCredentialStore,
)
from a2c_smcp.computer.mcp_clients.oauth_flow import OAuthFlow  # noqa: F401 — #179 宿主 handle
from a2c_smcp.computer.mcp_clients.oauth_types import (  # noqa: F401 — #179 宿主类型面（对齐 Rust crate root 导出）
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
    OAuthError,
    OAuthFlowOutcome,
    OAuthLaunch,
    OAuthStatus,
)
from a2c_smcp.computer.socketio.client import AuthProvider, SMCPComputerClient  # noqa: F401 — #200 动态 auth provider 类型面

__all__ = [
    "AuthProvider",
    "Computer",
    "SMCPComputerClient",
    "InMemoryOAuthCredentialStore",
    "OAuthBeginRequest",
    "OAuthCallback",
    "OAuthCancellation",
    "OAuthCancellationReason",
    "OAuthCredentialStore",
    "OAuthError",
    "OAuthFlow",
    "OAuthFlowOutcome",
    "OAuthLaunch",
    "OAuthStatus",
]
