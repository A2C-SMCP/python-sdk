"""
* 文件名: base
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, loguru
* 描述: 基础Namespace抽象类 / Base Namespace abstract class
"""

from typing import Any, cast
from urllib.parse import parse_qs

from socketio import AsyncNamespace

from a2c_smcp.exceptions import NameConflictError
from a2c_smcp.server.auth import AuthenticationProvider
from a2c_smcp.server.types import NAME_KEY, OFFICE_ID, SID
from a2c_smcp.server.utils import office_room
from a2c_smcp.utils.logger import ContextLogger, get_logger

logger = get_logger("server")


class BaseNamespace(AsyncNamespace):
    """
    基础Namespace抽象类，提供通用的连接管理和认证功能
    Base Namespace abstract class, provides common connection management and authentication features
    """

    def __init__(self, namespace: str, auth_provider: AuthenticationProvider) -> None:
        """
        初始化基础Namespace
        Initialize base namespace

        Args:
            namespace (str): 命名空间路径 / Namespace path
            auth_provider (AuthenticationProvider): 认证提供者 / Authentication provider
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

    async def on_connect(self, sid: SID, environ: dict, auth: dict | None = None) -> bool:
        """
        客户端连接事件处理，包含认证逻辑
        Client connection event handler, includes authentication logic

        Args:
            sid (SID): 客户端连接的ID / Client connection ID
            environ (dict): 请求的环境变量 / Request environment variables
            auth (dict | None): 认证信息 / Authentication information

        Returns:
            bool: 是否允许连接 / Whether to allow connection
        """
        ctx = ContextLogger(logger, {"sid": sid, "ns": self.namespace})
        try:
            ctx.info("Client connecting...")

            # 提取原始请求头
            # Extract raw request headers
            headers = self._extract_headers(environ)

            # 认证逻辑，直接传递原始数据给用户
            # Authentication logic, pass raw data directly to user
            is_authenticated = await self.auth_provider.authenticate(self.server, environ, auth, headers)
            if not is_authenticated:
                raise ConnectionRefusedError("Authentication failed")

            # 记录协议版本（兼容性已由 HTTP 握手中间件保证）；仅供 server:list_room 展示与诊断
            # Record protocol version (compatibility already enforced by the HTTP handshake
            # middleware); for server:list_room display & diagnostics only
            a2c_version = self._extract_a2c_version(environ)
            if a2c_version:
                session = await self.get_session(sid)
                session["a2c_version"] = a2c_version
                await self.save_session(sid, session)

            ctx.info("Client connected successfully")
            return True

        except Exception as e:
            ctx.exception(f"Connection error: {e}")
            raise ConnectionRefusedError("Invalid connection request") from e

    async def on_disconnect(self, sid: SID) -> None:
        """
        客户端断开连接事件处理
        Client disconnect event handler

        Args:
            sid (SID): 客户端连接的ID / Client connection ID
        """
        logger.info(f"SocketIO Client {sid} disconnecting from {self.namespace}...")

        # 清理name映射
        # Clean up name mapping
        await self._unregister_name(sid)

        # 清理房间连接：离开哪些房、以何种标识交给 ``leave_room``，由 :meth:`_rooms_to_leave_on_disconnect` 决定
        # （基类保持原语义；``SMCPNamespace`` 覆写为只处理 ``office:`` 房并交出原始 office_id，#216）。
        # Which rooms to leave (and in which identifier space) is decided by the overridable hook.
        for room in self._rooms_to_leave_on_disconnect(sid):
            await self.leave_room(sid, room)

        logger.info(f"SocketIO Client {sid} disconnected from {self.namespace}")

    async def trigger_event(self, event: str, *args: Any) -> Any:
        """
        触发事件，重写触发逻辑，将冒号转换为下划线
        Trigger event, override trigger logic, convert colons to underscores

        Args:
            event (str): 事件名称 / Event name
            *args: 事件参数 / Event arguments

        Returns:
            Any: 事件处理结果 / Event handling result
        """
        return await super().trigger_event(event.replace(":", "_"), *args)

    async def _ensure_name_registerable(self, office_id: OFFICE_ID, role: str, name: str, sid: SID) -> None:
        """
        名字注册闸门：键 ``(office_id, role, name)`` 被**其它** sid 占用时抛出（本 sid 持有视为可注册，幂等）。
        Name-registration gate: raise when ``(office_id, role, name)`` is held by *another* sid.

        #215：注册表键空间即协议的唯一性作用域——**房内同 role 同名唯一**（room-model.md §房内名字唯一性）；
        跨房同名、同名异 role 均允许，SDK **MUST NOT** 施加全局名字空间唯一性。``office_id`` 须由调用方
        **显式**给出目标房：Computer 换房的校验阶段，会话里仍是旧房。
        #215: the key space *is* the protocol's uniqueness scope (per room, per role). The target
        office is passed explicitly — during a Computer's room switch the session still names the old one.

        #213：``enter_room`` 在**任何成员关系变更之前**调用本闸门（协议 注记 3「校验必须先于副作用」）；
        ``_register_name`` 亦经本方法判定，判据**单点**、不会漂移。
        #213: ``enter_room`` runs this gate before any membership change; ``_register_name`` reuses it.

        Args:
            office_id (OFFICE_ID): 目标房 / Target office
            role (str): 客户端角色 / Client role
            name (str): 客户端名称 / Client name
            sid (SID): 客户端连接ID / Client connection ID

        Raises:
            NameConflictError: 键已被其他 sid 使用时（``4105``；``ValueError`` 子类，兼容既有
                ``except ValueError`` 契约）/ When the key is already held by another sid.
        """
        existing_sid = self._name_to_sid_map.get((office_id, role, name))
        if existing_sid is not None and existing_sid != sid:
            # 冗长诊断（**含对端 sid**）只进日志：ack 载荷 MUST NOT 携带其它会话的内部标识
            # （error-handling.md:149）。异常消息本身只含自身上下文 ⇒ 泄露**构造上**不可能，
            # 而非"记得别把 str(e) 塞进 payload"（#214）。
            # Verbose diagnostics (peer sid included) go to logs only; the exception message is
            # self-relative by construction, so it can never leak into an ack payload (#214).
            logger.warning(
                f"名字冲突 / name conflict: office={office_id!r} role={role!r} name={name!r} held by "
                f"sid={existing_sid!r}, requested by sid={sid!r}, namespace={self.namespace}",
            )
            raise NameConflictError()

    async def _register_name(self, office_id: OFFICE_ID, role: str, name: str, sid: SID) -> None:
        """
        注册 ``(office_id, role, name)`` → sid 映射，键已被其它 sid 持有则抛出异常
        Register the ``(office_id, role, name)`` → sid mapping; raise if another sid holds the key

        Args:
            office_id (OFFICE_ID): 房间ID / Office ID
            role (str): 客户端角色 / Client role
            name (str): 客户端名称 / Client name
            sid (SID): 客户端连接ID / Client connection ID

        Raises:
            NameConflictError: 当键已被其他sid使用时 / When the key is already held by another sid
        """
        await self._ensure_name_registerable(office_id, role, name, sid)
        key = (office_id, role, name)
        if key in self._name_to_sid_map:
            # 如果是同一个sid重新注册，允许（幂等操作）
            # Allow re-registration by the same sid (idempotent operation)
            logger.debug(f"Name {key!r} re-registered by same sid '{sid}'")
        else:
            # 一个 sid 至多持有一个键：先释放它此前持有的（正常路径已由 leave_room 释放，此处兜住收敛失败的残留）
            # One key per sid: release any key it still holds (normally already released by leave_room).
            await self._unregister_name(sid)
            self._name_to_sid_map[key] = sid
            self._sid_to_name_key[sid] = key
            logger.debug(f"Registered name {key!r} -> sid '{sid}' in namespace {self.namespace}")

    async def _unregister_name(self, sid: SID) -> None:
        """
        注销sid当前持有的名字映射（按反向索引 ``sid → key`` 定位，**不**从会话字段反推）
        Unregister the name mapping held by ``sid`` (located via the reverse index, not the session)

        会话的 role/name 可能已被 ``on_server_join_office`` 的回滚 pop 掉（首次入房失败且收敛也失败时），
        从会话反推会找不到键 ⇒ 残留键把该房同 role 同名永久拒为 ``4105``。反向索引与注册同源写入，杜绝分叉。
        **归属守卫**：只删除确由本 sid 持有的键——绝不替其它 sid 注销（单点守卫，调用方无需各自再判）。
        Session fields may have been rolled back; the reverse index is written together with the registry,
        so it cannot diverge. Ownership guard: a key now held by another sid is never removed.

        Args:
            sid (SID): 客户端连接ID / Client connection ID
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

    async def _get_session_or_none(self, sid: SID) -> dict[str, Any] | None:
        """读会话；未知 sid（连接已移除）返回 ``None`` 而非抛 ``KeyError``（socketio 对未知 sid 抛 KeyError）。
        Read a session, returning ``None`` instead of raising ``KeyError`` for an unknown sid.

        供 fire-and-forget 事件使用：它们对「发起者已不在」只能静默丢弃（无 ack 通道），让 KeyError 逃出
        handler 只会产生 traceback 噪音（#216）。
        """
        try:
            return cast(dict[str, Any] | None, await self.get_session(sid))
        except KeyError:
            return None

    async def _emit_to_office(self, event: str, data: Any, office_id: OFFICE_ID, skip_sid: SID | None = None) -> None:
        """向 office 房广播（房名经 :func:`office_room` 换算为 ``office:{office_id}``，#216）。
        Broadcast to an office room; the socketio room name is ``office:{office_id}`` (#216).

        房间广播**只**经此发出：调用方只持有协议层的 ``office_id``，socketio 房名的换算在此单点完成，
        不会有调用点漏加前缀而把广播投进同名的私有 sid 房。
        The only room-broadcast path, so no call site can forget the prefix.
        """
        await self.emit(event, data, room=office_room(office_id), skip_sid=skip_sid)

    async def get_sid_by_name(self, office_id: OFFICE_ID, role: str, name: str) -> SID | None:
        """
        在指定房内按 role + name 解析 sid（#215：路由名字解析 MUST 限定在会话所在房内，
        MUST NOT 做全局裸名解析——room-model.md §跨房间访问防护）
        Resolve a sid by role + name *inside* the given office (never a global bare-name lookup)

        Args:
            office_id (OFFICE_ID): 房间ID（调用方所在房）/ Office ID (the caller's office)
            role (str): 目标角色 / Target role
            name (str): 客户端名称 / Client name

        Returns:
            SID | None: 对应的sid，如果不存在则返回None / Corresponding sid, or None if not found
        """
        return self._name_to_sid_map.get((office_id, role, name))

    @staticmethod
    def _extract_headers(environ: dict) -> list:
        """
        从请求环境中提取原始请求头列表
        Extract raw request headers list from request environment

        Args:
            environ (dict): 请求环境变量 / Request environment variables

        Returns:
            list: 原始请求头列表 / Raw request headers list
        """
        # 尝试从不同的环境变量结构中获取headers
        # Try to get headers from different environment variable structures
        headers: list = environ.get("asgi.scope", {}).get("headers", [])
        if not headers:
            headers = environ.get("HTTP_HEADERS", [])

        return headers

    @staticmethod
    def _extract_a2c_version(environ: dict) -> str | None:
        """
        从请求环境的 query string 中解析 ``a2c_version``（ASGI / WSGI 通用）。
        Parse ``a2c_version`` from the request environ query string (ASGI / WSGI alike).

        python-socketio 在 ASGI 与 WSGI 下均填充 ``QUERY_STRING``；附 ``asgi.scope`` 兜底。
        python-socketio populates ``QUERY_STRING`` for both ASGI and WSGI; ``asgi.scope``
        is a defensive fallback.
        """
        qs = environ.get("QUERY_STRING", "")
        if not qs:
            scope_qs = environ.get("asgi.scope", {}).get("query_string", b"")
            qs = scope_qs.decode("latin-1") if isinstance(scope_qs, bytes) else (scope_qs or "")
        values = parse_qs(qs).get("a2c_version")
        return values[0] if values else None
