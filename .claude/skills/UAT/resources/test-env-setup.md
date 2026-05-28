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

需要三个 window，按顺序启动 Server → Computer → Agent。

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
cd /Users/liulonggang/PycharmProjects/python-sdk && PORT=$(cat /tmp/a2c-uat-port) && uv run a2c-computer run --url http://127.0.0.1:$PORT --approve-all-mcp --no-color 2>&1 | tee /tmp/a2c-uat-logs/computer.log
```

等待 `capture-pane` 输出中出现连接成功提示。

### 步骤 4：启动 Agent

创建新 window，执行 Agent 测试脚本。Agent 使用同步客户端：

```bash
cd /Users/liulonggang/PycharmProjects/python-sdk && uv run python -c "
import socketio
from a2c_smcp.smcp import SMCP_NAMESPACE

PORT = open('/tmp/a2c-uat-port').read().strip()
url = f'http://127.0.0.1:{PORT}'
print(f'Agent connecting to {url}', flush=True)

client = socketio.Client()
client.connect(url, socketio_path='/socket.io', namespaces=[SMCP_NAMESPACE], transports=['polling'], wait=True, wait_timeout=10)
print('Agent connected', flush=True)
# ... 测试交互 ...
" 2>&1 | tee /tmp/a2c-uat-logs/agent.log
```

### 步骤 5：测试执行

按场景用例，通过 `execute-command` 在对应 window 的 pane 中发送命令，通过
`capture-pane` 获取输出。

### 步骤 6：日志收集

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

### 步骤 7：清理

```
mcp__tmux__kill-session  sessionId: <session-id>
```

```bash
rm -f /tmp/a2c-uat-port /tmp/a2c-uat-logs/*.log
```

## 关键注意事项

1. **端口动态分配**：每次测试分配随机端口，通过 `/tmp/a2c-uat-port` 文件传递
2. **进程启动顺序**：必须 Server 就绪后才能启动 Computer 和 Agent
3. **日志双写**：所有进程输出通过 `tee` 同时写入文件和 tmux pane
4. **等待策略**：使用 `capture-pane` 轮询检查关键字符串（如 `SERVER_PORT=`、`connected`），
   每次间隔 1-2 秒，最多等待 15 秒
5. **环境隔离**：SKILL_HOME 使用临时目录，避免污染用户真实配置
