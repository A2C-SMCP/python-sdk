# filename: client.py
# @Time    : 2025/8/17 16:55
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
import base64
from typing import Any

from mcp.types import CallToolResult, Resource
from pydantic import TypeAdapter
from socketio import AsyncClient

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.computer.blob import (
    BlobHandleError,
    BlobHandleInvalidError,
    decode_blob_handle,
)
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.base_client import MCPCapabilityNotSupportedError, MCPServerNotFoundError
from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.smcp import (
    GET_BLOB_EVENT,
    GET_CONFIG_EVENT,
    GET_DESKTOP_EVENT,
    GET_RESOURCES_EVENT,
    GET_TOOLS_EVENT,
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_EVENT,
    UPDATE_DESKTOP_EVENT,
    UPDATE_TOOL_LIST_EVENT,
    A2CResource,
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
    GetToolsReq,
    GetToolsRet,
    LeaveOfficeReq,
    MCPServerInput,
    ResourceAnnotations,
    ToolCallReq,
    UpdateComputerConfigReq,
)
from a2c_smcp.smcp import (
    MCPServerConfig as SMCPServerConfigDict,
)
from a2c_smcp.utils.handshake import (
    DEFAULT_HANDSHAKE_TRANSPORTS,
    HANDSHAKE_CONNECT_ERRORS,
    apply_polling_first_guard,
    build_handshake_url,
    build_protocol_version_error,
    extract_4008_payload,
)
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# 默认鉴权 HTTP header 名（SDK 侧默认，可由调用方覆盖）
# Default auth HTTP header name for the SDK (consumers may override)
DEFAULT_AUTH_HEADER_NAME = "access_token"


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
        auth_header_name: str = DEFAULT_AUTH_HEADER_NAME,
        **kwargs: Any,
    ) -> None:  # noqa: E112
        """
        初始化Computer侧Socket.IO客户端
        Initialize Computer-side Socket.IO client

        Args:
            computer (Computer): 绑定的Computer实例 / Bound Computer instance
            namespace (str): Socket.IO命名空间，默认 ``/smcp`` / Socket.IO namespace, default ``/smcp``
            auth_header_name (str): 鉴权 HTTP header 名，默认 ``access_token``。
                连接时若通过 headers 传入该字段，将作为鉴权凭据转发给 Server。/
                Auth HTTP header name, default ``access_token``. When present in headers at connect time,
                it is forwarded as credential to the Server.
        """
        super().__init__(*args, **kwargs)
        self.computer = computer
        # 实例级握手配置 / Per-instance handshake config
        self._namespace = namespace
        self._auth_header_name = auth_header_name
        # 将客户端以 weakref 方式绑定回 Computer，避免循环强引用
        self.computer.socketio_client = self
        self.on(TOOL_CALL_EVENT, self.on_tool_call, namespace=self._namespace)
        self.on(GET_TOOLS_EVENT, self.on_get_tools, namespace=self._namespace)
        self.on(GET_CONFIG_EVENT, self.on_get_config, namespace=self._namespace)
        self.on(GET_DESKTOP_EVENT, self.on_get_desktop, namespace=self._namespace)
        self.on(GET_RESOURCES_EVENT, self.on_get_resources, namespace=self._namespace)
        self.on(GET_BLOB_EVENT, self.on_get_blob, namespace=self._namespace)
        self.office_id: str | None = None

    @property
    def namespace(self) -> str:
        """
        返回当前实例使用的 Socket.IO 命名空间
        Return the Socket.IO namespace used by this instance
        """
        return self._namespace

    @property
    def auth_header_name(self) -> str:
        """
        返回当前实例使用的鉴权 HTTP header 名
        Return the auth HTTP header name used by this instance
        """
        return self._auth_header_name

    async def connect(self, url: str, *args: Any, **kwargs: Any) -> None:
        """
        覆盖 ``AsyncClient.connect``：注入协议版本握手，使所有调用点（CLI / 交互式 / 测试）
        自动合规，无需各处重复拼接。
        Override ``AsyncClient.connect`` to inject the protocol version handshake so every
        call site (CLI / interactive / tests) is automatically compliant without duplication.

        - 协议 MUST：自动从 ``PROTOCOL_VERSION`` 常量拼接 ``a2c_version``（保留调用方既有 query）
        - 协议 §1 polling-first MUST 护栏：调用方显式 WS-only 不静默放行，强制重注入 polling-first
        - 捕获 4008 → 主动 ``disconnect()`` → 抛 :class:`ProtocolVersionError`；非 4008 保持原异常
        """
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

    async def join_office(self, office_id: str) -> None:
        """
        加入一个Office（Socket.IO中的Room）
        Join an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID，在A2C-smcp协议中，OfficeID即为Socket.IO RoomID / Room ID, in A2C-smcp protocol,
                OfficeID is Socket.IO RoomID

        Raises:
            RuntimeError: 当加入房间失败时（例如重名）/ When joining room fails (e.g., duplicate name)
        """
        # 提前设置 office_id，避免服务器广播事件时 office_id 仍为 None 的时序竞争问题
        # Set office_id before sending request to avoid race condition when server broadcasts events
        self.office_id = office_id

        try:
            # 使用 call 方法等待服务器返回结果 / Use call method to wait for server response
            result = await self.call(
                JOIN_OFFICE_EVENT,
                EnterOfficeReq(office_id=office_id, role="computer", name=self.computer.name),
                namespace=self._namespace,
            )

            # 检查返回结果 / Check return result
            if isinstance(result, (list, tuple)) and len(result) >= 2:
                success, error_msg = result[0], result[1]
                if not success:
                    # 加入失败，重置 office_id / Reset office_id on failure
                    self.office_id = None
                    raise RuntimeError(f"加入房间失败 / Failed to join office: {error_msg}")
            elif not result:
                # 加入失败，重置 office_id / Reset office_id on failure
                self.office_id = None
                raise RuntimeError("加入房间失败：服务器未返回结果 / Failed to join office: No response from server")
        except Exception:
            # 发生异常时重置 office_id / Reset office_id on exception
            self.office_id = None
            raise

    async def leave_office(self, office_id: str) -> None:
        """
        离开一个Office（Socket.IO中的Room）

        Args:
            office_id (str): 房间ID
        """
        await self.emit(LEAVE_OFFICE_EVENT, LeaveOfficeReq(office_id=office_id))
        self.office_id = None

    async def emit_update_config(self) -> None:
        """
        当前MCP配置更新时需要触发此事件向信令服务器推送，进而触发Agent端的配置更新

        不需要传递当前的配置参数，因为Agnet会通过其它接口进行刷新
        """
        if self.office_id:
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
        if self.office_id:
            await self.emit(UPDATE_TOOL_LIST_EVENT, UpdateComputerConfigReq(computer=self.computer.name))

    async def emit_refresh_desktop(self) -> None:
        """
        桌面刷新触发：当资源列表或资源内容变化时，通知信令服务器。服务端会广播 notify:update_desktop。
        Desktop refresh trigger: notify server when resources list/content changed; server will broadcast notify:update_desktop.
        """
        if self.office_id:
            await self.emit(UPDATE_DESKTOP_EVENT, UpdateComputerConfigReq(computer=self.computer.name))

    async def on_tool_call(self, data: ToolCallReq) -> dict:
        """
        信令服务器通知计算机端，有工具调用请求

        Args:
            data (ToolCallReq): 请求数据

        Returns:
            dict: 工具调用结果的字典表示（JSON 可序列化）
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
            return ret.model_dump(mode="json")
        except Exception as e:
            error_result = CallToolResult(isError=True, structuredContent={"error": str(e), "error_type": type(e).__name__}, content=[])
            return error_result.model_dump(mode="json")

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
        # 从 Computer 中获取初始化时传入的配置集合（不可变元组）
        # From Computer, get the immutable tuple of initial MCP server configs
        for cfg in self.computer.mcp_servers:
            # 使用强校验转换为协议定义（中英文）/ Validate strictly to protocol definition (bilingual)
            # 若类型不匹配，抛出异常，属于硬性 Bug / If mismatched, raise to surface a hard bug.
            validated_server: dict = TypeAdapter(SMCPServerConfigDict).validate_python(cfg.model_dump(mode="json"), from_attributes=True)
            servers[cfg.name] = validated_server

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
                mcp_server_name=mcp_server,
            )
        except MCPCapabilityNotSupportedError as e:
            logger.warning(f"client:get_resources MCP Server '{mcp_server}' 未声明 resources 能力 / capability missing: {e}")
            return ErrorPayload(
                code=int(ErrorCode.MCP_CAPABILITY_NOT_SUPPORTED),
                message="MCP Server does not support 'resources' capability",
                mcp_server_name=mcp_server,
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
        # office/role 隔离：Server 已保证同房间路由，但 ``computer`` 标识仍需匹配
        # office/role isolation: Server guarantees same-room routing, but ``computer`` MUST match
        if self.computer.name != data["computer"]:
            raise SMCPNamespaceError("计算机标识不匹配")

        handle = data["blob_handle"]
        chunk_offset = data.get("chunk_offset", 0)
        max_chunk_bytes_req = data.get("max_chunk_bytes")
        max_chunk_bytes = self.computer.blob_thresholds.clamp_chunk(max_chunk_bytes_req)

        # 1) 解码句柄 → kind 派发 / Decode handle → kind dispatch
        try:
            kind, payload = decode_blob_handle(handle)
        except BlobHandleInvalidError as e:
            logger.warning(f"client:get_blob invalid handle: {e}")
            return _blob_error(reason="invalid_handle")

        resolver = self.computer.blob_resolvers.get(kind)
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
        end = min(chunk_offset + max_chunk_bytes, resolved.total_size)
        chunk = resolved.payload[chunk_offset:end]
        eof = end == resolved.total_size
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


def _blob_error(*, reason: str) -> ErrorPayload:
    """构造 ``4018 Blob Not Accessible`` flat ErrorPayload，``reason`` 经 ``details`` 下沉。
    Build ``4018`` flat ErrorPayload with ``reason`` under ``details`` (per error-handling.md §4018).
    """
    return ErrorPayload(
        code=int(ErrorCode.BLOB_NOT_ACCESSIBLE),
        message="Blob not accessible",
        details={"reason": reason},
    )
