# -*- coding: utf-8 -*-
# filename: blob.py
# @Author  : JQQ
# @Software: PyCharm

"""
统一通用二进制拉取例程 / Unified generic binary-pull routine.

供 Agent SDK（async / sync）在 ``get_skill`` 与 ``tool_call`` 二进制旁路两处共用，避免拉取
循环、错误协调、并行重组三处重复。
Shared by Agent SDK (async + sync) for ``get_skill`` and ``tool_call`` binary sideband — eliminates
triplicate fetch loops, error coordination, and parallel reassembly.

协议依据 / Protocol: ``a2c-smcp-protocol`` ``docs/specification/blob-transfer.md``。
设计依据 / Design source: ``docs/design-0.2.1-skill-computer-management.md`` §4.5。

并行安全 / Parallel safety:
    ``client:get_blob`` 协议 §3 明文：``chunk_offset`` 为资源字节绝对偏移、Computer 无服务端
    状态 → 「天然幂等、可并行不同 offset」。本例程在 ``concurrency>1`` 时启用并行红利——
    与 SFTP 等有状态句柄拉开差距；丢失即等同自我贬值。Each chunk request is idempotent and
    parallelizable; the parallel branch realizes that protocol guarantee.

错误协调矩阵 / Error coordination matrix (concurrency>1):
    +-----------------------------+---------------------------------------+
    | 4018 ``invalid_handle``     | 取消所有在飞 + raise                  |
    | 4018 ``forbidden``          | 取消所有在飞 + raise                  |
    | 4018 ``gone``               | 取消所有在飞 + raise（上层回生产者）  |
    | 4018 ``range``              | 取消 + 串行 fallback 从 0 重读        |
    | sha256 / total_size 漂移    | 取消 + 串行 fallback 从 0 重读        |
    | 全量 sha256 校验失败        | 串行从 0 重读（最多 ``max_retries``） |
    +-----------------------------+---------------------------------------+

串行模式同表，但「取消所有在飞」简化为顺序退出当前循环。
Serial mode follows the same table; "cancel all" simplifies to exiting the loop.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from a2c_smcp.smcp import ErrorCode

# ── 公开类型 / Public types ──────────────────────────────────────────────

AsyncBlobCall = Callable[[str, str, int, int], Awaitable[Mapping[str, Any]]]
"""异步单块拉取函数签名 / Async single-chunk pull callable signature.

签名 / Signature: ``(computer, blob_handle, chunk_offset, max_chunk_bytes) -> GetBlobRet | ErrorPayload dict``

调用方（Agent SDK）封装底层 socketio.AsyncClient.call，注入 ``namespace`` / ``agent`` /
``req_id`` 等业务字段后再传入本例程。
The caller (Agent SDK) wraps socketio.AsyncClient.call, injecting ``namespace`` / ``agent`` /
``req_id`` etc, then passes the wrapped callable into this routine.
"""

SyncBlobCall = Callable[[str, str, int, int], Mapping[str, Any]]
"""同步单块拉取函数签名 / Sync single-chunk pull callable signature.

Sync 镜像，签名同上但直接返回 dict（不是 coroutine）。
Sync mirror of ``AsyncBlobCall``; returns dict directly (no coroutine).
"""

DEFAULT_CHUNK_SIZE: int = 256 * 1024  # 256 KiB — 与 Computer 端 BlobThresholds.chunk_max_bytes 一致默认
DEFAULT_MAX_RETRIES: int = 3  # 跨块漂移 / 全量 sha256 不一致情形下的串行重读上限

logger = logging.getLogger(__name__)


# ── 内部异常 / Internal exceptions ───────────────────────────────────────


class BlobTransferError(Exception):
    """``drain_blob`` 拉取阶段不可恢复错误基类 / Unrecoverable error during ``drain_blob`` pull.

    Attributes:
        reason: 协议 4018 ``details.reason`` 值（``invalid_handle`` / ``forbidden`` / ``gone`` /
            ``range``），或 ``"sha256_mismatch"`` / ``"max_retries_exceeded"`` 这类客户端自检失败.
            Protocol ``4018 details.reason`` value or client-side diagnostic.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class _RecoverableDrift(Exception):
    """跨块 sha256 / total_size 漂移 → 触发串行重读（内部信号，外部不可见）.

    Internal signal for cross-chunk drift requiring serial reread; not raised externally.
    """


class _RecoverableRange(Exception):
    """并发态遇到 ``range`` → 触发串行 fallback（内部信号）.

    Internal signal for parallel-mode range error requiring serial fallback; internal only.
    """


# ── 公开 API / Public API ────────────────────────────────────────────────


async def drain_blob(
    call: AsyncBlobCall,
    computer: str,
    blob_handle: str,
    *,
    concurrency: int = 1,
    chunk_size: int | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[bytes, str]:
    """异步拉取 blob 全量字节 / Asynchronously pull all blob bytes.

    并行安全 / Parallel-safe: 当 ``concurrency > 1`` 时利用协议 §3「天然幂等、可并行不同 offset」
    红利并发拉取剩余块，按 offset 重组并校验全量 ``sha256``。

    Args:
        call: 单块拉取函数（异步），签名见 :data:`AsyncBlobCall`.
        computer: 目标 Computer 名（仅诊断；call 已具体路由）.
        blob_handle: 来自某生产者通道的不透明句柄.
        concurrency: 并发度；``1`` 串行（保守默认），``>1`` 启用并行模式.
        chunk_size: 客户建议单块上限；缺省 :data:`DEFAULT_CHUNK_SIZE`（Computer 会 clamp）.
        max_retries: 串行重读上限（应对源漂移 / 全量 sha256 不一致）.

    Returns:
        ``(payload_bytes, mime_type)``: 完整字节内容 + 内容 MIME.

    Raises:
        BlobTransferError: 4018 ``invalid_handle`` / ``forbidden`` / ``gone`` 不重试；
            或 ``max_retries`` 仍未通过 ``sha256`` 校验.
    """
    effective_chunk = chunk_size or DEFAULT_CHUNK_SIZE
    if concurrency <= 1:
        return await _drain_serial_async(call, computer, blob_handle, effective_chunk, max_retries)
    try:
        return await _drain_parallel_async(call, computer, blob_handle, effective_chunk, concurrency, max_retries)
    except (_RecoverableDrift, _RecoverableRange) as e:
        logger.info(f"drain_blob: parallel → serial fallback (reason: {type(e).__name__})")
        return await _drain_serial_async(call, computer, blob_handle, effective_chunk, max_retries)


def drain_blob_sync(
    call: SyncBlobCall,
    computer: str,
    blob_handle: str,
    *,
    concurrency: int = 1,
    chunk_size: int | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[bytes, str]:
    """同步拉取 blob 全量字节（async 镜像）/ Synchronous mirror of :func:`drain_blob`.

    ``concurrency > 1`` 用 :class:`concurrent.futures.ThreadPoolExecutor` 并发拉取，错误协调矩阵
    与 async 版完全一致。
    Parallel branch uses ``ThreadPoolExecutor``; error coordination matrix mirrors async exactly.
    """
    effective_chunk = chunk_size or DEFAULT_CHUNK_SIZE
    if concurrency <= 1:
        return _drain_serial_sync(call, computer, blob_handle, effective_chunk, max_retries)
    try:
        return _drain_parallel_sync(call, computer, blob_handle, effective_chunk, concurrency, max_retries)
    except (_RecoverableDrift, _RecoverableRange) as e:
        logger.info(f"drain_blob_sync: parallel → serial fallback (reason: {type(e).__name__})")
        return _drain_serial_sync(call, computer, blob_handle, effective_chunk, max_retries)


# ── 串行实现 / Serial implementations ────────────────────────────────────


async def _drain_serial_async(
    call: AsyncBlobCall,
    computer: str,
    blob_handle: str,
    chunk_size: int,
    max_retries: int,
) -> tuple[bytes, str]:
    for attempt in range(max_retries):
        try:
            return await _do_serial_drain_async(call, computer, blob_handle, chunk_size)
        except _RecoverableDrift:
            logger.info(f"drain_blob: source drift detected, restarting from offset=0 (attempt {attempt + 1})")
            continue
    raise BlobTransferError(reason="max_retries_exceeded", message=f"source drift unresolved after {max_retries} retries")


def _drain_serial_sync(
    call: SyncBlobCall,
    computer: str,
    blob_handle: str,
    chunk_size: int,
    max_retries: int,
) -> tuple[bytes, str]:
    for attempt in range(max_retries):
        try:
            return _do_serial_drain_sync(call, computer, blob_handle, chunk_size)
        except _RecoverableDrift:
            logger.info(f"drain_blob_sync: source drift detected, restarting from offset=0 (attempt {attempt + 1})")
            continue
    raise BlobTransferError(reason="max_retries_exceeded", message=f"source drift unresolved after {max_retries} retries")


async def _do_serial_drain_async(
    call: AsyncBlobCall,
    computer: str,
    blob_handle: str,
    chunk_size: int,
) -> tuple[bytes, str]:
    offset = 0
    accumulator: list[bytes] = []
    first_sha: str | None = None
    first_size: int | None = None
    mime: str = ""
    while True:
        ret = await call(computer, blob_handle, offset, chunk_size)
        _raise_for_blob_error(ret)
        cur_sha = str(ret["sha256"])
        cur_size = int(ret["total_size"])
        if first_sha is None:
            first_sha, first_size, mime = cur_sha, cur_size, str(ret["mime_type"])
        elif cur_sha != first_sha or cur_size != first_size:
            raise _RecoverableDrift()
        decoded = base64.b64decode(ret["blob"])
        accumulator.append(decoded)
        offset = int(ret["chunk_offset"]) + len(decoded)
        if ret.get("eof"):
            break
    full = b"".join(accumulator)
    assert first_sha is not None  # 不变式：循环至少跑一次（首块即设 first_sha）
    if hashlib.sha256(full).hexdigest() != first_sha:
        raise _RecoverableDrift()
    return full, mime


def _do_serial_drain_sync(
    call: SyncBlobCall,
    computer: str,
    blob_handle: str,
    chunk_size: int,
) -> tuple[bytes, str]:
    offset = 0
    accumulator: list[bytes] = []
    first_sha: str | None = None
    first_size: int | None = None
    mime: str = ""
    while True:
        ret = call(computer, blob_handle, offset, chunk_size)
        _raise_for_blob_error(ret)
        cur_sha = str(ret["sha256"])
        cur_size = int(ret["total_size"])
        if first_sha is None:
            first_sha, first_size, mime = cur_sha, cur_size, str(ret["mime_type"])
        elif cur_sha != first_sha or cur_size != first_size:
            raise _RecoverableDrift()
        decoded = base64.b64decode(ret["blob"])
        accumulator.append(decoded)
        offset = int(ret["chunk_offset"]) + len(decoded)
        if ret.get("eof"):
            break
    full = b"".join(accumulator)
    assert first_sha is not None
    if hashlib.sha256(full).hexdigest() != first_sha:
        raise _RecoverableDrift()
    return full, mime


# ── 并行实现 / Parallel implementations ──────────────────────────────────


async def _drain_parallel_async(
    call: AsyncBlobCall,
    computer: str,
    blob_handle: str,
    chunk_size: int,
    concurrency: int,
    max_retries: int,  # noqa: ARG001 — retries 仅在 fallback 后由 serial 路径消费
) -> tuple[bytes, str]:
    # 步骤 1 / Step 1: 首块串行获知 total_size / sha256 / mime
    first = await call(computer, blob_handle, 0, chunk_size)
    _raise_for_blob_error(first)
    total_size = int(first["total_size"])
    expected_sha = str(first["sha256"])
    mime = str(first["mime_type"])
    chunks: dict[int, bytes] = {0: base64.b64decode(first["blob"])}
    if first.get("eof") or total_size == 0:
        full = chunks[0]
        if hashlib.sha256(full).hexdigest() != expected_sha:
            raise _RecoverableDrift()
        return full, mime

    # 步骤 2 / Step 2: 计算剩余 offset 集合
    first_chunk_len = len(chunks[0])
    offsets = list(range(first_chunk_len, total_size, chunk_size))
    sem = asyncio.Semaphore(concurrency)

    async def fetch(off: int) -> tuple[int, Mapping[str, Any]]:
        async with sem:
            ret = await call(computer, blob_handle, off, chunk_size)
        # 单块协议错误就地抛 → TaskGroup 自动取消其他在飞任务
        # Per-chunk protocol errors raised in-place → TaskGroup auto-cancels others
        _raise_for_blob_error(ret)
        # 漂移就地抛 → 与协议错误一并进入 TaskGroup except* 分支
        # Drift raised in-place → joins protocol errors in the TaskGroup except* dispatch
        if str(ret["sha256"]) != expected_sha or int(ret["total_size"]) != total_size:
            raise _RecoverableDrift()
        return off, ret

    # 步骤 3 / Step 3: TaskGroup 结构化并发；首个失败 → 自动取消所有在飞
    # TaskGroup gives us structured cancellation: first failure auto-cancels all pending
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(fetch(off)) for off in offsets]
    except* BlobTransferError as eg:
        # invalid_handle / forbidden / gone / range → 上层处理（range 也直接外抛由 drain_blob 转 fallback）
        first_exc = eg.exceptions[0]
        if isinstance(first_exc, BlobTransferError) and first_exc.reason == "range":
            raise _RecoverableRange() from first_exc
        raise first_exc from eg
    except* _RecoverableDrift as eg:
        # 任一块 sha256/total_size 漂移 → 串行 fallback 从 0 重读
        raise _RecoverableDrift() from eg

    # 步骤 4 / Step 4: 聚合（每块已在 fetch 内校验协议错误 + 漂移）
    # Step 4: collect (each chunk already validated inside fetch)
    for t in tasks:
        off, ret = t.result()
        chunks[off] = base64.b64decode(ret["blob"])

    # 步骤 5 / Step 5: 按 offset 重组 + 全量 sha256 自证
    full = b"".join(chunks[off] for off in sorted(chunks))
    if hashlib.sha256(full).hexdigest() != expected_sha:
        raise _RecoverableDrift()
    return full, mime


def _drain_parallel_sync(
    call: SyncBlobCall,
    computer: str,
    blob_handle: str,
    chunk_size: int,
    concurrency: int,
    max_retries: int,  # noqa: ARG001
) -> tuple[bytes, str]:
    # 步骤 1
    first = call(computer, blob_handle, 0, chunk_size)
    _raise_for_blob_error(first)
    total_size = int(first["total_size"])
    expected_sha = str(first["sha256"])
    mime = str(first["mime_type"])
    chunks: dict[int, bytes] = {0: base64.b64decode(first["blob"])}
    if first.get("eof") or total_size == 0:
        full = chunks[0]
        if hashlib.sha256(full).hexdigest() != expected_sha:
            raise _RecoverableDrift()
        return full, mime

    first_chunk_len = len(chunks[0])
    offsets = list(range(first_chunk_len, total_size, chunk_size))

    # 步骤 2-3 / Steps 2-3: ThreadPoolExecutor 并发拉取；首个失败立即取消其余（best-effort）
    # First failure cancels best-effort; ThreadPoolExecutor cannot truly cancel running futures
    results: dict[int, Mapping[str, Any]] = {}
    first_error: BaseException | None = None
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(call, computer, blob_handle, off, chunk_size): off for off in offsets}
        for fut in list(futures):
            try:
                ret = fut.result()
                _raise_for_blob_error(ret)
                if str(ret["sha256"]) != expected_sha or int(ret["total_size"]) != total_size:
                    first_error = _RecoverableDrift()
                    break
                results[futures[fut]] = ret
            except BlobTransferError as e:
                first_error = e
                break
            except _RecoverableDrift as e:  # pragma: no cover — already wrapped above
                first_error = e
                break
        # 取消其余 future（best-effort）/ Cancel remaining (best-effort)
        for fut in futures:
            if not fut.done():
                fut.cancel()
    if first_error is not None:
        if isinstance(first_error, BlobTransferError) and first_error.reason == "range":
            raise _RecoverableRange() from first_error
        raise first_error

    chunks.update({off: base64.b64decode(r["blob"]) for off, r in results.items()})
    full = b"".join(chunks[off] for off in sorted(chunks))
    if hashlib.sha256(full).hexdigest() != expected_sha:
        raise _RecoverableDrift()
    return full, mime


# ── 通用辅助 / Common helpers ────────────────────────────────────────────


def _raise_for_blob_error(payload: Mapping[str, Any]) -> None:
    """检测 flat ErrorPayload (``code=4018``)，按 ``details.reason`` 抛 :class:`BlobTransferError`。
    Detect a flat ``4018`` ErrorPayload and raise :class:`BlobTransferError` keyed by ``details.reason``.

    成功响应（无 ``code`` 字段）直接 ``return``，由调用方继续消费。
    Success payloads (no ``code``) return; caller continues normal flow.
    """
    code = payload.get("code")
    if code is None:
        return
    if code == int(ErrorCode.BLOB_NOT_ACCESSIBLE):
        details = payload.get("details") or {}
        reason = str(details.get("reason", "invalid_handle"))
        raise BlobTransferError(reason=reason, message=str(payload.get("message", "Blob not accessible")))
    # 其它协议错误码：直接以原始 code 透传（不再 swallow 成 success）
    # Other protocol error codes: surface them verbatim (do not silently swallow)
    raise BlobTransferError(
        reason=f"protocol_error_{code}",
        message=str(payload.get("message", f"Protocol error code {code}")),
    )
