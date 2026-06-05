#!/usr/bin/env bash
# Acceptance for seeds/marketplace/valid-single-plugin/
# Axis: MK-VAL-01 — happy single-plugin marketplace registers expected SKILL.
set -Eeuo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
SEEDS_ROOT="$SEED_DIR/.."
SEED_NAME="$(basename "$SEED_DIR")"
TMPDIR="$(mktemp -d -t "a2c-mp-${SEED_NAME}.XXXXXX")"
WORK="$TMPDIR/work"
BARE="$TMPDIR/${SEED_NAME}.git"
HOME_DIR="$TMPDIR/skill-home"
LOG="$TMPDIR/run.log"

cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT INT TERM

fail() {
  echo "FAIL: $*" >&2
  echo "---- last 60 log lines ----" >&2
  tail -60 "$LOG" >&2 || true
  exit 1
}

# 1. Build worktree (materialize _seeds.manifest) + bare repo
bash "$SEEDS_ROOT/_helpers/init_bare_repo.sh" "$SEED_DIR" "$WORK" "$BARE" > "$LOG" 2>&1 \
  || fail "init_bare_repo failed"

# 2. Drive stage_marketplace_skills
uv run --no-sync python "$SEEDS_ROOT/_helpers/run_marketplace_stage.py" \
  --name "uat-seed-mp" \
  --bare "$BARE" \
  --home "$HOME_DIR" \
  >> "$LOG" 2>&1

# 3. PASS 判据
grep -q "STAGED_NAMES=\['foo:valid-skill-pkg'\]" "$LOG" \
  || fail "expected STAGED_NAMES=['foo:valid-skill-pkg'], log shows: $(grep STAGED_NAMES= "$LOG" || true)"

# 物化包根存在
SKILL_MD="$HOME_DIR/marketplace/uat-seed-mp/plugins/foo/skills/valid-skill-pkg/SKILL.md"
[[ -f "$SKILL_MD" ]] || fail "staged SKILL.md not found at $SKILL_MD"

grep -q "valid-skill-pkg" "$SKILL_MD" || fail "staged SKILL.md missing expected name token"

# Ref source 字段对
grep -q "source': 'marketplace:uat-seed-mp'" "$LOG" \
  || fail "expected ref source 'marketplace:uat-seed-mp' in log"

echo "PASS: marketplace seed ${SEED_NAME}"
