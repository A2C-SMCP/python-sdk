# -*- coding: utf-8 -*-
# filename: conftest.py
# @Time    : 2025/9/30 16:55
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文：集成测试全局fixtures，提供Socket.IO测试服务器与端口。
English: Global fixtures for integration tests, providing Socket.IO test server and free port.
"""

import socket
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest
from socketio import ASGIApp

from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.testing import UvicornTestServer
from tests.integration_tests.mock_socketio_server import MockComputerServerNamespace, create_computer_test_socketio


@pytest.fixture(autouse=True)
def isolate_a2c_identity_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """v0.5.0（protocol#64）：隔离 Computer 身份持久化目录（``$XDG_CONFIG_HOME/a2c``）。

    缺了它，集成测试里任何真实 ``SMCPComputerClient`` 都会读写开发者本机的
    ``~/.config/a2c/computer-id``——测试之间共享身份、并污染真实配置。
    Isolate the persisted Computer identity so integration tests never touch the real config.
    """
    xdg_home = tmp_path / "xdg-config"
    xdg_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    yield xdg_home


@pytest.fixture
def basic_server_port() -> int:
    """
    中文：查找可用端口。
    English: Find an available TCP port.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def socketio_server(basic_server_port: int) -> AsyncGenerator[MockComputerServerNamespace, None]:
    """
    中文：启动基于标准SMCPNamespace的测试服务器，返回命名空间以便测试访问。
    English: Start test server based on standard SMCPNamespace and return the namespace for test access.
    """
    sio = create_computer_test_socketio()
    # 避免关闭时后台任务异常 / avoid background task issues on shutdown
    sio.eio.start_service_task = False
    asgi_app = ASGIApp(sio, socketio_path="/socket.io")

    server = UvicornTestServer(asgi_app, port=basic_server_port)
    await server.up()
    try:
        yield sio.namespace_handlers[SMCP_NAMESPACE]  # type: ignore[index]
    finally:
        # 强制快速关闭，不等待连接清理 / Force fast shutdown without waiting for connection cleanup
        await server.down(force=True)
