# CLI 使用指南

A2C Computer CLI 提供交互式命令行界面，用于 Computer 端的运行、配置管理、工具查询与 Socket.IO 连接。

## 安装与启动

### 安装

```bash
# 安装 CLI 依赖
pip install "a2c-smcp[cli]"
```

### 启动

```bash
# 通过模块启动
python -m a2c_smcp.computer.cli.main run

# 通过命令启动（如果配置了 console_scripts）
a2c-computer run

# 常用参数
a2c-computer run --auto-connect true --auto-reconnect true
```

启动后进入交互模式（提示符 `a2c>`），输入 `help` 查看可用命令。

## 命令总览

### 状态与查询

| 命令 | 说明 |
|------|------|
| `status` | 查看各 MCP Server 状态 |
| `tools` | 列出所有可用工具 |
| `mcp` | 打印当前 MCP 配置 |
| `history [n]` | 查看工具调用历史（最多 10 条） |
| `desktop [size] [uri]` | 获取桌面信息 |

### Server 管理

| 命令 | 说明 |
|------|------|
| `server add <json\|@file>` | 添加/更新 MCP Server 配置 |
| `server rm <name\|bundle_id>` | 移除 MCP Server 配置 |
| `start <name\|bundle_id>\|all` | 启动 MCP Server |
| `stop <name\|bundle_id>\|all` | 停止 MCP Server |

#### 寻址：`<name>` 还是 `<bundle_id>`？

`name` 是**给人看的显示名**（允许重名），`bundle_id` 是 **server 的唯一身份**。上述三个命令两者都收：

1. 按 `name` 唯一命中 → 直接执行；
2. 没有 server 叫这个名，但它是个已注册的 `bundle_id` → 按 `bundle_id` 执行；
3. **有多个 server 同名**（合法共存）→ 列出候选并要求改用 `bundle_id` 重试，**不会**替你猜一个：

```
a2c> stop filesystem
⚠ 有 2 个 server 叫 'filesystem' / 2 servers named 'filesystem':
   bundle_a3f9c2e1  filesystem  (plugin:fs-tools)
   filesystem       filesystem  (user)
请用 bundle_id 重试 / Retry with a bundle_id
```

4. 都不匹配 → 报「未找到」。**绝不会**出现「打印已停止/已移除，实际什么都没做」的假回执。

各 server 的 `bundle_id` 用 `status` 命令查看。

### Inputs 管理

| 命令 | 说明 |
|------|------|
| `inputs load @file` | 从文件加载 inputs 定义 |
| `inputs add <json\|@file>` | 添加/更新 input 定义 |
| `inputs rm <id>` | 删除 input 定义 |
| `inputs get <id>` | 查看 input 定义 |
| `inputs list` | 列出所有 inputs 定义 |
| `inputs value list` | 列出已解析的值缓存 |
| `inputs value get <id>` | 查看指定值缓存 |
| `inputs value set <id> [value]` | 设置值缓存 |
| `inputs value rm <id>` | 删除值缓存 |
| `inputs value clear [id]` | 清空值缓存 |

### Socket.IO 连接

| 命令 | 说明 |
|------|------|
| `socket connect <url>` | 连接信令服务器 |
| `socket join <office_id> <name>` | 加入房间 |
| `socket leave` | 离开当前房间 |
| `notify update` | 通知配置更新 |

### 调试工具

| 命令 | 说明 |
|------|------|
| `tc <json\|@file>` | 测试工具调用 |
| `render <json\|@file>` | 测试占位符渲染 |

### 其他

| 命令 | 说明 |
|------|------|
| `help` | 显示帮助 |
| `quit` / `exit` | 退出 CLI |

## 配置格式

### Server 配置（stdio 示例）

```json
{
  "name": "my-mcp",
  "type": "stdio",
  "disabled": false,
  "forbidden_tools": [],
  "tool_meta": {
    "dangerous_tool": {"auto_apply": false}
  },
  "server_parameters": {
    "command": "my_mcp_server",
    "args": ["--flag"],
    "env": {"KEY": "${input:API_KEY}"},
    "cwd": null,
    "encoding": "utf-8",
    "encoding_error_handler": "strict"
  }
}
```

### Inputs 配置

```json
[
  {
    "id": "API_KEY",
    "type": "promptString",
    "description": "API Key",
    "default": "",
    "password": true
  },
  {
    "id": "REGION",
    "type": "pickString",
    "description": "Select region",
    "options": ["us-east-1", "eu-west-1"],
    "default": "us-east-1"
  }
]
```

## 常见操作

### 1. 完整启动流程

```bash
# 加载 inputs 定义
a2c> inputs load @./inputs.json

# 添加 Server 配置
a2c> server add @./server.json

# 启动服务
a2c> start all

# 查看状态和工具
a2c> status
a2c> tools
```

### 2. 连接信令服务器

```bash
# 连接
a2c> socket connect http://localhost:8000

# 加入房间
a2c> socket join my-office "My Computer"

# 通知配置更新
a2c> notify update
```

### 3. 测试工具调用

```bash
# 直接调用
a2c> tc {"computer":"local","agent":"debug","req_id":"1","tool_name":"echo","params":{"text":"hello"},"timeout":30}

# 从文件加载
a2c> tc @./tool_call.json

# 查看历史
a2c> history
```

### 4. Playwright MCP 示例

```bash
# 添加 Playwright MCP
a2c> server add {"name":"playwright","type":"stdio","disabled":false,"forbidden_tools":[],"tool_meta":{},"server_parameters":{"command":"npx","args":["@playwright/mcp@latest"],"env":null,"cwd":null,"encoding":"utf-8","encoding_error_handler":"strict"}}

# 启动
a2c> start playwright

# 查看工具
a2c> tools
```

## Plugin / Marketplace 配置校验（非交互 CLI）

面向 marketplace plugin 作者与维护者：在发布或安装前发现不合法配置，本地开发与 CI 均可自动检查。校验
**零副作用**——不 clone、不安装 plugin、不修改用户配置或物化状态。

```bash
# <path> 可以是 marketplace 仓库根目录，也可以是单个 plugin 目录（自动识别）
a2c-computer plugin validate <path>
a2c-computer marketplace validate <path>   # 双入口别名，同一实现

# CI 中消费机器可读输出
a2c-computer plugin validate <path> --json
```

**校验范围**（复用安装路径同一套解析规则，不另立规则）：

- `.tfrobot-plugin/marketplace.json`：JSON 语法、必填字段（`name` / `owner` / `plugins`）、字段类型、
  kebab 命名规则（§3）；
- 每个 plugin 条目（§4）：必填 `name` / `source`、source 5 类形态（§5）、`strict` 冲突（§4.4）、
  引用路径存在性；**畸形条目（install 时被静默跳过的）会显式报出**；
- `.tfrobot-plugin/plugin.json`（§6）：语法 / `name` 必填 / 命名；损坏的 plugin.json（install 时仅记
  WARNING 后按空兜底）会报 error；缺失则记 warning（合法但建议保留）；
- `mcp-servers/*.json`（§8）：MCP Server 配置校验 + 文件名（去 `.json`）必须等于配置内 `name`；
  `inputs.json` 做语法检查；
- `skills/` SKILL 深检：`SKILL.md` 存在性、frontmatter `description`、skill 目录名 kebab 规则、
  跨容器重名；marketplace 条目 / plugin.json 的 `skills` 覆写路径存在性与越界检查。

**远程 source 跳过**：条目引用 git 远程仓库（`url` / `github` / `cnb` / `git-subdir`）时仅校验 source
字段本身格式，plugin 本体记 warning 跳过深检（本地零网络校验）。

**退出码**：`0` = 无错误（warnings 不影响）；`1` = 存在任一错误。每条诊断含**文件路径、配置位置
（如 `plugins[2].source`）与可理解原因**。

成功示例：

```console
$ a2c-computer plugin validate ./my-marketplace
Validating marketplace at /abs/my-marketplace
✓ valid — 4 file(s) checked, 0 warning(s)
```

失败示例：

```console
$ a2c-computer plugin validate ./my-marketplace
Validating marketplace at /abs/my-marketplace
✗ .tfrobot-plugin/marketplace.json:plugins[1].source [path-missing] local plugin directory not found: 'plugins/auth-tools' (...)
✗ plugins/data-toolkit/skills/Bad_Name [invalid-skill-name] skill dir name 'Bad_Name' is not strict kebab-case (...)
✗ invalid — 2 error(s), 0 warning(s)
$ echo $?
1
```

`--json` 输出结构（CI 消费契约面）：

```json
{
  "valid": false,
  "mode": "marketplace",
  "path": "/abs/my-marketplace",
  "checked_files": [".tfrobot-plugin/marketplace.json"],
  "errors":  [{"code": "path-missing", "file": ".tfrobot-plugin/marketplace.json", "location": "plugins[1].source", "message": "..."}],
  "warnings": [{"code": "remote-source-skipped", "file": ".tfrobot-plugin/marketplace.json", "location": "plugins[0].source", "message": "..."}]
}
```

## 注意事项

1. **Server 名称唯一性**: 相同工具名会冲突，使用 `tool_meta.alias` 设置别名
2. **占位符渲染**: 确保 `${input:id}` 对应的 inputs 已定义
3. **Socket.IO 会话**: `notify update` 需要先 `connect` 和 `join`
4. **长 JSON**: 建议使用 `@file.json` 方式加载

## 故障排查

| 问题 | 解决方案 |
|------|---------|
| 看不到工具 | 确认已 `start all`，检查进程是否启动 |
| 工具名冲突 | 配置 `tool_meta.alias` |
| 占位符未替换 | 确认已 `inputs load` 且 id 正确 |
| 无法通知远端 | 确认已 `connect` 和 `join` |

## 参考

- [Computer 使用指南](computer-guide.md)
- [MCP 配置参考](../reference/mcp-config.md)
- [Inputs 配置参考](../reference/inputs-config.md)
- 代码位置: `a2c_smcp/computer/cli/`
