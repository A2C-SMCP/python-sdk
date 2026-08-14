# -*- coding: utf-8 -*-
# filename: conftest.py
# @Time    : 2025/10/05 15:47
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文: e2e Agent 测试公共夹具。启动真实 Socket.IO HTTP 服务器，并提供 Computer 真实客户端模拟。
English: Common fixtures for e2e Agent tests. Boots a real Socket.IO HTTP server and provides real Computer client mock.
"""

from __future__ import annotations

import contextlib
import socket
import time
from collections.abc import Iterator
from typing import Any

import pytest
import socketio

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.smcp import GET_DESKTOP_EVENT, GET_TOOLS_EVENT, SMCP_NAMESPACE, TOOL_CALL_EVENT
from a2c_smcp.testing import (
    UvicornTestServer,
    create_local_async_server,
    create_local_sync_server,
    run_http_server,
)

# ============================================================================
# 中文: 服务端装配与 runner 已下沉至 a2c_smcp.testing（#187）——原裸装配副本缺版本握手
#       中间件（#93 假绿风险），现已统一为生产等价装配，裸客户端需显式携带 a2c_version
# English: Assembly + runners now from a2c_smcp.testing (#187); the old bare copy lacked the
#       version-handshake middleware (#93 false-green risk) — now production-equivalent, and
#       raw clients must carry a2c_version explicitly
# ============================================================================


@pytest.fixture(scope="session")
def server_endpoint() -> Iterator[str]:
    """
    中文: 提供形如 http://127.0.0.1:PORT 的服务端地址。
    English: Provide server endpoint like http://127.0.0.1:PORT
    """
    with run_http_server() as (host, port):
        yield f"http://{host}:{port}"


@contextlib.contextmanager
def _mock_computer_client(url: str) -> Iterator[socketio.Client]:
    """
    中文: 创建并连接一个模拟 Computer 的 socketio.Client，自动注册工具处理器。
    English: Create and connect a mock Computer socketio.Client, auto register tool handlers.
    """
    client = socketio.Client()

    # 注册 Computer 端的请求处理器 / Register Computer-side request handlers
    def _on_get_tools(data):
        # 返回最简工具列表 / minimal tool list
        return {
            "tools": [
                {
                    "name": "echo",
                    "bundle_id": "echosrv",  # #152 D1：required，name ≠ bundle_id 分叉
                    "description": "echo input",
                    "params_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
                    "return_schema": {"type": "object"},
                },
                {
                    "name": "add",
                    "bundle_id": "addsrv",  # #152 D1：required，name ≠ bundle_id 分叉
                    "description": "add two numbers",
                    "params_schema": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                    },
                    "return_schema": {"type": "object"},
                },
            ],
            "req_id": data.get("req_id", "r1"),
        }

    def _on_get_desktop(data):
        return {"desktops": ["window://1", "window://2"], "req_id": data.get("req_id", "r2")}

    def _on_tool_call(data):
        # 简单的工具调用实现 / Simple tool call implementation
        tool_name = data.get("tool_name")
        params = data.get("params", {})

        if tool_name == "echo":
            return {
                "content": [{"type": "text", "text": f"Echo: {params.get('message', '')}"}],
                "isError": False,
            }
        elif tool_name == "add":
            a = params.get("a", 0)
            b = params.get("b", 0)
            return {
                "content": [{"type": "text", "text": f"Result: {a + b}"}],
                "isError": False,
            }
        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

    client.on(GET_TOOLS_EVENT, _on_get_tools, namespace=SMCP_NAMESPACE)
    client.on(GET_DESKTOP_EVENT, _on_get_desktop, namespace=SMCP_NAMESPACE)
    client.on(TOOL_CALL_EVENT, _on_tool_call, namespace=SMCP_NAMESPACE)

    client.connect(
        f"{url}?a2c_version={PROTOCOL_VERSION}",  # 裸客户端不自动携带版本，必须显式带出 / raw client must carry it explicitly
        socketio_path="/socket.io",
        namespaces=[SMCP_NAMESPACE],
        transports=["polling"],  # 仅使用轮询，避免WSGI环境的WebSocket升级失败 / force polling
        wait=True,
        wait_timeout=10,
    )
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            client.disconnect()


@pytest.fixture()
def mock_computer_client(server_endpoint: str) -> Iterator[socketio.Client]:
    """
    中文: 已连接到 Server 的模拟 Computer 客户端（同步）。
    English: Connected mock Computer client (sync).
    """
    with _mock_computer_client(server_endpoint) as c:
        yield c


# ============================================================================
# 中文: 异步服务器相关 fixtures
# English: Async server related fixtures
# ============================================================================


@pytest.fixture
def async_server_port() -> int:
    """
    中文: 查找可用端口用于异步服务器。
    English: Find an available TCP port for async server.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
async def async_socketio_server(async_server_port: int):
    """
    中文: 启动基于 SMCPNamespace 的异步测试服务器，返回命名空间。
    English: Start async test server based on SMCPNamespace and return the namespace.
    """
    import time

    start = time.time()
    sio, ns, asgi_app = create_local_async_server()

    server = UvicornTestServer(asgi_app, port=async_server_port)
    await server.up()
    print(f"\n[E2E] Async server started on port {async_server_port} in {time.time() - start:.2f}s")
    try:
        yield ns
    finally:
        shutdown_start = time.time()
        # 强制快速关闭，不等待连接清理 / Force fast shutdown without waiting for connection cleanup
        await server.down(force=True)
        print(f"[E2E] Async server shutdown in {time.time() - shutdown_start:.2f}s")


@pytest.fixture()
async def async_mock_computer_client(async_socketio_server, async_server_port: int):
    """
    中文: 已连接到异步 Server 的模拟 Computer 客户端。每个测试创建独立客户端，但复用服务器。
    English: Connected mock Computer client (async). Each test creates its own client but reuses the server.
    """
    import time

    start = time.time()
    client = socketio.AsyncClient()
    print("\n[E2E] Creating async computer client...")

    # 注册 Computer 端的请求处理器 / Register Computer-side request handlers
    async def _on_get_tools(data):
        return {
            "tools": [
                {
                    "name": "echo",
                    "bundle_id": "echosrv",  # #152 D1：required，name ≠ bundle_id 分叉
                    "description": "echo input",
                    "params_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
                    "return_schema": {"type": "object"},
                },
                {
                    "name": "add",
                    "bundle_id": "addsrv",  # #152 D1：required，name ≠ bundle_id 分叉
                    "description": "add two numbers",
                    "params_schema": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                    },
                    "return_schema": {"type": "object"},
                },
            ],
            "req_id": data.get("req_id", "r1"),
        }

    async def _on_get_desktop(data):
        return {"desktops": ["window://1", "window://2"], "req_id": data.get("req_id", "r2")}

    async def _on_tool_call(data):
        tool_name = data.get("tool_name")
        params = data.get("params", {})

        if tool_name == "echo":
            return {
                "content": [{"type": "text", "text": f"Echo: {params.get('message', '')}"}],
                "isError": False,
            }
        elif tool_name == "add":
            a = params.get("a", 0)
            b = params.get("b", 0)
            return {
                "content": [{"type": "text", "text": f"Result: {a + b}"}],
                "isError": False,
            }
        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

    client.on(GET_TOOLS_EVENT, _on_get_tools, namespace=SMCP_NAMESPACE)
    client.on(GET_DESKTOP_EVENT, _on_get_desktop, namespace=SMCP_NAMESPACE)
    client.on(TOOL_CALL_EVENT, _on_tool_call, namespace=SMCP_NAMESPACE)

    await client.connect(
        f"http://127.0.0.1:{async_server_port}?a2c_version={PROTOCOL_VERSION}",  # 裸客户端显式携带版本 / raw client carries it explicitly
        socketio_path="/socket.io",
        namespaces=[SMCP_NAMESPACE],
        transports=["polling"],  # 仅使用 polling，避免 WebSocket 关闭延迟 / Use polling only to avoid WebSocket close delay
        wait=True,
        wait_timeout=5,
    )
    print(f"[E2E] Async computer client connected in {time.time() - start:.2f}s")
    try:
        yield client
    finally:
        disconnect_start = time.time()
        # 快速断开，不等待清理 / Fast disconnect without waiting for cleanup
        if client.connected:
            await client.eio.disconnect(abort=True)  # 强制断开 / Force disconnect
        print(f"[E2E] Async computer client disconnected in {time.time() - disconnect_start:.2f}s")
