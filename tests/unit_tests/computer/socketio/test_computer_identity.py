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

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from socketio import AsyncClient

import a2c_smcp.computer.socketio.client as computer_client_mod
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

    def test_no_identity_before_connect(self, isolate_a2c_identity_config: Path) -> None:
        """构造客户端 / import 都不得产生身份配置副作用（整棵 XDG 树为空，不只是身份文件）。"""
        client = _make_client()
        assert client._computer_id is None
        assert list(isolate_a2c_identity_config.rglob("*")) == [], "connect 之前不得产生任何身份配置副作用"

    def test_pure_mcp_host_construction_creates_no_identity(
        self,
        tmp_path: Path,
        isolate_a2c_identity_config: Path,
    ) -> None:
        """纯 MCP 宿主（只构造 Computer、不连 Socket.IO）不得创建身份配置（整棵 XDG 树为空）。"""
        from a2c_smcp.computer.computer import Computer

        Computer(name="pure-mcp-host", blob_cache_root=tmp_path)

        assert list(isolate_a2c_identity_config.rglob("*")) == [], "Computer 构造不得物化身份（时机 = 首次 connect）"

    @pytest.mark.asyncio
    async def test_dual_instance_env_identity_isolation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fix-review 🔴1：两台注入不同 env 的 Computer ⇒ 各自独立的身份文件（不得共读 ambient）。

        与 ``test_computer_env_injection.py`` 的 A/B 双实例范式同构：环境锚点必须跟实例走。
        """
        from a2c_smcp.computer.computer import Computer

        env_a = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-a"), "HOME": str(tmp_path / "home-a")}
        env_b = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-b"), "HOME": str(tmp_path / "home-b")}
        comp_a = Computer(name="A", env=env_a, blob_cache_root=tmp_path / "blob-a")
        comp_b = Computer(name="B", env=env_b, blob_cache_root=tmp_path / "blob-b")
        client_a = SMCPComputerClient(computer=comp_a)
        client_b = SMCPComputerClient(computer=comp_b)
        monkeypatch.setattr(AsyncClient, "connect", AsyncMock())

        await client_a.connect("http://localhost:1")
        await client_b.connect("http://localhost:1")

        assert client_a._computer_id != client_b._computer_id
        assert identity_store_path(env_a).read_text(encoding="utf-8") == f"{client_a._computer_id}\n"
        assert identity_store_path(env_b).read_text(encoding="utf-8") == f"{client_b._computer_id}\n"
        assert identity_store_path(env_a) != identity_store_path(env_b)

        # 重置也跟随实例 env：只改 A 的文件，B 不受影响
        new_a = await client_a.reset_identity()
        assert identity_store_path(env_a).read_text(encoding="utf-8") == f"{new_a}\n"
        assert identity_store_path(env_b).read_text(encoding="utf-8") == f"{client_b._computer_id}\n"

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
        """嵌入方绕过 connect 直接 join ⇒ _amaterialize_computer_id 兜底并落盘（幂等）。"""
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


class TestEnvAnchoring:
    """fix-review 🟡3/🟡4/🟡8：env 全路径锚点、只读视图与包级导出。"""

    def test_only_home_differs_yields_distinct_store_paths(self, tmp_path: Path) -> None:
        """XDG 未设置时，身份路径必须跟随**注入 env** 的 HOME，而不是 ambient。"""
        env_a = {"HOME": str(tmp_path / "home-a")}
        env_b = {"HOME": str(tmp_path / "home-b")}

        path_a = identity_store_path(env_a)
        path_b = identity_store_path(env_b)

        assert path_a != path_b
        assert path_a == (tmp_path / "home-a" / ".config" / "a2c" / "computer-id").resolve()
        assert path_b == (tmp_path / "home-b" / ".config" / "a2c" / "computer-id").resolve()

    @pytest.mark.asyncio
    async def test_dual_instance_home_only_identity_isolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """两台仅 HOME 不同的 Computer ⇒ 两条不同身份文件（XDG 未设置路径）。"""
        from a2c_smcp.computer.computer import Computer

        env_a = {"HOME": str(tmp_path / "home-a")}
        env_b = {"HOME": str(tmp_path / "home-b")}
        client_a = SMCPComputerClient(
            computer=Computer(name="A", env=env_a, blob_cache_root=tmp_path / "blob-a"),
        )
        client_b = SMCPComputerClient(
            computer=Computer(name="B", env=env_b, blob_cache_root=tmp_path / "blob-b"),
        )
        monkeypatch.setattr(AsyncClient, "connect", AsyncMock())

        await client_a.connect("http://localhost:1")
        await client_b.connect("http://localhost:1")

        assert client_a._computer_id != client_b._computer_id
        assert identity_store_path(env_a).read_text(encoding="utf-8") == f"{client_a._computer_id}\n"
        assert identity_store_path(env_b).read_text(encoding="utf-8") == f"{client_b._computer_id}\n"

    def test_computer_env_is_read_only(self, tmp_path: Path) -> None:
        """fix-review 🟡4：公开 env 是只读视图；`_resolve_env` 委托同一视图。"""
        from a2c_smcp.computer.computer import Computer

        env = {"XDG_CONFIG_HOME": str(tmp_path)}
        comp = Computer(name="readonly-env", env=env, blob_cache_root=tmp_path)

        assert comp.env is not None and comp.env["XDG_CONFIG_HOME"] == str(tmp_path)
        with pytest.raises(TypeError):
            comp.env["XDG_CONFIG_HOME"] = "/tmp/evil"  # type: ignore[index]
        assert comp._resolve_env() is not None and comp._resolve_env()["XDG_CONFIG_HOME"] == str(tmp_path)
        assert comp._env == env, "实例持有的原映射不被改写"

    def test_computer_identity_error_is_exported(self) -> None:
        """fix-review 🟡8：宿主必须捕获的 ComputerIdentityError 进包级 __all__。"""
        from a2c_smcp.computer import ComputerIdentityError as exported
        from a2c_smcp.computer import __all__ as computer_all
        from a2c_smcp.computer.identity import ComputerIdentityError

        assert "ComputerIdentityError" in computer_all
        assert exported is ComputerIdentityError


class TestIdentityRaces:
    """fix-review 🔴1/🔴2 的竞态回归锁。"""

    @pytest.mark.asyncio
    async def test_join_superseded_during_materialization_keeps_no_desired(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """物化期间断连 ⇒ join 不得写回 office_id（否则「已断开却残留 desired」回旧房，#203）。"""
        client = _make_client()
        client._computer_id = None
        gate = threading.Event()

        def _slow_load(*, env: object = None) -> str:
            assert gate.wait(timeout=5), "测试未释放物化闸门"
            return "abcdef01-2345-4678-8abc-def012345678"

        monkeypatch.setattr(computer_client_mod, "load_or_create_computer_id", _slow_load)
        client.call = AsyncMock(return_value=None)  # type: ignore[method-assign]

        task = asyncio.create_task(client.join_office("R1"))
        for _ in range(500):
            if client._identity_lock.locked():
                break
            await asyncio.sleep(0.01)
        assert client._identity_lock.locked(), "测试未能进入物化窗口"

        # 物化期间用户断开：generation 前进、desired 清空
        client._on_namespace_disconnect("client disconnect")
        gate.set()

        await task  # 被抢占时静默让位（成功路径同样返回 None，故不以返回值断言）
        client.call.assert_not_awaited()
        assert client.office_id is None, "不得把 desired 写回已断开的连接"

    @pytest.mark.asyncio
    async def test_reset_during_materialization_is_not_clobbered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reset 与在途物化互斥：stale 物化结果不得覆盖新身份（内存旧 / 磁盘新）。"""
        client = _make_client()
        client._computer_id = None
        gate = threading.Event()
        reset_entered = threading.Event()
        stale = "abcdef01-2345-4678-8abc-def012345678"
        fresh = "abcdef02-2345-4678-8abc-def012345679"

        def _slow_load(*, env: object = None) -> str:
            assert gate.wait(timeout=5), "测试未释放物化闸门"
            return stale

        def _reset(*, env: object = None) -> str:
            reset_entered.set()  # 进入「文件操作」的确定性探针
            return fresh

        monkeypatch.setattr(computer_client_mod, "load_or_create_computer_id", _slow_load)
        monkeypatch.setattr(computer_client_mod, "reset_computer_id", _reset)

        materialize = asyncio.create_task(client._amaterialize_computer_id())
        for _ in range(500):
            if client._identity_lock.locked():
                break
            await asyncio.sleep(0.01)
        assert client._identity_lock.locked()

        reset = asyncio.create_task(client.reset_identity())
        # 关键探针：物化持锁期间 reset 必须被挡在文件操作之外（无 `_identity_lock` 时此处会提前置位）
        for _ in range(50):
            await asyncio.sleep(0.01)
        assert not reset_entered.is_set(), "有锁时 reset 不得在物化完成前进入文件操作"
        gate.set()

        assert await materialize == stale
        assert await reset == fresh
        assert reset_entered.is_set()
        assert client._computer_id == fresh, "内存必须是 reset 后的新身份"
