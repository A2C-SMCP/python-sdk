"""
* 文件名: sync_namespace
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, loguru, pydantic
* 描述: 同步版本SMCP协议Namespace实现 / Synchronous SMCP protocol Namespace implementation
"""

import copy
import threading
from typing import Any, cast

from pydantic import TypeAdapter

from a2c_smcp.exceptions import (
    AlreadyInRoomError,
    RoomFullError,
    RoomRejection,
    SMCPNamespaceError,
)
from a2c_smcp.server.sync_auth import SyncAuthenticationProvider
from a2c_smcp.server.sync_base import SyncBaseNamespace
from a2c_smcp.server.types import OFFICE_ID, SID
from a2c_smcp.server.utils import (
    build_room_rejection_ack,
    default_session_name,
    get_all_sessions_in_office,
    office_ids_of_rooms,
    office_room,
    parse_payload,
    resolve_broadcast_origin,
    warn_on_claimed_identity_mismatch,
)
from a2c_smcp.smcp import (
    CANCEL_TOOL_CALL_EVENT,
    CANCEL_TOOL_CALL_NOTIFICATION,
    ENTER_OFFICE_NOTIFICATION,
    GET_BLOB_EVENT,
    GET_CONFIG_EVENT,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_SKILL_EVENT,
    GET_SKILLS_EVENT,
    GET_TOOLS_EVENT,
    LEAVE_OFFICE_NOTIFICATION,
    PUT_BLOB_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_EVENT,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_EVENT,
    UPDATE_DESKTOP_NOTIFICATION,
    UPDATE_SKILLS_EVENT,
    UPDATE_SKILLS_NOTIFICATION,
    UPDATE_TOOL_LIST_EVENT,
    UPDATE_TOOL_LIST_NOTIFICATION,
    AgentCallData,
    EnterOfficeNotification,
    EnterOfficeReq,
    ErrorCode,
    ErrorPayload,
    GetBlobReq,
    GetBlobRet,
    GetComputerConfigReq,
    GetComputerConfigRet,
    GetDeskTopReq,
    GetDeskTopRet,
    GetResourcesReq,
    GetResourcesRet,
    GetSkillReq,
    GetSkillRet,
    GetSkillsReq,
    GetSkillsRet,
    GetToolsReq,
    GetToolsRet,
    LeaveOfficeNotification,
    LeaveOfficeReq,
    ListRoomReq,
    ListRoomRet,
    PutBlobReq,
    PutBlobRet,
    SessionInfo,
    ToolCallReq,
    UpdateComputerConfigReq,
    build_bad_request_error,
    build_computer_not_found_error,
    build_internal_error,
    build_non_agent_client_call_error,
    build_room_rejection_error,
    is_protocol_error_payload,
)
from a2c_smcp.utils.logger import get_logger

logger = get_logger("server")

# 在途断连守卫的哨兵：与任何合法 Computer 响应（dict / None / flat ErrorPayload）都不相等，
# 用于把「目标在途断连」与「Computer 正常返回 None」区分开（#100 Phase 1，sync）。
# Sentinel for the in-flight disconnect guard: distinct from any valid Computer response.
_DISCONNECTED: object = object()


class SyncSMCPNamespace(SyncBaseNamespace):
    """
    同步SMCP命名空间，处理SMCP相关事件（同步）
    Synchronous Socket.IO namespace for handling SMCP-related events
    """

    def __init__(self, auth_provider: SyncAuthenticationProvider) -> None:
        """
        初始化SMCP命名空间（同步）
        Initialize SMCP namespace (sync)
        """
        super().__init__(namespace=SMCP_NAMESPACE, auth_provider=auth_provider)
        # 在途 ``client:*`` 调用的「断连信号」登记表，以**目标 Computer SID** 为 key（#100 Phase 1，async 镜像）。
        # 同步版 ``self.call`` 阻塞当前 worker 线程，故由独立 daemon 子线程跑 ``call``、主线程等共享事件；
        # registry 跨线程读写（worker 线程登记/注销 vs ``on_disconnect`` 线程触发），用 ``_inflight_lock`` 守护。
        # Per-target in-flight disconnect signals (#100 Phase 1, sync mirror): target Computer SID -> set of Events.
        self._inflight_disconnect_signals: dict[SID, set[threading.Event]] = {}
        self._inflight_lock = threading.Lock()

    def on_disconnect(self, sid: SID) -> None:
        """断连处理（同步）：先走 base 的 name/room 清理，再唤醒以 ``sid`` 为目标的全部在途 ``client:*`` 调用（#100 Phase 1）。
        On disconnect: run base name/room cleanup first, then wake every in-flight call targeting ``sid``.

        **先 super() 再 fire**（与 async 对齐，关闭 TOCTOU 微窗）：``_unregister_name`` 删除 name 映射后，窗内并发新调用的
        TOCTOU 复查 ``get_sid_by_name`` 读到 None 直接短路 404，不会错过 fire 退化为满 timeout。信号按 sid 登记、``super()``
        不触碰信号 registry，故 fire 置于其后对已登记信号零损失；``_fire`` 持 ``_inflight_lock`` 而 ``super()`` 不持锁，无死锁。
        Super-then-fire (mirrors async) closes the TOCTOU window; already-registered signals still wake.
        """
        super().on_disconnect(sid)
        self._fire_inflight_disconnect_signals(sid)

    def _fire_inflight_disconnect_signals(self, sid: SID) -> None:
        """唤醒以 ``sid`` 为目标的全部在途断连信号（幂等）。Wake all in-flight signals targeting ``sid`` (idempotent)."""
        with self._inflight_lock:
            signals = list(self._inflight_disconnect_signals.get(sid, ()))
        for ev in signals:
            ev.set()

    def _register_inflight_signal(self, computer_sid: SID) -> threading.Event:
        """登记并返回一个针对 ``computer_sid`` 的在途断连信号。Register and return a fresh disconnect signal."""
        ev = threading.Event()
        with self._inflight_lock:
            self._inflight_disconnect_signals.setdefault(computer_sid, set()).add(ev)
        return ev

    def _discard_inflight_signal(self, computer_sid: SID, ev: threading.Event) -> None:
        """注销在途信号；set 空时删除 key（防 dict 泄漏）。Unregister a signal; drop the key when the set empties."""
        with self._inflight_lock:
            bucket = self._inflight_disconnect_signals.get(computer_sid)
            if bucket is not None:
                bucket.discard(ev)
                if not bucket:
                    self._inflight_disconnect_signals.pop(computer_sid, None)

    def _rooms_to_leave_on_disconnect(self, sid: SID) -> list[str]:
        """只离开 **office 房**（``office:`` 前缀）并交出**原始 office_id**——本类 ``leave_room`` 以 office_id 为参数（#216）。
        Only office rooms, as raw office ids (this class's ``leave_room`` takes an office id, #216).
        """
        return office_ids_of_rooms(self.rooms(sid))

    def enter_room(self, sid: SID, room: OFFICE_ID, namespace: str | None = None) -> None:
        """
        客户端加入房间，维护session中的sid/name/office_id字段（同步）
        Client joins room, maintain sid/name/office_id in session (sync)

        #213 两阶段结构（协议 room-model.md §Computer 加入规则 注记 3「校验必须先于副作用」）：
        阶段 1 校验（角色约束 / 目标房同名 / 名字注册闸门，零成员关系副作用）；阶段 2 生效
        （退旧房 → 入新房 → 写会话 → 注册 name → 广播）。不变量：本方法抛错后 socketio 真实成员
        关系与会话状态**一致**——被拒客户端绝不留在目标房里收该房 ``notify:*``。
        Two-phase structure mirroring the async implementation (#213).
        """
        session = self.get_session(sid)

        if session.get("sid") != sid:
            session["sid"] = sid
        if not session.get("name"):
            session["name"] = default_session_name(session.get("role"), sid)

        # ── 阶段 1：校验（零成员关系副作用） / Phase 1: validation (no membership side effect) ──
        past_room: OFFICE_ID | None = None
        if session.get("role") == "agent":
            if session.get("office_id") and session.get("office_id") != room:
                logger.warning(
                    f"Agent sid: {sid} already in room: {session.get('office_id')}, can't join room: {room}",
                )
                # 领域异常承载协议码（4106）：handler 据此回 flat ErrorPayload（#214）
                raise AlreadyInRoomError()
            elif not session.get("office_id"):
                for participant_sid, _participant_eio_sid in self.server.manager.get_participants(SMCP_NAMESPACE, office_room(room)):
                    participant_session = self.get_session(participant_sid)
                    if participant_session.get("role") == "agent":
                        logger.warning(f"Room {room!r} already has an agent; rejecting sid={sid}")
                        raise RoomFullError()
            else:
                logger.warning(
                    f"Agent sid: {sid} already in room: {session.get('office_id')}. 正在重复加入房间",
                )
                return
        else:
            if session.get("office_id") == room:
                logger.warning(
                    f"Computer sid: {sid} already in room: {session.get('office_id')}. 正在重复加入房间",
                )
                return

            # Computer 可切换房间，但**退房动作推迟到阶段 2**：目标房闸门必须先查完（协议 注记 3，
            # 否则目标房同名被拒时它已离开原房且对端已收到 notify:leave_office，落成无房中间态）。
            # The old room is left in phase 2 — all target-room gates must run first.
            past_room = session.get("office_id") or None

        # 房内同 role 同名闸门（#215，键 ``(office_id, role, name)``；Agent / Computer 同一判据）：必须早于任何
        # 成员关系变更（#213）；目标房显式传入（换房的 Computer 此刻会话里仍是旧房）。镜像 async 实现。
        # Per-room, per-role name gate (#215) — must fire before any membership change (#213).
        self._ensure_name_registerable(room, session["role"], session["name"], sid)

        # ── 阶段 2：生效（成员关系变更） / Phase 2: effects (membership changes) ──
        registered = False
        try:
            if past_room:
                # 旧房离开不可撤销（广播已发出）⇒ 必须排在校验之后 / irreversible: post-validation only
                self.leave_room(sid, past_room)

            # socketio 房名 = ``office:{room}``，与私有 sid 房命名空间不相交（#216）/ prefixed room name (#216)
            super().enter_room(sid, office_room(room))
            session["office_id"] = room
            self.save_session(sid, session)

            # 注册name到sid的映射
            # Register name-to-sid mapping
            self._register_name(room, session["role"], session["name"], sid)
            registered = True

            # 根据角色发送不同的通知 / Send different notifications based on role
            notification_data: EnterOfficeNotification = {"office_id": room}
            if session.get("role") == "computer":
                notification_data["computer"] = session.get("name")
            else:
                notification_data["agent"] = session.get("name")

            self._emit_to_office(ENTER_OFFICE_NOTIFICATION, notification_data, room, skip_sid=sid)
        except Exception:
            # 失败收敛 —— 按**提交点**分刀：旧房离开未提交（会话 office_id 仍 == past_room）⇒ 一切原样，
            # 不收敛（否则会把客户端从合法所属的旧房无声摘除）；否则摘成「无房」并与会话同步。
            # Commit-point discrimination; see the async implementation for the full rationale.
            if past_room is None or session.get("office_id") != past_room:
                try:
                    # 按反向索引注销本次注册的键；``_unregister_name`` 自带归属守卫，绝不替并发抢占者注销
                    # Unregisters via the reverse index; ``_unregister_name`` is ownership-guarded.
                    if registered:
                        self._unregister_name(sid)
                    for stale_office in office_ids_of_rooms(list(self.rooms(sid))):
                        # 非成员退房是安全 no-op。**静默**摘除、不发 notify:leave_office（同 async：目标房
                        # 从未宣告；旧房若已提交离开，其广播已由 leave_room 发出）。已知边界：末步 enter
                        # 广播部分投递后抛错时，已收到的对端会留下幻影成员（协议无撤回原语）。
                        # Silent eviction, mirroring the async implementation.
                        super().leave_room(sid, office_room(stale_office))
                    if "office_id" in session:
                        del session["office_id"]
                        self.save_session(sid, session)
                except Exception as conv_err:
                    # 收敛不得掩盖原始异常（**已知边界**：收敛自身失败则该 sid 可能仍留房且会话带 office_id，
                    # 二者一致但未清零；刻意不做二次收敛、不上抛）。镜像 async 实现。
                    # Convergence must not mask the original error; no second convergence (#213).
                    logger.error(f"enter_room 失败收敛未完成 sid={sid} room={room}: {conv_err}", exc_info=True)
            raise

    def leave_room(self, sid: SID, room: OFFICE_ID, namespace: str | None = None) -> None:
        """
        在离开房间之前发布离开消息（同步）
        Publish leave message before leaving room (sync)
        """
        session = self.get_session(sid)

        # 构建离开通知，使用name而不是sid
        # Build leave notification using name instead of sid
        client_name = session.get("name", "")
        notification = (
            LeaveOfficeNotification(office_id=room, computer=client_name)
            if session.get("role") == "computer"
            else LeaveOfficeNotification(office_id=room, agent=client_name)
        )
        self._emit_to_office(LEAVE_OFFICE_NOTIFICATION, notification, room, skip_sid=sid)

        # 注销name映射
        # Unregister name mapping
        self._unregister_name(sid)

        if "office_id" in session:
            del session["office_id"]
        self.save_session(sid, session)

        # socketio 房名带 ``office:`` 前缀（#216）/ prefixed socketio room name (#216)
        super().leave_room(sid, office_room(room))

    def on_server_join_office(
        self, sid: str, data: EnterOfficeReq | None = None, *_extra: Any
    ) -> ErrorPayload | None:
        """
        同步：Computer/Agent加入房间
        Sync: Computer or Agent joins room

        #214 ack 形态（镜像 async；协议 error-handling.md:177-205）：**成功 ⇒ 空 ack**（``None``）；
        失败 ⇒ flat ``ErrorPayload``（400 / 403 / 4101 / 4105 / 4106 / 500）。
        Ack shape per #214 — mirrors the async implementation.

        **参数绑定也必须回 ack**（error-handling.md:102-106 覆盖「框架层参数提取器」时机）：
        ``data`` 具默认值、多余位置参数由 ``*_extra`` 吸收 ⇒ 二者都落到 schema 校验 ⇒ ``400``；
        否则异常逃出 handler ⇒ 不发 ACK ⇒ 调用方挂到自身超时。
        """
        # 具备 ack 通道 ⇒ 校验失败（含多余位置参数）MUST 回 400，MUST NOT 静默不 ack（error-handling.md:102-106）
        role_info = parse_payload("server:join_office", TypeAdapter(EnterOfficeReq), data, _extra, sid)
        if role_info is None:
            return build_bad_request_error()
        expected_role = role_info["role"]
        declared_name = role_info["name"]
        office_id = role_info["office_id"]
        if not office_id:
            # ``office_id`` 取值非法 ⇒ 400（空串入房后会话 office_id 为假值 ⇒ 幽灵成员）。镜像 async 实现。
            logger.warning(f"server:join_office office_id 为空 sid={sid}")
            return build_bad_request_error()

        session = self.get_session(sid)
        # 4106 的 details 需报「会话**当前**所在房」⇒ 必须在 enter_room 可能改动/收敛会话之前取。
        previous_office = session.get("office_id")
        backup_session = copy.deepcopy(session)

        try:
            # 身份声明一致性（协议 events.md:610）：同一 sid 声明了与既有会话不同的 role **或**
            # name ⇒ 403（非房间语义）；先于任何会话写入与房间副作用，故无需回滚。
            # #221：此前只判 role（name 半刻意未实现）。协议核对结论见 async 版注释——403 的定义即
            # 「role / name 与会话不符」，faq.md:162 的补救是「重连或换 sid」，rust 亦已实现该半。
            # 判据用 `is not None`（与 role 侧对称；本仓会话里的 name 必然非空，故与真值性等价——
            # 详见 async 版注释）。镜像 async 实现。
            # Identity mismatch in *either* declared field ⇒ 403; identity is immutable per sid.
            existing_role = session.get("role")
            existing_name = session.get("name")
            # 声明空名时 `enter_room` 会归一成默认名 ⇒ 判据须用归一后的期望值比对（理由详见 async 版）。
            # Empty declarations are compared post-normalization — see the async implementation.
            expected_name = declared_name or default_session_name(expected_role, sid)
            if (existing_role is not None and existing_role != expected_role) or (
                existing_name is not None and existing_name != expected_name
            ):
                logger.warning(
                    f"server:join_office 身份声明不符 sid={sid}: "
                    f"session=({existing_role!r}, {existing_name!r}) "
                    f"request=({expected_role!r}, {declared_name!r})",
                )
                return build_room_rejection_error(ErrorCode.FORBIDDEN)

            session["role"] = expected_role
            session["name"] = role_info["name"]
            self.save_session(sid, session)

            self.enter_room(sid, office_id)
            return None
        except Exception as e:
            # 只回滚本处理器写入的 role / name；**房间归属不回滚**——``enter_room`` 失败时已按提交点
            # 收敛（可能已删除 office_id），整体覆盖 backup 会把旧房号复活、使会话与真实成员关系分叉。
            # 缺失字段须**删除**而非赋 None（后者会污染后续 role 判定）。镜像 async 实现（#213）。
            # Roll back only the fields written here; room ownership belongs to enter_room (#213).
            live_session = self.get_session(sid)
            for field in ("role", "name"):
                if field in backup_session:
                    live_session[field] = backup_session[field]
                else:
                    live_session.pop(field, None)
            self.save_session(sid, live_session)

            if isinstance(e, RoomRejection):
                # 业务拒绝：领域异常承载结构化 reason ⇒ 转 flat ErrorPayload（协议码 + 自身相关上下文）
                logger.warning(f"server:join_office 被拒 sid={sid} code={e.code}: {e}")
                # 走**不抛**的助手（本分支在 except 子句内，二次抛出会顶替原异常逃出 handler）
                return build_room_rejection_ack(
                    e,
                    target_office_id=office_id,
                    declared_role=expected_role,
                    current_office_id=previous_office,
                )

            # 未知内部异常：笼统文案回 ack（不让客户端挂到超时），原文只进日志（#214 / §通用错误码 500）
            logger.error(f"server:join_office 未预期异常 sid={sid}: {e}", exc_info=True)
            return build_internal_error()

    def on_server_leave_office(
        self, sid: str, data: LeaveOfficeReq | None = None, *_extra: Any
    ) -> ErrorPayload | None:
        """
        同步：Computer/Agent离开房间
        Sync: Computer or Agent leaves room

        房间号**只取服务端权威的会话状态**，绝不用客户端载荷（载荷可携带任意房间号 ⇒ 跨房注入，
        或携带 None ⇒ ``room=None`` 即全命名空间广播）。无房可退时幂等成功。
        The room comes from the authoritative session only, never from the client payload.

        #214 ack 形态：**成功与「无房幂等」都回空 ack**（``None``）；载荷 schema 校验失败回 ``400``；
        未预期内部异常回 ``500``。「载荷房号与会话不符」**不是**错误（协议 MUST NOT 因此拒绝）。

        **参数绑定也必须回 ack**：``data`` 具默认值、``*_extra`` 吸收多余位置参数 ⇒ 二者都落到
        schema 校验 ⇒ ``400``（否则异常逃出 handler ⇒ 不发 ACK ⇒ 调用方挂到自身超时）。
        """
        # 具备 ack 通道 ⇒ 校验失败 MUST 回 400，MUST NOT 静默不 ack（error-handling.md:102-106）
        leave_info = parse_payload("server:leave_office", TypeAdapter(LeaveOfficeReq), data, _extra, sid)
        if leave_info is None:
            return build_bad_request_error()

        try:
            session = self.get_session(sid)

            office_id = session.get("office_id")
            if not office_id:
                # 无房可退：清理路径幂等成功。但会话可能已与真实成员关系**漂移**（旧版采信载荷
                # ⇒ 清空会话却把人留在原房），故按服务端权威的 socketio 成员关系收敛一次。
                # Idempotent success, but first converge the authoritative socketio membership.
                # 只收敛 **office 房**（``office:`` 前缀，#216）——结构上排除私有 sid 房与其它房。
                for stale_office in office_ids_of_rooms(list(self.rooms(sid))):
                    self.leave_room(sid, stale_office)
                return None

            claimed = leave_info.get("office_id")
            if claimed != office_id:
                # 载荷与会话不一致：仅告警并按会话执行（协议明令 MUST NOT 因此拒绝）
                # Payload disagrees with the session: warn and follow the session.
                logger.warning(
                    f"leave_office: payload claims office {claimed!r} but session {sid} is in {office_id!r}; "
                    f"leaving the session's office",
                )

            self.leave_room(sid, office_id)
            return None
        except Exception as e:
            # 未知内部异常：笼统文案回 ack（不让客户端挂到超时），原文只进日志（#214 / §通用错误码 500）
            logger.error(f"server:leave_office 未预期异常 sid={sid}: {e}", exc_info=True)
            return build_internal_error()

    def on_server_tool_call_cancel(self, sid: str, data: AgentCallData | None = None, *_extra: Any) -> None:
        """
        同步：广播取消ToolCall到房间内的其他成员
        Sync: broadcast tool call cancellation to other members in the room

        fire-and-forget：载荷非法 / 非 Agent / 未入房 ⇒ 告警后**静默丢弃**；载荷 ``agent`` 与会话不符只告警、
        按会话执行（#216）。镜像 async。
        """
        agent_call = parse_payload(CANCEL_TOOL_CALL_EVENT, TypeAdapter(AgentCallData), data, _extra, sid)
        if agent_call is None:
            return
        origin = resolve_broadcast_origin(CANCEL_TOOL_CALL_EVENT, self._get_session_or_none(sid), sid, "agent")
        if origin is None:
            return
        office_id, agent_name = origin
        # 出向 ``agent`` 取会话名；载荷自称不符只告警、按会话执行（events.md §房间广播类事件的目标来源：MUST NOT 拒绝）
        # The outbound ``agent`` is the session's name; a mismatching claim is logged, never rejected.
        warn_on_claimed_identity_mismatch(CANCEL_TOOL_CALL_EVENT, sid, "agent", agent_call["agent"], agent_name)
        agent_call = AgentCallData(agent=agent_name, req_id=agent_call["req_id"])

        # 广播到 office 房间，而不是 Agent 的私有房间 / Broadcast to office room, not Agent's private room
        self._emit_to_office(CANCEL_TOOL_CALL_NOTIFICATION, agent_call, office_id, skip_sid=sid)

    def _broadcast_computer_update(self, sid: str, data: Any, extra: tuple[Any, ...], event: str, notification: str) -> None:
        """``server:update_*`` ×4 的公共实现（同步）：校验 → 判定发起者 → 向其所在房广播。镜像 async（#216）。
        Shared body of the four ``server:update_*`` handlers (sync mirror).
        """
        update_req = parse_payload(event, TypeAdapter(UpdateComputerConfigReq), data, extra, sid)
        if update_req is None:
            return
        origin = resolve_broadcast_origin(event, self._get_session_or_none(sid), sid, "computer")
        if origin is None:
            return
        office_id, computer_name = origin
        # 出向 ``computer`` 取会话名：否则可用 ``computer="peer"`` 让接收方去刷新另一个 Computer（events.md
        # §房间广播类事件的目标来源）。载荷自称不符只告警、按会话执行（MUST NOT 拒绝）。
        # The outbound ``computer`` is the session's name, never the payload's claim.
        warn_on_claimed_identity_mismatch(event, sid, "computer", update_req["computer"], computer_name)
        self._emit_to_office(notification, {"computer": computer_name}, office_id, skip_sid=sid)

    def on_server_update_config(self, sid: str, data: UpdateComputerConfigReq | None = None, *_extra: Any) -> None:
        """
        同步：广播更新MCP配置
        Sync: broadcast MCP config update
        """
        self._broadcast_computer_update(sid, data, _extra, UPDATE_CONFIG_EVENT, UPDATE_CONFIG_NOTIFICATION)

    def on_server_update_tool_list(self, sid: str, data: UpdateComputerConfigReq | None = None, *_extra: Any) -> None:
        """
        同步：广播工具列表更新
        Sync: broadcast tool list update
        """
        self._broadcast_computer_update(sid, data, _extra, UPDATE_TOOL_LIST_EVENT, UPDATE_TOOL_LIST_NOTIFICATION)

    def on_client_tool_call(self, sid: str, data: ToolCallReq | None = None, *_extra: Any) -> dict | ErrorPayload:
        """
        同步：响应工具调用，使用 call 方法等待 Computer 返回结果
        Sync: respond to tool call, use call method to wait for Computer response

        经 :meth:`_relay_client_call` 统一载荷校验 + SID 解析 + office/role 隔离 + flat ErrorPayload 透传
        （#99/#216）；「仅 Agent 可发起」由 relay 对全部 ``client:*`` 统一判定。载荷按 ``ToolCallReq`` 校验
        （#216 起与 async 同一 schema；此前 sync 只校验「是 dict」、timeout 可缺省）。
        Routed via ``_relay_client_call`` (#99/#216); validated as ``ToolCallReq`` like the async side.
        """
        # tool_call 响应是 Computer 回传的 CallToolResult 原样 dict（无固定 A2C TypedDict），ret_adapter 用
        # TypeAdapter(dict) 原样透传；per-request timeout（取自校验后的载荷）透传给底层 self.call。
        # tool_call's response is the Computer's raw CallToolResult dict, so use TypeAdapter(dict) passthrough.
        return cast(
            "dict | ErrorPayload",
            self._relay_client_call(
                sid,
                data,
                _extra,
                TOOL_CALL_EVENT,
                TypeAdapter(ToolCallReq),
                TypeAdapter(dict),
                timeout_from_request=True,
            ),
        )

    def on_client_get_tools(self, sid: str, data: GetToolsReq | None = None, *_extra: Any) -> GetToolsRet | ErrorPayload:
        """
        同步：获取指定 Computer 的工具列表（``client:get_tools``）/ Sync: get tool list of specified Computer.

        经 :meth:`_relay_client_call` 统一 isolation + flat ErrorPayload 透传（v0.2.2：所有 ``client:*``
        ack 统一 flat ErrorPayload，旧路由非豁免）。
        """
        return cast(
            "GetToolsRet | ErrorPayload",
            self._relay_client_call(sid, data, _extra, GET_TOOLS_EVENT, TypeAdapter(GetToolsReq), TypeAdapter(GetToolsRet)),
        )

    def on_client_get_desktop(self, sid: str, data: GetDeskTopReq | None = None, *_extra: Any) -> GetDeskTopRet | ErrorPayload:
        """
        同步：获取指定 Computer 的桌面视图（``client:get_desktop``）/ Sync: get desktop view from Computer.

        要求 Agent 与 Computer 同一 office；经 :meth:`_relay_client_call` 统一 isolation + flat ErrorPayload 透传。
        """
        return cast(
            "GetDeskTopRet | ErrorPayload",
            self._relay_client_call(sid, data, _extra, GET_DESKTOP_EVENT, TypeAdapter(GetDeskTopReq), TypeAdapter(GetDeskTopRet)),
        )

    def _relay_client_call(
        self,
        sid: str,
        data: Any,
        extra: tuple[Any, ...],
        event: str,
        req_adapter: TypeAdapter[Any],
        ret_adapter: TypeAdapter[Any],
        *,
        timeout_from_request: bool = False,
    ) -> Any:
        """同步版通用 ``client:*`` 事件路由 / Sync mirror of ``_relay_client_call``.

        统一收敛 office/role 隔离校验、Computer SID 解析、flat ErrorPayload 透传。
        Unifies office/role isolation, Computer SID lookup, and flat-ErrorPayload pass-through.

        Computer 名在**发起者所在房内**未命中（#215：他房同名 / 名字属于 Agent 同样落此分支，与「不存在」不可区分）
        → 回 flat ``ErrorPayload(404)``（#92，error-handling.md §20 + §78），**不**抛未捕获异常（同步 socketio 下
        抛异常会杀线程、不回 ack，致 Agent ``call`` 静默超时）。

        拒绝顺序镜像 async（#216）：载荷非法 ``400`` → 发起者会话已不存在（raise，唯一静默路径）→ 未入房
        ``4103`` → 非 Agent ``403`` → 房内解析不到 ``404``；注册表不变量破坏仍 raise ``SMCPNamespaceError``。
        Rejection order mirrors async (#216): 400 → silent → 4103 → 403 → 404.

        ``timeout_from_request`` 仅 ``tool_call`` 为真（取载荷 ``timeout``）；其余 ``client:*`` 走 socketio 默认。
        Only ``tool_call`` passes its per-request timeout; other events use the socketio default.

        协议依据 / Protocol: events.md 各 ``client:*`` 事件 + error-handling.md flat ErrorPayload.
        """
        req = parse_payload(event, req_adapter, data, extra, sid)
        if req is None:
            return build_bad_request_error()

        agent_session = self.get_session(sid)
        if agent_session is None:
            # 发起者（Agent）飞行中断连：会话已不存在。显式 raise 替代 None.get 的 AttributeError
            # （对齐 #31 隔离不变量显式 raise）；协议许可 Server MAY 静默不 ack、不新增错误码（v0.2.2）。
            # Originator (Agent) disconnected in-flight: session gone. Explicit raise instead of an
            # AttributeError on None.get (aligns with #31). Protocol allows Server MAY silently not-ack.
            raise SMCPNamespaceError(f"发起者会话不存在（可能已断连）：{event} / originator session gone")
        agent_office = agent_session.get("office_id")
        if not agent_office:
            # 无房发起者无从在房内解析目标（room-model.md §跨房间访问防护）⇒ flat 4103（#216 §四）
            # An office-less initiator cannot resolve anything in-room ⇒ flat 4103 (#216)
            logger.warning(f"{event} 发起者未加入任何房间 sid={sid}")
            return build_room_rejection_error(ErrorCode.NOT_IN_ROOM)
        if agent_session.get("role") != "agent":
            # client:* 发起方为 Agent；存活调用方的拒绝必须可感 ⇒ flat 403（#216）。镜像 async。
            logger.warning(f"{event} 非 Agent 发起 sid={sid} role={agent_session.get('role')!r}")
            return build_non_agent_client_call_error()

        # #215：名字解析**限定在发起者所在房内**、按 role=computer 查（MUST NOT 全局裸名解析）。解析不到统一回
        # 404——与「该名字存在于其它房 / 属于同房 Agent」对外不可区分，杜绝探测他房成员存在性。
        # #215: resolve inside the initiator's office only; a miss is a uniform 404 (no cross-room probing).
        computer_name = req["computer"]
        computer_sid = self.get_sid_by_name(agent_office, "computer", computer_name)
        if not computer_sid:
            return build_computer_not_found_error(computer_name)

        try:
            session = self.get_session(computer_sid)
        except KeyError:
            # socket 已被移除（断连收尾）；注销先于移除 ⇒ 下方复查必然判为「已离开」
            # Socket already removed; unregistration precedes removal, so the re-check below sees it gone.
            session = None
        # 解析后目标已离房 / 断连（同步服务端多线程分发下可达：解析与读会话之间另一线程跑完 on_disconnect / 换房）
        # ⇒ 与「不存在」同义回 404（同在途断连分支）。以注册表复查判定：注销恒先于清会话 office_id。
        # Target left between resolution and session read (reachable under threaded sync dispatch) → 404.
        if self.get_sid_by_name(agent_office, "computer", computer_name) != computer_sid:
            return build_computer_not_found_error(computer_name)
        # 不变量守卫：注册表仍指向该 sid、会话却与键矛盾 ⇒ 注册表损坏，显式 raise（#31：隔离不变量只 raise）。
        # Invariant guard: registry still points at the sid but the session contradicts the key → raise (#31).
        if not session or session.get("role") != "computer" or session.get("office_id") != agent_office:
            raise SMCPNamespaceError(f"名字注册表与会话不一致：{event} / name registry diverged from session")

        # tool_call 透传 per-request timeout；其余 client:* 事件 timeout=None → 用 socketio 默认
        # （勿对默认事件传 timeout=None，socketio 下会变成永久等待而非默认 60s）。
        # Pass tool_call's per-request timeout; other client:* events leave it None (socketio default).
        call_kwargs: dict[str, Any] = {"to": computer_sid, "namespace": SMCP_NAMESPACE}
        if timeout_from_request:
            call_kwargs["timeout"] = req["timeout"]

        client_response = self._call_with_disconnect_guard(event, data, agent_office, computer_name, computer_sid, call_kwargs)
        if client_response is _DISCONNECTED:
            # 目标在途断连：Computer 中途消失 == 不存在，回 flat ErrorPayload(404)（与 #92/#99 同一 payload）。
            # Target disconnected mid-flight: a vanished Computer == not found → flat ErrorPayload(404).
            return build_computer_not_found_error(computer_name)
        if is_protocol_error_payload(client_response):
            return TypeAdapter(ErrorPayload).validate_python(client_response)
        return ret_adapter.validate_python(client_response)

    def _call_with_disconnect_guard(
        self,
        event: str,
        data: Any,
        office_id: OFFICE_ID,
        computer_name: str,
        computer_sid: SID,
        call_kwargs: dict[str, Any],
    ) -> Any:
        """在途断连守卫（#100 Phase 1，sync）：把阻塞的 ``self.call`` 丢到 daemon 子线程，主线程等「完成 OR 断连信号」.

        同步 socketio ``self.call`` 阻塞当前线程、其等待原语在 ``call()`` 内部不可达，无法外部中断；故子线程跑
        ``call``、主线程 ``wait`` 一个**既被完成路径又被 ``on_disconnect`` 触发**的共享事件。三结局严格区分：
          - 子线程正常返回 → 透传结果（``ret_adapter`` 在调用方校验）；
          - 子线程抛异常（如真超时 ``TimeoutError``）→ 在主线程**原样重抛**（绝不转 404）；
          - 断连先到（box 仍空）→ 返回 ``_DISCONNECTED`` 哨兵 → 调用方回 404。
        断连时被遗弃的子线程仍阻塞在 ``call`` 内直到其自身 timeout 退出（有界 daemon 僵尸，仅断连这一罕见事件触发）。
        **容量评估**：每个在途调用（含 happy path）多占 1 个 daemon 线程（原 worker 阻塞在 ``wait``）；worst-case
        线程上限随**并发在途调用数**线性增长，断连时再叠加「等各自 ``timeout`` 退出」的僵尸 runner。signaling 负载下可接受，
        高并发场景如需收敛可另开 issue 引入线程池/信号量（不在本 issue 范围）。
        Sync in-flight disconnect guard: run blocking ``self.call`` in a daemon thread, wait a shared event set by
        either completion or ``on_disconnect``; orphaned thread exits at its own timeout (bounded daemon zombie).
        Capacity note: each in-flight call costs one extra daemon thread; worst-case thread count grows linearly
        with concurrent in-flight calls (plus zombies awaiting their own timeout on disconnect).
        """
        disconnect_ev = self._register_inflight_signal(computer_sid)
        try:
            # TOCTOU 复查：解析 SID 与登记之间目标可能已断连（信号永不会被 fire）。用 ``get_sid_by_name`` 复查
            # （``on_disconnect`` → base ``_unregister_name`` 清掉 name 映射）关闭该窗口。
            # TOCTOU re-check: target may have died between SID resolve and registration → short-circuit to 404.
            if disconnect_ev.is_set() or self.get_sid_by_name(office_id, "computer", computer_name) is None:
                return _DISCONNECTED

            # ``disconnect_ev`` 一事二用：完成路径由 ``_runner`` 在 finally 触发，断连路径由 ``on_disconnect`` 触发，
            # 主线程 ``wait`` 二者之一。醒来后查 box 区分（``value`` 优先于断连，确保真 ack 不被误判为断连）。
            # ``disconnect_ev`` doubles as completion + disconnect signal; the box disambiguates after wakeup.
            box: dict[str, Any] = {}

            def _runner() -> None:
                try:
                    box["value"] = self.call(event, data, **call_kwargs)
                except BaseException as exc:  # noqa: BLE001 捕获后在调用方线程重抛 / re-raised on caller thread
                    box["error"] = exc
                finally:
                    disconnect_ev.set()

            threading.Thread(target=_runner, name=f"a2c-relay-{event}", daemon=True).start()
            disconnect_ev.wait()
            if "value" in box:
                return box["value"]
            if "error" in box:
                raise box["error"]
            return _DISCONNECTED
        finally:
            self._discard_inflight_signal(computer_sid, disconnect_ev)

    def on_client_get_config(self, sid: str, data: GetComputerConfigReq | None = None, *_extra: Any) -> GetComputerConfigRet | ErrorPayload:
        """同步：透明转发 ``client:get_config`` 至目标 Computer，返回其 MCP 配置 / Sync relay of ``client:get_config``.

        返回协议已定义的 ``GetComputerConfigRet``（``servers`` 占位符原样，解析后密钥不外传）；
        经 :meth:`_relay_client_call` 统一 isolation + flat ErrorPayload 透传。
        协议依据 / Protocol: events.md §client:get_config；data-structures.md §GetComputerConfigRet.
        """
        return cast(
            "GetComputerConfigRet | ErrorPayload",
            self._relay_client_call(
                sid,
                data,
                _extra,
                GET_CONFIG_EVENT,
                TypeAdapter(GetComputerConfigReq),
                TypeAdapter(GetComputerConfigRet),
            ),
        )

    def on_client_get_resources(self, sid: str, data: GetResourcesReq | None = None, *_extra: Any) -> GetResourcesRet | ErrorPayload:
        """
        同步：透明转发 ``client:get_resources`` 至目标 Computer（含 cursor 翻页）。
        Sync: relay ``client:get_resources`` to the target Computer (with cursor pagination).
        """
        return cast(
            "GetResourcesRet | ErrorPayload",
            self._relay_client_call(sid, data, _extra, GET_RESOURCES_EVENT, TypeAdapter(GetResourcesReq), TypeAdapter(GetResourcesRet)),
        )

    def on_client_get_skills(self, sid: str, data: GetSkillsReq | None = None, *_extra: Any) -> GetSkillsRet | ErrorPayload:
        """同步：透明转发 ``client:get_skills`` / Sync relay of ``client:get_skills``."""
        return cast(
            "GetSkillsRet | ErrorPayload",
            self._relay_client_call(sid, data, _extra, GET_SKILLS_EVENT, TypeAdapter(GetSkillsReq), TypeAdapter(GetSkillsRet)),
        )

    def on_client_get_skill(self, sid: str, data: GetSkillReq | None = None, *_extra: Any) -> GetSkillRet | ErrorPayload:
        """同步：透明转发 ``client:get_skill`` / Sync relay of ``client:get_skill``."""
        return cast(
            "GetSkillRet | ErrorPayload",
            self._relay_client_call(sid, data, _extra, GET_SKILL_EVENT, TypeAdapter(GetSkillReq), TypeAdapter(GetSkillRet)),
        )

    def on_client_get_blob(self, sid: str, data: GetBlobReq | None = None, *_extra: Any) -> GetBlobRet | ErrorPayload:
        """同步：透明转发 ``client:get_blob`` / Sync relay of ``client:get_blob``.

        Server **不**重组 blob，按 ``computer`` 逐 ack 透传（与 async 一致）.
        Server does NOT reassemble; each chunk is a separate ack (mirrors async).
        """
        return cast(
            "GetBlobRet | ErrorPayload",
            self._relay_client_call(sid, data, _extra, GET_BLOB_EVENT, TypeAdapter(GetBlobReq), TypeAdapter(GetBlobRet)),
        )

    def on_client_put_blob(self, sid: str, data: PutBlobReq | None = None, *_extra: Any) -> PutBlobRet | ErrorPayload:
        """同步：透明转发 ``client:put_blob`` / Sync relay of ``client:put_blob`` (v0.4.0 #196).

        Server **不**缓冲 / 重组，按 ``computer`` 逐 ack 透传（与 async 一致）.
        Server does NOT buffer/reassemble; each chunk is a separate ack (mirrors async).
        """
        return cast(
            "PutBlobRet | ErrorPayload",
            self._relay_client_call(sid, data, _extra, PUT_BLOB_EVENT, TypeAdapter(PutBlobReq), TypeAdapter(PutBlobRet)),
        )

    def on_server_update_desktop(self, sid: str, data: UpdateComputerConfigReq | None = None, *_extra: Any) -> None:
        """
        同步：将事件广播至对应的房间内其他参与者，通知桌面刷新
        Sync: broadcast to others in the room to notify desktop update

        Args:
            sid (str): 发起者ID，应为Computer / Initiator ID, should be Computer
            data (UpdateComputerConfigReq): 载荷复用 UpdateConfigReq，仅需 computer 标识
        """
        self._broadcast_computer_update(sid, data, _extra, UPDATE_DESKTOP_EVENT, UPDATE_DESKTOP_NOTIFICATION)

    def on_server_update_skills(self, sid: str, data: UpdateComputerConfigReq | None = None, *_extra: Any) -> None:
        """同步：``server:update_skills`` → ``notify:update_skills`` 广播.

        Sync mirror of ``on_server_update_skills``; broadcasts SKILL set change to office.
        协议依据 / Protocol: events.md §server:update_skills / §notify:update_skills.
        """
        self._broadcast_computer_update(sid, data, _extra, UPDATE_SKILLS_EVENT, UPDATE_SKILLS_NOTIFICATION)

    def on_server_list_room(
        self, sid: str, data: ListRoomReq | None = None, *_extra: Any
    ) -> ListRoomRet | ErrorPayload:
        """
        同步：列出指定房间内的所有会话信息。Agent可以通过此事件查询房间内的所有Computer和Agent。
        Sync: List all sessions in the specified room. Agent can query all Computers and Agents in the room via this event.

        #214 ack 形态（镜像 async）：**成功**回 ``ListRoomRet``；失败回 flat ``ErrorPayload``——
        ``400``（载荷畸形）/ ``4103``（会话无房）/ ``4104``（显式点名他房）。**不再**静默不 ack。
        Ack shape per #214 — mirrors the async implementation.

        **参数绑定也必须回 ack**：``data`` 具默认值、``*_extra`` 吸收多余位置参数 ⇒ 二者都落到
        schema 校验 ⇒ ``400``。

        Args:
            sid (str): 发起者ID，一般是Agent / Initiator ID, usually Agent
            data (ListRoomReq): 列出房间请求数据，包含office_id和req_id / List room request data with office_id and req_id

        Returns:
            ListRoomRet | ErrorPayload: 房间内所有会话信息列表，或 flat ErrorPayload
                                       / List of all session info in the room, or a flat ErrorPayload
        """
        # 验证请求数据：具备 ack 通道 ⇒ 校验失败 MUST 回 400（error-handling.md:102-106）
        list_room_req = parse_payload("server:list_room", TypeAdapter(ListRoomReq), data, _extra, sid)
        if list_room_req is None:
            return build_bad_request_error()
        office_id = list_room_req["office_id"]
        req_id = list_room_req["req_id"]

        try:
            # 验证发起者权限：确保Agent在请求的房间内 / Verify initiator permission
            agent_session = self.get_session(sid)
            if agent_session is None:
                # **防御性死分支**：实测 socketio 的 ``get_session`` 对未知 sid 抛
                # ``KeyError('Session not found')`` 而**不返回 None**（由下方 catch-all 收编为 500）。
                # Defensive dead branch: the real "originator gone" path raises KeyError.
                raise SMCPNamespaceError(
                    "发起者会话不存在（可能已断连）：server:list_room / originator session gone",
                )
            agent_office_id = agent_session.get("office_id")

            if not agent_office_id:
                # 无房 ⇒ 4103（会话自身无房可报，故无 code-specific details）
                logger.warning(f"server:list_room 无房 sid={sid}, requested={office_id!r}")
                return build_room_rejection_error(ErrorCode.NOT_IN_ROOM)

            if agent_office_id != office_id:
                # 显式点名他房 ⇒ 4104，且**不泄露**目标房的存在性或成员信息（§Cross Room Access 不变量）
                logger.warning(
                    f"server:list_room 跨房越权 sid={sid}: session={agent_office_id!r} requested={office_id!r}",
                )
                return build_room_rejection_error(ErrorCode.CROSS_ROOM_ACCESS, target_office_id=office_id)

            # 使用工具函数获取房间内所有会话信息 / Use utility function to get all session info in the room
            all_sessions = get_all_sessions_in_office(office_id, self.server)

            # 转换为SessionInfo格式 / Convert to SessionInfo format
            sessions: list[SessionInfo] = []
            for session in all_sessions:
                if session.get("role") in ["computer", "agent"]:
                    session_info: SessionInfo = {
                        "sid": session.get("sid", ""),
                        "name": session.get("name", ""),
                        "role": session["role"],
                        "office_id": session.get("office_id", ""),
                    }
                    # a2c_version 为 NotRequired：仅在握手时记录到则带出 / NotRequired: include only if recorded at handshake
                    if session.get("a2c_version"):
                        session_info["a2c_version"] = session["a2c_version"]
                    sessions.append(session_info)

            return ListRoomRet(sessions=sessions, req_id=req_id)
        except Exception as e:
            # 与 join / leave 对称的 catch-all：有 ack 通道 ⇒ 失败必须产出 ack（error-handling.md:102-106）
            logger.error(f"server:list_room 未预期异常 sid={sid}: {e}", exc_info=True)
            return build_internal_error()
