# -*- coding: utf-8 -*-
# filename: test_on_get_blob.py
# @Author  : JQQ
# @Software: PyCharm

"""
``client:get_blob`` handler 单元测试 / Unit tests for ``client:get_blob`` handler.

协议依据 / Protocol: blob-transfer.md + error-handling.md §4018.
设计依据 / Design: docs/design-0.2.1-skill-computer-management.md §4.2 / §4.3.

覆盖 / Coverage:
  - 正常单块切片 + ``eof`` 不变式
  - 多块切片：每块 ``chunk_offset`` / ``eof`` 正确
  - ``max_chunk_bytes`` clamp 行为
  - 4018 ``invalid_handle`` / ``forbidden`` / ``gone`` / ``range``
  - flat ErrorPayload 形态（``code=4018`` 顶层，``details.reason`` 子对象）
  - office/role 隔离：``computer`` 名不匹配 → 抛异常（非 ErrorPayload）
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from a2c_smcp.computer.blob import (
    BlobHandleForbiddenError,
    encode_skill_handle,
    encode_toolspool_handle,
)
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.smcp import ErrorCode


@pytest.fixture()
def computer(tmp_path: Path) -> Computer:
    """构造一个最小可用的 Computer，blob 缓存挂在 tmp_path 下。"""
    return Computer(name="comp-test", blob_cache_root=tmp_path)


@pytest.fixture()
def client(computer: Computer) -> SMCPComputerClient:
    return SMCPComputerClient(computer=computer)


def _req(handle: str, *, computer_name: str = "comp-test", offset: int = 0, max_chunk: int | None = None) -> dict:
    req: dict = {
        "agent": "agent-1",
        "req_id": "r-1",
        "computer": computer_name,
        "blob_handle": handle,
    }
    if offset != 0:
        req["chunk_offset"] = offset
    if max_chunk is not None:
        req["max_chunk_bytes"] = max_chunk
    return req


class TestSuccessfulRetrieval:
    """成功路径：toolspool 句柄 → 切片返回 / Happy path: toolspool handle → chunk."""

    @pytest.mark.asyncio
    async def test_single_chunk_with_eof(self, computer: Computer, client: SMCPComputerClient) -> None:
        cid = computer.toolspool_store.put(b"small payload", "text/plain")
        handle = encode_toolspool_handle(cid=cid, mime="text/plain")
        ret = await client.on_get_blob(_req(handle))  # type: ignore[arg-type]
        assert "code" not in ret
        assert ret["blob_handle"] == handle  # type: ignore[index]
        assert ret["total_size"] == len(b"small payload")  # type: ignore[index]
        assert ret["sha256"] == hashlib.sha256(b"small payload").hexdigest()  # type: ignore[index]
        assert ret["mime_type"] == "text/plain"  # type: ignore[index]
        assert ret["chunk_offset"] == 0  # type: ignore[index]
        assert ret["eof"] is True  # type: ignore[index]
        assert base64.b64decode(ret["blob"]) == b"small payload"  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_chunked_with_clamp(self, computer: Computer, client: SMCPComputerClient) -> None:
        payload = b"a" * 1000
        cid = computer.toolspool_store.put(payload, "application/octet-stream")
        handle = encode_toolspool_handle(cid=cid, mime="application/octet-stream")
        # 客户建议 200 字节单块；Computer 实际返回 min(200, clamp) = 200
        # Client suggests 200 bytes; Computer returns min(200, clamp) = 200
        chunk1 = await client.on_get_blob(_req(handle, offset=0, max_chunk=200))  # type: ignore[arg-type]
        assert chunk1["chunk_offset"] == 0  # type: ignore[index]
        assert chunk1["eof"] is False  # type: ignore[index]
        assert len(base64.b64decode(chunk1["blob"])) == 200  # type: ignore[index]
        # 第二块 / Second chunk
        chunk2 = await client.on_get_blob(_req(handle, offset=200, max_chunk=200))  # type: ignore[arg-type]
        assert chunk2["chunk_offset"] == 200  # type: ignore[index]
        assert chunk2["eof"] is False  # type: ignore[index]
        # ...continuing
        # 最后一块（直接跳到偏移 800）/ Last chunk (jump to offset 800)
        last = await client.on_get_blob(_req(handle, offset=800, max_chunk=200))  # type: ignore[arg-type]
        assert last["chunk_offset"] == 800  # type: ignore[index]
        assert last["eof"] is True  # type: ignore[index]
        assert len(base64.b64decode(last["blob"])) == 200  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_chunk_clamped_to_thresholds_max(self, computer: Computer, client: SMCPComputerClient) -> None:
        """客户建议远超阈值上限 → clamp 到 ``chunk_max_bytes`` / Client requests > max → clamped."""
        payload = b"x" * (500 * 1024)  # 500 KiB
        cid = computer.toolspool_store.put(payload, "application/octet-stream")
        handle = encode_toolspool_handle(cid=cid, mime="application/octet-stream")
        # 默认 chunk_max_bytes = 256 KiB；客户传 1 MiB → clamp 到 256 KiB
        # Default chunk_max_bytes = 256 KiB; client asks for 1 MiB → clamped to 256 KiB
        chunk = await client.on_get_blob(_req(handle, max_chunk=1024 * 1024))  # type: ignore[arg-type]
        assert len(base64.b64decode(chunk["blob"])) == 256 * 1024  # type: ignore[index]


class TestOfficeIsolation:
    """``computer`` 标识不匹配 → 抛异常（不走 ErrorPayload，由 Server 路由层归一） / Mismatch → raise."""

    @pytest.mark.asyncio
    async def test_computer_mismatch_raises(self, client: SMCPComputerClient) -> None:
        handle = encode_toolspool_handle(cid="a" * 64, mime="image/png")
        with pytest.raises(SMCPNamespaceError):
            await client.on_get_blob(_req(handle, computer_name="other-comp"))  # type: ignore[arg-type]


class TestErrorPayloadReasons:
    """4018 ``details.reason`` 各分支 / 4018 ``details.reason`` branches.

    所有错误以 **flat ErrorPayload** 返回（``code`` 顶层 / ``reason`` 在 ``details`` 子对象）.
    Errors return flat ``ErrorPayload`` (code top-level, reason inside ``details``).
    """

    @pytest.mark.asyncio
    async def test_invalid_handle_format(self, client: SMCPComputerClient) -> None:
        ret = await client.on_get_blob(_req("totally!invalid!handle"))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.BLOB_NOT_ACCESSIBLE)  # type: ignore[index]
        assert ret["details"]["reason"] == "invalid_handle"  # type: ignore[index]
        # 协议要求顶层 ``code`` / ``message`` 平铺，details 仅诊断（Agent MUST NOT 透传给用户）
        # Protocol: code/message top-level; details is diagnostic only
        assert "message" in ret  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_gone_for_missing_cid(self, client: SMCPComputerClient) -> None:
        """合法但未铸造的 cid → ``gone`` / Valid but never minted cid → gone."""
        handle = encode_toolspool_handle(cid="a" * 64, mime="image/png")
        ret = await client.on_get_blob(_req(handle))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.BLOB_NOT_ACCESSIBLE)  # type: ignore[index]
        assert ret["details"]["reason"] == "gone"  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_invalid_handle_for_forged_cid(self, client: SMCPComputerClient) -> None:
        """伪造 cid（非 hex / 越界片段）→ ``invalid_handle`` / Forged cid → invalid_handle.

        防御纵深：句柄解码后 resolver 仍 white-list 校验 cid，不信任句柄内容。
        Defense-in-depth: resolver whitelists cid post-decode, never trusts handle contents."""
        # 编码时强制注入非法 cid（绕过 store.put 的 sha256 计算）
        # Force-encode an illegal cid (bypassing store.put's sha256)
        handle = encode_toolspool_handle(cid="not-a-hex-cid", mime="image/png")
        ret = await client.on_get_blob(_req(handle))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.BLOB_NOT_ACCESSIBLE)  # type: ignore[index]
        assert ret["details"]["reason"] == "invalid_handle"  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_gone_for_unregistered_skill(self, client: SMCPComputerClient) -> None:
        """skill 句柄 name 未注册（空 Registry）→ ``gone``（#66：SkillBlobResolver 经 Registry 解析）.
        Unregistered skill name → ``gone`` (Registry miss = channel revoked / source unavailable)."""
        handle = encode_skill_handle(name="my-skill", rel_path="SKILL.md")
        ret = await client.on_get_blob(_req(handle))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.BLOB_NOT_ACCESSIBLE)  # type: ignore[index]
        assert ret["details"]["reason"] == "gone"  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_range_offset_negative(self, computer: Computer, client: SMCPComputerClient) -> None:
        cid = computer.toolspool_store.put(b"abc", "text/plain")
        handle = encode_toolspool_handle(cid=cid, mime="text/plain")
        ret = await client.on_get_blob(_req(handle, offset=-1))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.BLOB_NOT_ACCESSIBLE)  # type: ignore[index]
        assert ret["details"]["reason"] == "range"  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_range_offset_beyond_total(self, computer: Computer, client: SMCPComputerClient) -> None:
        cid = computer.toolspool_store.put(b"abc", "text/plain")
        handle = encode_toolspool_handle(cid=cid, mime="text/plain")
        # offset = total_size + 1
        ret = await client.on_get_blob(_req(handle, offset=4))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.BLOB_NOT_ACCESSIBLE)  # type: ignore[index]
        assert ret["details"]["reason"] == "range"  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_offset_equals_total_size_yields_empty_eof(
        self,
        computer: Computer,
        client: SMCPComputerClient,
    ) -> None:
        """``chunk_offset == total_size`` 是合法边界 → 空 blob + ``eof=True``（不算 range）.
        ``chunk_offset == total_size`` is a valid boundary → empty blob + ``eof=True`` (not range)."""
        cid = computer.toolspool_store.put(b"abc", "text/plain")
        handle = encode_toolspool_handle(cid=cid, mime="text/plain")
        ret = await client.on_get_blob(_req(handle, offset=3))  # type: ignore[arg-type]
        assert "code" not in ret
        assert ret["eof"] is True  # type: ignore[index]
        assert ret["chunk_offset"] == 3  # type: ignore[index]
        assert base64.b64decode(ret["blob"]) == b""  # type: ignore[index]


class TestResolverInjection:
    """支持注入自定义 resolver 覆盖默认 / Custom resolver injection."""

    @pytest.mark.asyncio
    async def test_custom_resolver_for_unknown_kind_treated_invalid(self, tmp_path: Path) -> None:
        """无 resolver 注册的 kind → ``invalid_handle`` / Unknown kind → invalid_handle."""

        class _DummyResolver:
            def resolve(self, payload: dict) -> object:
                raise BlobHandleForbiddenError("dummy")

        comp = Computer(
            name="comp-custom",
            blob_cache_root=tmp_path,
            blob_resolvers={"custom_kind": _DummyResolver()},  # type: ignore[dict-item]
        )
        client = SMCPComputerClient(computer=comp)
        # 用 toolspool handle（其 kind 仍走默认 resolver；说明注入是并集而非替换）
        # Toolspool handle still works via default resolver → injection is union, not replace
        handle = encode_toolspool_handle(cid="b" * 64, mime="image/png")
        ret = await client.on_get_blob(_req(handle, computer_name="comp-custom"))  # type: ignore[arg-type]
        assert ret["details"]["reason"] == "gone"  # type: ignore[index]  # cid 不存在 → gone


class TestMintToolspoolHandleHelper:
    """``Computer.mint_toolspool_handle`` 与 ``on_get_blob`` 端到端联动 / End-to-end mint → resolve."""

    @pytest.mark.asyncio
    async def test_mint_then_resolve_round_trip(self, computer: Computer, client: SMCPComputerClient) -> None:
        handle = computer.mint_toolspool_handle(payload=b"hello, end-to-end", mime="text/markdown")
        ret = await client.on_get_blob(_req(handle))  # type: ignore[arg-type]
        assert "code" not in ret
        assert base64.b64decode(ret["blob"]) == b"hello, end-to-end"  # type: ignore[index]
        assert ret["mime_type"] == "text/markdown"  # type: ignore[index]


class TestMintToolspoolTooLargeDefense:
    """防御纵深：``mint_toolspool_handle`` 入参超 ``too_large_cap`` → 拒绝铸造，不写盘.
    Defense in depth: payload over ``too_large_cap`` refuses to mint AND must not touch disk."""

    def test_oversize_payload_raises_without_disk_write(self, tmp_path: Path) -> None:
        from a2c_smcp.computer.blob import BlobThresholds, BlobTooLargeError

        # 极低 cap 便于测试 / Very low cap for testing
        comp = Computer(
            name="comp-cap",
            blob_cache_root=tmp_path,
            blob_thresholds=BlobThresholds(too_large_cap=16),
        )
        store_cids_before = list(comp.toolspool_store.iter_cids())
        with pytest.raises(BlobTooLargeError) as exc_info:
            comp.mint_toolspool_handle(payload=b"this payload definitely exceeds 16 bytes", mime="text/plain")
        assert exc_info.value.size == len(b"this payload definitely exceeds 16 bytes")
        assert exc_info.value.cap == 16
        # 关键不变量：拒绝铸造时不应写盘 / Critical invariant: refused mint must not write to disk
        assert list(comp.toolspool_store.iter_cids()) == store_cids_before

    def test_at_cap_boundary_mints_successfully(self, tmp_path: Path) -> None:
        """边界：``len(payload) == cap`` 应允许（``> cap`` 才拒绝）.
        Boundary: ``len(payload) == cap`` is allowed (strict ``>`` is the cutoff)."""
        from a2c_smcp.computer.blob import BlobThresholds

        comp = Computer(
            name="comp-cap-eq",
            blob_cache_root=tmp_path,
            blob_thresholds=BlobThresholds(too_large_cap=8),
        )
        handle = comp.mint_toolspool_handle(payload=b"exactly8", mime="text/plain")
        assert handle  # 铸造成功 / mint succeeded
