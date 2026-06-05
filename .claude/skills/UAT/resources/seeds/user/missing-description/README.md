# `user/missing-description`

**Axis**: US-ERR-02 (user-source frontmatter 缺 description)

**派生**: `_seeds.manifest` 指向
[`_common/invalid-missing-desc`](../../_common/invalid-missing-desc/)

**违规点**（唯一）: frontmatter 不含 `description` 键

**期望被测行为**:

- Acceptance 把 `_common/invalid-missing-desc/` 拷进
  `$A2C_SKILL_HOME/user/invalid-missing-desc/`
- `stage_user_skills` 在 `staging.py:_build_user_ref` 内**跳过**该 SKILL
- ERROR 日志含 `user SKILL ... SKILL.md frontmatter missing required 'description'`
- STAGED_NAMES 不含该 SKILL
- registry 内没有该 SKILL
