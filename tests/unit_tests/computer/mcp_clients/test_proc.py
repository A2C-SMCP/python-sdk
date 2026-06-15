# -*- coding: utf-8 -*-
# filename: test_proc.py
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
"""单元测试：进程树工具 _proc（兑底强杀专用）。

中文：覆盖 list_own_child_pids 能发现本进程直接子进程、force_kill_pids 能强杀进程组，
以及对已退出 / 不存在 PID 的静默容错（绝不抛出）。
English: cover child enumeration, process-group force-kill, and silent tolerance of dead/bogus pids.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from a2c_smcp.computer.mcp_clients._proc import force_kill_pids, list_own_child_pids


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only force-kill fallback")
def test_list_own_child_pids_finds_direct_child() -> None:
    """spawn 一个直接子进程，应出现在 list_own_child_pids 结果中。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # noqa: S603
    try:
        # 子进程注册可能有极短延迟，最多重试 ~2s / brief retry for child registration
        found = False
        for _ in range(20):
            if proc.pid in list_own_child_pids():
                found = True
                break
            time.sleep(0.1)
        assert found, f"spawned child {proc.pid} not found among own children"
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only force-kill fallback")
def test_force_kill_pids_kills_process_group() -> None:
    """force_kill_pids 应按进程组强杀（子进程以独立 session 启动，pgid==pid）。"""
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    assert _pid_alive(proc.pid)
    force_kill_pids({proc.pid})
    # 回收并确认已死 / reap and confirm dead
    proc.wait(timeout=5)
    assert proc.returncode is not None
    assert proc.returncode == -signal.SIGKILL or not _pid_alive(proc.pid)


def test_force_kill_pids_tolerates_dead_and_bogus_pids() -> None:
    """对已退出 / 不存在的 PID 静默容错，绝不抛出。"""
    # 一个极不可能存在的高位 PID / a very unlikely-to-exist high pid
    force_kill_pids({2_000_000_000})
    force_kill_pids(set())  # 空集即返回 / empty set no-ops


def test_list_own_child_pids_returns_set() -> None:
    """无子进程上下文也应返回一个 set（best-effort，不抛出）。"""
    result = list_own_child_pids()
    assert isinstance(result, set)
