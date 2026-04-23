# -*- coding: utf-8 -*-
"""
中文：Computer 端 Socket.IO 客户端自定义 namespace 的集成测试。

背景：修复前 ``SMCPComputerClient`` 将事件处理器绑死在 ``/smcp``，即便 CLI
通过 ``--namespace`` 覆盖了连接命名空间，也会导致服务端下发到 ``/<custom>``
的事件被客户端静默丢弃。本测试用真实 Socket.IO (uvicorn + python-socketio)
在 ``/tf-smcp`` 路径下跑一次 ``client:tool_call`` 下行，验证 Computer 正常
收到并执行工具调用。

English: Integration test for ``SMCPComputerClient`` on a custom Socket.IO
namespace. Pre-fix, event handlers were pinned to ``/smcp`` regardless of the
connect-time namespace, so server-dispatched events on ``/<custom>`` were
silently dropped. This test brings up a real Socket.IO server (uvicorn +
python-socketio) on ``/tf-smcp`` and exercises a ``client:tool_call``
round-trip, asserting the Computer receives and executes the tool call.

注意：Server 侧的 ``SMCPNamespace`` 内部仍有 ``/smcp`` 硬编码（下次迭代处理），
因此本测试绕开 ``SMCPNamespace`` 的事件转发逻辑，直接从 AsyncNamespace 下发
``client:tool_call``。这已足够验证 Computer 端握手 & 事件订阅在自定义 namespace
下的正确性——也正是本次修复的范围。
Note: The Server's ``SMCPNamespace`` still hardcodes ``/smcp`` for internal
routing (scheduled for a follow-up). We therefore bypass the Server's event
forwarding and dispatch ``client:tool_call`` directly from the namespace.
That is enough to exercise the Computer-side handshake/subscription behaviour
on a custom namespace, which is the scope of this fix.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult, TextContent
from socketio import ASGIApp, AsyncNamespace, AsyncServer

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import TOOL_CALL_EVENT
from a2c_smcp.utils.logger import logger
from tests.integration_tests.computer.socketio.mock_uv_server import UvicornTestServer

CUSTOM_NAMESPACE = "/tf-smcp"


class _CustomNamespaceServer(AsyncNamespace):
    """
    中文：最小的自定义命名空间服务端，仅用于观测连接与记录 sid。
    English: Minimal custom-namespace server side, used only to observe connections and record sids.
    """

    def __init__(self, namespace_path: str) -> None:
        super().__init__(namespace=namespace_path)
        self.connected_sids: list[str] = []

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:  # type: ignore[override]
        self.connected_sids.append(sid)
        logger.info(f"[custom-ns] connected sid={sid} on {self.namespace}")
        return True

    async def on_disconnect(self, sid: str) -> None:  # type: ignore[override]
        if sid in self.connected_sids:
            self.connected_sids.remove(sid)


@pytest.fixture
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def mock_computer() -> Computer:
    comp = MagicMock(spec=Computer)
    comp.aexecute_tool = AsyncMock(
        return_value=CallToolResult(
            isError=False,
            content=[TextContent(text="ok", type="text")],
        ),
    )
    comp.aget_available_tools = AsyncMock(return_value=[])
    return comp


@pytest.fixture
async def custom_ns_server(free_port: int) -> AsyncGenerator[tuple[_CustomNamespaceServer, AsyncServer, int], Any]:
    """
    中文：启动绑定到 ``/tf-smcp`` 的 AsyncServer。
    English: Bring up an AsyncServer bound to ``/tf-smcp``.
    """
    sio = AsyncServer(async_mode="asgi", cors_allowed_origins="*", async_handlers=True)
    sio.eio.start_service_task = False
    ns = _CustomNamespaceServer(CUSTOM_NAMESPACE)
    sio.register_namespace(ns)

    app = ASGIApp(sio, socketio_path="/socket.io")
    server = UvicornTestServer(app, port=free_port)
    await server.up()
    try:
        yield ns, sio, free_port
    finally:
        await server.down(force=True)


@pytest.mark.asyncio
async def test_computer_client_receives_tool_call_on_custom_namespace(
    mock_computer: Computer,
    custom_ns_server: tuple[_CustomNamespaceServer, AsyncServer, int],
) -> None:
    """
    中文：端到端验证 ``SMCPComputerClient(namespace="/tf-smcp")`` 能接收并处理
    下行的 ``client:tool_call`` 事件。修复前事件处理器绑定到 ``/smcp``，即使客户
    端成功在 ``/tf-smcp`` 建立连接，服务端下发到 ``/tf-smcp`` 的事件也会丢失 ——
    ``mock_computer.aexecute_tool`` 不会被调用，断言会失败。

    English: End-to-end check that ``SMCPComputerClient(namespace="/tf-smcp")``
    receives and processes a ``client:tool_call`` dispatched by the server on
    ``/tf-smcp``. Pre-fix, handlers were bound to ``/smcp`` so events dispatched
    on ``/tf-smcp`` were silently dropped — ``mock_computer.aexecute_tool``
    would never be invoked and the assertion below would fail.
    """
    ns, sio, port = custom_ns_server
    mock_computer.name = "test-computer"

    client = SMCPComputerClient(computer=mock_computer, namespace=CUSTOM_NAMESPACE)

    # 前置自洽：事件处理器必须全部注册在自定义 namespace 下
    # Pre-condition: all handlers must be bound to the custom namespace
    assert CUSTOM_NAMESPACE in client.handlers
    assert "/smcp" not in client.handlers
    assert TOOL_CALL_EVENT in client.handlers[CUSTOM_NAMESPACE]

    await client.connect(
        f"http://127.0.0.1:{port}",
        socketio_path="/socket.io",
        namespaces=[CUSTOM_NAMESPACE],
    )

    # 等待连接建立 / wait for handshake
    for _ in range(50):
        if ns.connected_sids:
            break
        await asyncio.sleep(0.02)
    assert ns.connected_sids, "client did not connect to /tf-smcp"
    computer_sid = client.namespaces[CUSTOM_NAMESPACE]

    # 服务端向客户端下发 tool_call / server dispatches tool_call to client
    tool_call_req = {
        "computer": "test-computer",
        "tool_name": "echo",
        "params": {"msg": "hello"},
        "agent": "test-agent",
        "req_id": "req-ns-1",
        "timeout": 5,
    }
    await sio.emit(
        TOOL_CALL_EVENT,
        tool_call_req,
        to=computer_sid,
        namespace=CUSTOM_NAMESPACE,
    )

    # 等待客户端侧处理 / wait for client-side handling
    for _ in range(100):
        if mock_computer.aexecute_tool.called:
            break
        await asyncio.sleep(0.02)

    assert mock_computer.aexecute_tool.called, (
        "SMCPComputerClient did not receive the tool_call dispatched on the custom "
        "namespace. This is the regression — event handlers were bound to the "
        "hardcoded /smcp namespace so events on /tf-smcp were silently dropped."
    )
    mock_computer.aexecute_tool.assert_called_with(
        req_id="req-ns-1",
        tool_name="echo",
        parameters={"msg": "hello"},
        timeout=5,
    )

    await client.disconnect()
