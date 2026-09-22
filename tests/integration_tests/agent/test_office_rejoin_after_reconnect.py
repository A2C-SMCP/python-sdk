# -*- coding: utf-8 -*-
"""
#203 自动重连后恢复 Office 成员关系（Agent 侧，异步，真实 wire）；含 #218 显式 join 等 ACK。

服务端房间成员关系随会话销毁（``server/namespace.py`` 断连时退房并清 ``session["office_id"]``）。
因此传输层断线自动重连后，新 SID 不在房间里：Agent 发出的 ``client:*`` 会被服务端按 office 校验
拒绝，也收不到任何 ``notify:*`` 广播。本文件验证：重连完成后客户端重放 ``server:join_office``，
新 SID 重新成为该 Office 成员；无法恢复时清空回房意图，不保留"看似还在房间"的状态。

**#218 追加**：显式 ``join_office`` 已由「无 ack 的 emit」改为等 ACK ⇒ 首次入房被**真实服务端**
拒绝（房内已有 Agent）必须抛 ``SMCPProtocolError`` 且 ``.code`` 可机器分流。

Real-wire coverage for #203 (replay) and #218 (explicit join waits for the ack) on the Agent side.

断线模拟同 Computer 侧：服务端主动 CLOSE 会让 socketio ``will_reconnect=False``，只有传输层突然
断开才走 ``TRANSPORT_ERROR`` → 自动重连。/ Drop simulation mirrors the Computer-side test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from socketio import ASGIApp, AsyncServer

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.testing import UvicornTestServer
from a2c_smcp.utils import office as office_mod
from tests.integration_tests.mock_socketio_server import MockComputerServerNamespace

_CONNECT_TIMEOUT = 10.0
_WAIT_INTERVAL = 0.01
_QUIESCENCE = 0.3
_OFFICE = "agent-rejoin-office"
_AGENT_NAME = "agent-rejoin-1"


class _OfficeRecordingNamespace(MockComputerServerNamespace):
    """按到达顺序记录 ``server:join_office``（跨 SID，故 append 而非 sid 覆盖写）。"""

    def __init__(self) -> None:
        super().__init__()
        self.join_record: list[tuple[str, dict]] = []
        self.joined = asyncio.Event()
        # 自第 N 次（1-based）join 起一律拒绝（复现"旧会话尚未回收"的瞬态拒绝）
        self.reject_from: int | None = None

    async def on_server_join_office(self, sid: str, data: Any):  # type: ignore[override]
        if self.reject_from is not None and len(self.join_record) + 1 >= self.reject_from:
            payload = dict(data)
            self.join_record.append((sid, payload))
            self.joined.set()
            # v0.5.0（#214）：失败 = flat ErrorPayload（顶层含 code）。旧元组形态已废除——
            # 留在旧契约会让替身与真实服务端分叉，且用例只覆盖「形状不认识」分支，
            # 覆盖不到 #212 要消费的 4101 带码分支。
            return {
                "code": 4101,
                "message": "Room already has an agent",
                "details": {"office_id": payload.get("office_id")},
            }
        result = await super().on_server_join_office(sid, data)
        self.join_record.append((sid, dict(data)))
        self.joined.set()
        return result


@pytest.fixture
async def office_server(basic_server_port: int) -> AsyncGenerator[_OfficeRecordingNamespace, None]:
    """真实 ASGI Socket.IO 服务端（记录 agent 入房的命名空间）。"""
    sio = AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        ping_timeout=10,
        ping_interval=10,
        async_handlers=True,
    )
    sio.eio.start_service_task = False
    ns = _OfficeRecordingNamespace()
    sio.register_namespace(ns)
    asgi_app = ASGIApp(sio, socketio_path="/socket.io")
    server = UvicornTestServer(asgi_app, port=basic_server_port)
    await server.up()
    try:
        yield ns
    finally:
        await server.down(force=True)


def _make_agent(**kwargs: Any) -> AsyncSMCPAgentClient:
    """构造测试用 Agent（收紧重连节奏以提速，语义不变）。"""
    auth = DefaultAgentAuthProvider(agent_id=_AGENT_NAME, office_id=_OFFICE)
    return AsyncSMCPAgentClient(
        auth_provider=auth,
        reconnection=True,
        reconnection_attempts=10,
        reconnection_delay=0.1,
        reconnection_delay_max=0.3,
        **kwargs,
    )


async def _connect(agent: AsyncSMCPAgentClient, port: int) -> None:
    await agent.connect_to_server(f"http://localhost:{port}", socketio_path="/socket.io")


async def _wait_for_new_sid(agent: AsyncSMCPAgentClient, first_sid: str) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CONNECT_TIMEOUT
    while True:
        sid = agent.namespaces.get(SMCP_NAMESPACE)
        if sid is not None and sid != first_sid:
            return sid
        if loop.time() > deadline:
            raise AssertionError("重连后命名空间未在期限内重建 / namespace not re-established after reconnect")
        await asyncio.sleep(_WAIT_INTERVAL)


async def _wait_until(predicate: Any, message: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CONNECT_TIMEOUT
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(_WAIT_INTERVAL)


@pytest.mark.asyncio
async def test_agent_rejoins_office_after_auto_reconnect(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """自动重连后，Agent 必须重放入房，新 SID 重新成为 Office 成员（服务端可见）。"""
    agent = _make_agent()
    try:
        await _connect(agent, basic_server_port)
        first_sid = agent.namespaces[SMCP_NAMESPACE]
        await agent.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)
        assert len(office_server.join_record) == 1
        assert office_server.join_record[0][0] == first_sid

        office_server.joined.clear()
        ws = agent.eio.ws
        assert ws is not None
        await ws.close()
        new_sid = await _wait_for_new_sid(agent, first_sid)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)

        assert len(office_server.join_record) == 2, f"重连后未重放入房：{office_server.join_record}"
        sid, payload = office_server.join_record[-1]
        assert sid == new_sid, "重放的 join 必须来自重连后的新 SID"
        assert payload["office_id"] == _OFFICE
        assert payload["role"] == "agent"
        assert payload["name"] == _AGENT_NAME
    finally:
        await agent.disconnect()


@pytest.mark.asyncio
async def test_agent_rejected_rejoin_clears_desired_office(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回房被拒（如旧会话未回收导致的一房一 Agent 拒绝）→ 清空意图，不得保留假成员状态。

    **必须断言走的是「被拒」分支而非「异常/超时」分支**：两条分支都会清空 intent，只断言
    ``_desired_office is None`` 时，替身一侧写坏（如抛 NameError）会让用例改走异常分支而**照样全绿**
    ——本单实锤踩到过。故用日志把「客户端确实读到了协议码」钉死。
    Assert the rejection branch (not the exception path) was taken, via the logged protocol code.
    """
    # #218：拒绝日志由**共享产出者**（utils.office，两路径同文案）产出 ⇒ 打桩目标随之迁移；
    # 若仍打桩 agent.client 的模块 logger，断言会**静默失空**（调用列表为空反而看不出问题）。
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)

    office_server.reject_from = 2
    agent = _make_agent()
    try:
        await _connect(agent, basic_server_port)
        first_sid = agent.namespaces[SMCP_NAMESPACE]
        await agent.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)

        office_server.joined.clear()
        ws = agent.eio.ws
        assert ws is not None
        await ws.close()
        await _wait_for_new_sid(agent, first_sid)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)

        assert len(office_server.join_record) == 2
        await _wait_until(
            lambda: agent._desired_office is None,
            "回房被拒后回房意图未清空 / desired office not cleared after a rejected replay",
        )
        # 被拒分支确实被走到（而非异常/超时分支）：客户端读到了协议码 4101
        logged = " ".join(str(call) for call in fake_logger.error.call_args_list)
        assert "被拒绝" in logged and "4101" in logged, f"应走「被拒」分支并读到协议码，实得日志：{logged}"

        # 单次尝试：被拒后不得重试
        await asyncio.sleep(_QUIESCENCE)
        assert len(office_server.join_record) == 2, "回房被拒后不得重试"
    finally:
        await agent.disconnect()


# ── #218 C1：显式 join 等 ACK，被拒可感（真实服务端裁决）──────────────────────────


@pytest.mark.asyncio
async def test_explicit_join_rejected_by_real_server_raises_with_code(
    office_server: _OfficeRecordingNamespace,  # 请求它是为了启动服务器（副作用），非断言对象
    basic_server_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次入房被**真实服务端**拒绝 ⇒ 抛 ``SMCPProtocolError`` 且 ``.code`` 可机器分流。

    裁决来自真实 ``enter_room`` 闸门（房内已有 Agent ⇒ 4101），**非**替身 ack：这是 #219 要求的
    「经 SDK API + 真实服务端」场景。改前该拒绝对调用方完全不可感（不抛、不告警）。
    """
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)

    first = _make_agent()
    second = _make_agent()
    try:
        await _connect(first, basic_server_port)
        await first.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        assert first._confirmed_office == (_OFFICE, _AGENT_NAME), "成功入房必须落账已确认成员关系"

        await _connect(second, basic_server_port)
        with pytest.raises(SMCPProtocolError) as ei:
            await second.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

        assert ei.value.code == 4101, f"应带真实协议码，实得 {ei.value!r}"
        assert second._desired_office is None, "首次被拒（无已确认房可回退）⇒ 清空意图"
        assert second._confirmed_office is None

        # 同一产出者、同一文案：拒绝日志含协议码（与重放路径共用 utils.office 的产出者）
        logged = " ".join(str(call) for call in fake_logger.error.call_args_list)
        assert "4101" in logged, f"显式路径的拒绝必须经共享产出者记录，实得：{logged}"
    finally:
        await first.disconnect()
        await second.disconnect()


@pytest.mark.asyncio
async def test_agent_manual_disconnect_drops_membership_intent(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """手工断开清空意图：再次连接不得静默回旧房间。"""
    agent = _make_agent()
    try:
        await _connect(agent, basic_server_port)
        await agent.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)
        assert len(office_server.join_record) == 1

        await agent.disconnect()
        assert agent._desired_office is None, "手工断开后回房意图必须清空"

        await _connect(agent, basic_server_port)
        await _wait_until(
            lambda: SMCP_NAMESPACE in agent.namespaces,
            "重新连接后命名空间未建立 / namespace not established on reconnect",
        )
        await asyncio.sleep(_QUIESCENCE)
        assert len(office_server.join_record) == 1, "手工断开后的连接不得自动回房"
    finally:
        await agent.disconnect()


@pytest.mark.asyncio
async def test_agent_leave_office_then_reconnect_does_not_rejoin(
    office_server: _OfficeRecordingNamespace,
    basic_server_port: int,
) -> None:
    """显式退房后断线重连不回房。"""
    agent = _make_agent()
    try:
        await _connect(agent, basic_server_port)
        first_sid = agent.namespaces[SMCP_NAMESPACE]
        await agent.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        await asyncio.wait_for(office_server.joined.wait(), timeout=_CONNECT_TIMEOUT)
        await agent.leave_office(_OFFICE, namespace=SMCP_NAMESPACE)
        assert agent._desired_office is None

        ws = agent.eio.ws
        assert ws is not None
        await ws.close()
        await _wait_for_new_sid(agent, first_sid)
        await asyncio.sleep(_QUIESCENCE)

        assert len(office_server.join_record) == 1, "显式退房后重连不得回房"
    finally:
        await agent.disconnect()
