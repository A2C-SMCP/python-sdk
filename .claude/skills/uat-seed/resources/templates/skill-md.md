# 模板：标准 SKILL.md frontmatter

> 用于 `_common/` 各形态、以及种子的内嵌 SKILL.md。

## happy 形态（well-formed 最小可用）

```markdown
---
name: <kebab-case-name>           # 与目录 basename 一致（user 源除外）
description: |
  <"做什么 + 何时用"，首句包含核心触发关键词；1024 字符以内>
license: MIT                       # 可选
version: 1.0.0                     # 可选；mcp/marketplace 种子常用
allowed-tools:                     # 可选
  - Read
  - Bash
compatibility: "a2c-smcp>=0.2.1"   # 可选
metadata:                           # 可选；任意自定义元数据
  axis: CM-01
  derived-from: _common/valid-skill-pkg
---

# <Title>

<body —— LLM 实际读到的指令内容。happy 种子可以简短，重点是 frontmatter
正确性，body 仅做"存在性"占位>
```

## invalid: missing description

```markdown
---
name: invalid-missing-desc
# description 故意省略 —— 触发 _build_user_ref / _finalize_and_register 的跳过
---

# Placeholder
```

## invalid: bad name（非 kebab-case）

```markdown
---
name: InvalidCamelCase             # 触发 SkillNameError
description: "Name uses camelCase to trigger validation."
---

# Placeholder
```

## invalid: name 与目录 basename 不一致

```markdown
---
name: some-other-name              # ≠ 目录名（marketplace.md §4 校正期望测试）
description: "Frontmatter name differs from directory basename."
---

# Placeholder
```

## 字段约束速查

| 字段 | 类型 | 约束 | 来源 |
|---|---|---|---|
| `name` | string | 1–64 字符；`[a-z0-9-]`；不以 `-` 开头/结尾；无 `--` | tfrobot-marketplace skill v1 §3.1 |
| `description` | string | 1–1024 字符；非空 | 同上 |
| `license` | string | 无字符上限 | 同上 §3.2 |
| `version` | string | 推荐 semver | 同上 |
| `allowed-tools` | array<string> | LLM 工具白名单 | 同上 |
| `compatibility` | string | 推荐含 a2c-smcp 版本范围 | 同上 |
| `metadata` | object | 任意自定义 | 同上 |

mcp 种子额外字段（位于 `Resource._meta` 不是 frontmatter）：

| `_meta` 字段 | 模式 | 含义 |
|---|---|---|
| `source` | 全部 | "mounted" / "archive" / "resources" |
| `mount_dir` | mounted | 绝对路径 |
| `archive_uri` | archive | HTTP(S) |
| `archive_format` | archive | "tar.gz" / "zip" |
| `archive_sha256` | archive | 可选 |
| `version` | 全部 | 可选；语义化版本 |
| `etag` | 全部 | 可选；缓存校验 |
