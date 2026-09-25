"""
* 文件名: utils
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, pydantic
* 描述: Server端工具函数 / Server-side utility functions
"""

from collections.abc import Iterable, Mapping
from typing import Any, TypeVar, cast

from pydantic import TypeAdapter, ValidationError
from socketio import AsyncServer, Server

from a2c_smcp.exceptions import RoomRejection, SMCPNamespaceError
from a2c_smcp.server.types import OFFICE_ID, ComputerSession
from a2c_smcp.smcp import SMCP_NAMESPACE, ErrorPayload, build_internal_error, build_room_rejection_error
from a2c_smcp.utils.logger import get_logger

logger = get_logger("server")

_T = TypeVar("_T")

# socketio 房名中 office 房的命名空间前缀（#216 / 协议 room-model.md §房间标识）。
# socketio 为每个连接自动建一个以 sid 命名的私有房；若房名直接取客户端自选的 ``office_id``，取
# ``office_id = <对端 SID>`` 即可进入对端私有房并向其投递广播。加前缀让两个命名空间**无条件不相交**
# （零运行期成本，与 rust-sdk 同一前缀）。前缀只存在于 socketio 层：会话、ack、通知载荷里的
# ``office_id`` 一律是原值。
# Room-name prefix for office rooms, keeping them disjoint from socketio's per-sid private rooms.
OFFICE_ROOM_PREFIX = "office:"


def office_room(office_id: OFFICE_ID) -> str:
    """``office_id`` → socketio 房名（``office:{office_id}``）/ socketio room name of an office."""
    return f"{OFFICE_ROOM_PREFIX}{office_id}"


def office_id_of_room(room: str) -> OFFICE_ID | None:
    """socketio 房名 → ``office_id``；非 office 房（私有 sid 房 / 其它房）返回 ``None``。
    Inverse of :func:`office_room`; ``None`` for rooms that are not office rooms.
    """
    if room.startswith(OFFICE_ROOM_PREFIX):
        return room[len(OFFICE_ROOM_PREFIX) :]
    return None


def office_ids_of_rooms(rooms: Iterable[str]) -> list[OFFICE_ID]:
    """从 ``rooms(sid)`` 中挑出 office 房并还原 ``office_id``（私有 sid 房等一律跳过）。
    Office ids among a socket's rooms; the private sid room and foreign rooms are skipped.
    """
    return [office_id for office_id in map(office_id_of_room, rooms) if office_id is not None]


def parse_payload(event: str, adapter: TypeAdapter[_T], data: Any, extra: tuple[Any, ...], sid: str) -> _T | None:
    """``server:*`` / ``client:*`` 载荷的边界校验单点（#216 / 协议 security.md §数据验证要求）。
    Single boundary validator for every inbound payload.

    协议给出的 TypedDict **不构成校验**（``{"office_id": None}`` 会原样穿过），故每个事件入口都经此显式
    校验。非法 ⇒ 记告警并返回 ``None``，由调用方按事件的承载形态处置：具备 ack 通道的事件回
    ``build_bad_request_error()``（``400``），fire-and-forget 事件静默丢弃（协议 error-handling.md
    §错误响应格式：MUST NOT 为此新增 ack）。

    非法 = 任一：多余位置参数（协议载荷是**单个** dict；刻意不静默忽略）/ schema 校验失败（含无载荷
    ``None``、非 dict、缺字段、类型错）。日志里的校验器文案可能内嵌输入值——只进日志，从不回显进 ack。

    Returns ``None`` when the payload is invalid (extra positional args or a schema failure); the
    caller answers ``400`` on ack-bearing events and drops fire-and-forget ones.

    Args:
        event: 事件名（仅用于日志）/ event name, for the log line
        adapter: 载荷的 TypeAdapter / payload TypeAdapter
        data: 原始载荷 / raw payload
        extra: handler 以 ``*_extra`` 吸收的多余位置参数 / extra positional args
        sid: 发起者 SID（仅用于日志）/ initiator SID, for the log line
    """
    if extra:
        logger.warning(f"{event} 多余位置参数 sid={sid}: {len(extra)} 个 / extra positional args")
        return None
    try:
        return adapter.validate_python(data)
    except ValidationError as exc:
        logger.warning(f"{event} 载荷校验失败 sid={sid} / payload validation failed: {exc}")
        return None


def default_session_name(role: str | None, sid: str) -> str:
    """客户端**未**（或空串）声明 ``name`` 时，会话采用的归一默认名。

    单一权威：``enter_room`` 写入它，``on_server_join_office`` 的身份一致性判据（协议 events.md:610）
    也用它预测「本次声明落定后会话里的 name 是什么」。两侧若各写一份表达式，一旦漂移就会出现
    「声明空名 → 服务端归一成 X → 再次声明空名被判成身份冲突（X ≠ ""）」，而客户端**无法复现**服务端
    生成的名字（``sid`` 前缀），只能重连自救。

    Single source of truth for the normalized default name: used both when ``enter_room`` stores it and
    when the identity-consistency check predicts what the session's name will become. Divergence would
    make a repeat empty-name join look like an identity change the client cannot recover from.
    """
    return f"{role or 'unknown'}_{sid[:6]}"


def build_room_rejection_ack(
    rejection: RoomRejection,
    *,
    target_office_id: str | None,
    declared_role: str | None,
    current_office_id: str | None,
) -> ErrorPayload:
    """领域拒绝 → flat ``ErrorPayload``；**保证不抛**（#214）。

    存在的理由 / Why this exists：``on_server_join_office`` 在 ``except`` 子句里调用本函数——若它
    自身抛错，新异常会**顶替**原异常逃出 handler，socketio 随即**不发 ACK**，调用方挂到自身超时。
    那正是本单要消灭的形态。:func:`build_room_rejection_error` 对**未映射的码**抛 ``ValueError``
    （例如下游直接实例化基类 :class:`RoomRejection`，其 ``code = 0``，或自定义子类带了自己发明的码），
    故此处兜底成 ``500``：宁可给一个笼统但**有回应**的 ack，也不能让调用方挂起。

    Never raises: the join handlers call this from inside an ``except`` clause, where a secondary
    exception would replace the original one, escape the handler and leave the caller unacked.

    协议依据 / Protocol: error-handling.md:102-106（具备 ack 通道的事件，失败 **MUST** 产出 ack）。

    Args:
        rejection: 抛出的领域拒绝 / the raised domain rejection
        target_office_id: 被拒的目标房（发起者自己声明）/ rejected target office (self-declared)
        declared_role: 发起者自己声明的 role / the role the initiator declared
        current_office_id: 会话当前所在房 / the session's current office

    Returns:
        flat ``ErrorPayload``（永不抛）/ a flat ``ErrorPayload`` (never raises)
    """
    try:
        return build_room_rejection_error(
            rejection.code,
            target_office_id=target_office_id,
            declared_role=declared_role,
            current_office_id=current_office_id,
        )
    except (TypeError, ValueError):
        # 未映射 / 形态非法的码 ⇒ 降级为 500（仍产出 ack）。原文进日志，便于定位是哪条子类没接好。
        # 捕获 **TypeError 同样必要**：``build_room_rejection_error`` 入口的 ``int(code)`` 对非 int
        # （如下游把 ``code`` 写成 ``None`` / 字符串）抛的是 TypeError——漏掉它，这条"二次抛出"就会
        # 与 ValueError 一样逃出 handler ⇒ 不发 ACK（本单的核心不变量）。两种异常都收，才是"保证不抛"。
        # TypeError matters too: the builder's `int(code)` raises it for non-int codes.
        # Unmapped/invalid code ⇒ degrade to 500 (still acked); the detail stays in the log.
        logger.exception(
            f"房间拒绝码未接入码表或形态非法，降级为 500 / unmapped or malformed room-rejection code, "
            f"degraded to 500: {rejection.code!r} ({type(rejection).__name__})",
        )
        return build_internal_error()


def resolve_broadcast_origin(
    event: str, session: Mapping[str, Any] | None, sid: str, required_role: str
) -> tuple[OFFICE_ID, str] | None:
    """fire-and-forget 广播类事件的发起者判定：返回 ``(广播目标 office_id, 发起者会话名)``，不满足则 ``None``（丢弃）。
    Decide whether a fire-and-forget broadcast may proceed; returns ``(office_id, session_name)`` or ``None``.

    协议依据 / Protocol:

    - error-handling.md §错误响应格式 (a)：``server:tool_call_cancel`` 与 ``server:update_*`` 无 ack 通道，非法请求
      **静默丢弃即合规**，MUST NOT 为此新增 ack（#216）。故「会话不存在 / 角色不符 / 未入房」一律告警后丢弃，
      **不**抛异常：抛出不产生任何对端可感响应，只会让任一客户端能往服务端日志灌 traceback。
    - events.md §房间广播类事件的目标来源：广播**目标**与出向 ``notify:*`` 的**身份字段**都 MUST 取自发起者
      **会话**（绝不取载荷）⇒ 本函数把两者一并从会话给出；未入房时绝不以 ``room=None`` 降级为全命名空间广播。

    Returns ``None`` (logged) when the session is gone, has the wrong role, or is not in any office —
    never a namespace-wide ``room=None``. Both the target and the outbound identity come from the session.
    """
    if not session:
        logger.warning(f"{event} 发起者会话不存在，丢弃 sid={sid} / originator session gone, dropped")
        return None
    if session.get("role") != required_role:
        logger.warning(
            f"{event} 仅 {required_role} 可发起，丢弃 sid={sid} role={session.get('role')!r} "
            f"/ only {required_role} may emit this, dropped",
        )
        return None
    office_id = session.get("office_id")
    if not office_id:
        logger.warning(f"{event} 发起者未加入任何房间，丢弃 sid={sid} / initiator not in any office, dropped")
        return None
    return cast(OFFICE_ID, office_id), cast(str, session.get("name", ""))


def warn_on_claimed_identity_mismatch(event: str, sid: str, field: str, claimed: str, actual: str) -> None:
    """载荷自称的身份与会话不符 ⇒ 只告警（events.md：SHOULD 告警后按会话执行，**MUST NOT** 拒绝）。
    A claimed identity that disagrees with the session is logged only — never a rejection.
    """
    if claimed != actual:
        logger.warning(
            f"{event} 载荷 {field}={claimed!r} 与会话 {actual!r} 不符，按会话执行 sid={sid} "
            f"/ payload {field} disagrees with the session; following the session",
        )


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
    for sid, _eio_sid in sio.manager.get_participants(SMCP_NAMESPACE, office_room(office_id)):
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
    for sid, _eio_sid in sio.manager.get_participants(SMCP_NAMESPACE, office_room(office_id)):
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
    for sid, _eio_sid in sio.manager.get_participants(SMCP_NAMESPACE, office_room(office_id)):
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
    for sid, _eio_sid in sio.manager.get_participants(SMCP_NAMESPACE, office_room(office_id)):
        try:
            session = sio.get_session(sid, namespace=SMCP_NAMESPACE)
            if session:
                sessions.append(session)
        except Exception:
            # 忽略无效的会话 / Ignore invalid sessions
            continue

    return sessions
