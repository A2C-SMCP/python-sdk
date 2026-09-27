"""
* 文件名: sync_base
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, loguru
* 描述: 同步版本基础Namespace抽象类 / Synchronous Base Namespace abstract class
"""

import threading
from typing import Any, cast
from urllib.parse import parse_qs

from socketio import Namespace

from a2c_smcp.exceptions import NameConflictError
from a2c_smcp.server.name_registry import release_name, reserve_name
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
        # 注册表锁（v0.5.0 审查 🔴7/S1）：同步服务端每个事件一个线程（``async_handlers=True``），断连收尾跑在
        # engine.io 线程上——「校验 + 写入」与「注销」必须互斥，否则 ① 断连清理先跑完、join 后写入 ⇒ 注册表
        # 残留指向死 sid 的键（同名重连永久 4105）；② 两个同名 join 都通过校验 ⇒ 房内双同名。可重入：注销
        # 可能在持锁路径内被再次调用。锁内**只**做纯字典操作（不 emit、不读写会话），无锁序问题。
        # Registry lock: check-and-set and unregister must be mutually exclusive across handler threads.
        self._registry_lock = threading.RLock()

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

        # 清理房间连接：离开哪些房、以何种标识交给 ``leave_room``，由 :meth:`_rooms_to_leave_on_disconnect` 决定
        # （基类保持原语义；``SMCPNamespace`` 覆写为只处理 ``office:`` 房并交出原始 office_id，#216）。
        # Which rooms to leave (and in which identifier space) is decided by the overridable hook.
        try:
            for room in self._rooms_to_leave_on_disconnect(sid):
                self.leave_room(sid, room)
        finally:
            # 残留注销**排在退房广播之后**（S3）：先注销会让同名新连接在旧 leave 广播发出前完成入房并广播 enter，
            # 对端收到「enter(X) → leave(X)」而丢掉新的 X。常态下键已由 leave_room 注销，此处只兜住「注册落在
            # 退房快照之后」的残留（并发 join 的线程窗口）——残留键对应的在场宣告须补一次 leave。
            # ⚠️ 必须在 ``finally`` 里（与 async 同构）：退房广播抛错时跳过注销 = 死 sid 永久占名 ⇒ 同名重连恒 ``4105``。
            # Residual cleanup runs after the leave broadcasts (S3); in ``finally`` so a failed broadcast cannot leak a key.
            with self._registry_lock:
                residual = release_name(self._name_to_sid_map, self._sid_to_name_key, sid)
            if residual is not None:
                try:
                    self._announce_residual_leave(sid, residual)
                except Exception:
                    logger.exception(f"残留键 leave 宣告失败（键已注销）/ residual leave announce failed: {residual}")
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
        **原子**注册 ``(office_id, role, name)`` → sid（同步）：注册表锁内一次完成存活 / Agent 席位 / 同名校验与写入。
        Atomically register the ``(office_id, role, name)`` → sid mapping under the registry lock (sync).

        存活判据 ``manager.is_connected``：断连收尾一开始（``pre_disconnect``）即为假，早于断连 handler——故
        「注册先于断连」时由断连的残留注销清掉，「注册晚于断连开始」时在此被拒，两种交错都不留死 sid 的键（🔴7）。
        ``is_connected`` turns False before the disconnect handler runs, so no interleaving leaves a dead key.

        **已知边界（刻意保留）**：本原子步位于 ``enter_room`` 的生效阶段（``super().enter_room`` 与写会话
        ``office_id`` **之后**），不前移到第一个副作用之前——前移会打开「注册表已指向该 sid、会话尚无
        ``office_id``」的窗口，relay 在其中误报注册表损坏（#213 已论证）。代价：**并发竞争的输家**（4101 / 4105）
        会短暂进入目标 socketio 房、换房的 Computer 已先退掉旧房，随后按提交点收敛为无房。仅限竞争输家；
        阶段 1 的预检仍挡住一切非并发的冲突。
        Known boundary: a concurrent-race loser is briefly in the room before convergence (kept deliberately).

        Raises:
            SessionGoneError: sid 已进入断连收尾。
            RoomFullError: ``4101``（目标房已有另一 Agent）。
            NameConflictError: 当键已被其他sid使用时 / When the key is already held by another sid
        """
        with self._registry_lock:
            reserve_name(
                self._name_to_sid_map,
                self._sid_to_name_key,
                (office_id, role, name),
                sid,
                connected=bool(self.server.manager.is_connected(sid, self.namespace)),
            )
        logger.debug(f"Registered name {(office_id, role, name)!r} -> sid '{sid}' in namespace {self.namespace}")

    def _unregister_name(self, sid: SID, office_id: OFFICE_ID | None = None) -> None:
        """
        注销sid当前持有的名字映射（同步镜像：按反向索引 ``sid → key`` 定位、不从会话字段反推；归属守卫只删本 sid 持有的键）
        Unregister the mapping held by ``sid`` (sync; via the reverse index, ownership-guarded)

        ``office_id`` 非空时只注销该房的键（``leave_room`` 用）。/ With ``office_id``, only that office's key.
        """
        with self._registry_lock:
            key = release_name(self._name_to_sid_map, self._sid_to_name_key, sid, office_id)
        if key is not None:
            logger.debug(f"Unregistered name {key!r} for sid '{sid}' in namespace {self.namespace}")

    def _holds_name(self, office_id: OFFICE_ID, role: str, name: str, sid: SID) -> bool:
        """``(office_id, role, name)`` 此刻是否仍由 ``sid`` 持有（锁内读取）。/ Whether ``sid`` still holds the key."""
        with self._registry_lock:
            return self._name_to_sid_map.get((office_id, role, name)) == sid

    def _announce_residual_leave(self, sid: SID, key: NAME_KEY) -> None:
        """断连残留键的补发退房宣告钩子（基类无协议知识 ⇒ no-op；``SyncSMCPNamespace`` 覆写）。
        Hook to announce a leave for a residual key found at disconnect (no-op in the base class).
        """

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
