# Acceptance: `_common/valid-skill-pkg`

**Axis**: CM-01

**校验项**:

- [ ] `SKILL.md` 存在
- [ ] YAML frontmatter 可解析
- [ ] `name` = "valid-skill-pkg"
- [ ] `description` 非空且为字符串
- [ ] 目录结构包含 `SKILL.md` / `scripts/run.py` / `references/usage.md`
- [ ] `parse_skill_frontmatter` + `_finalize_and_register` 不会因 frontmatter 跳过

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

assert fm.get("name") == "valid-skill-pkg", f"unexpected name: {fm.get('name')!r}"
desc = fm.get("description")
assert isinstance(desc, str) and desc, f"description must be non-empty string, got: {desc!r}"

assert (d / "scripts" / "run.py").is_file(), "scripts/run.py missing"
assert (d / "references" / "usage.md").is_file(), "references/usage.md missing"

print(f"PASS: _common/{d.name}")
PY
```
