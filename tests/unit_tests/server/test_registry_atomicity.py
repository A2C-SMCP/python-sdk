# -*- coding: utf-8 -*-
"""
* 文件名: test_registry_atomicity
* 描述: v0.5.0 跨节点审查 🔴7 / S1-S4 —— 名字注册表的原子性与断连收尾的交错（async + sync 双路径）。

        - 🔴7：断连收尾先跑完、join 后注册 ⇒ 注册表残留死 sid 的键 ⇒ 同名重连永久 4105。
          修法：注册在锁内（sync）/ 零挂起点（async）原子校验 ``manager.is_connected``。
        - S1：同名 / 同房第二个 Agent 的「先查后写」无锁 ⇒ 双同名 / 双 Agent。
        - S2：relay 复查与会话字段读取的先后 ⇒ 并发退房误报注册表损坏（raise ⇒ Agent 干等超时）；
          死 sid 残留 ⇒ 404 + 自愈。
        - S3：断连先注销再广播 leave ⇒ 同名新连接的 enter 可能先于旧 leave。
        - S4：在途断连守卫只判 ``is None`` ⇒ 同名换 sid 时挂到满超时。

        交错一律用**钩子**确定性复现（在真实调用链的某一步里插入另一条路径的完整执行），不靠 sleep 抢时序；
        S1 的锁用真实线程 + 放大窗口的字典验证。

Deterministic interleaving tests for the v0.5.0 review's registry findings (red 7, S1-S4).
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from a2c_smcp.exceptions import NameConflictError, RoomFullError
from a2c_smcp.server import AuthenticationProvider, SMCPNamespace, SyncAuthenticationProvider, SyncSMCPNamespace
from a2c_smcp.smcp import ENTER_OFFICE_NOTIFICATION, LEAVE_OFFICE_NOTIFICATION, build_computer_not_found_error

TOOLS_RET = {"tools": [], "req_id": "r"}
KINDS = ["async", "sync"]


def _ns(kind: str, sessions: dict[str, dict[str, Any]]) -> Any:
    """会话表可控、socketio 层被 mock 的命名空间（live-dict 语义；与 test_name_key_space 同款）。"""
    if kind == "async":
        ns: Any = SMCPNamespace(MagicMock(spec=AuthenticationProvider))
        ns.server = MagicMock()
        ns.server.enter_room = AsyncMock()
        ns.server.leave_room = AsyncMock()
        ns.get_session = AsyncMock(side_effect=lambda sid: sessions.get(sid))
        ns.save_session = AsyncMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
        ns.emit = AsyncMock()
        ns.call = AsyncMock(return_value=dict(TOOLS_RET))
    else:
        ns = SyncSMCPNamespace(MagicMock(spec=SyncAuthenticationProvider))
        ns.server = MagicMock()
        ns.server.enter_room = MagicMock()
        ns.server.leave_room = MagicMock()
        ns.get_session = MagicMock(side_effect=lambda sid: sessions.get(sid))
        ns.save_session = MagicMock(side_effect=lambda sid, sess: sessions.__setitem__(sid, sess))
        ns.emit = MagicMock()
        ns.call = MagicMock(return_value=dict(TOOLS_RET))
    ns.server.rooms = MagicMock(return_value=[])
    ns.server.manager = MagicMock()
    ns.server.manager.get_participants = MagicMock(return_value=[])
    # 存活表：缺省全部在线；pre_disconnect 语义 = 先把 sid 标为离线，再跑断连 handler
    ns._alive = set(sessions)
    ns.server.manager.is_connected = MagicMock(side_effect=lambda sid, _namespace: sid in ns._alive)
    return ns


async def _run(value: Any) -> Any:
    """统一 sync / async：协程则 await，普通值原样返回。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _join(role: str, name: str, office: str) -> dict[str, str]:
    return {"role": role, "name": name, "office_id": office}


def _get_tools(computer: str) -> dict[str, str]:
    return {"agent": "ag", "req_id": "r", "computer": computer}


def _emitted(ns: Any, event: str) -> list[dict[str, Any]]:
    return [c.args[1] for c in ns.emit.call_args_list if c.args and c.args[0] == event]


def _disconnect_now(ns: Any, sid: str) -> Any:
    """模拟 socketio 的断连收尾：``pre_disconnect``（标离线）→ 断连 handler。"""
    ns._alive.discard(sid)
    return ns.on_disconnect(sid)


def _run_inline(kind: str, maybe_coro: Any) -> None:
    """在钩子里**完整**跑完另一条路径（async 钩子本身是同步回调 ⇒ 用新事件循环线程跑协程）。"""
    if kind == "sync" or not inspect.isawaitable(maybe_coro):
        return
    box: dict[str, BaseException] = {}

    def _target() -> None:
        try:
            asyncio.run(maybe_coro)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]


@pytest.mark.parametrize("kind", KINDS)
class TestDeadSidNeverHoldsName:
    """🔴7：断连收尾落在「入房」与「注册」之间，注册表不得残留死 sid。"""

    async def test_disconnect_between_enter_and_register_leaves_no_key(self, kind: str) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}, "pc-2": {}}
        ns = _ns(kind, sessions)
        save = ns.save_session.side_effect

        def _save(sid: str, sess: dict[str, Any]) -> None:
            save(sid, sess)
            if sid == "pc" and sess.get("office_id") == "office-a":
                # 入房已生效、注册尚未发生 —— 另一线程恰在此刻把该 sid 的断连收尾整段跑完
                _run_inline(kind, _disconnect_now(ns, "pc"))

        ns.save_session.side_effect = _save

        await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a")))

        assert ns._name_to_sid_map == {}, f"死 sid 不得占名：{ns._name_to_sid_map}"
        assert ns._sid_to_name_key == {}
        assert not _emitted(ns, ENTER_OFFICE_NOTIFICATION), "被拒的死 sid 不得宣告入房"
        # 同名新连接必须能入房（修复前：永久 4105）
        ns.save_session.side_effect = save
        assert await _run(ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a"))) is None

    async def test_register_after_disconnect_began_is_rejected(self, kind: str) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = _ns(kind, sessions)
        ns._alive.discard("pc")  # pre_disconnect 已发生，断连 handler 尚未跑

        ack = await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a")))

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert ns._name_to_sid_map == {}
        assert "office_id" not in sessions["pc"], "被拒注册须按失败收敛摘房"


@pytest.mark.parametrize("kind", KINDS)
class TestAtomicReservation:
    """S1：Agent 席位 / 同名的「校验 + 写入」原子化（注册表判据，与写入同一步）。"""

    async def test_second_agent_joining_in_the_gate_window_is_4101(self, kind: str) -> None:
        sessions: dict[str, dict[str, Any]] = {"ag-1": {}, "ag-2": {}}
        ns = _ns(kind, sessions)
        save = ns.save_session.side_effect
        injected = {"done": False}

        def _save(sid: str, sess: dict[str, Any]) -> None:
            save(sid, sess)
            if sid == "ag-1" and sess.get("office_id") == "office-a" and not injected["done"]:
                injected["done"] = True
                # ag-1 已过阶段 1 闸门、尚未注册 —— ag-2 的完整入房落在这个窗口里
                _run_inline(kind, ns.on_server_join_office("ag-2", _join("agent", "bot-2", "office-a")))

        ns.save_session.side_effect = _save

        ack = await _run(ns.on_server_join_office("ag-1", _join("agent", "bot-1", "office-a")))

        assert isinstance(ack, dict) and ack["code"] == 4101, ack
        agents = [k for k in ns._name_to_sid_map if k[1] == "agent"]
        assert agents == [("office-a", "agent", "bot-2")], agents
        assert "office_id" not in sessions["ag-1"]

    def test_threaded_same_name_registration_has_exactly_one_winner(self, kind: str) -> None:
        if kind == "async":
            pytest.skip("async 原子性由零挂起点保证（结构性），线程竞速只对 sync 有意义")
        sids = [f"pc-{i}" for i in range(8)]
        ns = _ns(kind, {sid: {} for sid in sids})

        class _SlowDict(dict):  # 放大「读到空位 → 写入」之间的窗口，让无锁实现必然交错
            def get(self, key: Any, default: Any = None) -> Any:
                value = super().get(key, default)
                time.sleep(0.005)
                return value

        ns._name_to_sid_map = _SlowDict()
        outcomes: dict[str, str] = {}
        barrier = threading.Barrier(len(sids))

        def _worker(sid: str) -> None:
            barrier.wait()
            try:
                ns._register_name("office-a", "computer", "pc", sid)
                outcomes[sid] = "ok"
            except NameConflictError:
                outcomes[sid] = "4105"

        threads = [threading.Thread(target=_worker, args=(sid,)) for sid in sids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [sid for sid, o in outcomes.items() if o == "ok"]
        assert len(winners) == 1, f"同名注册必须恰有一个赢家：{outcomes}"
        assert dict(ns._name_to_sid_map) == {("office-a", "computer", "pc"): winners[0]}
        # 反向索引只记赢家（败者不得留下指向该键的残留）
        assert ns._sid_to_name_key == {winners[0]: ("office-a", "computer", "pc")}

    def test_threaded_agent_slot_has_exactly_one_winner(self, kind: str) -> None:
        if kind == "async":
            pytest.skip("同上")
        sids = [f"ag-{i}" for i in range(8)]
        ns = _ns(kind, {sid: {} for sid in sids})
        outcomes: dict[str, str] = {}
        barrier = threading.Barrier(len(sids))
        real_items = dict.items

        class _SlowDict(dict):
            def items(self) -> Any:  # type: ignore[override]
                snapshot = list(real_items(self))
                time.sleep(0.005)
                return snapshot

        ns._name_to_sid_map = _SlowDict()

        def _worker(sid: str) -> None:
            barrier.wait()
            try:
                ns._register_name("office-a", "agent", sid, sid)
                outcomes[sid] = "ok"
            except RoomFullError:
                outcomes[sid] = "4101"

        threads = [threading.Thread(target=_worker, args=(sid,)) for sid in sids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(o == "ok" for o in outcomes.values()) == 1, f"一房一 Agent：{outcomes}"


@pytest.mark.parametrize("kind", KINDS)
class TestDisconnectOrdering:
    """S3 + 残留：退房广播先于注销；注册晚于退房快照的残留键须补发 leave。"""

    async def test_leave_is_broadcast_while_name_still_registered(self, kind: str) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = _ns(kind, sessions)
        await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a")))
        ns.server.rooms.return_value = ["pc", "office:office-a"]
        held_at_leave: list[bool] = []
        emit = ns.emit.side_effect

        def _emit(event: str, *args: Any, **kwargs: Any) -> None:
            if event == LEAVE_OFFICE_NOTIFICATION:
                held_at_leave.append(("office-a", "computer", "pc") in ns._name_to_sid_map)
            if emit is not None:
                emit(event, *args, **kwargs)

        ns.emit.side_effect = _emit

        await _run(_disconnect_now(ns, "pc"))

        assert held_at_leave == [True], f"leave 广播必须早于注销（否则同名新连接的 enter 可抢先）：{held_at_leave}"
        assert ns._name_to_sid_map == {}

    async def test_residual_key_after_leave_snapshot_is_announced(self, kind: str) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = _ns(kind, sessions)
        await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a")))
        ns.emit.reset_mock()
        ns.server.rooms.return_value = []  # 断连的退房快照早于入房（线程窗口）⇒ 退房循环看不到该房

        await _run(_disconnect_now(ns, "pc"))

        assert ns._name_to_sid_map == {}
        assert _emitted(ns, LEAVE_OFFICE_NOTIFICATION) == [{"office_id": "office-a", "computer": "pc"}]

    async def test_key_released_during_enter_broadcast_is_compensated(self, kind: str) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = _ns(kind, sessions)

        def _emit(event: str, *args: Any, **kwargs: Any) -> None:
            if event == ENTER_OFFICE_NOTIFICATION:
                # enter 广播途中该 sid 的断连收尾已注销并发出过 leave ⇒ 对端最终看到「leave → enter」
                ns._alive.discard("pc")
                ns._sid_to_name_key.pop("pc", None)
                ns._name_to_sid_map.pop(("office-a", "computer", "pc"), None)

        ns.emit.side_effect = _emit

        await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a")))

        events = [c.args[0] for c in ns.emit.call_args_list]
        assert events == [ENTER_OFFICE_NOTIFICATION, LEAVE_OFFICE_NOTIFICATION], events


@pytest.mark.parametrize("kind", KINDS)
class TestRelayRechecks:
    """S2 + S4：relay 复查与在途断连守卫同一判据（``!= sid``），死 sid ⇒ 404 + 自愈。"""

    async def _setup(self, kind: str) -> tuple[Any, dict[str, dict[str, Any]]]:
        sessions: dict[str, dict[str, Any]] = {"ag": {}, "pc": {}}
        ns = _ns(kind, sessions)
        await _run(ns.on_server_join_office("ag", _join("agent", "bot", "office-a")))
        await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a")))
        return ns, sessions

    async def test_vanished_target_session_is_404_and_self_heals(self, kind: str) -> None:
        ns, sessions = await self._setup(kind)
        del sessions["pc"]  # 会话已不存在，注册表仍指向它（死 sid 残留形态）

        ret = await _run(ns.on_client_get_tools("ag", _get_tools("pc")))

        assert ret == build_computer_not_found_error("pc"), ret
        assert ("office-a", "computer", "pc") not in ns._name_to_sid_map, "死 sid 残留须自愈注销"

    async def test_leave_landing_on_the_recheck_is_404_not_raise(self, kind: str) -> None:
        ns, sessions = await self._setup(kind)
        real = ns.get_sid_by_name
        calls = {"n": 0}

        def _leave_now() -> None:
            ns._sid_to_name_key.pop("pc", None)
            ns._name_to_sid_map.pop(("office-a", "computer", "pc"), None)
            sessions["pc"].pop("office_id", None)

        if kind == "async":

            async def _get_sid(*args: Any) -> Any:
                calls["n"] += 1
                result = await real(*args)
                if calls["n"] == 2:  # relay 复查：返回仍命中，随即退房落地（先注销、后删 office_id）
                    _leave_now()
                return result

        else:

            def _get_sid(*args: Any) -> Any:  # type: ignore[misc]
                calls["n"] += 1
                result = real(*args)
                if calls["n"] == 2:
                    _leave_now()
                return result

        ns.get_sid_by_name = _get_sid

        ret = await _run(ns.on_client_get_tools("ag", _get_tools("pc")))

        assert ret == build_computer_not_found_error("pc"), ret

    async def test_name_swapped_to_new_sid_before_guard_is_404_without_call(self, kind: str) -> None:
        ns, _sessions = await self._setup(kind)
        real = ns._register_inflight_signal

        def _register(sid: str) -> Any:
            # 解析与信号登记之间：旧 sid 断连（其 fire 已发生）、同名新 sid 已注册
            ns._name_to_sid_map[("office-a", "computer", "pc")] = "pc-new"
            return real(sid)

        ns._register_inflight_signal = _register

        ret = await _run(ns.on_client_get_tools("ag", _get_tools("pc")))

        assert ret == build_computer_not_found_error("pc"), ret
        ns.call.assert_not_called()
