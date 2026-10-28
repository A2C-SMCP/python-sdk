# -*- coding: utf-8 -*-
# filename: path_resolver.py
# @Author  : JQQ
# @Software: PyCharm
"""
按路径读取的生产者通道：``readRoots`` 围栏 + 九步 resolve 管线 + path 句柄复核.

Protocol: a2c-smcp-protocol ``blob-transfer.md`` §生产者三：按路径读取（protocol#65）。
本模块只负责 path core（词法围栏 / 非严格 realpath / 固定拒绝集 / 打开与 stat / 句柄铸造 /
拉取期复核）；事件接线（``client:resolve_path`` / ``get_blob`` 派发）由 config+wire 流完成。

安全不变量 / Security invariants:
  - 越界判定先于任何存在性探测（词法围栏不访问文件系统）；
  - 非严格 realpath 用 ``lstat`` / ``readlink`` 语义（``os.path.realpath(strict=False)``），
    **不**用跟随链接的 ``stat`` / ``exists()`` 判定存在性；
  - 打开前 ``lstat`` 类型预检，打开非阻塞，避免 FIFO 阻塞 / 设备副作用；
  - 同一打开句柄取 stat 键 + sha256；拉取期从同一句柄切片；
  - 固定拒绝集即便在 ``readRoots`` 内也拒绝；硬链接以打开句柄文件身份兜底。
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from a2c_smcp.computer.blob.handle import (
    BlobHandleForbiddenError,
    BlobHandleGoneError,
    BlobHandleInvalidError,
    encode_path_handle,
)
from a2c_smcp.computer.inputs.value_store import resolve_value_store_path
from a2c_smcp.computer.settings.scope import user_settings_path
from a2c_smcp.utils.mime import guess_mime
from a2c_smcp.utils.path_norm import (
    InvalidPathError,
    PathPlatform,
    native_platform,
    normalize_path,
    realpath_nonstrict,
    to_native_path,
)

logger = logging.getLogger(__name__)

# 铸造期失败原因（协议 4020 ``details.reason``，开放枚举）/ Mint-time failure reasons.
ResolveReason = Literal["invalid_path", "forbidden", "not_found", "not_a_file"]

# 惰性切片闭包类型 / Lazy slice closure type: (offset, length) -> bytes
_BlobSlicer = Callable[[int, int], bytes]

# sha256 读取块大小 / Hash read chunk size.
_HASH_CHUNK_BYTES: int = 1024 * 1024

# Computer 身份文件名（#235 落点；本模块只做拒绝集，不依赖其实现）/ computer-id filename.
_COMPUTER_ID_FILENAME = "computer-id"

# ``os.pread`` 仅 POSIX 提供；Windows 回退到 lseek + read.
_OS_PREAD: Callable[[int, int, int], bytes] | None = getattr(os, "pread", None)


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
        """读取 ``[offset, offset+length)``；边界语义与 ``ResolvedBlob.slice`` 一致.

        ``offset == total_size`` → ``b""``（EOF probe）；``offset > total_size`` / 负数 →
        ``ValueError``；``length`` 超出剩余自动截断。
        """
        if offset < 0 or length < 0:
            raise ValueError(f"offset/length must be non-negative: offset={offset}, length={length}")
        if offset > self.total_size:
            raise ValueError(f"offset {offset} > total_size {self.total_size}")
        if length == 0 or offset == self.total_size:
            return b""
        actual = min(length, self.total_size - offset)
        return self._slicer(offset, actual)

    def close(self) -> None:
        """释放打开句柄（幂等，重复调用不抛）/ Release the opened handle (idempotent)."""
        if self._closer is not None:
            self._closer()


@dataclass(frozen=True)
class _StatKey:
    """打开句柄的 stat 键（句柄内编码 + 拉取期比对）/ Stat key encoded in the handle."""

    total_size: int
    mtime_ms: int
    dev: int
    ino: int


def build_read_roots(raw: Sequence[str]) -> list[ReadRoot]:
    """把 settings ``readRoots`` 原始列表编译为双形态根列表.

    每项 MUST 为绝对路径且可 ``normalize_path``；非法项丢弃（配置诊断由接线层记录）。
    ``realpath`` / 归一失败的根只保留配置形态，``real`` 为 ``None``。
    """
    platform = native_platform()
    roots: list[ReadRoot] = []
    for item in raw or ():
        if not isinstance(item, str) or not item:
            logger.warning("readRoots: 丢弃非字符串或空路径项 / dropping non-str or empty item: %r", item)
            continue
        try:
            configured = normalize_path(item, platform)
        except InvalidPathError as e:
            logger.warning("readRoots: 丢弃非法路径 / dropping invalid root %r: %s", item, e)
            continue
        real: str | None
        try:
            real = normalize_path(realpath_nonstrict(configured, platform), platform)
        except (OSError, ValueError, InvalidPathError) as e:
            logger.warning("readRoots: realpath 失败，仅保留配置形态 / realpath failed for %r: %s", configured, e)
            real = None
        roots.append(ReadRoot(configured=configured, real=real))
    return roots


def build_protected_paths(
    *,
    extra_paths: Sequence[str | os.PathLike[str]] = (),
    landing_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    platform: PathPlatform | None = None,
) -> ProtectedPaths:
    """构造固定拒绝集：默认含 SDK 配置 / 状态目录，并可追加接线层已知路径.

    默认集覆盖 ``$XDG_CONFIG_HOME/a2c``（或 ``~/.config/a2c``）与
    ``$XDG_STATE_HOME/a2c``（或 ``~/.local/state/a2c``）——settings / ``computer-id`` /
    input 值缓存均在这些目录下。``extra_paths`` 由 config+wire 流注入（flag settings 等）。

    Note:
        ``resolve_path`` / ``resolve_for_pull`` 的 ``protected=None`` 只是防御性兜底
        （每次请求重建）；接线层 SHOULD 预构建一次并复用，热路径不依赖本默认集。
    """
    pf: PathPlatform = native_platform() if platform is None else platform
    environ: Mapping[str, str] = os.environ if env is None else env

    # 单一权威：config / state 目录解析复用 settings / inputs 的既有函数，避免复制 fallback 漂移。
    # Single authority: reuse the settings / inputs resolvers instead of duplicating fallbacks.
    candidates: list[str] = []
    try:
        settings_file = user_settings_path(environ)
        config_dir = settings_file.parent
        candidates.extend(
            [
                str(config_dir),
                str(settings_file),
                str(config_dir / _COMPUTER_ID_FILENAME),
            ],
        )
    except (OSError, ValueError) as e:  # pragma: no cover - 防御性：env 异常形态
        logger.warning("拒绝集: 无法解析 user config dir / cannot resolve user config dir: %s", e)
    try:
        # 整目录入拒绝集（settings / computer-id / 凭据等均在其下），不只保护 input-values.json。
        # Protect the whole state dir, not just input-values.json.
        state_dir = resolve_value_store_path(environ).parent
        candidates.append(str(state_dir))
    except (OSError, ValueError) as e:  # pragma: no cover - 防御性
        logger.warning("拒绝集: 无法解析 state dir / cannot resolve state dir: %s", e)

    candidates.extend(str(p) for p in extra_paths)

    protected: set[str] = set()
    file_ids: set[tuple[int, int]] = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            configured = normalize_path(candidate, pf)
        except (InvalidPathError, ValueError) as e:
            logger.warning("拒绝集: 丢弃非法路径 / dropping invalid protected path %r: %s", candidate, e)
            continue
        protected.add(configured)
        real: str | None
        try:
            real = normalize_path(realpath_nonstrict(configured, pf), pf)
        except (OSError, ValueError, InvalidPathError):
            real = None
        if real is not None:
            protected.add(real)
        # 硬链接 SHOULD：记录实际文件的 (dev, ino)。目录不展开（前缀判定已覆盖）。
        try:
            st = os.stat(to_native_path(real or configured, pf))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            file_ids.add((st.st_dev, st.st_ino))

    normalized_landing: str | None = None
    if landing_root:
        try:
            configured_landing = normalize_path(str(landing_root), pf)
            try:
                normalized_landing = normalize_path(realpath_nonstrict(configured_landing, pf), pf)
            except (OSError, ValueError, InvalidPathError):
                normalized_landing = configured_landing
        except (InvalidPathError, ValueError) as e:
            logger.warning("拒绝集: 丢弃非法 landingRoot / dropping invalid landingRoot %r: %s", landing_root, e)

    return ProtectedPaths(paths=tuple(sorted(protected)), landing_root=normalized_landing, file_ids=frozenset(file_ids))


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
    pf: PathPlatform = native_platform() if platform is None else platform
    protected_set = build_protected_paths(platform=pf) if protected is None else protected

    try:
        n = normalize_path(path, pf)
    except (InvalidPathError, ValueError) as e:
        raise PathResolveError("invalid_path", str(e)) from e

    canonical, fd, _ = _run_pipeline(n, roots, protected_set, pf)
    try:
        # 9) 同一打开句柄取 stat 键 + sha256；计算后 SHOULD 再 fstat，变化则重算一次。
        key_before = _stat_key(os.fstat(fd))
        digest = _hash_fd(fd)
        key_after = _stat_key(os.fstat(fd))
        if key_after != key_before:
            logger.warning("resolve_path: 文件在计算 sha256 期间被改写，重算一次 / file changed while hashing, recomputing once")
            key_before = key_after
            digest = _hash_fd(fd)
    except OSError as e:
        raise PathResolveError("forbidden", f"stat/read failed while hashing: {e}") from e
    finally:
        _close_quietly(fd)

    mime = guess_mime(canonical)
    handle = encode_path_handle(
        canonical,
        total_size=key_before.total_size,
        mtime=key_before.mtime_ms,
        sha256=digest,
        mime=mime,
        dev=key_before.dev,
        ino=key_before.ino,
    )
    return ResolvedPath(
        canonical_path=canonical,
        blob_handle=handle,
        mime_type=mime,
        total_size=key_before.total_size,
        sha256=digest,
        mtime=key_before.mtime_ms,
    )


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
    pf: PathPlatform = native_platform() if platform is None else platform
    canonical_path, total_size, mtime, sha256, mime, dev, ino = _validate_path_payload(handle_payload, pf)
    protected_set = build_protected_paths(platform=pf) if protected is None else protected

    try:
        _, fd, st = _run_pipeline(canonical_path, roots, protected_set, pf)
    except PathResolveError as e:
        _raise_pull_error(e)
        raise  # pragma: no cover — _raise_pull_error 必抛

    key = _stat_key(st)
    if key.total_size != total_size or key.mtime_ms != mtime:
        _close_quietly(fd)
        raise BlobHandleGoneError(
            f"path handle stat key mismatch: size {key.total_size}/{total_size}, mtime {key.mtime_ms}/{mtime}",
        )
    if dev is not None and (key.dev != dev or key.ino != ino):
        _close_quietly(fd)
        raise BlobHandleGoneError(f"path handle file identity mismatch: ({key.dev},{key.ino}) vs ({dev},{ino})")

    # 同一打开句柄切片；close 幂等 + GC 兜底。canonical 与句柄内路径在正常情况下一致。
    closed = False

    def _close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - 防御性
            logger.debug("path pull close failed", exc_info=True)

    def _slice(offset: int, length: int) -> bytes:
        if closed:
            raise ValueError("path handle is closed")
        return _read_fd(fd, offset, length)

    result = PathPullResult(
        canonical_path=canonical_path,
        mime_type=mime,
        total_size=total_size,
        sha256=sha256,
        _slicer=_slice,
        _closer=_close,
    )
    # GC 兜底：调用方忘记 close 时也不会泄漏 fd。
    weakref.finalize(result, _close)
    return result


# ── 管线内部 / Pipeline internals ────────────────────────────────────────


def _run_pipeline(
    n: str,
    roots: Sequence[ReadRoot],
    protected: ProtectedPaths,
    platform: PathPlatform,
) -> tuple[str, int, os.stat_result]:
    """执行第 2–8 步；成功返回 ``(canonical_path, fd, fstat)``，失败抛 ``PathResolveError``.

    调用方负责关闭返回的 fd。**越界判定先于任何存在性探测**。
    """
    # 2) readRoots 空 → forbidden
    if not roots:
        raise PathResolveError("forbidden", "readRoots is empty")

    # 3) 词法围栏（不访问文件系统）
    if not _within_any(n, roots):
        raise PathResolveError("forbidden", f"path is outside readRoots: {n!r}")

    # 4) 非严格 realpath（含悬空链接）；结果必须可归一 / 可 UTF-8 表示
    try:
        canonical = normalize_path(realpath_nonstrict(n, platform), platform)
        canonical.encode("utf-8")
    except (OSError, ValueError, InvalidPathError, UnicodeError) as e:
        raise PathResolveError("forbidden", f"realpath failed: {e}") from e

    # 5) realpath 围栏（只认 real 形态）；本步之前的任何 FS 错误都归 forbidden
    if not _within_any_real(canonical, roots):
        raise PathResolveError("forbidden", f"canonical path is outside readRoots: {canonical!r}")

    # 6) 固定拒绝集
    if _is_protected(canonical, protected, platform):
        raise PathResolveError("forbidden", f"path is in the fixed rejection set: {canonical!r}")

    # 7) 类型预检（打开之前）
    native = to_native_path(canonical, platform)
    try:
        st = os.lstat(native)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise PathResolveError("not_found", f"path does not exist: {canonical!r}") from e
    except OSError as e:
        raise PathResolveError("forbidden", f"lstat failed: {e}") from e
    if not stat.S_ISREG(st.st_mode):
        raise PathResolveError("not_a_file", f"not a regular file: {canonical!r}")

    # 8) 非阻塞打开（SHOULD O_NOFOLLOW）+ fstat + 最终路径复核
    flags = os.O_RDONLY
    for flag_name in ("O_NONBLOCK", "O_NOFOLLOW", "O_BINARY", "O_CLOEXEC"):
        flags |= getattr(os, flag_name, 0)
    try:
        fd = os.open(native, flags)
    except OSError as e:
        raise PathResolveError(_open_error_reason(e, native), f"open failed: {e}") from e

    try:
        try:
            fst = os.fstat(fd)
        except OSError as e:
            raise PathResolveError("forbidden", f"fstat failed: {e}") from e
        if not stat.S_ISREG(fst.st_mode):
            raise PathResolveError("not_a_file", f"opened fd is not a regular file: {canonical!r}")
        # 身份比对（跨平台）：lstat 与 open 之间文件被替换（含 Windows 无 O_NOFOLLOW 的链接替换）
        # → forbidden。Windows 无 /proc / F_GETPATH，此项是最终路径复核的替代保障。
        # Identity check (cross-platform): the file was replaced between lstat and open.
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise PathResolveError("forbidden", "file identity changed between lstat and open")
        if (fst.st_dev, fst.st_ino) in protected.file_ids:
            raise PathResolveError("forbidden", "file identity is in the fixed rejection set (hard link)")
        final_path = _fd_final_path(fd, platform)
        if final_path is not None:
            # 最终路径复核：重跑第 5–6 步（防御 resolve 后的链接 / 路径替换）
            if not _within_any_real(final_path, roots):
                raise PathResolveError("forbidden", f"opened file final path is outside readRoots: {final_path!r}")
            if _is_protected(final_path, protected, platform):
                raise PathResolveError("forbidden", f"opened file final path is in the rejection set: {final_path!r}")
    except BaseException:
        _close_quietly(fd)
        raise
    return canonical, fd, fst


def _validate_path_payload(
    payload: Mapping[str, Any],
    platform: PathPlatform,
) -> tuple[str, int, int, str, str, int | None, int | None]:
    """校验句柄 payload（防御纵深：decode 之外再次检查）/ Re-validate the decoded payload."""
    canonical_path = payload.get("canonical_path")
    total_size = payload.get("total_size")
    mtime = payload.get("mtime")
    sha256 = payload.get("sha256")
    mime = payload.get("mime")
    dev = payload.get("dev")
    ino = payload.get("ino")

    if not isinstance(canonical_path, str) or not canonical_path:
        raise BlobHandleInvalidError("path handle missing canonical_path")
    try:
        normalized = normalize_path(canonical_path, platform)
    except (InvalidPathError, ValueError) as e:
        raise BlobHandleInvalidError(f"path handle has invalid canonical_path: {e}") from e
    if normalized != canonical_path:
        raise BlobHandleInvalidError(f"path handle canonical_path is not normalized: {canonical_path!r}")
    if not isinstance(total_size, int) or isinstance(total_size, bool) or total_size < 0:
        raise BlobHandleInvalidError("path handle invalid total_size")
    if not isinstance(mtime, int) or isinstance(mtime, bool):
        raise BlobHandleInvalidError("path handle invalid mtime")
    if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise BlobHandleInvalidError("path handle invalid sha256")
    if not isinstance(mime, str) or not mime:
        raise BlobHandleInvalidError("path handle missing mime")
    if (dev is None) != (ino is None):
        raise BlobHandleInvalidError("path handle must carry both dev and ino, or neither")
    if dev is not None and (not isinstance(dev, int) or isinstance(dev, bool) or not isinstance(ino, int) or isinstance(ino, bool)):
        raise BlobHandleInvalidError("path handle invalid file identity")
    return canonical_path, total_size, mtime, sha256, mime, dev, ino


def _raise_pull_error(exc: PathResolveError) -> None:
    """把铸造期 reason 映射到拉取期 4018 异常 / Map mint reasons to pull-time 4018 errors."""
    if exc.reason == "invalid_path":
        raise BlobHandleInvalidError(str(exc)) from exc
    if exc.reason == "forbidden":
        raise BlobHandleForbiddenError(str(exc)) from exc
    raise BlobHandleGoneError(str(exc)) from exc


def _is_within(path: str, root: str, *, case_insensitive: bool = False) -> bool:
    """按**路径分量**判定 ``path`` 是否在 ``root`` 内（非裸字符串前缀）.

    与 :func:`a2c_smcp.utils.path.is_within` 语义一致（Path 对象版）；本函数服务
    ``canonical_path`` 字符串形态（含 Windows ``/C:/`` 根与可选 casefold），两处勿各自漂移。
    ``case_insensitive`` 仅由固定拒绝集在 Windows 上使用；readRoots 围栏按协议保持区分大小写。
    """
    if case_insensitive:
        path_cmp = path.casefold()
        root_cmp = root.casefold()
    else:
        path_cmp, root_cmp = path, root
    if path_cmp == root_cmp:
        return True
    if root_cmp.endswith("/"):
        return path_cmp.startswith(root_cmp)
    return path_cmp.startswith(root_cmp + "/")


def _within_any(path: str, roots: Sequence[ReadRoot]) -> bool:
    """词法围栏：配置形态或 realpath 形态任一命中即通过（协议：区分大小写）."""
    for root in roots:
        if _is_within(path, root.configured):
            return True
        if root.real is not None and _is_within(path, root.real):
            return True
    return False


def _within_any_real(path: str, roots: Sequence[ReadRoot]) -> bool:
    """realpath 围栏：只认 realpath 形态."""
    return any(root.real is not None and _is_within(path, root.real) for root in roots)


def _is_protected(canonical_path: str, protected: ProtectedPaths, platform: PathPlatform) -> bool:
    """固定拒绝集判定（含 ``.skillenv`` / landing ``.part``）.

    Windows 上文件名大小写不敏感：拒绝集按 casefold 比较，防止 ``.SKILLENV`` 绕过；
    readRoots 围栏不走此分支（协议要求按码点区分大小写）。
    """
    case_insensitive = platform == "windows"
    if any(_is_within(canonical_path, entry, case_insensitive=case_insensitive) for entry in protected.paths):
        return True
    name = canonical_path.rsplit("/", 1)[-1]
    compare_name = name.casefold() if case_insensitive else name
    if compare_name == ".skillenv":
        return True
    if (
        protected.landing_root is not None
        and _is_within(canonical_path, protected.landing_root, case_insensitive=case_insensitive)
        and compare_name.endswith(".part")
    ):
        return True
    return False


def _stat_key(st: os.stat_result) -> _StatKey:
    """stat → 句柄 stat 键；mtime 毫秒向负无穷取整."""
    return _StatKey(
        total_size=st.st_size,
        mtime_ms=st.st_mtime_ns // 1_000_000,
        dev=st.st_dev,
        ino=st.st_ino,
    )


def _hash_fd(fd: int) -> str:
    """从 fd 起点流式计算全量 sha256（不改变调用方的语义位置）."""
    hasher = hashlib.sha256()
    offset = 0
    while True:
        chunk = _read_fd(fd, offset, _HASH_CHUNK_BYTES)
        if not chunk:
            break
        hasher.update(chunk)
        offset += len(chunk)
    return hasher.hexdigest()


def _read_fd(fd: int, offset: int, length: int) -> bytes:
    """按绝对偏移读 fd（POSIX ``pread``；Windows 回退 ``lseek`` + ``read``）."""
    if _OS_PREAD is not None:
        return _OS_PREAD(fd, length, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


def _close_quietly(fd: int) -> None:
    """关闭 fd；关闭失败只记 DEBUG，不掩盖原始错误 / Close quietly; never mask the original error."""
    try:
        os.close(fd)
    except OSError:  # pragma: no cover - 防御性
        logger.debug("path resolver: closing fd failed", exc_info=True)


def _open_error_reason(exc: OSError, native: str) -> ResolveReason:
    """打开失败 → 4020 reason（协议第 8 步）/ Map open errors to 4020 reasons."""
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return "not_found"
    if isinstance(exc, IsADirectoryError):
        return "not_a_file"
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return "not_found"
    if exc.errno == errno.EISDIR:
        return "not_a_file"
    if os.name == "nt" and exc.errno in (errno.EACCES, errno.EPERM):
        # Windows 打开目录常报 EACCES；用 lstat 区分目录 / 权限。
        try:
            if stat.S_ISDIR(os.lstat(native).st_mode):
                return "not_a_file"
        except OSError:
            pass
    return "forbidden"


def _fd_final_path(fd: int, platform: PathPlatform) -> str | None:
    """取已打开句柄的最终路径（Linux ``/proc/self/fd`` / macOS ``F_GETPATH``）.

    取不到时返回 ``None``（SHOULD 级检查，不阻断）；取到但无法规范化 / 已删除 → ``forbidden``。
    """
    target: str | None = None
    try:
        if os.name == "posix":
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                import fcntl

                f_getpath = getattr(fcntl, "F_GETPATH", None)
                if f_getpath is not None:
                    buf = fcntl.fcntl(fd, f_getpath, b"\x00" * 1024)
                    target = os.fsdecode(buf.split(b"\x00", 1)[0])
    except (OSError, UnicodeError):
        return None
    if target is None:
        return None
    if target.endswith(" (deleted)"):
        raise PathResolveError("forbidden", "file was deleted after open")
    try:
        return normalize_path(target, platform)
    except (InvalidPathError, ValueError) as e:
        raise PathResolveError("forbidden", f"final path cannot be normalized: {e}") from e
