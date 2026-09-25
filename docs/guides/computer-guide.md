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

> **改名需要新连接。** 协议规定：同一 sid 声明与既有会话不同的 `role` / `name` ⇒ 拒绝（`403`）。
> `server:join_office` 声明的 `name` 取自 `computer.name`，故**不要**在同一连接上改 `computer.name`
> 后再次 `join_office`——服务端会以 `403` 拒绝，而客户端会抛 `RuntimeError`。要改名请**新起一条连接**
> （新 sid）再入房；CLI 的 `socket join <office> <name>` 已按此自动完成（见 cli-guide）。

### 动态 auth provider

> #200（方案 C 原生透传）：python-socketio 原生支持 auth callable，且**每次握手**
> （首连 + 每次自动重连）都会重新求值。SDK 以显式 `auth_provider` 参数公开该能力，
> 零新增机制、纯 asyncio。

```python
from a2c_smcp.computer.socketio import SMCPComputerClient

async def fresh_auth() -> dict:
    # 每次握手前调用：轮换短期凭证无需拆除健康连接
    return {"token": await token_exchange.fetch()}  # 例：最新短期 Token

client = SMCPComputerClient(computer=computer)
await client.connect(
    "http://localhost:8000",
    auth_provider=fresh_auth,  # 类型：AuthProvider = Callable[[], Awaitable[dict]]
    namespaces=["/smcp"],
)
```

**语义**：

- provider 在首次连接和每次自动重连的握手前被 `await` 重新求值——重连携带调用时最新
  auth；静态 `auth` 调用方式不受影响（零回归）。
- `auth` 与 `auth_provider` 互斥：同时传入 → `ValueError`；非 callable → `TypeError`（早失败）。

> 注意与 Agent 侧 `AgentAuthProvider`（ABC，`get_connection_auth()`）区分：那是服务端鉴权的
> 提供者抽象；本节的 `AuthProvider` 是 Computer 客户端**连接面**的零参 callable 别名（每次握手
> 重新求值）。命名相近、语义不同，勿混用。

**Provider 契约（务必遵守）**：

1. **异常不得内嵌 secret**——engineio 会对 provider 异常打印 traceback（上游限制，跟踪
   [#201](https://github.com/A2C-SMCP/python-sdk/issues/201)）。
2. **无超时保障**——provider 永不返回会无限挂起握手（上游缺陷，跟踪 #201）；获取、等待、
   重试凭证由 provider 实现方负责。
3. provider 异常表现为连接失败，重试节奏沿用 socketio 有界重试，瞬时失败可自愈。
4. SDK 不持久化、不打印 auth payload。
5. 推荐 async callable（同步 callable 亦被原生路径接受）。

### 断线重连后的 Office 自动回房

> #203：Socket.IO 的房间成员关系属于**会话**——传输层断线自动重连后 namespace 换新 SID，
> 服务端已销毁旧会话的成员关系。SDK 会在重连成功后自动重放 `server:join_office`
> （镜像 rust-sdk#204），无需宿主干预。

**语义**：

- `office_id` 是**期望成员关系（desired）**：传输中断且底层会自动重连时保留，重连成功后自动回房；
  回房**持续失败**（重名被拒 / 一房一 Agent 被拒等，且重试预算耗尽）才清空并打错误日志——**不会**停留在
  "看起来还在房间、实际收不到流量"的状态（该状态是该 Issue 修复的核心）。
- **自己预置 `office_id` 再 `connect()`**（宿主直接赋值，不经 `join_office`）同样走这条重放通道：首连
  成功即入房；若撞上冲突（例如上一进程被 kill 后旧会话仍在房里 → `4105`）也享受下面同一套退避重试
  ——冷启动自愈靠的就是它。CLI 的 `socket join` 走**显式** `join_office`，**不**重试。
- **手工断开**（`client.disconnect()`）、服务端踢出、重连彻底放弃（重试次数用尽）均清空期望，
  下次 `connect()` 不会静默回旧房间。
- **`leave_office` 无条件清本地意图（#223）**：先清 desired + 已确认房号、作废在途自动回房，再尽力通知
  服务端；namespace 不在册（已断线）时**不发包**也**不抛**异常——那时服务端会话已随连接销毁，本就没有
  可退的成员关系。此前是「先发包后清」，断线时直接抛 `BadNamespaceError` ⇒ 本地意图留着，重连后的自动
  回房会把用户刚退掉的房又回一遍（CLI `socket leave` 在断线期间就撞在这上面）。
- **瞬态冲突的有界退避重试（#212）**：静默断线后服务端要等自身心跳超时才回收旧会话（socket.io 默认
  `ping_interval(25) + ping_timeout(20)` ⇒ 最长 **45 秒**），这段时间的回房重放会被判 `4101` / `4105`。
  SDK 对这两码按 `1→2→4→5→5…` 秒退避重试，**默认预算 45 秒**，预算耗尽才清空状态 + 打错误日志 ⇒
  网络抖动通常**无感自愈**，无需人工重入。有界等待仍是 10 秒/次；退避期间 office 操作互斥空闲
  （每次尝试各取一次锁），恢复耗时的上界 = 预算 + 一次有界等待。
  - **可配置**：`client.office_rejoin_retry_budget = 30.0`（公开实例属性；`<= 0` 关掉重试）。
    部署方调大了服务端 `ping_interval` / `ping_timeout` 时须相应调大——协议的 SHOULD 级部署约束
    （回收窗口应与客户端可接受的恢复时延相称）是客户端补偿能生效的前提，调大到远超预算时重试必然徒劳。
  - `4106` / `500` / 未知码 / 无码 / 传输层失败**不重试**（重试不改变结果，或成员关系可能已变）。
- **断连 / 回放在途窗口内的状态上报（#223）**：`emit_update_config` / `emit_update_tool_list` /
  `emit_refresh_desktop` / `emit_update_skills` 的发送判据是「**有入房意图**（`office_id` 非空）∧
  **服务端已确认在房**（`_confirmed_office_id` 非空）∧ namespace 在册」——与协议 `computer.md §2.2` 的
  「Computer SHOULD 在成功加入 Office 后才发送这四个事件」对齐（rust 侧同款 `has_confirmed_office()`）。
  判据不成立时**不发包**（发了也会被服务端按「未入房」丢弃，而这些事件是 fire-and-forget、无
  ack，客户端察觉不到），改为**记下这一类别**；等成员关系重新确立（自动回房成功或显式入房成功）后
  **逐条补发一次**。协议明确允许这种做法（「未加入 Office 时，本地变化可以被记录或合并，但不应产生跨房间
  可见通知」）。
  - 效果：**自本端感知断连起**，断连 / 退避重试窗口内改配置、改工具、改 SKILL、改桌面，房内 Agent 不会
    漏——回房成功后它们各自收到一次对应的 `notify:update_*`（在此之前，工具面靠 `notify:enter_office`
    自动重拉能自愈，config / skills / desktop 三类则要等到下一次变更）。
  - 合并语义：同一类别在窗口内改多次只补发**一次**（按事件名去重）；补发失败（例如又断线）的类别留待
    **下一次**成员关系确立时再补，退房不清空。
  - 从未入房时不记录（房内没有旧状态可陈旧，入房时对面本就会重拉）；预算耗尽 / 已退房后 `office_id`
    已清，同样不记录。
  - **已知边界（无法消除）**：从物理掉线到本端察觉（`disconnect` 钩子触发）之间，判据仍为真 ⇒ 走直发
    分支、包随垂死的传输一起丢，既不送达也不补发。要消除它必须给这四个事件加 ack，协议明确禁止；故上面
    的保证只从「本端感知断连」起算。
  - **已知边界（既存，非本单引入）**：Computer **进程重启**后用同名入房时，房内 Agent 只会被
    `notify:enter_office` 触发重拉**工具**；config / skills / desktop 的旧视图要等到下一次变更才刷新。
- Agent 侧（`AsyncSMCPAgentClient` / `SMCPAgentClient`）同语义：`join_office(office_id, agent_name)`
  会记住该意图，自动重连后重放（同样享受退避重试）；`leave_office` / 手工断开清空。
- **差异（自 #218 起）**：Agent 的**显式** `join_office` 也等 ACK（有界 10 秒，与回房同值），入房被拒
  抛 `SMCPProtocolError`（`.code` 可分流）、失败后的意图去留与回房**同一张效应表**；Computer 的显式
  `join_office` 仍走 socketio 默认超时并抛 `RuntimeError`（不带协议码），且**不重试**
  （显式入房撞上冲突即永久冲突——退避重试只给回放路径）。

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
