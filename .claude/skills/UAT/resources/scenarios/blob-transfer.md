# 场景：blob-transfer

## 测试目标

验证 SKILL 资源的 inline/blob 阈值切换、SHA256 端到端一致性、三级渐进披露以及
tool_call 二进制数据的 sideband blob 传输。

## 类型

完整链路（需要 Agent → Server → Computer 三进程）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用
3. tmux MCP 工具可用
4. 测试用 skill 资源文件已准备（小文件 <32KB、大文件 >32KB）

## 环境准备

按 `resources/test-env-setup.md` 中"完整链路场景环境"的步骤启动三个进程，
并在启动 Computer 前准备测试资源文件：

### 资源文件准备

> **复用 seed**: `seeds/_helpers/blob-resources` 提供测试资源生成 + Agent 驱动脚本

```bash
SKILL_HOME=/tmp/a2c-uat-skill-home-$$
mkdir -p $SKILL_HOME/user/blob-test

# SKILL.md（小文件，< inline_budget）
cat > $SKILL_HOME/user/blob-test/SKILL.md << 'EOF'
---
name: blob-test
description: Skill for blob transfer UAT
---
# Blob Test
This skill tests inline and blob transfer.
EOF

# 使用 seed 生成测试资源文件 + 获取 SHA256
SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
bash "$SEEDS_ROOT/_helpers/blob-resources/generate.sh" "$SKILL_HOME/user/blob-test"
```

### tmux 环境拓扑

```
tmux session: a2c-uat
├── window: server     →  Server 进程（端口 → /tmp/a2c-uat-port）
├── window: computer   →  A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$ a2c-computer run --url http://127.0.0.1:<PORT> --approve-all-mcp --auto-connect --auto-reconnect
└── window: agent      →  Agent Python 测试脚本
```

> **inline_budget 默认值**: 32 KiB（32768 字节）。可通过 `A2C_SKILL_INLINE_BUDGET` 环境变量
> 调整阈值，但本场景使用默认值。

### 启动顺序（严格按序）

1. **创建 tmux session** + 日志目录
2. **启动 Server**（server window）→ 等待 `SERVER_PORT=` 出现
3. **启动 Computer**（computer window）→ 等待 `a2c>` 提示符出现
4. **Computer 加入 Office**：在 computer pane 中发送 `socket join blob-uat-office blob-comp-001`（rawMode=true）→ 等待 `已加入房间 / Joined office`
5. **挂载 binary MCP server**（B-04 前置）：在 computer pane 中发送 `server add @<项目根>/.claude/skills/UAT/resources/seeds/mcp/binary_image_tool_server_config.json`（rawMode=true）→ 等待 `✅`；然后用 `sed` 将 config 中的 `<PROJECT_ROOT>` 替换为实际项目根路径

   > **简化方案**：先将 config 文件复制到 `/tmp` 并替换 `<PROJECT_ROOT>`：
   > ```bash
   > sed "s|<PROJECT_ROOT>|/Users/liulonggang/PycharmProjects/python-sdk|g" \
   >   <项目根>/.claude/skills/UAT/resources/seeds/mcp/binary_image_tool_server_config.json \
   >   > /tmp/a2c-uat-logs/binary-mcp-config.json
   > ```
   > 然后在 Computer CLI 中：`server add @/tmp/a2c-uat-logs/binary-mcp-config.json`

6. **启动 Agent**：运行 agent 驱动脚本（见下方）

### Agent 驱动脚本

> **复用 seed**: `seeds/_helpers/blob-resources/agent_blob_driver.py`

```bash
cd /Users/liulonggang/PycharmProjects/python-sdk && \
uv run python .claude/skills/UAT/resources/seeds/_helpers/blob-resources/agent_blob_driver.py \
  --port-file /tmp/a2c-uat-port \
  --office-id blob-uat-office \
  --computer-name blob-comp-001 \
  2>&1 | tee /tmp/a2c-uat-logs/agent.log
```

> **不加 `--skip-b04`**：默认执行全部 B-01~B-04 用例。

## 环境变量

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$
mkdir -p $A2C_SKILL_HOME
```

## 测试用例

### B-01: 小资源 inline（< inline_budget 直接返回）

- **优先级**: P0
- **前置**: 完整链路环境已搭建，Computer 已加入 Office，user skill blob-test 可见
- **步骤**:
  1. Agent 连接 Server 并加入 Office（`blob-uat-office`）
  2. Agent 通过 `server:list_room` 发现 Computer（`blob-comp-001`）
  3. Agent 发起 `client:get_skill`，参数 `name="blob-test"`, `computer="blob-comp-001"`
  4. 检查响应中 SKILL.md 的传输方式（inline）
- **预期结果**:
  - `get_skill` 响应中 SKILL.md 内容直接在 body 中返回（inline）
  - 无 blob_handle 字段
  - total_size <= 32768
  - sha256 与本地计算一致

### B-01b: small.txt inline（100 字节）

- **优先级**: P0
- **前置**: B-01 成功
- **步骤**:
  1. Agent 发起 `client:get_skill`，参数 `name="blob-test"`, `rel_path="small.txt"`
  2. 检查 small.txt 的传输方式
- **预期结果**:
  - body 直接返回（inline），无 blob_handle
  - total_size == 100
  - sha256 == `d82c6aa133a0fc25b087f46ad7ed2a3042772e612e015571e61753ff55ba6da8`

### B-02: 大资源 blob handle（> inline_budget 返回 handle）

- **优先级**: P0
- **引用 seed**: `seeds/_helpers/blob-resources`
- **前置**: B-01 成功
- **步骤**:
  1. Agent 发起 `client:get_skill`，参数 `name="blob-test"`, `rel_path="large.txt"`
  2. 观察 large.txt 的传输方式
  3. 如返回 blob_handle，Agent 发起 `client:get_blob` 获取完整内容
- **预期结果**:
  - `get_skill` 响应中 large.txt 返回 blob_handle（非 inline）
  - blob_handle 为非空字符串
  - `get_blob` 返回完整 large.txt 内容
  - total_size == 65536
  - sha256 == `fee47b1f0d7685a226fd5f2b9dd8f525038bbb05fe9d89a5d75c249edac868e3`

### B-03: SHA256 一致性（mint vs serve）

- **优先级**: P0
- **前置**: B-02 成功，已获取 large.txt 的 blob 内容
- **步骤**:
  1. 从 B-02 响应中记录 sha256 值和 total_size
  2. 对返回的 blob 内容本地计算 SHA256
  3. 对比 mint 端返回的 SHA256 和本地计算的 SHA256
- **预期结果**:
  - mint 端 sha256 与 serve 端（本地计算）完全一致
  - total_size 与实际 blob 内容长度一致
  - SHA256 与 `generate.sh` 产出的 large_sha256 一致

### B-04: tool_call 二进制（sideband blob 传输）

- **优先级**: P1
- **引用 seed**: `seeds/mcp/binary_image_tool_server`（返回确定性 PNG 二进制的 MCP stdio server）
- **引用 seed**: `seeds/mcp/binary_image_tool_server_config.json`（挂载该 server 的 MCP 配置，含 `auto_apply: true`）
- **前置**: 完整链路环境已搭建，Computer 已挂载 binary_image_tool_server MCP server（通过 `server add @...config.json`）
- **步骤**:
  1. 确认 Computer 已挂载 binary_image_tool_server（通过 Computer CLI `tools` 命令看到 `big_image` / `small_image`）
  2. Agent 发起 `client:tool_call`，调用 `big_image` 工具（`params: {}`, `timeout: 30`）
  3. 观察 tool_call 响应中 `_meta.a2c_blob_handle` 是否存在（big_image 32768B → base64 ~43.7KB > inline_budget）
  4. 如包含 blob_handle，Agent 发起 `client:get_blob` 获取完整二进制
  5. 验证 sha256 和 total_size
- **预期结果**:
  - `call_tool` 响应 `_meta` 包含 `a2c_blob_handle`（big_image base64 后 ~43.7KB > 32KB inline_budget）
  - `get_blob` 返回完整二进制数据
  - sha256 == `a06fa47c2671def27679fe048a287aeb2823c07a1e15d6395e02b3cec681c73d`
  - total_size == 32768
  - 无数据截断或损坏

> **MCP server 挂载**: 使用 seed 提供的 `seeds/mcp/binary_image_tool_server_config.json`
> 通过 Computer CLI `server add @<path>` 挂载。config 预设 `default_tool_meta.auto_apply: true`
> 跳过二次确认。使用前需将 `<PROJECT_ROOT>` 替换为实际项目根路径。

## 清理

1. Kill tmux session `a2c-uat`
2. 清理 `/tmp/a2c-uat-port`、`/tmp/a2c-uat-logs/`、`/tmp/a2c-uat-skill-home-*/`

## 日志收集

完整链路场景必须收集三端日志：

1. **Server pane**: `/tmp/a2c-uat-logs/server.log` + tmux capture-pane
2. **Computer pane**: `/tmp/a2c-uat-logs/computer.log` + tmux capture-pane
3. **Agent pane**: `/tmp/a2c-uat-logs/agent.log` + tmux capture-pane

每个用例执行后，对三个 pane 都执行 `capture-pane lines: 50`。
失败时增加到 `lines: 200` 并读取文件日志。
