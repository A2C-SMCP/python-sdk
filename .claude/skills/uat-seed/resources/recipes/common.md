# Recipe: `_common/` — SKILL 包原料

> `_common/` 是 SKILL 包的**单一定义源**。其他三源在 setup 阶段从这里派生。

## 何时创建一个新 `_common/<name>/`

- mcp / marketplace / user 中某条新种子需要的 SKILL 包形态在 `_common/` 缺
- 已有形态不能复用（差异在 SKILL.md frontmatter / 文件结构 / scripts 内容）

判定原则：**只要 SKILL.md 内容或文件结构有差异**，就开新 `_common`；不要为节省目录
而过度复用导致语义不清。

## 目录结构模板

```
seeds/_common/<name>/
├── SKILL.md                ← 必需
├── scripts/run.py          ← 可选（推荐 happy 形态都有，便于覆盖 scripts/ 路径）
├── references/usage.md     ← 可选
├── assets/icon.svg         ← 可选
├── README.md               ← 必需
└── acceptance.md           ← 必需（静态校验）
```

## SKILL.md frontmatter 规范

参考 `resources/templates/skill-md.md`。三类常见形态：

### happy 形态（well-formed）

```yaml
---
name: valid-skill-pkg              # 与目录名严格一致
description: "Well-formed minimal SKILL for happy-path seed derivation. Acts on demand."
license: MIT
version: 1.0.0
allowed-tools: ["Read"]
compatibility: "a2c-smcp>=0.2.1"
metadata:
  axis: CM-01
---
```

### invalid 形态

每种 invalid 在 frontmatter 一个**确定的字段**违规，**不要**多重违规（会让 acceptance
难以判定到底命中哪条）：

```yaml
---
# invalid-missing-desc/SKILL.md
name: invalid-missing-desc
# description 故意省略
---
```

```yaml
---
# invalid-bad-name/SKILL.md
name: InvalidCamelCase           # 期望 SkillNameError
description: "Has invalid camelCase name to trigger name validation."
---
```

```yaml
---
# invalid-name-mismatch/SKILL.md
name: some-other-name             # ≠ 目录名 invalid-name-mismatch
description: "name in frontmatter differs from directory basename."
---
```

## README.md 模板

```markdown
# `_common/<name>`

**Axis**: CM-XX （对应 failure-axes.md）

**形态**: <happy | invalid-missing-desc | invalid-bad-name | invalid-name-mismatch | deep-nested | multi-file>

**期望被派生使用方式**:
- mcp: <如何被打包/挂载> (e.g. `_archives/build.sh` 把本目录打成 `valid-1.0.0.tar.gz`)
- marketplace: <如何被拷进 plugin/skills/>
- user: <如何被拷进 home-user / workdir>

**SKILL.md 关键字段**:
- name: ...
- description: ...
- 其他: ...

**已派生引用**:
- seeds/mcp/server_archive_ok.py
- seeds/marketplace/valid-single-plugin/plugins/foo/skills/foo-skill/  ← `cp -r` from here
- seeds/user/home-user-basic/                                            ← `cp -r` from here
```

## acceptance.md 模板（纯静态校验）

```markdown
# Acceptance: `_common/<name>`

**Axis**: CM-XX

**校验项**:
- [ ] `SKILL.md` 存在
- [ ] YAML frontmatter 可解析
- [ ] `name` 字段 = "<expected>"
- [ ] `description` 字段 <存在 | 不存在>（按 invalid 维度定）
- [ ] 目录结构包含: <SKILL.md, scripts/, ...>
- [ ] (invalid 才有) `parse_skill_frontmatter` 后 `_finalize_and_register` 会跳过

## 自动化脚本

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
python - "$SEED_DIR" <<'PY'
import sys, yaml
from pathlib import Path
d = Path(sys.argv[1])

skill_md = d / "SKILL.md"
assert skill_md.is_file(), f"SKILL.md missing in {d}"

text = skill_md.read_text(encoding="utf-8")
assert text.startswith("---\n"), "frontmatter fence missing"

parts = text.split("---\n", 2)
assert len(parts) >= 3, "frontmatter not closed"
fm = yaml.safe_load(parts[1]) or {}

# 按本 _common 期望分支编辑下面 assertions
# happy:
# assert fm.get("name") == "<expected>"
# assert isinstance(fm.get("description"), str) and fm["description"]
# invalid-missing-desc:
# assert "description" not in fm
# ...

print(f"PASS: _common/{d.name}")
PY
```
```

## 命名一览

| name | axis | 形态简介 |
|---|---|---|
| `valid-skill-pkg` | CM-01 | well-formed 最小可用 |
| `multi-file-skill-pkg` | CM-02 | 多文件多目录（cursor / 计数测试用） |
| `invalid-missing-desc` | CM-03 | frontmatter 缺 description |
| `invalid-bad-name` | CM-04 | name 非 kebab-case |
| `invalid-name-mismatch` | CM-05 | name ≠ 目录 basename |
| `deep-nested` | CM-06 | `<root>/a/b/SKILL.md`（user 源忽略 fixture） |

## 创建检查清单

- [ ] 目录名与 SKILL.md frontmatter `name` 关系明确（要么一致，要么是 invalid-name-mismatch）
- [ ] frontmatter 违规**只有一处**（如果是 invalid 形态）
- [ ] README.md 写明 "已派生引用" 列表（即使暂空，留 placeholder）
- [ ] acceptance.md 自动化脚本能跑通（happy 期望成功解析 / invalid 期望命中违规分支）
- [ ] `seeds/README.md` 索引登记一行

## 演进规则

- 改 `_common/<name>/` 后**必须**重跑所有派生它的种子 acceptance（README 的"已派生
  引用"列表是检索清单）
- 不允许 `_common/<name>/` 内的 SKILL.md 出现"既有 description 又没有 description"
  这种自相矛盾——一个目录一种语义
