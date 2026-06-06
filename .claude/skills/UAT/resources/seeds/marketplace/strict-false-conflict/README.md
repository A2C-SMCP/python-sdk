# `marketplace/strict-false-conflict`

**Axis**: MK-STRICT-FALSE-CONFLICT

**形态**: marketplace 工作树，strict=false，plugin.json 声明组件字段 → 冲突降级

**用途**: 供 `strict-mode` UAT 场景 S-03 复用

**提供**:
- marketplace 名: `strict-false-conflict`
- plugin `audit`，含 `skills/greet` skill
- marketplace.json entry 含 `strict: false`（无 entry.skills）
- plugin.json 含 `skills: ["skills"]`（声明组件字段 → 冲突）

**期望被测行为**:
- `marketplace add` 退出码 0（降级，非硬错误）
- stderr 含 "conflicting manifests" + "strict=false" + "plugin.json declares components"
- skills 数量 = 0（plugin 被跳过）
- marketplace 已添加（`marketplace list` 可见）
