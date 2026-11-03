# -*- coding: utf-8 -*-
# filename: utils.py
# @Time    : 2025/8/19 10:54
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
from typing import TYPE_CHECKING

import httpx
from mcp.client.session import MessageHandlerFnT

from a2c_smcp.computer.mcp_clients.base_client import _CONNECT_HANDSHAKE_TIMEOUT, BaseMCPClient
from a2c_smcp.computer.mcp_clients.http_client import HttpMCPClient
from a2c_smcp.computer.mcp_clients.model import MCPServerConfig, SseServerConfig, StdioServerConfig, StreamableHttpServerConfig
from a2c_smcp.computer.mcp_clients.sse_client import SseMCPClient
from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient

if TYPE_CHECKING:
    from a2c_smcp.computer.mcp_clients.oauth_coordinator import OAuthCoordinator

# #247：stdio（npx/uvx）首次冷启动需下载 + 启动，常超过 30s；120s 留余量，同时仍能收敛死锁。
# Generous stdio handshake budget for first-run npx/uvx downloads, still bounded against wedges.
STDIO_CONNECT_HANDSHAKE_TIMEOUT = 120.0


def client_factory(
    config: MCPServerConfig,
    message_handler: MessageHandlerFnT | None = None,
    oauth_coordinator: "OAuthCoordinator | None" = None,
    httpx_transport: httpx.AsyncBaseTransport | None = None,
) -> BaseMCPClient:
    """根据配置创建客户端/Create client based on config.

    ``oauth_coordinator``（#179）：仅 Streamable HTTP 客户端消费——注入后
    httpx auth 由 OAuthClientProvider 接管（bounded connect / 交互式 flow）。
    ``httpx_transport``：仅测试/诊断注入（#133 接缝），生产路径不传。
    """
    # 根据实际配置创建不同类型的客户端/Create different types of clients based on the actual configuration
    client: BaseMCPClient
    handshake_timeout: float | None
    match config:
        case StdioServerConfig():
            client = StdioMCPClient(config.server_parameters, message_handler=message_handler)
            # #247：npx/uvx 首次冷启动需下载 + 启动，30s 会误杀；120s 留余量且仍能收敛死锁。
            handshake_timeout = STDIO_CONNECT_HANDSHAKE_TIMEOUT
        case SseServerConfig():
            client = SseMCPClient(config.server_parameters, message_handler=message_handler)
            handshake_timeout = _CONNECT_HANDSHAKE_TIMEOUT
        case StreamableHttpServerConfig():
            client = HttpMCPClient(
                config.server_parameters,
                message_handler=message_handler,
                oauth_coordinator=oauth_coordinator,
                httpx_transport=httpx_transport,
            )
            # #247：coordinator-attached（交互式 OAuth）的 initialize 内联 auth flow，等用户浏览器
            # 授权可达分钟级 → 不设 SDK 上界，由 flow TTL（10 分钟）+ 取消链约束；其余 HTTP 与
            # manager bounded connect 的 30s 对齐。
            handshake_timeout = None if oauth_coordinator is not None else _CONNECT_HANDSHAKE_TIMEOUT
        case _:
            raise ValueError(f"Unsupported config type: {type(config)}")  # noqa
    client.set_connect_handshake_timeout(handshake_timeout)
    return client
