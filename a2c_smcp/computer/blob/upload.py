# -*- coding: utf-8 -*-
# filename: upload.py
# @Author  : JQQ
# @Software: PyCharm
"""
``client:put_blob`` 上行写入的有界上传会话管理（v0.4.0 #196）。
Bounded upload-session management for the ``client:put_blob`` write channel (v0.4.0 #196).

协议依据 / Protocol: ``a2c-smcp-protocol`` ``docs/specification/blob-transfer.md`` §3（事件 +
上传会话生命周期）/ §7（landing 沙箱）；``error-handling.md`` §4019（reason 开放枚举）。

核心不变量 / Core invariants:
  - **写入沙箱由写入原语强制**（§7）：一切落盘（``.part`` 与最终产物）构造上严格落于 landing root
    内（``upload_id`` 派生安全名 + 消毒后 ``name_hint``）；Agent 拿不到写任意路径的能力。
  - **有界会话 MUST**（§3）：闲置超时 + 并发上限 + 孤儿 ``.part`` GC（阈值经 :class:`BlobThresholds`
    注入，SDK 自治、不进协议常量）；无跨尝试断点（失败重试 = 新 ``upload_id`` 从 0 重传）。
  - **声明-校验镜像**：Agent 首块声明 ``total_size`` / ``sha256``，Computer 增量计算、末块比对；
    不符 → ``4019 integrity``（丢弃 ``.part``，不返回 path）。增量 hasher 的累积 digest 即全量
    sha256（数学等价于「末块重算」，协议 RFC 叙述级推荐 ``.part`` + 增量 + 原子 rename）。
  - **in-order 强制**：``chunk_offset`` == 已收字节（无稀疏缓冲）；末块另需
    ``chunk_offset + 本块字节数 == total_size``。
  - **fail-closed**：landing root 未配置 / 不可写 → ``4019 forbidden``（零字节落盘）。

落盘布局 / On-disk layout:
  - in-flight:  ``<landingRoot>/.a2c-upload/<upload_id>.part``
  - finalized: ``<landingRoot>/<upload_id>[_<sanitized name_hint>]``（``os.replace`` 原子定稿）
  - GC 严格限于 landing root 内（目录成员白名单 + 路径 resolve 围栏断言）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from a2c_smcp.computer.blob.thresholds import BlobThresholds
from a2c_smcp.smcp import ErrorCode, ErrorPayload, PutBlobRet

logger = logging.getLogger(__name__)

# in-flight ``.part`` 子目录（landing root 内，与最终产物分离，GC 只扫这里）。
# In-flight ``.part`` subdirectory inside the landing root (kept apart from final artifacts).
_PART_DIR_NAME = ".a2c-upload"

# ``name_hint`` 消毒白名单：字母 / 数字 / ``.`` / ``-`` / ``_``；其余字符折叠为 ``_``。
# Sanitization whitelist for ``name_hint``: alnum plus ``.-_``; everything else folds to ``_``.
_SAFE_NAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_HINT_LEN = 64
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _blob_write_error(*, reason: str, message: str, **details: Any) -> ErrorPayload:
    """构造 ``4019 Blob Write Failed`` flat ErrorPayload（``reason`` 等经 ``details`` 下沉）。

    Build the ``4019`` flat ErrorPayload (``reason`` etc. under ``details``, per
    error-handling.md §4019 / §错误响应格式「code-specific 字段下沉 details」).
    """
    payload_details: dict[str, Any] = {"reason": reason}
    payload_details.update(details)
    return ErrorPayload(
        code=int(ErrorCode.BLOB_WRITE_FAILED),
        message=message,
        details=payload_details,
    )


def sanitize_name_hint(name_hint: str | None) -> str:
    """
    消毒 ``name_hint`` 为安全文件名片段 / Sanitize ``name_hint`` into a safe filename fragment.

    规则 / Rules（协议 §7「Computer 生成安全名」的 python-sdk 实现，SDK 自治）:
      - ``None`` / 空 → 空串（最终名 = 纯 ``upload_id``）
      - 取 basename（拒任何路径分隔语义）、白名单外字符折叠 ``_``、剥前后 ``._-``
      - 长度夹取 ``_MAX_NAME_HINT_LEN``；消毒后为空（如 ``name_hint="../.."``）→ 空串

    返回值恒可安全嵌入 ``f"{upload_id}_{fragment}"``（``upload_id`` 为 hex32 前缀，
    构造上杜绝穿越与 ``.`` / ``..`` 目标）。
    The result is always safe to embed after the hex32 ``upload_id`` prefix.
    """
    if not name_hint:
        return ""
    fragment = _SAFE_NAME_CHARS_RE.sub("_", name_hint.strip())[:_MAX_NAME_HINT_LEN].strip("._-")
    return fragment


class _UploadSession:
    """单个在途上传会话的受限状态 / The bounded state of one in-flight upload."""

    __slots__ = ("upload_id", "part_path", "final_path", "fh", "received", "hasher", "last_active",
                 "total_size", "declared_sha256")

    def __init__(self, upload_id: str, part_path: Path, final_path: Path, fh: Any,
                 total_size: int, declared_sha256: str, now: float) -> None:
        self.upload_id = upload_id
        self.part_path = part_path
        self.final_path = final_path
        self.fh = fh
        self.received = 0
        self.hasher = hashlib.sha256()
        self.last_active = now
        self.total_size = total_size
        self.declared_sha256 = declared_sha256

    def close(self) -> None:
        """关闭 ``.part`` 句柄并删除残留文件（幂等）/ Close the ``.part`` handle + unlink (idempotent)."""
        try:
            self.fh.close()
        except OSError:
            logger.debug("put_blob: closing stale .part handle failed for %s", self.upload_id, exc_info=True)
        try:
            self.part_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("put_blob: cannot unlink stale .part %s", self.part_path, exc_info=True)


class BlobUploadStore:
    """
    ``client:put_blob`` 上传会话表（线程安全，有界 MUST）。

    The upload-session table for ``client:put_blob`` (thread-safe; bounded per protocol MUST).

    landing root 由调用方每次传入（config-first：Computer 从 settings resolve 的 ``landingRoot``
    取值，进程生命周期内缓存——运行期变更需重启 Computer，部署决策）。会话状态（``.part`` 句柄 /
    已收字节 / 增量 hasher）**只在内存**；进程重启即全部作废，遗留 ``.part`` 由孤儿 GC 回收。

    阈值（闲置超时 / 并发上限 / 绝对上限）经 :class:`BlobThresholds` 注入；env 覆盖见
    :func:`a2c_smcp.computer.blob.thresholds.default_thresholds`。
    """

    def __init__(self, landing_root: Path | None, thresholds: BlobThresholds) -> None:
        self._landing_root = Path(landing_root) if landing_root is not None else None
        self._thresholds = thresholds
        self._sessions: dict[str, _UploadSession] = {}
        self._lock = threading.Lock()

    @property
    def landing_root(self) -> Path | None:
        """本 store 绑定的落盘根（``None`` = 未配置，fail-closed 拒绝一切上传）。"""
        return self._landing_root

    # ── 对外入口 / Public entry ────────────────────────────────────────────

    def handle_chunk(self, data: Mapping[str, Any]) -> PutBlobRet | ErrorPayload:
        """
        处理单个 ``client:put_blob`` 块（首块 / 后续块 / 末块统一入口）。

        Handle one ``client:put_blob`` chunk (first / middle / final in one entry).

        Args:
            data: ``PutBlobReq`` 形态的 dict（``req_id`` 回显进 ack）。

        Returns:
            ``PutBlobRet``（成功）或 flat ``4019`` ErrorPayload。

        线程安全：全路径持锁（块级 ``.part`` 追加 ≤ 256 KiB 量级，锁内 IO 可忽略）。
        Thread-safe: the whole path runs under the lock (chunk-sized appends only).
        """
        with self._lock:
            self._expire_stale_sessions()
            upload_id = data.get("upload_id")
            if upload_id is None:
                return self._handle_first_chunk(data)
            return self._handle_subsequent_chunk(data, str(upload_id))

    def discard_all(self) -> None:
        """作废全部在途会话（测试 / 关停清理）/ Drop all in-flight sessions (tests / shutdown)."""
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()

    # ── 首块 / First chunk ────────────────────────────────────────────────

    def _handle_first_chunk(self, data: Mapping[str, Any]) -> PutBlobRet | ErrorPayload:
        # 1) fail-closed：landing root 未配置 → forbidden（零字节落盘，§7）。
        if self._landing_root is None:
            logger.warning("client:put_blob rejected: landingRoot not configured (fail-closed)")
            return _blob_write_error(reason="forbidden", message="landing root not configured")

        # 2) 声明校验（字段齐备、total_size ≥ 1、sha256 为 64 位 hex）→ invalid_declaration。
        total_size = data.get("total_size")
        declared_sha256 = data.get("sha256")
        if not isinstance(total_size, int) or isinstance(total_size, bool) or total_size < 1:
            logger.warning("client:put_blob invalid declaration: total_size=%r", total_size)
            return _blob_write_error(reason="invalid_declaration", message="total_size must be an int >= 1")
        if not isinstance(declared_sha256, str) or _SHA256_HEX_RE.match(declared_sha256) is None:
            logger.warning("client:put_blob invalid declaration: sha256=%r", declared_sha256)
            return _blob_write_error(reason="invalid_declaration", message="sha256 must be a 64-char hex string")

        # 3) 绝对上限（首块决断，零字节落盘）→ too_large（拒绝路径不建任何目录/文件）。
        if total_size > self._thresholds.upload_max_bytes:
            logger.warning(
                "client:put_blob too_large: declared=%d cap=%d", total_size, self._thresholds.upload_max_bytes
            )
            return _blob_write_error(
                reason="too_large", message="declared total_size exceeds the upload cap", total_size=total_size
            )

        # 4) 并发上限 → busy（Agent SHOULD 退避后从 0 重传）。
        if len(self._sessions) >= self._thresholds.upload_max_concurrent:
            logger.warning(
                "client:put_blob busy: %d/%d sessions in flight",
                len(self._sessions), self._thresholds.upload_max_concurrent,
            )
            return _blob_write_error(reason="busy", message="too many concurrent uploads")

        # 5) 接纳会话：建 landing 暂存目录（不可建 → forbidden「沙箱不可写」）+ 开 `.part` 句柄。
        part_dir = self._landing_root / _PART_DIR_NAME
        try:
            part_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("client:put_blob landing root not writable: %s (%s)", self._landing_root, e)
            return _blob_write_error(reason="forbidden", message="landing root not writable")
        upload_id = uuid.uuid4().hex
        name_hint = data.get("name_hint")
        fragment = sanitize_name_hint(name_hint if isinstance(name_hint, str) else None)
        final_name = f"{upload_id}_{fragment}" if fragment else upload_id
        part_path = part_dir / f"{upload_id}.part"
        final_path = self._landing_root / final_name
        try:
            fh = open(part_path, "wb")
        except OSError as e:
            logger.warning("client:put_blob cannot create .part %s: %s", part_path, e)
            return _blob_write_error(reason="forbidden", message="landing root not writable")
        session = _UploadSession(
            upload_id=upload_id,
            part_path=part_path,
            final_path=final_path,
            fh=fh,
            total_size=total_size,
            declared_sha256=declared_sha256.lower(),
            now=time.monotonic(),
        )
        self._sessions[upload_id] = session
        # 孤儿 GC 挂在首块建立点（上传频率低，扫描代价可忽略；此时新会话已在 live 集合）。
        # 覆盖「进程重启后表空、无 stale 可触发」的盲区——崩溃遗留 .part 在下一次上传时回收。
        # Orphan GC rides on first-chunk creation (new session already in the live set); this
        # covers the post-restart blind spot where an empty table can never trigger stale expiry.
        self._collect_orphan_parts()
        # 首块即 eof（单块上传）为合法退化：fallthrough 到通用块路径一次定稿。
        # First chunk with eof=True (single-chunk upload) is a legal degenerate: fall through.
        return self._append_chunk(data, session)

    # ── 后续块 / Subsequent chunks ────────────────────────────────────────

    def _handle_subsequent_chunk(self, data: Mapping[str, Any], upload_id: str) -> PutBlobRet | ErrorPayload:
        session = self._sessions.get(upload_id)
        if session is None:
            logger.warning("client:put_blob unknown/expired upload_id: %r", upload_id)
            return _blob_write_error(
                reason="invalid_upload", message="unknown or expired upload session", upload_id=upload_id
            )
        # 声明字段仅首块携带，后续块 MUST NOT（违反 → invalid_declaration，§3 流程 2）。
        if any(k in data for k in ("total_size", "sha256", "name_hint")):
            logger.warning("client:put_blob declaration fields re-sent on a subsequent chunk: %r", upload_id)
            return _blob_write_error(
                reason="invalid_declaration", message="declaration fields must only appear on the first chunk"
            )
        return self._append_chunk(data, session)

    # ── 通用块路径（首块 / 后续块共享）/ Common chunk path ─────────────────

    def _append_chunk(self, data: Mapping[str, Any], session: _UploadSession) -> PutBlobRet | ErrorPayload:
        req_id = str(data.get("req_id", ""))
        chunk_offset = data.get("chunk_offset")
        eof = bool(data.get("eof", False))
        blob_b64 = data.get("blob")

        # in-order：chunk_offset == 已收字节（无稀疏缓冲）→ range。
        if not isinstance(chunk_offset, int) or isinstance(chunk_offset, bool) or chunk_offset != session.received:
            logger.warning(
                "client:put_blob out-of-order chunk: offset=%r received=%d upload_id=%s",
                chunk_offset, session.received, session.upload_id,
            )
            return _blob_write_error(
                reason="range", message="chunk_offset does not match received bytes", upload_id=session.upload_id
            )

        # base64 解码失败（非法载荷）→ invalid_declaration（块字段非法族）。
        if not isinstance(blob_b64, str):
            return _blob_write_error(
                reason="invalid_declaration", message="blob must be a base64 string", upload_id=session.upload_id
            )
        try:
            chunk = base64.b64decode(blob_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            logger.warning("client:put_blob base64 decode failed (%s): %s", session.upload_id, e)
            return _blob_write_error(
                reason="invalid_declaration", message="blob is not valid base64", upload_id=session.upload_id
            )

        # 过流防御（DoS）：**任何**块（含非 eof）写后不得超首块声明的 ``total_size``。声明即契约
        # （§6「声明后不可变」）——首块 ``too_large`` 决断的前提是接收字节 == 声明字节；若无此
        # 检查，恶意 Agent 可声明 1 字节后无限追加非 eof 块（每块 in-order 合法、持续刷新
        # last_active），``upload_max_bytes`` 被击穿、``.part`` 无界增长。追加写不可回退，超界即
        # 不可恢复违约 → 作废会话（Agent 重试 = 新 upload_id 从 0 重传，§3 无跨尝试断点）。
        # Overrun guard: received bytes must never exceed the declared total_size (any chunk);
        # otherwise the first-chunk too_large cap is bypassable by endless in-order appends.
        if chunk_offset + len(chunk) > session.total_size:
            logger.warning(
                "client:put_blob chunk overruns declared total_size: end=%d declared=%d upload_id=%s",
                chunk_offset + len(chunk), session.total_size, session.upload_id,
            )
            self._drop_session(session)
            return _blob_write_error(
                reason="range", message="received bytes exceed the declared total_size",
                upload_id=session.upload_id,
            )

        # 末块总量一致性：chunk_offset + 本块字节数 == total_size，否则 range（§3 流程 4）。
        if eof and chunk_offset + len(chunk) != session.total_size:
            logger.warning(
                "client:put_blob final chunk size mismatch: end=%d declared=%d upload_id=%s",
                chunk_offset + len(chunk), session.total_size, session.upload_id,
            )
            return _blob_write_error(
                reason="range", message="final chunk does not complete the declared total_size",
                upload_id=session.upload_id,
            )

        # 追加 + 增量 hash；IO 失败 → io_error（作废会话，删 .part）。
        try:
            session.fh.write(chunk)
            session.hasher.update(chunk)
        except OSError as e:
            logger.warning("client:put_blob .part write failed (%s): %s", session.upload_id, e)
            self._drop_session(session)
            return _blob_write_error(
                reason="io_error", message="write to landing root failed", upload_id=session.upload_id
            )
        session.received += len(chunk)
        session.last_active = time.monotonic()

        if not eof:
            ret: PutBlobRet = {
                "upload_id": session.upload_id,
                "chunk_offset": chunk_offset,
                "req_id": req_id,
            }
            return ret
        return self._finalize(session, chunk_offset, req_id)

    def _finalize(self, session: _UploadSession, chunk_offset: int, req_id: str) -> PutBlobRet | ErrorPayload:
        """末块定稿：fsync → 完整性比对 → 原子 rename 进 landing root（§3 流程 4）。"""
        try:
            session.fh.flush()
            os.fsync(session.fh.fileno())
        except OSError as e:
            logger.warning("client:put_blob fsync failed (%s): %s", session.upload_id, e)
            self._drop_session(session)
            return _blob_write_error(
                reason="io_error", message="flushing the upload failed", upload_id=session.upload_id
            )
        # 完整性：增量 hasher 的累积 digest 即 Computer 重算的全量 sha256（数学等价「末块重算」）。
        recomputed = session.hasher.hexdigest()
        if recomputed != session.declared_sha256:
            logger.warning(
                "client:put_blob integrity mismatch: upload_id=%s declared=%s recomputed=%s",
                session.upload_id, session.declared_sha256, recomputed,
            )
            self._drop_session(session)
            return _blob_write_error(
                reason="integrity", message="sha256 mismatch; upload discarded", upload_id=session.upload_id
            )
        # 原子 rename 定稿（`.part` → 安全名产物）；IO 失败 → io_error。
        try:
            session.fh.close()
            os.replace(session.part_path, session.final_path)
        except OSError as e:
            logger.warning("client:put_blob finalize rename failed (%s): %s", session.upload_id, e)
            self._drop_session(session)
            return _blob_write_error(
                reason="io_error", message="finalizing the upload failed", upload_id=session.upload_id
            )
        landing_path_str = str(session.final_path)
        self._sessions.pop(session.upload_id, None)
        # 围栏断言（防御纵深）：产物 resolve 后必须仍在 landing root 内（§7 不变量 #5）。
        self._assert_within_root(session.final_path)
        ret: PutBlobRet = {
            "upload_id": session.upload_id,
            "chunk_offset": chunk_offset,
            "landing_path": landing_path_str,
            "total_size": session.received,
            "sha256": recomputed,
            "req_id": req_id,
        }
        return ret

    # ── 有界会话（GC）/ Bounded sessions (GC) ─────────────────────────────

    def _expire_stale_sessions(self) -> None:
        """作废闲置超时会话（须持锁调用）/ Drop sessions idle beyond the timeout (caller holds lock).

        协议 MUST（§3 生命周期表）：超时后该 ``upload_id`` → ``4019 invalid_upload``。
        """
        now = time.monotonic()
        stale = [
            s for s in self._sessions.values()
            if now - s.last_active > self._thresholds.upload_idle_timeout_seconds
        ]
        for session in stale:
            logger.info("client:put_blob session expired (idle): %s", session.upload_id)
            self._drop_session(session)
        if stale:
            self._collect_orphan_parts()

    def _collect_orphan_parts(self) -> None:
        """孤儿 ``.part`` GC（须持锁）：删 ``.a2c-upload`` 内不属于任何活跃会话的**超龄**文件。

        Orphan ``.part`` GC (caller holds lock): remove files in the staging dir that belong
        to no live session **and are older than the idle timeout** (mtime 宽限). 协议 MUST（§3）
        + GC 严格限于 landing root（§7 不变量 #5）——只扫 ``_PART_DIR_NAME`` 目录成员，绝不越界。

        为何按龄宽限（表外 ≠ 立即孤儿）：同机多 Computer 进程共享同一 user-scope ``landingRoot``
        时，另一进程**在途**会话的 ``.part`` 也在本表之外——其 mtime 随每次写块刷新，永不满龄；
        崩溃遗留的孤儿则停止刷新、超龄后必被收。单进程语义不变（会话表本就覆盖全部活会话），
        GC 本就是最终一致。
        Why age-grace: an in-flight session of a sibling process sharing the landing root stays
        fresh (mtime bumped per chunk) and is spared; crash leftovers stop aging and get collected.
        """
        if self._landing_root is None:
            return
        part_dir = self._landing_root / _PART_DIR_NAME
        live = {s.part_path.name for s in self._sessions.values()}
        idle_timeout = self._thresholds.upload_idle_timeout_seconds
        now_wall = time.time()
        try:
            entries = list(part_dir.iterdir())
        except OSError:
            return  # 目录不存在（尚无上传）/ absent until the first upload.
        for entry in entries:
            if entry.name in live:
                continue
            try:
                if entry.is_dir():
                    continue  # 只收文件；目录非本 store 产物，勿递归（围栏纪律）。
                if now_wall - entry.stat().st_mtime <= idle_timeout:
                    continue  # 未超龄：可能是共享 root 的兄弟进程在途会话（见 docstring）。
                entry.unlink(missing_ok=True)
                logger.debug("client:put_blob orphan .part collected: %s", entry)
            except OSError:
                logger.debug("client:put_blob orphan .part unlink failed: %s", entry, exc_info=True)

    def _drop_session(self, session: _UploadSession) -> None:
        """作废单个会话：关句柄、删 ``.part``、移出表（须持锁）/ Drop one session (caller holds lock)."""
        self._sessions.pop(session.upload_id, None)
        session.close()

    def _assert_within_root(self, path: Path) -> None:
        """防御纵深围栏：``path`` resolve 后必须落在 landing root 内（违规说明安全名构造被破坏）。"""
        if self._landing_root is None:
            return
        root_resolved = self._landing_root.resolve()
        resolved = path.resolve()
        if root_resolved not in resolved.parents and resolved != root_resolved:
            # 隔离不变量显式 raise（assert 会被 -O 剥离，先例 SMCPNamespaceError）。
            raise RuntimeError(
                f"put_blob sandbox fence violated: {resolved} escaped landing root {root_resolved}"
            )
