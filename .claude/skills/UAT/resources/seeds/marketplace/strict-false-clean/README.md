# `marketplace/strict-false-clean`

**Axis**: MK-STRICT-FALSE-CLEAN

**形态**: marketplace 工作树，strict=false，plugin.json 不声明组件字段

**用途**: 供 `strict-mode` UAT 场景 S-02 复用

**提供**:
- marketplace 名: `strict-false-clean`
- plugin `audit`，含 2 个 skill 目录：
  - `skills/greet`（约定目录，始终扫描）
  - `extra-skills/review`（entry.skills 指定）
- marketplace.json entry 含 `strict: false, skills: ["extra-skills"]`
- plugin.json 为 `{}`（不声明任何组件字段）

**期望被测行为**:
- `marketplace add` 注册 2 个 skill：`audit:greet`、`audit:review`
- 无冲突错误（plugin.json 无组件字段）
