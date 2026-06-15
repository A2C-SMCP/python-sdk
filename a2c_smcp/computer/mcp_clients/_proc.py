# -*- coding: utf-8 -*-
# filename: _proc.py
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
"""进程树工具：枚举本进程的直接子进程 PID，并按进程组强杀（兑底强杀专用）。

中文：仅服务于 MCP stdio 子进程的「兑底强杀」——当优雅关闭（mcp SDK 的 graceful→SIGTERM→SIGKILL）
在极端情形下仍未回收子进程时，作为最后保险，确保 ``Computer.shutdown()`` 永不被卡死的 stdio 子进程
拖死。所有函数均为 best-effort：无法枚举 / 无权限 / 非 POSIX 时静默降级，**绝不抛出**。

English: process-tree helpers used **only** as the force-kill fallback for MCP stdio children, so
``Computer.shutdown()`` can never hang on a wedged stdio child. Every function is best-effort and
degrades silently (never raises) when enumeration / permission is unavailable or the platform is
not POSIX.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

__all__ = ["list_own_child_pids", "force_kill_pids"]


def list_own_child_pids() -> set[int]:
    """返回当前进程的直接子进程 PID 集合（best-effort）。

    中文：Linux 走 ``/proc/<pid>/task/<pid>/children``（需内核 ``CONFIG_PROC_CHILDREN``，GitHub
    Actions 的 ubuntu runner 默认开启）；其它 POSIX 退回 ``pgrep -P``；均不可用 / 非 POSIX 返回空集。
    English: Linux fast path via procfs ``children``; POSIX fallback via ``pgrep -P``; empty set otherwise.
    """
    if sys.platform == "win32":  # pragma: no cover - 项目运行于 POSIX / project is POSIX-only
        return set()
    pid = os.getpid()
    # Linux 快路径：procfs children（无子进程开销）/ Linux fast path: procfs children
    try:
        data = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
        return {int(x) for x in data.split()}
    except OSError:
        pass
    # POSIX 通用退路：pgrep -P（macOS / 无 procfs）/ POSIX fallback: pgrep -P
    try:
        out = subprocess.run(  # noqa: S603,S607
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return {int(x) for x in out.stdout.split()}
    except (OSError, ValueError, subprocess.SubprocessError):  # pragma: no cover - 环境兜底
        return set()


def force_kill_pids(pids: set[int]) -> None:
    """对给定 PID 集合按进程组 SIGKILL 强杀（best-effort，**绝不抛出**）。

    中文：mcp 的 ``stdio_client`` 以 ``start_new_session=True`` 启动子进程（独立会话 / 进程组，
    ``pgid == pid``），故优先 ``os.killpg`` 原子回收整棵子树，退回 ``os.kill``。进程已退出 / 无权限
    均静默忽略。
    English: prefer ``os.killpg`` (mcp spawns children in their own session/process group) to reap the
    whole tree atomically; fall back to ``os.kill``. Already-exited / permission errors are ignored.
    """
    if sys.platform == "win32":  # pragma: no cover - 项目运行于 POSIX / project is POSIX-only
        return
    for pid in pids:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
