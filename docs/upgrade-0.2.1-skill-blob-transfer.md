# python-sdk 升级工单：0.2.1 — SKILL 通道 + 通用二进制传输 + MCP Marketplace Plugin

> **性质**：开发工单（不是规范）。协议为唯一权威，本文件只规定 **python-sdk 如何落地**。
> **目标读者**：python-sdk 实现工程师
> **前置门控**：✅ 已通过——协议 0.2.1 已在 `a2c-smcp-protocol` 评审、合并、发布（`main`）。代码仓库可跟进实现。
> **基线**：python-sdk 已完成 0.2.0 适配；协议版本握手在 PR #33（`feature/v0.2-version-handshake`，base `develop`）在途。
> **包/协议版本**：本次为 **0.2.x 加性能力**（协议侧已定为缺陷澄清级，不 MINOR bump）。`PROTOCOL_VERSION` 常量**不变**；包 PATCH 自由。

---

## 0. 范围

**在范围**（0.2.1 能力跟进）：

1. **SKILL 通道** —— `client:get_skills` / `client:get_skill`，`server:update_skills` / `notify:update_skills`；多 source：`mcp:` / **`marketplace:`（git plugin skills）** / `user`（DropIn）。
2. **通用二进制传输** —— `client:get_blob` + 无状态不透明 `blob_handle` 生产者-消费者模型。
3. **`client:tool_call` 二进制一致性** —— 超内联预算的二进制 content item 经 `_meta.a2c_blob_handle` 旁路（唯一触及既有事件的破坏性点）。

**不在范围**（勿在本工单内做，避免与他人冲突）：

- 协议版本握手 / WS-only §5 收口 —— 由 **PR #33** 承载，已在该 PR 评论给出 `websocket` scope 改动点。本工单与 PR #33 **解耦**，base 同样指向 `develop`，合并顺序无强依赖。

---

## 1. 协议权威（只读引用，禁止在 SDK 侧二次规范）

字段/事件/错误码/边界**一律以协议文档为准**，实现与之偏离即为 bug：

| 主题 | 权威文档 |
|---|---|
| SKILL 通道（命名 lexer、多 source、沙箱、变更检测、生命周期） | `a2c-smcp-protocol` `docs/specification/skill.md` |
| 通用二进制传输（句柄契约、分块/背压/完整性、4018） | `docs/specification/blob-transfer.md` |
| 事件请求/响应 + Computer 处理流程 | `docs/specification/events.md`（`client:get_skill[s]` / `client:get_blob` / `tool_call`） |
| TypedDict 字段 | `docs/specification/data-structures.md` |
| 错误码 4016/4017/4018、4014 复用 | `docs/specification/error-handling.md` |
| **跨语言适配总纲（先读这份）** | `docs/migrations/v0.2.1-skill-and-blob-transfer.md` |

本工单只补充协议文档没有、也不该有的内容：**python-sdk 的模块落点、既有约定对齐、测试与验收**。

---

## 2. python-sdk 基线与必须对齐的既有约定

| 约定 | 要求 |
|---|---|
| **async + sync 双镜像** | 凡 `client.py` 的改动必须等价镜像到 `sync_client.py`；`server/namespace.py`↔`sync_namespace.py`、`server/base.py`↔`sync_base.py` 同理。**禁止只做 async**（参考 PR #33 同步镜像边界处理） |
| **请求构造走 `_request_builders`** | Agent 侧新请求（get_skills/get_skill/get_blob）**MUST** 复用 `a2c_smcp/agent/_request_builders.py` 的 `create_*_request` 纯函数模式（#30 重构产物），不在 client 内联拼 dict |
| **协议保真 > Issue 字面** | 与 PR #33 同样原则：库实测行为/协议权威优先，偏离 Issue 字面处在代码 docstring 写明理由 |
| **lint/type 净** | `uv run poe lint`（ruff + mypy）零告警；`uv run poe test` 零回归 |
| **协议镜像缺口即补** | `smcp.py` 是协议结构在 SDK 的镜像层，缺字段即补，注释标注"对齐 <协议文档> §x" |

---

## 3. 工作分解（WBS，按模块 + 真实文件路径）

### 3.1 协议镜像层 — `a2c_smcp/smcp.py`

- **事件常量**新增：`GET_SKILLS_EVENT="client:get_skills"`、`GET_SKILL_EVENT="client:get_skill"`、`GET_BLOB_EVENT="client:get_blob"`、`UPDATE_SKILLS_EVENT="server:update_skills"`、`UPDATE_SKILLS_NOTIFICATION="notify:update_skills"`（与既有 `*_EVENT` 命名/注释风格一致）。
- **TypedDict** 新增（字段严格对齐 `data-structures.md`）：`A2CSkillRef`（**无 `mcp_server` 字段**；`path` 必选）、`GetSkillsReq/Ret`、`GetSkillReq/Ret`（`body` 与 `blob_handle` 恰一）、`GetBlobReq/Ret`；类型别名 `BlobHandle = str`。
- **`server:update_skills`/`notify:update_skills` 复用** `UpdateComputerConfigReq`（已存在），不新建结构。
- **`ErrorCode`** 增 `4016`/`4017`/`4018`（沿用既有 enum 风格 + 注释）；`4014` 复用语义在 docstring 标注；`4018.details.reason` 与 `4017.details.reason` 为开放枚举，解析方 MUST 容忍未知值兜底。

### 3.2 Computer — SKILL 子系统（**本次最大工作量**）

新建包 `a2c_smcp/computer/skills/`：

- `registry.py` —— Skill Registry：`name → A2CSkillRef`；O(1) 精确匹配；孤儿标记/恢复；校验失败不入册（记 ERROR，不向 Agent 硬报错）。
- `naming.py` —— name 合成 + lexer + MCP server 段规范化（= Claude Code 通用规则 `[^a-zA-Z0-9_-]→_`，**不实现** `claude.ai ` 特例）；非法 → `4016`。
- `staging.py` —— 多 source 物化到统一本地安装目录（marketplace SKILL v1 §2 包结构）：
  - `mcp:` —— 复用 `computer/mcp_clients/manager.py` 枚举 `skill://` 资源，按 `_meta.source ∈ {mounted, archive, resources}` 物化。
  - **`marketplace:` —— 用户配置 git 源：clone/pull + 对账（这是 "MCP marketplace Plugin" 能力的落点）**。
  - `user` —— 本地 DropIn 目录扫描。
- 安全铁律：包根绝对路径**只**由 Registry 经 name 解析；`safe_join`+`realpath` 必在包根内；`.skillenv` 等敏感文件任何 `rel_path` 下不可读出（→ `4017 forbidden`，不泄漏存在性）。

接入点：

- `a2c_smcp/computer/computer.py` —— 持有 registry；扩展 `_on_manager_change`（现仅处理 `window://`）**并行**处理 `skill://` 集合/内容变化 → 触发 `server:update_skills`；新增 marketplace/user 源的本地探测（机制 SDK 自决，与 `window://` 探测同构）。
- `a2c_smcp/computer/socketio/client.py`（`SMCPComputerClient`）——
  - 处理入站 `client:get_skills`（排除孤儿，不读 body）、`client:get_skill`（`4016`/`4014`/`4017`；仅 SKILL.md 剥 frontmatter；文本≤预算→`body`，否则铸 `blob_handle`、`total_size` 超上限→`4017 too_large` 不铸句柄）、`client:get_blob`（解析句柄→**重施 §9 沙箱**→切片→`4018`；单块≤Server `maxHttpBufferSize`）。
  - emit `server:update_skills`（复用 `UpdateComputerConfigReq`）。
  - **`tool_call` 返回前后处理**：遍历 `CallToolResult.content`，超内联预算的二进制 item 清空内联 `data`/`blob`、写 item `_meta.a2c_blob_handle`(+`a2c_total_size`/`a2c_sha256`，MIME 复用 item `mimeType`)；小图原样内联。工具失败仍走 MCP `isError`。

### 3.3 通用 blob-transfer

- Computer 端句柄铸造/解析与 §3.2 复用同一沙箱（句柄不透明、无状态、可重解析；解析时重跑边界校验，不信任句柄内容）。
- Agent 端**统一拉取例程** —— 新建 `a2c_smcp/utils/blob.py`（与 `utils/handshake.py` 同层）：`drain_blob(call, computer, blob_handle) -> (bytes, mime)`：`chunk_offset` 循环至 `eof`；`eof` 后校验 `sha256`；跨块 `sha256`/`total_size` 变化→从 0 重读；`4018` 分支（`invalid_handle`/`forbidden` 不重试，`gone` 回生产者重取，`range` 修偏移）。**get_skill 与 tool_call 两处共用此一函数。**

### 3.4 Agent 消费 — `a2c_smcp/agent/{client,sync_client,base}.py` + `_request_builders.py`

- `_request_builders.py` 增 `create_get_skills_request` / `create_get_skill_request` / `create_get_blob_request`。
- 处理 `notify:update_skills` → 自动重拉 `client:get_skills`（与既有 `notify:update_*` 自动刷新模式一致）。
- `get_skill` 响应**分支**：`body` 直接用；`blob_handle` → `utils/blob.drain_blob`。
- **`tool_call` 消费**：遍历 `CallToolResult.content`，命中 `_meta.a2c_blob_handle` → `drain_blob` 还原后再交付上层（**不实现 = 大二进制工具结果静默变空**，破坏性关键点）。
- async + sync 双实现。

### 3.5 Server 路由 — `a2c_smcp/server/{namespace,sync_namespace}.py`

- 路由 `client:get_skills`/`client:get_skill`/`client:get_blob`（确认现有 `client:*` 转发是否泛化；若白名单则加）。
- 收 `server:update_skills` → 向房间广播 `notify:update_skills`（复用既有 update 广播路径）。
- Server **不**重组 blob，按 `computer` 逐 ack 透传。

---

## 4. 强制工程约束

- **sync 镜像 parity**：每个 async 改动同 PR 内补 sync 镜像，否则视为未完成。
- **安全不变量回归化**：沙箱穿越 / `.skillenv` forbidden（不泄漏存在性）/ handle 不信任 / `4017 too_large` 不铸句柄——每条都要有**对应的红→绿反例测试**。
- **协议保真**：偏离任何协议文档即 bug；确有库行为差异在 docstring 标注权威路径（同 PR #33 §4008 提取的处理范式）。
- **占位符不展开**：`$TFROBOT_*` 由 Agent SDK 在 prompt 渲染层处理，Computer 协议层只送原始字节。

---

## 5. 测试要求（按现有 `tests/{unit_tests,integration_tests,e2e}/{agent,computer,server,utils}` 分层）

| 层 | 必覆盖 |
|---|---|
| `unit_tests/utils` | `drain_blob`：多块重组 / `eof` / sha256 校验失败重读 / `total_size` 跨块变更重读 / `4018` 各 reason |
| `unit_tests/computer` | name lexer（合法/非法→4016）；沙箱（`..`/绝对路径/symlink 逃逸→4017 traversal）；`.skillenv`→4017 forbidden（存在/不存在同 reason）；`too_large` 不铸句柄；inline-vs-handle 阈值；marketplace 源 clone/对账；孤儿标记/恢复 |
| `unit_tests/agent` | get_skill 分支 body/handle；**tool_call content `_meta.a2c_blob_handle` 还原**；notify:update_skills→重拉 |
| `unit_tests/server` | 三个新 `client:*` 路由；`server:update_skills`→`notify:update_skills` 广播 |
| `integration_tests` | get_skills→get_skill→get_blob 全链路（含二进制 round-trip sha256 一致）；marketplace plugin 源端到端纳管 |
| `e2e` | Agent 渐进式披露：读 SKILL.md→按 rel_path 取引用资源（文本内联 + 二进制经 blob）|

零回归（对齐 PR #33 的"基线 → 零回归"验收口径）。

---

## 6. 验收清单

- [ ] `smcp.py`：5 事件常量 + 7 TypedDict + `BlobHandle` + `ErrorCode 4016/4017/4018`，注释标注协议出处
- [ ] `computer/skills/`：registry + naming + staging（mcp / **marketplace git** / user 三源）
- [ ] `computer.py` `_on_manager_change` 扩展 `skill://` + 非 MCP 源探测 → `server:update_skills`
- [ ] `computer/socketio/client.py`：get_skills/get_skill/get_blob 处理 + tool_call 二进制旁路（async+sync）
- [ ] `utils/blob.py` `drain_blob` 单一例程，get_skill 与 tool_call 共用
- [ ] `agent/_request_builders.py` 三 builder；`agent/client.py`+`sync_client.py` 消费分支 + tool_call 扫描
- [ ] `server/namespace.py`+`sync_namespace.py` 路由 + 广播
- [ ] 安全反例测试全部红→绿；`uv run poe lint` 净；`uv run poe test` 零回归
- [ ] `PROTOCOL_VERSION` 未改动（本次 0.2.x 加性）

---

## 7. 建议 PR 拆分（与 PR #33 解耦，base 均 `develop`）

建一个 parent tracking issue，子 PR 串行/并行：

1. `feat(smcp): 0.2.1 协议镜像（events/TypedDict/ErrorCode）` —— 无行为，先合，解锁其余
2. `feat(blob): 通用二进制传输（Computer 句柄 + utils/blob.drain_blob）`
3. `feat(skill): Computer SKILL 子系统（registry/naming/staging 三源含 marketplace）`
4. `feat(skill/tool_call): Agent 消费 + tool_call 二进制一致性`
5. `feat(server): SKILL/blob 事件路由 + update_skills 广播`

每个子 PR 自带 async+sync 镜像与对应测试；2/3 可并行，4 依赖 1/2，5 依赖 1。

---

## 8. 风险与依赖

- **marketplace git 源**：网络拉取/缓存/对账失败要降级（记 ERROR、不阻断其余 source、不向 Agent 硬报错）；首拉成本与定时对账策略 SDK 自决。
- **staging 目录隔离**：对齐 CC——默认每用户私有 home + 创建时 `0o700` 防御性写，隔离交 OS 权限（**不**做 path deny-list）；协议 §9.2 `MUST NOT 跨用户共享` 待校准为 SHOULD/部署指引（详见 skill-computer-management §2.3 决策③）。
- **底层 mcp 包**：沿用 0.2 锚定（`mcp >= 1.15.0`），`Resource._meta` / `annotations` 须可用（启动自检 fail-fast，沿用 versioning.md §校验建议）。
- **与 PR #33 无代码冲突**：本工单不碰 `server/middleware.py` / `utils/handshake.py` / `version.py`；如需先合 PR #33 仅为减少 rebase，无强依赖。

---

## 参考

- 协议：`a2c-smcp-protocol` `docs/specification/{skill,blob-transfer,events,data-structures,error-handling}.md`
- 跨语言适配总纲：`docs/migrations/v0.2.1-skill-and-blob-transfer.md`
- 同期协议侧改动：versioning.md §5（WS-only 收口，归 PR #33，不在本工单）
