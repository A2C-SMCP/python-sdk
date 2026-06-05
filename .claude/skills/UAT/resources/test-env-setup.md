# 测试环境搭建指南

## 前置准备

```bash
# 确保依赖已安装
uv sync --all-groups

# 创建日志目录
mkdir -p /tmp/a2c-uat-logs

# 确认 a2c-computer 可用
uv run a2c-computer --help
```

## CLI-only 场景环境

只需一个 tmux session，直接执行子命令：

### tmux 操作序列

1. **创建 session**

```
mcp__tmux__create-session  name: a2c-uat
```

2. **获取 pane ID**

```
mcp__tmux__list-windows  sessionId: <session-id>
mcp__tmux__list-panes    windowId: <window-id>
```

3. **执行命令**

```
mcp__tmux__execute-command  paneId: <pane-id>  command: cd /Users/liulonggang/PycharmProjects/python-sdk && uv run a2c-computer marketplace list --json
```

4. **捕获输出**

```
mcp__tmux__capture-pane  paneId: <pane-id>  lines: 50
```

5. **清理**

```
mcp__tmux__kill-session  sessionId: <session-id>
```

## 完整链路场景环境

需要三个 window，按顺序启动 Server → Computer → Computer 加入 Office → Agent。

### 步骤 1：创建 session + 日志目录

```
mcp__tmux__create-session  name: a2c-uat
```

```bash
mkdir -p /tmp/a2c-uat-logs
```

### 步骤 2：启动 Server

在 server window 中执行：

```bash
cd /Users/liulonggang/PycharmProjects/python-sdk && uv run python -c "
import socket, sys, time
from tests.e2e.conftest import _run_server_process
from multiprocessing import Event

# 获取空闲端口
s = socket.socket(); s.bind(('127.0.0.1', 0))
port = s.getsockname()[1]; s.close()

# 写入端口文件供其他进程读取
with open('/tmp/a2c-uat-port', 'w') as f:
    f.write(str(port))

print(f'SERVER_PORT={port}', flush=True)
e = Event()
_run_server_process(port, e)
e.wait()
" 2>&1 | tee /tmp/a2c-uat-logs/server.log
```

等待 `capture-pane` 输出中出现 `SERVER_PORT=` 后，读取端口号。

### 步骤 3：启动 Computer

创建新 window，执行：

```bash
cd /Users/liulonggang/PycharmProjects/python-sdk && PORT=$(cat /tmp/a2c-uat-port) && A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$ uv run a2c-computer run --url http://127.0.0.1:$PORT --approve-all-mcp --auto-connect --auto-reconnect 2>&1 | tee /tmp/a2c-uat-logs/computer.log
```

等待 `capture-pane` 输出中出现 `a2c>` 提示符（连接成功并进入交互模式）。

> **注意**: `a2c-computer run` 不支持 `--no-color` 参数（会报错退出）。如需控制颜色输出，
> 请在支持 ANSI 的终端中运行，或在 PyCharm 中启用 "Emulate terminal"。

### 步骤 4：Computer 加入 Office（Socket.IO Room）

Computer 必须加入 Office 后，Agent 才能通过 `client:*` 事件路由请求到 Computer。
在 Computer pane 中通过交互式 CLI 命令加入（使用 `rawMode=true`）：

```
socket join <office_id> <computer_name>
```

其中：
- `<office_id>`: 房间 ID，Agent 也必须加入同一个房间
- `<computer_name>`: Computer 在房间中的名称，Agent 通过此名称路由请求

等待输出中出现 `已加入房间 / Joined office`。

> **关键**: 未加入 Office 的 Computer 无法响应 Agent 的 `client:get_skill` / `client:call_tool`
> 等请求（Server 端 `_relay_client_call` 要求 Agent 与 Computer 在同一 office 内）。

### 步骤 5：启动 Agent

创建新 window，执行 Agent 测试脚本。Agent 必须连接后加入**同一个 Office**：

```bash
cd /Users/liulonggang/PycharmProjects/python-sdk && uv run python <agent-test-script> 2>&1 | tee /tmp/a2c-uat-logs/agent.log
```

Agent 脚本中的关键流程：

1. 连接 Server（`transports=['polling']`，polling-first）
2. 调用 `server:join_office`（`role="agent"`, `name=...`, `office_id=...`）
3. 调用 `server:list_room` 获取 Computer 名称
4. 使用 Computer **名称**（非 SID）作为 `client:*` 请求的 `computer` 字段

### 步骤 6：测试执行

按场景用例，通过 `execute-command` 在对应 window 的 pane 中发送命令，通过
`capture-pane` 获取输出。

### 步骤 7：日志收集

测试完成后或失败时，收集所有 pane 日志：

```
# 对每个 pane 执行
mcp__tmux__capture-pane  paneId: <server-pane>   lines: 200
mcp__tmux__capture-pane  paneId: <computer-pane>  lines: 200
mcp__tmux__capture-pane  paneId: <agent-pane>     lines: 200
```

同时读取文件日志作为补充：

```bash
cat /tmp/a2c-uat-logs/server.log
cat /tmp/a2c-uat-logs/computer.log
cat /tmp/a2c-uat-logs/agent.log
```

### 步骤 8：清理

```
mcp__tmux__kill-session  sessionId: <session-id>
```

```bash
rm -f /tmp/a2c-uat-port /tmp/a2c-uat-logs/*.log
```

## 关键注意事项

1. **端口动态分配**：每次测试分配随机端口，通过 `/tmp/a2c-uat-port` 文件传递
2. **进程启动顺序**：必须 Server 就绪后才能启动 Computer，Computer 加入 Office 后才能启动 Agent
3. **Office 加入是必须步骤**：Computer 必须通过 `socket join` 加入房间，否则 Agent 的 `client:*` 请求会超时
4. **Computer 名称 vs SID**：Agent 的 `client:*` 请求中 `computer` 字段使用的是 Computer 的**名称**（`socket join` 时设置的），不是 Socket.IO SID
5. **日志双写**：所有进程输出通过 `tee` 同时写入文件和 tmux pane
6. **等待策略**：使用 `capture-pane` 轮询检查关键字符串（如 `SERVER_PORT=`、`a2c>`、`Joined office`），
   每次间隔 1-2 秒，最多等待 15 秒
7. **环境隔离**：SKILL_HOME 使用临时目录，避免污染用户真实配置
