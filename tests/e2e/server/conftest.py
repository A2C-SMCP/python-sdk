# -*- coding: utf-8 -*-
# filename: conftest.py
# @Time    : 2025/10/05 14:10
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文: e2e Server 测试公共夹具。启动真实 Socket.IO HTTP 服务器，并提供 Agent 与 Computer 真实客户端。
English: Common fixtures for e2e Server tests. Boots a real Socket.IO HTTP server and provides real Agent/Computer clients.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterator

import pytest
import socketio

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.testing import (
    UvicornTestServer,
    create_local_async_server,
    create_local_sync_server,
    run_http_server,
)

# ============================================================================
# 中文: 服务端装配与 runner 已下沉至 a2c_smcp.testing（#187）——本 conftest 的裸装配副本
#       曾缺失版本握手中间件（#93 假绿风险），现已统一为生产等价装配
# English: Assembly + runners now from a2c_smcp.testing (#187); the old bare copy lacked the
#       version-handshake middleware (#93 false-green risk) — now production-equivalent
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
def _socketio_client(url: str) -> Iterator[socketio.Client]:
    """
    中文: 创建并连接一个真实 socketio.Client，自动断开与关闭。
    English: Create and connect a real socketio.Client, auto cleanup.
    """
    client = socketio.Client()
    client.connect(
        f"{url}?a2c_version={PROTOCOL_VERSION}",  # 裸客户端不自动携带版本，必须显式带出 / raw client must carry it explicitly
        socketio_path="/socket.io",
        namespaces=[SMCP_NAMESPACE],
        transports=["polling"],  # 仅使用轮询，避免WSGI环境的WebSocket升级失败 / force polling to avoid websocket upgrade under WSGI
        wait=True,
        wait_timeout=10,
    )
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            client.disconnect()


@pytest.fixture()
def agent_client(server_endpoint: str) -> Iterator[socketio.Client]:
    """
    中文: 已连接到 Server 的 Agent 客户端（同步）。
    English: Connected Agent client (sync).
    """
    with _socketio_client(server_endpoint) as c:
        yield c


@pytest.fixture()
def computer_client(server_endpoint: str) -> Iterator[socketio.Client]:
    """
    中文: 已连接到 Server 的 Computer 客户端（同步）。
    English: Connected Computer client (sync).
    """
    with _socketio_client(server_endpoint) as c:
        yield c


# ============================================================================
# 中文: 异步服务器相关 fixtures（装配下沉至 a2c_smcp.testing，#187）
# English: Async server related fixtures (assembly now from a2c_smcp.testing, #187)
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
    sio, ns, asgi_app = create_local_async_server()

    server = UvicornTestServer(asgi_app, port=async_server_port)
    await server.up()
    try:
        yield ns
    finally:
        # 强制快速关闭，不等待连接清理 / Force fast shutdown without waiting for connection cleanup
        await server.down(force=True)


@pytest.fixture()
async def async_agent_client(async_socketio_server, async_server_port: int):
    """
    中文: 已连接到异步 Server 的 Agent 客户端。
    English: Connected Agent client (async).
    """
    import asyncio

    client = socketio.AsyncClient()
    await client.connect(
        f"http://127.0.0.1:{async_server_port}?a2c_version={PROTOCOL_VERSION}",  # 裸客户端显式携带版本 / raw client carries it explicitly
        socketio_path="/socket.io",
        namespaces=[SMCP_NAMESPACE],
        transports=["polling"],
        wait=True,
        wait_timeout=10,
    )
    try:
        yield client
    finally:
        # 中文: 等待一小段时间确保所有事件处理完成 / English: Wait briefly to ensure all events are processed
        await asyncio.sleep(0.05)
        with contextlib.suppress(Exception):
            # 中文: 使用超时避免长时间等待 / English: Use timeout to avoid long wait
            await asyncio.wait_for(client.disconnect(), timeout=0.5)


@pytest.fixture()
async def async_computer_client(async_socketio_server, async_server_port: int):
    """
    中文: 已连接到异步 Server 的 Computer 客户端。
    English: Connected Computer client (async).
    """
    import asyncio

    client = socketio.AsyncClient()
    await client.connect(
        f"http://127.0.0.1:{async_server_port}?a2c_version={PROTOCOL_VERSION}",  # 裸客户端显式携带版本 / raw client carries it explicitly
        socketio_path="/socket.io",
        namespaces=[SMCP_NAMESPACE],
        transports=["polling"],
        wait=True,
        wait_timeout=10,
    )
    try:
        yield client
    finally:
        # 中文: 等待一小段时间确保所有事件处理完成 / English: Wait briefly to ensure all events are processed
        await asyncio.sleep(0.05)
        with contextlib.suppress(Exception):
            # 中文: 使用超时避免长时间等待 / English: Use timeout to avoid long wait
            await asyncio.wait_for(client.disconnect(), timeout=0.5)
