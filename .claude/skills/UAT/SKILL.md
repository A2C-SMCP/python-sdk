---
name: uat
description: 执行 A2C-SMCP Python SDK 的用户验收测试（UAT）。通过 tmux MCP 工具在真实终端环境中验证 CLI 命令和端到端协议流程。
---

# UAT - User Acceptance Testing Skill

## Description

用户验收测试（UAT）技能。通过 tmux 终端自动化工具，以真实用户视角对 A2C-SMCP SDK 的 CLI
命令和协议流程进行端到端验收测试。

## Instructions

### 角色定位

你是一名 QA 测试工程师，负责对 A2C-SMCP Python SDK 执行用户验收测试。你将使用 tmux MCP
工具在真实终端环境中模拟用户操作，验证 CLI 命令和协议交互是否符合预期。

### 前置条件检查

执行任何测试前，**必须**确认以下条件：

1. **项目已安装**：`uv sync --all-groups` 已执行，`a2c-computer` 命令可用
2. **tmux MCP 可用**：能调用 `mcp__tmux__*` 系列工具（创建 session、执行命令、捕获输出）
3. **测试 marketplace 仓库可访问**：有可用的 Git URL 用于 marketplace 测试

如果任一条件未满足，**停止测试**并提示用户先完成准备工作。

### 执行协议

1. **加载场景**：读取 `resources/scenarios/<scenario>.md`
2. **种子前置 audit**：识别场景引用的所有 `resources/seeds/<source>/<name>`；逐条跑
   acceptance（`/uat-seed audit <source> <name>`）。任一 ❌ → 进入
   [Seed 升级流程](#seed-升级流程确认是-seed-病) 后再执行 scenario；
   任一缺失 → 进入 [Seed 缺口流程](#seed-缺口流程scenario-需要的-seed-不存在)
3. **准备环境**：按场景要求创建 tmux session、启动必要进程
4. **逐用例执行**：
   - 通过 tmux `execute-command` 发送 CLI 命令
   - 通过 tmux `capture-pane` 获取输出
   - 对比预期结果，标记 PASS / FAIL
   - 用例 FAIL 时**先**走 [诊断三问](#诊断三问fail-时逐条回答)，判定是 SUT 病还是
     seed 病，再决定走 `/fix-issue` 或 `/uat-seed` 还是直接报 FAIL
5. **收集日志**：捕获所有 tmux pane 的完整输出
6. **输出报告**：汇总所有用例的执行结果 + 「Seed 反馈」节

### tmux 环境管理

#### Session 命名规范

```
a2c-uat              # 主 session
a2c-uat-server       # Server 进程（需要完整链路时）
a2c-uat-computer     # Computer 进程（需要完整链路时）
a2c-uat-agent        # Agent 进程（需要完整链路时）
a2c-uat-cli          # 独立 CLI 命令（不需要完整链路时）
```

#### 日志捕获策略

**所有测试必须保证三端日志可收集**。具体策略：

1. **tmux pane 捕获**：每个测试步骤后，使用 `capture-pane` 获取对应 pane 的完整输出
2. **文件日志兜底**：Server/Computer/Agent 的输出同时重定向到 `/tmp/a2c-uat-logs/` 目录
3. **失败时全量收集**：任一用例 FAIL 时，立即捕获所有 tmux pane 的最近 200 行输出

日志文件路径约定：

```
/tmp/a2c-uat-logs/
├── server.log        # Server 进程输出
├── computer.log      # Computer CLI 输出
└── agent.log         # Agent 客户端输出
```

启动进程时使用 `tee` 双写：

```bash
uv run python server_script.py 2>&1 | tee /tmp/a2c-uat-logs/server.log
```

#### 进程生命周期

```
1. 创建 tmux session + windows
2. 启动进程（重定向到日志文件）
3. 等待进程就绪（轮询 capture-pane 检查输出）
4. 执行测试用例
5. 测试完成后 kill session（自动清理所有进程）
```

### CLI-only 场景 vs 完整链路场景

#### CLI-only 场景（如 marketplace-ops）

只需要一个 tmux window，直接执行 `a2c-computer` 子命令：

```
tmux session: a2c-uat
└── window: cli  →  a2c-computer marketplace add <url> --trust
```

启动方式：

1. 创建 tmux session `a2c-uat`
2. 在默认 window 中执行命令
3. `capture-pane` 获取输出验证

#### 完整链路场景（如 full-protocol）

需要三个 tmux window，分别运行 Server / Computer / Agent：

```
tmux session: a2c-uat
├── window: server     →  Server 进程（端口动态分配，写入 /tmp/a2c-uat-port）
├── window: computer   →  a2c-computer run --url http://127.0.0.1:<PORT>
└── window: agent      →  Agent 客户端 Python 脚本
```

启动流程（严格按顺序）：

1. 创建 tmux session `a2c-uat`
2. 创建 3 个 window（server / computer / agent）
3. **先启动 Server**：在 server window 执行启动脚本，等待端口就绪
4. **再启动 Computer**：读取端口后执行 `a2c-computer run --url ...`，等待连接成功
5. **最后启动 Agent**：执行 Agent 客户端脚本

详见 `resources/test-env-setup.md`。

### 测试原则

- **真实进程视角**：测试启动真实的 Python 进程，不使用 mock
- **可观测性**：每个关键步骤捕获 tmux pane 输出；失败时全量收集所有 pane 日志
- **幂等性**：测试不应产生残留状态（marketplace remove 清理、tmp 目录隔离）
- **独立性**：每个场景可独立执行
- **容错性**：单个用例失败不阻塞后续用例执行
- **种子可信**：场景引用的 seed（`resources/seeds/<source>/<name>`）必须当次 audit
  PASS 才作为前置；不允许带 FAIL/Flaky 的 seed 进入正式测试

### Seed 依赖与触发 `/uat-seed`

> Scenario 中所有 fixture（MCP Server、marketplace 仓库、SKILL 包等）**只能**通过
> `resources/seeds/<source>/<name>` 路径引用——不在 scenario 文档里 inline 长 fixture。
> 当 seed 行为与 scenario 期望不符时，本节定义诊断 → 升级流程。

#### 触发条件：从 UAT 看到的三种"seed 病"

| 现象 | 含义 | 默认动作 |
|---|---|---|
| **种子 acceptance FAIL（仍可复现）** | seed 自身坏了（被 _common 改动 / SDK 行为变化 / 平台差异） | 暂停 scenario → `/uat-seed audit <source> <name>` 复现 → 走 upgrade |
| **scenario 期望与 seed 提供的"被测行为"对不上** | seed 的失败维度不准 / 触发的不是 scenario 想测的代码路径 | 暂停 → `/uat-seed audit` 取 seed 实际期望 → 与 scenario 期望比对 → 二选一改 |
| **scenario 需要的形态在 seeds 不存在** | 缺新 seed（新模式 / 新失败维度 / 新组合） | 进入 [缺口流程](#seed-缺口流程) |

#### 诊断三问（FAIL 时**逐条**回答）

1. **是 SUT bug 还是 seed bug？**
   - SUT bug：被测系统（a2c-smcp）行为变了但没改对 → 走 `/fix-issue`，不动 seed
   - Seed bug：seed 自身的资产 / acceptance 与协议条款偏离 → 走 `/uat-seed` upgrade
   - 判据：`/uat-seed audit <name>` 独立跑能否复现 FAIL？能 → seed 病；不能 → SUT 病
2. **协议依据是否还在？**
   - 在 `a2c-smcp-protocol/docs/specification/skill.md` 找 seed 的 axis 对应条款
   - 找不到 → 该 seed 不应存在，或协议有变更未同步（走 `/add-feature` 协议先行）
3. **seed 期望与 scenario 期望一致吗？**
   - seed README 写明的"期望被测行为"是 scenario 真正想验证的目标吗？
   - 不一致 → 改 scenario 期望（向 seed 看齐）或 改 seed（向 scenario 看齐）；两边
     都不能动 → 写一条新 seed

#### Seed 升级流程（确认是 seed 病）

```
1. /uat-seed audit <source> <name>            # 独立复现
2. 决定 upgrade 维度：
   - 资产本体（脚本 / 目录 / 归档）问题 → 修资产
   - acceptance 断言过紧 / 过松 → 修 acceptance（确保仍对齐协议条款）
   - axis 在 failure-axes.md 不准确 → 先改 failure-axes
3. /uat-seed audit <source> <name>            # 验收
4. 若 seed 派生自 _common/<x> → 跑全部派生方 audit（README 内"已派生引用"列）
5. 回到 scenario，重跑相关 UAT 用例
6. 在 UAT 报告"Seed 反馈"节登记升级条目（含 axis、修了什么、为什么）
```

#### Seed 缺口流程（scenario 需要的 seed 不存在）

> 在 UAT 执行**期间**遇到（不是 scenario 编写时——那是 `/uat-scenario` 的责任）：
> 例如 P1/P2 用例发现需要"archive 模式 happy" 但 seed 库当前只有 resources 模式。

```
1. 在当前 UAT 报告暂列该用例为 ⏭️ Skipped + 注明"待 seed: ..."
2. 完成本场景其他用例
3. 场景结束时统一汇报缺口：
   - 缺口名（按 failure-axes.md 命名约定）
   - 在哪条用例需要
   - 期望被测行为（一句话）
4. 由用户决策：
   a. 立刻调 /uat-seed create <source> <name> 补 → 补完回测
   b. 留到下个开发周期，本场景接受 Skipped 通过
```

**反模式**（**禁止**）：

- 在 scenario 临时 inline 一份 fixture 凑用例通过
- 改 seed 让它"勉强"匹配错的 scenario 期望
- 跳过有 FAIL 的 seed 直接跑场景（"反正大概率能过"）

### 二次复验机制

对所有 **FAIL** 结果执行二次复验：

1. **重试执行**：等待 2-3 秒后重新执行相同命令
2. **pane 输出复查**：增加 `capture-pane` 的行数，检查是否有延迟输出
3. **综合判定**：两次结果一致则采信，不一致标记为 **Flaky**

### 报告格式

```
## UAT 报告 - [场景名称]
日期：YYYY-MM-DD
分支：[当前分支]
环境：tmux session a2c-uat

### 测试结果摘要
- 总用例数：N
- 通过：N ✅
- 失败：N ❌
- 跳过：N ⏭️

### 用例详情
| # | 用例 | 优先级 | 引用 seed | 结果 | 复验 | 备注 |
|---|------|--------|-----------|------|------|------|
| 1 | ...  | P0     | seeds/mcp/server_resources_ok | ✅   | -    |      |

### 失败用例详情
#### [用例名称]
- **步骤**：...
- **预期**：...
- **实际**：...
- **诊断**：SUT bug / Seed bug / Scenario 期望不准（参 [诊断三问](#诊断三问fail-时逐条回答)）
- **引用 seed**：`seeds/<source>/<name>` —— 当次 `/uat-seed audit` 状态：✅ / ❌
- **tmux 输出**：
  ```
  [粘贴 capture-pane 内容]
  ```
- **日志线索**：[从 /tmp/a2c-uat-logs/ 中提取的关键错误信息]

### Seed 反馈（本场景对种子库的变更需求与已执行升级）

| 类型 | seed | axis | 触发用例 | 动作 | 状态 |
|---|---|---|---|---|---|
| upgrade | seeds/mcp/server_resources_ok | happy | F-05 | 修 acceptance.sh 断言：补 ref source 字段验证 | ✅ done |
| 缺口 | seeds/mcp/server_archive_ok | happy | F-08 | `/uat-seed create mcp server_archive_ok` | ⏭️ 待执行 |
| 缺口 | seeds/marketplace/strict-false-conflict | MK-STR-02 | M-12 | 同上 | ⏭️ 待执行 |

> **规则**：本节为空 = 当次跑没有 seed 病；任一行 ⏭️ 待执行 意味着相关用例仍是
> Skipped；用户决策补 seed 后回测。

### 进程日志摘要
#### Server
```
[最近 N 行关键输出]
```
#### Computer
```
[最近 N 行关键输出]
```
#### Agent
```
[最近 N 行关键输出]
```
```

## User-invocable

- name: uat
- description: 执行 A2C-SMCP SDK 用户验收测试（UAT）。指定场景名执行单场景；不指定场景则进入全量测试模式。

## Arguments

- scenario: （可选）测试场景名称（如 marketplace-ops, full-protocol）。不填则全量执行。

## Prompt

请执行 UAT 测试。

首先确认前置条件：

1. `a2c-computer` 命令是否可用？
2. tmux MCP 工具是否可用？
3. 测试 marketplace 仓库是否可访问？

---

**判断执行模式：**

### 单场景模式（`$scenario` 已指定）

加载场景 `$scenario` 的测试用例并开始执行。

---

### 全量测试模式（未指定 `$scenario`）

扫描 `resources/scenarios/` 目录，获取所有可用场景列表。

#### 执行顺序

1. **CLI-only 场景优先**（无需多进程）：marketplace-ops → plugin-management → settings-scope
2. **完整链路场景随后**（需多进程）：full-protocol → strict-mode → blob-transfer → skill-discovery

#### 全量测试执行协议

对每个场景，严格按以下步骤执行：

**步骤 1：环境准备**

按场景类型（CLI-only / 完整链路）创建 tmux session 和必要的进程。

**步骤 2：执行场景**

执行该场景的所有测试用例，完整流程：加载场景 → 逐用例执行（发送命令、捕获输出、对比预期、标记 PASS/FAIL）→ 输出该场景 UAT 报告

**步骤 3：环境清理**

每个场景完成后：
1. 捕获所有 tmux pane 输出作为日志
2. Kill tmux session 清理进程
3. 清理 `/tmp/a2c-uat-*` 临时文件

**步骤 4：上下文管理**

执行 `/compact [场景名] UAT 完成。通过 X/Y 用例。[若有失败：失败用例：xxx]` 压缩上下文。

**步骤 5：结果判断**

- **全部通过（✅）**：继续下一个场景
- **存在失败（❌）**：压缩上下文后，**立即中止全量测试**，输出已完成场景的进度汇总

#### 全量测试完成后

输出总汇总报告。
