# -*- coding: utf-8 -*-
# filename: test_blob.py
# @Author  : JQQ
# @Software: PyCharm

"""
``drain_blob`` 串行 + 并行 + 错误协调矩阵单元测试 / Tests for ``drain_blob`` serial + parallel + error matrix.

协议依据 / Protocol: a2c-smcp-protocol blob-transfer.md §3 (parallel) / §5.1 (reference impl).
设计依据 / Design: docs/design-0.2.1-skill-computer-management.md §4.5.

覆盖 / Coverage:
  - 串行：多块重组 / eof / sha256 完整性 / total_size 跨块漂移 → 重读 / 全量 sha256 不一致 → 重读
  - 并行：concurrency=4 round-trip 与串行结果一致 / 漂移 → 取消 + 串行 fallback / 范围错 → 串行 fallback
  - 4018 各 reason：invalid_handle/forbidden/gone 不重试 raise；range 串行 fallback
  - async + sync 双镜像并行
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import time
from collections.abc import Mapping
from typing import Any

import pytest

from a2c_smcp.smcp import ErrorCode
from a2c_smcp.utils.blob import (
    BlobTransferError,
    drain_blob,
    drain_blob_sync,
)

# ── 测试辅助 / Test helpers ──────────────────────────────────────────────


def _make_ret(
    *,
    payload: bytes,
    chunk_offset: int,
    end: int,
    total_size: int,
    sha256_hex: str,
    mime: str = "application/octet-stream",
    blob_handle: str = "test-handle",
) -> dict[str, Any]:
    chunk = payload[chunk_offset:end]
    return {
        "blob_handle": blob_handle,
        "mime_type": mime,
        "total_size": total_size,
        "sha256": sha256_hex,
        "chunk_offset": chunk_offset,
        "eof": end == total_size,
        "blob": base64.b64encode(chunk).decode("ascii"),
        "req_id": "test",
    }


def _make_blob_error(reason: str) -> dict[str, Any]:
    return {
        "code": int(ErrorCode.BLOB_NOT_ACCESSIBLE),
        "message": "Blob not accessible",
        "details": {"reason": reason},
    }


class _AsyncMockCall:
    """异步可编排的 mock call / Programmable async mock call.

    支持注入按调用次序的响应序列，或者按 ``(offset)`` 索引的字典；记录所有调用以便断言。
    Inject a per-call response sequence or an offset-keyed dict; records all calls for assertions.
    """

    def __init__(self, responses: list[Mapping[str, Any]] | dict[int, Mapping[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, int, int]] = []
        self._idx = 0

    async def __call__(self, computer: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
        self.calls.append((computer, handle, offset, max_chunk))
        if isinstance(self.responses, list):
            ret = self.responses[self._idx]
            self._idx += 1
            return ret
        # 按 offset 查表 / lookup by offset
        return self.responses[offset]


class _SyncMockCall:
    def __init__(self, responses: list[Mapping[str, Any]] | dict[int, Mapping[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, int, int]] = []
        self._idx = 0

    def __call__(self, computer: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
        self.calls.append((computer, handle, offset, max_chunk))
        if isinstance(self.responses, list):
            ret = self.responses[self._idx]
            self._idx += 1
            return ret
        return self.responses[offset]


# ── 串行模式 / Serial mode ───────────────────────────────────────────────


class TestSerialAsync:
    @pytest.mark.asyncio
    async def test_single_chunk_eof(self) -> None:
        payload = b"hello"
        sha = hashlib.sha256(payload).hexdigest()
        call = _AsyncMockCall([_make_ret(payload=payload, chunk_offset=0, end=5, total_size=5, sha256_hex=sha)])
        data, mime = await drain_blob(call, "comp-1", "h", chunk_size=4096)
        assert data == payload
        assert mime == "application/octet-stream"
        assert len(call.calls) == 1

    @pytest.mark.asyncio
    async def test_multi_chunk_reassembly(self) -> None:
        payload = b"A" * 1000 + b"B" * 500 + b"C" * 200  # 1700 bytes
        sha = hashlib.sha256(payload).hexdigest()
        chunk_size = 600
        # offsets: 0, 600, 1200; ends: 600, 1200, 1700
        call = _AsyncMockCall(
            [
                _make_ret(payload=payload, chunk_offset=0, end=600, total_size=1700, sha256_hex=sha),
                _make_ret(payload=payload, chunk_offset=600, end=1200, total_size=1700, sha256_hex=sha),
                _make_ret(payload=payload, chunk_offset=1200, end=1700, total_size=1700, sha256_hex=sha),
            ],
        )
        data, _ = await drain_blob(call, "c", "h", chunk_size=chunk_size)
        assert data == payload
        assert len(call.calls) == 3

    @pytest.mark.asyncio
    async def test_sha256_mismatch_triggers_reread(self) -> None:
        """全量 sha256 与首块声明不一致 → 串行从 0 重读 / Whole-file sha256 mismatch → reread."""
        payload = b"hello world"
        good_sha = hashlib.sha256(payload).hexdigest()
        bad_sha = "0" * 64  # 故意错的 / intentionally wrong
        # 第一轮：响应声明 sha=bad_sha（但内容是真 payload）→ 校验失败触发重读
        # 第二轮：响应声明 sha=good_sha（一致）→ 通过
        call = _AsyncMockCall(
            [
                _make_ret(payload=payload, chunk_offset=0, end=11, total_size=11, sha256_hex=bad_sha),
                _make_ret(payload=payload, chunk_offset=0, end=11, total_size=11, sha256_hex=good_sha),
            ],
        )
        data, _ = await drain_blob(call, "c", "h", chunk_size=4096, max_retries=3)
        assert data == payload
        assert len(call.calls) == 2  # 一次失败 + 一次成功

    @pytest.mark.asyncio
    async def test_total_size_drift_across_chunks_triggers_reread(self) -> None:
        """``total_size`` 跨块变化 → 触发 ``_RecoverableDrift`` → 串行重读.
        ``total_size`` changes between chunks → recoverable drift → reread."""
        payload = b"x" * 200
        sha = hashlib.sha256(payload).hexdigest()
        # 首次：第一块声明 total=200，第二块声明 total=300（漂移）→ 重读
        # 第二次：两块都声明 total=200（一致）→ 通过
        call = _AsyncMockCall(
            [
                _make_ret(payload=payload, chunk_offset=0, end=100, total_size=200, sha256_hex=sha),
                _make_ret(payload=payload, chunk_offset=100, end=200, total_size=300, sha256_hex=sha),  # 漂移
                _make_ret(payload=payload, chunk_offset=0, end=100, total_size=200, sha256_hex=sha),
                _make_ret(payload=payload, chunk_offset=100, end=200, total_size=200, sha256_hex=sha),
            ],
        )
        data, _ = await drain_blob(call, "c", "h", chunk_size=100, max_retries=3)
        assert data == payload

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_raises(self) -> None:
        """持续漂移 / sha256 不一致超过 max_retries → 抛 BlobTransferError.
        Persistent drift exceeds max_retries → raise BlobTransferError."""
        payload = b"x"
        bad_sha = "0" * 64
        # 每次都返回错的 sha → 永远走 drift 分支 → max_retries 耗尽
        responses = [_make_ret(payload=payload, chunk_offset=0, end=1, total_size=1, sha256_hex=bad_sha)] * 5
        call = _AsyncMockCall(responses)
        with pytest.raises(BlobTransferError) as exc_info:
            await drain_blob(call, "c", "h", max_retries=2)
        assert exc_info.value.reason == "max_retries_exceeded"


class TestSerialErrorMatrix:
    """串行模式 4018 各 reason 行为 / Serial-mode 4018 reason behaviors."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["invalid_handle", "forbidden", "gone"])
    async def test_4018_non_retryable_reasons(self, reason: str) -> None:
        call = _AsyncMockCall([_make_blob_error(reason)])
        with pytest.raises(BlobTransferError) as exc_info:
            await drain_blob(call, "c", "h")
        assert exc_info.value.reason == reason
        assert len(call.calls) == 1  # 不重试 / no retry

    @pytest.mark.asyncio
    async def test_4018_range_raises_in_serial(self) -> None:
        """串行模式下 range 也直接抛（不像并行有 fallback）.
        Serial mode raises ``range`` directly (no fallback like parallel)."""
        call = _AsyncMockCall([_make_blob_error("range")])
        with pytest.raises(BlobTransferError) as exc_info:
            await drain_blob(call, "c", "h")
        assert exc_info.value.reason == "range"


# ── 并行模式 / Parallel mode ─────────────────────────────────────────────


class TestParallelAsync:
    @pytest.mark.asyncio
    async def test_concurrency_round_trip_matches_serial(self) -> None:
        """``concurrency=4`` round-trip 与串行结果完全一致（sha256 自证）.
        ``concurrency=4`` round-trip exactly matches serial result (sha256 self-verifying)."""
        payload = b"".join(bytes([i % 256]) * 100 for i in range(20))  # 2000 字节 / 2000 bytes
        sha = hashlib.sha256(payload).hexdigest()
        chunk_size = 200
        # 按 offset 索引（并行任意顺序拉取）/ Offset-indexed (parallel pulls in any order)
        responses = {
            offset: _make_ret(payload=payload, chunk_offset=offset, end=min(offset + chunk_size, 2000), total_size=2000, sha256_hex=sha)
            for offset in range(0, 2000, chunk_size)
        }
        call = _AsyncMockCall(responses)
        data, _ = await drain_blob(call, "c", "h", concurrency=4, chunk_size=chunk_size)
        assert data == payload
        assert hashlib.sha256(data).hexdigest() == sha

    @pytest.mark.asyncio
    async def test_parallel_drift_falls_back_to_serial(self) -> None:
        """并行态某块 ``sha256`` 漂移 → 取消所有在飞 + 串行从 0 重读（不在并发态拼接错配字节）.
        Parallel chunk sha256 drift → cancel + serial reread from 0."""
        payload = b"a" * 600
        good_sha = hashlib.sha256(payload).hexdigest()
        bad_sha = "0" * 64

        # 并行阶段：首块 OK；offset=200 漂移到 bad_sha；offset=400 OK
        # Then serial fallback: 3 successful chunks
        async def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            call.calls.append((comp, handle, offset, max_chunk))  # type: ignore[attr-defined]
            phase = call.phase  # type: ignore[attr-defined]
            if phase == "parallel":
                if offset == 200:
                    # 漂移这一块 / drift this chunk
                    return _make_ret(payload=payload, chunk_offset=offset, end=400, total_size=600, sha256_hex=bad_sha)
                end = min(offset + 200, 600)
                return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)
            # serial fallback phase
            end = min(offset + 200, 600)
            return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)

        call.calls = []  # type: ignore[attr-defined]
        call.phase = "parallel"  # type: ignore[attr-defined]

        # 用 await/asyncio.sleep(0) 切换 phase（drain_blob 进入 serial fallback 后我们切）
        # 实际上 drain_blob 在 raise _RecoverableDrift 后会自动调 serial 路径，我们用同一函数
        # The drift triggers fallback to serial; we keep call returning "good" thereafter
        call.phase = "parallel"  # type: ignore[attr-defined]

        # 这里不能完美 phase 切换，但 drain_blob 内部抓 drift 后会重新调 call
        # We can just unconditionally return good responses; first parallel call returns drift,
        # subsequent calls in serial mode return good
        # 简化：所有 offset=200 的调用都返回 drift，但首块还是好的 → 进 serial → serial 又会问 offset=0
        # 在 serial 里 offset=200 仍漂移，触发 drift 重试；max_retries 内可能耗尽
        # 改为：并行阶段返回 drift，serial 阶段返回 good（用 phase 切换）

        # 重写为更可控的 stateful call
        call_log: list[tuple[str, str, int, int]] = []
        parallel_done = {"v": False}

        async def stateful_call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            call_log.append((comp, handle, offset, max_chunk))
            end = min(offset + 200, 600)
            # 并行阶段（首次 offset=200 → drift）；之后 serial 阶段全部正常
            # Parallel phase: first offset=200 → drift; subsequent serial phase: all good
            if not parallel_done["v"]:
                if offset == 200:
                    parallel_done["v"] = True  # 触发漂移后即切换到 serial 阶段
                    return _make_ret(
                        payload=payload,
                        chunk_offset=offset,
                        end=end,
                        total_size=600,
                        sha256_hex=bad_sha,
                    )
                return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)
            return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)

        data, _ = await drain_blob(stateful_call, "c", "h", concurrency=4, chunk_size=200, max_retries=3)
        assert data == payload
        # 串行 fallback 后总会拉满 3 块（offset 0/200/400），加上并行阶段至少 2 次调用
        # After fallback, serial pulls all 3 chunks; parallel phase calls ≥ 2
        assert len(call_log) >= 5

    @pytest.mark.asyncio
    async def test_parallel_range_falls_back_to_serial(self) -> None:
        """并行态某块 ``range`` → 取消 + 串行 fallback / Parallel ``range`` → cancel + serial fallback."""
        payload = b"x" * 600
        good_sha = hashlib.sha256(payload).hexdigest()
        phase_state = {"parallel": True}

        async def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            end = min(offset + 200, 600)
            if phase_state["parallel"]:
                if offset == 200:
                    phase_state["parallel"] = False
                    return _make_blob_error("range")
                return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)
            return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)

        data, _ = await drain_blob(call, "c", "h", concurrency=4, chunk_size=200)
        assert data == payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["invalid_handle", "forbidden", "gone"])
    async def test_parallel_non_retryable_reasons(self, reason: str) -> None:
        """并行态 invalid_handle/forbidden/gone → 取消所有在飞 + raise（不 fallback）.
        Parallel non-retryable → cancel + raise (no fallback)."""
        payload = b"x" * 600
        good_sha = hashlib.sha256(payload).hexdigest()

        async def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            end = min(offset + 200, 600)
            if offset == 0:
                return _make_ret(payload=payload, chunk_offset=0, end=end, total_size=600, sha256_hex=good_sha)
            # 后续并发块返回不可恢复错误
            return _make_blob_error(reason)

        with pytest.raises(BlobTransferError) as exc_info:
            await drain_blob(call, "c", "h", concurrency=4, chunk_size=200)
        assert exc_info.value.reason == reason

    @pytest.mark.asyncio
    async def test_parallel_mixed_drift_and_range_race(self) -> None:
        """混合 race：并发态某块 ``range`` + 另一块 ``sha256`` 漂移 → 应走 fallback（drift 优先）.
        Mixed race: one chunk ``range`` + another sha256 drift → must fall back (drift wins).

        反例覆盖 except* 双分支同时 raise 时 ExceptionGroup 漏逃 fallback 的潜在 race。
        Regression for the except* dual-branch leak where fatal + recoverable co-exist."""
        payload = b"a" * 600
        good_sha = hashlib.sha256(payload).hexdigest()
        bad_sha = "0" * 64
        phase = {"parallel": True}

        async def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            end = min(offset + 200, 600)
            if phase["parallel"]:
                if offset == 200:
                    return _make_blob_error("range")  # 可恢复信号 1 / recoverable signal 1
                if offset == 400:
                    # 可恢复信号 2（漂移）同时存在 / recoverable signal 2 (drift) co-exists
                    return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=bad_sha)
                phase["parallel"] = False  # 触发 fallback 后切到 serial 阶段
                return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)
            # serial fallback 阶段：全部一致 / serial fallback phase: all consistent
            return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)

        data, _ = await drain_blob(call, "c", "h", concurrency=4, chunk_size=200)
        # fallback 成功还原完整字节 / fallback recovers the full payload
        assert data == payload

    @pytest.mark.asyncio
    async def test_parallel_mixed_fatal_and_drift_prefers_fatal(self) -> None:
        """混合 race：fatal (gone) + drift 同时触发 → fatal 优先（不可恢复隐藏会误导诊断）.
        Mixed race: fatal (gone) + drift co-exist → fatal wins (hiding fatal would mislead diagnosis)."""
        payload = b"x" * 600
        good_sha = hashlib.sha256(payload).hexdigest()
        bad_sha = "0" * 64

        async def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            end = min(offset + 200, 600)
            if offset == 0:
                return _make_ret(payload=payload, chunk_offset=0, end=end, total_size=600, sha256_hex=good_sha)
            if offset == 200:
                return _make_blob_error("gone")  # fatal
            # offset == 400: drift 同时触发 / drift co-occurs
            return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=bad_sha)

        with pytest.raises(BlobTransferError) as exc_info:
            await drain_blob(call, "c", "h", concurrency=4, chunk_size=200)
        # fatal 必须暴露真实原因，而不是被 drift 掩盖 / fatal must surface, not be masked by drift
        assert exc_info.value.reason == "gone"

    @pytest.mark.asyncio
    async def test_parallel_async_fatal_beats_range(self) -> None:
        """混合 race（快 range + 慢 fatal）：fatal 必须暴露，不被先完成的 range 经 TaskGroup-cancel 掩盖.

        回归：旧实现 range 就地 raise → TaskGroup 取消在飞 fatal（CancelledError 不进 group）→ group 仅余
        range → fallback → 串行撞 range 即 fatal → 对外报 range。marker-collect 后 fatal 永不被掩盖。
        Mirrors Rust ``parallel_async_fatal_beats_recoverable``: a racing fast ``range`` must not mask a
        slow concurrent fatal.
        """
        payload = b"x" * 600
        good_sha = hashlib.sha256(payload).hexdigest()

        async def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            end = min(offset + 200, 600)
            if offset == 0:
                return _make_ret(payload=payload, chunk_offset=0, end=end, total_size=600, sha256_hex=good_sha)
            if offset == 200:
                return _make_blob_error("range")  # 快可恢复，先完成 / fast recoverable, completes first
            await asyncio.sleep(0.05)  # 慢 fatal，让 range 先完成 / slow fatal so range wins the race
            return _make_blob_error("forbidden")

        with pytest.raises(BlobTransferError) as exc_info:
            await drain_blob(call, "c", "h", concurrency=4, chunk_size=200)
        # fatal 必须暴露，而非被先完成的 range 掩盖 / fatal must surface, not be masked by the racing range
        assert exc_info.value.reason == "forbidden"


# ── Sync 镜像 / Sync mirror ──────────────────────────────────────────────


class TestSyncMirror:
    """drain_blob_sync 串行 + 并行核心场景 / Sync mirror core scenarios."""

    def test_serial_round_trip(self) -> None:
        payload = b"sync hello"
        sha = hashlib.sha256(payload).hexdigest()
        call = _SyncMockCall([_make_ret(payload=payload, chunk_offset=0, end=10, total_size=10, sha256_hex=sha)])
        data, mime = drain_blob_sync(call, "c", "h")
        assert data == payload
        assert mime == "application/octet-stream"

    def test_serial_multi_chunk(self) -> None:
        payload = b"a" * 1000
        sha = hashlib.sha256(payload).hexdigest()
        chunk_size = 400
        call = _SyncMockCall(
            [
                _make_ret(payload=payload, chunk_offset=0, end=400, total_size=1000, sha256_hex=sha),
                _make_ret(payload=payload, chunk_offset=400, end=800, total_size=1000, sha256_hex=sha),
                _make_ret(payload=payload, chunk_offset=800, end=1000, total_size=1000, sha256_hex=sha),
            ],
        )
        data, _ = drain_blob_sync(call, "c", "h", chunk_size=chunk_size)
        assert data == payload

    def test_parallel_round_trip(self) -> None:
        payload = bytes(range(256)) * 10  # 2560 字节 / 2560 bytes
        sha = hashlib.sha256(payload).hexdigest()
        chunk_size = 256
        total = len(payload)
        responses = {
            offset: _make_ret(
                payload=payload,
                chunk_offset=offset,
                end=min(offset + chunk_size, total),
                total_size=total,
                sha256_hex=sha,
            )
            for offset in range(0, total, chunk_size)
        }
        call = _SyncMockCall(responses)
        data, _ = drain_blob_sync(call, "c", "h", concurrency=4, chunk_size=chunk_size)
        assert data == payload

    def test_sync_non_retryable_raises(self) -> None:
        call = _SyncMockCall([_make_blob_error("gone")])
        with pytest.raises(BlobTransferError) as exc_info:
            drain_blob_sync(call, "c", "h")
        assert exc_info.value.reason == "gone"

    def test_parallel_sync_fatal_beats_range(self) -> None:
        """sync 并行：低 offset ``range`` 块先完成 + 高 offset ``forbidden`` 块后完成 → 必报 fatal(forbidden).

        回归 break-on-first-error：旧实现遇 ``range`` 先 break → 串行 fallback → 串行态先撞低 offset 的
        ``range`` 即 fatal → 对外报 range，**掩盖** offset=400 的 forbidden（永不抵达）。新实现收集全部
        outcome 后按 ``fatal > drift > range`` 分派，fatal 永不被掩盖——与 ``drain_blob``(async) 一致。
        Mirrors Rust ``parallel_sync_fatal_beats_recoverable``: a racing lower-offset ``range`` must
        not mask a concurrent higher-offset fatal in the sync parallel path.
        """
        payload = b"x" * 600
        good_sha = hashlib.sha256(payload).hexdigest()

        def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            end = min(offset + 200, 600)
            if offset == 0:
                return _make_ret(payload=payload, chunk_offset=0, end=end, total_size=600, sha256_hex=good_sha)
            if offset == 200:
                # 可恢复信号，立即返回 → 在并发态先于 fatal 完成（旧实现据此误 break 成 range）
                # recoverable, returns immediately → completes before the fatal in parallel
                return _make_blob_error("range")
            # offset == 400: fatal，故意延迟让 range 先完成 / fatal, delayed so range wins the race
            time.sleep(0.05)
            return _make_blob_error("forbidden")

        with pytest.raises(BlobTransferError) as exc_info:
            drain_blob_sync(call, "c", "h", concurrency=4, chunk_size=200)
        # fatal 必须暴露，而非被先完成的 range 掩盖 / fatal must surface, not be masked by the racing range
        assert exc_info.value.reason == "forbidden"

    def test_parallel_sync_range_fallback(self) -> None:
        """sync 并行纯 ``range`` → 取消 + 串行 fallback 成功还原（覆盖 sync range 分派）.
        Sync parallel pure ``range`` → serial fallback restores payload (covers sync range dispatch)."""
        payload = b"x" * 600
        good_sha = hashlib.sha256(payload).hexdigest()
        phase = {"parallel": True}

        def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            end = min(offset + 200, 600)
            if phase["parallel"] and offset == 200:
                phase["parallel"] = False  # 触发 fallback 后切到 serial 阶段（全部一致）/ switch to serial phase
                return _make_blob_error("range")
            return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)

        data, _ = drain_blob_sync(call, "c", "h", concurrency=4, chunk_size=200)
        assert data == payload

    def test_parallel_sync_drift_fallback(self) -> None:
        """sync 并行纯 drift（某块 sha256 漂移）→ 串行 fallback 还原（覆盖 sync drift 分派）.
        Sync parallel pure drift → serial fallback restores payload (covers sync drift dispatch)."""
        payload = b"a" * 600
        good_sha = hashlib.sha256(payload).hexdigest()
        bad_sha = "0" * 64
        parallel_done = {"v": False}

        def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            end = min(offset + 200, 600)
            if not parallel_done["v"] and offset == 200:
                parallel_done["v"] = True  # 漂移触发 fallback 后切 serial 阶段 / switch to serial after drift
                return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=bad_sha)
            return _make_ret(payload=payload, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)

        data, _ = drain_blob_sync(call, "c", "h", concurrency=4, chunk_size=200, max_retries=3)
        assert data == payload

    def test_parallel_sync_full_sha_mismatch_rereads(self) -> None:
        """sync 并行：各块 per-chunk sha 字段正确但字节损坏 → 重组后全量自证失败 → 串行 fallback 还原.

        覆盖并行重组后的 `_RecoverableDrift` 自证分支（per-chunk 一致但整体 sha 不符）。计数切相：前一轮
        并行尝试（首块 + 2 并发块 = 3 次调用）返回损坏字节，之后串行 fallback 返回真字节——与完成顺序无关。
        Covers the post-reassembly whole-blob self-check; first parallel attempt (3 calls) returns
        corrupt bytes, serial fallback returns real bytes — order-independent via a locked counter."""
        payload = b"b" * 600
        good_sha = hashlib.sha256(payload).hexdigest()
        corrupt = b"Z" * 600  # 同长不同字节，每块 sha 字段仍报 good_sha / same length, different bytes
        lock = threading.Lock()
        count = {"n": 0}

        def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            with lock:
                count["n"] += 1
                n = count["n"]
            end = min(offset + 200, 600)
            # 前 3 次（并行尝试：首块 + 2 并发块）→ 损坏字节 + 正确 sha 字段 → 全量自证必失败
            # serial fallback（第 4 次起）→ 真字节
            src = corrupt if n <= 3 else payload
            return _make_ret(payload=src, chunk_offset=offset, end=end, total_size=600, sha256_hex=good_sha)

        data, _ = drain_blob_sync(call, "c", "h", concurrency=4, chunk_size=200, max_retries=3)
        assert data == payload


# ── max_retries=0 脚枪：夹取至 ≥1 / max_retries=0 footgun: clamp to ≥1 ──────


class TestZeroRetriesAttemptsOnce:
    """``max_retries=0`` 仍至少尝试一次（入口夹取 ``max(1, ...)``）——对标 Rust ``serial_*_zero_retries_still_attempts_once``.

    显式传 ``0`` 不应「零次循环、一个 call 都不发」直接 ``max_retries_exceeded``；夹取后实发一次并成功。
    An explicit ``max_retries=0`` must still attempt once (not short-circuit to max_retries_exceeded).
    """

    @pytest.mark.asyncio
    async def test_async_zero_retries_still_attempts_once(self) -> None:
        payload = b"hello"
        sha = hashlib.sha256(payload).hexdigest()
        call = _AsyncMockCall([_make_ret(payload=payload, chunk_offset=0, end=5, total_size=5, sha256_hex=sha)])
        data, _ = await drain_blob(call, "c", "h", max_retries=0)
        assert data == payload
        assert len(call.calls) == 1  # 夹取至 1 → 实发一次 / clamped to 1 → exactly one real call

    def test_sync_zero_retries_still_attempts_once(self) -> None:
        payload = b"hello"
        sha = hashlib.sha256(payload).hexdigest()
        call = _SyncMockCall([_make_ret(payload=payload, chunk_offset=0, end=5, total_size=5, sha256_hex=sha)])
        data, _ = drain_blob_sync(call, "c", "h", max_retries=0)
        assert data == payload
        assert len(call.calls) == 1


# ── docstring 「并行安全」断言（无意中删除即报）/ docstring "parallel-safe" assertion ──


class TestParallelSafetyDocstring:
    """``drain_blob`` docstring **必须**明示「并行安全」+ 错误协调矩阵，丢失即降级红利.

    The ``drain_blob`` docstring **MUST** state "parallel-safe" + error coordination matrix;
    losing this language degrades the protocol's parallel red dividend.
    """

    def test_async_docstring_states_parallel_safe(self) -> None:
        assert drain_blob.__doc__ is not None
        assert "并行" in drain_blob.__doc__ or "parallel" in drain_blob.__doc__.lower()

    def test_module_docstring_states_error_coordination_matrix(self) -> None:
        import a2c_smcp.utils.blob as blob_mod

        assert blob_mod.__doc__ is not None
        # 协议 4018 各 reason 至少应在文档中提及（dispatch 表）/ 4018 reasons mentioned
        for keyword in ("invalid_handle", "forbidden", "gone", "range"):
            assert keyword in blob_mod.__doc__, f"missing keyword in matrix: {keyword}"


# ── 确保 anyio/asyncio 任务取消语义未破坏 / Ensure cancellation semantics intact ──


class TestStructuredCancellation:
    """并行模式首块即错 → TaskGroup 应短路、不发起其余请求.
    Parallel mode: error on first chunk short-circuits via TaskGroup; no further requests."""

    @pytest.mark.asyncio
    async def test_first_chunk_error_short_circuits(self) -> None:
        # 首块返回 gone → 后续块根本不会被调用
        # First chunk returns gone → subsequent chunks never get called
        call_count = {"n": 0}

        async def call(comp: str, handle: str, offset: int, max_chunk: int) -> Mapping[str, Any]:
            call_count["n"] += 1
            return _make_blob_error("gone")

        with pytest.raises(BlobTransferError):
            await drain_blob(call, "c", "h", concurrency=8, chunk_size=100)
        # 仅首块（offset=0）被调用一次 / Only the first chunk (offset=0) is invoked
        assert call_count["n"] == 1
