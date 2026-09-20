"""
* 文件名: _cancel
* 作者: JQQ
* 版权: 2023 JQQ. All rights reserved.
* 依赖: None
* 描述: Agent 侧取消信号的轮询投递件（#209） / cancel-signal polling helpers (#209)

协议依据（protocol#58 / PR#60，`events.md §server:tool_call_cancel`「Agent 侧主动取消的终态来源」）：

1. **仅投递信号** —— 发出 ``server:tool_call_cancel`` 后**继续等待原 ``client:tool_call`` 的 ack**，
   以该 ack 为终态；**MUST NOT** 因已发出取消而本地提早返回。故本模块**只负责把信号转成一次广播**，
   不参与任何 await 的中断。
2. **不冒充 Computer** —— 结果级 ``a2c_cancelled`` 仅由 Computer 产出；本模块只发 ``AgentCallData``
   载体，不写任何结果级标记。
3. **如实透出** —— 终态分类见 :func:`a2c_smcp.agent.types.classify_tool_call_outcome`。

**一律轮询**：宿主只能提供 ``is_set()``（内核 ``CancellationToken`` 只有 ``is_cancelled`` property、
``asyncio.Event.wait()`` 在无事件循环线程下会 ``RuntimeError``、取消亦可能从另一线程打入），故实现
**不得**假定存在可 await 的 ``wait``。

English: Polling helpers that turn a :class:`~a2c_smcp.agent.types.CancelSignal` into **one**
``server:tool_call_cancel`` broadcast. Polling is mandatory — the host may only expose ``is_set()``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

from a2c_smcp.agent.types import DEFAULT_CANCEL_POLL_INTERVAL, CancelSignal
from a2c_smcp.utils.cancellation import cancel_entry_snapshot, restore_swallowed_cancel
from a2c_smcp.utils.logger import get_logger

logger = get_logger("agent")

#: sync watcher 线程名 —— 线程收口断言依赖它 / the sync watcher thread name (used by leak assertions).
CANCEL_WATCHER_THREAD_NAME = "a2c-agent-tool-cancel"

#: ``stop()`` 等待 watcher 线程收口的上界（秒）。轮询体在 ``stop`` 置位后即时退出，故实际远小于此值。
#: Upper bound (seconds) for joining the watcher thread; the poll loop exits promptly once stopped.
WATCHER_STOP_TIMEOUT: float = 1.0

#: 广播回调：sync 形态取 req_id 发一次 fire-and-forget / sync broadcast callback.
SyncBroadcast = Callable[[str], None]
#: 广播回调：async 形态 / async broadcast callback.
AsyncBroadcast = Callable[[str], Awaitable[None]]


class CancelSendGate:
    """单次取消广播闸门 —— 一次调用至多广播一次取消（#209 幂等裁决）。

    Watcher（后台线程 / 协程）与 ``emit_tool_call`` 自身的超时兜底臂都会尝试广播，二者可能同时
    到达；本闸门保证 ``try_claim()`` **至多返回一次 True**。``try_claim()`` 亦被用作「关门」：
    调用方收尾时抢占一次，使**已返回后不可能再有迟到广播**。

    / A once-only gate so a single call broadcasts at most one cancel, and never after it returned.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed = False

    def try_claim(self) -> bool:
        """抢占广播权；返回 True 表示本次调用是唯一被允许广播的一方。"""
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True


def _broadcast_once(gate: CancelSendGate, broadcast: SyncBroadcast, req_id: str) -> None:
    """抢占闸门并广播一次；广播失败**吞掉**（取消是 best-effort，绝不外泄影响原调用）。

    ``except Exception`` 天然不捕获 ``asyncio.CancelledError``（BaseException 分支），故不会吞取消。
    """
    if not gate.try_claim():
        return
    try:
        broadcast(req_id)
    except Exception as e:  # noqa: BLE001 - 取消广播 best-effort，失败不得影响原调用
        logger.warning(f"发送取消广播失败（不影响原调用）/ cancel broadcast failed (original call unaffected): {e}")


class SyncCancelWatcher:
    """同步侧取消 watcher：daemon 线程轮询 ``is_set()``，命中即广播一次并退出。

    线程名的稳定性由 :data:`CANCEL_WATCHER_THREAD_NAME` 保证（测试据此断言无泄漏）。

    / Sync-side watcher: a daemon thread polling ``is_set()``; broadcasts once and exits.
    """

    def __init__(
        self,
        signal: CancelSignal,
        gate: CancelSendGate,
        req_id: str,
        broadcast: SyncBroadcast,
        interval: float = DEFAULT_CANCEL_POLL_INTERVAL,
    ) -> None:
        self._signal = signal
        self._gate = gate
        self._req_id = req_id
        self._broadcast = broadcast
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动 watcher 线程 / start the watcher thread."""
        self._thread = threading.Thread(
            target=self._run,
            name=CANCEL_WATCHER_THREAD_NAME,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """收口：先「关门」再叫停并 join。

        **先** ``gate.try_claim()`` 是承重的：抢占广播权后 watcher 即使恰好轮询到信号也无法再广播，
        杜绝「``emit_tool_call`` 已返回后仍有迟到取消广播」的窗口。join 用有限上界——轮询体在
        ``stop`` 置位后即时退出，故实际不等待。

        / Seal the gate first, then stop and join. Sealing owns the broadcast right so no cancel can
        leak out after the call has already returned.
        """
        self._gate.try_claim()
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=WATCHER_STOP_TIMEOUT)

    def _run(self) -> None:
        """轮询体：**先查后睡** —— 进入时已置位则立即投递（不白等一个间隔）。"""
        while not self._stop.is_set():
            if self._signal.is_set():
                _broadcast_once(self._gate, self._broadcast, self._req_id)
                return
            # 用 stop 事件做等待，收口时即时唤醒（不必等满一个轮询间隔）。
            self._stop.wait(self._interval)


class AsyncCancelWatcher:
    """异步侧取消 watcher：task 轮询 ``is_set()``，命中即 ``await`` 广播一次并退出。

    收口走 :meth:`stop`：关门 → ``cancel()`` → ``await``。``await`` 期间的 ``CancelledError``
    须区分来源 —— 只吞「我们自己取消了 watcher」，外部对本次调用的取消必须如实补抛
    （#211 同族：吞掉取消会让宿主的 ``wait_for`` / ``timeout`` 静默失效）。

    / Async-side watcher; ``stop()`` distinguishes our own cancellation of the watcher from an
    external cancellation of the enclosing call, restoring the latter (see #211).
    """

    def __init__(
        self,
        signal: CancelSignal,
        gate: CancelSendGate,
        req_id: str,
        broadcast: AsyncBroadcast,
        interval: float = DEFAULT_CANCEL_POLL_INTERVAL,
    ) -> None:
        self._signal = signal
        self._gate = gate
        self._req_id = req_id
        self._broadcast = broadcast
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        # 入口快照：收尾处比对「本次调用期间是否新收到外部取消」/ entry snapshot for external-cancel restore.
        self._cancel_entry = cancel_entry_snapshot()

    def start(self) -> None:
        """启动 watcher task / start the watcher task."""
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """收口：关门 → 取消 → 等待其结束。"""
        self._gate.try_claim()
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # 我们自己取消了 watcher ⇒ 吞掉；但若本次调用期间收到了**外部**取消，必须补抛（#211）。
            restore_swallowed_cancel(self._cancel_entry)
        except Exception as e:  # noqa: BLE001 - watcher 内部异常不得顶替原调用的返回值
            logger.warning(f"取消 watcher 异常（不影响原调用）/ cancel watcher failed (call unaffected): {e}")

    async def _run(self) -> None:
        """轮询体：**先查后睡** —— 进入时已置位则立即投递（不白等一个间隔）。"""
        while True:
            if self._signal.is_set():
                if self._gate.try_claim():
                    try:
                        await self._broadcast(self._req_id)
                    except Exception as e:  # noqa: BLE001 - 取消广播 best-effort
                        logger.warning(
                            f"发送取消广播失败（不影响原调用）/ cancel broadcast failed "
                            f"(original call unaffected): {e}"
                        )
                return
            await asyncio.sleep(self._interval)
