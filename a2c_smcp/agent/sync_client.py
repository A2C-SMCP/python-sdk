"""
* 文件名: sync_client
* 作者: JQQ
* 创建日期: 2025/9/30
* 最后修改日期: 2025/9/30
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, mcp
* 描述: 同步Agent客户端实现 / Synchronous Agent client implementation
"""

import threading
from collections.abc import Mapping
from typing import Any, cast

from mcp.types import CallToolResult, TextContent
from socketio import Client

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.agent import _blob_sideband as _sb
from a2c_smcp.agent._cancel import CancelSendGate
from a2c_smcp.agent.auth import AgentAuthProvider
from a2c_smcp.agent.base import TOOL_CALL_TIMEOUT_ERRORS, BaseAgentSyncClient
from a2c_smcp.agent.errors import SMCPProtocolError, raise_for_error_payload
from a2c_smcp.agent.types import AgentEventHandler, CancelSignal
from a2c_smcp.smcp import (
    ENTER_OFFICE_NOTIFICATION,
    GET_BLOB_EVENT,
    GET_CONFIG_EVENT,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_SKILL_EVENT,
    GET_SKILLS_EVENT,
    GET_TOOLS_EVENT,
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_NOTIFICATION,
    LIST_ROOM_EVENT,
    PUT_BLOB_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
    UPDATE_SKILLS_NOTIFICATION,
    UPDATE_TOOL_LIST_NOTIFICATION,
    EnterOfficeNotification,
    EnterOfficeReq,
    GetBlobRet,
    GetComputerConfigRet,
    GetDeskTopRet,
    GetResourcesRet,
    GetSkillRet,
    GetSkillsRet,
    GetToolsRet,
    LeaveOfficeNotification,
    ListRoomReq,
    SessionInfo,
    UpdateMCPConfigNotification,
    UpdateToolListNotification,
)
from a2c_smcp.utils.blob import PutBlobResult, drain_blob_sync, pump_blob_sync
from a2c_smcp.utils.handshake import (
    DEFAULT_HANDSHAKE_TRANSPORTS,
    HANDSHAKE_CONNECT_ERRORS,
    apply_polling_first_guard,
    build_handshake_url,
    build_protocol_version_error,
    extract_4008_payload,
)
from a2c_smcp.utils.logger import ContextLogger, get_logger
from a2c_smcp.utils.mime import is_text_mime
from a2c_smcp.utils.office import (
    OFFICE_JOIN_TIMEOUT,
    log_join_rejection,
    parse_join_ack,
    resolve_join_failure,
)

logger = get_logger("agent")


class SMCPAgentClient(Client, BaseAgentSyncClient):
    """
    SMCP协议的同步Agent客户端实现
    Synchronous SMCP protocol Agent client implementation

    注意：当前Client操作是非线程安全的，不可以在多线程环境下使用
    Note: Current Client operations are not thread-safe, cannot be used in multi-threaded environments

    **线程模型（据实）/ Thread model, as it actually is**：engineio 对每个 MESSAGE 包**新建一个
    daemon 线程**派发（``_trigger_event(..., run_async=True)`` ⇒ ``start_background_task``），
    ``connect`` / ``disconnect`` 则是**内联**触发。故**事件处理器与用户主线程天然并发**——这正是
    #218 给 office 操作补 ``_office_op_lock`` 的理由（此前 sync 侧无任何 wire 互斥，重放的
    check-then-send 与显式 join 可交错 ⇒ 净效果「客户端以为退房、服务端仍在房」）。
    Handlers run on per-packet threads and the lifecycle hooks are dispatched inline, so handlers
    race the user thread by construction — hence the office op-lock added in #218.

    已备案的**跨线程例外**共两处（其余仍不线程安全）：

    - **#203/#218 自动回房**（``_rejoin_office``）：跑在独立 daemon 线程，只做"发出 join + 等 ACK"
      这一件事——generation **双检（取到 op-lock 后 / 应用结果前）**保证陈旧结果不落状态，且与显式
      join/leave 共用 ``_office_op_lock`` 互斥。可接受的最坏后果：等待期间连接被拆时，该线程吃满
      ``OFFICE_JOIN_TIMEOUT``（10s）后退出（socketio ``call()`` 不会在断链时快速失败）。
    - **#209 取消 watcher**（``_cancel.SyncCancelWatcher``）：仅在调用方传入 ``cancel`` 时启动，只做
      "轮询 ``is_set()`` + 发一次 ``server:tool_call_cancel``"，**不接触**本类任何可变状态，且在
      ``emit_tool_call`` 返回前被 join 收口（``threading.enumerate()`` 可断言无线程残留）。

    **残余风险（已披露，非本类可控）**：python-socketio 自陈 ``call`` / ``emit`` **非线程安全**
    （多线程同时 emit 会让多包消息乱序）⇒ ``_office_op_lock`` 只串行化**office 操作**，其它用户 RPC
    与回房仍可能交错；跨线程调用本类的其它方法依旧不在支持范围内。
    / Residual: socketio's own ``call``/``emit`` are not thread-safe; the op-lock serializes office
    operations only.

    / Two sanctioned cross-thread exceptions: the #203 office replay and the #209 cancel watcher
    (started only when ``cancel`` is given; touches no mutable client state; joined before return).
    """

    def __init__(
        self,
        auth_provider: AgentAuthProvider,
        event_handler: AgentEventHandler | None = None,
        *args: Any,
        namespace: str = SMCP_NAMESPACE,
        **kwargs: Any,
    ) -> None:
        """
        初始化同步SMCP Agent客户端
        Initialize synchronous SMCP Agent client

        Args:
            auth_provider (AgentAuthProvider): 认证提供者 / Authentication provider
            event_handler (AgentEventHandler | None): 事件处理器 / Event handler
            namespace (str): Socket.IO命名空间，默认 ``/smcp``。
                事件处理器注册与后续 emit/call 均使用此值 /
                Socket.IO namespace, default ``/smcp``. Used for handler registration
                and all subsequent emit/call sites.
            *args: Client构造参数 / Client constructor arguments
            **kwargs: Client构造参数 / Client constructor arguments
        """
        # 初始化基类
        # Initialize base classes
        Client.__init__(self, *args, **kwargs)
        BaseAgentSyncClient.__init__(self, auth_provider, event_handler)

        # 实例级命名空间 / Per-instance namespace
        self._namespace = namespace

        # 注册事件处理器
        # Register event handlers
        self.register_event_handlers()

    @property
    def namespace(self) -> str:
        """
        返回当前实例使用的 Socket.IO 命名空间
        Return the Socket.IO namespace used by this instance
        """
        return self._namespace

    def emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        """
        发送事件，包含事件验证逻辑
        Send event with event validation logic

        Args:
            event (str): 事件名称 / Event name
            data (Any): 事件数据 / Event data
            namespace (Optional[str]): 命名空间 / Namespace
            callback (Any): 回调函数 / Callback function
        """
        # 验证事件合法性
        # Validate event legality
        self.validate_emit_event(event)

        # 调用 Client 的 emit 方法
        # Call Client's emit method
        Client.emit(self, event, data, namespace, callback)

    def call(self, event: str, data: Any = None, namespace: str | None = None, timeout: int = 60) -> Any:
        """
        调用事件并等待响应
        Call event and wait for response

        Args:
            event (str): 事件名称 / Event name
            data (Any): 事件数据 / Event data
            namespace (Optional[str]): 命名空间 / Namespace
            timeout (int): 超时时间 / Timeout duration

        Returns:
            Any: 响应数据 / Response data
        """
        # 验证事件合法性
        # Validate event legality
        self.validate_emit_event(event)

        # 调用 Client 的 call 方法
        # Call Client's call method
        return Client.call(self, event, data, namespace, timeout)

    def connect_to_server(
        self,
        url: str,
        namespace: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        连接到SMCP服务器
        Connect to SMCP server

        Args:
            url (str): 服务器URL / Server URL
            namespace (str | None): 命名空间；不传则沿用构造器传入的实例命名空间。
                若显式传入新值，会同步更新实例命名空间并重新注册事件处理器，确保事件
                订阅在正确的命名空间生效 /
                Namespace; fall back to the instance namespace if omitted. When a new
                value is provided explicitly, the instance namespace is updated and event
                handlers are re-registered so that subscriptions bind to the right namespace.
            **kwargs: 连接参数 / Connection parameters
        """
        # 若显式指定命名空间且与实例当前值不同，则更新并重新注册事件处理器
        # If explicit namespace differs from instance, update and re-register handlers
        if namespace is not None and namespace != self._namespace:
            self._namespace = namespace
            self.register_event_handlers()

        # 获取认证信息
        # Get authentication info
        auth_data = self.auth_provider.get_connection_auth()
        headers = self.auth_provider.get_connection_headers()

        # 协议 MUST：自动从 PROTOCOL_VERSION 常量拼接 a2c_version（保留调用方既有 query）
        # Protocol MUST: auto-append a2c_version from the PROTOCOL_VERSION constant (preserving caller query)
        handshake_url = build_handshake_url(url, PROTOCOL_VERSION)

        # 合并连接参数；transports 默认 polling 优先以保 4008 HTTP body 可读（调用方可覆盖）
        # Merge connect params; default transports polling-first so the 4008 HTTP body is readable (caller may override)
        connect_kwargs = {
            "auth": auth_data,
            "headers": headers,
            "namespaces": [self._namespace],
            "transports": DEFAULT_HANDSHAKE_TRANSPORTS,
            **kwargs,
        }

        # 协议 §1 polling-first MUST 护栏（统一接线，详见 handshake.apply_polling_first_guard）
        # Protocol §1 polling-first MUST guard (unified wiring)
        connect_kwargs["transports"] = apply_polling_first_guard(connect_kwargs.get("transports"), logger)

        logger.info(f"Connecting to SMCP server at {url} (a2c_version={PROTOCOL_VERSION})")
        try:
            self.connect(handshake_url, **connect_kwargs)
        except HANDSHAKE_CONNECT_ERRORS as e:
            payload = extract_4008_payload(e)
            if payload is None:
                # 非协议版本错误：保持原异常 / not a version error: preserve the original exception
                raise
            # 协议 §4 MUST：先主动断开再抛异常，防止底层库自动重连触发 4008 死循环
            # Protocol §4 MUST: proactively disconnect before raising (anti reconnect-loop)
            self.disconnect()
            raise build_protocol_version_error(payload) from e
        logger.info("Connected to SMCP server successfully")

    def emit_tool_call(
        self,
        computer: str,
        tool_name: str,
        params: dict,
        timeout: int,
        cancel: CancelSignal | None = None,
    ) -> CallToolResult:
        """
        发起SMCP工具调用
        Initiate SMCP tool call

        #209 取消入口 / Cancel entry (#209):
            宿主传入 ``cancel`` 即可中断一次**在途**工具调用（如用户点「停止」）。语义由协议
            `events.md §server:tool_call_cancel`（protocol#58 / PR#60）冻结：

            1. **仅投递信号** —— 信号置位后发出 ``server:tool_call_cancel``，随后**继续等待原
               ``client:tool_call`` 的 ack**，以该 ack 为终态；**不**本地提早返回。Computer 未中断时
               须**等满本次调用自身的 ``timeout``**（协议不保证提前返回）。本方法用后台 daemon 线程
               轮询信号，**不会**去唤醒阻塞中的 ``socketio.Client.call``。
            2. **不冒充 Computer** —— **不**本地合成 ``meta.a2c_cancelled``（该标记仅由 Computer 产出）。
            3. **如实透出** —— 终态按 ack 的结果级 ``meta`` 分类，用
               :func:`~a2c_smcp.agent.types.classify_tool_call_outcome` 读取；
               **不得**依据「本端已发出取消信号」改写终态。

            故「已请求取消」 **不是**「已取消」的充分条件：取消是协作式的，``req_id`` 命中不到时
            Computer 静默忽略，该次调用可能照常返回正常结果，或（未中断且自身超时到点）以
            ``meta.a2c_timeout`` 收尾 —— 后者即「取消未被确认」。

            ``cancel`` 契约见 :class:`~a2c_smcp.agent.types.CancelSignal`：**只要求** ``is_set()``，
            实现一律轮询（不假定存在可 await 的 ``wait``）。``threading.Event`` 或任意仅含
            ``is_set()`` 的对象可直接从**另一线程**置位。

            Cancel entry: pass ``cancel`` to interrupt an in-flight tool call. Only the signal is
            delivered — the original ack remains the sole terminal-state source.

        Args:
            computer (str): 远程计算机名称 / Remote computer name
            tool_name (str): 工具名称 / Tool name
            params (dict): 工具调用参数 / Tool call parameters
            timeout (int): 超时时间 / Timeout duration
            cancel (CancelSignal | None): 宿主取消信号（**只要求** ``is_set()``）/ host cancel signal.

        Returns:
            CallToolResult: MCP协议工具调用结果 / MCP protocol tool call result

        Raises:
            TypeError: ``cancel`` 非空但不满足 :class:`CancelSignal`（无 ``is_set()``）。
                这是本方法唯一的非返回路径（调用方传参错误，不做静默降级）。
        """
        req = self.create_tool_call_request(computer, tool_name, params, timeout)
        ctx = ContextLogger(logger, {"computer": computer, "tool": tool_name, "req_id": req["req_id"]})

        # #209：宿主信号 → 至多一次 server:tool_call_cancel 广播。watcher 只投递信号，不参与阻塞等待。
        gate = CancelSendGate()
        watcher = self._start_cancel_watcher(cancel, gate, req["req_id"], self._namespace)

        try:
            ctx.debug("Calling tool")
            res = self.call(TOOL_CALL_EVENT, req, timeout=timeout, namespace=self._namespace)
            # 协议级错误（如 #99 目标 Computer 不存在 → flat ErrorPayload(404)）→ 抛 SMCPProtocolError，
            # 由下方专门分支转成干净 isError 结果（与 6 个 get_* 方法的 raise_for_error_payload 调用点一致）。
            # Protocol errors (e.g. #99 computer-not-found → flat ErrorPayload(404)) → raise, handled below.
            raise_for_error_payload(res)
            # v0.2.1：返回前 drain content items 的 _meta.a2c_blob_handle / drain binary sideband pre-return
            res = self._resolve_tool_call_binary_sideband(res, computer)
            return CallToolResult.model_validate(res, by_name=True)

        except TOOL_CALL_TIMEOUT_ERRORS:
            # 发送取消请求（幂等：宿主信号已广播过则不再重复——闸门保证至多一次）
            # Send cancel request (idempotent: the gate allows at most one broadcast per call)
            if gate.try_claim():
                self._broadcast_tool_call_cancel(req["req_id"], self._namespace)
            return self.handle_tool_call_timeout(req["req_id"])

        except SMCPProtocolError as e:
            # 协议级错误（如 #99 的 404 Computer 不存在）→ 干净 isError 结果；不透传 details（防泄露，见 errors.py），
            # 不打 error 级栈噪声；保持 emit_tool_call「始终返回 CallToolResult、不抛出」契约。
            # Protocol error (e.g. #99 404 computer-not-found) → clean isError result; no details leak, no stack noise.
            ctx.warning(f"Tool call protocol error: {e}")
            return CallToolResult(
                content=[TextContent(text=f"工具调用失败 / Tool call failed: {e}", type="text")],
                isError=True,
            )

        except Exception as e:
            ctx.error(f"Tool call failed: {e}", exc_info=True)
            return CallToolResult(
                content=[TextContent(text=f"工具调用失败 / Tool call failed: {str(e)}", type="text")],
                isError=True,
            )

        finally:
            # ``stop()`` 内部先「关门」（抢占广播权）再叫停 —— 单一收口缝，杜绝「已返回后迟到广播」。
            # ``stop()`` seals the gate then tears down: the single seam guaranteeing no late broadcast.
            if watcher is not None:
                watcher.stop()

    def get_tools_from_computer(self, computer: str, timeout: int = 20) -> GetToolsRet:
        """
        从指定计算机获取工具列表
        Get tools list from specified computer

        Args:
            computer (str): 计算机名称 / Computer Name
            timeout (int): 超时时间 / Timeout duration

        Returns:
            GetToolsRet: 工具列表响应 / Tools list response
        """
        req = self.create_get_tools_request(computer)

        try:
            logger.debug(f"Getting tools from computer {computer}")
            response = self.call(GET_TOOLS_EVENT, req, namespace=self._namespace, timeout=timeout)

            # 验证响应
            # Validate response
            if response.get("req_id") != req["req_id"]:
                raise ValueError("Invalid response with mismatched req_id")

            return GetToolsRet(tools=response.get("tools", []), req_id=response["req_id"])

        except Exception as e:
            logger.error(f"Failed to get tools from computer {computer}: {e}", exc_info=True)
            raise

    def get_config_from_computer(self, computer: str, timeout: int = 20) -> GetComputerConfigRet:
        """
        从指定计算机获取 MCP 配置（servers 键 = bundle_id，协议 #18）（#149，sync 镜像）
        Get MCP config from the specified computer (servers keyed by bundle_id) (#149, sync mirror)

        Args:
            computer (str): 计算机名称 / Computer Name
            timeout (int): 超时时间 / Timeout duration

        Returns:
            GetComputerConfigRet: 配置响应（``servers[bundle_id]`` + 可选 ``inputs``）/ Config response.

        备注 / Notes:
            ``GetComputerConfigRet`` 无 ``req_id`` 字段、``on_get_config`` 也不 echo req_id，故**不做** req_id 校验；
            ``on_get_config`` 无 flat ErrorPayload 路径，故**不需** raise_for_error_payload。与 async 无语义差异。
        """
        req = self.create_get_config_request(computer)
        logger.debug(f"Getting config from computer {computer}")
        response = self.call(GET_CONFIG_EVENT, req, namespace=self._namespace, timeout=timeout)
        ret: GetComputerConfigRet = {"servers": response.get("servers", {})}
        if response.get("inputs") is not None:
            ret["inputs"] = response["inputs"]
        return ret

    def register_event_handlers(self) -> None:
        """
        注册SMCP协议事件处理器
        Register SMCP protocol event handlers
        """
        self.on(ENTER_OFFICE_NOTIFICATION, self._on_computer_enter_office, namespace=self._namespace)
        self.on(LEAVE_OFFICE_NOTIFICATION, self._on_computer_leave_office, namespace=self._namespace)
        self.on(UPDATE_CONFIG_NOTIFICATION, self._on_computer_update_config, namespace=self._namespace)
        self.on(UPDATE_DESKTOP_NOTIFICATION, self._on_desktop_updated, namespace=self._namespace)
        # v0.2.1 SKILL 集合更新自动重拉 / v0.2.1 auto-refresh on SKILL set change
        self.on(UPDATE_SKILLS_NOTIFICATION, self._on_skills_updated, namespace=self._namespace)
        # #127 MCP 运行期工具集变化自动重拉 / #127 auto-refresh on runtime tool-set change
        self.on(UPDATE_TOOL_LIST_NOTIFICATION, self._on_computer_update_tool_list, namespace=self._namespace)
        # #203 引擎级 namespace 生命周期钩子（自动重连后回房）。两个 handler 刻意写成**同步函数**：
        # engineio 以 run_async=False 内联触发 'disconnect'，在其中做阻塞调用会卡住拆链。
        # Engine-level namespace hooks (#203), deliberately sync — 'disconnect' is dispatched inline.
        self.on("connect", self._on_namespace_connect, namespace=self._namespace)
        self.on("disconnect", self._on_namespace_disconnect, namespace=self._namespace)
        self.on("__disconnect_final", self._on_namespace_disconnect_final, namespace=self._namespace)

    # ── #203 Office 成员关系：自动回房（与异步侧 / Computer 侧同构）──────────────────────────

    def _on_namespace_connect(self) -> None:
        """
        namespace (重)连接钩子：desired 仍在则起 daemon 线程回房。**不得阻塞**。

        Namespace (re)connect hook: spawn a daemon thread to replay the join. Must not block —
        本钩子内联在读循环的分发线程上（engineio 对 MESSAGE 包 run_async 派发，socketio 的
        ``_handle_connect`` 在其中 await 本钩子），而在其中同步 ``call`` 等 ACK 会把连接完成信号
        （``connect()`` 的 wait_timeout）一并拖住。
        Runs on the read-loop dispatch thread, where a blocking ack wait would also stall the
        connect-completion signal.

        会话边界（#217 裁决 5）：推进 generation 与会话纪元、作废随会话销毁的已确认成员关系；意图保留。
        Begins a new office session: generation + session epoch advance, confirmed membership is dropped.

        守卫用 ``self._namespace in self.namespaces`` 而非 ``self.connected``（后者要等
        ``connect()`` 返回后才置位）。/ Guard on the registered namespace, not ``self.connected``.
        """
        self._begin_office_session(drop_desired=False)
        desired = self._desired_office
        if desired is None:
            return
        generation = self._office_generation
        thread = threading.Thread(
            target=self._rejoin_office,
            args=(desired, generation),
            name="a2c-agent-office-rejoin",
            daemon=True,
        )
        self._office_rejoin_thread = thread
        thread.start()

    def _on_namespace_disconnect(self, reason: str | None = None) -> None:
        """namespace 断连钩子：按原因决定回房意图去留。**不得阻塞**（内联触发路径）。

        仅"传输中断且底层会自动重连"保留意图；手工断开 / 服务端踢出 / 未启用重连一律清空。
        两种分支都走会话边界（清已确认房号：成员关系属于会话）。
        """
        if reason == self.reason.TRANSPORT_ERROR and self.reconnection:
            self._begin_office_session(drop_desired=False)
            return
        self._begin_office_session(drop_desired=True)

    def _on_namespace_disconnect_final(self) -> None:
        """重连彻底放弃 → 会话边界 + 清空回房意图。"""
        self._begin_office_session(drop_desired=True)

    def _rejoin_office(self, desired: tuple[str, str], generation: int) -> None:
        """
        自动回房（daemon 线程）：重放 ``server:join_office`` 并校验 ACK。

        Replay ``server:join_office`` on the fresh namespace and validate its ack.

        同步侧无法取消线程，故以 generation **双检**（**取到 op-lock 后**、应用结果前）作废陈旧结果
        ——检查必须在锁内：线程不可取消，锁外检查会让陈旧线程在更新的 JOIN/LEAVE 之后仍把包发上线。
        The staleness check runs *inside* the op-lock: threads are not cancellable, so a check made
        outside could still emit the packet after a newer operation has landed.

        失败效应与日志走与显式 ``join_office`` **同一张表、同一个产出者**（#217 同态口径）；**不再**
        借 ``_drop_desired_office()`` 清状态（那次额外 generation bump 在补了 op-lock 后失去必要，
        且会让两侧 generation 序列分叉）。单次尝试（镜像 rust-sdk#204）。
        """
        session = self._office_session
        with self._office_op_lock:
            if generation != self._office_generation or self._desired_office != desired:
                return  # 已被更新的操作接管 / superseded
            if self._namespace not in self.namespaces:
                return  # 连接又断了：交给下一次 connect 钩子
            office_id, agent_name = desired
            try:
                result = self.call(
                    JOIN_OFFICE_EVENT,
                    EnterOfficeReq(office_id=office_id, role="agent", name=agent_name),
                    namespace=self._namespace,
                    timeout=OFFICE_JOIN_TIMEOUT,
                )
            except Exception as e:
                # 陈旧性检查与效应施加必须在**同一次**状态锁获取内（隔离审查报出的竞态）：锁外比对会被
                # GIL 抢占 —— 并发 `join(B)` 的入口段（bump generation + 写 desired）落在窗口里时，
                # 本陈旧回放会把它的意图与已确认成员关系一并清成 None。/ Staleness check and effect
                # application share one acquisition; a lock-free compare can be preempted in between.
                with self._office_state_lock:
                    if generation != self._office_generation or self._desired_office != desired:
                        return
                    effect = resolve_join_failure(None, confirmed=self._confirmed_office)
                    self._desired_office, self._confirmed_office = effect.desired, effect.confirmed
                logger.error(f"自动重新加入 Office 失败: {office_id} - {e}")
                return
            verdict = parse_join_ack(result)
            with self._office_state_lock:
                if verdict.ok and session == self._office_session:
                    # 成功是对服务端**事实**的陈述 ⇒ 无视操作抢占（#213），受会话纪元约束（S11）
                    self._confirmed_office = desired
                if generation != self._office_generation or self._desired_office != desired:
                    return  # 结果已作废：除上面「已成事实」的落账外，不得改动 desired / 日志
                if verdict.ok:
                    logger.info(f"已自动重新加入 Office: {office_id}")
                    return
                confirmed_before = self._confirmed_office
                effect = resolve_join_failure(verdict, confirmed=confirmed_before)
                self._desired_office, self._confirmed_office = effect.desired, effect.confirmed
            # 带协议码：4101/4105 是重连撞旧会话的**瞬态**冲突（#212 将对其做有界退避重试），
            # 无码则是「未获裁决」（形状不认识 / 空响应）。Transient vs indeterminate, by code.
            log_join_rejection(office_id, verdict, confirmed_name=confirmed_before[1] if confirmed_before else None)

    def _on_computer_enter_office(self, data: EnterOfficeNotification) -> None:
        """
        处理Computer加入办公室事件的内部方法
        Internal method to handle Computer enter office event
        """
        try:
            # 使用父类的处理方法
            # Use parent class handling method
            self.handle_computer_enter_office(data)

            # 自动获取工具列表
            # Automatically get tools list
            computer = self.validate_office_data(data)
            tools_response = self.get_tools_from_computer(computer)
            self.process_tools_response(tools_response, computer)

        except Exception as e:
            logger.error(f"Error in _on_computer_enter_office: {e}", exc_info=True)

    def _on_computer_leave_office(self, data: LeaveOfficeNotification) -> None:
        """
        处理Computer离开办公室事件的内部方法
        Internal method to handle Computer leave office event
        """
        # 使用父类的处理方法
        # Use parent class handling method
        self.handle_computer_leave_office(data)

    def _on_computer_update_config(self, data: UpdateMCPConfigNotification) -> None:
        """
        处理Computer更新配置事件的内部方法
        Internal method to handle Computer update config event
        """
        try:
            # 使用父类的处理方法
            # Use parent class handling method
            self.handle_computer_update_config(data)

            # 重新获取工具列表
            # Re-get tools list
            computer = data["computer"]
            tools_response = self.get_tools_from_computer(computer)
            self.process_tools_response(tools_response, computer)

        except Exception as e:
            logger.error(f"Error in _on_computer_update_config: {e}", exc_info=True)

    def _on_computer_update_tool_list(self, data: UpdateToolListNotification) -> None:
        """
        处理Computer工具列表更新事件的内部方法（#127 sync mirror）
        Internal handler for a Computer's tool-list-changed event (#127, sync mirror)

        镜像 ``_on_computer_update_config`` 的三段式：预清回调 ``on_computer_update_tool_list`` → 全量回拉
        ``client:get_tools`` → ``process_tools_response`` 触发 ``on_tools_received`` 重加。
        """
        try:
            # 使用父类的处理方法（预清回调）
            # Use parent class handling method (pre-clean hook)
            self.handle_computer_update_tool_list(data)

            # 重新获取工具列表
            # Re-get tools list
            computer = data["computer"]
            tools_response = self.get_tools_from_computer(computer)
            self.process_tools_response(tools_response, computer)

        except Exception as e:
            logger.error(f"Error in _on_computer_update_tool_list: {e}", exc_info=True)

    def get_desktop_from_computer(
        self,
        computer: str,
        *,
        size: int | None = None,
        window: str | None = None,
        timeout: int = 20,
    ) -> GetDeskTopRet:
        """
        从指定计算机获取桌面信息
        Get desktop from specified computer

        Args:
            computer (str): 计算机名称 / Computer Name
            size (int | None): 限制窗口数量 / Limit windows count
            window (str | None): 指定窗口URI / Specific window URI
            timeout (int): 超时时间 / Timeout
        """
        req = self.create_get_desktop_request(computer, size=size, window=window)
        logger.debug(f"Getting desktop from computer {computer}, size={size}, window={window}")
        response = self.call(GET_DESKTOP_EVENT, req, namespace=self._namespace, timeout=timeout)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for desktop")
        return GetDeskTopRet(desktops=response.get("desktops", []), req_id=response["req_id"])

    def get_resources(
        self,
        computer: str,
        mcp_server: str,
        cursor: str | None = None,
        timeout: int = 20,
    ) -> GetResourcesRet:
        """
        同步：透明转发获取指定 Computer 上某 MCP Server 的资源列表（含 cursor 翻页）。
        Sync: transparently get a MCP Server's resource list on the target Computer (with cursor pagination).

        SDK **不**自动遍历翻页——cursor 由调用方控制：首次传 ``None``，响应含 ``next_cursor``
        时由调用方决定是否带该 cursor 继续请求（协议指南 §5.3 第 3 点）。
        The SDK does **not** auto-paginate — the cursor is caller-controlled (protocol guide §5.3 #3).

        Args:
            computer (str): 目标 Computer 名称 / Target Computer name
            mcp_server (str): 目标 MCP Server 的 bundle_id（= get_config servers 字典 key，协议 #18）/ Target server bundle_id
            cursor (str | None): MCP 标准翻页游标；首次传 None / MCP pagination cursor; None for first page
            timeout (int): 超时时间（秒）/ Timeout in seconds

        Returns:
            GetResourcesRet: 资源页（含可选 next_cursor）/ Resource page (with optional next_cursor)

        Raises:
            SMCPProtocolError: ``4014`` MCP Server 未注册 / ``4015`` 未声明 ``resources`` 能力
            ValueError: 响应 ``req_id`` 不匹配 / mismatched response ``req_id``
        """
        req = self.create_get_resources_request(computer, mcp_server, cursor)
        logger.debug(f"Getting resources from computer {computer}, mcp_server={mcp_server}, cursor={cursor}")
        response = self.call(GET_RESOURCES_EVENT, req, namespace=self._namespace, timeout=timeout)
        # flat ErrorPayload（4014 / 4015）→ 抛 SMCPProtocolError / raise SMCPProtocolError on flat ErrorPayload
        raise_for_error_payload(response)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for resources")
        ret: GetResourcesRet = {
            "resources": response.get("resources", []),
            "req_id": response["req_id"],
        }
        if response.get("next_cursor") is not None:
            ret["next_cursor"] = response["next_cursor"]
        return ret

    def _on_desktop_updated(self, data: dict) -> None:
        """
        处理桌面更新通知：默认自动拉取一次桌面。
        Handle desktop updated notification: automatically fetch desktop once.
        """
        try:
            computer = data.get("computer")
            if not computer:
                logger.warning("UPDATE_DESKTOP_NOTIFICATION missing 'computer'")
                return
            ret = self.get_desktop_from_computer(computer)
            self.process_desktop_response(ret, computer)
        except Exception as e:
            logger.error(f"Error handling desktop updated notification: {e}", exc_info=True)

    def get_skills(self, computer: str, timeout: int = 20) -> GetSkillsRet:
        """同步获取目标 Computer 的 SKILL 清单（v0.2.1 sync mirror of async ``get_skills``）."""
        req = self.create_get_skills_request(computer)
        logger.debug(f"Getting skills from computer {computer}")
        response = self.call(GET_SKILLS_EVENT, req, namespace=self._namespace, timeout=timeout)
        raise_for_error_payload(response)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for skills")
        ret: GetSkillsRet = {"skills": response.get("skills", []), "req_id": response["req_id"]}
        return ret

    def get_skill(
        self,
        computer: str,
        name: str,
        rel_path: str | None = None,
        timeout: int = 30,
    ) -> GetSkillRet:
        """同步获取 SKILL 包内单个资源（v0.2.1 sync mirror）；body 与 blob_handle 分支自动处理.

        文本 MIME 的 blob_handle 自动 :func:`drain_blob_sync` 回填 body；二进制保留 blob_handle.
        """
        req = self.create_get_skill_request(computer, name, rel_path)
        logger.debug(f"Getting skill {name!r} rel_path={rel_path!r} from computer {computer}")
        response = self.call(GET_SKILL_EVENT, req, namespace=self._namespace, timeout=timeout)
        raise_for_error_payload(response)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for skill")
        ret: GetSkillRet = dict(response)  # type: ignore[assignment]
        mime_type = str(response.get("mime_type", ""))
        if "blob_handle" in response and "body" not in response and is_text_mime(mime_type):
            payload, _ = drain_blob_sync(
                self._make_blob_call(),
                computer,
                response["blob_handle"],
            )
            decoded = _sb.decode_text_body(payload)
            if decoded is not None:
                ret["body"] = decoded
                ret.pop("blob_handle", None)
            else:
                logger.warning(f"get_skill text body decode failed for name={name!r}; keeping blob_handle")
        return ret

    def get_blob(
        self,
        computer: str,
        blob_handle: str,
        *,
        chunk_offset: int = 0,
        max_chunk_bytes: int | None = None,
        timeout: int = 30,
    ) -> GetBlobRet:
        """同步通用二进制拉取单块入口（sync mirror of async ``get_blob``，低层 API）."""
        req = self.create_get_blob_request(computer, blob_handle, chunk_offset, max_chunk_bytes)
        logger.debug(f"Getting blob from computer {computer} offset={chunk_offset}")
        response = self.call(GET_BLOB_EVENT, req, namespace=self._namespace, timeout=timeout)
        raise_for_error_payload(response)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for blob")
        return GetBlobRet(**response)

    def _make_blob_call(self) -> Any:
        """构造 :func:`drain_blob_sync` 的 ``call`` 适配器（sync mirror of async ``_make_blob_call``）."""

        def _call(computer: str, blob_handle: str, chunk_offset: int, max_chunk_bytes: int) -> dict:
            req = self.create_get_blob_request(computer, blob_handle, chunk_offset, max_chunk_bytes)
            ack = self.call(GET_BLOB_EVENT, req, namespace=self._namespace)
            return cast(dict, ack)

        return _call

    def put_blob(
        self,
        computer: str,
        data: bytes,
        *,
        name_hint: str | None = None,
        chunk_size: int | None = None,
        timeout: int = 30,
    ) -> PutBlobResult:
        """同步上行落盘（sync mirror of async ``put_blob``；v0.4.0 #196）.

        ``socketio.Client`` 引擎在后台线程驱动收发，阻塞 ``call`` 不阻塞事件循环；错误面与
        async 版完全一致（含首块 ``TimeoutError`` → ``BlobUploadUnsupportedError`` 归一）。
        协议依据 / Protocol: events.md §client:put_blob；blob-transfer.md §3/§7. 完整语义文档
        见 :meth:`a2c_smcp.agent.client.AsyncSMCPAgentClient.put_blob`.
        """
        call = self._make_put_blob_call(computer, timeout)
        return pump_blob_sync(call, computer, data, name_hint=name_hint, chunk_size=chunk_size)

    def _make_put_blob_call(self, computer: str, timeout: int) -> Any:
        """构造 :func:`pump_blob_sync` 的 ``call`` 适配器（sync mirror of async ``_make_put_blob_call``）."""

        def _call(
            upload_id: str | None,
            chunk_offset: int,
            eof: bool,
            chunk: bytes,
            declaration: Mapping[str, Any] | None,
        ) -> dict:
            req = self.create_put_blob_request(computer, upload_id, chunk_offset, eof, chunk, declaration)
            ack = self.call(PUT_BLOB_EVENT, req, namespace=self._namespace, timeout=timeout)
            return cast(dict, ack)

        return _call

    def _resolve_tool_call_binary_sideband(self, raw: Any, computer: str) -> Any:
        """同步：扫描 ``CallToolResult`` content items 的 ``_meta.a2c_blob_handle`` 并 drain 还原.

        Sync mirror of async ``_resolve_tool_call_binary_sideband``. 协议依据 / Protocol: blob-transfer.md §5.

        实现策略 / Strategy: 通过 :mod:`._blob_sideband` 共享纯函数，async/sync 仅在 ``drain_blob_sync``
        与异常兜底处保留差异.
        Pure structural transforms shared with async via ``_blob_sideband``; only the
        ``drain_blob_sync`` call and per-item error shell differ.
        """
        call = self._make_blob_call()
        for item, meta, handle in _sb.extract_sideband_handles(raw):
            try:
                payload, _mime = drain_blob_sync(call, computer, handle)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"tool_call binary sideband drain failed for handle={handle!r}: {e}; keeping _meta.a2c_blob_handle intact",
                )
                continue
            _sb.inject_payload_into_content_item(item, meta, payload)
        return raw

    def _on_skills_updated(self, data: dict) -> None:
        """同步：处理 SKILL 集合更新通知（v0.2.1 sync mirror）；默认自动重拉 ``client:get_skills`` 并回调 ``on_skills_received``.

        Sync mirror: default auto-refresh + ``on_skills_received`` dispatch; hook errors isolated.
        """
        try:
            computer = data.get("computer")
            if not computer:
                logger.warning("UPDATE_SKILLS_NOTIFICATION missing 'computer'")
                return
            ret = self.get_skills(computer)
            skills = ret.get("skills", [])
            logger.info(f"Skills refreshed from computer {computer}: count={len(skills)}")
            # v0.2.1: 派发 on_skills_received（hook 异常独立隔离，不污染拉取链路）
            # Dispatch on_skills_received; hook errors are isolated and never propagated.
            if self.event_handler and hasattr(self.event_handler, "on_skills_received"):
                try:
                    self.event_handler.on_skills_received(computer, skills, self)
                except Exception as hook_exc:
                    logger.error(
                        f"on_skills_received hook raised for computer {computer}: {hook_exc}",
                        exc_info=True,
                    )
        except Exception as e:
            logger.error(f"Error handling skills updated notification: {e}", exc_info=True)

    def get_computers_in_office(self, office_id: str, timeout: int = 20) -> list[SessionInfo]:
        """
        获取指定房间内的所有Computer信息
        Get all computers info in the specified office

        Args:
            office_id (str): 房间ID / Office ID
            timeout (int): 超时时间 / Timeout duration

        Returns:
            list[SessionInfo]: Computer信息列表 / List of computer info
        """
        agent_config = self.auth_provider.get_agent_config()
        req = ListRoomReq(
            agent=agent_config["agent"],
            req_id=f"list_computers_{agent_config['agent']}_{office_id}",
            office_id=office_id,
        )

        try:
            logger.debug(f"Getting computers in office {office_id}")
            response = self.call(LIST_ROOM_EVENT, req, namespace=self._namespace, timeout=timeout)

            # flat ErrorPayload（#214：400 / 4103 无房 / 4104 跨房）→ 抛 SMCPProtocolError。
            # 必须**先于** req_id 校验：ErrorPayload 无 req_id，会被误报成「响应 req_id 不匹配」。
            # Must precede the req_id check: an ErrorPayload carries no req_id.
            raise_for_error_payload(response)

            # 验证响应 / Validate response
            if response.get("req_id") != req["req_id"]:
                raise ValueError("Invalid response with mismatched req_id")

            # 过滤出Computer角色的会话 / Filter sessions with computer role
            all_sessions = response.get("sessions", [])
            computers = [s for s in all_sessions if s.get("role") == "computer"]
            return computers

        except Exception as e:
            logger.error(f"Failed to get computers in office {office_id}: {e}", exc_info=True)
            raise
