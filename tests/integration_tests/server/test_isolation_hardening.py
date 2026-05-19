# -*- coding: utf-8 -*-
"""
* 文件名: test_isolation_hardening
* 描述: GitHub #31 —— on_client_* / on_server_* 的 office/role 隔离在 ``-O``
        （断言被字节码剥离）下必须依旧生效。
        Hardening regression: office/role isolation MUST still hold when Python runs
        with ``-O`` (assert statements stripped at compile time).

设计说明 / Design note:
  本用例不依赖具体异常类型——只验证"设计无关的安全不变量"：在断言被剥离时，
  跨房间访问 **不得** 静默放行并泄露另一房间的会话数据。
  This case is exception-type agnostic. It only asserts the design-agnostic security
  invariant: with assertions stripped, a cross-room request MUST NOT silently pass
  and leak the other room's session data.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

# 子进程脚本：构造 namespace，mock 跨房间会话，调用 on_server_list_room。
# Subprocess script: build namespace, mock cross-room sessions, call on_server_list_room.
# Agent 在 office_A，却请求 office_B —— 隔离生效应拒绝；失效则会泄露 office_B 数据。
# Agent in office_A requests office_B — isolation must reject; if broken it leaks office_B data.
_ASYNC_SCRIPT = textwrap.dedent(
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    import a2c_smcp.server.namespace as ns_mod
    from a2c_smcp.server import SMCPNamespace, AuthenticationProvider

    async def main():
        ns = SMCPNamespace(MagicMock(spec=AuthenticationProvider))
        ns.server = MagicMock()
        ns.get_session = AsyncMock(
            return_value={"sid": "a_sid", "name": "a", "role": "agent", "office_id": "office_A"}
        )
        leaked = [
            {"sid": "spy", "name": "spy", "role": "agent", "office_id": "office_B"},
            {"sid": "c_b", "name": "c_b", "role": "computer", "office_id": "office_B"},
        ]
        ns_mod.aget_all_sessions_in_office = AsyncMock(return_value=leaked)
        try:
            ret = await ns.on_server_list_room(
                "a_sid", {"agent": "a_sid", "req_id": "r", "office_id": "office_B"}
            )
        except Exception:
            print("REJECTED")
            return
        # 未抛异常 = 隔离被剥离，office_B 数据被泄露
        print("LEAK:" + ",".join(s["sid"] for s in ret.get("sessions", [])))

    asyncio.run(main())
    """
)

_SYNC_SCRIPT = textwrap.dedent(
    """
    from unittest.mock import MagicMock
    import a2c_smcp.server.sync_namespace as ns_mod
    from a2c_smcp.server import SyncSMCPNamespace, SyncAuthenticationProvider

    ns = SyncSMCPNamespace(MagicMock(spec=SyncAuthenticationProvider))
    ns.server = MagicMock()
    ns.get_session = MagicMock(
        return_value={"sid": "a_sid", "name": "a", "role": "agent", "office_id": "office_A"}
    )
    leaked = [
        {"sid": "spy", "name": "spy", "role": "agent", "office_id": "office_B"},
        {"sid": "c_b", "name": "c_b", "role": "computer", "office_id": "office_B"},
    ]
    ns_mod.get_all_sessions_in_office = MagicMock(return_value=leaked)
    try:
        ret = ns.on_server_list_room(
            "a_sid", {"agent": "a_sid", "req_id": "r", "office_id": "office_B"}
        )
    except Exception:
        print("REJECTED")
    else:
        print("LEAK:" + ",".join(s["sid"] for s in ret.get("sessions", [])))
    """
)


def _run_optimized(script: str) -> str:
    """以 ``python -O`` 执行脚本（断言被剥离），返回 stdout 末行。"""
    proc = subprocess.run(
        [sys.executable, "-O", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr}"
    return proc.stdout.strip().splitlines()[-1]


def test_async_list_room_isolation_holds_under_O() -> None:
    """``-O`` 下异步 on_server_list_room 的跨房间隔离必须依旧拒绝。"""
    result = _run_optimized(_ASYNC_SCRIPT)
    assert result == "REJECTED", f"跨房间隔离在 -O 下被剥离，泄露了 office_B 数据: {result}"


def test_sync_list_room_isolation_holds_under_O() -> None:
    """``-O`` 下同步 on_server_list_room 的跨房间隔离必须依旧拒绝。"""
    result = _run_optimized(_SYNC_SCRIPT)
    assert result == "REJECTED", f"跨房间隔离在 -O 下被剥离，泄露了 office_B 数据: {result}"
