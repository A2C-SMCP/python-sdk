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
from typing import Any, cast

from pydantic import TypeAdapter

from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.server.sync_auth import SyncAuthenticationProvider
from a2c_smcp.server.sync_base import SyncBaseNamespace
from a2c_smcp.server.types import OFFICE_ID, SID
from a2c_smcp.server.utils import get_all_sessions_in_office
from a2c_smcp.smcp import (
    CANCEL_TOOL_CALL_NOTIFICATION,
    ENTER_OFFICE_NOTIFICATION,
    GET_BLOB_EVENT,
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
    UpdateComputerConfigReq,
    UpdateMCPConfigNotification,
    is_protocol_error_payload,
)
from a2c_smcp.utils.logger import get_logger

logger = get_logger("server")


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

    def enter_room(self, sid: SID, room: OFFICE_ID, namespace: str | None = None) -> None:
        """
        客户端加入房间，维护session中的sid/name/office_id字段（同步）
        Client joins room, maintain sid/name/office_id in session (sync)
        """
        session = self.get_session(sid)

        if session.get("sid") != sid:
            session["sid"] = sid
        if not session.get("name"):
            session["name"] = f"{session.get('role', 'unknown')}_{sid[:6]}"

        if session.get("role") == "agent":
            if session.get("office_id") and session.get("office_id") != room:
                logger.error(f"Agent sid: {sid} already in room: {session.get('office_id')}, can't join room: {room}")
                raise ValueError("Agent sid already in room")
            elif not session.get("office_id"):
                for participant_sid, _participant_eio_sid in self.server.manager.get_participants(SMCP_NAMESPACE, room):
                    participant_session = self.get_session(participant_sid)
                    if participant_session.get("role") == "agent":
                        raise ValueError("Agent already in room")
            else:
                logger.warning(
                    f"Agent sid: {sid} already in room: {session.get('office_id')}. 正在重复加入房间",
                )
                return
        else:
            if session.get("office_id") and (past_room := session.get("office_id")) != room:
                self.leave_room(sid, past_room)
            elif session.get("office_id") == room:
                logger.warning(
                    f"Computer sid: {sid} already in room: {session.get('office_id')}. 正在重复加入房间",
                )
                return

            # 检查房间内是否已有同名的Computer
            # Check if there's already a Computer with the same name in the room
            computer_name = session.get("name")
            if computer_name:
                for participant_sid, _participant_eio_sid in self.server.manager.get_participants(SMCP_NAMESPACE, room):
                    if participant_sid == sid:
                        continue
                    participant_session = self.get_session(participant_sid)
                    if participant_session.get("role") == "computer" and participant_session.get("name") == computer_name:
                        raise ValueError(f"Computer with name '{computer_name}' already exists in room '{room}'")

        super().enter_room(sid, room)
        session["office_id"] = room
        self.save_session(sid, session)

        # 注册name到sid的映射
        # Register name-to-sid mapping
        self._register_name(session["name"], sid)

        # 根据角色发送不同的通知 / Send different notifications based on role
        notification_data: EnterOfficeNotification = {"office_id": room}
        if session.get("role") == "computer":
            notification_data["computer"] = session.get("name")
        else:
            notification_data["agent"] = session.get("name")

        self.emit(
            ENTER_OFFICE_NOTIFICATION,
            notification_data,
            skip_sid=sid,
            room=room,
        )

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
        self.emit(LEAVE_OFFICE_NOTIFICATION, notification, skip_sid=sid, room=room)

        # 注销name映射
        # Unregister name mapping
        self._unregister_name(sid)

        if "office_id" in session:
            del session["office_id"]
        self.save_session(sid, session)

        super().leave_room(sid, room)

    def on_server_join_office(self, sid: str, data: EnterOfficeReq) -> tuple[bool, str | None]:
        """
        同步：Computer/Agent加入房间
        Sync: Computer or Agent joins room
        """
        role_info = TypeAdapter(EnterOfficeReq).validate_python(data)
        expected_role = role_info["role"]

        session = self.get_session(sid)
        backup_session = copy.deepcopy(session)

        try:
            if session.get("role") and session["role"] != expected_role:
                return False, f"Role mismatch, expected {expected_role}, but {session['role']} use this sid exists"

            session["role"] = expected_role
            session["name"] = role_info["name"]
            self.save_session(sid, session)

            self.enter_room(sid, role_info["office_id"])
            return True, None
        except Exception as e:
            self.save_session(sid, backup_session)
            return False, f"Internal server error: {str(e)}"

    def on_server_leave_office(self, sid: str, data: LeaveOfficeReq) -> tuple[bool, str | None]:
        """
        同步：Computer/Agent离开房间
        Sync: Computer or Agent leaves room
        """
        try:
            self.leave_room(sid, data["office_id"])
            return True, None
        except Exception as e:
            return False, f"Internal server error: {str(e)}"

    def on_server_tool_call_cancel(self, sid: str, data: AgentCallData) -> None:
        """
        同步：广播取消ToolCall到房间内的其他成员
        Sync: broadcast tool call cancellation to other members in the room
        """
        session = self.get_session(sid)
        if session["role"] != "agent":
            raise SMCPNamespaceError("目前仅支持Agent调用取消ToolCall的操作")

        agent_call = TypeAdapter(AgentCallData).validate_python(data)
        if session.get("name") != agent_call["agent"]:
            raise SMCPNamespaceError("取消工具调用的广播仅可以由对应Agent发出")

        # 广播到 office 房间，而不是 Agent 的私有房间 / Broadcast to office room, not Agent's private room
        office_id = session.get("office_id")
        self.emit(
            CANCEL_TOOL_CALL_NOTIFICATION,
            agent_call,
            room=office_id,
            skip_sid=sid,
        )

    def on_server_update_config(self, sid: str, data: UpdateComputerConfigReq) -> None:
        """
        同步：广播更新MCP配置
        Sync: broadcast MCP config update
        """
        session = self.get_session(sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError("目前仅支持Computer调用更新MCP配置的操作")

        update_config = TypeAdapter(UpdateComputerConfigReq).validate_python(data)
        self.emit(
            UPDATE_CONFIG_NOTIFICATION,
            UpdateMCPConfigNotification(computer=update_config["computer"]),
            room=session["office_id"],
            skip_sid=sid,
        )

    def on_server_update_tool_list(self, sid: str, data: UpdateComputerConfigReq) -> None:
        """
        同步：广播工具列表更新
        Sync: broadcast tool list update
        """
        session = self.get_session(sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError("目前仅支持Computer上报工具列表变更")

        update_req = TypeAdapter(UpdateComputerConfigReq).validate_python(data)

        self.emit(
            UPDATE_TOOL_LIST_NOTIFICATION,
            {"computer": update_req["computer"]},
            room=session.get("office_id"),
            skip_sid=sid,
        )

    def on_client_tool_call(self, sid: str, data: dict) -> dict:
        """
        同步：响应工具调用，使用 call 方法等待 Computer 返回结果
        Sync: respond to tool call, use call method to wait for Computer response
        """
        session = self.get_session(sid)
        if session["role"] != "agent":
            raise SMCPNamespaceError("目前仅支持Agent调用工具")

        tool_call = TypeAdapter(dict).validate_python(data)

        # 通过name获取computer的sid
        # Get computer's sid by name
        computer_name = tool_call["computer"]
        computer_sid = self.get_sid_by_name(computer_name)
        if not computer_sid:
            raise ValueError(f"Computer with name '{computer_name}' not found")

        # 使用 call 方法调用 Computer，等待返回结果 / Use call method to invoke Computer and wait for result
        return cast(
            dict,
            self.call(
                TOOL_CALL_EVENT,
                tool_call,
                to=computer_sid,
                namespace=SMCP_NAMESPACE,
            ),
        )

    def on_client_get_tools(self, sid: str, data: GetToolsReq) -> GetToolsRet | ErrorPayload:
        """
        同步：获取指定 Computer 的工具列表（``client:get_tools``）/ Sync: get tool list of specified Computer.

        经 :meth:`_relay_client_call` 统一 isolation + flat ErrorPayload 透传（v0.2.2：所有 ``client:*``
        ack 统一 flat ErrorPayload，旧路由非豁免）。
        """
        return cast(
            "GetToolsRet | ErrorPayload",
            self._relay_client_call(sid, data, GET_TOOLS_EVENT, TypeAdapter(GetToolsRet)),
        )

    def on_client_get_desktop(self, sid: str, data: GetDeskTopReq) -> GetDeskTopRet | ErrorPayload:
        """
        同步：获取指定 Computer 的桌面视图（``client:get_desktop``）/ Sync: get desktop view from Computer.

        要求 Agent 与 Computer 同一 office；经 :meth:`_relay_client_call` 统一 isolation + flat ErrorPayload 透传。
        """
        return cast(
            "GetDeskTopRet | ErrorPayload",
            self._relay_client_call(sid, data, GET_DESKTOP_EVENT, TypeAdapter(GetDeskTopRet)),
        )

    def _relay_client_call(
        self,
        sid: str,
        data: Any,
        event: str,
        ret_adapter: TypeAdapter[Any],
    ) -> Any:
        """同步版通用 ``client:*`` 事件路由 / Sync mirror of ``_relay_client_call``.

        统一收敛 office/role 隔离校验、Computer SID 解析、flat ErrorPayload 透传。
        Unifies office/role isolation, Computer SID lookup, and flat-ErrorPayload pass-through.

        协议依据 / Protocol: events.md 各 ``client:*`` 事件 + error-handling.md flat ErrorPayload.
        """
        computer_name = data["computer"]
        computer_sid = self.get_sid_by_name(computer_name)
        if not computer_sid:
            raise ValueError(f"Computer with name '{computer_name}' not found")

        session = self.get_session(computer_sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError(f"目前仅支持 Computer 响应 {event} / target SID is not a Computer")

        agent_session = self.get_session(sid)
        if agent_session is None:
            # 发起者（Agent）飞行中断连：会话已不存在。显式 raise 替代 None.get 的 AttributeError
            # （对齐 #31）；协议许可 Server MAY 静默不 ack、不新增错误码（v0.2.2）。
            raise SMCPNamespaceError(f"发起者会话不存在（可能已断连）：{event} / originator session gone")
        if session.get("office_id") != agent_session.get("office_id"):
            raise SMCPNamespaceError(
                f"跨房间访问被拒绝：{event} 仅限同一 office / cross-office {event} access denied",
            )

        client_response = self.call(
            event,
            data,
            to=computer_sid,
            namespace=SMCP_NAMESPACE,
        )
        if is_protocol_error_payload(client_response):
            return TypeAdapter(ErrorPayload).validate_python(client_response)
        return ret_adapter.validate_python(client_response)

    def on_client_get_resources(self, sid: str, data: GetResourcesReq) -> GetResourcesRet | ErrorPayload:
        """
        同步：透明转发 ``client:get_resources`` 至目标 Computer（含 cursor 翻页）。
        Sync: relay ``client:get_resources`` to the target Computer (with cursor pagination).
        """
        return cast(
            "GetResourcesRet | ErrorPayload",
            self._relay_client_call(sid, data, GET_RESOURCES_EVENT, TypeAdapter(GetResourcesRet)),
        )

    def on_client_get_skills(self, sid: str, data: GetSkillsReq) -> GetSkillsRet | ErrorPayload:
        """同步：透明转发 ``client:get_skills`` / Sync relay of ``client:get_skills``."""
        return cast(
            "GetSkillsRet | ErrorPayload",
            self._relay_client_call(sid, data, GET_SKILLS_EVENT, TypeAdapter(GetSkillsRet)),
        )

    def on_client_get_skill(self, sid: str, data: GetSkillReq) -> GetSkillRet | ErrorPayload:
        """同步：透明转发 ``client:get_skill`` / Sync relay of ``client:get_skill``."""
        return cast(
            "GetSkillRet | ErrorPayload",
            self._relay_client_call(sid, data, GET_SKILL_EVENT, TypeAdapter(GetSkillRet)),
        )

    def on_client_get_blob(self, sid: str, data: GetBlobReq) -> GetBlobRet | ErrorPayload:
        """同步：透明转发 ``client:get_blob`` / Sync relay of ``client:get_blob``.

        Server **不**重组 blob，按 ``computer`` 逐 ack 透传（与 async 一致）.
        Server does NOT reassemble; each chunk is a separate ack (mirrors async).
        """
        return cast(
            "GetBlobRet | ErrorPayload",
            self._relay_client_call(sid, data, GET_BLOB_EVENT, TypeAdapter(GetBlobRet)),
        )

    def on_server_update_desktop(self, sid: str, data: UpdateComputerConfigReq) -> None:
        """
        同步：将事件广播至对应的房间内其他参与者，通知桌面刷新
        Sync: broadcast to others in the room to notify desktop update

        Args:
            sid (str): 发起者ID，应为Computer / Initiator ID, should be Computer
            data (UpdateComputerConfigReq): 载荷复用 UpdateConfigReq，仅需 computer 标识
        """
        session = self.get_session(sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError("目前仅支持Computer上报桌面刷新")

        update_req = TypeAdapter(UpdateComputerConfigReq).validate_python(data)
        self.emit(
            UPDATE_DESKTOP_NOTIFICATION,
            {"computer": update_req["computer"]},
            room=session.get("office_id"),
            skip_sid=sid,
        )

    def on_server_update_skills(self, sid: str, data: UpdateComputerConfigReq) -> None:
        """同步：``server:update_skills`` → ``notify:update_skills`` 广播.

        Sync mirror of ``on_server_update_skills``; broadcasts SKILL set change to office.
        协议依据 / Protocol: events.md §server:update_skills / §notify:update_skills.
        """
        session = self.get_session(sid)
        if session["role"] != "computer":
            raise SMCPNamespaceError("目前仅支持 Computer 上报 SKILL 变更 / only Computers may emit update_skills")

        update_req = TypeAdapter(UpdateComputerConfigReq).validate_python(data)
        self.emit(
            UPDATE_SKILLS_NOTIFICATION,
            {"computer": update_req["computer"]},
            room=session.get("office_id"),
            skip_sid=sid,
        )

    def on_server_list_room(self, sid: str, data: ListRoomReq) -> ListRoomRet:
        """
        同步：列出指定房间内的所有会话信息。Agent可以通过此事件查询房间内的所有Computer和Agent。
        Sync: List all sessions in the specified room. Agent can query all Computers and Agents in the room via this event.

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
        agent_session = self.get_session(sid)
        if agent_session is None:
            # 发起者飞行中断连防御（同 _relay_client_call）：显式 raise 替代 None.get 的 AttributeError。
            raise SMCPNamespaceError("发起者会话不存在（可能已断连）：server:list_room / originator session gone")
        agent_office_id = agent_session.get("office_id")

        if agent_office_id != office_id:
            raise SMCPNamespaceError(f"Agent只能查询自己所在房间的会话信息。Agent office: {agent_office_id}, requested: {office_id}")

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
