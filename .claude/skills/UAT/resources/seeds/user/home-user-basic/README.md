# `user/home-user-basic`

**Axis**: US-VAL-01 (user-source happy, `<home>/user/`)

**派生**: `_seeds.manifest` 指向
[`_common/valid-skill-pkg`](../../_common/valid-skill-pkg/)

**期望被测行为**:

- Acceptance 把 `_common/valid-skill-pkg/` 内容拷进
  `$A2C_SKILL_HOME/user/valid-skill-pkg/`（**目录 basename = `valid-skill-pkg`**，与
  frontmatter `name` 一致，符合 user 源 §5.0 单段裸名约束）
- `stage_user_skills(registry, home, workdirs=())` 注册 `valid-skill-pkg`
- ref `source` 字段 = `"user"`，`path` 指向就地目录（不是被拷贝到别处）
