# -*- coding: utf-8 -*-
# filename: debouncer.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 事件去抖器：缓存失效 + 300ms debounce + 单次 emit（v0.2.1）
SKILL event debouncer: cache invalidation + 300ms debounce + single emit (v0.2.1)

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §8.1 / §8.2
                   （借鉴 Claude Code ``clearAllCaches`` + ``skillChangeDetector`` 300ms debounce）。

多源 SKILL 变更（mcp ``ResourceListChanged`` / user 源文件 watcher / 后续 CLI marketplace·plugin 操作）
统一喂给本去抖器的 :meth:`mark_dirty`，在 ``window_ms``（默认 300ms）窗口内合并为**一次** emit：
1. 窗口到期 → 先 ``invalidate()`` 做缓存失效（重扫文件源等），再 ``on_emit()`` 推送 ``server:update_skills``；
2. 窗口内多次 ``mark_dirty`` → 取消未到期 task、重排，**末次胜出**（避免抖动期间多次广播）；
3. ``on_emit`` 失败 → ERROR 日志 + **单次重试**（设计 §12「emit 失败重试」），仍失败则放弃本轮。
All SKILL-change sources funnel into :meth:`mark_dirty`; events within ``window_ms`` coalesce into a
single ``invalidate → emit`` settlement (latest-wins; emit failure retried once then dropped).

线程模型 / Threading: 本去抖器假定在 Computer 单 asyncio 事件循环线程内被驱动；来自其它线程（如 watchdog
观察者线程）的触发**必须**先经 ``loop.call_soon_threadsafe`` marshal 回事件循环线程，再调 :meth:`mark_dirty`
（见 :class:`~a2c_smcp.computer.skills.watcher.SkillFileWatcher` 的接入）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# 默认去抖窗口（毫秒）/ Default debounce window (ms)。对齐 CC skillChangeDetector 的 300ms。
DEFAULT_DEBOUNCE_MS = 300

# 结算回调类型 / Settlement callback types。
EmitCallback = Callable[[], Awaitable[None]]
InvalidateCallback = Callable[[], Awaitable[None]]


class SkillEventDebouncer:
    """
    SKILL 变更去抖器 / SKILL-change debouncer（设计 §8.2）。

    :param on_emit: 结算末端的 emit 协程（通常 → ``client.emit_update_skills``）；无副作用幂等更佳。
    :param invalidate: 结算前的缓存失效协程（重扫文件源 / 清缓存）；``None`` 表示无需失效，直接 emit。
    :param window_ms: 去抖窗口毫秒数（默认 :data:`DEFAULT_DEBOUNCE_MS`）；``<=0`` 表示下个事件循环 tick 即结算。
    """

    def __init__(
        self,
        on_emit: EmitCallback,
        *,
        invalidate: InvalidateCallback | None = None,
        window_ms: int = DEFAULT_DEBOUNCE_MS,
    ) -> None:
        self._on_emit = on_emit
        self._invalidate = invalidate
        self._window_ms = max(0, window_ms)
        self._task: asyncio.Task[None] | None = None

    def mark_dirty(self) -> None:
        """
        标脏并重排一次结算 / Mark dirty and (re)schedule a settlement。

        窗口内多次调用 → 取消未到期 task、重排，**末次胜出**（多事件合并为一次 emit）。
        必须在事件循环线程内调用（跨线程触发先经 ``loop.call_soon_threadsafe``）。
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._settle_after_delay())

    async def _settle_after_delay(self) -> None:
        """等待去抖窗口后结算；窗口内被新的 :meth:`mark_dirty` 取消则放弃本次（末次胜出）。"""
        await asyncio.sleep(self._window_ms / 1000)
        await self._run_settlement()

    async def _run_settlement(self) -> None:
        """结算一次：先缓存失效（失败不阻断 emit），再 emit（失败 ERROR + 单次重试）。"""
        if self._invalidate is not None:
            try:
                await self._invalidate()
            except Exception as e:
                logger.error("SKILL 缓存失效失败（仍继续 emit）/ invalidate failed (emit anyway): %s", e, exc_info=True)
        try:
            await self._on_emit()
        except Exception as e:
            logger.error("emit_update_skills 失败，重试一次 / emit failed, retrying once: %s", e, exc_info=True)
            try:
                await self._on_emit()
            except Exception as e2:
                logger.error("emit_update_skills 重试仍失败，放弃本轮 / emit retry failed, giving up: %s", e2, exc_info=True)

    async def aflush(self) -> None:
        """
        立即结算挂起窗口 / Settle the pending window immediately。

        取消去抖等待，直接 ``invalidate → emit``；**无挂起则 no-op**（不会凭空 emit）。
        主要供单元测试**确定性结算**（免 ``sleep`` 猜时）；停机不走本方法——:meth:`Computer.shutdown` 用
        :meth:`aclose` **丢弃**挂起 emit（停机时无需再广播）。
        Mainly for deterministic settlement in tests; shutdown uses :meth:`aclose` (drops the pending emit).
        """
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        self._task = None
        await self._run_settlement()

    async def aclose(self) -> None:
        """
        关闭去抖器，丢弃未结算的挂起 emit / Close the debouncer, dropping any pending unsettled emit。

        生命周期清理（停机）：取消挂起 task 并等待其结束（幂等）。**不**冲刷——停机时无需再广播。
        """
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
