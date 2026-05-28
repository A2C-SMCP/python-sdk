# 场景：full-protocol

## 测试目标

验证 Agent ↔ Server ↔ Computer 完整协议流程：连接、office 加入、tool_call 路由、
get_config / get_tools / get_desktop / list_room、SKILL 通知广播、版本握手、
断连守卫、tool_call_cancel、leave_office。

## 类型

完整链路（需要 Server + Computer + Agent 三进程）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用
3. tmux MCP 工具可用
4. Computer 至少挂载一个 MCP server（有工具 + 有资源）

## 环境准备

按 `resources/test-env-setup.md` 中"完整链路场景环境"的步骤，启动三个进程：

### Seed 依赖

> **复用 seed**: `seeds/_helpers/full-protocol` 提供 Agent 驱动脚本

1. 创建 tmux session `a2c-uat`
2. 启动 Server（动态端口 → `/tmp/a2c-uat-port`）
3. 启动 Computer（连接 Server，`--approve-all-mcp --auto-connect true --auto-reconnect true`）
4. Computer 加入 office
5. 启动 Agent 驱动脚本：

```bash
cd /Users/liulonggang/PycharmProjects/python-sdk && \
uv run python .claude/skills/UAT/resources/seeds/_helpers/full-protocol/agent_protocol_driver.py \
  --port-file /tmp/a2c-uat-port \
  --office-id proto-uat-office \
  --computer-name <computer_name> \
  2>&1 | tee /tmp/a2c-uat-logs/agent.log
```

所有进程输出通过 `tee` 双写到 `/tmp/a2c-uat-logs/`。

## 测试用例

### F-01: Computer 连接并加入 office

- **优先级**: P0
- **步骤**:
  1. 启动 Computer，连接 Server
  2. Computer CLI 中执行 `join_office test-office-001`
  3. 捕获 Computer pane 输出
  4. 捕获 Server pane 输出
- **预期结果**:
  - Computer 输出包含 `"Connected"` 或 `"connected"` 关键词
  - `join_office` 返回 `"office_id": "test-office-001"`
  - Server 日志包含 Computer sid 和 name

### F-02: Agent 连接并发起 tool_call

- **优先级**: P0
- **前置**: F-01 成功，Computer 已加入 office 且有 MCP server
- **步骤**:
  1. Agent 连接 Server
  2. Agent 通过 Socket.IO 发起 `client:call_tool` 事件，参数：
     ```json
     {"computer": "<computer_name>", "tool_name": "<known_tool>", "params": {}, "timeout": 30000, "req_id": "F-02"}
     ```
  3. 等待 Computer 执行并返回结果
  4. 捕获 Agent / Computer / Server 三端 pane 输出
- **预期结果**:
  - Agent 收到 ack 响应，`req_id` = `"F-02"`
  - 响应包含 `result` 字段（工具返回内容）
  - 响应**不**含 `code` 字段（非 ErrorPayload）
  - Computer 日志包含 MCP 工具调用记录
  - Server 日志包含 `"client:call_tool"` 路由记录

### F-03: SKILL 通知广播（notify:update_skills）

- **优先级**: P0
- **前置**: F-01 成功，Agent 已连接
- **步骤**:
  1. Agent 注册 `notify:update_skills` 事件监听
  2. Computer 通过 CLI 命令触发 skill 更新（如 marketplace add/remove 或 skill refresh）
  3. 等待 Agent 收到通知
  4. 捕获三端 pane 输出
- **预期结果**:
  - Agent 收到 `notify:update_skills` 事件
  - 通知 payload 包含 `computer` 字段（触发的 Computer name）
  - Server 日志包含 `"notify:update_skills"` 广播记录

### F-04: 版本握手成功

- **优先级**: P0
- **步骤**:
  1. Agent 携带正确 `a2c_version` query 参数连接 Server（如 `a2c_version=0.2.1`）
  2. 连接建立后，Agent 发起 `server:list_room`，参数：
     ```json
     {"office_id": "test-office-001", "req_id": "F-04"}
     ```
  3. 检查返回的 session info
- **预期结果**:
  - 连接成功（非 4008 拒绝）
  - `list_room` 响应 `sessions` 列表中包含 Computer 和 Agent
  - Agent session 的 `a2c_version` 字段非空

### F-05: 版本不兼容拒绝（Socket.IO 4008）

- **优先级**: P0
- **步骤**:
  1. Agent 携带不兼容版本（如 `a2c_version=99.0.0`）尝试连接
  2. 捕获 Agent 和 Server pane 输出
- **预期结果**:
  - 连接被拒（HTTP 400 或 Socket.IO connect_error）
  - Agent 收到 ErrorPayload：`code` = `4008`
  - ErrorPayload 包含 `server_version`、`client_version`、`min_supported`、`max_supported` 四字段
  - Server 日志包含版本不兼容记录

### F-06: Computer 断连后重连

- **优先级**: P1
- **前置**: F-01 成功
- **步骤**:
  1. Computer 正常连接
  2. Agent 发起一个 tool_call（长超时工具），在请求飞行中 kill Computer 进程
  3. 观察 Agent 侧响应
  4. 重新启动 Computer（`--auto-reconnect true`）
  5. Computer 重新连接后再次发起 tool_call
  6. 捕获全程 Agent / Server / Computer pane 输出
- **预期结果**:
  - 断连后 Server 日志包含 disconnect 记录
  - 飞行中 tool_call 返回断连错误（非静默挂死）
  - 重连后 Computer 输出包含 `"Connected"` 或 `"Reconnected"`
  - 重连后新 tool_call 正常返回结果

### F-07: get_config 获取 Computer MCP 配置

- **优先级**: P0
- **前置**: F-01 成功，Computer 有 MCP server 配置
- **步骤**:
  1. Agent 通过 Socket.IO 发起 `client:get_config`，参数：
     ```json
     {"computer": "<computer_name>", "req_id": "F-07"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应 `req_id` = `"F-07"`
  - 响应包含 `servers` 字段（dict），键包含已挂载的 MCP server name
  - 每个 server 包含 `type`（`"stdio"` / `"sse"` / `"streamable"`）、`disabled`、`tool_meta`
  - 响应**不**含 `code` 字段（非 ErrorPayload）

### F-08: get_tools 获取工具列表

- **优先级**: P0
- **前置**: F-01 成功，Computer MCP server 已连接并注册工具
- **步骤**:
  1. Agent 发起 `client:get_tools`，参数：
     ```json
     {"computer": "<computer_name>", "req_id": "F-08"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应 `req_id` = `"F-08"`
  - `tools` 列表非空，每个元素包含 `name`、`description`、`params_schema`
  - 工具名称格式为 `<server_name>/<tool_name>` 或 alias（如有配置）
  - 响应**不**含 `code` 字段

### F-09: get_desktop 获取桌面布局

- **优先级**: P0
- **前置**: F-01 成功，Computer MCP server 提供了 `window://` 资源
- **步骤**:
  1. Agent 发起 `client:get_desktop`，参数：
     ```json
     {"computer": "<computer_name>", "req_id": "F-09"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应 `req_id` = `"F-09"`
  - `desktops` 列表包含至少一个元素（window 资源内容）
  - 响应**不**含 `code` 字段

### F-10: list_room 查询房间成员

- **优先级**: P0
- **前置**: F-01 成功，Computer 已加入 office，Agent 已连接
- **步骤**:
  1. Agent 发起 `server:list_room`，参数：
     ```json
     {"office_id": "test-office-001", "req_id": "F-10"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应 `req_id` = `"F-10"`
  - `sessions` 列表长度 ≥ 2（至少 1 Computer + 1 Agent）
  - Computer session：`role` = `"computer"`，`name` 非空，`office_id` = `"test-office-001"`
  - Agent session：`role` = `"agent"`，`name` 非空
  - 所有 session 的 `sid` 非空且唯一

### F-11: leave_office 并收到通知

- **优先级**: P0
- **前置**: F-01 成功，Agent 已连接并注册 `notify:leave_office` 监听
- **步骤**:
  1. Agent 发起 `server:leave_office`，参数：
     ```json
     {"office_id": "test-office-001", "req_id": "F-11"}
     ```
  2. 捕获 Agent / Server pane 输出
  3. 验证 Computer 是否收到 `notify:leave_office`
- **预期结果**:
  - `leave_office` 返回成功
  - Computer 收到 `notify:leave_office` 通知
  - 通知 payload 包含 `agent` 字段（离开的 Agent name）和 `office_id` = `"test-office-001"`
  - 再次 `list_room` 该 office 时 sessions 列表不含该 Agent

### F-12: tool_call_cancel 取消工具调用

- **优先级**: P1
- **前置**: F-01 成功，Computer 有一个长时间执行的工具
- **步骤**:
  1. Agent 发起 `client:call_tool`（选择一个长超时工具）
  2. 在工具执行中，Agent 发起 `server:tool_call_cancel`，参数：
     ```json
     {"computer": "<computer_name>", "req_id": "F-12"}
     ```
  3. 捕获三端 pane 输出
- **预期结果**:
  - Computer 收到 `notify:tool_call_cancel` 通知
  - Computer 日志显示工具执行被中断
  - Agent 原始 tool_call 收到取消响应（非正常结果）

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
