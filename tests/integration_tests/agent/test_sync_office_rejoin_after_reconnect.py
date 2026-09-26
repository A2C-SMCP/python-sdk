# -*- coding: utf-8 -*-
"""
#203 自动重连后恢复 Office 成员关系（Agent 侧，同步，真实 wire）。

与异步侧同语义（见 ``test_office_rejoin_after_reconnect.py``），但走**同步客户端**
（``socketio.Client`` 的后台线程模型）：回房必须另起 daemon 线程（connect 钩子内联在读循环的
分发线程上，同步 ``call`` 会阻塞该线程并拖住连接完成信号），线程不可取消 ⇒ 靠 generation 双检
作废陈旧结果。

Same semantics as the async Agent test, on the sync client: the replay runs on a daemon thread
(the connect hook runs inline on the read-loop dispatch thread, where a blocking ack wait would
stall the connect-completion signal) and staleness is handled by the generation guard alone.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from socketio import Server, WSGIApp
from werkzeug.serving import make_server

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.testing import create_local_sync_server
from a2c_smcp.utils import office as office_mod
from tests.integration_tests.mock_sync_smcp_server import MockSyncSMCPNamespace

_CONNECT_TIMEOUT = 10.0
_WAIT_INTERVAL = 0.01
_QUIESCENCE = 0.3
_OFFICE = "sync-agent-rejoin-office"
_AGENT_NAME = "sync-agent-rejoin-1"


class _RecordingSyncNamespace(MockSyncSMCPNamespace):
    """按到达顺序记录 ``server:join_office``（跨 SID，故 append）并给出线程安全的等待原语。"""

    def __init__(self) -> None:
        super().__init__()
        self.join_record: list[tuple[str, dict]] = []
        self.join_event = threading.Event()
        self.reject_from: int | None = None

    def on_server_join_office(self, sid: str, data: Any) -> Any:  # #214：成功 = None；失败 = flat ErrorPayload
        if self.reject_from is not None and len(self.join_record) + 1 >= self.reject_from:
            payload = dict(data)
            self.join_record.append((sid, payload))
            self.join_event.set()
            # v0.5.0（#214）：失败 = flat ErrorPayload（顶层含 code）。旧元组形态已废除——
            # 留在旧契约会让替身与真实服务端分叉，且用例只覆盖「形状不认识」分支，
            # 覆盖不到 4101 的带码分支（#212 的退避重试正是消费这一支：瞬态冲突 ⇒ 重试）。
            return {
                "code": 4101,
                "message": "Room already has an agent",
                "details": {"office_id": payload.get("office_id")},
            }
        result = super().on_server_join_office(sid, data)
        self.join_record.append((sid, dict(data)))
        self.join_event.set()
        return result


class _ServerThread(threading.Thread):
    """Werkzeug 多线程 WSGI 服务器（``threaded=True`` 必需）。"""

    def __init__(self, app: Any, port: int) -> None:
        super().__init__(daemon=True)
        self.server = make_server("127.0.0.1", port, app, threaded=True)

    def run(self) -> None:
        self.server.serve_forever()

    def stop(self) -> None:
        self.server.shutdown()


@pytest.fixture
def sync_office_server(sync_server_port: int) -> Iterator[tuple[_RecordingSyncNamespace, int]]:
    sio = Server(
        cors_allowed_origins="*",
        ping_timeout=10,
        ping_interval=10,
        async_handlers=False,
    )
    sio.eio.start_service_task = False
    ns = _RecordingSyncNamespace()
    sio.register_namespace(ns)
    thread = _ServerThread(WSGIApp(sio, socketio_path="/socket.io"), sync_server_port)
    thread.start()
    try:
        yield ns, sync_server_port
    finally:
        thread.stop()
        thread.join(timeout=5)


@pytest.fixture
def sync_server_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_agent() -> SMCPAgentClient:
    auth = DefaultAgentAuthProvider(agent_id=_AGENT_NAME, office_id=_OFFICE)
    return SMCPAgentClient(
        auth_provider=auth,
        reconnection=True,
        reconnection_attempts=10,
        reconnection_delay=0.1,
        reconnection_delay_max=0.3,
    )


def _wait_until(predicate: Any, message: str, timeout: float = _CONNECT_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(_WAIT_INTERVAL)


def test_sync_agent_rejoins_office_after_auto_reconnect(
    sync_office_server: tuple[_RecordingSyncNamespace, int],
) -> None:
    """自动重连后，同步 Agent 必须重放入房，新 SID 重新成为 Office 成员（服务端可见）。"""
    ns, port = sync_office_server
    agent = _make_agent()
    try:
        agent.connect_to_server(f"http://localhost:{port}", socketio_path="/socket.io")
        first_sid = agent.namespaces[SMCP_NAMESPACE]
        agent.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        assert ns.join_event.wait(timeout=_CONNECT_TIMEOUT), "首连入房未被服务端收到"
        assert len(ns.join_record) == 1
        assert ns.join_record[0][0] == first_sid

        # 掐断底层 WebSocket（不发 CLOSE 包）→ TRANSPORT_ERROR → 自动重连（后台线程）
        ns.join_event.clear()
        ws = agent.eio.ws
        assert ws is not None, "握手后应已升级 websocket / transport upgraded to websocket"
        # 硬掐 TCP（不发 CLOSE 帧）：websocket-client 的 close() 走优雅握手机制，与服务端
        # 读循环存在竞态（实测 ~12% 不触发重连），abort() 直接断链，稳定落 TRANSPORT_ERROR。
        ws.abort()

        _wait_until(
            lambda: agent.namespaces.get(SMCP_NAMESPACE) not in (None, first_sid),
            "重连后命名空间未在期限内重建 / namespace not re-established after reconnect",
        )
        new_sid = agent.namespaces[SMCP_NAMESPACE]
        assert ns.join_event.wait(timeout=_CONNECT_TIMEOUT), "重连后未重放入房 / no replay after reconnect"

        assert len(ns.join_record) == 2, f"重连后未重放入房：{ns.join_record}"
        sid, payload = ns.join_record[-1]
        assert sid == new_sid, "重放的 join 必须来自重连后的新 SID"
        assert payload["office_id"] == _OFFICE
        assert payload["role"] == "agent"
        assert payload["name"] == _AGENT_NAME
    finally:
        agent.disconnect()


def test_sync_agent_manual_disconnect_drops_membership_intent(
    sync_office_server: tuple[_RecordingSyncNamespace, int],
) -> None:
    """手工断开清空意图：再次连接不得静默回旧房间。"""
    ns, port = sync_office_server
    agent = _make_agent()
    try:
        agent.connect_to_server(f"http://localhost:{port}", socketio_path="/socket.io")
        agent.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        assert ns.join_event.wait(timeout=_CONNECT_TIMEOUT)
        assert len(ns.join_record) == 1

        agent.disconnect()
        assert agent._desired_office is None, "手工断开后回房意图必须清空"

        agent.connect_to_server(f"http://localhost:{port}", socketio_path="/socket.io")
        _wait_until(
            lambda: SMCP_NAMESPACE in agent.namespaces,
            "重新连接后命名空间未建立 / namespace not established on reconnect",
        )
        time.sleep(_QUIESCENCE)
        assert len(ns.join_record) == 1, "手工断开后的连接不得自动回房"
    finally:
        agent.disconnect()


def test_sync_agent_rejected_rejoin_clears_desired_office(
    sync_office_server: tuple[_RecordingSyncNamespace, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回房持续被拒 → 退避重试到**预算耗尽**后清空意图（同步镜像 async 侧）。

    **必须断言走的是「被拒」分支而非「异常/超时」分支**：两条分支都会清空 intent，只断言
    ``_desired_office is None`` 时，替身一侧写坏会让用例改走异常分支而照样全绿（async 版实锤）。
    故用日志把「客户端确实读到了协议码」钉死。/ Assert the rejection branch via the logged code.
    """
    # #218：拒绝日志由**共享产出者**（utils.office，两路径同文案）产出 ⇒ 打桩目标随之迁移。
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)

    ns, port = sync_office_server
    ns.reject_from = 2
    agent = _make_agent()
    # #212：预算压到极小 ⇒ 几次重试后立即认输（默认 45s 会让本用例白跑满预算）。
    agent.office_rejoin_retry_budget = 0.3
    try:
        agent.connect_to_server(f"http://localhost:{port}", socketio_path="/socket.io")
        first_sid = agent.namespaces[SMCP_NAMESPACE]
        agent.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        assert ns.join_event.wait(timeout=_CONNECT_TIMEOUT)

        ns.join_event.clear()
        ws = agent.eio.ws
        assert ws is not None
        ws.abort()  # 硬掐 TCP（同主用例）/ hard TCP kill, same as the main case
        _wait_until(
            lambda: agent.namespaces.get(SMCP_NAMESPACE) not in (None, first_sid),
            "重连后命名空间未在期限内重建 / namespace not re-established after reconnect",
        )
        assert ns.join_event.wait(timeout=_CONNECT_TIMEOUT), "回房请求未被服务端收到"

        _wait_until(
            lambda: agent._desired_office is None,
            "重试预算耗尽后回房意图未清空 / desired office not cleared after the budget ran out",
        )
        # 被拒分支确实被走到（而非异常/超时分支）：客户端读到了协议码 4101
        logged = " ".join(str(call) for call in fake_logger.error.call_args_list)
        assert "被拒绝" in logged and "4101" in logged, f"应走「被拒」分支并读到协议码，实得日志：{logged}"

        # #212：瞬态冲突（4101）在预算内必须重试（行为变更：此前一次即终）
        assert len(ns.join_record) >= 3, "瞬态冲突必须重试（至少多打一次）"
    finally:
        agent.disconnect()


def test_sync_agent_rejoin_self_heals_after_transient_conflict(
    sync_office_server: tuple[_RecordingSyncNamespace, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**本单的目标形态**（同步镜像）：撞尚未回收的旧会话 → 退避重试 → 回收后自动回房成功、无 ERROR。

    与 async 侧同样用**替身开关**造拒绝（真实回收窗口由 Computer 侧用例覆盖）；本用例证的是同步侧
    的重试与落账。
    """
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)

    ns, port = sync_office_server
    ns.reject_from = 2
    agent = _make_agent()
    agent.office_rejoin_retry_budget = 5.0
    try:
        agent.connect_to_server(f"http://localhost:{port}", socketio_path="/socket.io")
        first_sid = agent.namespaces[SMCP_NAMESPACE]
        agent.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        assert ns.join_event.wait(timeout=_CONNECT_TIMEOUT)

        ns.join_event.clear()
        ws = agent.eio.ws
        assert ws is not None
        ws.abort()
        _wait_until(
            lambda: agent.namespaces.get(SMCP_NAMESPACE) not in (None, first_sid),
            "重连后命名空间未在期限内重建 / namespace not re-established after reconnect",
        )
        assert ns.join_event.wait(timeout=_CONNECT_TIMEOUT), "回房请求未被服务端收到"
        # 前置断言用 `>=`：退避窗口内计数不再静止（同 async 侧）。
        assert len(ns.join_record) >= 2, "前置：重放确已被拒一次"

        ns.reject_from = None  # 服务端此刻已完成回收 ⇒ 下一次重试应成功
        _wait_until(
            lambda: agent._confirmed_office == (_OFFICE, _AGENT_NAME),
            "重试未自愈 / the retry did not restore the office membership",
        )

        assert len(ns.join_record) >= 3, "自愈必须靠一次真实重放（而非替身放行）"
        assert ns.join_record[-1][0] == agent.namespaces[SMCP_NAMESPACE], "重放须来自当前 SID"
        fake_logger.error.assert_not_called()
    finally:
        agent.disconnect()


# ── #218 C1：显式 join 等 ACK，被拒可感（真实服务端裁决，同步镜像）──────────────


@pytest.fixture
def real_sync_server(sync_server_port: int) -> Iterator[int]:
    """**真实**同步 SMCP 服务端（``LocalSyncSMCPNamespace`` 继承正式实现，含 ``enter_room`` 闸门）。

    本文件其余用例的替身命名空间重写了 ``on_server_join_office`` 而**不经过**房间闸门，故「被真实
    服务端拒绝」这一格必须换用正式实现，否则测的是替身自己的分支。/
    The other tests in this file use a stand-in namespace that bypasses the room gates; the
    "rejected by the real server" case needs the production implementation.
    """
    sio, _ns, app = create_local_sync_server()
    sio.eio.start_service_task = False
    thread = _ServerThread(app, sync_server_port)
    thread.start()
    try:
        yield sync_server_port
    finally:
        thread.stop()
        thread.join(timeout=5)


def test_sync_explicit_join_rejected_by_real_server_raises_with_code(
    real_sync_server: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次入房被**真实服务端**拒绝（房内已有 Agent ⇒ 4101）⇒ 抛 ``SMCPProtocolError``。

    同步侧该调用由「发射即返回」变为**阻塞调用**（最多 OFFICE_JOIN_TIMEOUT）；被拒可感与异步逐字
    同构（同一效应表、同一日志产出者）。
    """
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)

    first = _make_agent()
    second = _make_agent()
    try:
        first.connect_to_server(f"http://localhost:{real_sync_server}", socketio_path="/socket.io")
        first.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        assert first._confirmed_office == (_OFFICE, _AGENT_NAME), "成功入房必须落账已确认成员关系"

        second.connect_to_server(f"http://localhost:{real_sync_server}", socketio_path="/socket.io")
        with pytest.raises(SMCPProtocolError) as ei:
            second.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)

        assert ei.value.code == 4101, f"应带真实协议码，实得 {ei.value!r}"
        assert second._desired_office is None, "首次被拒（无已确认房可回退）⇒ 清空意图"
        assert second._confirmed_office is None

        logged = " ".join(str(call) for call in fake_logger.error.call_args_list)
        assert "4101" in logged, f"显式路径的拒绝必须经共享产出者记录，实得：{logged}"
    finally:
        first.disconnect()
        second.disconnect()


def test_sync_explicit_and_replay_rejections_are_byte_identical_on_real_server(
    real_sync_server: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#219（同步镜像）：显式 join 与重放被真实闸门以同一理由（4101）拒绝 ⇒ ERROR 文本逐字相同。

    重放在同一 sid 上直接调用（同异步侧）：被拒 sid 在服务端无房间残留，与重连后的新 sid 同构。
    """
    fake_logger = MagicMock()
    monkeypatch.setattr(office_mod, "logger", fake_logger)

    first = _make_agent()
    second = _make_agent()
    second.office_rejoin_retry_budget = 0.0  # 单次尝试：本用例比的是文案，不是退避
    try:
        first.connect_to_server(f"http://localhost:{real_sync_server}", socketio_path="/socket.io")
        first.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        second.connect_to_server(f"http://localhost:{real_sync_server}", socketio_path="/socket.io")
        with pytest.raises(SMCPProtocolError) as ei:
            second.join_office(_OFFICE, _AGENT_NAME, namespace=SMCP_NAMESPACE)
        assert ei.value.code == 4101

        second._desired_office = (_OFFICE, _AGENT_NAME)
        second._rejoin_office((_OFFICE, _AGENT_NAME), second._office_generation)

        texts = [call.args[0] for call in fake_logger.error.call_args_list]
        assert len(texts) == 2, f"两条路径须各产出一条共享 ERROR，实得：{texts}"
        assert texts[0] == texts[1] and "4101" in texts[0], f"首次被拒与重放被拒的可感结果须同态：{texts}"
    finally:
        first.disconnect()
        second.disconnect()
