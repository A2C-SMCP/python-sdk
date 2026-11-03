# -*- coding: utf-8 -*-
# filename: test_utils.py
# @Time    : 2025/8/19 16:58
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
测试 client_factory 工厂方法，确保不同类型配置能正确实例化对应客户端。
Test client_factory factory method to ensure correct client instantiation for each config type.
"""

import pytest
from polyfactory.factories.pydantic_factory import ModelFactory

from a2c_smcp.computer.mcp_clients import base_client as base_client_mod
from a2c_smcp.computer.mcp_clients.http_client import HttpMCPClient
from a2c_smcp.computer.mcp_clients.model import SseServerConfig, StdioServerConfig, StreamableHttpServerConfig
from a2c_smcp.computer.mcp_clients.sse_client import SseMCPClient
from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient
from a2c_smcp.computer.mcp_clients.utils import STDIO_CONNECT_HANDSHAKE_TIMEOUT, client_factory


class StdioServerConfigFactory(ModelFactory[StdioServerConfig]):
    __model__ = StdioServerConfig

    @classmethod
    def vrl(cls) -> str | None:
        """生成有效的VRL脚本或None / Generate valid VRL script or None"""
        return cls.__random__.choice([None, '.result = "success"', ".transformed = true"])


class SseServerConfigFactory(ModelFactory[SseServerConfig]):
    __model__ = SseServerConfig

    @classmethod
    def vrl(cls) -> str | None:
        """生成有效的VRL脚本或None / Generate valid VRL script or None"""
        return cls.__random__.choice([None, '.result = "success"', ".transformed = true"])


class StreamableHttpServerConfigFactory(ModelFactory[StreamableHttpServerConfig]):
    __model__ = StreamableHttpServerConfig

    @classmethod
    def vrl(cls) -> str | None:
        """生成有效的VRL脚本或None / Generate valid VRL script or None"""
        return cls.__random__.choice([None, '.result = "success"', ".transformed = true"])


@pytest.mark.parametrize(
    "config_factory,expected_type",
    [
        (StdioServerConfigFactory, StdioMCPClient),
        (SseServerConfigFactory, SseMCPClient),
        (StreamableHttpServerConfigFactory, HttpMCPClient),
    ],
)
def test_client_factory_type(config_factory, expected_type):
    """
    检查 client_factory 根据不同配置类型返回正确的客户端类型。
    Check client_factory returns the correct client type for each config type.
    """
    config = config_factory.build()
    client = client_factory(config)
    assert isinstance(client, expected_type)


def test_client_factory_handshake_timeout_policy():
    """#247：deadline 按传输类型配置（stdio 冷启动 120s；非 OAuth 30s；交互式 OAuth 无上界）。"""
    stdio_client = client_factory(StdioServerConfigFactory.build())
    assert stdio_client._connect_handshake_timeout == STDIO_CONNECT_HANDSHAKE_TIMEOUT

    expected_default = base_client_mod._CONNECT_HANDSHAKE_TIMEOUT
    assert client_factory(SseServerConfigFactory.build())._connect_handshake_timeout == expected_default
    assert client_factory(StreamableHttpServerConfigFactory.build())._connect_handshake_timeout == expected_default

    # coordinator-attached = 交互式 OAuth：不设 SDK 上界（由 flow TTL 10 分钟 + 取消链约束）
    oauth_client = client_factory(StreamableHttpServerConfigFactory.build(), oauth_coordinator=object())
    assert oauth_client._connect_handshake_timeout is None


def test_client_factory_invalid_type():
    """
    检查 client_factory 对不支持的类型抛出异常。
    Check client_factory raises exception for unsupported config types.
    """

    class DummyConfig:
        pass

    with pytest.raises(ValueError):
        client_factory(DummyConfig())  # noqa
