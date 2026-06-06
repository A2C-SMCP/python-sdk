# `_common/invalid-missing-desc`

**Axis**: CM-03

**形态**: invalid（frontmatter 缺 description）

**违规点**（仅一处）: frontmatter 不含 `description` 键

**期望被派生使用方式**:

- **user**: `missing-description/_seeds.manifest` 指向本目录 → 期望
  `stage_user_skills` ERROR 日志 + 不注册
- _（未来）_ **mcp**: 用于 `frontmatter_missing` 失败种子

**已派生引用**:

- seeds/user/missing-description/
