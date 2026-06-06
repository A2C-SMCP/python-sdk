# -*- coding: utf-8 -*-
# filename: test_toolspool.py
# @Author  : JQQ
# @Software: PyCharm

"""
``.blobspool`` 内容寻址暂存单元测试 / Tests for content-addressed ``.blobspool``.

设计依据 / Design: docs/design-0.2.1-skill-computer-management.md §4.3 「跨 Computer 重启可解析」.
协议依据 / Protocol: blob-transfer.md §5.4 (handle untrusted, resolver re-validates).
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from a2c_smcp.computer.blob.handle import BlobHandleGoneError, BlobHandleInvalidError
from a2c_smcp.computer.blob.toolspool import ToolspoolBlobStore


class TestPutGetRoundTrip:
    """``put`` → ``get`` 完整 round-trip / Full put → get round-trip."""

    def test_basic_round_trip(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"hello, blob", "application/octet-stream")
        assert cid == hashlib.sha256(b"hello, blob").hexdigest()
        payload, mime = store.get(cid)
        assert payload == b"hello, blob"
        # mime 持久化在 sidecar；store.get 返回磁盘 mime 由 resolver 与 handle 中 mime 协调
        # mime persisted in sidecar; resolver reconciles with handle's mime
        assert mime == "application/octet-stream"

    def test_idempotent_put_same_bytes(self, tmp_path: Path) -> None:
        """相同字节多次 put → 同一 cid，无重复副本 / Same bytes → same cid, no duplicates."""
        store = ToolspoolBlobStore(tmp_path)
        cid1 = store.put(b"same content", "text/plain")
        cid2 = store.put(b"same content", "text/plain")
        assert cid1 == cid2
        # 只有一份 blob 文件（+ sidecar）/ Only one blob file (+ sidecar)
        blob_files = list(store.iter_cids())
        assert len(blob_files) == 1

    def test_empty_payload(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"", "application/octet-stream")
        assert cid == hashlib.sha256(b"").hexdigest()
        payload, _ = store.get(cid)
        assert payload == b""

    def test_large_payload(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        big = b"x" * (5 * 1024 * 1024)  # 5 MiB
        cid = store.put(big, "application/octet-stream")
        payload, _ = store.get(cid)
        assert payload == big
        assert len(payload) == len(big)


class TestCrossRestartParseability:
    """跨进程重启可解析（设计 §4.3 承诺）/ Survives Computer restarts (design §4.3 promise)."""

    def test_cid_resolvable_after_new_store_instance(self, tmp_path: Path) -> None:
        store1 = ToolspoolBlobStore(tmp_path)
        cid = store1.put(b"persistent content", "image/png")
        # 模拟 Computer 重启：新建 store 实例指向同一目录
        # Simulate Computer restart: new store instance pointing at the same dir
        store2 = ToolspoolBlobStore(tmp_path)
        payload, mime = store2.get(cid)
        assert payload == b"persistent content"
        assert mime == "image/png"

    def test_iter_cids_after_restart(self, tmp_path: Path) -> None:
        store1 = ToolspoolBlobStore(tmp_path)
        store1.put(b"a", "text/plain")
        store1.put(b"b", "text/plain")
        store2 = ToolspoolBlobStore(tmp_path)
        cids = set(store2.iter_cids())
        assert cids == {hashlib.sha256(b"a").hexdigest(), hashlib.sha256(b"b").hexdigest()}


class TestCidValidationDefenseInDepth:
    """``cid`` 白名单校验防伪造 / cid whitelist defends against forged handles."""

    def test_get_rejects_traversal_cid(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        # 写入一个合法文件做对照 / Place a real file as a baseline
        store.put(b"real", "text/plain")
        # 任何含 ``..`` / ``/`` 的 cid 都应 invalid_handle
        for forged in ("..", "../../etc/passwd", "a/b", "../" + "0" * 60):
            with pytest.raises(BlobHandleInvalidError, match="cid"):
                store.get(forged)

    def test_get_rejects_wrong_length_cid(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        for forged in ("abc", "a" * 63, "a" * 65, "a" * 128):
            with pytest.raises(BlobHandleInvalidError, match="cid"):
                store.get(forged)

    def test_get_rejects_non_hex_cid(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        # 64 位但含非 hex 字符 / Length 64 but non-hex
        forged = "G" * 64
        with pytest.raises(BlobHandleInvalidError, match="cid"):
            store.get(forged)

    def test_get_gone_for_missing_cid(self, tmp_path: Path) -> None:
        """cid 格式合法但文件不在 → ``4018 gone`` / Valid cid form, file absent → gone."""
        store = ToolspoolBlobStore(tmp_path)
        missing = "a" * 64
        with pytest.raises(BlobHandleGoneError):
            store.get(missing)

    def test_exists_returns_false_for_invalid_cid(self, tmp_path: Path) -> None:
        """``exists`` 探测非法 cid → False（不抛，方便"先看再读"）/ ``exists`` returns False for invalid cid."""
        store = ToolspoolBlobStore(tmp_path)
        assert store.exists("..") is False
        assert store.exists("not-hex") is False
        assert store.exists("") is False

    def test_exists_true_after_put(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"x", "text/plain")
        assert store.exists(cid) is True


class TestConcurrentPutAtomicity:
    """跨线程/进程并发 put 同 cid（同字节）不应抛 FileNotFoundError / 同名 tmp race.
    Concurrent put of the same cid (same bytes) must not race on shared tmp filename."""

    def test_concurrent_same_cid_no_race(self, tmp_path: Path) -> None:
        """同 cid 的并发 put 在 N 个线程内一致 + 不抛异常（唯一 tmp 名前缀 + 幂等分支）.
        Concurrent same-cid puts across N threads remain consistent and exception-free."""
        store = ToolspoolBlobStore(tmp_path)
        payload = b"contended bytes" * 100
        expected_cid = hashlib.sha256(payload).hexdigest()

        def put_once() -> str:
            return store.put(payload, "application/octet-stream")

        # 16 个并发线程 / 16 concurrent threads
        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(lambda _: put_once(), range(16)))

        # 所有调用返回同一 cid（内容寻址）/ All return the same cid (content-addressed)
        assert set(results) == {expected_cid}
        # 物理 blob 文件仍只有一份 / Still exactly one blob file
        assert list(store.iter_cids()) == [expected_cid]
        # 字节可正确回读 / Bytes round-trip cleanly
        bytes_out, _ = store.get(expected_cid)
        assert bytes_out == payload

    def test_concurrent_distinct_cids_independent(self, tmp_path: Path) -> None:
        """不同 cid 并发 put 互不干扰，最终全部可读回.
        Concurrent puts of distinct cids do not interfere; all readable afterwards."""
        store = ToolspoolBlobStore(tmp_path)
        payloads = [f"distinct-{i}".encode() * 50 for i in range(16)]

        def put_one(payload: bytes) -> str:
            return store.put(payload, "text/plain")

        with ThreadPoolExecutor(max_workers=8) as executor:
            cids = list(executor.map(put_one, payloads))

        # cid 集合与 payload 数量一致 / cid set size matches payload count
        assert len(set(cids)) == len(payloads)
        # 每个 cid 均可回读字节一致 / Each cid round-trips correctly
        for cid, payload in zip(cids, payloads, strict=True):
            bytes_out, _ = store.get(cid)
            assert bytes_out == payload


class TestMimeValidation:
    """``put`` 拒绝非法 mime / ``put`` rejects invalid mime."""

    def test_put_rejects_empty_mime(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        with pytest.raises(ValueError, match="mime"):
            store.put(b"x", "")

    def test_put_rejects_non_ascii_mime(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        with pytest.raises(ValueError, match="mime"):
            store.put(b"x", "image/png;charset=中文")


class TestStatAndReadChunk:
    """``ToolspoolBlobStore.stat()`` + ``read_chunk()`` 惰性原语（v0.2.1 #51）.
    Lazy primitive tests for ``stat()`` + ``read_chunk()`` (v0.2.1 #51)."""

    def test_stat_returns_size_and_mime_without_reading_payload(self, tmp_path: Path) -> None:
        """stat 只读 metadata，不读 payload bytes / stat reads metadata only."""
        store = ToolspoolBlobStore(tmp_path)
        payload = b"y" * (256 * 1024)
        cid = store.put(payload, "application/octet-stream")
        info = store.stat(cid)
        assert info.size == 256 * 1024
        assert info.mime == "application/octet-stream"

    def test_read_chunk_mid_stream(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        payload = bytes(range(256)) * 4  # 1024 bytes, deterministic
        cid = store.put(payload, "application/octet-stream")
        chunk = store.read_chunk(cid, 100, 200)
        assert chunk == payload[100:300]

    def test_read_chunk_end_aligned(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        payload = b"a" * 1024
        cid = store.put(payload, "text/plain")
        chunk = store.read_chunk(cid, 1024 - 100, 100)
        assert chunk == payload[-100:]

    def test_read_chunk_past_eof_returns_empty(self, tmp_path: Path) -> None:
        """``offset >= total_size`` → ``b""`` (mirrors Python file.read past EOF)."""
        store = ToolspoolBlobStore(tmp_path)
        payload = b"abc"
        cid = store.put(payload, "text/plain")
        assert store.read_chunk(cid, 3, 100) == b""
        assert store.read_chunk(cid, 10, 100) == b""

    def test_read_chunk_length_over_remaining_short_read(self, tmp_path: Path) -> None:
        """``length > remaining`` → OS 自然短读（不 raise）."""
        store = ToolspoolBlobStore(tmp_path)
        payload = b"abcdef"  # 6 bytes
        cid = store.put(payload, "text/plain")
        assert store.read_chunk(cid, 3, 999) == b"def"

    def test_read_chunk_length_zero_no_io(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """``length == 0`` → ``b""`` 无 I/O 触发 / no I/O when length is 0."""
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"hello", "text/plain")

        def fail_open(*args, **kwargs):  # type: ignore[no-untyped-def]
            pytest.fail("read_chunk(length=0) must not invoke open()")

        monkeypatch.setattr("builtins.open", fail_open)
        assert store.read_chunk(cid, 0, 0) == b""

    def test_read_chunk_invalid_cid_raises(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        for forged in ("..", "../../etc/passwd", "not-hex" * 9, ""):
            with pytest.raises(BlobHandleInvalidError):
                store.read_chunk(forged, 0, 100)

    def test_read_chunk_gone_cid_raises(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        missing = "a" * 64  # valid hex, no file
        with pytest.raises(BlobHandleGoneError):
            store.read_chunk(missing, 0, 100)

    def test_read_chunk_negative_offset_raises(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"hello", "text/plain")
        with pytest.raises(ValueError, match="non-negative"):
            store.read_chunk(cid, -1, 5)

    def test_stat_invalid_cid_raises(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        with pytest.raises(BlobHandleInvalidError):
            store.stat("not-hex")

    def test_stat_gone_cid_raises(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        missing = "b" * 64
        with pytest.raises(BlobHandleGoneError):
            store.stat(missing)

    def test_concurrent_read_chunks_independent(self, tmp_path: Path) -> None:
        """并发 read_chunk 各自独立 fd，结果可靠拼接为原文.
        Concurrent read_chunk calls use independent fds; results reassemble to original."""
        store = ToolspoolBlobStore(tmp_path)
        payload = b"abcdefghij" * 1024  # 10 KiB, deterministic
        cid = store.put(payload, "text/plain")
        chunks_per_offset: dict[int, bytes] = {}

        def fetch(offset: int) -> tuple[int, bytes]:
            return offset, store.read_chunk(cid, offset, 1024)

        with ThreadPoolExecutor(max_workers=10) as pool:
            offsets = list(range(0, len(payload), 1024))
            for offset, chunk in pool.map(fetch, offsets):
                chunks_per_offset[offset] = chunk

        reassembled = b"".join(chunks_per_offset[off] for off in sorted(chunks_per_offset))
        assert reassembled == payload
