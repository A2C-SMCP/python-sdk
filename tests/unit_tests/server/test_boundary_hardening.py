# -*- coding: utf-8 -*-
"""
* 文件名: test_boundary_hardening
* 描述: #216 —— 取值域与边界校验加固（async + sync 双路径，同一用例体参数化跑两套实现）。

        协议依据 / Protocol: a2c-smcp-protocol develop（protocol#61 自查 A / B + office_id 取值域，PR#62）
          - security.md §数据验证要求：载荷 schema 校验是 MUST，禁止依赖类型标注；
          - error-handling.md §错误响应格式：具备 ack 通道的事件（全部 ``client:*`` + join/leave/list_room）
            校验失败 MUST 回 flat ``400``、MUST NOT 静默不 ack；fire-and-forget 事件非法即静默丢弃，
            MUST NOT 为此新增 ack；
          - room-model.md §房间标识：``office_id`` MUST NOT 与 SID 命名空间重叠；
          - room-model.md §跨房间访问防护：无房 ⇒ ``4103``；房内解析不到 ⇒ ``404``。

        错误码断言用协议字面值（wire contract）。

#216 contract tests: boundary validation, office-less / non-agent ``client:*`` rejections as flat
payloads, fire-and-forget drops, and the ``office:`` room-name namespace.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.server import AuthenticationProvider, SMCPNamespace, SyncAuthenticationProvider, SyncSMCPNamespace
from a2c_smcp.smcp import ErrorCode, build_room_rejection_error

OK_RET: dict[str, Any] = {"tools": [], "req_id": "r"}


def _async_ns(sessions: dict[str, dict[str, Any]], rooms: list[str] | None = None) -> SMCPNamespace:
    ns = SMCPNamespace(MagicMock(spec=AuthenticationProvider))
    ns.server = MagicMock()
    ns.server.enter_room = AsyncMock()
    ns.server.leave_room = AsyncMock()
    ns.server.rooms = MagicMock(return_value=list(rooms or []))
    ns.server.manager = MagicMock()
    ns.server.manager.get_participants = MagicMock(return_value=[])
    ns.get_session = AsyncMock(side_effect=lambda sid: sessions.get(sid))
    ns.save_session = AsyncMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
    ns.emit = AsyncMock()
    ns.call = AsyncMock(return_value=dict(OK_RET))
    return ns


def _sync_ns(sessions: dict[str, dict[str, Any]], rooms: list[str] | None = None) -> SyncSMCPNamespace:
    ns = SyncSMCPNamespace(MagicMock(spec=SyncAuthenticationProvider))
    ns.server = MagicMock()
    ns.server.enter_room = MagicMock()
    ns.server.leave_room = MagicMock()
    ns.server.rooms = MagicMock(return_value=list(rooms or []))
    ns.server.manager = MagicMock()
    ns.server.manager.get_participants = MagicMock(return_value=[])
    ns.get_session = MagicMock(side_effect=lambda sid: sessions.get(sid))
    ns.save_session = MagicMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
    ns.emit = MagicMock()
    ns.call = MagicMock(return_value=dict(OK_RET))
    return ns


async def _run(value: Any) -> Any:
    """sync 实现返回值、async 实现返回协程 —— 统一取结果。"""
    return await value if inspect.isawaitable(value) else value


@pytest.fixture(params=["async", "sync"])
def make_ns(request: pytest.FixtureRequest) -> Callable[..., Any]:
    return _async_ns if request.param == "async" else _sync_ns


def _join(role: str, name: str, office: str) -> dict[str, str]:
    return {"role": role, "name": name, "office_id": office}


_BASE = {"agent": "ag", "req_id": "r", "computer": "pc"}

# 每个 client:* handler 的最小合法载荷 / minimal valid payload per client:* handler
CLIENT_ROUTES: list[tuple[str, dict[str, Any]]] = [
    ("on_client_get_tools", dict(_BASE)),
    ("on_client_get_config", dict(_BASE)),
    ("on_client_get_desktop", dict(_BASE)),
    ("on_client_get_resources", {**_BASE, "mcp_server": "srv"}),
    ("on_client_get_skills", dict(_BASE)),
    ("on_client_get_skill", {**_BASE, "name": "sk"}),
    ("on_client_get_blob", {**_BASE, "blob_handle": "h"}),
    ("on_client_put_blob", {**_BASE, "chunk_offset": 0, "eof": True, "blob": ""}),
    ("on_client_tool_call", {**_BASE, "tool_name": "t", "params": {}, "timeout": 5}),
]
ROUTE_IDS = [name for name, _ in CLIENT_ROUTES]

NOT_IN_ROOM = build_room_rejection_error(ErrorCode.NOT_IN_ROOM)


def _assert_code(ack: Any, code: int) -> None:
    assert isinstance(ack, dict) and ack.get("code") == code, f"期望 flat {code}，实得 {ack!r}"


# =====================================================================
# §四 —— client:* 的隔离拒绝以 flat ErrorPayload 承载（不再 raise ⇒ 调用方挂到超时）
# =====================================================================


class TestClientRouteRejections:
    @pytest.mark.parametrize(("handler", "payload"), CLIENT_ROUTES, ids=ROUTE_IDS)
    async def test_office_less_initiator_gets_4103(self, make_ns: Any, handler: str, payload: dict) -> None:
        """从未入房（无 role、无 office）的会话发 client:* ⇒ flat 4103，逐字节等于 builder 产出。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {"sid": "ag"}}
        ns = make_ns(sessions)

        ack = await _run(getattr(ns, handler)("ag", payload))

        assert ack == NOT_IN_ROOM, ack
        ns.call.assert_not_called()

    @pytest.mark.parametrize(("handler", "payload"), CLIENT_ROUTES, ids=ROUTE_IDS)
    async def test_left_office_initiator_gets_4103(self, make_ns: Any, handler: str, payload: dict) -> None:
        """入过房又退房（有 role、无 office）⇒ 同样 4103。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}}
        ns = make_ns(sessions)
        assert await _run(ns.on_server_join_office("ag", _join("agent", "ag", "office-a"))) is None
        assert await _run(ns.on_server_leave_office("ag", {"office_id": "office-a"})) is None

        ack = await _run(getattr(ns, handler)("ag", payload))

        assert ack == NOT_IN_ROOM, ack

    @pytest.mark.parametrize(("handler", "payload"), CLIENT_ROUTES, ids=ROUTE_IDS)
    async def test_non_agent_initiator_gets_403(self, make_ns: Any, handler: str, payload: dict) -> None:
        """已入房的 Computer 发 client:*（协议：client:* 发起方为 Agent）⇒ flat 403，且不转发。"""
        sessions: dict[str, dict[str, Any]] = {"other": {}, "pc": {}}
        ns = make_ns(sessions)
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        assert await _run(ns.on_server_join_office("other", _join("computer", "other", "office-a"))) is None

        ack = await _run(getattr(ns, handler)("other", payload))

        _assert_code(ack, 403)
        assert "details" not in ack, ack
        ns.call.assert_not_called()

    @pytest.mark.parametrize(("handler", "payload"), CLIENT_ROUTES, ids=ROUTE_IDS)
    async def test_agent_in_office_still_relays(self, make_ns: Any, handler: str, payload: dict) -> None:
        """正对照：房内 Agent → 同房 Computer 正常转发（否则上面的拒绝用例可能靠「全拒」假绿）。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = make_ns(sessions)
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        assert await _run(ns.on_server_join_office("ag", _join("agent", "ag", "office-a"))) is None
        # Computer 回 flat ErrorPayload ⇒ 各路由原样透传（免去逐事件构造成功形态）
        passthrough = {"code": 4014, "message": "MCP server not found"}
        ns.call.return_value = dict(passthrough)

        ack = await _run(getattr(ns, handler)("ag", payload))

        assert ns.call.call_count == 1
        assert ack == passthrough, ack

    async def test_originator_session_gone_stays_silent(self, make_ns: Any) -> None:
        """唯一保留的静默路径：发起者会话已不存在（协议 §飞行中断连 MAY 不 ack）。"""
        ns = make_ns({})
        with pytest.raises(SMCPNamespaceError):
            await _run(ns.on_client_get_tools("gone", dict(_BASE)))


# =====================================================================
# §一 —— 全部具备 ack 的事件：载荷非法 ⇒ 400（含绑定层：无载荷 / 多参）
# =====================================================================


def _malformed(payload: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    missing = {k: v for k, v in payload.items() if k != "computer"}
    wrong_type = {**payload, "computer": 123}
    return [
        ("missing_computer", (missing,)),
        ("none_payload", (None,)),
        ("non_dict_payload", ("pc",)),
        ("wrong_type", (wrong_type,)),
        ("extra_positional", (payload, "extra")),
    ]


MALFORMED_CASES = [
    pytest.param(handler, args, id=f"{handler}-{label}") for handler, payload in CLIENT_ROUTES for label, args in _malformed(payload)
]


class TestAckEventValidation:
    @pytest.mark.parametrize(("handler", "args"), MALFORMED_CASES)
    async def test_client_route_malformed_payload_is_400(self, make_ns: Any, handler: str, args: tuple) -> None:
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = make_ns(sessions)
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        assert await _run(ns.on_server_join_office("ag", _join("agent", "ag", "office-a"))) is None

        ack = await _run(getattr(ns, handler)("ag", *args))

        assert ack == {"code": 400, "message": "Invalid request payload"}, ack
        ns.call.assert_not_called()

    @pytest.mark.parametrize("handler", ROUTE_IDS)
    async def test_client_route_without_payload_is_400(self, make_ns: Any, handler: str) -> None:
        """零参 emit（socketio 把 None 载荷折叠成零参包）⇒ 绑定层也必须落到 400。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}}
        ns = make_ns(sessions)
        assert await _run(ns.on_server_join_office("ag", _join("agent", "ag", "office-a"))) is None

        ack = await _run(getattr(ns, handler)("ag"))

        _assert_code(ack, 400)

    async def test_join_with_empty_office_id_is_400(self, make_ns: Any) -> None:
        """``office_id=""`` 为取值非法（events.md §server:join_office 400）：否则入房后会话 falsy ⇒ 幽灵态。"""
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = make_ns(sessions)

        ack = await _run(ns.on_server_join_office("pc", _join("computer", "pc", "")))

        _assert_code(ack, 400)
        assert "office_id" not in sessions["pc"]
        ns.server.enter_room.assert_not_called()

    async def test_leave_with_none_office_id_is_400(self, make_ns: Any) -> None:
        """验收钉死：``{"office_id": None}`` 被拒并回 400（不再产生 ``room=None``）。"""
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = make_ns(sessions)
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        ns.emit.reset_mock()

        ack = await _run(ns.on_server_leave_office("pc", {"office_id": None}))

        _assert_code(ack, 400)
        ns.emit.assert_not_called()
        assert sessions["pc"]["office_id"] == "office-a"


# =====================================================================
# §一 —— fire-and-forget：非法即**静默丢弃**（不 raise、不广播、不新增 ack）
# =====================================================================

UPDATE_HANDLERS = ["on_server_update_config", "on_server_update_tool_list", "on_server_update_desktop", "on_server_update_skills"]
CANCEL = {"agent": "ag", "req_id": "r"}


class TestFireAndForgetDrops:
    @staticmethod
    async def _joined(make_ns: Any) -> tuple[Any, dict[str, dict[str, Any]]]:
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = make_ns(sessions)
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        assert await _run(ns.on_server_join_office("ag", _join("agent", "ag", "office-a"))) is None
        ns.emit.reset_mock()
        return ns, sessions

    @pytest.mark.parametrize("handler", UPDATE_HANDLERS)
    @pytest.mark.parametrize(
        "args",
        [pytest.param((None,), id="none"), pytest.param(({},), id="missing"), pytest.param(({"computer": 1},), id="wrong_type"),
         pytest.param(({"computer": "pc"}, "extra"), id="extra"), pytest.param((), id="no_payload")],
    )
    async def test_update_malformed_is_dropped(self, make_ns: Any, handler: str, args: tuple) -> None:
        ns, _ = await self._joined(make_ns)
        assert await _run(getattr(ns, handler)("pc", *args)) is None
        ns.emit.assert_not_called()

    @pytest.mark.parametrize("handler", UPDATE_HANDLERS)
    async def test_update_from_agent_is_dropped(self, make_ns: Any, handler: str) -> None:
        ns, _ = await self._joined(make_ns)
        assert await _run(getattr(ns, handler)("ag", {"computer": "pc"})) is None
        ns.emit.assert_not_called()

    @pytest.mark.parametrize("handler", UPDATE_HANDLERS)
    async def test_update_from_never_joined_session_is_dropped(self, make_ns: Any, handler: str) -> None:
        """从未入房的会话无 role ⇒ 旧实现 ``session["role"]`` KeyError；现应显式丢弃。"""
        ns = make_ns({"x": {}})
        assert await _run(getattr(ns, handler)("x", {"computer": "pc"})) is None
        ns.emit.assert_not_called()

    @pytest.mark.parametrize("handler", UPDATE_HANDLERS)
    async def test_update_from_computer_that_left_is_dropped(self, make_ns: Any, handler: str) -> None:
        ns, _ = await self._joined(make_ns)
        assert await _run(ns.on_server_leave_office("pc", {"office_id": "office-a"})) is None
        ns.emit.reset_mock()
        assert await _run(getattr(ns, handler)("pc", {"computer": "pc"})) is None
        ns.emit.assert_not_called()

    @pytest.mark.parametrize("handler", UPDATE_HANDLERS)
    async def test_update_valid_is_broadcast_to_prefixed_room(self, make_ns: Any, handler: str) -> None:
        """正对照 + §三：合法上报广播到 ``office:<id>``（socketio 层房名带前缀）。"""
        ns, _ = await self._joined(make_ns)
        await _run(getattr(ns, handler)("pc", {"computer": "pc"}))
        assert ns.emit.call_count == 1
        assert ns.emit.call_args.kwargs["room"] == "office:office-a"
        assert ns.emit.call_args.kwargs["skip_sid"] == "pc"

    @pytest.mark.parametrize(
        ("sid", "args"),
        [pytest.param("ag", (None,), id="none"), pytest.param("ag", ({"agent": "ag"},), id="missing_req_id"),
         pytest.param("ag", (CANCEL, "extra"), id="extra"), pytest.param("ag", (), id="no_payload"),
         pytest.param("pc", (CANCEL,), id="from_computer"), pytest.param("x", (CANCEL,), id="never_joined")],
    )
    async def test_cancel_invalid_is_dropped(self, make_ns: Any, sid: str, args: tuple) -> None:
        ns, sessions = await self._joined(make_ns)
        sessions["x"] = {}
        assert await _run(ns.on_server_tool_call_cancel(sid, *args)) is None
        ns.emit.assert_not_called()

    @pytest.mark.parametrize("handler", UPDATE_HANDLERS)
    async def test_update_notification_identity_comes_from_session(self, make_ns: Any, handler: str) -> None:
        """events.md §房间广播类事件的目标来源：出向 ``notify:*`` 的身份字段 MUST 取自发起者会话。

        以 ``computer="peer"`` 冒用他人身份上报 ⇒ **不拒绝**（载荷与会话不一致 MUST NOT 拒绝），但广播体里是
        发起者自己的会话名——否则接收方会去刷新**另一个** Computer。
        """
        ns, _ = await self._joined(make_ns)
        await _run(getattr(ns, handler)("pc", {"computer": "peer"}))
        assert ns.emit.call_count == 1
        assert ns.emit.call_args.args[1] == {"computer": "pc"}, ns.emit.call_args

    async def test_cancel_with_mismatched_agent_name_follows_session(self, make_ns: Any) -> None:
        """载荷 ``agent`` 与会话不符 ⇒ 告警后**按会话执行**（MUST NOT 拒绝），出向 ``agent`` 取会话名。"""
        ns, _ = await self._joined(make_ns)
        await _run(ns.on_server_tool_call_cancel("ag", {"agent": "other", "req_id": "r"}))
        assert ns.emit.call_count == 1
        assert ns.emit.call_args.args[1] == {"agent": "ag", "req_id": "r"}, ns.emit.call_args
        assert ns.emit.call_args.kwargs["room"] == "office:office-a"

    @pytest.mark.parametrize("handler", [*UPDATE_HANDLERS, "on_server_tool_call_cancel"])
    async def test_initiator_session_gone_keyerror_is_dropped(self, make_ns: Any, handler: str) -> None:
        """真实 socketio 对未知 sid 的 ``get_session`` 抛 ``KeyError``（非返回 None）⇒ 同样静默丢弃、不抛。"""
        ns = make_ns({})
        ns.get_session.side_effect = KeyError("Session not found")
        payload = dict(CANCEL) if handler == "on_server_tool_call_cancel" else {"computer": "pc"}
        assert await _run(getattr(ns, handler)("gone", payload)) is None
        ns.emit.assert_not_called()

    async def test_cancel_valid_is_broadcast_to_prefixed_room(self, make_ns: Any) -> None:
        ns, _ = await self._joined(make_ns)
        await _run(ns.on_server_tool_call_cancel("ag", dict(CANCEL)))
        assert ns.emit.call_count == 1
        assert ns.emit.call_args.kwargs["room"] == "office:office-a"


# =====================================================================
# §三 —— socketio 房名与 SID 命名空间结构性分离（``office:{office_id}``）
# =====================================================================


def _rooms_calls(mock: Any) -> list[str]:
    """取 server.enter_room / leave_room 的 room 实参（位置或关键字）。"""
    out = []
    for call in mock.call_args_list:
        room = call.kwargs.get("room", call.args[1] if len(call.args) > 1 else None)
        out.append(room)
    return out


class TestOfficeRoomNamespace:
    async def test_join_enters_prefixed_socketio_room(self, make_ns: Any) -> None:
        ns = make_ns({"pc": {}})
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        assert _rooms_calls(ns.server.enter_room) == ["office:office-a"]

    async def test_office_id_equal_to_peer_sid_cannot_enter_private_room(self, make_ns: Any) -> None:
        """构造 ``office_id = <对端 SID>``：socketio 层进入的是 ``office:<SID>``，绝非对端私有房。"""
        ns = make_ns({"victim-sid": {}, "attacker": {}})
        assert await _run(ns.on_server_join_office("attacker", _join("computer", "atk", "victim-sid"))) is None
        rooms = _rooms_calls(ns.server.enter_room)
        assert rooms == ["office:victim-sid"], rooms
        assert "victim-sid" not in rooms

    async def test_join_broadcast_targets_prefixed_room(self, make_ns: Any) -> None:
        ns = make_ns({"pc": {}})
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        rooms = [c.kwargs.get("room") for c in ns.emit.call_args_list]
        assert rooms == ["office:office-a"], rooms

    async def test_agent_occupancy_scan_uses_prefixed_room(self, make_ns: Any) -> None:
        ns = make_ns({"ag": {}})
        assert await _run(ns.on_server_join_office("ag", _join("agent", "ag", "office-a"))) is None
        scanned = [c.args[1] for c in ns.server.manager.get_participants.call_args_list]
        assert scanned and all(r == "office:office-a" for r in scanned), scanned

    async def test_leave_leaves_prefixed_room_and_broadcasts_there(self, make_ns: Any) -> None:
        ns = make_ns({"pc": {}})
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        ns.emit.reset_mock()
        assert await _run(ns.on_server_leave_office("pc", {"office_id": "office-a"})) is None
        assert _rooms_calls(ns.server.leave_room) == ["office:office-a"]
        assert [c.kwargs.get("room") for c in ns.emit.call_args_list] == ["office:office-a"]

    async def test_leave_without_office_only_converges_office_rooms(self, make_ns: Any) -> None:
        """无房幂等分支按 socketio 真实成员关系收敛：只离开 ``office:`` 房，不碰私有房 / 非 office 房。"""
        ns = make_ns({"pc": {}}, rooms=["pc", "office:stale", "foreign-room"])
        assert await _run(ns.on_server_leave_office("pc", {"office_id": "whatever"})) is None
        assert _rooms_calls(ns.server.leave_room) == ["office:stale"]
        # 通知载荷里的 office_id 是**原值**（前缀只存在于 socketio 层）
        notif = ns.emit.call_args_list[0]
        assert notif.args[1]["office_id"] == "stale"
        assert notif.kwargs["room"] == "office:stale"

    async def test_disconnect_leaves_only_office_rooms(self, make_ns: Any) -> None:
        ns = make_ns({"pc": {"sid": "pc", "role": "computer", "name": "pc", "office_id": "office-a"}},
                     rooms=["pc", "office:office-a", "foreign-room"])
        await _run(ns.on_disconnect("pc"))
        assert _rooms_calls(ns.server.leave_room) == ["office:office-a"]

    async def test_computer_switch_room_uses_prefixed_names(self, make_ns: Any) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = make_ns(sessions)
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))) is None
        assert await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-b"))) is None
        assert _rooms_calls(ns.server.leave_room) == ["office:office-a"]
        assert _rooms_calls(ns.server.enter_room) == ["office:office-a", "office:office-b"]
        assert sessions["pc"]["office_id"] == "office-b"
