"""
* 文件名: client
* 作者: JQQ
* 创建日期: 2025/9/30
* 最后修改日期: 2025/9/30
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, mcp, asyncio
* 描述: 异步Agent客户端实现 / Asynchronous Agent client implementation
"""

from typing import Any, cast

from mcp.types import CallToolResult, TextContent
from socketio import AsyncClient

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.agent import _blob_sideband as _sb
from a2c_smcp.agent.auth import AgentAuthProvider
from a2c_smcp.agent.base import BaseAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError, raise_for_error_payload
from a2c_smcp.agent.types import AsyncAgentEventHandler
from a2c_smcp.smcp import (
    CANCEL_TOOL_CALL_EVENT,
    ENTER_OFFICE_NOTIFICATION,
    GET_BLOB_EVENT,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_SKILL_EVENT,
    GET_SKILLS_EVENT,
    GET_TOOLS_EVENT,
    LEAVE_OFFICE_NOTIFICATION,
    LIST_ROOM_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
    UPDATE_SKILLS_NOTIFICATION,
    UPDATE_TOOL_LIST_NOTIFICATION,
    AgentCallData,
    EnterOfficeNotification,
    GetBlobRet,
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
from a2c_smcp.utils.blob import drain_blob
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

        # 协议 §1 polling-first MUST 护栏（统一接线，详见 handshake.apply_polling_first_guard）
        # Protocol §1 polling-first MUST guard (unified wiring)
        connect_kwargs["transports"] = apply_polling_first_guard(connect_kwargs.get("transports"), logger)

        logger.info(f"Connecting to SMCP server at {url} (a2c_version={PROTOCOL_VERSION})")
        try:
            await self.connect(handshake_url, **connect_kwargs)
        except HANDSHAKE_CONNECT_ERRORS as e:
            payload = extract_4008_payload(e)
            if payload is None:
                # 非协议版本错误：保持原异常 / not a version error: preserve the original exception
                raise
            # 协议 §4 MUST：先主动断开再抛异常，防止底层库自动重连触发 4008 死循环
            # Protocol §4 MUST: proactively disconnect before raising (anti reconnect-loop)
            await self.disconnect()
            raise build_protocol_version_error(payload) from e
        logger.info("Connected to SMCP server successfully")

    async def emit_tool_call(self, computer: str, tool_name: str, params: dict, timeout: int) -> CallToolResult:
        """
        异步发起SMCP工具调用
        Async initiate SMCP tool call

        v0.2.1 二进制一致性 / Binary consistency:
            返回前扫描 ``CallToolResult.content``，命中 ``_meta.a2c_blob_handle`` 的 content item
            **自动** :func:`drain_blob` 还原全量字节、回填 ``data`` (ImageContent) 或 ``blob``
            (EmbeddedResource.BlobResourceContents)，并清理 ``_meta`` 中的 ``a2c_*`` 字段——确保
            上层拿到的是与小图内联路径**完全等价**的 ``CallToolResult``。
            **不实现 = 大二进制工具结果静默变空**（协议关键破坏性点）。

            Pre-return: scan ``CallToolResult.content`` and **automatically** drain items carrying
            ``_meta.a2c_blob_handle`` so callers see binary-equivalent ``CallToolResult`` regardless
            of inline-vs-handle routing. Skipping this would silently empty large binary outputs.

        协议依据 / Protocol: blob-transfer.md §5 (生产者通道接入契约) +
        data-structures.md §BlobHandle (载体随响应结构而异表).

        Args:
            computer (str): 远程计算机名称 / Remote computer name
            tool_name (str): 工具名称 / Tool name
            params (dict): 工具调用参数 / Tool call parameters
            timeout (int): 超时时间 / Timeout duration

        Returns:
            CallToolResult: MCP协议工具调用结果（二进制旁路已还原）/ Result with binary sideband resolved.
        """
        req = self.create_tool_call_request(computer, tool_name, params, timeout)
        ctx = ContextLogger(logger, {"computer": computer, "tool": tool_name, "req_id": req["req_id"]})

        try:
            ctx.debug("Calling tool")
            res = await self.call(TOOL_CALL_EVENT, req, timeout=timeout, namespace=self._namespace)
            # 协议级错误（如 #99 目标 Computer 不存在 → flat ErrorPayload(404)）→ 抛 SMCPProtocolError，
            # 由下方专门分支转成干净 isError 结果（与 6 个 get_* 方法的 raise_for_error_payload 调用点一致）。
            # Protocol errors (e.g. #99 computer-not-found → flat ErrorPayload(404)) → raise, handled below.
            raise_for_error_payload(res)
            # v0.2.1：返回前 drain content items 的 _meta.a2c_blob_handle / drain binary sideband pre-return
            res = await self._resolve_tool_call_binary_sideband(res, computer)
            return CallToolResult.model_validate(res, by_name=True)

        except TimeoutError:
            # 发送取消请求
            # Send cancel request
            agent_config = self.auth_provider.get_agent_config()
            cancel_data = AgentCallData(agent=agent_config["agent"], req_id=req["req_id"])
            await self.emit(CANCEL_TOOL_CALL_EVENT, cancel_data, namespace=self._namespace)
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
        # v0.2.1 SKILL 集合更新自动重拉（仿 notify:update_*）/ v0.2.1 auto-refresh on SKILL set change
        self.on(UPDATE_SKILLS_NOTIFICATION, self._on_skills_updated, namespace=self._namespace)
        # #127 MCP 运行期工具集变化自动重拉（仿 notify:update_config）/ #127 auto-refresh on runtime tool-set change
        self.on(UPDATE_TOOL_LIST_NOTIFICATION, self._on_computer_update_tool_list, namespace=self._namespace)

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

    async def _on_computer_update_tool_list(self, data: UpdateToolListNotification) -> None:
        """
        处理Computer工具列表更新事件的内部方法（#127）
        Internal handler for a Computer's tool-list-changed event (#127)

        镜像 ``_on_computer_update_config`` 的三段式：先派发预清回调 ``on_computer_update_tool_list``
        （供消费方清理旧工具，见 base.handle_computer_update_tool_list），再全量回拉 ``client:get_tools``，
        最后经 ``process_tools_response`` 触发 ``on_tools_received`` 重加。纯 MCP 运行期工具增删
        （不伴随 config 变更）由此在 Agent 侧可见。

        Mirrors ``_on_computer_update_config``: pre-clean hook → full refetch → ``on_tools_received`` re-add.
        """
        try:
            # 使用父类的异步处理方法（预清回调）
            # Use parent class async handling method (pre-clean hook)
            await self.handle_computer_update_tool_list(data)

            # 重新获取工具列表
            # Re-get tools list
            computer = data["computer"]
            tools_response = await self.get_tools_from_computer(computer)
            await self.process_tools_response(tools_response, computer)

        except Exception as e:
            logger.error(f"Error in _on_computer_update_tool_list: {e}", exc_info=True)

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

    async def get_skills(self, computer: str, timeout: int = 20) -> GetSkillsRet:
        """异步获取目标 Computer 的 SKILL 清单（v0.2.1，轻量元数据，不含 SKILL.md body）.

        Async get the target Computer's SKILL inventory (lightweight metadata, no SKILL.md body).

        协议依据 / Protocol: events.md §client:get_skills；data-structures.md §GetSkillsRet.

        Raises:
            SMCPProtocolError: ``4014`` SKILL ``name`` 复用（Computer 端 Registry 异常等罕见路径）
            ValueError: 响应 ``req_id`` 不匹配
        """
        req = self.create_get_skills_request(computer)
        logger.debug(f"Getting skills from computer {computer}")
        response = await self.call(GET_SKILLS_EVENT, req, namespace=self._namespace, timeout=timeout)
        raise_for_error_payload(response)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for skills")
        ret: GetSkillsRet = {"skills": response.get("skills", []), "req_id": response["req_id"]}
        return ret

    async def get_skill(
        self,
        computer: str,
        name: str,
        rel_path: str | None = None,
        timeout: int = 30,
    ) -> GetSkillRet:
        """异步获取 SKILL 包内单个资源（v0.2.1）.

        响应分支自动还原 / Response branches auto-resolved:
          - 文本内联 ``body`` → 直接返回（含 ``body`` 字段）
          - 二进制或过大文本 ``blob_handle`` → **自动**通过 :func:`drain_blob` 拉取全量字节，
            填回 ``body`` 字段（base64 解码后的字节用 ``latin-1`` 还原为 str 不安全；故仅文本类
            MIME 才会回填 ``body``；二进制 MIME 保留 ``blob_handle`` 供调用方走 :func:`get_blob`
            或 :func:`drain_blob` 自取字节）.

        Branches: inline ``body`` returned as-is; ``blob_handle`` auto-drained via :func:`drain_blob`,
        text MIME fills ``body``, binary MIME keeps ``blob_handle`` for caller's byte handling.

        协议依据 / Protocol: events.md §client:get_skill；data-structures.md §GetSkillRet
        （``body`` 与 ``blob_handle`` 恰一存在）.

        Raises:
            SMCPProtocolError: ``4014`` name 未命中 / ``4016`` name 格式非法 / ``4017`` rel_path 不可达
        """
        req = self.create_get_skill_request(computer, name, rel_path)
        logger.debug(f"Getting skill {name!r} rel_path={rel_path!r} from computer {computer}")
        response = await self.call(GET_SKILL_EVENT, req, namespace=self._namespace, timeout=timeout)
        raise_for_error_payload(response)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for skill")
        # body / blob_handle 恰一存在；blob_handle 分支按 MIME 决定是否自动 drain 回填 body
        # Exactly one of body / blob_handle. blob_handle branch auto-drains for text MIME only.
        ret: GetSkillRet = dict(response)  # type: ignore[assignment]
        mime_type = str(response.get("mime_type", ""))
        if "blob_handle" in response and "body" not in response and is_text_mime(mime_type):
            payload, _ = await drain_blob(
                self._make_blob_call(),
                computer,
                response["blob_handle"],
            )
            decoded = _sb.decode_text_body(payload)
            if decoded is not None:
                ret["body"] = decoded
                ret.pop("blob_handle", None)
            else:
                # 解码失败保留 blob_handle 让调用方走二进制路径 / Decode failure → keep handle for binary path
                logger.warning(f"get_skill text body decode failed for name={name!r}; keeping blob_handle")
        return ret

    async def get_blob(
        self,
        computer: str,
        blob_handle: str,
        *,
        chunk_offset: int = 0,
        max_chunk_bytes: int | None = None,
        timeout: int = 30,
    ) -> GetBlobRet:
        """异步通用二进制拉取**单块**入口（低层 API）.

        Async generic binary single-chunk pull (low-level API).

        用于需要细粒度控制 ``chunk_offset`` / ``max_chunk_bytes`` 的场景（如自定义并发 drain）.
        For full-blob drain，调用方 SHOULD 使用 :func:`a2c_smcp.utils.blob.drain_blob`.

        协议依据 / Protocol: events.md §client:get_blob；blob-transfer.md §3 (parallel-safe per offset).
        """
        req = self.create_get_blob_request(computer, blob_handle, chunk_offset, max_chunk_bytes)
        logger.debug(f"Getting blob from computer {computer} offset={chunk_offset}")
        response = await self.call(GET_BLOB_EVENT, req, namespace=self._namespace, timeout=timeout)
        raise_for_error_payload(response)
        if response.get("req_id") != req["req_id"]:
            raise ValueError("Invalid response with mismatched req_id for blob")
        return GetBlobRet(**response)

    def _make_blob_call(self) -> Any:
        """构造 :func:`drain_blob` 的 ``call`` 适配器（绑定本 client 的 socketio 与 namespace）.

        Build a ``call`` adapter for :func:`drain_blob` bound to this client's socketio + namespace.
        """

        async def _call(computer: str, blob_handle: str, chunk_offset: int, max_chunk_bytes: int) -> dict:
            req = self.create_get_blob_request(computer, blob_handle, chunk_offset, max_chunk_bytes)
            ack = await self.call(GET_BLOB_EVENT, req, namespace=self._namespace)
            return cast(dict, ack)

        return _call

    async def _resolve_tool_call_binary_sideband(self, raw: Any, computer: str) -> Any:
        """扫描 ``CallToolResult`` 字典中 content items 的 ``_meta.a2c_blob_handle``，
        :func:`drain_blob` 还原全量字节并回填 ``data`` / ``blob``，清理 ``_meta.a2c_*``.

        Scan content items for ``_meta.a2c_blob_handle``; drain via :func:`drain_blob` and inject
        ``data`` / ``blob`` back; strip ``_meta.a2c_*`` keys so caller sees a normal inline result.

        协议依据 / Protocol: blob-transfer.md §5 + data-structures.md §BlobHandle.

        实现策略 / Strategy: 通过 :mod:`._blob_sideband` 纯函数收敛 async/sync 共享的结构变换，
        本方法只保留 ``await drain_blob`` 与错误兜底两处真正异步差异.
        Pure structural transforms live in ``_blob_sideband`` (#30 pattern); this method retains
        only the ``await drain_blob`` differentiator and the per-item error-tolerance shell.
        """
        call = self._make_blob_call()
        for item, meta, handle in _sb.extract_sideband_handles(raw):
            try:
                payload, _mime = await drain_blob(call, computer, handle)
            except Exception as e:  # noqa: BLE001 — 任何 drain 失败均原样保留 + 警告，避免吞噬整轮 tool_call
                logger.warning(
                    f"tool_call binary sideband drain failed for handle={handle!r}: {e}; keeping _meta.a2c_blob_handle intact",
                )
                continue
            _sb.inject_payload_into_content_item(item, meta, payload)
        return raw

    async def _on_skills_updated(self, data: dict) -> None:
        """处理 SKILL 集合更新通知（v0.2.1）：默认自动重拉 ``client:get_skills`` 并回调 ``on_skills_received``.

        Handle ``notify:update_skills``: default behavior re-fetches ``client:get_skills`` once and
        dispatches ``on_skills_received`` to the configured event handler (mirrors the existing
        ``notify:update_*`` auto-refresh pattern; hook errors are isolated and never propagated).
        """
        try:
            computer = data.get("computer")
            if not computer:
                logger.warning("UPDATE_SKILLS_NOTIFICATION missing 'computer'")
                return
            ret = await self.get_skills(computer)
            skills = ret.get("skills", [])
            logger.info(f"Skills refreshed from computer {computer}: count={len(skills)}")
            # v0.2.1: 派发 on_skills_received（hook 异常独立隔离，不污染拉取链路）
            # Dispatch on_skills_received; hook errors are isolated and never propagated.
            if self.event_handler and hasattr(self.event_handler, "on_skills_received"):
                try:
                    await self.event_handler.on_skills_received(computer, skills, self)
                except Exception as hook_exc:
                    logger.error(
                        f"on_skills_received hook raised for computer {computer}: {hook_exc}",
                        exc_info=True,
                    )
        except Exception as e:
            logger.error(f"Error handling skills updated notification: {e}", exc_info=True)

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
