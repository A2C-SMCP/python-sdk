# -*- coding: utf-8 -*-
# filename: test_tool_call_binary_roundtrip.py
# @Author  : JQQ
# @Software: PyCharm

"""
v0.2.1 ``tool_call`` 二进制旁路端到端集成测试 / End-to-end ``tool_call`` binary sideband roundtrip.

协议依据 / Protocol:
  - a2c-smcp-protocol blob-transfer.md §5（生产者通道接入契约）
  - data-structures.md §BlobHandle（MCP CallToolResult content item _meta.a2c_blob_handle 旁路）
  - events.md §client:tool_call + §client:get_blob

测试 / Test:
  - Agent emit_tool_call → Computer aexecute_tool 返回超 inline_budget 二进制 →
    on_tool_call 自动铸 toolspool handle → Server 透传 → Agent emit_tool_call 返回前
    自动 drain_blob 还原 → CallToolResult.content 字节与铸造时**完全一致**
  - 内联小图原样保留路径（不走 sideband）
"""

from __future__ import annotations

import base64
import hashlib
import socket
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent
from socketio import ASGIApp, AsyncServer

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.computer.blob import BlobThresholds
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import SMCP_NAMESPACE
from tests.integration_tests.computer.socketio.mock_uv_server import UvicornTestServer
from tests.integration_tests.mock_socketio_server import MockComputerServerNamespace


@pytest.fixture()
def server_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
async def smcp_server(server_port: int) -> AsyncGenerator[None, None]:
    sio = AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        ping_timeout=10,
        ping_interval=10,
        async_handlers=True,
    )
    sio.register_namespace(MockComputerServerNamespace())
    sio.eio.start_service_task = False
    asgi_app = ASGIApp(sio, socketio_path="/socket.io")
    server = UvicornTestServer(asgi_app, port=server_port)
    await server.up()
    try:
        yield
    finally:
        await server.down(force=True)


class TestToolCallBinaryRoundTrip:
    """End-to-end：mint (Computer) → wire (Server route) → drain (Agent) → 字节一致."""

    @pytest.mark.asyncio
    async def test_oversize_image_roundtrip_bytes_identical(
        self,
        smcp_server: None,
        server_port: int,
        tmp_path: Path,
    ) -> None:
        office_id = "office-tc-1"
        # Computer：inline_budget 故意设小（128B）使任何 1 KiB+ 图像必走 sideband
        # Computer with intentionally small inline_budget so any 1 KiB+ image goes via sideband
        computer = Computer(
            name="comp-tc-1",
            blob_cache_root=tmp_path,
            blob_thresholds=BlobThresholds(inline_budget=128, too_large_cap=10 * 1024 * 1024),
        )
        # mock aexecute_tool 返回固定大图 / Stub aexecute_tool to return fixed large image
        payload = bytes((i * 37 + 11) % 256 for i in range(4096))  # 4 KiB random-ish bytes
        expected_sha = hashlib.sha256(payload).hexdigest()
        result = CallToolResult(
            content=[ImageContent(type="image", data=base64.b64encode(payload).decode("ascii"), mimeType="image/png")],
            isError=False,
        )
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        comp_client = SMCPComputerClient(computer=computer)
        await comp_client.connect(
            f"http://localhost:{server_port}",
            socketio_path="/socket.io",
            auth={"mock": "ok"},
            namespaces=[SMCP_NAMESPACE],
        )
        await comp_client.join_office(office_id)

        agent = AsyncSMCPAgentClient(auth_provider=DefaultAgentAuthProvider(agent_id="robot-tc-1", office_id=office_id))
        await agent.connect_to_server(
            f"http://localhost:{server_port}",
            namespace=SMCP_NAMESPACE,
            socketio_path="/socket.io",
        )
        # 加入同房间 / Join same office
        from a2c_smcp.smcp import JOIN_OFFICE_EVENT

        ok, err = await agent.call(
            JOIN_OFFICE_EVENT,
            {"role": "agent", "office_id": office_id, "name": "robot-tc-1"},
            namespace=SMCP_NAMESPACE,
        )
        assert ok and err is None

        try:
            ret = await agent.emit_tool_call(
                computer="comp-tc-1",
                tool_name="screenshot",
                params={},
                timeout=10,
            )
            # 协议关键不变量：返回的 CallToolResult 与小图内联完全等价
            # Critical invariant: returned CallToolResult is byte-identical to inline-path result
            assert ret.isError is False
            assert len(ret.content) == 1
            item = ret.content[0]
            assert isinstance(item, ImageContent)
            decoded = base64.b64decode(item.data)
            assert decoded == payload
            assert hashlib.sha256(decoded).hexdigest() == expected_sha
            # _meta.a2c_* 字段已被 Agent 端 drain 后清理（透明 sideband）
            # _meta.a2c_* fields cleared post-drain (transparent sideband)
            assert item.meta is None or "a2c_blob_handle" not in (item.meta or {})
        finally:
            await agent.disconnect()
            await comp_client.disconnect()

    @pytest.mark.asyncio
    async def test_small_image_stays_inline_no_sideband(
        self,
        smcp_server: None,
        server_port: int,
        tmp_path: Path,
    ) -> None:
        """小尺寸图像（≤ inline_budget）原样内联，不走 sideband / Below-budget image stays inline."""
        office_id = "office-tc-2"
        computer = Computer(
            name="comp-tc-2",
            blob_cache_root=tmp_path,
            blob_thresholds=BlobThresholds(inline_budget=4096),  # 4 KiB budget
        )
        # 8 字节 PNG header，远小于 budget / 8-byte PNG header well below budget
        small_payload = b"\x89PNG\r\n\x1a\n"
        result = CallToolResult(
            content=[ImageContent(type="image", data=base64.b64encode(small_payload).decode("ascii"), mimeType="image/png")],
            isError=False,
        )
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        comp_client = SMCPComputerClient(computer=computer)
        await comp_client.connect(
            f"http://localhost:{server_port}",
            socketio_path="/socket.io",
            auth={"mock": "ok"},
            namespaces=[SMCP_NAMESPACE],
        )
        await comp_client.join_office(office_id)

        agent = AsyncSMCPAgentClient(auth_provider=DefaultAgentAuthProvider(agent_id="robot-tc-2", office_id=office_id))
        await agent.connect_to_server(
            f"http://localhost:{server_port}",
            namespace=SMCP_NAMESPACE,
            socketio_path="/socket.io",
        )
        from a2c_smcp.smcp import JOIN_OFFICE_EVENT

        ok, err = await agent.call(
            JOIN_OFFICE_EVENT,
            {"role": "agent", "office_id": office_id, "name": "robot-tc-2"},
            namespace=SMCP_NAMESPACE,
        )
        assert ok and err is None

        try:
            ret = await agent.emit_tool_call(
                computer="comp-tc-2",
                tool_name="ping",
                params={},
                timeout=10,
            )
            assert ret.isError is False
            item = ret.content[0]
            assert isinstance(item, ImageContent)
            decoded = base64.b64decode(item.data)
            assert decoded == small_payload
        finally:
            await agent.disconnect()
            await comp_client.disconnect()
