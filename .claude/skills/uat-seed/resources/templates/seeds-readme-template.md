# UAT 种子库 — 索引

> 本目录是 A2C-SMCP UAT 场景的可复用种子库。每条种子由
> [`uat-seed`](../../../uat-seed/SKILL.md) SKILL 管理。新增/修改/验收**只**通过该
> SKILL 进行。

## 顶层布局

```
seeds/
├── _common/        ← 跨源共享 SKILL 包原料（单一定义源）
├── mcp/             ← 可执行 MCP Server 种子
├── marketplace/     ← Git 仓库种子
└── user/            ← 就地 DropIn 种子
```

详细规范见 [`uat-seed/resources/layout.md`](../../../uat-seed/resources/layout.md)。

## 索引

> 维护规则：每条种子**先登记后创建**；废弃前**先**检查"引用 scenarios"列。

### `_common/`

| name | axis | 形态 | acceptance | 派生引用方 |
|---|---|---|---|---|
<!-- 示例：
| valid-skill-pkg | CM-01 | happy | _common/valid-skill-pkg/acceptance.md | mcp/server_*_ok.py, marketplace/valid-*, user/home-user-basic |
-->

### `mcp/`

| name | mode | axis | acceptance | 引用 scenarios |
|---|---|---|---|---|
<!-- 示例：
| server_resources_ok | resources | happy | mcp/server_resources_ok.acceptance.sh | skill-mcp-modes.md |
| server_archive_bad_sha | archive | MC-ARC-03 | mcp/server_archive_bad_sha.acceptance.sh | skill-mcp-modes.md |
-->

### `marketplace/`

| name | axis | acceptance | 引用 scenarios |
|---|---|---|---|
<!-- 示例：
| valid-single-plugin | MK-VAL-01 | marketplace/valid-single-plugin/acceptance.sh | skill-marketplace.md |
-->

### `user/`

| name | axis | acceptance | 引用 scenarios |
|---|---|---|---|
<!-- 示例：
| home-user-basic | US-VAL-01 | user/home-user-basic/acceptance.md | skill-user-dropin.md |
| override-low-vs-high | US-OVR-01 | user/override-low-vs-high/acceptance.md | skill-user-dropin.md |
-->

## 待补种子

> 由 UAT scenario 写作过程中发现缺口时登记，调用
> `/uat-seed create <source> <name>` 处理后从本节移除。

<!-- 示例：
- [ ] seeds/mcp/server_cursor_exceed_max_pages.py — 用于 P2 边界测试场景 X
-->

## 审计状态

最近一次 `/uat-seed audit --all`：未运行。

跑过后会在此记录：日期、PASS/FAIL 统计、FAIL 详情链接。
