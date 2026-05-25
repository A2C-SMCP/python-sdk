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

from collections.abc import Mapping
from pathlib import Path


def resolve_xdg_first(env: Mapping[str, str], xdg_var: str, *subpath: str, fallback: Path) -> Path:
    """
    XDG-first 路径解析 / XDG-first path resolution（镜像 Claude Code「XDG 优先 + 回退」范式）。

    若 ``env[xdg_var]`` 为**绝对路径** → 取 ``$<xdg_var>/<*subpath>``；否则（未设置 / 相对值——按 XDG
    规范相对值视为未设置）→ 取 ``fallback``。结果统一 ``expanduser().resolve()`` 返回绝对路径；**不**创建目录。
    If ``env[xdg_var]`` is an **absolute** path, returns ``$<xdg_var>/<*subpath>``; otherwise
    (unset, or a relative value treated as unset per the XDG spec) returns ``fallback``. The result
    is ``expanduser().resolve()``-d to an absolute path; no directory is created.

    供 SKILL Home（``XDG_DATA_HOME``）与 user 配置目录（``XDG_CONFIG_HOME``）等复用——两者仅 env 变量、
    子路径、fallback 不同。env-override（如 ``A2C_SKILL_HOME``）等更高优先级旋钮由调用方在调用前自行处理。
    Reused by SKILL Home and the user config dir; higher-priority env overrides are handled by callers.

    :param env: 环境映射（便于测试注入）/ env mapping (injectable for tests).
    :param xdg_var: XDG 环境变量名（如 ``XDG_DATA_HOME`` / ``XDG_CONFIG_HOME``）/ the XDG env var name.
    :param subpath: 追加在 ``$<xdg_var>`` 之后的路径段 / segments appended under ``$<xdg_var>``.
    :param fallback: XDG 未设置 / 非绝对时的回退路径 / fallback path when XDG is unset / non-absolute.
    """
    raw = env.get(xdg_var, "").strip()
    xdg_path = Path(raw) if raw else None
    # XDG 规范：变量必须为绝对路径，相对值按未设置处理。/ XDG spec: must be absolute; relative treated as unset.
    base = xdg_path.joinpath(*subpath) if xdg_path is not None and xdg_path.is_absolute() else fallback
    return base.expanduser().resolve()


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
