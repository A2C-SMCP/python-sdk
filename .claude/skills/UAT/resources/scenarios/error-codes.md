# 场景：error-codes

## 测试目标

验证 A2C-SMCP 协议错误码 4016~4018 的负面路径：SKILL 名称格式非法、
SKILL 资源不可达（路径穿越 / forbidden / not_found / too_large）、
Blob 句柄无效（invalid_handle / forbidden / gone）。
同时覆盖 4014（SKILL name 合法但不存在，复用 MCP_SERVER_NOT_FOUND 语义）。
以及 Server 路由层错误：目标 Computer 不存在时，所有 `client:*` 事件应返回
flat ErrorPayload 而非抛出未捕获 ValueError（issue #92 回归守卫）。

## 类型

完整链路（需要 Agent → Server → Computer 三进程）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用
3. tmux MCP 工具可用
4. Computer 至少有一个已注册的 SKILL（通过 marketplace 或 user drop-in）

## 环境准备

按 `resources/test-env-setup.md` 中"完整链路场景环境"的步骤启动三个进程。

### Seed 依赖

> **复用 seed**: `seeds/_helpers/error-codes` 提供 Agent 驱动脚本 + SKILL_HOME 搭建工具

Computer 需至少有一个已注册 SKILL（含 `.skillenv` 文件，供 E-06 测试），
通过 `setup_skill_home.sh` 一键搭建：

```bash
SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
SKILL_HOME=/tmp/a2c-uat-skill-home-$$
bash "$SEEDS_ROOT/_helpers/error-codes/setup_skill_home.sh" "$SKILL_HOME" "$SEEDS_ROOT"
```

### tmux 环境拓扑

```
tmux session: a2c-uat
├── window: server     →  Server 进程
├── window: computer   →  A2C_SKILL_HOME=$SKILL_HOME a2c-computer run --approve-all-mcp --auto-connect --auto-reconnect
│                        （SKILL_HOME 下含 env-skill + valid-skill-pkg 两个 user SKILL）
└── window: agent      →  Agent 驱动脚本（seeds/_helpers/error-codes/agent_error_codes_driver.py）
```

### 启动顺序

1. 搭建 SKILL_HOME（见上方 Seed 依赖）
2. 创建 tmux session + 日志目录
3. 启动 Server → 启动 Computer（使用上述 SKILL_HOME）→ Computer 加入 office
4. 启动 Agent 驱动脚本：

```bash
cd /Users/liulonggang/PycharmProjects/python-sdk && \
uv run python .claude/skills/UAT/resources/seeds/_helpers/error-codes/agent_error_codes_driver.py \
  --port-file /tmp/a2c-uat-port \
  --office-id err-uat-office \
  --computer-name <computer_name> \
  --skill-with-env env-skill \
  2>&1 | tee /tmp/a2c-uat-logs/agent.log
```

## 环境变量

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$
```

## 测试用例

### E-01: SKILL name 格式非法 → 4016

- **优先级**: P0
- **前置**: 完整链路环境已搭建，Computer 连接成功
- **步骤**:
  1. Agent 通过 Socket.IO 发起 `client:get_skill`，参数：
     ```json
     {"computer": "<computer_name>", "name": "../etc/passwd", "req_id": "E-01"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4016`（SKILL_NAME_INVALID）
  - `details.name` = `"../etc/passwd"`
  - `message` 非空，含格式错误相关描述

### E-02: SKILL name 含非法字符 → 4016

- **优先级**: P0
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_skill`，参数：
     ```json
     {"computer": "<computer_name>", "name": "foo:bar:baz:qux", "req_id": "E-02"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4016`（SKILL_NAME_INVALID）
  - `details.name` = `"foo:bar:baz:qux"`

### E-03: SKILL name 合法但不存在 → 4014

- **优先级**: P0
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_skill`，参数：
     ```json
     {"computer": "<computer_name>", "name": "nonexistent-skill", "req_id": "E-03"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4014`（MCP_SERVER_NOT_FOUND，SKILL 复用语义）
  - `mcp_server_name` 字段存在
  - `message` 非空，含 SKILL 未找到相关描述

### E-04: SKILL rel_path 路径穿越 → 4017

- **优先级**: P0
- **前置**: 完整链路环境已搭建，Computer 有一个已注册 SKILL（如 `foo:valid-skill-pkg`）
- **步骤**:
  1. Agent 发起 `client:get_skill`，参数：
     ```json
     {"computer": "<computer_name>", "name": "foo:valid-skill-pkg", "rel_path": "../../etc/passwd", "req_id": "E-04"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4017`（SKILL_RESOURCE_NOT_ACCESSIBLE）
  - `details.reason` = `"traversal"`
  - `details.rel_path` = `"../../etc/passwd"`

### E-05: SKILL rel_path 绝对路径 → 4017

- **优先级**: P1
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_skill`，参数：
     ```json
     {"computer": "<computer_name>", "name": "foo:valid-skill-pkg", "rel_path": "/etc/shadow", "req_id": "E-05"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4017`
  - `details.reason` 为 `"traversal"` 或类似拒绝原因
  - `details.rel_path` = `"/etc/shadow"`

### E-06: SKILL rel_path 指向 .skillenv 文件 → 4017

- **优先级**: P0
- **前置**: 完整链路环境已搭建，SKILL 目录下存在 `.skillenv` 文件
  （如 SKILL_HOME 下有 `user/some-skill/.skillenv`）
- **步骤**:
  1. Agent 发起 `client:get_skill`，参数：
     ```json
     {"computer": "<computer_name>", "name": "some-skill", "rel_path": ".skillenv", "req_id": "E-06"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4017`
  - `details.reason` = `"forbidden"`
  - `details.rel_path` = `".skillenv"`

### E-07: SKILL rel_path 不存在的文件 → 4017

- **优先级**: P0
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_skill`，参数：
     ```json
     {"computer": "<computer_name>", "name": "foo:valid-skill-pkg", "rel_path": "nonexistent.md", "req_id": "E-07"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4017`
  - `details.reason` = `"not_found"`
  - `details.rel_path` = `"nonexistent.md"`

### E-08: Blob 句柄无效 → 4018

- **优先级**: P0
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_blob`，参数：
     ```json
     {"computer": "<computer_name>", "blob_handle": "a2c:invalid:totally-fake-handle", "req_id": "E-08"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4018`（BLOB_NOT_ACCESSIBLE）
  - `details.reason` = `"invalid_handle"` 或 `"gone"`

### E-09: Blob 句柄为空字符串 → 4018

- **优先级**: P1
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_blob`，参数：
     ```json
     {"computer": "<computer_name>", "blob_handle": "", "req_id": "E-09"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4018`
  - `details.reason` = `"invalid_handle"`

### E-10: Blob 句柄跨 Computer 复用 → 4018

- **优先级**: P1
- **前置**: E-08 之前的某个测试中 Computer 曾返回有效的 blob_handle
- **步骤**:
  1. 假设有另一个 Computer（或构造不匹配的 computer name）
  2. 使用一个合法格式但属于不同 Computer 的 blob_handle 发起请求：
     ```json
     {"computer": "other-computer", "blob_handle": "<handle_from_computer_a>", "req_id": "E-10"}
     ```
  3. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` = `4018`
  - `details.reason` = `"invalid_handle"` 或 `"forbidden"`

> **注意**: E-10 使用 `"other-computer"` 这个**不存在**的 computer name，
> issue #92 修复后，路由层应先拦截并返回 computer-not-found 错误。
> 若 Server 先做 Computer 存在性校验再检查 blob，则 `code` 为 computer-not-found 错误码；
> 若 Server 先转发到 Computer 再做 blob 校验，则 `code` 为 4018。
> 驱动脚本对 E-10 接受两种错误码。

### E-11: get_skill 目标 Computer 不存在 → ErrorPayload（#92 回归）

- **优先级**: P0
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_skill`，`computer` 指向不存在的名称：
     ```json
     {"computer": "ghost-computer-999", "name": "any-skill", "req_id": "E-11"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload（**不是**超时/异常）
  - `code` 为 computer-not-found 错误码（新增 `404` 或复用 `4014`，取决于修复合入）
  - `message` 非空，含 Computer 不存在相关描述
- **回归关联**: issue #92 — 原先 `raise ValueError` 导致 Agent 静默超时

### E-12: get_blob 目标 Computer 不存在 → ErrorPayload（#92 回归）

- **优先级**: P0
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_blob`，`computer` 指向不存在的名称：
     ```json
     {"computer": "ghost-computer-999", "blob_handle": "a2c:blob:some-handle", "req_id": "E-12"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` 为 computer-not-found 错误码（同 E-11）
  - `message` 非空

### E-13: get_resources 目标 Computer 不存在 → ErrorPayload（#92 回归）

- **优先级**: P1
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_resources`，`computer` 指向不存在的名称：
     ```json
     {"computer": "ghost-computer-999", "mcp_server_name": "some-server", "req_id": "E-13"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` 为 computer-not-found 错误码（同 E-11）
  - `message` 非空

### E-14: get_tools 目标 Computer 不存在 → ErrorPayload（#92 回归）

- **优先级**: P1
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_tools`，`computer` 指向不存在的名称：
     ```json
     {"computer": "ghost-computer-999", "req_id": "E-14"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` 为 computer-not-found 错误码（同 E-11）
  - `message` 非空

### E-15: get_skills 目标 Computer 不存在 → ErrorPayload（#92 回归）

- **优先级**: P1
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:get_skills`，`computer` 指向不存在的名称：
     ```json
     {"computer": "ghost-computer-999", "req_id": "E-15"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` 为 computer-not-found 错误码（同 E-11）
  - `message` 非空

### E-16: tool_call 目标 Computer 不存在 → ErrorPayload（#92 回归）

- **优先级**: P0
- **前置**: 完整链路环境已搭建
- **步骤**:
  1. Agent 发起 `client:tool_call`，`computer` 指向不存在的名称：
     ```json
     {"computer": "ghost-computer-999", "tool_name": "some_tool", "arguments": {}, "req_id": "E-16"}
     ```
  2. 捕获 Agent pane 输出
- **预期结果**:
  - 响应为 flat ErrorPayload
  - `code` 为 computer-not-found 错误码（同 E-11）
  - `message` 非空

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
