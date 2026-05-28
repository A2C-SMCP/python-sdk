#!/usr/bin/env bash
# Acceptance for seeds/mcp/server_resources_no_subs.py
# Axis: MC-RES-01 — resources-mode SKILL has no sub-resources
# Expected: stage_mcp_skills returns [] for this server,
#           Computer log contains "resources-mode SKILL has no sub-resources",
#           no SKILL materialized under home/mcp/.
set -Eeuo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
SEED_NAME="server_resources_no_subs"
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

# 1. 跑 staging（期望 0 退出 — staging 健壮容错；失败 SKILL 跳过、不阻断）
uv run --no-sync python "$SEED_DIR/_helpers/run_staging.py" \
  --stdio-server "$SEED_DIR/${SEED_NAME}.py" \
  --home "$HOME_DIR" \
  > "$LOG" 2>&1

# 2. PASS 判据（正向断言）：
#    - 日志含期望错误关键字 "resources-mode SKILL has no sub-resources"
#    - STAGED_NAMES=[] （没有 SKILL 注册成功）
#    - $HOME_DIR/mcp 下没有 SKILL.md（没有 staging 残留）
grep -q "resources-mode SKILL has no sub-resources" "$LOG" \
  || fail "expected 'resources-mode SKILL has no sub-resources' in log"

grep -q "STAGED_NAMES=\[\]" "$LOG" \
  || fail "expected STAGED_NAMES=[], got: $(grep STAGED_NAMES= "$LOG" || true)"

if [[ -d "$HOME_DIR/mcp" ]]; then
  found=$(find "$HOME_DIR/mcp" -name SKILL.md | head -1 || true)
  [[ -z "$found" ]] || fail "no SKILL.md should be staged, but found: $found"
fi

echo "PASS: seed ${SEED_NAME}"
