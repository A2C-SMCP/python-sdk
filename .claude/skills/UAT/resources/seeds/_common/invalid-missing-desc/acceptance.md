# Acceptance: `_common/invalid-missing-desc`

**Axis**: CM-03

**校验项**（静态）:

- [ ] `SKILL.md` 存在
- [ ] frontmatter 可解析
- [ ] frontmatter `name` = "invalid-missing-desc"
- [ ] frontmatter **不**含 `description` 键（违规点正确）
- [ ] 不含其他违规字段（保证违规点唯一）

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
parts = text.split("---\n", 2)
fm = yaml.safe_load(parts[1]) or {}

assert fm.get("name") == "invalid-missing-desc", f"unexpected name: {fm.get('name')!r}"
# 关键违规点：description 不存在
assert "description" not in fm, f"description should be absent (axis CM-03), got: {fm.get('description')!r}"

print(f"PASS: _common/{d.name}")
PY
```
