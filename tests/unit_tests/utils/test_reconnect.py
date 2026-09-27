# -*- coding: utf-8 -*-
"""
v0.5.0 审查 🔴5 / Y10：socketio 自动重连窗口的两条停止路径（三个客户端各测一遍）。

驱动的是**上游真实的** ``_handle_reconnect`` 循环（只把上游 ``connect`` 换成受控桩），因为缺陷恰恰长在
「SDK 覆写 ↔ 上游循环」的接缝上：

- 🔴5：重连窗口内宿主调 ``disconnect()`` —— 上游实现是空操作（不置 ``_reconnect_abort``、不触发钩子）⇒
  重连不停、意图不清、稍后自动回旧房。断言**机制**（重连任务真的结束 + 之后不再尝试）与**终态**（意图清空）。
- Y10：重连尝试撞上 4008 —— Computer 的 ``ProtocolVersionError`` 逃出循环（任务带异常死亡、意图永久残留）；
  Agent 的 4008 被当普通 ``ConnectionError`` 吞掉 ⇒ 无限重试。断言：恰一次尝试即停、任务正常结束、意图清空。

Drives socketio's real reconnect loop with a stubbed upstream ``connect`` for all three clients.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from engineio.exceptions import ConnectionError as EngineConnError
from socketio import AsyncClient, Client
from socketio.exceptions import ConnectionError as SioConnError

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.utils.reconnect import forget_finished_reconnect_task

_DEADLINE = 3.0
_QUIESCENCE = 0.2
_RECONNECT_KW: dict[str, Any] = {
    "reconnection": True,
    "reconnection_attempts": 0,  # 0 = 不限（上游缺省）——Y10 无限重试的真实形态
    "reconnection_delay": 0.01,
    "reconnection_delay_max": 0.02,
    "randomization_factor": 0,
}


def _sio_4008() -> SioConnError:
    """engineio 在 HTTP 400 时的真实异常链：socketio ConnectionError.__cause__ = engineio(msg, body)。"""
    e = SioConnError("Unexpected status code 400 in server response")
    e.__cause__ = EngineConnError(
        "Unexpected status code 400 in server response",
        {"code": 4008, "message": "Protocol version mismatch", "server_version": "9.9.0", "client_version": "0.0.0"},
    )
    return e


def _prime_connection_args(client: Any) -> None:
    """上游重连循环复用首连参数；单测不走首连，手工补齐。"""
    client.connection_url = "http://127.0.0.1:1"
    client.connection_headers = {}
    client.connection_auth = None
    client.connection_transports = None
    client.connection_namespaces = [SMCP_NAMESPACE]
    client.socketio_path = "socket.io"


def _make_computer() -> SMCPComputerClient:
    computer = MagicMock(spec=Computer)
    computer.name = "reconnect-computer"
    client = SMCPComputerClient(computer=computer, **_RECONNECT_KW)
    client.office_id = "officeA"  # 传输中断且会重连 ⇒ desired 保留（#203）
    return client


def _make_async_agent() -> AsyncSMCPAgentClient:
    client = AsyncSMCPAgentClient(auth_provider=DefaultAgentAuthProvider(agent_id="a", office_id="officeA"), **_RECONNECT_KW)
    client._desired_office = ("officeA", "agent-1")
    return client


def _make_sync_agent() -> SMCPAgentClient:
    client = SMCPAgentClient(auth_provider=DefaultAgentAuthProvider(agent_id="a", office_id="officeA"), **_RECONNECT_KW)
    client._desired_office = ("officeA", "agent-1")
    return client


def _async_intent(client: Any) -> Any:
    return client.office_id if isinstance(client, SMCPComputerClient) else client._desired_office


# ── 🔴5 async：Computer + Agent ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [_make_computer, _make_async_agent], ids=["computer", "async-agent"])
async def test_async_disconnect_during_reconnect_window_aborts_reconnect(factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """重连窗口内 ``disconnect()`` ⇒ 重连任务结束、此后零尝试、入房意图清空。"""
    attempts: list[float] = []

    async def failing_connect(self: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(time.monotonic())
        raise SioConnError("server unreachable")

    monkeypatch.setattr(AsyncClient, "connect", failing_connect)
    client = factory()
    _prime_connection_args(client)
    client._reconnect_task = asyncio.ensure_future(client._handle_reconnect())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DEADLINE
    while len(attempts) < 2:  # 确证已处于「重连窗口」（至少两次失败尝试）
        assert loop.time() < deadline, "夹具失效：重连循环未发起尝试"
        await asyncio.sleep(0.005)
    assert not client.connected

    await asyncio.wait_for(client.disconnect(), timeout=_DEADLINE)

    # 机制断言：重连任务**真的**结束（上游 disconnect 在窗口内是空操作 ⇒ 缺覆写时任务仍在跑）
    assert client._reconnect_task.done(), "disconnect() 必须中止在途自动重连"
    assert client._reconnect_task.exception() is None
    settled = len(attempts)
    await asyncio.sleep(_QUIESCENCE)
    assert len(attempts) == settled, "中止后不得再有重连尝试"
    # 终态断言：意图清空 ⇒ 即便此后重连，也不会回旧房
    assert _async_intent(client) is None, "手工断开必须清空入房意图"


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [_make_computer, _make_async_agent], ids=["computer", "async-agent"])
async def test_async_disconnect_outside_reconnect_window_keeps_upstream_semantics(factory: Any) -> None:
    """**负对照**：无在途重连时 ``disconnect()`` 原样走上游、不动入房意图（首连失败的内部清理即走这里，
    宿主预置的意图须保留给重试）。防「一律清意图」的过度实现。"""
    client = factory()
    await client.disconnect()
    assert _async_intent(client) is not None


# ── 🔴5 sync：Agent ────────────────────────────────────────────────────────────


def test_sync_disconnect_during_reconnect_window_aborts_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[float] = []

    def failing_connect(self: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(time.monotonic())
        raise SioConnError("server unreachable")

    monkeypatch.setattr(Client, "connect", failing_connect)
    client = _make_sync_agent()
    _prime_connection_args(client)
    task = threading.Thread(target=client._handle_reconnect, daemon=True)
    client._reconnect_task = task
    task.start()
    deadline = time.monotonic() + _DEADLINE
    while len(attempts) < 2:
        assert time.monotonic() < deadline, "夹具失效：重连循环未发起尝试"
        time.sleep(0.005)

    client.disconnect()

    assert not task.is_alive(), "disconnect() 必须中止在途自动重连"
    settled = len(attempts)
    time.sleep(_QUIESCENCE)
    assert len(attempts) == settled, "中止后不得再有重连尝试"
    assert client._desired_office is None, "手工断开必须清空入房意图"


# ── Y10：重连尝试撞上 4008 ⇒ 恰一次即停 ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [_make_computer, _make_async_agent], ids=["computer", "async-agent"])
async def test_async_4008_during_reconnect_stops_the_loop_and_clears_intent(factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    async def version_rejected(self: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        raise _sio_4008()

    monkeypatch.setattr(AsyncClient, "connect", version_rejected)
    client = factory()
    _prime_connection_args(client)
    task = asyncio.ensure_future(client._handle_reconnect())
    client._reconnect_task = task

    await asyncio.wait_for(asyncio.wait({task}), timeout=_DEADLINE)
    assert task.done(), "4008 必须终止重连（Agent 缺修复时无限重试 ⇒ 超时）"
    # Computer 缺修复时 ProtocolVersionError 逃出循环 ⇒ 任务带异常死亡、``__disconnect_final`` 不触发
    assert task.exception() is None, f"4008 不得让重连任务带异常死亡：{task.exception()!r}"
    assert attempts == [1], f"4008 不得重试（versioning.md §4），实得 {len(attempts)} 次"
    assert _async_intent(client) is None, "重连彻底放弃 ⇒ 入房意图必须清空（经 __disconnect_final）"


def test_sync_4008_during_reconnect_stops_the_loop_and_clears_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def version_rejected(self: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        raise _sio_4008()

    monkeypatch.setattr(Client, "connect", version_rejected)
    client = _make_sync_agent()
    _prime_connection_args(client)
    task = threading.Thread(target=client._handle_reconnect, daemon=True)
    client._reconnect_task = task
    task.start()
    task.join(_DEADLINE)

    assert not task.is_alive(), "4008 必须终止重连（缺修复时无限重试）"
    assert attempts == [1]
    assert client._desired_office is None


@pytest.mark.asyncio
async def test_computer_non_version_handshake_error_in_reconnect_loop_keeps_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    """裸 ``RuntimeError``（``HANDSHAKE_CONNECT_ERRORS`` 成员，如 aiohttp「Connection closed.」）在重连循环内
    被规整为 ``ConnectionError`` ⇒ 循环照常退避重试（而不是带异常死亡），且**不**清意图。"""
    attempts: list[int] = []

    async def flaky(self: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("Connection closed.")
        self.connected = True  # 第三次「成功」

    monkeypatch.setattr(AsyncClient, "connect", flaky)
    client = _make_computer()
    _prime_connection_args(client)
    task = asyncio.ensure_future(client._handle_reconnect())
    client._reconnect_task = task
    await asyncio.wait_for(asyncio.wait({task}), timeout=_DEADLINE)

    assert task.exception() is None, "非 4008 握手错误不得杀死重连任务"
    assert len(attempts) == 3
    assert client.office_id == "officeA", "瞬时失败不得清意图（重连成功后由连接钩子回房）"


# ── forget_finished_reconnect_task ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finished_reconnect_task_is_forgotten_on_next_connect_entry() -> None:
    """上游只在重连**成功**时清 ``_reconnect_task``；中止 / 放弃后残留的已结束任务会让同一客户端再连之后的
    掉线**永不自动重连**（``_handle_eio_disconnect`` 以 ``not self._reconnect_task`` 为启动条件）。"""
    client = _make_computer()
    finished = asyncio.ensure_future(asyncio.sleep(0))
    await finished
    client._reconnect_task = finished
    forget_finished_reconnect_task(client)
    assert client._reconnect_task is None

    alive = asyncio.ensure_future(asyncio.sleep(10))
    client._reconnect_task = alive
    forget_finished_reconnect_task(client)
    assert client._reconnect_task is alive, "在途任务不得被丢弃"
    alive.cancel()
