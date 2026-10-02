# -*- coding: utf-8 -*-
"""
* 文件名: test_registry_atomicity
* 描述: v0.5.0 跨节点审查 🔴7 / S1-S4 —— 名字注册表的原子性与断连收尾的交错（async + sync 双路径）。

        - 🔴7：断连收尾先跑完、join 后注册 ⇒ 残留死 sid 的键 / 席位 ⇒ 重连永久被拒。
          修法：注册 / 占席在锁内（sync）/ 零挂起点（async）原子校验 ``manager.is_connected``。
        - S1 → #230：同 role 第二个会话的「先查后写」⇒ 双席。现由阶段 1 的原子占席（``_claim_seat``）承担，
          并发输家在动任何成员关系**之前**即被 4101 拒绝（协议「席位检查与占席 MUST 原子」+「校验先于副作用」）。
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

from a2c_smcp.exceptions import RoomFullError
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


def _call_inline(kind: str, fn: Any, *args: Any) -> Any:
    """在钩子里**完整**跑完另一条路径并取回其返回值（sync 直接调用；async 在新事件循环线程里跑完）。"""
    if kind == "sync":
        return fn(*args)
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["ret"] = asyncio.run(fn(*args))
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("ret")


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
        assert ns._seat_to_sid == {} and ns._sid_to_seats == {}, f"死 sid 不得占席：{ns._seat_to_sid}"
        assert not _emitted(ns, ENTER_OFFICE_NOTIFICATION), "被拒的死 sid 不得宣告入房"
        # 同名新连接必须能入房（修复前：永久被拒）
        ns.save_session.side_effect = save
        assert await _run(ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a"))) is None

    async def test_register_after_disconnect_began_is_rejected(self, kind: str) -> None:
        sessions: dict[str, dict[str, Any]] = {"pc": {}}
        ns = _ns(kind, sessions)
        ns._alive.discard("pc")  # pre_disconnect 已发生，断连 handler 尚未跑

        ack = await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a")))

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert ns._name_to_sid_map == {}
        assert ns._seat_to_sid == {}, "已开始断连的 sid 不得占席"
        assert "office_id" not in sessions["pc"], "被拒注册须按失败收敛摘房"
        ns.server.enter_room.assert_not_called()  # 占席即被拒 ⇒ 连 socketio 房都没进


@pytest.mark.parametrize("kind", KINDS)
class TestAtomicSeatClaim:
    """#230（protocol#66 注记 3/4）：席位检查与占席原子、且先于任何副作用——并发输家不碰任何成员关系。"""

    @pytest.mark.parametrize("role", ["agent", "computer"])
    async def test_rival_joining_in_the_effect_window_is_4101_and_untouched(self, kind: str, role: str) -> None:
        """首个会话已过阶段 1（已占席）、尚在阶段 2 —— 同 role 的第二个会话在此窗口完整入房 ⇒ 后者 4101，前者成功。"""
        sessions: dict[str, dict[str, Any]] = {"s-1": {}, "s-2": {}}
        ns = _ns(kind, sessions)
        save = ns.save_session.side_effect
        rival: dict[str, Any] = {}

        def _save(sid: str, sess: dict[str, Any]) -> None:
            save(sid, sess)
            if sid == "s-1" and sess.get("office_id") == "office-a" and "ack" not in rival:
                rival["ack"] = None  # 占位，防重入
                rival["ack"] = _call_inline(kind, ns.on_server_join_office, "s-2", _join(role, "other", "office-a"))

        ns.save_session.side_effect = _save

        ack = await _run(ns.on_server_join_office("s-1", _join(role, "first", "office-a")))

        assert ack is None, ack
        assert rival["ack"] == {"code": 4101, "message": f"Room already has {'an agent' if role == 'agent' else 'a computer'}",
                                "details": {"office_id": "office-a", "role": role}}, rival
        assert ns._seat_to_sid == {("office-a", role): "s-1"}
        assert "office_id" not in sessions["s-2"]
        entered = [c.args[0] for c in ns.server.enter_room.call_args_list]
        assert entered == ["s-1"], f"输家在占席处即被拒，绝不进 socketio 房：{entered}"

    async def test_switching_computer_losing_the_race_keeps_its_old_room(self, kind: str) -> None:
        """并发版场景 #4：C2 在 R2、与 C1 竞争空房 R1 而落败 ⇒ C2 仍在 R2，R2 **未**收到 notify:leave_office。"""
        sessions: dict[str, dict[str, Any]] = {"c1": {}, "c2": {}}
        ns = _ns(kind, sessions)
        assert await _run(ns.on_server_join_office("c2", _join("computer", "c2", "office-b"))) is None
        ns.emit.reset_mock()
        save = ns.save_session.side_effect
        rival: dict[str, Any] = {}

        def _save(sid: str, sess: dict[str, Any]) -> None:
            save(sid, sess)
            if sid == "c1" and sess.get("office_id") == "office-a" and "ack" not in rival:
                rival["ack"] = None
                rival["ack"] = _call_inline(kind, ns.on_server_join_office, "c2", _join("computer", "c2", "office-a"))

        ns.save_session.side_effect = _save

        assert await _run(ns.on_server_join_office("c1", _join("computer", "c1", "office-a"))) is None

        assert isinstance(rival["ack"], dict) and rival["ack"]["code"] == 4101, rival
        assert sessions["c2"].get("office_id") == "office-b", "输家必须仍在原房"
        assert ns._seat_to_sid == {("office-a", "computer"): "c1", ("office-b", "computer"): "c2"}
        assert not _emitted(ns, LEAVE_OFFICE_NOTIFICATION), "校验先于副作用：原房不得收到 leave"

    async def test_failed_enter_frees_the_seat_only_after_the_name(self, kind: str) -> None:
        """审查 🟡1：入房失败回滚必须**先**注销名字、摘房，**最后**才释放席位（与 leave_room / 断连同序）。

        反序时（sync 两次独立取锁之间可达）：席位已空、名字键仍挂在失败方名下 ⇒ 同名新来者占到席位后在
        ``_register_name`` 撞上残留键 ⇒ RegistryInvariantError ⇒ 合法新来者收到 500（且不在重试集合内）。
        钩子在「席位被释放」的那一刻插入同名 rival 的完整入房：正确顺序下 rival 必得空 ack。
        """
        sessions: dict[str, dict[str, Any]] = {"x": {}, "y": {}}
        ns = _ns(kind, sessions)

        def _emit(event: str, *args: Any, **kwargs: Any) -> None:
            if event == ENTER_OFFICE_NOTIFICATION and kwargs.get("skip_sid") == "x":
                raise ConnectionError("enter broadcast failed")  # x 已注册名字后失败

        ns.emit.side_effect = _emit
        real_release = ns._release_seats
        rival: dict[str, Any] = {}

        def _release(sid: str, office_id: Any = None) -> Any:
            out = real_release(sid, office_id)
            if sid == "x" and "ack" not in rival:
                rival["ack"] = None
                if inspect.isawaitable(out):
                    # async：先完成真实释放，再在新线程里跑 rival（与 sync 的「两次取锁之间」同构）
                    async def _then() -> None:
                        await out
                        rival["ack"] = _call_inline(kind, ns.on_server_join_office, "y", _join("computer", "PC", "office-a"))

                    return _then()
                rival["ack"] = _call_inline(kind, ns.on_server_join_office, "y", _join("computer", "PC", "office-a"))
            return out

        ns._release_seats = _release

        ack = await _run(ns.on_server_join_office("x", _join("computer", "PC", "office-a")))

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert rival["ack"] is None, f"席位释放时失败方的名字必须已注销，同名新来者应成功：{rival['ack']!r}"
        assert ns._seat_to_sid == {("office-a", "computer"): "y"}
        assert ns._name_to_sid_map == {("office-a", "computer", "PC"): "y"}

    async def test_roomless_after_convergence_holds_no_seat(self, kind: str) -> None:
        """审查 🟡2：换房途中旧房 leave 已删会话 office_id、却在提交（save）时抛错 ⇒ 收敛为无房，**旧房席位也须释放**。

        否则无房的 sid 永久占着旧房 Computer 席位 ⇒ 旧房恒 4101（直到它断连）。「无房 ⇒ 不持任何席位」须是结构性保证。
        """
        sessions: dict[str, dict[str, Any]] = {"c": {}}
        ns = _ns(kind, sessions)
        assert await _run(ns.on_server_join_office("c", _join("computer", "c", "office-a"))) is None
        save = ns.save_session.side_effect
        boom = {"armed": True}

        def _save(sid: str, sess: dict[str, Any]) -> None:
            if boom["armed"] and sid == "c" and "office_id" not in sess:
                boom["armed"] = False  # 只在 leave_room(旧房) 的提交处抛一次
                raise ConnectionError("session store down")
            save(sid, sess)

        ns.save_session.side_effect = _save

        ack = await _run(ns.on_server_join_office("c", _join("computer", "c", "office-b")))

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert "office_id" not in sessions["c"]
        assert ns._seat_to_sid == {} and ns._sid_to_seats == {}, f"无房会话不得持有任何席位：{ns._seat_to_sid}"

    async def test_uncommitted_old_room_leave_keeps_old_seat_frees_target(self, kind: str) -> None:
        """正对照（提交点分刀）：旧房 leave 在**提交前**失败（广播抛错）⇒ 仍在旧房 ⇒ 旧房席位保留、仅目标席位释放。"""
        sessions: dict[str, dict[str, Any]] = {"c": {}}
        ns = _ns(kind, sessions)
        assert await _run(ns.on_server_join_office("c", _join("computer", "c", "office-a"))) is None

        def _emit(event: str, *args: Any, **kwargs: Any) -> None:
            if event == LEAVE_OFFICE_NOTIFICATION:
                raise ConnectionError("leave broadcast failed")

        ns.emit.side_effect = _emit

        ack = await _run(ns.on_server_join_office("c", _join("computer", "c", "office-b")))

        assert isinstance(ack, dict) and ack["code"] == 500, ack
        assert sessions["c"].get("office_id") == "office-a"
        assert ns._seat_to_sid == {("office-a", "computer"): "c"}, ns._seat_to_sid


class TestThreadedSeatClaim:
    """sync 专属：真实线程竞速下「检查 + 占席」恰一个赢家（async 的原子性由零挂起点结构性保证，无需线程竞速）。"""

    @pytest.mark.parametrize("role", ["agent", "computer"])
    def test_threaded_seat_claim_has_exactly_one_winner(self, role: str) -> None:
        kind = "sync"
        sids = [f"s-{i}" for i in range(8)]
        ns = _ns(kind, {sid: {} for sid in sids})

        class _SlowDict(dict):  # 放大「读到空位 → 写入」之间的窗口，让无锁实现必然交错
            def get(self, key: Any, default: Any = None) -> Any:
                value = super().get(key, default)
                time.sleep(0.005)
                return value

        ns._seat_to_sid = _SlowDict()
        outcomes: dict[str, str] = {}
        barrier = threading.Barrier(len(sids))

        def _worker(sid: str) -> None:
            barrier.wait()
            try:
                ns._claim_seat("office-a", role, sid)
                outcomes[sid] = "ok"
            except RoomFullError:
                outcomes[sid] = "4101"

        threads = [threading.Thread(target=_worker, args=(sid,)) for sid in sids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [sid for sid, o in outcomes.items() if o == "ok"]
        assert len(winners) == 1, f"每 role 一席必须恰有一个赢家：{outcomes}"
        assert dict(ns._seat_to_sid) == {("office-a", role): winners[0]}
        # 反向索引只记赢家（败者不得留下指向该席位的残留）
        assert ns._sid_to_seats == {winners[0]: {("office-a", role)}}


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

    async def test_failed_leave_broadcast_still_releases_name_and_wakes_inflight(self, kind: str) -> None:
        """复审 🔴：S3 把注销挪到 leave 广播之后——广播抛错（pubsub publish 失败等）若跳过注销，死 sid 永久占名
        ⇒ 重连恒被拒（🔴7 换了触发条件）；在途 relay 的断连信号也不得因此漏发。"""
        sessions: dict[str, dict[str, Any]] = {"pc": {}, "pc-2": {}}
        ns = _ns(kind, sessions)
        await _run(ns.on_server_join_office("pc", _join("computer", "pc", "office-a")))
        ns.server.rooms.return_value = ["office:office-a"]
        inflight = ns._register_inflight_signal("pc")

        def _emit(event: str, *args: Any, **kwargs: Any) -> None:
            if event == LEAVE_OFFICE_NOTIFICATION:
                raise ConnectionError("pubsub down")

        ns.emit.side_effect = _emit

        with pytest.raises(ConnectionError):
            await _run(_disconnect_now(ns, "pc"))

        assert ns._name_to_sid_map == {}, "广播失败也必须注销（否则死 sid 永久占名）"
        assert ns._seat_to_sid == {}, "广播失败也必须释放席位（否则该房该 role 永久 4101）"
        assert inflight.is_set(), "清理中途抛错也必须唤醒在途调用"
        ns.emit.side_effect = None
        ns.server.rooms.return_value = []
        assert await _run(ns.on_server_join_office("pc-2", _join("computer", "pc", "office-a"))) is None


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
        assert ("office-a", "computer") not in ns._seat_to_sid, "死 sid 的席位同样须自愈释放"

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
