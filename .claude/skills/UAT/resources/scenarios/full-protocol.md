# 场景：full-protocol

## 测试目标

验证 Agent ↔ Server ↔ Computer 完整协议流程：连接、office 加入、tool_call 路由、
SKILL 通知广播、版本握手、断连守卫。

## 类型

完整链路（需要 Server + Computer + Agent 三进程）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用
3. tmux MCP 工具可用

## 环境准备

按 `resources/test-env-setup.md` 中"完整链路场景环境"的步骤，启动三个进程：

1. 创建 tmux session `a2c-uat`
2. 启动 Server（动态端口 → `/tmp/a2c-uat-port`）
3. 启动 Computer（连接 Server）
4. 启动 Agent（连接 Server）

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
  - Computer 输出包含连接成功提示
  - `join_office` 返回成功

### F-02: Agent 连接并发起 tool_call

- **优先级**: P0
- **前置**: F-01 成功，Computer 已加入 office 且有 MCP server
- **步骤**:
  1. Agent 连接 Server
  2. Agent 通过 Socket.IO 发起 `client:call_tool` 事件
  3. 等待 Computer 执行并返回结果
  4. 捕获 Agent / Computer / Server 三端 pane 输出
- **预期结果**:
  - Agent 收到正确的 tool_call 返回结果
  - Computer 日志显示 MCP server 执行了工具
  - Server 日志显示消息路由

### F-03: SKILL 通知广播（notify:update_skills）

- **优先级**: P0
- **前置**: F-01 成功，Agent 已连接
- **步骤**:
  1. Computer 更新 skills（通过 CLI 命令或 MCP server 上报）
  2. 观察 Agent 是否收到 `notify:update_skills` 通知
  3. 捕获三端 pane 输出
- **预期结果**:
  - Agent 收到 skills 更新通知
  - Server 日志显示广播事件

### F-04: 版本握手成功

- **优先级**: P0
- **步骤**:
  1. Agent 携带正确 `a2c_version` query 参数连接 Server
  2. 捕获 Server pane 输出
- **预期结果**:
  - 连接成功（非 4008 拒绝）
  - Server 日志显示版本匹配

### F-05: 版本不兼容拒绝（Socket.IO 4008）

- **优先级**: P0
- **步骤**:
  1. Agent 携带不兼容版本（如 `a2c_version=99.0.0`）尝试连接
  2. 捕获 Agent 和 Server pane 输出
- **预期结果**:
  - 连接被拒
  - Agent 收到 4008 错误码
  - Server 日志显示版本不兼容

### F-06: Computer 断连后重连

- **优先级**: P1
- **前置**: F-01 成功
- **步骤**:
  1. Computer 正常连接
  2. 模拟断连（kill Computer tmux window 后重新启动）
  3. 观察 Server 和 Agent 日志
  4. Computer 重新启动并连接
  5. 观察恢复行为
- **预期结果**:
  - 断连后 Server 日志显示 disconnect
  - 重连后 Computer 恢复正常
  - 飞行中的请求不静默丢失（返回断连错误）

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
