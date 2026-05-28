#!/usr/bin/env bash
# Acceptance for seeds/marketplace/strict-false-conflict/
# Axis: MK-STRICT-FALSE-CONFLICT — strict=false + plugin.json declares components → conflict
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
  --name "strict-false-conflict" \
  --bare "$BARE" \
  --home "$HOME_DIR" \
  >> "$LOG" 2>&1 || true  # staging may fail, that's expected

# Expect conflict error
grep -q "conflicting manifests" "$LOG" \
  || fail "expected 'conflicting manifests' error"

grep -q "strict=false" "$LOG" \
  || fail "expected 'strict=false' in error"

# Expect 0 staged skills
grep -q "STAGED_NAMES=\[\]" "$LOG" \
  || fail "expected STAGED_NAMES=[], got: $(grep STAGED_NAMES= "$LOG" || true)"

echo "PASS: marketplace seed ${SEED_NAME}"
