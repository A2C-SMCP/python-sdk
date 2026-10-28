# -*- coding: utf-8 -*-
# filename: test_computer_identity.py
# @Time    : 2026/10/28
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 侧 ``computer_id`` 生产者（``SMCPComputerClient``）单测 —— protocol#64。

覆盖验收中的「计算机生产者」组：物化时机（首次 connect，不在构造 / import）、两个入房构造点同值、
重放复用、损坏 fail-fast、reset_identity 的连接态拒绝与新值持久化。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from socketio import AsyncClient

from a2c_smcp.computer.identity import ComputerIdentityError, identity_store_path, load_or_create_computer_id
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.utils.computer_id import is_canonical_computer_id


def _make_client() -> SMCPComputerClient:
    computer = MagicMock()
    computer.name = "test_computer"
    return SMCPComputerClient(computer=computer)


class TestMaterialization:
    """物化时机与失败语义。"""

    def test_no_identity_before_connect(self) -> None:
        """构造客户端 / import 都不得产生身份配置副作用。"""
        client = _make_client()
        assert client._computer_id is None
        assert not identity_store_path().exists(), "connect 之前不得落盘身份文件"

    def test_pure_mcp_host_construction_creates_no_identity(self, tmp_path: Path) -> None:
        """纯 MCP 宿主（只构造 Computer、不连 Socket.IO）不得创建身份配置。"""
        from a2c_smcp.computer.computer import Computer

        Computer(name="pure-mcp-host", blob_cache_root=tmp_path)

        assert not identity_store_path().exists(), "Computer 构造不得物化身份（时机 = 首次 connect）"

    @pytest.mark.asyncio
    async def test_connect_materializes_and_persists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport_connect = AsyncMock()
        monkeypatch.setattr(AsyncClient, "connect", transport_connect)
        client = _make_client()

        await client.connect("http://localhost:1")

        assert client._computer_id is not None and is_canonical_computer_id(client._computer_id)
        assert transport_connect.await_count == 1
        assert identity_store_path().read_text(encoding="utf-8") == f"{client._computer_id}\n"

    @pytest.mark.asyncio
    async def test_corrupt_identity_aborts_before_transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = identity_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("corrupt\n", encoding="utf-8")
        transport_connect = AsyncMock()
        monkeypatch.setattr(AsyncClient, "connect", transport_connect)
        client = _make_client()

        with pytest.raises(ComputerIdentityError):
            await client.connect("http://localhost:1")

        transport_connect.assert_not_awaited()


class TestJoinRequests:
    """两个入房构造点携带同一 ``computer_id``。"""

    @pytest.mark.asyncio
    async def test_join_office_request_carries_identity(self) -> None:
        client = _make_client()
        client._computer_id = "abcdef01-2345-4678-8abc-def012345678"
        client.call = AsyncMock(return_value=None)  # type: ignore[method-assign]

        await client.join_office("R1")

        payload = client.call.await_args.args[1]
        assert payload["role"] == "computer"
        assert payload["computer_id"] == client._computer_id
        assert payload["office_id"] == "R1"

    @pytest.mark.asyncio
    async def test_replay_request_carries_same_identity(self) -> None:
        client = _make_client()
        client._computer_id = "abcdef01-2345-4678-8abc-def012345678"
        client.call = AsyncMock(return_value=None)  # type: ignore[method-assign]
        client.namespaces[SMCP_NAMESPACE] = "fake-sid"
        client.office_id = "R1"
        client._office_generation = 0

        await client._arejoin_office("R1", 0)

        payload = client.call.await_args.args[1]
        assert payload["computer_id"] == client._computer_id
        assert payload["name"] == client.computer.name
        assert payload["office_id"] == "R1"

    @pytest.mark.asyncio
    async def test_lazy_identity_for_embedded_join_without_connect(self) -> None:
        """嵌入方绕过 connect 直接 join ⇒ _ensure_computer_id 兜底并落盘（幂等）。"""
        client = _make_client()
        client.call = AsyncMock(return_value=None)  # type: ignore[method-assign]

        await client.join_office("R1")

        assert client._computer_id is not None
        assert load_or_create_computer_id() == client._computer_id


class TestResetIdentity:
    """reset_identity：已连接 / 有入房意图时拒绝；否则换值并持久化。"""

    @pytest.mark.asyncio
    async def test_refused_when_holding_office_intent(self) -> None:
        client = _make_client()
        client._computer_id = load_or_create_computer_id()
        client.office_id = "R1"

        with pytest.raises(RuntimeError, match="请先断开"):
            await client.reset_identity()

        assert load_or_create_computer_id() == client._computer_id, "拒绝时不得改动持久化值"

    @pytest.mark.asyncio
    async def test_refused_when_connected(self) -> None:
        client = _make_client()
        client._computer_id = load_or_create_computer_id()
        # socketio 的 connected 是实例属性（连接态），直接置位模拟已连接
        client.connected = True  # type: ignore[attr-defined]

        with pytest.raises(RuntimeError, match="请先断开"):
            await client.reset_identity()

    @pytest.mark.asyncio
    async def test_reset_when_disconnected_replaces_value(self) -> None:
        client = _make_client()
        old = load_or_create_computer_id()
        client._computer_id = old

        new = await client.reset_identity()

        assert new != old and is_canonical_computer_id(new)
        assert client._computer_id == new
        assert load_or_create_computer_id() == new
