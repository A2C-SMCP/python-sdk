# 设计文档：0.2.1 — CLI Marketplace/Plugin/Skill 管理 UX

> **性质**：python-sdk 的 **CLI 表面**设计（如何让用户在 `a2c-computer` 上管理 marketplace/plugin/skill）。**不是协议规范**，也不是内部模型规范（后者归 [`design-0.2.1-skill-computer-management.md`](design-0.2.1-skill-computer-management.md)）。
> **范本**：Claude Code 的 marketplace/plugin/skill 三层模型 + 意图/物化两层 + reconciler；本文档逐项对齐，仅在 A2C 分布式架构（Agent ↔ Server ↔ Computer 三进程）下调整必要的事件机制。
> **追踪**：GitHub Issue [#39](https://github.com/A2C-SMCP/python-sdk/issues/39) 拓展范围。原 #39 仅落 `computer/skills/` 内部六模块；本设计补 CLI 命令/REPL/settings.json/reconciler 表面 UX。
> **关联文档**：
> - [`design-0.2.1-skill-computer-management.md`](design-0.2.1-skill-computer-management.md) — 内部 SkillRegistry/Staging/Sandbox 模型
> - [`upgrade-0.2.1-skill-blob-transfer.md`](upgrade-0.2.1-skill-blob-transfer.md) — 开发工单
> - `a2c-smcp-protocol/docs/specification/skill.md` — 协议约束

---

## 0. 核心决策摘要（先看这张表，再读全文）

| # | 决策项 | 选项 | 备注 |
|---|---|---|---|
| 1 | Namespace 层级 vs 可见名 | **结构** 3 层（marketplace→plugin→skill）；**Agent 可见 name** 2 段 `<plugin>:<skill>`（mp 名走 `source`，§2.1） | 对齐 CC + 协议 0.2.2 |
| 2 | 命令风格 | **裸词**（不加 `/` 前缀） | A2C Computer CLI 是纯管理工具、不混自然语言 |
| 3 | Marketplace 模型 | 1 git repo = 1 marketplace = N plugin = N skill | 仓库根需 `.tfrobot-plugin/marketplace.json` |
| 4 | Manifest 路径 | `.tfrobot-plugin/marketplace.json` + 每 plugin 下 `plugin.json` | "claude" → "tfrobot" 命名 |
| 5 | Install 语义 | Eager clone + **显式 install** | 与 CC 一致，与 #39 设计文档 §2.2 `auto_update` 解耦 |
| 6 | Enable 颗粒度 | **仅 plugin 层**（一开一关，统辖其下**全部贡献**：所有 skill **+ 其携带的 MCP server config**） | CC 风格：disable = 整 plugin 下线（skills 隐藏 + bundled MCP server 停并摘除）；与 §7.1 步骤 5 对齐 |
| 7 | Plugin 可携带 | skills + MCP server config | 不含 hooks/commands/agents/outputStyles |
| 8 | 三源命名（协议 0.2.2 定稿） | marketplace: `<plugin>:<skill>`（2 段，**无 mp 前缀**）；mcp: `mcp:<server>:<skill>`（3 段）；user: `<skill>`（裸名 1 段） | 段数消歧；mp 溯源走 `A2CSkillRef.source`；冲突 install 层拦截（§2.1/§2.4） |
| 9 | Trust 层 | **CC 风格**：首次 add `y/N`/`--trust`；信任由 settings.json policy 字段（`strictKnownMarketplaces`/`trustedMarketplaces`/`blockedMarketplaces`）load 时**计算**，**不**落物化文件 | 校正：CC 不在 known_marketplaces.json 存 trusted（§6.1） |
| 10 | scope 分层 | user / project / local / flag / policy 五级（CC 完整对齐） | first-source-wins for policy |
| 11 | 自发现路径 | 监控根（递归）：`$A2C_SKILL_HOME/user/`、**全部已登记工作目录** `<workdir>/.tfrobot/skills/`（skill 属能力层、跨全部登记目录全局并集、不随 active 切换）；发现单元：`<root>/<skill>/SKILL.md`（根下一级）；MCP 走 `_on_manager_change` 的 `skill://` | **不监** `marketplace/` clone 树（走显式 refresh，CC 同；详见 §5.0/§8.3） |
| 12 | Git 实现 | `subprocess` 调 `git` CLI（SSH→HTTPS fallback、`GIT_TERMINAL_PROMPT=0`） | |
| 13 | 进度反馈 | Rich 进度条 + 错误表格汇总 | 多 marketplace 批操作友好 |
| 14 | Banner UX | 仅 `plugins=0 AND servers=0` 时出 | marketplace 数量不计入 |
| 15 | Tab 补全 | 静态语法树 + 动态名称（mp/plugin/skill/server）+ 文件路径 `@<file>` | prompt_toolkit Completer |
| 16 | Help 组织 | 按命名空间分组、默认列 namespace 列表、`help <ns>` 才详情 | git/kubectl 风格 |
| 17 | emit_update_skills 节奏 | 清缓存 + **debounce 300ms** + 单次 emit；watcher 文件变更同节奏 | 借鉴 CC `clearAllCaches` + `resetSentSkillNames` |
| 18 | MCP server 冲突 | 用原名；外来同名 → **硬抛**（无 rename/force-override 逃生口，name 即身份）；冲突判定排除 plugin 自有 server | install=抛错，reconciler=跳过+WARN（§7.2/§10.6） |
| 19 | Uninstall 级联 | 默认 stop+remove plugin 携带的 MCP server；`--keep-servers` 跳过 | |
| 20 | settings.json 职责 | **只**放 `enabledPlugins` + `extraKnownMarketplaces` + MCP 门控字段 + trust policy 字段；**不放** MCP server 定义/inputs | 校正：MCP defs 移出 settings.json（见 #25） |
| 21 | Reconciler | **additive-only 只增不删**（true-CC）；孤儿靠显式 `plugin uninstall` / `plugin gc` 清 | 校正：CC 不自动清理「声明没、物化有」 |
| 22 | 老 flag 迁移 | settings.json 与 `--config/--inputs` **并存**；启动合并，settings 优先 | 不破坏老脚本 |
| 23 | JSON 输出 | 启动时**全局** `--json` flag | REPL 内也默认 JSON |
| 24 | 非交互形态 | Typer 子命令 + REPL 同名同义 | `a2c-computer marketplace add ...` 与 REPL `marketplace add ...` 走同一逻辑 |
| 25 | MCP server 定义文件 | A2C **原生 schema**（`{servers,inputs}`）→ workspace/project `.tfrobot/mcp.json` + user `$XDG_CONFIG_HOME/a2c/mcp.json` | `server_parameters` 嵌套 + 治理字段与标准 `.mcp.json` 不兼容，**不同名混用**（§9.1） |
| 26 | MCP 批准门控 | **全套 CC**：首见未知 server 弹批准框；`enableAllProjectMcpServers`/`enabledMcpjsonServers`/`disabledMcpjsonServers`；批准状态写 **local scope** | MCP 执行任意命令，每用户先批准（§9.2） |
| 27 | inputs/env/secret | **完整对标 VS Code**：`${env:VAR}` + `${input:id}` + 预定义变量；`password:true` 走 **OS keyring**（`keyring` 库）；env 注入 `A2C_INPUT_<ID>`；`envFile`；**密钥永不落明文** | §9.3，含 headless 降级 |
| 28 | scope 聚合 | **user 为主** + workspace **active workdir 单根**作 project/local（根随任务切换）+ **能力层**（`enabledPlugins`/`extraKnownMarketplaces`/skills）跨全部登记目录全局并集 | 访谈定稿；映射 CC `--add-dir`（详见 §5.0/§5.1） |

---

## 1. 范围

### 1.1 在范围

1. **CLI 命令表面**：marketplace / plugin / skill 三组命名空间命令（add/install/list/info/enable/disable/refresh/remove）+ 现有命令保持。
2. **REPL UX**：tab 补全、help 重组、Rich 进度反馈、Banner、zero-state 引导。
3. **意图层文件**：`settings.json` 格式 + 五级 scope 合并；与现有 `--config/--inputs` 共存。
4. **Reconciler**：启动时声明 vs 物化对账；自动 clone 缺失、自动清理废弃。
5. **事件触发链**：CLI 动作 → 缓存失效 → debounce 300ms → emit_update_skills；与文件 watcher 同节奏。
6. **Trust 流程**：首次 add 弹 y/N；持久化进 `known_marketplaces.json`。

### 1.2 不在范围

- **协议层改动**：`smcp.py` TypedDict / event 常量不变（v0.2.1 已锁定）。
- **SkillRegistry / Staging / Sandbox 内部实现**：归 `design-0.2.1-skill-computer-management.md`。
- **SKILL.md frontmatter 完整字段集**：归 SKILL 协议 v1——**恰好 6 字段**（`name`/`description`/`license`/`compatibility`/`metadata`/`allowed-tools`，全部来自 Agent Skills 开放标准）。**无 `version`、无 `when_to_use`**：`when_to_use` 折进 `description`（"做什么+何时用"），`version` 取自 plugin.json/marketplace entry。
- **远端 marketplace 索引服务**：A2C 暂不维护中心化目录；marketplace 只通过 git URL 添加。
- **后台定时 reconciler 守护进程**：与 `design-0.2.1-skill-computer-management.md` §2.2 一致，显式后置 backlog。
- **Plugin hook 脚本（lifecycle script）**：v0.2.1 不实现；plugin 仅声明 skill 与 MCP server config。

---

## 2. 命名空间与三源映射

### 2.1 两段命名（marketplace 源）

`<plugin>:<skill>`（**2 段，marketplace 名不进可见 ID**——已由 a2c-smcp 协议 0.2.2 `skill.md §1` 定稿）

- `<plugin>`：来自 marketplace.json `plugins[].name` 字段（**优先**），等同 plugin 子目录 `plugin.json.name`。冲突时 entry.name 胜出（CC `pluginLoader.ts:2437`）。
- `<skill>`：plugin 内 `skills/<skill-name>/SKILL.md` 父目录的 basename；frontmatter `name:` 仅作显示名、不改 ID。
- **marketplace 溯源不进 `name`**：完整 marketplace 归属由 `A2CSkillRef.source = "marketplace:<repo>"` 独立承载；跨 marketplace 同名 `<plugin>` 的冲突在 **install 层** `<plugin>@<marketplace>` 拦截（CC 同），不靠 name 区分。

**强制铁律**：ID 完全由路径推导，frontmatter 不可覆盖（防伪）。与 CC `loadPluginCommands.ts:726` 一致。

### 2.2 mcp 源（双段名）

`mcp:<mcp_server_name>:<skill_name>`

- `<mcp_server_name>`：MCP server 在 Computer 配置中的 name（用户在 `server add` 时指定）。
- `<skill_name>`：MCP server 通过 `skill://` resource 暴露的资源 basename。

**不引入 plugin 中间层**：`mcp:` 源不经过 marketplace/plugin 包装；MCP server 自身即是 SKILL 容器。

### 2.3 user 源（单段名）

直接 `<skill_name>`，无前缀。

- 路径来源（**所有 DropIn 都生效**，按优先级合并；完整布局见 §5.0）：
  1. `$A2C_SKILL_HOME/user/<skill_name>/SKILL.md`（全局个人 SKILL）
  2. **全部已登记工作目录** `<workdir>/.tfrobot/skills/<skill_name>/SKILL.md`（项目本地 SKILL，可入 git）——skill 属**能力发现层**，跨全部登记工作目录**全局并集**（与 `enabledPlugins`/`extraKnownMarketplaces` 同层、置最低优先级、**不随 active workdir 切换**，见 §5.0/§5.1）。
- 同名冲突：workspace skill 覆盖 user；登记工作目录间同名 → 按登记顺序后者覆盖 + WARN（§5.4）。

### 2.4 lexer 规则（a2c-smcp 协议 0.2.2 `skill.md §1` 定稿）

`:` 已被协议正式承认为合法分隔符。name 按**段数消歧**（1/2/3）：

```
skill-name       = user-name | marketplace-name | mcp-name
user-name        = skill                       ; 1 段（裸名，无 ":"）
marketplace-name = plugin ":" skill            ; 2 段
mcp-name         = "mcp" ":" server ":" skill  ; 3 段

kebab  = [a-z0-9]+(-[a-z0-9]+)*    ; leaf（skill/plugin）严格 kebab，1–64；天然排除首尾 "-" 与连续 "--"
server = [A-Za-z0-9_-]{1,64}        ; mcp 源 server 段，§1.3 规范化后（保留大小写+下划线）
```

逐形态正则（各加 1–64 长度约束）：

| 形态 | 正则 |
|---|---|
| user | `^[a-z0-9]+(-[a-z0-9]+)*$` |
| marketplace | `^[a-z0-9]+(-[a-z0-9]+)*:[a-z0-9]+(-[a-z0-9]+)*$` |
| mcp | `^mcp:[A-Za-z0-9_-]{1,64}:[a-z0-9]+(-[a-z0-9]+)*$` |

**`4016 SKILL_NAME_INVALID` 触发边界**（仅用于 `get_skill` 入参校验）：
- 段数 ∉ {1, 2, 3}
- 3 段但首段 ≠ 字面 `mcp`（3 段为 mcp 专属）
- 任一段不符上述字符集（leaf 须严格 kebab）

> ⚠️ **lexer 必守**：user 裸名是合法 **1 段、无 `:`** —— **不得**因"缺 `:`/缺 source 前缀"报 4016（协议明令删除了该错误判据）。
>
> ⚠️ **字符集不全收紧**（纠正本设计早期 Fork②"严格"误判）：只有 **leaf（skill/plugin）严格 kebab**；**mcp 源 `<server>` 段保持宽松 `[A-Za-z0-9_-]`**（镜像 CC `normalizeNameForMCP`，归一化 `re.sub(r'[^a-zA-Z0-9_-]', '_', name)` 保留大小写+下划线）。收紧 server 段反而偏离 CC（`GitHub_MCP`→`github-mcp` 制造新分叉）、扩大碰撞面（`My_Api`/`my-api` 互撞 → §1.5 拒绝第二个 → 技能对 Agent 静默消失）。server 段规范化后长度=0 或 >64 → §1.5 **拒绝注册**（非 4016）。
>
> **naming.py 待改**：合成裸名——marketplace `<plugin>:<skill>`、user 裸 `<skill>`（**不再**拼 `marketplace:`/`user:` 前缀）；mcp `mcp:<server>:<skill>` 不变；server 段归一化字符集/大小写**契约不变**。

---

## 3. 仓库布局与 Manifest

### 3.1 完整布局

> 布局**对齐 tfrobot-marketplace 协议 v1 §2.1**（决策：优先 CC 互操作）。plugin 默认聚集在 `plugins/<name>/`（可被 `marketplace.json` 的 `metadata.pluginRoot` 覆写）；plugin manifest 在**嵌套** `<plugin>/.tfrobot-plugin/plugin.json`（镜像 CC `.claude-plugin/`，作者从 CC 复制只需改目录名）；plugin 携带的 MCP server 走 **`mcp-servers/<name>.json` 文件式**（非内嵌 plugin.json，§3.3）。

```
my-team-skills/                            ← git repo root = Marketplace
├── .tfrobot-plugin/
│   └── marketplace.json                   ← 仓库级 manifest（必需）
└── plugins/                               ← 默认聚集目录（可被 metadata.pluginRoot 覆写）
    ├── frontend-design/                   ← plugin（name = "frontend-design"）
    │   ├── .tfrobot-plugin/
    │   │   └── plugin.json                ← plugin manifest（strict=true 时必需，嵌套）
    │   ├── skills/                         ← SKILL 子树（SKILL 协议 §2）
    │   │   ├── figma/
    │   │   │   ├── SKILL.md                ← 强制大小写
    │   │   │   ├── references/api.md       ← 附属资源，按需 rel_path 拉
    │   │   │   ├── scripts/bootstrap.sh
    │   │   │   └── .skillenv               ← 可选（SKILL 协议 §5，归协议；见 §9.3 边界）
    │   │   └── tailwind/SKILL.md
    │   └── mcp-servers/                    ← MCP Server 子树（文件式，mcp-servers 协议 §1）
    │       ├── figma-mcp.json              ← 单个 server 配置；文件名 = 配置内 name
    │       └── inputs.json                 ← 可选，plugin 范围占位符输入定义
    └── backend-helpers/
        ├── .tfrobot-plugin/plugin.json
        └── skills/pg-explain/SKILL.md
```

- **plugin 内容下限**：至少 `skills/` 或 `mcp-servers/` 之一非空；两者皆空 = 空载，加载器 WARN。
- **名称字符集**（marketplace/plugin/skill/server）：严格 kebab `[a-z0-9-]`（注：mcp 源运行时 server 段宽松，见 §2.4——但 marketplace 发布的 `mcp-servers/<name>.json` 文件名仍须 kebab）。

### 3.2 `.tfrobot-plugin/marketplace.json`

字段**对齐 tfrobot-marketplace 协议 §3**（必填 `name`/**`owner`**/`plugins`）：

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "my-team-skills",
  "owner": { "name": "Team X", "email": "team@example.com" },
  "description": "Internal SKILL bundle",
  "metadata": { "pluginRoot": "./plugins" },
  "plugins": [
    {
      "name": "frontend-design",
      "source": "frontend-design",
      "description": "Frontend design assistants",
      "version": "1.2.0",
      "author": { "name": "Team X" },
      "category": "design",
      "tags": ["figma", "tailwind"],
      "strict": true
    },
    {
      "name": "auth-tools",
      "source": { "source": "cnb", "repo": "turingfocus/tfs-auth-plugin", "ref": "v0.3.1" }
    }
  ]
}
```

- `name`：marketplace ID（kebab，无空格）；安装时显示 `<plugin>@<name>`。**不进 skill 可见 ID**（§2.1），仅在 catalog/安装语法 + `A2CSkillRef.source` 出现。**保留名**：`tfrobot-`/`turingfocus-`/`tfs-` 前缀官方保留。
- `owner`（**必填，对象** `{name, email?}`）：marketplace 维护者——校正：marketplace 级用 `owner`（catalog 维护者），plugin 级才用 `author`（代码作者）。
- `metadata.pluginRoot`：plugin source 基准目录前缀（默认让 `"source":"frontend-design"` 等价 `"./plugins/frontend-design"`）。
- `plugins[].source`：**Plugin source schema**（tfrobot-marketplace 协议 §5，5 类）——相对路径 `"./x"` / `{source:"url"|"github"|"git-subdir"|"cnb",...}`。`github`/`cnb` 是简写糖（归一化到 github.com / cnb.cool）；`git-subdir` 用于 monorepo 子目录（**v1 支持**，`git clone --filter=tree:0 --sparse-checkout=<path>`，curator 模式，见 §14 WBS C）。注意 marketplace source（`extraKnownMarketplaces`，§5.3）与 plugin source 是**两套独立 schema**（disjoint union，协议 §5.3）：marketplace 用 `url`/`github`/`git`/`cnb`，plugin 用 `相对路径`/`url`/`github`/`git-subdir`/`cnb`。
- `plugins[].strict`（默认 `true`，§3.4）。`category`/`tags`：纯发现元数据。
- 未知字段静默丢弃（CC `schemas.ts:1248`，爆炸半径 = 0）。

### 3.3 `<plugin>/.tfrobot-plugin/plugin.json` + `mcp-servers/` 文件

> **校正**：plugin 携带的 MCP server **不内嵌 plugin.json**——tfrobot-marketplace 协议（mcp-servers §8）明确"MCP 配置内嵌 plugin.json"为 v1 不引入项，理由是"文件式与 SKILL 子树设计对齐 + 避免 JSON 嵌套膨胀"。我方早期内嵌 `mcp_servers`/`inputs` 的设计**作废**。

`plugin.json`（路径 `<plugin>/.tfrobot-plugin/plugin.json`，仅元数据，**不含组件内联定义**）：

```json
{
  "name": "frontend-design",
  "version": "1.2.0",
  "description": "Figma + Tailwind helpers",
  "author": { "name": "Team X" },
  "license": "MIT",
  "keywords": ["figma", "tailwind"]
}
```

MCP server 走 **`<plugin>/mcp-servers/<name>.json`** 文件式（mcp-servers 协议 §1/§2）：
- 每个 server 一份独立 JSON，**文件名（去 `.json`）必须 = 配置内 `name`**。
- 结构 = A2C-SMCP `MCPServerConfig`（`type`/`server_parameters`/`forbidden_tools`/`tool_meta`/`vrl` 等，§9.1 原生 schema）。
- `<plugin>/mcp-servers/inputs.json`（可选）：占位符输入定义数组，**plugin 范围共享**（v1 不跨 plugin），被 server 配置的 `${input:<id>}` 引用。
- install plugin 时：枚举 `mcp-servers/*.json`（除 inputs.json）→ 注册进 Computer MCP 管理器（经 §9.2 批准门控 + §10.6 同名冲突硬抛）；inputs.json 合进 inputs 池（**消歧策略待定，见 §15 D2**）。

- `skills`：默认扫 `<plugin>/skills/<name>/SKILL.md`（约定，SKILL 协议 §2）；plugin.json/marketplace entry 可追加路径（strict 语义 §3.4）。
- v0.2.1 **消费** `skills/` + `mcp-servers/`；CC 私有组件（`commands/`/`agents/`/`hooks/`/`.mcp.json`/`lspServers`/`bin/` 等）**识别但忽略不报错**（协议 §6.3，保证两端互操作）。
- **plugin.json 缺失兜底**（strict=true，loading-behavior §2）：`name` 取 marketplace entry，**不**从目录名兜底；组件**不按约定猜**（`skills/`/`mcp-servers/` 约定扫描是 SKILL/mcp 协议明许的例外）。JSON 解析失败 = 致命错误，不降级。

### 3.4 Strict 字段语义（CC 对齐）

| 场景 | 行为 |
|---|---|
| 只有 `<plugin>/skills/` 约定目录 | 自动发现，最常见 |
| `plugin.json` 写了 `skills: [...]` | 追加扫描，在约定目录之外 |
| marketplace entry 写了 `skills` + `plugin.json` 存在 + `strict=true`（默认） | entry 的路径追加进 `plugin.skillsPaths` |
| marketplace entry 写了 `skills` + `plugin.json` 存在 + `strict=false` | **硬错误**：`conflicting manifests` |
| marketplace entry 写了 `skills` + 无 `plugin.json` | entry 就是 manifest，entry.skills 是唯一来源 |

### 3.5 Plugin manifest 字段归属表

| 字段 | plugin.json | marketplace entry | 谁赢 |
|---|---|---|---|
| `name` | ✅ | ✅（必填，作 ID） | entry.name 用作 plugin ID；plugin.json 的 name 加载后设到 displayName |
| `description` / `version` / `author` / `homepage` / `license` | ✅ | ✅（可选） | plugin.json 胜；plugin.json 缺失才用 entry |
| `skills` / `mcpServers`（**组件路径**，非内联配置） | ✅ | ✅ | strict=true：追加合并；strict=false：禁止并存。实际 MCP 配置在 `mcp-servers/<name>.json` 文件（§3.3） |
| `category` / `tags` | ❌ | ✅ entry 独有 | 纯浏览/搜索元数据 |
| `strict` | ❌ | ✅ entry 独有 | 控制合并模式 |
| `source` | ❌ | ✅ entry 独有 | 告诉 CLI 从哪取这个 plugin |

---

## 4. CLI 命令完整动词集

### 4.1 命令分组（裸词，无 `/` 前缀）

```
[server]      add | rm | start | stop                       (现有)
[inputs]      load | add | update | rm | get | list         (现有)
              value <list|get|set|rm|clear>                  (现有)
[marketplace] add | list | info | remove | refresh | set    (新)
[plugin]      install | uninstall | enable | disable        (新)
              list | info                                    (新)
[skill]       list | info                                    (新)
[socket]      connect | join | leave                         (现有)
[notify]      update                                         (现有)
[settings]    show | edit | get | set                        (新)
[utility]     tools | desktop | mcp | render | tc | history | help | quit | exit  (现有)
```

### 4.2 Marketplace 命令

| 命令 | 行为 |
|---|---|
| `marketplace add <git-url> [--name N] [--trust] [--auto-update] [--no-clone]` | 添加新 marketplace。首次需 `y/N` 确认 trust；`--trust` 跳过。默认 eager clone。`--no-clone` 仅注册意图（不推荐，与 #39 §2.2 eager add 决策冲突，仅 debug 用）。冲突 name → 报错。 |
| `marketplace list [--json]` | 列出所有已知 marketplace（trusted/clone 状态、上次刷新时间、auto_update 旗）。 |
| `marketplace info <name>` | 详情：URL、clone 路径、commit SHA、plugins[] 列表（含 installed 状态）、auto_update、trusted、上次刷新。 |
| `marketplace remove <name> [--keep-plugins]` | 移除 marketplace。**默认级联**：卸载其下所有 installed plugin（含其携带的 MCP server）。`--keep-plugins` 保留 installed 状态但标记为 orphaned（Registry 仍可解析但 marketplace clone 树被删）。需 `y/N` 确认。 |
| `marketplace refresh [<name>|all]` | git pull 失败则全量重 clone；与缓存 plugin 集合对账；emit_update_skills。Rich 进度条 + 失败汇总表。 |
| `marketplace set <name> auto-update=<bool>` | 设置 per-source `auto_update` 旗。 |

### 4.3 Plugin 命令

| 命令 | 行为 |
|---|---|
| `plugin install <plugin>@<marketplace> [--version <v>]` | 安装单个 plugin。检查 MCP server 名冲突：bundled server name 已存在**且不归属本 plugin**（不在其 `bundledMcpServers` 记录里）→ **硬抛、原子失败、不留半装状态**（无 rename/force 逃生口）。`--version` 锁版本（暂用 git tag/SHA，v0.2.1 默认 latest）。 |
| `plugin uninstall <plugin>@<marketplace> [--keep-servers]` | 卸载。默认 stop+remove plugin 携带的 MCP server；`--keep-servers` 保留 MCP server config。 |
| `plugin enable <plugin>@<marketplace>` | 写 `enabledPlugins[<id>] = true`；emit_update_skills。 |
| `plugin disable <plugin>@<marketplace>` | 写 `enabledPlugins[<id>] = false`；**停掉并从生效 MCP 定义层摘除该 plugin 携带的 MCP server**（与 §7.1 步骤 5「禁用 plugin 不合并其 mcp_servers」对齐——禁用 = 整 plugin 贡献下线）；**物化层保留**（clone 树 + installed_plugins.json 记录不动），`enable` 可廉价复原（重新挂载 server + 暴露 skill），无需重 clone/重装；emit_update_skills（server 停所引发的 tools 变更经 `server:update_tool_list` 同步广播）。区别于 `uninstall`：disable 留 installed 记录、可一键回滚；uninstall 删 installed 记录、移除 server config。 |
| `plugin list [--available] [--json]` | 默认列 installed enabled；`--available` 含 installed disabled + cloned-but-not-installed。 |
| `plugin info <plugin>@<marketplace>` | 详情：version、commit SHA、install 路径、enabled 状态、skills[]、mcp_servers[]、inputs[]。 |

### 4.4 Skill 命令（跨源扁平视图）

| 命令 | 行为 |
|---|---|
| `skill list [--source mp\|mcp\|user] [--json]` | 跨源列出当前可见 SKILL（Agent 看到的集合）。展示 name、source、enabled、orphan 标志。 |
| `skill info <name>` | 详情：source、marketplace/plugin/server 归属、版本（取自 plugin/marketplace，非 frontmatter）、frontmatter（description/compatibility/allowed-tools）、SKILL.md 路径、附属资源大小。 |

> Skill 没有 `install/uninstall/enable/disable`——这些操作都通过 plugin 层（marketplace 源）或 server add/rm（mcp 源）或文件操作（user 源）完成。CLI 只暴露 list/info 两个只读动词，避免与 plugin 层混淆。

### 4.5 Settings 命令

| 命令 | 行为 |
|---|---|
| `settings show [--scope user\|project\|local\|flag\|policy\|merged]` | 展示某 scope 的 settings.json 内容；默认 `merged`（合并后视图）。 |
| `settings edit [--scope user\|project\|local]` | 用 `$EDITOR` 打开该 scope 的 settings.json；保存后 reconcile。 |
| `settings get <key> [--scope ...]` | 读取单字段。 |
| `settings set <key> <value> [--scope user\|project\|local]` | 写单字段。policy/flag scope 只读。 |

### 4.6 Typer 非交互形态

所有 REPL 命令同时作为 `a2c-computer` 子命令暴露：

```bash
a2c-computer marketplace add git@github.com:team/skills.git --trust --auto-update
a2c-computer plugin install frontend-design@my-team-skills
a2c-computer plugin list --json
a2c-computer skill list --source mp --json
a2c-computer --json settings show --scope merged
```

非交互模式下：
- 不打 Rich 表格、不显示进度条（除非 `--progress`）。
- 默认 JSON 输出（即 `--json` 隐式 on）。
- 退出码：0 成功、1 用户错（参数错/冲突）、2 网络错、3 内部错。

---

## 5. 意图层 settings.json 与 Scope

### 5.0 本地目录布局与路径解析（总览）

> 本节是 §5（意图层 settings）、§6（物化文件）、§8.3（watcher 范围）的**路径地图**。`$A2C_SKILL_HOME` 的解析规则由姊妹文档
> [`design-0.2.1-skill-computer-management.md`](design-0.2.1-skill-computer-management.md) §2.3 定稿，本文档**复用不改写**：
> 默认 `$XDG_DATA_HOME/a2c/skills` → 回退 `~/.a2c/skills`；env 覆盖键 `A2C_SKILL_HOME`；**MUST NOT 跨用户共享 / 不放系统目录**（启动 fail-fast 校验）。

```
# ── Skill Home（$A2C_SKILL_HOME，物化与 user DropIn 同栖）──────────────
$A2C_SKILL_HOME/                          # 默认 $XDG_DATA_HOME/a2c/skills → ~/.a2c/skills
├── user/                                 # user 源 DropIn（★ 自发现 + watcher）
│   └── <skill>/SKILL.md
├── marketplace/                          # marketplace 源 clone 树（物化，✗ 不自发现，靠显式 refresh/install）
│   └── <mp>/.tfrobot-plugin/marketplace.json + <plugin>/...
├── known_marketplaces.json               # 物化记录，CLI 维护勿手编（§6.1）
└── installed_plugins.json                # 物化记录（§6.2）

# ── user scope 意图（主）────────────────────────────────────────────
$XDG_CONFIG_HOME/a2c/                     # → ~/.config/a2c
├── settings.json                         # user scope 意图/治理（§5.1）
└── mcp.json                              # user scope MCP 定义（§9.1）

# ── workspace 登记的工作目录 <workdir>（active 充当 project/local 单根；全部登记目录共献能力层）──
<workdir>/.tfrobot/
├── settings.json                         # project scope（入 git）
├── settings.local.json                   # local scope（不入 git）
├── skills/<skill>/SKILL.md               # project/local DropIn（★ 自发现 + watcher）
├── mcp.json                              # project scope MCP 定义
└── mcp.local.json                        # local scope MCP 定义
```

**自发现路径策略**（★ = 被 watcher 实时监控的发现根；其余靠显式操作）：

| 源 | 发现路径 | 自发现 | 机制 |
|---|---|---|---|
| `user` | `$A2C_SKILL_HOME/user/` **+ 全部已登记 `<workdir>/.tfrobot/skills/`**（能力层全局，不随 active 切换） | ★ 是 | 文件 watcher，递归监控、过滤 `**/SKILL.md`（§8.3） |
| `marketplace` | `$A2C_SKILL_HOME/marketplace/<mp>/<plugin>/...` | ✗ 否 | clone 树，仅经 `marketplace add/refresh` / `plugin install` 变更（§8.3 三条理由） |
| `mcp` | 无本地路径（server 经 `skill://` resource 暴露） | ★ 是 | `manager` + `_on_manager_change`（无目录扫描） |

- **发现单元**：`<root>/<skill>/SKILL.md`（根下**一级**，name = 目录 basename，单段无前缀）；深于一级的 `SKILL.md` 忽略（§8.3）。
- **两层解析模型**（访谈定稿，取代旧「整份并集」；详见 §5.1 校正）：
  - **(A) 能力发现层 = workspace 全局、始终生效**：`enabledPlugins`/`extraKnownMarketplaces`/skill DropIn 跨**全部已登记工作目录**取并集（置最低优先级），让 Agent 能力面**稳定、不随 active 跳变**。所有 `<workdir>/.tfrobot/skills/` 都是 user 源发现根，watcher 逐目录注册。= CC `--add-dir` 聚合集（机制一字不差）。
  - **(B) active-workdir 单根层 = 随任务切换**：其余 project/local 键（trust policy / MCP 批准门控 / permissions / 全部标量）**只取 active workdir** 的 `.tfrobot/settings[.local].json`，**不跨目录并集** → 标量冲突天然消失、敏感键隔离。**无 active**（空闲/启动）时 project/local 全空，只用 user scope（+ A 层）。
- **同名优先级**：能力层并集（最低）< `user` < active-workdir `project/local`；能力层内**登记目录间同名** → 按登记顺序后者覆盖 + WARN（§5.4）。
- **钉死单根、不向上遍历**（照 CC，源码 `settings.ts` 把 root 硬钉为 `originalCwd`）：每个根仅取其**自身**的 `.tfrobot/`，**不**沿目录树向上合并祖先目录。CC 里只有 CLAUDE.md 类记忆文件才向上 walk（CLAUDE.md→文件系统根、skills/commands→git root、settings→不走，三套停止点各异）。A2C 的「多根」来自 **workspace 持久登记的工作目录集**（非目录树祖先遍历、非 ad-hoc `--add-dir`）；与 CC 的唯一两点适配：登记集**持久** + 主根**动态**（active 随任务切换）。

### 5.1 文件位置（按 scope 从低到高，high 覆盖 low）

> **A2C scope = user 为主 + active-workdir 单根 + 能力层全局**（访谈定稿，校正 #28）：A2C Computer 是常驻 daemon、并非 CC 那种"跑在单一 `$CWD`"。workspace **持久登记**多个工作目录；任一时刻**至多一个 active workdir**（绑定当前 Agent 任务/调用）。解析分两层——**(A) 能力发现层**（`enabledPlugins`/`extraKnownMarketplaces`/skills）跨**全部登记工作目录**取并集、置最低优先级（稳定能力面）；**(B) 其余 project/local**（trust/MCP 批准/permissions/标量）**只取 active workdir 单根**、不跨目录并集（CC 单根语义、根随任务切换）。**无 active** 时 project/local 全空、仅 user scope + A 层。映射 CC：active workdir ≙ CC 主 cwd；其余登记目录 ≙ CC `--add-dir`（仅能力三件套、最低优先级）。

| Scope（低→高） | 文件 / 来源 | 聚合范围 |
|---|---|---|
| 能力发现层（最低） | **全部已登记工作目录**的 `<workdir>/.tfrobot/settings[.local].json` | **仅** `enabledPlugins`+`extraKnownMarketplaces`（+ skills 走 DropIn）；跨全部登记目录并集（= CC `--add-dir`） |
| user（**主**） | `$XDG_CONFIG_HOME/a2c/settings.json` → fallback `~/.config/a2c/settings.json` | 全键 |
| project | **active workdir** 的 `<workdir>/.tfrobot/settings.json`（入 git、团队共享） | 全键、单根（不跨目录并集）；**无 active 时空** |
| local | **active workdir** 的 `<workdir>/.tfrobot/settings.local.json`（不入 git） | 同上 |
| flag | `--settings <file>` 启动参数指定 | 全键 |
| policy（最高） | 四子源（first-source-wins，**不合并**） | 全键 |

### 5.2 policy 子源（first-source-wins）

| 优先级 | 平台 | 位置 |
|---|---|---|
| 1（最高） | 全平台 | Remote managed endpoint `${A2C_BASE_API_URL}/api/computer/settings`（30 min poll；OAuth/team 关联）。**仅 v0.2.2+ 实现**；v0.2.1 不引入 |
| 2 | macOS | `/Library/Managed Preferences/com.a2c.computer.plist`（per-user）或全局变体 |
| 2 | Windows | 注册表 `HKLM\SOFTWARE\Policies\A2CComputer\Settings` |
| 2 | Linux | （无 OS 原生 MDM，跳过） |
| 3 | macOS | `/Library/Application Support/A2CComputer/managed-settings.json`（+ `managed-settings.d/*.json`） |
| 3 | Windows | `C:\Program Files\A2CComputer\managed-settings.json` |
| 3 | Linux | `/etc/a2c-computer/managed-settings.json` |
| 4（最低） | Windows | 注册表 `HKCU\SOFTWARE\Policies\A2CComputer\Settings` |

> **v0.2.1 范围**：仅实现层级 2/3/4 的本地文件路径读取；Remote managed endpoint 留 stub、不发起网络请求。Linux/macOS/Windows 三平台都至少能读 layer 3 文件。

### 5.3 字段 schema

> **校正（来自 CC 开发者问询）**：MCP server **定义**与 inputs **不在** settings.json（这点 CC 也是分离的——MCP defs 在独立 `.mcp.json`/`~/.claude.json`）。settings.json 只放**意图与治理**：plugin 启停、marketplace 声明、MCP 门控开关、trust policy。MCP 定义见 §9.1 `.tfrobot/mcp.json`。

```jsonc
{
  "$schema": "https://a2c-smcp.dev/schemas/computer-settings-0.2.1.json",
  // 注：settings.json 无 version 字段（复刻 CC：passthrough + 全可选，见 §5.6）

  // 声明：要哪些 marketplace
  "extraKnownMarketplaces": {
    "my-team-skills": {
      "source": { "type": "git", "url": "git@github.com:team/skills.git" },
      "autoUpdate": true
    }
  },

  // 声明：哪些 plugin 启用/禁用（缺键 = 未安装；true = 启用；false = 装但禁用）
  "enabledPlugins": {
    "frontend-design@my-team-skills": true,
    "deprecated-thing@my-team-skills": false
  },

  // Trust policy（CC 风格，load 时计算信任，不落物化文件）
  "strictKnownMarketplaces": false,                 // true = 白名单模式，仅 trustedMarketplaces 可用
  "trustedMarketplaces": ["my-team-skills"],        // 显式信任名单
  "blockedMarketplaces": ["sketchy-repo"],          // 黑名单

  // MCP 门控（CC 风格；server 定义在 .tfrobot/mcp.json，这里只控启用/批准）
  "enableAllProjectMcpServers": false,              // true = 自动批准本 workspace .tfrobot/mcp.json 全部 server
  "enabledMcpjsonServers": ["figma-mcp"],           // 已批准
  "disabledMcpjsonServers": ["sketchy-mcp"],        // 已拒绝
  "allowedMcpServers": [],                          // 企业级允许名单（policy scope）
  "deniedMcpServers": [],                           // 企业级拒绝名单（policy scope）

  // 杂项
  "permissions": {
    "additionalDirectories": ["/path/to/other"]
  }
}
```

### 5.3.1 字段规格（normative）

> 上面 jsonc 是示例；下表是**规范**。「可写 scope」= CLI `settings set` / 手编可落盘的 scope（flag/policy 经 CLI **只读**，§4.5）。合并语义见 §5.4，版本/校验见 §5.6。

| 字段 | 类型 | 可写 scope | 默认 | 约束 / 校验 | 合并 |
|---|---|---|---|---|---|
| `extraKnownMarketplaces` | `{ [name]: { source:{type:"git",url}, autoUpdate?:bool } }` | user/project/local | `{}` | `name` 唯一；git url 形态校验 | 对象递归深合并（嵌套 `source` 不整体替换） |
| `enabledPlugins` | `{ [<plugin>@<mp>]: bool }` | user/project/local | `{}` | key 形如 `plugin@marketplace` | 对象递归深合并（值为 bool，同 key 高 scope 赢） |
| `strictKnownMarketplaces` | `bool` | user/project/policy | `false` | 白名单模式开关 | 取最高 scope |
| `trustedMarketplaces` | `string[]` | user/project/local/policy | `[]` | 元素 = marketplace name | array 拼接去重 |
| `blockedMarketplaces` | `string[]` | user/project/policy | `[]` | 与 trusted 冲突时 **blocked 优先** | array 拼接去重 |
| `enableAllProjectMcpServers` | `bool` | user/project/local | `false` | 批准本 workspace 全部共享 server | 取最高 scope |
| `enabledMcpjsonServers` | `string[]` | local（批准写入处，#26） | `[]` | 元素 = server name | array 拼接去重 |
| `disabledMcpjsonServers` | `string[]` | local | `[]` | 与 enabled 冲突 **disabled 优先** | array 拼接去重 |
| `allowedMcpServers` | `string[]` | **policy only** | `[]` | 非 policy scope 出现 → 忽略+WARN（§5.6） | array 拼接去重 |
| `deniedMcpServers` | `string[]` | **policy only** | `[]` | 同上；企业级拒绝名单 | array 拼接去重 |
| `permissions.additionalDirectories` | `string[]` | user/project/local | `[]` | 绝对路径、**非**系统共享目录 | array 拼接去重 |

- `$schema`：允许出现、仅供编辑器补全/校验，CLI **不消费**。
- 「取最高 scope」标量字段不做合并，按 §5.1 优先级 high 覆盖 low。

### 5.4 合并规则（⚠️ 读/写两套语义相反，照 CC）

**读合并**（启动加载，多 scope 叠加，`settingsMergeCustomizer`）：
- **object/map 字段**（`enabledPlugins`/`extraKnownMarketplaces` 等）：**递归深合并**（lodash `mergeWith` 等价，照 CC `settingsMergeCustomizer`——customizer 只特判数组，其余交给默认递归合并）——逐层按 key 取并集，**只有同名叶子 key 才由高 scope 覆盖**；嵌套对象继续向下递归、**不整体替换**。
  - 例 1（叶子覆盖）：project `enabledPlugins["foo@mp"]=true`、local `=false` → **false**（local 赢）；project 另有 `bar@mp:true` 而 local 没提 → bar 保留。
  - 例 2（嵌套深合并，**与浅合并的关键区别**）：user `extraKnownMarketplaces["mp"]={source:…, autoUpdate:false}`、project 仅给 `{autoUpdate:true}` → 合并为 `{source:…, autoUpdate:true}`（**`source` 不丢**）；浅合并会把整个 `source` 替换掉。
- **scalar 字段**（`strictKnownMarketplaces`/`enableAllProjectMcpServers` 等布尔/标量）：高 scope 整体覆盖（取最高 scope）。
- **array 字段**（`enabledMcpjsonServers`/`trustedMarketplaces`/`permissions.additionalDirectories` 等）：**拼接去重** `uniq([...低, ...高])`（低 scope 在前）。
- policy scope 内部 first-source-wins（不合并子源；照 CC：remote > MDM > managed-settings.json[+.d/] > HKCU，只取最高且有内容者，再整体叠加进主链）。

**写回单 scope**（CLI 改某一个 settings 文件，语义**相反**）：
- **array 字段**：**直接替换**（不拼接）。
- **删字段**：写 `undefined`（= 删 key）。
- 写后 `markInternalWrite(<path>)` 给 file-watcher 打标，**避免把自己的写回当用户手编触发重载循环**（CC `settings.ts:~500` 同款机制）。

### 5.5 与现有 `--config @file` / `--inputs @file` 共存

老 flag 现在喂的是 **MCP 定义层**（§9.1），不是 settings.json：

```
MCP 定义合并顺序（高 → 低，§9.1 scope；同 §5.1 active-workdir 单根模型）：
  1. policy（managed-mcp.json）            ← 最高
  2. active workdir local (.tfrobot/mcp.local.json)
  3. active workdir project (.tfrobot/mcp.json)
  4. user ($XDG_CONFIG_HOME/a2c/mcp.json)
  5. --config / --inputs flag             ← 老接口，最低优先级
  6. 默认值

  注：MCP 定义层同构 settings 的 (B) 层——**只取 active workdir** 的 mcp.json/mcp.local.json，
  **不**跨登记目录并集（无 active 时仅 user + flag + 默认）；批准门控随之一致。
  与能力层（enabledPlugins/extraKnownMarketplaces/skills 全局并集）正交。

settings.json（意图/治理层）独立按 §5.1/§5.4 合并。
```

- `--config @servers.json` / `--inputs @inputs.json` 等价于在 MCP 定义层最低优先级注入；
- 老脚本零迁移即可继续工作；新场景推荐 `.tfrobot/mcp.json` + settings.json + GitOps。

### 5.6 校验与前向兼容（复刻 CC：passthrough + 全可选，无版本字段）

> 决策（CC 源码核实）：CC 的 settings **没有** version 字段、**无**迁移 runner——前向兼容完全靠「**全字段 `.optional()` + 顶层 `.passthrough()`**」的加法式纪律（CC `types.ts` schema 头注释明言「unknown fields preserved … invalid settings simply not used but remain in the file」）。A2C settings.json **复刻此模型**：人编文件不背版本负担；`version` 只留在 **CLI 维护的物化文件**（§6.1/§6.2）。

- **未知字段**：**静默保留**（passthrough，照 CC `SettingsSchema.passthrough()`）——未知顶层键不报错、不剥离、原样留在磁盘。`settings set` 是**单字段写**、不整体重写文件，未知键自然留存（升降级都不丢）。
- **类型/取值校验**：`settings/schema.py` 以 TypedDict 定结构 + Pydantic 运行时校验（scope 枚举、git url 形态、`<plugin>@<mp>` key 形态等）。**单个已知键**校验失败 → 该键**被过滤不用**（回退默认）但**仍留在文件**，错误收进 `ValidationError[]` 经 `settings show` / 诊断命令呈现（照 CC：错误不阻断启动、不整文件作废，类比 CC Doctor/status；非法权限/MCP 规则逐条过滤而非整文件作废）。
- **前向兼容靠加法**：新增字段一律 `Optional` + 给默认；**不**引入 `version` 协商、**不**写迁移逻辑（与 CC 一致）。语义变更走「新字段 + 旧字段降级读」的加法式演进。
- **scope 越权**：policy-only 字段（`allowedMcpServers`/`deniedMcpServers`）出现在非 policy scope → 过滤不用 + 记 `ValidationError`（杜绝用户态自我提权）。
- **vs 物化文件（§6.3）**：物化文件 CLI 维护、**带 `version`** + 损坏走 `.corrupt-<ts>.bak` 整文件降级；settings 是人编文件、**无 version** + 字段级容错（passthrough 保留、不备份、不整体清空）。

---

## 6. 物化层文件（CLI 自动维护，不可手编）

### 6.1 `$A2C_SKILL_HOME/known_marketplaces.json`

```jsonc
{
  "version": 1,
  "marketplaces": {
    "my-team-skills": {
      "source": { "type": "git", "url": "git@github.com:team/skills.git" },
      "installLocation": "/home/user/.local/share/a2c/skills/marketplace/my-team-skills",
      "lastUpdated": "2026-05-21T10:30:00Z",
      "commitSha": "abc1234",
      "autoUpdate": true
    }
  }
}
```

- key = marketplace.json 里的 `name`（不是用户输入的 alias）。
- `installLocation` 是绝对路径。
- **无 `trusted` 字段**（校正自 CC 问询）：信任不落物化文件，由 settings.json 的 `strictKnownMarketplaces`/`trustedMarketplaces`/`blockedMarketplaces` 在 load 时计算。首次 `marketplace add` 的 y/N 应答会写进**对应 scope 的 settings.json**（追加 `trustedMarketplaces`），而非这里。

### 6.2 `$A2C_SKILL_HOME/installed_plugins.json`

```jsonc
{
  "version": 1,
  "plugins": {
    "frontend-design@my-team-skills": [
      {
        "scope": "user",
        "installPath": "/home/user/.local/share/a2c/skills/marketplace/my-team-skills/frontend-design",
        "version": "1.2.0",
        "commitSha": "abc1234",
        "installedAt": "2026-05-10T...",
        "lastUpdated": "2026-05-10T...",
        "bundledMcpServers": ["figma-mcp"]
      }
    ]
  }
}
```

- `bundledMcpServers`：**A2C 扩展字段**（CC 的 installed_plugins.json **没有**此字段——CC 把 bundled MCP 存在 plugin 目录的 `.mcp.json` 里，uninstall 随 `deletePluginDataDir` 清）。A2C 为做 uninstall 级联（§4.3 决策 #19）显式记录该 plugin install 时注册的 MCP server name，用于精准清理。
- 数组化：scope 维度（`scope`+`projectPath` 精确匹配），为「user + workspace 工作目录同时装、不同版本」预留（对齐 CC V2 schema，实际已支持多 scope 并存；v0.2.1 常见为单元素）。
- 字段集对齐 CC：`scope`(managed|user|project|local) / `projectPath`(project/local 必填) / `installPath`(版本化路径) / `version` / `installedAt` / `lastUpdated` / `commitSha` + A2C 扩展 `bundledMcpServers`。

### 6.3 文件级写保护、原子写与损坏恢复

- 文件顶端注释：`// Maintained automatically by a2c-computer. DO NOT EDIT.`
- CLI 启动时若发现手编痕迹（schema 不匹配/未知字段）→ WARN + 用 in-memory 解析后续重写覆盖。
- **原子写（优于 CC 现状）**：写临时文件 + `fsync` + atomic `rename`。CC 这两个元数据文件实际用的是 `writeFileSync`（非原子、无锁、进程中途死会留半截 JSON——CC 开发者自评"反面教材"），A2C **不抄这条**，做成原子写。
- **并发**：A2C Computer 单用户单进程为常态（SKILL Home `MUST NOT` 跨用户共享，见 #39 设计 §2.3），仍加**文件锁**（`fcntl`/`msvcrt`）防同用户多实例撕裂；拿不到锁 → 退避重试 + WARN。
- **损坏恢复（优于 CC 现状）**：load 失败时先备份 `.corrupt-<ts>.bak` **再**降级为空配置（CC 是静默重置无备份，紧接 save 会永久覆盖损坏数据——A2C 留备份避免数据永久丢失）；marketplace 物化记录丢失靠下次 reconcile 按 settings 声明重装（plugin 安装记录无法自动重建，仅 WARN）。

---

## 7. Reconciler（启动对账）

### 7.1 启动流程

> **校正（CC 问询）**：reconciler 先把所有 scope 合并成**单一声明视图**再对账（不是逐 scope）；且为 **additive-only 只增不删**——CC 的 reconcile 返回值只有 `{installed, updated, failed, upToDate, skipped}`，**没有 removed/deleted**。"声明没有、物化有"的条目**完全不动**，绝不自动清理。好处：不会误删别的 scope 装的东西；代价：孤儿需显式清（§7.3）。

```
1. 读所有意图层 settings.json（policy → flag → local → project → user → addDir）
   ↓ 合并出单一声明视图
   - declaredMarketplaces: dict[name, source]
   - enabledPlugins:       dict[plugin_id, bool]
   （MCP 定义不在此层——见 §9.1，独立合并）
2. 读 known_marketplaces.json（物化）
3. reconcileMarketplaces()  ← additive-only:
   - declared∖materialized（missing） → git clone --depth 1（SSH→HTTPS fallback），写 known_marketplaces.json
   - source 变了（sourceChanged）     → 重 clone 覆盖
   - autoUpdate=true                  → git pull
   - materialized∖declared            → **不动**（不清理；孤儿留存，靠 §7.3 显式清）
4. 读 installed_plugins.json
5. 加载 plugin（按 enabledPlugins × installed_plugins.json 交集）
   - 启用的 plugin：注册 skills 进 SkillRegistry、合并其 mcp_servers 进 MCP 定义层（经 §9.2 门控）
   - 禁用的 plugin：跳过（物化不动）
6. 启动文件 watcher（详见 §8）
7. emit_update_skills（首次）
```

### 7.2 失败降级

- git clone/pull 失败：记 ERROR、不阻断其余 marketplace、该 marketplace 标记为 `lastError`、对 Agent 不可见（Registry 不入册）。
- **MCP server name 冲突（非对称处理）**：
  - **冲突判定**：bundled server name 已存在 **且** 该 server 不在「待装 plugin 的 `bundledMcpServers` 记录」里（即不是它自己上次装的）。重装/重启时 plugin 自有的 server 命中自己 → **不算冲突**（幂等再物化）。
  - **交互/CLI `plugin install`**：外来同名 → **硬抛**（`MCPServerNameConflictError`），原子失败、不留半装状态。用户自行解决（删/改自己的同名 server，或在自有 marketplace 仓库里改 plugin manifest 的 server name）。**不**提供 `--rename`/`--force-override`（类比「软件双开」：name 即身份，不给官方旁路；force-override 会静默毁用户配置，rename 会让 `mcp:<server>:*` 命名映射与 manifest 声明对不上）。
  - **reconciler 启动自动加载**：外来同名 → **跳过该 plugin 加载 + WARN + 留意图层 `enabled` 不动**（不能让一个冲突 plugin 拖垮整个启动；冲突消解后重启即恢复）。
- known_marketplaces.json 文件损坏：备份 `.corrupt-<ts>.bak` + 降级空配置 + 下次 reconcile 按 settings 声明重装（§6.3）。

### 7.3 显式 sync 与孤儿清理

- `/plugin sync` 或 `/marketplace refresh` 手动触发 reconcile（不重启进程；仍 additive-only）。
- **孤儿清理**（additive-only 的必要补充）：`plugin gc` / `marketplace prune` 列出"所有 scope 都不再声明"的孤儿 plugin/marketplace，`y/N` 确认后清理（含 clone 树 + installed_plugins.json 条目 + bundled MCP）。这是 CC additive-only 模型下唯一的删除入口（CC 的 `markPluginVersionOrphaned` + 显式 uninstall 对应物）。

---

## 8. 事件触发链与文件 Watcher

### 8.1 模型（借鉴 CC `clearAllCaches` 模式，适配分布式架构）

```
变更源                       缓存失效                            emit_update_skills（debounce 300ms）
─────────────────────────────────────────────────────────────────────────────────────────
[plugin install/uninstall    → SkillRegistry.invalidate()       → SocketIO emit
 plugin enable/disable         + _acollect_skill_refs cache     ↓
 marketplace add/refresh       + manager.list_skill_resources   server:update_skills
 marketplace remove]           cache 清                          ↓ (server 广播)
                                                                 notify:update_skills
[file watcher: user/project   → 同上 (debounce 300ms)            ↓ (Agent 自动重拉)
 SKILL.md change]                                                client:get_skills

[_on_manager_change ResourceListChanged → 同上 (现有 desktop 流程，扩展并行 skill:// 分支)
 ResourceUpdated(skill://)]
```

> **双广播**：上图是 **skill 维度**（emit_update_skills）。`plugin install/uninstall/enable/disable` 因统辖其携带的 MCP server config（决策 #6），同时会**起停 bundled MCP server**——server 起停引发的工具集变化经现有 `server:update_tool_list → notify:update_tool_list` 路径独立广播（与 skill emit 并行，互不替代）。即一次 `plugin disable` 触发**两条**通知：少了 skills（update_skills）+ 少了 tools（update_tool_list）。

### 8.2 Debounce 实现

```python
class SkillEventDebouncer:
    def __init__(self, computer, *, window_ms: int = 300):
        self._window_ms = window_ms
        self._task: asyncio.Task | None = None

    def mark_dirty(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._emit_after_delay())

    async def _emit_after_delay(self) -> None:
        await asyncio.sleep(self._window_ms / 1000)
        await self._invalidate_caches()
        await self._emit_update_skills()
```

- 任意 CLI 动作完成后调用 `mark_dirty()`。
- 文件 watcher 每次 fire 也调用 `mark_dirty()`。
- 多事件 300ms 内合并为一次 emit。

### 8.3 File watcher 范围

> **监控范围 ≠ 发现单元**（CC 同）。watcher 监的是**根目录、递归**子树（watchdog/chokidar 天然递归监子目录），过滤器为 `**/SKILL.md`；而 SKILL 的「发现单元」是 `<root>/<skill>/SKILL.md`（根下**一级**，name = 目录 basename，单段、无前缀）。深于一级的 `SKILL.md`（如 `<root>/a/b/SKILL.md`）→ 忽略 + DEBUG 日志（user/project 源走单段命名，不引入 `:` 嵌套）；同级附属资源（`reference/*.md`、`scripts/*` 等）非 `SKILL.md`，不计为 skill。

| 监控根（递归） | 发现单元 | 监控内容 | debounce |
|---|---|---|---|
| `$A2C_SKILL_HOME/user/` | `user/<skill>/SKILL.md` | 任意层 `SKILL.md` 增删改 | 300ms |
| **全部已登记工作目录** `<workdir>/.tfrobot/skills/`（能力层、逐目录注册递归 watcher、全局并集，§5.0） | `<workdir>/.tfrobot/skills/<skill>/SKILL.md` | 任意层 `SKILL.md` 增删改 | 300ms |
| `$A2C_SKILL_HOME/marketplace/<mp>/...` （clone 树） | — | **不监** | — |
| MCP `ResourceListChanged` / `ResourceUpdated(skill://)` | — | 现有 `_on_manager_change` 扩展（[`design-0.2.1-skill-computer-management.md`](design-0.2.1-skill-computer-management.md) §5.1） | 300ms |

**为什么 clone 树不上 watcher**（与 CC `getWatchablePaths()` 一致——CC 的 `~/.claude/plugins/cache/<mp>/<plugin>/<ver>/skills/` 也不在监控列表）：

1. **变更只经操作发生**：clone 树是 CLI 自有，唯一改动入口是 `marketplace add/refresh/remove` 与 `plugin install/uninstall/enable/disable`，这些操作自己已调 `mark_dirty()`/缓存失效（§8.1 第二条线，对应 CC `clearAllCaches()`），无需 watcher 重复探测。
2. **避免 git pull 雪崩**：一次 `git pull`/重 clone 会写入几十~上百文件，若监控 clone 树则每个文件触发 watcher——即便 300ms debounce 也会和「显式 refresh 末尾一次 emit」语义打架（变成假 emit 叠加）。
3. **守住意图/物化两层边界**：clone 树是物化产物，监控它等于让物化层自己产生「变更意图」，破坏 reconciler 的单向同步模型。

**已知副作用与开发期姿势**：手工 `cd` 进 `$A2C_SKILL_HOME/marketplace/<mp>/` 直接编辑 SKILL.md **不会被实时检测**，须等下次 `marketplace refresh` 或重启（CC 同样接受此取舍——clone 树不应被手编）。若需对某个 marketplace skill 做本地快速迭代，正确做法是把它 symlink/copy 到 `$CWD/.tfrobot/skills/`（被监控的 project 源）开发，定稿后回推 marketplace 仓库。

实现：Python 用 `watchdog` 库（跨平台 inotify/FSEvents/ReadDirectoryChangesW）；`PollingObserver` fallback 给不支持的 FS（如某些网络挂载 / 容器 overlayfs）。

### 8.4 进程内信号（仅 REPL UI）

- 单独 `asyncio.Event`（不走 Socket.IO）让 REPL prompt 状态栏刷新 plugin/skill 计数。
- 仅用于 UI；Agent 通信走 Socket.IO 路径。

---

## 9. MCP 定义层 / inputs / secret（对标 VS Code）

> 本节是决策 #25/#26/#27 的展开。settings.json（§5）只放意图/治理；MCP server 的**定义**与 inputs 在这里，**完整对标 VS Code** 的 `mcp.json` + input variables + SecretStorage 模型，但 schema 用 A2C 原生（§9.1 解释为何不能复用标准 `.mcp.json`）。

### 9.1 `.tfrobot/mcp.json` — MCP server 定义（A2C 原生 schema）

**为何不复用标准 `.mcp.json`**：A2C `MCPServerConfig` 与业界 `.mcp.json` 结构不兼容——连接参数包在 `server_parameters` 下（标准是扁平 `{command,args,env}`）、`type` 用 `"streamable"`（标准 `"http"`）、timeout 是 ISO8601 字符串、且含 `forbidden_tools`/`tool_meta`/`vrl`/`disabled` 治理字段。同名 `.mcp.json` 会被 CC/VS Code 抢着按各自 schema 解析 → 静默误解析，比换名更危险。故 **A2C 用原生 schema + 自有文件名**。

**文件位置（按 scope，对应 §5.1 active-workdir 单根模型——MCP 定义同构 settings (B) 层）**：

| Scope | 位置 | 备注 |
|---|---|---|
| user（**主**） | `$XDG_CONFIG_HOME/a2c/mcp.json` | daemon 常态主源 |
| project | **active workdir** `<workdir>/.tfrobot/mcp.json` | 入 git、团队共享；**单根、不跨目录并集**；无 active 时空 |
| local | **active workdir** `<workdir>/.tfrobot/mcp.local.json` | 不入 git；同上 |
| policy | managed 路径下 `managed-mcp.json` | 企业下发 |
| flag | `--config @file`（老接口，最低优先级） | 兼容 |

查找优先级 policy > active-local > active-project > user > flag（对齐 CC enterprise > local > project > user，A2C 主根随任务动态切换）。**无 active workdir** 时只取 user + flag + 默认；MCP server 定义**不**像能力层那样跨登记目录并集（敏感面隔离，与 §5.1 (B) 一致）。

**schema（A2C 原生 `GetComputerConfigRet` 形状 + VS Code 风格扩展字段）**：

```jsonc
{
  "servers": {
    "figma-mcp": {
      "type": "stdio",
      "server_parameters": {
        "command": "node",
        "args": ["./figma-server.js"],
        "env": { "FIGMA_TOKEN": "${input:figma_token}", "LOG": "${env:LOG_LEVEL}" },
        "cwd": "${workspaceFolder}"
      },
      "envFile": "${workspaceFolder}/.env",   // VS Code 风格：从 .env 加载更多 env
      "forbidden_tools": ["delete_file"],     // A2C 治理字段
      "vrl": "..."
    }
  },
  "inputs": [
    { "id": "figma_token", "type": "promptString", "description": "Figma API token", "password": true }
  ]
}
```

**变量替换（对标 VS Code）**：
- `${input:id}` → §9.3 解析链取值。
- `${env:VAR}` → 进程环境变量；缺失替换为空串 + WARN（VS Code parity）。
- 预定义变量：`${workspaceFolder}`（当前 workspace 工作目录）、`${userHome}`、`${pathSeparator}`。
- `envFile`：加载 `.env` 的 KEY=VALUE 进该 server 的 env；显式 `env` 字段**覆盖** envFile 同名项（显式胜）。

**渲染时机（安全关键）**：变量**只在 MCP server 本地 spawn 时**展开（经 `comp._config_render.arender` + resolver）；**绝不**在 `client:get_config` / `client:get_skills` 等发往 Agent 的 payload 里展开——发出去的永远是 `${input:}`/`${env:}` 占位符，**密钥值不离开 Computer**（与 #39 设计 §4.4「占位符不展开」同原则）。

### 9.2 MCP 批准门控（全套 CC）

MCP server 执行任意命令，故团队 git 共享的 `.tfrobot/mcp.json` 里的 server 每用户须先批准（CC `getProjectMcpServerStatus` 模型）：

- **状态判定**：server name 既不在 `enabledMcpjsonServers` 也不在 `disabledMcpjsonServers`，且 `enableAllProjectMcpServers !== true` → `pending` → 启动时弹批准框。
- **批准框三选**（写 **local scope** settings.json，个人决定、不污染共享层）：
  - `[a]ll` → `enableAllProjectMcpServers = true`
  - `[y]es` → 追加 `enabledMcpjsonServers`
  - `[n]o` → 追加 `disabledMcpjsonServers`
- **企业治理**（policy scope）：`allowedMcpServers`/`deniedMcpServers` 名单；`allowManagedMcpServersOnly=true` 时仅 managed 能控 allow 名单。
- **plugin 携带的 MCP server 不弹框**：用户**显式 install plugin** 即视为批准（类比 CC「user-added = trusted」），直接进 `enabledMcpjsonServers`；只有**未经显式添加**的 workspace 共享 server 才 pending。
- **非交互/`--json`**：pending server 无 TTY → 默认**跳过该 server + WARN**（不连接）；`--approve-all-mcp` 显式全批，或预置 `enableAllProjectMcpServers`。
- 用户级（`$XDG_CONFIG_HOME/a2c/mcp.json`）server 视为用户自己加的，不弹框。

### 9.3 inputs / env / secret（完整对标 VS Code SecretStorage）

A2C input 定义本就照搬 VS Code（promptString/pickString + `password`，A2C 另加 command 类型）。**值管理完整对标 VS Code**：首次解析后持久化、跨重启不再问、密钥进 OS keychain。

**取值解析链**（`${input:id}` 按序解析，命中即止）：
```
1. 进程内 cache (_cache)                          ← 本会话已解析
2. 环境变量 A2C_INPUT_<ID_UPPER>                  ← 编排层/CI 注入（12-factor，密钥不落 A2C 盘）
3. OS keyring（仅 password:true）                 ← VS Code SecretStorage 等价（keyring 库）
4. 非密钥持久化值（仅非 password）                ← $XDG_STATE_HOME/a2c/input-values.json
5. 交互 prompt（仅 TTY；promptString/pickString/command 按现有 resolver）
6. default
```

**解析后持久化**（按类分流，密钥与非密钥分离）：

| input 类型 | 落点 | 跨重启 |
|---|---|---|
| 来自 env（步骤 2 命中） | **不持久化**（编排层拥有） | env 重注入 |
| `password:true`（prompt 得） | **OS keyring**（`keyring` 库；service=`a2c-computer`，key=`<workspace>:<id>` 或全局 `<id>`） | keyring survive，**不再 prompt** |
| 非密钥（promptString/pickString） | 明文 `$XDG_STATE_HOME/a2c/input-values.json`（0600、gitignored） | cache survive |
| `command` 类型 | **不持久化值**（真相是命令，重算）；可选短 TTL 内存缓存 | 重算 |

**OS keyring 后端（对标 VS Code SecretStorage）**：`keyring` 库自动选 macOS Keychain / Windows Credential Manager / Linux Secret Service（libsecret）。

**headless / keyring 不可用降级**（容器/CI/无 Secret Service）：
- `password:true` 仍可经**步骤 2 env**（`A2C_INPUT_<ID>`）解析——这条永远在；
- 若无 env 且无 keyring 且无 TTY → **硬错误**「secret `<id>` 无法解析；请用 `A2C_INPUT_<ID>` 环境变量或在 TTY 重试」，**绝不写明文**（杜绝 AWS Q 把密钥明文落盘的反模式）。

**与 `.skillenv` 的边界（D1，与 tfrobot SKILL 协议 §5 交叉核对）**：本节的 `${input:}`/keyring/env 机制**只**服务 **MCP server 配置**的占位符解析；与 SKILL 的 `.skillenv` 是**不同层、不同机制、不重叠**：

| 维度 | §9.3 inputs/secret（本节） | `.skillenv`（SKILL 协议 §5，协议 owned） |
|---|---|---|
| 服务对象 | MCP server config（`${input:}` 占位符） | SKILL `scripts/` **脚本执行**的环境变量 |
| 取值机制 | env→keyring→明文 state→prompt→default | dotenv：字面 `KEY=VALUE` / 空值 `KEY=` 查**用户 vault** |
| 变量展开 | `${input:}`/`${env:}` 展开 | **无** `${VAR}` 展开（协议 §5 R6） |
| 落位 | A2C SDK（本设计） | Computer 侧 A2C-SMCP 执行层解释；a2c 已 `4017 .skillenv forbidden` |

**铁律**：不得把 keyring/`${input:}` 套到 `.skillenv` 上；`.skillenv` 的 vault 语义归协议+Computer 执行层，本设计不碰、不改、不重定义。

**plugin `inputs.json` 入池消歧（D2，决策：命名空间前缀自动消歧）**：plugin 的 `mcp-servers/inputs.json` 是 plugin-scoped（协议 v1 不跨 plugin 共享）；合进 Computer 全局 inputs 池时,为避免两个 plugin 同 `id` 撞：
- **池内存前缀 id**：`<plugin>@<marketplace>/<id>`（如 `frontend-design@my-team/figma_token`）。
- **plugin 内 `${input:<id>}` 引用用裸 id**：渲染该 plugin 的 `mcp-servers/*.json` 时，resolver 按**当前 plugin 上下文**把裸 `<id>` 解析到带前缀的池条目。
- **user/CLI 直接定义的 inputs（非 plugin 来源）**：无前缀，裸 id 入池（沿用现状）。
- 选 (a) 自动前缀而非 (b) 撞则硬抛：plugin 安装是常规操作，硬抛会让"装两个都用 `api_token` 的正常 plugin"失败——前缀消歧对用户透明、零打断。

**安全不变量**：
- 密钥（`password:true` 解析值）**绝不**写入 `.tfrobot/mcp.json`（只存占位符）、不写 `input-values.json`、不进日志（全程掩码，沿用 `cli_io.py` `is_password`）。
- 渲染只在 server 本地 spawn 时，发往 Agent 的 payload 永远是占位符（§9.1 渲染时机）。
- `${env:}`/`${input:}` 在 `get_config`/`get_skills` 中**不展开**。

**现状迁移**：现有 `inputs/resolver.py` 是纯内存（`_cache`）+ password 仅掩码、无任何持久化。本设计在解析链里**前插步骤 2（env）+ 步骤 3/4（keyring/明文 state）**，并在 prompt 解析后按类持久化；定义加载仍走现有路径（plugin 声明的 inputs 合并进定义池）。Tier 1（env）必做、Tier 2（keyring）一并做（用户要求完整对标 VS Code）。

**模块落点**：
- `inputs/resolver.py`：解析链（env → keyring → 明文 state → prompt → default）。
- `inputs/render.py`：`${env:}`/`${input:}`/`${workspaceFolder}` 等变量替换 + `envFile` 加载。
- 新增 `inputs/secret_store.py`：`keyring` 封装 + 可用性探测 + 降级。
- 新增 `inputs/value_store.py`：非密钥明文 state（0600、原子写）。

---

## 10. REPL 交互细节

### 10.1 Banner（zero-state 引导）

触发条件：`plugins=0 AND servers=0`（marketplace 数量不计入）。

```
  ╭──────────────────────────────────────────────────────────╮
  │  A2C Computer — ready (0 plugins, 0 servers)             │
  │                                                          │
  │  Next steps:                                             │
  │    • marketplace add <git-url>   add a SKILL marketplace │
  │    • server add @<file>          add an MCP server       │
  │    • help                        full command list       │
  ╰──────────────────────────────────────────────────────────╯
```

非零状态时仅一行：
```
A2C Computer  ·  2 plugins  ·  3 servers
进入交互模式，输入 help 查看命令
```

### 10.2 Help 组织（默认折叠 namespace）

```
a2c> help
Namespaces (type "help <name>" for details):
  server       MCP server lifecycle
  inputs       Input definitions and values
  marketplace  SKILL marketplaces (git sources)
  plugin       Plugins (skill+mcp bundles)
  skill        Skills cross-source query
  socket       Socket.IO connection control
  notify       Send notifications to Agent
  settings     Edit settings.json files
  utility      tools / desktop / mcp / render / tc / history

a2c> help marketplace
marketplace add <git-url> [--name N] [--trust] [--auto-update]
  Add a new SKILL marketplace from a git URL. Prompts for trust on first add.

marketplace list [--json]
  ...
```

`?market<TAB>` fuzzy 搜索 namespace 匹配。

### 10.3 Tab 补全（prompt_toolkit `Completer`）

| 上下文 | 补全 |
|---|---|
| 行首 | namespace 词 (`marketplace`, `plugin`, ...) |
| `marketplace ` | 子命令词 (`add list info ...`) |
| `marketplace add ` | URL 提示（`<git-url>`） |
| `marketplace info ` | 已存在 marketplace 名（动态） |
| `plugin install ` | 所有 available plugin（`<plugin>@<marketplace>`，动态） |
| `plugin uninstall/enable/disable ` | 已 installed plugin（动态） |
| `skill info ` | 当前 skill 全集（动态） |
| `server add ` | 文件路径或 `<json>` 提示 |
| `server add @` | 工作目录下文件路径 |
| 任意 `--flag` 位置 | 该命令的 flag 集 |

### 10.4 Rich 进度反馈（git clone/pull）

```
a2c> marketplace refresh
  my-team-skills      ⠋ Cloning... 45% (2.3 MiB / 5.1 MiB)
  other-team-skills   ⠹ Pulling...
  broken-team         ✗ Connection refused

Summary
────────────────────────────────────────────────
  ✓ my-team-skills      v1.2.0 → v1.3.0 (3 plugins)
  ✓ other-team-skills   v0.9.0 (unchanged)
  ✗ broken-team         git: Connection refused
────────────────────────────────────────────────
3 marketplaces · 1 updated · 1 unchanged · 1 failed
```

实现：`rich.progress.Progress` + 并行 `git` subprocess 通过 `--progress` 标志解析输出行（百分比 + KiB）。

### 10.5 Trust prompt

```
a2c> marketplace add git@github.com:team/skills.git

  ⚠ Untrusted marketplace
  Source: git@github.com:team/skills.git
  Trust this source and continue? [y/N]: y

  ✓ Trusted (saved to known_marketplaces.json)
  Cloning... ⠋
  ✓ Cloned (5 plugins found)

  Next: plugin install <name>@my-team-skills
```

`marketplace add ... --trust` 跳过 prompt。

### 10.6 Plugin MCP server 冲突（硬抛、无逃生口）

name 即身份，外来同名直接抛错——交互与非交互行为一致（无 prompt、无 flag 旁路）：

```
a2c> plugin install frontend-design@my-team-skills

  ✗ MCP server name conflict: 'figma-mcp'
    Existing: figma-mcp (added manually 2026-05-19, owner=user)
    Plugin brings: figma-mcp (transport=stdio, command=node)

  Install aborted. No changes made.
  Resolve by one of:
    • rename/remove your existing server:  server rm figma-mcp
    • or rename the server in the plugin's own manifest (if you own the repo)
```

- **原子失败**：抛 `MCPServerNameConflictError`，plugin 与其 skills 一个都不装、不写 `installed_plugins.json`。
- **判定排除自有**：若 `figma-mcp` 本就是该 plugin 上次装的（命中 `bundledMcpServers`）→ 不算冲突，正常幂等再物化。
- 非交互 / `--json`：同样硬抛，退出码 1 + JSON error（`{"error":"mcp_server_name_conflict","name":"figma-mcp","owner":"user"}`）。

---

## 11. JSON 输出（`--json` 全局 flag）

启动时 `a2c-computer --json` 进 REPL：
- 所有命令默认 JSON 输出（不打 Rich 表格、不上色）。
- 进度反馈以 line-delimited JSON：`{"event":"clone_progress","name":"my-team","pct":45}`。
- 交互式 prompt（trust 确认）→ 非交互场景必须用 flag（如 `--trust`），缺则报错退出。MCP server 冲突无 prompt（直接硬抛，§10.6）。
- 退出码语义同 §4.6。

```bash
$ a2c-computer --json marketplace list
[
  {
    "name": "my-team-skills",
    "url": "git@github.com:team/skills.git",
    "trusted": true,
    "autoUpdate": true,
    "lastUpdated": "2026-05-21T10:30:00Z",
    "commitSha": "abc1234",
    "plugins": [
      {"name": "frontend-design", "installed": true, "enabled": true},
      {"name": "backend-helpers",  "installed": false, "enabled": false}
    ]
  }
]
```

---

## 12. 模块落点（实现）

### 12.1 新增模块

```
a2c_smcp/computer/
├── settings/                    ← 新（意图/治理层 + 物化层 + reconciler）
│   ├── __init__.py
│   ├── schema.py                ← settings.json TypedDict（enabledPlugins/extraKnownMarketplaces/MCP 门控/trust policy）
│   ├── scope.py                 ← 五级 scope 路径解析、读/写两套 merge customizer（§5.4）
│   ├── policy.py                ← policy scope 四子源 first-source-wins
│   ├── store.py                 ← settings.json / known_marketplaces.json / installed_plugins.json 读写（原子写+锁+.bak）
│   ├── mcp_config.py            ← .tfrobot/mcp.json 定义层（A2C 原生 schema）多 scope 加载 + 门控（§9.1/§9.2）
│   └── reconciler.py            ← 启动对账（additive-only）+ plugin gc / marketplace prune
├── inputs/                      ← 现有模块，新增以下（§9.3 VS Code 对标）
│   ├── ... (现有 base/resolver/render/cli_io)
│   ├── secret_store.py          ← keyring 封装 + 可用性探测 + headless 降级（password:true）
│   └── value_store.py           ← 非密钥明文 state（$XDG_STATE_HOME/a2c/input-values.json，0600 原子写）
├── skills/                      ← 现有 #39 范围、新增以下
│   ├── ... (现有六模块)
│   ├── debouncer.py             ← SkillEventDebouncer
│   └── watcher.py               ← watchdog 集成
├── cli/
│   ├── main.py                  ← Typer 子命令拓展（marketplace/plugin/skill/settings 子命令）
│   ├── interactive_impl.py      ← REPL dispatcher 扩展
│   ├── commands/                ← 新（按 namespace 拆分）
│   │   ├── __init__.py
│   │   ├── marketplace.py
│   │   ├── plugin.py            ← 含 gc / MCP 批准框
│   │   ├── skill.py
│   │   └── settings.py
│   ├── completer.py             ← prompt_toolkit Completer
│   ├── progress.py              ← Rich 进度条 wrapper
│   ├── banner.py                ← zero-state banner
│   └── help.py                  ← 分组 help 渲染
```

### 12.2 改动现有模块

| 文件 | 改动 |
|---|---|
| `computer.py` | 持有 `SettingsStore`、`Reconciler`、`SkillEventDebouncer`；启动调 reconcile（additive-only）；扩展 `_on_manager_change` 调 debouncer（与 [`design-0.2.1-skill-computer-management.md`](design-0.2.1-skill-computer-management.md) §5.1 同步） |
| `inputs/resolver.py` | 解析链前插 env(`A2C_INPUT_<ID>`)→keyring→明文 state；prompt 后按类持久化（§9.3） |
| `inputs/render.py` | `${env:VAR}`/`${input:id}`/`${workspaceFolder}` 等变量替换 + `envFile` 加载（§9.1） |
| `socketio/client.py` | `emit_update_skills` 改为通过 debouncer 触发（不裸调） |
| `cli/main.py` | 新增 `--json` / `--settings <file>` / `--add-dir <dir>` / `--approve-all-mcp` 全局 flag；新增 typer 子命令（marketplace/plugin/skill/settings/migrate-settings） |
| `cli/interactive_impl.py` | dispatcher 拆分到 `cli/commands/*`；保留 backward-compat |
| `mcp_clients/manager.py` | 与 #39 一致：新增 `list_skill_resources` 完整消费 cursor 翻页 |

### 12.3 不动的模块（v0.2.1 范围外）

- `agent/*`：Agent 侧本就是 `notify:update_skills` 接收后自动重拉，无新逻辑。
- `server/*`：路由 + 广播已在 #41 完成（PR #45 已合并）。
- `smcp.py`：协议 TypedDict 不变。

---

## 13. 测试范围（红→绿）

### 13.1 单元

- `settings/scope.py`：**读/写两套 merge 语义**——读时 object **递归深合并** + array 拼接去重（低先）；写时 array **整体替换**、`undefined` 删 key（§5.4）。`project=true`+`local=false`→false。
- `settings/policy.py`：policy scope 四子源 first-source-wins（不合并）。
- `settings/reconciler.py`：**additive-only**——missing→clone、sourceChanged→重 clone、`materialized∖declared`→**不动**（断言不删）；失败降级；`plugin gc` 列孤儿。
- `settings/mcp_config.py`：`.tfrobot/mcp.json` 多 scope 加载优先级；门控 `pending`/`enabled`/`disabled` 判定；plugin-bundled 免批准。
- `inputs/secret_store.py`：keyring 可用→存取 password；keyring 不可用→降级 env、无 env 无 TTY→硬错误（**不写明文**）。
- `inputs/render.py`：`${env:}`/`${input:}`/`${workspaceFolder}` 替换；`envFile` 加载 + 显式 env 覆盖。
- `skills/debouncer.py`：300ms 合并；并发 mark_dirty 不丢；emit 失败重试。
- `cli/completer.py`：动态名称补全反映最新 Registry 状态。
- naming（段数消歧）：user 1 段裸名 `^[a-z0-9]+(-[a-z0-9]+)*$`、marketplace 2 段 `<plugin>:<skill>`、mcp 3 段 `mcp:<server>:<skill>`（首段须字面 `mcp`）；**user 裸名不得因缺 `:` 报错**；段数∉{1,2,3} / 3 段首段≠mcp / leaf 非严格 kebab → `4016`；mcp server 段宽松 `[A-Za-z0-9_-]`。

### 13.2 集成

- `marketplace add` 全链路：trust prompt → 写 settings.json `trustedMarketplaces` → git clone → known_marketplaces.json（**无 trusted 字段**）落盘 → emit_update_skills。
- `plugin install` 外来同名 MCP server → 硬抛 + 原子失败（不留半装）；自有同名 → 幂等放行；`plugin uninstall` 清理 bundledMcpServers。
- **MCP 批准门控**：workspace `.tfrobot/mcp.json` 未知 server → pending → 批准框 → 写 **local** `enabledMcpjsonServers`；plugin-bundled server 免批准直连。
- **inputs 解析链**：env `A2C_INPUT_<ID>` 命中 → 不落盘；password prompt → keyring 存 → 重启不再问；非密钥 → 明文 state；keyring 不可用 + 无 env + 无 TTY → 硬错误。
- File watcher：user/project 目录 SKILL.md 增删改 → debounce → emit；CLI 写回 settings 经 `markInternalWrite` 不触发重载循环。
- Reconciler additive-only：物化多于声明的条目重启后**仍在**；`plugin gc` 才清。
- `--config @file` + `.tfrobot/mcp.json` 同时存在 → settings/mcp 定义层优先合并。

### 13.3 E2E（pexpect）

- 交互式 `marketplace add` → 输入 `y` → 看到 progress → 最终成功 banner。
- 非交互 `a2c-computer marketplace add ... --trust --json` → JSON 输出退出码 0。
- MCP 批准框 E2E：workspace 共享 server 首启 → 弹框 → `y` → 重启不再弹。
- Banner 触发条件：首次启动零状态、添加 server 后非零 → 重启不出 banner。

### 13.4 安全反例（继承 #39 §5.3 + 本设计新增）

- Sandbox 穿越 / `.skillenv` forbidden / `too_large` 不铸句柄 / staging 隔离 / name 寻址防越权。
- Trust 拒绝：`strictKnownMarketplaces=true` + 不在 `trustedMarketplaces` + 非交互 `--json` → 退出码 1 + JSON error。
- MCP server 外来同名冲突（交互/非交互一致）→ 硬抛 `MCPServerNameConflictError`、退出码 1、不留半装状态；自有同名 → 不触发。
- **密钥不落明文**：`password:true` 值绝不出现在 `.tfrobot/mcp.json`/`input-values.json`/日志；keyring 不可用且无 env → 硬错误而非明文落盘。
- **占位符不外泄**：`get_config`/`get_skills` payload 中 `${input:}`/`${env:}` 不展开，密钥不离开 Computer。

---

## 14. 实施 WBS

按工作量与依赖关系切 5 个子 PR（base = `develop-v0.2.1`，head = 各 feature 分支；最后整线合 `develop`）：

| # | 范围 | 主要文件 | 依赖 |
|---|---|---|---|
| A | settings.json 架构 + 五级 scope（读/写两套 merge）+ reconciler（additive-only + gc）+ 物化文件（原子写/锁/.bak） | `computer/settings/*` | — |
| B | `computer/skills/` 内部六模块（**等同 #39 原 PR**） | `computer/skills/*` (除 debouncer/watcher 外的六模块) | — |
| C | `.tfrobot/mcp.json` 定义层（原生 schema 多 scope）+ MCP 批准门控 + Plugin manifest 加载（`plugins/<n>/.tfrobot-plugin/plugin.json` + `mcp-servers/<n>.json` 文件式）+ source 5 类（含 **git-subdir sparse clone** + github/cnb 简写糖归一化）+ curator 模式 | `computer/settings/mcp_config.py`, `computer/skills/{staging,manifest,sources}.py`（新） | A, B |
| D | inputs/env/secret（VS Code 对标）：`render.py` 变量替换/envFile + `secret_store.py` keyring + `value_store.py` 明文 state + `resolver.py` 解析链 | `computer/inputs/*` | A |
| E | CLI 命令拓展（marketplace/plugin/skill/settings + completer + progress + banner + 批准框） | `computer/cli/*` | A, B, C, D |
| F | File watcher + debouncer + emit_update_skills 链路 + markInternalWrite | `computer/skills/{debouncer,watcher}.py`, `computer/socketio/client.py` | A, B, C |

A ⫫ B（并行）；C 依赖 A+B；D 依赖 A；E 依赖 A/B/C/D；F 依赖 A/B/C。A/B 合入后立即 unblock 其余。

---

## 15. 待解决（写在前面、避免日后再决策）

- ✅ **协议命名已先行落地**（a2c-smcp 0.2.2 原地修订，不 bump `PROTOCOL_VERSION`，因 0.2 未冻结）：`skill.md §1` 定稿 name 分源格式（user 1 段 / marketplace 2 段 / mcp 3 段，`:` 合法分隔符）、leaf 严格 kebab + server 段宽松、`A2CSkillRef.version` 来源（`data-structures.md`）。**python-sdk 跟进实现 4 点**：
  1. `naming.py` 合成裸名：marketplace `<plugin>:<skill>` / user 裸 `<skill>` / mcp `mcp:<server>:<skill>`（server 段归一化字符集/大小写不变）；
  2. 4016 lexer 改**段数消歧**，**接受 user 1 段裸名**（勿因缺 `:` 报错）；
  3. `A2CSkillRef.version` 按源取值（marketplace→plugin/entry；mcp→`skill://._meta.version`；user→null/省略，`NotRequired`）；
  4. install 层落实跨 marketplace `<plugin>` 唯一性拦截（`<plugin>@<marketplace>`）。
  > 注：协议团队指出 #1/#2 改的是**现有字段产出取值**，在已冻结的 0.2 上属破坏性 MINOR（0.3.0），仅因 0.2 未冻结才得以原地改；#3 是 PATCH 安全澄清。
- 🟡 **`envFile` 字段**：`.tfrobot/mcp.json` 的 `envFile`（VS Code 对标）是 SDK 侧 render 特性、对 `MCPServerConfig` 加性；若要进协议 `client:get_config` 需走 `add-feature` 追认（命名项已先行落地、此项可后续一并提）。v0.2.1 SDK 内实现、文档标「待协议追认」。
- **`keyring` 可选依赖**：Tier 2 OS keychain 需 `keyring` 库。作为**可选依赖**（`pip install a2c-smcp[keyring]`）；缺失/不可用时降级到 env（§9.3），核心功能不依赖它。
- ✅ **D1 `.skillenv` vs §9.3 边界**（已落 §9.3）：`${input:}`/keyring 只服务 MCP server 配置；`.skillenv`（SKILL 脚本执行、用户 vault）归协议 + Computer 执行层（a2c 已 `4017`-forbidden）。两者不重叠，铁律：不把 keyring 套到 `.skillenv`。
- ✅ **D2 plugin `inputs.json` 消歧**（已定 §9.3，方案 a）：池内存前缀 id `<plugin>@<marketplace>/<id>`；plugin 内 `${input:<id>}` 用裸 id 按当前 plugin 上下文解析；user/CLI 定义无前缀。自动消歧、对用户透明。
- ✅ **D3 MCP schema 对齐版本**（已核）：`MCPServerConfig` 在 v0.2.0→v0.2.1 **零结构差异**（git diff `c57fa56..HEAD` 该区域完全一致；v0.2.1 是 skill/blob 加性升级）。tfrobot mcp-servers 协议 §7 把"对齐 v0.2.0"提到 v0.2.1 是纯文档一行改（**在 tfrobot-marketplace 仓库,非本项目**）。
- **Remote managed settings**：v0.2.1 仅留 stub；v0.2.2+ 实现 OAuth 拉取与 30 分钟 poll。
- **Plugin version pinning**：v0.2.1 仅支持 latest（commit HEAD）；v0.2.2+ 支持 `<plugin>@<marketplace>@<version>` 三段语法。
- **Plugin lifecycle hooks**（post_install/pre_uninstall 脚本）：默认禁用、`--allow-hooks` 显式开启；v0.2.1 不实现，留 manifest 字段位。
- ✅ **workspace scope 聚合模型**（访谈定稿，§5.0/§5.1/§9.1）：持久登记多工作目录 + **active workdir 单根**（绑定当前 Agent 任务）作 project/local。**能力层**（`enabledPlugins`/`extraKnownMarketplaces`/skills）跨**全部登记目录**全局并集、置最低优先级（稳定能力面、不随 active 跳变）；其余 project/local 键（trust/MCP 批准/permissions/标量）+ **MCP server 定义**（§9.1 mcp.json）**只取 active workdir、不跨目录并集**（敏感面隔离）；无 active 时仅 user + 能力层。映射 CC：active≙主 cwd、其余登记目录≙`--add-dir`；A2C 仅「登记持久 + 主根动态」两点适配。**取代**早期 #28「整份并集」草案。
- **inputs 值跨 workspace 分档**：v0.2.1 非密钥值用全局 `input-values.json`；workspace 概念已落地（§5.1），是否按 **active workdir** 分档（避免同 id 互覆盖）待 v0.2.2+ 定，schema 预留。
- **Marketplace 中心化目录服务**：A2C 不维护；marketplace 只通过 git URL 添加。

---

## 16. 验收清单

- [ ] `settings.json` 五级 scope **读/写两套 merge** 语义：测试覆盖每一组合（读：object **递归深合并** + array 拼接去重；写：array 整体替换 + undefined 删 key）。深合并需含「嵌套对象 `source` 不被高 scope 整体替换」用例。
- [ ] settings.json **只**含 `enabledPlugins`/`extraKnownMarketplaces`/MCP 门控/trust policy；**不含** MCP server 定义/inputs。
- [ ] **workspace active-workdir 模型**（§5.1）：能力层（`enabledPlugins`/`extraKnownMarketplaces`/skills）跨全部登记目录全局并集；其余 project/local 键 + `mcp.json` 仅取 active workdir、切任务切根；无 active 时 project/local 空。测试覆盖：① 非 active 目录的 trust/MCP 批准**不**生效；② 非 active 目录的 plugin/skill **仍**可见（能力层）；③ active 切换后 project/local 随之切换。
- [ ] settings.json **无 `version` 字段**（复刻 CC passthrough）；未知键静默保留、单字段校验失败仅过滤不用不拒载；`version` 仅存于物化文件（§6.1/§6.2）。
- [ ] `.tfrobot/mcp.json` A2C 原生 schema 多 scope（user 主 + **active workdir 单根**，不跨目录并集；§9.1）；`${env:}`/`${input:}`/`${workspaceFolder}` 替换 + `envFile`。
- [ ] **MCP 批准门控**：workspace 共享 server 首见 pending → 批准框 → 写 local；plugin-bundled 免批准；`enableAllProjectMcpServers`/`--approve-all-mcp` 生效。
- [ ] **inputs/secret（VS Code 对标）**：解析链 env→keyring→明文 state→prompt→default；password 走 keyring 重启不再问；keyring 不可用降级 env，**绝不写明文**。
- [ ] Trust：CC 风格 settings.json policy 字段计算（`strictKnownMarketplaces`/`trustedMarketplaces`/`blockedMarketplaces`）；known_marketplaces.json **无** trusted 字段。
- [ ] `marketplace add/list/info/remove/refresh/set` 六命令完整；trust prompt 流程红→绿。
- [ ] `plugin install/uninstall/enable/disable/list/info` + `plugin gc` 完整；MCP server 外来同名硬抛 + 原子失败、自有同名幂等放行；bundledMcpServers 联动卸载。
- [ ] **Reconciler additive-only**：物化多于声明不自动清理；孤儿靠 `plugin gc`/`marketplace prune`。
- [ ] 物化文件**原子写 + 锁 + .corrupt 备份**（优于 CC writeFileSync）。
- [ ] `skill list/info` 跨三源扁平视图。
- [ ] `settings show/edit/get/set` 四命令；非编辑器场景纯 CLI 可改；写回经 `markInternalWrite` 不触发 watcher 重载循环。
- [ ] `--json` 全局 flag：所有命令机器可读输出；JSON line-delimited 进度。
- [ ] `--config/--inputs` 老 flag 与 `.tfrobot/mcp.json` 共存，启动合并。
- [ ] Tab 补全：动词/子命令/flag/动态名称/文件路径全覆盖。
- [ ] Help 分组：默认列 namespace、`help <ns>` 详情。
- [ ] Banner 仅 `plugins=0 AND servers=0` 触发。
- [ ] File watcher：user/project SKILL.md 改动 300ms debounce → emit_update_skills。
- [ ] Rich 进度条：`marketplace refresh` 多 marketplace 并行进度 + 失败汇总。
- [ ] `uv run poe lint` 净；`uv run poe test` 零回归；`PROTOCOL_VERSION` 未改动。

---

## 参考

- 内部模型：[`design-0.2.1-skill-computer-management.md`](design-0.2.1-skill-computer-management.md)
- 工单：[`upgrade-0.2.1-skill-blob-transfer.md`](upgrade-0.2.1-skill-blob-transfer.md)
- 协议：`a2c-smcp-protocol/docs/specification/skill.md`、`events.md`、`error-handling.md`
- CC 实现范本（用户提供，全文档逐处引用）：
  - `pluginLoader.ts:2420` — plugin.json + entry merge
  - `loadPluginCommands.ts:726` — skill ID `<plugin>:<skill-dir>`
  - `marketplaceManager.ts` — known_marketplaces.json schema
  - `installedPluginsManager.ts` — installed_plugins.json V2 schema
  - `skillChangeDetector.ts:277` — chokidar 300ms debounce
  - `cacheUtils.ts:44` — `clearAllCaches()` 三层失效
  - `attachments.ts:2607` — sentSkillNames Map（不可移植，A2C 改用 Socket.IO 推）
  - `settings.ts:319` — policy scope first-source-wins
  - `mdm/constants.ts` — OS 原生 MDM 路径
