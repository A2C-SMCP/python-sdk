# -*- coding: utf-8 -*-
# filename: path.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
路径工具 / Path utilities

跨子系统复用的纯路径助手（无 a2c 依赖，仅 stdlib）。
Cross-subsystem pure path helpers (no a2c deps, stdlib only).
"""

from __future__ import annotations

from pathlib import Path


def is_within(path: Path, parent: Path) -> bool:
    """
    ``path`` 是否等于或位于 ``parent`` 之下 / Whether ``path`` is at or nested under ``parent``。

    纯词法判定（基于 :meth:`pathlib.PurePath.relative_to`）——**不**解析符号链接，调用方需自行
    传入已 ``resolve()`` 的路径以获得防穿越语义。
    Lexical check via :meth:`pathlib.PurePath.relative_to`; does NOT resolve symlinks — callers
    should pass already-``resolve()``-d paths for anti-traversal semantics.
    """
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
