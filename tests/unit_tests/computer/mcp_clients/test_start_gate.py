# -*- coding: utf-8 -*-
# filename: test_start_gate.py
# @Author  : JQQ
# @Software: PyCharm
"""`McpStartGate` 单元测试（#208，镜像 rust `mcp_start_gate.rs`）。

门是本次并发启动特性的**唯一**上限承载件，故其不变量被测在最前面：

1. 未配置（``max is None``）= 不限流**但仍计数** —— ``drain()`` 与「排队者即刻失败」两条语义
   在串行路径上同样必须成立（rust 用 ``Semaphore::MAX_PERMITS`` + ``AtomicUsize`` 达成同效）。
2. 上限是**硬**的：任意时刻 ``in_flight <= max``，被唤醒者**原子接手**许可、不复查容量。
3. ``configure`` 是**构造期**策略：任何 acquire 尝试之后调用即 fail-closed（rust 为 panic，
   python 抛异常——仓内不变量用抛不用 assert，``-O`` 会剥离 assert）。
4. ``close()`` 令**排队者**即刻失败，但**不中断在途**（收敛是 ``drain()`` 的职责）。

确定性口径：负向断言（「它还没被放行」）一律用**计数器 + 少量 ``sleep(0)`` tick**，
不用任意墙钟 sleep —— 门的快路径无挂起点，tick 足以让任何被错误放行的任务跑起来。
"""

from __future__ import annotations

import asyncio

import pytest

from a2c_smcp.computer.mcp_clients.start_gate import (
    McpStartGate,
    McpStartGateClosed,
)


async def _tick(times: int = 3) -> None:
    """让出事件循环若干次，使已就绪的任务跑到其下一个挂起点。

    ``create_task`` 的任务在首次 ``sleep(0)`` 才被调度；给足 3 次以覆盖
    「任务内部先 await 一次再入队」这类实现差异。

    / Yield to the loop a few times so ready tasks reach their next suspension point.
    """
    for _ in range(times):
        await asyncio.sleep(0)


# ────────────────────── 基本计数与上限 / Counting & cap ──────────────────────


@pytest.mark.asyncio
async def test_default_uncapped_but_still_counted() -> None:
    """未配置上限 = 不限流，但 in_flight 仍须如实计数（drain 与 close 语义的前提）。"""
    gate = McpStartGate()
    assert gate.max is None
    assert gate.in_flight == 0

    permit = await gate.acquire()
    assert gate.in_flight == 1
    # 不限流：第二个不需等待
    permit2 = await gate.acquire()
    assert gate.in_flight == 2

    permit.release()
    assert gate.in_flight == 1
    permit2.release()
    assert gate.in_flight == 0


@pytest.mark.asyncio
async def test_cap_enforced_and_released_in_fifo_order() -> None:
    """cap=2：第三个必须等待；释放一个后**按 FIFO** 放行恰好一个。"""
    gate = McpStartGate(max=2)
    assert gate.max == 2

    first = await gate.acquire()
    second = await gate.acquire()
    assert gate.in_flight == 2

    third_task = asyncio.create_task(gate.acquire())
    fourth_task = asyncio.create_task(gate.acquire())
    await _tick()
    assert not third_task.done(), "达上限时第三个 acquire 不得被放行"
    assert not fourth_task.done()
    assert gate.in_flight == 2, "排队者不得计入 in_flight"

    first.release()
    await _tick()
    assert third_task.done(), "释放后须唤醒队首"
    assert not fourth_task.done(), "一次释放只放行一个"
    assert gate.in_flight == 2

    third = third_task.result()
    third.release()
    await _tick()
    assert fourth_task.done()
    assert gate.in_flight == 2

    second.release()
    fourth_task.result().release()
    assert gate.in_flight == 0


@pytest.mark.asyncio
async def test_permit_release_is_idempotent() -> None:
    """重复释放不得把 in_flight 减成负数、也不得凭空多放行一个排队者。"""
    gate = McpStartGate(max=1)
    permit = await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    await _tick()
    assert not waiter.done()

    permit.release()
    permit.release()  # 幂等
    assert gate.in_flight == 1, "重复释放不得多发许可"

    await _tick()
    assert waiter.done()
    waiter.result().release()
    assert gate.in_flight == 0


@pytest.mark.asyncio
async def test_async_context_manager_releases_on_exception() -> None:
    """``async with await gate.acquire()`` 在异常路径也必须释放（rust 靠 Drop）。"""
    gate = McpStartGate(max=1)
    with pytest.raises(ValueError, match="boom"):
        async with await gate.acquire():
            assert gate.in_flight == 1
            raise ValueError("boom")
    assert gate.in_flight == 0

    # 释放后可立即再次获取
    async with await gate.acquire():
        assert gate.in_flight == 1


# ────────────────────── configure：构造期、fail-closed ──────────────────────


@pytest.mark.asyncio
async def test_configure_clamps_zero_to_one() -> None:
    """``0`` 意味着「永不启动」无意义 ⇒ 按 1 处理（镜像 rust ``max.max(1)``）。"""
    gate = McpStartGate(max=5)
    gate.configure(0)
    assert gate.max == 1

    gate2 = McpStartGate()
    gate2.configure(0)
    assert gate2.max == 1


@pytest.mark.asyncio
async def test_configure_after_any_acquire_attempt_raises() -> None:
    """任何 acquire **尝试**（含随后失败的）之后 configure 必须 fail-closed。

    与 rust `set_max_after_first_acquire_panics` 同构：运行期换门会让已在旧门上排队的
    等待者脱离新上限乃至悬挂，故配置必须在启动前完成。
    """
    gate = McpStartGate()
    await gate.acquire()
    with pytest.raises(RuntimeError, match="before"):
        gate.configure(3)


@pytest.mark.asyncio
async def test_configure_after_failed_acquire_also_raises() -> None:
    """关闭后的**失败** acquire 同样锁死构造期（rust 单测显式断言这条）。"""
    gate = McpStartGate()
    gate.close()
    with pytest.raises(McpStartGateClosed):
        await gate.acquire()
    with pytest.raises(RuntimeError, match="before"):
        gate.configure(3)


@pytest.mark.asyncio
async def test_configure_before_any_acquire_is_allowed() -> None:
    """构造期（尚未 acquire）允许安装/替换上限。"""
    gate = McpStartGate()
    gate.configure(4)
    assert gate.max == 4
    gate.configure(2)
    assert gate.max == 2


# ────────────────────── close / drain ──────────────────────


@pytest.mark.asyncio
async def test_close_fails_queued_waiters_but_not_in_flight() -> None:
    """``close()``：排队者**即刻**失败；在途许可**依然有效**（不中断）。"""
    gate = McpStartGate(max=1)
    inflight = await gate.acquire()
    queued = asyncio.create_task(gate.acquire())
    await _tick()
    assert not queued.done()

    gate.close()
    assert gate.is_closed()
    await _tick()
    assert queued.done(), "close 必须让排队者即刻失败，而非等许可释放"
    with pytest.raises(McpStartGateClosed):
        queued.result()

    # 在途许可不受影响：仍能释放，计数归零
    assert gate.in_flight == 1
    inflight.release()
    assert gate.in_flight == 0


@pytest.mark.asyncio
async def test_close_is_idempotent_and_acquire_after_close_raises() -> None:
    gate = McpStartGate()
    gate.close()
    gate.close()  # 幂等
    with pytest.raises(McpStartGateClosed):
        await gate.acquire()


@pytest.mark.asyncio
async def test_close_drains_all_waiters_even_when_uncapped() -> None:
    """未配置上限时也用同一路径：排队只可能由「已达上限」造成，这里以 cap=0→1 构造。"""
    gate = McpStartGate(max=1)
    held = await gate.acquire()
    waiters = [asyncio.create_task(gate.acquire()) for _ in range(4)]
    await _tick()
    assert all(not t.done() for t in waiters)

    gate.close()
    await _tick()
    for t in waiters:
        assert t.done()
        with pytest.raises(McpStartGateClosed):
            t.result()

    held.release()
    assert gate.in_flight == 0


@pytest.mark.asyncio
async def test_drain_waits_for_in_flight_to_converge() -> None:
    """``drain()`` 在 in_flight 归零前不得返回（rust `mcp_lifecycle_gate.write()` 的 python 面）。"""
    gate = McpStartGate(max=3)
    permits = [await gate.acquire() for _ in range(3)]

    drain_task = asyncio.create_task(gate.drain())
    await _tick()
    assert not drain_task.done(), "在途未收敛时 drain 不得返回"

    permits[0].release()
    await _tick()
    assert not drain_task.done(), "仍有 2 个在途"

    permits[1].release()
    permits[2].release()
    await _tick()
    assert drain_task.done(), "in_flight 归零后 drain 必须返回"


@pytest.mark.asyncio
async def test_drain_on_fresh_or_idle_gate_returns_immediately() -> None:
    """空门 drain 立即返回（boot 回滚 / 重复 aclose 的幂等前提）。"""
    gate = McpStartGate()
    await asyncio.wait_for(gate.drain(), timeout=1)

    gate2 = McpStartGate(max=2)
    permit = await gate2.acquire()
    permit.release()
    await asyncio.wait_for(gate2.drain(), timeout=1)


@pytest.mark.asyncio
async def test_close_then_drain_waits_for_in_flight_holder() -> None:
    """关闸→收敛的标准 shutdown 序列：close 不中断在途，drain 等它自然结束。"""
    gate = McpStartGate(max=2)
    permit = await gate.acquire()
    gate.close()

    drain_task = asyncio.create_task(gate.drain())
    await _tick()
    assert not drain_task.done()

    permit.release()
    await _tick()
    assert drain_task.done()


# ────────────────────── 取消安全 / Cancellation safety ──────────────────────


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_consume_or_leak_a_permit() -> None:
    """被取消的排队者：既不占许可、也不泄漏许可（后续 waiter 仍能按序被放行）。"""
    gate = McpStartGate(max=1)
    holder = await gate.acquire()

    cancelled = asyncio.create_task(gate.acquire())
    successor = asyncio.create_task(gate.acquire())
    await _tick()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert gate.in_flight == 1, "被取消的排队者不得改变在途计数"

    holder.release()
    await _tick()
    assert successor.done(), "取消者腾出的队列位置不得卡住后继"
    assert gate.in_flight == 1
    successor.result().release()
    assert gate.in_flight == 0
