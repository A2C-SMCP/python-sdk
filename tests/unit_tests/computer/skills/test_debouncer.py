# -*- coding: utf-8 -*-
# filename: test_debouncer.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SkillEventDebouncer 单元测试（v0.2.1，#67）/ Unit tests for SkillEventDebouncer。

设计依据 / Design: docs/design-0.2.1-cli-marketplace-ux.md §8.1 / §8.2。

测试意图 / Test intentions:
- 窗口到期触发：invalidate → emit（顺序）；多次 mark_dirty 窗口内合并为单次 emit（末次胜出）；
- aflush 确定性结算挂起窗口（免 sleep）；无挂起则 no-op（不凭空 emit）；
- emit 失败 ERROR + 单次重试；invalidate 失败不阻断 emit；
- aclose 取消挂起、丢弃未结算 emit；无 invalidate 回调也能 emit。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import a2c_smcp.computer.skills.debouncer as debouncer_mod
from a2c_smcp.computer.skills.debouncer import SkillEventDebouncer


class _Recorder:
    """记录 invalidate/emit 调用次数与顺序；可配置 emit 前 N 次失败 / Records calls; emit fails first N times。"""

    def __init__(self, *, emit_fail_times: int = 0, invalidate_raises: bool = False) -> None:
        self.order: list[str] = []
        self.emit_calls = 0
        self.invalidate_calls = 0
        self._emit_fail_times = emit_fail_times
        self._invalidate_raises = invalidate_raises

    async def emit(self) -> None:
        self.emit_calls += 1
        self.order.append("emit")
        if self.emit_calls <= self._emit_fail_times:
            raise RuntimeError("emit boom")

    async def invalidate(self) -> None:
        self.invalidate_calls += 1
        self.order.append("invalidate")
        if self._invalidate_raises:
            raise RuntimeError("invalidate boom")


# ── 窗口结算 + 合并 / window settlement + coalescing ─────────────────────────
async def test_emits_after_window() -> None:
    rec = _Recorder()
    deb = SkillEventDebouncer(rec.emit, invalidate=rec.invalidate, window_ms=40)
    deb.mark_dirty()
    await asyncio.sleep(0.12)
    assert rec.emit_calls == 1
    assert rec.invalidate_calls == 1
    assert rec.order == ["invalidate", "emit"]  # 先失效后 emit


async def test_resched_delays_emit() -> None:
    # 窗口内再次 mark_dirty → 取消前次、重排（末次胜出），原窗口到期不应 emit。
    rec = _Recorder()
    deb = SkillEventDebouncer(rec.emit, invalidate=rec.invalidate, window_ms=80)
    deb.mark_dirty()
    await asyncio.sleep(0.04)
    deb.mark_dirty()  # 重排：从此刻起再等 80ms
    await asyncio.sleep(0.04)  # 距第二次仅 40ms < 80ms，且第一次已被取消
    assert rec.emit_calls == 0
    await asyncio.sleep(0.12)
    assert rec.emit_calls == 1  # 仅第二次窗口结算


async def test_multiple_marks_coalesce_single_emit() -> None:
    rec = _Recorder()
    deb = SkillEventDebouncer(rec.emit, invalidate=rec.invalidate, window_ms=10_000)
    deb.mark_dirty()
    deb.mark_dirty()
    deb.mark_dirty()
    await deb.aflush()  # 确定性结算（免等 10s）
    assert rec.emit_calls == 1
    assert rec.invalidate_calls == 1


# ── aflush ───────────────────────────────────────────────────────────────────
async def test_aflush_no_pending_is_noop() -> None:
    rec = _Recorder()
    deb = SkillEventDebouncer(rec.emit, invalidate=rec.invalidate, window_ms=50)
    await deb.aflush()  # 从未 mark_dirty → 不应 emit
    assert rec.emit_calls == 0
    assert rec.invalidate_calls == 0


async def test_aflush_settles_pending_immediately() -> None:
    rec = _Recorder()
    deb = SkillEventDebouncer(rec.emit, invalidate=rec.invalidate, window_ms=10_000)
    deb.mark_dirty()
    await deb.aflush()  # 立即结算，无需等 10s 窗口
    assert rec.emit_calls == 1
    # 结算后再 aflush 为 no-op（无新挂起）
    await deb.aflush()
    assert rec.emit_calls == 1


# ── 失败降级 / failure isolation ─────────────────────────────────────────────
async def test_emit_failure_retried_once_then_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    rec = _Recorder(emit_fail_times=1)  # 首次失败、重试成功
    deb = SkillEventDebouncer(rec.emit, window_ms=5_000)
    deb.mark_dirty()
    debouncer_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR)
    try:
        await deb.aflush()  # 不应抛
    finally:
        debouncer_mod.logger.removeHandler(caplog.handler)
    assert rec.emit_calls == 2  # 失败一次 + 重试一次
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_emit_failure_retry_also_fails_swallowed() -> None:
    rec = _Recorder(emit_fail_times=99)  # 始终失败
    deb = SkillEventDebouncer(rec.emit, window_ms=5_000)
    deb.mark_dirty()
    await deb.aflush()  # 重试仍失败 → 吞掉，不抛
    assert rec.emit_calls == 2  # 仅重试一次（共两次）


async def test_invalidate_failure_does_not_block_emit() -> None:
    rec = _Recorder(invalidate_raises=True)
    deb = SkillEventDebouncer(rec.emit, invalidate=rec.invalidate, window_ms=5_000)
    deb.mark_dirty()
    await deb.aflush()
    assert rec.invalidate_calls == 1
    assert rec.emit_calls == 1  # invalidate 抛了，emit 仍执行


async def test_no_invalidate_callback_emits() -> None:
    rec = _Recorder()
    deb = SkillEventDebouncer(rec.emit, window_ms=5_000)  # invalidate=None
    deb.mark_dirty()
    await deb.aflush()
    assert rec.emit_calls == 1
    assert rec.invalidate_calls == 0


# ── aclose ─────────────────────────────────────────────────────────────────
async def test_aclose_cancels_pending_no_emit() -> None:
    rec = _Recorder()
    deb = SkillEventDebouncer(rec.emit, invalidate=rec.invalidate, window_ms=10_000)
    deb.mark_dirty()
    await deb.aclose()  # 丢弃挂起 emit
    assert rec.emit_calls == 0
    # 给被取消的 task 一个调度机会，确认无延迟 emit 漏出
    await asyncio.sleep(0.02)
    assert rec.emit_calls == 0


async def test_aclose_idempotent_without_pending() -> None:
    rec = _Recorder()
    deb = SkillEventDebouncer(rec.emit, window_ms=50)
    await deb.aclose()  # 无挂起
    await deb.aclose()  # 重复
    assert rec.emit_calls == 0
