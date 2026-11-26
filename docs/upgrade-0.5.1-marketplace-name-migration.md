# 升级迁移：marketplace 注册名改为清单 `name`

> 适用版本：0.5.1（#268 / D3）。协议依据：`skill.md §1.2` + `runtime-contract §5.14`——注册名唯一性 /
> 禁止静默改名属协议管辖；存量注册的**一致性迁移**由 SDK 提供显式命令（本 SDK：`marketplace migrate`）。
> 设计文档：[`design-0.5.1-marketplace-name-migration.md`](design-0.5.1-marketplace-name-migration.md)。

0.5.1 起，新 `marketplace add` 以清单顶层 `name` 注册（#260）；`--name` 只作**期望名校验**、不再是别名。
升级前留下的注册仍可能是**URL 派生名**或显式 `--name` 别名。本命令把这些旧注册迁移到清单名，
并同步改写所有引用面。

## 1. 谁需要迁移

先自查（只读）：

```bash
# 1) 注册名与清单名不同 → legacy
a2c-computer marketplace info <name>
#    输出含 manifestName / manifestNameInvalid / legacyName

# 2) 完整清单（默认 dry-run，零写入）
a2c-computer marketplace migrate
```

`marketplace migrate` 默认 **dry-run**：只打印计划与冲突，不写任何字节。

## 2. 迁移步骤

**建议先停止所有 Computer / REPL 实例**（迁移不持有全局锁，跨文件顺序一致性靠停机保证）。

```bash
# 1) 看计划与冲突（零写入）
a2c-computer marketplace migrate

# 建议先以 JSON 存档，便于脚本判定
a2c-computer marketplace migrate --json > migrate-plan.json

# 2) 确认后写盘
a2c-computer marketplace migrate --apply

# 3) 可选：同时改写 user scope 的 extraKnownMarketplaces 声明
a2c-computer marketplace migrate --apply --update-declarations
```

- `--name <old>`（可重复）只缩小选择范围；**不缩小冲突判定范围**（冲突只要涉及选中项就整体 abort）。
- `--update-declarations` 只改 **user scope** 的 `extraKnownMarketplaces`；project/local 永远只报告。
- 迁移**不联网**（无 `--reclone`；缺 catalog 请用 `marketplace add/refresh` 恢复）。

## 3. 退出码

| 码 | 含义 | 处置 |
|----|------|------|
| `0` | 成功 / 无操作 | — |
| `1` | **冲突整体 abort**（未写任何字节）或命令级用法错误 | 按输出修复冲突（改名/删重复注册/处理目标目录）后重跑 |
| `3` | **部分完成**：有 `unresolved` / `blocked` 项、keyring 不可用导致某项 abort、或存在 project/local 手动引用 | 按报告逐项处理后**重跑**（幂等，不会重复迁移已完成项） |

## 4. 破坏性说明（务必阅读）

- **plugin id 变化**：`<plugin>@<old>` → `<plugin>@<new>`。外部脚本 / 文档 / CI 里硬编码的 pid 需同步。
- **project / local 层不改写**：用 `--scope project|local` 安装并启用的插件，需在对应
  `.tfrobot/settings[.local].json` 手工改 `enabledPlugins` / `installedPlugins` / trust / blocked / 声明，
  否则迁移后处于未启用态（报告的 `followUps` 会给出文件、JSON 指针与建议值）。
- **`extraKnownMarketplaces` 默认只报告**：user scope 可加 `--update-declarations`；project/local 必须手改。
  若不改，宿主调用 `reconcile` 时会按声明**重建旧名注册**（`reconcile-will-resurrect` 告警）。
- **env 注入值**（`A2C_SMCP_<ENV_SEGMENT>`）随 scoped id 变化，需自行更新。
- **secret 目标键冲突**：若 `new` 下已存在不同值的 secret / input 值，该项会 abort（不覆盖既有值）。
- **keyring 不可用且存在旧 secret**：该项 abort（零写入）；修好 keyring 后重跑。
- 迁移**无回滚**（forward-only）：失败后重跑续做；不接受 `--rollback`。

## 5. 离线 / 无清单（grandfather）

| 情形 | 行为 |
|------|------|
| 有本地 clone + 清单可读 | 完全离线迁移（只读盘 + rename + 写 settings） |
| clone 存在但清单不可读 | `unresolved: manifest-unreadable`，保留旧名；可先 `marketplace refresh` |
| clone 缺失 | `unresolved: catalog-missing`；按 `upgrade-0.5.1-marketplace-catalog-recovery.md` 或 `marketplace add/refresh` 恢复 |
| `manifestNameBaseline` 与当前清单名不符 | `blocked: baseline-mismatch`——协议禁止的静默改名，先按 §5.14 处置 |

## 6. 迁移后确认

```bash
a2c-computer marketplace list
a2c-computer marketplace info <new-name>     # legacyName 应为 false
a2c-computer plugin list                     # pid 应为 <plugin>@<new-name>
a2c-computer plugin enable <plugin>@<new-name>   # project/local 手工处理后
```

## 6.5 已知边界

- **keyring 无跨平台列举 API**：**已从新清单移除**的 plugin（无论是否仍安装）其旧 secret 不会被重键
  （#301 D14 carve-out：无 rename 目标即不重绑），会留在 keyring 中成为孤儿（不影响运行，只是残留）；
  可重录或手工清理。**仍在新清单**但根 / `inputs.json` 不可读的已安装 plugin 会整项 abort（退出码 3），
  修复根或 inputs 后重跑即可续做。
- **迁移期间请停止其他 Computer / REPL 实例**：`--apply` 写入前会重新检测 + 重新预检，并在写入时
  复核目标 input/secret 键；但跨文件顺序一致性没有全局锁，并发写入仍可能造成部分项 abort（exit 3）。
- **`install_plugin` / 其他进程在迁移窗口内改账本**：无法完全避免；重跑即可续做。
- **flag / policy 层引用不迁不改，且参与 §5.14 re-add 守卫（#296）**：迁移只改写 user scope 与显式
  `--scope project|local`；`--settings`（flag）文件与 policy（MDM）层是只读面，不追溯改写。其中的
  `<plugin>@<name>` 旧引用仍算意图引用——迁移后 `marketplace remove` + 同名不同源 re-add 会因这些残留引用
  以 `validation` / `dangling-references` 被拒绝；需在声明这些引用的层里显式清理后再 re-add。

## 7. 失败与续跑

- 迁移按 marketplace 串行、每项前向收敛；**重跑 = 续跑**（已完成项自动 no-op）。
- 崩溃后可安全重跑：`known[new]` 已写但目录未搬 → 自动认领旧目录；目录已搬但旧键未删 → 自动收敛。
- 无法归因的孤儿目录**不会**被认领（报告 `catalog-missing`），避免搬错目录。
