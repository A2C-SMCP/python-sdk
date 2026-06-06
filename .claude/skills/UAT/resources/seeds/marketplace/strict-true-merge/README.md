# `marketplace/strict-true-merge`

**Axis**: MK-STRICT-TRUE

**形态**: marketplace 工作树，strict=true（默认），entry.skills + plugin.json.skills 追加合并

**用途**: 供 `strict-mode` UAT 场景 S-01 复用

**提供**:
- marketplace 名: `strict-true-merge`
- plugin `audit`，含 3 个 skill 目录：
  - `skills/greet`（约定目录，始终扫描）
  - `extra-skills/review`（entry.skills 指定）
  - `more-skills/scan`（plugin.json.skills 指定）
- marketplace.json entry 含 `skills: ["extra-skills"]`
- plugin.json 含 `skills: ["more-skills"]`

**期望被测行为**:
- `marketplace add` 注册 3 个 skill：`audit:greet`、`audit:review`、`audit:scan`
- 所有 skill source = `marketplace:strict-true-merge`
