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
from collections.abc import Awaitable, Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        max_retries: 串行重读上限（应对源漂移 / 全量 sha256 不一致）；入口夹取至 ``max(1, ...)``——
            显式传 ``0`` 仍至少尝试一次，不会「零次循环直接 max_retries_exceeded」.
            Clamped to ``max(1, ...)`` at entry: an explicit ``0`` still attempts once.

    Returns:
        ``(payload_bytes, mime_type)``: 完整字节内容 + 内容 MIME.

    Raises:
        BlobTransferError: 4018 ``invalid_handle`` / ``forbidden`` / ``gone`` 不重试；
            或 ``max_retries`` 仍未通过 ``sha256`` 校验.
    """
    effective_chunk = chunk_size or DEFAULT_CHUNK_SIZE
    # 夹取至 ≥1：显式 max_retries=0 不应「零次循环、一个 call 都不发」即报 max_retries_exceeded（脚枪）。
    # Clamp to ≥1: an explicit max_retries=0 must still attempt once, never short-circuit to
    # max_retries_exceeded without a single call.
    effective_retries = max(1, max_retries)
    if concurrency <= 1:
        return await _drain_serial_async(call, computer, blob_handle, effective_chunk, effective_retries)
    try:
        return await _drain_parallel_async(call, computer, blob_handle, effective_chunk, concurrency, effective_retries)
    except (_RecoverableDrift, _RecoverableRange) as e:
        logger.info(f"drain_blob: parallel → serial fallback (reason: {type(e).__name__})")
        return await _drain_serial_async(call, computer, blob_handle, effective_chunk, effective_retries)


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
    # 夹取至 ≥1：与 async 入口一致，显式 max_retries=0 仍至少尝试一次（脚枪防御）。
    # Clamp to ≥1: mirrors the async entry; an explicit max_retries=0 still attempts once.
    effective_retries = max(1, max_retries)
    if concurrency <= 1:
        return _drain_serial_sync(call, computer, blob_handle, effective_chunk, effective_retries)
    try:
        return _drain_parallel_sync(call, computer, blob_handle, effective_chunk, concurrency, effective_retries)
    except (_RecoverableDrift, _RecoverableRange) as e:
        logger.info(f"drain_blob_sync: parallel → serial fallback (reason: {type(e).__name__})")
        return _drain_serial_sync(call, computer, blob_handle, effective_chunk, effective_retries)


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

    async def fetch(off: int) -> tuple[str, int, Mapping[str, Any] | None]:
        async with sem:
            ret = await call(computer, blob_handle, off, chunk_size)
        # 可恢复信号（range / 漂移）返回 marker、**不 raise** → 不触发 TaskGroup 取消（镜像 sync：
        # recoverable 永不早退，收集到结束再分派）。仅 fatal（非 range 的 4018）就地 raise → TaskGroup
        # fail-fast 取消其余在飞。如此 fatal 永不被「先完成的 range」经 TaskGroup-cancel 掩盖。
        # Recoverable signals (range / drift) return a marker instead of raising — they do NOT trigger
        # TaskGroup cancellation (mirrors sync: recoverable never short-circuits). Only fatal raises
        # in-place → TaskGroup fail-fast cancels the rest, so a fatal can never be masked by a racing
        # range that finished first (the prior in-place-raise behavior had exactly that leak).
        try:
            _raise_for_blob_error(ret)
        except BlobTransferError as e:
            if e.reason == "range":
                return ("range", off, None)
            raise
        if str(ret["sha256"]) != expected_sha or int(ret["total_size"]) != total_size:
            return ("drift", off, None)
        return ("ok", off, ret)

    # 步骤 3 / Step 3: TaskGroup 结构化并发；**仅 fatal** 触发 fail-fast 取消，recoverable 收集到结束再分派。
    # 与 _drain_parallel_sync 完全一致：收集全部 outcome 后按 fatal > drift > range 分派，fatal 永不被
    # race 中先完成的 range 掩盖（旧实现 range 就地抛 → TaskGroup 取消在飞 fatal → group 仅余 range，
    # 在「快 range + 慢 fatal」竞态下错误降级成 range；marker-collect 消除该漏洞）。
    # Only fatal triggers fail-fast cancellation; recoverable markers are collected and dispatched
    # after the group completes — exactly mirroring _drain_parallel_sync (fatal > drift > range).
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(fetch(off)) for off in offsets]
    except BaseExceptionGroup as eg:
        # recoverable 不再 raise → group 内只可能是 fatal（或 call 自身抛的未知异常）。
        # Recoverable no longer raises → the group can only carry a fatal (or an unknown exc from call).
        flat = _flatten_exception_group(eg)
        fatal = next((sub for sub in flat if isinstance(sub, BlobTransferError)), None)
        if fatal is not None:
            raise fatal from eg
        # 未识别的异常 group → 原样抛 / Unrecognized group → re-raise
        raise

    # 步骤 4 / Step 4: 无 fatal——收集 marker，按 drift > range 分派（fatal 已在 except 优先抛出）
    # Step 4: no fatal — collect markers, dispatch drift > range (fatal already surfaced above)
    has_drift = False
    has_range = False
    for t in tasks:
        kind, off, ret = t.result()
        if kind == "drift":
            has_drift = True  # 漂移：源被改写，从 0 串行重读最稳妥 / drift: source rewritten, serial reread
        elif kind == "range":
            has_range = True
        else:  # "ok"
            assert ret is not None  # marker 不变式：kind=="ok" ⟺ ret 非空 / invariant: ok ⟺ ret present
            chunks[off] = base64.b64decode(ret["blob"])
    if has_drift:
        # drift 优先于 range / drift wins over range
        raise _RecoverableDrift()
    if has_range:
        raise _RecoverableRange()

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

    # 步骤 2-3 / Steps 2-3: ThreadPoolExecutor 并发拉取；按 **完成顺序**（``as_completed``）收集**全部**
    # 已完成 outcome 后再按 ``fatal > drift > range`` 分派——**镜像 async** ``_drain_parallel_async``
    # 的 ``_flatten_exception_group`` 分派，而非「遇首个错误即 break」。
    #
    # 为何不 break-on-first-error：并发态下若一个 ``range`` 块先于一个 fatal（``invalid_handle`` /
    # ``forbidden`` / ``gone``）块完成，先 break → ``_RecoverableRange`` → 串行 fallback → 串行态再遇
    # ``range`` 即 fatal → **对外报 range，掩盖真实 forbidden/gone**（不可重试错误被降级成貌似瞬态）。
    # 那会让 ``drain_blob``(async) 报 forbidden、``drain_blob_sync``(sync) 报 range —— 双端对同一服务端
    # 状况给出不一致诊断。收集全部 outcome 后分派即消除该 race。
    # Collect ALL completed outcomes, then dispatch by fatal > drift > range — mirroring the async
    # path. Breaking on the first error would let a racing ``range`` mask a concurrent fatal,
    # diverging from drain_blob (async). Recoverable signals never break; only fatal stops early.
    results: dict[int, Mapping[str, Any]] = {}
    fatal: BlobTransferError | None = None
    has_drift = False
    has_range = False
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(call, computer, blob_handle, off, chunk_size): off for off in offsets}
        for fut in as_completed(futures):
            off = futures[fut]
            try:
                ret = fut.result()
                _raise_for_blob_error(ret)
                if str(ret["sha256"]) != expected_sha or int(ret["total_size"]) != total_size:
                    has_drift = True  # 漂移：不 break，继续收集（让并存 fatal 必被发现）/ drift: keep collecting
                    continue
                results[off] = ret
            except BlobTransferError as e:
                if e.reason == "range":
                    has_range = True  # range：不 break，继续收集 / range: keep collecting
                else:
                    fatal = fatal or e  # 仅 fatal 记录并停止；运行中 future 无法真正取消，等其自然完成
                    break
        # 取消其余 future（best-effort，ThreadPoolExecutor 无法真正终止已运行任务）
        # Cancel remaining (best-effort; ThreadPoolExecutor cannot terminate in-flight work)
        for fut in futures:
            if not fut.done():
                fut.cancel()
    # 分派优先级与 async 完全一致：fatal > drift > range / Same priority as async: fatal > drift > range
    if fatal is not None:
        raise fatal
    if has_drift:
        raise _RecoverableDrift()
    if has_range:
        raise _RecoverableRange()

    chunks.update({off: base64.b64decode(r["blob"]) for off, r in results.items()})
    full = b"".join(chunks[off] for off in sorted(chunks))
    if hashlib.sha256(full).hexdigest() != expected_sha:
        raise _RecoverableDrift()
    return full, mime


# ── 通用辅助 / Common helpers ────────────────────────────────────────────


def _flatten_exception_group(eg: BaseExceptionGroup) -> Iterator[BaseException]:
    """递归扁平化 ExceptionGroup → 叶子异常 / Recursively flatten ExceptionGroup into leaf exceptions.

    TaskGroup 在并发态可能产生嵌套 group（fatal + recoverable 同时触发时）；扁平化后由调用方
    集中分派优先级，避免 "outer plain ``except`` 不接 group 而漏走 fallback" 的隐蔽 race。
    TaskGroup may produce nested groups in race scenarios; flattening lets the caller dispatch
    priorities centrally without leaking through an outer plain-tuple ``except``.
    """
    for exc in eg.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            yield from _flatten_exception_group(exc)
        else:
            yield exc


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
