# -*- coding: utf-8 -*-
"""
中文：Computer 客户端握手配置测试（对齐 Rust ``crates/smcp-computer/tests/handshake_config_test.rs`` #86 变更）。
English: Handshake config tests for the Computer-side client.

#112(AS-38)：连接面鉴权改走 Socket.IO ``auth`` dict（字段 ``token``）。Computer 客户端**不持有凭据、
不构造 auth**——auth dict 由调用方在 ``connect(url, auth=...)`` 提供（CLI 经 ``--auth`` 注入）。
故本客户端不再有 ``auth_field_name``/header 鉴权配置项，仅保留 ``namespace`` 握手配置。

覆盖场景 / Covered scenarios:
  1. 自定义 namespace 贯穿所有事件处理器注册
  2. namespace getter 返回构造器传入的真实值
  3. 未显式指定时默认落在 ``/smcp``
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import (
    CANCEL_TOOL_CALL_NOTIFICATION,
    GET_BLOB_EVENT,
    GET_CONFIG_EVENT,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_SKILL_EVENT,
    GET_SKILLS_EVENT,
    GET_TOOLS_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
)


def test_computer_client_default_handshake_config() -> None:
    """未显式指定时，Computer 客户端默认 namespace 为 ``/smcp`` / default namespace is ``/smcp``"""
    client = SMCPComputerClient(computer=MagicMock())

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
        GET_SKILLS_EVENT,
        GET_SKILL_EVENT,
        CANCEL_TOOL_CALL_NOTIFICATION,  # #96：notify:tool_call_cancel 接收处理器
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


def test_computer_client_custom_namespace_propagates() -> None:
    """自定义 namespace 独立生效并贯穿处理器注册 / custom namespace propagates to handler registration"""
    client = SMCPComputerClient(
        computer=MagicMock(),
        namespace="/custom",
    )

    assert client.namespace == "/custom"
    assert "/custom" in client.handlers
