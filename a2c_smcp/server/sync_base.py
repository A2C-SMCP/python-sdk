"""
* 文件名: sync_base
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, loguru
* 描述: 同步版本基础Namespace抽象类 / Synchronous Base Namespace abstract class
"""

from typing import Any, cast
from urllib.parse import parse_qs

from socketio import Namespace

from a2c_smcp.exceptions import NameConflictError
from a2c_smcp.server.sync_auth import SyncAuthenticationProvider
from a2c_smcp.server.types import NAME_KEY, OFFICE_ID, SID
from a2c_smcp.server.utils import office_room
from a2c_smcp.utils.logger import ContextLogger, get_logger

logger = get_logger("server")


class SyncBaseNamespace(Namespace):
    """
    同步基础Namespace抽象类，提供通用的连接管理和认证功能
    Synchronous Base Namespace abstract class, provides common connection management and authentication
    """

    def __init__(self, namespace: str, auth_provider: SyncAuthenticationProvider) -> None:
        """
        初始化基础Namespace
        Initialize base namespace
        """
        super().__init__(namespace=namespace)
        self.auth_provider = auth_provider
        # (office_id, role, name) → sid 映射表（#215：键空间即协议的房内唯一性作用域）
        # (office_id, role, name) → sid mapping (#215: the key space is the per-room uniqueness scope)
        self._name_to_sid_map: dict[NAME_KEY, SID] = {}
        # 反向索引 sid → 其持有的唯一键：注销按 sid 删，**不**依赖可变的会话字段反推（回滚会 pop 掉 role/name，
        # 反推失败即留下永久 4105 的残留键）。一个 sid 至多持有一个键。
        # Reverse index sid → its single key: unregister by sid, never by re-deriving from mutable session fields.
        self._sid_to_name_key: dict[SID, NAME_KEY] = {}

    def on_connect(self, sid: SID, environ: dict, auth: dict | None = None) -> bool:
        """
        客户端连接事件处理，包含认证逻辑（同步）
        Client connection event handler with authentication (sync)
        """
        ctx = ContextLogger(logger, {"sid": sid, "ns": self.namespace})
        try:
            ctx.info("Client connecting...")

            # 提取原始请求头
            # Extract raw request headers
            headers = self._extract_headers(environ)

            # 认证逻辑，直接传递原始数据给用户
            # Authentication logic, pass raw data directly to user
            is_authenticated = self.auth_provider.authenticate(self.server, environ, auth, headers)
            if not is_authenticated:
                raise ConnectionRefusedError("Authentication failed")

            # 记录协议版本（兼容性已由 HTTP 握手中间件保证）；仅供 server:list_room 展示与诊断
            # Record protocol version (compatibility already enforced by the HTTP handshake
            # middleware); for server:list_room display & diagnostics only
            a2c_version = self._extract_a2c_version(environ)
            if a2c_version:
                session = self.get_session(sid)
                session["a2c_version"] = a2c_version
                self.save_session(sid, session)

            ctx.info("Client connected successfully")
            return True
        except Exception as e:
            ctx.exception(f"Connection error: {e}")
            raise ConnectionRefusedError("Invalid connection request") from e

    def on_disconnect(self, sid: SID) -> None:
        """
        客户端断开连接事件处理（同步）
        Client disconnect event handler (sync)
        """
        logger.info(f"SocketIO Client {sid} disconnecting from {self.namespace}...")

        # 清理name映射
        # Clean up name mapping
        self._unregister_name(sid)

        # 清理房间连接：离开哪些房、以何种标识交给 ``leave_room``，由 :meth:`_rooms_to_leave_on_disconnect` 决定
        # （基类保持原语义；``SMCPNamespace`` 覆写为只处理 ``office:`` 房并交出原始 office_id，#216）。
        # Which rooms to leave (and in which identifier space) is decided by the overridable hook.
        for room in self._rooms_to_leave_on_disconnect(sid):
            self.leave_room(sid, room)
        logger.info(f"SocketIO Client {sid} disconnected from {self.namespace}")

    def trigger_event(self, event: str, *args: Any) -> Any:
        """
        触发事件，重写触发逻辑，将冒号转换为下划线（同步）
        Trigger event, override logic to replace ':' with '_' (sync)
        """
        return super().trigger_event(event.replace(":", "_"), *args)

    def _ensure_name_registerable(self, office_id: OFFICE_ID, role: str, name: str, sid: SID) -> None:
        """
        名字注册闸门：键 ``(office_id, role, name)`` 被**其它** sid 占用时抛出（本 sid 持有视为可注册，幂等）。

        同步镜像 async ``BaseNamespace._ensure_name_registerable``：键空间即协议的房内唯一性作用域（#215），
        ``office_id`` 须显式给出目标房；``enter_room`` 在任何成员关系变更之前调用（#213），判据单点。
        Sync mirror of the async gate (#215 composite key; #213 validate-before-effects).

        Raises:
            NameConflictError: ``4105``；``ValueError`` 子类，兼容既有 ``except ValueError`` 契约。
        """
        existing_sid = self._name_to_sid_map.get((office_id, role, name))
        if existing_sid is not None and existing_sid != sid:
            # 冗长诊断（含对端 sid）只进日志；异常消息只含自身上下文 ⇒ 泄露构造上不可能（#214）。
            # Verbose diagnostics (peer sid included) go to logs only (#214).
            logger.warning(
                f"名字冲突 / name conflict: office={office_id!r} role={role!r} name={name!r} held by "
                f"sid={existing_sid!r}, requested by sid={sid!r}, namespace={self.namespace}",
            )
            raise NameConflictError()

    def _register_name(self, office_id: OFFICE_ID, role: str, name: str, sid: SID) -> None:
        """
        注册 ``(office_id, role, name)`` → sid 映射，键已被其它 sid 持有则抛出异常（同步）
        Register the ``(office_id, role, name)`` → sid mapping (sync)

        Raises:
            NameConflictError: 当键已被其他sid使用时 / When the key is already held by another sid
        """
        self._ensure_name_registerable(office_id, role, name, sid)
        key = (office_id, role, name)
        if key in self._name_to_sid_map:
            # 如果是同一个sid重新注册，允许（幂等操作）
            # Allow re-registration by the same sid (idempotent operation)
            logger.debug(f"Name {key!r} re-registered by same sid '{sid}'")
        else:
            # 一个 sid 至多持有一个键：先释放它此前持有的（正常路径已由 leave_room 释放，此处兜住收敛失败的残留）
            # One key per sid: release any key it still holds (normally already released by leave_room).
            self._unregister_name(sid)
            self._name_to_sid_map[key] = sid
            self._sid_to_name_key[sid] = key
            logger.debug(f"Registered name {key!r} -> sid '{sid}' in namespace {self.namespace}")

    def _unregister_name(self, sid: SID) -> None:
        """
        注销sid当前持有的名字映射（同步镜像：按反向索引 ``sid → key`` 定位、不从会话字段反推；归属守卫只删本 sid 持有的键）
        Unregister the mapping held by ``sid`` (sync; via the reverse index, ownership-guarded)
        """
        key = self._sid_to_name_key.pop(sid, None)
        if key is not None and self._name_to_sid_map.get(key) == sid:
            del self._name_to_sid_map[key]
            logger.debug(f"Unregistered name {key!r} for sid '{sid}' in namespace {self.namespace}")

    def _rooms_to_leave_on_disconnect(self, sid: SID) -> list[str]:
        """断连时需要 ``leave_room`` 的房间（标识须与本类 ``leave_room`` 的入参语义一致）。
        Rooms to ``leave_room`` on disconnect, in the identifier space this class's ``leave_room`` expects.

        基类：除 socketio 自动建立的私有 sid 房外的全部房间（socketio 房名原样）。``SMCPNamespace`` 覆写为只取
        ``office:`` 前缀房并还原为 office_id（其 ``leave_room`` 以 office_id 为参数，#216）——换算与 ``leave_room``
        同在一个类里，避免基类假定子类的房名约定。
        Base: every room except the private sid room (raw names). ``SMCPNamespace`` narrows it to office rooms.
        """
        return [room for room in self.rooms(sid) if room != sid]

    def _get_session_or_none(self, sid: SID) -> dict[str, Any] | None:
        """读会话；未知 sid（连接已移除）返回 ``None`` 而非抛 ``KeyError``（socketio 对未知 sid 抛 KeyError）。
        Read a session, returning ``None`` instead of raising ``KeyError`` for an unknown sid.

        供 fire-and-forget 事件使用：它们对「发起者已不在」只能静默丢弃（无 ack 通道），让 KeyError 逃出
        handler 只会产生 traceback 噪音（#216）。
        """
        try:
            return cast(dict[str, Any] | None, self.get_session(sid))
        except KeyError:
            return None

    def _emit_to_office(self, event: str, data: Any, office_id: OFFICE_ID, skip_sid: SID | None = None) -> None:
        """向 office 房广播（房名经 :func:`office_room` 换算为 ``office:{office_id}``，#216）。
        Broadcast to an office room; the socketio room name is ``office:{office_id}`` (#216).

        房间广播**只**经此发出：调用方只持有协议层的 ``office_id``，socketio 房名的换算在此单点完成，
        不会有调用点漏加前缀而把广播投进同名的私有 sid 房。
        The only room-broadcast path, so no call site can forget the prefix.
        """
        self.emit(event, data, room=office_room(office_id), skip_sid=skip_sid)

    def get_sid_by_name(self, office_id: OFFICE_ID, role: str, name: str) -> SID | None:
        """
        在指定房内按 role + name 解析 sid（#215：MUST NOT 做全局裸名解析）
        Resolve a sid by role + name inside the given office (never a global bare-name lookup)
        """
        return self._name_to_sid_map.get((office_id, role, name))

    @staticmethod
    def _extract_headers(environ: dict) -> list:
        """
        从请求环境中提取原始请求头列表
        Extract raw request headers list from request environment
        """
        headers: list = environ.get("asgi.scope", {}).get("headers", [])
        if not headers:
            headers = environ.get("HTTP_HEADERS", [])
        return headers

    @staticmethod
    def _extract_a2c_version(environ: dict) -> str | None:
        """
        从请求环境的 query string 中解析 ``a2c_version``（ASGI / WSGI 通用）。
        Parse ``a2c_version`` from the request environ query string (ASGI / WSGI alike).
        """
        qs = environ.get("QUERY_STRING", "")
        if not qs:
            scope_qs = environ.get("asgi.scope", {}).get("query_string", b"")
            qs = scope_qs.decode("latin-1") if isinstance(scope_qs, bytes) else (scope_qs or "")
        values = parse_qs(qs).get("a2c_version")
        return values[0] if values else None
