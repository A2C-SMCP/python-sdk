# filename: client.py
# @Time    : 2025/8/17 16:55
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
import asyncio
import base64
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias, cast

from mcp.types import CallToolResult, Resource
from pydantic import TypeAdapter
from socketio import AsyncClient

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.computer.blob import (
    BlobHandleError,
    BlobHandleInvalidError,
    decode_blob_handle,
    encode_skill_handle,
)
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.base_client import MCPCapabilityNotSupportedError, MCPServerNotFoundError
from a2c_smcp.computer.mcp_clients.model import GetSkillRet as GetSkillRetModel
from a2c_smcp.computer.skills import SkillNameError, SkillSandboxError, parse_skill_name
from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.smcp import (
    CANCEL_TOOL_CALL_NOTIFICATION,
    GET_BLOB_EVENT,
    GET_CONFIG_EVENT,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_SKILL_EVENT,
    GET_SKILLS_EVENT,
    GET_TOOLS_EVENT,
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_EVENT,
    PUT_BLOB_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_EVENT,
    UPDATE_DESKTOP_EVENT,
    UPDATE_SKILLS_EVENT,
    UPDATE_TOOL_LIST_EVENT,
    A2CResource,
    AgentCallData,
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
    LeaveOfficeReq,
    MCPServerInput,
    PutBlobReq,
    PutBlobRet,
    ResourceAnnotations,
    ToolCallReq,
    UpdateComputerConfigReq,
)
from a2c_smcp.smcp import (
    MCPServerConfig as SMCPServerConfigDict,
)
from a2c_smcp.utils.bundle_id import resolve_bundle_id
from a2c_smcp.utils.handshake import (
    DEFAULT_HANDSHAKE_TRANSPORTS,
    HANDSHAKE_CONNECT_ERRORS,
    apply_polling_first_guard,
    build_handshake_url,
    build_protocol_version_error,
    extract_4008_payload,
)
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.office import NO_RESPONSE_MESSAGE, OFFICE_REJOIN_TIMEOUT, parse_join_ack

logger = get_logger(__name__)

AuthProvider: TypeAlias = Callable[[], Awaitable[dict[str, Any]]]
"""
动态 auth provider 类型别名（#200 方案 C）/ Dynamic auth provider type alias (#200 plan C).

零参 **async** callable，返回通用、可序列化的 Socket.IO auth dict，
供 ``SMCPComputerClient.connect(url, auth_provider=...)`` 注入。
Zero-arg **async** callable returning a generic, serializable Socket.IO auth dict,
injected via ``SMCPComputerClient.connect(url, auth_provider=...)``.

原生透传语义 / Native passthrough semantics (zero SDK machinery):
  - python-socketio 在**每次握手**（首连 + 每次自动重连尝试）都会 ``await`` 重新求值 →
    轮换短期凭证无需拆除健康连接 / re-evaluated by python-socketio at EVERY handshake
    (first connect + every auto-reconnect attempt) — rotate short-lived credentials
    without tearing down healthy connections;
  - provider 异常表现为连接失败，重试节奏沿用 socketio 有界重试，瞬时失败可自愈 /
    provider exceptions surface as connection failure; bounded socketio retry applies,
    transient failures self-heal on the next attempt;
  - 同步 callable 亦被原生路径接受（推荐 async）/ sync callables are also accepted by the
    native path (async is recommended).

Provider 契约（务必遵守）/ Provider contract (must follow):
  - 异常**不得内嵌 secret**——engineio 会对 provider 异常打印 traceback（上游限制，跟踪 #201）/
    exceptions MUST NOT embed secrets (engineio logs their traceback; upstream limitation #201);
  - 无超时保障：provider 永不返回会无限挂起握手（上游缺陷，跟踪 #201）/
    no timeout guarantee: a never-returning provider hangs the handshake (upstream defect #201);
  - SDK 不持久化、不打印 auth payload / the SDK never persists or logs the auth payload.
"""


def _to_a2c_resource(res: Resource) -> A2CResource:
    """
    中文: 将 MCP ``Resource`` 映射为 A2C 协议层 ``A2CResource``（snake_case mirror）。
    英文: Map an MCP ``Resource`` to the A2C protocol ``A2CResource`` (snake_case mirror).

    映射协议固定的 ``A2CResource`` 子集（``annotations`` 内含 ``audience`` /
    ``priority`` / ``last_modified``）：仅做 camelCase→snake_case 字段名规整，
    不按 scheme 或内容做过滤丢弃；MCP ``Resource.title`` / ``icons`` 按 v0.2
    规范故意不纳入（A2CResource 字段集固定，非内容过滤）。
    Maps the protocol-fixed ``A2CResource`` subset; camelCase→snake_case key
    normalization only, no scheme/content-driven dropping. MCP ``Resource.title``
    / ``icons`` are intentionally omitted per the v0.2 spec (fixed field set).
    协议依据 / Protocol: a2c-smcp-protocol data-structures.md#A2CResource。
    """
    a2c: A2CResource = {"uri": str(res.uri), "name": res.name}
    if res.description is not None:
        a2c["description"] = res.description
    if res.mimeType is not None:
        a2c["mime_type"] = res.mimeType
    if res.size is not None:
        a2c["size"] = res.size
    if res.annotations is not None:
        ann: ResourceAnnotations = {}
        if res.annotations.audience is not None:
            ann["audience"] = list(res.annotations.audience)
        if res.annotations.priority is not None:
            ann["priority"] = res.annotations.priority
        # last_modified：协议 ResourceAnnotations 已声明；防御式读取，兼容当前/未来 MCP 版本
        # last_modified: declared by protocol ResourceAnnotations; defensive getattr
        # so it works whether or not the installed MCP Annotations model carries it.
        last_modified = getattr(res.annotations, "lastModified", None)
        if last_modified is not None:
            ann["last_modified"] = last_modified
        if ann:
            a2c["annotations"] = ann
    if res.meta is not None:
        a2c["_meta"] = res.meta
    return a2c


class SMCPComputerClient(AsyncClient):
    """
    SMCP协议Computer侧的Socket.IO客户端，在创建的时候需要指定 MCPServerManager
    如果在使用Socket.IO过程中，需要实现SMCP协议，则需要使用此客户端，不能仅仅使用原生AsyncClient
    """

    def __init__(
        self,
        *args: Any,
        computer: Computer,
        namespace: str = SMCP_NAMESPACE,
        **kwargs: Any,
    ) -> None:  # noqa: E112
        """
        初始化Computer侧Socket.IO客户端
        Initialize Computer-side Socket.IO client

        Args:
            computer (Computer): 绑定的Computer实例 / Bound Computer instance
            namespace (str): Socket.IO命名空间，默认 ``/smcp`` / Socket.IO namespace, default ``/smcp``

        Note:
            #112(AS-38)：连接面鉴权走 Socket.IO ``auth`` dict（字段 ``token``）。Computer 侧的 ``auth`` dict
            由调用方在 ``connect(url, auth=...)`` 时提供（CLI 经 ``--auth`` 注入），本客户端不持有凭据、
            不构造 ``auth``。/ The connection ``auth`` dict (field ``token``) is supplied by the caller at
            ``connect(url, auth=...)`` (CLI injects via ``--auth``); this client holds no credential.
        """
        super().__init__(*args, **kwargs)
        self.computer = computer
        # 实例级握手配置 / Per-instance handshake config
        self._namespace = namespace
        # 将客户端以 weakref 方式绑定回 Computer，避免循环强引用
        self.computer.socketio_client = self
        self.on(TOOL_CALL_EVENT, self.on_tool_call, namespace=self._namespace)
        self.on(GET_TOOLS_EVENT, self.on_get_tools, namespace=self._namespace)
        self.on(GET_CONFIG_EVENT, self.on_get_config, namespace=self._namespace)
        self.on(GET_DESKTOP_EVENT, self.on_get_desktop, namespace=self._namespace)
        self.on(GET_RESOURCES_EVENT, self.on_get_resources, namespace=self._namespace)
        self.on(GET_BLOB_EVENT, self.on_get_blob, namespace=self._namespace)
        # v0.4.0 上行写入通道 / upload write channel (#196)
        self.on(PUT_BLOB_EVENT, self.on_put_blob, namespace=self._namespace)
        # v0.2.1 SKILL 通道发现 / 渐进式披露 / SKILL channel discovery & progressive disclosure (#66)
        self.on(GET_SKILLS_EVENT, self.on_get_skills, namespace=self._namespace)
        self.on(GET_SKILL_EVENT, self.on_get_skill, namespace=self._namespace)
        # v0.2.1 工具调用取消 / tool_call cancellation（#96）：接收 Server 广播的 notify:tool_call_cancel，
        # 按 req_id 中断本机在途工具调用。notify:* 仅接收、不回执。
        self.on(CANCEL_TOOL_CALL_NOTIFICATION, self.on_tool_call_cancel, namespace=self._namespace)
        # ── #203 Office 成员关系：desired + generation ──────────────────────────────────────
        # ``office_id`` 语义 = **desired**（"想要加入的房间"）：传输中断且底层会自动重连时**保留**，
        # 等 namespace 重连后自动回房；显式 leave / 手工断开 / 服务端踢出 / 重连彻底放弃 / 回房失败
        # 时清空。Socket.IO 房间成员关系属于**会话**，断线重连后服务端已销毁旧会话的成员关系，故
        # 客户端必须主动重放 ``server:join_office``（协议 runtime-contract §4.5.3：reconnect MUST
        # 投影当前 desired state）。/ ``office_id`` is the *desired* membership: retained across a
        # transport drop when socketio will reconnect, cleared when it cannot be honored.
        self.office_id: str | None = None
        # ``_confirmed_office_id`` = **服务端已确认**的成员关系（#213）：只在 join 裁决为成功时写入，
        # 在退房 / 服务端踢出 / 重连彻底放弃 / 回房被拒时清空。与 ``office_id``（意图）分离是必需的——
        # join 入口会预写意图（见下），被抢占的那次 join 会把**未经确认**的房号短暂留在字段里；
        # 若拒绝回退直接取 ``office_id``，并发下就会把「从未加入过的房间」钉回状态。
        # ``_confirmed_office_id`` = the last server-confirmed membership, kept apart from the desired
        # ``office_id`` because the entry pre-write can hold an unconfirmed intent (#213).
        self._confirmed_office_id: str | None = None
        self._office_generation: int = 0
        self._office_rejoin_task: asyncio.Task[None] | None = None
        self._office_op_lock = asyncio.Lock()
        # 引擎级（非协议）namespace 生命周期钩子。两个 handler 刻意写成**同步函数**：engineio 以
        # ``run_async=False`` 内联触发 'disconnect'（engineio/async_client.py:614），在其中 await 会把
        # 拆链与 ``eio.disconnect()`` 一起卡住——函数签名是"零 await"的结构性保证。
        # Engine-level namespace hooks, deliberately **sync** functions: engineio dispatches
        # 'disconnect' inline (run_async=False), where awaiting would stall the teardown.
        self.on("connect", self._on_namespace_connect, namespace=self._namespace)
        self.on("disconnect", self._on_namespace_disconnect, namespace=self._namespace)
        self.on("__disconnect_final", self._on_namespace_disconnect_final, namespace=self._namespace)

    @property
    def namespace(self) -> str:
        """
        返回当前实例使用的 Socket.IO 命名空间
        Return the Socket.IO namespace used by this instance
        """
        return self._namespace

    def _in_office(self) -> bool:
        """
        ``server:update_*`` 上报守卫：是否"已入房**且**连接可用"。

        Guard for the ``server:update_*`` emitters: in an office *and* on a live namespace.

        #203：不能只看 ``office_id``——desired 在重连窗口内被刻意保留，此时 namespace 不在册，
        ``emit`` 会抛 ``BadNamespaceError``（破坏 ``Computer`` 侧记录的"未入房 → no-op"契约）。
        Since #203 the desired office survives a reconnect window, where the namespace is not
        registered and ``emit`` would raise ``BadNamespaceError``.
        """
        return self.office_id is not None and self._namespace in self.namespaces

    async def connect(
        self,
        url: str,
        *args: Any,
        auth_provider: AuthProvider | None = None,
        **kwargs: Any,
    ) -> None:
        """
        覆盖 ``AsyncClient.connect``：注入协议版本握手，使所有调用点（CLI / 交互式 / 测试）
        自动合规，无需各处重复拼接。
        Override ``AsyncClient.connect`` to inject the protocol version handshake so every
        call site (CLI / interactive / tests) is automatically compliant without duplication.

        - 协议 MUST：自动从 ``PROTOCOL_VERSION`` 常量拼接 ``a2c_version``（保留调用方既有 query）
        - 协议 §1 polling-first MUST 护栏：调用方显式 WS-only 不静默放行，强制重注入 polling-first
        - 捕获 4008 → 主动 ``disconnect()`` → 抛 :class:`ProtocolVersionError`；非 4008 保持原异常
        - #200 动态 auth（方案 C 原生透传，零新增机制）：``auth_provider`` 与静态 ``auth`` 互斥
          （同时传入 → ValueError）、非 callable → TypeError 早失败；底层每次握手（首连 + 自动重连）
          重新求值。 / #200 dynamic auth (plan C, native passthrough): ``auth_provider`` is mutually
          exclusive with static ``auth`` (ValueError), non-callable fails fast (TypeError); the
          underlying client re-evaluates it at every handshake (first connect + auto-reconnects).
        """
        # #200 方案 C：显式 auth_provider → 原生 auth 路径，零新增机制
        # #200 plan C: explicit auth_provider routed to the native auth path (no new machinery)
        if auth_provider is not None:
            # 与静态 auth 互斥（含按上游签名位置传入：url 之后第 2 个位置参数 = auth）
            # Mutually exclusive with static auth (incl. positional per upstream signature:
            # the 2nd positional arg after url is auth).
            if kwargs.get("auth") is not None or (len(args) > 1 and args[1] is not None):
                raise ValueError(
                    "auth 与 auth_provider 互斥，只能二选一 / 'auth' and 'auth_provider' are mutually exclusive; provide only one",
                )
            # 运行时防御：类型注解已保证 callable，此处拦截非类型化调用方传入的非 callable 值
            # Runtime defense: the annotation already guarantees callable; this intercepts
            # non-callable values injected by untyped callers.
            if not callable(cast(Any, auth_provider)):
                # 运行时不探测签名（对 partial/builtins 脆弱）——零参 async 由 AuthProvider 契约保证
                # The signature is not probed at runtime (fragile for partials/builtins) —
                # zero-arg async is guaranteed by the AuthProvider contract.
                raise TypeError(
                    "auth_provider 必须是 callable（零参 async 契约见 AuthProvider 文档）"
                    "/ 'auth_provider' must be a callable (zero-arg async per the AuthProvider contract)",
                )
            kwargs["auth"] = auth_provider
        handshake_url = build_handshake_url(url, PROTOCOL_VERSION)
        kwargs.setdefault("transports", DEFAULT_HANDSHAKE_TRANSPORTS)
        # 协议 §1 polling-first MUST 护栏（统一接线，详见 handshake.apply_polling_first_guard）
        kwargs["transports"] = apply_polling_first_guard(kwargs.get("transports"), logger)
        logger.info(f"Connecting to SMCP server at {url} (a2c_version={PROTOCOL_VERSION})")
        try:
            await super().connect(handshake_url, *args, **kwargs)
        except HANDSHAKE_CONNECT_ERRORS as e:
            payload = extract_4008_payload(e)
            if payload is None:
                # 非协议版本错误：保持原异常 / not a version error: preserve the original exception
                raise
            # 协议 §4 MUST：先主动断开再抛异常，防止底层库自动重连触发 4008 死循环
            # Protocol §4 MUST: proactively disconnect before raising (anti reconnect-loop)
            await self.disconnect()
            raise build_protocol_version_error(payload) from e

    async def emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        """
        相较于父类方法，提供一个event校验能力，在A2C-smcp协议内，Computer客户端不允许发起 notify:* 事件与 client:* 事件

        A2C-smcp协议内：
            notify:* 事件由信令服务器发起，用于通知客户端
            client:* 事件由ComputerClient执行，一般会给出执行结果
            agent:* 事件由AgentClient执行，一般会给出执行结果
            server:* 事件由服务管理器执行，但一般不需要给出执行结果

        Args:
            event (str): 发送的事件名称
            data (Any): 发送的数据
            namespace (str | None): 命名空间
            callback (Any): 回调
        """
        if event.startswith("notify:"):
            raise ValueError("ComputerClient不允许使用notify:*事件")  # pragma: no cover
        if event.startswith("client:"):
            raise ValueError("ComputerClient不允许发起client:*事件")  # pragma: no cover
        # 未显式传入时使用实例命名空间 / Fall back to instance namespace if not provided
        effective_namespace = namespace if namespace is not None else self._namespace
        await super().emit(event, data, effective_namespace, callback)

    def _cancel_office_rejoin(self) -> None:
        """
        作废在途自动回房（**同步 cancel，不 await**）。

        Cancel any in-flight office replay without awaiting it — call sites sit on inline paths
        (engineio 的 namespace 钩子) where awaiting is not allowed.
        """
        task = self._office_rejoin_task
        self._office_rejoin_task = None
        if task is not None and not task.done():
            task.cancel()

    def _on_namespace_connect(self) -> None:
        """
        namespace (重)连接钩子：desired 仍在则调度自动回房。**必须零 await**。
        Namespace (re)connect hook: schedule the office replay when a desired office remains.

        #203：Socket.IO 房间成员关系随会话销毁，重连后新 SID 不在房间里——这里重放
        ``server:join_office`` 恢复成员关系。/ Room membership is session-scoped, so the new SID
        must replay ``server:join_office``.

        为什么甩后台任务而不是内联 await（上游时序，实测）：本钩子被 ``_handle_connect`` **内联
        await**，其返回后才会 ``_connect_event.set()``；内联等 JOIN 的 ACK 会推迟"连接完成"信号，
        且钩子内抛出的异常会让 ``set()`` 永不执行（重连周期被拖到 wait_timeout）。
        Why a detached task instead of an inline await: this hook is awaited inline by
        ``_handle_connect`` *before* ``_connect_event.set()``; an inline ack wait delays that
        signal and any exception would skip it entirely.

        守卫用 ``self._namespace in self.namespaces`` 而**不是** ``self.connected``：socketio 的
        ``connected`` 要等覆写的 ``connect()`` 返回后才置位，早于本任务第一步（实测），用它会让
        回房被静默跳过。 / Guard on the registered namespace, never on ``self.connected``: the
        latter is still False when the replay task first runs (verified empirically).
        """
        self._office_generation += 1
        self._cancel_office_rejoin()
        desired = self.office_id
        if desired is None:
            return
        self._office_rejoin_task = asyncio.create_task(self._arejoin_office(desired, self._office_generation))

    def _on_namespace_disconnect(self, reason: str | None = None) -> None:
        """
        namespace 断连钩子：按断开原因决定 desired 的去留。**必须零 await**（内联触发路径）。
        Namespace disconnect hook: decide whether the desired office survives, keyed on the reason.

        - ``transport error`` 且底层会自动重连 → **保留** desired（等重连后自动回房）；
        - 其余（手工断开 ``client disconnect`` / 服务端踢出 ``server disconnect`` / 未启用重连）
          → **清空**，下次连接不得静默回旧房间。/ Retained only for a transport error that will be
          retried; every other reason clears the intent.
        """
        self._office_generation += 1
        self._cancel_office_rejoin()
        # 无论哪种原因，**已确认**房号都随本次会话作废：房间成员关系属于会话（见类头注释），断开即销毁，
        # 重连后的新 SID 从未加入过该房——要重新成为成员必须靠回房重放成功（那时才重新落账）。
        # 与 desired 的差别只在下面这一支：传输中断且会自动重连时 desired **保留**（待重放），
        # 但它此时只是「意图」，不得当作「已确认」。#213
        self._confirmed_office_id = None
        if reason == self.reason.TRANSPORT_ERROR and self.reconnection:
            return
        self.office_id = None

    def _on_namespace_disconnect_final(self) -> None:
        """
        重连彻底放弃（重试次数用尽 / ``shutdown()`` 中止）→ 清空 desired。**必须零 await**。

        socketio 只在"不会再重连"时触发 ``__disconnect_final``；此时若保留旧 ``office_id``，状态会
        长期撒谎（emit 守卫放行却无连接可用）。/ ``__disconnect_final`` is socketio's only reliable
        "will not reconnect" signal; keeping the stale id past it would be a standing lie.
        """
        self._office_generation += 1
        self._cancel_office_rejoin()
        self.office_id = None
        self._confirmed_office_id = None

    async def _arejoin_office(self, office_id: str, generation: int) -> None:
        """
        自动回房：在新建立的 namespace 连接上重放 ``server:join_office``。

        Replay ``server:join_office`` on the freshly re-established namespace connection (#203).

        结果只在 generation 仍新鲜时落到状态上：期间用户显式换房/退房（generation 前进）则整条结果
        作废——陈旧回房的失败**不得**清掉用户刚设好的房号。/ Results are applied only while the
        generation is still current, so a superseded replay can never clobber a newer office.

        单次尝试：被拒即清空 desired + 错误日志（镜像 rust-sdk#204 语义，不做重试）。
        Single attempt, mirroring rust-sdk#204: a rejection clears the desired office and logs.
        """
        async with self._office_op_lock:
            if generation != self._office_generation or self.office_id != office_id:
                return  # 已被更新的操作接管 / superseded by a newer operation
            if self._namespace not in self.namespaces:
                return  # 连接又断了：交给下一次 connect 钩子 / the connection dropped again
            try:
                result = await self.call(
                    JOIN_OFFICE_EVENT,
                    EnterOfficeReq(office_id=office_id, role="computer", name=self.computer.name),
                    namespace=self._namespace,
                    timeout=OFFICE_REJOIN_TIMEOUT,
                )
            except Exception as e:
                if generation != self._office_generation or self.office_id != office_id:
                    return  # 结果已作废，不得用陈旧结果改状态
                self.office_id = None
                self._confirmed_office_id = None
                logger.error(f"自动重新加入 Office 失败: {office_id} - {e}")
                return
            ok, error_msg = parse_join_ack(result)
            if ok:
                # 同 join_office：成功是对服务端事实的陈述，不受 supersession 守卫约束（#213）
                self._confirmed_office_id = office_id
            if generation != self._office_generation or self.office_id != office_id:
                return  # 结果已作废：除上面「已成事实」的落账外，不得改动 desired / 日志
            if ok:
                logger.info(f"已自动重新加入 Office: {office_id}")
            else:
                self.office_id = None
                self._confirmed_office_id = None
                logger.error(f"自动重新加入 Office 被拒绝: {office_id} - {error_msg}")

    async def join_office(self, office_id: str) -> None:
        """
        加入一个Office（Socket.IO中的Room）
        Join an Office (Room in Socket.IO)

        #203：本方法同时是"desired 意图"的声明点——入口**先同步**推进 generation 并作废在途自动回房，
        再写入 ``office_id``，最后才抢 office 操作锁。顺序不可反转：若在途回房的失败路径能清状态，
        会把这里刚写入的房号抹掉（四个 emit 守卫随之静默失效）。
        This method also declares the desired intent: the generation is advanced and any in-flight
        replay invalidated *before* the new id is written, so a superseded replay's failure path can
        never clear it.

        #213：失败时的房号去留按**失败形态**分刀（服务端拒绝不改变既有成员关系 ⇒ 客户端也不得清掉
        仍在的房间）：
          - 服务端**明确拒绝**（给了裁决文案）⇒ 回退到 ``_confirmed_office_id``（最近一次被服务端确认的
            房号，通常是旧房；从未确认过则为 ``None``）；
          - 传输层失败 / 空响应（无法判定服务端是否已生效）⇒ 维持清空语义（不臆断）。
        回退取的是**已确认**房号而非入口快照：入口预写的意图在并发下可能属于一次「裁决被丢弃」的
        在途 join，绝不代表真实成员关系。
        #213: a rejection restores the last *server-confirmed* office — never the desired field, whose
        value may be an unconfirmed pre-write from a superseded in-flight join.

        Args:
            office_id (str): 房间ID，在A2C-smcp协议中，OfficeID即为Socket.IO RoomID / Room ID, in A2C-smcp protocol,
                OfficeID is Socket.IO RoomID

        Raises:
            RuntimeError: 当加入房间失败时（例如重名）/ When joining room fails (e.g., duplicate name)
        """
        generation = self._office_generation + 1
        self._office_generation = generation
        self._cancel_office_rejoin()
        # 提前设置 office_id，避免服务器广播事件时 office_id 仍为 None 的时序竞争问题
        # Set office_id before sending request to avoid race condition when server broadcasts events
        self.office_id = office_id

        try:
            # 使用 call 方法等待服务器返回结果 / Use call method to wait for server response
            async with self._office_op_lock:
                result = await self.call(
                    JOIN_OFFICE_EVENT,
                    EnterOfficeReq(office_id=office_id, role="computer", name=self.computer.name),
                    namespace=self._namespace,
                )

            # 检查返回结果 / Check return result
            verdict = parse_join_ack(result)
        except Exception:
            # 失败清空 office_id —— 但仅当本次操作仍是最新意图时才清（compare-and-clear）：
            # 并发/后到的 join 或 leave 已推进 generation 时，其写入的房号不得被本次失败抹掉。
            # Compare-and-clear: a newer operation's office id must survive this failure.
            #
            # 已知语义（有意，非副作用）：若 generation 的推进来自 **断连钩子**（传输中断且会自动重连），
            # 则这里**保留** office_id = 本次声明的房号——失败的是"这一次尝试"，不是"想要在这个房间"的
            # 意图；重连后回房会重新裁决，成功即恢复、被拒才清空（#203 口径 1）。调用方看到 RuntimeError
            # 表示本次未落地，可自行重试；此时 office_id 非空**不代表**已在房间，判断"是否真在房间里"
            # 请用 `_in_office()`。
            # Deliberate: a disconnect-driven generation bump keeps the intent (the attempt failed,
            # not the wish to be in that room); the post-reconnect replay re-adjudicates it.
            #
            # 传输层失败（超时 / 命名空间不可用）**无法判定**服务端是否已生效 ⇒ 保守清空，不臆断房号，
            # 已确认房号一并不再可信：请求**可能已发出**（超时类确已发出；`BadNamespaceError` 类在发送前
            # 抛出，但那时会话已销毁、成员关系本就不存在）⇒ 两种情形清空都准确，而继续宣称旧房则可能撒谎。
            # 只有在「本次仍是最新意图」时才清（被断连抢占时由断连钩子负责）。
            # An inconclusive outcome invalidates the confirmed room too: the request did go out, so the
            # old membership may already be gone. Cleared only while this attempt is still current.
            if self._office_generation == generation:
                self.office_id = None
                self._confirmed_office_id = None
            raise

        ok, error_msg = verdict
        if not ok:
            # 服务端给出了裁决 ⇒ **明确拒绝**。协议 room-model.md「加入时校验失败 ⇒ 拒绝加入」且拒绝
            # **不改变既有成员关系**（校验先于副作用），故此处回退到**已确认**房号，而不是清空——清空会让
            # 宿主以为「不在任何房」，与真实成员关系相反（#213 的服务端修正后尤其如此：换房被拒时它仍在
            # 旧房）。空响应（NO_RESPONSE_MESSAGE）不是裁决，维持清空语义（与上面传输层失败一致）。
            # An explicit verdict = rejection: the server does not change existing membership, so fall
            # back to the last *confirmed* office instead of clearing. An empty ack is not a verdict.
            if self._office_generation == generation:
                self.office_id = self._confirmed_office_id if error_msg != NO_RESPONSE_MESSAGE else None
            raise RuntimeError(f"加入房间失败 / Failed to join office: {error_msg}")

        # 裁决为成功：记录服务端已确认的成员关系。**刻意不受 supersession 守卫约束**——成功是关于
        # 服务端**事实**的陈述：即便本次 join 已被后到操作抢占（desired 归后者），这次成员变更**真实
        # 发生过**，不落账会让之后任何一次「被拒回退」指向更旧、且已失效的房号。
        # 次序安全：`call` 由 ``_office_op_lock`` 串行，且从锁释放到本行无 await ⇒ 后完成者胜，
        # 陈旧成功不会覆盖更新的真值。
        # Recorded regardless of supersession: a success is a statement of server-side fact.
        self._confirmed_office_id = office_id

    async def leave_office(self, office_id: str) -> None:
        """
        离开一个Office（Socket.IO中的Room）

        #203：退房同时作废 desired 与在途自动回房——重连不得把用户刚退掉的房间再回一遍。
        Leaving also invalidates the desired intent and any in-flight replay.

        Args:
            office_id (str): 房间ID
        """
        self._office_generation += 1
        self._cancel_office_rejoin()
        async with self._office_op_lock:
            await self.emit(LEAVE_OFFICE_EVENT, LeaveOfficeReq(office_id=office_id))
        self.office_id = None
        # 已确认房号随之作废：退房后再被拒的 join 不得把旧房号回退回来（#213）
        self._confirmed_office_id = None

    async def emit_update_config(self) -> None:
        """
        当前MCP配置更新时需要触发此事件向信令服务器推送，进而触发Agent端的配置更新

        不需要传递当前的配置参数，因为Agnet会通过其它接口进行刷新
        """
        if self._in_office():
            await self.emit(UPDATE_CONFIG_EVENT, UpdateComputerConfigReq(computer=self.computer.name))

    async def update_config(self) -> None:
        """
        当前MCP配置更新时需要触发此事件向信令服务器推送，进而触发Agent端的配置更新

        不需要传递当前的配置参数，因为Agnet会通过其它接口进行刷新
        """
        await self.emit(UPDATE_CONFIG_EVENT, UpdateComputerConfigReq(computer=self.computer.name))

    async def emit_update_tool_list(self) -> None:
        """
        工具列表变更时需要触发此事件向信令服务器推送，服务端会广播 notify:update_tool_list。
        When tool list changes, emit event to server; it will broadcast notify:update_tool_list.
        """
        if self._in_office():
            await self.emit(UPDATE_TOOL_LIST_EVENT, UpdateComputerConfigReq(computer=self.computer.name))

    async def emit_refresh_desktop(self) -> None:
        """
        桌面刷新触发：当资源列表或资源内容变化时，通知信令服务器。服务端会广播 notify:update_desktop。
        Desktop refresh trigger: notify server when resources list/content changed; server will broadcast notify:update_desktop.
        """
        if self._in_office():
            await self.emit(UPDATE_DESKTOP_EVENT, UpdateComputerConfigReq(computer=self.computer.name))

    async def emit_update_skills(self) -> None:
        """
        SKILL 集合变更（增/删/物化更新）时触发，向信令服务器推送 server:update_skills；服务端广播
        notify:update_skills，Agent 据此自动重拉 client:get_skills（与既有 notify:update_* 自动刷新一致）。
        When the SKILL set changes, emit server:update_skills; server broadcasts notify:update_skills.

        复用 UpdateComputerConfigReq（协议 events.md §server:update_skills 明确复用，不新建结构）；
        office_id 守卫与 :meth:`emit_update_tool_list` 一致——未入房间不发送。
        Reuses UpdateComputerConfigReq (protocol reuses it); office_id guard mirrors emit_update_tool_list.

        v0.2.1（S14，#67）：本方法是 :class:`~a2c_smcp.computer.skills.debouncer.SkillEventDebouncer` 的
        **低层 emit sink**——多源 SKILL 变更（mcp ``ResourceListChanged`` / user 源文件 watcher / CLI 操作）
        统一经 ``Computer`` 持有的去抖器在 300ms 窗口内合并后调用本方法，事件处理器**不**应裸调它（设计 §8.1）。
        This is the low-level emit sink for the Computer-owned ``SkillEventDebouncer``; event handlers must
        route through the debouncer (300ms coalescing) rather than calling this directly.
        """
        if self._in_office():
            await self.emit(UPDATE_SKILLS_EVENT, UpdateComputerConfigReq(computer=self.computer.name))

    async def on_tool_call(self, data: ToolCallReq) -> dict:
        """
        信令服务器通知计算机端，有工具调用请求

        v0.2.1 二进制旁路 / Binary sideband:
            返回前遍历 ``CallToolResult.content``，超内联预算（``BlobThresholds.inline_budget``）的
            二进制 content item 经 :meth:`Computer.mint_toolspool_handle` 写入 ``.blobspool``、清空
            内联 ``data`` / ``blob``、写 item ``_meta.a2c_blob_handle`` (+ ``a2c_total_size`` /
            ``a2c_sha256``)。小尺寸二进制原样内联；工具失败仍走 MCP ``isError``。
            Pre-return: oversize binary content items (per ``inline_budget``) are minted to
            ``.blobspool``, inline ``data`` / ``blob`` cleared, and ``_meta.a2c_blob_handle`` etc.
            written. Below-budget items inline as-is; tool failures still go via MCP ``isError``.

        协议依据 / Protocol: blob-transfer.md §5（生产者通道接入契约）+ data-structures.md
        §BlobHandle 「MCP CallToolResult content item ``_meta.a2c_blob_handle`` 旁路」.

        Args:
            data (ToolCallReq): 请求数据 / request data.

        Returns:
            dict: 工具调用结果的字典表示（JSON 可序列化，二进制旁路已铸造）.
        """
        # Server 通过 session 保证请求来自同一 office，无需在此验证 agent 与 office_id 的关系
        # Server guarantees request is from same office via session, no need to validate agent vs office_id here
        if self.computer.name != data["computer"]:
            raise SMCPNamespaceError("计算机标识不匹配")
        try:
            ret = await self.computer.aexecute_tool(
                req_id=data["req_id"],
                tool_name=data["tool_name"],
                parameters=data["params"],
                timeout=data["timeout"],
            )
            # 将 CallToolResult 转换为字典以便 JSON 序列化 / Convert CallToolResult to dict for JSON serialization
            # 注意：**禁止**传 ``by_alias=True``——协议要求结果级元数据出线 key 为顶层 ``meta``（见 data-structures.md
            # §「结果级 meta vs 子级 _meta」/ #115）。默认（按字段名）dump 才得 ``meta``；by_alias 会回退到 alias ``_meta``，
            # 破坏 Agent 对 ``meta.a2c_cancelled`` / ``a2c_timeout`` 的解析。
            # NOTE: do NOT pass ``by_alias=True`` — the protocol requires result-level metadata to wire out under the
            # top-level key ``meta`` (data-structures.md / #115). Default (by field name) dump yields ``meta``; by_alias
            # would fall back to the alias ``_meta`` and break the Agent's reading of ``meta.a2c_cancelled`` / ``a2c_timeout``.
            raw = ret.model_dump(mode="json")
            # v0.2.1 二进制旁路铸造（不动 isError 路径）/ v0.2.1 binary sideband minting (isError untouched)
            self._mint_oversize_binary_content(raw)
            return raw
        except Exception as e:
            error_result = CallToolResult(isError=True, structuredContent={"error": str(e), "error_type": type(e).__name__}, content=[])
            return error_result.model_dump(mode="json")

    async def on_tool_call_cancel(self, data: AgentCallData) -> None:
        """信令服务器广播 ``notify:tool_call_cancel``：按 ``req_id`` 中断本机在途工具调用（#96）。

        Server broadcasts ``notify:tool_call_cancel``; interrupt this Computer's in-flight tool call by ``req_id``.

        - 该通知按 office 房间广播，可能投递到房间内并未承载此 ``req_id`` 的其它 Computer——此时为无害 no-op；
        - ``notify:*`` 通知不回执（返回 ``None``），与协议一致；
        - ``AgentCallData`` 不含 ``computer`` 字段，故仅以 ``req_id`` 归属判定（在途 ``req_id`` 在本机唯一）。

        - The notification is room-broadcast and may reach Computers not running this ``req_id`` → harmless no-op;
        - ``notify:*`` returns no ack (``None``); matches the protocol;
        - ``AgentCallData`` carries no ``computer`` field, so ownership is decided solely by ``req_id``.

        Args:
            data (AgentCallData): 取消通知数据（``agent`` / ``req_id``）/ cancel notification data.
        """
        req_id = data["req_id"]
        cancelled = await self.computer.acancel_tool(req_id)
        if cancelled:
            logger.info(f"工具调用已按 notify:tool_call_cancel 中断 / cancelled in-flight tool call req_id={req_id}")
        else:
            logger.debug(f"notify:tool_call_cancel 未命中在途/已完成 req_id={req_id}，忽略 / no in-flight call, ignored")

    def _mint_oversize_binary_content(self, raw: dict) -> None:
        """遍历 ``CallToolResult`` content items，超内联预算的二进制 item 铸造 toolspool 句柄.

        Walk ``CallToolResult.content`` and mint a toolspool handle for any binary item whose
        decoded payload exceeds ``BlobThresholds.inline_budget``. Replaces inline ``data`` / ``blob``
        with ``_meta.a2c_blob_handle`` (+ ``a2c_total_size`` / ``a2c_sha256``).

        协议依据 / Protocol: blob-transfer.md §5 + design §4.2 (Computer on_tool_call mint).
        """
        content = raw.get("content")
        if not isinstance(content, list):
            return
        budget = self.computer.blob_thresholds.inline_budget
        too_large_cap = self.computer.blob_thresholds.too_large_cap
        for item in content:
            if not isinstance(item, dict):
                continue
            # 提取候选字节字段 / Extract candidate bytes field
            payload_b64: str | None = None
            mime: str = str(item.get("mimeType") or "")
            inline_key: str | None = None
            # ImageContent / AudioContent：顶层 ``data`` (base64) + ``mimeType``
            if isinstance(item.get("data"), str):
                payload_b64 = item["data"]
                inline_key = "data"
            # EmbeddedResource.BlobResourceContents：``resource.blob`` (base64) + ``resource.mimeType``
            elif isinstance(item.get("resource"), dict) and isinstance(item["resource"].get("blob"), str):
                payload_b64 = item["resource"]["blob"]
                inline_key = "resource.blob"
                mime = str(item["resource"].get("mimeType") or mime)
            if payload_b64 is None:
                continue
            try:
                payload_bytes = base64.b64decode(payload_b64, validate=False)
            except Exception:  # noqa: BLE001 — 非合法 base64 视为不铸造（直接保留原样）
                continue
            size = len(payload_bytes)
            if size <= budget:
                # 小尺寸：原样内联 / Below budget: keep inline
                continue
            if size > too_large_cap:
                # 超绝对上限：Computer 端拒绝铸造，记 warning；上游 MCP 工具结果转 isError
                # Over hard cap: refuse to mint; convert to error (DoS defense)
                logger.warning(f"on_tool_call binary item size {size} exceeds too_large_cap {too_large_cap}; skipping mint")
                continue
            # _meta 形状校验前置到 mint 之前，避免上游 MCP 工具 dump 出非 dict ``_meta``（如 None）
            # 导致 ``.blobspool`` 落盘后无引用孤儿 cid（依赖后续 GC 清理）.
            # Validate / prepare ``_meta`` BEFORE minting so non-dict ``_meta`` (e.g. ``None`` from
            # some pydantic dumps) cannot leave an orphan cid in ``.blobspool``.
            # ``dict.setdefault`` 对 ``None`` 不替换（返回原 ``None``），故必须显式处理三态：
            # ``dict.setdefault`` does NOT replace ``None`` values, so handle three states explicitly:
            existing_meta = item.get("_meta")
            if existing_meta is None:
                meta: dict[str, Any] = {}
                item["_meta"] = meta
            elif isinstance(existing_meta, dict):
                meta = existing_meta
            else:
                logger.warning(
                    f"on_tool_call skipping mint: item['_meta'] is not a dict ({type(existing_meta).__name__}); keeping inline",
                )
                continue
            try:
                handle = self.computer.mint_toolspool_handle(payload_bytes, mime or "application/octet-stream")
            except Exception as e:  # noqa: BLE001 — 铸造失败不阻断整轮 tool_call，保留原始内联字节
                logger.warning(f"on_tool_call mint failed for item size={size}: {e}; keeping inline")
                continue
            # 写 _meta.a2c_blob_handle + 清空内联字节 / Write sideband, clear inline payload
            meta["a2c_blob_handle"] = handle
            meta["a2c_total_size"] = size
            meta["a2c_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
            # 清空内联载体 / Clear inline carrier
            if inline_key == "data":
                item["data"] = ""
            elif inline_key == "resource.blob":
                item["resource"]["blob"] = ""

    async def on_get_tools(self, data: GetToolsReq) -> GetToolsRet:
        """
        信令服务器通知计算机端，有工具调用请求

        Args:
            data (GetToolsReq): 请求数据
        """
        # Server 通过 session 保证请求来自同一 office，无需在此验证 agent 与 office_id 的关系
        # Server guarantees request is from same office via session, no need to validate agent vs office_id here
        if self.computer.name != data["computer"]:
            raise SMCPNamespaceError("计算机标识不匹配")

        mcp_tools = await self.computer.aget_available_tools()

        return GetToolsRet(tools=mcp_tools, req_id=data["req_id"])

    async def on_get_desktop(self, data: GetDeskTopReq) -> GetDeskTopRet:
        """
        获取当前计算机桌面（窗口资源组织后的视图）。
        Get current desktop organized from window resources.

        Args:
            data (GetDeskTopReq): 请求数据（包含 computer, robot_id, req_id 等）。

        Returns:
            GetDeskTopRet: 桌面数据与 req_id。
        """
        # Server 通过 session 保证请求来自同一 office，无需在此验证 agent 与 office_id 的关系
        # Server guarantees request is from same office via session, no need to validate agent vs office_id here
        if self.computer.name != data["computer"]:
            raise SMCPNamespaceError("计算机标识不匹配")
        size = data.get("desktop_size")
        window_uri = data.get("window")
        desktops = await self.computer.get_desktop(size=size, window_uri=window_uri)
        return GetDeskTopRet(desktops=desktops, req_id=data["req_id"])

    async def on_get_config(self, data: GetComputerConfigReq) -> GetComputerConfigRet:
        """
        获取当前计算机的 MCP 配置（供 Agent 端刷新使用）。
        Get current machine MCP configuration for Agent refresh.

        中文：校验计算机标识后，收集并序列化所有 MCP Server 配置，返回 SMCP 协议定义的配置结构。
        English: Validate computer identifier, then collect and serialize all MCP server configs
        into SMCP protocol defined structure.

        Args:
            data (GetComputerConfigReq): 请求数据。Request payload.

        Returns:
            GetComputerConfigRet: SMCP 协议定义的 MCP 配置返回。SMCP formatted MCP configuration.
        """
        # Server 通过 session 保证请求来自同一 office，无需在此验证 agent 与 office_id 的关系
        # Server guarantees request is from same office via session, no need to validate agent vs office_id here
        if self.computer.name != data["computer"]:
            raise SMCPNamespaceError("计算机标识不匹配")

        servers: dict[str, dict] = {}
        # #149：数据源 = **运行期活跃集的 raw 投影**（F2/PROTO-2：wire 投影 MUST 读运行期权威、MUST NOT 读构造期死快照）。
        # active_server_configs() 的 SET 取 manager 运行期权威、body 取未渲染 raw（占位符字面保留、绝不外泄已解析 secret）。
        # #149: source = raw projection of the runtime-active set (F2/PROTO-2: wire MUST read runtime authority, never the
        # construction-time dead snapshot). SET from manager authority; body kept raw (placeholders literal, no secret leak).
        for cfg in self.computer.active_server_configs():
            # 身份键 = bundle_id（协议 #18）；从 raw config derive（与注册边界 seam 一致，raw #17）。
            # no-double-open：同 bundle_id 保留首个（与 manager boot first-wins 一致）。
            bundle_id = resolve_bundle_id(cfg)
            if bundle_id in servers:
                continue
            # 使用强校验转换为协议定义（中英文）/ Validate strictly to protocol definition (bilingual)
            # 若类型不匹配，抛出异常，属于硬性 Bug / If mismatched, raise to surface a hard bug.
            validated_server: dict = TypeAdapter(SMCPServerConfigDict).validate_python(cfg.model_dump(mode="json"), from_attributes=True)
            # 物化解析后 bundle_id（entry 携 bundle_id + name(display)，供 Agent tool→server 归属桥）
            validated_server["bundle_id"] = bundle_id
            servers[bundle_id] = validated_server

        inputs: list[MCPServerInput] = []
        for i in self.computer.inputs:
            validated_input: MCPServerInput = TypeAdapter(MCPServerInput).validate_python(i.model_dump(mode="json"), from_attributes=True)
            inputs.append(validated_input)

        # 端到端返回强校验（中英双语）/ End-to-end response strict validation (bilingual)
        ret = TypeAdapter(GetComputerConfigRet).validate_python({"servers": servers, "inputs": inputs})
        return ret

    async def on_get_resources(self, data: GetResourcesReq) -> GetResourcesRet | ErrorPayload:
        """
        透明转发指定 MCP Server 的 ``resources/list``（含 cursor 翻页）。
        Transparent forward of a MCP Server's ``resources/list`` (with cursor pagination).

        协议依据 / Protocol: a2c-smcp-protocol events.md#client:get_resources。
        Computer 不做 scheme / 元数据过滤、不做跨 Server 聚合；翻页由 Agent 通过 cursor 控制。
        Computer does no scheme/metadata filtering and no cross-server aggregation; pagination is Agent-driven.

        错误语义（flat ErrorPayload，经 Socket.IO ack 第一参回传，无嵌套 envelope）/
        Error semantics (flat ErrorPayload returned as the Socket.IO ack first arg, no nested envelope):
          - ``mcp_server`` 未注册 → ``4014 MCP Server Not Found``
          - 目标 Server 未声明 ``resources`` 能力 → ``4015 MCP Capability Not Supported``

        Args:
            data (GetResourcesReq): 请求数据（computer / mcp_server / 可选 cursor / req_id）。

        Returns:
            GetResourcesRet | ErrorPayload: 成功为资源页，失败为 flat ErrorPayload。
        """
        # Server 通过 session 保证请求来自同一 office，无需在此验证 agent 与 office_id 的关系
        # Server guarantees request is from same office via session, no need to validate agent vs office_id here
        if self.computer.name != data["computer"]:
            raise SMCPNamespaceError("计算机标识不匹配")
        mcp_server = data["mcp_server"]
        cursor = data.get("cursor")
        try:
            resources, next_cursor = await self.computer.get_resources(mcp_server, cursor)
        except MCPServerNotFoundError as e:
            logger.warning(f"client:get_resources 引用未注册 MCP Server '{mcp_server}' / unregistered server: {e}")
            return ErrorPayload(
                code=int(ErrorCode.MCP_SERVER_NOT_FOUND),
                message="MCP Server not registered",
                mcp_server=mcp_server,
            )
        except MCPCapabilityNotSupportedError as e:
            logger.warning(f"client:get_resources MCP Server '{mcp_server}' 未声明 resources 能力 / capability missing: {e}")
            return ErrorPayload(
                code=int(ErrorCode.MCP_CAPABILITY_NOT_SUPPORTED),
                message="MCP Server does not support 'resources' capability",
                mcp_server=mcp_server,
                capability="resources",
            )
        ret: GetResourcesRet = {
            "resources": [_to_a2c_resource(r) for r in resources],
            "req_id": data["req_id"],
        }
        if next_cursor is not None:
            ret["next_cursor"] = next_cursor
        return ret

    async def on_get_blob(self, data: GetBlobReq) -> GetBlobRet | ErrorPayload:
        """
        通用二进制拉取 / Generic binary pull.

        协议依据 / Protocol: a2c-smcp-protocol events.md#client:get_blob + blob-transfer.md。
        无状态、幂等、可并行不同 ``chunk_offset``——Computer 不保留任何 session / cursor。
        Stateless, idempotent, parallel-safe across ``chunk_offset``s — Computer keeps no
        session / cursor state.

        安全 / Security (blob-transfer.md §5.4):
          - 句柄解码 + kind 派发 → resolver 重施铸造通道边界校验，**绝不**信任句柄内容
          - 单块大小 clamp 到 ``BlobThresholds.chunk_max_bytes``，保证 base64+envelope ≤ Server buffer

        错误语义（flat ErrorPayload，无嵌套 envelope）/ Errors (flat ErrorPayload):
          - 4018 ``invalid_handle``：句柄格式非法 / 不识别 / kind 未注册 resolver
          - 4018 ``forbidden``：resolver 重施鉴权失败（如 skill orphan / 沙箱拒绝）
          - 4018 ``gone``：源已不可达（cid 已 GC / SKILL 卸载）
          - 4018 ``range``：``chunk_offset`` < 0 或 > ``total_size``

        Args:
            data (GetBlobReq): ``computer`` / ``blob_handle`` / 可选 ``chunk_offset`` / 可选 ``max_chunk_bytes`` / ``req_id``。

        Returns:
            GetBlobRet | ErrorPayload: 成功为切片块（``base64`` 编码），失败为 flat ErrorPayload。
        """
        # 核心逻辑抽取至模块级 ``_process_get_blob``，让同步 wire 测试 mock 与本 async wrapper
        # 复用同一份实现，避免"两份同源 handler 逻辑双份维护"的脆弱点（PR #52 fix-review）.
        # Core logic extracted to module-level ``_process_get_blob``, shared with sync wire mock,
        # eliminating dual-maintenance of equivalent sync/async handler bodies (PR #52 fix-review).
        return _process_get_blob(data, self.computer)

    async def on_put_blob(self, data: PutBlobReq) -> PutBlobRet | ErrorPayload:
        """
        上行写入单块处理 / Upload-write chunk handling（``client:put_blob``，v0.4.0 #196）.

        协议依据 / Protocol: a2c-smcp-protocol events.md §client:put_blob + blob-transfer.md §3/§7。
        与 ``client:get_blob`` 的**方向镜像**：``chunk_offset`` / ``eof`` 由 Agent 驱动推进，
        ``sha256`` / ``total_size`` 由 Agent 声明、Computer 校验。核心逻辑（声明校验 / in-order /
        ``.part`` + 增量 sha256 / 原子 rename / 有界会话）全部在
        :class:`a2c_smcp.computer.blob.upload.BlobUploadStore`。

        安全 / Security (blob-transfer.md §7):
          - 写入沙箱由写入原语强制：落盘目标 = settings ``landingRoot``（config-first，仅受信 scope
            可设，project 供给已被 ``TRUSTED_SCOPE_ONLY_FIELDS`` 过滤）；``landing_path`` 构造上
            严格落于 root 内，Agent 拿不到写任意路径的能力
          - fail-closed：landing root 未配置 / 不可写 → ``4019 forbidden``（零字节落盘）

        错误语义（flat ErrorPayload，无嵌套 envelope）/ Errors (flat ErrorPayload, §4019 开放枚举):
          - ``invalid_declaration``：首块声明非法（``total_size < 1`` / 字段缺失 / sha256 非 64-hex /
            后续块重复携带声明字段 / blob 非 base64）
          - ``too_large``：声明超 ``upload_max_bytes``（首块决断，零字节落盘）
          - ``busy``：并发在途会话达上限（Agent SHOULD 退避后从 0 重传）
          - ``invalid_upload``：``upload_id`` 未知 / 闲置超时作废
          - ``range``：``chunk_offset != 已收字节``；末块总量不闭合
          - ``integrity``：末块重算 sha256 与声明不符（丢弃 ``.part``，不返回 path）
          - ``forbidden`` / ``io_error``：沙箱不可写 / 落盘 IO 失败

        Args:
            data (PutBlobReq): ``computer`` / 可选 ``upload_id``（缺省首块）/ ``chunk_offset`` /
                ``eof`` / 首块声明（``total_size`` / ``sha256`` / 可选 ``name_hint``）/ ``blob`` / ``req_id``。

        Returns:
            PutBlobRet | ErrorPayload: 成功为块 ack（末块含 ``landing_path``），失败为 flat 4019。
        """
        # 与 on_get_blob 同款抽取：模块级纯函数核心（_process_put_blob），同步 wire 测试复用。
        # Same extraction as on_get_blob: module-level pure core shared with sync wire tests.
        return _process_put_blob(data, self.computer)

    async def on_get_skills(self, data: GetSkillsReq) -> GetSkillsRet:
        """
        SKILL 清单发现 / SKILL inventory discovery（``client:get_skills``）.

        协议依据 / Protocol: events.md#client:get_skills；skill.md §6（排除孤儿 / 不排序 / 不去重 / 不读 body）。
        从 Registry 取**活跃** SKILL 轻量元数据（``A2CSkillRef`` 列表，不含 SKILL.md body——body 由
        ``client:get_skill`` 拉取）；孤儿（source 消失）由 :meth:`Computer.get_skills` 经 ``active_refs`` 排除。
        Returns lightweight active SKILL refs (orphans excluded; no body). Body is fetched via get_skill.

        Args:
            data (GetSkillsReq): 请求数据（computer / req_id）。

        Returns:
            GetSkillsRet: ``skills`` 列表 + ``req_id``。
        """
        # Server 通过 session 保证请求来自同一 office；computer 标识仍需匹配（隔离不变量显式 raise）
        if self.computer.name != data["computer"]:
            raise SMCPNamespaceError("计算机标识不匹配")
        return GetSkillsRet(skills=self.computer.get_skills(), req_id=data["req_id"])

    async def on_get_skill(self, data: GetSkillReq) -> GetSkillRet | ErrorPayload:
        """
        SKILL 包内单资源渐进式披露 / Progressive disclosure of one in-package SKILL resource（``client:get_skill``）.

        协议依据 / Protocol: events.md#client:get_skill；skill.md §9（安全模型）；blob-transfer.md §5（生产者通道）。

        错误语义（flat ErrorPayload，经 Socket.IO ack 第一参回传，无嵌套 envelope）/ Errors:
          - ``4016`` ``SKILL_NAME_INVALID``（``details.name``）：``name`` 违反 SKILL name lexer；
          - ``4014``（复用 ``MCP_SERVER_NOT_FOUND``，``details.name``）：``name`` 合法但 Registry 未命中 / 孤儿；
          - ``4017`` ``SKILL_RESOURCE_NOT_ACCESSIBLE``（``details.reason`` / ``rel_path`` / ``total_size``）：
            ``rel_path`` 沙箱穿越 / ``.skillenv`` forbidden / not_found / too_large（too_large **不铸句柄**）。

        成功路径 / Success path：
          - 仅 **SKILL.md** 剥 frontmatter（消费字节 = 剥离后 body）；其它资源 = 原始字节；
          - 文本且 ``total_size`` ≤ 内联预算 → ``body`` 内联；否则铸 ``blob_handle`` 转 ``client:get_blob``；
          - ``body`` 与 ``blob_handle`` **恰一存在**（经 :class:`GetSkillRetModel` XOR 服务侧自校验）。

        Args:
            data (GetSkillReq): 请求数据（computer / name / 可选 rel_path / req_id）。

        Returns:
            GetSkillRet | ErrorPayload: 成功为单资源响应，失败为 flat ErrorPayload。
        """
        if self.computer.name != data["computer"]:
            raise SMCPNamespaceError("计算机标识不匹配")
        name = data["name"]
        rel_path = data.get("rel_path")

        # 1) name lexer → 4016（格式硬错）/ malformed name → 4016
        try:
            parse_skill_name(name)
        except SkillNameError as e:
            logger.warning(f"client:get_skill invalid skill name {name!r}: {e.reason}")
            return _skill_name_invalid(name)

        # 2) Registry 解析 → 4014（合法但未注册 / 孤儿）/ valid-but-absent → 4014
        ref = self.computer.get_skill_ref(name)
        if ref is None:
            logger.warning(f"client:get_skill name not in registry (unregistered or orphaned): {name!r}")
            return _skill_not_found(name)

        # 3) 沙箱解析 + 消费字节视图 → 4017（traversal/forbidden/not_found/too_large）
        try:
            view = self.computer.read_skill_resource(ref, rel_path)
        except SkillSandboxError as e:
            logger.warning(f"client:get_skill resource not accessible: name={name!r} reason={e.reason} rel={e.rel_path!r}")
            return _skill_resource_error(e)

        # 4) inline body vs blob_handle（文本且 ≤ 内联预算 → body；否则铸句柄）
        budget = self.computer.blob_thresholds.inline_budget
        body: str | None = None
        if view.is_text and view.total_size <= budget:
            try:
                body = view.read_all().decode("utf-8")
            except UnicodeDecodeError:
                # 声称文本但非 UTF-8 → 回退句柄路径（保守，避免内联破损字符串）
                logger.debug(f"client:get_skill {name!r} rel={view.rel_path!r} textual mime but not UTF-8; routing to blob_handle")
                body = None
        blob_handle: str | None = None
        if body is None:
            # too_large 已在 read_skill_resource 铸造期拦截（抛 4017），此处铸句柄安全
            blob_handle = encode_skill_handle(name, view.rel_path)

        # 5) 服务侧 XOR 自校验 + 规整 wire 形态（exclude_none 丢弃缺席的 body/blob_handle）
        model = GetSkillRetModel(
            name=name,
            rel_path=view.rel_path,
            mime_type=view.mime,
            total_size=view.total_size,
            sha256=view.sha256,
            req_id=data["req_id"],
            body=body,
            blob_handle=blob_handle,
        )
        return cast(GetSkillRet, model.model_dump(exclude_none=True))


def _skill_name_invalid(name: str) -> ErrorPayload:
    """``4016 SKILL_NAME_INVALID`` flat ErrorPayload（``details.name``，error-handling.md §4016）。"""
    return ErrorPayload(code=int(ErrorCode.SKILL_NAME_INVALID), message="Invalid skill name", details={"name": name})


def _skill_not_found(name: str) -> ErrorPayload:
    """``4014`` 复用 flat ErrorPayload：name 合法但 Registry 未命中（未注册 / 卸载 / 孤儿）。

    SKILL 通道复用 ``MCP_SERVER_NOT_FOUND`` 语义（error-handling.md §SKILL）；``name`` 经 ``details`` 下沉
    （SKILL 按 name 寻址、非 mcp_server(bundle_id)，故不平铺 ``mcp_server``）。
    """
    return ErrorPayload(code=int(ErrorCode.MCP_SERVER_NOT_FOUND), message="Skill not found", details={"name": name})


def _skill_resource_error(e: SkillSandboxError) -> ErrorPayload:
    """``4017 SKILL_RESOURCE_NOT_ACCESSIBLE`` flat ErrorPayload（``details.reason`` / ``rel_path`` / ``total_size``）。

    ``reason`` 为开放枚举（traversal/forbidden/not_found/too_large）；``total_size`` 仅 too_large 携带
    （error-handling.md §4017）。``.skillenv`` 等敏感文件存在/不存在**同 reason** forbidden（不泄漏存在性）。
    """
    details: dict[str, Any] = {"reason": e.reason, "rel_path": e.rel_path}
    if e.total_size is not None:
        details["total_size"] = e.total_size
    return ErrorPayload(code=int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE), message="Skill resource not accessible", details=details)


def _blob_error(*, reason: str) -> ErrorPayload:
    """构造 ``4018 Blob Not Accessible`` flat ErrorPayload，``reason`` 经 ``details`` 下沉。
    Build ``4018`` flat ErrorPayload with ``reason`` under ``details`` (per error-handling.md §4018).
    """
    return ErrorPayload(
        code=int(ErrorCode.BLOB_NOT_ACCESSIBLE),
        message="Blob not accessible",
        details={"reason": reason},
    )


def _process_get_blob(data: GetBlobReq, computer: Computer) -> GetBlobRet | ErrorPayload:
    """通用二进制拉取的纯同步核心逻辑 / Pure synchronous core logic for ``client:get_blob``.

    抽取自 :meth:`SMCPComputerClient.on_get_blob`（v0.2.1 #51 PR #52 fix-review）；让同步
    ``socketio.Client`` wire 测试 mock 与 async ``SMCPComputerClient`` 复用同一份实现，
    消除"两份同源 handler 逻辑双份维护"的脆弱点。``on_get_blob`` body 本是同步代码
    （无 ``await``），抽取为纯函数零行为变更。
    Extracted from :meth:`SMCPComputerClient.on_get_blob` so sync ``socketio.Client`` (test
    wire mock) and async ``SMCPComputerClient`` share one implementation. The async handler's
    body is pure-sync code (no ``await``); extraction is behavior-neutral.

    完整协议依据 / 安全 / 错误语义文档保留在 :meth:`SMCPComputerClient.on_get_blob`.
    Full protocol / security / error semantics docs are retained on the async wrapper.

    Args:
        data: ``GetBlobReq`` payload.
        computer: 持有 ``name`` / ``blob_thresholds`` / ``blob_resolvers`` 的 ``Computer`` 实例.

    Returns:
        ``GetBlobRet`` (成功) 或 ``ErrorPayload`` (4018 invalid_handle / forbidden / gone / range).

    Raises:
        SMCPNamespaceError: ``computer.name`` 与 ``data["computer"]`` 不匹配（防御纵深，理论不可达）.
    """
    # office/role 隔离：Server 已保证同房间路由，但 ``computer`` 标识仍需匹配
    # office/role isolation: Server guarantees same-room routing, but ``computer`` MUST match
    if computer.name != data["computer"]:
        raise SMCPNamespaceError("计算机标识不匹配")

    handle = data["blob_handle"]
    chunk_offset = data.get("chunk_offset", 0)
    max_chunk_bytes_req = data.get("max_chunk_bytes")
    max_chunk_bytes = computer.blob_thresholds.clamp_chunk(max_chunk_bytes_req)

    # 1) 解码句柄 → kind 派发 / Decode handle → kind dispatch
    try:
        kind, payload = decode_blob_handle(handle)
    except BlobHandleInvalidError as e:
        logger.warning(f"client:get_blob invalid handle: {e}")
        return _blob_error(reason="invalid_handle")

    resolver = computer.blob_resolvers.get(kind)
    if resolver is None:
        logger.warning(f"client:get_blob no resolver for kind={kind!r}")
        return _blob_error(reason="invalid_handle")

    # 2) 解析（resolver 内部重施铸造通道边界校验）/ Resolve (resolver re-applies channel auth)
    try:
        resolved = resolver.resolve(payload)
    except BlobHandleError as e:
        reason = getattr(e, "reason", "forbidden")
        logger.warning(f"client:get_blob resolver rejected handle: kind={kind}, reason={reason}, err={e}")
        return _blob_error(reason=reason)

    # 3) 范围校验 / Range check
    if not isinstance(chunk_offset, int) or chunk_offset < 0 or chunk_offset > resolved.total_size:
        logger.warning(
            f"client:get_blob range out of bounds: offset={chunk_offset}, total_size={resolved.total_size}",
        )
        return _blob_error(reason="range")

    # 4) 切片 + base64 编码（单块 ≤ clamp 后的 max_chunk_bytes）/ Slice + base64 (chunk ≤ clamp)
    # v0.2.1 #51: 走 lazy slice，仅单 chunk 入内存（不再触发全量 read_bytes）
    # Lazy slice (v0.2.1 #51): only one chunk in memory; no full-file read triggered.
    remaining = resolved.total_size - chunk_offset
    slice_len = min(max_chunk_bytes, remaining)
    chunk = resolved.slice(chunk_offset, slice_len)
    # eof 公式保留 len(chunk) 形式：与原 ``end == total_size`` 等价，对 OS 短读健壮.
    # Preserve len(chunk) form: equivalent to original ``end == total_size``, robust to short-reads.
    eof = chunk_offset + len(chunk) == resolved.total_size
    ret: GetBlobRet = {
        "blob_handle": handle,
        "mime_type": resolved.mime,
        "total_size": resolved.total_size,
        "sha256": resolved.sha256,
        "chunk_offset": chunk_offset,
        "eof": eof,
        "blob": base64.b64encode(chunk).decode("ascii"),
        "req_id": data["req_id"],
    }
    return ret


def _process_put_blob(data: PutBlobReq, computer: Computer) -> PutBlobRet | ErrorPayload:
    """上行写入单块的纯同步核心逻辑 / Pure synchronous core for one ``client:put_blob`` chunk.

    与 :func:`_process_get_blob` 同款抽取（v0.4.0 #196）：同步 ``socketio.Client`` wire 测试 mock
    与 async :meth:`SMCPComputerClient.on_put_blob` 复用同一份实现。会话/落盘细节全部委托
    :class:`a2c_smcp.computer.blob.upload.BlobUploadStore`（有界会话 MUST / landing 沙箱 / 4019 映射）。
    Same extraction pattern as ``_process_get_blob``; session & disk details live in BlobUploadStore.

    Args:
        data: ``PutBlobReq`` payload.
        computer: 持有 ``name`` / ``blob_upload_store`` 的 ``Computer`` 实例.

    Returns:
        ``PutBlobRet`` (成功；末块含 ``landing_path``) 或 ``ErrorPayload`` (4019).

    Raises:
        SMCPNamespaceError: ``computer.name`` 与 ``data["computer"]`` 不匹配（防御纵深，理论不可达）.
    """
    # office/role 隔离：Server 已保证同房间路由，但 ``computer`` 标识仍需匹配
    # office/role isolation: Server guarantees same-room routing, but ``computer`` MUST match
    if computer.name != data["computer"]:
        raise SMCPNamespaceError("计算机标识不匹配")
    return computer.blob_upload_store.handle_chunk(data)
