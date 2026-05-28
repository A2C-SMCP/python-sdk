# 场景设计指南：二进制 / Blob 传输类

适用于涉及 SKILL blob handle、二进制 tool_call、SHA256 校验等二进制传输场景。

## 核心特征

- **阈值切换**：根据 inline_budget（默认 256B）自动切换 inline ↔ blob handle
- **SHA256 一致性**：mint 端和 serve 端的 SHA256/total_size 必须一致
- **渐进披露**：get_skills → get_skill → get_blob 三级渐进
- **完整链路必须**：blob 传输需要 Agent 发起请求，经 Server 路由到 Computer 解析

## 设计原则

### 1. 阈值边界测试

blob 传输的核心是 inline_budget 阈值。必须覆盖三个边界：

| 资源大小 | 传输模式 | 测试要点 |
| -------- | -------- | -------- |
| < inline_budget（如 100B） | inline | 直接在响应中返回内容 |
| = inline_budget（如 256B） | 边界 | 确认含等号的边界行为 |
| > inline_budget（如 1KB） | blob handle | 返回 handle，需二次获取 |

**测试方法**：准备三个不同大小的测试资源文件，分别请求并验证传输模式。

### 2. SHA256 端到端验证

blob 传输的完整性依赖 SHA256 校验。验证流程：

1. **mint 端**：Computer 创建 blob handle 时计算 SHA256
2. **serve 端**：Agent 通过 blob handle 获取内容后验证 SHA256

用例中必须：
- 在请求资源时记录返回的 SHA256 和 total_size
- 获取 blob 内容后，对比 SHA256 是否一致
- 验证 total_size 与实际内容长度一致

### 3. 渐进披露的三级验证

SKILL 资源获取遵循三级渐进模式：

```
Level 1: get_skills → 返回 skill 列表（含 metadata，不含资源内容）
Level 2: get_skill  → 返回单个 skill 详情（含 inline 资源或 blob handle）
Level 3: get_blob   → 通过 handle 获取完整 blob 内容
```

每个 level 是一个独立用例，验证：

| Level | 验证要点 |
| ----- | -------- |
| 1 | skills 列表包含预期的 skill 名称和基本信息 |
| 2 | inline 资源直接包含内容；大资源返回 blob handle |
| 3 | blob handle 可解析为完整内容，SHA256 一致 |

### 4. 二进制 tool_call 的 sideband

tool_call 返回二进制数据（如图片）时走 sideband blob：

1. Agent 发起 tool_call
2. Computer 执行 MCP 工具，返回二进制结果
3. SDK 自动将大二进制转为 blob handle
4. Agent 通过 blob handle 获取完整二进制

测试需要：
- 准备一个返回二进制数据的 MCP server（如返回图片）
- 发起 tool_call 并验证返回值中包含 blob handle
- 通过 blob handle 获取并验证二进制完整性

### 5. 完整链路环境

blob 传输场景需要完整的 Agent ↔ Server ↔ Computer 链路，且 Computer 需要挂载
一个能返回不同大小资源的 MCP server。

环境拓扑：

```
tmux session: a2c-uat
├── window: server     →  Server
├── window: computer   →  a2c-computer run --url <URL> --config <blob-test-mcp.json>
└── window: agent      →  Agent 测试脚本
```

`blob-test-mcp.json` 需要配置一个能返回多种大小资源的 MCP server。

## 验证清单补充项

- [ ] 阈值边界三个区间（小/等/大）均有覆盖
- [ ] SHA256 端到端验证（mint vs serve 一致性）
- [ ] 渐进披露三级均有对应用例
- [ ] 二进制 tool_call sideband 有独立用例
- [ ] 完整链路环境已搭建（含 blob-test MCP server）
