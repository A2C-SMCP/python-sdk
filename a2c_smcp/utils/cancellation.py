# -*- coding: utf-8 -*-
# filename: cancellation.py
# @Author  : JQQ
# @Software: PyCharm
"""外部取消信号的还原（#211）。

停机 / 断开 / 启动链路上存在多处**按设计**吞掉 ``CancelledError`` 的位置——第三方状态机的
``AsyncMachine.process_context``（服务于库自身的 ``cancel_running_transitions``）、
``BaseMCPClient._close_task`` 的限时等待、``contextlib.suppress(asyncio.CancelledError)``——使外部对入口
协程的取消在链路上消失：协程「正常返回」→ ``asyncio.wait_for`` / ``asyncio.timeout`` 因未见
``CancelledError`` 而**不抛 ``TimeoutError``**，嵌入宿主（CLI 退出、宿主 teardown、``__aexit__``）拿不到
任何超时信号。

吞点枚举**注定不全**（第三方内部还会新增），故判据取**计数式**而非逐点封堵：``Task.cancelling()`` 是
「已收到但未被确认的取消请求数」，只在 ``cancel()`` 时自增、只在显式 ``uncancel()`` 时自减。入口处取快照、
收尾处比对，只还原**本次调用期间**新收到的取消——不误伤长活任务上的历史遗留计数；嵌套
``asyncio.timeout`` / ``wait_for`` 在自身收尾时会 ``uncancel()``（实测），故内部超时不会误报。

语义是**协作式**取消：被吞的取消**不中止**工作（停机必须在非取消上下文跑完，否则 mcp 的子进程强杀会被
``CancelledError`` 抢占跳过、stdio 子进程残留），而是在入口收尾处补抛——调用方拿到的是「未在时限内
完成」的真实信号，而非静默成功。**(#211)**

**只挂「宿主可 await 的最外层入口」**：若挂到内层步骤（如 ``BaseMCPClient.adisconnect``、
``BaseMCPClient.aconnect``、``MCPServerManager._astop_client``），内层补抛会中断外层收尾——``_astop_all``
循环中途抛出会让**剩余 client 不再被停止**、``aclose`` 的 ``_clear_all`` 被跳过，等于把「取消可观测」
换成「拆除不完整」。嵌套装饰本身是安全的（只有最外层补抛），但装饰点仍应选最外层。

**装饰面判据**（避免后人反复重扫）：= **宿主可 await 的生命周期入口**，即
``Computer.boot_up`` / ``Computer.shutdown`` / ``MCPServerManager.aclose`` /
``MCPServerManager.astart_all`` / ``astart_client`` / ``astop_all`` / ``astop_client``
（CLI REPL 的 ``start``/``stop`` 命令直接 await 后四个）。**RPC 入口刻意不覆盖**（``aexecute_tool`` /
``aget_available_tools`` / ``get_desktop`` / ``get_resources`` 等）：它们经 ``async_session`` **懒连接**
才触达同一状态机吞点，且 socketio 事件面对 ``CancelledError`` 没有处置约定——混进来会把「工具调用
超时/取消」的语义与「生命周期取消」搅在一起。

**跨任务边界**：作用域按「当前任务 + 嵌套深度」判定，换任务（``asyncio.create_task`` / ``gather`` /
``TaskGroup`` 会复制 context）即视作该任务的最外层——否则在被装饰入口内 spawn 的任务里再调被装饰入口时，
会读到父任务的深度而永不补抛（吞掉的取消静默丢失；实测该退化形态）。
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import Any, ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")

# 被装饰入口的作用域：(发起任务, 嵌套深度)。仅**当前任务的**最外层（深度归零）补抛，
# 既避免内层补抛中断外层收尾，又保证换任务后（context 被复制）仍按新任务的最外层判定。
_scope: ContextVar[tuple[asyncio.Task | None, int] | None] = ContextVar("a2c_cancellation_scope", default=None)


def _scope_depth() -> int:
    """当前任务内被装饰入口的嵌套深度；**换任务即视作 0**（上下文复制会让父任务的作用域跟进来）。"""
    scope = _scope.get()
    if scope is None or scope[0] is not asyncio.current_task():
        return 0
    return scope[1]


def cancel_entry_snapshot() -> int:
    """取当前任务的取消计数快照（入口处调用）；无运行任务（纯同步上下文）时返回 0。"""
    task = asyncio.current_task()
    return task.cancelling() if task is not None else 0


def restore_swallowed_cancel(entry: int) -> None:
    """自 ``entry`` 快照以来若新收到过取消，则补抛 ``CancelledError``（收尾处调用）。"""
    task = asyncio.current_task()
    if task is not None and task.cancelling() > entry:
        raise asyncio.CancelledError("cancellation restored at entry point (see #211)")


def restores_cancellation(fn: Callable[_P, Coroutine[Any, Any, _T]]) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """装饰宿主可 await 的**最外层**入口：把被下游吞掉的外部取消在收尾处还原成 ``CancelledError``。

    装饰后：正常完成且本次调用期间收到过取消 → 抛 ``CancelledError``；正常完成且无取消 → 原样返回；
    函数自身抛错 → 原样传播（不会被补抛顶替）。嵌套装饰时只有最外层补抛。

    见模块 docstring 的语义说明与「只挂最外层」约束。
    """

    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        entry = cancel_entry_snapshot()
        token = _scope.set((asyncio.current_task(), _scope_depth() + 1))
        try:
            result = await fn(*args, **kwargs)
        finally:
            _scope.reset(token)
        if _scope_depth() == 0:
            restore_swallowed_cancel(entry)
        return result

    return wrapper
