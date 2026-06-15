# -*- coding: utf-8 -*-
# filename: base_client.py
# @Time    : 2025/8/18 10:57
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from enum import StrEnum
from typing import Generic, TypeVar, cast

from mcp import ClientSession, Tool
from mcp.client.session import MessageHandlerFnT
from mcp.types import (
    AnyUrl,
    CallToolResult,
    CancelledNotification,
    CancelledNotificationParams,
    ClientNotification,
    InitializeResult,
    ReadResourceResult,
    Resource,
    TextResourceContents,
)
from pydantic import BaseModel
from transitions.core import EventData
from transitions.extensions import AsyncMachine

from a2c_smcp.computer.mcp_clients._proc import force_kill_pids
from a2c_smcp.utils import is_window_uri
from a2c_smcp.utils.async_property import async_property
from a2c_smcp.utils.logger import get_logger, truncate

logger = get_logger("computer")

# 优雅关闭整体超时（秒）：mcp 子进程回收（graceful 2s + SIGTERM→SIGKILL 升级）通常秒级完成，
# 超过此阈值视为异常卡死 → 触发「兑底强杀」。属防御阈值，正常路径远低于此。
# Overall graceful-teardown budget before the force-kill fallback fires. mcp's own child reaping
# (2s graceful + SIGTERM→SIGKILL escalation) finishes in seconds; exceeding this means a wedge.
_TEARDOWN_TIMEOUT = 10.0

# 泛型参数，用于约束 MCP Server 参数类型
# Generic parameter for constraining MCP Server parameter types
ParamsT = TypeVar("ParamsT", bound=BaseModel)


class MCPCapabilityNotSupportedError(Exception):
    """
    中文: MCP Server 未声明所需 capability（如 `resources`）。
    英文: MCP Server did not declare a required capability (e.g. `resources`).

    上层（`client:get_resources` 处理器）应映射为 wire-level `4015 MCP_CAPABILITY_NOT_SUPPORTED`。
    Upper layers (`client:get_resources` handler) should map this to wire-level
    `4015 MCP_CAPABILITY_NOT_SUPPORTED`.
    """


class MCPServerNotFoundError(Exception):
    """
    中文: 引用的 MCP Server 名称在 Manager 中未注册。
    英文: The referenced MCP Server name is not registered in the Manager.

    上层（`client:get_resources` 处理器）应映射为 wire-level `4014 MCP_SERVER_NOT_FOUND`。
    Upper layers (`client:get_resources` handler) should map this to wire-level
    `4014 MCP_SERVER_NOT_FOUND`.
    """


class STATES(StrEnum):
    initialized = "initialized"
    connected = "connected"
    disconnected = "disconnected"
    error = "error"


TRANSITIONS = [
    {
        "trigger": "aconnect",
        "source": STATES.initialized,
        "dest": STATES.connected,
        "prepare": "aprepare_connect",
        "conditions": "acan_connect",
        "before": "abefore_connect",
        "after": "aafter_connect",
    },
    {
        "trigger": "adisconnect",
        "source": STATES.connected,
        "dest": STATES.disconnected,
        "prepare": "aprepare_disconnect",
        "conditions": "acan_disconnect",
        "before": "abefore_disconnect",
        "after": "aafter_disconnect",
    },
    {
        "trigger": "aerror",
        "source": "*",
        "dest": STATES.error,
        "prepare": "aprepare_error",
        "conditions": "acan_error",
        "before": "abefore_error",
        "after": "aafter_error",
    },
    {
        "trigger": "ainitialize",
        "source": "*",
        "dest": STATES.initialized,
        "prepare": "aprepare_initialize",
        "conditions": "acan_initialize",
        "before": "abefore_initialize",
        "after": "aafter_initialize",
    },
]


class A2CAsyncMachine(AsyncMachine):
    @staticmethod
    async def await_all(callables: list[Callable]) -> list:
        """
        Executes callables without parameters in parallel and collects their results.

        A2C协议中，需要在状态机的状态变化函数之间管理异步上下文，但由于原生实现 await_all 方法使用 asyncio.gather会导致上下文打开与关闭处于
            不同的async task中进而导致关闭异常。因此重写此实现，将await_all方法变为同步执行。以此实现上下文打开与关闭处于同一个async task中

        Args:
            callables (list): A list of callable functions

        Returns:
            list: A list of results. Using asyncio the list will be in the same order as the passed callables.
        """
        ret = []
        for c in callables:
            ret.append(await c())
        return ret


class BaseMCPClient(ABC, Generic[ParamsT]):
    def __init__(
        self,
        params: ParamsT,
        state_change_callback: Callable[[str, str], None | Awaitable[None]] | None = None,
        message_handler: MessageHandlerFnT | None = None,
    ) -> None:
        """
        基类初始化

        Attributes:
            params (ParamsT): MCP Server启动参数
            state_change_callback (Callable[[str, str], None | Awaitable[None]]): 状态变化回调，兼容同步与异步
            message_handler (Callable[..., Awaitable[None]] | None):
                自定义消息处理回调，符合 MCP ClientSession 的 message_handler 要求；若提供，则在构建 ClientSession 时传入。
                Custom message handler callback compatible with MCP ClientSession's message_handler; if provided,
                    it will be passed when creating the ClientSession.
        """
        self.params: ParamsT = params
        self._state_change_callback = state_change_callback
        # 私有属性：用于处理 ServerNotification（如 listChanged）的通用回调；在创建 ClientSession 时传入
        # Private attribute: general callback to handle ServerNotification (e.g., listChanged);
        # forwarded to ClientSession on creation
        self._message_handler = message_handler
        self._aexit_stack = AsyncExitStack()
        self._async_session: ClientSession | None = None
        self._session_keep_alive_task: asyncio.Task | None = None
        self._create_session_success_event = asyncio.Event()
        self._create_session_failure_event = asyncio.Event()
        self._async_session_closed_event = asyncio.Event()
        # 优雅关闭信号：替代 task.cancel() 触发 keep-alive 收尾，确保 _aexit_stack.aclose()
        # （内含 mcp SDK 的 graceful→SIGTERM→SIGKILL 子进程回收）在**非取消**上下文运行；否则
        # CancelledError 会抢占 mcp 的 anyio.fail_after，跳过强杀 → stdio 子进程残留、其
        # ThreadedChildWatcher 的 os.waitpid 永久阻塞（慢 CI 偶发整套 e2e 挂死的根因）。
        # Graceful-close signal replacing task.cancel() so _aexit_stack.aclose() runs cancellation-free
        # and mcp's child force-kill actually executes (cancel would skip it → wedged stdio child).
        self._close_event = asyncio.Event()
        # 本 client 启动的子进程 PID（仅 stdio 传输填充），供「兑底强杀」使用。
        # PIDs of children spawned by this client (stdio only); used by the force-kill fallback.
        self._child_pids: set[int] = set()
        # 私有属性：初始化结果（用于后续能力/元信息使用）；断开连接时需清理
        # Private attribute: InitializeResult cached for later capabilities/meta usage; must be cleared on disconnect
        self._initialize_result: InitializeResult | None = None
        # 会话级「已订阅 window:// URI」集合，保证 list_windows() 的资源订阅**幂等**：每个资源一会话内至多
        # subscribe 一次。否则每次 list_windows() 都重订阅，触发支持订阅的 Server 反复回发 resource_updated，
        # 与 Agent「桌面更新自动回拉 GET_DESKTOP → 又触发 list_windows()」串成自放大反馈环（#110 慢 CI 挂死根因）。
        # 会话拆除时清空，使重连后会重新订阅。
        # Per-session set of already-subscribed window:// URIs, making list_windows() subscription idempotent
        # (subscribe each resource at most once per session). Without it, every list_windows() re-subscribes and
        # makes a subscribe-capable server re-emit resource_updated, forming a self-amplifying loop with the
        # Agent's auto GET_DESKTOP refresh (root cause of the #110 slow-CI hang). Cleared on session teardown so
        # a reconnect re-subscribes.
        self._subscribed_window_uris: set[str] = set()

        # 初始化异步状态机
        self.machine = A2CAsyncMachine(
            model=self,
            states=STATES,
            transitions=TRANSITIONS,
            initial=STATES.initialized,
            send_event=True,  # 传递事件对象给回调
            auto_transitions=False,  # 禁用自动生成的状态转移
            ignore_invalid_triggers=False,  # 忽略无效触发器
        )

    async def _trigger_state_change(self, event: EventData) -> None:
        """
        触发状态变化回调，兼容同步与异步

        Args:
            event (EventData): 事件对象
        """
        if not self._state_change_callback:
            return

        callback_result = self._state_change_callback(event.transition.source, event.transition.dest)
        # 处理异步回调
        if isinstance(callback_result, Awaitable):
            await callback_result

    @async_property
    async def async_session(self) -> ClientSession:
        """
        异步会话对象

        Returns:
            ClientSession: MCP 官方异步会话，用于触发 MCP Server 指令
        """
        if self._async_session is None:
            await self.aconnect()
        return cast(ClientSession, self._async_session)

    @property
    def initialize_result(self) -> InitializeResult | None:
        """
        初始化结果只读访问（可能为None，表示未初始化或已清理）
        Read-only access for InitializeResult (may be None if not initialized or already cleaned)
        """
        return self._initialize_result

    @abstractmethod
    async def _create_async_session(self) -> ClientSession:
        """
        创建异步会话对象。一般在此方法内对需要保持的上下文压栈管理

        Returns:
            ClientSession: MCP 官方异步会话，用于触发 MCP Server 指令
        """
        raise NotImplementedError

    async def _keep_alive_task(self) -> None:
        """
        async_session 保活，进而保证其它连接可以正常使用它。

        在MCP源码设计中，xxx_client与ClientSession均使用了anyio的task_group来管理子任务。但这带来一个维护问题，在Manager中需要管理多个Client，如果
          Client的AsyncSession是基于anyio.task_group打开，那么在关闭时，必须严格按照打开顺序关闭，否则会导致anyio报错。基于这个anyio特性，因为我需要让
          ClientSession在一个独立的Asyncio Task中运行，如此可以保证这个上下文的打开关联在这个内部Task中，从而可以实现自由关闭。在Manager中可
          以独立启停Client

        在这个实现中主要完成以下几个工作：

        1. 完成 self._async_session的创建
        2. 将需要持续保证的上下文压栈 self._aexit_stack
        3. 通过 asyncio.Event().wait() 来保证上下文的持续，同时通过响应 self._session_keep_alive_task.done() 来完成上下文的关闭
        4. 得到关闭信号后，对 self._aexit_stack 里的上下文进行关闭
        """
        logger.debug(f"Session keep-alive task: {asyncio.current_task().get_name()}")
        # 每个 keep-alive 任务实例使用干净的关闭信号 / fresh close signal per task instance
        self._close_event.clear()
        try:
            # 创建异步会话，同时完成上下文的压栈
            self._async_session = await self._create_async_session()
            # 通知创建成功
            self._create_session_success_event.set()
            # 持续等待关闭信号
            try:
                # 等待 _close_task 通过 _close_event.set() 触发的优雅关闭。
                # 不再依赖 task.cancel —— cancel 会令 finally 中 _aexit_stack.aclose() 在
                # CancelledError 抢占下跳过 mcp 的子进程强杀，导致 stdio 子进程残留、os.waitpid 永久阻塞。
                # Wait for the graceful close signal; do NOT rely on task.cancel (it would let the
                # finally-block aclose() be pre-empted by CancelledError, skipping mcp's child force-kill).
                await self._close_event.wait()
            except asyncio.CancelledError:
                # 向后兼容：仍容忍历史 cancel 路径 / back-compat: still tolerate the legacy cancel path
                logger.debug(f"Session keep-alive task cancelled: {asyncio.current_task().get_name()}")
        except Exception as e:
            logger.error(f"Session keep-alive task error: {asyncio.current_task().get_name()}: {e}", exc_info=True)
            self._create_session_failure_event.set()
            await self.aerror()

        finally:
            # 关闭上下文
            await self._aexit_stack.aclose()
            # 清理session
            self._async_session = None
            # 清理初始化结果，确保会话真正关闭时协议初始化态一并清理
            # Cleanup InitializeResult to align with actual session teardown
            self._initialize_result = None
            # 会话关闭即清空已订阅集合：订阅随会话失效，重连后须重新订阅（幂等性以会话为界）。
            # Subscriptions die with the session; clear so a reconnect re-subscribes (idempotency is per-session).
            self._subscribed_window_uris.clear()
            self._async_session_closed_event.set()

    # region 状态转换回调函数基类实现
    async def aprepare_connect(self, event: EventData) -> None:
        """连接准备操作（可重写）"""
        logger.debug(f"Preparing connection with event: {event}\n\nserver params: {truncate(self.params)}")

    async def acan_connect(self, event: EventData) -> bool:
        """连接条件检查（可重写）"""
        logger.debug(f"Checking connection conditions with event: {event}\n\nserver params: {truncate(self.params)}")
        return True

    async def abefore_connect(self, event: EventData) -> None:
        """连接前操作（可重写）"""
        logger.debug(f"Before connection actions with event: {event}\n\nserver params: {truncate(self.params)}")

    async def on_enter_connected(self, event: EventData) -> None:
        """进入已连接状态（可重写）"""
        logger.debug(f"Entering connected state with event: {event}\n\nserver params: {truncate(self.params)}")
        self._session_keep_alive_task = asyncio.create_task(self._keep_alive_task())
        # 等待会话创建成功
        await self._create_session_success_event.wait()
        # 初始化client_session
        # 存储初始化返回结果，供后续使用
        # Store InitializeResult for later use
        self._initialize_result = await (await self.async_session).initialize()

    async def aafter_connect(self, event: EventData) -> None:
        """连接后操作（可重写）"""
        logger.debug(f"After connection actions with event: {event}\n\nserver params: {truncate(self.params)}")
        await self._trigger_state_change(event)

    async def aprepare_disconnect(self, event: EventData) -> None:
        """断开准备操作（可重写）"""
        logger.debug(f"Preparing disconnection with event: {event}\n\nserver params: {truncate(self.params)}")

    async def acan_disconnect(self, event: EventData) -> bool:
        """断开条件检查（可重写）"""
        logger.debug(f"Checking disconnection conditions with event: {event}\n\nserver params: {truncate(self.params)}")
        return (await self.async_session) is not None

    async def abefore_disconnect(self, event: EventData) -> None:
        """断开前操作（可重写）"""
        logger.debug(f"Before disconnection actions with event: {event}\n\nserver params: {truncate(self.params)}")

    async def on_enter_disconnected(self, event: EventData) -> None:
        """状态机进入断开状态时的回调（可重写）"""
        logger.debug(f"Entering disconnected state with event: {event}\n\nserver params: {truncate(self.params)}")
        # 关闭异步会话，保证资源的正常释放
        logger.debug(f"Enter disconnected state async task: {asyncio.current_task().get_name()}")
        await self._close_task()
        # 等待会话关闭
        await self._async_session_closed_event.wait()

    async def aafter_disconnect(self, event: EventData) -> None:
        """断开后操作（可重写）"""
        logger.debug(f"After disconnection actions with event: {event}\n\nserver params: {truncate(self.params)}")
        await self._trigger_state_change(event)

    async def aprepare_error(self, event: EventData) -> None:
        """错误准备操作（可重写）"""
        logger.debug(f"Preparing error with event: {event}\n\nserver params: {truncate(self.params)}")

    async def acan_error(self, event: EventData) -> bool:
        """错误条件检查（可重写）"""
        logger.debug(f"Checking error conditions with event: {event}\n\nserver params: {truncate(self.params)}")
        return True

    async def abefore_error(self, event: EventData) -> None:
        """错误前操作（可重写）"""
        logger.debug(f"Before error actions with event: {event}\n\nserver params: {truncate(self.params)}")

    async def on_enter_error(self, event: EventData) -> None:
        """状态机进入错误状态时的回调（可重写）"""
        logger.debug(f"Entered error state with event: {event}\n\nserver params: {truncate(self.params)}")
        # 将所有异步Event全部clear
        await self._close_task()

    async def aafter_error(self, event: EventData) -> None:
        """错误后操作（可重写）"""
        logger.debug(f"After error actions with event: {event}\n\nserver params: {truncate(self.params)}")
        await self._trigger_state_change(event)

    async def aprepare_initialize(self, event: EventData) -> None:
        """初始化准备操作（可重写）"""
        logger.debug(f"Preparing initialization with event: {event}\n\nserver params: {truncate(self.params)}")

    async def acan_initialize(self, event: EventData) -> bool:
        """初始化条件检查（可重写）"""
        logger.debug(f"Checking initialization conditions with event: {event}\n\nserver params: {truncate(self.params)}")
        return True

    async def abefore_initialize(self, event: EventData) -> None:
        """初始化前操作（可重写）"""
        logger.debug(f"Before initialization actions with event: {event}\n\nserver params: {truncate(self.params)}")

    async def on_enter_initialized(self, event: EventData) -> None:
        """状态机进入初始化状态时的回调（可重写）"""
        logger.debug(f"Entered initialized state with event: {event}\n\nserver params: {truncate(self.params)}")
        # 将所有异步Event全部clear
        self._create_session_success_event.clear()
        self._create_session_failure_event.clear()
        self._async_session_closed_event.clear()
        await self._close_task()

    async def aafter_initialize(self, event: EventData) -> None:
        """初始化后操作（可重写）"""
        logger.debug(f"After initialization actions with event: {event}\n\nserver params: {truncate(self.params)}")
        await self._trigger_state_change(event)

    async def list_tools(self) -> list[Tool]:
        """
        获取可用工具列表，MCP协议获取接口可分页，在此会尝试获取所有。对于大模型使用场景而言，需要一次性给出所有可用工具，没有必要分页，如果数据量过大，则属于设计问题。

        Returns:
            list[Tool]: 工具列表
        """
        if self.state != STATES.connected:
            raise ConnectionError("Not connected to server")
        tools: list[Tool] = []
        if self.initialize_result and self.initialize_result.capabilities.tools:
            asession = cast(ClientSession, await self.async_session)
            ret = await asession.list_tools()
            tools.extend(ret.tools)
            while ret.nextCursor:
                ret = await asession.list_tools(cursor=ret.nextCursor)
                tools.extend(ret.tools)
        return tools

    async def list_resources_page(self, cursor: str | None = None) -> tuple[list[Resource], str | None]:
        """
        中文: 单页透传 MCP `resources/list`；不做 scheme 过滤、不订阅、不穷举翻页。
        英文: Single-page transparent forward of MCP `resources/list`; no scheme filter, no subscription, no pagination exhaustion.

        与 `list_windows()` 严格独立——本方法保持单页语义，调用方自行翻页。供 v0.2 `client:get_resources` 透传使用。
        Strictly independent from `list_windows()` — this method preserves single-page semantics; callers paginate themselves.
        Used by the v0.2 `client:get_resources` transparent-forward path.

        Args:
            cursor (str | None): MCP 翻页游标；首次调用传 None / Pagination cursor; pass None for the first page.

        Returns:
            tuple[list[Resource], str | None]: (本页资源, 下一页游标——None 表示末页) /
                (resources on this page, next cursor — None when last page).

        Raises:
            MCPCapabilityNotSupportedError: MCP Server 未声明 `resources` 能力（→ 上层映射 4015）/
                MCP Server did not declare `resources` capability (mapped to 4015 upstream).
            ConnectionError: 客户端未连接 / Client not connected.
        """
        if self.state != STATES.connected:
            raise ConnectionError("Not connected to server")
        if not (self.initialize_result and self.initialize_result.capabilities.resources):
            raise MCPCapabilityNotSupportedError("MCP Server did not declare 'resources' capability")
        asession = cast(ClientSession, await self.async_session)
        ret = await asession.list_resources(cursor=cursor) if cursor is not None else await asession.list_resources()
        return list(ret.resources), ret.nextCursor

    async def list_windows(self) -> list[Resource]:
        """
        列出当前MCP服务可用的窗口资源列表。只要 MCP Server 声明了 resources 能力即可发现 window:// 资源。

        subscribe 是可选增强：当 resources.subscribe=True 时，会自动订阅 window:// 资源的变更通知。

        同时开发者需要注意维护好 window:// 状态

        Returns:
            list[Resource]: 当前可用的窗口类资源
        """
        # 只检查是否支持 resources 能力，不要求 subscribe
        if not (self.initialize_result and self.initialize_result.capabilities.resources):
            return []

        try:
            asession = cast(ClientSession, await self.async_session)
            # 中文: 支持分页获取资源；与 list_tools 一致，穷举所有页后再进行过滤与订阅
            # 英文: Support pagination; same as list_tools, exhaust all pages then filter and subscribe
            resources: list[Resource] = []
            ret = await asession.list_resources()
            if ret:
                resources.extend(ret.resources)
                while ret.nextCursor:
                    ret = await asession.list_resources(cursor=ret.nextCursor)
                    resources.extend(ret.resources)
            # 返回满足WindowURI协议要求的Resource
            # Return only resources that conform to WindowURI (window:// scheme)
            # v0.2 协议指南 §6.2 / §6.4：priority 来自 Resource.annotations.priority（float [0,1]，缺省 0.0）
            # v0.2 protocol §6.2/§6.4: priority comes from Resource.annotations.priority (float [0,1], default 0.0)
            filtered: list[tuple[Resource, float]] = []
            for res in resources:
                # 类型守卫：快速判定并过滤非 window:// 资源
                if not is_window_uri(res.uri):
                    continue
                annotations = getattr(res, "annotations", None)
                prio_raw = getattr(annotations, "priority", None) if annotations is not None else None
                if prio_raw is None:
                    prio: float = 0.0
                else:
                    try:
                        prio_f = float(prio_raw)
                    except (TypeError, ValueError):
                        logger.warning(
                            f"annotations.priority 非数值类型，按 0.0 处理 / non-numeric priority, treat as 0.0: {prio_raw!r}",
                        )
                        prio_f = 0.0
                    if not 0.0 <= prio_f <= 1.0:
                        logger.warning(
                            f"annotations.priority 越界 [0.0, 1.0]，按 0.0 处理 / out-of-range priority: {prio_f}",
                        )
                        prio_f = 0.0
                    prio = prio_f
                filtered.append((res, prio))

            # 同一 MCP 内按 priority 降序排序（仅在本客户端内比较）
            filtered.sort(key=lambda x: x[1], reverse=True)
            # subscribe 是可选增强：仅当 Server 声明支持订阅时才订阅 window:// 资源。
            # **幂等订阅**：仅订阅本会话尚未订阅过的 URI。否则重复 list_windows() 会反复 subscribe，
            # 令支持订阅的 Server 反复回发 resource_updated → 与 Agent 自动 GET_DESKTOP 串成自放大反馈环
            # （#110：慢 CI 上事件循环被风暴压垮、整套 e2e 偶发挂死）。已订阅集合在会话拆除时清空。
            # Subscribe is optional. **Idempotent**: only subscribe URIs not yet subscribed in this session;
            # re-subscribing would make a subscribe-capable server re-emit resource_updated, forming a
            # self-amplifying loop with the Agent's auto GET_DESKTOP refresh (#110 slow-CI e2e hang).
            if self.initialize_result.capabilities.resources.subscribe:
                for r, _ in filtered:
                    uri_str = str(r.uri)
                    if uri_str in self._subscribed_window_uris:
                        continue
                    await asession.subscribe_resource(r.uri)
                    self._subscribed_window_uris.add(uri_str)
            return [r for r, _ in filtered]
        except Exception as e:
            logger.error(f"Error listing resources for connector {truncate(self.params.model_dump(mode='json'))}: {e}", exc_info=True)
            return []

    async def get_window_detail(self, resource: Resource | str) -> ReadResourceResult:
        """
        中文: 读取单个窗口资源的详细内容（通过 MCP read_resource）。
        英文: Read details for a single window resource via MCP read_resource.

        Args:
            resource (Resource | str): 要读取的资源（或其 URI 字符串）。

        Returns:
            list[object]: 资源内容块列表（如 TextContent/BlobContent 等）。读取失败返回空列表。
        """
        try:
            asession = cast(ClientSession, await self.async_session)
            uri_val: AnyUrl
            if isinstance(resource, Resource):
                uri_val = resource.uri  # type: ignore[assignment]
            else:
                # 当传入为字符串时，交由底层进行校验/解析
                uri_val = AnyUrl(resource)  # type: ignore[call-arg]

            return await asession.read_resource(uri_val)
        except Exception as e:
            logger.error(f"Read window resource failed: {resource}: {e}", exc_info=True)
            return ReadResourceResult(contents=[TextResourceContents(text="获取资源失败", uri=resource.uri)])

    async def call_tool(self, tool_name: str, params: dict) -> CallToolResult:
        """
        运行指定工具（子类必须实现）

        在此不需要再考虑工具Alias的问题，由外层Manager进行处理，因此直接尝试调用触发MCP协议即可

        Args:
            tool_name (str): 被调用的工具名称
            params (dict): 调用参数

        Returns:
            CallToolResult: 调用结果 MCP 协议标准制定

        取消传播（#96 最后一公里）：本方法被取消（上层 :meth:`Computer.acancel_tool` → task.cancel，
        或 Manager 的 ``asyncio.wait_for`` 超时）时，会 best-effort 向远端 MCP Server 补发
        ``notifications/cancelled``，使远端有机会真正停止该工具执行——MCP 官方 SDK 取消客户端请求时
        **不**自动发此通知、也不暴露客户端 request_id（见 SDK issue #1410/#1419，至最新版本仍未修），故由我方补发。
        Cancellation propagation (#96 last mile): on cancellation this best-effort emits MCP
        ``notifications/cancelled`` to the remote server, since the official SDK neither emits it on
        client-request cancellation nor exposes the client request_id (SDK issues #1410/#1419).
        """
        if self.state != STATES.connected:
            raise ConnectionError("Not connected to server")
        session = await self.async_session
        # 捕获本次 MCP 请求 id：ClientSession.call_tool → send_request 在首个真正 await 之前同步自增分配
        # self._request_id；从此处读取到分配点之间无 await 挂起 → 读到的即本次将使用的 id（并发安全）。
        # Capture this request's MCP id: send_request assigns self._request_id synchronously before its first
        # real await; no suspension between this read and that assignment → the value read is the id used.
        request_id = getattr(session, "_request_id", None)
        try:
            return await session.call_tool(tool_name, params)
        except asyncio.CancelledError:
            # best-effort：通知远端取消该请求；不阻塞取消展开。/ best-effort notify remote; never block teardown.
            if request_id is not None:
                await self._emit_mcp_cancelled(session, request_id)
            raise

    async def _emit_mcp_cancelled(self, session: ClientSession, request_id: int) -> None:
        """向远端 MCP Server best-effort 发送 ``notifications/cancelled``（#96 取消最后一公里）。

        在调用方被取消的展开期内发送：``asyncio.shield`` 防止发送本身被二次取消打断，``wait_for`` 限时避免拖住
        teardown，``suppress`` 兜底会话已关闭等异常。MCP 取消为协作式（server 可忽略），故全程 best-effort。
        Best-effort emit of MCP ``notifications/cancelled``. Shielded against re-cancellation, time-boxed to not
        stall teardown, and fully suppressed (the session may already be closing). Cooperative per MCP spec.
        """

        async def _send() -> None:
            with suppress(Exception):
                await session.send_notification(
                    ClientNotification(
                        CancelledNotification(
                            params=CancelledNotificationParams(requestId=request_id, reason="A2C tool_call cancelled"),
                        ),
                    ),
                    related_request_id=request_id,
                )

        with suppress(Exception):
            # asyncio.shield 内部已将协程包装为 Task，无需再 ensure_future / shield already wraps the coro in a Task.
            await asyncio.wait_for(asyncio.shield(_send()), timeout=2.0)

    async def _close_task(self) -> None:
        """优雅关闭 keep-alive 任务及其持有的 MCP 会话 / stdio 子进程。

        中文（三步，确保 ``Computer.shutdown()`` 永不被卡死子进程拖死）：
          1. ``_close_event.set()`` 触发任务收尾（其 ``finally`` 运行 ``_aexit_stack.aclose()``，
             在**非取消**上下文中让 mcp 的 graceful→SIGTERM→SIGKILL 子进程回收得以生效）；
          2. 以 ``asyncio.wait_for`` 限时等待，并 ``asyncio.shield`` 防止外层取消把 CancelledError
             灌进收尾流程（否则重蹈强杀被跳过的覆辙）；
          3. 超时则按 PID「兑底强杀」子进程树，再有限等待；仍不结束则放弃任务并补发 closed 事件。
        English: signal graceful close, bounded shielded wait, then PID-based force-kill fallback so a
        wedged child can never hang shutdown.
        """
        task = self._session_keep_alive_task
        if task is None or task.done():
            return
        # 1. 触发优雅关闭 / signal graceful close
        self._close_event.set()
        # 错误路径（aerror→on_enter_error→_close_task 在任务自身上下文内调用）不能 await 自己
        # The error path may invoke this from within the task itself; never await self.
        if asyncio.current_task() is task:
            return
        # 2. 限时等待收尾（shield 防外层取消污染收尾流程）/ bounded wait, shielded from outer cancel
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_TEARDOWN_TIMEOUT)
            return
        except TimeoutError:
            logger.error(
                f"MCP client teardown exceeded {_TEARDOWN_TIMEOUT}s; force-killing child process tree "
                f"(pids={self._child_pids or 'none'}). server params: {truncate(self.params)}",
            )
        except asyncio.CancelledError:
            logger.debug("Session keep-alive task close-wait was cancelled")
            return
        except Exception as e:
            logger.error(f"Session keep-alive task failed: {e}", exc_info=True)
            return
        # 3. 兑底强杀 + 再次有限等待 / force-kill fallback then bounded re-wait
        await self._aforce_kill()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_TEARDOWN_TIMEOUT)
        except asyncio.CancelledError:
            self._async_session_closed_event.set()
        except Exception as e:  # noqa: BLE001 - 兜底：放弃任务，绝不让 shutdown 挂死
            logger.error(f"MCP client teardown still pending after force-kill; abandoning task: {e}")
            task.cancel()
            # 子进程已强杀，补发 closed 事件，避免 on_enter_disconnected 的 closed 等待挂死
            # Child already force-killed; ensure closed-event is set so disconnect doesn't hang.
            self._async_session_closed_event.set()

    async def _aforce_kill(self) -> None:
        """兑底强杀本 client 启动的子进程树（仅 stdio 传输有子进程；其它传输为 no-op）。

        best-effort，**绝不抛出**。Force-kill this client's child tree (stdio only; no-op otherwise).
        """
        pids = set(self._child_pids)
        if not pids:
            return
        force_kill_pids(pids)
