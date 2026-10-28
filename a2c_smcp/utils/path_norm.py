# -*- coding: utf-8 -*-
# filename: path_norm.py
# @Author  : JQQ
# @Software: PyCharm
"""
``canonical_path`` 词法规范化（纯函数）/ Lexical ``canonical_path`` normalization (pure).

协议依据 / Protocol: a2c-smcp-protocol ``blob-transfer.md`` §canonical_path 规范化 +
``fixtures/canonical_path_conformance_vectors.json``（protocol#65）。

设计约束 / Design constraints:
  - 与宿主 OS 无关：``platform`` 显式传入，Windows 形态在任意 CI 上可测；
  - 不做 ``~`` 展开、百分号编解码、Unicode 归一，也不改变非盘符部分大小写；
  - 只负责**词法**部分；符号链接解析（realpath）由 ``computer/blob/path_resolver.py`` 负责。

算法 / Algorithm:

  POSIX
    空串 / U+0000 ⇒ ``invalid_path``；MUST 以 ``/`` 开头，``\\`` 为普通字符；
    按 ``/`` 切段 → 丢空段与 ``.`` → ``..`` 弹段（根处钳制）→ ``/`` 连接。

  Windows
    ``\\.\\`` 设备前缀 ⇒ ``invalid_path``（先于一切）；剥 ``\\?\\`` 前缀，其后
    ``UNC\\``（大小写不敏感）⇒ ``invalid_path``；``\\`` → ``/``；canonical ``/X:``
    剥前导 ``/``（恰为 ``X:`` 补 ``/``）；MUST ``X:/`` 开头；盘符大写；余部同 POSIX
    切段，分量限制（``< > : " | ? *`` / U+0001–U+001F / 以 ``.`` 或空格结尾）先于
    ``..`` 弹出判定 ⇒ ``invalid_path``。
"""

from __future__ import annotations

import os
from typing import Literal

PathPlatform = Literal["posix", "windows"]

# Windows 分量禁用字符 / Windows forbidden component characters.
_WIN_FORBIDDEN_CHARS = frozenset('<>:"|?*')

# 合法平台取值 / Valid platform values.
_PLATFORMS = ("posix", "windows")


class InvalidPathError(ValueError):
    """路径无法规范化 → ``4020 invalid_path`` / Path cannot be normalized."""

    reason = "invalid_path"


def native_platform() -> PathPlatform:
    """返回宿主平台形态 / Return the host platform form."""
    return "windows" if os.name == "nt" else "posix"


def normalize_path(path: str, platform: PathPlatform | None = None) -> str:
    """把路径规范化为 ``canonical_path`` 形态 / Normalize a path to ``canonical_path`` form.

    Args:
        path: 原始路径（POSIX 绝对路径；Windows 原生 ``C:\\...`` 或 canonical ``/C:/...``）。
        platform: ``posix`` / ``windows``；缺省取宿主平台。

    Returns:
        规范路径字符串（POSIX 以 ``/`` 开头；Windows 为 ``/X:/...``、盘符大写、``/`` 分隔）。

    Raises:
        InvalidPathError: 空串、含 U+0000、非绝对路径、Windows 分量非法等。
        ValueError: ``platform`` 不是 ``posix`` / ``windows``。
    """
    pf = _check_platform(platform)
    if not isinstance(path, str):
        raise InvalidPathError(f"path must be str, got {type(path).__name__}")
    if not path or "\x00" in path:
        raise InvalidPathError("path is empty or contains U+0000")
    return _normalize_posix(path) if pf == "posix" else _normalize_windows(path)


def to_native_path(canonical_path: str, platform: PathPlatform | None = None) -> str:
    """``canonical_path`` → 宿主原生路径（供 ``lstat`` / ``open``）.

    POSIX 原样返回；Windows ``/C:/a/b`` → ``C:\\a\\b``。
    """
    pf = _check_platform(platform)
    if pf == "posix":
        return canonical_path
    if not _is_canonical_windows_path(canonical_path):
        raise InvalidPathError(f"not a canonical Windows path: {canonical_path!r}")
    # ``/C:/a/b`` → ``C:\a\b``；``/C:/`` → ``C:\``
    return canonical_path[1:].replace("/", "\\")


def realpath_nonstrict(path: str, platform: PathPlatform | None = None) -> str:
    """非严格 realpath：解析符号链接（含悬空链接），缺失分量原样保留.

    等价 ``os.path.realpath(..., strict=False)``；**MUST NOT** 以跟随链接的 ``stat`` /
    ``exists()`` 判定存在性。返回宿主原生路径，调用方再经 :func:`normalize_path` 归一。
    """
    return os.path.realpath(to_native_path(path, platform), strict=False)


# ── 内部实现 / Internal implementation ───────────────────────────────────


def _check_platform(platform: PathPlatform | None) -> PathPlatform:
    pf: PathPlatform = native_platform() if platform is None else platform
    if pf not in _PLATFORMS:
        raise ValueError(f"unsupported platform: {pf!r}")
    return pf


def _normalize_posix(path: str) -> str:
    if not path.startswith("/"):
        raise InvalidPathError(f"POSIX path must be absolute: {path!r}")
    segments: list[str] = []
    for segment in path.split("/"):
        if segment == "" or segment == ".":
            continue
        if segment == "..":
            # 根处钳制：已在根时忽略 / Clamp at root: ignore when already at root.
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


def _normalize_windows(path: str) -> str:
    # ① 设备前缀不支持（先于 ``\\?\`` 剥离判定）/ Device prefix unsupported (before verbatim).
    if path.startswith("\\\\.\\"):
        raise InvalidPathError(f"Windows device prefix is not supported: {path!r}")
    if path.startswith("\\\\?\\"):
        path = path[4:]
        # ② 剥离后 UNC ⇒ 不支持 / UNC after verbatim prefix ⇒ unsupported.
        if path[:4].upper() == "UNC\\":
            raise InvalidPathError(f"UNC path is not supported: {path!r}")
    # ③ ``\`` → ``/``
    path = path.replace("\\", "/")
    # ④ canonical ``/X:`` 形态剥前导 ``/``；恰为 ``X:`` 时补 ``/``
    if _is_canonical_windows_drive(path):
        path = path[1:]
        if len(path) == 2:
            path += "/"
    # ⑤ MUST ``X:/`` 开头 / MUST start with ``X:/``
    if not _is_windows_drive_path(path):
        raise InvalidPathError(f"Windows path must be drive-absolute: {path!r}")
    drive = path[0].upper()
    segments: list[str] = []
    for segment in path[3:].split("/"):
        if segment == "" or segment == ".":
            continue
        _check_windows_component(segment)
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return f"/{drive}:/" + "/".join(segments)


def _is_canonical_windows_drive(path: str) -> bool:
    """``/X:`` 或 ``/X:/...`` / ``/X:`` or ``/X:/...``."""
    return len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":" and (len(path) == 3 or path[3] == "/")


def _is_canonical_windows_path(path: str) -> bool:
    """``/X:/`` 或 ``/X:/...``（canonical 形态）/ Canonical ``/X:/`` form."""
    return len(path) >= 4 and path[0] == "/" and path[1].isalpha() and path[2] == ":" and path[3] == "/"


def _is_windows_drive_path(path: str) -> bool:
    """``X:/...`` / ``X:/...``."""
    return len(path) >= 3 and path[0].isalpha() and path[1] == ":" and path[2] == "/"


def _check_windows_component(segment: str) -> None:
    """Windows 分量限制；``.`` / ``..`` 本身豁免 / Component limits; ``.`` / ``..`` exempt."""
    if segment == "." or segment == "..":
        return
    if any(ch in _WIN_FORBIDDEN_CHARS for ch in segment):
        raise InvalidPathError(f"Windows path component contains a forbidden character: {segment!r}")
    if any(0x01 <= ord(ch) <= 0x1F for ch in segment):
        raise InvalidPathError(f"Windows path component contains a control character: {segment!r}")
    if segment.endswith(".") or segment.endswith(" "):
        raise InvalidPathError(f"Windows path component ends with '.' or space: {segment!r}")
