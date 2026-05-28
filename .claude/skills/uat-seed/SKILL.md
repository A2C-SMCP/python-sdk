---
name: uat-seed
description:
  创建、扩充、验收 A2C-SMCP UAT 种子服务与数据。种子按 SKILL 来源（mcp / marketplace
  / user / _common）分目录组织，每条种子伴随**可执行的验收方法**，供 UAT scenarios
  复用。无参数时进入交互向导；带 source+name 则定向走对应 recipe。
argument-hint:
  "[create|audit|list|init] <source> <name>   （留空则进入向导）"
---

# UAT 种子服务与数据管理 — Create / Audit / List / Init

你是一名资深 QA 架构师，负责为 A2C-SMCP SDK 的 UAT 场景体系建设**可复用、可验收**的
种子库。"种子" = 测试场景需要的"被测对象 fixture"，包含：

- **MCP Server 种子**：可执行 Python 脚本，启动后通过 `skill://` 资源暴露各种形态的
  SKILL（mounted / archive / resources 三模式 × happy / 各类失败维度）
- **Marketplace 种子**：可被 `git clone` 的本地仓库（marketplace + plugins + skills
  + mcp-servers 各种组合）
- **User 源种子**：就地 DropIn 形态的 SKILL 包（拷进 `$A2C_SKILL_HOME/user/` 或
  `<workdir>/.tfrobot/skills/`）
- **`_common` 共享原料**：well-formed 与各失败维度的 SKILL 包定义，三源在 setup
  阶段通过拷贝/打包派生出自家形态——**单一定义源**，避免三处复制。

## 使用方式

```
/uat-seed                                       # 无参数：进入交互向导
/uat-seed init                                   # 首次：初始化 seeds/ 骨架 + README 索引
/uat-seed create mcp server_archive_bad_sha     # 创建：archive 模式 + bad sha256 失败种子
/uat-seed create marketplace strict-false-conflict
/uat-seed create user override-low-vs-high
/uat-seed create common valid-skill-pkg
/uat-seed audit mcp server_archive_bad_sha      # 验收：跑该种子的接受方法
/uat-seed audit --all                            # 验收：扫所有种子并跑各自验收
/uat-seed list [mcp|marketplace|user|common]    # 列出种子（按目录）
```

## 动作分流

| 动作 | 触发条件 | 执行路径 |
|---|---|---|
| **init** | 明确 `init` 或 `seeds/` 不存在 | → [初始化流程](#初始化流程) |
| **create** | `create <source> <name>` | → [创建流程](#创建流程) |
| **audit** | `audit <source> <name>` 或 `audit --all` | → [验收流程](#验收流程) |
| **list** | `list [source]` | → [列表流程](#列表流程) |
| **wizard** | 无参 | → [交互向导](#交互向导) |

## Input

$ARGUMENTS

---

## 核心契约（read me first）

### 1. 种子库位置（**不可变**）

```
.claude/skills/UAT/resources/seeds/
├── README.md              ← 索引：每条种子一行（name | source | mode | failure-axis | acceptance | 引用 scenarios）
├── _common/                ← 跨源共享原料（SKILL 包定义，well-formed + 各失败维度）
├── mcp/                    ← 可执行 MCP Server 种子
├── marketplace/            ← Git 仓库种子
└── user/                   ← 就地静态目录种子
```

完整布局规范见 `resources/layout.md`。

### 2. 每条种子的"四件套"（**强制**）

| 件 | 内容 | 用途 |
|---|---|---|
| **资产本体** | 脚本 / 目录 / git 仓库 | 被 UAT scenario 引用 |
| **README 段落** | 种子目录下 `README.md` 一节 | 描述：模式、失败维度、期望被测系统行为 |
| **acceptance.sh / acceptance.md** | 可执行（或可手动跑）的验收脚本/清单 | 独立于 UAT scenario 验证种子本身**正确** |
| **`seeds/README.md` 索引一行** | 顶层索引登记 | 全局检索、复用追踪、孤儿发现 |

**缺一件不通过**。审查清单见 `resources/recipes/<source>.md`。

### 3. 失败语义编码在名字里（**强制**）

| 后缀模板 | 含义 |
|---|---|
| `_ok` | happy path（必须放在每个模式 / 子能力的第一个） |
| `_missing_<field>` | 缺字段 |
| `_bad_<thing>` | 字段值错（如 `_bad_sha`、`_bad_format`） |
| `_no_<thing>` | 应有的资源不存在（如 `server_resources_no_subs`） |
| `_<bomb\|escape\|traversal>` | 安全攻击面（archive bomb、symlink escape、path traversal） |
| `_<axis>_conflict` | 业务冲突（如 `strict_false_conflict`、`name_collision`） |

不允许出现 `test1` / `foo` / `tmp` 这种含义不明的名字——一个名字承担一种期望被测行为。

### 4. `_common/` 是 SKILL 内容的唯一定义源（**强制**）

`mcp/` / `marketplace/` / `user/` 在 setup 阶段通过 **拷贝（`cp -r`）或打包（`tar czf`）**
派生 `_common/<x>`，**不得**自行内置 SKILL.md 副本。改一份 SKILL 内容 = 三源同步生效。

例外：`_common/` 装不下的"源特有结构"（如 marketplace 的 `marketplace.json` /
`plugin.json`、mcp 的 server 启动脚本）当然留在各自目录。

### 5. `_archives/` 二进制策略（**强制**）

`seeds/mcp/_archives/` 内的 `.tar.gz` / `.zip` 二进制：

- **不**直接 `git add` 包体（不可读、不可 diff、可能携带恶意载荷）
- 提供 `_archives/build.sh` + `_archives/manifest.json`（记每个归档 sha256 + 来源
  `_common/<x>`），CI / 本地 audit 时 `build.sh` 重建后比对 manifest sha256
- 路径攻击面归档（`path-traversal.tar.gz`、`tar-bomb.tar.gz`）由 `build.sh` 用代码
  显式构造，不留二进制黑盒

---

## 初始化流程

> 触发条件：`/uat-seed init` 或 `seeds/` 不存在。

### Step Init-1: 检查 `seeds/` 状态

```bash
ls .claude/skills/UAT/resources/seeds/ 2>/dev/null
```

- 已存在且非空 → 报告状态，询问是否要 reset（默认 **不**）
- 不存在 → 继续

### Step Init-2: 创建骨架

```bash
mkdir -p .claude/skills/UAT/resources/seeds/{_common,mcp/_archives,marketplace,user}
```

### Step Init-3: 写入顶层 README 索引模板

读取 `resources/templates/seeds-readme-template.md` 写到 `seeds/README.md`。

### Step Init-4: 写入各 source 子目录 README

读取 `resources/templates/source-readme-<source>.md` 分别写到 `seeds/<source>/README.md`。

### Step Init-5: 报告

```
✅ seeds/ 骨架已创建。下一步：
   /uat-seed create common valid-skill-pkg        # 最小 well-formed SKILL 原料
   /uat-seed create mcp server_resources_ok       # 最小 resources 模式 happy path
   ...
```

---

## 创建流程

### Step Create-1: 参数校验

```
/uat-seed create <source> <name>
```

- `<source>` ∈ {`mcp`, `marketplace`, `user`, `common`}（缺则进交互向导）
- `<name>` 必填，校验：
  - kebab-case 或 snake_case（mcp 用 snake_case 因为是 Python 模块名；其余 kebab）
  - 包含明确的"模式 + 失败维度"语义（参见 [核心契约 §3](#3-失败语义编码在名字里强制)）
  - 不与现有种子重名

### Step Create-2: 加载 source 对应 recipe

| Source | Recipe |
|---|---|
| `mcp` | `resources/recipes/mcp.md` |
| `marketplace` | `resources/recipes/marketplace.md` |
| `user` | `resources/recipes/user.md` |
| `common` | `resources/recipes/common.md` |

Recipe 内容包含：目录结构、模板路径、典型字段、acceptance 编写要点。

### Step Create-3: 失败维度澄清（仅当 `<name>` 含失败语义）

读取 `resources/guides/failure-axes.md`，定位该失败维度的 protocol/source 引用：

- 协议条款（如 `skill.md §3` archive_sha256）
- SDK 触发点（如 `staging.py:_materialize_archive` 抛 `SkillStagingError`）
- 期望 Computer 端行为（ERROR 日志 + 跳过 + 不阻断其他 SKILL）

向用户**复述一遍**该失败维度的期望被测行为，确认后再生成资产。

### Step Create-4: 生成资产

按 recipe 指引：

1. 复制/渲染模板到 `seeds/<source>/<name>/` 或 `seeds/<source>/<name>.py`
2. 如需 `_common` 原料 → 检查是否已存在；不存在则**先**走一次 `create common <...>`
3. 写入 `seeds/<source>/<name>/README.md` 或在父目录 README 追加段落

### Step Create-5: 编写 acceptance

每条种子必须配 acceptance（**两选一**，与 source 性质匹配）：

| Source | Acceptance 形态 | 落点 |
|---|---|---|
| `mcp` | `acceptance.sh`（可执行）+ 预期输出片段 | `seeds/mcp/<name>.acceptance.sh` |
| `marketplace` | `acceptance.sh`（可执行）+ 预期输出 | `seeds/marketplace/<name>/acceptance.sh` |
| `user` | `acceptance.md`（手动验收清单）+ 自动化片段（如有） | `seeds/user/<name>/acceptance.md` |
| `common` | `acceptance.md`（静态校验：SKILL.md 解析、文件结构） | `seeds/_common/<name>/acceptance.md` |

acceptance 必须**独立**于任何 UAT scenario——直接跑就能证明"种子本身按设计行为"。
具体 acceptance 设计原则见 `resources/guides/acceptance-design.md`。

### Step Create-6: 登记顶层索引

向 `seeds/README.md` 追加一行：

```
| <name> | <source> | <mode> | <failure-axis> | <acceptance 路径> | <已引 scenarios:暂空> |
```

### Step Create-7: 自检清单

- [ ] 资产本体已生成（脚本可执行 / 目录结构完整）
- [ ] 种子 README 段落已写（含模式 + 失败维度 + 期望被测行为）
- [ ] acceptance 已写且**当前主分支跑通**（happy）或**按设计触发期望错误**（failure）
- [ ] 顶层 `seeds/README.md` 索引行已追加
- [ ] `_common` 依赖关系：如复用 `_common/<x>`，已在 README 段落注明
- [ ] 二进制归档（如有）已通过 `_archives/build.sh` 生成，**不**直接提交二进制

### Step Create-8: 输出

```
✅ 种子已创建：seeds/<source>/<name>
   acceptance 已通过：✅ / 待跑：⏭️
   下一步：/uat-seed audit <source> <name>  # 显式跑一次验收
   或：在 UAT scenario 中通过路径引用：seeds/<source>/<name>
```

---

## 验收流程

> 验收是种子库的**地基**：只有当一条种子的 acceptance 能稳定跑过（happy 路径 PASS /
> 失败路径触发期望错误），它才有资格被 UAT scenario 引用。

### Step Audit-1: 收集目标

- `audit <source> <name>` → 单条
- `audit --all` → 扫 `seeds/<source>/` 全部
- `audit --since <ref>` → 仅 git 变更涉及的种子

### Step Audit-2: 加载对应 acceptance

按 source 找到 acceptance 文件：

```bash
seeds/mcp/<name>.acceptance.sh
seeds/marketplace/<name>/acceptance.sh
seeds/user/<name>/acceptance.md       # 含手动 + 自动片段
seeds/_common/<name>/acceptance.md    # 静态校验
```

### Step Audit-3: 执行

| Source | 执行方式 |
|---|---|
| `mcp` | `bash seeds/mcp/<name>.acceptance.sh` → 比对 stdout/stderr 与脚本内 expected 片段 |
| `marketplace` | `bash seeds/marketplace/<name>/acceptance.sh`（含 `git init` 临时变 bare → `a2c-computer marketplace add file://...` 等） |
| `user` | 跑 acceptance.md 内自动化片段；手动项要求用户逐项确认 |
| `common` | 静态校验：`python -c "import yaml; yaml.safe_load(...)"` + 目录结构 assert |

每条 acceptance 都要包含**幂等性清理**——audit 完成后不留 `/tmp` 残留 / 不污染
`A2C_SKILL_HOME`。

### Step Audit-4: 报告

```
## UAT 种子验收报告
日期：YYYY-MM-DD
范围：<all | source | name>

### 结果摘要
- 总数：N    PASS：N ✅    FAIL：N ❌    Flaky：N ⚠️

### 详情
| 种子 | source | 期望 | 实际 | 备注 |
|---|---|---|---|---|
| server_archive_ok | mcp | happy: list 返 1 个 archive 模式 SKILL | ✅ | - |
| server_archive_bad_sha | mcp | failure: Computer ERROR "archive sha256 mismatch" | ❌ | 实际触发 "archive_uri unreachable" |

### 失败种子需处理
- server_archive_bad_sha：acceptance 触发了错误的失败路径 → 检查 sha 是否被意外修对
```

### Step Audit-5: 失败时的二次复验

任一种子 FAIL：

1. 重新跑一次（确认非 flaky）
2. 检查依赖 `_common/<x>` 是否被改过 → 引用方未同步
3. 检查协议/SDK 是否在 develop 分支已改变行为 → 该种子需要更新（不是 bug）

---

## 列表流程

### Step List-1: 读 `seeds/README.md` 索引表

直接渲染索引表，按 source 分组：

```
## seeds 索引（按 source）

### mcp
| name | mode | failure-axis | acceptance | 引用 scenarios |
| ... | ... | ... | ... | ... |

### marketplace
...

### user
...

### _common
...
```

### Step List-2: 孤儿检测（顺便做）

- 索引登记 ∉ 文件系统 → 报「索引脏，需清理」
- 文件系统 ∉ 索引登记 → 报「未登记种子，请补登记或删除」

---

## 交互向导

> 触发条件：`/uat-seed` 不带参数。

依次问：

1. **此次目的**：create / audit / list / init？
2. （若 create）**source**：mcp / marketplace / user / common？
3. （若 create + 非 common）**所属模式**：
   - mcp → mounted / archive / resources / 混合
   - marketplace → valid / strict / entry.skills / plugin source 五类 / 失败
   - user → 单源 / 多源覆盖 / 失败
4. **失败维度**（若非 happy）：从 `failure-axes.md` 列表选；无对应项 → 进入"新增失败维度"流程
5. **协议依据**：用户给出（或我从 `a2c-smcp-protocol/docs/specification/skill.md` 引用）

确认后走 `create` 流程。

---

## 与 `uat-scenario` / `UAT` 的协作契约

`uat-seed` 是种子库的**唯一入口**。其他两个 SKILL 通过两条路径触发本 SKILL：

```
            scenario 编写期                         scenario 执行期
                  │                                      │
                  ▼                                      ▼
┌─────────────────────────────┐         ┌──────────────────────────────┐
│ uat-scenario                │         │ UAT                          │
│  - Step 2b: 检索 seeds 索引 │         │  - 执行前: seed 前置 audit   │
│  - 命中 → 路径引用          │         │  - 用例 FAIL: 诊断三问       │
│  - 未命中 → "待补种子" TODO │         │  - 判定 seed 病 → 升级       │
│  - 不允许 inline fixture    │         │  - 判定 缺口 → 补 seed       │
└─────────────┬───────────────┘         └──────────────┬───────────────┘
              │  /uat-seed create                      │  /uat-seed audit
              │                                        │  /uat-seed create
              ▼                                        ▼
                       ┌─────────────────┐
                       │   uat-seed      │
                       │  (本 SKILL)     │
                       └─────────────────┘
```

### A. 来自 `uat-scenario`（编写期：发现缺口）

scenario 文档中**只能通过路径引用**已登记的种子：

```markdown
### F-04: archive 模式 sha256 校验失败被正确拒绝

- 前置：启动种子 MCP Server → `seeds/mcp/server_archive_bad_sha.py`
- 步骤：1. （由 acceptance 驱动 stdio + stage_mcp_skills） ...
- 预期：Computer 日志包含 `archive sha256 mismatch`
```

scenario 编写发现种子库**缺**所需 fixture：

1. **不允许** scenario 文档内 inline 长 fixture
2. 在 scenario 顶部 `## 待补种子` 区段登记 TODO
3. 调用 `/uat-seed create <source> <name>` 走本 SKILL 流程**先**产出种子 + 通过 acceptance
4. 回到 scenario 编辑，引用该种子，清掉 TODO

### B. 来自 `UAT`（执行期：发现 seed 病或缺口）

UAT 跑场景时可能遇到三种"seed 病"（详见 `UAT/SKILL.md` §Seed 依赖与触发 `/uat-seed`）：

1. **种子 acceptance FAIL** → `/uat-seed audit <name>` 复现 → 走 [创建/升级流程](#创建流程)
   的"修正"路径（修资产 / 修 acceptance / 修 failure-axes）
2. **scenario 期望与 seed 行为对不上** → audit 取 seed 实际期望 → 与 scenario 期望
   比对 → 两边权衡：要么调 scenario（向 seed 看齐），要么 `/uat-seed` upgrade（向
   scenario 看齐）
3. **scenario 运行时发现缺口**（如 P1 用例需要 archive 模式 happy 但库里没有）→ 用例
   暂列 Skipped + 用户决策：立刻 `/uat-seed create` 补 or 留到下个周期

执行期升级的关键约束：

- 修 seed 前**必须**先在 `failure-axes.md` 找 axis 行 + 协议条款；找不到 → 不是 seed
  病，是协议层缺少规定，走 `/add-feature`
- 修 seed 后**必须**重跑全部派生它的种子 acceptance（README 的"已派生引用"列）
- UAT 报告"Seed 反馈"节如实登记本次升级条目

### 反模式（**禁止**，对两侧调用方都成立）

- ❌ scenario 文档内 inline fixture（哪怕只是 server 脚本片段）
- ❌ 复用"差不多"的 seed 凑合（axis 不对会假阳性）
- ❌ 跳过 acceptance 直接进 scenario（"反正大概率能过"）
- ❌ 改 seed 让它"勉强"匹配错的 scenario 期望——而不修 scenario
- ❌ FAIL 的 seed 上线进入正式 UAT 跑

---

## 资源索引

| 路径 | 用途 |
|---|---|
| `resources/layout.md` | 种子库目录布局完整规范 |
| `resources/recipes/common.md` | `_common/` 种子创建 recipe + acceptance 模板 |
| `resources/recipes/mcp.md` | MCP Server 种子创建 recipe + acceptance 模板 |
| `resources/recipes/marketplace.md` | Marketplace 种子创建 recipe + acceptance 模板 |
| `resources/recipes/user.md` | User 源种子创建 recipe + acceptance 模板 |
| `resources/guides/failure-axes.md` | 失败维度分类（按协议条款 + SDK 触发点） |
| `resources/guides/acceptance-design.md` | acceptance 设计原则（独立性 / 幂等 / 期望对齐协议） |
| `resources/templates/seeds-readme-template.md` | 顶层 `seeds/README.md` 索引模板 |
| `resources/templates/source-readme-mcp.md` | `seeds/mcp/README.md` 模板 |
| `resources/templates/source-readme-marketplace.md` | `seeds/marketplace/README.md` 模板 |
| `resources/templates/source-readme-user.md` | `seeds/user/README.md` 模板 |
| `resources/templates/source-readme-common.md` | `seeds/_common/README.md` 模板 |
| `resources/templates/skill-md.md` | 标准 SKILL.md frontmatter 模板 |
| `resources/templates/mcp-server-scaffold.py` | MCP Server 启动脚本模板（含 stdio/streamable 选项） |

---

## 设计原则（不可变）

1. **种子 = 测试资产，不是测试本身**——种子只描述"被测对象长什么样、被测系统应当
   作何反应"；具体编排（先做 A 再做 B）归 scenario。
2. **acceptance 必须独立**——脱离任何 UAT scenario 都能跑、都能判 PASS/FAIL；这是
   保证种子库不腐烂的唯一手段。
3. **协议是种子的唯一来源依据**——每条失败种子的"期望被测行为"必须能引用到
   `a2c-smcp-protocol/docs/specification/skill.md` 的条款；引不到 → 该失败维度不应
   存在（或先在协议补条款）。
4. **`_common` 是单一定义源**——SKILL 包的内容（SKILL.md/scripts/...）只在
   `_common/` 写一次；其他源派生使用。
5. **失败语义可读**——文件名后缀直接告诉你这条种子在测什么失败；不读 README 也
   能猜到。
6. **二进制不入库**——`_archives/` 由 `build.sh` 生成，CI 重建比对 sha256。
7. **新失败维度走协议先行**——若 scenario 编写需要的失败维度在 `failure-axes.md`
   不存在，先在协议层补 / 在 `failure-axes.md` 登记 rationale，再产出种子。

---

## 与 A2C-SMCP 项目记忆挂钩

- **release work must be cut from main** — 本 SKILL 与 seeds/ 的工作分支从 `main`
  切，不复用当前 feature 分支。
- **explicit start command** — 创建 seeds / 改 seeds 的实操**只在用户显式说"开始"
  后执行**；本 SKILL 的所有"流程"步骤是在被显式调用时才走，闲时不动磁盘。
- **no over-engineering** — MVP 优先 happy path × 3 源 + 1~2 个最关键失败维度；不
  一上来 50 条种子。
