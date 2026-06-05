# Acceptance: `_common/minimal-scan`

**Axis**: CM-06

**校验项**:
- [ ] `SKILL.md` 存在
- [ ] YAML frontmatter 可解析
- [ ] `name` 字段 = "scan"
- [ ] `description` 字段存在且非空

## 自动化脚本

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 - "$SEED_DIR" <<'PY'
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

assert fm.get("name") == "scan", f"expected name='scan', got {fm.get('name')}"
assert isinstance(fm.get("description"), str) and fm["description"], "description missing or empty"

print(f"PASS: _common/{d.name}")
PY
```
