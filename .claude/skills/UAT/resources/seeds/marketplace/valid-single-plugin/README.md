# `marketplace/valid-single-plugin`

**Axis**: MK-VAL-01 (happy: 1 plugin 1 skill)

**形态**: 完整 marketplace 工作树 → acceptance 转为本地 bare repo → `stage_marketplace_skills` 注册

**派生**:

- `plugins/foo/skills/valid-skill-pkg/_seeds.manifest` 指向
  [`_common/valid-skill-pkg`](../../_common/valid-skill-pkg/)
- 目录名 `valid-skill-pkg` 与 `_common` SKILL.md frontmatter `name` 一致（marketplace
  §4 包根目录名 = frontmatter.name 契约）

**期望被测行为**:

- `stage_marketplace_skills("uat-seed-mp", {type:git, url:file://…}, ...)` 成功
- 注册 1 个 SKILL，name = `foo:valid-skill-pkg`（`<plugin>:<skill>` 合成）
- `source` 字段 = `marketplace:uat-seed-mp`
- 物化目录 `<home>/marketplace/uat-seed-mp/` 存在
- 物化包根 `<home>/marketplace/uat-seed-mp/plugins/foo/skills/valid-skill-pkg/SKILL.md` 存在
