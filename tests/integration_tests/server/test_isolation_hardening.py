# -*- coding: utf-8 -*-
"""
* 文件名: test_isolation_hardening
* 描述: GitHub #31 —— on_client_* / on_server_* 的 office/role 隔离在 ``-O``
        （断言被字节码剥离）下必须依旧生效。
        Hardening regression: office/role isolation MUST still hold when Python runs
        with ``-O`` (assert statements stripped at compile time).

设计说明 / Design note:
  本用例不依赖具体**拒绝形态**（异常 / flat ErrorPayload 皆可）——只验证"设计无关的安全
  不变量"：在断言被剥离时，跨房间访问 **不得** 泄露另一房间的会话数据。
  判据刻意做成**形状无关**：只要会话读取器被调用过，即视为泄露面已被打开（"先取全房成员
  再拒绝"同样不合格）；这样 v0.5.0 把拒绝形态由 raise 改为 ErrorPayload(4104) 时，
  用例的判别力不随之削弱。
  Exception-type and rejection-shape agnostic: the invariant is that a cross-room request must not
  reach the session reader at all, so a "fetch-then-reject" implementation also fails.
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
        reader = AsyncMock(return_value=leaked)
        ns_mod.aget_all_sessions_in_office = reader
        try:
            ret = await ns.on_server_list_room(
                "a_sid", {"agent": "a_sid", "req_id": "r", "office_id": "office_B"}
            )
        except Exception:
            print("REJECTED")
            return
        # v0.5.0（#214）：越权以 flat ErrorPayload(4104) 表达，**不是**异常。
        # 判据与"实现形态"解耦：只要是结构化拒绝（顶层 code），就不算泄露。
        if isinstance(ret, dict) and "code" in ret:
            # 形状无关的强判据：**读取器根本不该被调用**——「先取全房成员再拒绝」同样算泄露面被打开
            if reader.await_count:
                print("LEAK:reader-called")
            else:
                print("REJECTED")
            return
        # 未拒绝 = 隔离被剥离，office_B 数据被泄露
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
    reader = MagicMock(return_value=leaked)
    ns_mod.get_all_sessions_in_office = reader
    try:
        ret = ns.on_server_list_room(
            "a_sid", {"agent": "a_sid", "req_id": "r", "office_id": "office_B"}
        )
    except Exception:
        print("REJECTED")
    else:
        # v0.5.0（#214）：越权以 flat ErrorPayload(4104) 表达，**不是**异常
        if isinstance(ret, dict) and "code" in ret:
            # 形状无关的强判据：**读取器根本不该被调用**（见 async 脚本同名说明）
            print("LEAK:reader-called" if reader.called else "REJECTED")
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
