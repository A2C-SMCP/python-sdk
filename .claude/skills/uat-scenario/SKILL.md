---
name: uat-scenario
description:
  创建、更新或删除 UAT 场景文档，确保以 tmux 终端交互验证为核心，符合 A2C-SMCP
  SDK 的 UAT 最佳实践。重型场景自动加载对应设计指南。无参数时自动扫描 Git
  变更，分析受影响的场景并批量更新。
argument-hint:
  "[create|update|delete] <场景名称>  （留空则自动扫描 Git 变更）"
---

# UAT 场景管理 — Create / Update / Delete / Auto-Scan

你是一名资深 QA 架构师，负责按标准流程管理 A2C-SMCP SDK 的 UAT 场景体系。你的工作
不仅是编写场景文档，还包括分析测试环境依赖（测试仓库、MCP 配置）、生成环境需求报告、
维护场景参考文档的完整性。

## 使用方式

```
/uat-scenario                                      # 无参数：自动扫描 Git 变更，批量评估受影响场景
/uat-scenario create strict-mode                   # 新建场景
/uat-scenario update marketplace-ops 增加 refresh 失败用例  # 更新场景
/uat-scenario delete deprecated-scenario           # 删除场景
/uat-scenario existing:scenarios/marketplace-ops.md  # 改造已有场景
```

如果省略动作前缀，通过上下文推断：提到已有场景名 + 增/改/删描述 →
update；`existing:` 前缀 → update（改造模式）；描述全新场景 → create。

## 动作分流

| 动作          | 触发条件                                                | 执行路径                        |
| ------------- | ------------------------------------------------------- | ------------------------------- |
| **auto-scan** | 参数为空                                                | → [自动扫描流程](#自动扫描流程) |
| **create**    | 明确 `create` 或描述的是新场景                          | → [创建流程](#创建流程)         |
| **update**    | 明确 `update`/`extend`/`existing:` 前缀，后跟已有场景名 | → [更新流程](#更新流程)         |
| **delete**    | 明确 `delete`/`remove`                                  | → [删除流程](#删除流程)         |

## Input

$ARGUMENTS

---

## 自动扫描流程

> 触发条件：`/uat-scenario` 不带任何参数。
>
> 目标：检查自 UAT 场景最后更新以来的所有 Git 变更，评估哪些场景需要新增或更新。

### Step A-1: 确定 UAT 基准时间

```bash
git log --format="%ai %H %s" -- ".claude/skills/UAT/resources/scenarios/" | head -5
```

取第一行的日期时间作为 **基准时间 T**。

### Step A-2: 收集 Git 变更

```bash
git log --oneline --after="<T>" -- a2c_smcp/ tests/
```

如果 commit 较多（>15 条），再用 `git diff --stat` 聚焦关键文件变动：

- `a2c_smcp/computer/cli/` — CLI 命令变更
- `a2c_smcp/computer/skills/` — SKILL 系统变更
- `a2c_smcp/server/` — Server 协议变更
- `a2c_smcp/agent/` — Agent 客户端变更
- `a2c_smcp/smcp.py` — 协议定义变更
- `tests/e2e/` — e2e 测试变更（可能暴露新的测试点）

### Step A-3: 建立"变更 → 场景"映射

读取 `.claude/skills/UAT/resources/scenarios/` 下所有场景文件，提取功能范围关键词。

映射规则：

| 变更特征                                             | 可能影响的场景                               |
| ---------------------------------------------------- | -------------------------------------------- |
| `cli/commands/marketplace`                           | `marketplace-ops.md`                         |
| `skills/manifest.py`, `skills/staging.py`            | `strict-mode.md`, `skill-discovery.md`       |
| `skills/resource.py`, `utils/blob.py`                | `blob-transfer.md`                           |
| `server/`, `smcp.py` 中的 events                     | `full-protocol.md`                           |
| `computer/cli/commands/plugin`                       | `plugin-management.md`                       |
| `computer/cli/commands/settings`                     | `settings-scope.md`                          |
| `computer/skills/watcher.py`                         | `skill-discovery.md`（watcher 相关用例）     |
| `computer/skills/sandbox.py`                         | 新场景（sandbox-security.md）                |
| `smcp.py` 中 ErrorCode 变更                          | `full-protocol.md`（错误码验证）             |
| 全新的 CLI 子命令或协议事件，无对应场景文件          | → 候选新增场景                               |

### Step A-4: 输出变更影响报告

在执行修改前，先输出分析报告：

```
## UAT 场景更新扫描报告

**基准时间**：<T>
**新增 commits**：<N> 条

### 受影响场景评估

| 场景文件 | 影响程度 | 建议动作 | 关联变更摘要 |
|---------|---------|---------|------------|
| marketplace-ops.md | 高 | update | feat: marketplace set 新增 strict 选项 |
| strict-mode.md（新） | — | create | feat: 全新的 strict 模式 |

### 可忽略变更

以下变更判断为不影响 UAT 场景（纯重构/类型标注/文档）：
- <commit>: refactor: ...
```

### Step A-5: 逐场景执行更新/创建

按影响程度从高到低，对每个需要变更的场景执行对应的 update 或 create 流程。

### Step A-6: 汇总

```
✅ 本轮扫描完成：更新 X 个场景，新增 Y 个场景，无需变更 Z 个场景。
```

---

## 创建流程

### Step 1: 信息采集与范围确认

向用户确认以下信息：

1. **功能范围**：覆盖哪些 CLI 命令 / 协议事件 / 子系统？
2. **场景类型**：CLI-only 还是完整链路？
3. **核心用户流程**：主要操作路径是什么？
4. **环境依赖**：是否需要测试仓库、MCP server 配置、特定 Server 版本？
5. **配置差异**：是否有 strict/non-strict、inline/blob 等配置组合需要覆盖？

### Step 2: 环境依赖分析

> **为什么要做这一步？** 测试环境依赖是 UAT 的地基。如果场景需要的测试仓库或
> MCP 配置不存在，测试无法执行。

检查 `.claude/skills/UAT/resources/test-env-setup.md` 中已有的环境配置，
逐项核对新场景所需的环境：

- **测试仓库**：是否需要特定 manifest 结构的 marketplace Git 仓库？
- **MCP Server**：是否需要特定的 MCP server 配置（stdio/http/sse）？
- **Server 版本**：是否需要特定协议版本（版本握手测试）？
- **SKILL_HOME**：是否需要干净的隔离环境？

**判断结果**：

- **无缺口** → 跳到 Step 4
- **有缺口** → 进入 Step 3

### Step 3: 生成环境需求报告

将报告输出到 `.claude/skills/UAT/resources/env-requests/` 目录，文件名格式
`env-request-<场景名>.md`：

```markdown
# 环境需求报告 — [场景名称]

> 关联 UAT 场景：`scenarios/<name>.md`  请求日期：YYYY-MM-DD  状态：🟡 待准备 | 🟢 已就绪

## 需求背景

[一句话说明为什么需要这些环境配置]

## 需求清单

### 1. 测试 Marketplace 仓库

| 字段 | 要求 | 说明 |
| ---- | ---- | ---- |
| manifest strict | true/false | 用于测试 strict 模式 |
| plugin 数量 | ≥2 | 用于测试多 plugin 场景 |
| skill 数量 | ≥3 | 用于测试 skill 发现 |

**仓库结构**：[描述或给出目录树]

### 2. MCP Server 配置

[如有需要]

## 对现有环境的影响

- [ ] 不影响现有环境
- [ ] 需要新建测试仓库
- [ ] 需要修改 test-env-setup.md

## 验证方式

[环境准备完成后，如何验证配置正确]
```

**交接动作**：报告生成后告知用户，并暂停场景编写直到环境就绪。

### Step 4: 选择场景类型 & 加载设计指南

根据场景特征，判断类型并加载对应的设计指南：

| 场景类型             | 特征                                                        | 设计指南                                 |
| -------------------- | ----------------------------------------------------------- | ---------------------------------------- |
| **CLI 命令类**       | 仅涉及 CLI 子命令（无需 Server/Computer/Agent）             | 无需加载，遵循通用原则                    |
| **协议流程类**       | 涉及 Agent↔Server↔Computer 完整链路                         | `resources/guides/protocol-flow.md`      |
| **Marketplace/Plugin 类** | 涉及 marketplace CRUD、strict 模式、plugin 生命周期   | `resources/guides/marketplace-plugin.md` |
| **二进制/Blob 传输类** | 涉及 SKILL blob handle、二进制 tool_call、SHA256 校验     | `resources/guides/blob-transfer.md`      |

### Step 5: 编写场景文档

输出到 `.claude/skills/UAT/resources/scenarios/<name>.md`。

#### 通用设计原则

**1. UAT ≠ 单元测试**

> UAT 的本质是站在用户视角验证产品功能，用户操作的是终端命令行，不是 Python API。

- 每个用例必须包含 **tmux 终端操作步骤**
- 断言必须基于 **终端可观测性**（命令输出、退出码、日志文本）
- **Python API 仅作为辅助**：用于准备测试环境或触发无法通过 CLI 触发的状态

严禁出现：

- 用例步骤仅包含 Python 函数调用，没有 CLI 命令
- 断言仅验证函数返回值
- 将"函数返回 None"作为通过条件

**2. 操作成本感知**

- CLI-only 场景按命令依赖关系编排
- 完整链路场景按"先建环境 → 再测功能 → 最后清理"编排
- 寻找**状态复用机会**：一个用例的终态是下一个用例的前置条件

**3. 环境隔离**

使用 `A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$` 隔离环境，避免污染真实配置。
临时目录在清理步骤中删除。

**4. 日志可收集**

所有进程输出通过 `tee` 双写到 `/tmp/a2c-uat-logs/`，确保三端日志可追溯。

#### 断言编写指南

| 类型     | 好的断言                                  | 坏的断言                              |
| -------- | ----------------------------------------- | ------------------------------------- |
| 退出码   | 命令退出码为 0                            | 函数无异常抛出                        |
| 输出内容 | JSON 输出包含 `"skills": 1`              | 内部变量值为 1                        |
| 错误信息 | 输出包含 `"error": "name conflict"`      | 异常类型为 ValueError                 |
| 状态变更 | 再次 list 已不包含该 marketplace          | 内部缓存已清除                        |
| 进程行为 | Computer pane 显示 "connected"           | socketio.Client.connected 为 True    |
| 文件状态 | clone 目录存在且包含 marketplace.json     | Path.exists() 返回 True              |

#### 文档结构

1. **测试目标** — 一句话，强调用户视角
2. **类型** — CLI-only / 完整链路
3. **前置条件** — 环境依赖、测试仓库、MCP 配置
4. **环境变量** — A2C_SKILL_HOME 等隔离配置
5. **测试用例** — 按优先级分组（P0/P1/P2），每个用例包含 tmux 命令 + 输出断言
6. **清理** — 恢复环境的步骤
7. **日志收集** — 指定哪些 pane 输出需要捕获

#### 用例编号规则

- 场景缩写 + 两位数字：如 `M-01`（Marketplace）、`F-01`（Full-protocol）
- P0 从 01 开始，P1/P2 接续编号
- 清理用例使用 99

### Step 6: 更新 test-env-setup.md

如果新场景需要特殊的环境配置（如新的测试仓库结构），更新
`.claude/skills/UAT/resources/test-env-setup.md` 中对应章节。

### Step 7: 验证清单

- [ ] 每个用例都有 tmux 终端操作步骤（非 Python API 调用）
- [ ] 所有断言基于终端可观测性（输出文本、退出码、文件状态）
- [ ] 前置条件中的环境依赖在 test-env-setup.md 中有记录
- [ ] 用例编号遵循 `缩写-NN` 规则，无重复
- [ ] P0 用例覆盖核心用户流程
- [ ] 异常场景包含具体的错误输出描述（错误信息、退出码）
- [ ] 清理用例能恢复到初始状态（保证幂等性）
- [ ] 环境使用 `A2C_SKILL_HOME` 隔离，不污染真实配置
- [ ] 日志收集策略已标注（CLI-only 仅 pane 输出；完整链路需三端）
- [ ] 如有新增环境需求，已生成环境需求报告

---

## 更新流程

### Step 1: 定位目标场景

读取 `.claude/skills/UAT/resources/scenarios/<name>.md`。

### Step 2a: 理解变更意图（标准更新）

| 变更类型       | 示例                                       | 影响范围                   |
| -------------- | ------------------------------------------ | -------------------------- |
| **新增用例**   | 「增加 marketplace refresh 失败用例」      | 新增用例，可能调整编排策略 |
| **修改用例**   | 「M-04 的预期结果需要改」                  | 局部修改，保持编排不变     |
| **调整编排**   | 「P1 和 P2 用例重新分组」                  | 重排结构，检查依赖         |
| **补充验证点** | 「增加退出码验证」                         | 在已有用例中追加断言       |
| **修正错误**   | 「这个命令路径写错了」                     | 直接修正                   |

### Step 2b: 质量审计（改造模式）

按质量审计清单检查：

| 检查项                 | 问题特征                     | 改造方向                                     |
| ---------------------- | ---------------------------- | -------------------------------------------- |
| 缺少 tmux 命令步骤     | 用例仅描述"调用函数"        | 补充完整终端命令                             |
| 断言不基于终端输出     | 断言为"返回 None"           | 改为"输出包含 xxx"、"退出码为 0"            |
| 缺少环境隔离           | 未设置 A2C_SKILL_HOME       | 补充隔离变量                                 |
| 缺少退出码验证         | 只验证输出不验证退出码      | 补充退出码断言                               |
| 缺少错误输出描述       | 异常用例只说"应报错"        | 明确具体的错误信息文本                       |
| 缺少清理步骤           | 测试后未清理临时文件/目录   | 补充清理命令                                 |

### Step 3: 加载设计指南

判断场景类型，加载对应指南，确保变更符合设计原则。

### Step 4: 执行变更

读取并编辑场景文件。变更完成后：

1. 更新场景文档的测试目标和编排结构
2. 如有新增环境需求，走 [创建流程 Step 3](#step-3-生成环境需求报告)

### Step 5: 变更验证清单

- [ ] 用例编号连续，无跳号或重复
- [ ] 编排结构间的依赖仍然正确
- [ ] 环境隔离配置正确
- [ ] 新增的环境需求已生成报告（如有）

---

## 删除流程

### Step 1: 确认删除目标

读取目标场景文件，展示基本信息，请用户确认。

### Step 2: 检查依赖

检查是否有其他场景或文档引用了该场景。

### Step 3: 执行删除

1. 删除场景文件
2. 如有对应的环境需求报告，提示用户是否一并清理
3. 检查 test-env-setup.md 是否需要更新

---

## 设计指南索引

| 指南                    | 适用场景                                     | 文件                                    |
| ----------------------- | -------------------------------------------- | --------------------------------------- |
| 协议流程类              | Server+Computer+Agent 完整链路               | `resources/guides/protocol-flow.md`     |
| Marketplace/Plugin 类   | marketplace CRUD、strict 模式、plugin 生命周期 | `resources/guides/marketplace-plugin.md` |
| 二进制/Blob 传输类      | blob handle、二进制 tool_call、SHA256 校验   | `resources/guides/blob-transfer.md`     |

指南随实践积累持续扩展。当发现新的场景类型有独特的设计模式时，创建新的指南文件并
在此注册。
