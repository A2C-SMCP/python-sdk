# -*- coding: utf-8 -*-
# filename: test_base_client_page.py
# @Time    : 2026/4/29
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文: `base_client.list_resources_page` 单页透传 API 单元测试。
英文: Unit tests for `base_client.list_resources_page` single-page transparent forward API.

覆盖 v0.2 协议指南 §6.3.1 `client:get_resources` 透传链路在 SDK 端的能力基础（Issue #13）。
Covers SDK-side foundation for v0.2 protocol §6.3.1 `client:get_resources` transparent-forward path (Issue #13).
"""
from asyncio import Queue
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import ClientSession
from mcp.types import (
    AnyUrl,
    Implementation,
    InitializeResult,
    ListResourcesResult,
    Resource,
    ResourcesCapability,
    ServerCapabilities,
)
from pydantic import BaseModel

from a2c_smcp.computer.mcp_clients.base_client import (
    STATES,
    BaseMCPClient,
    MCPCapabilityNotSupportedError,
)


class _DummyParams(BaseModel):
    pass


class _MockClient(BaseMCPClient):
    """最小可连接 client，便于单元测试 list_resources_page。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._async_session = None
        self.aexit_stack = AsyncMock(spec=AsyncExitStack)

    async def _create_async_session(self) -> ClientSession:
        return MagicMock(spec=ClientSession)


@pytest.fixture
async def state_changes() -> Queue:
    return Queue()


@pytest.fixture
async def connected_client(state_changes):
    """返回已连接的 client，并在 fixture 退出时清理。"""
    async def _on_state_change(src: str, dst: str) -> None:
        await state_changes.put((src, dst))

    client = _MockClient(_DummyParams(), state_change_callback=_on_state_change)
    await client.aconnect()
    yield client
    if client.state == STATES.connected:
        await client.adisconnect()


def _set_caps(client: _MockClient, *, has_resources: bool) -> None:
    client._initialize_result = InitializeResult(
        protocolVersion="2025-06-18",
        capabilities=ServerCapabilities(
            resources=ResourcesCapability(subscribe=False, listChanged=False) if has_resources else None,
        ),
        serverInfo=Implementation(name="test", version="1.0.0"),
    )


@pytest.mark.asyncio
async def test_list_resources_page_first_page_no_cursor(connected_client):
    """
    cursor=None: 调底层 list_resources()（无 cursor 参数），返回 (resources, next_cursor)。
    """
    _set_caps(connected_client, has_resources=True)
    mock_session = connected_client._async_session
    res_a = Resource(uri=AnyUrl("dpe://com.example.docs/a.md"), name="a")
    res_b = Resource(uri=AnyUrl("window://com.example.editor/main"), name="main")
    mock_session.list_resources = AsyncMock(
        return_value=ListResourcesResult(resources=[res_a, res_b], nextCursor="page-2"),
    )

    resources, next_cursor = await connected_client.list_resources_page()

    assert resources == [res_a, res_b]
    assert next_cursor == "page-2"
    # 首次调用不应携带 cursor 参数（保持 MCP 默认行为）
    mock_session.list_resources.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_list_resources_page_with_cursor(connected_client):
    """
    cursor=有值: 透传给底层 list_resources(cursor=...)。
    """
    _set_caps(connected_client, has_resources=True)
    mock_session = connected_client._async_session
    res = Resource(uri=AnyUrl("dpe://com.example.docs/b.md"), name="b")
    mock_session.list_resources = AsyncMock(
        return_value=ListResourcesResult(resources=[res], nextCursor=None),
    )

    resources, next_cursor = await connected_client.list_resources_page(cursor="page-2")

    assert resources == [res]
    assert next_cursor is None
    mock_session.list_resources.assert_awaited_once_with(cursor="page-2")


@pytest.mark.asyncio
async def test_list_resources_page_last_page_returns_none_cursor(connected_client):
    """
    末页: nextCursor 为 None 时透传 None（让调用方据此停止翻页）。
    """
    _set_caps(connected_client, has_resources=True)
    mock_session = connected_client._async_session
    mock_session.list_resources = AsyncMock(
        return_value=ListResourcesResult(resources=[], nextCursor=None),
    )

    resources, next_cursor = await connected_client.list_resources_page(cursor="last-page")

    assert resources == []
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_resources_page_no_capability_raises(connected_client):
    """
    capability 缺失: MCP Server 未声明 `resources` 能力 → 抛 MCPCapabilityNotSupportedError（→ 上层映射 4015）。
    """
    _set_caps(connected_client, has_resources=False)

    with pytest.raises(MCPCapabilityNotSupportedError):
        await connected_client.list_resources_page()


@pytest.mark.asyncio
async def test_list_resources_page_not_connected_raises():
    """
    未连接状态调用应抛 ConnectionError（与 list_tools 行为一致）。
    """
    client = _MockClient(_DummyParams())
    with pytest.raises(ConnectionError, match="Not connected to server"):
        await client.list_resources_page()


@pytest.mark.asyncio
async def test_list_resources_page_does_not_filter_by_scheme(connected_client):
    """
    透明转发: 任意 scheme（dpe / window / 业务自定义）都应原样返回，由 Agent 自决过滤。
    """
    _set_caps(connected_client, has_resources=True)
    mock_session = connected_client._async_session
    items = [
        Resource(uri=AnyUrl("dpe://h/a"), name="a"),
        Resource(uri=AnyUrl("window://h/b"), name="b"),
        Resource(uri=AnyUrl("https://example.com/c"), name="c"),
        Resource(uri=AnyUrl("custom-scheme://h/d"), name="d"),
    ]
    mock_session.list_resources = AsyncMock(
        return_value=ListResourcesResult(resources=items, nextCursor=None),
    )

    resources, _ = await connected_client.list_resources_page()
    assert resources == items


@pytest.mark.asyncio
async def test_list_resources_page_does_not_subscribe(connected_client):
    """
    不订阅资源: 与 list_windows 解耦——透传发现路径不应触发 subscribe_resource。
    """
    _set_caps(connected_client, has_resources=True)
    mock_session = connected_client._async_session
    mock_session.list_resources = AsyncMock(
        return_value=ListResourcesResult(resources=[], nextCursor=None),
    )
    mock_session.subscribe_resource = AsyncMock()

    await connected_client.list_resources_page()

    mock_session.subscribe_resource.assert_not_called()
