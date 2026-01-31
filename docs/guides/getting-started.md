# 快速开始

本指南帮助你快速上手 A2C-SMCP Python SDK。

## 安装

### 从 PyPI 安装

```bash
# 稳定版
pip install a2c-smcp

# 预发行版（推荐，当前处于 RC 阶段）
pip install --pre a2c-smcp

# 使用 Poetry
poetry add a2c-smcp --allow-prereleases
```

### 可选依赖

```bash
# 安装 CLI 支持
pip install "a2c-smcp[cli]"
```

## 核心概念

A2C-SMCP 有三个核心角色：

| 角色 | 职责 | 你的场景 |
|------|------|---------|
| **Agent** | 工具调用发起方 | 构建智能体/机器人 |
| **Server** | 信令服务器 | 提供中心化消息路由 |
| **Computer** | MCP 服务管理者 | 管理本地工具服务 |

选择你的角色，阅读对应的快速示例。

---

## 场景一：我是 Agent 开发者

你需要调用远程 Computer 上的工具。

### 同步示例

```python
from a2c_smcp.agent import DefaultAgentAuthProvider, SMCPAgentClient

# 1. 创建认证提供者
auth = DefaultAgentAuthProvider(
    agent_id="my_agent",
    office_id="my_office",
    api_key="your_api_key"
)

# 2. 创建客户端并连接
client = SMCPAgentClient(auth_provider=auth)
client.connect_to_server("http://localhost:8000")

# 3. 加入房间
client.join_office("my_office", "my_agent")

# 4. 调用工具
result = client.emit_tool_call(
    computer="target_computer",
    tool_name="file_read",
    params={"path": "/tmp/readme.txt"},
    timeout=30
)

print(result)
```

### 异步示例

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
    await client.join_office("my_office", "my_agent")

    result = await client.emit_tool_call(
        computer="target_computer",
        tool_name="file_read",
        params={"path": "/tmp/readme.txt"},
        timeout=30
    )
    print(result)

asyncio.run(main())
```

**下一步**: 阅读 [Agent 使用指南](agent-guide.md)

---

## 场景二：我需要搭建 Server

你需要提供中心信令服务。

### FastAPI + Socket.IO 示例

```python
from fastapi import FastAPI
import socketio
from a2c_smcp.server import SMCPNamespace, DefaultAuthenticationProvider

app = FastAPI()

# 1. 创建认证提供者
auth = DefaultAuthenticationProvider(
    admin_secret="your_admin_secret",
    api_key_name="x-api-key"
)

# 2. 创建 SMCP 命名空间
smcp_ns = SMCPNamespace(auth)

# 3. 创建 Socket.IO 服务器
sio = socketio.AsyncServer(cors_allowed_origins="*")
sio.register_namespace(smcp_ns)

# 4. 挂载到 FastAPI
socket_app = socketio.ASGIApp(sio, app)

# 运行: uvicorn main:socket_app
```

**下一步**: 阅读 [Server 使用指南](server-guide.md)

---

## 场景三：我需要管理 MCP 服务

你需要在本机管理多个 MCP Server 并暴露工具。

### 使用 CLI

```bash
# 启动 Computer CLI
a2c-computer run --auto-connect true

# 进入交互模式后：
a2c> server add @./my_mcp_config.json  # 添加 MCP Server
a2c> start all                          # 启动所有服务
a2c> tools                              # 查看可用工具
a2c> socket connect http://localhost:8000  # 连接信令服务器
a2c> socket join my_office "My Computer"   # 加入房间
```

### 编程方式

```python
import asyncio
from a2c_smcp.computer import Computer

async def main():
    # 创建 Computer
    computer = Computer(
        name="my_computer",
        mcp_servers={...},  # MCP Server 配置
        auto_connect=True
    )

    # 启动
    await computer.boot_up(session=None)

    # 获取可用工具
    tools = await computer.aget_available_tools()
    print(f"Available tools: {len(tools)}")

asyncio.run(main())
```

**下一步**: 阅读 [Computer 使用指南](computer-guide.md) 或 [CLI 使用指南](cli-guide.md)

---

## 完整示例：三方协作

以下展示 Agent、Server、Computer 三方如何协作。

### 1. 启动 Server

```python
# server_app.py
from fastapi import FastAPI
import socketio
from a2c_smcp.server import SMCPNamespace, DefaultAuthenticationProvider

app = FastAPI()
auth = DefaultAuthenticationProvider(admin_secret="secret")
smcp_ns = SMCPNamespace(auth)
sio = socketio.AsyncServer(cors_allowed_origins="*")
sio.register_namespace(smcp_ns)
socket_app = socketio.ASGIApp(sio, app)

# uvicorn server_app:socket_app --port 8000
```

### 2. 启动 Computer（CLI）

```bash
a2c-computer run
# a2c> server add {"name":"echo","type":"stdio",...}
# a2c> start echo
# a2c> socket connect http://localhost:8000
# a2c> socket join office-001 "EchoComputer"
```

### 3. Agent 调用工具

```python
from a2c_smcp.agent import DefaultAgentAuthProvider, SMCPAgentClient

auth = DefaultAgentAuthProvider(
    agent_id="my_agent",
    office_id="office-001",
    api_key="secret"
)

client = SMCPAgentClient(auth_provider=auth)
client.connect_to_server("http://localhost:8000")

# 获取工具列表
tools = client.get_tools_from_computer("EchoComputer", timeout=10)
print(f"Tools: {[t['name'] for t in tools['tools']]}")

# 调用工具
result = client.emit_tool_call(
    computer="EchoComputer",
    tool_name="echo",
    params={"message": "Hello, World!"},
    timeout=30
)
print(result)
```

---

## 下一步

- [Agent 使用指南](agent-guide.md) - 深入了解 Agent 客户端
- [Server 使用指南](server-guide.md) - 搭建和配置信令服务器
- [Computer 使用指南](computer-guide.md) - 管理 MCP 服务
- [CLI 使用指南](cli-guide.md) - 使用命令行工具
- [协议规范](https://github.com/A2C-SMCP/a2c-smcp-protocol) - 了解协议细节
