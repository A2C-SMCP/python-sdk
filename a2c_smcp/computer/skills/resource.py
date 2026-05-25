# -*- coding: utf-8 -*-
# filename: resource.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 包内资源读取视图（v0.2.1）/ In-package SKILL resource read view (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §9（沙箱）、blob-transfer.md §3
                      （完整性）、§5（生产者通道）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §4.2 / §4.4 / §5.1。

存在意义 / Why this module：
    ``client:get_skill`` 铸造期（决定 inline body vs blob_handle）与 ``client:get_blob`` 解析期
    （kind=skill 重施沙箱后回源切片）**必须**对「Agent 最终消费的资源字节」给出**一致**的
    ``total_size`` / ``sha256``（设计 §4.4「资源字节三处一致」）。把「沙箱解析 + frontmatter 剥离
    + MIME + 流式 sha256 + 惰性切片」收敛到本模块单一入口 :func:`resolve_skill_view`，让两处共用，
    从根上杜绝实现漂移。
    The get_skill mint (inline-vs-handle decision) and the get_blob resolve (re-sandbox + re-serve)
    MUST agree on ``total_size`` / ``sha256`` for the bytes the Agent ultimately consumes. This
    module is the single source of that logic so the two paths cannot drift.

消费字节基准 / Consumed-bytes baseline（协议 blob-transfer.md §3 + 设计 §4.4）：
    - **SKILL.md（入口）**：frontmatter 剥离后 body 的 UTF-8 字节；
    - **其它包内资源**：原始字节；
    - 占位符 ``$TFROBOT_*`` **不**在此展开（Agent SDK prompt 渲染层职责，非协议层）。
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from a2c_smcp.computer.skills.sandbox import DEFAULT_SKILL_FILE, SkillSandboxError, resolve_skill_resource
from a2c_smcp.computer.skills.staging import strip_skill_frontmatter

# SKILL.md 固定 MIME（frontmatter 剥离后仍是 markdown 正文）/ Fixed MIME for the SKILL.md entry doc.
DEFAULT_SKILL_MIME = "text/markdown"
# 流式 sha256 / 惰性切片的读块大小 / Read-block size for streaming sha256 & lazy slicing.
_HASH_BLOCK = 1024 * 1024  # 1 MiB

# 可内联为 ``body`` 的文本类 MIME 允许集（非 text/* 的常见文本格式）/ Textual MIME allowlist.
_TEXTUAL_NON_TEXT_MIME = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
    },
)

# 惰性切片闭包 / Lazy slice closure: (offset, length) -> bytes
_Slicer = Callable[[int, int], bytes]


def looks_textual(mime: str) -> bool:
    """MIME 是否文本类（可内联为 ``body``）/ Whether a MIME is textual enough to inline as ``body``。

    ``text/*`` 或常见文本格式（json/xml/yaml/...）→ ``True``；其余（含 ``application/octet-stream``
    与各类二进制）→ ``False``（一律铸句柄）。最终能否内联仍需调用方叠加「≤ inline_budget」+「UTF-8
    可解码」判定。
    """
    return mime.startswith("text/") or mime in _TEXTUAL_NON_TEXT_MIME


@dataclass(frozen=True, eq=False)
class SkillResourceView:
    """沙箱解析后的 SKILL 资源描述符 / Sandboxed SKILL resource descriptor。

    ``total_size`` / ``sha256`` 均基于**消费字节**（SKILL.md→剥离后 body；其它→原始字节）。
    ``slice`` / ``read_all`` 同样返回消费字节，故 get_skill 内联与 get_blob 切片字节完全一致。

    Attributes:
        rel_path: 回显用相对路径（缺省请求回显 ``"SKILL.md"``）。
        abs_path: 沙箱校验后的真实绝对路径（``realpath``）。
        mime: 资源 MIME（SKILL.md 固定 ``text/markdown``；其它 ``mimetypes.guess_type``）。
        total_size: 消费字节数。
        sha256: 消费字节的 sha256 十六进制（流式计算，不全量驻留）。
        is_entry: 是否服务「frontmatter 剥离后的 SKILL.md 入口」。
        is_text: MIME 是否文本类（:func:`looks_textual`），供 inline 决策。
        _slicer: 惰性切片闭包；SKILL.md 走内存剥离 body，其它走 disk seek+read。
    """

    rel_path: str
    abs_path: Path
    mime: str
    total_size: int
    sha256: str
    is_entry: bool
    is_text: bool
    _slicer: _Slicer = field(repr=False)

    def slice(self, offset: int, length: int) -> bytes:
        """读取消费字节 ``[offset, offset+length)``，截断到 ``total_size``（语义同 ResolvedBlob.slice）。"""
        if offset < 0 or length < 0:
            raise ValueError(f"offset/length must be non-negative: offset={offset}, length={length}")
        if offset > self.total_size:
            raise ValueError(f"offset {offset} > total_size {self.total_size}")
        if length == 0 or offset == self.total_size:
            return b""
        actual = min(length, self.total_size - offset)
        return self._slicer(offset, actual)

    def read_all(self) -> bytes:
        """读取全部消费字节（供 get_skill 内联 ``body``；调用方应已确认 ≤ inline_budget）。"""
        return self.slice(0, self.total_size)


def _stream_sha256(path: Path) -> str:
    """流式计算文件 sha256（O(_HASH_BLOCK) 内存）/ Streaming file sha256 (bounded memory)。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(_HASH_BLOCK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _disk_slicer(path: Path) -> _Slicer:
    """构造从磁盘 seek+read 的惰性切片闭包（每次调用独立打开，幂等可并发）。"""

    def _slice(offset: int, length: int) -> bytes:
        with path.open("rb") as f:
            f.seek(offset)
            return f.read(length)

    return _slice


def _mem_slicer(data: bytes) -> _Slicer:
    """构造从内存字节切片的闭包（SKILL.md 剥离后 body）。"""

    def _slice(offset: int, length: int) -> bytes:
        return data[offset : offset + length]

    return _slice


def resolve_skill_view(
    root: Path,
    rel_path: str | None,
    *,
    too_large_cap: int | None = None,
) -> SkillResourceView:
    """沙箱解析 SKILL 包内资源 → :class:`SkillResourceView`（mint / serve 共用唯一入口）。

    流程 / Flow：
      1. :func:`resolve_skill_resource` 包根内沙箱（``..`` / 绝对路径 / symlink 逃逸 → traversal；
         ``.skillenv`` 等 → forbidden；缺失/目录 → not_found）——失败抛 :class:`SkillSandboxError`；
      2. **DoS 守卫**（仅当传 ``too_large_cap``，即 get_skill 铸造期）：先 ``stat`` 原始字节，
         超上限 → 抛 ``SkillSandboxError("too_large", total_size=raw_size)``，**不读文件**（避免 OOM）；
         get_blob 解析期不传 cap（协议 4018 无 too_large 语义，分块传输自然受限）；
      3. 消费字节基准：``SKILL.md`` 入口读全文 → :func:`strip_skill_frontmatter` → UTF-8 body 字节
         （内存切片）；其它资源 → 原始磁盘字节（惰性 seek+read 切片）；
      4. ``total_size`` / ``sha256`` 基于消费字节（SKILL.md 即剥离后 body；其它流式哈希原始文件）。

    §9.2 name 寻址防越权不变量 / anti-escalation invariant：本函数**只**接受 ``root: Path``（由
    Registry 经 name 解析得到的包根），**绝不**接受 name、**绝不**从 name/rel_path 推导 FS 路径。

    Args:
        root: SKILL 包根绝对目录（来自 ``A2CSkillRef.path``，经 Registry 解析）。
        rel_path: 包根内 POSIX 相对路径；``None`` / 空 → 入口 ``SKILL.md``。
        too_large_cap: 铸造期绝对字节上限；``None`` 表示不做 too_large 守卫（get_blob 解析期）。

    Returns:
        SkillResourceView: 消费字节视图。

    Raises:
        SkillSandboxError: 沙箱拒绝（traversal/forbidden/not_found）或 too_large（仅 mint 期）。
    """
    target = resolve_skill_resource(root, rel_path)
    rel_echo = DEFAULT_SKILL_FILE if not rel_path else rel_path
    is_entry = target.name == DEFAULT_SKILL_FILE

    if is_entry:
        # SKILL.md 入口：先 stat 守卫（避免读超大文件），再读全文剥 frontmatter。
        if too_large_cap is not None:
            raw_size = target.stat().st_size
            if raw_size > too_large_cap:
                raise SkillSandboxError("too_large", rel_path=rel_echo, total_size=raw_size)
        raw_text = target.read_text(encoding="utf-8", errors="replace")
        body_bytes = strip_skill_frontmatter(raw_text).encode("utf-8")
        total_size = len(body_bytes)
        # 守卫也覆盖剥离后 body（罕见：frontmatter 极小而 body 极大且原始未超）。
        if too_large_cap is not None and total_size > too_large_cap:
            raise SkillSandboxError("too_large", rel_path=rel_echo, total_size=total_size)
        return SkillResourceView(
            rel_path=rel_echo,
            abs_path=target,
            mime=DEFAULT_SKILL_MIME,
            total_size=total_size,
            sha256=hashlib.sha256(body_bytes).hexdigest(),
            is_entry=True,
            is_text=True,
            _slicer=_mem_slicer(body_bytes),
        )

    # 其它包内资源：原始字节 + 惰性 disk 切片 + 流式 sha256。
    raw_size = target.stat().st_size
    if too_large_cap is not None and raw_size > too_large_cap:
        raise SkillSandboxError("too_large", rel_path=rel_echo, total_size=raw_size)
    guessed, _ = mimetypes.guess_type(target.name)
    mime = guessed or "application/octet-stream"
    return SkillResourceView(
        rel_path=rel_echo,
        abs_path=target,
        mime=mime,
        total_size=raw_size,
        sha256=_stream_sha256(target),
        is_entry=False,
        is_text=looks_textual(mime),
        _slicer=_disk_slicer(target),
    )
