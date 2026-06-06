# -*- coding: utf-8 -*-
# filename: toolspool.py
# @Author  : JQQ
# @Software: PyCharm
"""
``tool_call`` 二进制旁路的内容寻址暂存 / Content-addressed staging for ``tool_call`` binary sideband.

布局 / Layout::

    <cache_root>/.blobspool/<cid>      # 字节内容（裸 bytes，无 envelope）
    <cache_root>/.blobspool/<cid>.mime # MIME 元数据（UTF-8，单行）

cid = ``sha256`` 十六进制 —— 内容寻址保证：
  - 相同字节复用同一 cid，幂等写入；
  - 句柄**跨 Computer 重启**仍可解析（cid 在磁盘则可读、不在则 ``4018 gone``）；
  - 不签名 / 不 MAC（协议 §1/§5.4 把安全边界定在 resolver 重施鉴权 / 沙箱）。

设计依据 / Design source: ``docs/design-0.2.1-skill-computer-management.md`` §4.3
``kind=toolspool`` 章节 + 「跨 Computer 重启可解析」承诺。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from a2c_smcp.computer.blob.handle import BlobHandleGoneError, BlobHandleInvalidError
from a2c_smcp.utils.atomic_io import atomic_write_bytes, atomic_write_text
from a2c_smcp.utils.path import is_within

logger = logging.getLogger(__name__)

# 暂存根子目录名（``.<prefix>`` 与 SKILL Home 内 source 子目录命名风格分开）
# Staging subdir name (with leading dot, distinct from source-named subdirs in SKILL Home)
BLOBSPOOL_DIRNAME: str = ".blobspool"

# cid 形态校验：64 位十六进制（sha256），避免伪造句柄借由 cid 路径越界
# cid validation: 64 hex chars (sha256); blocks path-traversal via cid spoofing
_CID_LEN: int = 64
_HEX_CHARS: frozenset[str] = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class BlobStat:
    """``.blobspool`` 条目元数据（不读字节）/ Entry metadata, never reads payload bytes.

    v0.2.1 #51 lazy slice 接入点：``ToolspoolBlobResolver.resolve()`` 走 ``stat()`` + closure
    ``read_chunk()``，避免一次性 ``read_bytes()`` 全量入内存。
    v0.2.1 #51 lazy-slice entry: resolver uses ``stat() + read_chunk()`` to avoid
    eager full-file reads.

    Attributes:
        size: 字节数（``os.stat().st_size``）。Size in bytes.
        mime: ``.mime`` sidecar 内容（UTF-8，已 strip）。MIME from sidecar (stripped).
    """

    size: int
    mime: str


class ToolspoolBlobStore:
    """内容寻址 ``.blobspool`` 暂存（线程/协程安全：文件原子重命名 + 内容寻址幂等）.

    Content-addressed ``.blobspool`` store; concurrency-safe via atomic rename + idempotency.

    Attributes:
        root: 暂存根目录绝对路径（``<cache_root>/.blobspool``，启动时确保创建）。
    """

    def __init__(self, cache_root: Path) -> None:
        """初始化暂存 / Initialize the store.

        Args:
            cache_root: 上层缓存根（如 ``~/.a2c``）；本暂存挂在其下 ``.blobspool/`` 子目录。
                v0.2.1 milestone 内 #39 接管 SKILL Home 后会替换为 SKILL Home 同级。
                Will be replaced by SKILL Home sibling once #39 lands the home resolver.
        """
        self.root: Path = (cache_root / BLOBSPOOL_DIRNAME).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, payload: bytes, mime: str) -> str:
        """写入字节内容；返回 ``cid``（``sha256`` hex）/ Stage bytes, return ``cid``.

        幂等：内容已存在则直接复用（不重写），避免相同字节多副本。
        Idempotent: existing content is reused (no rewrite) — no duplicates for same bytes.

        Args:
            payload: 原始字节内容（已**解码后**字节，非 base64 编码）。
                Raw payload bytes (decoded, not base64-encoded).
            mime: 内容 MIME，用于 ``get_blob`` 回显。

        Returns:
            cid: 内容寻址 id（``sha256`` 十六进制）。

        Raises:
            ValueError: ``mime`` 为空 / 非 ASCII（防误传非文本 MIME 走入暂存元数据）。
        """
        if not mime or not mime.isascii():
            raise ValueError(f"mime must be a non-empty ASCII string, got {mime!r}")
        cid = hashlib.sha256(payload).hexdigest()
        blob_path = self._blob_path(cid)
        mime_path = self._mime_path(cid)
        if blob_path.exists():
            # 幂等：内容存在则直接复用 / Idempotent: reuse existing content
            # 仍刷新 mime 以兜底首次写盘失败的极端场景 / refresh mime to recover stale state
            atomic_write_text(mime_path, mime)
            return cid
        atomic_write_bytes(blob_path, payload)
        atomic_write_text(mime_path, mime)
        return cid

    def get(self, cid: str) -> tuple[bytes, str]:
        """按 ``cid`` 回查（一次性全量读）/ Look up by ``cid`` (eager full read).

        保留为 round-trip 便利接口；新代码 SHOULD 用 :meth:`stat` + :meth:`read_chunk` 惰性读，
        避免大 blob 的内存放大。``ToolspoolBlobResolver`` 自 v0.2.1 #51 起已迁移到惰性接口。
        Kept as eager convenience; new code SHOULD use lazy reads via stat + read_chunk.
        Resolver migrated to lazy interface in v0.2.1 #51.

        Returns:
            (payload, mime): 字节内容与 MIME。

        Raises:
            BlobHandleInvalidError: ``cid`` 格式非法（非 64 位 hex）—— 防伪造句柄借
                路径片段越界（``..`` / 绝对路径片段）.
                Invalid cid format — defends against forged handles using traversal segments.
            BlobHandleGoneError: cid 格式合法但文件已不在（被 GC / 从未铸造 / 已淘汰）。
                Valid cid but file absent (GC'd / never minted / evicted).
        """
        _validate_cid(cid)
        blob_path = self._blob_path(cid)
        mime_path = self._mime_path(cid)
        # 双 realpath 校验：解析后**仍**在 root 内（防 symlink 逃逸 / 异常路径片段）
        # Double realpath check: resolved path still under root (anti symlink escape).
        try:
            blob_real = blob_path.resolve(strict=True)
            mime_real = mime_path.resolve(strict=True)
        except FileNotFoundError as e:
            raise BlobHandleGoneError(f"toolspool entry missing: cid={cid}") from e
        if not is_within(blob_real, self.root) or not is_within(mime_real, self.root):
            # 理论不可达——cid 已经过白名单校验。出现即视为 invalid_handle（不泄漏内部细节）
            # Theoretically unreachable post-cid-validation; treat as invalid_handle (no leak)
            raise BlobHandleInvalidError(f"resolved path escapes blobspool root: cid={cid}")
        payload = blob_real.read_bytes()
        mime = mime_real.read_text(encoding="utf-8").strip()
        return payload, mime

    def stat(self, cid: str) -> BlobStat:
        """读取条目元数据：尺寸 + MIME；**不读 payload 字节**.

        Read entry metadata: size + MIME; never reads payload bytes.

        v0.2.1 #51: ``ToolspoolBlobResolver.resolve()`` 入口，仅 metadata + path realpath 校验.

        Raises:
            BlobHandleInvalidError: ``cid`` 格式非法 / Invalid cid format.
            BlobHandleGoneError: ``cid`` 格式合法但文件已不在 / Valid cid, file absent.
        """
        _validate_cid(cid)
        blob_path = self._blob_path(cid)
        mime_path = self._mime_path(cid)
        try:
            blob_real = blob_path.resolve(strict=True)
            mime_real = mime_path.resolve(strict=True)
        except FileNotFoundError as e:
            raise BlobHandleGoneError(f"toolspool entry missing: cid={cid}") from e
        if not is_within(blob_real, self.root) or not is_within(mime_real, self.root):
            raise BlobHandleInvalidError(f"resolved path escapes blobspool root: cid={cid}")
        size = blob_real.stat().st_size  # metadata only — no payload read
        mime = mime_real.read_text(encoding="utf-8").strip()
        return BlobStat(size=size, mime=mime)

    def read_chunk(self, cid: str, offset: int, length: int) -> bytes:
        """从 ``cid`` 字节流 ``[offset, offset+length)`` 区间读取；越界自然返回短读 / 空.

        Read ``[offset, offset+length)`` slice; over-EOF returns short / empty bytes.

        v0.2.1 #51: 惰性切片底层原语，每次 ``open()`` + ``seek()`` + ``read()`` 独立 fd；
        OS file-cache 让重复读廉价；并发 chunk 自然独立。
        Lazy slice primitive: each call opens an independent fd; OS file-cache makes
        repeated reads cheap; concurrent chunks naturally isolated.

        边界语义 / Boundary semantics:
          - ``length == 0`` → ``b""`` (no I/O at OS level)
          - ``offset >= size`` → ``b""`` (mirrors Python file.read past EOF)
          - ``length`` 超出剩余 → 自然短读（OS 截断）/ Natural short-read on over-length

        Raises:
            BlobHandleInvalidError: ``cid`` 格式非法 / Invalid cid format.
            BlobHandleGoneError: ``cid`` 格式合法但文件已不在 / Valid cid, file absent.
            ValueError: ``offset < 0`` 或 ``length < 0`` / Negative offset or length.
        """
        if offset < 0 or length < 0:
            raise ValueError(f"offset/length must be non-negative: offset={offset}, length={length}")
        if length == 0:
            return b""
        _validate_cid(cid)
        blob_path = self._blob_path(cid)
        try:
            blob_real = blob_path.resolve(strict=True)
        except FileNotFoundError as e:
            raise BlobHandleGoneError(f"toolspool entry missing: cid={cid}") from e
        if not is_within(blob_real, self.root):
            raise BlobHandleInvalidError(f"resolved path escapes blobspool root: cid={cid}")
        with open(blob_real, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def exists(self, cid: str) -> bool:
        """轻量存在性探测，不读字节 / Lightweight existence probe; never reads bytes.

        ``cid`` 非法直接返回 ``False``（不抛），便于上层"先看再读"语义。
        Invalid cid returns ``False`` silently for "probe before read" semantics.
        """
        try:
            _validate_cid(cid)
        except BlobHandleInvalidError:
            return False
        return self._blob_path(cid).is_file()

    def iter_cids(self) -> Iterator[str]:
        """枚举当前所有 cid（仅诊断 / GC 用，不保证排序）/ Enumerate all cids (diagnostics only).

        过滤掉 ``.mime`` 旁路文件与任何不符合 cid 形态的条目（防御读盘脏数据）。
        Skips ``.mime`` sidecars and any entry not matching cid form (defensive).
        """
        if not self.root.is_dir():
            return
        for entry in self.root.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if name.endswith(".mime"):
                continue
            if len(name) == _CID_LEN and all(ch in _HEX_CHARS for ch in name):
                yield name

    # ── 内部 / Internal ────────────────────────────────────────────────

    def _blob_path(self, cid: str) -> Path:
        return self.root / cid

    def _mime_path(self, cid: str) -> Path:
        return self.root / f"{cid}.mime"


def _validate_cid(cid: str) -> None:
    """cid 白名单校验：长度 = 64、字符集 ⊆ ``[0-9a-f]``。
    Whitelist cid: length 64, charset hex-lower. Anything else → invalid_handle.
    """
    if not isinstance(cid, str) or len(cid) != _CID_LEN or not all(ch in _HEX_CHARS for ch in cid):
        raise BlobHandleInvalidError(f"invalid cid form (must be sha256 hex): {cid!r}")
