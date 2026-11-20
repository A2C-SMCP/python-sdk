# 设计文档：0.5.1 — 存量 marketplace 注册名迁移（D3）

> 状态：**Phase 1 设计已审查通过（2026-11-17），实现（Phase 2）待前置**——`#270`（派生算法与凭据 slug 约束）、
> `#276`（staging/source 安全）、`#260`（核心语义 + **D4 legacy refresh baseline**）合入后开工。
> 关联：python-sdk#268（本单）/ #260（核心语义）/ #270 / #276 / #275 / #271 / rust-sdk#289（镜像）/ rust-sdk#272 /
> a2c-smcp-protocol#79（协议裁决）/ Discussion [a2c-smcp-protocol#81](https://github.com/A2C-SMCP/a2c-smcp-protocol/discussions/81)（双端对齐留痕）。
> 性质：**SDK 自治**（protocol#79 定「名称唯一性 / 禁止静默改名」归协议管辖，**派生与迁移算法归 SDK 自治**）；
> 无 wire 变更、无新事件、无新错误码、不动协议结构。

---

## 0. 核心决策摘要（先看这张表）

| # | 决策 | 结论 |
|---|------|------|
| D1 | 迁移时机 | **显式命令**触发；**不做** boot 自动迁移、不做隐式改名 |
| D2 | 目标名来源 | 本地 clone 的**清单顶层 `name`**；不读网络、不用当前 URL 重算旧名（#270） |
| D3 | 旧名识别 | 「`known_marketplaces.json` 键 ≠ 该键 clone 目录内清单 `name`」= legacy；**账本 + 清单为唯一判据** |
| D4 | 不可判定 | 目录缺失 / 清单不可读 / 清单名非法 → **grandfather 保留旧名** + 报告，绝不猜 |
| D5 | 权威 vs 派生 | 用户意图（`installedPlugins` / `enabledPlugins` / `trustedMarketplaces` / plugin-scoped input+secret）**改写**；账本与 clone **重建**，不做字符串重写 |
| D6 | 可改写边界 | user scope 改写；**project / local 只报告不改**（用户裁决 #268）。project 入 git；local 虽不入 git，同样不代改（保持单一处置口径）。报告必须显式标出「这些插件迁移后将处于未启用态」 |
| D7 | 冲突 | 预检阶段**整项拒绝**（不半途改名）；无冲突项照常迁移；有冲突即退出码 1 |
| D8 | 幂等 | 每个 marketplace 独立、前向收敛；重复执行自动 no-op；任意一步崩溃后可**重跑续做** |
| D9 | 迁移顺序 | 「新侧就绪 → 换目录 → 撤旧侧」；**不提供回滚**，P0 之后只有前向 |
| D10 | secret 重键 | **先写新键、回读验证后删旧键**；keyring 不可用 → skipped（保留旧键、绝不落明文） |
| D11 | 跨 SDK | 迁移语义**各自实现**；python 只处理自己的键空间（两侧键空间实际同构，但实现与发布互不阻塞，见 §2.4） |
| D12 | 命令形态 | `a2c-computer marketplace migrate [--dry-run] [--name <old>]... [--yes] [--json] [--reclone]` |

---

## 1. 范围

### 1.1 在范围

- 将**存量**注册名（URL 派生名 / 显式 `--name` 别名）迁移为清单 `name`，覆盖 Issue 列出的全部权威面与派生面。
- 迁移的**检测、规划、dry-run、执行、冲突拒绝、崩溃恢复、离线策略**。
- 迁移文档与 release notes 的**内容草案**（随 Phase 2 落地）。

### 1.2 不在范围

- **不改** #260 的核心语义（注册名取清单 name、同名不同源拒绝、refresh 改名拒绝、`--name` 仅作期望名校验），只消费它产出的 **legacy refresh baseline**。
- **不改** #270（派生算法 / 同源校验）、#276（staging & source 脱敏与路径穿越）、#275 / #271（installPath / ledger 语义）。
- 不引入 Computer 级新 API；不做跨 SDK 的统一迁移工具；不动协议。
- 不迁移 `blockedMarketplaces`（当前无消费者，见 §4.2 F13）、不改 `extraKnownMarketplaces`（声明面，见 §2.3）。

### 1.3 术语

| 术语 | 含义 |
|------|------|
| **旧名 / legacy 名** | `known_marketplaces.json` 的键 ≠ 该键 clone 内清单 `name` 时的那个键 |
| **目标名 / 清单名** | 本地 clone 的 `.tfrobot-plugin/marketplace.json` 顶层 `name` |
| **权威面** | 用户意图，SDK 无权丢弃，只能按迁移计划改写 |
| **派生面** | SDK 自己维护的物化状态，删除可重建（协议 runtime-contract §4.9「账本可弃、删除它无损」） |
| **grandfather** | 保留旧名不动，只报告（协议 §5.14 授权的存量一致性交给 SDK 迁移方案） |

---

## 2. 权威模型（权威 / 派生两层）

### 2.1 权威面（迁移必须改写）

| 面 | 位置 | 键形态 | 代码 |
|----|------|--------|------|
| 安装意图 | user `settings.json` → `installedPlugins` | 数组，元素 `<plugin>@<marketplace>` | `installer.py:686` `_write_installed_plugin` |
| 启用意图 | **按安装 scope**：user / project / local 各自的 `settings[.local].json` → `enabledPlugins` | 对象，键 `<plugin>@<marketplace>`，值 bool | `installer.py:302` `_write_enabled_plugin`；写入 scope 取自账本条目（`installer.py:1458-1463`、`cli/commands/plugin.py:391`） |
| 信任 | user `settings.json` → `trustedMarketplaces` | 字符串数组，元素 = marketplace **name** | `marketplace.py:135` `_record_trust` / `:153` `_revoke_trust` |
| plugin-scoped input 值 | `$XDG_STATE_HOME/a2c/input-values.json` | 扁平 map，键 `<plugin>@<marketplace>/<id>` | `plugin_pool.py:36` `prefix_input_id`；`value_store.py` |
| plugin-scoped secret | OS keyring，service `a2c-computer` | 键同上 | `secret_store.py:26`；键在 `resolver.py:185` 起算并以 `resolved_id` 存取 |

> **启用位随安装 scope 落盘**（易被忽略，是本设计最大的正确性风险点）：`plugin install <pid> --scope project` 之后
> `plugin enable` 会把 `enabledPlugins[<pid>] = true` 写进 **project settings**（`plugin.py:391` 从账本逐条取 scope，
> 注释明写「绝不默认 `user`」）。因此迁移只改 user 层会让这类插件在合并视图里**既没有新键、旧键又不满足
> `key ∈ installed` 门控**（活跃判据 `recovery.py:124-129` / `reconciler.py:140-156`）——即**静默熄灯**。
> 处置见 §4.1 F2/F3 与 §5.1 C7。

> **安装意图固定落 user scope**：`installedPlugins` 是全局一次的事实，SDK 不随 `--scope` 写进可提交文件
> （`installer.py:697-699`）。读取侧是 merged 并集（`reconciler.py:127-137`），故 project/local 的手写声明也会被认可——
> 迁移对它是**只报告**（D6）。

### 2.2 派生面（优先重建，不做字符串重写）

| 面 | 位置 | 迁移方式 |
|----|------|----------|
| marketplace 物化记录 | `$A2C_SKILL_HOME/known_marketplaces.json` → `marketplaces[<name>]` | **以新名重写记录**：`source` / `commitSha` / `autoUpdate` / `lastUpdated` 从旧记录复制，`installLocation` **重算**为 `marketplace_skill_dir(home, new).resolve()`，随后删旧键 |
| catalog clone | `<home>/marketplace/<name>/` | 目录搬迁（离线、原子）；失败才重 clone（`--reclone`） |
| 外部 plugin 树 | `<home>/marketplace/.plugins/<mp>/<plugin>/` | 目录搬迁（同级 rename，与 catalog 搬迁同阶段） |
| 安装账本 | `$A2C_SKILL_HOME/installed_plugins.json` → `plugins[<pid>]` | **不重写 pid / installPath**：先以新 pid `materialize_plugin` 重建，成功后才删旧记录 |
| 运行期 SKILL 引用 | `A2CSkillRef.source = marketplace:<name>` | 不持久化；下次 boot / restage 自然刷新 |
| 运行期 SKILL name | `<plugin>:<skill>` | **不含 marketplace**（`naming.py:197-211`），改名零影响 |

**为什么不做字符串重写**：`installPath` 是物化事实（相对 source 指向 catalog 内、独立 source 指向 `.plugins/<mp>/<plugin>`），
重写字符串会把「记录」和「磁盘」解耦；而账本本就允许整条丢弃后重建（`store.py:373-402` 的 legacy 丢弃路径是同一姿态）。
少量重建成本换掉一整类「改了一半」的半态。

### 2.3 `extraKnownMarketplaces` 的权威地位

`marketplace add` **不写**该字段（两侧 SDK 同构）。因此：

- 它**不是** SDK 账本的前驱，不能默认用户都有声明；
- 存在时它是**声明面**（config-first，可能落在 user / project / local 任一 scope，project 还入 git），
  `reconciler.reconcile` 按它做 additive-only 对账（`reconciler.py:299-398`）；
- 本设计对它的处置：**只报告，不改写**。理由：声明是用户/仓库的显式意图，SDK 单方面改写 project 文件违反 D6；
  且与 #260 裁决 2 一致（声明式换源一律 `validation`、需显式 remove/re-add）。
- 声明源与已注册源**不一致**时的处置见 #287（[`design-0.5.1-boot-recovery-source-diagnostics.md`](design-0.5.1-boot-recovery-source-diagnostics.md)）：
  boot 只诊断、不换源；迁移**不管**这条路径，二者互不替代。

**已知后果（必须写进 dry-run 告警）**：`reconcile` 的 missing 分支会按声明**重新 clone `<home>/marketplace/<old>`
并把 `known[old]` 写回**（`reconciler.py:332-390`）。本仓内没有 `reconcile` 调用点（boot 走的是
`computer.py` 的治理恢复路径），但它是公开入口（`settings/__init__.py` 导出），**宿主**（TFRobotServer 等）可能直接调用。
若真被调用，迁移后的旧名会被声明面复活，用户会认为迁移失败。故：

- dry-run 对每个命中项给出警告：「`<file>` 的 `extraKnownMarketplaces` 声明了 `<old>`，reconcile 会重建该注册」；
- 迁移文档给出改声明的具体片段（用户自己改，入 git 的文件不代改）。

### 2.4 跨 SDK 差异（各自实现，不共享代码）

Discussion#81 已双端确认「迁移语义各自实现」。rust 侧实现面差异：

| 面 | python-sdk | rust-sdk | 对本设计的影响 |
|----|------------|----------|----------------|
| 安装意图 | user settings `installedPlugins` | 独立 `installed_plugins_intent.json`（权威，`A2C_SKILL_HOME`） | 只写 settings 一侧 |
| 安装账本 | `installed_plugins.json`，pid/installPath 可重建 | 同构 | 均可弃重建 |
| `known_marketplaces` 顺序 | dict（保序） | serde_json Map（**不保序**） | 迁移**不得依赖记录顺序**，所有权仲裁以「意图 > 账本 > 顺序」为准 |
| `extraKnownMarketplaces` | add 不写 | add 不写 | 同口径 |
| 凭据 slug | #270 在推同源校验 | #285 已合入 | 迁移检测不得用 URL 重算旧名（两侧同口径） |

**关于 secret 键空间的订正（重要，需回写记录层）**：Discussion#81 的 rust 侧陈述为「rust keyring 键 = 全局 `<id>`」，
据此推出的「python 需重键 / rust 不需要」**与 rust 代码不符**：

- `rust-sdk crates/smcp-computer/src/inputs/plugin_pool.rs:131` 把 plugin input id 前缀化为 `<plugin>@<mp>/<id>`；
- `.../inputs/resolver.rs:178` 算出 `scoped_id`，`:211` `secret_store.get(&resolved_id)`、`:232` `secret_store.set(&resolved_id, …)`
  —— 存取的**就是 scoped id**；
- 只有 `.../inputs/secret_store.rs:22` 的**模块注释**仍写「key=全局 `<id>`」，属陈旧注释。

结论：**两侧代码同构**（python `resolver.py:185/227-280` 同形）。因此：

- 本设计**不**声称「rust 不需要重键」；正确表述是「两侧都需要重键，但各自实现」；
- 该事实已在 Discussion#81 订正（2026-11-17，两侧均 scoped id、各自重键），记录层与代码一致；
- 对 python 实现无影响：无论 rust 如何，python 只处理 `<plugin>@<mp>/<id>` 键空间。

---

## 3. 旧名识别

### 3.1 判据（唯一）

对 `known_marketplaces.json` 的每个键 `K`：

1. 定位 `dir = marketplace_skill_dir(home, K)`（`skills/home.py:129`，即 `<home>/marketplace/<K>/`）；
2. 读 `dir/.tfrobot-plugin/marketplace.json` 顶层 `name` 得 `M`（`manifest.py:78` `read_marketplace_manifest`）；
3. `M == K` → 已合规，不在迁移面；`M != K` → **legacy**，迁移计划 `K → M`。

**不做的事**：不调用 `default_marketplace_name(url)`（`marketplace.py:116`）判断「这个名是不是 URL 派生的」。
理由（#270）：① 存储的 URL 可能携带凭据/非常规 scheme，重算会把凭据 slug 进名字；② #260 之后派生规则本身被废弃，
用废弃规则「验明正身」没有意义；③ 账本里没有 provenance 字段，任何事后推断都不可靠。

### 3.2 旧名来源（URL 派生 vs `--name`）——**诊断标签，非判据**

Issue 要求覆盖「旧名来源识别」。本设计的答案是：**两种来源在迁移上完全等价**（都是「注册键 ≠ 清单名」，
#260 D6 起显式别名不再被支持，两类都必须迁移），因此**来源分类不参与任何决策**。若需要展示，
只在 dry-run 的 `origin` 列给一个**尽力而为的诊断标签**，且受两条约束：

- 仅当记录的 `source.url` 通过当前（#270 之后的）严格解析时才计算；否则标 `unknown`；
- 计算结果**不得**用于改名、冲突判定或写盘，且输出前必须过 `known_git_source_for_display`（`utils/git_url.py:93`，
  `marketplace.py:93` 的 `_display_source_url` 是它的 CLI 包装），避免把凭据 slug 出来的名字直接展示。

实现优先级最低：Phase 2 可先只输出 `legacy`，把 `origin` 列留给后续增强。

### 3.3 grandfather（不可判定即保留）

以下情形**保留旧名**并报告，不迁移、不猜测、不报错退出：

| 情形 | `reason` |
|------|----------|
| `known_marketplaces` 有条目但 clone 目录不存在（且无 §7.4 的可认领目录） | `catalog-missing` |
| clone 内 `.tfrobot-plugin/marketplace.json` 缺失 / JSON 畸形 / 无 `name` | `manifest-unreadable` |
| 清单 `name` 不满足严格 kebab 或超长（`schema.py:213`） | `invalid-manifest-name` |
| 被 `--name` 过滤条件排除 | `not-selected` |

> grandfather 的语义与协议 §5.14 一致：存量一致性交给 SDK 迁移方案；**离线 / 无清单时保留旧名**是用户裁决的第 1 条。

### 3.4 与 #260 **D4 legacy refresh baseline** 的边界

#260 D4（A）规定：legacy 注册（旧名从未由清单解析）**首次成功 refresh** 时记录 **baseline 清单名**、保留旧名、不报改名拒绝；
仅 baseline 之后再改名才拒绝。

本设计：

- **复用 #260 的 baseline 字段与写入点**，**不另立**一套 baseline 存储（Phase 2 硬前置：baseline 未落地则无法保证
  「迁移前的 refresh 不会误拒」）；
- 检测仍然**优先读本地清单**（离线可用、不依赖 refresh 发生过）；
- baseline 只在「本地清单不可读但 baseline 存在」时作为**次选判据**补齐 `K → M`（补的是 §3.3 表第 2 行）；
  与本地清单冲突时以**本地清单为准**并记 WARN（清单是当前事实，baseline 是历史观测点）。

---

## 4. 迁移面清单（逐面：检测 / 动作 / 失败影响）

### 4.1 总表

| # | 面 | 权威/派生 | 检测 | 动作 | 失败处理 |
|---|---|---|---|---|---|
| F1 | user `installedPlugins` | 权威 | 元素以 `@<old>` 结尾 | 加新元素、删旧元素 | **fail-closed**：整项中止，已写部分保留待续跑 |
| F2 | user `enabledPlugins` | 权威 | 键以 `@<old>` 结尾 | 新键 = 旧值（`true`/`false` 都照抄），再删旧键 | fail-closed |
| F3 | project / local `enabledPlugins`、以及 project/local 的 `installedPlugins` 声明 | 权威（**只报告**） | 同 F2；扫描面 = `os.getcwd()` ∪ 账本 `projectPath` | **不改**，输出文件 + 点路径 + 建议片段；**并显式告警「迁移后该插件将处于未启用态」** | 不适用（该告警不阻塞执行，但写入汇总与 `--json`） |
| F4 | `trustedMarketplaces` | 权威 | 元素 ∈ 旧名集 | 加新名 → 删旧名 | 加失败 fail-closed；删失败 WARN（残留会让同名 add 跳过确认，但不阻断既定动作） |
| F5 | plugin input 值 | 权威 | 键前缀 `<plugin>@<old>/` | 单次原子写：新键 ← 旧值，删旧键 | fail-closed（**先写后删**，中途失败不丢值） |
| F6 | plugin secret | 权威 | 同上 | keyring：写新 → 回读验证 → 删旧 | keyring 不可用/写失败 → `skipped`，**旧键保留**、绝不落明文 |
| F7 | `known_marketplaces[<old>]` | 派生 | — | 先写 `known[new]`（`installLocation` 重算），待搬迁与重建完成后删 `known[old]` | 失败 → 保留旧条目（可重建） |
| F8 | catalog clone | 派生 | 目录存在性 | `<home>/marketplace/<old>` → `<new>` rename | 失败 → `--reclone` 时重 clone；否则整项中止于搬迁前 |
| F9 | 外部 plugin 树 | 派生 | 目录存在性 | `<home>/marketplace/.plugins/<old>` → `<new>` rename（与 F8 同阶段，紧跟 F8） | 失败 → 整项中止（F8 的可重入性保证重跑安全）；`--reclone` 时逐 plugin 重新 `locate_plugin_root` |
| F10 | `installed_plugins.json` | 派生 | pid 以 `@<old>` 结尾 | **不重写**：先以新 pid `materialize_plugin` 重建，成功后才删旧记录 | 重建失败 → **保留旧记录**（成为孤儿，`plugin gc` 可清） |
| F11 | 运行期 `A2CSkillRef.source` | 派生（内存） | — | 不迁移；说明 + 提示重启 REPL | 不适用 |
| F12 | `extraKnownMarketplaces` | 声明 | 键 ∈ 旧名集 | **只报告**（§2.3 的 reconcile 复活告警） | 不适用 |
| F13a | `blockedMarketplaces` | 声明（只报告） | 元素 ∈ 旧名集 | **只报告** | 不适用 |
| F13b | `strictKnownMarketplaces` | — | **标量 bool**（`schema.py:193/268`），无 per-name 面 | 不涉及 | 不适用 |

### 4.2 逐面说明（只写需要展开的）

**F1/F2 —— 为什么要「先加后减」**：活跃集 = installed ∧ `enabledPlugins[id] is True`（`reconciler.py:140-156`）。
若先删旧再加新，崩溃窗口里两项都不存在 = 插件静默熄灯。先加后减的最坏跨步状态是「旧新都在」，语义是重复安装而非丢失。

**F2 —— 必须按安装 scope 逐层处理**：见 §2.1 的加注。user 层能改就改；project/local 层按 D6 只报告，但**必须**产出
`will-be-inactive` 告警——否则用户看到「迁移成功」，实际插件从此不加载。这与「不静默」原则同级，不能降级为普通 WARN。

**F2 —— `false` 必须照抄**：`enabledPlugins: false` 与缺键在激活语义上等价，但它是用户的显式意图；迁移把它归一为缺键属于
「静默改变用户意图」，违反不静默原则。

**F3 —— 报告面怎么找全**：project/local 锚定进程 cwd（`scope.py:254-291`，#116）。CLI 可能在任意 cwd 运行，
故报告面 = `{os.getcwd()} ∪ {账本记录的 projectPath}`（`installer.py:335` `_clear_enabled_entries_visible_layers` 用同一并集口径）。
文件不存在时静默跳过（不凭空建目录，同该函数的存在性守卫）。

**F4 —— trust 残留的两面性**：残留旧名不只是「新名未受信」，它会让**同名源 add 跳过交互确认**（`marketplace.py:197-217` 的
desync 豁免）。故删除旧名是迁移的一部分，不只是清理。删除失败降级 WARN（`marketplace_remove` 的既有姿态，`marketplace.py:377-382`）。

**F5 —— value store 单次原子写**：`input-values.json` 是整文件 map（`value_store.py`），一次 `ValueStore.set` 等价于
「读-改-写整文件」。迁移把「写新 + 删旧」合并成**一次**原子写，避免两次写之间的半态。

**F6 —— secret 重键顺序**：keyring 是逐键 API（`secret_store.py`），无法多键原子。规则：
`set(new) == True` 且 `get(new) == old_value` → 才 `delete(old)`；否则 `skipped` 并把旧键留在原处。
**绝不**在未确认新键可读前删旧键——secret 丢了不可恢复（用户拿不回来，只能重录）。

**F9 —— 外树与 catalog 同阶段**：独立源 plugin 的根是 `<home>/marketplace/.plugins/<mp>/<plugin>`（`staging.py:891`
`external_plugin_dir`），其路径段含 marketplace 名。P2 内顺序固定为 **catalog 先、外树后**：catalog 是 P4 定位一切的起点，
先换它才能读新清单；外树只被 `locate_plugin_root` 的独立源分支使用（`staging.py:1043-1056`）。任一 rename 失败即整项中止
（此时 `known[new]` 已写、catalog 可能已换——续跑按 §7.4 认领，不会重复搬迁）。

**F10 —— 为什么保留旧账本记录而不是直接删**：`materialize_plugin`（`installer.py:940`）在写账本前会做 clone/定位 +
manifest 校验 + 依赖解析，失败即抛。删掉旧记录而新记录没建成 = 依赖信息（`mcpServers` bundle_id 数组）丢失，
`reclaimable_mcp_deps` / 治理重挂的输入变空。保留旧记录 = 只是多一条孤儿，用户可以 `plugin gc`。

**F10 —— 重建能沿用哪些元数据**：`materialize_plugin` 的参数只有 `scope` / `project_path` / `version`
（`installer.py:940-951`），`installedAt` 在函数内部恒写 `now`（`:1014`）。故**沿用** `scope` / `projectPath` / `version`，
**不沿用** `installedAt`（由本次重物化重写；如需保留原始安装时间，另开可选入参，见 §15 Q3）。
逐条重建：账本条目是 `pid → list[record]`（按 `scope` 分条，`store.py:107-139`），多 scope 记录逐条处理。

**F10 —— plugin 不在新清单里怎么办**：`find_plugin_entry` 找不到会抛（`installer.py:982-984`）。策略：
**不把它当作迁移失败**——F1 照常把意图迁到新 pid，F10 保留旧账本记录，dry-run/实跑都产出
`warn: plugin '<p>' not in the new manifest` 并给出处置建议（`plugin uninstall <p>@<new>` 或等待上游恢复）。
理由：上游删插件是外部事实，拿它阻断整个 marketplace 的改名代价过大；而保留旧记录保证依赖信息不丢。

**F11 —— 重启提示**：`A2CSkillRef.source` 是内存派生（`staging.py` `_build_marketplace_ref`），迁移命令不触碰 Registry；
若在 REPL 内执行，成功后调 `comp.mark_skills_dirty()`（同 `marketplace add/remove/refresh` 的既有标脏点，
`marketplace.py:481-510`）触发重扫。

**F13a —— 为什么 `blockedMarketplaces` 只报告**：该字段目前**无消费者**（全仓仅 `schema.py` 声明 + 校验，无执行点），
陈旧条目今天是惰性的。一旦 #80（strict / 白名单）落地，陈旧的 **block** 条目会 **fail-open**（旧名被拉黑、新名放行）——
这是 security 方向的残留。故本设计：只报告，且**要求 #80 落地时一并复查**本项（§15 Q5）。

---

## 5. 冲突分类与拒绝

### 5.1 预检阶段（零写入）拒绝的冲突

| 编号 | 冲突 | 判据 | 输出 |
|---|---|---|---|
| C1 | 多个旧名 → 同一目标名 | 计划里两个 `K` 的清单名相同 | `conflict: duplicate-target`，**全部**排除（不部分合并） |
| C2 | 目标名已被别的源占用 | `known_marketplaces[new]` 存在且 `source != 旧记录.source` | `conflict: name-taken-by-other-source`，排除（对齐 #260 D2） |
| C3 | 目标 pid 已存在且启用状态冲突 | **user 层** `enabledPlugins[<p>@<new>]` 存在且值 ≠ user 层 `enabledPlugins[<p>@<old>]` | `conflict: enabled-state-conflict`，排除该 plugin 所在项 |
| C4 | 跨 marketplace 裸名冲突（#257） | 目标 pid `<p>@<new>` 与**其他** marketplace 的已装 pid 同裸名 | `conflict: bare-name-clash`，排除 |
| C5 | 目标名非法 | 清单名不满足 `is_valid_marketplace_name` | §3.3 grandfather（`invalid-manifest-name`），非 conflict |
| C6 | 旧名与清单名相同 | — | no-op，不在计划内 |
| C7 | 来源 plugin 在新清单中缺失 | `find_plugin_entry(new_manifest, p) is None` | **不是冲突**：照常迁移 + `warning` + 保留旧账本记录（§4.2 F10） |

**C4 的源 pid 排除（必须显式，否则本项恒拒）**：既有判据 `_bare_name_clashes_pids`（`installer.py:229-238`）对
「同裸名不同 pid」即报冲突，而迁移的源 pid `<p>@<old>` 与被迁移目标 `<p>@<new>` **恰好同裸名**；源 pid 在 P3 之前
一直在 merged `installedPlugins` 与账本里。因此：

- C4 与写路径都必须**显式排除本项全部源 pid**（含其它正在迁移的项）；
- P1 的 `installedPlugins` 写入**不得**走 `guard_bare_name=True`（`installer.py:715-733` 的锁内复核会当场抛
  `validation`）；迁移用非守卫 RMW，并在命令文档里说明这一点；
- 代价是 P1→P3 窗口内确实存在「同裸名双装」瞬态。该瞬态会让**并发**的 `plugin install/enable` fail-closed 拒绝
  第二个同名插件——这是可接受的（迁移期间本就应该停机，§7.3），且因为两 pid 属同一次迁移，不会被误判为长期违规。

**目标名已被同源占用（C2 的同源分支）不是冲突**，而是「用户已经以新名 add 过」的**合并场景**：

- 目标目录存在且清单名 = `new`，`source` 与旧记录相等 → 视为 `already-migrated`：
  仅把意图 / trust / input / secret 从旧名改指新名，删旧账本条目；**不**再搬迁目录、**不**删目标目录。
- 两侧插件集不一致（目标名下少装了某插件）→ 不是冲突；按 F1 把缺的插件意图补到新名下（照抄旧值）。

**检查顺序（`--name` 过滤不得改变判定）**：C1/C2/C4 必须先在**全量 legacy 集**上判定，再套 `--name` 选择，
否则过滤掉重复目标中的一个会让 C1 假性消失、迁出双份新名。滤除项标 `not-selected`，不参与执行但仍出现在 dry-run 里。

### 5.2 迁移中途的失败（非冲突）

单项内部的失败（settings 写失败 / rename 失败 / keyring 不可用）**不**改冲突分类，也不跨项传播：

- 该项标 `failed`（或 `skipped`，见 F6），其余项继续；
- 退出码 1；
- 已完成的步骤保持（前向可续，§7.6）。

### 5.3 输出形状

沿用 #260 裁决 6 的结构化失败先例（`cli/commands/plugin.py:115` `_plugin_error` 的 `{"error": "validation", ...}`）：

```json
{
  "command": "marketplace migrate",
  "dryRun": true,
  "items": [
    {
      "old": "acme", "new": "acme-skills", "status": "rename",
      "origin": "url-derived|explicit-alias|unknown",
      "source": "https://…(脱敏展示)",
      "plugins": [{"id": "figma@acme", "newId": "figma@acme-skills", "action": "move"}],
      "actions": {
        "installedPlugins": 2, "enabledPlugins": 2, "trustedMarketplaces": "add+remove",
        "inputValues": 3, "secrets": 1, "catalog": "rename", "externalTrees": 1, "ledger": "rebuild"
      },
      "warnings": ["plugin 'x' not present in the new manifest"],
      "followUps": [
        {"file": "/repo/.tfrobot/settings.json", "pointer": "/enabledPlugins/x@acme",
         "effect": "will-be-inactive", "suggested": "…"}
      ]
    }
  ],
  "conflicts": [{"code": "duplicate-target", "names": ["a", "b"], "target": "acme-skills"}],
  "skipped": [{"name": "foo", "reason": "manifest-unreadable"}]
}
```

`--dry-run` 与实跑的 JSON 形状一致（`dryRun` 区分），便于脚本先审后跑。

---

## 6. dry-run 输出（人读形态）

```text
Marketplace name migration plan (dry run)
 Old      New          Status     Plugins  Trust  Inputs  Catalog  Follow-ups
 acme     acme-skills  rename           2  ✓→✓         3  rename   1 will-be-inactive
 legacy2  —            skipped          —  —           —  manifest-unreadable

Extra-known declarations that will resurrect the old name (fix before/after migrate):
  /repo/.tfrobot/settings.json: extraKnownMarketplaces.acme
```

零计划（全部合规 / 全部 grandfather）时打印一行说明并退出 0；有 conflict 退出 1；仅 skipped / follow-up 退出 0（不阻塞）。

---

## 7. 执行顺序、幂等与崩溃恢复

### 7.1 单项阶段（每个 marketplace 独立、串行）

```text
P0  preflight      读盘、计算计划、冲突检测                零写入（唯一可「零变更退出」的阶段）
P1  additive(new)  新名侧权威面全部写入（旧名侧保持）      installedPlugins / enabledPlugins(user) / trust / input 值 / secret
P2  catalog swap   known[new] → rename catalog → rename .plugins/<old> → materialize(new_pid)
P3  switch-off     旧名侧权威面删除                        enabled / installed / trust / input 值 / secret
P4  ledger cleanup 重建成功的 pid 才删旧账本记录（F10）
P5  report         汇总 + 依赖告警（project/local、extraKnownMarketplaces、blocked）
```

**为什么 P1 必须整体早于 P2**：P2 一旦搬目录，旧 pid 立刻无法解析；因此新名侧的全部权威意图必须在搬目录**之前**就位，
否则搬完就会出现「旧的坏了、新的也没配上」的熄灯态。

**为什么 P2 内先写 `known[new]` 再 rename**：这是 §7.4 崩溃恢复成立的前提——`known` 里有 `new` 条目，
才能在「目录已搬但 known 未更新」的反向窗口之外，识别「known 已写但目录还没搬」的正向窗口并认领旧目录。

**为什么 P3 在 P2 之后**：P2 的 `materialize_plugin` 需要新意图（`installedPlugins` 有 `<p>@<new>` 不是必需，
但 `reconcile`/boot 的活跃门控需要）与 `known[new]`；先满足它，才能安全地撤旧。

**为什么 P4 在 P3 之后**：删旧账本记录前必须确认新记录已建成；同时 P4 只处理「新记录存在」的 pid。

### 7.2 幂等（同一命令重复执行）

每个步骤都是「**看当前状态 → 只在缺失时写**」：

| 步骤 | 幂等判据 |
|------|----------|
| P1 | 新 pid / 新 key / 新 trust 已存在 → no-op（按旧值覆盖写，等价） |
| P2 | 目录已在 `<new>` → 跳过 rename；`known[new]` 已存在且 source 相同 → 覆盖写 |
| P2 | 新账本记录已完整（`ledger_entry_fully_materialized`）→ 跳过重建 |
| P3 | 旧键已不在 → no-op |
| P4 | 旧记录已不在 → no-op |

因此**重跑 = 续跑**。

> **注意**：若 `extraKnownMarketplaces` 声明了旧名且宿主确实调用了 `reconcile`，`known[old]` 会被重建、
> dry-run 永远非空（§2.3）。这是**已知后果**而非迁移 bug；dry-run 会在该情形打专门告警。

### 7.3 崩溃恢复（在任意点被杀）

设计不引入 journal，靠**状态自描述** + 固定顺序：

| 崩溃点 | 磁盘事实 | 续做方式 |
|--------|---------|----------|
| P1 中 | 新侧部分在、旧侧齐全 | 重跑 P1 补齐 |
| P1 后 / P2 中（`known[new]` 已写、目录未搬） | `known` 有 old+new，`dir(new)` 缺失，`dir(old)` 内清单名 = new | §7.4 认领：`dir(old)` 属于 `new` → 继续 rename |
| P2 中（catalog 已搬、外树未搬） | `dir(new)` 在、`.plugins/<old>` 在 | 重跑只补外树 rename（F8 已满足 → 跳过） |
| P2 中（目录已搬、`known[old]` 未删） | `known` 有 old+new，两边目录状态已对齐 | 重跑：三条件同时成立才判 `old` 已被本命令替换并删键 —— ① `known[new]` 存在且 `source` 与旧记录相同；② `dir(old)` 缺失；③ `dir(new)` 内清单名 = new。**这不是检测判据**（D3 仍要求账本键 ≠ 清单名），而是「本命令自身 P2 留下的收敛收尾」，三者缺一即按 §3.3 grandfather |
| P3 中 | 旧侧部分已删 | 重跑 P3 补齐 |
| P4 中 | 部分新账本记录在 | 重跑只补缺失项 |

**前提**：迁移期间 **Computer / REPL 应停止运行**（见 §7.5 的并发说明）。命令启动时做一次**尽力而为**的锁探测
（非持有），失败只提示、不阻断。

### 7.4 「可认领目录」规则（崩溃恢复的核心）

适用条件：`known[new]` 已写、但 `marketplace_skill_dir(home, new)` 不存在（P2 中断在 rename 之前）。

```text
candidates = [D for D in <home>/marketplace/*            # 排除 .plugins
              if D.is_dir() and D.name not in known_keys and manifest_name(D) == new]
if len(candidates) == 1: 认领 D，把它 rename 到 dir(new)
if len(candidates) == 0: 判 conflict: catalog-lost（不猜，报告 + 建议 --reclone）
if len(candidates) >  1: 判 conflict: ambiguous-catalog（不猜，列出候选交用户）
```

两条附加判据（缺一即为「猜」）：目录名**不是任何 `known` 键**（排除已注册的其它 marketplace）；
其内清单名恰好等于 `new`（不是 `old`——若等于 `old` 说明它根本没被搬过，属正常 P2 之前的状态）。

> 这条规则只覆盖「目录已搬离旧名、还没搬到新名」的窗口。**目录名与清单名都还是旧名**时不适用——那是尚未开工的正常状态。

### 7.5 目录搬迁原子性与并发

`os.rename`（`Path.rename`）在同一文件系统内原子：`<home>/marketplace/<old>` → `<home>/marketplace/<new>`
同父目录、同挂载点，恒成立；`.plugins/<old>` → `.plugins/<new>` 同理。**不做**跨设备 fallback（不复制、不删源）——
跨设备意味着 `A2C_SKILL_HOME` 被拆开挂载，属异常部署，直接报错更安全。

并发：本命令**不加全局锁**。既有写原语（`update_known_marketplaces` / `update_installed_plugins`，`store.py:524-568`）
各自持旁车锁做 RMW，但**跨文件的顺序一致性没有锁**。因此：

- 命令启动时用 `file_lock(..., retries=0)` 做一次**瞬时探测**（拿到就立刻释放）——只用于提示「另一个 a2c 进程正在写物化文件」；
  **不得**常驻持有：现有写原语会在内部再次 `file_lock` 同一旁车文件，同进程重入会退避后抛 `SettingsLockError`
  （`store.py:204-260`、`:510-511` 的显式警告）；
- 探测**不是**活性信号：旁车 `.lock` 文件创建后不删除（`store.py:225-227` 只 `O_CREAT`，无 unlink），
  它的存在**只**代表「曾经写过」，不能据此判断有没有进程在跑。文档与提示文案都必须写清这一点，避免制造恒真告警。

### 7.6 没有回滚

回滚要求把已写的用户意图再改回去，而 P1 之后的状态是「新侧就绪、旧侧完整」——此时撤销新侧会得到
「旧侧仍在但即将被 P2 破坏」的中间态；P2 之后旧侧已不可用，撤销新侧只会得到**旧新皆无的熄灯态**，并把已重键的
plugin-scoped input/secret 变成新名下的孤儿键（旧键已删）。**物理上不存在「回到迁移前」** 的路径。

因此：

- **唯一可零变更退出的是 P0**（冲突 / 缺 `--yes` / 用户拒绝确认）：此时尚未写任何字节；
- P1 之后只有**前向收敛**：失败 → 报告 → **重跑**；不接受「回滚」语义，也不提供 `--rollback` 开关。

---

## 8. 离线 / 无清单 / 缓存不可用

| 情形 | 行为 |
|------|------|
| 有本地 clone + 清单可读 | **完全离线迁移**（默认路径：只读盘 + rename + 写 settings，无网络） |
| 有 clone 但清单不可读 | grandfather（`manifest-unreadable`）；提示先 `marketplace refresh`（可能修好） |
| 无 clone | grandfather（`catalog-missing`）；提示按 `docs/upgrade-0.5.1-marketplace-catalog-recovery.md` 恢复 |
| rename 失败（权限 / 软链 / 占用） | 整项中止，保留旧状态；`--reclone` 时改为从记录 source 重 clone 到新名（**需要网络**） |
| 独立源 plugin 的外树 rename 失败 | 同上；`--reclone` 时对每个独立源 plugin 重新 `locate_plugin_root`（`staging.py:1004`） |
| keyring 不可用 | secret 项 `skipped`（报告里明确「重录需重新输入」），其余面照常迁移 |
| `input-values.json` 不可读 | F5 整项跳过 + WARN（不覆盖不可读文件，绝不「修复」成空 map） |
| 网络 | **默认不使用**。`--reclone` 是唯一会联网的开关，且只在 rename 失败时触发 |

> 离线优先级的设计理由：迁移是**本地状态整理**，不是同步；能在磁盘上完成的事绝不依赖远端。

---

## 9. project / local 只报告

- 报告内容：文件绝对路径 + JSON 点路径（如 `/enabledPlugins/figma@acme`）+ 建议改法 + **后果**
  （`will-be-inactive` / `will-stay-disabled`）；
- 绝不写入（含「顺手清掉旧键」）：project 入 git、随仓库分发（`schema.py:139-173` 的受信供给讨论同源关注点）；
  local 虽不入 git（`scope.py:101-103`），仍按用户裁决（Q7）保持同处置口径；
- `--json` 下进 `item.followUps[]`；人读形态在表格下方汇总一行 + 详情见 JSON；
- **不**因为存在 project/local 残留就把该项判失败——它是正常可预期状态（D6），但警告必须显著。

---

## 10. CLI 形态

### 10.1 非交互（Typer）

```bash
a2c-computer marketplace migrate [--name <old>]... [--dry-run] [--yes] [--json] [--reclone]
```

| 选项 | 语义 |
|------|------|
| `--name <old>` | 只迁移指定旧名（可重复）；缺省 = 全部 legacy。**只缩小选择范围，不缩小冲突判定范围**（§5.1） |
| `--dry-run` | 只出计划，零写入 |
| `--yes` | 非交互确认（**非交互必需**，与 `marketplace add --trust` 同纪律） |
| `--json` | 结构化输出（含 dry-run） |
| `--reclone` | 允许在 rename 失败时从记录的 source 重 clone（联网） |

退出码沿用 `marketplace.py:63-65`：`0` 成功（含全 no-op）/ `1` 用户错误（冲突、缺 `--yes`、写失败）/ `2` 网络错（仅 `--reclone` 路径）。

### 10.2 交互（REPL）

`marketplace migrate [--dry-run]` → 打印计划 → 沿用 `repl_dispatch` 的 `_confirm` 风格确认 → 执行 →
成功后 `comp.mark_skills_dirty()`（同 `marketplace add/remove/refresh` 的既有标脏点，`marketplace.py:481-510`）。

### 10.3 名称与位置

- 子命令名 `migrate`（不复用 `rename`：一次可能迁多个，且目标是清单名而非用户指定的新名）；
- library 层入口 `marketplace_migrate(...)`，与其它 handler 同形（显式 `registry/home/env` + flags + 返回退出码），
  放在 `a2c_smcp/computer/cli/commands/marketplace.py`；纯逻辑抽到
  `a2c_smcp/computer/settings/marketplace_migration.py`（检测 / 计划 / 执行，便于单测）；
- **单一权威**：检测 / 计划 / 执行全在库层，CLI 只做参数解析与展示（同 #260 详细描述第 10 条）。

### 10.4 `marketplace info` 增加 `manifestName`（Q9 已裁决，Phase 2 一并实现）

`marketplace_info`（`marketplace.py:298-328`）当前 payload 为
`name / url / installLocation / commitSha / autoUpdate / trusted / lastUpdated / installedPlugins`，**不含清单名**，
导致用户无法自查「我这条注册是不是 legacy」。Phase 2 在该 payload（及其人读表格）中增加：

- `manifestName`：本地 clone 清单顶层 `name`；不可读时 `null`；
- `legacyName: bool`：`manifestName is not None and manifestName != name`（= §3.1 判据的直接呈现）。

两者**只读派生**，不写盘、不影响 `known_marketplaces` 记录形态（不是 #260 baseline 字段，勿混）。

---

## 11. 与相邻 Issue 的边界

| Issue | 关系 |
|-------|------|
| #260 | 提供 D4 baseline（**硬前置**）；本设计只读它、不重复实现；#260 的 `--name` 语义（D6 B）决定「显式别名不再被支持」→ 别名必须迁移 |
| #270 | 提供严格派生与同源校验；本设计**不重算**旧名，只用账本值比对 source |
| #276 / #263 | 展示与日志构造期脱敏；迁移输出中的 source URL 一律走 `known_git_source_for_display`（`utils/git_url.py:93`；CLI 包装 `marketplace.py:93` `_display_source_url`） |
| #275 / #271 | installPath / ledger 语义。本设计**不重写** `installPath`（§2.2），若这两单把账本升级为 installPath 权威，需回来复核 F10 |
| #257 | 跨 marketplace 裸名拦截（已合入）；C4 复用其判据但**必须排除源 pid**（§5.1） |
| #267 | 迁移文档先例（`docs/upgrade-0.5.1-marketplace-catalog-recovery.md`），本单的迁移文档与之并列 |

---

## 12. 测试计划（Phase 2 验收映射）

| 验收条目（Issue） | 测试 |
|---|---|
| 旧名来源（URL / `--name`） | 两个夹具：`add <url>`（派生名 ≠ 清单名）、`add --name custom`，同一迁移路径；断言 `origin` 标签（若实现） |
| 同名多旧名 | `duplicate-target` 冲突，两者均不改写 |
| 冲突拒绝 | C2（同名不同源）/ C4（裸名）各一例：退出码 1、零写入（字节级比对 settings） |
| **C4 源 pid 排除** | 旧 pid 已装、无第三方同名 → 迁移成功；注入一个**第三方**同裸名 marketplace → 拒绝。变异：删掉源 pid 排除 → 第一个用例必须转红 |
| 幂等重放 | 连续跑两次：第二次全 no-op，settings/账本/目录 mtime 不变 |
| 崩溃恢复 | 注入点（monkeypatch 在 P1/P2/P3 中途抛）→ 重跑 → 终态一致；**必须真造出「rename 已完成、`known[new]` 未写」与「`known[new]` 已写、rename 未做」两种磁盘形态**，否则 §7.4 的规则永远假绿 |
| project/local 不改写 | project settings 含旧键 → 文件字节不变 + `followUps[]` 有该项且 `effect == "will-be-inactive"` |
| **project scope 安装仍启用** | `plugin install <pid> --scope project` + `enable` 后迁移 → 断言合并视图的活跃判据结果**不劣化**（不静默熄灯）；同上构造 local scope 两例 |
| secret 重键 | fake keyring：新键写入后旧键删除；keyring 不可用 → 旧键保留 + `skipped` |
| 离线 grandfather | 删掉 clone / 破坏清单 → `skipped`，零写入，退出码 0 |
| input 值迁移 | `input-values.json` 单次原子写（写入次数 spy）+ 值逐字节保留 |
| 账本重建 | `installPath` 指向新目录、`scope`/`projectPath`/`version` 与旧记录一致；旧 pid 记录删除（重建成功）/ 保留（重建失败注入） |
| 外树搬迁 | 独立源 plugin：`.plugins/<old>` → `<new>` 成功 / 失败中止 / `--reclone` 重建 三分支 |
| known 记录形态 | 新记录字段集与 staging 写出的一致，且 `installLocation` 指向 `<home>/marketplace/<new>` |
| 并发提示 | 先跑一次正常 `marketplace add`（留下 `.lock`）→ 迁移**不**报「另一实例在运行」；另一端真持锁时 → 探测提示 |
| 顺序无关 | 打乱 `known_marketplaces` 键序（同 rust serde Map）→ 计划与结果不变 |
| `info` 展示字段（§10.4） | legacy 注册 → `manifestName != name` 且 `legacyName is True`；清单不可读 → `manifestName` 为 `null`、`legacyName` 为 `False` |

**变异验证要求**（沿用本仓纪律）：至少对「先加后减」「先写新 secret 再删旧」「P4 成功才删旧账本」「C4 排除源 pid」
四处做变异，各至少 1 个用例转红。

---

## 13. 迁移文档与 release notes（草案，Phase 2 落盘）

### 13.1 迁移文档 `docs/upgrade-0.5.1-marketplace-name-migration.md`（结构）

```text
# 升级迁移：marketplace 注册名改为清单 name
1. 背景与影响面（旧名来源：URL 派生 / --name）
2. 如何判断自己受影响
   a2c-computer marketplace info <name>           # manifestName 与 name 不同 → legacy
   a2c-computer marketplace migrate --dry-run     # 计划非空即受影响的完整清单
3. 迁移步骤
   a2c-computer marketplace migrate --dry-run     # 先看计划与冲突
   a2c-computer marketplace migrate               # 交互确认后执行（脚本用 --yes）
4. 破坏性说明
   - plugin id 变化（<plugin>@<old> → <plugin>@<new>），外部脚本 / 文档引用需同步
   - 用 --scope project / local 安装并启用的插件：本命令不改写这两层，需要在对应
     .tfrobot/settings[.local].json 手工改 enabledPlugins 键，否则迁移后处于未启用态
   - extraKnownMarketplaces 若声明了旧名，需自行改声明（否则 reconcile 会重建旧名注册）
5. 离线 / 无清单（grandfather）行为
6. 迁移后确认（marketplace list / plugin list / plugin enable）
7. 失败与续跑（重跑即可；无回滚）
```

> **自查路径依赖 §10.4**：`marketplace info` 当前 payload 不含清单名，Phase 2 一并加 `manifestName` / `legacyName`
> （Q9 已裁决）。迁移文档的两条自查路径都以该字段为前提，故 §10.4 与本文档必须同 PR 落地。

### 13.2 CHANGELOG / release notes 要点

- **Breaking**：marketplace 注册名 = 清单 `name`；`<plugin>@<marketplace>` 中的 marketplace 段随之变化（#260 已标）；
- 新增 `marketplace migrate`（含 `--dry-run`）；**不做**自动迁移，升级后旧名可继续使用直到显式迁移（不静默改名）；
- 迁移影响面：安装/启用意图、trust、plugin-scoped input 与 secret；project/local 只报告（迁移后需手工处理）；
- 指向迁移文档。

---

## 14. 实现落点（Phase 2 预排）

| 模块 | 改动 |
|------|------|
| `settings/marketplace_migration.py`（新） | 检测 / 计划 / 执行（纯编排 + 显式资源注入，便于单测） |
| `cli/commands/marketplace.py` | 新增 `marketplace_migrate` handler + REPL dispatch 分支；`marketplace_info` 增加 `manifestName` / `legacyName`（§10.4，Q9） |
| `cli/main.py` | 注册 `marketplace_app.command("migrate")` |
| `settings/installer.py` | 复用 `_write_installed_plugin` / `_write_enabled_plugin` / `materialize_plugin`，**不新造写原语** |
| `inputs/plugin_pool.py` | 复用 `prefix_input_id` 做键改写（不复制格式字符串） |
| `inputs/value_store.py` / `inputs/secret_store.py` | 复用 `ValueStore` / `SecretStore` 原语；批量重键逻辑放迁移模块（不改两 store 的公共语义） |
| `skills/home.py` / `skills/staging.py` | 复用 `marketplace_skill_dir`（`home.py:129`）与 `external_plugin_dir`（`staging.py:891`）计算路径 |

> 所有跨模块的路径/键格式一律**复用既有函数**，不写字面量——与 `SCOPE_ORDER` 单一权威同理（`schema.py:70-109` 的教训）。

---

## 15. 裁决记录（2026-11-17 架构师审查通过）

| # | 问题 | 结论 | 状态 |
|---|------|------|------|
| Q1 | 命令名 `marketplace migrate` 与 rust 侧命名是否统一 | 保持 python 自有命名，语义在 Discussion#81 对齐即可 | ✅ 已批准 |
| Q2 | 非交互是否强制 `--yes` | 强制（破坏面大，参照 `--trust` 纪律） | ✅ 已批准 |
| Q3 | F10 删旧账本记录 vs 保留为孤儿；`installedAt` 是否加参沿用 | 重建成功才删旧记录；`installedAt` 由重物化重写，不加参（留待 #271/#275 一并考虑） | ✅ 已批准 |
| Q4 | `origin` 诊断标签是否 Phase 2 就做 | 延后（§3.2），Phase 2 只输出 `legacy` | ✅ 已批准 |
| Q5 | `blockedMarketplaces` 陈旧条目 | 本次只报告；**要求 #80 落地时复查**（fail-open 风险） | ✅ 已批准 |
| Q6 | baseline 未落地时的降级行为 | Phase 2 **硬前置**，不降级实现 | ✅ 已批准 |
| Q7 | local scope 是否应改写（它不入 git，D6 的理由只对 project 成立） | **用户裁决：local 也只报告不改**，保持 D6 单一处置口径 | ✅ 已裁决 |
| Q8 | Discussion#81 的 rust secret 键空间说法 | **已按代码订正**：两侧均 scoped id、各自重键；§2.4 正文已与该结论一致 | ✅ 已订正 |
| Q9 | `marketplace info` 是否增加 `manifestName` | **用户裁决：增加**，Phase 2 一并实现（§10.4，含 `legacyName`） | ✅ 已裁决 |

---

## 16. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-11-17 | Phase 1 初稿（Issue #268）；隔离审查后修订 7🔴+8🟡（阶段顺序重排、可认领目录判据、C4 源 pid 排除、F13 拆分、`installedAt` 不可沿用、外树纳入 P2 等） |
| 2026-11-17 | 架构师审查通过；落定 Q1–Q6（批准）、Q7/Q9（用户裁决）、Q8（Discussion#81 已订正）；新增 §10.4 `manifestName` 面 |
