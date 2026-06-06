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

from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.server.auth import AuthenticationProvider
from a2c_smcp.server.base import BaseNamespace
from a2c_smcp.server.types import OFFICE_ID, SID
from a2c_smcp.server.utils import aget_all_sessions_in_office
from a2c_smcp.smcp import (
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
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
    UPDATE_SKILLS_NOTIFICATION,
    UPDATE_TOOL_LIST_NOTIFICATION,
    AgentCallData,
    EnterOfficeNotification,
    EnterOfficeReq,
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
    SessionInfo,
    ToolCallReq,
    UpdateComputerConfigReq,
    UpdateMCPConfigNotification,
    build_computer_not_found_error,
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

    async def enter_room(self, sid: SID, room: OFFICE_ID, namespace: str | None = None) -> None:
        """
        客户端加入房间，相较于父方法，添加了对sid的合规校验。维护session中的sid和name字段。
        Client joins room, adds sid compliance validation compared to parent method.
        Maintains 'sid' and 'name' fields in session.

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
            session["name"] = f"{session.get('role', 'unknown')}_{sid[:6]}"

        if session["role"] == "agent":
            # 如果sid已经存在于某个房间中，并且房间号不是当前房间号
            # If sid already exists in a room and the room number is not the current room
            if session.get("office_id") and session.get("office_id") != room:
                logger.error(f"Agent sid: {sid} already in room: {session.get('office_id')}, can't join room: {room}")
                raise ValueError("Agent sid already in room")

            # 如果sid不存在于任何房间中
            # If sid doesn't exist in any room
            elif not session.get("office_id"):
                # 获取房间内所有参与者
                # Get all participants in the room
                participants = self.server.manager.get_participants(SMCP_NAMESPACE, room)

                # 检查房间内是否已有Agent
                # Check if there's already an Agent in the room
                for participant_sid, _participant_eio_sid in participants:
                    participant_session = await self.get_session(participant_sid)
                    if participant_session.get("role") == "agent":
                        raise ValueError("Agent already in room")
            else:
                logger.warning(f"Agent sid: {sid} already in room: {session.get('office_id')}. 正在重复加入房间")
                return
        else:
            # Computer可以切换房间，但需要注意向即将离开的房间广播离开消息
            # Computer can switch rooms, but need to broadcast leave message to the room being left
            if session.get("office_id") and (past_room := session.get("office_id")) != room:
                await self.leave_room(sid, past_room)
            elif session.get("office_id") == room:
                logger.warning(f"Computer sid: {sid} already in room: {session.get('office_id')}. 正在重复加入房间")
                return

            # 检查房间内是否已有同名的Computer
            # Check if there's already a Computer with the same name in the room
            computer_name = session.get("name")
            if computer_name:
                participants = self.server.manager.get_participants(SMCP_NAMESPACE, room)
                for participant_sid, _participant_eio_sid in participants:
                    if participant_sid == sid:
                        continue
                    participant_session = await self.get_session(participant_sid)
                    if participant_session.get("role") == "computer" and participant_session.get("name") == computer_name:
                        raise ValueError(f"Computer with name '{computer_name}' already exists in room '{room}'")

        # 加入新房间
        # Join new room
        await super().enter_room(sid, room)

        # 保存sid与房间号的映射关系
        # Save mapping between sid and room number
        session["office_id"] = room
        await self.save_session(sid, session)

        # 注册name到sid的映射
        # Register name-to-sid mapping
        await self._register_name(session["name"], sid)

        # 根据角色发送不同的通知 / Send different notifications based on role
        notification_data: EnterOfficeNotification = {"office_id": room}
        if session.get("role") == "computer":
            notification_data["computer"] = session.get("name")
        else:
            notification_data["agent"] = session.get("name")

        # 广播加入新房间的消息至房间内其它人
        # Broadcast join message to others in the room
        await self.emit(
            ENTER_OFFICE_NOTIFICATION,
            notification_data,
            skip_sid=sid,
            room=room,
        )

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
        await self.emit(LEAVE_OFFICE_NOTIFICATION, notification, skip_sid=sid, room=room)

        # 注销name映射
        # Unregister name mapping
        await self._unregister_name(sid)

        # 维护session中的office_id字段
        # Maintain office_id field in session
        if "office_id" in session:
            del session["office_id"]
        await self.save_session(sid, session)

        # 调用父类方法离开房间
        # Call parent method to leave room
        await super().leave_room(sid, room)

    async def on_server_join_office(self, sid: str, data: EnterOfficeReq) -> tuple[bool, str | None]:
        """
        事件名：server:join_office 由全局变量 JOIN_OFFICE_EVENT 定义
        Computer或者Agent加入房间，为了突显smcp的办公特性，因此加入房间的动作命名为join_office
        Event name: server:join_office defined by global variable JOIN_OFFICE_EVENT
        Computer or Agent joins room, named join_office to highlight SMCP office characteristics

        Args:
            sid (str): 客户端ID 可能是 Computer或者Agent / Client ID, could be Computer or Agent
            data (EnterOfficeReq): 加入房间的数据 / Room join data

        Returns:
            tuple[bool, Optional[str]]: 返回是否允许加入房间，以及可能的错误信息
                                      / Returns whether joining is allowed and possible error message
        """
        role_info = TypeAdapter(EnterOfficeReq).validate_python(data)
        expected_role = role_info["role"]

        session = await self.get_session(sid)
        backup_session = copy.deepcopy(session)

        try:
            # 检查角色是否匹配
            # Check if role matches
            if session.get("role") and session["role"] != expected_role:
                return False, f"Role mismatch, expected {expected_role}, but {session['role']} use this sid exists"

            # 设置会话信息
            # Set session information
            session["role"] = expected_role
            session["name"] = role_info["name"]
            await self.save_session(sid, session)

            # 加入房间
            # Join room
            await self.enter_room(sid, role_info["office_id"])
            return True, None

        except Exception as e:
            # 恢复会话状态
            # Restore session state
            await self.save_session(sid, backup_session)
            return False, f"Internal server error: {str(e)}"

    async def on_server_leave_office(self, sid: str, data: LeaveOfficeReq) -> tuple[bool, str | None]:
        """
        事件名：server:leave_office 由全局变量 LEAVE_OFFICE_EVENT 定义
        Computer或者Agent离开房间，为了突显smcp的办公特性，因此离开房间的动作命名为leave_office
        Event name: server:leave_office defined by global variable LEAVE_OFFICE_EVENT
        Computer or Agent leaves room, named leave_office to highlight SMCP office characteristics

        Args:
            sid (str): 客户端ID 可能是 Computer或者Agent / Client ID, could be Computer or Agent
            data (LeaveOfficeReq): 离开房间的数据 / Room leave data

        Returns:
            tuple[bool, Optional[str]]: 返回是否允许离开房间，以及可能的错误信息
                                      / Returns whether leaving is allowed and possible error message
        """
        try:
            await self.leave_room(sid, data["office_id"])
            return True, None
        except Exception as e:
            return False, f"Internal server error: {str(e)}"

    async def on_server_tool_call_cancel(self, sid: str, data: AgentCallData) -> None:
        """
        将事件广播至对应的房间内所有Computer，通知取消工具调用
        Broadcast event to all Computers in the corresponding room, notifying tool call cancellation

        Args:
            sid (str): 发起者ID，应该是Agent / Initiator ID, should be Agent
            data (AgentCallData): Agent调用数据 / Agent call data
        """
        session = await self.get_session(sid)
        if session["role"] != "agent":
            raise SMCPNamespaceError("目前仅支持Agent调用取消ToolCall的操作")

        agent_call = TypeAdapter(AgentCallData).validate_python(data)
        if session.get("name") != agent_call["agent"]:
            raise SMCPNamespaceError("取消工具调用的广播仅可以由对应Agent发出")

        # 广播到 office 房间，而不是 Agent 的私有房间 / Broadcast to office room, not Agent's private room
        office_id = session.get("office_id")
        await self.emit(
            CANCEL_TOOL_CALL_NOTIFICATION,
            agent_call,
            room=office_id,
            skip_sid=sid,
        )

    async def on_server_update_config(self, sid: str, data: UpdateComputerConfigReq) -> None:
        """
        将事件广播至对应的房间内所有Computer，通知更新MCP配置
        Broadcast event to all Computers in the corresponding room, notifying MCP config update

        Args:
            sid (str): 发起者ID，应该是Computer / Initiator ID, should be Computer
            data (UpdateComputerConfigReq): 更新配置请求数据 / Update config request data
        """
        session = await self.get_session(sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError("目前仅支持Computer调用更新MCP配置的操作")

        update_config = TypeAdapter(UpdateComputerConfigReq).validate_python(data)

        await self.emit(
            UPDATE_CONFIG_NOTIFICATION,
            UpdateMCPConfigNotification(computer=update_config["computer"]),
            room=session["office_id"],
            skip_sid=sid,
        )

    async def on_server_update_tool_list(self, sid: str, data: UpdateComputerConfigReq) -> None:
        """
        将事件广播至对应的房间内其他参与者，通知工具列表更新。
        Broadcast to others in the room to notify tool list update.

        Args:
            sid (str): 发起者ID，应为Computer / Initiator ID, should be Computer
            data (UpdateComputerConfigReq): 载荷复用 UpdateConfigReq，仅需 computer 标识 / Reuse UpdateConfigReq for payload
        """
        session = await self.get_session(sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError("目前仅支持Computer上报工具列表变更")

        update_req = TypeAdapter(UpdateComputerConfigReq).validate_python(data)

        await self.emit(
            UPDATE_TOOL_LIST_NOTIFICATION,
            {"computer": update_req["computer"]},
            room=session.get("office_id"),
            skip_sid=sid,
        )

    async def on_client_tool_call(self, sid: str, data: ToolCallReq) -> dict | ErrorPayload:
        """
        响应工具调用。注意因为Namespace的方法名与事件名称有耦合，因此需要保证 TOOL_CALL_EVENT 是 tool_call
        Respond to tool call. Note that due to coupling between Namespace method names and event names,
        TOOL_CALL_EVENT must be "tool_call"

        如果未来全局变量 TOOL_CALL_EVENT = "tool_call" 有修改，这里的方法名也需要修改
        If the global variable TOOL_CALL_EVENT = "tool_call" is modified in the future,
            the method name here also needs to be modified

        经 :meth:`_relay_client_call` 统一 SID 解析 + office/role 隔离 + flat ErrorPayload(404) 透传（#99：
        目标 Computer 不存在不再 raise ValueError 致 Agent ``call`` 静默超时，与其余 ``client:*`` 事件对齐）。
        仅 Agent 可发起工具调用，故委托前保留角色校验。
        Routed via ``_relay_client_call`` (#99: missing Computer → flat ErrorPayload(404), not an uncaught
        ValueError that times the Agent out). Only an Agent may invoke a tool, hence the role check before relay.

        Args:
            sid (str): 客户端ID，一般是AgentID / Client ID, usually AgentID
            data (ToolCallReq): 工具调用数据 / Tool call data

        Returns:
            dict | ErrorPayload: 工具调用结果，或目标 Computer 不存在时的 flat ErrorPayload(404)。
                Tool call result, or a flat ErrorPayload(404) when the target Computer is absent.
        """
        session = await self.get_session(sid)
        if session["role"] != "agent":
            raise SMCPNamespaceError("目前仅支持Agent调用工具")

        # tool_call 响应是 Computer 回传的 CallToolResult 原样 dict（无固定 A2C TypedDict），ret_adapter 用
        # TypeAdapter(dict) 原样透传；per-request timeout 透传给底层 self.call。
        # tool_call's response is the Computer's raw CallToolResult dict, so use TypeAdapter(dict) passthrough.
        tool_call = TypeAdapter(ToolCallReq).validate_python(data)
        return cast(
            "dict | ErrorPayload",
            await self._relay_client_call(
                sid,
                tool_call,
                TOOL_CALL_EVENT,
                TypeAdapter(dict),
                timeout=tool_call["timeout"],
            ),
        )

    async def on_client_get_tools(self, sid: str, data: GetToolsReq) -> GetToolsRet | ErrorPayload:
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
            await self._relay_client_call(sid, data, GET_TOOLS_EVENT, TypeAdapter(GetToolsRet)),
        )

    async def on_client_get_desktop(self, sid: str, data: GetDeskTopReq) -> GetDeskTopRet | ErrorPayload:
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
            await self._relay_client_call(sid, data, GET_DESKTOP_EVENT, TypeAdapter(GetDeskTopRet)),
        )

    async def _relay_client_call(
        self,
        sid: str,
        data: Any,
        event: str,
        ret_adapter: TypeAdapter[Any],
        timeout: float | None = None,
    ) -> Any:
        """通用 ``client:*`` 事件路由 / Generic ``client:*`` event router.

        统一收敛 office/role 隔离校验、Computer SID 解析、flat ErrorPayload 透传，
        让新事件 handler 缩到一行。
        Unifies office/role isolation, Computer SID lookup, and flat-ErrorPayload pass-through,
        so each new event handler is a one-liner.

        协议依据 / Protocol: events.md 各 ``client:*`` 事件 + error-handling.md flat ErrorPayload.

        Args:
            sid: 发起者 SID（一般是 Agent）/ Initiator SID (usually Agent).
            data: 请求负载，**MUST** 含 ``computer`` 字段 / payload MUST contain ``computer``.
            event: 转发的 Socket.IO 事件名（``GET_*_EVENT`` 常量）/ Socket.IO event name to relay.
            ret_adapter: 成功响应的 TypeAdapter（如 ``TypeAdapter(GetResourcesRet)``），用于强校验.
                Pydantic TypeAdapter for the success-shape return.
            timeout: 仅 ``tool_call`` 透传 per-request 超时给底层 ``self.call``；其余 ``client:*`` 事件留空
                （None）→ 走 socketio 默认。Only ``tool_call`` passes a per-request timeout; others None.

        Returns:
            成功响应（按 ``ret_adapter`` 校验后的 TypedDict）或 flat ``ErrorPayload``。
            Success TypedDict (validated) or flat ErrorPayload.

        Returns 在 ``computer`` 名未找到对应 SID 时回 flat ``ErrorPayload(404)``（#92）——属路由层
        「资源不存在」，按 error-handling.md §20（Computer 不存在）+ §78（client:* ack 协议级错误 MUST
        为 flat ErrorPayload）经 ack 通道返回，**不**抛未捕获异常（避免 Agent ``call`` 静默超时）。
        Returns a flat ``ErrorPayload(404)`` when the ``computer`` name has no SID (#92).

        Raises:
            SMCPNamespaceError: ``role != "computer"`` 或跨房间访问（office_id 不一致）——属 sid/session
                隔离拒绝（安全：跨房间不泄露 Computer 存在性），刻意保留 raise 形态，见 exceptions.py.
        """
        computer_name = data["computer"]
        computer_sid = await self.get_sid_by_name(computer_name)
        if not computer_sid:
            return build_computer_not_found_error(computer_name)

        session = await self.get_session(computer_sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError(f"目前仅支持 Computer 响应 {event} / target SID is not a Computer")

        agent_session = await self.get_session(sid)
        if agent_session is None:
            # 发起者（Agent）飞行中断连：会话已不存在。显式 raise 替代 None.get 的 AttributeError
            # （对齐 #31 隔离不变量显式 raise）；协议许可 Server MAY 静默不 ack、不新增错误码（v0.2.2）。
            # Originator (Agent) disconnected in-flight: session gone. Explicit raise instead of an
            # AttributeError on None.get (aligns with #31). Protocol allows Server MAY silently not-ack.
            raise SMCPNamespaceError(f"发起者会话不存在（可能已断连）：{event} / originator session gone")
        if session.get("office_id") != agent_session.get("office_id"):
            raise SMCPNamespaceError(
                f"跨房间访问被拒绝：{event} 仅限同一 office / cross-office {event} access denied",
            )

        # tool_call 透传 per-request timeout；其余 client:* 事件 timeout=None → 用 socketio 默认
        # （勿对默认事件传 timeout=None，socketio 下会变成永久等待而非默认 60s）。
        # Pass tool_call's per-request timeout; other client:* events leave it None (socketio default).
        call_kwargs: dict[str, Any] = {"to": computer_sid, "namespace": SMCP_NAMESPACE}
        if timeout is not None:
            call_kwargs["timeout"] = timeout

        # 在途断连守卫（#100 Phase 1）：把 ``self.call`` 与「目标 Computer 断连信号」竞速。socketio ``call`` 的
        # 等待原语不监听连接掉线，目标在途断连会静默等到满 timeout；竞速让 ``on_disconnect`` 触发的信号先到时
        # 立刻放弃等待、回 flat ErrorPayload(404)（语义：Computer 中途消失 == 不存在，与 #92/#99 同一 payload）。
        # In-flight disconnect guard (#100): race ``self.call`` against the target's disconnect signal.
        disconnect_ev = self._register_inflight_signal(computer_sid)
        try:
            # TOCTOU 复查：解析 SID 与登记信号之间目标可能已断连——此时信号永不会被 fire。用 ``get_sid_by_name``
            # 复查（``on_disconnect`` → base ``_unregister_name`` 会清掉 name 映射）关闭该窗口。
            # TOCTOU re-check: target may have died between SID resolve and registration → short-circuit to 404.
            if disconnect_ev.is_set() or await self.get_sid_by_name(computer_name) is None:
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

    async def on_client_get_config(self, sid: str, data: GetComputerConfigReq) -> GetComputerConfigRet | ErrorPayload:
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
            await self._relay_client_call(sid, data, GET_CONFIG_EVENT, TypeAdapter(GetComputerConfigRet)),
        )

    async def on_client_get_resources(self, sid: str, data: GetResourcesReq) -> GetResourcesRet | ErrorPayload:
        """
        透明转发 ``client:get_resources`` 至目标 Computer（含 cursor 翻页）。
        Relay ``client:get_resources`` to the target Computer (with cursor pagination).

        Computer 返回的 flat ErrorPayload（4014 / 4015）原样回传，不做 GetResourcesRet 强转。
        Flat ErrorPayload (4014 / 4015) from the Computer is passed through verbatim.
        """
        return cast(
            "GetResourcesRet | ErrorPayload",
            await self._relay_client_call(sid, data, GET_RESOURCES_EVENT, TypeAdapter(GetResourcesRet)),
        )

    async def on_client_get_skills(self, sid: str, data: GetSkillsReq) -> GetSkillsRet | ErrorPayload:
        """透明转发 ``client:get_skills`` 至目标 Computer / Relay ``client:get_skills``.

        协议依据 / Protocol: events.md §client:get_skills；data-structures.md §GetSkillsRet（轻量元数据）.
        """
        return cast(
            "GetSkillsRet | ErrorPayload",
            await self._relay_client_call(sid, data, GET_SKILLS_EVENT, TypeAdapter(GetSkillsRet)),
        )

    async def on_client_get_skill(self, sid: str, data: GetSkillReq) -> GetSkillRet | ErrorPayload:
        """透明转发 ``client:get_skill`` 至目标 Computer / Relay ``client:get_skill``.

        协议依据 / Protocol: events.md §client:get_skill；error-handling.md §4016 / §4017（``details.reason`` 透传）.
        """
        return cast(
            "GetSkillRet | ErrorPayload",
            await self._relay_client_call(sid, data, GET_SKILL_EVENT, TypeAdapter(GetSkillRet)),
        )

    async def on_client_get_blob(self, sid: str, data: GetBlobReq) -> GetBlobRet | ErrorPayload:
        """透明转发 ``client:get_blob`` 至目标 Computer / Relay ``client:get_blob``.

        Server **不**重组 blob，按 ``computer`` 逐 ack 透传；并行红利（协议 §3）由 Agent 侧 drain 例程
        通过对不同 ``chunk_offset`` 并发调用实现。
        Server does NOT reassemble; each ``chunk_offset`` is a separate ack. Parallel dividend
        (protocol §3) is realized by the Agent's drain routine issuing concurrent calls per offset.

        协议依据 / Protocol: events.md §client:get_blob；blob-transfer.md §3 (parallel) / §5.4 (handle untrusted).
        """
        return cast(
            "GetBlobRet | ErrorPayload",
            await self._relay_client_call(sid, data, GET_BLOB_EVENT, TypeAdapter(GetBlobRet)),
        )

    async def on_server_update_desktop(self, sid: str, data: UpdateComputerConfigReq) -> None:
        """
        将事件广播至对应的房间内其他参与者，通知桌面刷新。
        Broadcast to others in the room to notify desktop update.

        Args:
            sid (str): 发起者ID，应为Computer / Initiator ID, should be Computer
            data (UpdateComputerConfigReq): 载荷复用 UpdateConfigReq，仅需 computer 标识
        """
        session = await self.get_session(sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError("目前仅支持Computer上报桌面刷新")

        update_req = TypeAdapter(UpdateComputerConfigReq).validate_python(data)
        await self.emit(
            UPDATE_DESKTOP_NOTIFICATION,
            {"computer": update_req["computer"]},
            room=session.get("office_id"),
            skip_sid=sid,
        )

    async def on_server_update_skills(self, sid: str, data: UpdateComputerConfigReq) -> None:
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
        session = await self.get_session(sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError("目前仅支持 Computer 上报 SKILL 变更 / only Computers may emit update_skills")

        update_req = TypeAdapter(UpdateComputerConfigReq).validate_python(data)
        await self.emit(
            UPDATE_SKILLS_NOTIFICATION,
            {"computer": update_req["computer"]},
            room=session.get("office_id"),
            skip_sid=sid,
        )

    async def on_server_list_room(self, sid: str, data: ListRoomReq) -> ListRoomRet:
        """
        列出指定房间内的所有会话信息。Agent可以通过此事件查询房间内的所有Computer和Agent。
        List all sessions in the specified room. Agent can query all Computers and Agents in the room via this event.

        Args:
            sid (str): 发起者ID，一般是Agent / Initiator ID, usually Agent
            data (ListRoomReq): 列出房间请求数据，包含office_id和req_id / List room request data with office_id and req_id

        Returns:
            ListRoomRet: 房间内所有会话信息列表 / List of all session info in the room
        """
        # 验证请求数据 / Validate request data
        list_room_req = TypeAdapter(ListRoomReq).validate_python(data)
        office_id = list_room_req["office_id"]
        req_id = list_room_req["req_id"]

        # 验证发起者权限：确保Agent在请求的房间内 / Verify initiator permission: ensure Agent is in the requested room
        agent_session = await self.get_session(sid)
        if agent_session is None:
            # 发起者飞行中断连防御（同 _relay_client_call）：显式 raise 替代 None.get 的 AttributeError。
            # In-flight disconnect guard (same as _relay_client_call): explicit raise over AttributeError.
            raise SMCPNamespaceError("发起者会话不存在（可能已断连）：server:list_room / originator session gone")
        agent_office_id = agent_session.get("office_id")

        if agent_office_id != office_id:
            raise SMCPNamespaceError(f"Agent只能查询自己所在房间的会话信息。Agent office: {agent_office_id}, requested: {office_id}")

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
                # a2c_version 为 NotRequired：仅在握手时记录到则带出 / NotRequired: include only if recorded at handshake
                if session.get("a2c_version"):
                    session_info["a2c_version"] = session["a2c_version"]
                sessions.append(session_info)

        return ListRoomRet(sessions=sessions, req_id=req_id)
