# -*- coding: utf-8 -*-
# filename: path_resolver.py
# @Author  : JQQ
# @Software: PyCharm
"""
按路径读取的生产者通道：``readRoots`` 围栏 + 九步 resolve 管线 + path 句柄复核.

Protocol: a2c-smcp-protocol ``blob-transfer.md`` §生产者三：按路径读取（protocol#65）。
本模块只负责 path core（词法围栏 / 非严格 realpath / 固定拒绝集 / 打开与 stat / 句柄铸造 /
拉取期复核）；事件接线（``client:resolve_path`` / ``get_blob`` 派发）由 config+wire 流完成。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from a2c_smcp.utils.path_norm import PathPlatform

# 铸造期失败原因（协议 4020 ``details.reason``，开放枚举）/ Mint-time failure reasons.
ResolveReason = Literal["invalid_path", "forbidden", "not_found", "not_a_file"]

# 惰性切片闭包类型 / Lazy slice closure type: (offset, length) -> bytes
_BlobSlicer = Callable[[int, int], bytes]


class PathResolveError(Exception):
    """``client:resolve_path`` 铸造期失败 → 协议 ``4020`` / Mint-time ``4020`` failure.

    Attributes:
        reason: ``invalid_path`` / ``forbidden`` / ``not_found`` / ``not_a_file``。
    """

    def __init__(self, reason: ResolveReason, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason: ResolveReason = reason


@dataclass(frozen=True)
class ReadRoot:
    """一个授权读取根的双形态 / One authorized read root in two forms.

    Attributes:
        configured: ``normalize_path(配置值)``（配置形态；词法围栏命中即可）。
        real: ``normalize_path(realpath(配置值))``（realpath 形态；realpath 围栏唯一依据）；
            realpath / 归一失败时为 ``None``，由调用方记配置诊断。
    """

    configured: str
    real: str | None


@dataclass(frozen=True)
class ProtectedPaths:
    """固定拒绝集（即便在 ``readRoots`` 内也拒绝）/ Fixed rejection set.

    Attributes:
        paths: 已归一化的 absolute 路径；按**路径分量前缀**判定（``p == e`` 或
            ``p.startswith(e + "/")``），因此文件与目录都可传入。
        landing_root: 上行写入根；其下末级分量以 ``.part`` 结尾的文件拒绝。
        file_ids: ``(st_dev, st_ino)`` 集合，用于硬链接 SHOULD 防御。
    """

    paths: tuple[str, ...] = ()
    landing_root: str | None = None
    file_ids: frozenset[tuple[int, int]] = frozenset()


@dataclass(frozen=True, eq=False)
class ResolvedPath:
    """``client:resolve_path`` 成功结果 / Successful resolve result.

    Attributes:
        canonical_path: 规范路径（realpath 后经 ``normalize_path``，可直接回用作 ``path``）。
        blob_handle: 现铸的不透明 path 句柄（转 ``client:get_blob`` 拉字节）。
        mime_type: 确定性 MIME（复用 ``utils/mime.py``）。
        total_size: 文件字节数。
        sha256: 全量 sha256 十六进制（小写）。
        mtime: 最后修改时间，Unix epoch **毫秒**（向负无穷取整）。
    """

    canonical_path: str
    blob_handle: str
    mime_type: str
    total_size: int
    sha256: str
    mtime: int


@dataclass(frozen=True, eq=False)
class PathPullResult:
    """``client:get_blob`` 拉取期复核结果 / Pull-time revalidation result.

    Attributes:
        canonical_path: 句柄内规范路径（复核通过）。
        mime_type: 句柄内 MIME。
        total_size: 句柄内总字节数（与打开句柄 stat 键相符）。
        sha256: 句柄内摘要（拉取期**不**重算）。
        _slicer: 从**同一打开句柄**惰性读取的闭包；调用方据此切片。
        _closer: 释放打开句柄；``close()`` 与 GC 兜底调用。
    """

    canonical_path: str
    mime_type: str
    total_size: int
    sha256: str
    _slicer: _BlobSlicer = field(repr=False)
    _closer: Callable[[], None] | None = field(default=None, repr=False)

    def slice(self, offset: int, length: int) -> bytes:
        """读取 ``[offset, offset+length)``；边界语义与 ``ResolvedBlob.slice`` 一致."""
        raise NotImplementedError("path core skeleton — implemented in the next commit")

    def close(self) -> None:
        """释放打开句柄（幂等）/ Release the opened handle (idempotent)."""
        raise NotImplementedError("path core skeleton — implemented in the next commit")


def build_read_roots(raw: Sequence[str]) -> list[ReadRoot]:
    """把 settings ``readRoots`` 原始列表编译为双形态根列表.

    每项 MUST 为绝对路径且可 ``normalize_path``；非法项丢弃（配置诊断由接线层记录）。
    ``realpath`` / 归一失败的根只保留配置形态，``real`` 为 ``None``。
    """
    raise NotImplementedError("path core skeleton — implemented in the next commit")


def build_protected_paths(
    *,
    extra_paths: Sequence[str] = (),
    landing_root: str | None = None,
    env: Mapping[str, str] | None = None,
    platform: PathPlatform | None = None,
) -> ProtectedPaths:
    """构造固定拒绝集：默认含 SDK 配置 / 状态目录，并可追加接线层已知路径.

    默认集覆盖 ``$XDG_CONFIG_HOME/a2c``（或 ``~/.config/a2c``）与
    ``$XDG_STATE_HOME/a2c``（或 ``~/.local/state/a2c``）——settings / ``computer-id`` /
    input 值缓存均在这些目录下。``extra_paths`` 由 config+wire 流注入（flag settings 等）。
    """
    raise NotImplementedError("path core skeleton — implemented in the next commit")


def resolve_path(
    path: str,
    roots: Sequence[ReadRoot],
    *,
    protected: ProtectedPaths | None = None,
    platform: PathPlatform | None = None,
) -> ResolvedPath:
    """执行协议九步 resolve 管线并铸造 path 句柄.

    顺序 MUST：``normalize_path``（``..`` 词法先消解）→ ``readRoots`` 空 / 词法围栏 →
    非严格 realpath 围栏 → 固定拒绝集 → ``lstat`` 类型预检 → 非阻塞打开 + ``fstat`` →
    同一打开句柄 stat 键 + sha256 → 铸造句柄。前序失败即返回，越界先于存在性探测。

    Args:
        path: Agent 给出的绝对路径（原生或 canonical 形态）。
        roots: :func:`build_read_roots` 的结果。
        protected: 固定拒绝集；``None`` 时使用默认集（不含接线层额外路径）。
        platform: 缺省取宿主平台。

    Raises:
        PathResolveError: ``invalid_path`` / ``forbidden`` / ``not_found`` / ``not_a_file``。
    """
    raise NotImplementedError("path core skeleton — implemented in the next commit")


def resolve_for_pull(
    handle_payload: Mapping[str, Any],
    roots: Sequence[ReadRoot],
    *,
    protected: ProtectedPaths | None = None,
    platform: PathPlatform | None = None,
) -> PathPullResult:
    """``client:get_blob`` 解析 path 句柄时按**当前** ``readRoots`` 重跑第 3–8 步.

    拉取期失败映射：越界 / 拒绝集 / 其余打开错误 → ``BlobHandleForbiddenError``；
    不存在 / 非普通文件 → ``BlobHandleGoneError``；stat 键不符（已改写）→ ``BlobHandleGoneError``；
    句柄结构非法 → ``BlobHandleInvalidError``。相符则回填句柄内 ``sha256`` / ``total_size``，
    并返回从同一打开句柄切片的能力。
    """
    raise NotImplementedError("path core skeleton — implemented in the next commit")
