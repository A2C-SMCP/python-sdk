# 设计文档：0.5.1 — boot 治理恢复对「声明源变化」只诊断、不换源

> 状态：**已实现**（#287 声明源冲突诊断；**#293 非法声明可观测**，两者均已合入 develop）。
> 关联：python-sdk#287（声明源冲突）/ **python-sdk#293（非法声明接入恢复报告）** / rust-sdk#295（#287 镜像）/
> rust-sdk#267（rust 侧非法声明可观测性跟踪）/ #294（CLI 三类展示，消费本单 report 字段，**已实现**）/
> #295（`settings show` 凭据回显，另案）/ #260（marketplace 注册名与同名不同源语义）/
> #268（存量注册名迁移，D3）/ #261 / #263 / #281（source 与账本键展示脱敏口径）/
> a2c-smcp-protocol Discussion [#81](https://github.com/A2C-SMCP/a2c-smcp-protocol/discussions/81)（双端对齐留痕）。
> 性质：**SDK 自治**（字段名与 API 形状由 SDK 决定，两端只要求语义一致）；**无 wire 变更**、无新事件、
> 无新错误码、不动协议结构、不新增一致性向量。

---

## 1. 问题

`extraKnownMarketplaces[name].source` 是 config-first 的**声明源**，`known_marketplaces.json` 是它的
派生物。但 0.5.0 及以前，boot 的治理恢复（`recover_marketplace_skills`）只读 `known_marketplaces.json`
的已注册源去 stage，**从不比对**声明源。于是：

- 用户把某 marketplace 的声明源改到另一个仓库后重启 → boot 静默按旧源恢复，既无提示也不拒绝；
- 用户以为「改了配置就换了源」，实际仍在跑旧仓库的内容——直到某天发现行为对不上才回头排查。

## 2. 协议依据

- **runtime-contract §2.3**：权威方向 MUST 为「意图 → 派生态」，且**不可逆**；known / 物化账本 / clone
  都是派生物，不得作为独立输入参与决策。
- **runtime-contract §5.14**：同一注册名 MUST 至多绑定一个 source；**同名不同源 MUST 失败、MUST NOT
  静默覆盖 / 合并 / 替换**；同源判定规则由 SDK 定义，**无法确定时 MUST fail-closed**；换源只经显式
  `remove` → re-add。

两条合起来给出本设计的两半：

| 依据 | 结论 |
|------|------|
| §5.14「MUST NOT 静默替换」 | boot **不得**自动改用声明源——那正是被禁止的静默换源 |
| §2.3 + 「MUST NOT 静默」 | 不一致**必须**让用户看见 ⇒ 诊断是必需项，不是可选项 |

> 注：§5.14 的「MUST 失败（结构化 `validation`）」约束的是**注册提交点**（`add` / re-add / refresh）。
> boot 恢复不是注册提交点，没有可返回 `validation` 的调用方，且启动不得被阻断；因此这里的形态是
> **诊断 + 保持既有注册**——结果与「拒绝换源」一致（旧注册、旧内容、旧能力都不动），只是不产生错误返回。

## 3. 行为

### 3.1 检测

对 **`installedPlugins` 意图涉及**的每个 marketplace（无安装意图的市场不检测；空意图早退行为不变）：

1. 无 `extraKnownMarketplaces[name]` 声明条目 → 不检测（没有「声明源」可比）。
2. 无**可用**的 `known_marketplaces[name]` 记录（记录缺失 / 记录非映射 / `source` 缺失或非映射）→ 不检测
   （没有可比的「已注册源」）。该情形不由本诊断报告：**非惰性市场**由 stage 循环记入 `failed_marketplaces`
   （降级）；**全惰性市场**（`installed_disabled` ∧ 账本完好）在 stage 循环里先零动作 `continue`，因此两处
   报告都不出现。判据与 stage 循环共用 `store.known_marketplace_source`（单一出处），否则同一次恢复会同时打
   「沿用已注册源」与「已降级」两条互斥诊断。
3. 否则用 **`same_source`**（SDK 单一权威，与 `reconcile` 同一函数）比对两侧 `source`。

`same_source` **只做字符串级比较**：`url` 去首尾空白、其余键逐字段相等。`ssh://` / `https://` /
scp-like / `.git` 后缀等等价写法**不归一**——协议明许 SDK 不做等价归一，无法确定即 fail-closed
（视为不同源）。任一侧 source 不可识别（非 `git` / 缺 `url` / 非映射）同样视为不同源。

检测**不区分市场是否活跃**：`installed_disabled`（已装未启用）的市场同样会被诊断——口径是「意图涉及
即检测」，而不是「本轮有动作才检测」。

### 3.2 恢复行为

**不变**。仍按 `known_marketplaces.json` 的**已注册源** stage；SKILL / bundled server 归属照常恢复；
`failed_marketplaces` 不变、lifecycle 不降级、boot 不阻断。

### 3.3 可见面

| 面 | 内容 |
|----|------|
| `GovernanceRecoveryReport.marketplace_source_conflicts` | `list[MarketplaceSourceConflict]`，条目含 `marketplace` / `declared_source_display` / `known_source_display` |
| `GovernanceRecoveryReport.marketplace_declaration_errors` | `list[MarketplaceDeclarationError]`（#293），条目含 `scope` / `marketplace` / `field` / `reason` / `source_path` |
| recovery 日志（冲突） | 1 条 `WARNING` 汇总（**冲突数 + 脱敏后的市场名列表**，不含 URL）+ 每市场 1 条 `DEBUG`（两侧脱敏 URL） |
| recovery 日志（声明错误） | 1 条 `WARNING` 汇总（**条数 + 脱敏后的市场名列表**）+ 每条 1 条 `DEBUG`（scope / 脱敏 field / 脱敏 reason / source_path） |
| `Computer.boot_up` 汇总行 | `... N declared-source conflicts, M marketplace declaration errors`；冲突数**与**声明错误数各自**单独即可触发**汇总行（否则「零恢复 + 有冲突/有错误」会静默无痕） |
| CLI `run_governance_remount` | [#294](https://github.com/A2C-SMCP/python-sdk/issues/294) 起**分三类打印**（降级 → 源冲突 → 声明错误，与 rust 同序），文案锚点各不相同：`settings.json[<scope>]` / `declared source differs` / `degraded during`；冲突与降级**只展示脱敏市场名**（两侧 source 仍只在 DEBUG）；无 `--json`（REPL 启动路径内部函数） |

`marketplace_source_conflicts` 三字段**全部是展示值**，不可用于任何再克隆或比较：`marketplace` 走
`redact_ledger_key`（账本键可手编，可能是携带凭据的 URL 残形），两侧 source 走 `display_source`（去
userinfo / query / fragment，path 段含凭据样文本一律占位符）。日志同样只用这三个字段。

`marketplace_declaration_errors` 的五个字段同样是展示值：`field` 保留 `extraKnownMarketplaces.` 前缀便于
定位，其后的名字段按 `redact_ledger_key`（**段漏斗**，比散文脱敏更严）展示，`reason` 走
`redact_git_urls_in_text`（散文，保留句式）+ 控制/方向覆写字符清洗，`source_path` 走**路径轴脱敏**，且
「无法排除 userinfo」时整值 fail-closed（详见 §6.1）。

### 3.4 非法声明（#293）

§3.1 的检测只覆盖**合法**声明。声明本身不合法时（漏 scheme、`owner/repo` 简写、非 `git`、缺 `source`），
`_validate_extra_marketplaces` 会把它**整条丢弃**，于是 recovery 看到的是「无声明」——既不报冲突也无从
提示，仍按旧源恢复。这与本设计要消灭的「静默」同型，只是根因在 settings 校验错误的**呈现通道**。

#293 把它接进同一份报告：

| 项 | 口径 |
|---|---|
| 采集范围 | **全部** `extraKnownMarketplaces` 前缀的 settings 校验错误（`field == 前缀` 或 `前缀 + "."`），**含无安装意图的市场**、**含 flag 层**；同层的 `enabledPlugins` / `installedPlugins` 非法键不并入（另案） |
| 分层语义 | **逐条 + `scope`**，不聚合「该市场无声明」——user 层非法而 local 层合法时，合并视图仍有可用声明，该层错误单独报 |
| 与冲突的关系 | **正交、不重复计数**：被丢弃的声明不在 `declared` 视图里，§3.1 的检测看不到它；同一市场名可在两个列表各出现一次（那是两条不同的事实，压制任一都会误导） |
| 采集点 | `Computer.reconcile_governance`，**在** `recover_marketplace_skills` 返回后**无条件**回填——该函数拿到的 `declared` 已过校验、无处取错误；且空安装意图时它早退 |
| 显式 `declared=` 契约 | 接受 `ResolvedSettings`（携带 `.errors`）与裸 `Mapping`（遗留形态、恒空）。**类型即载体**：调用方不可能「只传视图忘传错误」 |
| 恢复行为 | **完全不变**：仍按已注册源 stage，`failed_marketplaces` / 降级 / 账本均不动 |

名字解析（`declaration_marketplace`，镜像 rust）：名称型错误（`reason` 以 kebab 文案开头）时**整个剩余段
就是名字**，不按后缀切分——非法名本身可能就是 `acme.source.url`；合法名的子字段错误才剥
`.source.url` / `.source.type` / `.source` / `.autoUpdate`。

## 4. 怎么换源

诊断出来之后，换源仍是**显式**动作：

```bash
a2c-computer marketplace remove <name>     # 级联处理其下插件（或 --keep-plugins 保留安装态）
a2c-computer marketplace add <new-git-url> --trust
a2c-computer plugin install <plugin>@<name>
a2c-computer plugin enable <plugin>@<name>
```

boot 不会替你做这件事——重启多少次都只是重复同一条诊断。

## 5. 与 #268 迁移模型的分工

| | #287（本单） | #268（D3 存量注册名迁移） |
|---|---|---|
| 触发 | 每次 boot | 显式命令 `marketplace migrate` |
| 处理对象 | 声明源 vs 已注册源**不一致** | 注册名 ≠ 清单 `name` 的**存量注册** |
| 是否改声明 | **不改** | **不改** `extraKnownMarketplaces`（见该设计 §1.2、§2.3） |
| 是否换源 | **不换**（只诊断） | 不换（只改名） |

两者互不替代：迁移把「旧名」改成「清单名」，但**不会**替用户改声明源；本单诊断声明源与注册源不一致，
但**不会**替用户改名或换源。都跑过之后仍不一致的，只能由用户显式 remove / re-add。
详见 [`design-0.5.1-marketplace-name-migration.md`](design-0.5.1-marketplace-name-migration.md)。

## 6. 与 rust-sdk#295 的差异

两端**语义一致**（同名不同源 → 只诊断、不换源、旧能力保留、不降级），但三处实现口径不同，均属有意的 SDK
自治差异，**不是漂移**：

| 维度 | rust-sdk#295（PR rust-sdk#298） | python-sdk#287（冲突诊断） |
|------|-------------------------------|----------------------|
| 检测范围 | `declared ∩ known` **全量**（含已注册但从未安装的市场） | **`installedPlugins` 意图涉及**的市场（2026-11-20 用户裁决收窄） |
| 日志形态 | 逐条结构化 `WARN`（`reason=marketplace_source_conflict`） | 一条 `WARN` 汇总（**含脱敏后的市场名**，不含 URL）+ 逐条 `DEBUG`（含两侧脱敏 URL） |
| CLI 展示 | `marketplace_source_conflict_line` 纯函数提示（只展示注册名） | 同类纯函数 `marketplace_source_conflict_line` / `marketplace_declaration_error_line` / `marketplace_degraded_line`（[#294](https://github.com/A2C-SMCP/python-sdk/issues/294) 已落地，文案对拍 rust） |

差异 1 的理由：用户裁决只看「有安装意图、真会被恢复」的市场，避免对从未安装的声明噪音告警；若未来要与
rust 对齐为全量，需在两端同时改并重新裁决。

差异 2 的理由：python 默认日志级别是 `INFO`；若只逐条打 `WARN`，用户要么被 N 条重复告警淹没、要么（降为
DEBUG 时）完全看不见；故日志侧改为「一条汇总点名 + 逐条 DEBUG 给 URL 明细」。rust 侧的逐条结构化 `WARN`
带 `reason` 码，适合被日志检索/告警规则消费——两端面向的消费方式不同。**CLI 展示面已由
[#294](https://github.com/A2C-SMCP/python-sdk/issues/294) 接线**（不再依赖日志级别），故「日志形态不同」
不影响用户能否看见。

差异 3 的理由（**已由 [#294](https://github.com/A2C-SMCP/python-sdk/issues/294) 收口**，当前仅剩文案资产差异：python 侧三类都做成纯函数以便单测钉措辞）：python 的 CLI 展示面曾另开 follow-up，
本版先保证 `GovernanceRecoveryReport` 与日志可观测。

### 6.1 非法声明（#293）的对拍差异

字段与 DTO 形状**逐字镜像** rust（`MarketplaceDeclarationError`：`scope` / `marketplace` / `field` /
`reason` / `source_path`；report 字段 `marketplace_declaration_errors`），四处实现口径不同，均属有意的
SDK 自治差异：

| 维度 | rust（`settings/schema.rs` + `computer.rs`） | python（本仓） | 理由 |
|------|---------------------------------------------|----------------|------|
| 脱敏落点 | **`validate_settings` 出口**统一脱敏，所有 `SettingsValidationError` 消费者（CLI 文本 / 报告 / 诊断）拿到同一安全值 | `validate_settings` / `SettingsValidationError` **保留原值**；采集处（恢复报告）与展示漏斗（`format_settings_errors`，[#295](https://github.com/A2C-SMCP/python-sdk/issues/295) 起）各自调用 `sanitized_marketplace_error`（幂等） | 校验器保留原值便于库宿主自行处置；两处消费同一函数，CLI 文本 / 报告 / 诊断的**展示值**口径一致，差异只在「错误对象本身是否带原值」 |
| Flag scope | **从 report 字段过滤掉**（其 CLI 启动边界另有打印 + `include_marketplace_declaration_errors` 抑制开关），WARN 仍记全量 | **不过滤**，`scope == flag` 照常进报告 | 用户裁决「报告**所有**前缀错误」，且已接受同次启动多条同义提示；python 没有 rust 那套抑制开关，过滤会让 flag 层错误从 report 消失（库宿主无处可见） |
| 名字脱敏强度 | 名字与 reason 同走散文脱敏（`redact_git_urls_in_text`，只去 userinfo、保留 path） | 名字走**段漏斗**（`redact_ledger_key`），reason 才走散文脱敏 | 名字是**单个不可信值**：path 段里的 `alice:PW@example.com` 会被散文脱敏原样放出（#261 已定的口径） |
| `source_path` 脱敏 | 只 `scrub_diagnostic_chars`（其测试 `test_extra_marketplaces_error_source_path_scrubbed_not_url_redacted` 明确断言 Windows 路径原样保留、不做 URL 脱敏）⇒ 路径段里的 `alice:pw@…` **会回显** | 路径轴 `sanitize_path_for_diagnostics` + **「无法排除 userinfo 即整值占位」**；无 userinfo 的路径（Windows / `alice@corp` / 普通 `%` / `?` 尾部）照常展示 | 诊断面同样不该回显路径段凭据；`/` 与 `\` 双段边界避免 scp-like 脱敏器把 Windows 路径整条吞掉（#263 四轮）。python 此处比 rust 严 |

`source_path` 采用**单一 fail-closed 判据**——「能不能排除 userinfo」，不能就整值占位：

```
剥掉 Windows 盘符前缀后：最后一个 `@` 在第一个 `:` 之后  ⇒  整值 <redacted-git-source>
否则：sanitize_path_for_diagnostics(scrub_diagnostic_chars(path))  ⇒  保留结构
```

判据在**原文**与**每一轮 `percent-decode → NFKC`** 形态上各判一次（先解码再归一，全角 `％３A` 解码后
才是 `：`；轮数上界与 #282 的
`_MAX_CREDENTIAL_DECODE_ROUNDS` 共用同一常量）。四个细节都是复现出来的承重点：

| 细节 | 为什么必须 | 退化后漏的向量 |
|------|-----------|---------------|
| `rfind("@")` 而非 `find` / `find(":")` 而非 `rfind` | `alice@corp:pw@example.com` 的首个 `@` 在 `:` 之前；`alice:pw@host:8080` 的最后一个 `:` 在 `@` 之后 | 两类整组泄漏 |
| 判**整条文本**而非逐段 | 凭据里的字面 `\` / `/`（`alice:pw/x@example.com`）会把 userinfo 拆成两段 | 段内分隔符类 |
| 剥 Windows 设备前缀 + 盘符（含无斜杠的 `C:repos\…`） | 这些 `:` 是盘符、不是 userinfo；`\\?\` 里的 `?` 还会被路径轴原语当 query 起点 | Windows 三类负对照被误遮 |
| NFKC + 多轮 decode | 全角 `：`/`＠`/`％３A`、四层 `%2525253A` | 全角类、深层编码类 |

这样能覆盖全部「路径轴原语必漏」的形态（均经复现实测）：控制/方向覆写字符穿插、`#`/`?` 夹在 `:` 与 `@`
之间、段内空白、凭据内字面 `/` 与 `\`、percent-encoded（含编码出的分隔符与前导 `@`）、全角、多层编码。
**负对照**（同样实测）：Windows `C:\repos\alice@corp\settings.json` / `C:repos\alice@corp\settings.json`（drive-relative）/ `\\?\C:\repos\alice@corp\settings.json` 逐字保留、纯 `100%25done` 路径保留、
「`@` 在前 `:` 在后」的普通路径（`alice@corp/notes:2024`）保留、`?token=` 尾部仍由路径轴原语遮蔽、
无 `:`/`@` 关系的控制字符路径仍按字符清洗。

刻意**不用** `credential_like_text` 逐段判定：该谓词对任意 `@` / `:` / 残留 `%` 都返回 `True`
（`alice@corp`、`100%25done`、`repo@2.git` 全命中），套用会把普通路径整条遮蔽、打翻上面两条负对照；
它服务的是「单个不可信值是否整体可疑」，而这里要回答的是「这条**路径**能否排除 userinfo」，判据更窄。

根因在共享原语 `utils/git_url.py::sanitize_path_for_diagnostics`（先 escape 再判段、且要求字面标记），
修它会波及 #236 / 路径 resolver 等全部调用方，故本单**不改共享原语**，只在采集处 fail-closed。

**`reason` 的散文脱敏不对 path 段 fail-closed**（与名字段不对称）：`redact_git_urls_in_text` 只去 userinfo、
保留 path。当前**不可达**——`_validate_extra_marketplaces` 的 reason 全是静态文案（`malformed git url` /
`missing or non-object 'source'` / `only source.type == 'git' is supported` / `marketplace name must be strict
kebab (1-64)` / `must be bool, got <type>`），不含任何用户值；本模块的 reason 脱敏是纵深防御（防未来某个
validator 把值拼进 reason）。若将来有 reason 携带用户值，应改走段漏斗。#295 起该 reason 脱敏同样用于
`format_settings_errors`（CLI 展示），纵深防御面随之覆盖 `settings show` / `settings get` / boot 批准流程。

`per_scope_layers` 已**对齐** rust：显式 `declared=ResolvedSettings` 也取其 `.per_scope_layers`（用户
2026-11-22 裁决，本单范围），故 project-origin bundled server 在两条路径上都过 #159 Option B 的 PENDING
信任门；裸 `Mapping` 是其结构性缺席（无 per-scope 信息可用，协议 §2.5-5 明许行为等价）。

CLI 行**顺序与 rust 对拍**：降级 → 源冲突 → 声明错误；三条文案逐字一致（仅引号风格不同：python `!r`
给 `'acme'`、rust `{:?}` 给 `"acme"`，与 python 既有降级行一致）。python 侧把降级行也提成纯函数
（rust 是内联的），便于单测钉措辞。

两端共同点（对拍口径）：报告字段只放脱敏展示值；声明源变化**永不**导致换源；换源只经显式 remove → re-add。

## 7. 已知边界

**（已由 #293 收口）声明本身不合法时曾经不诊断。** 历史形态：`_validate_extra_marketplaces` 把 `source`
缺失 / 非 `git` / URL 畸形的声明**整条丢弃**，错误只进 `ResolvedSettings.errors`，而 `Computer` 不读该字段
⇒ 用户把声明 URL 改出 typo 后重启，recovery 只看到「无声明」，既不报冲突也无从提示。现由 §3.4 接进
`GovernanceRecoveryReport.marketplace_declaration_errors` + WARN 汇总 + boot 计数。

**（已由 [#295](https://github.com/A2C-SMCP/python-sdk/issues/295) 收口）`settings show` / `format_settings_errors`
曾对非法 marketplace 名回显原始凭据形态。** 现展示漏斗逐条复用 `sanitized_marketplace_error`（与恢复报告同一
口径，覆盖 `settings show` / `settings get` / boot 批准流程），并整行 `rich.markup.escape()`——手编
`evil[/]a[/]b` 键不再让 `settings show` / `settings get` 抛 `MarkupError`。

仍存在的相邻边界（各有归属，不在本设计范围）：

| 边界 | 归属 |
|------|------|
| `enabledPlugins` 非法键 / `mcp.json: {e}` 的 pydantic repr 仍回显原值（同一展示漏斗） | [#304](https://github.com/A2C-SMCP/python-sdk/issues/304)（`enabledPlugins` 整值过名字漏斗；`mcp.json` 行改摘要 + DEBUG 明细） |
| `installedPlugins` / `enabledPlugins` 的同类 invalid key 仍静默（不进本报告） | 另案评估（用户裁决 5） |
| `settings.json` 整文件不可解析时只记一条 `<file>` 错误，不含 marketplace 子树 | 既有行为（§5.6 容错），不在本设计范围 |
| `reason` 的散文脱敏不对 path 段 fail-closed（当前 reason 全静态、不可达） | §6.1；reason 若引入用户值时改走段漏斗 |
| `sanitize_path_for_diagnostics` 的「先 escape 再判段 + 要求字面 `@`/`?`/`#`」在**其他**调用方仍可能漏 path 段凭据（控制字符邻接 / 段内空白 / percent-encoded 三类） | 共享原语问题，不在本单；本单只在 `source_path` 采集处 fail-closed |
| 当 `.tfrobot/mcp.json` 解析出 ≥1 个 server 时，同一份非法声明会在同一次启动出现**两条** CLI 行（`run_mcp_approval` 的 `format_settings_errors` + 本单的声明行），叠加 boot/重挂两条 `WARN` ≈ 4 条同义提示 | 已知边界：用户 2026-11-22 裁决接受同义提示；`format_settings_errors` 的脱敏已由 [#295](https://github.com/A2C-SMCP/python-sdk/issues/295) 收口（措辞差异保留）。**不**为去重而重新解析 settings（违反「只消费 report 字段」） |
| project-origin bundled server 落 PENDING 后，CLI 只打 INFO 日志、**没有批准入口**（`run_mcp_approval` 不覆盖 bundled 声明面）——审批由 client / 宿主承担（参考 client 未来补线索） | follow-up（架构师开跟踪 issue，不在 #293/#294 实现） |

## 8. 不做

- 不在 boot 自动换源 / 自动 remove-re-add；
- 不改 `failed_marketplaces`、不降级、不阻断 boot；
- 不动 `reconcile`（声明式对账）的 `validation` 拒绝语义（#260）；
- 不改 `validate_settings` / `_validate_extra_marketplaces` 的校验与丢弃语义（非法条目照旧丢弃）；
- 不把声明错误计入 `marketplace_source_conflicts` 或 `failed_marketplaces`（三者正交，不重复计数）；
- 不动协议仓、不新增 wire 事件 / 错误码 / 一致性向量。
