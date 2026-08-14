# -*- coding: utf-8 -*-
# filename: conftest.py
# @Time    : 2025/10/05 16:20
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文: E2E 测试根目录公共夹具，提供 Computer-Agent-Server 三者集成测试所需的基础设施。
English: Root-level E2E test fixtures providing infrastructure for Computer-Agent-Server integration tests.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.testing import (
    UvicornTestServer,
    create_local_async_server,
    create_local_sync_server,
    run_http_server,
)

# ============================================================================
# 中文: 服务端装配与 runner 已下沉至 a2c_smcp.testing（#187），此处仅 re-export 供本目录测试引用
# English: Assembly + runners now live in a2c_smcp.testing (#187); re-exported here for local tests
# ============================================================================


@pytest.fixture(scope="session")
def integration_server_endpoint() -> Iterator[str]:
    """
    中文: 提供形如 http://127.0.0.1:PORT 的服务端地址，用于集成测试。
    English: Provide server endpoint like http://127.0.0.1:PORT for integration tests.
    """
    with run_http_server() as (host, port):
        yield f"http://{host}:{port}"


# ============================================================================
# 中文: MCP Server 配置辅助函数 / English: MCP Server config helpers
# ============================================================================


def create_mcp_server_config(
    name: str,
    script_path: str,
    disabled: bool = False,
) -> dict[str, Any]:
    """
    中文: 创建 MCP Server 配置，用于 Computer 端测试
    English: Create MCP Server config for Computer-side testing
    """
    return {
        "name": name,
        "type": "stdio",
        "disabled": disabled,
        "forbidden_tools": [],
        "tool_meta": {},
        "default_tool_meta": {
            "auto_apply": True,
        },
        "server_parameters": {
            "command": sys.executable,  # 使用当前 Python 解释器 / Use current Python interpreter
            "args": [script_path],
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
    }


@pytest.fixture
def mcp_server_config_path(tmp_path: Path) -> Path:
    """
    中文: 创建临时 MCP Server 配置文件路径
    English: Create temporary MCP Server config file path
    """
    config_file = tmp_path / "mcp_servers.json"
    # 创建一个基础配置，包含测试用的 MCP Server
    # Create a basic config with test MCP Server
    config = create_mcp_server_config(
        name="e2e-test-server",
        script_path="tests/integration_tests/computer/mcp_servers/resources_subscribe_stdio_server.py",
    )
    config_file.write_text(json.dumps([config], ensure_ascii=False), encoding="utf-8")
    return config_file


# ============================================================================
# 中文: 异步服务器相关 fixtures（装配下沉至 a2c_smcp.testing，#187）
# English: Async server related fixtures (assembly now from a2c_smcp.testing, #187)
# ============================================================================


@pytest.fixture
def async_integration_server_port() -> int:
    """
    中文: 查找可用端口用于异步集成服务器。
    English: Find an available TCP port for async integration server.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
async def async_integration_socketio_server(async_integration_server_port: int):
    """
    中文: 启动基于 SMCPNamespace 的异步集成测试服务器，返回命名空间。
    English: Start async integration test server based on SMCPNamespace and return the namespace.
    """
    setup_start = time.time()
    sio, ns, asgi_app = create_local_async_server()
    print(f"[E2E Fixture] Server creation took {time.time() - setup_start:.2f}s")

    server_start = time.time()
    server = UvicornTestServer(asgi_app, port=async_integration_server_port)
    await server.up()
    print(f"[E2E Fixture] Server startup took {time.time() - server_start:.2f}s")

    try:
        yield ns
    finally:
        shutdown_start = time.time()
        print(f"[E2E Fixture] Starting shutdown Server {shutdown_start}")
        # 强制快速关闭，不等待连接清理 / Force fast shutdown without waiting for connection cleanup
        await server.down(force=True)
        print(f"[E2E Fixture] Server shutdown took {time.time() - shutdown_start:.2f}s")
