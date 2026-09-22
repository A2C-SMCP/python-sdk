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
    api_key="your_api_key",        # 凭据：注入 Socket.IO 连接 auth dict 的 token 字段（#112 / AS-38）
    auth_field_name="token",       # auth dict 内凭据字段名（默认 token，可覆盖）
    extra_headers={                # 额外路由请求头（非鉴权；不再承载凭据）
        "User-Agent": "MyAgent/1.0"
    },
    auth_data={                    # 额外业务认证数据，与 api_key 合并进 auth dict
        "tenant_id": "t1"
    }
)
# 上述配置产生的连接 auth dict：{"tenant_id": "t1", "token": "your_api_key"}
# 连接面鉴权统一走 auth dict（HTTP header 不再参与鉴权）
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

> **本调用等待服务端裁决（自 #218 起）。** `server:join_office` 有 ack 通道（成功空 ack / 失败 flat
> `ErrorPayload`），Agent 客户端现在**读取**该 ack：入房被拒会抛
> `SMCPProtocolError`，`.code` 可机器分流（见下方「入房失败的处理」）。等待是**有界**的
> （`OFFICE_JOIN_TIMEOUT` = 10 秒）；同步侧还要叠加 office 操作锁的等待（前方有在途 office 操作时，
> 典型最坏约 2×10 秒，多个排队者时更长）——调用方在事件回调内调用会阻塞该回调线程。
>
> **取锁期间被抢占 ⇒ 本调用静默返回、不发包**（返回形如成功，不抛）。触发者是更新的声明（并发
> `leave_office` / 换房）或会话边界（断连/重连钩子）：前者由后到者负责，后者在「传输中断且会自动
> 重连」时保留意图交给重放，在手工断开 / 服务端踢出时意图已清空、不会再有重放。需要确认是否真的在
> 房里时，以服务端事实为准。
>
> **身份在一条连接内不可变。** 协议规定：同一 sid 声明与既有会话不同的 `role` / `name` ⇒ 拒绝（`403`）。
> 因此**同一条连接**上不能用不同的 `agent_name` 再次 `join_office`——服务端会以 `403` 拒绝。
> 只要本端仍记得已确认的会话身份，异常文案就会附带「本连接会话身份已固化为 `<name>`；改名须重新建立
> 连接」提示；**先 `leave_office` 再改名不会**出现该提示（退房会作废本地身份记忆），此时按同一条规则
> 处理：换名须**重新建立连接**（新 sid）后再入房。换房（不同 `office_id`）不受影响，但 Agent **必须**
> 先 `leave_office` 再入新房（见下文）。

### 入房失败的处理

```python
from a2c_smcp.agent.errors import SMCPProtocolError

try:
    await async_client.join_office("my_office", "my_agent")
except SMCPProtocolError as e:
    # e.code: 4101 房内已有 Agent / 4105 同名 / 4106 已在其它房 / 403 改名被拒 / 400 载荷畸形
    #         -1 = 服务端拒绝但码不可解析（未获裁决）
    print(f"入房被拒：{e.code} {e.error_message}")  # str(e) 形如 "[4101] Room already has an agent"
```

失败后的**本地状态**遵守一条统一规则（显式入房与自动回房同规则、同文案）：

- **明确拒绝**：入房意图回退到「最近一次被服务端确认过的房间」——已经在一个房里换房被拒时，不会
  连带丢掉原房的自动回房能力；
- **未获裁决 / 传输层失败**（超时、连接已断）：意图与「已确认房」一并清空，**不臆断**服务端状态。

> 上述效应**只在本次声明仍是最新时施加**：若同拍已被断连 / 重连的会话边界抢先（超时前连接已断，
> 钩子推进了 generation），意图**保留**待重连重放——重放会用同一张表重新裁决，成功即恢复、被拒才清空。

超时不是 builtin 异常：`socketio.exceptions.TimeoutError` **不是** `TimeoutError` 的子类，按
`a2c_smcp.agent.base.OFFICE_ACK_TIMEOUT_ERRORS` 捕获；未连接就调用则抛
`socketio.exceptions.BadNamespaceError`（发送前失败，**不保留**意图，请先连接）。

**超时（`未获裁决`）后如何收敛**：服务端可能其实已把你加入房间。协议支持的恢复手段是**重发同一次
入房**——同会话重复加入同一房间会被服务端幂等放行（成功 = 空 ack）；要换房则必须先 `leave_office`，
否则会被 `4106` 拒绝。

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

- 协议事件: [事件规范](https://github.com/A2C-SMCP/a2c-smcp-protocol/blob/main/specification/events.md)
- 数据结构: [数据结构规范](https://github.com/A2C-SMCP/a2c-smcp-protocol/blob/main/specification/data-structures.md)
