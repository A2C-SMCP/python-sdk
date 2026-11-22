# 设计文档：0.5.1 — 存量 marketplace 注册名迁移（D3）

> 状态：**P0 设计修订已按 2026-11-22 用户裁决回填（docs-only）**；实现（P1）待本设计合并后以**单一 PR**落地。
> Phase 1 设计于 2026-11-17 审查通过；2026-11-22 裁决 supersede 了其中的 D7/D10/D12 与 §7.4 判据（见 §15 Q10–Q16）。
> 前置：#260（核心语义 + **D4 legacy refresh baseline**，已合）、#270（派生算法与凭据 slug 约束，已 CLOSED）、
> #276（staging/source 安全，已合）、#275（账本 installPath 语义，已 CLOSED）——**均已满足**。
> 关联：python-sdk#268（本单，v0.5.1 硬门）/ #260 / #270 / #276 / #275 / #271 / #295 / #296 / rust-sdk#289（镜像，已实现）/
> rust-sdk#272 / a2c-smcp-protocol#79（协议裁决）/ protocol#86（v0.5.1 发布门）/
> Discussion [a2c-smcp-protocol#81](https://github.com/A2C-SMCP/a2c-smcp-protocol/discussions/81)（双端对齐留痕）。
> 性质：**SDK 自治**（protocol#79 定「名称唯一性 / 禁止静默改名」归协议管辖，**派生与迁移算法归 SDK 自治**）；
> 无 wire 变更、无新事件、无新错误码、不动协议结构。列表相邻项：#296（flag/policy 引用守卫）、#295（展示脱敏收尾）
> 建议先合或同批，减少 rebase 与双套脱敏；本设计只依赖它们的既有原语，不阻塞。

---

## 0. 核心决策摘要（先看这张表）

| # | 决策 | 结论 |
|---|------|------|
| D1 | 迁移时机 | **显式命令**触发；**不做** boot 自动迁移、不做隐式改名 |
| D2 | 目标名来源 | 本地 clone 的**清单顶层 `name`**；不读网络、不用当前 URL 重算旧名（#270） |
| D3 | 旧名识别 | 「`known_marketplaces.json` 键 ≠ 该键 clone 目录内清单 `name`」= legacy；**账本 + 清单为唯一判据** |
| D4 | 不可判定 | 目录缺失 / 清单不可读 / 清单名非法 → **grandfather 保留旧名** + 报告，绝不猜 |
| D5 | 权威 vs 派生 | 用户意图（`installedPlugins` / `enabledPlugins` / `trustedMarketplaces` / **`blockedMarketplaces`** / plugin-scoped input+secret）**改写**；`extraKnownMarketplaces` 默认只报告（`--update-declarations` 才改 user scope）；账本与 clone **重建**，不做字符串重写 |
| D6 | 可改写边界 | user scope 改写；**project / local 只报告不改**（用户裁决 #268）。project 入 git；local 虽不入 git，同样不代改（保持单一处置口径）。报告必须显式标出「这些插件迁移后将处于未启用态」与「这些旧名引用将失效」 |
| D7 | 冲突 | **2026-11-22 裁决（supersedes 原「整项拒绝、其余继续」）**：预检任一**涉及选中项**的冲突 → **整体 abort、零写入、退出码 1**；仅**未选中项之间**的冲突 → 报告但不阻塞。不半途改名、不部分迁移 |
| D8 | 幂等 | 每个 marketplace 独立、前向收敛；重复执行自动 no-op；任意一步崩溃后可**重跑续做**（无 journal） |
| D9 | 迁移顺序 | 「新侧就绪 → 换目录 → 撤旧侧」；**不提供回滚**，P0 之后只有前向（与 rust journal/备份/rollback 的差异已由 Discussion#81 接受） |
| D10 | secret 重键 | **先写新键、回读验证后删旧键**；旧 secret 存在且 keyring 不可用 → **按 marketplace abort（零写入）、其余项继续、退出码 3**；目标键已存在且值不同 → **abort**（不覆盖既有 secret）；绝不落明文 |
| D11 | 跨 SDK | 迁移语义**各自实现**；python 只处理自己的键空间（两侧键空间实际同构，但实现与发布互不阻塞，见 §2.4）。rust#289/PR#299 **已实现**（含 journal/备份/`--rollback`，为已接受差异） |
| D12 | 命令形态 | `a2c-computer marketplace migrate [--name <old>]... [--apply] [--update-declarations] [--json]`；**默认 dry-run**（零写入），`--apply` 才写；**删除 `--yes` / `--reclone`**；退出码 **0 / 1 / 3**；REPL **只允许 dry-run** |
| D13 | 只读预检 | dry-run 与 apply 的预检一律走 **strict 只读 loader**：状态文件损坏 → **命令级 abort（退出码 3）、零写入、不 rename 损坏文件**（不走 `.corrupt-*.bak` 恢复路径） |
| D14 | secret 候选派生 | **不按键前缀枚举 keyring**（无跨平台列举 API）；从 catalog clone / 外树的 `mcp-servers/inputs.json` 枚举 `password: true` 候选，已从清单移除的 plugin 经账本 `installPath` 定位；无法证明无旧 secret → 按 marketplace abort |

---

## 1. 范围

### 1.1 在范围

- 将**存量**注册名（URL 派生名 / 显式 `--name` 别名）迁移为清单 `name`，覆盖 Issue 列出的全部权威面与派生面。
- 迁移的**检测、规划、dry-run、执行、冲突拒绝、崩溃恢复、离线策略**。
- 迁移文档与 release notes 的**内容草案**（随 P1 实现 PR 落地）。

### 1.2 不在范围

- **不改** #260 的核心语义（注册名取清单 name、同名不同源拒绝、refresh 改名拒绝、`--name` 仅作期望名校验），只消费它产出的 **legacy refresh baseline**（`manifestNameBaseline`，已由 #260 交付）。
- **不改** #270（派生算法 / 同源校验）、#276（staging & source 脱敏与路径穿越）、#275 / #271（installPath / ledger 语义）。
- 不引入 Computer 级新 API；不做跨 SDK 的统一迁移工具；不动协议。
- **不引入 journal / 备份 / `--rollback`**：python 走 forward-only（状态自描述 + 重跑续做），与 rust#289 的 journal/备份/回滚为 Discussion#81 已接受差异。
- `extraKnownMarketplaces` **默认只报告**；仅 `--update-declarations` 改写 **user scope**，project/local 永远只报告（§2.3）。
- `blockedMarketplaces` **user scope 改写**（与 trust 同批 raw RMW），project/local 只报告（§4.1 F13a）。
- `baseline` 缺失的**合规**注册（注册名 = 清单名）不承担回填：由既有 refresh 路径补写（#260），本命令不改写已合规项。
- 不迁移 env 注入值（`A2C_SMCP_<ENV_SEGMENT>`）：键随 scoped id 变化，只能报告（§4.1 F14）。

### 1.3 术语

| 术语 | 含义 |
|------|------|
| **旧名 / legacy 名** | `known_marketplaces.json` 的键 ≠ 该键 clone 内清单 `name` 时的那个键 |
| **目标名 / 清单名** | 本地 clone 的 `.tfrobot-plugin/marketplace.json` 顶层 `name` |
| **权威面** | 用户意图，SDK 无权丢弃，只能按迁移计划改写 |
| **派生面** | SDK 自己维护的物化状态，删除可重建（协议 runtime-contract §4.9「账本可弃、删除它无损」） |
| **grandfather** | 保留旧名不动，只报告（协议 §5.14 授权的存量一致性交给 SDK 迁移方案） |
| **unresolved** | 本命令无法安全迁移的项（grandfather 的一档）：保留旧名、零写入、退出码 3 |
| **blocked** | 迁移会违反已定规则（如 #260 的 baseline 改名拒绝），必须用户先处置：保留旧名、零写入、退出码 3 |
| **项级 abort** | 预检/执行中期发现的项内问题（keyring 不可用、目标键异值、rename 失败）：该项不继续写，其余项继续，退出码 3 |

---

## 2. 权威模型（权威 / 派生两层）

### 2.1 权威面（迁移必须改写）

| 面 | 位置 | 键形态 | 代码 |
|----|------|--------|------|
| 安装意图 | user `settings.json` → `installedPlugins` | 数组，元素 `<plugin>@<marketplace>` | `installer.py:692` `_write_installed_plugin` |
| 启用意图 | **按安装 scope**：user / project / local 各自的 `settings[.local].json` → `enabledPlugins` | 对象，键 `<plugin>@<marketplace>`，值 bool | `installer.py:311` `_write_enabled_plugin`；写入 scope 取自账本条目（`installer.py:1506` `enable_plugin` 的 `scope`，`cli/commands/plugin.py:391` 从账本逐条取） |
| 信任 | user `settings.json` → `trustedMarketplaces` | 字符串数组，元素 = marketplace **name** | `marketplace.py:239` `_load_trusted` / `:244` `_record_trust` / `:262` `_revoke_trust` |
| 阻断 | user `settings.json` → `blockedMarketplaces`（project/local 只报告） | 字符串数组，元素 = marketplace **name** | `schema.py:123/339` 声明；**无执行点**（见 §4.2 F13a） |
| plugin-scoped input 值 | `$XDG_STATE_HOME/a2c/input-values.json` | 扁平 map，键 `<plugin>@<marketplace>/<id>` | `plugin_pool.py:36` `prefix_input_id`；`value_store.py:37` `resolve_value_store_path` / `:55` `ValueStore` |
| plugin-scoped secret | OS keyring，service `a2c-computer` | 键同上 | `secret_store.py:26` `SERVICE_NAME`；键在 `resolver.py:185` 起算并以 `resolved_id`（scoped）存取 |

> **启用位随安装 scope 落盘**（易被忽略，是本设计最大的正确性风险点）：`plugin install <pid> --scope project` 之后
> `plugin enable` 会把 `enabledPlugins[<pid>] = true` 写进 **project settings**（`plugin.py:391` 从账本逐条取 scope，
> 注释明写「绝不默认 `user`」）。因此迁移只改 user 层会让这类插件在合并视图里**既没有新键、旧键又不满足
> `key ∈ installed` 门控**（活跃判据 `recovery.py:124-129` / `reconciler.py:140-156`）——即**静默熄灯**。
> 处置见 §4.1 F2/F3 与 §5.1 C3。

> **安装意图固定落 user scope**：`installedPlugins` 是全局一次的事实，SDK 不随 `--scope` 写进可提交文件
> （`installer.py:697-699`）。读取侧是 merged 并集（`reconciler.py:127-137`），故 project/local 的手写声明也会被认可——
> 迁移对它是**只报告**（D6）。

### 2.2 派生面（优先重建，不做字符串重写）

| 面 | 位置 | 迁移方式 |
|----|------|----------|
| marketplace 物化记录 | `$A2C_SKILL_HOME/known_marketplaces.json` → `marketplaces[<name>]` | **以新名重写记录**：`source` / `commitSha` / `autoUpdate` / `lastUpdated` / 未知扩展键从旧记录复制，`installLocation` **重算**为 `marketplace_skill_dir(home, new).resolve()`，`manifestNameBaseline` **重置为新清单名**（**不沿用旧 baseline**，见下），随后删旧键 |
| catalog clone | `<home>/marketplace/<name>/` | 目录搬迁（离线、原子）；**失败即按项 abort**，不做重 clone——缺 catalog 由用户按 `marketplace add/refresh` 恢复（`--reclone` 已删除） |
| 外部 plugin 树 | `<home>/marketplace/.plugins/<mp>/<plugin>/` | 目录搬迁（同级 rename，与 catalog 搬迁同阶段） |
| 安装账本 | `$A2C_SKILL_HOME/installed_plugins.json` → `plugins[<pid>]` | **不重写 pid / installPath**：先以新 pid `materialize_plugin` 重建，成功后才删旧记录 |
| 运行期 SKILL 引用 | `A2CSkillRef.source = marketplace:<name>` | 不持久化；下次 boot / restage 自然刷新 |
| 运行期 SKILL name | `<plugin>:<skill>` | **不含 marketplace**（`naming.py:197-211`），改名零影响 |

> **`manifestNameBaseline` 必须重置为 `new`**：该字段语义是「注册锚定的清单名」（`marketplace_identity.py:49`）。
> 若把旧记录的 baseline（= 旧名，或 legacy 缺失）原样带到 `known[new]` 上，下一次 refresh 观测到的清单名
> （`new`）与 baseline 不符 → 触发 #260 §5.14 改名拒绝，迁移后 refresh 立刻坏掉。旧 baseline 与迁移结论
> 无关，**不得复制**。相反，合规注册（注册名 = 清单名）缺 baseline 的，本命令**不**回填——由 refresh 补写。

**为什么不做字符串重写**：`installPath` 是物化事实（相对 source 指向 catalog 内、独立 source 指向 `.plugins/<mp>/<plugin>`），
重写字符串会把「记录」和「磁盘」解耦；而账本本就允许整条丢弃后重建（`store.py:391` `_drop_legacy_mcp_dep_records` 的
legacy 丢弃路径是同一姿态）。
少量重建成本换掉一整类「改了一半」的半态。

### 2.3 `extraKnownMarketplaces` 的权威地位

`marketplace add` **不写**该字段（两侧 SDK 同构）。因此：

- 它**不是** SDK 账本的前驱，不能默认用户都有声明；
- 存在时它是**声明面**（config-first，可能落在 user / project / local 任一 scope，project 还入 git），
  `reconciler.reconcile` 按它做 additive-only 对账（`reconciler.py:325-410`）；
- **2026-11-22 裁决**：默认**只报告不改写**；`--update-declarations` 才改写，且**仅 user scope**
  （project/local 即使带 flag 也只报告——project 入 git、local 虽不入 git 但保持单一处置口径，理由同 D6）；
  声明是用户/仓库的显式意图，与 #260 裁决 2 一致（声明式换源一律 `validation`、需显式 remove/re-add）。
- 带 `--update-declarations` 时：user `settings.json` 的 `extraKnownMarketplaces[old]` **整条迁移**到
  `[new]`（保留 `source` / `autoUpdate` / 未知扩展键；**不**用账本 source 覆盖声明值）；若 `[new]` 已存在
  且 source 不同源 → **冲突 → 整体 abort、零写入、退出码 1**（C9）；同源已存在 → 保留既有条目、删旧键。
- 声明源与已注册源**不一致**时的处置见 #287（[`design-0.5.1-boot-recovery-source-diagnostics.md`](design-0.5.1-boot-recovery-source-diagnostics.md)）：
  boot 只诊断、不换源；迁移**不管**这条路径，二者互不替代。

**已知后果（必须写进 dry-run 告警）**：`reconcile` 的 missing 分支会按声明**重新 clone `<home>/marketplace/<old>`
并把 `known[old]` 写回**（`reconciler.py:325-410`）。本仓内没有 `reconcile` 调用点（boot 走的是
`computer.py` 的治理恢复路径），但它是公开入口（`settings/__init__.py` 导出），**宿主**（TFRobotServer 等）可能直接调用。
若真被调用，迁移后的旧名会被声明面复活，用户会认为迁移失败。故：

- dry-run 对每个命中项给出警告：「`<file>` 的 `extraKnownMarketplaces` 声明了 `<old>`，reconcile 会重建该注册」；
- 带 `--update-declarations` 时上述 user-scope 告警自动消失；project/local 的同类告警**永远存在**（只报告），
  迁移文档给出改声明的具体片段（用户自己改，入 git 的文件不代改）。

### 2.4 跨 SDK 差异（各自实现，不共享代码）

Discussion#81 已双端确认「迁移语义各自实现」。rust 侧实现面差异（**rust#289 / PR#299 已实现并合入 `dev-0.5.0`**，
本表描述的是已实现事实，不再是提案）：

| 面 | python-sdk | rust-sdk（已实现） | 对本设计的影响 |
|----|------------|--------------------|----------------|
| 安装意图 | user settings `installedPlugins` | 独立 `installed_plugins_intent.json`（权威，`A2C_SKILL_HOME`） | 只写 settings 一侧 |
| 安装账本 | `installed_plugins.json`，pid/installPath 可重建 | 同构（派生缓存） | 均可弃重建 |
| `known_marketplaces` 顺序 | dict（保序） | serde_json Map（**不保序**） | 迁移**不得依赖记录顺序**，所有权仲裁以「意图 > 账本 > 顺序」为准 |
| `extraKnownMarketplaces` | add 不写 | add 不写 | 同口径：默认只报告 |
| 凭据 slug | #270 已合入（同源校验） | #285 已合入 | 迁移检测不得用 URL 重算旧名（两侧同口径） |
| 崩溃恢复 | **forward-only**：无 journal / 备份 / `--rollback`，靠状态自描述 + 重跑续做 | journal + 备份目录 + `--resume` / `--rollback <ts>` | **已接受差异**（Discussion#81，2026-11-18/22 两次确认）；语义（检测、冲突整体 abort、secret 重键、baseline、默认 dry-run、`--update-declarations` 仅 user）两侧一致 |

**关于 secret 键空间的订正**（2026-11-17 已写回 Discussion#81）：Discussion#81 的 rust 侧陈述为「rust keyring 键 = 全局 `<id>`」，
据此推出的「python 需重键 / rust 不需要」**与两侧代码都不符**：

- `rust-sdk crates/smcp-computer/src/inputs/plugin_pool.rs:131` 把 plugin input id 前缀化为 `<plugin>@<mp>/<id>`；
- `.../inputs/resolver.rs:178` 算出 `scoped_id`，`:211` `secret_store.get(&resolved_id)`、`:232` `secret_store.set(&resolved_id, …)`
  —— 存取的**就是 scoped id**；
- `.../inputs/secret_store.rs:22` 的**模块注释**仍写「key=全局 `<id>`」，属陈旧注释；**python 侧
  `secret_store.py:17` 有同型陈旧注释**（P1 实现时顺带订正，零行为变化）。

结论：**两侧代码同构**（python `resolver.py:175-211/244-249` 同形，scoped id 存取）。因此：

- 本设计**不**声称「rust 不需要重键」；正确表述是「两侧都需要重键，但各自实现」；
- 对 python 实现无影响：无论 rust 如何，python 只处理 `<plugin>@<mp>/<id>` 键空间。

---

## 3. 旧名识别

### 3.1 判据（唯一）

对 `known_marketplaces.json` 的每个键 `K`：

1. 定位 `dir = marketplace_skill_dir(home, K)`（`skills/home.py:129`，即 `<home>/marketplace/<K>/`）；
2. 读 `dir/.tfrobot-plugin/marketplace.json` 顶层 `name` 得 `M`（`manifest.py:86` `read_marketplace_manifest` /
   `marketplace_identity.py:125` `read_manifest_marketplace_name`）；
3. `M == K` → 已合规，不在迁移面；`M != K` → **legacy**，迁移计划 `K → M`。
4. 判据只看**账本 + 本地清单**；#260 之后账本记录另有 `manifestNameBaseline`（`store.py:101`），仅作 §3.4 的次选判据。

**不做的事**：不调用 `default_marketplace_name(url)`（`marketplace.py:180`，已标 `.. deprecated:: 0.5.1`）判断「这个名是不是 URL 派生的」。
理由（#270）：① 存储的 URL 可能携带凭据/非常规 scheme，重算会把凭据 slug 进名字；② #260 之后派生规则本身被废弃，
用废弃规则「验明正身」没有意义；③ 账本里没有 provenance 字段，任何事后推断都不可靠。

### 3.2 旧名来源（URL 派生 vs `--name`）——**诊断标签，非判据**

Issue 要求覆盖「旧名来源识别」。本设计的答案是：**两种来源在迁移上完全等价**（都是「注册键 ≠ 清单名」，
#260 D6 起显式别名不再被支持，两类都必须迁移），因此**来源分类不参与任何决策**。若需要展示，
只在 dry-run 的 `origin` 列给一个**尽力而为的诊断标签**，且受两条约束：

- 仅当记录的 `source.url` 通过当前（#270 之后的）严格解析时才计算；否则标 `unknown`；
- 计算结果**不得**用于改名、冲突判定或写盘，且输出前必须过 `known_git_source_for_display`（`utils/git_url.py:94`，
  `marketplace.py:155` 的 `_display_source_url` 是它的 CLI 包装），避免把凭据 slug 出来的名字直接展示。

**2026-11-22 裁决：维持 Q4「延后」**——P1 不实现 `origin` 标签。验收改为「**两种旧名来源迁移路径等价，
不要求 origin 标签**」（§12）。跨 SDK 对拍清单同样延后到 P2（post-release）。

### 3.3 grandfather（不可判定即保留）

以下情形**保留旧名**并报告，不迁移、不猜测；均为**逐项非致命**（其余项继续，最终退出码 **3**）：

| 情形 | 状态 / `reason` |
|------|------------------|
| `known_marketplaces` 有条目但 clone 目录不存在（且无 §7.4 的可认领目录） | `unresolved` / `catalog-missing` |
| clone 内 `.tfrobot-plugin/marketplace.json` 缺失 / JSON 畸形 / 无 `name` | `unresolved` / `manifest-unreadable` |
| 清单 `name` 不满足严格 kebab 或超长（`schema.py:213`） | `unresolved` / `invalid-manifest-name` |
| 旧名不适合做路径段（空 / `.` / `..` / 以 `.` 起 / 含 `/` `\\` NUL） | `unresolved` / `unsafe-old-name`（对齐 rust；不把不可信键交给 `rename`） |
| clone 目录是符号链接（不猜解析目标） | `unresolved` / `symlink-catalog`（对齐 rust） |
| 有 `manifestNameBaseline` 且**不等于**当前清单名（#260 视角本就是改名拒绝态） | `blocked` / `baseline-mismatch`——迁移**不得**隐式接受协议禁止的改名；提示先按 §5.14 处置 |
| 被 `--name` 过滤条件排除 | `not-selected`（不计入失败） |

> grandfather 的语义与协议 §5.14 一致：存量一致性交给 SDK 迁移方案；**离线 / 无清单时保留旧名**是用户裁决的第 1 条。
> `unresolved` 与 `blocked` 都**非致命**：不阻断其它项，不产生退出码 1；但必须让最终退出码为 3（部分完成），
> 避免「全绿但实际上有项没迁」的静默假成功。

### 3.4 与 #260 **D4 legacy refresh baseline** 的边界

#260 D4（A）规定：legacy 注册（旧名从未由清单解析）**首次成功 refresh** 时记录 **baseline 清单名**、保留旧名、不报改名拒绝；
仅 baseline 之后再改名才拒绝。**该字段已由 #260 交付**（`store.py:101` `manifestNameBaseline`；写入点
`staging.py:1317-1322`，既有值永不改写、缺失则取本次观测值；判定见 `staging.py:1496-1505`），不再是本设计的硬前置。

本设计：

- **复用 #260 的 baseline 字段与写入点**，**不另立**一套 baseline 存储；
- 检测仍然**优先读本地清单**（离线可用、不依赖 refresh 发生过）；
- baseline 只在「本地清单不可读但 baseline 存在」时作为**次选判据**补齐 `K → M`（补的是 §3.3 表 `manifest-unreadable` 行），
  且要求 `baseline != K`（否则是合规项，不回填）；与本地清单冲突时以**本地清单为准**并记 WARN（清单是当前事实，
  baseline 是历史观测点）；本地清单可读但 baseline ≠ 清单名 → `blocked / baseline-mismatch`（§3.3）。
- 迁移写出的 `known[new]` 一律 `manifestNameBaseline = new`（§2.2），**不复制**旧 baseline。

---

## 4. 迁移面清单（逐面：检测 / 动作 / 失败影响）

### 4.1 总表

| # | 面 | 权威/派生 | 检测 | 动作 | 失败处理 |
|---|---|---|---|---|---|
| F1 | user `installedPlugins` | 权威 | 元素以 `@<old>` 结尾 | 加新元素、删旧元素 | **fail-closed**：该项中止、不再继续写；已写步骤保留待续跑（forward-only，§7.6） |
| F2 | user `enabledPlugins` | 权威 | 键以 `@<old>` 结尾 | 新键 = 旧值（`true`/`false` 都照抄），再删旧键 | fail-closed |
| F3 | project / local `enabledPlugins`、以及 project/local 的 `installedPlugins` 声明 | 权威（**只报告**） | 同 F2；扫描面 = `os.getcwd()` ∪ 账本 `projectPath` | **不改**，输出文件 + 点路径 + 建议片段；**并显式告警「迁移后该插件将处于未启用态」** | 不适用（该告警不阻塞执行，但令退出码为 3，且写入汇总与 `--json`） |
| F4 | `trustedMarketplaces` | 权威 | 元素 ∈ 旧名集 | 加新名 → 删旧名 | 加失败 fail-closed；删失败 WARN（残留只是陈旧标记，见 §4.2 F4） |
| F5 | plugin input 值 | 权威 | 键前缀 `<plugin>@<old>/`（**按克隆/外树 `inputs.json` 派生候选**，非按键前缀全表扫） | **P1 单次原子写加新键；P3 单次原子写删旧键**（对齐 §7.1 分阶段） | **目标键已存在且值不同 → 该项 abort（零写入）**；其余 fail-closed（**先写后删**，中途失败不丢值） |
| F6 | plugin secret | 权威 | **从 catalog clone / 外树的 `mcp-servers/inputs.json` 枚举 `password: true` 候选**（D14）；已从清单移除的 plugin 经账本 `installPath` 定位 | keyring：写新 → 回读验证 → 删旧 | **旧 secret 存在且 keyring 不可用 → 该项 abort（零写入）、其余继续、退出码 3**；**目标键已存在且值不同 → abort**；本次已 set 的新键在失败时清理；绝不落明文 |
| F7 | `known_marketplaces[<old>]` | 派生 | — | 先写 `known[new]`（`installLocation` 重算、`manifestNameBaseline = new`），待搬迁与重建完成后删 `known[old]` | 失败 → 保留旧条目（可重建） |
| F8 | catalog clone | 派生 | 目录存在性 | `<home>/marketplace/<old>` → `<new>` rename | 失败 → 该项 abort（保留旧状态）；不做重 clone，恢复交 `marketplace add/refresh` |
| F9 | 外部 plugin 树 | 派生 | 目录存在性 | `<home>/marketplace/.plugins/<old>` → `<new>` rename（与 F8 同阶段，紧跟 F8） | 失败 → 该项 abort（F8 的可重入性保证重跑安全） |
| F10 | `installed_plugins.json` | 派生 | pid 以 `@<old>` 结尾 | **不重写**：先以新 pid `materialize_plugin` 重建，成功后才删旧记录 | 重建失败 → **保留旧记录**（成为孤儿，`plugin gc` 可清） |
| F11 | 运行期 `A2CSkillRef.source` | 派生（内存） | — | 不迁移；说明 + 提示重启 REPL | 不适用 |
| F12 | `extraKnownMarketplaces` | 声明 | 键 ∈ 旧名集；按 scope 分别检测 | **默认只报告**（§2.3 的 reconcile 复活告警）；`--update-declarations` 时**仅 user scope** 改键，project/local 仍只报告 | 目标键不同源 → 冲突（整体 abort，exit 1）；同源 → 合并 |
| F13a | `blockedMarketplaces` | 权威（user scope）/ 只报告（project/local） | 元素 ∈ 旧名集 | **user scope 改写成新名**（与 trust 同一 raw RMW）；project/local **只报告** | 写失败 fail-closed；残留旧名 = 陈旧阻断（见 §4.2 F13a） |
| F13b | `strictKnownMarketplaces` | — | **标量 bool**（`schema.py:121`），无 per-name 面 | 不涉及 | 不适用 |
| F14 | env 注入值 `A2C_SMCP_<ENV_SEGMENT>` | 环境（非持久） | 无法可靠枚举（键随 scoped id 派生） | **只报告**：迁移文档提示用户自行更新 | 不适用 |

### 4.2 逐面说明（只写需要展开的）

**F1/F2 —— 为什么要「先加后减」**：活跃集 = installed ∧ `enabledPlugins[id] is True`（`reconciler.py:140-156`）。
若先删旧再加新，崩溃窗口里两项都不存在 = 插件静默熄灯。先加后减的最坏跨步状态是「旧新都在」，语义是重复安装而非丢失。

**F2 —— 必须按安装 scope 逐层处理**：见 §2.1 的加注。user 层能改就改；project/local 层按 D6 只报告，但**必须**产出
`will-be-inactive` 告警——否则用户看到「迁移成功」，实际插件从此不加载。这与「不静默」原则同级，不能降级为普通 WARN。

**F2 —— `false` 必须照抄**：`enabledPlugins: false` 与缺键在激活语义上等价，但它是用户的显式意图；迁移把它归一为缺键属于
「静默改变用户意图」，违反不静默原则。

**F3 —— 报告面怎么找全**：project/local 锚定进程 cwd（`scope.py:254-291`，#116）。CLI 可能在任意 cwd 运行，
故报告面 = `{os.getcwd()} ∪ {账本记录的 projectPath}`（`installer.py:344` `_clear_enabled_entries_visible_layers` 用同一并集口径）。
文件不存在时静默跳过（不凭空建目录，同该函数的存在性守卫）。

**F4 —— trust 原语分层（订正 2026-11-17 的过时理由）**：

- 旧文本称「残留旧名会让同名源 add 跳过交互确认」——**#260 起不再成立**：交互式 `marketplace add` **一律**弹确认，
  `trustedMarketplaces` 不再豁免 prompt（`marketplace.py:421-424`）。因此迁移 trust 的目的是**保持 `list/info`
  的 `trusted` 标记与事实一致**（`marketplace.py:239` 按 name 读），不是消除安全绕过。
- 原语分两层：CLI 交互层 `_record_trust` / `_revoke_trust`（单名 append/remove，各一次持锁 raw RMW）；
  **迁移层不复用它们**——迁移把 `trustedMarketplaces` 与 `blockedMarketplaces` 合并进**一次** user-scope raw RMW
  （§4.1 F13a），避免 N 次锁写与中间态。删除旧名失败降级 WARN（沿用 `marketplace_remove` 的既有姿态）。

**F5 —— value store 分阶段单次原子写 + 目标键冲突**：`input-values.json` 是整文件 map（`value_store.py:55`），
一次 `ValueStore.replace_all` 等价于「读-改-写整文件」。**P1 只加新键（一次原子写）**、**P3 只删旧键（一次原子写）**：
与 §7.1「P1 新侧就绪、P3 撤旧侧」一致——若 P1 就删旧键，P2 失败会让旧注册的 input 值凭空消失。
写前检查目标键：已存在且值不同 → **该项 abort（零写入）**；同值 → 视为已迁移（幂等 no-op）。
文件本身不可读 / 非法 JSON → **命令级 abort（strict loader，exit 3）**，绝不把它「修复」成空 map。

**F6 —— secret 重键顺序与候选派生**：keyring 是逐键 API（`secret_store.py:69`），无法多键原子，也**没有跨平台列举 API**，
故候选必须**派生**而非按前缀枚举（D14）：

1. 枚举新清单内**全部 plugin**（含未安装的清单内 plugin——它们历史上装过、可能在 keyring 留值）的
   `<clone>/<plugin>/mcp-servers/inputs.json`，收集 `password: true` 的 input id → 拼 `<plugin>@<old>/<id>`；
   （已不在清单、也已卸载的 plugin 见下方「已知边界」）
2. 已装但**已从清单移除/改名**的 plugin：按账本 `installPath` 定位其 `inputs.json`（键名 plugin 段取 pid 前缀）；
3. 既无清单条目又无可用 `installPath` → **无法证明无旧 secret** → 该项 abort（零写入，exit 3）；
4. **已安装** plugin 的 `inputs.json` 畸形 / 根不可读 → 该项 abort（保守，不猜）；
   **从未安装且无根**的清单内 plugin **不计候选**（不存在可被写入的 scoped secret，2026-11-22 架构师确认）；
   `inputs.json` 缺失本身不是错误（该 plugin 无 inputs）。

写序：`get(old)` → `set(new)` → 回读校验；仅当 `set` 成功且 `get(new)` 与旧值相等才 `delete(old)`。
**目标键已存在且值不同 → abort（不覆盖既有 secret）**；`delete(old)` 失败只告警（新键已可读，值不丢）。
本次尝试已 set 的新键在中途失败时**尽力清理**（避免产生新孤儿）。
**绝不**在未确认新键可读前删旧键——secret 丢了不可恢复（用户拿不回来，只能重录）。
旧 secret 存在且 **keyring 不可用** → 该项 abort、零写入、其余继续、退出码 3（D10）。

**已知边界（必须写进报告与迁移文档）**：keyring 无跨平台列举 API，**已卸载 plugin 的孤儿 secret** 无法被发现，
因此迁移无法重键、也无法证明其不存在。这类旧键会留在 keyring 里成为孤儿（不影响运行，只是残留）；
迁移文档提示用户可重录或手工清理。

**F9 —— 外树与 catalog 同阶段**：独立源 plugin 的根是 `<home>/marketplace/.plugins/<mp>/<plugin>`（`staging.py:1063`
`external_plugin_dir`），其路径段含 marketplace 名。P2 内顺序固定为 **catalog 先、外树后**：catalog 是 P4 定位一切的起点，
先换它才能读新清单；外树只被 `locate_plugin_root` 的独立源分支使用（`staging.py:1181`）。任一 rename 失败即该项 abort
（此时 `known[new]` 已写、catalog 可能已换——续跑按 §7.4 认领，不会重复搬迁）；**不重 clone**，恢复交
`marketplace add/refresh`。

**F10 —— 为什么保留旧账本记录而不是直接删**：`materialize_plugin`（`installer.py:946`）在写账本前会做 clone/定位 +
manifest 校验 + 依赖解析，失败即抛。删掉旧记录而新记录没建成 = 依赖信息（`mcpServers` bundle_id 数组）丢失，
`reclaimable_mcp_deps` / 治理重挂的输入变空。保留旧记录 = 只是多一条孤儿，用户可以 `plugin gc`。

**F10 —— 重建能沿用哪些元数据**：`materialize_plugin` 的参数只有 `scope` / `project_path` / `version`
（`installer.py:946-960`），`installedAt` 在函数内部恒写 `now`（`:1028`）。故**沿用** `scope` / `projectPath` / `version`，
**不沿用** `installedAt`（由本次重物化重写；如需保留原始安装时间，另开可选入参，见 §15 Q3）。
逐条重建：账本条目是 `pid → list[record]`（按 `scope` 分条，`store.py:107-139`），多 scope 记录逐条处理。

**F10 —— plugin 不在新清单里怎么办**：`find_plugin_entry`（`manifest.py:102`）找不到时 `materialize_plugin` 会抛。策略：
**不把它当作迁移失败**——F1 照常把意图迁到新 pid，F10 保留旧账本记录，dry-run/实跑都产出
`warn: plugin '<p>' not in the new manifest` 并给出处置建议（`plugin uninstall <p>@<new>` 或等待上游恢复）。
理由：上游删插件是外部事实，拿它阻断整个 marketplace 的改名代价过大；而保留旧记录保证依赖信息不丢。

**F11 —— 重启提示**：`A2CSkillRef.source` 是内存派生（`staging.py` `_build_marketplace_ref`），迁移命令不触碰 Registry；
若在 REPL 内执行 dry-run，只打印计划、不标脏；`--apply` 在 REPL 被拒（§10.2），故无「REPL 内迁移后标脏」路径——
shell 执行后由下次 REPL/boot 重扫（与既有 `marketplace add/remove/refresh` 的标脏点 `marketplace.py:931/954/963` 不同，
这是刻意的：跨进程无法标脏，文档直接提示重启）。

**F13a —— `blockedMarketplaces`：为什么 user 改写、project/local 只报告**：该字段目前**无消费者**（全仓仅
`schema.py:123/339` 声明 + 校验，`completer.py:41` 补全词表，无执行点），陈旧条目今天是惰性的。一旦 #80（strict /
白名单）落地，陈旧的 **block** 条目会 **fail-open**（旧名被拉黑、新名放行）——security 方向的残留。故 2026-11-22
裁决：user scope **随迁移改写**以消除本 SDK 可安全改写的陈旧阻断；project/local 按 D6 只报告，并在报告里显式标注
`blockedReferenceInvalid`，**要求 #80 落地时一并复查**本项（§15 Q5）。与 trust 共用一次 user-scope raw RMW（F4）。

---

## 5. 冲突分类与拒绝

### 5.1 预检阶段（零写入）拒绝的冲突

**2026-11-22 裁决（D7）**：下表任一冲突**只要涉及至少一个选中项**，命令**整体 abort**：零写入、不迁移任何项、
退出码 1。仅存在于**未选中项之间**的冲突只报告、不阻塞（因为 `--name` 过滤已把它们排除在执行面之外）。

| 编号 | 冲突 | 判据 | 输出 |
|---|---|---|---|
| C1 | 多个旧名 → 同一目标名 | 计划里两个 `K` 的清单名相同 | `conflict: duplicate-target`，**整体 abort**（不部分合并） |
| C2 | 目标名已被别的源占用 | `known_marketplaces[new]` 存在且 `source != 旧记录.source` | `conflict: name-taken-by-other-source`，整体 abort（对齐 #260 D2） |
| C3 | 目标 pid 已存在且启用状态冲突 | **user 层** `enabledPlugins[<p>@<new>]` 存在且值 ≠ user 层 `enabledPlugins[<p>@<old>]` | `conflict: enabled-state-conflict`，整体 abort |
| C4 | 跨 marketplace 裸名冲突（#257） | 目标 pid `<p>@<new>` 与**其他** marketplace 的已装 pid 同裸名 | `conflict: bare-name-clash`，整体 abort |
| C5 | 目标名非法 | 清单名不满足 `is_valid_marketplace_name` | §3.3 grandfather（`invalid-manifest-name`），非 conflict |
| C6 | 旧名与清单名相同 | — | no-op，不在计划内 |
| C7 | 来源 plugin 在新清单中缺失 | `find_plugin_entry(new_manifest, p) is None` | **不是冲突**：照常迁移 + `warning` + 保留旧账本记录（§4.2 F10） |
| C8 | 目标目录 / 外部树已存在 | `<home>/marketplace/<new>` 或 `.plugins/<new>` 存在，**且不属 C2 的同源合并场景**（含大小写不敏感 FS 上的 case-only 碰撞） | `conflict: target-dir-exists`，整体 abort（不删、不合并目标目录；`rename` 不能覆盖） |
| C9 | 声明面目标冲突（仅 `--update-declarations`） | user `extraKnownMarketplaces[<new>]` 存在且 `source` 与旧声明不同源 | `conflict: extra-known-target-conflict`，整体 abort（同源 → 合并且删旧键） |

**C4 的源 pid 排除（必须显式，否则本项恒拒）**：既有判据 `_bare_name_clash_pids`（`installer.py:238-248`）对
「同裸名不同 pid」即报冲突，而迁移的源 pid `<p>@<old>` 与被迁移目标 `<p>@<new>` **恰好同裸名**；源 pid 在 P3 之前
一直在 merged `installedPlugins` 与账本里。因此：

- C4 与写路径都必须**显式排除本项全部源 pid**（含其它正在迁移的项）；
- P1 的 `installedPlugins` 写入**不得**走 `guard_bare_name=True`（`installer.py:698/728-731` 的锁内复核会当场抛
  `validation`）；迁移用非守卫 RMW，并在命令文档里说明这一点；
- 代价是 P1→P3 窗口内确实存在「同裸名双装」瞬态。该瞬态会让**并发**的 `plugin install/enable` fail-closed 拒绝
  第二个同名插件——这是可接受的（迁移期间本就应该停机，§7.3），且因为两 pid 属同一次迁移，不会被误判为长期违规。

**目标名已被同源占用（C2 的同源分支）不是冲突**，而是「用户已经以新名 add 过」的**合并场景**（此场景同时也是 C8 的
唯一例外：目标目录存在不算冲突）：

- 目标目录存在且清单名 = `new`，`source` 与旧记录相等 → 视为 `already-migrated`：
  仅把意图 / trust / blocked / input / secret 从旧名改指新名，删旧账本条目；**不**再搬迁目录、**不**删目标目录。
  目标目录存在但清单名 ≠ `new` 或 source 不可证同源 → **C8 / C2 冲突**（整体 abort），绝不把内容不同的目录当同源合并。
- 两侧插件集不一致（目标名下少装了某插件）→ 不是冲突；按 F1 把缺的插件意图补到新名下（照抄旧值）。
- 目标 `known[new]` 的 `manifestNameBaseline` 已是 `new` 或缺失 → 按 §2.2 规则写成 `new`，**不沿用**旧条目 baseline。

**检查顺序（`--name` 过滤不得改变判定）**：计划类冲突（C1–C4、C8、C9）必须先在**全量 legacy 集**上判定，再套
`--name` 选择，否则过滤掉重复目标中的一个会让 C1 假性消失、迁出双份新名。滤除项标 `not-selected`，不参与执行但仍
出现在 dry-run 里。冲突是否**阻塞**由「是否涉及选中项」决定（见本节开头），不由过滤后的集合重算。

### 5.2 执行期失败（非冲突）

项内部的失败（settings 写失败 / rename 失败 / 目标键碰撞）**不**改冲突分类，也**不**跨项传播：

- **P0.5 预检发现**的问题（keyring 不可用、目标键异值、secret 候选不可枚举）：该项标 `aborted`/`blocked`，
  **该项零写入**——预检先于任何写入，天然满足；
- **P1 之后**的写失败：该项标 `aborted`，**停止对该项继续写**；已写步骤**保留**（forward-only，不逐项回滚，
  §7.6），重跑按 §7.2/§7.4 续做；
- 其余项继续执行；
- 最终退出码 **3**（部分完成）。

与 D7 的区别：**冲突是命令级**（整体 abort、exit 1、任何项都不写）；**执行期失败是项级**（其余项照常、exit 3）；
**forward-only 不做项内回滚**（python 与 rust 的已接受差异）。

### 5.3 输出形状

沿用 #260 裁决 6 的结构化失败先例（`cli/commands/plugin.py:115` `_plugin_error` 的 `{"error": "validation", ...}`）：

```json
{
  "command": "marketplace migrate",
  "dryRun": true,
  "status": "planned",            // planned | applied | aborted(conflict/state-corrupt) | partial
  "applyRequested": false,        // 是否传了 --apply（aborted 时区分「dry-run」与「apply 被拦下」）
  "applied": false,               // 是否真的执行过写入
  "exitCode": 0,                  // 0 / 1 / 3（与进程退出码一致，便于脚本判定）
  "updateDeclarations": false,
  "items": [
    {
      "old": "acme", "new": "acme-skills", "action": "rename",   // rename | noop | unresolved | blocked | aborted | not-selected
      "reason": null,                                            // unresolved/blocked 的机器码（§3.3）
      "source": "https://…(脱敏展示)",
      "plugins": [{"id": "figma@acme", "newId": "figma@acme-skills", "action": "move"}],
      "actions": {
        "installedPlugins": 2, "enabledPlugins": 2, "trustedMarketplaces": "add+remove",
        "blockedMarketplaces": "add+remove",
        "extraKnownMarketplaces": "report|rewrite(user)", "inputValues": 3, "secrets": 1,
        "catalog": "rename", "externalTrees": 1, "ledger": "rebuild"
      },
      "warnings": ["plugin 'x' not present in the new manifest"],
      "incomplete": false,        // 迁移完成但插件未重建（旧账本保留）→ true，最终退出码 3
      "followUps": [
        {"file": "/repo/.tfrobot/settings.json", "pointer": "/enabledPlugins/x@acme",
         "effect": "will-be-inactive", "suggested": "…"},
        {"file": "/repo/.tfrobot/settings.json", "pointer": "/extraKnownMarketplaces/acme",
         "effect": "reconcile-will-resurrect", "suggested": "…"}
      ]
    }
  ],
  "conflicts": [{"code": "duplicate-target", "names": ["a", "b"], "target": "acme-skills"}],
  // 冲突的 involves 是内部阻塞判据，不序列化；names/target 一律脱敏展示值
  "corrupt": []                   // strict 只读预检发现的损坏状态文件（非空 → status=aborted、exit 3）
}
```

dry-run（默认）与 `--apply` 的 JSON 形状一致（`dryRun` 区分），便于脚本先审后跑。dry-run 的 `exitCode` 即
「若 apply 会得到的退出码」（计划含冲突 → 1；含 unresolved/blocked/follow-up → 3；否则 0）。
`origin` 字段按 Q4 **不输出**（§3.2）。`--json` 的所有自由文本（source / 名字 / 路径）一律走脱敏展示原语。

---

## 6. dry-run 输出（人读形态）

```text
Marketplace name migration plan (dry run; run with --apply to execute)
 Old      New          Action      Plugins  Trust  Blocked  Inputs  Catalog  Follow-ups
 acme     acme-skills  rename            2  ✓→✓    —→—          3  rename   1 will-be-inactive
 legacy2  —            unresolved        —  —      —            —  —        manifest-unreadable

Extra-known declarations that will resurrect the old name (fix before/after migrate):
  /repo/.tfrobot/settings.json: extraKnownMarketplaces.acme

No conflicts. 1 item can be migrated; 1 needs attention (exit 3 on --apply).
```

**退出码（与 D12 一致）**：

- **0**：成功 / 全 no-op（含全合规、无 follow-up）；
- **1**：**冲突整体 abort**（不写任何字节）或命令级用法错误；
- **3**：**部分完成**——存在 `unresolved` / `blocked` 项、keyring-abort 项、或 project/local 的
  `will-be-inactive` / 陈旧 trust/blocked/extraKnown 手动引用（其余项已迁移；重跑可续）。
- 不再产出 2（网络错）：`--reclone` 已删除，迁移全程离线。

dry-run 也按同一口径返回「apply 会得到的退出码」，便于 CI 先审。

---

## 7. 执行顺序、幂等与崩溃恢复

### 7.1 单项阶段（每个 marketplace 独立、串行）

> 命名提示：本节 `P0`–`P5` 是**单次执行的阶段**；与 Issue 交付拆分里的「P0（设计文档）/ P1（实现 PR）」
> 同名不同义，不要混用。

```text
P-1 strict load     strict 只读加载全部将被改写的状态文件        零写入；损坏 → 命令级 abort（exit 3），不 rename 损坏文件
P0  preflight       计算计划、冲突检测（命令级）                  零写入（P-1/P0/P0.5 都是零变更退出的阶段）
P0.5 item preflight 项级：secret 候选派生 + keyring 可用性 +     零写入；不通过 → 该项 abort/blocked（exit 3），
                    目标 input/secret 键冲突检查                  其余项继续
P1  additive(new)   新名侧权威面全部写入（旧名侧保持）           installedPlugins / enabledPlugins(user) / trust /
                                                                blocked / extraKnown(user, --update-declarations) /
                                                                input 值 / secret
P2  catalog swap    known[new] → rename catalog → rename .plugins/<old> → materialize(new_pid)
P3  switch-off      旧名侧权威面删除                             enabled / installed / trust / blocked / extraKnown(user) /
                                                                input 值 / secret
P4  ledger cleanup  重建成功的 pid 才删旧账本记录（F10）
P5  report          汇总 + 依赖告警（project/local、extraKnown、blocked、env）
```

**为什么有 P-1（strict 只读 loader）**：现有 `load_known_marketplaces` / `load_installed_plugins` 走
`read_jsonc_with_recovery`——损坏文件会被**改名**成 `.corrupt-<ts>.bak` 后按空配置继续（`store.py:283-327`）。
这违反 dry-run 零写入，而且会让迁移基于「空账本」算出灾难性计划。因此新增 strict 只读变体（`load_*_strict`），
损坏 → 抛结构化错误（携带路径）→ **命令级 abort、exit 3、零写入、不产生 `.corrupt-*`**。同样地，settings 文件用
`read_raw_object`（`raw.py:68`，损坏即抛 `SettingsFileCorruptError`，不落盘）而不是容错的 `load_settings_file`
（`scope.py:200`，损坏时静默降级为空 + 记错）；`input-values.json` 也必须 strict 解析（`ValueStore.load` 是容错的，
不能用来做「将被改写」的读面）。

**为什么 P0.5 必须先于任何写入（D10 的直接后果）**：keyring 不可用且存在旧 secret 时要求「按 marketplace abort、
零写入」；若把 secret 检查放到 P1 写入之后再发现，已写的 intent/trust 就在磁盘上了。故 secret 候选派生、keyring
可用性、目标键冲突必须在**项级写入前**一次性判定。候选来源见 §4.2 F6。

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
| P1 | 新 pid / 新 key / 新 trust / 新 blocked 已存在 → no-op（按旧值覆盖写，等价）；目标 input/secret 已存在**同值** → no-op |
| P2 | 目录已在 `<new>` → 跳过 rename；`known[new]` 已存在且 source 相同 → 覆盖写（baseline 恒写 `new`） |
| P2 | 新账本记录已完整（`ledger_entry_fully_materialized`）→ 跳过重建 |
| P3 | 旧键已不在 → no-op |
| P4 | 旧记录已不在 → no-op |

因此**重跑 = 续跑**。两条附加纪律：

- **`--apply` 不接受外部传入的 plan**：命令在写入前**重新检测 + 重新预检**（dry-run 的输出只是给人看的），
  消除「先 dry-run、后状态变化、再 apply」的 plan/apply 漂移（对齐 rust §4.0）。
- 目标键**已存在且值不同**（input 值 / secret / enabled 状态）不是「覆盖」而是冲突/blocked——幂等只在**同值**时成立。

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

### 7.4 「可认领目录」规则（崩溃恢复的核心，2026-11-22 判据修正）

适用条件：本次计划项为 `old → new`，且 `marketplace_skill_dir(home, new)` 不存在（P2 中断在 rename 之前），
同时 `known[new]` 存在（本命令 P2 已写）。**修正点（Q14 裁决配套）**：原判据要求候选目录名**不是任何 `known` 键**，
恰好把崩溃窗口里的 `dir(old)`（它当然是 `known[old]` 的键）排除在外，导致主窗口永远认领不到；现放宽为
**允许 `D.name == old`**，并补 source 证明。

```text
old_source = source(known[old])                       # 旧键必须仍在册，否则不可归因（不认领）
new_rec    = known[new]                               # 必须存在且 same_source(new_rec.source, old_source)
candidates = [D for D in <home>/marketplace/*         # 排除 .plugins
              if D 是真实目录（非 symlink）
              and manifest_name(D) == new
              and (D.name == old                          # ① 崩溃主窗口：目录还叫旧名
                   or D.name not in known_keys)]          # ② 历史窗口：已搬离旧名、尚未落到新名
if len(candidates) == 1: 认领 D → rename 到 dir(new)（随后按 P2 继续）
if len(candidates) == 0: unresolved / catalog-lost（不猜；建议 marketplace add/refresh 或人工核对）
if len(candidates) >  1: unresolved / ambiguous-catalog（不猜，列出候选交用户）
```

四条**必须同时成立**的判据（缺一即为「猜」）：

1. `known[old]` 与 `known[new]` 同时在册，且 `same_source`（`marketplace_identity.py:163`）——证明是**同一次迁移**
   的两条记录，而不是两个独立 marketplace；
2. 旧名是安全路径段（§3.3 `unsafe-old-name`）；
3. 候选目录内清单名 == `new`（不是 `old`——等于 `old` 说明目录没被搬过，属正常 P2 之前状态）；
4. 候选目录不是 symlink（§3.3 `symlink-catalog`）。

`known[old]` 已不在、只剩一个「名字不在 known 里、清单名为 new」的孤儿目录 → **不认领**：无法证明它属于本命令的迁移，
按§3.3 grandfather（`catalog-missing`）报告，避免把别人的目录搬走。

> 另一方向的窗口（**目录已搬到 `new`、`known[old]` 未删**）不走认领，走 §7.3 的「三条件收敛收尾」。
> 认领与收敛都只服务 forward-only 重跑；**不引入 journal / 备份 / `--rollback`**（D9，已接受差异）。

### 7.5 目录搬迁原子性与并发

`os.rename`（`Path.rename`）在同一文件系统内原子：`<home>/marketplace/<old>` → `<home>/marketplace/<new>`
同父目录、同挂载点，恒成立；`.plugins/<old>` → `.plugins/<new>` 同理。**不做**跨设备 fallback（不复制、不删源）——
跨设备意味着 `A2C_SKILL_HOME` 被拆开挂载，属异常部署，直接报错更安全。

并发：本命令**不加全局锁**（不引入 rust 式的 migration 专用锁；forward-only 不需要互斥 journal）。既有写原语
（`update_known_marketplaces` / `update_installed_plugins`，`store.py:562/591`）各自持旁车锁做 RMW，但**跨文件的顺序
一致性没有锁**。因此：

- 命令启动时做一次**只读**并发探测（P1 实现取舍）：对**既有**旁车 `.lock` 尝试非阻塞 `flock`
  （拿到即释放）——只用于提示「另一个 a2c 进程正在写物化文件」。**不创建**锁文件（dry-run 必须零写入）；
  `flock` 成功本身即「没有进程在写」的证据，因此不会把「锁文件存在」误读成活性信号。
  **不得**常驻持有：现有写原语会在内部再次 `file_lock` 同一旁车文件，同进程重入会退避后抛 `SettingsLockError`
  （`store.py:209-260`、`:525-528` 的显式警告）；win32 跳过（best-effort）。
- 探测**不是**「锁文件存在即有进程」：旁车 `.lock` 创建后不删除（`store.py:231` 只 `O_CREAT`，无 unlink），
  只有**实际抢锁失败**才提示；文档与提示文案都必须写清这一点，避免制造恒真告警。
- `--apply` 在写入前**重新检测 + 重新预检**（§7.2），因此即使 dry-run 与分析之间发生外部改动，也不会按陈旧计划写盘；
  写入期间若检测到目标键/文件形状与预检不符（如目标 pid 突然存在）→ 该项 abort（exit 3），不强行覆盖。

### 7.6 没有回滚

回滚要求把已写的用户意图再改回去，而 P1 之后的状态是「新侧就绪、旧侧完整」——此时撤销新侧会得到
「旧侧仍在但即将被 P2 破坏」的中间态；P2 之后旧侧已不可用，撤销新侧只会得到**旧新皆无的熄灯态**，并把已重键的
plugin-scoped input/secret 变成新名下的孤儿键（旧键已删）。**物理上不存在「回到迁移前」** 的路径。

因此：

- **可零变更退出的是 P-1 / P0 / P0.5**（状态损坏 / 冲突 / 项级预检不通过 / 未传 `--apply`）：此时尚未写任何字节；
- P1 之后只有**前向收敛**：失败 → 报告 → **重跑**；不接受「回滚」语义，也不提供 `--rollback` 开关；
- 该差异（python forward-only vs rust journal/备份/rollback）已在 Discussion#81 记录并在 2026-11-18/22 两次确认。

---

## 8. 离线 / 无清单 / 缓存不可用

| 情形 | 行为 |
|------|------|
| 有本地 clone + 清单可读 | **完全离线迁移**（默认路径：只读盘 + rename + 写 settings，无网络） |
| 有 clone 但清单不可读 | grandfather（`manifest-unreadable`）；提示先 `marketplace refresh`（可能修好）；若 baseline 存在且 ≠ 旧名，按其作次选判据（§3.4） |
| 无 clone | grandfather（`catalog-missing`）；提示按 `docs/upgrade-0.5.1-marketplace-catalog-recovery.md` 恢复，或 `marketplace add/refresh` 重建 |
| rename 失败（权限 / 软链 / 占用 / 跨设备） | 该项 abort，保留旧状态、退出码 3；**不做重 clone**（`--reclone` 已删除），恢复交 `marketplace add/refresh` |
| 独立源 plugin 的外树 rename 失败 | 同上 |
| keyring 不可用且**存在**旧 secret | 该项 abort、**零写入**、其余项继续、退出码 3（D10）；报告里明确「需手工重录」 |
| keyring 不可用且**无**旧 secret | 无 secret 可重键 → 照常迁移（不因后端缺失误伤无密钥项） |
| secret 候选不可枚举（`inputs.json` 缺失/畸形、plugin 根不可读） | 该项 abort（无法证明无旧 secret），退出码 3 |
| `input-values.json` / 状态文件损坏 | **命令级 abort（strict loader）**：零写入、不 rename 损坏文件、退出码 3 |
| 网络 | **完全不使用**（无 `--reclone` 开关） |

> 离线优先级的设计理由：迁移是**本地状态整理**，不是同步；能在磁盘上完成的事绝不依赖远端。
> 缺 catalog 的唯一恢复通道是用户显式 `marketplace add/refresh`（它会按 #260 规则以清单名注册），迁移命令不代劳。

---

## 9. project / local 只报告

- 报告内容：文件绝对路径 + JSON 点路径（如 `/enabledPlugins/figma@acme`）+ 建议改法 + **后果**
  （`will-be-inactive` / `will-stay-disabled` / `stale-trust` / `blocked-reference-invalid` /
  `reconcile-will-resurrect`）；
- 覆盖的 project/local 面：`enabledPlugins`、`installedPlugins` 声明、`trustedMarketplaces`、
  `blockedMarketplaces`、`extraKnownMarketplaces`（这些面在 user scope 会被改写或按 flag 改写，在
  project/local **永远只报告**）；
- 绝不写入（含「顺手清掉旧键」）：project 入 git、随仓库分发（`schema.py:139-173` 的受信供给讨论同源关注点）；
  local 虽不入 git（`scope.py:101-103`），仍按用户裁决（Q7）保持同处置口径；
- `--json` 下进 `item.followUps[]`；人读形态在表格下方汇总一行 + 详情见 JSON；
- **不**因为存在 project/local 残留就判该项 **失败**——它是正常可预期状态（D6）；但**只要存在任何 follow-up，
  最终退出码必须是 3**，警告必须显著（不能让人误读为「全绿完成」）。
- 附带一提：`extraKnownMarketplaces` 在 project/local 的残留会被宿主调用的 `reconcile` 复活旧注册（§2.3），
  报告必须给出用户可复制的修改片段。

---

## 10. CLI 形态

### 10.1 非交互（Typer）

```bash
a2c-computer marketplace migrate [--name <old>]... [--apply] [--update-declarations] [--json]
```

| 选项 | 语义 |
|------|------|
| `--name <old>` | 只迁移指定旧名（可重复）；缺省 = 全部 legacy。**只缩小选择范围，不缩小冲突判定范围**（§5.1） |
| `--apply` | **真正写盘**。缺省（不带本 flag）= dry-run，零写入。`--apply` 本身就是确认，**不再有 `--yes`**（破坏性面由「默认只读」承担） |
| `--update-declarations` | 允许改写 **user scope** 的 `extraKnownMarketplaces`（project/local 无论是否带此 flag 都只报告，§2.3） |
| `--json` | 结构化输出（dry-run / apply 同形状，§5.3） |

**已删除的选项**（2026-11-22 裁决）：

- `--dry-run`（dry-run 成为默认值；反向 flag 为 `--apply`）；
- `--yes`（与默认 dry-run 重复，且遮蔽「显式写盘」的语义）；
- `--reclone`（迁移全程离线；缺 catalog 交 `marketplace add/refresh` 恢复）。

退出码（与 rust 对齐，`marketplace.py:97-99` 现有 `EXIT_OK/EXIT_USER_ERROR/EXIT_NETWORK_ERROR` 之外新增
`EXIT_PARTIAL = 3`，仅本命令产出 3）：

| 码 | 含义 |
|----|------|
| `0` | 成功（含全 no-op、纯 dry-run 且计划无风险） |
| `1` | **冲突整体 abort**（零写入）或命令级用法/写盘错误 |
| `3` | **部分完成**：unresolved / blocked / keyring-abort 项，或存在 project/local 手动引用（follow-up） |

### 10.2 交互（REPL）

`marketplace migrate [--update-declarations] [--json]` → 打印计划（dry-run）。**REPL 只允许 dry-run**：
`--apply` 一律拒绝并提示在 shell 执行（事务性迁移不放进交互式 REPL；与 rust 同口径）。
`--update-declarations` 在 REPL 可作为**计划修饰符**使用（它只在 `--apply` 时写盘，纯 dry-run 下只改变计划展示）。
REPL 输出额外提示 `Run in a shell: a2c-computer marketplace migrate --apply`。跨进程无法标脏，
文档提示迁移后在 shell 重启 REPL / 重新 boot。

### 10.3 名称与位置

- 子命令名 `migrate`（不复用 `rename`：一次可能迁多个，且目标是清单名而非用户指定的新名）；
- library 层入口 `marketplace_migrate(...)`，与其它 handler 同形（显式 `registry/home/env` + flags + 返回退出码），
  放在 `a2c_smcp/computer/cli/commands/marketplace.py`；纯逻辑抽到
  `a2c_smcp/computer/settings/marketplace_migration.py`（检测 / 计划 / 执行，便于单测）；
- **单一权威**：检测 / 计划 / 执行全在库层，CLI 只做参数解析与展示（同 #260 详细描述第 10 条）。

### 10.4 `marketplace info` 增加 `manifestName`（Q9 已裁决，P1 一并实现）

`marketplace_info`（`marketplace.py:583`）当前 payload 为
`name / url / installLocation / commitSha / autoUpdate / trusted / lastUpdated / installedPlugins`，**不含清单名**，
导致用户无法自查「我这条注册是不是 legacy」。P1 在该 payload（及其人读表格）中增加：

- `manifestName`：本地 clone 清单顶层 `name`（**仅当它是合法严格 kebab 时**）；不可读 / 非法 / 疑似凭据 → `null`；
- `manifestNameInvalid: bool`：清单名存在但不可安全展示（与 `manifestName == null` 区分「不可读」与「不可信」）；
- `legacyName: bool`：`manifestName is not None and manifestName != name`（= §3.1 判据的直接呈现）。

**`manifestName` 必须脱敏（2026-11-22 裁决）**：清单内容来自 git 源，属**不可信输入**；该值可能不是合法 kebab 名，
甚至携带凭据样文本（与账本键同一威胁模型）。展示规则与 `redact_ledger_key`（`marketplace_identity.py:189`）一致：

- 合法严格 kebab（`is_valid_marketplace_name`）→ 原样展示（干净名字仍可读，#295 的验收口径）；
- 非法 / 超长 / 含凭据样文本 → JSON 只给 `null` + `manifestNameInvalid: true`，**绝不携带原值**；
  人读表格显示固定占位符 `<invalid>`（同样不含原值）。

两者**只读派生**，不写盘、不影响 `known_marketplaces` 记录形态（不是 #260 baseline 字段，勿混）。
该字段是迁移文档两条自查路径的前提（§13.1），P1 必须与迁移命令同 PR 落地。

---

## 11. 与相邻 Issue 的边界

| Issue | 关系 |
|-------|------|
| #260 | 提供 D4 baseline（**已合入交付**，`manifestNameBaseline`）；本设计只读它、不重复实现；#260 的 `--name` 语义（D6 B）决定「显式别名不再被支持」→ 别名必须迁移 |
| #270 | 提供严格派生与同源校验（已 CLOSED）；本设计**不重算**旧名，只用账本值比对 source |
| #276 / #263 | 展示与日志构造期脱敏（已合入）；迁移输出中的 source URL 一律走 `known_git_source_for_display`（`utils/git_url.py:94`；CLI 包装 `marketplace.py:155` `_display_source_url`） |
| #275 / #271 | installPath / ledger 语义（#275 已 CLOSED；#271 仍在 v0.5.1）。本设计**不重写** `installPath`（§2.2），若后续把账本升级为 installPath 权威，需回来复核 F10 |
| #257 | 跨 marketplace 裸名拦截（已合入）；C4 复用其判据但**必须排除源 pid**（§5.1） |
| #267 | 迁移文档先例（`docs/upgrade-0.5.1-marketplace-catalog-recovery.md`），本单的迁移文档与之并列 |
| #295 | 展示面脱敏收尾（settings 校验错误回显）。与本设计的 `manifestName` 脱敏**同族**：都复用「单值/segment 漏斗」 `sanitize_segment_for_diagnostics`；本设计不等待它，但若它引入新漏斗，P1 同批切换 |
| #296 | flag/policy 层引用守卫（re-add 引用可被绕过）。迁移的 C4 源 pid 排除与「意图引用」判定要与它的容器集单点一致——**建议先合**，否则 P1 rebase 时复核 |
| rust-sdk#289 | 镜像实现（**已实现**，PR#299 合 `dev-0.5.0`）：语义对齐、实现独立；journal/备份/rollback 为已接受差异 |
| a2c-smcp-protocol#86 | v0.5.1 发布门 tracking：本单是 python 侧**硬门**（owner 2026-11-22 确认，不 defer） |

---

## 12. 测试计划（P1 验收映射）

### 12.1 夹具纪律（**强制修订**）

**不能用 `marketplace add` 构造 legacy 夹具**：`#260` 之后 `add` 以清单 `name` 注册（`staging.py:1473-1481`
的 `REASON_EXPECTED_NAME_MISMATCH` 分支），`--name` 只是**期望名校验**、不再是别名（`marketplace.py:419/436-443`）。
因此「先 add 出一个旧名再迁移」在这条代码线上**物理上不可能**——旧写法会得到一个 `validation` 而非 legacy 状态。

legacy 夹具必须**直接构造 pre-#260 磁盘状态**（与真实存量用户一致）：

1. `A2C_SKILL_HOME/known_marketplaces.json`：手写 `{"version": …, "marketplaces": {"<old>": {"source": {...}, "installLocation": …, "commitSha": …, "autoUpdate": …, "lastUpdated": …, ("manifestNameBaseline" 可选)}}}`；
2. `<home>/marketplace/<old>/.tfrobot-plugin/marketplace.json`：清单顶层 `name = <new>`（+ `plugins[]`）；
3. `user settings.json`：`installedPlugins` / `enabledPlugins` / `trustedMarketplaces` / `blockedMarketplaces` /
   `extraKnownMarketplaces` 的旧名引用；
4. `installed_plugins.json` 的旧 pid 记录（含 `scope` / `projectPath` / `installPath` / `mcpServers`）；
5. `$XDG_STATE_HOME/a2c/input-values.json` 与 fake keyring 的旧 scoped 键。

两种「旧名来源」（URL 派生 / 显式 `--name`）用**同一夹具模板**、只改旧名字面（如 `acme` vs `my-custom`），
断言**同一迁移路径**；不要求 `origin` 标签（§3.2）。

### 12.2 用例表

| 验收条目（Issue） | 测试 |
|---|---|
| 旧名来源等价 | 两种旧名字面的夹具 → 同一计划、同一终态；**不**断言 `origin` |
| 同名多旧名 | `duplicate-target` 冲突 → **整体 abort**：退出码 1，**全仓零写入**（字节级比对 settings / 账本 / 目录树） |
| 冲突整体 abort（D7） | C2 / C4 / C8 各一例：同一命令里另有一个**无冲突**项 → 断言该无冲突项**也未迁移**、退出码 1 |
| 未选中项冲突不阻塞 | 两个冲突项 + `--name` 只选第三个无冲突项 → 迁移成功、退出码 0，冲突在报告里列出 |
| **C4 源 pid 排除** | 旧 pid 已装、无第三方同名 → 迁移成功；注入一个**第三方**同裸名 marketplace → 整体 abort。变异：删掉源 pid 排除 → 第一个用例必须转红 |
| 幂等重放 | 连续 `--apply` 两次：第二次全 no-op，settings/账本/目录 mtime 不变 |
| 崩溃恢复 · 主窗口 | 注入「`known[new]` 已写、rename 未做」→ 重跑 → 认领 `dir(old)`（§7.4 修正判据）后终态一致 |
| 崩溃恢复 · 反向窗口 | 注入「rename 已完成、`known[old]` 未删」→ 重跑 → 走 §7.3 三条件收敛收尾 |
| 崩溃恢复 · 外树窗口 | 注入「catalog 已搬、外树未搬」→ 重跑只补外树 |
| 崩溃恢复 · 不认领 | 孤儿目录但 `known[old]` 不在册 → **不认领**、报告 unresolved（不搬别人目录） |
| project/local 不改写 | 五个面各含旧名 → 文件字节不变 + `followUps[]` 有对应 `effect`；退出码 3 |
| **project scope 安装仍启用** | `plugin install <pid> --scope project` + `enable` 后迁移 → 合并视图活跃判据**不劣化**（不静默熄灯）；同上 local 两例 |
| secret 重键 | fake keyring：新键写入后旧键删除；**keyring 不可用且旧 secret 存在 → 该项零写入、其余项迁移、退出码 3**（变异：把 abort 改成 skipped → 断言「零写入」的用例转红） |
| secret 候选派生（D14） | 已从新清单移除的 plugin（有账本 installPath）→ 旧 secret 仍被重键；无法定位的 plugin → 该项 abort、零写入 |
| 目标 secret / input 值键异值 | 预置 `new` 键不同值 → 该项 abort、旧值不动、退出码 3；同值 → 幂等 no-op |
| 离线 grandfather | 删掉 clone / 破坏清单 → `unresolved`，零写入，退出码 3 |
| strict loader 零写入 | 把 `known_marketplaces.json` / `installed_plugins.json` / settings / `input-values.json` 各写坏一份 → dry-run 与 `--apply` 都：退出码 3、**不产生 `.corrupt-*.bak`**、所有文件字节不变 |
| 默认 dry-run | 不带 `--apply`：计划非空、零写入、退出码 = 「apply 会得到」的码；`--apply` 才写 |
| REPL 只 dry-run | REPL 调 `migrate --apply` → 拒绝并提示在 shell 执行，零写入；`--update-declarations`（不带 `--apply`）→ 只影响计划展示、零写入 |
| input 值迁移 | `input-values.json` 单次原子写（写入次数 spy）+ 值逐字节保留 |
| 账本重建 | `installPath` 指向新目录、`scope`/`projectPath`/`version` 与旧记录一致；旧 pid 记录删除（重建成功）/ 保留（重建失败注入） |
| 外树搬迁 | 独立源 plugin：`.plugins/<old>` → `<new>` 成功 / 失败 abort（无重 clone）两分支 |
| known 记录形态 | 新记录字段集与 staging 写出的一致、`installLocation` 指向 `<home>/marketplace/<new>`、**`manifestNameBaseline == new`**（变异：复制旧 baseline → 迁移后 refresh 用例转红） |
| `extraKnownMarketplaces` 默认只报告 | 无 flag：user/project/local 声明都不改，产出 `reconcile-will-resurrect` 告警；文件字节不变 |
| `--update-declarations` 仅 user | 带 flag：user 声明键改写（保留 source/autoUpdate/未知键）；project/local 字节不变；目标键不同源 → 整体 abort |
| `blockedMarketplaces` | user 旧名改写为新名；project/local 只报告且字节不变 |
| 并发提示 | 先跑一次正常 `marketplace add`（留下 `.lock`）→ 迁移**不**报「另一实例在运行」；另一端真持锁时 → 探测提示 |
| 顺序无关 | 打乱 `known_marketplaces` 键序（同 rust serde Map）→ 计划与结果不变 |
| `info` 展示字段（§10.4） | legacy 注册 → `manifestName != name` 且 `legacyName is True`；清单不可读 → `manifestName` 为 `null`、`legacyName` 为 `False`；清单名非法/凭据样 → 输出无原值、`manifestNameInvalid` 为真 |

**变异验证要求**（沿用本仓纪律，全部必须至少 1 个用例转红）：`先加后减`、`先写新 secret 再删旧`、
`P4 成功才删旧账本`、`C4 排除源 pid`、`§7.4 认领谓词（含 D.name == old）`、`dry-run 不写（strict loader）`、
`keyring abort 前不写`、`baseline 不复制旧值`、`冲突整体 abort（无冲突项也不写）`。

---

## 13. 迁移文档与 release notes（草案，P1 落盘）

### 13.1 迁移文档 `docs/upgrade-0.5.1-marketplace-name-migration.md`（结构）

```text
# 升级迁移：marketplace 注册名改为清单 name
1. 背景与影响面（旧名来源：URL 派生 / --name；两者迁移路径等价）
2. 如何判断自己受影响
   a2c-computer marketplace info <name>           # manifestName 与 name 不同 → legacy
   a2c-computer marketplace migrate               # 默认 dry-run；计划非空即受影响的完整清单
3. 迁移步骤（先停所有 Computer / REPL 实例）
   a2c-computer marketplace migrate               # 1) dry-run 看计划与冲突（零写入）
   a2c-computer marketplace migrate --apply       # 2) 确认后写盘
   a2c-computer marketplace migrate --apply --update-declarations
                                                  # 可选：同时改 user 的 extraKnownMarketplaces 声明
4. 退出码与处置
   0 成功/无操作；1 冲突整体 abort（未写任何字节，按报告修复后重跑）；
   3 部分完成（unresolved/blocked/keyring-abort/需手工处理项——按报告补齐后重跑）
5. 破坏性说明
   - plugin id 变化（<plugin>@<old> → <plugin>@<new>），外部脚本 / 文档引用需同步
   - 用 --scope project / local 安装并启用的插件：本命令不改写这两层，需要在对应
     .tfrobot/settings[.local].json 手工改 enabledPlugins 键，否则迁移后处于未启用态
   - extraKnownMarketplaces 若声明了旧名：默认只报告（否则宿主 reconcile 会重建旧名注册），
     可加 --update-declarations 只改 user scope；project/local 永远需自行改声明
   - trustedMarketplaces / blockedMarketplaces 的 project/local 引用只报告，需手工处理
   - env 注入值（A2C_SMCP_<ENV_SEGMENT>）随 scoped id 变化，需自行更新
6. 离线 / 无清单（grandfather）行为；缺 catalog 用 marketplace add/refresh 恢复（迁移不 clone、不联网）
7. 迁移后确认（marketplace list / plugin list / plugin enable）
8. 失败与续跑（重跑即可；无回滚、无 journal；keyring abort 的项先修后端再重跑）
```

> **自查路径依赖 §10.4**：`marketplace info` 当前 payload 不含清单名，P1 一并加 `manifestName` / `legacyName`
> （Q9 已裁决，含脱敏）。迁移文档的两条自查路径都以该字段为前提，故 §10.4 与本文档必须同 PR 落地。

### 13.2 CHANGELOG / release notes 要点

- **Breaking**：marketplace 注册名 = 清单 `name`；`<plugin>@<marketplace>` 中的 marketplace 段随之变化（#260 已标）；
- 新增 `marketplace migrate`（**默认 dry-run**，`--apply` 写盘；`--update-declarations` 可选）；**不做**自动迁移，
  升级后旧名可继续使用直到显式迁移（不静默改名）；
- 迁移影响面：安装/启用意图、trust、**blocked**、plugin-scoped input 与 secret（user scope 改写）；project/local 只报告
  （迁移后需手工处理，退出码 3）；
- 迁移文档 + 指向迁移文档的升级指引；无回滚（前向重跑续做）。

---

## 14. 实现落点（P1 预排）

| 模块 | 改动 |
|------|------|
| `settings/marketplace_migration.py`（新） | 检测 / 计划 / P0.5 项级预检 / 执行（纯编排 + 显式资源注入，便于单测）；导出 `marketplace_migrate(...)` 库层入口 |
| `settings/store.py` | **新增 strict 只读变体** `load_known_marketplaces_strict` / `load_installed_plugins_strict`（+ 内部 `read_jsonc_strict`）：损坏即抛、不 rename、不降级空配置；既有容错 loader 语义不变。账本 strict 读 MUST `drop_legacy=False`（迁移要看到旧格式记录的 pid/installPath，默认视图会丢弃它们） |
| `settings/raw.py` / `settings/scope.py` | 复用 `read_raw_object` / `apply_write` / `SettingsFileCorruptError` 做 settings 的 strict 读与单次 raw RMW（**不**用容错的 `load_settings_file` 做写前读） |
| `settings/installer.py` | 复用 `_write_installed_plugin` / `_write_enabled_plugin` / `materialize_plugin`，**不新造写原语**；trust/blocked 的批量改写**不复用** CLI 的 `_record_trust` / `_revoke_trust`（单名、逐次锁写），改在迁移模块内一次 user-scope raw RMW |
| `inputs/plugin_pool.py` | 复用 `prefix_input_id` 做键改写（不复制格式字符串）；新增候选派生辅助（读 `mcp-servers/inputs.json`、筛 `password: true`）——**只读** |
| `inputs/value_store.py` / `inputs/secret_store.py` | 复用 `ValueStore` / `SecretStore` 原语；批量重键逻辑放迁移模块（不改两 store 的公共语义）。**顺带订正** `secret_store.py:17` 的「key=全局 id」陈旧模块注释（零行为变化） |
| `cli/commands/marketplace.py` | 新增 `marketplace_migrate` handler（默认 dry-run / `--apply` / `--update-declarations` / `--json`，`EXIT_PARTIAL = 3`）+ REPL dispatch 分支（只 dry-run）；`marketplace_info` 增加脱敏后的 `manifestName` / `legacyName` / `manifestNameInvalid`（§10.4，Q9） |
| `cli/main.py` | 注册 `marketplace_app.command("migrate")`（flags 与 §10.1 一致） |
| `skills/home.py` / `skills/staging.py` | 复用 `marketplace_skill_dir`（`home.py:129`）与 `external_plugin_dir`（`staging.py:1063`）、`locate_plugin_root`（`staging.py:1181`）计算路径 |
| `utils/git_url.py` / `skills/marketplace_identity.py` | 展示统一走既有脱敏原语（`known_git_source_for_display` / `diagnostic_url_for_display` / `redact_ledger_key` / `sanitize_segment_for_diagnostics`），**不新造展示拼接** |

> 所有跨模块的路径/键格式一律**复用既有函数**，不写字面量——与 `SCOPE_ORDER` 单一权威同理（`schema.py:70-109` 的教训）。

---

## 15. 裁决记录

### 15.1 2026-11-17 架构师审查（Phase 1）

| # | 问题 | 结论 | 状态 |
|---|------|------|------|
| Q1 | 命令名 `marketplace migrate` 与 rust 侧命名是否统一 | 保持 python 自有命名，语义在 Discussion#81 对齐即可 | ✅ 已批准 |
| Q2 | 非交互是否强制 `--yes` | ~~强制~~ → **2026-11-22 被 Q12 取代**：删除 `--yes`，默认 dry-run + `--apply` | ⛔ superseded |
| Q3 | F10 删旧账本记录 vs 保留为孤儿；`installedAt` 是否加参沿用 | 重建成功才删旧记录；`installedAt` 由重物化重写，不加参（留待 #271/#275 一并考虑） | ✅ 已批准 |
| Q4 | `origin` 诊断标签是否实现阶段就做 | 延后（§3.2）；2026-11-22 裁决**维持延后**，验收改为「两种旧名来源路径等价」 | ✅ 已批准 |
| Q5 | `blockedMarketplaces` 陈旧条目 | ~~本次只报告~~ → **2026-11-22 被 Q13 取代**：user scope 改写、project/local 只报告；#80 落地时复查仍有效 | ⛔ 部分 superseded |
| Q6 | baseline 未落地时的降级行为 | 实现**硬前置**，不降级实现 → **#260 已交付，前置满足** | ✅ 已满足 |
| Q7 | local scope 是否应改写（它不入 git，D6 的理由只对 project 成立） | **用户裁决：local 也只报告不改**，保持 D6 单一处置口径 | ✅ 已裁决 |
| Q8 | Discussion#81 的 rust secret 键空间说法 | **已按代码订正**：两侧均 scoped id、各自重键；另订正「python 侧同型陈旧注释」 | ✅ 已订正 |
| Q9 | `marketplace info` 是否增加 `manifestName` | **用户裁决：增加**，P1 一并实现（§10.4，含 `legacyName` 与脱敏） | ✅ 已裁决 |

### 15.2 2026-11-22 用户裁决（P0 设计修订，supersedes 部分 Phase 1 结论）

| # | 问题 | 结论 | 状态 |
|---|------|------|------|
| Q10 | 冲突的处置粒度 | **整体 abort、零写入、退出码 1**：任一所涉选中项的冲突 → 一个 marketplace 都不迁；仅未选中项之间的冲突 → 报告不阻塞。supersedes 原 D7「整项拒绝、无冲突项照常迁移」 | ✅ 已裁决 |
| Q11 | keyring 不可用时的语义 | 旧 secret 存在且 keyring 不可用 → **按 marketplace abort、零写入、其余项继续、退出码 3**（supersedes 原 F6「skipped、退出码 1」）；目标 secret/input 键异值同样 abort | ✅ 已裁决 |
| Q12 | CLI 形态与退出码 | **默认 dry-run + `--apply`**；退出码 **0/1/3**；**删除 `--yes` / `--reclone` / `--dry-run` flag**；REPL **只 dry-run**；`--update-declarations` 仅 user scope。与 rust PR#299 对齐 | ✅ 已裁决 |
| Q13 | 声明面与 blocked | `extraKnownMarketplaces` **默认只报告**、`--update-declarations` 仅改 user；`blockedMarketplaces` **user scope 改写**、project/local 只报告（supersedes Q5 的「只报告」） | ✅ 已裁决 |
| Q14 | 崩溃恢复的认领判据 | **修正 §7.4**：允许 `D.name == old`（崩溃主窗口）+ 校验 `known[old]/known[new]` 同源 + 清单名 == new；保持 **forward-only**，**不引入 journal / 备份 / `--rollback`** | ✅ 已裁决 |
| Q15 | 加固项（全部纳入 P1） | strict 只读 loader（dry-run 零写入、损坏不 rename）；keyring 候选派生（枚举 `inputs.json` 的 `password:true`，不按键前缀枚举）；目标 input/secret 键异值 → abort；`info.manifestName` 脱敏；新 `known[new]` 不沿用旧 `manifestNameBaseline` | ✅ 已裁决 |
| Q16 | 夹具与 origin 验收 | 夹具**不得用 `add` 构造 legacy**（#260 后 add 取清单名、`--name` 仅期望校验）→ 直接构造 pre-#260 known/clone/settings；验收「两种旧名来源路径等价」，不要求 origin 标签 | ✅ 已裁决 |

---

## 16. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-11-17 | Phase 1 初稿（Issue #268）；隔离审查后修订 7🔴+8🟡（阶段顺序重排、可认领目录判据、C4 源 pid 排除、F13 拆分、`installedAt` 不可沿用、外树纳入 P2 等） |
| 2026-11-17 | 架构师审查通过；落定 Q1–Q6（批准）、Q7/Q9（用户裁决）、Q8（Discussion#81 已订正）；新增 §10.4 `manifestName` 面 |
| 2026-11-22 | **P0：按 2026-11-22 用户裁决回填**（Q10–Q16）——D7 冲突整体 abort、D10 keyring 按项 abort + exit 3、D12 默认 dry-run + `--apply` + 删除 `--yes/--reclone` + exit 0/1/3 + REPL 只 dry-run、`extraKnown` 默认只报告 + `--update-declarations` 仅 user、`blocked` user 改写、§7.4 认领判据修正（允许 `D.name == old`）、新增 strict loader / keyring 候选派生 / 目标键异值 abort / `info.manifestName` 脱敏 / baseline 不复制、夹具改为直接构造 pre-#260 状态、文档订正（#260 baseline 已交付、rust#289/PR#299 已实现、trust 原语分层与过时理由、secret 注释两侧同型）；**docs-only，无代码改动** |
| 2026-11-22 | **P1 第三轮定向复核整改**：修 trust/blocked actions 读取顺序（_attach_scope_surfaces 先于 _attach_references）、人读表格对 blocked/not-selected 抑制执行面、`plan.errors` 脱敏、inputs.json 逐条容错（可证明非密钥的畸形条目跳过、仅可能含 password 的畸形条目 abort 并带 warning）、认领失败 action 改 unresolved |
| 2026-11-22 | **P1 第二轮复核整改**：修外树窗口的 removed-plugin 变体（候选根 raw/mapped 双探测）、strict 字段 `null` 形状、`password` 运行时归一、human/corrupt 脱敏、`install_plugin` 透传 `allow_clone`、dry-run 不建 home、成功摘要/非阻塞冲突措辞、blocked/未选项 actions、help/completer 登记、计划期歧义认领、secret repr、非数组账本记录；抽 `_locked_jsonc_rmw` 单点 |
| 2026-11-22 | **P1 独立审查整改**：修 5 项阻塞（外树已搬窗口的候选根 / P3 后 secret 清理保护 / known[old] 删除移到 P3+P4 之后 / `--json` 全量脱敏 / 迁移禁 clone）+ 加固（merge 双外树冲突、strict 字段形状、corrupt 守卫、非对象声明、重建失败 exit 3、认领排除 `.staging-*`、只读并发探测、JSON 契约）；补 14 例回归与变异复核 |
| 2026-11-22 | **P1 实现期澄清（不改语义）**：F5 明确 P1 加新键 / P3 删旧键（与 §7.1 一致）；F6 第 4 条明确「未安装且无根」的清单内 plugin 不计候选（架构师确认）；`aborted` 补入 JSON action 枚举 |
