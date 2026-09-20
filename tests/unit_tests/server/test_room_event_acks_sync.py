# -*- coding: utf-8 -*-
"""
* 文件名: test_room_event_acks_sync
* 描述: #214 —— 三个房间事件 ack 形态契约的**同步**镜像（async 为
        ``test_room_event_acks.py``）。

        除逐条镜像外，本文件末尾的 ``TestSyncAsyncPayloadParity`` 直接对拍两实现的**线上载荷**：
        payload 必须**逐字段一致**（双实现镜像约束），只测各自终端形态抓不到「sync 传错上下文」
        （如 4106 误把目标房当当前房）。/ Mirrors the async contract tests and additionally
        asserts byte-level payload parity between the sync and async implementations.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from a2c_smcp.server import SMCPNamespace, SyncAuthenticationProvider, SyncSMCPNamespace
from a2c_smcp.smcp import SMCP_NAMESPACE

PEER_SID = "PEER-SID-SENTINEL-9f3a"
PEER_NAME = "peer-name-sentinel"
NAMESPACE_NAME = SMCP_NAMESPACE


def _namespace(
    sessions: dict[str, dict[str, Any]],
    participants: list[tuple[str, str]] | None = None,
) -> SyncSMCPNamespace:
    """构造一个会话表可控、socketio 层被 mock 的 SyncSMCPNamespace（镜像 async 版）。"""
    ns = SyncSMCPNamespace(MagicMock(spec=SyncAuthenticationProvider))
    ns.server = MagicMock()
    ns.server.enter_room = MagicMock()
    ns.server.leave_room = MagicMock()
    ns.server.rooms = MagicMock(return_value=[])
    ns.server.manager = MagicMock()
    ns.server.manager.get_participants = MagicMock(return_value=list(participants or []))
    ns.get_session = MagicMock(side_effect=lambda sid: sessions[sid])
    ns.save_session = MagicMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
    ns.emit = MagicMock()
    return ns


def _assert_no_leak(payload: dict[str, Any]) -> None:
    """失败 ack 的 message / details MUST NOT 含其它会话的内部标识。"""
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    for sentinel in (PEER_SID, PEER_NAME, NAMESPACE_NAME):
        assert sentinel not in blob, f"失败 ack 泄露了 {sentinel!r}: {blob}"


class TestJoinOfficeAckShapeSync:
    """`server:join_office` 的 ack 形态（同步）。"""

    def test_success_is_empty_ack(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"sid-1": {}}
        ns = _namespace(sessions)

        ack = ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert ack is None, f"成功必须是空 ack（None），实得 {ack!r}"
        assert sessions["sid-1"]["office_id"] == "room-a"

    @pytest.mark.parametrize(
        "bad_payload",
        [{}, {"role": "agent"}, {"role": "not-a-role", "name": "n", "office_id": "o"}, None, "not-a-dict"],
    )
    def test_malformed_payload_acks_400(self, bad_payload: Any) -> None:
        """载荷 schema 校验失败 ⇒ 回 `400`，**绝不允许**静默不 ack。"""
        ns = _namespace({"sid-1": {}})

        ack = ns.on_server_join_office("sid-1", bad_payload)

        assert ack is not None, "载荷畸形必须产出 ack（否则客户端挂起到自身超时）"
        assert ack["code"] == 400, f"载荷畸形应回 400，实得 {ack!r}"
        assert "details" not in ack, f"400 不得回显客户端输入: {ack!r}"

    def test_role_mismatch_is_403(self) -> None:
        ns = _namespace({"sid-1": {"role": "agent"}})

        ack = ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 403, ack
        assert "details" not in ack, ack
        _assert_no_leak(ack)

    def test_room_full_is_4101_with_rejected_target_room(self) -> None:
        sessions: dict[str, dict[str, Any]] = {
            "sid-1": {"role": "agent"},
            PEER_SID: {"role": "agent", "name": PEER_NAME, "office_id": "room-a"},
        }
        ns = _namespace(sessions, participants=[(PEER_SID, "eio-peer")])

        ack = ns.on_server_join_office("sid-1", {"role": "agent", "name": "a1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 4101, ack
        assert ack["details"] == {"office_id": "room-a"}, ack
        _assert_no_leak(ack)

    def test_agent_switch_is_4106_with_current_room(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-old"}}
        ns = _namespace(sessions)

        ack = ns.on_server_join_office("sid-1", {"role": "agent", "name": "a1", "office_id": "room-new"})

        assert isinstance(ack, dict) and ack["code"] == 4106, ack
        assert ack["details"] == {"office_id": "room-old"}, (
            f"4106 的 details.office_id 必须是**当前**房而非目标房: {ack!r}"
        )
        assert sessions["sid-1"]["office_id"] == "room-old"
        _assert_no_leak(ack)

    def test_computer_same_name_in_target_room_is_4105(self) -> None:
        sessions: dict[str, dict[str, Any]] = {
            "sid-1": {"role": "computer", "name": "dup"},
            PEER_SID: {"role": "computer", "name": "dup", "office_id": "room-b"},
        }
        ns = _namespace(sessions, participants=[("sid-1", "eio-self"), (PEER_SID, "eio-peer")])

        ack = ns.on_server_join_office("sid-1", {"role": "computer", "name": "dup", "office_id": "room-b"})

        assert isinstance(ack, dict) and ack["code"] == 4105, ack
        assert ack["details"] == {"office_id": "room-b", "role": "computer"}, ack
        _assert_no_leak(ack)

    def test_registry_conflict_is_4105_without_peer_sid(self) -> None:
        ns = _namespace({"sid-1": {"role": "computer", "name": "taken"}})
        ns._name_to_sid_map = {"taken": PEER_SID}

        ack = ns.on_server_join_office("sid-1", {"role": "computer", "name": "taken", "office_id": "room-b"})

        assert isinstance(ack, dict) and ack["code"] == 4105, ack
        assert ack["details"] == {"office_id": "room-b", "role": "computer"}, ack
        _assert_no_leak(ack)  # 回归守卫：旧实现把 "sid '<peer>'" 自由文本塞进 error_msg

    def test_missing_payload_still_acks(self) -> None:
        """**参数绑定失败也必须回 ack**（同步镜像）：零参包 ⇒ 400。"""
        ns = _namespace({"sid-1": {}})

        ack = ns.on_server_join_office("sid-1")

        assert isinstance(ack, dict) and ack["code"] == 400, ack

    def test_valid_payload_plus_extra_argument_is_rejected(self) -> None:
        """合法载荷 + 多余位置参数 ⇒ 400（不静默忽略；同步镜像）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {}}
        ns = _namespace(sessions)

        ack = ns.on_server_join_office(
            "sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"}, "unexpected"
        )

        assert isinstance(ack, dict) and ack["code"] == 400, ack
        assert "office_id" not in sessions["sid-1"], "多参必须在**产生副作用之前**被拒"

    def test_registry_conflict_diagnostic_goes_to_log_not_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """名字冲突诊断（含对端 sid）进日志、不进 ack（同步镜像；双向断言的理由见 async 版）。"""
        from a2c_smcp.server import sync_base as sync_base_mod

        fake_logger = MagicMock()
        monkeypatch.setattr(sync_base_mod, "logger", fake_logger)

        ns = _namespace({"sid-1": {"role": "computer", "name": "taken"}})
        ns._name_to_sid_map = {"taken": PEER_SID}

        ack = ns.on_server_join_office("sid-1", {"role": "computer", "name": "taken", "office_id": "room-b"})

        logged = " ".join(str(call) for call in fake_logger.warning.call_args_list)
        assert PEER_SID in logged, "诊断（含对端 sid）必须留在服务端日志里"
        assert PEER_SID not in json.dumps(ack, ensure_ascii=False), "但绝不可进入 ack 载荷"

    def test_unmapped_rejection_code_degrades_to_500_not_escape(self) -> None:
        """未接入码表的领域拒绝 ⇒ 回 500（同步镜像；理由见 async 版）。"""
        from a2c_smcp.exceptions import RoomRejection

        ns = _namespace({"sid-1": {}})
        ns.enter_room = MagicMock(side_effect=RoomRejection())

        ack = ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack

    def test_non_int_rejection_code_degrades_to_500_not_escape(self) -> None:
        """`code` **非 int** 的领域拒绝 ⇒ 500（同步镜像；理由见 async 版）。"""
        from a2c_smcp.exceptions import RoomRejection

        class NoneCodeRejection(RoomRejection):
            code = None  # type: ignore[assignment]

        ns = _namespace({"sid-1": {}})
        ns.enter_room = MagicMock(side_effect=NoneCodeRejection())

        ack = ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack

    def test_unknown_exception_is_500_without_echoing_detail(self) -> None:
        ns = _namespace({"sid-1": {}})
        ns.enter_room = MagicMock(side_effect=RuntimeError("boom-internal-detail"))

        ack = ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert "Internal server error" not in ack["message"], ack
        assert "boom-internal-detail" not in json.dumps(ack, ensure_ascii=False), ack

    @pytest.mark.parametrize("event", ["leave_office", "list_room"])
    def test_valid_payload_plus_extra_argument_is_rejected_for_all_room_events(self, event: str) -> None:
        """三个房间事件的 `_extra` 分支都要有覆盖（同步镜像）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)
        ns.leave_room = MagicMock()

        if event == "leave_office":
            ack = ns.on_server_leave_office("sid-1", {"office_id": "room-a"}, "unexpected")
        else:
            ack = ns.on_server_list_room(
                "sid-1", {"agent": "a1", "req_id": "r-1", "office_id": "room-a"}, "unexpected"
            )

        assert isinstance(ack, dict) and ack["code"] == 400, ack
        assert sessions["sid-1"]["office_id"] == "room-a", "多参必须在产生副作用之前被拒"
        ns.leave_room.assert_not_called()


class TestLeaveOfficeAckShapeSync:
    """`server:leave_office` 的 ack 形态（同步）。"""

    def test_success_is_empty_ack(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)

        ack = ns.on_server_leave_office("sid-1", {"office_id": "room-a"})

        assert ack is None, f"成功必须是空 ack（None），实得 {ack!r}"
        assert "office_id" not in sessions["sid-1"]

    def test_no_room_is_idempotent_empty_ack(self) -> None:
        ns = _namespace({"sid-1": {"role": "agent"}})

        ack = ns.on_server_leave_office("sid-1", {"office_id": "room-a"})

        assert ack is None, f"无房退房是幂等成功，不得回错误载荷，实得 {ack!r}"

    @pytest.mark.parametrize("bad_payload", [{}, {"office_id": 123}, {"office_id": None}, None, "x"])
    def test_malformed_payload_acks_400(self, bad_payload: Any) -> None:
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)

        ack = ns.on_server_leave_office("sid-1", bad_payload)

        assert isinstance(ack, dict) and ack["code"] == 400, ack
        assert sessions["sid-1"]["office_id"] == "room-a"

    def test_payload_room_mismatch_is_still_success(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)

        ack = ns.on_server_leave_office("sid-1", {"office_id": "room-OTHER"})

        assert ack is None, f"载荷与实情不符不得构成拒绝，实得 {ack!r}"
        assert "office_id" not in sessions["sid-1"]

    def test_internal_error_is_500(self) -> None:
        ns = _namespace({"sid-1": {"role": "agent", "office_id": "room-a"}})
        ns.leave_room = MagicMock(side_effect=RuntimeError("boom-internal-detail"))

        ack = ns.on_server_leave_office("sid-1", {"office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert "boom-internal-detail" not in json.dumps(ack, ensure_ascii=False), ack


class TestListRoomAckShapeSync:
    """`server:list_room` 的 ack 形态（同步）。"""

    def test_success_returns_list_room_ret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ns = _namespace({"sid-1": {"role": "agent", "office_id": "room-a"}})
        monkeypatch.setattr(
            "a2c_smcp.server.sync_namespace.get_all_sessions_in_office",
            MagicMock(return_value=[{"sid": "sid-1", "name": "a1", "role": "agent", "office_id": "room-a"}]),
        )

        ret = ns.on_server_list_room("sid-1", {"agent": "a1", "req_id": "r-1", "office_id": "room-a"})

        assert ret["req_id"] == "r-1"
        assert len(ret["sessions"]) == 1

    @pytest.mark.parametrize("bad_payload", [{}, {"agent": "a"}, {"office_id": "o"}, None])
    def test_malformed_payload_acks_400(self, bad_payload: Any) -> None:
        ns = _namespace({"sid-1": {"role": "agent", "office_id": "room-a"}})

        ack = ns.on_server_list_room("sid-1", bad_payload)

        assert isinstance(ack, dict) and ack["code"] == 400, ack

    def test_not_in_room_is_4103(self) -> None:
        ns = _namespace({"sid-1": {"role": "agent"}})

        ack = ns.on_server_list_room("sid-1", {"agent": "a1", "req_id": "r-1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 4103, ack
        assert "details" not in ack, f"4103 无 details（会话自身无房可报）: {ack!r}"

    def test_cross_room_is_4104_with_rejected_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ns = _namespace({"sid-1": {"role": "agent", "office_id": "room-a"}})
        leaked = [{"sid": PEER_SID, "name": PEER_NAME, "role": "computer", "office_id": "room-b"}]
        monkeypatch.setattr(
            "a2c_smcp.server.sync_namespace.get_all_sessions_in_office",
            MagicMock(return_value=leaked),
        )

        ack = ns.on_server_list_room("sid-1", {"agent": "a1", "req_id": "r-1", "office_id": "room-b"})

        assert isinstance(ack, dict) and ack["code"] == 4104, ack
        assert ack["details"] == {"office_id": "room-b"}, ack
        _assert_no_leak(ack)

    def test_originator_session_gone_is_acked_not_raised(self) -> None:
        """发起者会话取不到 ⇒ 仍**产出 ack**（500），不抛出去让调用方挂到超时（同步镜像）。"""
        ns = _namespace({})
        ns.get_session = MagicMock(return_value=None)

        ack = ns.on_server_list_room("sid-gone", {"agent": "a1", "req_id": "r-1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack


class TestSyncAsyncPayloadParitySync:
    """双实现**线上载荷对拍**：同一场景下 sync 与 async 必须产出逐字段一致的 ErrorPayload。

    只测各自终端形态抓不到「sync 传错上下文」——例如 4106 误把目标房当成当前房、4105 的 role
    取成对端 role：两条断言在各自文件里都可能"看起来对"，只有对拍才暴露分歧。
    Parity check across the two implementations: the same scenario must produce identical payloads.
    """

    @staticmethod
    def _build_async(sessions: dict[str, dict[str, Any]], participants: list[tuple[str, str]]) -> SMCPNamespace:
        from unittest.mock import AsyncMock

        ns = SMCPNamespace(MagicMock())
        ns.server = MagicMock()
        ns.server.enter_room = AsyncMock()
        ns.server.leave_room = AsyncMock()
        ns.server.rooms = MagicMock(return_value=[])
        ns.server.manager = MagicMock()
        ns.server.manager.get_participants = MagicMock(return_value=list(participants))
        ns.get_session = AsyncMock(side_effect=lambda sid: sessions[sid])
        ns.save_session = AsyncMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
        ns.emit = AsyncMock()
        return ns

    async def test_join_failure_payloads_are_identical(self) -> None:
        scenarios: list[tuple[str, dict[str, Any], list[tuple[str, str]], dict[str, Any]]] = [
            (
                "role-mismatch",
                {"role": "computer", "name": "c1", "office_id": "room-a"},
                [],
                {"session": {"role": "agent"}},
            ),
            (
                "room-full",
                {"role": "agent", "name": "a1", "office_id": "room-a"},
                [(PEER_SID, "e")],
                {"session": {"role": "agent"}, PEER_SID: {"role": "agent", "name": "p", "office_id": "room-a"}},
            ),
            (
                "already-in-room",
                {"role": "agent", "name": "a1", "office_id": "room-new"},
                [],
                {"session": {"role": "agent", "office_id": "room-old"}},
            ),
            (
                "name-conflict-room",
                {"role": "computer", "name": "dup", "office_id": "room-b"},
                [("sid-1", "e"), (PEER_SID, "e2")],
                {
                    "session": {"role": "computer", "name": "dup"},
                    PEER_SID: {"role": "computer", "name": "dup", "office_id": "room-b"},
                },
            ),
            (
                "name-conflict-registry",
                {"role": "computer", "name": "taken", "office_id": "room-b"},
                [],
                {"session": {"role": "computer", "name": "taken"}, "_registry": {"taken": PEER_SID}},
            ),
            ("malformed", {}, [], {"session": {}}),
        ]

        for label, payload, participants, spec in scenarios:
            sync_sessions: dict[str, dict[str, Any]] = {}
            for key, value in spec.items():
                if key == "_registry":
                    continue
                sync_sessions["sid-1" if key == "session" else key] = dict(value)  # type: ignore[arg-type]
            async_sessions: dict[str, dict[str, Any]] = {k: dict(v) for k, v in sync_sessions.items()}

            sync_ns = _namespace(sync_sessions, participants=participants)
            if "_registry" in spec:
                sync_ns._name_to_sid_map = dict(spec["_registry"])
            async_ns = self._build_async(async_sessions, participants)
            if "_registry" in spec:
                async_ns._name_to_sid_map = dict(spec["_registry"])

            sync_ack = sync_ns.on_server_join_office("sid-1", payload)
            async_ack = await async_ns.on_server_join_office("sid-1", payload)

            assert sync_ack == async_ack, f"[{label}] sync 与 async 载荷分叉: {sync_ack!r} != {async_ack!r}"

    async def test_list_room_failure_payloads_are_identical(self) -> None:
        for label, session, payload in [
            ("not-in-room", {"role": "agent"}, {"agent": "a1", "req_id": "r", "office_id": "room-a"}),
            ("cross-room", {"role": "agent", "office_id": "room-a"}, {"agent": "a1", "req_id": "r", "office_id": "room-b"}),
            ("malformed", {"role": "agent", "office_id": "room-a"}, {}),
        ]:
            sync_ack = _namespace({"sid-1": dict(session)}).on_server_list_room("sid-1", payload)
            async_ack = await self._build_async({"sid-1": dict(session)}, []).on_server_list_room("sid-1", payload)

            assert sync_ack == async_ack, f"[{label}] sync 与 async 载荷分叉: {sync_ack!r} != {async_ack!r}"

    async def test_leave_failure_payloads_are_identical(self) -> None:
        for label, payload in [("malformed", {}), ("malformed-null", None)]:
            sync_ack = _namespace({"sid-1": {"role": "agent", "office_id": "room-a"}}).on_server_leave_office(
                "sid-1", payload
            )
            async_ack = await self._build_async(
                {"sid-1": {"role": "agent", "office_id": "room-a"}}, []
            ).on_server_leave_office("sid-1", payload)

            assert sync_ack == async_ack, f"[{label}] sync 与 async 载荷分叉: {sync_ack!r} != {async_ack!r}"
