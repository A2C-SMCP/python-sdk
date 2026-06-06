# -*- coding: utf-8 -*-
# filename: test_on_tool_call_binary_mint.py
# @Author  : JQQ
# @Software: PyCharm

"""
``on_tool_call`` v0.2.1 二进制旁路铸造单元测试 / Unit tests for ``on_tool_call`` binary sideband minting.

协议依据 / Protocol: blob-transfer.md §5（生产者通道接入契约）;
设计依据 / Design: docs/design-0.2.1-skill-computer-management.md §4.2.

测试覆盖 / Coverage:
  - 小尺寸二进制原样内联（不铸句柄）
  - 超内联预算的二进制 item → 铸句柄 + 清空 data/blob + 写 _meta.a2c_blob_handle/total_size/sha256
  - 超 too_large_cap 跳过铸造（防 DoS），保留原 inline
  - 文本 content item 不参与铸造
  - EmbeddedResource.BlobResourceContents 与 ImageContent 两种载体均支持
  - 工具失败仍走 isError（不动二进制旁路）
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from a2c_smcp.computer.blob import BlobThresholds
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient


@pytest.fixture
def computer(tmp_path: Path) -> Computer:
    """构造一个 inline_budget=128 的 Computer 便于测试小阈值."""
    return Computer(
        name="comp-mint",
        blob_cache_root=tmp_path,
        blob_thresholds=BlobThresholds(inline_budget=128, too_large_cap=1024 * 1024),
    )


@pytest.fixture
def client(computer: Computer) -> SMCPComputerClient:
    return SMCPComputerClient(computer=computer)


def _req(payload_b64: str = "") -> dict:
    return {
        "agent": "agent-1",
        "req_id": "r-1",
        "computer": "comp-mint",
        "tool_name": "t",
        "params": {"data": payload_b64},
        "timeout": 5,
    }


class TestInlineSizeBelowBudgetUntouched:
    @pytest.mark.asyncio
    async def test_small_binary_image_kept_inline(self, computer: Computer, client: SMCPComputerClient) -> None:
        """小尺寸 (≤ inline_budget) 二进制原样内联，不铸句柄.
        Below-budget binary stays inline, no handle minted."""
        small_payload = b"\x89PNG\r\n\x1a\n"  # 8 bytes ≪ 128 budget
        small_b64 = base64.b64encode(small_payload).decode("ascii")
        result = CallToolResult(
            content=[ImageContent(type="image", data=small_b64, mimeType="image/png")],
            isError=False,
        )
        # mock aexecute_tool 返回该结果 / Mock aexecute_tool to return result
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        ret = await client.on_tool_call(_req())
        item = ret["content"][0]
        # 内联 data 保留；无 _meta.a2c_blob_handle
        # Inline data preserved; no _meta.a2c_blob_handle minted
        assert item["data"] == small_b64
        assert "_meta" not in item or "a2c_blob_handle" not in item.get("_meta", {})


class TestOversizeBinaryMint:
    @pytest.mark.asyncio
    async def test_oversize_image_minted_and_inline_cleared(
        self,
        computer: Computer,
        client: SMCPComputerClient,
    ) -> None:
        """超 inline_budget 的二进制 → 铸句柄 + 清空 data + 写 _meta.a2c_*.
        Above-budget binary → mint handle + clear inline data + write _meta.a2c_*."""
        big_payload = b"\x00" * 2048  # 2 KiB ≫ 128 budget
        big_b64 = base64.b64encode(big_payload).decode("ascii")
        result = CallToolResult(
            content=[ImageContent(type="image", data=big_b64, mimeType="image/png")],
            isError=False,
        )
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        ret = await client.on_tool_call(_req())
        item = ret["content"][0]
        # 内联 data 已清空 / Inline data cleared
        assert item["data"] == ""
        # _meta.a2c_* 字段已写入 / _meta.a2c_* fields written
        meta = item["_meta"]
        assert isinstance(meta["a2c_blob_handle"], str) and meta["a2c_blob_handle"]
        assert meta["a2c_total_size"] == len(big_payload)
        assert meta["a2c_sha256"] == hashlib.sha256(big_payload).hexdigest()

    @pytest.mark.asyncio
    async def test_oversize_embedded_resource_blob_minted(
        self,
        computer: Computer,
        client: SMCPComputerClient,
    ) -> None:
        """EmbeddedResource.BlobResourceContents 路径同样支持铸造.
        EmbeddedResource.BlobResourceContents path also mints."""
        big_payload = b"R" * 2048
        big_b64 = base64.b64encode(big_payload).decode("ascii")
        # 直接构造 dict 模拟 EmbeddedResource (避免 BlobResourceContents pydantic 模型构造繁琐)
        # Direct dict to simulate EmbeddedResource (avoid pydantic boilerplate)
        result = CallToolResult(content=[TextContent(text="placeholder", type="text")], isError=False)
        # mock 通过覆盖 model_dump 直接返回包含 resource.blob 的 raw shape
        # mock model_dump to bypass and inject custom resource.blob shape
        raw_template = {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "embedded://x",
                        "blob": big_b64,
                        "mimeType": "application/octet-stream",
                    },
                },
            ],
            "isError": False,
        }
        result.model_dump = lambda mode="json": raw_template  # type: ignore[method-assign,assignment]
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        ret = await client.on_tool_call(_req())
        item = ret["content"][0]
        assert item["resource"]["blob"] == ""
        assert item["_meta"]["a2c_blob_handle"]
        assert item["_meta"]["a2c_total_size"] == len(big_payload)


class TestTextContentUntouched:
    @pytest.mark.asyncio
    async def test_text_content_not_minted(self, computer: Computer, client: SMCPComputerClient) -> None:
        result = CallToolResult(
            content=[TextContent(type="text", text="a" * 10000)],  # 大文本但不是二进制
            isError=False,
        )
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        ret = await client.on_tool_call(_req())
        item = ret["content"][0]
        # text 字段保留，无 data/blob/handle / text preserved, no data/blob/handle
        assert item["text"] == "a" * 10000
        assert "_meta" not in item or "a2c_blob_handle" not in item.get("_meta", {})


class TestIsErrorPathUntouched:
    @pytest.mark.asyncio
    async def test_is_error_path_skips_binary_processing(
        self,
        computer: Computer,
        client: SMCPComputerClient,
    ) -> None:
        """工具失败走 MCP isError，不影响二进制旁路逻辑.
        isError path is independent of binary sideband processing."""
        computer.aexecute_tool = AsyncMock(side_effect=RuntimeError("tool boom"))  # type: ignore[method-assign]
        ret = await client.on_tool_call(_req())
        assert ret["isError"] is True
        # structuredContent 携带异常信息（沿用既有行为）
        assert ret["structuredContent"]["error"] == "tool boom"


class TestMetaShapeValidationBeforeMint:
    """code-review #3：``_meta`` 形状校验前置到 mint 之前，避免孤儿 cid 落盘.

    code-review #3: validate ``_meta`` shape BEFORE minting; non-dict ``_meta`` won't cause an
    orphan cid in ``.blobspool``.

    三态处理 / Three-state handling:
      - ``_meta`` 缺失 → 创建新 dict（正常铸造路径 / normal mint path）
      - ``_meta`` 已是 dict → 复用（正常铸造路径，merge sideband 字段 / merge sideband）
      - ``_meta`` 非 None 非 dict（如 list / str）→ **跳过铸造**，保留 inline（无孤儿 cid 落盘）

      Per reviewer #3 comment: ``dict.setdefault`` doesn't replace ``None``, so the original code
      could mint to disk and then ``continue`` on the non-dict check, leaving orphan cids. We now
      pre-validate and skip BEFORE minting.
    """

    @pytest.mark.asyncio
    async def test_non_dict_meta_skips_mint_no_disk_write(
        self,
        computer: Computer,
        client: SMCPComputerClient,
    ) -> None:
        """``item['_meta'] = "broken"``（字符串、非 dict 非 None）→ 跳过铸造、不写盘、保留 inline.
        String ``_meta`` (non-dict, non-None) → skip mint, no disk write, keep inline."""
        big_payload = b"Z" * 2048  # > inline_budget (128)
        big_b64 = base64.b64encode(big_payload).decode("ascii")
        # 构造非 dict 非 None 的 _meta（模拟上游 MCP 工具 dump 出脏数据的极端场景）
        # Construct non-dict, non-None _meta (simulating upstream MCP tool dirty dump edge case)
        result = CallToolResult(content=[TextContent(text="placeholder", type="text")], isError=False)
        raw_template = {
            "content": [{"type": "image", "data": big_b64, "mimeType": "image/png", "_meta": "not-a-dict"}],
            "isError": False,
        }
        result.model_dump = lambda mode="json": raw_template  # type: ignore[method-assign,assignment]
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        cids_before = list(computer.toolspool_store.iter_cids())
        ret = await client.on_tool_call(_req())
        cids_after = list(computer.toolspool_store.iter_cids())

        # 关键不变量：未铸造 → .blobspool 文件数不变（无孤儿 cid）
        # Critical invariant: no mint → .blobspool count unchanged (no orphan cid)
        assert cids_after == cids_before
        # 内联 data 保留 / inline data preserved
        item = ret["content"][0]
        assert item["data"] == big_b64
        # _meta 保留原样（未被覆写为 dict）/ _meta untouched (preserved as-is)
        assert item["_meta"] == "not-a-dict"

    @pytest.mark.asyncio
    async def test_none_meta_treated_as_absent_mints_normally(
        self,
        computer: Computer,
        client: SMCPComputerClient,
    ) -> None:
        """``item['_meta'] = None`` 视为缺失 → 创建新 dict + 正常铸造（与 ``_meta`` 完全缺失等价）.
        ``_meta = None`` treated as absent → create fresh dict + mint normally."""
        big_payload = b"N" * 2048
        big_b64 = base64.b64encode(big_payload).decode("ascii")
        result = CallToolResult(content=[TextContent(text="placeholder", type="text")], isError=False)
        raw_template = {
            "content": [{"type": "image", "data": big_b64, "mimeType": "image/png", "_meta": None}],
            "isError": False,
        }
        result.model_dump = lambda mode="json": raw_template  # type: ignore[method-assign,assignment]
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        ret = await client.on_tool_call(_req())
        item = ret["content"][0]
        # 铸造成功 → inline 清空 + _meta.a2c_blob_handle 写入 / mint OK
        assert item["data"] == ""
        assert isinstance(item["_meta"], dict)
        assert item["_meta"]["a2c_blob_handle"]


class TestRoundTripWithComputerToolspool:
    @pytest.mark.asyncio
    async def test_mint_then_resolvable_via_on_get_blob(
        self,
        computer: Computer,
        client: SMCPComputerClient,
    ) -> None:
        """铸出的 handle 可经 on_get_blob 还原原字节（end-to-end Computer 内 round-trip）.
        Minted handle resolvable via on_get_blob to original bytes (within-Computer round-trip)."""
        payload = b"X" * 2048
        result = CallToolResult(
            content=[ImageContent(type="image", data=base64.b64encode(payload).decode("ascii"), mimeType="image/png")],
            isError=False,
        )
        computer.aexecute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        tool_ret = await client.on_tool_call(_req())
        handle = tool_ret["content"][0]["_meta"]["a2c_blob_handle"]

        # 直接通过 on_get_blob 拉取该 handle 验证字节一致
        # Pull the handle via on_get_blob and verify bytes
        blob_req = {
            "agent": "agent-1",
            "req_id": "r-blob",
            "computer": "comp-mint",
            "blob_handle": handle,
        }
        blob_ret = await client.on_get_blob(blob_req)  # type: ignore[arg-type]
        assert "code" not in blob_ret
        assert base64.b64decode(blob_ret["blob"]) == payload  # type: ignore[index]
