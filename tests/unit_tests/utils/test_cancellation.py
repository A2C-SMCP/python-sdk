# -*- coding: utf-8 -*-
# filename: test_cancellation.py
# @Author  : JQQ
# @Software: PyCharm
"""单元测试（#211）：外部取消信号的还原工具。

判据是**可观测性**：下游按设计吞掉取消后，宿主必须仍能通过 ``asyncio.wait_for`` / ``asyncio.timeout``
拿到 ``TimeoutError``（还原前表现为协程「正常返回」，宿主完全无感）。
"""

from __future__ import annotations

import asyncio

import pytest

from a2c_smcp.utils.cancellation import restores_cancellation


@restores_cancellation
async def _entry_swallowing_cancel() -> None:
    """模拟下游按设计吞掉取消（transitions ``process_context`` / ``contextlib.suppress`` 的形态）。"""
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        pass


@restores_cancellation
async def _entry_propagating_cancel() -> None:
    await asyncio.sleep(10)


@restores_cancellation
async def _entry_normal() -> str:
    await asyncio.sleep(0)
    return "ok"


@restores_cancellation
async def _inner_swallowing_cancel(sink: list[str]) -> None:
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        pass
    sink.append("inner-done")


@restores_cancellation
async def _outer_nested(sink: list[str]) -> None:
    await _inner_swallowing_cancel(sink)
    sink.append("outer-trailer-ran")  # 外层收尾：内层若在此前补抛就会被跳过


@pytest.mark.asyncio
async def test_swallowed_cancel_is_restored_as_timeout() -> None:
    """被吞的取消须在入口收尾处还原：宿主拿到 TimeoutError，而非「正常返回」。"""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_entry_swallowing_cancel(), timeout=0.1)


@pytest.mark.asyncio
async def test_normal_completion_is_untouched() -> None:
    """阴性对照：无取消时行为与返回值完全不变（不得凭空抛错）。"""
    assert await _entry_normal() == "ok"


@pytest.mark.asyncio
async def test_propagated_cancel_is_unchanged() -> None:
    """取消正常传播时保持原样（宿主直接 task.cancel() 的形态：任务呈已取消）。"""
    task = asyncio.create_task(_entry_propagating_cancel())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@restores_cancellation
async def _spawned_leaf() -> None:
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        pass  # 吞掉投给**本任务**的取消


@restores_cancellation
async def _spawner(sink: list[asyncio.Task[None]]) -> None:
    sink.append(asyncio.create_task(_spawned_leaf()))
    await sink[0]


def test_public_lifecycle_entries_are_decorated() -> None:
    """结构性护栏：宿主可 await 的生命周期入口必须都挂 ``@restores_cancellation``。

    行为用例只覆盖代表性路径（``shutdown``/``boot_up``/``aclose``/``astop_all``/``astart_all``），
    逐入口再写一份会重复；此处按模块 docstring 的「装饰面判据」把清单钉住，避免后人新增入口时漏挂。
    """
    from a2c_smcp.computer.computer import Computer
    from a2c_smcp.computer.mcp_clients.manager import MCPServerManager

    required = {
        Computer: ("boot_up", "shutdown"),
        MCPServerManager: ("aclose", "astart_all", "astart_client", "astop_all", "astop_client"),
    }
    missing = [
        f"{cls.__name__}.{name}"
        for cls, names in required.items()
        for name in names
        if not hasattr(getattr(cls, name), "__wrapped__")
    ]
    assert not missing, f"以下生命周期入口未挂 @restores_cancellation: {missing}"


@pytest.mark.asyncio
async def test_spawned_task_entry_restores_for_its_own_task() -> None:
    """跨任务边界：被装饰入口在**另一个被装饰入口 spawn 的任务**里运行时，仍须按该任务的最外层补抛。

    上下文会被 ``create_task`` 复制，若判据只看纯深度计数，子任务里会读到父任务的深度而永不补抛 ——
    投给子任务的取消就此静默丢失（实测该退化形态）。判据按「当前任务」归属即无此洞。
    """
    tasks: list[asyncio.Task[None]] = []
    outer = asyncio.create_task(_spawner(tasks))
    while not tasks:  # 等子任务真的被创建出来
        await asyncio.sleep(0.01)

    tasks[0].cancel()  # 取消直接投给**子任务**
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]

    assert tasks[0].cancelled(), "子任务内被吞的取消必须还原（否则该取消静默丢失）"
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer


@pytest.mark.asyncio
async def test_only_outermost_nested_entry_restores() -> None:
    """嵌套装饰：**只有最外层**补抛 —— 内层补抛会中断外层收尾。

    这正是「装饰点必须选最外层入口」的机器化判据：若内层先补抛，``outer-trailer-ran`` 不会出现，
    等价于 ``_astop_all`` 循环中途抛出 → 剩余 client 不再被停止。
    """
    sink: list[str] = []
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_outer_nested(sink), timeout=0.1)

    assert sink == ["inner-done", "outer-trailer-ran"], (
        f"内层不得补抛中断外层收尾；实测轨迹 {sink}"
    )
