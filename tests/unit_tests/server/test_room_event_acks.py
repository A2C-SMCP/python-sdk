# -*- coding: utf-8 -*-
"""
* 文件名: test_room_event_acks
* 描述: #214 —— 三个房间事件（server:join_office / server:leave_office / server:list_room）
        的 ack 形态契约（**异步**实现）。

        协议依据 / Protocol: a2c-smcp-protocol develop
          - error-handling.md §错误响应格式（:95 作用域 = 所有具备 ack 通道的事件）
          - error-handling.md §房间管理错误响应（:232-390 各码触发条件与 details 白名单）
          - error-handling.md:102-106（载荷校验失败 MUST 回 400，MUST NOT 静默不 ack）
          - events.md §server:join_office / §server:leave_office / §server:list_room
          - room-model.md §隔离保障

        核心契约 / Core contract：
          - 成功 = **空 ack**（`None`）—— 不是元组、不是空 dict；
          - 失败 = **flat `ErrorPayload`**（顶层 `code`）；
          - `details` 只含**与发起者自身相关**的上下文，**MUST NOT** 出现其它会话的内部标识。

        编号断言均为**协议字面值**（wire contract），刻意不写成 `ErrorCode.X`——契约是数字本身。

#214 contract tests for the three room-management events (async implementation).
Success is an **empty ack** (`None`); failures are flat `ErrorPayload` dicts; `details` MUST NOT
carry other sessions' internal identifiers.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from a2c_smcp.server import AuthenticationProvider, SMCPNamespace
from a2c_smcp.smcp import SMCP_NAMESPACE

# 对端会话的「内部标识」哨兵：任何失败 ack 中出现它们即为泄露。
# Sentinels for the peer session's internal identifiers: their presence in a failure ack = leak.
PEER_SID = "PEER-SID-SENTINEL-9f3a"
PEER_NAME = "peer-name-sentinel"
NAMESPACE_NAME = SMCP_NAMESPACE


def _namespace(
    sessions: dict[str, dict[str, Any]],
    participants: list[tuple[str, str]] | None = None,
) -> SMCPNamespace:
    """构造一个会话表可控、socketio 层被 mock 的 SMCPNamespace。

    会话用**同一个 dict 对象**承载（镜像 socketio 的 live-dict 语义），故 `get_session`
    返回的引用上的原地修改对后续断言可见。
    """
    ns = SMCPNamespace(MagicMock(spec=AuthenticationProvider))
    ns.server = MagicMock()
    ns.server.enter_room = AsyncMock()
    ns.server.leave_room = AsyncMock()
    ns.server.rooms = MagicMock(return_value=[])
    ns.server.manager = MagicMock()
    ns.server.manager.get_participants = MagicMock(return_value=list(participants or []))
    ns.get_session = AsyncMock(side_effect=lambda sid: sessions[sid])
    ns.save_session = AsyncMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
    ns.emit = AsyncMock()
    return ns


def _assert_no_leak(payload: dict[str, Any]) -> None:
    """失败 ack 的 message / details MUST NOT 含其它会话的内部标识。"""
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    for sentinel in (PEER_SID, PEER_NAME, NAMESPACE_NAME):
        assert sentinel not in blob, f"失败 ack 泄露了 {sentinel!r}: {blob}"


class TestJoinOfficeAckShape:
    """`server:join_office` 的 ack 形态。"""

    async def test_success_is_empty_ack(self) -> None:
        """成功 ⇒ **空 ack**（`None`），不是元组、不是空 dict。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {}}
        ns = _namespace(sessions)

        ack = await ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert ack is None, f"成功必须是空 ack（None），实得 {ack!r}"
        assert sessions["sid-1"]["office_id"] == "room-a"

    @pytest.mark.parametrize(
        "bad_payload",
        [
            {},
            {"role": "agent"},
            {"role": "not-a-role", "name": "n", "office_id": "o"},
            None,
            "not-a-dict",
            {"office_id": "o"},
        ],
    )
    async def test_malformed_payload_acks_400(self, bad_payload: Any) -> None:
        """载荷 schema 校验失败 ⇒ 回 `400`，**绝不允许**静默不 ack（挂到客户端自身超时）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {}}
        ns = _namespace(sessions)

        ack = await ns.on_server_join_office("sid-1", bad_payload)

        assert ack is not None, "载荷畸形必须产出 ack（否则客户端挂起到自身超时）"
        assert ack["code"] == 400, f"载荷畸形应回 400，实得 {ack!r}"
        assert "details" not in ack, f"400 不得回显客户端输入: {ack!r}"

    async def test_role_mismatch_is_403(self) -> None:
        """同一 sid 声明与会话不同的 role ⇒ `403`（身份声明冲突，**非**房间语义）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent"}}
        ns = _namespace(sessions)

        ack = await ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert isinstance(ack, dict), f"失败必须是 flat ErrorPayload，实得 {ack!r}"
        assert ack["code"] == 403, ack
        assert "details" not in ack, f"403 无 code-specific details: {ack!r}"
        _assert_no_leak(ack)

    async def test_room_full_is_4101_with_rejected_target_room(self) -> None:
        """目标房已有 Agent ⇒ `4101`，`details.office_id` = **被拒的目标房**。"""
        sessions: dict[str, dict[str, Any]] = {
            "sid-1": {"role": "agent"},
            PEER_SID: {"role": "agent", "name": PEER_NAME, "office_id": "room-a"},
        }
        ns = _namespace(sessions, participants=[(PEER_SID, "eio-peer")])

        ack = await ns.on_server_join_office("sid-1", {"role": "agent", "name": "a1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 4101, ack
        assert ack["details"] == {"office_id": "room-a"}, ack
        _assert_no_leak(ack)

    async def test_agent_switch_is_4106_with_current_room(self) -> None:
        """Agent 已在其它房又请求入新房 ⇒ `4106`，`details.office_id` = 会话**当前**所在房。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-old"}}
        ns = _namespace(sessions)

        ack = await ns.on_server_join_office("sid-1", {"role": "agent", "name": "a1", "office_id": "room-new"})

        assert isinstance(ack, dict) and ack["code"] == 4106, ack
        assert ack["details"] == {"office_id": "room-old"}, (
            f"4106 的 details.office_id 必须是**当前**房而非目标房: {ack!r}"
        )
        # 换房被拒不得改动既有成员关系
        assert sessions["sid-1"]["office_id"] == "room-old"
        _assert_no_leak(ack)

    async def test_computer_same_name_in_target_room_is_4105(self) -> None:
        """目标房已有同 role 同名会话 ⇒ `4105`（`details.office_id` = 目标房 + `role`）。"""
        sessions: dict[str, dict[str, Any]] = {
            "sid-1": {"role": "computer", "name": "dup"},
            PEER_SID: {"role": "computer", "name": "dup", "office_id": "room-b"},
        }
        ns = _namespace(sessions, participants=[("sid-1", "eio-self"), (PEER_SID, "eio-peer")])

        ack = await ns.on_server_join_office("sid-1", {"role": "computer", "name": "dup", "office_id": "room-b"})

        assert isinstance(ack, dict) and ack["code"] == 4105, ack
        assert ack["details"] == {"office_id": "room-b", "role": "computer"}, ack
        _assert_no_leak(ack)

    async def test_registry_conflict_is_4105_without_peer_sid(self) -> None:
        """名字注册表冲突（**冲突消息内含对端 sid**）⇒ `4105`，且 ack **绝不**携带该 sid。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "computer", "name": "taken"}}
        ns = _namespace(sessions)
        # 注册表里该名字已被**另一个** sid 占用（跨房裸名注册表，过渡态）
        ns._name_to_sid_map = {"taken": PEER_SID}

        ack = await ns.on_server_join_office("sid-1", {"role": "computer", "name": "taken", "office_id": "room-b"})

        assert isinstance(ack, dict) and ack["code"] == 4105, ack
        assert ack["details"] == {"office_id": "room-b", "role": "computer"}, ack
        _assert_no_leak(ack)  # 回归守卫：旧实现把 "sid '<peer>'" 自由文本塞进 error_msg

    async def test_missing_payload_still_acks(self) -> None:
        """**参数绑定失败也必须回 ack**：客户端不带载荷 emit（零参包）时，绑定发生在 handler 体内之前
        ——若 `data` 无默认值，异常会逃出 handler ⇒ socketio 根本不发 ACK ⇒ 调用方挂到自身超时
        （协议 error-handling.md:102-106 明令禁止，且明确覆盖「框架层参数提取器」这一时机）。

        本用例直接调用**生产 handler**（不经 mock 替身，替身自带默认值会掩盖真实签名）；
        线上等价由集成层 `test_join_office_ack_shapes_over_wire` 覆盖。
        """
        ns = _namespace({"sid-1": {}})

        ack = await ns.on_server_join_office("sid-1")

        assert isinstance(ack, dict) and ack["code"] == 400, ack

    async def test_valid_payload_plus_extra_argument_is_rejected(self) -> None:
        """**合法载荷 + 多余位置参数** ⇒ 400（载荷是单个 dict，多参即形状非法，绝不静默忽略）。

        这个用例是必需的：只测"零参"时，`*_extra` 到底是被拒绝还是被**静默丢弃**根本没被区分——
        而静默丢弃会让「客户端多传了一个参数」变成看不见的兼容性问题。
        A valid payload plus an extra positional arg must be rejected, not silently ignored.
        """
        sessions: dict[str, dict[str, Any]] = {"sid-1": {}}
        ns = _namespace(sessions)

        ack = await ns.on_server_join_office(
            "sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"}, "unexpected"
        )

        assert isinstance(ack, dict) and ack["code"] == 400, ack
        assert "office_id" not in sessions["sid-1"], "多参必须在**产生副作用之前**被拒"

    async def test_registry_conflict_diagnostic_goes_to_log_not_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """名字冲突的冗长诊断（**含对端 sid**）必须进日志、且**不得**进 ack —— 两个方向都钉。

        为什么不能只断言「payload 里没有 sid」：payload 由请求派生值与常量文案构成，**任何单点变异
        都不会让它转红**（本单的变异验证实测如此）。反向断言（sid 确实出现在日志里）才把「诊断下沉
        日志」这条不变量变成可单点判别的——否则把日志整条删掉，用例照绿。
        Assert both directions: the peer sid must be in the log AND absent from the ack payload.
        """
        from a2c_smcp.server import base as base_mod

        fake_logger = MagicMock()
        monkeypatch.setattr(base_mod, "logger", fake_logger)

        ns = _namespace({"sid-1": {"role": "computer", "name": "taken"}})
        ns._name_to_sid_map = {"taken": PEER_SID}

        ack = await ns.on_server_join_office("sid-1", {"role": "computer", "name": "taken", "office_id": "room-b"})

        logged = " ".join(str(call) for call in fake_logger.warning.call_args_list)
        assert PEER_SID in logged, "诊断（含对端 sid）必须留在服务端日志里，否则现场无法排查"
        assert PEER_SID not in json.dumps(ack, ensure_ascii=False), "但绝不可进入 ack 载荷"

    async def test_unmapped_rejection_code_degrades_to_500_not_escape(self) -> None:
        """**未接入码表的领域拒绝**（如下游直接实例化基类 `RoomRejection`，其 `code = 0`）⇒ 回 500。

        回归守卫：`build_room_rejection_error` 对未映射的码抛 `ValueError`；而该调用点在 handler 的
        `except` 子句**内部**——若它抛出来，新异常会顶替原异常逃出 handler ⇒ socketio 根本不发 ACK
        ⇒ 调用方挂到自身超时。那正是本单要消灭的形态，故必须走**不抛**的助手兜底成 500。
        """
        from a2c_smcp.exceptions import RoomRejection

        ns = _namespace({"sid-1": {}})
        ns.enter_room = AsyncMock(side_effect=RoomRejection())

        ack = await ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack

    async def test_non_int_rejection_code_degrades_to_500_not_escape(self) -> None:
        """`code` **非 int** 的领域拒绝 ⇒ 500（同样不得逃出 handler）。

        与"未映射码"是同族但**不同异常类型**：builder 入口的 `int(code)` 对 `None` / 字符串抛
        `TypeError`；助手若只捕 `ValueError`，这条路径依旧会二次抛出 ⇒ 不发 ACK。故必须以
        `code=None` 的子类单独钉死，而不是只测未映射的 int 码。
        """
        from a2c_smcp.exceptions import RoomRejection

        class NoneCodeRejection(RoomRejection):
            code = None  # type: ignore[assignment]

        ns = _namespace({"sid-1": {}})
        ns.enter_room = AsyncMock(side_effect=NoneCodeRejection())

        ack = await ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack

    async def test_unknown_exception_is_500_without_echoing_detail(self) -> None:
        """未知内部异常 ⇒ `500` + 笼统文案；**原始异常文本只进日志**，不得回给客户端。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {}}
        ns = _namespace(sessions)
        ns.enter_room = AsyncMock(side_effect=RuntimeError("boom-internal-detail"))

        ack = await ns.on_server_join_office("sid-1", {"role": "computer", "name": "c1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert "Internal server error" not in ack["message"], ack
        assert "boom-internal-detail" not in json.dumps(ack, ensure_ascii=False), ack

    @pytest.mark.parametrize("event", ["leave_office", "list_room"])
    async def test_valid_payload_plus_extra_argument_is_rejected_for_all_room_events(self, event: str) -> None:
        """三个房间事件的 `_extra` 分支都要有覆盖：只测 join 的话，把 leave / list_room 的检查
        整段删掉套件仍全绿（变异 M12 只证明了 join 一处）。/ Cover the `_extra` guard on all three.

        合法载荷 + 多余位置参数 ⇒ 400，且**不得产生副作用**。
        """
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)
        ns.leave_room = AsyncMock()

        if event == "leave_office":
            ack = await ns.on_server_leave_office("sid-1", {"office_id": "room-a"}, "unexpected")
        else:
            ack = await ns.on_server_list_room(
                "sid-1", {"agent": "a1", "req_id": "r-1", "office_id": "room-a"}, "unexpected"
            )

        assert isinstance(ack, dict) and ack["code"] == 400, ack
        assert sessions["sid-1"]["office_id"] == "room-a", "多参必须在产生副作用之前被拒"
        ns.leave_room.assert_not_awaited()


class TestLeaveOfficeAckShape:
    """`server:leave_office` 的 ack 形态。"""

    async def test_success_is_empty_ack(self) -> None:
        """有房退房 ⇒ 空 ack（`None`）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)

        ack = await ns.on_server_leave_office("sid-1", {"office_id": "room-a"})

        assert ack is None, f"成功必须是空 ack（None），实得 {ack!r}"
        assert "office_id" not in sessions["sid-1"]

    async def test_no_room_is_idempotent_empty_ack(self) -> None:
        """无房退房 ⇒ **幂等成功**（空 ack），必须**不走** `4103`。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent"}}
        ns = _namespace(sessions)

        ack = await ns.on_server_leave_office("sid-1", {"office_id": "room-a"})

        assert ack is None, f"无房退房是幂等成功，不得回错误载荷，实得 {ack!r}"

    @pytest.mark.parametrize("bad_payload", [{}, {"office_id": 123}, {"office_id": None}, None, "x"])
    async def test_malformed_payload_acks_400(self, bad_payload: Any) -> None:
        """载荷 schema 校验失败 ⇒ `400`（**不**静默不 ack）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)

        ack = await ns.on_server_leave_office("sid-1", bad_payload)

        assert isinstance(ack, dict) and ack["code"] == 400, ack
        # 校验失败不得产生副作用
        assert sessions["sid-1"]["office_id"] == "room-a"

    async def test_malformed_payload_wins_over_no_room_idempotence(self) -> None:
        """「无房幂等」与「载荷畸形 400」的交界：**校验在前** ⇒ 回 400，而非空 ack。

        协议对两者各有一句话（无房 ⇒ 幂等成功；schema 失败 ⇒ 400），交界处未明说。取「校验先于
        业务」既符合 error-handling.md:102-106 的口径（校验失败必产出结构化错误），也让两条规则
        不会因实现顺序不同而给出两种客户端可感行为。
        """
        ns = _namespace({"sid-1": {"role": "agent"}})  # 会话无房

        ack = await ns.on_server_leave_office("sid-1", {})

        assert isinstance(ack, dict) and ack["code"] == 400, ack

    async def test_payload_room_mismatch_is_still_success(self) -> None:
        """载荷房号与会话不符 ⇒ **仍成功**（协议：MUST NOT 因此拒绝，只按会话执行）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)

        ack = await ns.on_server_leave_office("sid-1", {"office_id": "room-OTHER"})

        assert ack is None, f"载荷与实情不符不得构成拒绝，实得 {ack!r}"
        assert "office_id" not in sessions["sid-1"]

    async def test_internal_error_is_500(self) -> None:
        """未知内部异常 ⇒ `500` + 笼统文案（原文进日志）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)
        ns.leave_room = AsyncMock(side_effect=RuntimeError("boom-internal-detail"))

        ack = await ns.on_server_leave_office("sid-1", {"office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert "boom-internal-detail" not in json.dumps(ack, ensure_ascii=False), ack


class TestListRoomAckShape:
    """`server:list_room` 的 ack 形态。"""

    @staticmethod
    def _sessions_in(office: str, members: list[dict[str, Any]]) -> Any:
        return AsyncMock(return_value=members)

    async def test_success_returns_list_room_ret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """成功仍为 `ListRoomRet`（`sessions` + `req_id`）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)
        monkeypatch.setattr(
            "a2c_smcp.server.namespace.aget_all_sessions_in_office",
            self._sessions_in("room-a", [{"sid": "sid-1", "name": "a1", "role": "agent", "office_id": "room-a"}]),
        )

        ret = await ns.on_server_list_room("sid-1", {"agent": "a1", "req_id": "r-1", "office_id": "room-a"})

        assert ret["req_id"] == "r-1"
        assert len(ret["sessions"]) == 1

    @pytest.mark.parametrize("bad_payload", [{}, {"agent": "a"}, {"office_id": "o"}, None])
    async def test_malformed_payload_acks_400(self, bad_payload: Any) -> None:
        """载荷畸形 ⇒ `400`（**不**静默不 ack）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)

        ack = await ns.on_server_list_room("sid-1", bad_payload)

        assert isinstance(ack, dict) and ack["code"] == 400, ack

    async def test_not_in_room_is_4103(self) -> None:
        """会话无 `office_id` ⇒ `4103`（无 code-specific details）。"""
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent"}}
        ns = _namespace(sessions)

        ack = await ns.on_server_list_room("sid-1", {"agent": "a1", "req_id": "r-1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 4103, ack
        assert "details" not in ack, f"4103 无 details（会话自身无房可报）: {ack!r}"

    async def test_cross_room_is_4104_with_rejected_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式点名非自己所在房 ⇒ `4104`（**不再**静默不 ack），且**不泄露**目标房成员信息。

        断言刻意分两层：payload 里没有成员数据（弱）+ **会话读取器根本没被调用**（强，形状无关）。
        只做前者时，「先取全房成员、再拒绝」的实现照样全绿——那已经把泄露面打开了。
        """
        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-a"}}
        ns = _namespace(sessions)
        leaked = [{"sid": PEER_SID, "name": PEER_NAME, "role": "computer", "office_id": "room-b"}]
        reader = self._sessions_in("room-b", leaked)
        monkeypatch.setattr("a2c_smcp.server.namespace.aget_all_sessions_in_office", reader)

        ack = await ns.on_server_list_room("sid-1", {"agent": "a1", "req_id": "r-1", "office_id": "room-b"})

        assert isinstance(ack, dict) and ack["code"] == 4104, ack
        assert ack["details"] == {"office_id": "room-b"}, ack
        _assert_no_leak(ack)
        reader.assert_not_called()

    async def test_originator_session_gone_is_acked_not_raised(self) -> None:
        """发起者会话取不到 ⇒ 仍**产出 ack**（500），而不是抛出去让调用方挂到超时。

        与 join / leave 对称的 catch-all：本单的不变量是「有 ack 通道 ⇒ 失败必产出 ack」，
        对 list_room 不设例外。

        .. note::

            ``is None`` 分支本身是**防御性死分支**：实测 ``socketio`` 的 ``get_session`` 对未知 sid
            抛 ``KeyError('Session not found')`` 而**不返回 None**，故线上"发起者已断连"走 KeyError
            ——它同样被这条 catch-all 收编为 500 ack（对已断连的 originator 没有读者，协议
            §飞行中断连 亦许可不回；此处回 500 无害且保持了三条事件的一致性）。两条分支在此都
            收敛到同一断言，故用例不区分它们。
        """
        ns = _namespace({})
        ns.get_session = AsyncMock(return_value=None)

        ack = await ns.on_server_list_room("sid-gone", {"agent": "a1", "req_id": "r-1", "office_id": "room-a"})

        assert isinstance(ack, dict) and ack["code"] == 500, ack


class TestEnterRoomDomainErrorTypes:
    """`enter_room` 的业务闸门改抛**领域异常**（携带协议码），且仍是 `ValueError` 子类。

    换型只增加信息量（码），不改变「校验失败」的类别 ⇒ #213 建立的 `except ValueError` /
    `pytest.raises(ValueError)` 契约保持兼容。
    """

    async def test_agent_switch_raises_already_in_room(self) -> None:
        from a2c_smcp.exceptions import AlreadyInRoomError

        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "agent", "office_id": "room-old"}}
        ns = _namespace(sessions)

        with pytest.raises(AlreadyInRoomError) as exc_info:
            await ns.enter_room("sid-1", "room-new")

        assert exc_info.value.code == 4106
        assert isinstance(exc_info.value, ValueError), "既有 except ValueError 契约必须保持"

    async def test_room_full_raises_room_full(self) -> None:
        from a2c_smcp.exceptions import RoomFullError

        sessions: dict[str, dict[str, Any]] = {
            "sid-1": {"role": "agent"},
            PEER_SID: {"role": "agent", "name": PEER_NAME, "office_id": "room-a"},
        }
        ns = _namespace(sessions, participants=[(PEER_SID, "eio-peer")])

        with pytest.raises(RoomFullError) as exc_info:
            await ns.enter_room("sid-1", "room-a")

        assert exc_info.value.code == 4101
        assert isinstance(exc_info.value, ValueError)

    async def test_registry_conflict_raises_name_conflict(self) -> None:
        from a2c_smcp.exceptions import NameConflictError

        sessions: dict[str, dict[str, Any]] = {"sid-1": {"role": "computer", "name": "taken"}}
        ns = _namespace(sessions)
        ns._name_to_sid_map = {"taken": PEER_SID}

        with pytest.raises(NameConflictError) as exc_info:
            await ns.enter_room("sid-1", "room-a")

        assert exc_info.value.code == 4105
        assert isinstance(exc_info.value, ValueError)
        # 异常消息**构造上**不含对端标识（冗长诊断走日志）——即使被误塞进 ack 也不会泄露
        assert PEER_SID not in str(exc_info.value)

    def test_exception_message_matches_protocol_payload_message(self) -> None:
        """防漂移：领域异常的文案必须与 builder 产出的协议文案**逐字一致**。

        两处各写一份字符串，一旦分叉，日志里的原因就和客户端收到的原因不是同一回事。
        Anti-drift: the exception text and the wire payload message must be one and the same string.
        """
        from a2c_smcp.exceptions import AlreadyInRoomError, NameConflictError, RoomFullError
        from a2c_smcp.smcp import build_room_rejection_error

        for exc_cls in (RoomFullError, NameConflictError, AlreadyInRoomError):
            exc = exc_cls()
            assert str(exc) == build_room_rejection_error(exc.code)["message"], exc_cls.__name__


class TestRoomRejectionCodesAreStructurallyConfined:
    """`4102` 为预留码：协议要求 SDK **MUST NOT** 主动返回。"""

    async def test_4102_is_never_produced_by_any_room_event(self) -> None:
        """三个房间事件在任何失败路径上都不得产出 `4102`。

        采样表**逐行**携带该场景所需的对端会话，避免「行写了、但夹具没构造出触发条件」⇒ 该行
        静默变成"成功"，而 ``isinstance(p, dict)`` 的过滤又把它悄悄剔除（本单实锤踩到过）。
        故下方先**硬断言每条路径都产出了 flat ErrorPayload**，再收集码。
        """
        rows: list[tuple[str, Any]] = []

        # (标签, 请求载荷, 本会话, 对端会话, 参与者, 注册表)
        join_rows: list[tuple[str, Any, dict[str, Any], dict[str, Any] | None, list[tuple[str, str]], dict[str, str]]] = [
            ("载荷畸形", {}, {}, None, [], {}),
            ("角色不符", {"role": "computer", "name": "n", "office_id": "r"}, {"role": "agent"}, None, [], {}),
            (
                "目标房已有 Agent",
                {"role": "agent", "name": "n", "office_id": "r"},
                {"role": "agent"},
                {"role": "agent", "name": "p", "office_id": "r"},
                [(PEER_SID, "e")],
                {},
            ),
            (
                "Agent 已在其它房",
                {"role": "agent", "name": "n", "office_id": "r2"},
                {"role": "agent", "office_id": "r1"},
                None,
                [],
                {},
            ),
            (
                "房内同名 Computer",
                {"role": "computer", "name": "dup", "office_id": "r"},
                {"role": "computer", "name": "dup"},
                # 对端必须是**同房同名 computer**，否则同名检查根本不触发（本行曾因此静默变成"成功"）
                {"role": "computer", "name": "dup", "office_id": "r"},
                [(PEER_SID, "e")],
                {},
            ),
            (
                "注册表冲突",
                {"role": "computer", "name": "taken", "office_id": "r"},
                {"role": "computer", "name": "taken"},
                None,
                [],
                {"taken": PEER_SID},
            ),
        ]
        for label, payload, sess, peer, participants, registry in join_rows:
            sessions: dict[str, dict[str, Any]] = {"sid-1": dict(sess)}
            if peer is not None:
                sessions[PEER_SID] = dict(peer)
            ns = _namespace(sessions, participants=participants)
            ns._name_to_sid_map = dict(registry)
            rows.append((label, await ns.on_server_join_office("sid-1", payload)))

        # leave：载荷畸形 / 内部异常
        ns_l1 = _namespace({"sid-1": {"role": "agent", "office_id": "r"}})
        rows.append(("leave 载荷畸形", await ns_l1.on_server_leave_office("sid-1", {})))
        ns_l2 = _namespace({"sid-1": {"role": "agent", "office_id": "r"}})
        ns_l2.leave_room = AsyncMock(side_effect=RuntimeError("x"))
        rows.append(("leave 内部异常", await ns_l2.on_server_leave_office("sid-1", {"office_id": "r"})))

        # list_room：载荷畸形 / 无房 / 越权 / 内部异常
        ns_r1 = _namespace({"sid-1": {"role": "agent"}})
        rows.append(("list_room 载荷畸形", await ns_r1.on_server_list_room("sid-1", {})))
        rows.append(
            (
                "list_room 无房",
                await ns_r1.on_server_list_room("sid-1", {"agent": "a", "req_id": "r", "office_id": "r"}),
            )
        )
        ns_r2 = _namespace({"sid-1": {"role": "agent", "office_id": "r1"}})
        rows.append(
            (
                "list_room 越权",
                await ns_r2.on_server_list_room("sid-1", {"agent": "a", "req_id": "r", "office_id": "r2"}),
            )
        )
        ns_r3 = _namespace({"sid-1": {"role": "agent", "office_id": "r1"}})
        ns_r3.get_session = AsyncMock(side_effect=RuntimeError("boom"))
        rows.append(
            (
                "list_room 内部异常",
                await ns_r3.on_server_list_room("sid-1", {"agent": "a", "req_id": "r", "office_id": "r1"}),
            )
        )

        # 硬断言：**每一条**采样路径都必须产出 flat ErrorPayload。若某行因夹具不成立而变成"成功"
        # （None），这里立刻转红——否则码集合会把该行静默剔除，用例看似覆盖、实则漏掉整行。
        # Every sampled path must produce a flat ErrorPayload — never a silent success.
        bad = [(label, ack) for label, ack in rows if not (isinstance(ack, dict) and "code" in ack)]
        assert not bad, f"以下路径未产出 flat ErrorPayload（夹具或实现有问题）：{bad!r}"
        codes = [ack["code"] for _, ack in rows]
        assert 4102 not in codes, f"4102 是预留码，SDK MUST NOT 主动返回；实得 {codes}"
        # 正对照：采样确实覆盖了多个**不同**码（否则"没有 4102"可能只是因为全都走了同一条兜底）
        assert len(set(codes)) >= 5, f"采样面过窄，无法支撑「任何路径都不产出 4102」：{codes}"

    def test_builder_refuses_to_emit_4102(self) -> None:
        """`4102` 是预留码：builder **构造上**拒绝产出，而不是靠调用方自觉。

        match 必须锚定**预留码专属**的那条守卫（而非「非房间管理码」的兜底分支）——两者都抛
        ValueError，只匹配 "4102" 时，把预留码守卫删掉、靠兜底分支接住也能过（变异验证抓到的空转）。
        """
        from a2c_smcp.smcp import build_room_rejection_error

        with pytest.raises(ValueError, match="预留码"):
            build_room_rejection_error(4102)

    def test_builder_rejects_non_room_codes(self) -> None:
        """非房间管理码（本 builder 的合法输入域之外）同样拒绝，避免悄悄产出无 code-specific 规则的载荷。"""
        from a2c_smcp.smcp import build_room_rejection_error

        for code in (4014, 4019, 9999):
            with pytest.raises(ValueError, match="非房间管理错误码"):
                build_room_rejection_error(code)

    def test_no_4102_literal_in_server_sources(self) -> None:
        """源码扫描兜底：``a2c_smcp/server`` 内不得出现裸 ``4102`` 字面量（新增路径的回归守卫）。"""
        import pathlib as _pathlib

        server_dir = _pathlib.Path(__file__).resolve().parents[3] / "a2c_smcp" / "server"
        offenders = [
            f"{path.name}:{lineno}"
            for path in server_dir.glob("*.py")
            for lineno, line in enumerate(path.read_text().splitlines(), 1)
            if "4102" in line
        ]
        assert not offenders, f"预留码 4102 不得出现在服务端源码：{offenders}"
