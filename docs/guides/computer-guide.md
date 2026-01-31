# Computer 使用指南

A2C-SMCP Computer 模块负责 MCP 服务的生命周期管理与调度，是连接 MCP Server 和 SMCP 协议的桥梁。

## 概述

Computer 模块的核心能力：

- **MCP Server 管理**: 统一管理多个 MCP Server 的生命周期
- **工具聚合**: 将多个 MCP Server 的工具聚合为统一视图
- **Desktop 支持**: 将 `window://` 资源组织为桌面视图
- **Socket.IO 通信**: 与 Server 保持长连接，处理 SMCP 协议事件

## 核心类型

### Computer 类

```python
from a2c_smcp.computer import Computer

computer = Computer(
    name="my_computer",              # Computer 名称
    inputs=set(),                    # 输入配置集合
    mcp_servers=set(),               # MCP Server 配置集合
    auto_connect=True,               # 是否自动连接
    auto_reconnect=True,             # 是否自动重连
    confirm_callback=None,           # 工具调用二次确认回调
    input_resolver=None              # 输入解析器
)
```

### MCP Server 配置

支持三种 MCP Server 类型：

```python
# Stdio 模式
stdio_config = {
    "name": "my-mcp",
    "type": "stdio",
    "disabled": False,
    "forbidden_tools": [],
    "tool_meta": {},
    "server_parameters": {
        "command": "npx",
        "args": ["@example/mcp-server"],
        "env": None,
        "cwd": None,
        "encoding": "utf-8",
        "encoding_error_handler": "strict"
    }
}

# Streamable HTTP 模式
http_config = {
    "name": "http-mcp",
    "type": "streamable",
    "disabled": False,
    "forbidden_tools": [],
    "tool_meta": {},
    "server_parameters": {
        "url": "http://localhost:8080",
        "headers": None,
        "timeout": "PT30S",
        "sse_read_timeout": "PT300S",
        "terminate_on_close": True
    }
}

# SSE 模式
sse_config = {
    "name": "sse-mcp",
    "type": "sse",
    "disabled": False,
    "forbidden_tools": [],
    "tool_meta": {},
    "server_parameters": {
        "url": "http://localhost:8080/sse",
        "headers": None,
        "timeout": 30.0,
        "sse_read_timeout": 300.0
    }
}
```

## 快速开始

### 编程方式

```python
import asyncio
from a2c_smcp.computer import Computer

async def main():
    # 创建 Computer
    computer = Computer(
        name="my_computer",
        mcp_servers={stdio_config}
    )

    # 启动
    await computer.boot_up(session=None)

    # 获取工具列表
    tools = await computer.aget_available_tools()
    print(f"Available tools: {len(tools)}")

    # 执行工具
    result = await computer.aexecute_tool(
        req_id="req-001",
        tool_name="example_tool",
        parameters={"param": "value"},
        timeout=30
    )
    print(result)

    # 关闭
    await computer.shutdown(session=None)

asyncio.run(main())
```

### 使用 CLI

CLI 是使用 Computer 的推荐方式，请参阅 [CLI 使用指南](cli-guide.md)。

## 工具管理

### 获取工具列表

```python
tools = await computer.aget_available_tools()
for tool in tools:
    print(f"- {tool['name']}: {tool['description']}")
```

### 执行工具

```python
from mcp.types import CallToolResult

result: CallToolResult = await computer.aexecute_tool(
    req_id="unique-request-id",
    tool_name="file_read",
    parameters={"path": "/tmp/test.txt"},
    timeout=30
)

if result.isError:
    print(f"Error: {result.content}")
else:
    print(f"Result: {result.content}")
```

### 工具调用历史

```python
history = await computer.aget_tool_call_history()
for record in history:
    print(f"{record.tool_name}: {record.result}")
```

## 工具元数据

通过 `tool_meta` 配置工具的行为：

```python
config = {
    "name": "my-mcp",
    "type": "stdio",
    "tool_meta": {
        "dangerous_tool": {
            "auto_apply": False,  # 需要二次确认
            "alias": "safe_name", # 别名（解决重名冲突）
            "tags": ["filesystem", "write"]
        }
    },
    "default_tool_meta": {
        "auto_apply": True  # 默认自动执行
    },
    ...
}
```

### 工具别名

当多个 MCP Server 存在同名工具时，使用别名区分：

```python
"tool_meta": {
    "read_file": {
        "alias": "local_read_file"  # Agent 使用 local_read_file 调用
    }
}
```

### 二次确认

设置 `auto_apply: False` 的工具需要通过 `confirm_callback` 确认：

```python
async def confirm_callback(tool_name: str, params: dict) -> bool:
    # 返回 True 允许执行，False 拒绝
    return input(f"Execute {tool_name}? (y/n)") == "y"

computer = Computer(
    name="my_computer",
    confirm_callback=confirm_callback,
    ...
)
```

## Desktop 支持

Computer 支持将 MCP Server 的 `window://` 资源组织为 Desktop 视图。

### 获取桌面

```python
desktops = await computer.get_desktop(
    size=10,                    # 限制窗口数量
    window_uri="window://..."   # 指定窗口（可选）
)

for desktop in desktops:
    print(desktop)
```

### 桌面组装规则

1. **size 截断**: 按数量上限截断
2. **server 优先级**: 最近调用工具的 Server 优先
3. **窗口排序**: 按 `priority` 降序
4. **fullscreen**: fullscreen 窗口独占

## Socket.IO 集成

### 绑定 Socket.IO 客户端

```python
from a2c_smcp.computer.socketio import SMCPComputerClient

client = SMCPComputerClient(computer=computer)
await client.connect("http://localhost:8000", namespaces=["/smcp"])

# 加入房间
await client.join_office("my_office")

# 通知工具列表更新
await client.emit_update_tool_list()
```

### 事件回调

Computer 内部会自动处理以下事件：

- `client:tool_call` → `computer.aexecute_tool()`
- `client:get_tools` → `computer.aget_available_tools()`
- `client:get_desktop` → `computer.get_desktop()`
- `client:get_config` → 返回配置信息

## Inputs 系统

Inputs 用于在配置中使用动态占位符：

```python
# 配置中使用占位符
config = {
    "name": "my-mcp",
    "server_parameters": {
        "command": "my-tool",
        "args": ["--api-key", "${input:api_key}"]
    }
}

# 定义 Input
input_def = {
    "id": "api_key",
    "type": "promptString",
    "description": "API Key",
    "password": True
}
```

详见 [Inputs 配置参考](../reference/inputs-config.md)。

## 最佳实践

1. **使用 CLI 进行调试**: CLI 提供交互式调试环境
2. **合理设置超时**: 根据工具特性设置合适的超时时间
3. **使用别名避免冲突**: 多 MCP Server 场景下使用别名
4. **配置二次确认**: 对危险操作启用二次确认

## 参考

- [CLI 使用指南](cli-guide.md)
- [MCP 配置参考](../reference/mcp-config.md)
- [Inputs 配置参考](../reference/inputs-config.md)
- [Desktop 系统](../advanced/desktop-system.md)
