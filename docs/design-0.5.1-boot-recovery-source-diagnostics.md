# 设计文档：0.5.1 — boot 治理恢复对「声明源变化」只诊断、不换源

> 状态：**已实现**（#287，镜像 rust-sdk#295）。
> 关联：python-sdk#287（本单）/ rust-sdk#295（镜像）/ #260（marketplace 注册名与同名不同源语义）/
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
2. 无 `known_marketplaces[name]` 记录 → 不检测（没有「已注册源」可比；该情形由既有
   `failed_marketplaces` 降级路径负责）。
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
| recovery 日志 | 1 条 `WARNING` 汇总（冲突数）+ 每市场 1 条 `DEBUG`（两侧脱敏 URL） |
| `Computer.boot_up` 汇总行 | `... N declared-source conflicts`，且冲突数**单独即可触发**汇总行（否则「零恢复 + 有冲突」会静默无痕） |
| CLI `run_governance_remount` | **不在本版**（follow-up） |

三个字段**全部是展示值**，不可用于任何再克隆或比较：`marketplace` 走 `redact_ledger_key`（账本键可手编，
可能是携带凭据的 URL 残形），两侧 source 走 `display_source`（去 userinfo / query / fragment，path 段含
凭据样文本一律占位符）。日志同样只用这三个字段。

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

## 6. 已知边界

**声明本身不合法时，boot 不会诊断。** `declared` 在生产路径来自 `resolve_settings`，其
`_validate_extra_marketplaces`（`a2c_smcp/computer/settings/schema.py`）会把 `source` 缺失 / 非 `git` /
URL 畸形的声明**整条丢弃**（错误记进 `ResolvedSettings.errors`，`Computer` 当前不读取该字段）。于是用户把
声明 URL 改出 typo（缺 scheme、多余空格等）后重启：recovery 看到的只是「无声明」，既不报冲突也无从提示，
仍按旧源恢复。

这与本单要消灭的「静默」是**同型**问题，但根因在 settings 校验错误的呈现通道，不属于 #287 定义的检测范围
（#287 只比对**合法**声明与已注册源）。要让 boot 也 fail-closed 地提示，需要把 `ResolvedSettings.errors`
里 marketplace 相关的条目接进恢复报告——另立 follow-up，本版不做。

## 7. 不做

- 不在 boot 自动换源 / 自动 remove-re-add；
- 不改 `failed_marketplaces`、不降级、不阻断 boot；
- 不动 `reconcile`（声明式对账）的 `validation` 拒绝语义（#260）；
- 不在 CLI `run_governance_remount` 展示冲突（follow-up）；
- 不接入 `ResolvedSettings.errors`（见 §6，follow-up）；
- 不动协议仓、不新增 wire 事件 / 错误码 / 一致性向量。
