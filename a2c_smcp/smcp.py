# -*- coding: utf-8 -*-
# filename: smcp.py
# @Time    : 2025/8/8 12:35
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
from enum import IntEnum
from typing import Any, Literal, NotRequired, TypeAlias

from typing_extensions import TypedDict

from a2c_smcp import types
from a2c_smcp.types import SERVER_NAME, TOOL_NAME

SMCP_NAMESPACE = "/smcp"
# 除了notify:外的所有事件 服务端必须实现，因为由服务端转换或者执行完毕。而notify:*事件均由Server发出，因此Server不需要实现
# 客户端事件 由client:开头的事件ComputerClient必须全部实现，一般由AgentClient触发，由Server转发。client: 开头的事件，会由特定的某一个
# ComputerClient执行
# 一般data中会明确指定computer的sid用于执行此事件，如果需要多个client执行，一般是通过server:事件触发，广播至房间内的所有Computer
TOOL_CALL_EVENT = "client:tool_call"
GET_CONFIG_EVENT = "client:get_config"
GET_TOOLS_EVENT = "client:get_tools"
GET_DESKTOP_EVENT = "client:get_desktop"
# v0.2 新增：资源发现事件 / v0.2 added: resource discovery event
GET_RESOURCES_EVENT = "client:get_resources"
# 服务端事件 由server:开头的事件服务端执行
JOIN_OFFICE_EVENT = "server:join_office"
LEAVE_OFFICE_EVENT = "server:leave_office"
UPDATE_CONFIG_EVENT = "server:update_config"
UPDATE_TOOL_LIST_EVENT = "server:update_tool_list"
# 桌面刷新事件：当资源列表或资源内容变化时，由Computer端通知Server广播。
# 中文: 当需要让Agent刷新桌面时，由Computer触发此事件。英文: Computer emits this when Agent should refresh desktop.
UPDATE_DESKTOP_EVENT = "server:update_desktop"
CANCEL_TOOL_CALL_EVENT = "server:tool_call_cancel"
# 列出房间内所有会话事件：Agent可以通过此事件查询指定房间内的所有会话信息。
# List all sessions in room event: Agent can query all session info in a specific room via this event.
LIST_ROOM_EVENT = "server:list_room"
# NOTIFY 通知事件  通知事件全部由Server发出（一般由Client触发其它事件，在响应这些事件时，Server发出通知）
#   1. 比如 AgentClient 发出 server:tool_call_cancel 事件，服务端接收后，发起 notify:tool_call_cancel 通知
#   2. 比如 ComputerClient 发出 server:join_office 事件，服务端接收后，发起 notify:enter_office 通知
# AgentClient与ComputerClient选择性接收。因为Notify均由Server发出，因此Server中不需要实现对应接收方法
CANCEL_TOOL_CALL_NOTIFICATION = "notify:tool_call_cancel"
ENTER_OFFICE_NOTIFICATION = "notify:enter_office"  # AgentClient必须实现 以此，配合 client:get_config 与 client:get_tools 更新工具配置
LEAVE_OFFICE_NOTIFICATION = "notify:leave_office"  # AgentClient必须实现 以此，配合 client:get_config 与 client:get_tools 更新工具配置
UPDATE_CONFIG_NOTIFICATION = "notify:update_config"  # AgentClient必须实现 以此，配合 client:get_config
UPDATE_TOOL_LIST_NOTIFICATION = "notify:update_tool_list"  # AgentClient必须实现，通过 client:get_tools 实现更新工具配置
# 桌面刷新通知：Server接收 server:update_desktop 后广播。Agent据此拉取最新桌面。
UPDATE_DESKTOP_NOTIFICATION = "notify:update_desktop"


class AgentCallData(TypedDict):
    agent: str  # agent name
    req_id: str


class ToolCallReq(AgentCallData):
    computer: str
    tool_name: str
    params: dict
    timeout: int


class GetToolsReq(AgentCallData):
    computer: str  # 计算机名称


class SMCPTool(TypedDict):
    """在Computer端侧管理多个MCP时，无法保证ToolName不重复。因此alias字段被添加以帮助用户进行区分不同工具。如果alias被设置，创建工具时将会使用alias。"""

    name: str
    description: str
    params_schema: dict
    return_schema: dict | None
    meta: NotRequired[types.Attributes | None]


class GetToolsRet(TypedDict):
    tools: list[SMCPTool]
    req_id: str


class EnterOfficeReq(TypedDict):
    role: Literal["computer", "agent"]
    name: str
    office_id: str


class LeaveOfficeReq(TypedDict):
    office_id: str


class UpdateComputerConfigReq(TypedDict):
    computer: str  # 机器人计算机自定义名称


class GetComputerConfigReq(AgentCallData):
    computer: str  # 机器人计算机自定义名称


class ToolMeta(TypedDict, total=False):
    auto_apply: NotRequired[bool | None]
    # 不同MCP工具返回值并不统一，虽然其满足MCP标准的返回格式，但具体的原始内容命名仍然无法避免出现不一致的情况。通过object_mapper可以方便
    # 前端对其进行转换，以使用标准组件渲染解析。
    ret_object_mapper: NotRequired[dict | None]
    # 工具别名，与 model.ToolMeta.alias 对齐，用于解决不同 Server 下工具重名冲突
    # Tool alias, align with model.ToolMeta.alias, used to resolve name conflicts across servers
    alias: NotRequired[str | None]
    # 工具标签，用于对工具进行分类
    # Tool tags, used to categorize tools
    tags: NotRequired[list[str] | None]


class BaseMCPServerConfig(TypedDict):
    """MCP服务器配置基类"""

    name: SERVER_NAME  # MCP Server的名称
    disabled: bool
    forbidden_tools: list[str]  # 禁用的工具列表，因为一个mcp可能有非常多工具，有些工具用户需要禁用。
    tool_meta: dict[TOOL_NAME, ToolMeta]
    # 默认工具元数据（可选）。当某个具体工具未在 tool_meta 中提供专门配置时，使用该默认配置。
    # Default tool metadata (optional). Used when a specific tool has no explicit entry in tool_meta.
    default_tool_meta: NotRequired[ToolMeta | None]
    # VRL脚本（可选）。用于对工具返回值进行动态转换和格式化。如果配置了VRL脚本，在初始化时会进行语法检查。
    # VRL script (optional). Used to dynamically transform and format tool return values. Syntax check on initialization.
    vrl: NotRequired[str | None]


# --- MCPServer 配置，参考借鉴： ---
# VSCode: https://code.visualstudio.com/docs/copilot/chat/mcp-servers#_configuration-format
# AWS Q Developer: https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html


class MCPServerInputBase(TypedDict):
    """MCP服务器输入配置基类"""

    id: str
    description: str


class MCPServerPromptStringInput(MCPServerInputBase):
    """字符串输入类型，参考：https://code.visualstudio.com/docs/reference/variables-reference#_input-variables"""

    type: Literal["promptString"]
    default: NotRequired[str | None]
    password: NotRequired[bool | None]


class MCPServerPickStringInput(MCPServerInputBase):
    """选择输入类型，参考：https://code.visualstudio.com/docs/reference/variables-reference#_input-variables"""

    type: Literal["pickString"]
    options: list[str]
    default: NotRequired[str | None]


class MCPServerCommandInput(MCPServerInputBase):
    """命令输入类型，参考：https://code.visualstudio.com/docs/reference/variables-reference#_input-variables"""

    type: Literal["command"]
    command: str
    args: NotRequired[dict[str, str] | None]


MCPServerInput = MCPServerPromptStringInput | MCPServerPickStringInput | MCPServerCommandInput


class MCPServerStdioParameters(TypedDict):
    command: str
    """The executable to run to start the server."""
    args: list[str]
    """Command line arguments to pass to the executable."""
    env: dict[str, str] | None
    """
    The environment to use when spawning the process.

    If not specified, the result of get_default_environment() will be used.
    """
    cwd: str | None
    """The working directory to use when spawning the process."""
    encoding: str
    """
    The text encoding used when sending/receiving messages to the server

    defaults to utf-8
    """
    encoding_error_handler: Literal["strict", "ignore", "replace"]
    """
    The text encoding error handler.

    See https://docs.python.org/3/library/codecs.html#codec-base-classes for
    explanations of possible values
    """


class MCPServerStdioConfig(BaseMCPServerConfig):
    """标准输入输出模式的MCP服务器配置"""

    type: Literal["stdio"]
    server_parameters: MCPServerStdioParameters


# !!! 注意在 MCP 官方 python-client 中有一个奇怪的问题 StreamableServerParams中 timeout 与 sse_read_timeout 是使用 timedelta 定义的，这个
# 定义会在序列化时自动转义为字符串（基于 ISO 8601）但是SSEServerParams，它直接使用float定义，因此这里需要适配一下。导致了两个定义名称一致，
# 但数据结构不一致。


class MCPServerStreamableHttpParameters(TypedDict):
    # The endpoint URL.
    url: str
    # Optional headers to include in requests.
    headers: dict[str, Any] | None
    # HTTP timeout for regular operations.
    timeout: str
    # Timeout for SSE read operations.
    sse_read_timeout: str
    # Close the client session when the transport closes.
    terminate_on_close: bool


class MCPServerStreamableHttpConfig(BaseMCPServerConfig):
    """StreamableHttpHTTP模式的MCP服务器配置"""

    type: Literal["streamable"]
    server_parameters: MCPServerStreamableHttpParameters


class MCPSSEParameters(TypedDict):
    # The endpoint URL.
    url: str
    # Optional headers to include in requests.
    headers: dict[str, Any] | None
    # HTTP timeout for regular operations.
    timeout: float
    # Timeout for SSE read operations.
    sse_read_timeout: float


class MCPSSEConfig(BaseMCPServerConfig):
    """SSE模式的MCP服务器配置"""

    type: Literal["sse"]
    server_parameters: MCPSSEParameters


MCPServerConfig = MCPServerStdioConfig | MCPServerStreamableHttpConfig | MCPSSEConfig


class GetComputerConfigRet(TypedDict):
    """完整的Computer配置文件类型"""

    inputs: NotRequired[list[MCPServerInput] | None]
    servers: dict[str, MCPServerConfig]


class LeaveOfficeNotification(TypedDict, total=False):
    """Agent或者Computer离开房间的通知，需要向房间内其他人广播。广播时间为真实离开之前，也就是即将离开"""

    office_id: str
    computer: str | None
    agent: str | None


class EnterOfficeNotification(TypedDict, total=False):
    """Agent或者Computer加入房间的通知，需要向房间内其他人广播。广播时间为真实加入之后"""

    office_id: str
    computer: str | None  # Computer Name
    agent: str | None  # Agent Name


class UpdateMCPConfigNotification(TypedDict, total=False):
    """
    MCP配置更新的通知，需要向房间内其他人广播
    """

    computer: str  # 机器人计算机自定义名称


class UpdateToolListNotification(TypedDict, total=False):
    """
    工具列表更新的通知，需要向房间内其他人广播。
    Notification of tool list update, should be broadcast to others in the room.
    """

    computer: str  # 机器人计算机自定义名称


class GetDeskTopReq(AgentCallData, total=True):
    """
    获取当前Computer的桌面信息。
    """

    computer: str  # computer name
    desktop_size: NotRequired[int]  # 指定希望获取的桌面内容长度。如果不指定，则会全量返回，由调用方进行处理。
    window: NotRequired[str]  # 指定获取的WindowURI，如果不指定则由Desktop自动组织，如果指定，会尝试获取指定的Window


Desktop: TypeAlias = str


class GetDeskTopRet(TypedDict, total=False):
    """
    Computer的桌面布局与内容信息。Agent可以通过相应指令来获取。
    The layout and content on Computer. Agent get it by spec event.
    """

    desktops: list[Desktop]
    req_id: str


class ListRoomReq(AgentCallData):
    """
    列出房间内所有会话的请求。Agent通过此请求获取指定房间内的所有Computer和Agent会话信息。
    Request to list all sessions in a room. Agent uses this to get all Computer and Agent session info in a specific room.
    """

    office_id: str  # 房间ID / Room ID


class SessionInfo(TypedDict, total=False):
    """
    会话信息，包含sid、name、role等基础信息。
    Session information, includes sid, name, role and other basic info.
    """

    sid: str  # 会话ID / Session ID
    name: str  # 会话名称 / Session name
    role: Literal["computer", "agent"]  # 会话角色 / Session role
    office_id: str  # 所属房间ID / Office ID
    # v0.2 新增：协议版本号，由 Server 在 HTTP 握手阶段从 URL query 写入；仅用于展示与诊断，不参与二次校验
    # v0.2 added: protocol version, written by Server from URL query during HTTP handshake; for display/diagnostics only
    a2c_version: NotRequired[str]


class ListRoomRet(TypedDict):
    """
    列出房间内所有会话的响应。返回房间内所有Computer和Agent的会话信息列表。
    Response for listing all sessions in a room. Returns list of all Computer and Agent session info in the room.
    """

    sessions: list[SessionInfo]  # 会话列表 / Session list
    req_id: str  # 请求ID / Request ID


# =====================================================================
# v0.2 协议新增 / v0.2 protocol additions
# 协议来源 / Protocol source: A2C-SMCP/a2c-smcp-protocol v0.2.0
#   - docs/specification/data-structures.md
#   - docs/specification/error-handling.md (ErrorCode 4006-4015)
# =====================================================================


class ErrorCode(IntEnum):
    """
    A2C-SMCP 协议错误码（v0.2.0）。
    A2C-SMCP protocol error codes (v0.2.0).
    """

    # MCP 上游授权 / MCP upstream authorization
    TOOL_AUTHORIZATION_REQUIRED = 4006
    TOOL_AUTHORIZATION_FAILED = 4007
    # 协议版本握手 / Protocol version handshake
    PROTOCOL_VERSION_MISMATCH = 4008
    # MCP Server 路由 / MCP Server routing
    MCP_SERVER_NOT_FOUND = 4014
    MCP_CAPABILITY_NOT_SUPPORTED = 4015


class ResourceAnnotations(TypedDict, total=False):
    """
    MCP Resource 标准 annotations（snake_case mirror）。
    MCP standard Resource annotations (snake_case mirror).
    """

    audience: list[Literal["user", "assistant"]]
    priority: float  # [0, 1]
    last_modified: str  # ISO 8601


class A2CResource(TypedDict, total=False):
    """
    A2C 协议层 Resource，snake_case mirror MCP Resource。
    A2C protocol-level Resource, snake_case mirror of MCP Resource.

    元数据分工 / Metadata partition:
      - annotations: MCP 标准字段（priority/audience/last_modified）
      - _meta:       A2C 扩展（如 fullscreen 等业务字段）
    """

    uri: str
    name: str
    description: str
    mime_type: str
    size: int
    annotations: ResourceAnnotations
    _meta: dict[str, Any]


class GetResourcesReq(AgentCallData, total=True):
    """
    透明转发 MCP 标准 resources/list；Agent 据此发现 window / 业务自定义 scheme 的资源。
    Transparent forward of MCP resources/list; Agent discovers window/custom-scheme resources.
    """

    computer: str
    mcp_server: str  # 必填：MCP Server 名称 / Required: MCP Server name
    cursor: NotRequired[str]


class GetResourcesRet(TypedDict, total=False):
    """
    资源列表响应，含 MCP 标准 cursor 翻页。
    Resource list response with MCP-standard cursor pagination.
    """

    resources: list[A2CResource]
    next_cursor: NotRequired[str]
    req_id: str


class ErrorPayload(TypedDict, total=False):
    """
    A2C-SMCP 错误响应（flat shape）。
    A2C-SMCP error response (flat shape).

    分层语义 / Layer semantics:
      - HTTP 层（仅 4008）：HTTP 400 + flat body + X-A2C-Error-Code header
      - Socket.IO ack 层（4014 / 4015 等）：ack callback 第一参 = flat dict

    分流字段顶层平铺 / Code-specific dispatch fields are top-level:
      - 4008: server_version / client_version
      - 4014: mcp_server_name

    details 是诊断容器；Agent MUST NOT 透传给最终用户（防泄露）。
    details is a diagnostic container; Agent MUST NOT propagate to end users.
    """

    code: int  # ErrorCode value
    message: str
    # 4008 / Protocol version mismatch
    server_version: str
    client_version: str
    # 4014 / MCP Server not found
    mcp_server_name: str
    # 诊断容器 / Diagnostic container
    details: dict[str, Any]
