#!/usr/bin/env bash
# Acceptance for seeds/marketplace/strict-true-merge/
# Axis: MK-STRICT-TRUE — strict=true merges entry.skills + plugin.json.skills
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

bash "$SEEDS_ROOT/_helpers/init_bare_repo.sh" "$SEED_DIR" "$WORK" "$BARE" > "$LOG" 2>&1 \
  || fail "init_bare_repo failed"

uv run --no-sync python "$SEEDS_ROOT/_helpers/run_marketplace_stage.py" \
  --name "strict-true-merge" \
  --bare "$BARE" \
  --home "$HOME_DIR" \
  >> "$LOG" 2>&1

# Expect 3 skills: audit:greet, audit:review, audit:scan
for skill in "audit:greet" "audit:review" "audit:scan"; do
  grep -q "$skill" "$LOG" || fail "expected skill '$skill' in STAGED_NAMES"
done

echo "PASS: marketplace seed ${SEED_NAME}"
