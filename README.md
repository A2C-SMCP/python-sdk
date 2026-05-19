# python-sdk
The official Python Client SDK for A2C-SMCP
 
---

# A2C-SMCP Python SDK 使用指南

本仓库提供 A2C-SMCP 在 Python 生态下的完整实现与配套工具，覆盖三大模块：`Computer`、`Server`、`Agent`。三者通过 Socket.IO 实现实时通信，协议细节请参考独立的协议仓库；本文着重介绍如何在工程中使用本 SDK。

本文档与以下文档保持一致，请结合阅读：

- 体系架构：`docs/index.md` 与工作流 `/arch`
- CLI 使用：`docs/cli.md` 与工作流 `/cli`
- 桌面渲染：工作流 `/desktop`
- 测试结构与 e2e：工作流 `/test-struct`


## 安装

- 从 PyPI 安装（稳定版）：
```bash
pip install a2c-smcp
```

- 从 PyPI 安装预发行版（当前处于 rc 阶段，建议开启预发行）：
```bash
# pip 方式（一次性）
pip install --pre a2c-smcp

# 或为当前环境开启预发行解析（可选）
pip config set global.prefer-binary true   # 可选

# Poetry 方式（推荐管理依赖）
poetry add a2c-smcp --allow-prereleases

# 或在 pyproject.toml 中显式声明
# [tool.poetry.dependencies]
# a2c-smcp = { version = "^0.0.0", allow_prereleases = true }
# 注：可将版本号替换为你期望的上界/下界约束，如 "^0.1.0" 或 "0.1.0rc*"
```


## 协议兼容性 (SDK ↔ A2C-SMCP Protocol)

本 SDK 实现 [A2C-SMCP 协议](https://github.com/A2C-SMCP/a2c-smcp-protocol)。SDK 内置的协议版本通过常量
`a2c_smcp.PROTOCOL_VERSION` 暴露，并在 Socket.IO 连接握手阶段以 URL query `a2c_version` 与 Server 协商。

```python
from a2c_smcp import PROTOCOL_VERSION  # 当前: "0.2.0"
```

**兼容矩阵 / Compatibility matrix**

| SDK 版本 | `a2c_smcp.PROTOCOL_VERSION` | 兼容的协议 Server 版本 | 说明 |
|---|---|---|---|
| `0.2.x` | `0.2.0` | `0.2.x` | v0.2.0 GA：URI 纯标识符化、`client:get_resources`、连接版本握手；DPE 已移出至 [dpe-protocol](https://github.com/A2C-SMCP/dpe-protocol) |
| `0.1.x`（pre-GA rc） | 无握手常量 | — | 早期 rc，无版本握手协商；请升级到 `0.2.x` |

**兼容性规则（协议 MUST）/ Compatibility rule (protocol MUST)**

- v0.x 阶段 **MUST** 严格匹配 `MAJOR.MINOR`；`PATCH` 任意。即客户端 `X.Y.Z` 仅与
  Server 支持区间 `[X.Y.0, X.Y.999]` 兼容（见协议 `versioning.md`）。
- 客户端 **MUST** 在连接 URL query 携带 `a2c_version`；缺失/非法 → Server 返回 HTTP 400。
- 版本不兼容时 Server **MUST** 返回 HTTP 400 + Socket.IO `4008`；不兼容客户端**连接被拒、绝不建立**
  （`versioning.md` §4 死循环防御：客户端不得对 4008 无限重连）。
- 客户端 **MUST** polling-first 握手（显式 `transports=["websocket"]` 会被 SDK 护栏纠正），
  以确保 `4008` 拒因可被还原归一为 `ProtocolVersionError`。
- Server **MUST** 将协商到的 `a2c_version` 写入会话信息，经 `server:list_room` 带出（仅展示/诊断，不二次校验）。

不兼容握手会抛出 `a2c_smcp.exceptions.ProtocolVersionError`（或底层连接异常，二者均满足"连接绝不建立"
的确定性保证）。升级指南参见协议仓库 `docs/migrations/v0.2-uri-metadata-refactor.md`。


## 快速开始

- 选择你的角色：
  - 需要在本机/容器内管理 MCP Servers → 使用 `Computer`
  - 需要提供中心信令服务（转发/广播通知） → 使用 `Server`
  - 需要作为业务侧“智能体”调用工具 → 使用 `Agent`

下文分别给出最小可用示例与进阶说明。


## 模块一：Computer（管理 MCP Server 与工具）

`Computer` 负责 MCP 工具的生命周期与调度管理，并通过 CLI 提供运行与配置能力。详见 `docs/cli.md`。

1) 启动交互式 CLI
```bash
python -m a2c_smcp.computer.cli.main run \
  --auto-connect true \
  --auto-reconnect true
# 如已配置 console_scripts，也可：a2c-computer run
```

2) 在 CLI 内常用命令（提示符 a2c>）：

- `inputs load @./inputs.json` 加载占位符输入定义
- `server add @./server_stdio.json` 添加/更新一个 MCP Server（stdio 示例）
- `start all` 启动全部 MCP Server
- `status` 查看运行状态；`tools` 列出可用工具
- 若需要连接信令服务：
  - `socket connect http://localhost:7000`
  - `socket join <office_id> "My Computer"`
- 配置变更通知：`notify update`

配置与命令的完整列表与注意事项请参见 `docs/cli.md`（已与测试用例保持一致）。


## 模块二：Server（中心信令服务器）

`Server` 提供 SMCP 协议的服务端实现，负责：

- 维护 Computer/Agent 元数据
- 转发信令与消息
- 将消息转换为 Notification 广播

支持异步与同步两种命名空间实现，建议在 FastAPI 等异步框架中使用异步版本。核心文档：`docs/server.md`。

最小异步集成示例（FastAPI + python-socketio）
```python
from fastapi import FastAPI
import socketio
from a2c_smcp.server import SMCPNamespace, DefaultAuthenticationProvider

app = FastAPI()

auth = DefaultAuthenticationProvider(
    admin_secret="your_admin_secret",
    api_key_name="x-api-key",
)
smcp_ns = SMCPNamespace(auth)

sio = socketio.AsyncServer(cors_allowed_origins="*")
sio.register_namespace(smcp_ns)

socket_app = socketio.ASGIApp(sio, app)
```

同步版本、会话查询与自定义认证示例请参考 `docs/server.md`。


## 模块三：Agent（业务侧智能体客户端）

`Agent` 提供同步与异步两种客户端，内置认证抽象与事件回调协议。核心文档：`docs/agent.md`。

同步最小示例
```python
from a2c_smcp.agent import DefaultAgentAuthProvider, SMCPAgentClient

auth = DefaultAgentAuthProvider(
    agent_id="my_agent",
    office_id="my_office",
    api_key="your_api_key",
)

client = SMCPAgentClient(auth_provider=auth)
client.connect_to_server("http://localhost:8000")

result = client.emit_tool_call(
    computer="target_computer",
    tool_name="file_read",
    params={"path": "/tmp/readme.txt"},
    timeout=30,
)
print(result)
```

异步最小示例
```python
import asyncio
from a2c_smcp.agent import DefaultAgentAuthProvider, AsyncSMCPAgentClient

async def main():
    auth = DefaultAgentAuthProvider(
        agent_id="my_agent",
        office_id="my_office",
        api_key="your_api_key",
    )

    client = AsyncSMCPAgentClient(auth_provider=auth)
    await client.connect_to_server("http://localhost:8000")

    ret = await client.emit_tool_call(
        computer="target_computer",
        tool_name="file_read",
        params={"path": "/tmp/readme.txt"},
        timeout=30,
    )
    print(ret)

asyncio.run(main())
```

事件回调（同步/异步）、拉取工具列表与错误处理等完整示例请参考 `docs/agent.md`。


## 桌面模式与窗口资源（Desktop）

`Computer` 可将 MCP Servers 暴露的 `window://` 资源整合为 Desktop 视图。设计细节与窗口选择/优先级规则见工作流 `/desktop`：

- 资源 URI 协议：参见 `a2c_smcp/utils/window_uri.py`（v0.2 起 `window://` 为**纯标识符**，不再解析 query）
- 元数据来源（v0.2）：`priority` / `audience` 取自 MCP `Resource.annotations`，`fullscreen` 等 A2C 扩展取自 `_meta`
- 与 `MCPServerManager` 的聚合策略：仅聚合 `window://`（非 window 资源不进桌面），按最近操作历史与 `priority` 组织
- `fullscreen` 规则：遇到全屏窗口，当前 Server 只渲染此窗口

集成测试中已包含示例 Stdio MCP 服务器，可参考：

- `tests/integration_tests/computer/mcp_servers/resources_stdio_server.py`
- `tests/integration_tests/computer/mcp_servers/resources_subscribe_stdio_server.py`


## VRL 转换（可选进阶）

已集成基于 `vrl-python` 的工具返回值转换能力：

- 在 `a2c_smcp/smcp.py` 与 `a2c_smcp/computer/mcp_clients/model.py` 中新增 `vrl` 脚本配置与校验
- 在 `MCPServerManager` 调用工具时自动尝试执行 VRL 转换，结果以 `A2C_VRL_TRANSFORMED` 存入 `CallToolResult.meta`
- 语法错误在配置阶段即抛出；运行期转换失败优雅降级（记录警告，不影响原始返回）

该能力在单元测试中已覆盖，可按需启用用于归一化不同 MCP 工具的返回结构。


## 结合测试的实践用法

本仓库测试结构见工作流 `/test-struct`：

- 单元测试：`tests/unit_tests/`
- 集成测试：`tests/integration_tests/`
- 端到端（e2e）：`tests/e2e/`

常用命令（需 Poetry 环境）：
```bash
# 运行全部测试
poetry run poe test

# 排除 e2e 的覆盖率
poetry run poe test-cov

# 仅运行 e2e
poetry run poe test-e2e

# Lint & Format
poetry run poe lint
poetry run poe format
```

注意：历史上因 Socket.IO 命名空间冲突导致“测试顺序影响结果”的问题，现已通过为集成测试使用独立命名空间路径修复（详见集成测试中的 mock server 配置）。


## 典型场景示例

- 让 Playwright MCP 提供浏览器自动化：
  - 在 CLI 中添加 stdio 配置：
    ```bash
    server add {"name":"playwright-mcp","type":"stdio","disabled":false,"forbidden_tools":[],"tool_meta":{},"server_parameters":{"command":"npx","args":["@playwright/mcp@latest","--port","8931"],"env":null,"cwd":null,"encoding":"utf-8","encoding_error_handler":"strict"}}
    start playwright-mcp
    tools
    ```
  - 在 Agent 中直接调用相应工具（如 `open_page`, `click`）

- 通过 Desktop 将多个 MCP 的窗口整合展示：
  - 启动多个支持 `window://` 资源的 MCP Server
  - 使用 `Computer` 聚合并渲染，高优先级/最近使用优先显示

- 多租户/测试隔离：
  - 在 `Agent` 的 `connect_to_server()` 传入自定义命名空间，实现环境隔离


## 项目结构速览

- `a2c_smcp/computer/` 计算机端（CLI、MCP 客户端与管理器、Desktop 聚合）
- `a2c_smcp/server/`   中央信令服务（命名空间、认证、类型与工具函数）
- `a2c_smcp/agent/`    业务侧客户端（同步/异步、认证与事件回调）
- `docs/`              模块使用文档与指南
- `tests/`             单元/集成/e2e 测试


## 参考与链接

- CLI 详细说明：`docs/cli.md`
- Agent 使用文档：`docs/agent.md`
- Server 使用文档：`docs/server.md`
- 协议与总体架构：`docs/index.md`（较长，按需阅读）


---

若你在集成或运行中遇到问题，欢迎通过 issue 反馈。建议附带：使用场景、最小复现配置（如 CLI 的 `server add` JSON）、日志片段与测试用例片段，便于快速定位。
