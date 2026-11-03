# -*- coding: utf-8 -*-
# filename: reconnect.py
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
socketio 自动重连窗口的共享护栏 / Shared guards for socketio's auto-reconnect window.

供 Computer（async）与 Agent（async / sync）三个客户端复用。两类缺陷同根——上游 python-socketio
（5.14.x）的重连循环只认「``_reconnect_abort`` 置位」这一个停止信号，而 SDK 的两个入口都没有接上它：

1. **重连窗口内 ``disconnect()`` 是空操作**（v0.5.0 审查 🔴5）：掉线后 ``namespaces`` 已被清空、
   eio 已断开，上游 ``disconnect()`` 既不发包、也不置 ``_reconnect_abort``、也不触发任何 SDK 钩子
   （客户端此时本就不是 ``connected``）⇒ 入房意图不清、重连不停，约 1 秒后自动回到旧房。只有
   ``shutdown()`` 会置中止位。:func:`adisconnect_aborting_reconnect` / :func:`disconnect_aborting_reconnect`
   把「断开」补全为「中止重连 + 等重连任务收敛 + 若恰好已重连成功则真正断开」。
2. **重连循环内遇到 4008**（v0.5.0 审查 Y10）：上游循环只吞 ``ConnectionError`` / ``ValueError``。
   Computer 覆写的 ``connect()`` 会把 4008 转成 ``ProtocolVersionError`` ⇒ 逃出循环、任务死亡、
   ``__disconnect_final`` 永不触发 ⇒ 意图永久残留；Agent 不覆写 ⇒ 4008 作为 ``ConnectionError``
   被吞掉 ⇒ **无限重试**（违反 versioning.md §4「不得静默重试」）。:func:`abort_reconnect_on_version_error`
   统一为：置中止位 + ERROR 日志 + 交还一个 ``ConnectionError`` 让循环下一拍中止 ⇒ 上游触发
   ``__disconnect_final`` ⇒ 既有钩子清意图。

**依赖上游私有属性**（``_reconnect_task`` / ``_reconnect_abort``）：这是唯一可用的接缝（公开的
``shutdown()`` 在「已连接」时只做 ``disconnect()``、不适用于「重连循环内部」），已按 5.14.3 源码核对；
升级 python-socketio 时须复核 ``_handle_reconnect`` 的形状。
Relies on socketio's private reconnect attributes — the only available seam; re-verify on upgrades.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from socketio.exceptions import ConnectionError as SioConnectionError

from a2c_smcp.utils.handshake import extract_4008_payload

#: 中止重连后等待重连任务收敛时的轮询步长（秒）。**必须反复置位**：上游 ``_handle_reconnect`` 进入时
#: 先 ``_reconnect_abort.clear()``——若我们恰好在「任务已创建、尚未执行到 clear」之间置位，会被抹掉。
#: 每轮重新置位即可跨过这条竞态（置位是幂等的）。
#: Re-set the abort flag on every poll: the reconnect loop clears it on entry.
_ABORT_POLL_INTERVAL = 0.05

#: 中止后等待重连任务收敛的**上界**（秒，与入房 ACK 的 ``OFFICE_JOIN_TIMEOUT`` 同值）。在途那次尝试可能卡在上游
#: eio 握手 / namespace 等待（慢网络、黑洞地址），无界等待会把 CLI 交互循环一并卡死。到点即记 WARNING 返回：
#: 入房意图已先清、中止位仍置位 ⇒ 循环在下一拍中止；那次尝试即便随后成功，连接钩子也不会回房。
#: Upper bound for the abort wait; on expiry we warn and return (intent already cleared, abort flag still set).
_ABORT_WAIT_TIMEOUT = 10.0

_logger = logging.getLogger(__name__)


def _reconnect_task_alive(client: Any) -> bool:
    """重连任务是否仍在途（async task / sync thread 两种形态）。

    上游只在「重连成功」时把 ``_reconnect_task`` 置回 ``None``；中止 / 放弃后它仍指向一个**已结束**的
    任务 ⇒ 不能只判非空。/ The attribute is only reset on success, so a finished task must count as idle.
    """
    task = getattr(client, "_reconnect_task", None)
    if task is None:
        return False
    if isinstance(task, threading.Thread):
        return task.is_alive()
    done = getattr(task, "done", None)
    return not done() if callable(done) else False


def in_reconnect_loop(client: Any) -> bool:
    """当前调用是否**来自**上游重连循环本身（``connect()`` 覆写据此区分首连与重连尝试）。

    判据是「当前 task / 线程就是 ``_reconnect_task``」——比 ``retry=False`` 精确（调用方也可能显式传它）。
    / True when the current task/thread *is* socketio's reconnect task.
    """
    task = getattr(client, "_reconnect_task", None)
    if task is None:
        return False
    if isinstance(task, threading.Thread):
        return task is threading.current_thread()
    try:
        return task is asyncio.current_task()
    except RuntimeError:  # 无运行中的事件循环 / no running loop
        return False


def forget_finished_reconnect_task(client: Any) -> None:
    """``connect()`` 入口调用：丢弃已结束的重连任务引用。

    上游 ``_handle_eio_disconnect`` 以 ``not self._reconnect_task`` 决定是否启动新一轮重连——中止 / 放弃
    后残留的「已结束任务」会让**同一客户端**再次 ``connect()`` 后的掉线**永不自动重连**。在重连循环
    内部调用时不得清（那是当前任务自身）。
    Drop a *finished* reconnect task so a later drop on the same client can start a fresh reconnect cycle.
    """
    if getattr(client, "_reconnect_task", None) is not None and not _reconnect_task_alive(client) and not in_reconnect_loop(client):
        client._reconnect_task = None


def _set_abort(client: Any) -> None:
    abort = getattr(client, "_reconnect_abort", None)
    if abort is not None:
        abort.set()


def _warn_abort_wait_expired() -> None:
    _logger.warning(
        f"中止自动重连后 {_ABORT_WAIT_TIMEOUT:.0f}s 内在途尝试仍未结束：不再等待（入房意图已清，中止位保持置位）"
        f" / reconnect abort wait expired; returning with the abort flag still set",
    )


def _user_disconnect_in_reconnect_window(client: Any) -> bool:
    """「宿主在重连窗口内断开」——**唯一**需要补全语义的情形。

    其余三种一律原样走上游：① 已连接 ⇒ 上游真断开，断连钩子（reason=client disconnect）自会清意图；
    ② 来自重连任务**内部** ⇒ 这是上游自己的清理（``connect()`` 在 namespace 未就绪时会调 ``self.disconnect()``），
    **绝不能**当成用户断开去中止重连 / 清意图——否则一次瞬时握手失败就会终结自愈（实测
    ``test_auth_provider_transient_failure_self_heals`` 因此变红）；③ 无连接也无在途重连 ⇒ 上游本就无副作用
    （首连失败的内部清理即走这里，预置的入房意图须保留给宿主重试）。
    Only a host disconnect during the reconnect window needs extra semantics; every other case is upstream's.
    """
    return not client.connected and _reconnect_task_alive(client) and not in_reconnect_loop(client)


async def adisconnect_aborting_reconnect(
    client: Any,
    disconnect: Callable[[], Awaitable[None]],
    on_abort: Callable[[], None],
) -> None:
    """async 版「真正的断开」：重连窗口内先 ``on_abort()``（清入房意图）、再中止重连并等其收敛。

    Args:
        client: socketio ``AsyncClient`` 实例（SDK 子类）。
        disconnect: 上游 ``AsyncClient.disconnect`` 绑定到 ``client`` 的调用（避免递归回覆写）。
        on_abort: 中止前的同步回调（清意图 + 会话边界）。**必须先于中止**：中止位只在两次尝试之间检查，
            在途的那次尝试可能恰好成功——意图已清，连接钩子就不会回房。

    收敛后若那次尝试恰好成功，上游 ``disconnect`` 会真正断开——调用方要的是「断开」这一终态。
    其它情形原样委托上游（见 :func:`_user_disconnect_in_reconnect_window`）。
    """
    if _user_disconnect_in_reconnect_window(client):
        on_abort()
        task = client._reconnect_task
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ABORT_WAIT_TIMEOUT
        while not task.done():
            if loop.time() >= deadline:
                _warn_abort_wait_expired()
                break
            _set_abort(client)
            await asyncio.wait({task}, timeout=_ABORT_POLL_INTERVAL)
    await disconnect()


def disconnect_aborting_reconnect(client: Any, disconnect: Callable[[], None], on_abort: Callable[[], None]) -> None:
    """sync 版：同 :func:`adisconnect_aborting_reconnect`（重连任务是线程，``join`` 等其收敛）。"""
    if _user_disconnect_in_reconnect_window(client):
        on_abort()
        task: threading.Thread = client._reconnect_task
        deadline = time.monotonic() + _ABORT_WAIT_TIMEOUT
        while task.is_alive():
            if time.monotonic() >= deadline:
                _warn_abort_wait_expired()
                break
            _set_abort(client)
            task.join(_ABORT_POLL_INTERVAL)
    disconnect()


def abort_reconnect_on_version_error(client: Any, logger: logging.Logger, detail: str) -> BaseException:
    """重连循环内遇到 4008：置中止位 + ERROR 日志，返回应交还给上游循环的 ``ConnectionError``。

    上游循环吞掉它后在下一拍看到中止位 ⇒ 触发 ``__disconnect_final`` ⇒ SDK 既有钩子清空入房意图。
    **不在这里调 ``disconnect()``**：本函数运行在重连任务内部，而 SDK 覆写的 ``disconnect()`` 会等待
    重连任务收敛（自等待）；此刻 eio 本就未连接，也无需再断。
    Called from inside the reconnect loop: returns the ``ConnectionError`` the loop swallows before it
    observes the abort flag and fires ``__disconnect_final``.
    """
    _set_abort(client)
    logger.error(
        f"自动重连被服务端以协议版本不兼容（4008）拒绝，停止重连并清空入房意图（不得静默重试，versioning.md §4）: {detail}"
        f" / auto-reconnect rejected with 4008; reconnection stopped",
    )
    return cast(BaseException, SioConnectionError(f"protocol version incompatible (4008): {detail}"))


def translate_reconnect_attempt_error(client: Any, exc: BaseException, logger: logging.Logger) -> BaseException | None:
    """``connect()`` 覆写的 except 分支调用：若本次尝试来自上游重连循环，返回应抛给循环的异常。

    - 非重连循环（首连 / 宿主手工重连）⇒ ``None``，调用方按原逻辑处理（4008 → ``ProtocolVersionError``）；
    - 4008 ⇒ :func:`abort_reconnect_on_version_error`（中止重连、清意图，不再重试）；
    - 其它已是 ``ConnectionError`` ⇒ 原样（调用方应 ``raise`` 原异常）；
    - 其它握手错误（如裸 ``RuntimeError``）⇒ 规整为 ``ConnectionError``——上游循环只吞 ``ConnectionError`` /
      ``ValueError``，漏网异常会让重连任务带异常死亡、``__disconnect_final`` 永不触发。

    / Map a failed reconnect attempt onto what socketio's loop can swallow; ``None`` outside the loop.
    """
    if not in_reconnect_loop(client):
        return None
    payload = extract_4008_payload(exc)
    if payload is not None:
        return abort_reconnect_on_version_error(client, logger, str(payload.get("message", payload)))
    if isinstance(exc, SioConnectionError):
        return cast(BaseException, exc)
    return cast(BaseException, SioConnectionError(str(exc)))
