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
"""

from __future__ import annotations

import os
from typing import Literal

PathPlatform = Literal["posix", "windows"]


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
    """
    raise NotImplementedError("path core skeleton — implemented in the next commit")


def to_native_path(canonical_path: str, platform: PathPlatform | None = None) -> str:
    """``canonical_path`` → 宿主原生路径（供 ``lstat`` / ``open``）.

    POSIX 原样返回；Windows ``/C:/a/b`` → ``C:\\a\\b``。
    """
    raise NotImplementedError("path core skeleton — implemented in the next commit")


def realpath_nonstrict(path: str, platform: PathPlatform | None = None) -> str:
    """非严格 realpath：解析符号链接（含悬空链接），缺失分量原样保留.

    等价 ``os.path.realpath(..., strict=False)``；**MUST NOT** 以跟随链接的 ``stat`` /
    ``exists()`` 判定存在性。返回宿主原生路径，调用方再经 :func:`normalize_path` 归一。
    """
    raise NotImplementedError("path core skeleton — implemented in the next commit")
