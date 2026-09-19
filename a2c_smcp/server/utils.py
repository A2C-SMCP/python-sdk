"""
* 文件名: utils
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, pydantic
* 描述: Server端工具函数 / Server-side utility functions
"""

from collections.abc import Mapping
from typing import Any, cast

from pydantic import TypeAdapter
from socketio import AsyncServer, Server

from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.server.types import OFFICE_ID, ComputerSession
from a2c_smcp.smcp import SMCP_NAMESPACE


def require_office_id(session: Mapping[str, Any], sid: str) -> OFFICE_ID:
    """
    取会话的 ``office_id``，未入房则**显式 raise**（「向发起者所在房间广播」的隔离前置）。

    这类 handler 要把通知广播到**发起者所在的房间**。若直接用 ``session.get("office_id")``
    得到 ``None``，socketio 的 ``room=None`` 语义是「广播给整个命名空间」——通知（含发起者
    名称、``req_id`` 等载荷）会跨房间泄漏给所有 office 的成员。故此处必须显式拒绝：隔离
    不变量只允许 raise（与 :class:`~a2c_smcp.exceptions.SMCPNamespaceError` 的 #31 口径一致），
    不允许降级为全命名空间广播。

    Resolve the session's ``office_id``, raising when the session is not in any office. These
    handlers broadcast to the *initiator's* room; ``session.get("office_id")`` returning ``None``
    would make socketio broadcast namespace-wide and leak across offices, so the office-less case
    is rejected explicitly rather than silently degraded.

    Args:
        session (Mapping[str, Any]): 发起者会话 / the initiator's session
        sid (str): 发起者 SID，用于错误信息 / initiator SID, for the error message

    Returns:
        OFFICE_ID: 发起者所在房间 / the initiator's office

    Raises:
        SMCPNamespaceError: 会话未加入任何房间时 / when the session has no office
    """
    office_id = session.get("office_id")
    if not office_id:
        raise SMCPNamespaceError(
            f"会话 {sid!r} 未加入任何房间，拒绝向房间广播：room=None 会降级为全命名空间广播"
            f" / Session {sid!r} is not in any office; refusing the room broadcast "
            f"because room=None would broadcast namespace-wide",
        )
    # session 是 Mapping[str, Any]（运行期会话形状由 on_connect 写入），此处已排除假值
    # session is a Mapping[str, Any] (shape written by on_connect); falsy values are excluded above
    return cast(OFFICE_ID, office_id)


async def aget_computers_in_office(office_id: OFFICE_ID, sio: AsyncServer) -> list[ComputerSession]:
    """
    从sio的 /smcp 命名空间中获取Computer所在房间的计算机列表
    Get list of computers in the room from the /smcp namespace of sio

    Args:
        office_id (OFFICE_ID): 房间号 / Room ID
        sio (AsyncServer): SocketIO实例 / SocketIO instance

    Returns:
        list[ComputerSession]: 计算机列表 / Computer list
    """
    computers = []
    # 这里office_id是房间号，但根据 SMCP 协议的设计，房间号也是AgentID，而Agent在SMCP_NAMESPACE仅可以同时存在于单一房间。
    # 因此可以直接用office_id获取rooms
    # Here office_id is the room number, but according to SMCP protocol design, room number is also AgentID,
    # and Agent can only exist in a single room in SMCP_NAMESPACE. Therefore, office_id can be used directly to get rooms
    # 排除OFFICE_ID实际上就是排除Agent，进而获取到的是Computers
    # Excluding OFFICE_ID actually excludes Agent, thus getting Computers
    for sid, _eio_sid in sio.manager.get_participants(SMCP_NAMESPACE, office_id):
        if sid != office_id:  # 排除Agent自身 / Exclude Agent itself
            try:
                session = await sio.get_session(sid, namespace=SMCP_NAMESPACE)
                if session.get("role") == "computer":
                    computer_session = TypeAdapter(ComputerSession).validate_python(session)
                    computers.append(computer_session)
            except Exception:
                # 忽略无效的会话 / Ignore invalid sessions
                continue

    return computers


def get_computers_in_office(office_id: OFFICE_ID, sio: Server) -> list[ComputerSession]:
    """
    从sio的 /smcp 命名空间中获取Computer所在房间的计算机列表（同步版本）
    Get list of computers in the room from the /smcp namespace of sio (synchronous version)

    Args:
        office_id (OFFICE_ID): 房间号 / Room ID
        sio (Server): SocketIO实例 / SocketIO instance

    Returns:
        list[ComputerSession]: 计算机列表 / Computer list
    """
    computers = []
    # 这里office_id是房间号，但根据 SMCP 协议的设计，房间号也是AgentID，而Agent在SMCP_NAMESPACE仅可以同时存在于单一房间。
    # 因此可以直接用office_id获取rooms
    # Here office_id is the room number, but according to SMCP protocol design, room number is also AgentID,
    # and Agent can only exist in a single room in SMCP_NAMESPACE. Therefore, office_id can be used directly to get rooms
    # 排除OFFICE_ID实际上就是排除Agent，进而获取到的是Computers
    # Excluding OFFICE_ID actually excludes Agent, thus getting Computers
    for sid, _eio_sid in sio.manager.get_participants(SMCP_NAMESPACE, office_id):
        if sid != office_id:  # 排除Agent自身 / Exclude Agent itself
            try:
                session = sio.get_session(sid, namespace=SMCP_NAMESPACE)
                if session.get("role") == "computer":
                    computer_session = TypeAdapter(ComputerSession).validate_python(session)
                    computers.append(computer_session)
            except Exception:
                # 忽略无效的会话 / Ignore invalid sessions
                continue

    return computers


async def aget_all_sessions_in_office(office_id: OFFICE_ID, sio: AsyncServer) -> list[dict]:
    """
    获取房间内所有会话信息
    Get all session information in the room

    Args:
        office_id (OFFICE_ID): 房间号 / Room ID
        sio (AsyncServer): SocketIO实例 / SocketIO instance

    Returns:
        list[dict]: 所有会话列表 / All session list
    """
    sessions = []
    for sid, _eio_sid in sio.manager.get_participants(SMCP_NAMESPACE, office_id):
        try:
            session = await sio.get_session(sid, namespace=SMCP_NAMESPACE)
            if session:
                sessions.append(session)
        except Exception:
            # 忽略无效的会话 / Ignore invalid sessions
            continue

    return sessions


def get_all_sessions_in_office(office_id: OFFICE_ID, sio: Server) -> list[dict]:
    """
    获取房间内所有会话信息（同步版本）
    Get all session information in the room (synchronous version)

    Args:
        office_id (OFFICE_ID): 房间号 / Room ID
        sio (Server): SocketIO实例 / SocketIO instance

    Returns:
        list[dict]: 所有会话列表 / All session list
    """
    sessions = []
    for sid, _eio_sid in sio.manager.get_participants(SMCP_NAMESPACE, office_id):
        try:
            session = sio.get_session(sid, namespace=SMCP_NAMESPACE)
            if session:
                sessions.append(session)
        except Exception:
            # 忽略无效的会话 / Ignore invalid sessions
            continue

    return sessions
