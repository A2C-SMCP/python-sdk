"""
* 文件名: client
* 作者: JQQ
* 创建日期: 2025/9/30
* 最后修改日期: 2025/9/30
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, mcp, asyncio
* 描述: 异步Agent客户端实现 / Asynchronous Agent client implementation
"""

from typing import Any

from mcp.types import CallToolResult, TextContent
from socketio import AsyncClient
from socketio.exceptions import ConnectionError as SioConnectionError

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.agent.auth import AgentAuthProvider
from a2c_smcp.agent.base import BaseAgentClient
from a2c_smcp.agent.errors import raise_for_error_payload
from a2c_smcp.agent.types import AsyncAgentEventHandler
from a2c_smcp.exceptions import ProtocolVersionError
from a2c_smcp.smcp import (
    CANCEL_TOOL_CALL_EVENT,
    ENTER_OFFICE_NOTIFICATION,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_TOOLS_EVENT,
    LEAVE_OFFICE_NOTIFICATION,
    LIST_ROOM_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
    AgentCallData,
    EnterOfficeNotification,
    GetDeskTopRet,
    GetResourcesRet,
    GetToolsRet,
    LeaveOfficeNotification,
    ListRoomReq,
    SessionInfo,
    UpdateMCPConfigNotification,
)
from a2c_smcp.utils.handshake import (
    DEFAULT_HANDSHAKE_TRANSPORTS,
    build_handshake_url,
    enforce_polling_first,
    extract_4008_payload,
)
from a2c_smcp.utils.logger import ContextLogger, get_logger

logger = get_logger("agent")


class AsyncSMCPAgentClient(AsyncClient, BaseAgentClient):
    """
    SMCP协议的异步Agent客户端实现
    Asynchronous SMCP protocol Agent client implementation
    """

    def __init__(
        self,
        auth_provider: AgentAuthProvider,
        event_handler: AsyncAgentEventHandler | None = None,
        *args: Any,
        namespace: str = SMCP_NAMESPACE,
        **kwargs: Any,
    ) -> None:
        """
        初始化异步SMCP Agent客户端
        Initialize asynchronous SMCP Agent client

        Args:
            auth_provider (AgentAuthProvider): 认证提供者 / Authentication provider
            event_handler (AsyncAgentEventHandler | None): 异步事件处理器 / Async event handler
            namespace (str): Socket.IO命名空间，默认 ``/smcp``。
                事件处理器注册与后续 emit/call 均使用此值 /
                Socket.IO namespace, default ``/smcp``. Used for handler registration
                and all subsequent emit/call sites.
            *args: AsyncClient构造参数 / AsyncClient constructor arguments
            **kwargs: AsyncClient构造参数 / AsyncClient constructor arguments
        """
        # 分别初始化 AsyncClient 与 BaseAgentClient
        # Initialize AsyncClient and BaseAgentClient respectively
        AsyncClient.__init__(self, *args, **kwargs)
        BaseAgentClient.__init__(self, auth_provider=auth_provider, event_handler=event_handler)

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

    async def emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        """
        异步发送事件，包含事件验证逻辑
        Async send event with event validation logic

        Args:
            event (str): 事件名称 / Event name
            data (Any): 事件数据 / Event data
            namespace (Optional[str]): 命名空间 / Namespace
            callback (Any): 回调函数 / Callback function
        """
        # 验证事件合法性
        # Validate event legality
        self.validate_emit_event(event)

        # 调用 AsyncClient 的 emit 方法
        # Call AsyncClient's emit method
        await AsyncClient.emit(self, event, data, namespace, callback)

    async def call(self, event: str, data: Any = None, namespace: str | None = None, timeout: int = 60) -> Any:
        """
        异步调用事件并等待响应
        Async call event and wait for response

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

        # 调用 AsyncClient 的 call 方法
        # Call AsyncClient's call method
        return await AsyncClient.call(self, event, data, namespace, timeout)

    # 事件合法性校验复用 BaseAgentClient.validate_emit_event
    # Reuse BaseAgentClient.validate_emit_event for event validation

    async def connect_to_server(
        self,
        url: str,
        namespace: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        异步连接到SMCP服务器
        Async connect to SMCP server

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

        # 协议 §1 polling-first MUST 护栏：调用方显式 WS-only 不静默放行
        # Protocol §1 polling-first MUST guard: caller-forced WS-only is not silently allowed
        effective_transports, overridden = enforce_polling_first(connect_kwargs.get("transports"))
        if overridden:
            logger.warning(
                "调用方显式 WS-only transports 违反 versioning.md §1 polling-first MUST；已强制"
                "重注入 polling-first（仍保留 websocket 供握手后升级）/ caller-forced WS-only "
                "violates §1 polling-first MUST; re-injected polling-first",
            )
        connect_kwargs["transports"] = effective_transports

        logger.info(f"Connecting to SMCP server at {url} (a2c_version={PROTOCOL_VERSION})")
        try:
            await self.connect(handshake_url, **connect_kwargs)
        except SioConnectionError as e:
            payload = extract_4008_payload(e)
            if payload is None:
                # 非协议版本错误：保持原异常 / not a version error: preserve the original exception
                raise
            # 协议 MUST：先主动断开再抛异常，防止底层库自动重连触发 4008 死循环
            # Protocol MUST: proactively disconnect before raising, preventing an auto-reconnect 4008 loop
            await self.disconnect()
            raise ProtocolVersionError(
                client_version=payload.get("client_version"),
                server_version=payload.get("server_version"),
                min_supported=payload.get("min_supported"),
                max_supported=payload.get("max_supported"),
                message=payload.get("message", "Protocol version mismatch"),
            ) from e
        logger.info("Connected to SMCP server successfully")

    async def emit_tool_call(self, computer: str, tool_name: str, params: dict, timeout: int) -> CallToolResult:
        """
        异步发起SMCP工具调用
        Async initiate SMCP tool call

        Args:
            computer (str): 远程计算机名称 / Remote computer name
            tool_name (str): 工具名称 / Tool name
            params (dict): 工具调用参数 / Tool call parameters
            timeout (int): 超时时间 / Timeout duration

        Returns:
            CallToolResult: MCP协议工具调用结果 / MCP protocol tool call result
        """
        req = self.create_tool_call_request(computer, tool_name, params, timeout)
        ctx = ContextLogger(logger, {"computer": computer, "tool": tool_name, "req_id": req["req_id"]})

        try:
            ctx.debug("Calling tool")
            res = await self.call(TOOL_CALL_EVENT, req, timeout=timeout, namespace=self._namespace)
            return CallToolResult.model_validate(res, by_name=True)

        except TimeoutError:
            # 发送取消请求
            # Send cancel request
            agent_config = self.auth_provider.get_agent_config()
            cancel_data = AgentCallData(agent=agent_config["agent"], req_id=req["req_id"])
            await self.emit(CANCEL_TOOL_CALL_EVENT, cancel_data, namespace=self._namespace)
            return self.handle_tool_call_timeout(req["req_id"])

        except Exception as e:
            ctx.error(f"Tool call failed: {e}", exc_info=True)
            return CallToolResult(
                content=[TextContent(text=f"工具调用失败 / Tool call failed: {str(e)}", type="text")],
                isError=True,
            )

    async def get_tools_from_computer(self, computer: str, timeout: int = 20) -> GetToolsRet:
        """
        异步从指定计算机获取工具列表
        Async get tools list from specified computer

        Args:
            computer (str): 计算机名称 / Computer Name
            timeout (int): 超时时间 / Timeout duration

        Returns:
            GetToolsRet: 工具列表响应 / Tools list response
        """
        req = self.create_get_tools_request(computer)

        try:
            logger.debug(f"Getting tools from computer {computer}")
            response = await self.call(GET_TOOLS_EVENT, req, namespace=self._namespace, timeout=timeout)

            # 验证响应
            # Validate response
            if response.get("req_id") != req["req_id"]:
                raise ValueError("Invalid response with mismatched req_id")

            return GetToolsRet(tools=response.get("tools", []), req_id=response["req_id"])

        except Exception as e:
            logger.error(f"Failed to get tools from computer {computer}: {e}", exc_info=True)
            raise

    def register_event_handlers(self) -> None:
        """
        注册SMCP协议事件处理器
        Register SMCP protocol event handlers
        """
        self.on(ENTER_OFFICE_NOTIFICATION, self._on_computer_enter_office, namespace=self._namespace)
        self.on(LEAVE_OFFICE_NOTIFICATION, self._on_computer_leave_office, namespace=self._namespace)
        self.on(UPDATE_CONFIG_NOTIFICATION, self._on_computer_update_config, namespace=self._namespace)
        self.on(UPDATE_DESKTOP_NOTIFICATION, self._on_desktop_updated, namespace=self._namespace)

    async def _on_computer_enter_office(self, data: EnterOfficeNotification) -> None:
        """
        处理Computer加入办公室事件的内部方法
        Internal method to handle Computer enter office event
        """
        try:
            # 使用父类的异步处理方法
            # Use parent class async handling method
            await self.handle_computer_enter_office(data)

            # 自动获取工具列表
            # Automatically get tools list
            computer = self.validate_office_data(data)
            tools_response = await self.get_tools_from_computer(computer)
            await self.process_tools_response(tools_response, computer)

        except Exception as e:
            logger.error(f"Error in _on_computer_enter_office: {e}", exc_info=True)

    async def _on_computer_leave_office(self, data: LeaveOfficeNotification) -> None:
        """
        处理Computer离开办公室事件的内部方法
        Internal method to handle Computer leave office event
        """
        try:
            # 使用父类的异步处理方法
            # Use parent class async handling method
            await self.handle_computer_leave_office(data)

        except Exception as e:
            logger.error(f"Error in _on_computer_leave_office: {e}", exc_info=True)

    async def _on_computer_update_config(self, data: UpdateMCPConfigNotification) -> None:
        """
        处理Computer更新配置事件的内部方法
        Internal method to handle Computer update config event
        """
        try:
            # 使用父类的异步处理方法
            # Use parent class async handling method
            await self.handle_computer_update_config(data)

            # 重新获取工具列表
            # Re-get tools list
            computer = data["computer"]
            tools_response = await self.get_tools_from_computer(computer)
            await self.process_tools_response(tools_response, computer)

        except Exception as e:
            logger.error(f"Error in _on_computer_update_config: {e}", exc_info=True)

    async def get_desktop_from_computer(
        self,
        computer: str,
        *,
        size: int | None = None,
        window: str | None = None,
        timeout: int = 20,
    ) -> GetDeskTopRet:
        """
        异步从指定计算机获取桌面信息
        Async get desktop from specified computer
        """
        req = self.create_get_desktop_request(computer, size=size, window=window)
        logger.debug(f"Getting desktop from computer {computer}, size={size}, window={window}")
        response = await self.call(GET_DESKTOP_EVENT, req, namespace=self._namespace, timeout=timeout)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for desktop")
        return GetDeskTopRet(desktops=response.get("desktops", []), req_id=response["req_id"])

    async def get_resources(
        self,
        computer: str,
        mcp_server: str,
        cursor: str | None = None,
        timeout: int = 20,
    ) -> GetResourcesRet:
        """
        异步：透明转发获取指定 Computer 上某 MCP Server 的资源列表（含 cursor 翻页）。
        Async: transparently get a MCP Server's resource list on the target Computer (with cursor pagination).

        SDK **不**自动遍历翻页——cursor 由调用方控制：首次传 ``None``，响应含 ``next_cursor``
        时由调用方决定是否带该 cursor 继续请求（协议指南 §5.3 第 3 点）。
        The SDK does **not** auto-paginate — the cursor is caller-controlled: pass ``None`` first;
        when the response carries ``next_cursor`` the caller decides whether to request again with it
        (protocol guide §5.3 #3).

        Args:
            computer (str): 目标 Computer 名称 / Target Computer name
            mcp_server (str): 目标 MCP Server 名称 / Target MCP Server name
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
        response = await self.call(GET_RESOURCES_EVENT, req, namespace=self._namespace, timeout=timeout)
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

    async def _on_desktop_updated(self, data: dict) -> None:
        """
        处理桌面更新通知：默认自动拉取一次桌面。
        Handle desktop updated notification: automatically fetch desktop once.
        """
        try:
            computer = data.get("computer")
            if not computer:
                logger.warning("UPDATE_DESKTOP_NOTIFICATION missing 'computer'")
                return
            ret = await self.get_desktop_from_computer(computer)
            # 复用基类同步处理器（仅日志），未来可扩展异步回调
            self.process_desktop_response(ret, computer)
        except Exception as e:
            logger.error(f"Error handling desktop updated notification: {e}", exc_info=True)

    async def get_computers_in_office(self, office_id: str, timeout: int = 20) -> list[SessionInfo]:
        """
        异步获取指定房间内的所有Computer信息
        Async get all computers info in the specified office

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
            response = await self.call(LIST_ROOM_EVENT, req, namespace=self._namespace, timeout=timeout)

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
