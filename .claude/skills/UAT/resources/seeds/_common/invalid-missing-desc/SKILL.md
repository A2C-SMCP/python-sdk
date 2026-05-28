---
name: invalid-missing-desc
# description 故意省略 —— 触发 _build_user_ref / _finalize_and_register 跳过路径
license: MIT
metadata:
  axis: CM-03
---

# invalid-missing-desc

Frontmatter intentionally omits `description` to exercise the
"frontmatter missing required 'description'" failure path in
`staging.py:_build_user_ref` (user source) and `_finalize_and_register`
(mcp source).
