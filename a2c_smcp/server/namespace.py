"""
* 文件名: namespace
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, loguru, pydantic
* 描述: SMCP协议Namespace实现 / SMCP protocol Namespace implementation
"""

import asyncio
import contextlib
import copy
from typing import Any, cast

from pydantic import TypeAdapter

from a2c_smcp.exceptions import (
    AlreadyInRoomError,
    RoomFullError,
    RoomRejection,
    SMCPNamespaceError,
)
from a2c_smcp.server.auth import AuthenticationProvider
from a2c_smcp.server.base import BaseNamespace
from a2c_smcp.server.types import OFFICE_ID, SID
from a2c_smcp.server.utils import (
    aget_all_sessions_in_office,
    build_room_rejection_ack,
    default_session_name,
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


class SMCPNamespace(BaseNamespace):
    """
    处理SMCP相关事件的Socket.IO命名空间
    Socket.IO namespace for handling SMCP-related events
    """

    def __init__(self, auth_provider: AuthenticationProvider) -> None:
        """
        初始化SMCP命名空间
        Initialize SMCP namespace

        Args:
            auth_provider (AuthenticationProvider): 认证提供者 / Authentication provider
        """
        super().__init__(namespace=SMCP_NAMESPACE, auth_provider=auth_provider)
        # 在途 ``client:*`` 调用的「断连信号」登记表，以**目标 Computer SID** 为 key（#100 Phase 1）。
        # 每个在途 ``_relay_client_call`` 注册一个独立 ``asyncio.Event``；目标 Computer ``on_disconnect`` 时
        # 触发其名下全部信号，使阻塞中的 ``self.call`` 竞速立刻醒来并回 flat ErrorPayload(404)，
        # 而非静默等到满 ``timeout``（socketio ``call`` 的等待原语不监听连接掉线）。
        # Per-target in-flight disconnect signals (#100 Phase 1): target Computer SID -> set of Events.
        self._inflight_disconnect_signals: dict[SID, set[asyncio.Event]] = {}

    async def on_disconnect(self, sid: SID) -> None:
        """断连处理：先走 base 的 name/room 清理，再唤醒以 ``sid`` 为目标的全部在途 ``client:*`` 调用（#100 Phase 1）。
        On disconnect: run base name/room cleanup first, then wake every in-flight call targeting ``sid``.

        **先 super() 再 fire**（关闭 TOCTOU 微窗）：``super().on_disconnect`` 内 ``_unregister_name`` 删除 name 映射后，
        恰在「解析 SID 与登记信号之间」的并发新调用，其 TOCTOU 复查 ``get_sid_by_name`` 会读到 None 直接短路 404，
        不会错过 fire 而退化为满 timeout 等待。信号按 sid 登记、``super()`` 不触碰信号 registry，故 fire 置于其后对
        **已登记**信号的唤醒零损失。幂等（重复 set 是 no-op）。
        Super-then-fire closes the TOCTOU window: after ``_unregister_name`` drops the name mapping, a concurrent
        call registering in the window re-checks ``get_sid_by_name`` → None → short-circuits to 404 instead of
        missing the fire. Already-registered signals still wake (super never touches the signal registry).
        """
        await super().on_disconnect(sid)
        self._fire_inflight_disconnect_signals(sid)

    def _fire_inflight_disconnect_signals(self, sid: SID) -> None:
        """唤醒以 ``sid`` 为目标的全部在途断连信号（幂等）。Wake all in-flight signals targeting ``sid`` (idempotent)."""
        for ev in list(self._inflight_disconnect_signals.get(sid, ())):
            ev.set()

    def _register_inflight_signal(self, computer_sid: SID) -> asyncio.Event:
        """登记并返回一个针对 ``computer_sid`` 的在途断连信号。Register and return a fresh disconnect signal."""
        ev = asyncio.Event()
        self._inflight_disconnect_signals.setdefault(computer_sid, set()).add(ev)
        return ev

    def _discard_inflight_signal(self, computer_sid: SID, ev: asyncio.Event) -> None:
        """注销在途信号；set 空时删除 key（防 dict 泄漏）。Unregister a signal; drop the key when the set empties."""
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

    async def enter_room(self, sid: SID, room: OFFICE_ID, namespace: str | None = None) -> None:
        """
        客户端加入房间，相较于父方法，添加了对sid的合规校验。维护session中的sid和name字段。
        Client joins room, adds sid compliance validation compared to parent method.
        Maintains 'sid' and 'name' fields in session.

        #213 两阶段结构（协议 room-model.md §Computer 加入规则 注记 3「校验必须先于副作用」）：

        - **阶段 1 校验**（零成员关系副作用）：角色约束、目标房同名检查、名字注册闸门；
        - **阶段 2 生效**：退旧房 → 入新房 → 写会话 → 注册 name → 广播 ``notify:enter_office``。

        不变量：本方法抛错后 socketio 的真实成员关系与会话状态**一致**——被拒客户端绝不留在目标房里
        （否则它会持续收到该房的 ``notify:*``，而会话说它不在任何房）。阶段 2 的任何抛错都会按
        **提交点**收敛：旧房离开未提交则一切原样，否则该 sid 被摘成「无房」且会话同步。

        #213 two-phase structure: every fallible gate runs before any membership change; any failure
        afterwards is converged so socketio membership and the session cannot diverge.

        Args:
            sid (SID): 客户端ID / Client ID
            room (OFFICE_ID): 房间ID / Room ID
            namespace (Optional[str]): 命名空间 / Namespace
        """
        session = await self.get_session(sid)

        # 确保session中有sid字段
        # Ensure 'sid' field exists in session
        if session.get("sid") != sid:
            session["sid"] = sid

        # 确保session中有name字段（如无可用默认值）
        # Ensure 'name' field exists in session (use default if missing)
        if not session.get("name"):
            session["name"] = default_session_name(session.get("role"), sid)

        # ── 阶段 1：校验（零成员关系副作用） / Phase 1: validation (no membership side effect) ──
        past_room: OFFICE_ID | None = None
        if session["role"] == "agent":
            # 如果sid已经存在于某个房间中，并且房间号不是当前房间号
            # If sid already exists in a room and the room number is not the current room
            if session.get("office_id") and session.get("office_id") != room:
                logger.warning(
                    f"Agent sid: {sid} already in room: {session.get('office_id')}, can't join room: {room}",
                )
                # 领域异常承载协议码（4106）：handler 据此回 flat ErrorPayload（#214）。
                # 异常消息只含自身上下文（无 sid）⇒ 不可能泄露进 ack 载荷。
                raise AlreadyInRoomError()

            # 如果sid不存在于任何房间中
            # If sid doesn't exist in any room
            elif not session.get("office_id"):
                # 获取房间内所有参与者
                # Get all participants in the room
                participants = self.server.manager.get_participants(SMCP_NAMESPACE, office_room(room))

                # 检查房间内是否已有Agent
                # Check if there's already an Agent in the room
                for participant_sid, _participant_eio_sid in participants:
                    participant_session = await self.get_session(participant_sid)
                    if participant_session.get("role") == "agent":
                        logger.warning(f"Room {room!r} already has an agent; rejecting sid={sid}")
                        raise RoomFullError()
            else:
                logger.warning(f"Agent sid: {sid} already in room: {session.get('office_id')}. 正在重复加入房间")
                return
        else:
            if session.get("office_id") == room:
                logger.warning(f"Computer sid: {sid} already in room: {session.get('office_id')}. 正在重复加入房间")
                return

            # Computer 可以切换房间，但**退房动作推迟到阶段 2**：目标房闸门必须先查完。
            # 若先退旧房再查目标房，一旦目标房同名被拒，该 Computer 已离开原房且对端已收到
            # ``notify:leave_office``，落成「无房」中间态却无任何补救语义（协议 注记 3）。
            # The old room is left in phase 2 — all target-room gates must run first.
            past_room = session.get("office_id") or None

        # 房内同 role 同名闸门（#215：键 ``(office_id, role, name)`` 即协议唯一性作用域，Agent / Computer 同一判据；
        # 跨房同名、同名异 role 放行）。必须早于任何成员关系变更：若留到阶段 2 的 ``_register_name``，socket 已进入
        # 房间而回滚只覆盖会话 ⇒ 被拒客户端留在房里继续收 ``notify:*``（#213）。目标房**显式**传入——换房的
        # Computer 此刻会话里仍是旧房。
        # Per-room, per-role name gate (#215) — must fire before any membership change (#213).
        await self._ensure_name_registerable(room, session["role"], session["name"], sid)

        # ── 阶段 2：生效（成员关系变更） / Phase 2: effects (membership changes) ──
        registered = False
        try:
            if past_room:
                # 旧房离开**不可撤销**（``notify:leave_office`` 已发出）⇒ 必须排在校验之后
                # Leaving the old room is irreversible (the notification is out) — post-validation only.
                await self.leave_room(sid, past_room)

            # 加入新房间：socketio 房名 = ``office:{room}``，与私有 sid 房命名空间不相交（#216）
            # Join new room; the socketio room name is prefixed so it can never be a private sid room (#216)
            await super().enter_room(sid, office_room(room))

            # 保存sid与房间号的映射关系
            # Save mapping between sid and room number
            session["office_id"] = room
            await self.save_session(sid, session)

            # 注册name到sid的映射
            # Register name-to-sid mapping
            await self._register_name(room, session["role"], session["name"], sid)
            registered = True

            # 根据角色发送不同的通知 / Send different notifications based on role
            notification_data: EnterOfficeNotification = {"office_id": room}
            if session.get("role") == "computer":
                notification_data["computer"] = session.get("name")
            else:
                notification_data["agent"] = session.get("name")

            # 广播加入新房间的消息至房间内其它人
            # Broadcast join message to others in the room
            await self._emit_to_office(ENTER_OFFICE_NOTIFICATION, notification_data, room, skip_sid=sid)
        except Exception:
            # 失败收敛 / Failure convergence —— 按**提交点**分刀：
            #   旧房离开若**未提交**（``leave_room`` 推进不到「删除会话 office_id」那一步），则成员关系、
            #   name 映射、会话、对端视图全未变化，收敛反而会把客户端从它合法所属的旧房**无声摘除**
            #   （对端永不获知）⇒ 此时什么都不做，原状即一致。
            #   否则（无旧房可退，或离开已提交）⇒ 把该 sid 从所有非自身房间摘除并清空 office_id，
            #   与会话保持一致（同 ``on_server_leave_office`` 的无房收敛方向，纯 socketio 层、不发广播）。
            # Commit-point discrimination, mirroring the convergence direction used by leave_office.
            if past_room is None or session.get("office_id") != past_room:
                try:
                    # ``_unregister_name`` 按反向索引注销本次注册的键；并发下该键可能已被别的 sid 抢走，
                    # 其内的**归属守卫**保证绝不替它注销。
                    # Unregisters via the reverse index; ``_unregister_name`` is ownership-guarded.
                    if registered:
                        await self._unregister_name(sid)
                    for stale_office in office_ids_of_rooms(list(self.rooms(sid))):
                        # 非成员退房是安全 no-op（manager 吞 KeyError）。此处**静默**摘除、不发
                        # notify:leave_office：目标房从未宣告过本次加入；旧房若已提交离开，其广播也已由
                        # leave_room 发出，再广播只会向对端投递一次重复/误导的离开。
                        # 已知边界：若末步 enter 广播**部分投递**后抛错，已收到的对端会留下幻影成员，需靠
                        # server:list_room 权威视图纠正（协议无撤回原语）。
                        await super().leave_room(sid, office_room(stale_office))
                    if "office_id" in session:
                        del session["office_id"]
                        await self.save_session(sid, session)
                except Exception as conv_err:
                    # 收敛不得掩盖原始异常，故只记日志后重抛原始异常。**已知边界**（best-effort）：收敛自身
                    # 失败（socketio 退房 / save 抛错）时该 sid 可能仍留在目标房且会话仍带 office_id——
                    # 两者一致但未清零。此处刻意**不做二次收敛**（重试同一失败路径无收益、可能成环），
                    # 也不上抛（会把「入房失败」误报为别的故障）。
                    logger.error(f"enter_room 失败收敛未完成 sid={sid} room={room}: {conv_err}", exc_info=True)
            raise

    async def leave_room(self, sid: SID, room: OFFICE_ID, namespace: str | None = None) -> None:
        """
        在离开房间之前发布离开消息
        Publish leave message before leaving room

        Args:
            sid (SID): 客户端ID / Client ID
            room (OFFICE_ID): 房间ID / Room ID
            namespace (Optional[str]): 命名空间 / Namespace
        """
        session = await self.get_session(sid)

        # 构建离开通知，使用name而不是sid
        # Build leave notification using name instead of sid
        client_name = session.get("name", "")
        notification = (
            LeaveOfficeNotification(office_id=room, computer=client_name)
            if session.get("role") == "computer"
            else LeaveOfficeNotification(office_id=room, agent=client_name)
        )

        # 广播离开消息
        # Broadcast leave message
        await self._emit_to_office(LEAVE_OFFICE_NOTIFICATION, notification, room, skip_sid=sid)

        # 注销name映射
        # Unregister name mapping
        await self._unregister_name(sid)

        # 维护session中的office_id字段
        # Maintain office_id field in session
        if "office_id" in session:
            del session["office_id"]
        await self.save_session(sid, session)

        # 调用父类方法离开房间（socketio 房名带 ``office:`` 前缀，#216）
        # Call parent method to leave room (prefixed socketio room name, #216)
        await super().leave_room(sid, office_room(room))

    async def on_server_join_office(
        self, sid: str, data: EnterOfficeReq | None = None, *_extra: Any
    ) -> ErrorPayload | None:
        """
        事件名：server:join_office 由全局变量 JOIN_OFFICE_EVENT 定义
        Computer或者Agent加入房间，为了突显smcp的办公特性，因此加入房间的动作命名为join_office
        Event name: server:join_office defined by global variable JOIN_OFFICE_EVENT
        Computer or Agent joins room, named join_office to highlight SMCP office characteristics

        #214 ack 形态（协议 error-handling.md:177-205；`(bool, str | None)` 元组形态**已废除**）：

        - **成功 ⇒ 空 ack**（本方法返回 ``None``，socketio 发零参 ACK）；
        - **失败 ⇒ flat ``ErrorPayload``**：``400``（载荷畸形）/ ``403``（身份声明冲突——本会话声明的
          ``role`` **或** ``name`` 与既有会话不符；改身份须新连接，见 §Server 处理规则）/
          ``4101``（目标房已有 Agent）/ ``4105``（房内同名）/ ``4106``（Agent 已在其它房）/
          ``500``（未预期内部异常，笼统文案、原文只进日志）。
        - ``details`` 只含**与发起者自身相关**的上下文（目标房 / 自己声明的 role / 自己当前所在房）。

        Ack shape per #214: success is an empty ack (`None`); failures are flat `ErrorPayload`.

        **参数绑定也必须回 ack**（error-handling.md:102-106 明确覆盖「框架层参数提取器」这一时机）：
        客户端不带载荷 emit（``socketio`` 把 ``None`` 折叠成零参包）或带多参 emit 时，绑定会在此之前
        失败 ⇒ 异常逃出 handler ⇒ **根本不发 ACK** ⇒ 调用方挂到自身超时。故 ``data`` 具默认值、
        多余位置参数由 ``*_extra`` 吸收，二者都落到 schema 校验 ⇒ 回 ``400``。
        Binding failures (missing/extra payload args) must also ack — hence the default and ``*_extra``.

        Args:
            sid (str): 客户端ID 可能是 Computer或者Agent / Client ID, could be Computer or Agent
            data (EnterOfficeReq): 加入房间的数据 / Room join data

        Returns:
            Optional[ErrorPayload]: ``None`` = 成功；否则为 flat ErrorPayload
                                   / None on success, otherwise a flat ErrorPayload
        """
        # 协议 error-handling.md:102-106：具备 ack 通道的事件，载荷 schema 校验失败 **MUST** 回
        # flat ErrorPayload(400)，**MUST NOT** 静默不 ack——「挂到客户端自身超时」与「立即收到
        # 结构化错误」是两种客户端可感行为。多余位置参数同属载荷形状非法（见 ``parse_payload``）。
        # A schema failure (incl. extra positional args) MUST still ack (400).
        role_info = parse_payload("server:join_office", TypeAdapter(EnterOfficeReq), data, _extra, sid)
        if role_info is None:
            return build_bad_request_error()
        expected_role = role_info["role"]
        declared_name = role_info["name"]
        office_id = role_info["office_id"]
        if not office_id:
            # ``office_id`` 取值非法（events.md §server:join_office 400）：空串入房后会话 office_id 为假值，
            # 处处被当成「未入房」⇒ 幽灵成员（在 socketio 房里、却收不到本房的任何路由）。
            # An empty office_id is an illegal value (400): it would leave a member the session calls room-less.
            logger.warning(f"server:join_office office_id 为空 sid={sid}")
            return build_bad_request_error()

        session = await self.get_session(sid)
        # 4106 的 details 需报「会话**当前**所在房」⇒ 必须在 enter_room 可能改动/收敛会话**之前**取。
        # 4106's details need the session's *current* room, captured before enter_room can touch it.
        previous_office = session.get("office_id")
        backup_session = copy.deepcopy(session)

        try:
            # 身份声明一致性（协议 events.md:610）：同一 sid 声明了与既有会话不同的 role **或**
            # name ⇒ 403（**非**房间语义）。先于任何会话写入与房间副作用，故无需回滚。
            #
            # #221 对齐：此前只实现了 role 那一半，name 那一半**刻意未实现**——理由是 CLI 的
            # `socket join <office> <name>` 会在**同一条连接**上先改 comp.name 再 join，实现它会把
            # 改名判死。协议侧核对结论是该偏离站不住：events.md:610 是全仓唯一的规范陈述（role/name
            # 共用一条规则、同一个码），error-handling.md:19 把 403 定义为「role / name 与会话不符」，
            # faq.md:162 对 403 给出的补救正是「**重连或换 sid**」——即改身份须新连接，不是在同一
            # sid 上改；rust-sdk（handler.rs:515-523）亦已实现该半。故本仓收敛到协议：改名改走
            # 「新连接」（CLI 见 `interactive_impl.py` 的透明重连路径）。
            #
            # 判据用 `is not None`（与 role 侧对称、语义直白）：本仓会话里存的 name **必然非空**
            # （`enter_room` 会把 falsy name 归一成默认名，见本文件 :180-181；失败回滚则删除该字段），
            # 故与真值性写法在本仓**行为等价**——差异只在「既有 name 为空串」这一不可持久化的态上。
            # 声明空串 name 仍属「另一个身份」⇒ 与既有 name 不同即拒。
            # 会话**未**声明过身份（首次入房，或上次入房失败已被字段级回滚清掉）⇒ 任意 role/name 放行。
            #
            # Bidirectional: a mismatch in *either* declared field ⇒ 403 (identity is immutable for the
            # lifetime of a sid; changing it requires a new connection, per faq.md:162 「重连或换 sid」).
            existing_role = session.get("role")
            existing_name = session.get("name")
            # 声明空名时，`enter_room` 会把它归一成默认名（:180-181，与 `default_session_name` 同源）
            # ⇒ 判据须用**归一后**的期望值比对：否则「声明空名 → 归一成 X → 再次声明空名」会被误判成
            # 身份变更（X ≠ ""），而 X 含 sid 前缀、客户端**无法复现**，只能重连自救。
            # 声明别的名字仍照常相撞 ⇒ 该归一不放宽改身份的约束。
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

            # 设置会话信息
            # Set session information
            session["role"] = expected_role
            session["name"] = role_info["name"]
            await self.save_session(sid, session)

            # 加入房间（成功 ⇒ 空 ack）
            # Join room (success ⇒ empty ack)
            await self.enter_room(sid, office_id)
            return None

        except Exception as e:
            # 恢复本处理器写入的 role / name（**房间归属不回滚**）：``enter_room`` 失败时已按提交点
            # 收敛——它可能已经删掉 office_id（换房中途失败）或保持旧房不变。整体覆盖 backup 会把
            # backup 里的旧房号复活，使会话与真实成员关系再次分叉（本单根因的同构形态）。
            # 缺失字段必须**删除**而非赋 None：后者会让 ``session["role"]`` 变成 None，污染后续 role 判定。
            # Roll back only the fields written here; room ownership belongs to enter_room (#213).
            live_session = await self.get_session(sid)
            for field in ("role", "name"):
                if field in backup_session:
                    live_session[field] = backup_session[field]
                else:
                    live_session.pop(field, None)
            await self.save_session(sid, live_session)

            if isinstance(e, RoomRejection):
                # 业务拒绝：领域异常承载结构化 reason ⇒ 转 flat ErrorPayload（协议码 + 自身相关上下文）。
                # 异常消息本身只含自身上下文（无对端 sid），details 由 builder 的白名单产出（#214）。
                logger.warning(f"server:join_office 被拒 sid={sid} code={e.code}: {e}")
                # 走**不抛**的助手：本分支在 except 子句内，二次抛出会顶替原异常逃出 handler ⇒ 不发 ACK
                return build_room_rejection_ack(
                    e,
                    target_office_id=office_id,
                    declared_role=expected_role,
                    current_office_id=previous_office,
                )

            # 未知内部异常：笼统文案回 ack（不让客户端挂到超时），原文只进日志（协议 §通用错误码 500）。
            # Unexpected internal error: generic text in the ack, the detail stays in the log.
            logger.error(f"server:join_office 未预期异常 sid={sid}: {e}", exc_info=True)
            return build_internal_error()

    async def on_server_leave_office(
        self, sid: str, data: LeaveOfficeReq | None = None, *_extra: Any
    ) -> ErrorPayload | None:
        """
        事件名：server:leave_office 由全局变量 LEAVE_OFFICE_EVENT 定义
        Computer或者Agent离开房间，为了突显smcp的办公特性，因此离开房间的动作命名为leave_office
        Event name: server:leave_office defined by global variable LEAVE_OFFICE_EVENT
        Computer or Agent leaves room, named leave_office to highlight SMCP office characteristics

        房间号**只取服务端权威的会话状态**，绝不用客户端载荷（载荷可携带任意房间号 ⇒ 跨房注入，
        或携带 None ⇒ ``room=None`` 即全命名空间广播）。无房可退时幂等成功，并按 socketio 真实
        成员关系收敛一次。
        The room comes from the authoritative session only, never from the client payload.

        #214 ack 形态：**成功与「无房幂等」都回空 ack**（``None``）；载荷 schema 校验失败回
        ``400``；未预期内部异常回 ``500``。注意「载荷房号与会话不符」**不是**错误（协议：
        MUST NOT 因此拒绝，只按会话执行），故它仍走成功路径。

        **参数绑定也必须回 ack**（error-handling.md:102-106 覆盖「框架层参数提取器」时机）：
        ``data`` 具默认值、多余位置参数由 ``*_extra`` 吸收，二者都落到 schema 校验 ⇒ ``400``；
        否则异常逃出 handler ⇒ 不发 ACK ⇒ 调用方挂到自身超时。
        Failure acks per #214: only `400` (schema) and `500` (internal) — a payload/session room
        mismatch is explicitly NOT a rejection.

        Args:
            sid (str): 客户端ID 可能是 Computer或者Agent / Client ID, could be Computer or Agent
            data (LeaveOfficeReq): 离开房间的数据 / Room leave data

        Returns:
            Optional[ErrorPayload]: ``None`` = 成功（含无房幂等）；否则为 flat ErrorPayload
                                   / None on success (incl. idempotent no-room), else a flat ErrorPayload
        """
        # 同 join：具备 ack 通道 ⇒ 校验失败 MUST 回 400，MUST NOT 静默不 ack（error-handling.md:102-106）。
        leave_info = parse_payload("server:leave_office", TypeAdapter(LeaveOfficeReq), data, _extra, sid)
        if leave_info is None:
            return build_bad_request_error()

        try:
            session = await self.get_session(sid)

            # 房间号**只取服务端权威的会话状态**，绝不用客户端载荷：载荷可携带任意房间号
            # （A 房成员因此能向 B 房注入 notify:leave_office），也可携带 None——后者会得到
            # room=None，而 socketio 的 room=None 是「广播给整个命名空间」⇒ 跨房间泄漏。
            # The room comes from the authoritative session only, never from the client payload.
            office_id = session.get("office_id")
            if not office_id:
                # 无房可退：清理路径幂等成功。但会话可能已与真实成员关系**漂移**（旧版采信载荷
                # ⇒ 清空会话却把人留在原房），故按服务端权威的 socketio 成员关系收敛一次。
                # 房间号取自服务端、非载荷，客户端无法借此向未加入的房间广播；正常未入房的
                # 客户端 rooms(sid) 只含自身房间 ⇒ 与「纯幂等空操作」等价。
                # Idempotent success, but first converge the authoritative socketio membership.
                # 只收敛 **office 房**（``office:`` 前缀，#216）：私有 sid 房与其它房在结构上被排除，
                # 不再依赖「非 sid 房都是 office 房」这一假设。
                # Only office rooms (``office:`` prefix, #216) are converged — structurally, not by assumption.
                for stale_office in office_ids_of_rooms(list(self.rooms(sid))):
                    await self.leave_room(sid, stale_office)
                return None

            claimed = leave_info.get("office_id")
            if claimed != office_id:
                # 载荷与会话不一致：仅告警并按会话执行（协议明令 MUST NOT 因此拒绝，避免与 rust 分叉）
                # Payload disagrees with the session: warn and follow the session.
                logger.warning(
                    f"leave_office: payload claims office {claimed!r} but session {sid} is in {office_id!r}; "
                    f"leaving the session's office",
                )

            await self.leave_room(sid, office_id)
            return None
        except Exception as e:
            # 未知内部异常：笼统文案回 ack（不让客户端挂到超时），原文只进日志（#214 / §通用错误码 500）
            logger.error(f"server:leave_office 未预期异常 sid={sid}: {e}", exc_info=True)
            return build_internal_error()

    async def on_server_tool_call_cancel(self, sid: str, data: AgentCallData | None = None, *_extra: Any) -> None:
        """
        将事件广播至对应的房间内所有Computer，通知取消工具调用
        Broadcast event to all Computers in the corresponding room, notifying tool call cancellation

        fire-and-forget（无 ack 通道）：载荷非法 / 非 Agent / 未入房 ⇒ 告警后**静默丢弃**；载荷 ``agent`` 与会话不符只告警、按会话执行
        （协议 error-handling.md §错误响应格式 (a)，#216）——MUST NOT 为此新增 ack。
        Fire-and-forget: invalid requests are logged and dropped, never acked (#216).

        Args:
            sid (str): 发起者ID，应该是Agent / Initiator ID, should be Agent
            data (AgentCallData): Agent调用数据 / Agent call data
        """
        agent_call = parse_payload(CANCEL_TOOL_CALL_EVENT, TypeAdapter(AgentCallData), data, _extra, sid)
        if agent_call is None:
            return
        origin = resolve_broadcast_origin(CANCEL_TOOL_CALL_EVENT, await self._get_session_or_none(sid), sid, "agent")
        if origin is None:
            return
        office_id, agent_name = origin
        # 出向 ``agent`` 取会话名；载荷自称不符只告警、按会话执行（events.md §房间广播类事件的目标来源：MUST NOT 拒绝）
        # The outbound ``agent`` is the session's name; a mismatching claim is logged, never rejected.
        warn_on_claimed_identity_mismatch(CANCEL_TOOL_CALL_EVENT, sid, "agent", agent_call["agent"], agent_name)
        agent_call = AgentCallData(agent=agent_name, req_id=agent_call["req_id"])

        # 广播到 office 房间，而不是 Agent 的私有房间 / Broadcast to office room, not Agent's private room
        await self._emit_to_office(CANCEL_TOOL_CALL_NOTIFICATION, agent_call, office_id, skip_sid=sid)

    async def _broadcast_computer_update(self, sid: str, data: Any, extra: tuple[Any, ...], event: str, notification: str) -> None:
        """``server:update_*`` ×4 的公共实现：校验 → 判定发起者 → 向其所在房广播 ``{"computer": <发起者会话名>}``。
        Shared body of the four ``server:update_*`` handlers.

        fire-and-forget（无 ack 通道）：载荷非法 / 非 Computer / 未入房 ⇒ 告警后**静默丢弃**（协议
        error-handling.md §错误响应格式 (a)，#216）。四个事件载荷同为 ``UpdateComputerConfigReq``
        （data-structures.md 明示共用），通知体亦同形。
        Fire-and-forget: invalid requests are logged and dropped, never acked (#216).
        """
        update_req = parse_payload(event, TypeAdapter(UpdateComputerConfigReq), data, extra, sid)
        if update_req is None:
            return
        origin = resolve_broadcast_origin(event, await self._get_session_or_none(sid), sid, "computer")
        if origin is None:
            return
        office_id, computer_name = origin
        # 出向 ``computer`` 取会话名：否则可用 ``computer="peer"`` 让接收方去刷新另一个 Computer（events.md
        # §房间广播类事件的目标来源）。载荷自称不符只告警、按会话执行（MUST NOT 拒绝）。
        # The outbound ``computer`` is the session's name, never the payload's claim.
        warn_on_claimed_identity_mismatch(event, sid, "computer", update_req["computer"], computer_name)
        await self._emit_to_office(notification, {"computer": computer_name}, office_id, skip_sid=sid)

    async def on_server_update_config(self, sid: str, data: UpdateComputerConfigReq | None = None, *_extra: Any) -> None:
        """
        将事件广播至对应的房间内所有Computer，通知更新MCP配置
        Broadcast event to all Computers in the corresponding room, notifying MCP config update

        Args:
            sid (str): 发起者ID，应该是Computer / Initiator ID, should be Computer
            data (UpdateComputerConfigReq): 更新配置请求数据 / Update config request data
        """
        await self._broadcast_computer_update(sid, data, _extra, UPDATE_CONFIG_EVENT, UPDATE_CONFIG_NOTIFICATION)

    async def on_server_update_tool_list(self, sid: str, data: UpdateComputerConfigReq | None = None, *_extra: Any) -> None:
        """
        将事件广播至对应的房间内其他参与者，通知工具列表更新。
        Broadcast to others in the room to notify tool list update.

        Args:
            sid (str): 发起者ID，应为Computer / Initiator ID, should be Computer
            data (UpdateComputerConfigReq): 载荷复用 UpdateConfigReq，仅需 computer 标识 / Reuse UpdateConfigReq for payload
        """
        await self._broadcast_computer_update(sid, data, _extra, UPDATE_TOOL_LIST_EVENT, UPDATE_TOOL_LIST_NOTIFICATION)

    async def on_client_tool_call(self, sid: str, data: ToolCallReq | None = None, *_extra: Any) -> dict | ErrorPayload:
        """
        响应工具调用。注意因为Namespace的方法名与事件名称有耦合，因此需要保证 TOOL_CALL_EVENT 是 tool_call
        Respond to tool call. Note that due to coupling between Namespace method names and event names,
        TOOL_CALL_EVENT must be "tool_call"

        如果未来全局变量 TOOL_CALL_EVENT = "tool_call" 有修改，这里的方法名也需要修改
        If the global variable TOOL_CALL_EVENT = "tool_call" is modified in the future,
            the method name here also needs to be modified

        经 :meth:`_relay_client_call` 统一载荷校验 + SID 解析 + office/role 隔离 + flat ErrorPayload 透传（#99：
        目标 Computer 不存在不再 raise ValueError 致 Agent ``call`` 静默超时，与其余 ``client:*`` 事件对齐）。
        「仅 Agent 可发起」由 relay 对**全部** ``client:*`` 统一判定（#216）——不再在此前置读 ``role``：
        未入房会话没有 role，前置读会先 KeyError 而吞掉协议要求的 ``4103``。
        Routed via ``_relay_client_call`` (#99/#216): validation, office/role isolation and flat ErrorPayload
        pass-through all happen there, in the protocol's order.

        Args:
            sid (str): 客户端ID，一般是AgentID / Client ID, usually AgentID
            data (ToolCallReq): 工具调用数据 / Tool call data

        Returns:
            dict | ErrorPayload: 工具调用结果，或目标 Computer 不存在时的 flat ErrorPayload(404)。
                Tool call result, or a flat ErrorPayload(404) when the target Computer is absent.
        """
        # tool_call 响应是 Computer 回传的 CallToolResult 原样 dict（无固定 A2C TypedDict），ret_adapter 用
        # TypeAdapter(dict) 原样透传；per-request timeout（取自校验后的载荷）透传给底层 self.call。
        # tool_call's response is the Computer's raw CallToolResult dict, so use TypeAdapter(dict) passthrough.
        return cast(
            "dict | ErrorPayload",
            await self._relay_client_call(
                sid,
                data,
                _extra,
                TOOL_CALL_EVENT,
                TypeAdapter(ToolCallReq),
                TypeAdapter(dict),
                timeout_from_request=True,
            ),
        )

    async def on_client_get_tools(self, sid: str, data: GetToolsReq | None = None, *_extra: Any) -> GetToolsRet | ErrorPayload:
        """
        获取指定 Computer 的工具列表 / Get tool list of specified Computer（``client:get_tools``）.

        经 :meth:`_relay_client_call` 统一 isolation + flat ErrorPayload 透传（v0.2.2：所有 ``client:*``
        ack 统一 flat ErrorPayload，旧路由非豁免）。
        Routed via ``_relay_client_call`` (unified isolation + flat ErrorPayload pass-through; v0.2.2
        makes flat ErrorPayload uniform across all ``client:*`` acks — old routes are not exempt).

        Args:
            sid (str): 发起者 SID，一般是 Agent / Initiator SID, usually Agent.
            data (GetToolsReq): 含 ``computer`` 字段，指向 Computer 的 Name / contains ``computer``.

        Returns:
            GetToolsRet | ErrorPayload: 工具列表，或 Computer 回传的 flat ErrorPayload。
        """
        return cast(
            "GetToolsRet | ErrorPayload",
            await self._relay_client_call(sid, data, _extra, GET_TOOLS_EVENT, TypeAdapter(GetToolsReq), TypeAdapter(GetToolsRet)),
        )

    async def on_client_get_desktop(self, sid: str, data: GetDeskTopReq | None = None, *_extra: Any) -> GetDeskTopRet | ErrorPayload:
        """
        获取指定 Computer 的桌面视图（窗口组织后）/ Get desktop view from specified Computer（``client:get_desktop``）.

        要求 Agent 与 Computer 同一 office；经 :meth:`_relay_client_call` 统一 isolation + flat
        ErrorPayload 透传。
        Requires same office; routed via ``_relay_client_call`` (unified isolation + flat ErrorPayload).

        Returns:
            GetDeskTopRet | ErrorPayload: 桌面视图，或 Computer 回传的 flat ErrorPayload。
        """
        return cast(
            "GetDeskTopRet | ErrorPayload",
            await self._relay_client_call(sid, data, _extra, GET_DESKTOP_EVENT, TypeAdapter(GetDeskTopReq), TypeAdapter(GetDeskTopRet)),
        )

    async def _relay_client_call(
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
        """通用 ``client:*`` 事件路由 / Generic ``client:*`` event router.

        统一收敛 office/role 隔离校验、Computer SID 解析、flat ErrorPayload 透传，
        让新事件 handler 缩到一行。
        Unifies office/role isolation, Computer SID lookup, and flat-ErrorPayload pass-through,
        so each new event handler is a one-liner.

        协议依据 / Protocol: events.md 各 ``client:*`` 事件 + error-handling.md flat ErrorPayload.

        拒绝顺序（对齐 room-model.md §跨房间访问防护 伪代码；**存活调用方**的每一种拒绝都经 ack 可感，#216）：

        1. 载荷校验失败（含无载荷 / 多余位置参数）⇒ flat ``400``（error-handling.md §错误响应格式）；
        2. 发起者会话已不存在 ⇒ raise（**唯一**的静默路径：协议 §飞行中断连 许可 Server MAY 不 ack）；
        3. 发起者未入房 ⇒ flat ``4103``（error-handling.md §Not In Room）；
        4. 发起者不是 Agent ⇒ flat ``403``（``client:*`` 发起方为 Agent；排在 4103 之后——未入房会话尚无 role）；
        5. ``computer`` 名在**发起者所在房内**解析不到 ⇒ flat ``404``（#92/#215：目标在他房 / 名字属于 Agent 均落此
           分支，与「不存在」对外不可区分）。

        Rejection order (#216): 400 → silent (originator gone) → 4103 → 403 → 404; every rejection of a live
        caller is an ack, never an uncaught exception that times the Agent out.

        Args:
            sid: 发起者 SID（一般是 Agent）/ Initiator SID (usually Agent).
            data: 原始请求负载；校验通过后**原样**转发（Computer 侧自校验，Server 不裁字段）
                / raw payload, relayed verbatim once validated.
            extra: handler 以 ``*_extra`` 吸收的多余位置参数 / extra positional args absorbed by the handler.
            event: 转发的 Socket.IO 事件名（``GET_*_EVENT`` 常量）/ Socket.IO event name to relay.
            req_adapter: 请求载荷的 TypeAdapter（边界校验）/ TypeAdapter of the request payload.
            ret_adapter: 成功响应的 TypeAdapter（如 ``TypeAdapter(GetResourcesRet)``），用于强校验.
                Pydantic TypeAdapter for the success-shape return.
            timeout_from_request: 仅 ``tool_call`` 为真——取载荷 ``timeout`` 透传给底层 ``self.call``；其余
                ``client:*`` 走 socketio 默认。Only ``tool_call`` passes its per-request timeout.

        Returns:
            成功响应（按 ``ret_adapter`` 校验后的 TypedDict）或 flat ``ErrorPayload``。
            Success TypedDict (validated) or flat ErrorPayload.

        Raises:
            SMCPNamespaceError: 发起者会话已不存在 / 注册表与会话不一致（不变量守卫）——见 exceptions.py.
        """
        req = parse_payload(event, req_adapter, data, extra, sid)
        if req is None:
            return build_bad_request_error()

        agent_session = await self.get_session(sid)
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
            # client:* 发起方为 Agent（events.md §Client 事件）；存活调用方的拒绝必须可感 ⇒ flat 403（#216）
            # Only agents may issue client:* calls; a live caller's rejection is an ack (#216)
            logger.warning(f"{event} 非 Agent 发起 sid={sid} role={agent_session.get('role')!r}")
            return build_non_agent_client_call_error()

        # #215：名字解析**限定在发起者所在房内**、按 role=computer 查（MUST NOT 全局裸名解析）。解析不到统一回
        # 404——与「该名字存在于其它房 / 属于同房 Agent」对外不可区分，杜绝探测他房成员存在性。
        # #215: resolve inside the initiator's office only; a miss is a uniform 404 (no cross-room probing).
        computer_name = req["computer"]
        computer_sid = await self.get_sid_by_name(agent_office, "computer", computer_name)
        if not computer_sid:
            return build_computer_not_found_error(computer_name)

        try:
            session = await self.get_session(computer_sid)
        except KeyError:
            # socket 已被移除（断连收尾）；注销先于移除 ⇒ 下方复查必然判为「已离开」
            # Socket already removed; unregistration precedes removal, so the re-check below sees it gone.
            session = None
        # 解析后目标已离房 / 断连（同步服务端多线程分发下可达：解析与读会话之间另一线程跑完 on_disconnect / 换房）
        # ⇒ 与「不存在」同义回 404（同在途断连分支）。以注册表复查判定：注销恒先于清会话 office_id。
        # Target left between resolution and session read (reachable under threaded sync dispatch) → 404.
        if await self.get_sid_by_name(agent_office, "computer", computer_name) != computer_sid:
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

        # 在途断连守卫（#100 Phase 1）：把 ``self.call`` 与「目标 Computer 断连信号」竞速。socketio ``call`` 的
        # 等待原语不监听连接掉线，目标在途断连会静默等到满 timeout；竞速让 ``on_disconnect`` 触发的信号先到时
        # 立刻放弃等待、回 flat ErrorPayload(404)（语义：Computer 中途消失 == 不存在，与 #92/#99 同一 payload）。
        # In-flight disconnect guard (#100): race ``self.call`` against the target's disconnect signal.
        disconnect_ev = self._register_inflight_signal(computer_sid)
        try:
            # TOCTOU 复查：解析 SID 与登记信号之间目标可能已断连——此时信号永不会被 fire。用 ``get_sid_by_name``
            # 复查（``on_disconnect`` → base ``_unregister_name`` 会清掉 name 映射）关闭该窗口。
            # TOCTOU re-check: target may have died between SID resolve and registration → short-circuit to 404.
            if disconnect_ev.is_set() or await self.get_sid_by_name(agent_office, "computer", computer_name) is None:
                return build_computer_not_found_error(computer_name)

            call_task = asyncio.ensure_future(self.call(event, data, **call_kwargs))
            wait_task = asyncio.ensure_future(disconnect_ev.wait())
            try:
                done, _pending = await asyncio.wait({call_task, wait_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if not wait_task.done():
                    wait_task.cancel()
                    # await 被取消的 waiter，避免罕见 GC 时序下的 "Task was destroyed but it is pending" 噪声告警。
                    # Await the cancelled waiter to avoid a "Task was destroyed but it is pending" warning.
                    with contextlib.suppress(asyncio.CancelledError):
                        await wait_task
            if call_task in done:
                # 目标在线：取回结果（真超时则原样抛 socketio TimeoutError，**绝不**转 404，守住语义区分）。
                # Target responded: return its result (a genuine TimeoutError propagates as-is, never 404).
                client_response = call_task.result()
            else:
                # 断连先到：放弃 ``self.call``（ack 回调随 manager.disconnect 清理，目标已断无副作用）。
                # Disconnect won: abandon the call; only cancel here (not on the timeout path).
                call_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await call_task
                return build_computer_not_found_error(computer_name)
        finally:
            self._discard_inflight_signal(computer_sid, disconnect_ev)

        # flat ErrorPayload 透传（无嵌套 envelope，禁止二次 unwrap；判定与 agent 侧统一）/
        # Pass flat ErrorPayload through (no nested envelope; predicate shared with agent side)
        if is_protocol_error_payload(client_response):
            return TypeAdapter(ErrorPayload).validate_python(client_response)
        return ret_adapter.validate_python(client_response)

    async def on_client_get_config(
        self, sid: str, data: GetComputerConfigReq | None = None, *_extra: Any
    ) -> GetComputerConfigRet | ErrorPayload:
        """透明转发 ``client:get_config`` 至目标 Computer，返回其 MCP 配置（占位符原样 + inputs 定义）。
        Relay ``client:get_config`` to the target Computer, returning its MCP config.

        返回协议已定义的 ``GetComputerConfigRet``（``servers`` 配置为占位符原样，解析后密钥不外传——
        见 security.md §零凭证传播）；经 :meth:`_relay_client_call` 统一 isolation + flat ErrorPayload 透传。
        Returns the protocol-defined ``GetComputerConfigRet`` (server configs in placeholder form;
        resolved secrets never leave the Computer); routed via ``_relay_client_call``.

        协议依据 / Protocol: events.md §client:get_config；data-structures.md §GetComputerConfigRet.
        """
        return cast(
            "GetComputerConfigRet | ErrorPayload",
            await self._relay_client_call(
                sid,
                data,
                _extra,
                GET_CONFIG_EVENT,
                TypeAdapter(GetComputerConfigReq),
                TypeAdapter(GetComputerConfigRet),
            ),
        )

    async def on_client_get_resources(self, sid: str, data: GetResourcesReq | None = None, *_extra: Any) -> GetResourcesRet | ErrorPayload:
        """
        透明转发 ``client:get_resources`` 至目标 Computer（含 cursor 翻页）。
        Relay ``client:get_resources`` to the target Computer (with cursor pagination).

        Computer 返回的 flat ErrorPayload（4014 / 4015）原样回传，不做 GetResourcesRet 强转。
        Flat ErrorPayload (4014 / 4015) from the Computer is passed through verbatim.
        """
        return cast(
            "GetResourcesRet | ErrorPayload",
            await self._relay_client_call(
                sid,
                data,
                _extra,
                GET_RESOURCES_EVENT,
                TypeAdapter(GetResourcesReq),
                TypeAdapter(GetResourcesRet),
            ),
        )

    async def on_client_get_skills(self, sid: str, data: GetSkillsReq | None = None, *_extra: Any) -> GetSkillsRet | ErrorPayload:
        """透明转发 ``client:get_skills`` 至目标 Computer / Relay ``client:get_skills``.

        协议依据 / Protocol: events.md §client:get_skills；data-structures.md §GetSkillsRet（轻量元数据）.
        """
        return cast(
            "GetSkillsRet | ErrorPayload",
            await self._relay_client_call(sid, data, _extra, GET_SKILLS_EVENT, TypeAdapter(GetSkillsReq), TypeAdapter(GetSkillsRet)),
        )

    async def on_client_get_skill(self, sid: str, data: GetSkillReq | None = None, *_extra: Any) -> GetSkillRet | ErrorPayload:
        """透明转发 ``client:get_skill`` 至目标 Computer / Relay ``client:get_skill``.

        协议依据 / Protocol: events.md §client:get_skill；error-handling.md §4016 / §4017（``details.reason`` 透传）.
        """
        return cast(
            "GetSkillRet | ErrorPayload",
            await self._relay_client_call(sid, data, _extra, GET_SKILL_EVENT, TypeAdapter(GetSkillReq), TypeAdapter(GetSkillRet)),
        )

    async def on_client_get_blob(self, sid: str, data: GetBlobReq | None = None, *_extra: Any) -> GetBlobRet | ErrorPayload:
        """透明转发 ``client:get_blob`` 至目标 Computer / Relay ``client:get_blob``.

        Server **不**重组 blob，按 ``computer`` 逐 ack 透传；并行红利（协议 §3）由 Agent 侧 drain 例程
        通过对不同 ``chunk_offset`` 并发调用实现。
        Server does NOT reassemble; each ``chunk_offset`` is a separate ack. Parallel dividend
        (protocol §3) is realized by the Agent's drain routine issuing concurrent calls per offset.

        协议依据 / Protocol: events.md §client:get_blob；blob-transfer.md §3 (parallel) / §5.4 (handle untrusted).
        """
        return cast(
            "GetBlobRet | ErrorPayload",
            await self._relay_client_call(sid, data, _extra, GET_BLOB_EVENT, TypeAdapter(GetBlobReq), TypeAdapter(GetBlobRet)),
        )

    async def on_client_put_blob(self, sid: str, data: PutBlobReq | None = None, *_extra: Any) -> PutBlobRet | ErrorPayload:
        """透明转发 ``client:put_blob`` 至目标 Computer / Relay ``client:put_blob`` (v0.4.0 #196).

        与 ``client:get_blob`` 同构：Server **不**缓冲 / 重组，按 ``computer`` 逐 ack 透传；
        in-order 推进（ack-paced）由 Agent 侧 pump 例程保证，Computer 的 4019 flat ErrorPayload
        原样回传。协议依据 / Protocol: events.md §client:put_blob；blob-transfer.md §3（上行）.
        """
        return cast(
            "PutBlobRet | ErrorPayload",
            await self._relay_client_call(sid, data, _extra, PUT_BLOB_EVENT, TypeAdapter(PutBlobReq), TypeAdapter(PutBlobRet)),
        )

    async def on_server_update_desktop(self, sid: str, data: UpdateComputerConfigReq | None = None, *_extra: Any) -> None:
        """
        将事件广播至对应的房间内其他参与者，通知桌面刷新。
        Broadcast to others in the room to notify desktop update.

        Args:
            sid (str): 发起者ID，应为Computer / Initiator ID, should be Computer
            data (UpdateComputerConfigReq): 载荷复用 UpdateConfigReq，仅需 computer 标识
        """
        await self._broadcast_computer_update(sid, data, _extra, UPDATE_DESKTOP_EVENT, UPDATE_DESKTOP_NOTIFICATION)

    async def on_server_update_skills(self, sid: str, data: UpdateComputerConfigReq | None = None, *_extra: Any) -> None:
        """将 ``server:update_skills`` 广播为 ``notify:update_skills`` / Broadcast SKILL set change.

        Computer 在 SKILL 集合变化（增/删/物化更新）时触发；Server 转广播给同 office 内的 Agent，
        Agent 据此自动重拉 ``client:get_skills``（仿既有 ``notify:update_*`` 自动刷新模式）。
        Computer emits on SKILL set change; Server broadcasts to office; Agent auto re-fetches
        ``client:get_skills`` (same pattern as other ``notify:update_*`` events).

        载荷复用 ``UpdateComputerConfigReq``（仅 ``computer`` 字段，data-structures.md §UpdateComputerConfigReq
        三事件族共用）。
        Payload reuses ``UpdateComputerConfigReq`` (data-structures.md shared across 3 event families).

        协议依据 / Protocol: events.md §server:update_skills / §notify:update_skills.
        """
        await self._broadcast_computer_update(sid, data, _extra, UPDATE_SKILLS_EVENT, UPDATE_SKILLS_NOTIFICATION)

    async def on_server_list_room(
        self, sid: str, data: ListRoomReq | None = None, *_extra: Any
    ) -> ListRoomRet | ErrorPayload:
        """
        列出指定房间内的所有会话信息。Agent可以通过此事件查询房间内的所有Computer和Agent。
        List all sessions in the specified room. Agent can query all Computers and Agents in the room via this event.

        #214 ack 形态（协议 events.md §server:list_room ack 语义）：**成功**回 ``ListRoomRet``；
        协议级错误以 flat ``ErrorPayload`` 投递——``400``（载荷畸形）/ ``4103``（会话尚未加入任何
        房间）/ ``4104``（请求的 ``office_id`` ≠ 会话自身所在房）。**不再**静默不 ack（客户端挂到
        自身超时）或返回空 ``sessions``（客户端误判「房间为空」）——那两种既有实现均已废除。

        Ack shape per #214: `ListRoomRet` on success, flat `ErrorPayload` (400 / 4103 / 4104) on failure.

        **参数绑定也必须回 ack**（error-handling.md:102-106 覆盖「框架层参数提取器」时机）：
        ``data`` 具默认值、多余位置参数由 ``*_extra`` 吸收，二者都落到 schema 校验 ⇒ ``400``。

        Args:
            sid (str): 发起者ID，一般是Agent / Initiator ID, usually Agent
            data (ListRoomReq): 列出房间请求数据，包含office_id和req_id / List room request data with office_id and req_id

        Returns:
            ListRoomRet | ErrorPayload: 房间内所有会话信息列表，或 flat ErrorPayload
                                       / List of all session info in the room, or a flat ErrorPayload
        """
        # 验证请求数据：具备 ack 通道 ⇒ 校验失败 MUST 回 400，MUST NOT 静默不 ack（error-handling.md:102-106）
        # Validate request data; a failure MUST still ack (400)
        list_room_req = parse_payload("server:list_room", TypeAdapter(ListRoomReq), data, _extra, sid)
        if list_room_req is None:
            return build_bad_request_error()
        office_id = list_room_req["office_id"]
        req_id = list_room_req["req_id"]

        try:
            # 验证发起者权限：确保Agent在请求的房间内 / Verify initiator permission
            agent_session = await self.get_session(sid)
            if agent_session is None:
                # **防御性死分支**：实测 socketio 的 ``get_session`` 对未知 sid 抛
                # ``KeyError('Session not found')`` 而**不返回 None**，故线上"发起者已断连"走的是
                # KeyError（由下方 catch-all 收编为 500；对已断连的 originator 而言 ack 没有读者，
                # 协议 §飞行中断连 亦许可不回）。保留本分支只为避免 ``None.get`` 的 AttributeError。
                # Defensive dead branch: the real "originator gone" path raises KeyError (socketio).
                raise SMCPNamespaceError(
                    "发起者会话不存在（可能已断连）：server:list_room / originator session gone",
                )
            agent_office_id = agent_session.get("office_id")

            if not agent_office_id:
                # 无房 ⇒ 4103（会话自身无房可报，故无 code-specific details）
                logger.warning(f"server:list_room 无房 sid={sid}, requested={office_id!r}")
                return build_room_rejection_error(ErrorCode.NOT_IN_ROOM)

            if agent_office_id != office_id:
                # 显式点名他房 ⇒ 4104。**不泄露**目标房的存在性或成员信息——「房间存在但无权访问」与
                # 「房间不存在」对外必须不可区分（error-handling.md §Cross Room Access 安全不变量）。
                # 故 details 只回**被拒的目标房号**（发起者自己声明的值），不回该房的任何成员数据。
                logger.warning(
                    f"server:list_room 跨房越权 sid={sid}: session={agent_office_id!r} requested={office_id!r}",
                )
                return build_room_rejection_error(ErrorCode.CROSS_ROOM_ACCESS, target_office_id=office_id)

            # 使用工具函数获取房间内所有会话信息 / Use utility function to get all session info in the room
            all_sessions = await aget_all_sessions_in_office(office_id, self.server)

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
                    # a2c_version 为 NotRequired：仅在握手时记录到则带出 / NotRequired: only if recorded
                    if session.get("a2c_version"):
                        session_info["a2c_version"] = session["a2c_version"]
                    sessions.append(session_info)

            return ListRoomRet(sessions=sessions, req_id=req_id)
        except Exception as e:
            # 与 join / leave 对称的 catch-all：**有 ack 通道 ⇒ 失败必须产出 ack**。成功路径上的
            # 意外异常（会话读取器、SessionInfo 组装）若逃出 handler，socketio 根本不发 ACK，
            # 调用方就挂到自身超时——那正是本单要消灭的形态（error-handling.md:102-106）。
            logger.error(f"server:list_room 未预期异常 sid={sid}: {e}", exc_info=True)
            return build_internal_error()
