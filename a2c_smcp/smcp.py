# -*- coding: utf-8 -*-
# filename: smcp.py
# @Time    : 2025/8/8 12:35
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
from enum import IntEnum
from typing import Annotated, Any, Literal, NotRequired, TypeAlias

from pydantic import BeforeValidator
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
# v0.2.1 新增：SKILL 通道发现 / 渐进式披露 + 通用二进制传输
# 协议依据 / Protocol: events.md §client:get_skill[s] / §client:get_blob
# v0.2.1 added: SKILL channel discovery / progressive disclosure + generic blob transfer
GET_SKILLS_EVENT = "client:get_skills"
GET_SKILL_EVENT = "client:get_skill"
GET_BLOB_EVENT = "client:get_blob"
# 服务端事件 由server:开头的事件服务端执行
JOIN_OFFICE_EVENT = "server:join_office"
LEAVE_OFFICE_EVENT = "server:leave_office"
UPDATE_CONFIG_EVENT = "server:update_config"
UPDATE_TOOL_LIST_EVENT = "server:update_tool_list"
# 桌面刷新事件：当资源列表或资源内容变化时，由Computer端通知Server广播。
# 中文: 当需要让Agent刷新桌面时，由Computer触发此事件。英文: Computer emits this when Agent should refresh desktop.
UPDATE_DESKTOP_EVENT = "server:update_desktop"
# v0.2.1 新增：SKILL 集合变化（增/删/物化更新）由 Computer 触发，Server 向房间广播 notify:update_skills
# 协议依据 / Protocol: events.md §server:update_skills；data-structures.md §UpdateComputerConfigReq（复用，不新建结构）
# v0.2.1 added: SKILL set change (add/remove/restage) emitted by Computer, broadcast by Server as notify:update_skills
UPDATE_SKILLS_EVENT = "server:update_skills"
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
# v0.2.1 新增：SKILL 集合变化广播。Agent 据此自动重拉 client:get_skills（与既有 notify:update_* 自动刷新模式一致）
# 协议依据 / Protocol: events.md §notify:update_skills
# v0.2.1 added: SKILL set change broadcast. Agent re-fetches client:get_skills (same pattern as other notify:update_*)
UPDATE_SKILLS_NOTIFICATION = "notify:update_skills"


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
    """在Computer端侧管理多个MCP时，无法保证ToolName不重复。因此alias字段被添加以帮助用户进行区分不同工具。如果alias被设置，创建工具时将会使用alias。

    #152 D1：``bundle_id`` 显式承载工具归属。``name`` 为聚合后 exposed_tool_name（恒 ``{bundle_id}__``
    开头），Agent 据 ``bundle_id`` 把工具关联回具体 server（分组展示、关联 config/resources），
    **MUST NOT** 切分 ``name`` 的 ``__`` 前缀反推归属（``name`` 对 Agent 不透明）。wire 字段名 = snake_case
    ``bundle_id``（与 rust 逐字一致，协议 data-structures.md）。
    """

    name: str
    # 所属 MCP Server 的**解析后** bundle_id（resolve_bundle_id 产物，恒非空；非配置中的显式声明值）。
    # 与 GetComputerConfigRet.servers 的 key、错误码 meta.mcp_server 属**同一身份空间**。
    bundle_id: str
    description: str
    params_schema: dict
    return_schema: dict | None
    meta: NotRequired[types.Attributes | None]


class GetToolsRet(TypedDict):
    tools: list[SMCPTool]
    req_id: str


class ConnectAuth(TypedDict):
    """
    中文: Socket.IO 连接握手阶段的 ``auth`` 对象（业务层）。协议唯一规范要求是 ``role``；其余字段
        （如 ``token``）是否存在、如何校验由业务层（Server 实现方）自决。
        #112(AS-38)：连接面鉴权凭据统一走 ``token`` 字段（SDK / 部署约定，与 rust-sdk / tfrobot-client /
        TFRS Provider 四处对齐，Epic TFRM-153）；JWT 仅 TFRobotServer 后端验签，SDK 只负责携带。
    英文: The Socket.IO handshake ``auth`` object (business layer). The only protocol requirement is
        ``role``; other fields (e.g. ``token``) are business-defined. Since #112(AS-38) the
        connection-auth credential travels in the ``token`` field (SDK / deployment convention).
    协议依据 / Protocol: a2c-smcp-protocol data-structures.md#auth-对象业务层connectauth。

    !!! note "role 字段的接线现状（#112/AS-38 范围说明）"
        本类型忠实镜像协议文档（``role`` 标为 MUST）。但 SDK 默认 provider **不在连接 ``auth`` 注入 role**：
        ``DefaultAgentAuthProvider.get_connection_auth()`` 仅产出 ``{token: ...}``，Computer 经 CLI ``--auth``
        注入的也只有 ``token``；服务端 role 仍经 ``EnterOfficeReq`` / ``join_office`` 建立（与 rust-sdk 行为一致）。
        因此 ``ConnectAuth`` 在 SDK 内是**协议参考类型**，``get_connection_auth()`` 返回裸 ``dict``、不以此为标注。
        「role-at-connect 全面接线」是既有的协议文档-实现分歧（全生态未实现），非本次范围，留待独立 protocol-first 对齐。
        The default providers do NOT inject ``role`` at connect (consistent with rust-sdk); role is
        established via ``EnterOfficeReq`` / ``join_office``. Full role-at-connect wiring is a pre-existing
        protocol-vs-impl divergence deferred to a separate protocol-first effort.
    """

    role: Literal["computer", "agent"]  # 协议 MUST：客户端角色 / protocol MUST: client role
    token: NotRequired[str]  # #112(AS-38) 连接面鉴权凭据字段 / connection-auth credential field


class EnterOfficeReq(TypedDict):
    role: Literal["computer", "agent"]
    name: str
    office_id: str


class LeaveOfficeReq(TypedDict):
    office_id: str


class UpdateComputerConfigReq(TypedDict):
    """
    复用 / Reused by:
      - server:update_config / notify:update_config（v0.1+）
      - server:update_desktop / notify:update_desktop（v0.2）
      - server:update_skills / notify:update_skills（v0.2.1）
    协议依据 / Protocol: data-structures.md §UpdateComputerConfigReq（明示三事件族共用同一结构，不新建）。
    """

    computer: str  # 机器人计算机自定义名称


class GetComputerConfigReq(AgentCallData):
    computer: str  # 机器人计算机自定义名称


class ToolMeta(TypedDict, total=False):
    auto_apply: NotRequired[bool | None]
    # 不同MCP工具返回值并不统一，虽然其满足MCP标准的返回格式，但具体的原始内容命名仍然无法避免出现不一致的情况。通过object_mapper可以方便
    # 前端对其进行转换，以使用标准组件渲染解析。
    ret_object_mapper: NotRequired[dict | None]
    # 工具别名（BundleID 模型，协议 0.3.0）。仅替换 exposed_tool_name 的**工具名部分**，仍带 {bundle_id}__ 前缀。
    # Tool alias (BundleID model): replaces only the tool-name part of exposed_tool_name, keeps the {bundle_id}__ prefix.
    alias: NotRequired[str | None]
    # 工具标签，用于对工具进行分类
    # Tool tags, used to categorize tools
    tags: NotRequired[list[str] | None]


class BaseMCPServerConfig(TypedDict):
    """MCP服务器配置基类"""

    name: SERVER_NAME  # MCP Server的名称
    # MCP Server 唯一身份（BundleID 模型，协议 0.3.0）。省略/未解析为 None；注册边界 derive 后恒有值。
    # MCP Server unique identity (BundleID model). None when omitted/unresolved; always set after derive.
    bundle_id: NotRequired[str | None]
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


def _reject_legacy_pick_options(v: Any) -> Any:
    """⑦ 旧 ``options: list[str]`` 直接拒绝并指路新结构（协议 v0.3.2，无 alias、无迁移期）。

    REPL ``inputs load`` 先经 smcp.py TypedDict 适配器校验（早于 model 层）——若无此 BeforeValidator，
    旧形式只会得到泛化的 pydantic 类型错误而非「指路新结构」报错。"""
    if isinstance(v, list) and v and all(isinstance(item, str) for item in v):
        raise ValueError(
            "旧 options: list[str] 形式已废弃（协议 v0.3.2）：请改用 "
            "[{'label': ..., 'value': ...}] 结构化形式（label=展示、value=注入值）"
        )
    return v


class PickStringOption(TypedDict):
    """PickString 选项（wire 镜像，协议 v0.3.2）：``label``=展示、``value``=注入值；两者允许重复。"""

    label: str
    value: str


class MCPServerPickStringInput(MCPServerInputBase):
    """选择输入类型，参考：https://code.visualstudio.com/docs/reference/variables-reference#_input-variables"""

    type: Literal["pickString"]
    options: Annotated[list[PickStringOption], BeforeValidator(_reject_legacy_pick_options)]
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
    """完整的Computer配置文件类型。

    #152 D1 不变量：``servers`` 的 **key = 解析后 bundle_id**，且每个 entry 的 ``bundle_id`` 字段 = 同值
    （运行期注入，见 #149 on_get_config）——与 SMCPTool.bundle_id 属**同一身份空间**，Agent 据此把工具
    归属回 server。注意 ``MCPServerConfig.bundle_id`` 类型为 ``str | None`` 承载的是**显式声明值**，
    wire 投影 MUST 物化为解析后值（绝不直接序列化模型字段，缺省会得 null）。
    """

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
    A2C-SMCP 协议错误码（v0.2.0 起，v0.2.1 加性追加 4016/4017/4018）。
    A2C-SMCP protocol error codes (v0.2.0+, v0.2.1 additively adds 4016/4017/4018).

    4014 复用语义 / 4014 reuse (v0.2.1, error-handling.md §SKILL):
      SKILL ``name`` **格式合法但不存在**（未注册 / 已卸载 / 已孤儿）复用 ``MCP_SERVER_NOT_FOUND`` 语义；
      ``name`` 格式非法 → 4016；``name`` 有效但 ``rel_path`` 不可达 → 4017。
      SKILL ``name`` legal-but-absent (unregistered / uninstalled / orphan) reuses ``MCP_SERVER_NOT_FOUND``;
      malformed name → 4016; name OK but rel_path unreachable → 4017.

    SKILL 通道**不使用** 4015 —— 未声明 ``resources`` capability 的 server 在物化阶段即被排除，不上送 Agent.
    SKILL channel does **not** use 4015 — servers lacking the ``resources`` capability are filtered out at
    staging time and never surface to the Agent.

    4017 / 4018 ``details.reason`` 为开放枚举 / open enum: 解析方 **MUST** 容忍未知值并兜底
    （默认「不重试 + 诊断」），未来可非破坏新增 reason.
    ``details.reason`` is an open enum: parsers **MUST** tolerate unknown values with a default of
    "do not retry + diagnose"; new reasons may be added non-breakingly.
    """

    # 通用「资源不存在」/ Generic "resource not found"
    # 本 SDK 用于 client:* 路由层目标 Computer 名未命中（error-handling.md §20 明确「Computer 不存在」归 404）。
    # 镜像协议已有定义，非协议新增。Used by the client:* router when the target Computer name is
    # absent (error-handling.md §20 maps "Computer 不存在" to 404). Mirrors an existing protocol code.
    NOT_FOUND = 404
    # MCP 上游授权 / MCP upstream authorization
    TOOL_AUTHORIZATION_REQUIRED = 4006
    TOOL_AUTHORIZATION_FAILED = 4007
    # 协议版本握手 / Protocol version handshake
    PROTOCOL_VERSION_MISMATCH = 4008
    # MCP Server 路由 / MCP Server routing
    MCP_SERVER_NOT_FOUND = 4014  # v0.2.1 复用：SKILL name 合法但 Registry 未命中（含孤儿/卸载）
    MCP_CAPABILITY_NOT_SUPPORTED = 4015
    # v0.2.1 新增：SKILL 通道 / SKILL channel
    # 协议依据 / Protocol: error-handling.md §4016 / §4017
    SKILL_NAME_INVALID = 4016  # client:get_skill 入参 name 违反 SKILL name lexer 规则（格式硬错）
    SKILL_RESOURCE_NOT_ACCESSIBLE = 4017  # rel_path 穿越 / .skillenv forbidden / not_found / too_large
    # v0.2.1 新增：通用二进制传输 / Generic binary transfer
    # 协议依据 / Protocol: error-handling.md §4018
    BLOB_NOT_ACCESSIBLE = 4018  # client:get_blob 拉取期：invalid_handle / forbidden / gone / range


# WebSocket close code（RFC 6455 私有段 4000-4999），用于 WS-only 直连握手按版本不匹配拒绝
# （服务端运行栈不支持 ASGI WebSocket Denial Response 时的回退形态）。
# WebSocket close code (RFC 6455 private range 4000-4999) for rejecting a WS-only direct
# handshake on version mismatch, when the server stack lacks ASGI WebSocket Denial Response.
#
# **MUST NOT** 与 ``ErrorCode.PROTOCOL_VERSION_MISMATCH``（4008，ErrorPayload.code，承载于
# HTTP 400 body）混用或互转——二者是**不同命名空间的不同值**，有意取不同数值，遵守
# “4008 是 HTTP body code，MUST NOT 与 WS close code 混淆” 的协议铁律。``4900`` 不携带
# 结构化 body（WS close reason ≤123 字节）。协议依据 / Protocol: versioning.md §5 / §4900。
# MUST NOT be conflated/converted with ``ErrorCode.PROTOCOL_VERSION_MISMATCH`` (4008,
# an ErrorPayload.code carried in the HTTP 400 body) — different namespaces, different values.
WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE: int = 4900


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
    mcp_server: str  # 必填：目标 MCP Server 的 bundle_id（= get_config servers 字典 key，协议 #18）/ Required: target server bundle_id
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
      - 4008: server_version / client_version / min_supported / max_supported
      - 4014: mcp_server（bundle_id）
      - 4015: mcp_server（bundle_id）/ capability

    v0.2.1 起，4016 / 4017 / 4018 的 code-specific 字段下沉到 ``details`` 子对象（无顶层平铺新字段）：
    From v0.2.1, code-specific fields for 4016 / 4017 / 4018 live under ``details`` (no new top-level fields):
      - 4016: details.name
      - 4017: details.reason (traversal/forbidden/not_found/too_large) / details.rel_path / details.total_size
      - 4018: details.reason (invalid_handle/forbidden/gone/range)
    协议依据 / Protocol: error-handling.md §各错误码标准字段总表 / §4016 / §4017 / §4018.

    details 是诊断容器；Agent MUST NOT 透传给最终用户（防泄露）。
    details is a diagnostic container; Agent MUST NOT propagate to end users.
    """

    code: int  # ErrorCode value
    message: str
    # 4008 / Protocol version mismatch（四字段对齐 error-handling.md §各错误码标准字段总表）
    # 4008 fields aligned with error-handling.md §standard fields table
    server_version: str
    client_version: str
    min_supported: str
    max_supported: str
    # 4014 / MCP Server not found；4015 / MCP Capability not supported。值 = 目标 server 的 **bundle_id**（协议 #18）
    mcp_server: str
    # 4015 / 缺失的 capability 名（如 "resources"）/ Missing capability name (e.g. "resources")
    capability: str
    # 诊断容器 / Diagnostic container
    details: dict[str, Any]


# =====================================================================
# v0.2.1 协议新增 / v0.2.1 protocol additions
# 协议来源 / Protocol source: A2C-SMCP/a2c-smcp-protocol v0.2.1
#   - docs/migrations/v0.2.1-skill-and-blob-transfer.md（先读总纲）
#   - docs/specification/data-structures.md §SKILL / §BlobHandle / §GetBlob*
#   - docs/specification/error-handling.md §4016 / §4017 / §4018（4014 复用）
#   - docs/specification/events.md §client:get_skill[s] / §client:get_blob / §server:update_skills
#   - docs/specification/skill.md / docs/specification/blob-transfer.md
# 加性升级 / Additive: PROTOCOL_VERSION 未改动；所有新结构均为 total=False 开放 TypedDict，
# 允许未来非破坏追加字段。
# =====================================================================


class A2CSkillRef(TypedDict):
    """
    SKILL 引用对象，``client:get_skills`` 返回列表元素。
    Skill reference object — element of ``client:get_skills`` return list.

    协议依据 / Protocol: data-structures.md §A2CSkillRef（v0.2.2 正式定稿必选集）；skill.md §1 命名 / §6 数据结构。

    关键约束 / Key invariants:
      - **必选 4 字段**：``name`` / ``source`` / ``path`` / ``description``（``total=True`` 默认，PEP 655：
        裸字段=必选、``NotRequired[]``=可选）。``description`` 跨三源恒存在（SKILL.md frontmatter 强制
        name+description，marketplace §3.1）。Producer(Computer) **MUST** 发齐 4 必选；Consumer(Agent)
        **MUST NOT** 假定任一可选字段存在。
        Required 4: name/source/path/description (total=True + ``NotRequired[]`` optionals). Producer MUST
        send all 4; Consumer MUST NOT assume any optional field is present.
      - ``name`` 是协议主键（合成全局唯一名，自 0.2.1 起为**裸名**：user 1 段 ``<skill>`` /
        marketplace 2 段 ``<plugin>:<skill>`` / mcp 3 段 ``mcp:<bundle_id>:<skill>``），Agent **MUST**
        当作不透明可比较字符串（**勿**解析结构，判来源用 ``source``）
        ``name`` is the protocol primary key (synthesized globally-unique **bare** name since 0.2.1:
        user 1-seg ``<skill>`` / marketplace 2-seg ``<plugin>:<skill>`` / mcp 3-seg
        ``mcp:<bundle_id>:<skill>``); Agent **MUST** treat it as an opaque comparable string.
      - mcp 形态的 server 段取 **``bundle_id``**（server 唯一身份，= ``get_config.servers`` 字典 key），
        **非** display ``name``——后者允许碰撞。故 mcp 形态 name 构造上不碰撞，且 Agent 可据 ``source``
        把一条 SKILL 关联回对应 A2C server（skill.md §1.3，协议 #142 supersede #18 的正交结论）
        The mcp server segment is the ``bundle_id`` (unique identity), never the collision-allowed display name.
      - ``path`` 必选（staging 落盘是所有 source 的统一第一步，故进入 Registry 的 SKILL 必有可读本地目录）
        ``path`` is required (staging is the unified first step for all sources; any SKILL in Registry
        has a readable local directory).
      - **无 raw display server 名字段** —— Agent 侧协议表面与 source 无关；server 身份由 ``source`` 里的
        ``bundle_id`` 承载，来源追溯由 ``source``（完整 provenance，**不**进 ``name``）与（仅 MCP）
        ``uri`` 承担（skill.md §1.6）
        **No raw display server-name field** — provenance is carried by ``source`` (which embeds the
        ``bundle_id``) and (MCP only) ``uri``.
    """

    # 主键 / Primary key（必选 / required）
    # 合成全局唯一裸名；如 user `my-helper` / marketplace `acme-audit:audit` / mcp `mcp:<bundle_id>:<skill>`
    # （mcp 例：`mcp:tfrobot-tools:code-review`；hash-fallback 例：`mcp:bundle_a1b2c3d4e5f60718:summarize`）
    name: str
    # 来源元数据 / Provenance（完整溯源；**不**进 name）（必选 / required）
    source: str  # 如 mcp:<bundle_id>（例 mcp:tfrobot-tools）/ marketplace:acme-skills / user
    uri: NotRequired[str]  # 仅 MCP 来源：skill://host/skill-name；Agent 非权威（skill.md §2）
    # 物化输出 / Materialization output（必选 / required）
    path: str  # 必选：Computer 本地绝对目录路径，面向 Agent SDK（脚本执行/文件访问），LLM 永不可见
    # SKILL.md frontmatter 派生 / Derived from SKILL.md frontmatter
    description: str  # 必选 / required：marketplace SKILL v1 §3.1（跨三源强制）
    license: NotRequired[str]
    compatibility: NotRequired[str]
    allowed_tools: NotRequired[list[str]]  # frontmatter "allowed-tools" 规范化为 list
    tags: NotRequired[list[str]]  # frontmatter "tags" 透传（分类元数据）；纯透传不校验：非 list[str] → 省略（skill.md §1.5）
    version: NotRequired[str]
    skill_metadata: NotRequired[dict[str, Any]]  # frontmatter.metadata 透传；A2C 不解释 / passthrough, A2C does not interpret


class GetSkillsReq(AgentCallData, total=True):
    """
    获取 SKILL 清单请求。协议依据 / Protocol: events.md §client:get_skills；data-structures.md §GetSkillsReq.
    Get SKILL inventory request.
    """

    computer: str


class GetSkillsRet(TypedDict, total=False):
    """
    获取 SKILL 清单响应——轻量元数据，**不含** SKILL.md body（body 由 ``client:get_skill`` 拉取）。
    Skill inventory response — lightweight metadata, **without** SKILL.md body (body via ``client:get_skill``).

    协议依据 / Protocol: data-structures.md §GetSkillsRet；skill.md §6 排除孤儿 / 不排序 / 不去重.
    """

    skills: list[A2CSkillRef]  # 当前已安装且可用 SKILL（排除孤儿；不排序、不去重）
    req_id: str


class GetSkillReq(AgentCallData, total=True):
    """
    获取 SKILL 包内单个资源请求。SKILL 本质是文件夹：``rel_path`` 缺省取包根 ``SKILL.md``（入口），
    携带 ``rel_path`` 取包内其它资源（渐进式披露）。
    Get a single resource inside a SKILL package. SKILL is essentially a folder: ``rel_path`` defaults
    to package-root ``SKILL.md`` (entry); pass ``rel_path`` to fetch other in-package resources
    (progressive disclosure).

    协议依据 / Protocol: events.md §client:get_skill；data-structures.md §GetSkillReq；
    skill.md §9 安全模型（``rel_path`` MUST 相对、无 ``..``、无绝对路径，沙箱在 Computer 解析时强制）.
    """

    computer: str
    name: str  # 必选：来自某 A2CSkillRef.name
    rel_path: NotRequired[str]  # 可选：SKILL 包根 POSIX 相对路径；缺省 = "SKILL.md"


class GetSkillRet(TypedDict, total=False):
    """
    获取 SKILL 包内单个资源响应。文本且可内联 → ``body`` 直接给出；二进制或过大文本 → ``blob_handle``
    转 ``client:get_blob`` 拉取——``body`` 与 ``blob_handle`` **恰一存在**（exactly one）。
    Get-skill response. Inlineable text → ``body``; binary or oversize text → ``blob_handle`` (fetched
    via ``client:get_blob``). Exactly one of ``body`` / ``blob_handle`` is present.

    协议依据 / Protocol: data-structures.md §GetSkillRet；blob-transfer.md §5 生产者通道接入契约；
    skill.md §9 安全 / too_large 不铸句柄.

    完整性 / Integrity:
      - ``total_size`` / ``sha256`` 基于 **Agent 最终消费的资源字节**（SKILL.md → frontmatter 剥离后 body；
        其它 → 原始字节；占位符 ``$TFROBOT_*`` **不展开**，由 Agent SDK 渲染层处理）
        ``total_size`` / ``sha256`` are based on the **bytes the Agent will ultimately consume**
        (SKILL.md → body with frontmatter stripped; others → raw bytes; ``$TFROBOT_*`` placeholders
        **NOT expanded**, handled by Agent SDK prompt rendering).
      - Agent **SHOULD** 用 ``sha256`` 校验内联 ``body``；handle 路径于 ``eof`` 后全量校验
        Agent **SHOULD** verify ``body`` against ``sha256`` inline; for handle path verify after ``eof``.
    """

    name: str  # 回显请求 name
    rel_path: str  # 回显请求 rel_path；缺省请求时回显 "SKILL.md"
    mime_type: str  # 资源 MIME，如 text/markdown / image/png
    total_size: int  # 资源总字节数（基于最终消费字节）
    sha256: str  # 全量资源 sha256 十六进制（完整性 + 变更检测）
    body: str  # 文本且 ≤ 内联预算：直接内容（与 blob_handle 恰一）
    blob_handle: str  # 否则：转 client:get_blob 的不透明句柄（与 body 恰一）
    req_id: str


# ── 通用二进制传输 / Generic binary transfer ──────────────────────────────
# 协议依据 / Protocol: blob-transfer.md（句柄契约 / 生产者-消费者模型 / 安全模型）


BlobHandle: TypeAlias = str
"""
不透明、Computer 铸造、无状态可重解析的 blob 句柄。
Opaque, Computer-minted, stateless-reparseable blob handle.

Agent 行为约束 / Agent behavior constraints (blob-transfer.md §1):
  - MUST 视为不透明字符串；MUST NOT 解析 / 拼接 / 伪造 / 跨 Computer 复用
  - MUST treat as opaque string; MUST NOT parse / concatenate / forge / reuse across Computers

载体随响应结构而异 / Carrier varies by response shape:
  - ``GetSkillRet.blob_handle``（A2C 可变结构 / A2C mutable）
  - MCP ``CallToolResult`` content item ``_meta.a2c_blob_handle``（MCP 不可变结构旁路 / MCP immutable sideband）
"""


class GetBlobReq(AgentCallData, total=True):
    """
    通用二进制拉取请求。Computer 无服务端状态、幂等可并行（``chunk_offset`` 为资源字节绝对偏移）。
    Generic binary pull request. Stateless on Computer, idempotent, parallelizable
    (``chunk_offset`` is absolute byte offset within the resource).

    协议依据 / Protocol: events.md §client:get_blob；data-structures.md §GetBlobReq；
    blob-transfer.md §2 (handle) / §3 (chunking) / §4 (errors).
    """

    computer: str
    blob_handle: str  # 必选：来自某通道响应的不透明句柄
    chunk_offset: NotRequired[int]  # 可选：资源字节绝对偏移；缺省 0；无状态幂等 → 可续传 / 重试 / 并行
    max_chunk_bytes: NotRequired[int]  # 可选：客户建议单块上限；Computer clamp（恒保证不超 Server buffer）


class GetBlobRet(TypedDict, total=False):
    """
    通用二进制拉取响应。
    Generic binary pull response.

    协议依据 / Protocol: data-structures.md §GetBlobRet；blob-transfer.md §3 完整性 / §4 错误 / §5 演进缝隙.

    完整性与一致性铁律 / Integrity & consistency invariants:
      - ``eof`` ⟺ ``chunk_offset + len(decode(blob)) == total_size``
      - ``eof`` 后 Agent **SHOULD** 校验重组 ``sha256`` == 响应 ``sha256``，不符即损坏、重读
        After ``eof`` Agent **SHOULD** verify reassembled ``sha256`` == response ``sha256``;
        on mismatch the data is corrupt — reread.
      - 一次逻辑读取内 ``sha256`` / ``total_size`` **MUST** 稳定；跨块变化 ⇒ 源被改写，
        Agent **MUST** 从 offset 0 重读
        Within one logical read ``sha256`` / ``total_size`` **MUST** be stable; on change ⇒ source rewritten,
        Agent **MUST** restart from offset 0.

    演进缝隙 / Evolution slack (open TypedDict): 未来可非破坏追加 ``content_encoding`` (gzip 等，缺省 identity)
    / ``etag``；offset / total_size / sha256 基于**解码后**资源字节，加压缩不致歧义。
    Future fields may be added non-breakingly; offset / total_size / sha256 are on **decoded** bytes.
    """

    blob_handle: str  # 回显
    mime_type: str
    total_size: int  # 首块即知；一次读取内恒定
    sha256: str  # 全量资源 sha256；跨块恒定
    chunk_offset: int  # 本块起始字节偏移
    eof: bool  # ⟺ chunk_offset + 本块字节数 == total_size
    blob: str  # base64，本块字节
    req_id: str


# 协议错误码取值集合（flat ErrorPayload 顶层 code）/ Protocol error-code value set (flat ErrorPayload top-level code)
_ERROR_CODE_VALUES: frozenset[int] = frozenset(int(c) for c in ErrorCode)


def is_protocol_error_payload(data: object) -> bool:
    """
    判定 ``data`` 是否为协议级 **flat ErrorPayload**：顶层 ``code`` 属协议错误码，无嵌套 envelope。
    Whether ``data`` is a protocol-level **flat ErrorPayload**: top-level ``code`` is a
    protocol error code, no nested envelope.

    server（透传判定）与 agent（抛 SMCPProtocolError）共用同一谓词，避免双重启发式漂移。
    Shared by server (passthrough decision) and agent (raise SMCPProtocolError) to avoid
    divergent heuristics. 协议依据 / Protocol: error-handling.md（禁止二次 unwrap）。
    """
    return isinstance(data, dict) and data.get("code") in _ERROR_CODE_VALUES


def build_computer_not_found_error(computer_name: str) -> ErrorPayload:
    """``client:*`` 路由层目标 Computer 名未注册 → flat ErrorPayload(404)。
    Build the flat ErrorPayload(404) returned when a ``client:*`` route targets an
    unregistered Computer name.

    sync/async 命名空间共用本 builder，保证两实现返回**逐字节一致**的负载（双实现镜像约束）。
    Shared by the sync/async namespaces so both implementations return byte-identical payloads.

    协议依据 / Protocol: error-handling.md §20（404 工具或 Computer 不存在）+ §78（所有
    ``client:*`` ack 通道协议级错误 MUST 为 flat ErrorPayload）。
    """
    return {
        "code": int(ErrorCode.NOT_FOUND),
        "message": f"Computer with name '{computer_name}' not found",
        "details": {"computer_name": computer_name},
    }
