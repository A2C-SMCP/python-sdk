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
| valid-skill-pkg | CM-01 | happy | _common/valid-skill-pkg/acceptance.md | mcp/server_resources_ok, mcp/server_resources_no_subs, marketplace/valid-single-plugin, marketplace/plugin-with-bundled-mcp, user/home-user-basic |
| invalid-missing-desc | CM-03 | invalid (frontmatter 无 description) | _common/invalid-missing-desc/acceptance.md | user/missing-description |
| minimal-greet | CM-04 | happy minimal | _common/minimal-greet/acceptance.md | marketplace/strict-true-merge, marketplace/strict-false-clean, marketplace/strict-false-conflict |
| minimal-review | CM-05 | happy minimal | _common/minimal-review/acceptance.md | marketplace/strict-true-merge, marketplace/strict-false-clean |
| minimal-scan | CM-06 | happy minimal | _common/minimal-scan/acceptance.md | marketplace/strict-true-merge |

### `mcp/`

| name | mode | axis | acceptance | 引用 scenarios |
|---|---|---|---|---|
| server_resources_ok | resources | happy | mcp/server_resources_ok.acceptance.sh | _待 scenario 引用_ |
| server_resources_no_subs | resources | MC-RES-01 | mcp/server_resources_no_subs.acceptance.sh | _待 scenario 引用_ |
| server_with_window_resources | resources | MC-RES-WIN | _待补_ | resource-discovery |
| server_no_resources_capability | tools-only | MC-NO-RES | _待补_ | resource-discovery |
| binary_image_tool_server | tools (binary image) | MC-BIN happy | mcp/binary_image_tool_server.acceptance.sh | blob-transfer (B-04) |
| binary_image_tool_server_config | tools (binary image mount config) | MC-BIN mount | _(config JSON, 无独立 acceptance)_ | blob-transfer (B-04 前置) |

### `marketplace/`

| name | axis | acceptance | 引用 scenarios |
|---|---|---|---|
| valid-single-plugin | MK-VAL-01 | marketplace/valid-single-plugin/acceptance.sh | marketplace-ops, skill-discovery |
| plugin-with-bundled-mcp | MK-BMC-01 | marketplace/plugin-with-bundled-mcp/acceptance.sh | plugin-management |
| strict-true-merge | MK-STR-TRUE | marketplace/strict-true-merge/acceptance.sh | strict-mode |
| strict-false-clean | MK-STR-FALSE-CLEAN | marketplace/strict-false-clean/acceptance.sh | strict-mode |
| strict-false-conflict | MK-STR-FALSE-CONFLICT | marketplace/strict-false-conflict/acceptance.sh | strict-mode |

### `_helpers/`

| name | 用途 | acceptance | 引用 scenarios |
|---|---|---|---|
| full-protocol | F-01~12 主 Agent 驱动（自动跑 F-01/02/04/05/07/08/09/10/11，跳过 F-03/06/12） | _helpers/full-protocol/README.md | full-protocol |
| full-protocol-supplement | F-03/F-06/F-12 补测驱动 + 慢速 MCP server | _helpers/full-protocol-supplement/acceptance.sh | full-protocol |
| blob-resources | Blob 资源相关测试辅助 | _helpers/blob-resources/README.md | blob-transfer |
| error-codes | 错误码测试辅助 | _helpers/error-codes/README.md | error-codes |
| resource-discovery | 资源发现测试辅助 | _helpers/resource-discovery/README.md | resource-discovery |
| skill-discovery | SKILL 发现测试辅助 | _helpers/skill-discovery/README.md | skill-discovery |

### `user/`

| name | axis | acceptance | 引用 scenarios |
|---|---|---|---|
| home-user-basic | US-VAL-01 | user/home-user-basic/acceptance.md | _待 scenario 引用_ |
| missing-description | US-ERR-02 | user/missing-description/acceptance.md | _待 scenario 引用_ |

## 待补种子

> 由 UAT scenario 写作过程中发现缺口时登记，调用
> `/uat-seed create <source> <name>` 处理后从本节移除。

| source | name | axis | 用途 | 关联 scenario |
|---|---|---|---|---|
| user | skill-with-skillenv | US-ERR-03 | 包含 `.skillenv` 文件的 SKILL，触发 4017 forbidden | error-codes (E-06) |

## 审计状态

最近一次 `/uat-seed audit --all`：MVP 初次跑 —— **7/7 PASS**（详见 [acceptance 汇总](#) — 工作目录 `git status` 未提交时跑）。

| 时间 | 范围 | PASS | FAIL | 说明 |
|---|---|---|---|---|
| 2026-05-28 | MVP all | 7 | 0 | _common × 2 + mcp × 2 + marketplace × 1 + user × 2 |
