# 设计文档：0.2.1 — SKILL Computer 管理 + A2C 发现与传递

> **性质**：python-sdk 实现设计（**不是协议规范**）。协议为唯一权威；本文档只规定协议明确判给 SDK 自决的部分、SDK 模块落点、既有约定对齐、WBS 与验收。
> **配套**：开发工单 [`docs/upgrade-0.2.1-skill-blob-transfer.md`](upgrade-0.2.1-skill-blob-transfer.md)（范围/约束/PR 拆分）；本文档是其设计补全。
> **范本**：Claude Code Marketplace/Plugin 操作生命周期（`MARKETPLACE_PLUGIN_OPERATIONS.md`，外部参考）——SKILL Computer 本地管理侧的设计蓝本。
> **追踪**：GitHub Milestone「v0.2.1 SKILL 通道 + 通用二进制传输」+ parent tracking issue。
> **前置门控**：✅ 协议 0.2.1 已在 `a2c-smcp-protocol` 评审/合并/发布（`main`）。`PROTOCOL_VERSION` 常量**不改**（0.2.x 加性）。

---

## 0. 协议权威（只读引用，禁止 SDK 侧二次规范）

字段/事件/错误码/边界一律以协议为准，实现与之偏离即 bug：

| 主题 | 权威文档（`a2c-smcp-protocol`） |
|---|---|
| 跨语言适配总纲（先读） | `docs/migrations/v0.2.1-skill-and-blob-transfer.md` |
| SKILL 通道（命名 lexer / 多 source / 沙箱 / 变更检测 / 生命周期） | `docs/specification/skill.md` |
| 通用二进制传输（句柄契约 / 分块背压完整性 / 4018） | `docs/specification/blob-transfer.md` |
| 事件请求响应 + Computer 处理流程 | `docs/specification/events.md` |
| TypedDict 字段 | `docs/specification/data-structures.md` |
| 错误码 4016/4017/4018，4014 复用 | `docs/specification/error-handling.md` |

本文档**只**补协议没有、也不该有的内容。

---

## 1. 范围

**在范围**

1. **A2C 发现与传递（协议侧）**：`client:get_skills` / `client:get_skill` / `client:get_blob`、`server:update_skills` / `notify:update_skills`；`client:tool_call` 二进制一致性（`_meta.a2c_blob_handle` 旁路）。镜像既有 `get_resources` v0.2 全链路范式。
2. **SKILL Computer 本地管理（SDK 自决侧）**：SKILL Home / 三源 staging（`mcp:` / `marketplace:` / `user`）/ Skill Registry / 命名 lexer / 变更检测 / 安全沙箱。

**不在范围**（解耦，勿在本工单触碰）

- 协议版本握手 / WS-only 收口 —— 归 PR #33（已合并）。本工单不碰 `server/middleware.py`、`utils/handshake.py`、`version.py`。
- 后台定时对账守护进程 —— 显式后置为 backlog（见 §3.2）。

---

## 2. 三项 SDK-自决关键决策（已评审锁定）

协议 `skill.md §4` 把「本地安装位置 / 管理 UX / 探测机制」判给 SDK。以下三项为本设计的架构基线：

### 2.1 决策 ① SKILL 源「意图层」落点 → **混合**

「意图层」= 用户声明「想要哪些 SKILL 源」的配置；「物化层」= 磁盘实际 staging 产物 + Registry；二者由 reconciler 对账（概念源自 Claude Code `MARKETPLACE_PLUGIN_OPERATIONS.md §1`）。

| Source | 意图层落点 | 物化层 | 变更检测 |
|---|---|---|---|
| `mcp:` | **复用现有 MCP Server 配置**（`MCPServerConfig`，经 `client:get_config`）。无独立意图层——协议 `skill.md §5`「MCP Server 连接动作本身即视为 SKILL 安装授权」 | SKILL Home `mcp/<server>/<skill>/` | 复用现有 MCP `ResourceListChanged/Updated` 经 `computer.py:_on_manager_change`（与 `window://` 并行） |
| `marketplace:` | **SKILL Home 下独立 registry**（git 源清单，对标 Claude Code `known_marketplaces.json`） | SKILL Home `marketplace/<repo>/<...>/<skill>/` | 启动对账 + 显式 refresh（§2.2） |
| `user` | **SKILL Home 下独立 registry**（DropIn 目录清单） | SKILL Home `user/<skill>/` | 启动扫描 + 显式 refresh / SDK 管理 UX 动作 |

**依据**：
- `mcp:` 已有可用意图（MCP 配置）+ 可用物化/变更线路（`manager` + `_on_manager_change`）。工单 §3.1/§3.2 明确规定 `mcp:` 复用 `mcp_clients/manager.py` 枚举 `skill://`、扩展 `_on_manager_change`。强行另起独立 registry = 重复造源管理。
- `marketplace:` / `user` 为净新增、无现有配置宿主，概念上正是 Claude Code marketplace 两层模型。把 git 源生命周期（clone/pull/孤儿/GC/autoUpdate）塞进 `MCPServerConfig`（传输/工具配置）= 范畴错误，且会把 SKILL 源意图泄进 `client:get_config`（违背协议理念 #2「对 Agent 暴露表面与 source 无关」）。
- 混合方案使「三源各按本性管理、对 Agent 表面统一」，正合 `skill.md §8`「表面与 source 无关，机制细节按源不同（§5）」。

> **`user` 源 DropIn 的两类根 + 与 active-workdir 的关系**（与 [`design-0.2.1-cli-marketplace-ux.md`](design-0.2.1-cli-marketplace-ux.md) §5.0/§5.1 对齐；上表「物化层」只画了第 ① 类）：
> - ① 全局 `$A2C_SKILL_HOME/user/<skill>/`（SKILL Home 内）；② 每个 **workspace 登记工作目录** `<workdir>/.tfrobot/skills/<skill>/`（**就地发现、不 staging 进 SKILL Home**，与 marketplace clone 树相反）。两类都被 watcher 实时监控。
> - 二者同属 CLI-UX 的**能力发现层**：跨**全部登记工作目录**全局并集、置最低优先级、**不随 active workdir 切换**。
> - **关键澄清**：CLI-UX 新增的 `active-workdir 单根` 概念只作用于 settings.json / mcp.json 的「(B) 敏感+标量层」（trust / MCP 批准 / permissions / 标量），**不影响 SKILL 可见集**——Skill Registry 与 `get_skills`（§4.1/§5.1）返回的是 **workspace 全局稳定集**，与当前任务绑定的 active workdir 无关。本文档（Registry/staging/sandbox/协议传递）因此**不受** settings 聚合/版本/active-workdir 决策影响。

### 2.2 决策 ② marketplace git 源刷新/对账 → **Claude Code 对标（无 TTL）**

- **eager add**：用户配置 git 源即时 `git clone --depth 1`（SSH→HTTPS 回退；`GIT_TERMINAL_PROMPT=0`、`GIT_ASKPASS=''`；超时可配，默认 120s）。不走 `gh`。
- **显式 refresh**：原地 `git pull`（失败则全量重 clone）→ 与缓存 SKILL 集合对账。
- **启动对账（reconcile）**：Computer 启动时按意图同步物化（缺的 clone、变的重拉、删的清理）；尊重 per-source `auto_update` 标志（对标 Claude Code「autoUpdate 启动时触发」，非 TTL）。
- **无 TTL 自动刷新**。后台定时对账守护进程**显式后置**为 backlog（协议 `skill.md §8.3` 只要求「定时 / 用户触发」其一；启动对账 + 显式 refresh 已满足；可日后非破坏加性引入）。
- **失败降级铁律**（工单 §8）：网络/clone/对账失败记 ERROR，不阻断其余 source，不向 Agent 硬报错，不入协议错误码（物化失败的 SKILL 不进 Registry，对 Agent 不可见）。

**依据**：范本 `MARKETPLACE_PLUGIN_OPERATIONS.md §0/§5` 明确「eager add，非懒加载」「无 TTL，要么显式要么 autoUpdate 启动」。纯懒加载相悖且把网络延迟塞进 Agent 朝向的轻量 `get_skills`；后台定时器是真·加性，范围/测试面膨胀，后置。

### 2.3 决策 ③ SKILL Home 路径 → **可配置，默认 `$XDG_DATA_HOME/a2c/skills`（回退 `~/.a2c/skills`）**

- 解析顺序：显式配置/环境变量覆盖 → `$XDG_DATA_HOME/a2c/skills` → `~/.a2c/skills`。
- env 覆盖键：`A2C_SKILL_HOME`（镜像 Claude Code `CLAUDE_CODE_PLUGIN_CACHE_DIR` 的「默认在用户 home + 可覆盖」范式）。
- 布局：`<skill_home>/<source>/<...>/<skill>/`（协议 `skill.md §4` 推荐三级分组）。
- 隔离铁律（协议 `skill.md §9.2`）：**MUST NOT 跨用户共享**，不放系统目录；多用户每用户独立 home；覆盖路径仍 MUST 非系统共享目录（启动时校验，违反 fail-fast）。

**依据**：Claude Code 范本本就是「默认用户 home + env 覆盖」；工单 §8 点名容器/多实例必须有覆盖旋钮，否则同镜像多实例撞目录。XDG-first 为跨平台惯例。

---

## 3. 架构总览

```
        Agent SDK                    Server                       Computer SDK
  ┌───────────────────┐      ┌──────────────────┐      ┌──────────────────────────────┐
  │ get_skills         │─────▶│ 路由 client:*     │─────▶│  ┌─ 协议发现传递侧 ───────────┐ │
  │ get_skill ─┐       │      │ (按 computer)     │      │  │ on_get_skills/skill/blob   │ │
  │ get_blob ◀─┘drain  │◀─────│ 广播 notify:*     │◀─────│  │ tool_call 二进制旁路        │ │
  │ utils/blob.drain   │      └──────────────────┘      │  │ emit_update_skills         │ │
  │ notify:update_skills│                                │  └────────────┬───────────────┘ │
  └───────────────────┘                                 │      Skill Registry (name→ref)  │
                                                          │  ┌─ SKILL Computer 本地管理 ──┐ │
                                                          │  │ SKILL Home staging          │ │
                                                          │  │ mcp: / marketplace: / user  │ │
                                                          │  │ 意图层↔物化层↔reconciler    │ │
                                                          │  │ naming lexer / 沙箱         │ │
                                                          │  └─────────────────────────────┘ │
                                                          └──────────────────────────────┘
```

两大块解耦：**协议发现传递侧**镜像 `get_resources` v0.2 范式（无 SDK 设计自由度，照协议）；**SKILL Computer 本地管理侧**按 §2 三决策落地（SDK 自决，对标 Claude Code）。Skill Registry 是二者唯一接缝（`name → A2CSkillRef`，含包根绝对路径）。

---

## 4. 协议发现传递侧设计（镜像 `get_resources` v0.2 范式）

### 4.1 协议镜像层 `a2c_smcp/smcp.py`

范本：现有 `GetResourcesReq/Ret`（`smcp.py:413-432`）、`ErrorPayload`（`:435-466`）、`is_protocol_error_payload`（`:473-483`）。

新增（字段严格对齐 `data-structures.md`，注释标注协议出处，沿用既有 `*_EVENT` 命名/注释风格）：

- 事件常量：`GET_SKILLS_EVENT="client:get_skills"`、`GET_SKILL_EVENT="client:get_skill"`、`GET_BLOB_EVENT="client:get_blob"`、`UPDATE_SKILLS_EVENT="server:update_skills"`、`UPDATE_SKILLS_NOTIFICATION="notify:update_skills"`。
- TypedDict：`A2CSkillRef`（**无 `mcp_server` 字段**；`path` 必选）、`GetSkillsReq/Ret`、`GetSkillReq/Ret`（`body` 与 `blob_handle` 恰一）、`GetBlobReq/Ret`；类型别名 `BlobHandle: TypeAlias = str`。
- `ErrorCode` 增 `SKILL_NAME_INVALID=4016`、`SKILL_RESOURCE_NOT_ACCESSIBLE=4017`、`BLOB_NOT_ACCESSIBLE=4018`（沿用 `IntEnum` 风格 + 注释）；`4014` 复用语义 docstring 标注；`4017/4018.details.reason` 为开放枚举，解析方 MUST 容忍未知值兜底（默认「不重试 + 诊断」）。
- `server:update_skills` / `notify:update_skills` **复用** `UpdateComputerConfigReq`（`smcp.py:92`），不新建结构。

### 4.2 Computer 生产侧 `a2c_smcp/computer/socketio/client.py`

范本：`on_get_resources`（`socketio/client.py:398-447`）的注册（`:143`）+ flat `ErrorPayload` 返回模式。

- `__init__` 注册三事件 handler（仿 `:143`）。
- `on_get_skills`：从 Registry 读可用项，排除孤儿，不读 body，按发现序返回。
- `on_get_skill`：name lexer 失败 → `4016`；name 不在 Registry → `4014`；`rel_path` `safe_join`+`realpath` 越界/命中 `.skillenv`/不存在 → `4017`（reason）；`total_size` 超上限 → `4017 too_large`（不铸句柄）；仅 SKILL.md 剥 frontmatter；文本 ≤ 内联预算 → `body`，否则铸 `blob_handle`。
- `on_get_blob`：解析句柄 → **重施 §6 沙箱** → 切片；`4018`(`invalid_handle`/`forbidden`/`gone`/`range`)；单块序列化 ≤ Server `maxHttpBufferSize`。
- `emit_update_skills`：仿 `emit_update_tool_list`（`:281-287`），复用 `UpdateComputerConfigReq`，`office_id` 守卫。
- `on_tool_call`（`:297-322`）返回前：遍历 `CallToolResult.content`，对超内联预算二进制 item 清空内联 `data`/`blob`、写 item `_meta.a2c_blob_handle`(+`a2c_total_size`/`a2c_sha256`，MIME 复用 item `mimeType`)；小图原样内联；工具失败仍走 MCP `isError`（既有不变量）。

错误以 flat `ErrorPayload` 经 Socket.IO ack 第一参回传（无嵌套 envelope），与 `on_get_resources` 完全一致。

### 4.3 blob_handle 无状态编码方案（SDK 自决，协议 `blob-transfer.md §1/§5` 约束）

句柄是**逻辑源描述符**，不是文件系统路径。两类生产者两种 kind，统一**单层**编码（无签名层）：

```
blob_handle = base64url( msgpack({
    "v": 1,
    "kind": "skill" | "toolspool",
    # kind=skill:     {"name": <A2CSkillRef.name>, "rel_path": <POSIX rel>}
    # kind=toolspool: {"cid": <content-addressed id = sha256>, "mime": <mime>}
}) )
```

- **不透明性**：协议 §1 的「不透明」是**Agent 行为 MUST**（Agent MUST NOT 解析/拼接/伪造/跨 Computer 复用），不可由 Computer 加密强制对端。SDK 满足契约即 Agent SDK 始终把句柄当字符串透传、不解码；这与协议要求一致。
- **不签名 / 不 MAC**：协议 §1/§5.4 把句柄安全边界定在「**Computer 解析时 MUST 重跑本通道边界校验，不信任句柄内容**」——这是唯一真安全根。MAC 是冗余防御层，不增加任何真正安全（伪造句柄仍要过 Registry + 沙箱），却带来 per-process secret 管理/轮转/测试 fixture 维护负担，**故不采用**。业界对照：SFTP / OCI registry / gRPC / OpenAI Realtime 均不签内部传输 token。
- **无状态可重解析**：无 session/游标/TTL/per-process secret。每次 `get_blob` 独立确定性回源、幂等、可并行（§4.5）：
  - `kind=skill`：**忽略句柄内任何路径**，用 `name` 经 Skill Registry O(1) 解析包根 → 对 `rel_path` **重跑 §6 沙箱**（不信任句柄内容，协议 §5.4）。Registry 未命中/孤儿 → `4018 gone`；沙箱失败 → `4018 forbidden`；msgpack 格式不识别 / `v` 未知 → `4018 invalid_handle`。
  - `kind=toolspool`:tool_call 二进制不可由工具重跑确定性重得，故铸造时溢写到**内容寻址 blob 暂存区**（SKILL Home 同级 `<skill_home>/.blobspool/<cid>`，`cid=sha256`）。`get_blob` 按 `cid` 回查并校验 sha256。暂存被 GC/淘汰 → `4018 gone`（协议许可「源消失 → gone → Agent 回生产者重取句柄」）。**句柄跨 Computer 重启可解析**（cid 在磁盘 / Registry 启动重建），无 HMAC 后此红利兑现。
- **句柄有效期语义**（协议 §5.3 要求文档化）：本 SDK 不设 TTL；句柄「有效」≡「源仍被铸造通道授权且可解析」。源变更由 `sha256`/`total_size` 检测，源消失由 `4018 gone` 表达。

### 4.4 阈值默认值（SDK 可配，协议要求「保证单条 ack 不超 Server buffer」）

| 项 | 默认 | 配置键 | 说明 |
|---|---|---|---|
| 内联预算 inline budget | 32 KiB | `A2C_SKILL_INLINE_BUDGET` | 文本资源 ≤ 此 → `body`；否则铸句柄。二进制 MIME 一律铸句柄 |
| 绝对上限 too_large cap | 100 MiB | `A2C_SKILL_MAX_SIZE` | `total_size` 超此 → `4017 too_large`，不铸句柄、零字节传输（DoS 防御） |
| 单块默认 max_chunk_bytes | 256 KiB（原始字节）| `A2C_BLOB_CHUNK_BYTES` | Computer 对客户建议值 clamp；恒保证 base64(+33%)+envelope ≤ Server `maxHttpBufferSize`（默认 1 MB），故 ~342 KiB base64 远低于 1 MB |

「资源字节」基准三处一致（协议 `blob-transfer.md`）：SKILL.md → frontmatter 剥离后 body；其它 → 原始字节；占位符 `$TFROBOT_*` **不展开**（Agent prompt 渲染层职责，非协议层）。

### 4.5 Agent 消费侧

- **统一拉取例程** `a2c_smcp/utils/blob.py`（与 `utils/handshake.py` 同层）：

  ```python
  async def drain_blob(
      call, computer: str, blob_handle: str, *,
      concurrency: int = 1,              # 默认串行（协议 reference impl 语义）；>1 启用并行
      chunk_size: int | None = None,     # 客户建议 max_chunk_bytes；缺省由 Computer clamp
  ) -> tuple[bytes, str]: ...
  ```

  共享行为：`eof` 后校验全量 `sha256`；跨块 `sha256`/`total_size` 变化 → 从 0 重读；`4018` 分支（`invalid_handle`/`forbidden` 不重试，`gone` 回生产者重取句柄，`range` 修偏移）。**get_skill 与 tool_call 两处共用此一函数。** 行为契约见协议 `blob-transfer.md §5.1` reference impl。

- **并行红利（协议 §3 明文）**：`chunk_offset` 为资源字节绝对偏移、Computer 无服务端状态 → 「天然幂等、可并行不同 offset」。`drain_blob` 实现两种模式：
  - `concurrency=1`：串行循环（协议 reference impl，最小可证），保守默认。
  - `concurrency>1`：
    1. 首块（offset=0，串行）获知 `total_size`/`sha256`/`mime_type`；
    2. 计算剩余 chunk 起点集合，经 `asyncio.Semaphore(concurrency)`（sync 镜像走 `ThreadPoolExecutor`）并发 `client:get_blob`；
    3. **错误协调**：任一块 `4018 invalid_handle`/`forbidden` → 取消所有在飞 + raise；`gone` → 取消 + raise（上层回生产者）；`range` → 取消 + 串行 fallback；任一块的 `sha256`/`total_size` 与首块不一致 → 取消所有在飞 + 从 0 串行重读（不在并发态拼接错配字节）；
    4. 按 offset 重组 → 全量 `sha256` 自证。

  docstring **必须** 明示「并行安全」与上述错误协调矩阵——这是和 SFTP（有状态句柄需多 handle）拉开差距的红利，丢失即等同自我贬值。
- **请求构造** `a2c_smcp/agent/_request_builders.py`（#30 纯函数范式，范本 `build_get_resources_request`）：增 `build_get_skills_request` / `build_get_skill_request` / `build_get_blob_request`。
- **Base 客户端** `a2c_smcp/agent/base.py`：`BaseAgentClient` / `BaseAgentSyncClient` 各增 `create_get_skills/skill/blob_request`（范本 `create_get_resources_request:119`）。
- **async/sync 双镜像** `agent/client.py` ↔ `agent/sync_client.py`（范本 `get_resources`，`client.py:357` / `sync_client.py:390`）：`get_skills` / `get_skill` / `get_blob`；`raise_for_error_payload`（`errors.py:44`）复用判错；`get_skill` 响应分支 `body` 直接用 / `blob_handle` → `drain_blob`；`tool_call` 消费遍历 `CallToolResult.content` 命中 `_meta.a2c_blob_handle` → `drain_blob` 还原后交付上层（**不实现 = 大二进制工具结果静默变空**，破坏性关键点）；`notify:update_skills` → 自动重拉 `client:get_skills`（仿既有 `notify:update_*` 自动刷新）。

### 4.6 Server 路由 `a2c_smcp/server/namespace.py` ↔ `sync_namespace.py`

范本：`on_client_get_resources`（`namespace.py:459` / `sync_namespace.py:459`）+ office/role 隔离校验（**显式 raise `SMCPNamespaceError`**，非 assert——对齐 #31/`-O` 安全加固）。

- 路由 `client:get_skills`/`client:get_skill`/`client:get_blob`（确认 `client:*` 转发是否已泛化；白名单则加三事件）。
- 收 `server:update_skills` → 向房间广播 `notify:update_skills`（复用既有 update 广播路径，范本 `on_server_update_tool_list:322`）。
- Server **不**重组 blob，按 `computer` 逐 ack 透传；flat `ErrorPayload` 原样透传（禁止二次 unwrap）。

---

## 5. SKILL Computer 本地管理侧设计（SDK 自决，对标 Claude Code）

新建包 `a2c_smcp/computer/skills/`：

| 模块 | 职责 | Claude Code 范本对照 |
|---|---|---|
| `home.py` | SKILL Home 路径解析（§2.3）+ 隔离 fail-fast 校验 + `<source>/<...>/<skill>/` 布局 | `getMarketplacesCacheDir()` / `CLAUDE_CODE_PLUGIN_CACHE_DIR` |
| `naming.py` | name 合成 + lexer + MCP server 段规范化（`[^a-zA-Z0-9_-]→_`，**不实现** `claude.ai ` 特例）；非法 → `4016` | `normalizeNameForMCP()`（去特例） |
| `registry.py` | Skill Registry：`name → A2CSkillRef`；O(1) 精确匹配；孤儿标记/恢复；校验失败不入册（记 ERROR，不硬报错） | `installed_plugins.json` 物化注册表 |
| `staging.py` | 多 source 物化到统一本地安装目录（marketplace SKILL v1 §2 包结构）：`mcp:` 经 `manager` 枚举 `skill://` 按 `_meta.source∈{mounted,archive,resources}` 物化；`marketplace:` git clone/pull+对账；`user` DropIn 扫描 | `loadAndCacheMarketplace()` / `cacheMarketplaceFromGit()` |
| `intent.py` | marketplace/user 独立 registry（意图层持久化）+ reconciler（启动/显式 refresh 对账意图↔物化）；`mcp:` 不经此（复用 MCP 配置） | `known_marketplaces.json` + `reconciler.ts` |
| `sandbox.py` | `safe_join`+`realpath` 包根内校验；`.skillenv` 等敏感文件任何 `rel_path` 下 `4017 forbidden`（不泄漏存在性）；name 寻址防越权（包根仅由 Registry 经 name 解析，禁从 name/rel_path 推导 FS 路径） | — |

### 5.1 接入点 `a2c_smcp/computer/computer.py`

- 持有 `SkillRegistry`（仿持有 `mcp_manager`，`computer.py:98`）。
- 扩展 `_on_manager_change`（`computer.py:187-257`，现仅 `window://`）：**并行**新增 `skill://` 分支——`ResourceListChanged` 重枚举 `skill://` 集合、`ResourceUpdated(skill://)` 重物化该 SKILL → 增量物化 → `emit_update_skills`。仿 `_acollect_window_uris`（`:259-271`）新增 `_acollect_skill_refs` 缓存对比（集合相同跳过，仅 DEBUG）。
- 新增 marketplace/user 源本地探测（启动 reconcile + 显式 refresh 入口；机制 SDK 自决，与 `window://` 探测同构）。**`user` 源探测含全部登记工作目录的 `<workdir>/.tfrobot/skills/` watcher**（能力发现层、全局并集、不随 active workdir 切换；见 §2.1 注与 CLI-UX §5.0/§5.1）。
- 委托方法 `get_skills` / `get_skill` / `get_blob`（仿 `get_resources` 委托 `manager`，但委托 `SkillRegistry`/`staging`/`sandbox`）。

### 5.2 mcp: 源枚举 `a2c_smcp/computer/mcp_clients/manager.py`

范本：`list_resources`（`manager.py:455-480`，单页 cursor 透传）、`list_windows`（按 scheme 过滤 `window://`，`:519-544`）、`get_windows_details`（`resources/read`，`:546-571`）。

- 新增 `list_skill_resources`：按 `skill://` scheme 过滤 + server 归属，**完整消费 cursor 翻页直至末尾**（协议 `skill.md §12`：Computer 完整消费翻页，区别于 `get_resources` 的 Agent 控制翻页）。
- `archive`/`resources` 模式经现有 `read_resource` 入口逐子资源物化。

### 5.3 安全模型落地（协议 `skill.md §9` 回归化为反例测试）

每条配「红→绿」反例测试（工单 §4）：

- 沙箱穿越：`..` / 绝对路径 / symlink 逃逸 → `4017 traversal`。
- `.skillenv` forbidden：任何 `rel_path` 命中 → `4017 forbidden`，存在/不存在**同 reason**（不泄漏存在性）。
- `too_large` 不铸句柄：超上限 → `4017 too_large`，零字节。
- 句柄不信任：`get_blob` 重施沙箱；伪造/篡改句柄不提权。
- staging 隔离：SKILL Home 非系统共享目录（启动 fail-fast）。
- name 寻址：包根仅 Registry 经 name 解析，不从 name 推导 FS 路径。

---

## 6. 强制工程约束（对齐工单 §4 + 既有约定）

- **sync 镜像 parity**：每个 async 改动同 PR 内补 sync 镜像（`agent/client.py↔sync_client.py`、`server/namespace.py↔sync_namespace.py`、`base.py` 内 `BaseAgentClient↔BaseAgentSyncClient`），否则视为未完成。Computer `socketio/client.py` 仅 async（Computer 总异步运行，无 sync 镜像，沿用现状）。
- **请求构造走 `_request_builders`**：新请求 MUST 复用纯函数模式（#30），不在 client 内联拼 dict。
- **隔离不变量显式 raise**：office/role 校验用 `SMCPNamespaceError`，禁 `assert`（#31，`-O` 下被剥离）。
- **协议保真 > Issue 字面**：偏离协议即 bug；确有库行为差异在 docstring 标注权威路径。
- **协议镜像缺口即补**：`smcp.py` 缺字段即补，注释标注「对齐 <协议文档> §x」。
- **lint/type 净**：`uv run poe lint`（ruff + mypy）零告警；`uv run poe test` 零回归。门为 mypy（非 Pyright）。

---

## 7. WBS / Milestone 拆解

GitHub Milestone「v0.2.1 SKILL 通道 + 通用二进制传输」+ parent tracking issue（镜像已关闭 Milestone #1「v0.2 协议升级」issue #8 范式）。按工单 §7 五条工作流拆 5 子 issue：

```
              [① smcp 协议镜像]  (无行为，先合，解锁其余)
                     │
        ┌────────────┼───────────────┐
        ▼            ▼               ▼
  [② blob 传输]  [③ skill 子系统]  [⑤ server 路由+广播]
        │            │
        └─────┬──────┘
              ▼
  [④ agent 消费 + tool_call 二进制一致性]  (依赖 ①②)
```

| # | 子 issue | 范围 | 主要文件 | 依赖 | label |
|---|---|---|---|---|---|
| ① | smcp 0.2.1 协议镜像 | 5 事件常量 + 7 TypedDict + `BlobHandle` + `ErrorCode 4016/4017/4018` | `smcp.py` | — | type/feature, priority/high, area/protocol |
| ② | 通用二进制传输 | Computer 句柄铸造/解析（§4.3，无 MAC 单层 msgpack）+ `utils/blob.drain_blob` 单一例程（串行 + `concurrency>1` 并行，§4.5）| `socketio/client.py`, `utils/blob.py` | ① | type/feature, priority/high, area/blob-transfer |
| ③ | Computer SKILL 子系统 | `computer/skills/`（home/naming/registry/staging/intent/sandbox 三源含 marketplace）+ `computer.py` `_on_manager_change` 扩展 + `manager.py` `skill://` 枚举 | `computer/skills/*`, `computer.py`, `mcp_clients/manager.py` | ① | type/feature, priority/high, area/skill, area/marketplace |
| ④ | Agent 消费 + tool_call 二进制一致性 | `_request_builders` 三 builder + base/client/sync_client 消费分支 + tool_call content 扫描 + notify:update_skills 重拉 | `agent/_request_builders.py`, `agent/base.py`, `agent/client.py`, `agent/sync_client.py`, `socketio/client.py`(tool_call 铸造) | ①② | type/feature, priority/high, area/skill, area/sync-mirror |
| ⑤ | Server 路由 + 广播 | 三 `client:*` 路由 + `server:update_skills`→`notify:update_skills` 广播（async+sync） | `server/namespace.py`, `server/sync_namespace.py` | ① | type/feature, priority/medium, area/protocol, area/sync-mirror |

②③ 可并行（均依赖 ①）；④ 依赖 ①②；⑤ 依赖 ①。每子 PR 自带 async+sync 镜像与对应测试。

---

## 8. 验收清单（对齐工单 §6）

- [ ] `smcp.py`：5 事件常量 + 7 TypedDict + `BlobHandle` + `ErrorCode 4016/4017/4018`，注释标注协议出处
- [ ] `computer/skills/`：home + naming + registry + staging + intent + sandbox（mcp / **marketplace git** / user 三源）
- [ ] `computer.py` `_on_manager_change` 扩展 `skill://` + 非 MCP 源探测 → `server:update_skills`
- [ ] `socketio/client.py`：get_skills/get_skill/get_blob 处理 + tool_call 二进制旁路 + emit_update_skills
- [ ] `utils/blob.py` `drain_blob` 单一例程（默认串行 + `concurrency>1` 并行 + 错误协调矩阵），get_skill 与 tool_call 共用；docstring 明示「并行安全」
- [ ] `agent/_request_builders.py` 三 builder；`agent/{base,client,sync_client}.py` 消费分支 + tool_call 扫描 + notify:update_skills 重拉
- [ ] `server/{namespace,sync_namespace}.py` 路由 + 广播（显式 raise 隔离）
- [ ] 安全反例测试全部红→绿（沙箱/`.skillenv`/句柄不信任/`too_large`/staging 隔离/name 寻址）
- [ ] `uv run poe lint` 净；`uv run poe test` 零回归；`PROTOCOL_VERSION` 未改动

---

## 9. 风险与依赖（对齐工单 §8）

- **marketplace git 源**：网络拉取/缓存/对账失败降级（记 ERROR、不阻断其余 source、不向 Agent 硬报错）；首拉成本与对账策略见 §2.2（无 TTL，后台定时后置 backlog）。
- **staging 目录隔离**：MUST NOT 跨用户共享；多用户每用户独立 home；覆盖路径仍校验非系统目录（§2.3）。
- **底层 mcp 包**：沿用 0.2 锚定（`mcp >= 1.15.0`），`Resource._meta`/`annotations` 须可用（启动自检 fail-fast）。
- **与 PR #33 无代码冲突**：本工单不碰 `server/middleware.py`/`utils/handshake.py`/`version.py`；无强依赖。

---

## 参考

- 协议：`a2c-smcp-protocol` `docs/specification/{skill,blob-transfer,events,data-structures,error-handling}.md`、`docs/migrations/v0.2.1-skill-and-blob-transfer.md`
- 工单：[`docs/upgrade-0.2.1-skill-blob-transfer.md`](upgrade-0.2.1-skill-blob-transfer.md)
- 本地管理范本：Claude Code `MARKETPLACE_PLUGIN_OPERATIONS.md`（外部参考，意图/物化两层 + reconciler + eager add + 无 TTL）
- 实现范本（v0.2 `get_resources` 全链路）：`smcp.py:413` / `socketio/client.py:398` / `mcp_clients/manager.py:455` / `server/namespace.py:459` / `agent/client.py:357` / `agent/base.py:119` / `agent/errors.py:44`
- 追踪范式：已关闭 Milestone #1「v0.2 协议升级」+ parent tracking issue #8
