"""
* 文件名: base
* 作者: JQQ
* 创建日期: 2025/9/30
* 最后修改日期: 2025/10/8
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, loguru
* 描述: Agent基础客户端抽象类（异步和同步版本）/ Agent base client abstract classes (async and sync versions)
"""

import asyncio
import functools
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, cast

from mcp.types import CallToolResult, TextContent
from socketio.exceptions import TimeoutError as SioTimeoutError

from a2c_smcp.agent import _request_builders as _rb
from a2c_smcp.agent._cancel import AsyncCancelWatcher, CancelSendGate, SyncCancelWatcher
from a2c_smcp.agent.auth import AgentAuthProvider
from a2c_smcp.agent.types import AgentEventHandler, AsyncAgentEventHandler, CancelSignal
from a2c_smcp.smcp import (
    CANCEL_TOOL_CALL_EVENT,
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_EVENT,
    AgentCallData,
    EnterOfficeNotification,
    EnterOfficeReq,
    GetBlobReq,
    GetComputerConfigReq,
    GetDeskTopReq,
    GetDeskTopRet,
    GetResourcesReq,
    GetSkillReq,
    GetSkillsReq,
    GetToolsReq,
    GetToolsRet,
    LeaveOfficeNotification,
    LeaveOfficeReq,
    PutBlobReq,
    ToolCallReq,
    UpdateMCPConfigNotification,
    UpdateToolListNotification,
)
from a2c_smcp.utils.logger import get_logger

logger = get_logger("agent")

#: ``client:tool_call`` 等待 ack 的超时异常类型 —— **必须**同时含 socketio 的 ``TimeoutError``。
#:
#: ⚠️ python-socketio 5.x 的 ``socketio.exceptions.TimeoutError`` **不是** builtin ``TimeoutError``
#: 的子类（MRO: ``TimeoutError → SocketIOError → Exception``），只写 ``except TimeoutError`` 会让
#: 超时兜底臂在真实链路上**永不可达**：自身超时被 ``except Exception`` 接走，于是既不广播
#: ``server:tool_call_cancel``、也不标结果级 ``meta.a2c_timeout``（协议 error-handling.md
#: §Agent 端超时要求两者都做），调用方只拿到一条空消息的普通失败。同族陷阱见
#: ``a2c_smcp/utils/blob.py`` 的 ``BlobUploadUnsupportedError`` 注记。
#:
#: / Ack-wait timeout types. socketio's ``TimeoutError`` is NOT a subclass of the builtin, so
#: catching only the builtin makes the timeout arm unreachable in production.
TOOL_CALL_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, SioTimeoutError)


class BaseAgentClient(ABC):
    """
    Agent异步基础客户端抽象类，提供通用的SMCP协议处理逻辑（异步版本）
    Agent async base client abstract class, provides common SMCP protocol handling logic (async version)
    """

    def __init__(
        self,
        auth_provider: AgentAuthProvider,
        event_handler: AsyncAgentEventHandler | None = None,
    ) -> None:
        """
        初始化异步基础Agent客户端
        Initialize async base Agent client

        Args:
            auth_provider (AgentAuthProvider): 认证提供者 / Authentication provider
            event_handler (AsyncAgentEventHandler | None): 异步事件处理器 / Async event handler
        """
        self.auth_provider = auth_provider
        self.event_handler = event_handler
        # ── #203 Office 成员关系：desired + generation（与 Computer 侧同构）─────────────────────
        # ``_desired_office`` = 调用方最后一次声明的入房意图 ``(office_id, agent_name)``。Agent 侧的
        # ``join_office`` 是**无 ack 的 emit**、且服务端房间成员关系随会话销毁，断线重连后必须由客户端
        # 重放 ``server:join_office``；``agent_name`` 只存在于 join 调用实参，故这里必须记住它。
        # The last declared membership intent; replayed after an auto-reconnect because room
        # membership is session-scoped and ``agent_name`` only exists as a call argument.
        self._desired_office: tuple[str, str] | None = None
        self._office_generation = 0
        self._office_rejoin_task: asyncio.Task[None] | None = None
        self._office_op_lock = asyncio.Lock()

    def _bump_office_generation(self) -> int:
        """推进 generation（作废在途自动回房）并返回新值 / advance the generation, invalidating in-flight replays."""
        self._office_generation += 1
        return self._office_generation

    def _cancel_office_rejoin(self) -> None:
        """作废在途自动回房（同步 cancel，不 await——调用点可能在内联路径上）。"""
        task = self._office_rejoin_task
        self._office_rejoin_task = None
        if task is not None and not task.done():
            task.cancel()

    def _drop_desired_office(self) -> None:
        """清空回房意图（显式退房 / 手工断开 / 服务端踢出 / 重连彻底放弃 / 回房失败）。"""
        self._bump_office_generation()
        self._cancel_office_rejoin()
        self._desired_office = None

    @abstractmethod
    async def emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        """
        异步发送事件的抽象方法
        Abstract method for async sending events
        """
        pass

    @abstractmethod
    async def call(self, event: str, data: Any = None, namespace: str | None = None, timeout: int = 60) -> Any:
        """
        异步调用事件并等待响应的抽象方法
        Abstract method for async calling events and waiting for response
        """
        pass

    def validate_emit_event(self, event: str) -> None:
        """
        验证发送事件的合法性
        Validate the legality of emitted events

        Args:
            event (str): 事件名称 / Event name

        Raises:
            ValueError: 当事件不合法时 / When event is invalid
        """
        _rb.validate_emit_event(event)

    def create_tool_call_request(self, computer: str, tool_name: str, params: dict, timeout: int) -> ToolCallReq:
        """
        创建工具调用请求对象
        Create tool call request object

        Args:
            computer (str): 目标计算机ID / Target computer ID
            tool_name (str): 工具名称 / Tool name
            params (dict): 调用参数 / Call parameters
            timeout (int): 超时时间 / Timeout duration

        Returns:
            ToolCallReq: 工具调用请求 / Tool call request
        """
        return _rb.build_tool_call_request(self.auth_provider.get_agent_config(), computer, tool_name, params, timeout)

    def create_get_tools_request(self, computer: str) -> GetToolsReq:
        """
        创建获取工具请求对象
        Create get tools request object

        Args:
            computer (str): 目标计算机ID / Target computer ID

        Returns:
            GetToolsReq: 获取工具请求 / Get tools request
        """
        return _rb.build_get_tools_request(self.auth_provider.get_agent_config(), computer)

    def create_get_config_request(self, computer: str) -> GetComputerConfigReq:
        """
        创建获取 Computer MCP 配置请求对象（#149）
        Create get-config request object (#149)

        Args:
            computer (str): 目标计算机ID / Target computer ID

        Returns:
            GetComputerConfigReq: 获取配置请求 / Get config request
        """
        return _rb.build_get_config_request(self.auth_provider.get_agent_config(), computer)

    def create_get_resources_request(self, computer: str, mcp_server: str, cursor: str | None = None) -> GetResourcesReq:
        """
        创建获取资源请求对象（透明转发 MCP resources/list）
        Create get-resources request object (transparent forward of MCP resources/list)

        Args:
            computer (str): 目标计算机ID / Target computer ID
            mcp_server (str): 目标 MCP Server 的 bundle_id（= get_config servers 字典 key，协议 #18）/ Target server bundle_id
            cursor (str | None): MCP 标准翻页游标；首次传 None / MCP pagination cursor; None for first page

        Returns:
            GetResourcesReq: 获取资源请求 / Get resources request
        """
        return _rb.build_get_resources_request(self.auth_provider.get_agent_config(), computer, mcp_server, cursor)

    def create_get_desktop_request(self, computer: str, *, size: int | None = None, window: str | None = None) -> GetDeskTopReq:
        """
        创建获取桌面请求对象
        Create get desktop request object

        Args:
            computer (str): 目标计算机ID / Target computer ID
            size (int | None): 桌面窗口数量上限 / Max number of windows
            window (str | None): 指定窗口URI / Specific window URI

        Returns:
            GetDeskTopReq: 获取桌面请求 / Get desktop request
        """
        return _rb.build_get_desktop_request(self.auth_provider.get_agent_config(), computer, size=size, window=window)

    def create_get_skills_request(self, computer: str) -> GetSkillsReq:
        """创建获取 SKILL 清单请求对象 / Create get-skills request object (v0.2.1).

        协议依据 / Protocol: a2c-smcp-protocol events.md §client:get_skills.
        """
        return _rb.build_get_skills_request(self.auth_provider.get_agent_config(), computer)

    def create_get_skill_request(self, computer: str, name: str, rel_path: str | None = None) -> GetSkillReq:
        """创建获取 SKILL 包内单个资源请求对象 / Create get-skill request object (v0.2.1).

        协议依据 / Protocol: events.md §client:get_skill. ``rel_path`` 缺省取 ``SKILL.md`` 入口.
        """
        return _rb.build_get_skill_request(self.auth_provider.get_agent_config(), computer, name, rel_path)

    def create_get_blob_request(
        self,
        computer: str,
        blob_handle: str,
        chunk_offset: int | None = None,
        max_chunk_bytes: int | None = None,
    ) -> GetBlobReq:
        """创建通用二进制拉取单块请求对象 / Create get-blob chunk request object (v0.2.1).

        协议依据 / Protocol: events.md §client:get_blob；blob-transfer.md.
        """
        return _rb.build_get_blob_request(
            self.auth_provider.get_agent_config(),
            computer,
            blob_handle,
            chunk_offset,
            max_chunk_bytes,
        )

    def create_put_blob_request(
        self,
        computer: str,
        upload_id: str | None,
        chunk_offset: int,
        eof: bool,
        blob: bytes,
        declaration: Mapping[str, Any] | None = None,
    ) -> PutBlobReq:
        """创建上行写入单块请求对象（v0.4.0 #196）/ Create put-blob chunk request object.

        协议依据 / Protocol: events.md §client:put_blob；blob-transfer.md §3/§7.
        ``upload_id`` 为 ``None`` 即首块（须携 ``declaration``）；``blob`` 为原始字节（内部 base64）.
        """
        return _rb.build_put_blob_request(
            self.auth_provider.get_agent_config(),
            computer,
            upload_id,
            chunk_offset,
            eof,
            blob,
            declaration,
        )

    def process_desktop_response(self, response: GetDeskTopRet, computer: str) -> None:
        """
        处理桌面响应（默认仅记录日志；留作后续扩展回调）。
        Process desktop response (log only by default; placeholder for future callbacks).
        """
        try:
            desktops = response.get("desktops", []) if isinstance(response, dict) else []
            logger.info(f"Received desktop from computer {computer}, windows={len(desktops)}")
        except Exception as e:
            logger.error(f"Error processing desktop response: {e}", exc_info=True)

    def handle_tool_call_timeout(self, req_id: str) -> CallToolResult:
        """
        处理工具调用超时情况
        Handle tool call timeout situation

        Args:
            req_id (str): 请求ID / Request ID

        Returns:
            CallToolResult: 超时错误结果 / Timeout error result
        """
        result = CallToolResult(
            content=[TextContent(text=f"工具调用超时 / Tool call timeout, req_id={req_id}", type="text")],
            isError=True,
        )
        # 协议结果级标记（32eea98 / protocol#5）：标识此结果为超时返回，使 Agent 能区分「超时 / 取消 / 普通失败」。
        # 经 ``.meta`` 写入真实 meta 字段（出线默认 dump→key=meta）；构造器传 ``meta=`` 会落 extra（字段 alias 为 ``_meta``）。
        # Protocol result-level marker (32eea98 / protocol#5): mark this result as a timeout return so the Agent can
        # distinguish timeout from cancellation / ordinary failure. Set via ``.meta`` (real field, wire key ``meta``);
        # passing ``meta=`` to the ctor would land in ``extra`` since the field alias is ``_meta``.
        result.meta = {"a2c_timeout": True}
        return result

    async def _abroadcast_tool_call_cancel(self, req_id: str, namespace: str | None) -> None:
        """广播 ``server:tool_call_cancel``（fire-and-forget，无 ack）——#209 宿主取消信号与自身超时兜底共用。

        载体 ``AgentCallData`` 仅 ``{agent, req_id}``（**无** computer、**无** reason）；``req_id``
        **MUST** 等于被取消的原 ``client:tool_call`` 的 ``req_id`` —— Computer 据此在在途调用表中定位，
        且该键全局唯一、永不复用。Server 收到后仅向房间广播 ``notify:tool_call_cancel``、
        **不回执**：因而本方法用 ``emit``（**非** ``call``）不等待 ack，ack 为 ``None`` 是合规预期。

        ``req_id`` 的铸造与生命周期完全封闭在 ``emit_tool_call`` 内、不外露（protocol#58 裁决），
        故本方法不对外公开。

        / Broadcast ``server:tool_call_cancel`` (fire-and-forget, no ack); shared by the #209 cancel
        entry and the self-timeout fallback.
        """
        agent_config = self.auth_provider.get_agent_config()
        cancel_data = AgentCallData(agent=agent_config["agent"], req_id=req_id)
        await self.emit(CANCEL_TOOL_CALL_EVENT, cancel_data, namespace=namespace)

    def _start_cancel_watcher(
        self,
        cancel: CancelSignal | None,
        gate: CancelSendGate,
        req_id: str,
        namespace: str | None,
    ) -> AsyncCancelWatcher | None:
        """校验宿主信号并启动取消 watcher；``cancel is None`` 时不创建（既有路径零改动）。

        入口 **fail-fast**：不满足 :class:`CancelSignal`（无 ``is_set()``）立即抛 ``TypeError`` ——
        清晰的调用方错误远优于「watcher 里静默 ``AttributeError`` ⇒ 取消永远不生效、只能等满超时」。

        / Validate the host signal and start the async watcher; ``TypeError`` when the shape is wrong.
        """
        if cancel is None:
            return None
        if not isinstance(cancel, CancelSignal):
            raise TypeError(
                "cancel 必须实现 is_set()（CancelSignal）/ cancel must implement is_set(): "
                f"实际类型 {type(cancel).__name__}"
            )
        watcher = AsyncCancelWatcher(
            signal=cancel,
            gate=gate,
            req_id=req_id,
            broadcast=functools.partial(self._abroadcast_tool_call_cancel, namespace=namespace),
        )
        watcher.start()
        return watcher

    def validate_office_data(self, data: EnterOfficeNotification | LeaveOfficeNotification) -> str:
        """
        验证办公室数据并返回计算机ID
        Validate office data and return computer ID

        Args:
            data: 办公室通知数据 / Office notification data

        Returns:
            str: 计算机ID / Computer ID

        Raises:
            AssertionError: 当数据无效时 / When data is invalid
        """
        agent_config = self.auth_provider.get_agent_config()
        assert data["office_id"] == agent_config["office_id"], "无效的办公室ID / Invalid office ID"
        assert data.get("computer"), "无效的计算机ID / Invalid computer ID"
        return cast(str, data["computer"])

    async def handle_computer_enter_office(self, data: EnterOfficeNotification) -> None:
        """
        异步处理Computer加入办公室事件
        Async handle Computer enter office event

        Args:
            data: 加入办公室通知数据 / Enter office notification data
        """
        try:
            computer = self.validate_office_data(data)
            logger.info(f"Computer {computer} entered office {data['office_id']}")

            # 调用异步事件处理器（强制携带 client 引用）
            # Call async event handler (force passing client reference)
            if self.event_handler:
                await self.event_handler.on_computer_enter_office(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer enter office: {e}", exc_info=True)

    async def handle_computer_leave_office(self, data: LeaveOfficeNotification) -> None:
        """
        异步处理Computer离开办公室事件
        Async handle Computer leave office event

        Args:
            data: 离开办公室通知数据 / Leave office notification data
        """
        try:
            computer = self.validate_office_data(data)
            logger.info(f"Computer {computer} left office {data['office_id']}")

            # 调用异步事件处理器（强制携带 client 引用）
            # Call async event handler (force passing client reference)
            if self.event_handler:
                await self.event_handler.on_computer_leave_office(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer leave office: {e}", exc_info=True)

    async def handle_computer_update_config(self, data: UpdateMCPConfigNotification) -> None:
        """
        异步处理Computer更新配置事件
        Async handle Computer update config event

        Args:
            data: 配置更新通知数据 / Config update notification data
        """
        try:
            computer = data["computer"]
            logger.info(f"Computer {computer} updated config")

            # 调用异步事件处理器（强制携带 client 引用）
            # Call async event handler (force passing client reference)
            if self.event_handler:
                await self.event_handler.on_computer_update_config(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer update config: {e}", exc_info=True)

    async def handle_computer_update_tool_list(self, data: UpdateToolListNotification) -> None:
        """
        异步处理Computer工具列表更新事件的**预清**回调（#127）
        Async pre-clean dispatch for a Computer's tool-list-changed event (#127)

        语义对齐 ``handle_computer_update_config``：派发消费方的预清回调 ``on_computer_update_tool_list``，
        供其清理该 Computer 的旧工具（回拉后经 ``on_tools_received`` 重加）。与 ``on_skills_received`` 一致，
        对新回调使用 ``hasattr`` 守卫保持向后兼容；本方法自有 try/except 隔离 hook 异常，**不**阻断上层回拉。

        Mirrors ``handle_computer_update_config`` but guards the new callback with ``hasattr`` (backward compat,
        same convention as ``on_skills_received``). Hook errors are isolated here and never block the caller's refetch.

        Args:
            data: 工具列表更新通知数据 / Tool list update notification data
        """
        try:
            computer = data["computer"]
            logger.info(f"Computer {computer} updated tool list")

            # 调用异步事件处理器（强制携带 client 引用）；新回调经 hasattr 守卫兼容旧 handler
            # Call async event handler (force passing client reference); hasattr guard keeps legacy handlers working
            if self.event_handler and hasattr(self.event_handler, "on_computer_update_tool_list"):
                await self.event_handler.on_computer_update_tool_list(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer update tool list: {e}", exc_info=True)

    async def process_tools_response(self, response: GetToolsRet, computer: str) -> None:
        """
        异步处理工具响应
        Async process tools response

        Args:
            response: 工具响应数据 / Tools response data
            computer: 计算机ID / Computer ID
        """
        try:
            if tools := response.get("tools"):
                logger.info(f"Received {len(tools)} tools from computer {computer}")

                # 调用异步事件处理器（强制携带 client 引用）
                # Call async event handler (force passing client reference)
                if self.event_handler:
                    await self.event_handler.on_tools_received(computer, tools, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error processing tools response: {e}", exc_info=True)

    async def join_office(self, office_id: str, agent_name: str, namespace: str | None = None) -> None:
        """
        加入一个Office（Socket.IO中的Room）
        Join an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID，在A2C-smcp协议中，OfficeID即为Socket.IO RoomID
                            / Room ID, in A2C-smcp protocol, OfficeID is the Socket.IO RoomID
            agent_name (str): Agent名称，提供给前端展示用
                            / Agent name, for frontend display
            namespace (str | None): 命名空间 / Namespace

        Note:
            #203：本方法同时声明 **desired 意图**——``(office_id, agent_name)`` 被记住，断线自动重连后
            由客户端重放入房（room 成员关系属于会话）。``leave_office`` / 手工断开 / 服务端踢出会清空它。
            Also declares the desired membership intent, replayed after an auto-reconnect.
        """
        self._bump_office_generation()
        self._cancel_office_rejoin()
        self._desired_office = (office_id, agent_name)
        # 与自动回房共用 office 操作锁（对称 Computer 侧）：回房在途时本 join 排队等候，避免
        # JOIN/LEAVE 在 wire 上重排（例如 LEAVE 抢先于在途 JOIN 落地 ⇒ 客户端以为已退房、服务端
        # 仍在房间里）。取消在途回房使等待是瞬时的，不会吃满回房超时。
        # Serialize with the replay so JOIN/LEAVE cannot be reordered on the wire.
        async with self._office_op_lock:
            await self.emit(
                JOIN_OFFICE_EVENT,
                EnterOfficeReq(office_id=office_id, role="agent", name=agent_name),
                namespace=namespace,
            )

    async def leave_office(self, office_id: str, namespace: str | None = None) -> None:
        """
        离开一个Office（Socket.IO中的Room）
        Leave an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID / Room ID
            namespace (str | None): 命名空间 / Namespace
        """
        self._drop_desired_office()
        # 与自动回房共用 office 操作锁（同 join_office：避免 LEAVE 抢先于在途 JOIN 落地）
        async with self._office_op_lock:
            await self.emit(LEAVE_OFFICE_EVENT, LeaveOfficeReq(office_id=office_id), namespace=namespace)

    @abstractmethod
    def register_event_handlers(self) -> None:
        """
        注册事件处理器，子类需要实现具体的注册逻辑
        Register event handlers, subclasses need to implement specific registration logic
        """
        # 这个方法需要在子类中实现具体的事件注册逻辑
        # This method needs to implement specific event registration logic in subclasses
        pass


class BaseAgentSyncClient(ABC):
    """
    Agent同步基础客户端抽象类，提供通用的SMCP协议处理逻辑（同步版本）
    Agent sync base client abstract class, provides common SMCP protocol handling logic (sync version)
    """

    def __init__(
        self,
        auth_provider: AgentAuthProvider,
        event_handler: AgentEventHandler | None = None,
    ) -> None:
        """
        初始化同步基础Agent客户端
        Initialize sync base Agent client

        Args:
            auth_provider (AgentAuthProvider): 认证提供者 / Authentication provider
            event_handler (AgentEventHandler | None): 同步事件处理器 / Sync event handler
        """
        self.auth_provider = auth_provider
        self.event_handler = event_handler
        # ── #203 Office 成员关系：desired + generation（与异步侧、Computer 侧同构）────────────
        # 同步侧差异：回房跑在独立 daemon 线程里（connect 钩子内联在读循环的分发线程上，``call`` 阻塞），
        # 线程**不可取消**，故只用 generation 判据作废陈旧结果，状态变更用锁保护（短临界区、不跨网络等待）。
        # Sync difference: the replay runs on a daemon thread (not cancellable), so staleness is
        # handled purely by the generation guard, with a short lock around state mutations only.
        self._desired_office: tuple[str, str] | None = None
        self._office_generation = 0
        self._office_rejoin_thread: threading.Thread | None = None
        self._office_state_lock = threading.Lock()

    def _bump_office_generation(self) -> int:
        """推进 generation（作废在途自动回房）并返回新值 / advance the generation, invalidating in-flight replays."""
        with self._office_state_lock:
            self._office_generation += 1
            return self._office_generation

    def _cancel_office_rejoin(self) -> None:
        """作废在途自动回房 / invalidate any in-flight replay.

        同步侧无法取消线程：只摘引用 + 推进 generation，线程自身的 generation 双检会丢弃其结果。
        Threads are not cancellable: the stale thread's own generation checks discard its result.
        """
        self._office_rejoin_thread = None

    def _drop_desired_office(self) -> None:
        """清空回房意图（显式退房 / 手工断开 / 服务端踢出 / 重连彻底放弃 / 回房失败）。"""
        self._bump_office_generation()
        self._cancel_office_rejoin()
        with self._office_state_lock:
            self._desired_office = None

    @abstractmethod
    def emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        """
        发送事件的抽象方法
        Abstract method for sending events
        """
        pass

    @abstractmethod
    def call(self, event: str, data: Any = None, namespace: str | None = None, timeout: int = 60) -> Any:
        """
        调用事件并等待响应的抽象方法
        Abstract method for calling events and waiting for response
        """
        pass

    def validate_emit_event(self, event: str) -> None:
        """
        验证发送事件的合法性
        Validate the legality of emitted events

        Args:
            event (str): 事件名称 / Event name

        Raises:
            ValueError: 当事件不合法时 / When event is invalid
        """
        _rb.validate_emit_event(event)

    def create_tool_call_request(self, computer: str, tool_name: str, params: dict, timeout: int) -> ToolCallReq:
        """
        创建工具调用请求对象
        Create tool call request object

        Args:
            computer (str): 目标计算机ID / Target computer ID
            tool_name (str): 工具名称 / Tool name
            params (dict): 调用参数 / Call parameters
            timeout (int): 超时时间 / Timeout duration

        Returns:
            ToolCallReq: 工具调用请求 / Tool call request
        """
        return _rb.build_tool_call_request(self.auth_provider.get_agent_config(), computer, tool_name, params, timeout)

    def create_get_tools_request(self, computer: str) -> GetToolsReq:
        """
        创建获取工具请求对象
        Create get tools request object

        Args:
            computer (str): 目标计算机ID / Target computer ID

        Returns:
            GetToolsReq: 获取工具请求 / Get tools request
        """
        return _rb.build_get_tools_request(self.auth_provider.get_agent_config(), computer)

    def create_get_config_request(self, computer: str) -> GetComputerConfigReq:
        """
        创建获取 Computer MCP 配置请求对象（#149）
        Create get-config request object (#149)

        Args:
            computer (str): 目标计算机ID / Target computer ID

        Returns:
            GetComputerConfigReq: 获取配置请求 / Get config request
        """
        return _rb.build_get_config_request(self.auth_provider.get_agent_config(), computer)

    def create_get_resources_request(self, computer: str, mcp_server: str, cursor: str | None = None) -> GetResourcesReq:
        """
        创建获取资源请求对象（透明转发 MCP resources/list）
        Create get-resources request object (transparent forward of MCP resources/list)

        Args:
            computer (str): 目标计算机ID / Target computer ID
            mcp_server (str): 目标 MCP Server 的 bundle_id（= get_config servers 字典 key，协议 #18）/ Target server bundle_id
            cursor (str | None): MCP 标准翻页游标；首次传 None / MCP pagination cursor; None for first page

        Returns:
            GetResourcesReq: 获取资源请求 / Get resources request
        """
        return _rb.build_get_resources_request(self.auth_provider.get_agent_config(), computer, mcp_server, cursor)

    def create_get_desktop_request(self, computer: str, *, size: int | None = None, window: str | None = None) -> GetDeskTopReq:
        """
        创建获取桌面请求对象
        Create get desktop request object

        Args:
            computer (str): 目标计算机ID / Target computer ID
            size (int | None): 桌面窗口数量上限 / Max number of windows
            window (str | None): 指定窗口URI / Specific window URI

        Returns:
            GetDeskTopReq: 获取桌面请求 / Get desktop request
        """
        return _rb.build_get_desktop_request(self.auth_provider.get_agent_config(), computer, size=size, window=window)

    def create_get_skills_request(self, computer: str) -> GetSkillsReq:
        """创建获取 SKILL 清单请求对象 / Create get-skills request object (v0.2.1).

        协议依据 / Protocol: a2c-smcp-protocol events.md §client:get_skills.
        """
        return _rb.build_get_skills_request(self.auth_provider.get_agent_config(), computer)

    def create_get_skill_request(self, computer: str, name: str, rel_path: str | None = None) -> GetSkillReq:
        """创建获取 SKILL 包内单个资源请求对象 / Create get-skill request object (v0.2.1).

        协议依据 / Protocol: events.md §client:get_skill. ``rel_path`` 缺省取 ``SKILL.md`` 入口.
        """
        return _rb.build_get_skill_request(self.auth_provider.get_agent_config(), computer, name, rel_path)

    def create_get_blob_request(
        self,
        computer: str,
        blob_handle: str,
        chunk_offset: int | None = None,
        max_chunk_bytes: int | None = None,
    ) -> GetBlobReq:
        """创建通用二进制拉取单块请求对象 / Create get-blob chunk request object (v0.2.1).

        协议依据 / Protocol: events.md §client:get_blob；blob-transfer.md.
        """
        return _rb.build_get_blob_request(
            self.auth_provider.get_agent_config(),
            computer,
            blob_handle,
            chunk_offset,
            max_chunk_bytes,
        )

    def create_put_blob_request(
        self,
        computer: str,
        upload_id: str | None,
        chunk_offset: int,
        eof: bool,
        blob: bytes,
        declaration: Mapping[str, Any] | None = None,
    ) -> PutBlobReq:
        """创建上行写入单块请求对象（v0.4.0 #196）/ Create put-blob chunk request object.

        协议依据 / Protocol: events.md §client:put_blob；blob-transfer.md §3/§7.
        ``upload_id`` 为 ``None`` 即首块（须携 ``declaration``）；``blob`` 为原始字节（内部 base64）.
        """
        return _rb.build_put_blob_request(
            self.auth_provider.get_agent_config(),
            computer,
            upload_id,
            chunk_offset,
            eof,
            blob,
            declaration,
        )

    def process_desktop_response(self, response: GetDeskTopRet, computer: str) -> None:
        """
        处理桌面响应（默认仅记录日志；留作后续扩展回调）。
        Process desktop response (log only by default; placeholder for future callbacks).
        """
        try:
            desktops = response.get("desktops", []) if isinstance(response, dict) else []
            logger.info(f"Received desktop from computer {computer}, windows={len(desktops)}")
        except Exception as e:
            logger.error(f"Error processing desktop response: {e}", exc_info=True)

    def handle_tool_call_timeout(self, req_id: str) -> CallToolResult:
        """
        处理工具调用超时情况
        Handle tool call timeout situation

        Args:
            req_id (str): 请求ID / Request ID

        Returns:
            CallToolResult: 超时错误结果 / Timeout error result
        """
        result = CallToolResult(
            content=[TextContent(text=f"工具调用超时 / Tool call timeout, req_id={req_id}", type="text")],
            isError=True,
        )
        # 协议结果级标记（32eea98 / protocol#5）：标识此结果为超时返回，使 Agent 能区分「超时 / 取消 / 普通失败」。
        # 经 ``.meta`` 写入真实 meta 字段（出线默认 dump→key=meta）；构造器传 ``meta=`` 会落 extra（字段 alias 为 ``_meta``）。
        # Protocol result-level marker (32eea98 / protocol#5): mark this result as a timeout return so the Agent can
        # distinguish timeout from cancellation / ordinary failure. Set via ``.meta`` (real field, wire key ``meta``);
        # passing ``meta=`` to the ctor would land in ``extra`` since the field alias is ``_meta``.
        result.meta = {"a2c_timeout": True}
        return result

    def _broadcast_tool_call_cancel(self, req_id: str, namespace: str | None) -> None:
        """广播 ``server:tool_call_cancel``（fire-and-forget，无 ack）——#209 宿主取消信号与自身超时兜底共用。

        载体 ``AgentCallData`` 仅 ``{agent, req_id}``（**无** computer、**无** reason）；``req_id``
        **MUST** 等于被取消的原 ``client:tool_call`` 的 ``req_id`` —— Computer 据此在在途调用表中定位，
        且该键全局唯一、永不复用。Server 收到后仅向房间广播 ``notify:tool_call_cancel``、
        **不回执**：因而本方法用 ``emit``（**非** ``call``）不等待 ack，ack 为 ``None`` 是合规预期。

        ``req_id`` 的铸造与生命周期完全封闭在 ``emit_tool_call`` 内、不外露（protocol#58 裁决），
        故本方法不对外公开。

        / Broadcast ``server:tool_call_cancel`` (fire-and-forget, no ack); shared by the #209 cancel
        entry and the self-timeout fallback.
        """
        agent_config = self.auth_provider.get_agent_config()
        cancel_data = AgentCallData(agent=agent_config["agent"], req_id=req_id)
        self.emit(CANCEL_TOOL_CALL_EVENT, cancel_data, namespace=namespace)

    def _start_cancel_watcher(
        self,
        cancel: CancelSignal | None,
        gate: CancelSendGate,
        req_id: str,
        namespace: str | None,
    ) -> SyncCancelWatcher | None:
        """校验宿主信号并启动取消 watcher；``cancel is None`` 时不创建（既有路径零改动）。

        入口 **fail-fast**：不满足 :class:`CancelSignal`（无 ``is_set()``）立即抛 ``TypeError`` ——
        清晰的调用方错误远优于「watcher 里静默 ``AttributeError`` ⇒ 取消永远不生效、只能等满超时」。

        / Validate the host signal and start the sync watcher; ``TypeError`` when the shape is wrong.
        """
        if cancel is None:
            return None
        if not isinstance(cancel, CancelSignal):
            raise TypeError(
                "cancel 必须实现 is_set()（CancelSignal）/ cancel must implement is_set(): "
                f"实际类型 {type(cancel).__name__}"
            )
        watcher = SyncCancelWatcher(
            signal=cancel,
            gate=gate,
            req_id=req_id,
            broadcast=functools.partial(self._broadcast_tool_call_cancel, namespace=namespace),
        )
        watcher.start()
        return watcher

    def validate_office_data(self, data: EnterOfficeNotification | LeaveOfficeNotification) -> str:
        """
        验证办公室数据并返回计算机ID
        Validate office data and return computer ID

        Args:
            data: 办公室通知数据 / Office notification data

        Returns:
            str: 计算机ID / Computer ID

        Raises:
            AssertionError: 当数据无效时 / When data is invalid
        """
        agent_config = self.auth_provider.get_agent_config()
        assert data["office_id"] == agent_config["office_id"], "无效的办公室ID / Invalid office ID"
        assert data.get("computer"), "无效的计算机ID / Invalid computer ID"
        return cast(str, data["computer"])

    def handle_computer_enter_office(self, data: EnterOfficeNotification) -> None:
        """
        处理Computer加入办公室事件
        Handle Computer enter office event

        Args:
            data: 加入办公室通知数据 / Enter office notification data
        """
        try:
            computer = self.validate_office_data(data)
            logger.info(f"Computer {computer} entered office {data['office_id']}")

            # 调用事件处理器（强制携带 client 引用）
            # Call event handler (force passing client reference)
            if self.event_handler:
                self.event_handler.on_computer_enter_office(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer enter office: {e}", exc_info=True)

    def handle_computer_leave_office(self, data: LeaveOfficeNotification) -> None:
        """
        处理Computer离开办公室事件
        Handle Computer leave office event

        Args:
            data: 离开办公室通知数据 / Leave office notification data
        """
        try:
            computer = self.validate_office_data(data)
            logger.info(f"Computer {computer} left office {data['office_id']}")

            # 调用事件处理器（强制携带 client 引用）
            # Call event handler (force passing client reference)
            if self.event_handler:
                self.event_handler.on_computer_leave_office(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer leave office: {e}", exc_info=True)

    def handle_computer_update_config(self, data: UpdateMCPConfigNotification) -> None:
        """
        处理Computer更新配置事件
        Handle Computer update config event

        Args:
            data: 配置更新通知数据 / Config update notification data
        """
        try:
            computer = data["computer"]
            logger.info(f"Computer {computer} updated config")

            # 调用事件处理器（强制携带 client 引用）
            # Call event handler (force passing client reference)
            if self.event_handler:
                self.event_handler.on_computer_update_config(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer update config: {e}", exc_info=True)

    def handle_computer_update_tool_list(self, data: UpdateToolListNotification) -> None:
        """
        处理Computer工具列表更新事件的**预清**回调（#127 sync mirror）
        Pre-clean dispatch for a Computer's tool-list-changed event (#127, sync mirror)

        语义对齐 ``handle_computer_update_config``：派发消费方的预清回调 ``on_computer_update_tool_list``，
        供其清理该 Computer 的旧工具（回拉后经 ``on_tools_received`` 重加）。对新回调使用 ``hasattr`` 守卫保持
        向后兼容；自有 try/except 隔离 hook 异常，**不**阻断上层回拉。

        Args:
            data: 工具列表更新通知数据 / Tool list update notification data
        """
        try:
            computer = data["computer"]
            logger.info(f"Computer {computer} updated tool list")

            # 调用事件处理器（强制携带 client 引用）；新回调经 hasattr 守卫兼容旧 handler
            # Call event handler (force passing client reference); hasattr guard keeps legacy handlers working
            if self.event_handler and hasattr(self.event_handler, "on_computer_update_tool_list"):
                self.event_handler.on_computer_update_tool_list(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer update tool list: {e}", exc_info=True)

    def process_tools_response(self, response: GetToolsRet, computer: str) -> None:
        """
        处理工具响应
        Process tools response

        Args:
            response: 工具响应数据 / Tools response data
            computer: 计算机ID / Computer ID
        """
        try:
            if tools := response.get("tools"):
                logger.info(f"Received {len(tools)} tools from computer {computer}")

                # 调用事件处理器（强制携带 client 引用）
                # Call event handler (force passing client reference)
                if self.event_handler:
                    self.event_handler.on_tools_received(computer, tools, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error processing tools response: {e}", exc_info=True)

    def join_office(self, office_id: str, agent_name: str, namespace: str | None = None) -> None:
        """
        加入一个Office（Socket.IO中的Room）
        Join an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID，在A2C-smcp协议中，OfficeID即为Socket.IO RoomID
                            / Room ID, in A2C-smcp protocol, OfficeID is the Socket.IO RoomID
            agent_name (str): Agent名称，提供给前端展示用
                            / Agent name, for frontend display
            namespace (str | None): 命名空间 / Namespace

        Note:
            #203：同异步侧——本方法同时声明 **desired 意图**，断线自动重连后由客户端重放入房。
            Also declares the desired membership intent, replayed after an auto-reconnect.
        """
        self._bump_office_generation()
        self._cancel_office_rejoin()
        with self._office_state_lock:
            self._desired_office = (office_id, agent_name)
        self.emit(
            JOIN_OFFICE_EVENT,
            EnterOfficeReq(office_id=office_id, role="agent", name=agent_name),
            namespace=namespace,
        )

    def leave_office(self, office_id: str, namespace: str | None = None) -> None:
        """
        离开一个Office（Socket.IO中的Room）
        Leave an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID / Room ID
            namespace (str | None): 命名空间 / Namespace
        """
        self._drop_desired_office()
        self.emit(LEAVE_OFFICE_EVENT, LeaveOfficeReq(office_id=office_id), namespace=namespace)

    @abstractmethod
    def register_event_handlers(self) -> None:
        """
        注册事件处理器，子类需要实现具体的注册逻辑
        Register event handlers, subclasses need to implement specific registration logic
        """
        # 这个方法需要在子类中实现具体的事件注册逻辑
        # This method needs to implement specific event registration logic in subclasses
        pass
