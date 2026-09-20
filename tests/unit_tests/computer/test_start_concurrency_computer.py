# -*- coding: utf-8 -*-
# filename: test_start_concurrency_computer.py
# @Author  : JQQ
# @Software: PyCharm
"""#208 Computer 级并发策略：**Input 交互解析串行**（验收标准 5）。

manager 侧的并发矩阵见 ``mcp_clients/test_start_concurrency.py``；治理恢复的 collect-then-batch 见
``test_computer_governance_recovery.py``。此处只覆盖**只有 Computer 层才有**的那一件：
``Computer._mcp_input_lock`` —— 并发启动期间交互 resolver **一次只允许一个请求在飞**
（对齐 rust ``mcp_input_resolve_lock``；协议 §5.13 每次实际启动重解析 Input，交互式 resolver
不得并行弹多个提示）。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inputs.resolver import InputResolver
from a2c_smcp.computer.mcp_clients.model import MCPServerPromptStringInput, StdioServerConfig


class _ConcurrencyProbeResolver(InputResolver):
    """并发探针 resolver：在 ``aresolve_by_id`` 内**让出**并登记最大并发度，放行由测试显式控制。

    探针必须在内部 ``await``（门），否则并发启动根本没有交叠窗口，测不出串行与否。
    用门而非墙钟：放行时机由测试决定 ⇒ 「最大并发 == 1」是确定性结论而非时序巧合。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.active = 0
        self.max_concurrent = 0
        self.entered = 0
        self._gates: list[asyncio.Event] = []

    async def aresolve_by_id(  # type: ignore[override]
        self,
        input_id: str,
        *,
        session: Any = None,
        plugin: str | None = None,
        marketplace: str | None = None,
    ) -> Any:
        self.active += 1
        self.entered += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        gate = asyncio.Event()
        self._gates.append(gate)
        try:
            await gate.wait()
        finally:
            self.active -= 1
        return await super().aresolve_by_id(input_id, session=session, plugin=plugin, marketplace=marketplace)

    def release_pass(self, n: int) -> None:
        released = 0
        for gate in self._gates:
            if not gate.is_set():
                gate.set()
                released += 1
                if released >= n:
                    return


class _RecordingClient:
    def __init__(self, config: StdioServerConfig, message_handler: Any = None) -> None:
        self.config = config
        self.state = "stopped"
        self.list_tools = AsyncMock(return_value=[])

    async def aconnect(self) -> None:
        self.state = "connected"

    async def adisconnect(self) -> None:
        self.state = "disconnected"


def _cfg(name: str, input_id: str) -> StdioServerConfig:
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command="/bin/echo", env={"V": f"${{input:{input_id}}}"}),
    )


async def _drain_releases(task: asyncio.Task[Any], resolver: _ConcurrencyProbeResolver, *, timeout: float = 5.0) -> Any:
    """反复放行直至 boot 完成；每轮都校验并发度不越界（放行期间也持续观测）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not task.done():
        assert resolver.max_concurrent <= 1, f"Input 解析出现并发（{resolver.max_concurrent}）"
        if loop.time() > deadline:
            task.cancel()
            raise AssertionError("等待并发启动完成超时")
        resolver.release_pass(4)
        await asyncio.sleep(0.001)
    return await task


@pytest.mark.asyncio
async def test_concurrent_starts_do_not_prompt_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """★验收 5：3 个 server 并发启动、各引一个 input ⇒ resolver 最大并发恒为 1。"""
    spawned: list[_RecordingClient] = []

    def factory(config: StdioServerConfig, message_handler: Any = None) -> _RecordingClient:
        client = _RecordingClient(config, message_handler)
        spawned.append(client)
        return client

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)

    inputs = {
        MCPServerPromptStringInput(id="a", description="a", default="va"),
        MCPServerPromptStringInput(id="b", description="b", default="vb"),
        MCPServerPromptStringInput(id="c", description="c", default="vc"),
    }
    resolver = _ConcurrencyProbeResolver(inputs)
    comp = Computer(
        name="c",
        inputs=set(),
        mcp_servers={_cfg("s0", "a"), _cfg("s1", "b"), _cfg("s2", "c")},
        auto_connect=True,
        auto_reconnect=False,
        input_resolver=resolver,
    )
    comp.with_mcp_start_concurrency(3)

    booting = asyncio.create_task(comp.boot_up())
    try:
        await _drain_releases(booting, resolver)
        assert comp.mcp_manager is not None
        assert len(spawned) == 3, "三个 server 都应完成启动"
        assert set(comp.mcp_manager._active_clients) == {"s0", "s1", "s2"}
    finally:
        # 断言失败时 booting 可能仍卡在 resolver 里 —— 先放行+取消再收尾，否则 shutdown 的
        # drain（等在途启动事务收敛）会永久等待，把「断言失败」退化成「超时挂起」（坏失败模式）。
        resolver.release_pass(64)
        if not booting.done():
            booting.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await booting
        await comp.shutdown()

    assert resolver.entered == 3, "每个 server 各解析自己的 input（恰好一次）"
    assert resolver.max_concurrent == 1, "Input 交互解析必须串行——并发启动不得并行弹多个提示"
