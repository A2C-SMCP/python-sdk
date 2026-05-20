# -*- coding: utf-8 -*-
"""
中文：Computer 客户端握手配置测试（对齐 Rust ``crates/smcp-computer/tests/handshake_config_test.rs``）。
English: Handshake config tests for the Computer-side client (mirrors Rust SDK 0.1.15).

覆盖场景 / Covered scenarios:
  1. 默认鉴权 header 名为 ``access_token``
  2. 自定义鉴权 header 名透传到 getter
  3. 自定义 namespace 贯穿所有事件处理器注册
  4. getter 返回构造器传入的真实值
  5. 向后兼容：未显式指定时默认落在 ``/smcp`` 与 ``access_token``
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from a2c_smcp.computer.socketio.client import DEFAULT_AUTH_HEADER_NAME, SMCPComputerClient
from a2c_smcp.smcp import (
    GET_BLOB_EVENT,
    GET_CONFIG_EVENT,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_TOOLS_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
)


def test_default_auth_header_name_is_access_token() -> None:
    """默认鉴权 header 名为 ``access_token`` / Default auth header is ``access_token``"""
    assert DEFAULT_AUTH_HEADER_NAME == "access_token"


def test_computer_client_default_handshake_config() -> None:
    """未显式指定时，Computer 客户端应落在 ``/smcp`` 与 ``access_token`` / Defaults fall back to ``/smcp`` and ``access_token``"""
    client = SMCPComputerClient(computer=MagicMock())

    assert client.namespace == SMCP_NAMESPACE
    assert client.auth_header_name == "access_token"


def test_computer_client_custom_auth_header_name() -> None:
    """构造器传入的自定义 header 名应能从 getter 读回 / Custom header name is exposed via getter"""
    client = SMCPComputerClient(
        computer=MagicMock(),
        auth_header_name="x-custom-auth",
    )

    assert client.auth_header_name == "x-custom-auth"
    # 默认 namespace 不应被误改 / Namespace must stay on default when not overridden
    assert client.namespace == SMCP_NAMESPACE


def test_computer_client_custom_namespace_registers_all_handlers() -> None:
    """自定义 namespace 必须覆盖所有事件处理器注册位点 / Custom namespace must be applied to every handler registration"""
    custom_ns = "/tf-smcp"
    client = SMCPComputerClient(computer=MagicMock(), namespace=custom_ns)

    # getter 反映传入值 / getter reflects injected value
    assert client.namespace == custom_ns

    # handlers 树里只应存在自定义命名空间 / handlers dict should only contain the custom namespace
    assert custom_ns in client.handlers
    assert SMCP_NAMESPACE not in client.handlers

    registered_events = set(client.handlers[custom_ns].keys())
    assert registered_events == {
        TOOL_CALL_EVENT,
        GET_TOOLS_EVENT,
        GET_CONFIG_EVENT,
        GET_DESKTOP_EVENT,
        GET_RESOURCES_EVENT,
        GET_BLOB_EVENT,
    }


@pytest.mark.asyncio
async def test_computer_client_emit_uses_instance_namespace() -> None:
    """emit 未显式传 namespace 时，应回落到实例命名空间 / emit without explicit namespace falls back to instance namespace"""
    custom_ns = "/tf-smcp"
    client = SMCPComputerClient(computer=MagicMock(), namespace=custom_ns)

    captured: dict = {}

    async def fake_super_emit(self, event, data=None, namespace=None, callback=None):  # noqa: ANN001
        captured["namespace"] = namespace
        captured["event"] = event

    # 替换父类 AsyncClient.emit 以捕获最终 namespace / Patch AsyncClient.emit to capture resolved namespace
    from socketio import AsyncClient

    original = AsyncClient.emit
    AsyncClient.emit = fake_super_emit  # type: ignore[assignment]
    try:
        await client.emit("server:some_event", {"x": 1})
    finally:
        AsyncClient.emit = original  # type: ignore[assignment]

    assert captured["namespace"] == custom_ns
    assert captured["event"] == "server:some_event"


def test_computer_client_full_custom_handshake() -> None:
    """namespace + header 同时自定义时两者独立生效 / Custom namespace and header propagate independently"""
    client = SMCPComputerClient(
        computer=MagicMock(),
        namespace="/custom",
        auth_header_name="x-tf-token",
    )

    assert client.namespace == "/custom"
    assert client.auth_header_name == "x-tf-token"
    assert "/custom" in client.handlers
