# -*- coding: utf-8 -*-
# filename: test_oauth_capability_propagation.py
# @Time    : 2026/08/13
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
#185：clear_oauth 能力撤销传播——capability_changed 语义 + 三向竞态安全 + 幂等。

Hermetic 测试：直接播种 manager 运行态（活跃 client / 路由 / coordinator），不经网络。
竞态测试用可编程 gate 的假 client 确定性构造「RPC 在途时 clear 到达」的交错时序，
不依赖 sleep 竞速。Rust 参考：rust-sdk PR #184/185 ``clear_oauth_with_outcome`` +
``withdraw_bundle_tool_routes`` + ``active_client_generations``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from mcp.types import Tool

from a2c_smcp.computer.mcp_clients.manager import (
    MCPServerManager,
    _is_oauth_required_error,
)
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerConnectionState,
    StreamableHttpServerConfig,
)
from a2c_smcp.computer.mcp_clients.oauth_coordinator import OAuthCoordinator
from a2c_smcp.computer.mcp_clients.oauth_types import OAuthError

BUNDLE_A = "oauth-server-a"
BUNDLE_B = "oauth-server-b"


def _tool(name: str) -> Tool:
    return Tool(name=name, inputSchema={"type": "object"})


class _GatedClient:
    """最小 client 面：list_tools 可经 gate 挂起（确定性构造 RPC 在途时序）、adisconnect 可编程失败。"""

    def __init__(
        self,
        tools: list[Tool] | None = None,
        gate: asyncio.Event | None = None,
        disconnect_error: Exception | None = None,
    ) -> None:
        self._tools: list[Tool] = tools or []
        self._gate = gate
        self._disconnect_error = disconnect_error
        self.list_calls = 0
        self.disconnected = False

    async def list_tools(self) -> list[Tool]:
        self.list_calls += 1
        if self._gate is not None:
            await self._gate.wait()
        return list(self._tools)

    async def adisconnect(self) -> None:
        self.disconnected = True
        if self._disconnect_error is not None:
            raise self._disconnect_error


class _FakeCoordinator:
    """clear_oauth 第一半所需的最小 coordinator 面。"""

    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1


class _GhostCoordinator(_FakeCoordinator):
    """_aoauth_connect 扩展所需面：aborted event（预置 set）+ fail_launch 计数。"""

    def __init__(self) -> None:
        super().__init__()
        self._aborted = asyncio.Event()
        self._aborted.set()
        self.fail_launch_calls = 0

    def flow_aborted_event(self) -> asyncio.Event:
        return self._aborted

    def fail_launch(self, error: OAuthError) -> None:
        self.fail_launch_calls += 1


def _seed_active_bundle(
    manager: MCPServerManager,
    bundle_id: str,
    client: _GatedClient,
    tool_names: list[str],
) -> None:
    """直接播种活跃 bundle：config + client + activation + 世代 + 路由 + 连接状态（不经网络）。

    config 一并播种——available_tools / _arefresh_tool_mapping 的构建段均读
    ``_servers_config``，缺 config 会 KeyError。
    """
    manager._servers_config[bundle_id] = StreamableHttpServerConfig(
        name=bundle_id,
        server_parameters={"url": f"https://mcp.example.com/{bundle_id}"},
    )
    manager._active_clients[bundle_id] = client  # type: ignore[assignment]
    manager._activation_intents.add(bundle_id)
    manager._active_client_generations[bundle_id] = manager._active_client_generations.get(bundle_id, 0) + 1
    for name in tool_names:
        manager._exposed_tools[f"{bundle_id}__{name}"] = (bundle_id, name)
    manager._connection_states[bundle_id] = MCPServerConnectionState.CONNECTED


def _seed_coordinator(manager: MCPServerManager, bundle_id: str) -> _FakeCoordinator:
    coordinator = _FakeCoordinator()
    manager._oauth_coordinators[bundle_id] = coordinator  # type: ignore[assignment]
    return coordinator


# ============================================================================
# capability_changed 语义 + 跨 bundle 隔离
# ============================================================================


class TestClearOAuthCapabilityChanged:
    @pytest.mark.asyncio
    async def test_clear_withdraws_routes_reports_change_and_is_idempotent(self) -> None:
        manager = MCPServerManager(auto_connect=False)
        client_a = _GatedClient([_tool("tool_a"), _tool("tool_b")])
        client_b = _GatedClient([_tool("tool_c")])
        _seed_active_bundle(manager, BUNDLE_A, client_a, ["tool_a", "tool_b"])
        _seed_active_bundle(manager, BUNDLE_B, client_b, ["tool_c"])
        coordinator_a = _seed_coordinator(manager, BUNDLE_A)

        changed = await manager.clear_oauth(BUNDLE_A)

        assert changed is True
        assert coordinator_a.cleared == 1
        # 目标 bundle：client 退役 + 传输断开 + 路由撤回 + 世代 bump
        assert BUNDLE_A not in manager._active_clients
        assert client_a.disconnected is True
        assert BUNDLE_A not in {route[0] for route in manager._exposed_tools.values()}
        assert manager._active_client_generations[BUNDLE_A] == 2
        # 连接状态：Started 保留 → AUTHORIZATION_REQUIRED（#184：clear 不清 activation）
        assert manager._connection_states[BUNDLE_A] == MCPServerConnectionState.AUTHORIZATION_REQUIRED
        # 跨 bundle 隔离：B 的路由 / client / 世代不受波及
        assert manager._exposed_tools[f"{BUNDLE_B}__tool_c"] == (BUNDLE_B, "tool_c")
        assert manager._active_clients[BUNDLE_B] is client_b  # type: ignore[comparison-overlap] — 测试假 client
        assert manager._active_client_generations[BUNDLE_B] == 1

        # 幂等：二次 clear 已清除的 bundle → False（不触发二次传播）
        changed_again = await manager.clear_oauth(BUNDLE_A)
        assert changed_again is False

    @pytest.mark.asyncio
    async def test_clear_without_client_or_routes_reports_no_change(self) -> None:
        """从未连接（无 client、无路由）的 bundle → capability_changed=False；状态 DISCONNECTED。"""
        manager = MCPServerManager(auto_connect=False)
        _seed_coordinator(manager, BUNDLE_A)

        changed = await manager.clear_oauth(BUNDLE_A)

        assert changed is False
        assert manager._connection_states[BUNDLE_A] == MCPServerConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_clear_stale_routes_only_still_reports_change(self) -> None:
        """无活跃 client 但有残留路由（防御面）→ 撤回路由也计 capability_changed。"""
        manager = MCPServerManager(auto_connect=False)
        _seed_coordinator(manager, BUNDLE_A)
        # 播种路由但**不**播种 client（模拟运行期回收缝隙的残留投影）
        manager._exposed_tools[f"{BUNDLE_A}__tool_a"] = (BUNDLE_A, "tool_a")

        changed = await manager.clear_oauth(BUNDLE_A)

        assert changed is True
        assert manager._exposed_tools == {}

    @pytest.mark.asyncio
    async def test_disconnect_failure_does_not_delay_revocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """传输断开失败仅 WARN——本地撤销已 commit 且照常返回 capability_changed=True。"""
        manager = MCPServerManager(auto_connect=False)
        client = _GatedClient([_tool("tool_a")], disconnect_error=RuntimeError("transport gone"))
        _seed_active_bundle(manager, BUNDLE_A, client, ["tool_a"])
        _seed_coordinator(manager, BUNDLE_A)
        # 项目自定义 logger 不向 caplog 传播 → 直接 spy 模块 logger（既有约定）
        warnings: list[str] = []
        monkeypatch.setattr(
            "a2c_smcp.computer.mcp_clients.manager.logger.warning",
            lambda msg, *_args, **_kwargs: warnings.append(msg),  # type: ignore[attr-defined]
        )

        changed = await manager.clear_oauth(BUNDLE_A)

        assert changed is True
        assert client.disconnected is True  # 断开**已尝试**
        assert BUNDLE_A not in manager._active_clients
        assert manager._exposed_tools == {}
        assert any("transport disconnect failed" in message for message in warnings)


# ============================================================================
# 三向竞态安全
# ============================================================================


class TestClearOAuthRaceSafety:
    @pytest.mark.asyncio
    async def test_clear_not_blocked_by_inflight_refresh_and_no_resurrection(self) -> None:
        """clear vs 在途全量刷新：clear 不因刷新 RPC 阻塞（<1s），且刷新提交校验
        检出世代失配 → 整轮重试 → 不复活已撤回路由（Rust snapshot-validate-retry 同构）。"""
        manager = MCPServerManager(auto_connect=False)
        gate = asyncio.Event()
        client_a = _GatedClient([_tool("tool_a")], gate=gate)
        _seed_active_bundle(manager, BUNDLE_A, client_a, ["tool_a"])
        _seed_coordinator(manager, BUNDLE_A)

        refresh_task = asyncio.create_task(manager.arefresh_tools())
        # 等刷新完成快照并进入 A 的 list_tools RPC（确定性交错，不靠 sleep 竞速）
        for _ in range(50):
            if client_a.list_calls >= 1:
                break
            await asyncio.sleep(0.01)
        assert client_a.list_calls == 1

        start = time.monotonic()
        changed = await asyncio.wait_for(manager.clear_oauth(BUNDLE_A), timeout=1.0)
        elapsed = time.monotonic() - start
        assert changed is True
        assert elapsed < 1.0  # clear 从不等待上游 tools/list RPC

        gate.set()
        await refresh_task
        # 刷新重试后不得复活已撤回的路由
        assert BUNDLE_A not in {route[0] for route in manager._exposed_tools.values()}

    @pytest.mark.asyncio
    async def test_pull_racing_clear_returns_no_stale_tools(self) -> None:
        """Agent 拉取 vs clear 竞态：发布前二次校验过滤已撤回候选——拉取结果不含被清除
        bundle 的过期工具（Rust list_available_tools_with_bundle_id 发布前重验证同构）。"""
        manager = MCPServerManager(auto_connect=False)
        gate = asyncio.Event()
        client_a = _GatedClient([_tool("tool_a")], gate=gate)
        client_b = _GatedClient([_tool("tool_b")])
        _seed_active_bundle(manager, BUNDLE_A, client_a, ["tool_a"])
        _seed_active_bundle(manager, BUNDLE_B, client_b, ["tool_b"])
        _seed_coordinator(manager, BUNDLE_A)

        async def _drain() -> list[tuple[str, Tool]]:
            return [(bid, tool) async for bid, tool in manager.available_tools()]

        pull_task = asyncio.create_task(_drain())
        # 等拉取完成快照并阻塞于 A 的 list_tools（此时快照已含 A 的候选证据）
        for _ in range(50):
            if client_a.list_calls >= 1:
                break
            await asyncio.sleep(0.01)
        assert client_a.list_calls == 1

        changed = await manager.clear_oauth(BUNDLE_A)
        assert changed is True

        gate.set()
        results = await pull_task
        # A 已撤回：候选被发布前校验过滤；B 不受波及
        assert [bid for bid, _ in results] == [BUNDLE_B]

    @pytest.mark.asyncio
    async def test_clear_epoch_rejects_inflight_start_commit(self) -> None:
        """start-vs-clear：连接 RPC 在途时 clear 发生 → 提交被 epoch 守卫拒绝并转
        OAuthRequired（补偿 python 单一全局锁无法复刻 Rust per-bundle lifecycle lock）。"""
        manager = MCPServerManager(auto_connect=False)
        _seed_coordinator(manager, BUNDLE_A)
        captured_epoch = manager._oauth_clear_epochs.get(BUNDLE_A, 0)

        # clear 在「start 连接在途」时发生（无 client/路由 → False，但 epoch 已 bump）
        changed = await manager.clear_oauth(BUNDLE_A)
        assert changed is False

        # 提交时仍用旧 epoch → 拒绝，且**已连接的 client 被 best-effort 退役**（🔴 隔离审查：
        # 拒绝后 client 无任何处置即被丢弃 → 连接泄漏 + keep-alive 任务悬挂）
        rejected_client = _GatedClient([_tool("tool_a")])
        with pytest.raises(OAuthError) as exc:
            await manager._commit_active_client(
                BUNDLE_A,
                rejected_client,  # type: ignore[arg-type] — 测试假 client 非完整协议面
                clear_epoch=captured_epoch,
            )
        assert _is_oauth_required_error(exc.value)
        assert BUNDLE_A not in manager._active_clients
        assert rejected_client.disconnected is True

        # 正对照：clear 之后再捕获的新 epoch → 提交放行（start-after-clear 属宿主显式意图）
        manager._servers_config[BUNDLE_A] = StreamableHttpServerConfig(
            name=BUNDLE_A,
            server_parameters={"url": f"https://mcp.example.com/{BUNDLE_A}"},
        )
        fresh_epoch = manager._oauth_clear_epochs.get(BUNDLE_A, 0)
        await manager._commit_active_client(
            BUNDLE_A,
            _GatedClient([_tool("tool_a")]),  # type: ignore[arg-type] — 测试假 client 非完整协议面
            clear_epoch=fresh_epoch,
        )
        assert BUNDLE_A in manager._active_clients

    @pytest.mark.asyncio
    async def test_ghost_rekick_does_not_overwrite_clear_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 隔离审查：clear 的 cancel 链经 ``_rekick`` 重派发的 ghost connect 任务，其
        aborted/fail 分支的状态写**不得**覆盖 clear 快速段的 committed 状态。

        时序构造（「clear 早于 URL 发布」竞速的确定性等价）：① 派发 connect 任务
        （dispatch 时捕获 epoch）→ ② clear 快速段（epoch +1 + 状态 AUTHORIZATION_REQUIRED）
        → ③ ghost 任务首轮调度：aborted 已 set → 走取消分支——状态写点须比对 epoch 失配
        → 不写（或 commit 路径被 epoch 守卫拒绝）；终值恒为 clear 的 committed 状态。
        """
        manager = MCPServerManager(auto_connect=False)
        manager._servers_config[BUNDLE_A] = StreamableHttpServerConfig(
            name=BUNDLE_A,
            server_parameters={"url": f"https://mcp.example.com/{BUNDLE_A}"},
        )
        coordinator = _GhostCoordinator()
        manager._oauth_coordinators[BUNDLE_A] = coordinator  # type: ignore[assignment]
        manager._activation_intents.add(BUNDLE_A)

        class _InstantClient:
            async def aconnect(self) -> None:
                return None

            async def adisconnect(self) -> None:
                return None

        def factory(
            config: Any,  # noqa: ANN401
            message_handler: Any = None,  # noqa: ANN401
            oauth_coordinator: OAuthCoordinator | None = None,
        ) -> _InstantClient:
            return _InstantClient()

        monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)

        # ① 派发（T2 尚未运行）
        manager._ensure_oauth_connect_task(BUNDLE_A, coordinator)  # type: ignore[arg-type]
        # ② clear 快速段（epoch +1 + 状态 commit）
        manager._oauth_clear_epochs[BUNDLE_A] = manager._oauth_clear_epochs.get(BUNDLE_A, 0) + 1
        manager._connection_states[BUNDLE_A] = MCPServerConnectionState.AUTHORIZATION_REQUIRED
        # ③ 让 ghost 任务跑完（done callback 清注册表）
        for _ in range(50):
            if not manager._oauth_connect_tasks:
                break
            await asyncio.sleep(0.01)
        assert manager._oauth_connect_tasks == {}
        # 终值：clear 的 committed 状态不被覆盖
        assert manager._connection_states[BUNDLE_A] == MCPServerConnectionState.AUTHORIZATION_REQUIRED

    @pytest.mark.asyncio
    async def test_ghost_state_write_atomic_with_epoch_under_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """复核 R1：epoch 比对 + 状态写须**同锁内同步段**——比对在锁外时，锁获取 await
        给 clear 快速段留插入窗口（持久化 store 的 clear() 有让出点），陈旧比对结论会
        覆盖 committed 状态。本测试以「第三方持锁（gated refresh）+ T2 比对后 bump」确定性
        构造该窗口：T2 先唤醒比对、挂在 _lock 上，随后 clear 快速段过、refresh 放锁——T2
        进锁后的重读（修复版）必须看到 bump 后的 epoch 而跳过写。"""
        manager = MCPServerManager(auto_connect=False)
        gate = asyncio.Event()
        slow_client = _GatedClient([_tool("tool_x")], gate=gate)
        _seed_active_bundle(manager, BUNDLE_B, slow_client, ["tool_x"])
        coordinator = _GhostCoordinator()
        manager._oauth_coordinators[BUNDLE_A] = coordinator  # type: ignore[assignment]
        manager._activation_intents.add(BUNDLE_A)
        manager._servers_config[BUNDLE_A] = StreamableHttpServerConfig(
            name=BUNDLE_A,
            server_parameters={"url": f"https://mcp.example.com/{BUNDLE_A}"},
        )

        class _InstantClient:
            async def aconnect(self) -> None:
                return None

            async def adisconnect(self) -> None:
                return None

        monkeypatch.setattr(
            "a2c_smcp.computer.mcp_clients.manager.client_factory",
            lambda config, message_handler=None, oauth_coordinator=None: _InstantClient(),
        )

        # 第三方持锁：gated refresh 在 list_tools RPC 上挂起（快照已过、锁仍持有）
        refresh_task = asyncio.create_task(manager.arefresh_tools())
        for _ in range(50):
            if slow_client.list_calls >= 1:
                break
            await asyncio.sleep(0.01)
        assert slow_client.list_calls == 1

        # 派发 ghost 任务并让它唤醒：aborted 已 set → 比对（锁外版在此通过）→ 挂在 _lock
        manager._ensure_oauth_connect_task(BUNDLE_A, coordinator)  # type: ignore[arg-type]
        for _ in range(10):
            await asyncio.sleep(0)
        # clear 快速段（epoch +1 + 状态 commit）——T2 此刻已持陈旧比对结论（若比对在锁外）
        manager._oauth_clear_epochs[BUNDLE_A] = manager._oauth_clear_epochs.get(BUNDLE_A, 0) + 1
        manager._connection_states[BUNDLE_A] = MCPServerConnectionState.AUTHORIZATION_REQUIRED
        gate.set()
        await refresh_task
        for _ in range(50):
            if not manager._oauth_connect_tasks:
                break
            await asyncio.sleep(0.01)
        assert manager._oauth_connect_tasks == {}
        # 终值：T2 进锁后重读 epoch 失配 → 不写；committed 状态不被覆盖
        assert manager._connection_states[BUNDLE_A] == MCPServerConnectionState.AUTHORIZATION_REQUIRED
