# UAT 种子库 — 索引

> 本目录是 A2C-SMCP UAT 场景的可复用种子库。每条种子由
> [`uat-seed`](../../../uat-seed/SKILL.md) SKILL 管理。新增/修改/验收**只**通过该
> SKILL 进行。

## 顶层布局

```
seeds/
├── _common/        ← 跨源共享 SKILL 包原料（单一定义源）
├── mcp/             ← 可执行 MCP Server 种子（stdio transport）
├── marketplace/     ← Git 仓库种子
└── user/            ← 就地 DropIn 种子
```

详细规范见 [`uat-seed/resources/layout.md`](../../../uat-seed/resources/layout.md)。

## 索引

> 维护规则：每条种子**先登记后创建**；废弃前**先**检查"引用 scenarios"列。

### `_common/`

| name | axis | 形态 | acceptance | 派生引用方 |
|---|---|---|---|---|
| valid-skill-pkg | CM-01 | happy | _common/valid-skill-pkg/acceptance.md | mcp/server_resources_ok, mcp/server_resources_no_subs, marketplace/valid-single-plugin, user/home-user-basic |
| invalid-missing-desc | CM-03 | invalid (frontmatter 无 description) | _common/invalid-missing-desc/acceptance.md | user/missing-description |

### `mcp/`

| name | mode | axis | acceptance | 引用 scenarios |
|---|---|---|---|---|
| server_resources_ok | resources | happy | mcp/server_resources_ok.acceptance.sh | _待 scenario 引用_ |
| server_resources_no_subs | resources | MC-RES-01 | mcp/server_resources_no_subs.acceptance.sh | _待 scenario 引用_ |

### `marketplace/`

| name | axis | acceptance | 引用 scenarios |
|---|---|---|---|
| valid-single-plugin | MK-VAL-01 | marketplace/valid-single-plugin/acceptance.sh | _待 scenario 引用_ |

### `user/`

| name | axis | acceptance | 引用 scenarios |
|---|---|---|---|
| home-user-basic | US-VAL-01 | user/home-user-basic/acceptance.md | _待 scenario 引用_ |
| missing-description | US-ERR-02 | user/missing-description/acceptance.md | _待 scenario 引用_ |

## 待补种子

> 由 UAT scenario 写作过程中发现缺口时登记，调用
> `/uat-seed create <source> <name>` 处理后从本节移除。

_当前无_

## 审计状态

最近一次 `/uat-seed audit --all`：MVP 初次跑 —— **7/7 PASS**（详见 [acceptance 汇总](#) — 工作目录 `git status` 未提交时跑）。

| 时间 | 范围 | PASS | FAIL | 说明 |
|---|---|---|---|---|
| 2026-05-28 | MVP all | 7 | 0 | _common × 2 + mcp × 2 + marketplace × 1 + user × 2 |
