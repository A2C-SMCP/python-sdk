# Acceptance: `user/missing-description`

**Axis**: US-ERR-02

**期望被测行为**:

1. **协议契约**: tfrobot-marketplace skill v1 §3.1 — `description` 必填
2. **SDK 实现**: `staging.py:_build_user_ref` 检测 frontmatter 缺 description → ERROR + 跳过
3. **可观测信号**:
   - stdout: `STAGED_NAMES=[]`
   - 日志（ERROR）含: `frontmatter missing required 'description'`
   - registry 不含该 SKILL（user 源 + frontmatter 错 → 不入册）

## 自动化脚本

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
SEEDS_ROOT="$(cd "$SEED_DIR/../.." && pwd)"  # seeds/user/<name>/ → seeds/
SEED_NAME="missing-description"
TMPDIR="$(mktemp -d -t "a2c-user-${SEED_NAME}.XXXXXX")"
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

# 1. 派生：拷 _common/invalid-missing-desc/ 到 $HOME/user/invalid-missing-desc/
src=$(awk '/^source:/{print $2}' "$SEED_DIR/_seeds.manifest")
case "$src" in
  _common/*) ;;
  *) fail "unsupported _seeds.manifest source: $src" ;;
esac
mkdir -p "$HOME_DIR/user/invalid-missing-desc"
cp -R "$SEEDS_ROOT/$src"/. "$HOME_DIR/user/invalid-missing-desc/"

# 2. 跑 stage_user_skills
uv run --no-sync python "$SEEDS_ROOT/user/_helpers/run_user_staging.py" \
  --home "$HOME_DIR" \
  > "$LOG" 2>&1

# 3. PASS 判据（正向断言）
grep -q "STAGED_NAMES=\[\]" "$LOG" \
  || fail "expected STAGED_NAMES=[]; got: $(grep STAGED_NAMES= "$LOG" || true)"

grep -q "frontmatter missing required 'description'" "$LOG" \
  || fail "expected ERROR with 'frontmatter missing required description' keyword"

echo "PASS: user seed ${SEED_NAME}"
```
