# filename: model.py
# @Time    : 2025/8/17 16:53
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, Self, TypeAlias, runtime_checkable

from mcp import StdioServerParameters, Tool
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters
from mcp.types import CallToolResult, ReadResourceResult, Resource
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from vrl_python import VRLRuntime

from a2c_smcp.computer.mcp_clients.oauth_types import OAuthOptions
from a2c_smcp.types import SERVER_NAME, TOOL_NAME
from a2c_smcp.utils.bundle_id import validate_explicit_bundle_id

if TYPE_CHECKING:
    from a2c_smcp.computer.mcp_clients.base_client import STATES

A2C_TOOL_META: str = "a2c_tool_meta"
# VRL转换后的结果存储Key。用于在CallToolResult.meta中存储VRL处理后的数据。
# Key for storing VRL-transformed result in CallToolResult.meta.
A2C_VRL_TRANSFORMED: str = "a2c_vrl_transformed"


class ToolMeta(BaseModel):
    auto_apply: bool | None = Field(default=None, title="是否自动使用", description="如果设置为False，则调用工具前会触发回调，请求用户批准")
    alias: str | None = Field(
        default=None,
        title="工具别名",
        description=(
            "工具别名（BundleID 模型，协议 0.3.0）。仅替换 exposed_tool_name 的**工具名部分**，"
            "仍带 `{bundle_id}__` 前缀（非对整个 exposed_tool_name 的完全替换）。含连字符/冲突的原始名"
            "可借此适配下游命名约束。"
        ),
    )
    tags: list[str] | None = Field(default=None, title="工具标签", description="用于对工具进行分类")
    # 不同MCP工具返回值并不统一，虽然其满足MCP标准的返回格式，但具体的原始内容命名仍然无法避免出现不一致的情况。通过object_mapper可以方便
    # 前端对其进行转换，以使用标准组件渲染解析。
    ret_object_mapper: dict | None = Field(
        default=None,
        title="字段转换映射",
        description="允许定义一个映射表完成MCPTool工具返回结构映射到自定义结构",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow", arbitrary_types_allowed=False)


class BaseMCPServerConfig(BaseModel):
    """MCP服务器配置基类"""

    name: SERVER_NAME  # MCP Server的名称
    # BundleID 模型（协议 0.3.0，a2c-smcp-protocol#15）：MCP Server 软件级唯一身份。
    # 省略时由 name 经确定性算法在 Computer 注册边界（derive-on-load）生成，解析后恒有值；
    # 此处仅承载**显式**值并校验（生成不在 Pydantic，因 config frozen=True）。见 utils/bundle_id.py。
    bundle_id: str | None = Field(
        default=None,
        title="Bundle 唯一标识",
        description="MCP Server 唯一身份（BundleID）。省略则由 name 确定性生成；显式值须为 [A-Za-z0-9_-] 且无连续 '__'。",
    )
    disabled: bool = Field(default=False, title="是否禁用", description="是否禁用MCP Server")
    forbidden_tools: list[TOOL_NAME] = Field(
        default_factory=list,
        title="禁用工具列表",
        description="禁用的工具列表，因为一个mcp可能有非常多工具，有些工具用户需要禁用。",
    )
    tool_meta: dict[TOOL_NAME, ToolMeta] = Field(default_factory=dict, title="工具元数据", description="工具元数据，用于描述工具的基本信息")
    # 默认工具元数据（可选）。当某个具体工具未在 tool_meta 中提供专门配置时，使用该默认配置。
    # Default tool metadata (optional). Used when a specific tool has no explicit entry in tool_meta.
    default_tool_meta: ToolMeta | None = Field(default=None)
    # VRL脚本（可选）。用于对工具返回值进行动态转换和格式化。如果配置了VRL脚本，在初始化时会进行语法检查。
    # VRL script (optional). Used to dynamically transform and format tool return values. Syntax check on initialization.
    vrl: str | None = Field(
        default=None,
        title="VRL脚本",
        description="用于对工具返回值进行动态转换和格式化的VRL脚本。配置后会在初始化时进行语法检查。",
    )
    # VS Code 对标的 envFile（v0.2.1 #65，§9.1）：spawn 时从 .env 加载 KEY=VALUE 进 stdio server 的 env，
    # 显式 env 同名项覆盖 envFile（显式胜）。SDK 加性字段（设计 §1092「待协议追认」），仅 Computer 本地
    # spawn 消费、不在 client:get_config 展开变量；非 stdio（sse/http 无 env）忽略此字段。
    # VS Code-parity envFile: at spawn, load KEY=VALUE from .env into a stdio server's env (explicit env wins).
    env_file: str | None = Field(default=None, alias="envFile", title="环境变量文件", description="VS Code 风格 envFile，spawn 时加载")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=False,
        frozen=True,
        populate_by_name=True,
    )
    """配置字段在初始化完成后不允许修改；``populate_by_name`` 令 ``envFile``(alias) 与 ``env_file``(名) 均可填充"""

    @field_validator("vrl")
    @classmethod
    def validate_vrl_syntax(cls, v: str | None) -> str | None:
        """
        验证VRL脚本语法。如果配置了VRL脚本但语法错误，则抛出异常。
        Validate VRL script syntax. Raises exception if VRL is configured but has syntax errors.

        Args:
            v (str | None): VRL脚本内容 / VRL script content

        Returns:
            str | None: 验证通过的VRL脚本 / Validated VRL script

        Raises:
            ValueError: VRL语法错误 / VRL syntax error
        """
        if v is None or v.strip() == "":
            return v

        # 使用VRL Runtime进行语法检查
        # Use VRL Runtime for syntax check
        diagnostic = VRLRuntime.check_syntax(v)
        if diagnostic is not None:
            # 语法错误，抛出异常
            # Syntax error, raise exception
            error_msg = f"VRL语法错误 / VRL syntax error:\n{diagnostic.formatted_message}"
            raise ValueError(error_msg)

        return v

    @field_validator("bundle_id")
    @classmethod
    def validate_bundle_id(cls, v: str | None) -> str | None:
        """仅校验**显式**提供的 bundle_id（非空、无连续 `__`、字符集 `[A-Za-z0-9_-]`）。

        Validate an explicitly-provided bundle_id only. 省略（None）不校验——触发注册边界缺省生成
        （:func:`a2c_smcp.utils.bundle_id.resolve_bundle_id`）。
        """
        if v is None:
            return v
        return validate_explicit_bundle_id(v)

    def __hash__(self) -> int:
        """以 ``name`` 作**哈希桶**键——**不**是身份判定 / Hash bucket only, NOT an identity claim.

        身份是 ``bundle_id``（BundleID 模型，协议 #18）：``name`` 已降级为纯 display、**允许碰撞**，
        故同名 ≠ 同一 Server。这里仍按 ``name`` 取哈希是**合法且安全**的——相等性由 Pydantic 的
        全字段 ``__eq__`` 判定，同名不同 config 只是落进同一哈希桶（碰撞），在 ``set`` 中**仍各自共存**，
        不会被误去重。真正的 Server 去重（no-double-open）由 Manager 按 ``bundle_id`` 负责，不在此处。
        Equal objects always share a name ⇒ the hash contract holds; same-name-different-config
        entries merely collide in a bucket and still coexist in a ``set``.
        """
        return hash(self.name)


class StdioServerConfig(BaseMCPServerConfig):
    type: Literal["stdio"] = "stdio"
    server_parameters: StdioServerParameters = Field(title="MCP Server启动参数", description="引用自MCP Python SDK官方配置")


class SseServerConfig(BaseMCPServerConfig):
    type: Literal["sse"] = "sse"
    server_parameters: SseServerParameters = Field(title="MCP SSE Server连接参数", description="引用自MCP Python SDK 官方配置")


class StreamableHttpServerConfig(BaseMCPServerConfig):
    type: Literal["streamable"] = "streamable"
    server_parameters: StreamableHttpParameters = Field(title="MCP HTTP Server连接参数", description="引用自MCP Python SDK 官方配置")
    oauth: OAuthOptions | None = Field(
        default=None,
        title="OAuth 配置",
        description="对齐 Rust HttpServerConfig.oauth（SDK 层，非 SMCP 协议）",
    )


MCPServerConfig: TypeAlias = StdioServerConfig | SseServerConfig | StreamableHttpServerConfig


class MCPServerInputBase(BaseModel):
    """MCP服务器输入项配置基类"""

    id: str
    """Input的唯一标准，即使跨类型，也不可重复"""
    description: str

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", arbitrary_types_allowed=False, frozen=True)
    """配置字段在初始化完成后不允许修改"""

    def __hash__(self) -> int:
        """以 id 作为唯一哈希，确保在 set 中按 id 去重"""
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        """按 id 判断相等性，确保不同内容但相同 id 的输入在集合中视为同一元素"""
        if not isinstance(other, MCPServerInputBase):
            return False
        return self.id == other.id


class MCPServerPromptStringInput(MCPServerInputBase):
    """字符串输入类型，参考：https://code.visualstudio.com/docs/reference/variables-reference#_input-variables"""

    type: Literal["promptString"] = Field(default="promptString")
    default: str | None = Field(default=None)
    password: bool | None = Field(default=None)


class PickStringOption(BaseModel):
    """PickString 选项（协议 v0.3.2，a2c-smcp-protocol#48）：``label``=展示、``value``=注入值。

    约束：label / value 非空（min_length=1）；label 与 value 均**允许重复**（不要求唯一）——
    SDK / client MUST NOT 按 value 反推原选 label（重复 label 下按序号返回条目本身）。
    """

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", arbitrary_types_allowed=False, frozen=True)


class MCPServerPickStringInput(MCPServerInputBase):
    """选择输入类型，参考：https://code.visualstudio.com/docs/reference/variables-reference#_input-variables"""

    type: Literal["pickString"] = Field(default="pickString")
    options: list[PickStringOption]
    default: str | None = Field(default=None)

    @field_validator("options", mode="before")
    @classmethod
    def _reject_legacy_string_options(cls, v: Any) -> Any:
        """⑦ 旧 ``options: list[str]`` 直接拒绝（协议 v0.3.2 破坏性变更：无 alias、无迁移期），报错指路新结构。"""
        if isinstance(v, list) and v and all(isinstance(item, str) for item in v):
            raise ValueError(
                "旧 options: list[str] 形式已废弃（协议 v0.3.2）：请改用 "
                "[{'label': ..., 'value': ...}] 结构化形式（label=展示、value=注入值）"
            )
        return v

    @model_validator(mode="after")
    def _validate_options_and_default(self) -> Self:
        """⑤ options 至少一项；④ default 若存在且非 None MUST 匹配至少一个 option.value（显式 null 视为无默认）。"""
        if not self.options:
            raise ValueError("options 至少一项 / options must contain at least one entry")
        if self.default is not None and not any(o.value == self.default for o in self.options):
            raise ValueError(
                f"default {self.default!r} 不匹配任一 option.value / default must match at least one option.value"
            )
        return self


class MCPServerCommandInput(MCPServerInputBase):
    """命令输入类型，参考：https://code.visualstudio.com/docs/reference/variables-reference#_input-variables"""

    type: Literal["command"] = Field(default="command")
    command: str = Field(title="")
    args: dict[str, str] | None = Field(default=None)


MCPServerInput = MCPServerPromptStringInput | MCPServerPickStringInput | MCPServerCommandInput


@runtime_checkable
class MCPClientProtocol(Protocol):
    state: "STATES"

    async def aconnect(self) -> None:
        """连接MCP Server"""
        ...

    async def adisconnect(self) -> None:
        """断开连接"""
        ...

    async def list_tools(self) -> list[Tool]:
        """获取可用工具列表"""
        return []

    async def call_tool(self, tool_name: str, params: dict) -> CallToolResult:
        """运行指定工具"""
        pass

    async def list_windows(self) -> list[Resource]:
        """列出当前MCP服务可用的窗口资源列表 / List window resources of the MCP server"""
        ...

    async def list_resources_page(self, cursor: str | None = None) -> tuple[list[Resource], str | None]:
        """单页透传 MCP `resources/list` / Single-page transparent forward of MCP `resources/list`"""
        ...

    async def get_window_detail(self, resource: Resource | str) -> ReadResourceResult:
        """获取当前Window的详细内容"""
        ...


class GetSkillRet(BaseModel):
    """
    ``client:get_skill`` 响应的服务侧 Pydantic 校验模型（v0.2.1）/ Server-side validation model for the
    ``client:get_skill`` response。

    协议 ``data-structures.md §GetSkillRet`` / ``skill.md §9`` 规定 ``body`` 与 ``blob_handle``
    **恰一存在**（exactly one）：文本且 ≤ 内联预算 → ``body``；二进制或过大文本 → ``blob_handle``。
    本模型用 :meth:`_check_body_blob_xor` 在 Computer 返回前强制该不变量（服务侧自校验，父 #42 跨 PR
    跟进项之一，明确合并到本 PR 范围）；smcp.py 的同名 ``GetSkillRet`` TypedDict 为线缆结构镜像。
    The protocol mandates exactly one of ``body`` / ``blob_handle``; this model enforces that invariant
    before the Computer returns. The TypedDict of the same name in smcp.py is the wire-shape mirror.

    字段语义见 smcp.py ``GetSkillRet`` / Field semantics: see the smcp.py ``GetSkillRet`` TypedDict.
    """

    name: str
    rel_path: str
    mime_type: str
    total_size: int
    sha256: str
    req_id: str
    body: str | None = Field(default=None)
    blob_handle: str | None = Field(default=None)

    # extra="forbid"：服务侧自构造，多余字段即 SDK bug，应硬失败暴露。
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_body_blob_xor(self) -> "GetSkillRet":
        """``body`` 与 ``blob_handle`` 恰一存在（XOR）/ Enforce exactly-one-of(body, blob_handle)。"""
        has_body = self.body is not None
        has_handle = self.blob_handle is not None
        if has_body == has_handle:
            both_or_neither = "both" if has_body else "neither"
            raise ValueError(
                f"GetSkillRet MUST carry exactly one of 'body' / 'blob_handle' (got {both_or_neither}); "
                "protocol data-structures.md §GetSkillRet / skill.md §9",
            )
        return self


# ── #184 启动/连接状态正交化 / Activation/connection state orthogonality ──────────


class MCPServerActivationState(StrEnum):
    """控制面启动意图 / Control-plane activation intent.

    该状态与传输连接正交：OAuth 授权尚未完成时，Server 仍可保持 ``started``，但连接状态为
    :attr:`MCPServerConnectionState.authorization_required`。
    """

    STOPPED = "stopped"
    """未启动或已被显式停止 / Not started or explicitly stopped."""

    STARTED = "started"
    """已接受启动请求（不因 OAuth 未授权而丢失） / Start request accepted."""


class MCPServerConnectionState(StrEnum):
    """数据面连接状态 / Data-plane connection state."""

    DISCONNECTED = "disconnected"
    """当前没有连接 / No current connection."""

    CONNECTING = "connecting"
    """正在建立连接 / Establishing a connection."""

    CONNECTED = "connected"
    """已连接，可提供 MCP 能力 / Connected and able to provide MCP capabilities."""

    AUTHORIZATION_REQUIRED = "authorization_required"
    """连接被 OAuth 授权前置条件阻塞 / Connection blocked on OAuth authorization."""

    ERROR = "error"
    """最近一次连接尝试失败 / The latest connection attempt failed."""


@dataclass(frozen=True)
class MCPServerRuntimeStatus:
    """MCP Server 的正交运行时状态 / Orthogonal MCP server runtime status.

    将控制面启动意图与数据面连接状态解耦：activation 跟踪用户是否请求了启动，
    connection 跟踪传输层状态。
    """

    bundle_id: str
    """稳定身份键 / Stable identity key."""

    name: str
    """展示名称（可碰撞，非身份） / Display name (may collide; NOT identity)."""

    activation: MCPServerActivationState
    """控制面启动意图 / Control-plane activation intent."""

    connection: MCPServerConnectionState
    """数据面连接状态 / Data-plane connection state."""

    def is_started(self) -> bool:
        """是否已接受启动请求 / Whether activation has been requested and retained."""
        return self.activation == MCPServerActivationState.STARTED

    def is_connected(self) -> bool:
        """是否已连接并可提供 MCP 能力 / Whether the data plane is connected."""
        return self.connection == MCPServerConnectionState.CONNECTED
