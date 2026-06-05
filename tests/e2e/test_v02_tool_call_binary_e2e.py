# -*- coding: utf-8 -*-
# filename: test_v02_tool_call_binary_e2e.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：A2C-SMCP v0.2.1 tool_call 二进制旁路真进程 e2e（真实 stdio MCP 子进程）。

  真实 MCP 子进程返回超内联预算的二进制图像 → Computer 在 on_tool_call 铸 ``_meta.a2c_blob_handle``
  旁路 → Server 透传 → Agent ``emit_tool_call`` 返回前自动 ``drain_blob`` 还原 → 字节/sha256 与生产端
  完全一致；小图原样内联（对照组）。这是 ``test_tool_call_binary_roundtrip.py`` 的真进程级抬升
  （集成版用 AsyncMock 替身 aexecute_tool，本 e2e 用真实 MCP 子进程）。

English: Real-process e2e for the v0.2.1 tool_call binary sideband, driven by a real stdio MCP
  subprocess. Oversize image → Computer mints ``_meta.a2c_blob_handle`` → Server relays → Agent
  auto-drains pre-return → bytes/sha256 identical to producer; the small image stays inline.

协议依据 / Protocol: blob-transfer.md §5（生产者通道）；data-structures.md §BlobHandle
  （CallToolResult content item ``_meta.a2c_blob_handle`` 旁路）；events.md §client:tool_call / §client:get_blob.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer import Computer
from a2c_smcp.computer.blob import BlobThresholds
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig, ToolMeta
from tests.e2e._skill_harness import connect_agent_and_computer, running_server, teardown
from tests.integration_tests.computer.mcp_servers import binary_image_stdio_server as bimg

pytestmark = pytest.mark.e2e

_BIN_SRV = Path(bimg.__file__).resolve()


def _stdio_cfg(name: str, script: Path) -> StdioServerConfig:
    """中文：构造真实 stdio MCP Server 配置；auto_apply=True 跳过二次确认回调（无人值守 e2e）。"""
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(script)]),
        default_tool_meta=ToolMeta(auto_apply=True),
    )


@pytest.mark.asyncio
async def test_oversize_image_tool_call_sideband_roundtrip(tmp_path) -> None:
    """中文：big_image 经旁路还原后字节/sha256 与生产端一致 / oversize image survives the sideband intact."""
    office_id = "e2e-toolcall-bin"
    computer = Computer(
        name="comp-bin",
        mcp_servers={_stdio_cfg("img-srv", _BIN_SRV)},
        blob_cache_root=tmp_path / "blob",
        # inline_budget 故意设小，使 4 KiB 图像必走旁路 / tiny budget forces the 4 KiB image via sideband
        blob_thresholds=BlobThresholds(inline_budget=128, too_large_cap=10 * 1024 * 1024),
    )

    expected = bimg.det_bytes(bimg.BIG_LEN)
    async with running_server() as base:
        agent, _comp_client = await connect_agent_and_computer(base, office_id, computer, agent_id="robot-bin")
        try:
            ret = await agent.emit_tool_call(computer="comp-bin", tool_name="big_image", params={}, timeout=15)
            assert ret.isError is False, f"工具调用失败 / tool call failed: {ret}"
            assert len(ret.content) == 1
            item = ret.content[0]
            assert item.type == "image"
            got = base64.b64decode(item.data)
            assert got == expected, "旁路还原后字节必须与生产端一致 / bytes must match producer after sideband"
            assert hashlib.sha256(got).hexdigest() == hashlib.sha256(expected).hexdigest()
        finally:
            await teardown(agent, computer)


@pytest.mark.asyncio
async def test_small_image_tool_call_inline(tmp_path) -> None:
    """中文：small_image ≤ inline_budget → 原样内联（不走旁路），字节一致（对照组）。"""
    office_id = "e2e-toolcall-bin-small"
    computer = Computer(
        name="comp-bin-s",
        mcp_servers={_stdio_cfg("img-srv", _BIN_SRV)},
        blob_cache_root=tmp_path / "blob",
        blob_thresholds=BlobThresholds(inline_budget=128, too_large_cap=10 * 1024 * 1024),
    )

    expected = bimg.det_bytes(bimg.SMALL_LEN)
    async with running_server() as base:
        agent, _comp_client = await connect_agent_and_computer(base, office_id, computer, agent_id="robot-bin-s")
        try:
            ret = await agent.emit_tool_call(computer="comp-bin-s", tool_name="small_image", params={}, timeout=15)
            assert ret.isError is False, f"工具调用失败 / tool call failed: {ret}"
            item = ret.content[0]
            assert item.type == "image"
            assert base64.b64decode(item.data) == expected
        finally:
            await teardown(agent, computer)
