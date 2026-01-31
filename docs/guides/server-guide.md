# Server 使用指南

A2C-SMCP Server 模块提供 SMCP 协议的服务端实现，负责中央信令服务器功能。

## 概述

Server 模块主要负责：

- 维护 Computer/Agent 元数据信息
- 信号传输转发消息
- 将收到的消息转换为 Notification 广播

## 核心组件

### 1. 认证系统

#### AuthenticationProvider（抽象基类）

```python
from a2c_smcp.server import AuthenticationProvider
from socketio import AsyncServer

class CustomAuthProvider(AuthenticationProvider):
    async def authenticate(
        self,
        sio: AsyncServer,
        environ: dict,
        auth: dict | None,
        headers: list
    ) -> bool:
        # 实现自定义认证逻辑
        # 从 environ、auth 或 headers 中提取认证信息
        pass
```

#### DefaultAuthenticationProvider（默认实现）

```python
from a2c_smcp.server import DefaultAuthenticationProvider

auth_provider = DefaultAuthenticationProvider(
    admin_secret="your_admin_secret",
    api_key_name="x-api-key"  # 可自定义 API 密钥字段名
)
```

### 2. 命名空间系统

#### SMCPNamespace（异步版本）

```python
from a2c_smcp.server import SMCPNamespace, DefaultAuthenticationProvider

auth_provider = DefaultAuthenticationProvider("admin_secret")
smcp_namespace = SMCPNamespace(auth_provider)
```

#### SyncSMCPNamespace（同步版本）

```python
from a2c_smcp.server import SyncSMCPNamespace, DefaultSyncAuthenticationProvider

auth_provider = DefaultSyncAuthenticationProvider("admin_secret")
smcp_namespace = SyncSMCPNamespace(auth_provider)
```

### 3. 类型定义

```python
from a2c_smcp.server import (
    OFFICE_ID,        # 房间 ID 类型别名
    SID,              # 会话 ID 类型别名
    BaseSession,      # 基础会话类型
    ComputerSession,  # Computer 会话类型
    AgentSession,     # Agent 会话类型
    Session           # 联合会话类型
)
```

### 4. 工具函数

```python
from a2c_smcp.server import (
    aget_computers_in_office,      # 异步获取房间内 Computer 列表
    get_computers_in_office,       # 同步获取房间内 Computer 列表
    aget_all_sessions_in_office,   # 异步获取房间内所有会话
    get_all_sessions_in_office,    # 同步获取房间内所有会话
)
```

## 使用示例

### 基础使用（FastAPI）

```python
import asyncio
from fastapi import FastAPI
import socketio
from a2c_smcp.server import SMCPNamespace, DefaultAuthenticationProvider

app = FastAPI()

# 1. 创建认证提供者
auth_provider = DefaultAuthenticationProvider(
    admin_secret="your_admin_secret",
    api_key_name="x-api-key"
)

# 2. 创建 SMCP 命名空间
smcp_namespace = SMCPNamespace(auth_provider)

# 3. 创建 Socket.IO 服务器并注册命名空间
sio = socketio.AsyncServer(cors_allowed_origins="*")
sio.register_namespace(smcp_namespace)

# 4. 挂载到 FastAPI
socket_app = socketio.ASGIApp(sio, app)

# 运行: uvicorn main:socket_app
```

### 同步版本（Flask/WSGI）

```python
from socketio import Server, WSGIApp
from a2c_smcp.server import SyncSMCPNamespace, DefaultSyncAuthenticationProvider

# 1. 创建同步认证提供者
auth_provider = DefaultSyncAuthenticationProvider(
    admin_secret="your_admin_secret",
    api_key_name="x-api-key"
)

# 2. 创建同步 SMCP 命名空间
smcp_namespace = SyncSMCPNamespace(auth_provider)

# 3. 创建 Socket.IO 同步服务器
sio = Server(cors_allowed_origins="*")
sio.register_namespace(smcp_namespace)

# 4. 在 WSGI 框架中使用
app = WSGIApp(sio)

# 运行: gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker main:app
```

### 自定义认证

```python
from a2c_smcp.server import AuthenticationProvider, SMCPNamespace

class DatabaseAuthProvider(AuthenticationProvider):
    def __init__(self, db_connection):
        self.db = db_connection

    async def authenticate(
        self,
        sio,
        environ: dict,
        auth: dict | None,
        headers: list
    ) -> bool:
        # 从 headers 中提取 API 密钥
        api_key = None
        for header in headers:
            if isinstance(header, (list, tuple)) and len(header) >= 2:
                header_name = header[0].decode("utf-8").lower() \
                    if isinstance(header[0], bytes) else str(header[0]).lower()
                header_value = header[1].decode("utf-8") \
                    if isinstance(header[1], bytes) else str(header[1])

                if header_name == "x-api-key":
                    api_key = header_value
                    break

        if not api_key:
            return False

        # 从数据库验证
        user = await self.db.get_user_by_api_key(api_key)
        return user is not None

# 使用
auth_provider = DatabaseAuthProvider(db_connection)
smcp_namespace = SMCPNamespace(auth_provider)
```

### 获取房间信息

```python
from a2c_smcp.server import aget_computers_in_office, aget_all_sessions_in_office

async def get_office_status(office_id: str, sio):
    # 获取房间内所有 Computer
    computers = await aget_computers_in_office(office_id, sio)
    print(f"Office {office_id} has {len(computers)} computers:")
    for computer in computers:
        print(f"  - {computer['name']} (sid: {computer['sid']})")

    # 获取房间内所有会话（包括 Agent）
    sessions = await aget_all_sessions_in_office(office_id, sio)
    print(f"Total sessions: {len(sessions)}")
```

## 支持的事件

### Server 事件（客户端 → Server）

| 事件名称 | 发起方 | 描述 |
|---------|-------|------|
| `server:join_office` | Agent/Computer | 加入房间 |
| `server:leave_office` | Agent/Computer | 离开房间 |
| `server:update_config` | Computer | 配置更新通知请求 |
| `server:update_tool_list` | Computer | 工具列表更新通知请求 |
| `server:update_desktop` | Computer | 桌面更新通知请求 |
| `server:tool_call_cancel` | Agent | 取消工具调用 |
| `server:list_room` | Agent | 列出房间内所有会话 |

### Client 事件（Agent → Computer，由 Server 路由）

| 事件名称 | 描述 |
|---------|------|
| `client:tool_call` | 工具调用 |
| `client:get_tools` | 获取工具列表 |
| `client:get_config` | 获取配置 |
| `client:get_desktop` | 获取桌面信息 |

### Notify 事件（Server → 广播）

| 事件名称 | 描述 |
|---------|------|
| `notify:enter_office` | 成员加入房间通知 |
| `notify:leave_office` | 成员离开房间通知 |
| `notify:update_config` | 配置更新通知 |
| `notify:update_tool_list` | 工具列表更新通知 |
| `notify:update_desktop` | 桌面更新通知 |
| `notify:tool_call_cancel` | 工具调用取消通知 |

## 会话管理

Server 模块自动管理客户端会话：

- **会话状态维护**: 自动跟踪连接状态
- **房间成员管理**: 自动处理加入/离开
- **角色验证**: 区分 Computer 和 Agent
- **权限控制**: 实现房间隔离

## 扩展性

1. **自定义认证**: 实现 `AuthenticationProvider` 接口
2. **事件扩展**: 继承 `SMCPNamespace` 添加新事件
3. **中间件**: 在基础类中添加中间件逻辑
4. **同步/异步**: 选择适合框架的版本

## 注意事项

1. **同步/异步选择**: FastAPI/Sanic 使用异步版本，Flask/Gunicorn 使用同步版本
2. **线程安全**: 异步版本所有方法都是异步的，确保线程安全
3. **内存管理**: 会话数据自动清理
4. **性能优化**: 大量连接时建议使用 Redis 作为会话存储

## 测试

```bash
# 运行所有 Server 测试
pytest tests/unit_tests/server/

# 运行集成测试
pytest tests/integration_tests/server/

# 带覆盖率
pytest tests/unit_tests/server/ --cov=a2c_smcp.server
```

## 参考

- 协议事件定义: [事件规范](../specification/events.md)
- 数据结构: [数据结构规范](../specification/data-structures.md)
- 房间模型: [房间隔离模型](../specification/room-model.md)
