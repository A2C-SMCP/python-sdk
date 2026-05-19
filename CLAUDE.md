# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A2C-SMCP (Agent To Computer SMCP) 是实现 SMCP 协议的 Python SDK，用于远程工具调用。该协议通过引入 "Computer" 抽象层来解决 Agent 系统中 MCP 协议的挑战，统一管理多个 MCP 服务器、处理认证/凭证，并通过 Socket.IO 实现网络穿透。

## 开发命令

```bash
# 安装依赖 (使用 uv 或 poetry)
uv sync --all-groups  # 或: poetry install --with dev,test,build

# 运行单元测试和集成测试
uv run poe test

# 运行测试并生成覆盖率报告 (不含 e2e)
uv run poe test-cov

# 仅运行 e2e 测试
uv run poe test-e2e

# 运行单个测试文件
uv run pytest tests/unit_tests/path/to/test_file.py

# 运行单个测试函数
uv run pytest tests/unit_tests/path/to/test_file.py::test_function_name

# Lint 和类型检查
uv run poe lint

# 代码格式化
uv run poe format

# 仅类型检查
uv run poe typecheck
```

## 架构

### 三模块系统 (Agent ↔ Server ↔ Computer)

SDK 实现了三个通过 Socket.IO 通信的协作组件：

1. **Computer** (`a2c_smcp/computer/`): 管理 MCP 服务器生命周期、工具调度和桌面窗口聚合。提供交互式 CLI。

2. **Server** (`a2c_smcp/server/`): 中央信令服务器，维护 Computer/Agent 元数据、路由消息、广播通知。同时提供同步和异步命名空间实现。

3. **Agent** (`a2c_smcp/agent/`): 业务侧客户端 SDK，用于在 Computer 上调用工具。提供同步 (`SMCPAgentClient`) 和异步 (`AsyncSMCPAgentClient`) 客户端。

### 协议定义

- **主要**: `a2c_smcp/smcp.py` - 基于 TypedDict 的协议结构和事件常量
- **辅助**: `a2c_smcp/computer/mcp_clients/model.py` - Pydantic 实现，用于数据校验

修改协议结构时，两个文件都需要更新。

### 关键子系统

**MCP 客户端管理** (`a2c_smcp/computer/mcp_clients/`):
- `base_client.py` + `base_client.pyi`: 使用 `transitions` 库的状态机基类。`.pyi` 存根文件提供状态机方法的类型提示。
- `stdio_client.py`, `http_client.py`, `sse_client.py`: 不同传输方式的实现
- `manager.py`: 编排多个 MCP 客户端

**桌面/窗口系统** (`a2c_smcp/computer/desktop/`):
- 将 MCP 服务器的 `window://` 资源聚合为统一的桌面视图（仅聚合 `window://`，非 window 资源不进桌面）
- `a2c_smcp/utils/window_uri.py`: 窗口 URI 解析（v0.2 起为**纯标识符**，不再解析 query）；
  优先级/全屏从 `Resource.annotations` / `_meta` 读取，由 `desktop/organize.py` 组织

**输入解析** (`a2c_smcp/computer/inputs/`):
- MCP 服务器配置的占位符解析系统

## 同步/异步一致性

Server 和 Agent 模块同时提供同步和异步实现。修改功能时，确保两个版本同步更新：
- Server: `namespace.py` (异步) / `sync_namespace.py` (同步)
- Agent: `client.py` (异步) / `sync_client.py` (同步)

## 事件系统约定

Socket.IO 事件遵循以下前缀：
- `client:*` - Agent → Computer (通过 Server 路由)
- `server:*` - 客户端 → Server
- `notify:*` - Server → 广播

添加/修改事件时，需要更新：
1. `smcp.py` 中的事件常量
2. 相关命名空间/客户端类中的处理方法
3. 测试中的 Mock 服务器事件处理器

### v0.2 协议新增 (a2c-smcp-protocol v0.2.0 GA)

- **`client:get_resources`** (`GET_RESOURCES_EVENT`): Agent → Computer，透明转发 MCP
  `resources/list`，支持 MCP 标准 `cursor` 翻页（SDK **不**自动遍历，cursor 由调用方控制）。
  返回 `{resources, next_cursor?}`；错误以 flat `ErrorPayload` 返回（`4014` MCP Server 未注册 /
  `4015` 未声明 `resources` 能力，无嵌套 envelope）。响应字段 camelCase→snake_case 规整
  （如 `mimeType`→`mime_type`）。
- **连接版本握手**: 客户端在 Socket.IO 连接 URL query 携带 `a2c_version`；Server 端由
  `A2CProtocolVersionASGIMiddleware` 校验，不兼容返回 HTTP 400 + Socket.IO `4008`，并写入
  `SessionInfo.a2c_version`（经 `server:list_room` 带出，仅展示/诊断）。不兼容客户端**连接被拒、
  绝不建立**（versioning.md §4 死循环防御）。客户端强制 polling-first（显式 `transports=["websocket"]`
  会被护栏纠正），确保 4008 可被还原归一为 `ProtocolVersionError`。
- **URI 纯标识符化**: `window://` 不再在 URI query 携带 `priority`/`fullscreen`；元数据下沉到 MCP
  `Resource.annotations`（`priority`/`audience`/`last_modified`）与 `_meta`（`fullscreen` 等 A2C 扩展）。
- **DPE 已整体移除**: A2C-SMCP 控制面不再涉及 `client:get_dpe` / `dpe://` 解析；DPE 迁出至独立
  [dpe-protocol](https://github.com/A2C-SMCP/dpe-protocol) 仓库。修改时勿重新引入 DPE 事件/类型。

## 测试结构

测试目录结构与 `a2c_smcp/` 源码结构保持一致：
- `tests/unit_tests/` - 单元测试
- `tests/integration_tests/` - 集成测试 (使用 Mock 服务器)
- `tests/e2e/` - 端到端测试 (使用 pexpect 运行真实进程)

集成测试使用独立的 Socket.IO 命名空间路径，避免测试间相互干扰。

## CLI 入口

```bash
# 通过已安装的包
a2c-computer run --auto-connect true --auto-reconnect true

# 通过模块
python -m a2c_smcp.computer.cli.main run
```
