# -*- coding: utf-8 -*-
# filename: start_gate.py
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
MCP 启动并发门（#208），逐语义对齐 rust-sdk ``crates/smcp-computer/src/mcp_start_gate.rs``。

**职责单一**：约束「同时有多少个 MCP 启动事务在途」。单个启动、批量启动与 Plugin 治理恢复
共享同一个门，故任意时刻总在途启动数 ≤ 配置上限（执行权属于 SDK，调用方不得另设第二套限流）。

设计取舍 / Design notes:

- **手写 FIFO 等待队列，不用 ``asyncio.Semaphore``。** ``Semaphore`` 没有 ``close()``，
  要满足「关闸即令排队者失败」仍须自写唤醒逻辑（等于没买）；更关键的是，取消一个 pending 的
  ``Semaphore.acquire()`` 依赖 CPython 内部的许可补偿路径——**许可记账 bug 恰好会长在本模块
  唯一要保证的不变量（上限）上**。手写队列把这段记账摊开在可测的几十行里。
- **无 detached task**：许可由调用方自己的 future 持有并通过 :meth:`StartPermit.release`
  归还（rust 靠 ``Drop``，本类另提供 async 上下文管理器以获异常安全）。
- **原子交接**：释放时把腾出的许可**直接交给队首**（``in_flight`` 上抬发生在唤醒之前），
  被唤醒者不复查容量 ⇒ 无惊群、严格 FIFO（同 tokio ``Semaphore`` 语义）。
- **未配置 = 不限流但仍计数**（``max is None``，对应 rust ``Semaphore::MAX_PERMITS`` +
  ``AtomicUsize``）：使 ``drain()`` 与「关闸令排队者即刻失败」在串行路径上同样成立。
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

GATE_CLOSED_MESSAGE = "MCP startup gate is closed (Computer is shutting down)"
"""关闸文案（与 rust ``InvalidState`` 同文案，便于双端排障对照）。"""


class McpStartGateClosed(RuntimeError):
    """门已关闭：新启动或排队中的启动被拒（镜像 rust ``ComputerError::InvalidState``）。

    ``RuntimeError`` 子类以便既有 ``except Exception`` 收尾路径照常捕获；
    判别用 ``isinstance`` / ``except McpStartGateClosed``。

    / Raised for new or queued starts after the gate is closed (mirrors rust's ``InvalidState``).
    """


class StartPermit:
    """一次启动事务的许可（rust ``StartPermit``）。由 :meth:`McpStartGate.acquire` 返回。

    释放**幂等**；推荐 ``async with await gate.acquire():`` 以获得异常安全。

    / A start-transaction permit; release is idempotent (rust releases via ``Drop``).
    """

    __slots__ = ("_gate", "_released")

    def __init__(self, gate: McpStartGate) -> None:
        self._gate = gate
        self._released = False

    def release(self) -> None:
        """归还许可（幂等）。/ Release the permit; idempotent."""
        if self._released:
            return
        self._released = True
        self._gate._release()

    async def __aenter__(self) -> StartPermit:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.release()


class McpStartGate:
    """Computer 级 MCP 启动并发门。/ Computer-level bounded-concurrency gate for MCP starts.

    Args:
        max: 最大并发启动数；``None``（缺省）= 不限流**但仍计数**。``0`` 按 ``1`` 处理。

    / ``max``: cap, or ``None`` for "uncapped but still counted"; ``0`` is clamped to 1.
    """

    def __init__(self, max: int | None = None) -> None:
        self._max: int | None = None
        self._in_flight = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._closed = False
        self._started = False
        # _idle 置位 ⇔ in_flight == 0（drain 的等待条件，见 _release）
        self._idle = asyncio.Event()
        self._idle.set()
        if max is not None:
            self.configure(max_concurrency=max)

    # ────────────────────── 只读观测 / Observability ──────────────────────

    @property
    def max(self) -> int | None:
        """当前上限；``None`` = 未配置（不限流但仍计数）。/ The cap, or ``None`` when unconfigured."""
        return self._max

    @property
    def in_flight(self) -> int:
        """在途启动事务数（不含排队者）。/ Number of in-flight start transactions (excludes queued)."""
        return self._in_flight

    def is_closed(self) -> bool:
        """是否已关闸。/ Whether the gate has been closed."""
        return self._closed

    # ────────────────────── 配置 / Configuration ──────────────────────

    def configure(self, max_concurrency: int) -> None:
        """安装/替换并发上限（**构造期**策略，一次生效；``0`` 按 ``1`` 处理）。

        🔴 **fail-closed**：任何 acquire **尝试**（含随后失败的，如关闸后的拒绝）之后调用即抛
        ``RuntimeError`` —— 运行期替换会让已在旧队列上排队的等待者脱离新上限乃至悬挂。
        配置必须在启动前完成。（rust 对应处以 panic 实现。）

        / Construction-time policy. Fails closed after ANY acquire attempt; ``0`` clamps to 1.
        """
        if self._started:
            raise RuntimeError(
                "MCP start concurrency must be configured at construction time, "
                "before any start transaction"
            )
        # 手写夹取（形参名不占用 builtins.max），对齐 rust `max.max(1)`
        self._max = max_concurrency if max_concurrency > 1 else 1

    # ────────────────────── 许可 / Permits ──────────────────────

    async def acquire(self) -> StartPermit:
        """取得许可：关闸 → 立即抛；达上限 → 排队等待（队首先得）；否则立即返回。

        / Acquire a permit; raises immediately when closed, otherwise queues at the cap.
        """
        # rust 在 acquire 顶部置 started（**含随后失败的路径**）⇒ 失败的 acquire 也锁死构造期
        self._started = True
        if self._closed:
            raise McpStartGateClosed(GATE_CLOSED_MESSAGE)
        if self._max is None or self._in_flight < self._max:
            self._in_flight += 1
            self._idle.clear()
            return StartPermit(self)

        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if self._detach_waiter(waiter):
                # 仍在队列中 ⇒ 从未获得许可，无需归还
                raise
            # 已不在队列：要么被 close 置了异常（从未授予），要么已原子交接。
            # 交接过的必须归还，否则许可永久泄漏（在途计数虚高 → 上限静默收紧）。
            if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                self._release()
            raise
        return StartPermit(self)

    def _detach_waiter(self, waiter: asyncio.Future[None]) -> bool:
        """把等待者从队列摘除（取消路径）。返回是否真的摘到了。/ Detach a waiter; True if found."""
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return False
        return True

    def _release(self) -> None:
        """归还一个许可：优先**原子交接**给队首；无人排队且已排空则置 idle。/ Release or hand off."""
        self._in_flight -= 1
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done() or waiter.cancelled():
                continue  # 已被取消/失败的残留项，跳过
            # 交接发生在唤醒之前：被唤醒者不再复查容量（无惊群、严格 FIFO）
            self._in_flight += 1
            waiter.set_result(None)
            return
        if self._in_flight == 0:
            self._idle.set()

    # ────────────────────── 关闭与收敛 / Close & drain ──────────────────────

    def close(self) -> None:
        """终态关闭：**不再接纳**新许可，**全部排队者即刻失败**（不等待许可释放）。

        幂等。**已授予的许可依然有效**——在途事务不被中断、不被取消，其收敛由 :meth:`drain`
        等待（对齐 rust：``close()`` 关的是取号口，不是正在办理的业务）。

        / Terminal close: idempotent; queued waiters fail immediately; in-flight permits stay valid.
        """
        if self._closed:
            return
        self._closed = True
        waiters = list(self._waiters)
        self._waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_exception(McpStartGateClosed(GATE_CLOSED_MESSAGE))

    async def drain(self) -> None:
        """等待在途启动事务收敛（``in_flight == 0``）。

        **调用方不得持任何锁**：在途事务可能正等待状态锁，持锁等待 = 自死锁。
        （rust 以 ``mcp_lifecycle_gate.write()`` 达成同效。）

        / Wait until in-flight starts converge. The caller must hold no lock.
        """
        while self._in_flight > 0:
            await self._idle.wait()
            self._idle.clear()
