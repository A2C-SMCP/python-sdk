# -*- coding: utf-8 -*-
# filename: test_update_tool_list.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：#127 —— Agent 收到 ``notify:update_tool_list`` 后自动回拉 ``client:get_tools`` 的单元测试（async + sync）。

  覆盖点（镜像 ``test_v021_consume.py::TestSkillsUpdatedAutoRefresh`` 的 hook/守卫/隔离范式）：
    1. 收到通知 → 自动回拉 ``get_tools_from_computer`` 并经 ``process_tools_response`` 派发 ``on_tools_received``。
    2. **预清回调**：先派发 ``on_computer_update_tool_list``（供消费方预清旧工具），再回拉——参数透传。
    3. 向后兼容：旧 handler 缺 ``on_computer_update_tool_list`` → hasattr 守卫静默跳过、仍回拉。
    4. 隔离：预清 hook 抛错被独立捕获、**不阻断**后续回拉（移除/改 schema 清理链路健壮）。
    5. ``event_handler is None`` → 不抛。

English: Unit tests for #127 —— Agent auto-refetches ``client:get_tools`` on ``notify:update_tool_list``.
  The new consumer callback ``on_computer_update_tool_list`` is the pre-clean hook (semantics aligned with
  ``on_computer_update_config``); the hasattr guard keeps legacy handlers working.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.sync_client import SMCPAgentClient


@pytest.fixture
def async_client() -> AsyncSMCPAgentClient:
    provider = DefaultAgentAuthProvider(agent_id="a", office_id="o")
    return AsyncSMCPAgentClient(auth_provider=provider)


@pytest.fixture
def sync_client() -> SMCPAgentClient:
    provider = DefaultAgentAuthProvider(agent_id="a", office_id="o")
    return SMCPAgentClient(auth_provider=provider)


# ── async ──────────────────────────────────────────────────────────


class TestAsyncUpdateToolListAutoRefresh:
    @pytest.mark.asyncio
    async def test_notify_triggers_refetch(self, async_client: AsyncSMCPAgentClient) -> None:
        """notify:update_tool_list → 自动回拉 get_tools_from_computer(computer)。"""
        tools = [{"name": "t-new"}]
        with patch.object(
            async_client,
            "get_tools_from_computer",
            new=AsyncMock(return_value={"tools": tools, "req_id": "r"}),
        ) as mock_refetch:
            await async_client._on_computer_update_tool_list({"computer": "comp-1"})
            mock_refetch.assert_awaited_once_with("comp-1")

    @pytest.mark.asyncio
    async def test_dispatches_preclean_then_tools_received(self, async_client: AsyncSMCPAgentClient) -> None:
        """先派发预清回调 on_computer_update_tool_list（参数透传），再回拉 → on_tools_received。"""
        tools = [{"name": "t-new"}]
        hook = AsyncMock()
        async_client.event_handler = hook  # type: ignore[assignment]
        with patch.object(
            async_client,
            "get_tools_from_computer",
            new=AsyncMock(return_value={"tools": tools, "req_id": "r"}),
        ):
            await async_client._on_computer_update_tool_list({"computer": "comp-1"})
            hook.on_computer_update_tool_list.assert_awaited_once_with({"computer": "comp-1"}, async_client)
            hook.on_tools_received.assert_awaited_once_with("comp-1", tools, async_client)
            # 顺序不变量（load-bearing）：预清必须**先于** on_tools_received，否则移除/换 schema 会残留旧定义
            # Ordering invariant: pre-clean MUST precede on_tools_received, else stale defs linger.
            ordered = [c[0] for c in hook.mock_calls if c[0] in ("on_computer_update_tool_list", "on_tools_received")]
            assert ordered == ["on_computer_update_tool_list", "on_tools_received"]

    @pytest.mark.asyncio
    async def test_legacy_handler_missing_preclean_still_refetches(self, async_client: AsyncSMCPAgentClient) -> None:
        """向后兼容：旧 handler 未实现 on_computer_update_tool_list → 不抛、仍回拉并回调 on_tools_received。"""

        class _LegacyEH:
            def __init__(self) -> None:
                self.received: list[Any] = []

            async def on_tools_received(self, computer: str, tools: list, client: Any) -> None:
                self.received.append((computer, tools))

        legacy = _LegacyEH()
        async_client.event_handler = legacy  # type: ignore[assignment]
        with patch.object(
            async_client,
            "get_tools_from_computer",
            new=AsyncMock(return_value={"tools": [{"name": "t"}], "req_id": "r"}),
        ) as mock_refetch:
            await async_client._on_computer_update_tool_list({"computer": "comp-1"})  # must not raise
            mock_refetch.assert_awaited_once_with("comp-1")
            assert legacy.received == [("comp-1", [{"name": "t"}])]

    @pytest.mark.asyncio
    async def test_preclean_hook_exception_does_not_block_refetch(self, async_client: AsyncSMCPAgentClient) -> None:
        """预清 hook 抛错被隔离，**不阻断**后续回拉（移除/改 schema 清理链路健壮）。"""
        hook = AsyncMock()
        hook.on_computer_update_tool_list.side_effect = RuntimeError("preclean boom")
        async_client.event_handler = hook  # type: ignore[assignment]
        with patch.object(
            async_client,
            "get_tools_from_computer",
            new=AsyncMock(return_value={"tools": [{"name": "t"}], "req_id": "r"}),
        ) as mock_refetch:
            await async_client._on_computer_update_tool_list({"computer": "comp-1"})  # must not raise
            mock_refetch.assert_awaited_once_with("comp-1")
            hook.on_tools_received.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_none_event_handler_does_not_raise(self, async_client: AsyncSMCPAgentClient) -> None:
        """event_handler is None → 不抛。"""
        async_client.event_handler = None  # type: ignore[assignment]
        with patch.object(
            async_client,
            "get_tools_from_computer",
            new=AsyncMock(return_value={"tools": [], "req_id": "r"}),
        ):
            await async_client._on_computer_update_tool_list({"computer": "comp-1"})  # must not raise


# ── sync mirror ────────────────────────────────────────────────────


class TestSyncUpdateToolListAutoRefresh:
    def test_notify_triggers_refetch(self, sync_client: SMCPAgentClient) -> None:
        tools = [{"name": "t-new"}]
        with patch.object(
            sync_client,
            "get_tools_from_computer",
            new=MagicMock(return_value={"tools": tools, "req_id": "r"}),
        ) as mock_refetch:
            sync_client._on_computer_update_tool_list({"computer": "comp-1"})
            mock_refetch.assert_called_once_with("comp-1")

    def test_dispatches_preclean_then_tools_received(self, sync_client: SMCPAgentClient) -> None:
        tools = [{"name": "t-new"}]
        hook = MagicMock()
        sync_client.event_handler = hook  # type: ignore[assignment]
        with patch.object(
            sync_client,
            "get_tools_from_computer",
            new=MagicMock(return_value={"tools": tools, "req_id": "r"}),
        ):
            sync_client._on_computer_update_tool_list({"computer": "comp-1"})
            hook.on_computer_update_tool_list.assert_called_once_with({"computer": "comp-1"}, sync_client)
            hook.on_tools_received.assert_called_once_with("comp-1", tools, sync_client)
            # 顺序不变量（load-bearing）：预清必须**先于** on_tools_received（sync 镜像）
            # Ordering invariant: pre-clean MUST precede on_tools_received (sync mirror).
            ordered = [c[0] for c in hook.mock_calls if c[0] in ("on_computer_update_tool_list", "on_tools_received")]
            assert ordered == ["on_computer_update_tool_list", "on_tools_received"]

    def test_legacy_handler_missing_preclean_still_refetches(self, sync_client: SMCPAgentClient) -> None:
        class _LegacyEH:
            def __init__(self) -> None:
                self.received: list[Any] = []

            def on_tools_received(self, computer: str, tools: list, client: Any) -> None:
                self.received.append((computer, tools))

        legacy = _LegacyEH()
        sync_client.event_handler = legacy  # type: ignore[assignment]
        with patch.object(
            sync_client,
            "get_tools_from_computer",
            new=MagicMock(return_value={"tools": [{"name": "t"}], "req_id": "r"}),
        ) as mock_refetch:
            sync_client._on_computer_update_tool_list({"computer": "comp-1"})  # must not raise
            mock_refetch.assert_called_once_with("comp-1")
            assert legacy.received == [("comp-1", [{"name": "t"}])]

    def test_preclean_hook_exception_does_not_block_refetch(self, sync_client: SMCPAgentClient) -> None:
        hook = MagicMock()
        hook.on_computer_update_tool_list.side_effect = RuntimeError("preclean boom")
        sync_client.event_handler = hook  # type: ignore[assignment]
        with patch.object(
            sync_client,
            "get_tools_from_computer",
            new=MagicMock(return_value={"tools": [{"name": "t"}], "req_id": "r"}),
        ) as mock_refetch:
            sync_client._on_computer_update_tool_list({"computer": "comp-1"})  # must not raise
            mock_refetch.assert_called_once_with("comp-1")
            hook.on_tools_received.assert_called_once()

    def test_none_event_handler_does_not_raise(self, sync_client: SMCPAgentClient) -> None:
        sync_client.event_handler = None  # type: ignore[assignment]
        with patch.object(
            sync_client,
            "get_tools_from_computer",
            new=MagicMock(return_value={"tools": [], "req_id": "r"}),
        ):
            sync_client._on_computer_update_tool_list({"computer": "comp-1"})  # must not raise
