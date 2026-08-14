# -*- coding: utf-8 -*-
# filename: utils.py
# @Time    : 2025/8/19 10:54
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
from typing import TYPE_CHECKING

import httpx
from mcp.client.session import MessageHandlerFnT

from a2c_smcp.computer.mcp_clients.base_client import BaseMCPClient
from a2c_smcp.computer.mcp_clients.http_client import HttpMCPClient
from a2c_smcp.computer.mcp_clients.model import MCPServerConfig, SseServerConfig, StdioServerConfig, StreamableHttpServerConfig
from a2c_smcp.computer.mcp_clients.sse_client import SseMCPClient
from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient

if TYPE_CHECKING:
    from a2c_smcp.computer.mcp_clients.oauth_coordinator import OAuthCoordinator


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
    match config:
        case StdioServerConfig():
            client = StdioMCPClient(config.server_parameters, message_handler=message_handler)
        case SseServerConfig():
            client = SseMCPClient(config.server_parameters, message_handler=message_handler)
        case StreamableHttpServerConfig():
            client = HttpMCPClient(
                config.server_parameters,
                message_handler=message_handler,
                oauth_coordinator=oauth_coordinator,
                httpx_transport=httpx_transport,
            )
        case _:
            raise ValueError(f"Unsupported config type: {type(config)}")  # noqa
    return client
