# -*- coding: utf-8 -*-
# filename: test_base_client_windows.py
# @Time    : 2025/10/02 17:05
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文: 集成测试 BaseMCPClient.list_windows，针对新提供的 Resources 服务器（无订阅/有订阅）。
英文: Integration tests for BaseMCPClient.list_windows with new Resources servers (no subscribe/with subscribe).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient


@pytest.mark.asyncio
async def test_list_windows_without_subscribe_returns_resources() -> None:
    """
    中文: 针对仅 Resources 且不支持订阅的服务器，list_windows 仍应正常返回 window:// 资源。
    英文: For Resources-only server without subscribe, list_windows should still return window:// resources.
    """
    server_py = Path(__file__).resolve().parents[2] / "computer" / "mcp_servers" / "resources_stdio_server.py"
    assert server_py.exists(), f"server script not found: {server_py}"

    params = StdioServerParameters(command=sys.executable, args=[str(server_py)])
    client = StdioMCPClient(params)

    await client.aconnect()
    await client._create_session_success_event.wait()

    # capabilities 检查 / check capabilities
    assert client.initialize_result is not None
    assert client.initialize_result.capabilities.resources is not None
    assert client.initialize_result.capabilities.resources.subscribe is False

    windows = await client.list_windows()
    assert isinstance(windows, list)
    assert len(windows) >= 1, "Resources capability without subscribe should still list window:// resources."
    uris = [str(r.uri) for r in windows]
    assert all(u.startswith("window://") for u in uris)

    await client.adisconnect()
    await client._async_session_closed_event.wait()


@pytest.mark.asyncio
async def test_list_windows_with_subscribe_returns_sorted_and_subscribed() -> None:
    """
    中文: 针对支持订阅的服务器，list_windows 应返回 window:// 资源，并按 ``annotations.priority`` 降序排序。
    英文: For server with subscribe enabled, list_windows should return window:// resources sorted by
          ``annotations.priority`` desc (v0.2 — priority no longer comes from URI query).
    """
    server_py = Path(__file__).resolve().parents[2] / "computer" / "mcp_servers" / "resources_subscribe_stdio_server.py"
    assert server_py.exists(), f"server script not found: {server_py}"

    params = StdioServerParameters(command=sys.executable, args=[str(server_py)])
    client = StdioMCPClient(params)

    await client.aconnect()
    await client._create_session_success_event.wait()

    # capabilities 检查 / check capabilities
    assert client.initialize_result is not None
    assert client.initialize_result.capabilities.resources is not None
    assert client.initialize_result.capabilities.resources.subscribe is True

    windows = await client.list_windows()
    assert isinstance(windows, list)
    assert len(windows) >= 1

    # v0.2: URI 为纯标识符，不再附 ?priority= / ?fullscreen= query
    # v0.2: URIs are pure identifiers (no query metadata)
    uris = [str(r.uri) for r in windows]
    assert all(u.startswith("window://") for u in uris)
    assert all("?" not in u for u in uris), "v0.2 windows MUST NOT carry URI query"

    # 订阅版 mock server 通过 annotations 声明：dashboard(priority=0.9, fullscreen=true) 与 main(priority=0.6)
    # base_client 按 annotations.priority 降序排序，因此 dashboard 在前
    # subscribe-enabled mock declares dashboard(priority=0.9, fullscreen=true) and main(priority=0.6) via annotations;
    # base_client sorts by annotations.priority desc, so dashboard comes first
    assert any("/dashboard" in u for u in uris), "dashboard window expected"
    assert any("/main" in u for u in uris), "main window expected"

    if len(uris) >= 2:
        assert "/dashboard" in uris[0]
        assert "/main" in uris[1]

    # 进一步校验元数据下沉到 annotations / _meta（v0.2 §4.1）
    # Verify metadata sinking into annotations / _meta (v0.2 §4.1)
    by_uri = {str(r.uri): r for r in windows}
    dash = next(r for u, r in by_uri.items() if "/dashboard" in u)
    main = next(r for u, r in by_uri.items() if "/main" in u)
    assert dash.annotations is not None and dash.annotations.priority == 0.9
    assert main.annotations is not None and main.annotations.priority == 0.6
    assert dash.meta == {"fullscreen": True}

    await client.adisconnect()
    await client._async_session_closed_event.wait()
