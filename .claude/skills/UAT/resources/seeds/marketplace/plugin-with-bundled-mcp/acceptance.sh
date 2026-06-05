#!/usr/bin/env bash
# Acceptance for seeds/marketplace/plugin-with-bundled-mcp/
# Axis: MK-BMC-01 — plugin with bundled MCP server registers skill + server.
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

# 1. Build worktree + bare repo
bash "$SEEDS_ROOT/_helpers/init_bare_repo.sh" "$SEED_DIR" "$WORK" "$BARE" > "$LOG" 2>&1 \
  || fail "init_bare_repo failed"

# 2. Verify bundled MCP server config exists in worktree
[[ -f "$WORK/plugins/foo/mcp-servers/figma-mcp.json" ]] \
  || fail "bundled MCP server config not found in worktree"

# 3. Drive stage_marketplace_skills
uv run --no-sync python "$SEEDS_ROOT/_helpers/run_marketplace_stage.py" \
  --name "mp-bundled-mcp" \
  --bare "$BARE" \
  --home "$HOME_DIR" \
  >> "$LOG" 2>&1

# 4. PASS 判据
grep -q "STAGED_NAMES=\['foo:valid-skill-pkg'\]" "$LOG" \
  || fail "expected STAGED_NAMES=['foo:valid-skill-pkg'], got: $(grep STAGED_NAMES= "$LOG" || true)"

echo "PASS: marketplace seed ${SEED_NAME}"
