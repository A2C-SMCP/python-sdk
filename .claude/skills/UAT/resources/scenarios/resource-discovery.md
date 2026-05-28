# 场景：resource-discovery

## 测试目标

验证 `client:get_resources` 协议事件：MCP 资源透明转发、camelCase→snake_case 规整、
cursor 翻页、`window://` 资源发现，以及错误码 4014（MCP Server 未注册）/
4015（未声明 resources 能力）。

## 类型

完整链路（需要 Agent → Server → Computer 三进程）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用
3. tmux MCP 工具可用
4. Computer 挂载了带 resources 能力的 MCP server

## 环境准备

按 `resources/test-env-setup.md` 中"完整链路场景环境"的步骤启动三个进程。

### Seed 依赖

> **复用 seed**: `seeds/_helpers/resource-discovery` 提供 Agent 驱动脚本

Computer 需额外挂载两个 MCP server seed：

### MCP Server 配置

```bash
SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
```

在 Computer 的 MCP 配置中注册：

1. **window-resource-server**（有 resources 能力）：
   ```json
   {
     "window-resource-server": {
       "type": "stdio",
       "disabled": false,
       "forbidden_tools": [],
       "tool_meta": {},
       "server_parameters": {
         "command": "python",
         "args": ["$SEEDS_ROOT/mcp/server_with_window_resources.py"]
       }
     }
   }
   ```

2. **no-resources-server**（无 resources 能力）：
   ```json
   {
     "no-resources-server": {
       "type": "stdio",
       "disabled": false,
       "forbidden_tools": [],
       "tool_meta": {},
       "server_parameters": {
         "command": "python",
         "args": ["$SEEDS_ROOT/mcp/server_no_resources_capability.py"]
       }
     }
   }
   ```

### tmux 环境拓扑

```
tmux session: a2c-uat
├── window: server     →  Server 进程
├── window: computer   →  a2c-computer run --approve-all-mcp --auto-connect --auto-reconnect
└── window: agent      →  Agent 驱动脚本（seeds/_helpers/resource-discovery/agent_resource_driver.py）
```

### Agent 启动命令

Computer 加入 office 后：

```bash
cd /Users/liulonggang/PycharmProjects/python-sdk && \
uv run python .claude/skills/UAT/resources/seeds/_helpers/resource-discovery/agent_resource_driver.py \
  --port-file /tmp/a2c-uat-port \
  --office-id res-uat-office \
  --computer-name <computer_name> \
  2>&1 | tee /tmp/a2c-uat-logs/agent.log
```

## 环境变量

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$
```

## 测试用例

### R-01: get_resources 成功返回资源列表

- **优先级**: P0
- **前置**: 完整链路环境已搭建，Computer 连接成功并加入 office，
  `window-resource-server` MCP server 已挂载
- **步骤**:
  1. Agent 连接 Server
  2. Agent 通过 Socket.IO 发起 `client:get_resources` 事件，参数：
     ```json
     {"computer": "<computer_name>", "mcp_server": "window-resource-server", "req_id": "R-01"}
     ```
  3. 捕获 Agent pane 输出
- **预期结果**:
  - 响应 `req_id` 为 `"R-01"`
  - `resources` 列表包含 3 个元素
  - `resources[0]` 满足：
    - `uri` = `"window://main-editor"`
    - `name` = `"main-editor"`
    - `description` = `"Primary code editor window"`
    - `mime_type` = `"text/plain"`（camelCase→snake_case 规整）
  - `resources[1]` 满足：
    - `uri` = `"window://terminal"`
    - `mime_type` = `"text/plain"`
  - `resources[2]` 满足：
    - `uri` = `"config://app-settings"`
    - `mime_type` = `"application/json"`
  - 无 `next_cursor` 字段（或为 `null`/空，因资源总数少，无分页）

### R-02: get_resources window:// 资源含 annotations

- **优先级**: P0
- **前置**: R-01 成功
- **步骤**:
  1. 从 R-01 响应中提取 `window://main-editor` 资源对象
  2. 检查其 `annotations` 和 `_meta` 字段
  3. 提取 `window://terminal` 资源对象，检查 annotations
- **预期结果**:
  - `window://main-editor` 资源：
    - `annotations.priority` = `0.9`
    - `annotations.audience` = `["assistant"]`
    - `annotations.last_modified` 非空（ISO 8601 字符串，snake_case）
    - `_meta.fullscreen` = `true`
  - `window://terminal` 资源：
    - `annotations.priority` = `0.5`
    - `annotations.audience` = `["user", "assistant"]`
    - `_meta.fullscreen` = `false`
  - 字段名全部 snake_case（`mime_type` / `last_modified`，无 camelCase 残留）

### R-03: get_resources 指定不存在的 MCP Server → 4014

- **优先级**: P0
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_resources`，参数：
     ```json
     {"computer": "<computer_name>", "mcp_server": "nonexistent-server", "req_id": "R-03"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4014`（MCP_SERVER_NOT_FOUND）
  - `mcp_server_name` = `"nonexistent-server"`
  - `message` 非空

### R-04: get_resources 目标 server 无 resources 能力 → 4015

- **优先级**: P0
- **前置**: 完整链路环境已搭建，`no-resources-server` 已挂载
- **步骤**:
  1. Agent 发起 `client:get_resources`，参数：
     ```json
     {"computer": "<computer_name>", "mcp_server": "no-resources-server", "req_id": "R-04"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4015`（MCP_CAPABILITY_NOT_SUPPORTED）
  - `mcp_server_name` = `"no-resources-server"`
  - `capability` = `"resources"`（缺失的能力名称）
  - `message` 非空

### R-05: get_resources 指定不存在的 Computer → 路由失败

- **优先级**: P1
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_resources`，参数：
     ```json
     {"computer": "ghost-computer", "mcp_server": "window-resource-server", "req_id": "R-05"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为错误（Computer 不在 office 或不存在）
  - 非成功响应（ErrorPayload 或超时）

## 清理

1. Kill tmux session `a2c-uat`
2. 清理 `/tmp/a2c-uat-port`、`/tmp/a2c-uat-logs/`、`/tmp/a2c-uat-skill-home/`

## 日志收集

完整链路场景必须收集三端日志：

1. **Server pane**: `/tmp/a2c-uat-logs/server.log` + tmux capture-pane
2. **Computer pane**: `/tmp/a2c-uat-logs/computer.log` + tmux capture-pane
3. **Agent pane**: `/tmp/a2c-uat-logs/agent.log` + tmux capture-pane

每个用例执行后，对三个 pane 都执行 `capture-pane lines: 50`。
失败时增加到 `lines: 200` 并读取文件日志。
