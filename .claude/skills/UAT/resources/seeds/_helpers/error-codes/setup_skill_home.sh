#!/usr/bin/env bash
# Set up SKILL_HOME for error-codes UAT scenario.
# Creates:
#   1. user/env-skill/  — a minimal SKILL with .skillenv file (E-06 needs it)
#   2. user/valid-skill-pkg/  — standard happy-path SKILL from _common (E-04/E-05/E-07 reference)
set -Eeuo pipefail

SKILL_HOME="${1:?Usage: setup_skill_home.sh <SKILL_HOME>}"
SEEDS_ROOT="${2:?Usage: setup_skill_home.sh <SKILL_HOME> <SEEDS_ROOT>}"

mkdir -p "$SKILL_HOME/user/env-skill"
mkdir -p "$SKILL_HOME/user/valid-skill-pkg"

# ── 1. env-skill: minimal SKILL + .skillenv ──
cat > "$SKILL_HOME/user/env-skill/SKILL.md" << 'EOF'
---
name: env-skill
description: "SKILL with .skillenv for error-codes UAT E-06 forbidden test"
---
# env-skill
This SKILL has a .skillenv file for testing 4017 forbidden reason.
EOF

# .skillenv — 内容不重要，只要存在就会被 sandbox 拒绝
cat > "$SKILL_HOME/user/env-skill/.skillenv" << 'EOF'
# Credential file — must be forbidden by sandbox
API_KEY=secret-value-for-testing
EOF

mkdir -p "$SKILL_HOME/user/env-skill/scripts"
cat > "$SKILL_HOME/user/env-skill/scripts/run.py" << 'EOF'
#!/usr/bin/env python3
"""Placeholder executable for env-skill."""
if __name__ == "__main__":
    print("env-skill placeholder")
EOF

# ── 2. valid-skill-pkg: copy from _common ──
cp -R "$SEEDS_ROOT/_common/valid-skill-pkg"/. "$SKILL_HOME/user/valid-skill-pkg/"

echo "SETUP_COMPLETE: $SKILL_HOME"
echo "  user/env-skill/          (SKILL with .skillenv)"
echo "  user/valid-skill-pkg/    (standard happy-path SKILL)"
