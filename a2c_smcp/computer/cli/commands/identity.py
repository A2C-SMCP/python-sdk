# -*- coding: utf-8 -*-
# filename: identity.py
# @Time    : 2026/10/28
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``identity`` 命令 handler（reset）/ ``identity`` command handler (reset)。

协议依据 / Protocol: a2c-smcp-protocol room-model.md §Computer 身份「重置」（protocol#64）。

两种入口，语义**有意不同**（用户 2026-10-27 拍板方案 A）：

- **REPL** ``identity reset``：复用 SDK 的 :meth:`SMCPComputerClient.reset_identity`——同进程可判定
  「已连接（含入房意图）」并**拒绝**（同一连接内改身份只会撞 ``403``）；无 client 时退化为纯文件重置。
- **独立 CLI** ``a2c-computer identity reset``：进程内没有运行中的 Computer，**无法**判定连接状态，
  只做**原子文件替换**并提示「需重启运行中的 Computer」；不计入「已连接拒绝」验收。

The REPL reuses the SDK API (can detect a live connection and refuse); the standalone CLI only swaps
the file atomically and tells the user to restart a running Computer.
"""

from __future__ import annotations

from collections.abc import Mapping

from a2c_smcp.computer.cli.utils import console, console_err
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.identity import ComputerIdentityError, reset_computer_id

#: 重置后新值何时生效的固定提示。/ Fixed hint on when the new value takes effect.
_RESTART_HINT = (
    "新身份只经**新连接**生效：请重启运行中的 Computer（或断开后重新连接）。"
    " / The new identity only takes effect on a new connection — restart or reconnect."
)


def reset_identity_cli(env: Mapping[str, str] | None = None) -> int:
    """
    独立 CLI ``a2c-computer identity reset``：原子替换身份文件，返回退出码。

    只做文件层动作：不判断、也无法判断是否有 Computer 正在运行（那是另一个进程）。失败（不可写 /
    目录异常）⇒ 退出码 1 + 明确错误，绝不静默。
    Replace the identity file atomically; no connection-state check (none can be known).
    """
    try:
        new_id = reset_computer_id(env=env)
    except ComputerIdentityError as e:
        console_err.print(f"[red]❌ 重置失败 / reset failed: {e}[/red]")
        return 1
    console.print(f"[green]✅ 已重置 Computer 身份 / identity reset: {new_id}[/green]")
    console.print(f"[yellow]{_RESTART_HINT}[/yellow]")
    return 0


async def repl_dispatch(comp: Computer, parts: list[str]) -> None:
    """
    REPL 分发：``identity reset``。已连接 / 有入房意图时**拒绝**（复用 SDK API 的判定）。

    Returns:
        None: 结果直接打印到 REPL 控制台。
    """
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub != "reset" or len(parts) > 2:
        console.print("[yellow]用法 / usage: identity reset[/yellow]")
        return

    client = comp.socketio_client
    try:
        # 复用同一个 SDK API（同进程可判定 connected / 入房意图）；无 client = 必然未连接 ⇒ 纯文件重置。
        # Reuse the same SDK API when a client exists; otherwise the process cannot be connected.
        new_id = await client.reset_identity() if client is not None else reset_computer_id()
    except RuntimeError as e:
        # RuntimeError：已连接拒绝；ComputerIdentityError（其子类）：文件不可写 / 目录异常
        console.print(f"[red]❌ 重置失败 / reset failed: {e}[/red]")
        return

    console.print(f"[green]✅ 已重置 Computer 身份 / identity reset: {new_id}[/green]")
    console.print(f"[yellow]{_RESTART_HINT}[/yellow]")
