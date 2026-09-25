# -*- coding: utf-8 -*-
"""
* 文件名: test_name_key_space
* 描述: #215 —— 名字唯一性键空间 = ``(office_id, role, name)``（async + sync 双路径）。

        协议依据 / Protocol: a2c-smcp-protocol develop（protocol#61 Q3 裁决，PR#62）
          - room-model.md §房内名字唯一性 / §跨房间访问防护（名字解析 MUST 限定在会话所在房内）
          - events.md §server:join_office「名字唯一性的作用域」
          - architecture.md §映射生命周期（注册 / 查询 / 注销）

        核心契约 / Core contract：
          - 跨房同名（同 role）**允许**；同房同名但不同 role **允许**；同房同 role 同名 ⇒ ``4105``；
          - ``client:*`` 路由在**发起者所在房内**解析；解析不到统一回 flat ``404``，与「该名字存在于
            其它房」**不可区分**（否则可探测他房成员存在性）；
          - 退房 / 断连按键空间清除映射，无悬挂项；注销绝不替其它 sid 删除映射。

        错误码断言用协议字面值（wire contract）。

#215 contract tests: the name registry is keyed by ``(office_id, role, name)``; routing resolves
names inside the initiator's office only and answers a uniform 404 otherwise.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.server import AuthenticationProvider, SMCPNamespace, SyncAuthenticationProvider, SyncSMCPNamespace
from a2c_smcp.smcp import GET_TOOLS_EVENT, build_computer_not_found_error

TOOLS_RET = {"tools": [], "req_id": "r"}


def _async_ns(sessions: dict[str, dict[str, Any]]) -> SMCPNamespace:
    """会话表可控、socketio 层被 mock 的 SMCPNamespace（live-dict 语义，镜像 test_room_event_acks）。"""
    ns = SMCPNamespace(MagicMock(spec=AuthenticationProvider))
    ns.server = MagicMock()
    ns.server.enter_room = AsyncMock()
    ns.server.leave_room = AsyncMock()
    ns.server.rooms = MagicMock(return_value=[])
    ns.server.manager = MagicMock()
    ns.server.manager.get_participants = MagicMock(return_value=[])
    ns.get_session = AsyncMock(side_effect=lambda sid: sessions.get(sid))
    ns.save_session = AsyncMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
    ns.emit = AsyncMock()
    ns.call = AsyncMock(return_value=dict(TOOLS_RET))
    return ns


def _sync_ns(sessions: dict[str, dict[str, Any]]) -> SyncSMCPNamespace:
    """同步镜像。"""
    ns = SyncSMCPNamespace(MagicMock(spec=SyncAuthenticationProvider))
    ns.server = MagicMock()
    ns.server.enter_room = MagicMock()
    ns.server.leave_room = MagicMock()
    ns.server.rooms = MagicMock(return_value=[])
    ns.server.manager = MagicMock()
    ns.server.manager.get_participants = MagicMock(return_value=[])
    ns.get_session = MagicMock(side_effect=lambda sid: sessions.get(sid))
    ns.save_session = MagicMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
    ns.emit = MagicMock()
    ns.call = MagicMock(return_value=dict(TOOLS_RET))
    return ns


def _join(role: str, name: str, office: str) -> dict[str, str]:
    return {"role": role, "name": name, "office_id": office}


def _get_tools(computer: str) -> dict[str, str]:
    return {"agent": "ag", "req_id": "r", "computer": computer}


class TestNameKeySpaceAsync:
    """异步实现。"""

    async def test_same_name_computers_in_different_offices_both_join(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"c-a": {}, "c-b": {}}
        ns = _async_ns(sessions)

        assert await ns.on_server_join_office("c-a", _join("computer", "pc", "office-a")) is None
        assert await ns.on_server_join_office("c-b", _join("computer", "pc", "office-b")) is None

        assert await ns.get_sid_by_name("office-a", "computer", "pc") == "c-a"
        assert await ns.get_sid_by_name("office-b", "computer", "pc") == "c-b"

    async def test_same_name_agents_in_different_offices_both_join(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"a-a": {}, "a-b": {}}
        ns = _async_ns(sessions)

        assert await ns.on_server_join_office("a-a", _join("agent", "bot", "office-a")) is None
        assert await ns.on_server_join_office("a-b", _join("agent", "bot", "office-b")) is None

    @pytest.mark.parametrize("computer_office", ["office-a", "office-b"])
    async def test_same_name_agent_and_computer_both_join(self, computer_office: str) -> None:
        """同名 Agent + Computer（同房或跨房）允许：``client:*`` 的路由地址已由字段区分 role。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = _async_ns(sessions)

        assert await ns.on_server_join_office("ag", _join("agent", "twin", "office-a")) is None
        assert await ns.on_server_join_office("pc", _join("computer", "twin", computer_office)) is None

    async def test_same_role_same_name_same_office_is_4105(self) -> None:
        """房内同 role 同名 ⇒ 4105（经真实先后 join，不靠 participants 桩）；被拒者不入房、不改注册表。"""
        sessions: dict[str, dict[str, Any]] = {"c-1": {}, "c-2": {}}
        ns = _async_ns(sessions)

        assert await ns.on_server_join_office("c-1", _join("computer", "pc", "office-a")) is None
        ack = await ns.on_server_join_office("c-2", _join("computer", "pc", "office-a"))

        assert isinstance(ack, dict) and ack["code"] == 4105, ack
        assert await ns.get_sid_by_name("office-a", "computer", "pc") == "c-1"
        assert "office_id" not in sessions["c-2"]
        assert ns.server.enter_room.await_count == 1

    async def test_route_to_name_only_in_other_office_is_uniform_404(self) -> None:
        """目标只存在于他房 ⇒ flat 404，且与「全不存在」**逐字节相同**（不泄露他房成员存在性）。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = _async_ns(sessions)
        await ns.on_server_join_office("ag", _join("agent", "bot", "office-a"))
        await ns.on_server_join_office("pc", _join("computer", "pc", "office-b"))

        in_other_room = await ns.on_client_get_tools("ag", _get_tools("pc"))

        # 与「该名字在任何房都不存在」的应答逐字节相同 / byte-identical to the "exists nowhere" answer
        assert in_other_room == build_computer_not_found_error("pc"), in_other_room
        ns.call.assert_not_awaited()

    async def test_route_resolves_inside_initiator_office(self) -> None:
        """两房各有同名 Computer ⇒ 路由到发起者所在房那台。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc-a": {}, "pc-b": {}}
        ns = _async_ns(sessions)
        # 他房先注册：若仍是全局裸名解析，会命中 pc-b
        await ns.on_server_join_office("pc-b", _join("computer", "pc", "office-b"))
        await ns.on_server_join_office("pc-a", _join("computer", "pc", "office-a"))
        await ns.on_server_join_office("ag", _join("agent", "bot", "office-a"))

        ret = await ns.on_client_get_tools("ag", _get_tools("pc"))

        assert ret == TOOLS_RET
        args, kwargs = ns.call.call_args
        assert args[0] == GET_TOOLS_EVENT
        assert kwargs["to"] == "pc-a"

    async def test_route_to_same_room_agent_name_is_404(self) -> None:
        """名字属于同房 **Agent**（非 Computer）⇒ 按 role=computer 解析不到 ⇒ 404。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}}
        ns = _async_ns(sessions)
        await ns.on_server_join_office("ag", _join("agent", "bot", "office-a"))

        ret = await ns.on_client_get_tools("ag", _get_tools("bot"))

        assert isinstance(ret, dict) and ret["code"] == 404, ret
        ns.call.assert_not_awaited()

    async def test_route_from_office_less_initiator_raises(self) -> None:
        """无房发起者无从在房内解析 ⇒ 未入房拒绝（承载形态转 4103 属 #216）。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {"role": "agent", "name": "bot"}, "pc": {}}
        ns = _async_ns(sessions)
        await ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))

        with pytest.raises(SMCPNamespaceError, match="未加入任何房间"):
            await ns.on_client_get_tools("ag", _get_tools("pc"))
        ns.call.assert_not_awaited()

    async def test_leave_office_clears_key(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}, "pc-2": {}}
        ns = _async_ns(sessions)
        await ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))

        assert await ns.on_server_leave_office("pc", {"office_id": "office-a"}) is None

        assert await ns.get_sid_by_name("office-a", "computer", "pc") is None
        assert ns._name_to_sid_map == {}
        # 名字已释放：他人可在同房取用
        assert await ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a")) is None

    async def test_disconnect_clears_key(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = _async_ns(sessions)
        await ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))

        await ns.on_disconnect("pc")

        assert ns._name_to_sid_map == {}

    async def test_computer_switch_room_moves_key(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}, "pc-2": {}}
        ns = _async_ns(sessions)
        await ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))

        assert await ns.on_server_join_office("pc", _join("computer", "pc", "office-b")) is None

        assert ns._name_to_sid_map == {("office-b", "computer", "pc"): "pc"}
        assert await ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a")) is None

    async def test_unregister_never_removes_key_held_by_another_sid(self) -> None:
        """人为破坏不变量：反向索引声称本 sid 持有的键实为他人所有 ⇒ 注销不得删除它（归属守卫）。"""
        sessions: dict[str, dict[str, Any]] = {
            "impostor": {"role": "computer", "name": "pc", "office_id": "office-a"},
        }
        ns = _async_ns(sessions)
        ns._name_to_sid_map = {("office-a", "computer", "pc"): "owner"}
        ns._sid_to_name_key = {"impostor": ("office-a", "computer", "pc"), "owner": ("office-a", "computer", "pc")}

        await ns._unregister_name("impostor")

        assert ns._name_to_sid_map == {("office-a", "computer", "pc"): "owner"}

    async def test_unregister_does_not_depend_on_mutable_session_fields(self) -> None:
        """会话的 role/name 被回滚 pop 掉（``on_server_join_office`` 的字段级回滚）后，断连仍须清除其持有的键。

        注销若从会话字段反推键，此时反推失败 ⇒ 残留键把该房同 role 同名**永久**拒为 4105（仅重启可恢复）。
        反向索引 ``sid → key`` 与注册同源写入，注销不读会话。
        """
        sessions: dict[str, dict[str, Any]] = {"pc": {}, "pc-2": {}}
        ns = _async_ns(sessions)
        assert await ns.on_server_join_office("pc", _join("computer", "pc", "office-a")) is None
        for field in ("role", "name"):
            sessions["pc"].pop(field)  # 人为制造会话字段与已注册键分叉

        await ns.on_disconnect("pc")

        assert ns._name_to_sid_map == {}
        assert ns._sid_to_name_key == {}
        assert await ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a")) is None

    @pytest.mark.parametrize("vanish", ["office_cleared", "socket_gone"])
    async def test_target_leaving_between_resolve_and_session_read_is_404(self, vanish: str) -> None:
        """解析命中后、读目标会话前，目标断连（同步服务端多线程分发下可达）⇒ 与不存在同义回 404，不误报注册表损坏。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = _async_ns(sessions)
        await ns.on_server_join_office("ag", _join("agent", "bot", "office-a"))
        await ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))
        reads = {"pc": 0}

        def _get_session(sid: str) -> dict[str, Any] | None:
            if sid == "pc":
                reads["pc"] += 1
                # 另一线程恰在此刻跑完 on_disconnect：先注销、再清 office_id / 移除 socket
                ns._name_to_sid_map.pop(("office-a", "computer", "pc"), None)
                ns._sid_to_name_key.pop("pc", None)
                if vanish == "socket_gone":
                    raise KeyError("Session not found")
                return {k: v for k, v in sessions["pc"].items() if k != "office_id"}
            return sessions.get(sid)

        ns.get_session.side_effect = _get_session

        ret = await ns.on_client_get_tools("ag", _get_tools("pc"))

        assert reads["pc"] == 1, "前提：必须走到读目标会话这一步（解析已命中）"
        assert ret == build_computer_not_found_error("pc"), ret
        ns.call.assert_not_awaited()


class TestNameKeySpaceSync:
    """同步实现（逐条镜像 async）。"""

    def test_same_name_computers_in_different_offices_both_join(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"c-a": {}, "c-b": {}}
        ns = _sync_ns(sessions)

        assert ns.on_server_join_office("c-a", _join("computer", "pc", "office-a")) is None
        assert ns.on_server_join_office("c-b", _join("computer", "pc", "office-b")) is None

        assert ns.get_sid_by_name("office-a", "computer", "pc") == "c-a"
        assert ns.get_sid_by_name("office-b", "computer", "pc") == "c-b"

    def test_same_name_agents_in_different_offices_both_join(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"a-a": {}, "a-b": {}}
        ns = _sync_ns(sessions)

        assert ns.on_server_join_office("a-a", _join("agent", "bot", "office-a")) is None
        assert ns.on_server_join_office("a-b", _join("agent", "bot", "office-b")) is None

    @pytest.mark.parametrize("computer_office", ["office-a", "office-b"])
    def test_same_name_agent_and_computer_both_join(self, computer_office: str) -> None:
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = _sync_ns(sessions)

        assert ns.on_server_join_office("ag", _join("agent", "twin", "office-a")) is None
        assert ns.on_server_join_office("pc", _join("computer", "twin", computer_office)) is None

    def test_same_role_same_name_same_office_is_4105(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"c-1": {}, "c-2": {}}
        ns = _sync_ns(sessions)

        assert ns.on_server_join_office("c-1", _join("computer", "pc", "office-a")) is None
        ack = ns.on_server_join_office("c-2", _join("computer", "pc", "office-a"))

        assert isinstance(ack, dict) and ack["code"] == 4105, ack
        assert ns.get_sid_by_name("office-a", "computer", "pc") == "c-1"
        assert "office_id" not in sessions["c-2"]
        assert ns.server.enter_room.call_count == 1

    def test_route_to_name_only_in_other_office_is_uniform_404(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = _sync_ns(sessions)
        ns.on_server_join_office("ag", _join("agent", "bot", "office-a"))
        ns.on_server_join_office("pc", _join("computer", "pc", "office-b"))

        in_other_room = ns.on_client_get_tools("ag", _get_tools("pc"))

        # 与「该名字在任何房都不存在」的应答逐字节相同 / byte-identical to the "exists nowhere" answer
        assert in_other_room == build_computer_not_found_error("pc"), in_other_room
        ns.call.assert_not_called()

    def test_route_resolves_inside_initiator_office(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc-a": {}, "pc-b": {}}
        ns = _sync_ns(sessions)
        ns.on_server_join_office("pc-b", _join("computer", "pc", "office-b"))
        ns.on_server_join_office("pc-a", _join("computer", "pc", "office-a"))
        ns.on_server_join_office("ag", _join("agent", "bot", "office-a"))

        ret = ns.on_client_get_tools("ag", _get_tools("pc"))

        assert ret == TOOLS_RET
        args, kwargs = ns.call.call_args
        assert args[0] == GET_TOOLS_EVENT
        assert kwargs["to"] == "pc-a"

    def test_route_to_same_room_agent_name_is_404(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"ag": {}}
        ns = _sync_ns(sessions)
        ns.on_server_join_office("ag", _join("agent", "bot", "office-a"))

        ret = ns.on_client_get_tools("ag", _get_tools("bot"))

        assert isinstance(ret, dict) and ret["code"] == 404, ret
        ns.call.assert_not_called()

    def test_route_from_office_less_initiator_raises(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"ag": {"role": "agent", "name": "bot"}, "pc": {}}
        ns = _sync_ns(sessions)
        ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))

        with pytest.raises(SMCPNamespaceError, match="未加入任何房间"):
            ns.on_client_get_tools("ag", _get_tools("pc"))
        ns.call.assert_not_called()

    def test_leave_office_clears_key(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}, "pc-2": {}}
        ns = _sync_ns(sessions)
        ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))

        assert ns.on_server_leave_office("pc", {"office_id": "office-a"}) is None

        assert ns.get_sid_by_name("office-a", "computer", "pc") is None
        assert ns._name_to_sid_map == {}
        assert ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a")) is None

    def test_disconnect_clears_key(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = _sync_ns(sessions)
        ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))

        ns.on_disconnect("pc")

        assert ns._name_to_sid_map == {}

    def test_computer_switch_room_moves_key(self) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}, "pc-2": {}}
        ns = _sync_ns(sessions)
        ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))

        assert ns.on_server_join_office("pc", _join("computer", "pc", "office-b")) is None

        assert ns._name_to_sid_map == {("office-b", "computer", "pc"): "pc"}
        assert ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a")) is None

    def test_unregister_never_removes_key_held_by_another_sid(self) -> None:
        sessions: dict[str, dict[str, Any]] = {
            "impostor": {"role": "computer", "name": "pc", "office_id": "office-a"},
        }
        ns = _sync_ns(sessions)
        ns._name_to_sid_map = {("office-a", "computer", "pc"): "owner"}
        ns._sid_to_name_key = {"impostor": ("office-a", "computer", "pc"), "owner": ("office-a", "computer", "pc")}

        ns._unregister_name("impostor")

        assert ns._name_to_sid_map == {("office-a", "computer", "pc"): "owner"}

    def test_unregister_does_not_depend_on_mutable_session_fields(self) -> None:
        """会话的 role/name 被回滚 pop 掉（``on_server_join_office`` 的字段级回滚）后，断连仍须清除其持有的键。

        注销若从会话字段反推键，此时反推失败 ⇒ 残留键把该房同 role 同名**永久**拒为 4105（仅重启可恢复）。
        反向索引 ``sid → key`` 与注册同源写入，注销不读会话。
        """
        sessions: dict[str, dict[str, Any]] = {"pc": {}, "pc-2": {}}
        ns = _sync_ns(sessions)
        assert ns.on_server_join_office("pc", _join("computer", "pc", "office-a")) is None
        for field in ("role", "name"):
            sessions["pc"].pop(field)  # 人为制造会话字段与已注册键分叉

        ns.on_disconnect("pc")

        assert ns._name_to_sid_map == {}
        assert ns._sid_to_name_key == {}
        assert ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a")) is None

    @pytest.mark.parametrize("vanish", ["office_cleared", "socket_gone"])
    def test_target_leaving_between_resolve_and_session_read_is_404(self, vanish: str) -> None:
        """解析命中后、读目标会话前，目标断连（同步服务端多线程分发下可达）⇒ 与不存在同义回 404，不误报注册表损坏。"""
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = _sync_ns(sessions)
        ns.on_server_join_office("ag", _join("agent", "bot", "office-a"))
        ns.on_server_join_office("pc", _join("computer", "pc", "office-a"))
        reads = {"pc": 0}

        def _get_session(sid: str) -> dict[str, Any] | None:
            if sid == "pc":
                reads["pc"] += 1
                # 另一线程恰在此刻跑完 on_disconnect：先注销、再清 office_id / 移除 socket
                ns._name_to_sid_map.pop(("office-a", "computer", "pc"), None)
                ns._sid_to_name_key.pop("pc", None)
                if vanish == "socket_gone":
                    raise KeyError("Session not found")
                return {k: v for k, v in sessions["pc"].items() if k != "office_id"}
            return sessions.get(sid)

        ns.get_session.side_effect = _get_session

        ret = ns.on_client_get_tools("ag", _get_tools("pc"))

        assert reads["pc"] == 1, "前提：必须走到读目标会话这一步（解析已命中）"
        assert ret == build_computer_not_found_error("pc"), ret
        ns.call.assert_not_called()
