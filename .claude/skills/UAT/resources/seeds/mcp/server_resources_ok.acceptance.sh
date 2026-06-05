#!/usr/bin/env bash
# Acceptance for seeds/mcp/server_resources_ok.py
# Axis: MC-RES happy
# Expected: stage_mcp_skills returns [valid-skill-pkg name], staging dir populated, registry has ref.
set -Eeuo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
SEEDS_ROOT="$SEED_DIR/.."
SEED_NAME="server_resources_ok"
TMPDIR="$(mktemp -d -t "a2c-seed-${SEED_NAME}.XXXXXX")"
LOG="$TMPDIR/run.log"
HOME_DIR="$TMPDIR/skill-home"

cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT INT TERM

fail() {
  echo "FAIL: $*" >&2
  echo "---- last 60 log lines ----" >&2
  tail -60 "$LOG" >&2 || true
  exit 1
}

# 1. 跑 staging
uv run --no-sync python "$SEED_DIR/_helpers/run_staging.py" \
  --stdio-server "$SEED_DIR/${SEED_NAME}.py" \
  --home "$HOME_DIR" \
  > "$LOG" 2>&1

# 2. PASS 判据：
#    - stdout 含 STAGED_NAMES=[...]，且非空列表
#    - skill home 下出现 mcp/<server>/<skill>/SKILL.md
grep -q "STAGED_NAMES=\[" "$LOG" || fail "STAGED_NAMES line missing"
grep -q "STAGED_NAMES=\[\]" "$LOG" && fail "STAGED_NAMES is empty"

# 期望物化目录形如: $HOME_DIR/mcp/seed/valid-skill-pkg/SKILL.md
SKILL_MD=$(find "$HOME_DIR/mcp" -mindepth 3 -maxdepth 3 -name SKILL.md | head -1)
[[ -n "$SKILL_MD" ]] || fail "no staged SKILL.md found under $HOME_DIR/mcp"

# 内容应来自 _common/valid-skill-pkg/SKILL.md（含 description）
grep -q "valid-skill-pkg" "$SKILL_MD" || fail "staged SKILL.md missing expected name token"

echo "PASS: seed ${SEED_NAME}"
