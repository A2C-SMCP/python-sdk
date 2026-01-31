# A2C-SMCP 数据结构定义

本文档定义了 A2C-SMCP 协议的所有数据结构，基于 `a2c_smcp/smcp.py` 中的 TypedDict 定义。

## 通用约定

- 所有数据结构使用 JSON 对象表示
- 字段命名使用 `snake_case` 风格
- 可选字段使用 `NotRequired` 标注
- 时间相关字段：超时使用整数秒

---

## 基础数据结构

### AgentCallData

Agent 发起调用的基础数据，被多个请求结构继承。

```python
class AgentCallData(TypedDict):
    agent: str      # Agent 名称/标识
    req_id: str     # 请求 ID，用于去重和关联
```

---

## 工具相关结构

### ToolCallReq

工具调用请求，继承自 `AgentCallData`。

```python
class ToolCallReq(AgentCallData):
    agent: str          # Agent 名称
    req_id: str         # 请求 ID
    computer: str       # 目标 Computer 名称
    tool_name: str      # 工具名称
    params: dict        # 工具调用参数
    timeout: int        # 超时时间（秒）
```

### SMCPTool

SMCP 协议中的工具定义，用于工具列表返回。

```python
class SMCPTool(TypedDict):
    name: str                           # 工具名称
    description: str                    # 工具描述
    params_schema: dict                 # 参数 JSON Schema
    return_schema: dict | None          # 返回值 JSON Schema（可选）
    meta: NotRequired[Attributes | None]  # 工具元数据（可选）
```

**说明**: 当 Computer 管理多个 MCP Server 时，可能存在工具名称冲突。此时可通过 `meta` 中的 `alias` 字段设置别名进行区分。

### ToolMeta

工具元数据配置。

```python
class ToolMeta(TypedDict, total=False):
    auto_apply: NotRequired[bool | None]
    # 是否自动应用（跳过二次确认）

    ret_object_mapper: NotRequired[dict | None]
    # 返回值对象映射，用于统一不同 MCP 工具的返回格式

    alias: NotRequired[str | None]
    # 工具别名，用于解决不同 Server 下的工具重名冲突

    tags: NotRequired[list[str] | None]
    # 工具标签，用于分类
```

### GetToolsReq

获取工具列表请求。

```python
class GetToolsReq(AgentCallData):
    agent: str      # Agent 名称
    req_id: str     # 请求 ID
    computer: str   # 目标 Computer 名称
```

### GetToolsRet

获取工具列表响应。

```python
class GetToolsRet(TypedDict):
    tools: list[SMCPTool]   # 工具列表
    req_id: str             # 请求 ID
```

---

## 房间管理结构

### EnterOfficeReq

加入房间请求。

```python
class EnterOfficeReq(TypedDict):
    role: Literal["computer", "agent"]  # 角色类型
    name: str                           # 名称
    office_id: str                      # 房间 ID
```

### LeaveOfficeReq

离开房间请求。

```python
class LeaveOfficeReq(TypedDict):
    office_id: str      # 房间 ID
```

### EnterOfficeNotification

成员加入房间通知。

```python
class EnterOfficeNotification(TypedDict, total=False):
    office_id: str              # 房间 ID
    computer: str | None        # 加入的 Computer 名称（若为 Computer）
    agent: str | None           # 加入的 Agent 名称（若为 Agent）
```

### LeaveOfficeNotification

成员离开房间通知。

```python
class LeaveOfficeNotification(TypedDict, total=False):
    office_id: str              # 房间 ID
    computer: str | None        # 离开的 Computer 名称
    agent: str | None           # 离开的 Agent 名称
```

### ListRoomReq

列出房间内会话请求。

```python
class ListRoomReq(AgentCallData):
    agent: str          # Agent 名称
    req_id: str         # 请求 ID
    office_id: str      # 房间 ID
```

### SessionInfo

会话信息。

```python
class SessionInfo(TypedDict, total=False):
    sid: str                                # 会话 ID
    name: str                               # 会话名称
    role: Literal["computer", "agent"]      # 角色
    office_id: str                          # 所属房间 ID
```

### ListRoomRet

列出房间内会话响应。

```python
class ListRoomRet(TypedDict):
    sessions: list[SessionInfo]     # 会话列表
    req_id: str                     # 请求 ID
```

---

## 配置相关结构

### UpdateComputerConfigReq

配置更新请求。

```python
class UpdateComputerConfigReq(TypedDict):
    computer: str       # Computer 名称
```

### UpdateMCPConfigNotification

配置更新通知。

```python
class UpdateMCPConfigNotification(TypedDict, total=False):
    computer: str       # Computer 名称
```

### UpdateToolListNotification

工具列表更新通知。

```python
class UpdateToolListNotification(TypedDict, total=False):
    computer: str       # Computer 名称
```

### GetComputerConfigReq

获取 Computer 配置请求。

```python
class GetComputerConfigReq(AgentCallData):
    agent: str          # Agent 名称
    req_id: str         # 请求 ID
    computer: str       # 目标 Computer 名称
```

### GetComputerConfigRet

获取 Computer 配置响应。

```python
class GetComputerConfigRet(TypedDict):
    inputs: NotRequired[list[MCPServerInput] | None]    # 输入定义列表
    servers: dict[str, MCPServerConfig]                 # MCP Server 配置映射
```

---

## Desktop 相关结构

### GetDeskTopReq

获取桌面信息请求。

```python
class GetDeskTopReq(AgentCallData, total=True):
    agent: str                      # Agent 名称
    req_id: str                     # 请求 ID
    computer: str                   # 目标 Computer 名称
    desktop_size: NotRequired[int]  # 可选：限制返回的桌面内容数量
    window: NotRequired[str]        # 可选：指定获取的 WindowURI
```

### GetDeskTopRet

获取桌面信息响应。

```python
Desktop: TypeAlias = str

class GetDeskTopRet(TypedDict, total=False):
    desktops: list[Desktop]     # 桌面内容列表（字符串形式）
    req_id: str                 # 请求 ID
```

---

## MCP Server 配置结构

### BaseMCPServerConfig

MCP Server 配置基类。

```python
class BaseMCPServerConfig(TypedDict):
    name: str
    # MCP Server 名称

    disabled: bool
    # 是否禁用

    forbidden_tools: list[str]
    # 禁用的工具列表

    tool_meta: dict[str, ToolMeta]
    # 工具元数据映射（工具名 → 元数据）

    default_tool_meta: NotRequired[ToolMeta | None]
    # 默认工具元数据，当具体工具未配置时使用

    vrl: NotRequired[str | None]
    # VRL 脚本，用于动态转换工具返回值
```

### MCPServerStdioConfig

标准输入输出模式的 MCP Server 配置。

```python
class MCPServerStdioParameters(TypedDict):
    command: str
    # 启动命令

    args: list[str]
    # 命令行参数

    env: dict[str, str] | None
    # 环境变量

    cwd: str | None
    # 工作目录

    encoding: str
    # 文本编码，默认 utf-8

    encoding_error_handler: Literal["strict", "ignore", "replace"]
    # 编码错误处理方式


class MCPServerStdioConfig(BaseMCPServerConfig):
    type: Literal["stdio"]
    server_parameters: MCPServerStdioParameters
```

### MCPServerStreamableHttpConfig

Streamable HTTP 模式的 MCP Server 配置。

```python
class MCPServerStreamableHttpParameters(TypedDict):
    url: str
    # 端点 URL

    headers: dict[str, Any] | None
    # 请求头

    timeout: str
    # HTTP 超时（ISO 8601 duration 格式）

    sse_read_timeout: str
    # SSE 读取超时（ISO 8601 duration 格式）

    terminate_on_close: bool
    # 关闭时是否终止客户端会话


class MCPServerStreamableHttpConfig(BaseMCPServerConfig):
    type: Literal["streamable"]
    server_parameters: MCPServerStreamableHttpParameters
```

### MCPSSEConfig

SSE 模式的 MCP Server 配置。

```python
class MCPSSEParameters(TypedDict):
    url: str
    # 端点 URL

    headers: dict[str, Any] | None
    # 请求头

    timeout: float
    # HTTP 超时（秒）

    sse_read_timeout: float
    # SSE 读取超时（秒）


class MCPSSEConfig(BaseMCPServerConfig):
    type: Literal["sse"]
    server_parameters: MCPSSEParameters
```

### MCPServerConfig

MCP Server 配置联合类型。

```python
MCPServerConfig = MCPServerStdioConfig | MCPServerStreamableHttpConfig | MCPSSEConfig
```

**注意**: MCP Server 类型为 `"stdio"`, `"streamable"`, `"sse"` 三种，其中 `"streamable"` 对应 MCP 官方的 Streamable HTTP 传输模式。

---

## 输入配置结构

输入配置用于定义 MCP Server 配置中的动态占位符。

### MCPServerInputBase

输入配置基类。

```python
class MCPServerInputBase(TypedDict):
    id: str             # 输入 ID
    description: str    # 描述
```

### MCPServerPromptStringInput

字符串输入类型。

```python
class MCPServerPromptStringInput(MCPServerInputBase):
    type: Literal["promptString"]
    default: NotRequired[str | None]        # 默认值
    password: NotRequired[bool | None]      # 是否为密码（隐藏输入）
```

### MCPServerPickStringInput

选择输入类型。

```python
class MCPServerPickStringInput(MCPServerInputBase):
    type: Literal["pickString"]
    options: list[str]                      # 可选项列表
    default: NotRequired[str | None]        # 默认值
```

### MCPServerCommandInput

命令输入类型（通过执行命令获取值）。

```python
class MCPServerCommandInput(MCPServerInputBase):
    type: Literal["command"]
    command: str                            # 要执行的命令
    args: NotRequired[dict[str, str] | None]  # 命令参数
```

### MCPServerInput

输入配置联合类型。

```python
MCPServerInput = MCPServerPromptStringInput | MCPServerPickStringInput | MCPServerCommandInput
```

---

## 类型别名

```python
from a2c_smcp.types import SERVER_NAME, TOOL_NAME, Attributes, AttributeValue

SERVER_NAME: TypeAlias = str    # MCP Server 名称
TOOL_NAME: TypeAlias = str      # 工具名称
AttributeValue: TypeAlias = str | int | float | bool | None
Attributes: TypeAlias = dict[str, AttributeValue]
Desktop: TypeAlias = str        # 桌面内容（字符串形式）
```

---

## 参考

- 类型定义源码: `a2c_smcp/smcp.py`
- Pydantic 模型: `a2c_smcp/computer/mcp_clients/model.py`
- 通用类型: `a2c_smcp/types.py`
