# Agent 使用指南

A2C-SMCP Agent 模块提供 Agent 端的 SMCP 协议客户端实现，支持同步和异步两种模式。

## 概述

Agent 模块主要包含：

- **认证系统**: 抽象认证接口和默认实现
- **客户端实现**: 同步和异步的 SMCP 协议客户端
- **事件处理**: 灵活的事件处理机制
- **类型定义**: 完整的类型系统支持

## 快速开始

### 同步客户端

```python
from a2c_smcp.agent import DefaultAgentAuthProvider, SMCPAgentClient

# 创建认证提供者
auth = DefaultAgentAuthProvider(
    agent_id="my_agent",
    office_id="my_office",
    api_key="your_api_key"
)

# 创建客户端并连接
client = SMCPAgentClient(auth_provider=auth)
client.connect_to_server("http://localhost:8000")

# 调用工具
result = client.emit_tool_call(
    computer="target_computer",
    tool_name="example_tool",
    params={"param1": "value1"},
    timeout=30
)

print(result)
```

### 异步客户端

```python
import asyncio
from a2c_smcp.agent import DefaultAgentAuthProvider, AsyncSMCPAgentClient

async def main():
    auth = DefaultAgentAuthProvider(
        agent_id="my_agent",
        office_id="my_office",
        api_key="your_api_key"
    )

    client = AsyncSMCPAgentClient(auth_provider=auth)
    await client.connect_to_server("http://localhost:8000")

    result = await client.emit_tool_call(
        computer="target_computer",
        tool_name="example_tool",
        params={"param1": "value1"},
        timeout=30
    )

    print(result)

asyncio.run(main())
```

## 认证系统

### 默认认证提供者

```python
from a2c_smcp.agent import DefaultAgentAuthProvider

auth = DefaultAgentAuthProvider(
    agent_id="my_agent",           # Agent 唯一标识
    office_id="my_office",         # 房间 ID
    api_key="your_api_key",        # API 密钥
    api_key_header="x-api-key",    # API 密钥请求头名称
    extra_headers={                # 额外请求头
        "User-Agent": "MyAgent/1.0"
    },
    auth_data={                    # 额外认证数据
        "token": "auth_token"
    }
)
```

### 自定义认证提供者

```python
from a2c_smcp.agent import AgentAuthProvider, AgentConfig

class MyAuthProvider(AgentAuthProvider):
    def __init__(self, agent_id: str, office_id: str):
        self._agent_id = agent_id
        self._office_id = office_id

    def get_agent_id(self) -> str:
        return self._agent_id

    def get_connection_auth(self) -> dict | None:
        return {"token": "my_custom_token"}

    def get_connection_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer my_token"}

    def get_agent_config(self) -> AgentConfig:
        return AgentConfig(
            agent=self._agent_id,
            office_id=self._office_id
        )
```

## 事件处理

### 同步事件处理器

```python
from a2c_smcp.agent.types import AgentEventHandler
from a2c_smcp.smcp import (
    EnterOfficeNotification,
    LeaveOfficeNotification,
    UpdateMCPConfigNotification,
    SMCPTool
)

class MyEventHandler:
    def on_computer_enter_office(
        self,
        data: EnterOfficeNotification,
        client: SMCPAgentClient
    ) -> None:
        print(f"Computer {data['computer']} joined")
        # 自动获取工具列表
        tools = client.get_tools_from_computer(data['computer'], timeout=10)
        print(f"Got {len(tools['tools'])} tools")

    def on_computer_leave_office(
        self,
        data: LeaveOfficeNotification,
        client: SMCPAgentClient
    ) -> None:
        print(f"Computer {data['computer']} left")

    def on_computer_update_config(
        self,
        data: UpdateMCPConfigNotification,
        client: SMCPAgentClient
    ) -> None:
        print(f"Computer {data['computer']} updated config")

    def on_tools_received(
        self,
        computer: str,
        tools: list[SMCPTool],
        client: SMCPAgentClient
    ) -> None:
        print(f"Received {len(tools)} tools from {computer}")

# 使用
handler = MyEventHandler()
client = SMCPAgentClient(auth_provider=auth, event_handler=handler)
```

### 异步事件处理器

```python
from a2c_smcp.agent.types import AsyncAgentEventHandler

class MyAsyncEventHandler:
    async def on_computer_enter_office(
        self,
        data: EnterOfficeNotification,
        client: AsyncSMCPAgentClient
    ) -> None:
        await self.handle_new_computer(data['computer'])

    async def on_computer_leave_office(
        self,
        data: LeaveOfficeNotification,
        client: AsyncSMCPAgentClient
    ) -> None:
        await self.cleanup_computer(data['computer'])

    async def on_computer_update_config(
        self,
        data: UpdateMCPConfigNotification,
        client: AsyncSMCPAgentClient
    ) -> None:
        await self.refresh_config(data['computer'])

    async def on_tools_received(
        self,
        computer: str,
        tools: list[SMCPTool],
        client: AsyncSMCPAgentClient
    ) -> None:
        await self.register_tools(computer, tools)
```

## 工具调用

### 基本调用

```python
from mcp.types import CallToolResult

# 同步调用
result: CallToolResult = client.emit_tool_call(
    computer="target_computer",
    tool_name="file_read",
    params={"path": "/path/to/file.txt"},
    timeout=30
)

if result.isError:
    print(f"Error: {result.content}")
else:
    print(f"Success: {result.content}")

# 异步调用
result = await async_client.emit_tool_call(
    computer="target_computer",
    tool_name="file_read",
    params={"path": "/path/to/file.txt"},
    timeout=30
)
```

### 获取工具列表

```python
# 同步
tools = client.get_tools_from_computer("target_computer", timeout=20)
for tool in tools['tools']:
    print(f"- {tool['name']}: {tool['description']}")

# 异步
tools = await async_client.get_tools_from_computer("target_computer", timeout=20)
```

### 获取桌面信息

```python
# 同步
desktop = client.get_desktop_from_computer(
    "target_computer",
    size=10,                           # 限制窗口数量
    window="window://specific_window", # 指定窗口（可选）
    timeout=20
)
print(f"Desktop windows: {len(desktop['desktops'])}")

# 异步
desktop = await async_client.get_desktop_from_computer(
    "target_computer",
    size=10,
    timeout=20
)
```

### 获取房间内 Computer 列表

```python
from a2c_smcp.smcp import SessionInfo

# 同步
computers: list[SessionInfo] = client.get_computers_in_office(
    "my_office",
    timeout=20
)
for c in computers:
    print(f"Computer: {c['name']} (sid: {c['sid']})")

# 异步
computers = await async_client.get_computers_in_office("my_office", timeout=20)
```

## 房间管理

### 加入房间

```python
# 同步
client.join_office("my_office", "my_agent")

# 异步
await async_client.join_office("my_office", "my_agent")
```

### 离开房间

```python
# 同步
client.leave_office("my_office")

# 异步
await async_client.leave_office("my_office")
```

## 错误处理

### 连接错误

```python
try:
    client.connect_to_server("http://localhost:8000")
except Exception as e:
    print(f"Connection failed: {e}")
```

### 工具调用错误

```python
try:
    result = client.emit_tool_call(
        computer="target_computer",
        tool_name="risky_tool",
        params={},
        timeout=10
    )

    if result.isError:
        print(f"Tool error: {result.content}")
    else:
        print(f"Success: {result.content}")

except TimeoutError:
    print("Tool call timed out")
except Exception as e:
    print(f"Unexpected error: {e}")
```

### 重试机制

```python
import time
from typing import Optional

def retry_tool_call(
    client: SMCPAgentClient,
    computer: str,
    tool_name: str,
    params: dict,
    max_retries: int = 3,
    timeout: int = 30
) -> Optional[CallToolResult]:
    for attempt in range(max_retries):
        try:
            result = client.emit_tool_call(
                computer, tool_name, params, timeout
            )
            if not result.isError:
                return result
        except Exception as e:
            print(f"Attempt {attempt + 1} failed: {e}")

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)  # 指数退避

    return None
```

## 资源管理

### 同步客户端

```python
try:
    client = SMCPAgentClient(auth_provider=auth)
    client.connect_to_server("http://localhost:8000")

    # 业务逻辑
    result = client.emit_tool_call(...)

finally:
    if client.connected:
        client.disconnect()
```

### 异步客户端（上下文管理器）

```python
async with AsyncSMCPAgentClient(auth_provider=auth) as client:
    await client.connect_to_server("http://localhost:8000")

    # 业务逻辑
    result = await client.emit_tool_call(...)

    # 自动断开连接
```

## 配置选项

### 连接配置

```python
# 同步
client.connect_to_server(
    url="http://localhost:8000",
    namespace="/smcp",
    transports=["websocket"],
    wait_timeout=10
)

# 异步
await async_client.connect_to_server(
    url="http://localhost:8000",
    namespace="/smcp",
    transports=["websocket"],
    wait_timeout=10
)
```

## 调试

```python
import logging

# 启用详细日志
logging.basicConfig(level=logging.DEBUG)

# 检查连接状态
if client.connected:
    print("Connected")
else:
    print("Not connected")

# 监听所有事件（调试用）
@client.on('*')
def catch_all(event, *args):
    print(f"Event: {event}, args: {args}")
```

## 常见问题

1. **连接失败**
   - 检查服务器 URL 是否正确
   - 验证网络连接
   - 确认认证信息有效

2. **工具调用超时**
   - 增加超时时间
   - 检查目标 Computer 是否在线
   - 验证工具名称和参数

3. **事件处理器未被调用**
   - 确认事件处理器已正确注册
   - 检查房间 ID 是否匹配
   - 验证 Socket.IO 连接状态

## 参考

- 协议事件: [事件规范](../specification/events.md)
- 数据结构: [数据结构规范](../specification/data-structures.md)
- API 参考: [Agent API](../reference/agent-api.md)
