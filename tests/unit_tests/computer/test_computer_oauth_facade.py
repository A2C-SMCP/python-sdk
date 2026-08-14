# -*- coding: utf-8 -*-
# filename: test_computer_oauth_facade.py
# @Time    : 2026/08/13
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
Computer 公共 OAuth facade 测试（#179）：pre-boot 守卫、with_oauth_credential_store
builder、boot 后委托 + store 到达两处 manager 构建点。
"""

from __future__ import annotations

import pytest

from a2c_smcp.computer import (
    Computer,
    OAuthBeginRequest,
    OAuthCallback,
    OAuthError,
    OAuthFlow,
)
from a2c_smcp.computer.mcp_clients.oauth_credential_store import InMemoryOAuthCredentialStore
from a2c_smcp.computer.mcp_clients.oauth_types import OAuthErrorCode

# ============================================================================
# Tests
# ============================================================================


class TestComputerOAuthFacade:
    @pytest.mark.asyncio
    async def test_pre_boot_facade_not_configured(self) -> None:
        computer = Computer(name="test")
        # boot 前 manager 未初始化 → NotConfigured（Rust manager-not-initialized 语义）
        with pytest.raises(OAuthError) as exc:
            await computer.oauth_status("some-bundle")
        assert exc.value.code == OAuthErrorCode.NotConfigured
        with pytest.raises(OAuthError) as exc:
            computer.create_oauth_flow("some-bundle", OAuthBeginRequest(redirect_uri="https://h.example/cb"))
        assert exc.value.code == OAuthErrorCode.NotConfigured
        with pytest.raises(OAuthError) as exc:
            await computer.complete_oauth("some-bundle", OAuthCallback(code="c", state="s"))
        assert exc.value.code == OAuthErrorCode.NotConfigured

    @pytest.mark.asyncio
    async def test_with_oauth_credential_store_reaches_boot_manager(self) -> None:
        """boot_up 构建 manager 时透传注入的 store（构造点 1）。"""
        store = InMemoryOAuthCredentialStore()
        computer = Computer(name="test").with_oauth_credential_store(store)
        assert computer._oauth_credential_store is store
        await computer.boot_up()
        assert computer.mcp_manager is not None
        assert computer.mcp_manager._oauth_credential_store is store

    @pytest.mark.asyncio
    async def test_with_oauth_credential_store_reaches_lazy_manager(self) -> None:
        """惰性 manager 构建点（_amount_rendered）同样透传注入的 store（构造点 2）。"""
        store = InMemoryOAuthCredentialStore()
        # auto_connect=False：只验证 store 到达构建点，不发起真实网络连接
        computer = Computer(name="test", auto_connect=False).with_oauth_credential_store(store)
        # 不经 boot_up，直接触发惰性物化路径
        from a2c_smcp.computer.mcp_clients.model import StreamableHttpServerConfig

        raw = StreamableHttpServerConfig(
            name="lazy-server",
            server_parameters={"url": "https://mcp.example.com/mcp"},
        )
        validated = StreamableHttpServerConfig.model_validate(raw)
        await computer._amount_rendered(raw, validated)
        assert computer.mcp_manager is not None
        assert computer.mcp_manager._oauth_credential_store is store

    @pytest.mark.asyncio
    async def test_booted_facade_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """boot 后 facade 委托 manager（异步路径经 stub manager 验证）。"""
        computer = Computer(name="test")
        await computer.boot_up()
        assert computer.mcp_manager is not None

        # 以 stub 替换 manager 的 facade 面，验证 Computer 委托形状
        async def fake_status(bundle_id: str):
            assert bundle_id == "b1"
            return "status-ok"

        def fake_create(bundle_id: str, request: OAuthBeginRequest):
            assert bundle_id == "b1"
            assert request.redirect_uri == "https://h.example/cb"
            return "flow-handle"

        async def fake_complete(bundle_id: str, callback: OAuthCallback):
            assert callback.code == "c1"
            return "outcome"

        async def fake_cancel(bundle_id: str, cancellation):
            return "cancelled-outcome"

        async def fake_clear(bundle_id: str):
            assert bundle_id == "b1"

        computer.mcp_manager.oauth_status = fake_status  # type: ignore[method-assign]
        computer.mcp_manager.create_oauth_flow = fake_create  # type: ignore[method-assign]
        computer.mcp_manager.complete_oauth = fake_complete  # type: ignore[method-assign]
        computer.mcp_manager.cancel_oauth = fake_cancel  # type: ignore[method-assign]
        computer.mcp_manager.clear_oauth = fake_clear  # type: ignore[method-assign]

        assert await computer.oauth_status("b1") == "status-ok"
        flow = computer.create_oauth_flow("b1", OAuthBeginRequest(redirect_uri="https://h.example/cb"))
        assert flow == "flow-handle"
        assert isinstance(OAuthFlow, type)  # handle 类型可导入（导出面冒烟）
        assert await computer.complete_oauth("b1", OAuthCallback(code="c1", state="s1")) == "outcome"
        from a2c_smcp.computer.mcp_clients.oauth_types import OAuthCancellation, OAuthCancellationReason

        assert (
            await computer.cancel_oauth(
                "b1",
                OAuthCancellation(state="s1", reason=OAuthCancellationReason.Cancelled),
            )
            == "cancelled-outcome"
        )
        await computer.clear_oauth("b1")

    def test_default_store_is_in_memory(self) -> None:
        computer = Computer(name="test")
        assert isinstance(computer._oauth_credential_store, InMemoryOAuthCredentialStore)


class _RecordingClient:
    """记录 emit_update_tool_list 调用次数的伪 Socket.IO 客户端（#185 传播面）。"""

    def __init__(self) -> None:
        self.update_called = 0

    async def emit_update_tool_list(self) -> None:
        self.update_called += 1


class TestClearOAuthCapabilityPropagation:
    """#185：clear_oauth 能力撤销传播——revision bump + update_tool_list 广播 + 幂等。"""

    @staticmethod
    async def _booted_computer() -> Computer:
        computer = Computer(name="test", auto_connect=False)
        await computer.boot_up()
        assert computer.mcp_manager is not None
        return computer

    @pytest.mark.asyncio
    async def test_capability_changed_bumps_revision_and_emits(self) -> None:
        computer = await self._booted_computer()
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]

        async def fake_clear(bundle_id: str) -> bool:
            assert bundle_id == "b1"
            return True

        computer.mcp_manager.clear_oauth = fake_clear  # type: ignore[method-assign]
        assert computer.capability_revision == 0

        await computer.clear_oauth("b1")

        assert computer.capability_revision == 1
        assert client.update_called == 1

    @pytest.mark.asyncio
    async def test_no_change_no_bump_no_emit(self) -> None:
        """capability_changed=False（幂等重复 clear）→ 不 bump、不广播。"""
        computer = await self._booted_computer()
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]

        async def fake_clear(bundle_id: str) -> bool:
            return False

        computer.mcp_manager.clear_oauth = fake_clear  # type: ignore[method-assign]

        await computer.clear_oauth("b1")

        assert computer.capability_revision == 0
        assert client.update_called == 0

    @pytest.mark.asyncio
    async def test_repeated_clear_idempotent_at_computer_level(self) -> None:
        """第一次有效（bump + 广播）、第二次无效（零传播）——端到端幂等。"""
        computer = await self._booted_computer()
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]

        calls: list[bool] = [True, False]

        async def fake_clear(bundle_id: str) -> bool:
            return calls.pop(0)

        computer.mcp_manager.clear_oauth = fake_clear  # type: ignore[method-assign]

        await computer.clear_oauth("b1")
        await computer.clear_oauth("b1")

        assert computer.capability_revision == 1
        assert client.update_called == 1

    @pytest.mark.asyncio
    async def test_emit_failure_does_not_lose_revision_bump(self) -> None:
        """emit 失败仅记日志（与 _on_manager_change 同姿态）——revision bump 为本地状态必然发生。"""
        computer = await self._booted_computer()

        class _FailingClient:
            async def emit_update_tool_list(self) -> None:
                raise RuntimeError("socketio gone")

        client = _FailingClient()
        computer.socketio_client = client  # type: ignore[assignment]

        async def fake_clear(bundle_id: str) -> bool:
            return True

        computer.mcp_manager.clear_oauth = fake_clear  # type: ignore[method-assign]

        await computer.clear_oauth("b1")  # 不抛

        assert computer.capability_revision == 1
