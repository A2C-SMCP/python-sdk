# Acceptance: `user/home-user-basic`

**Axis**: US-VAL-01

**期望被测行为**:

1. **协议契约**: tfrobot-marketplace skill v1 §2 / §3；user 源 §5.0（DropIn 就地发现）
2. **SDK 实现**: `staging.py:stage_user_skills` 扫 `<home>/user/<skill>/SKILL.md`
3. **可观测信号**:
   - stdout: `STAGED_NAMES=['valid-skill-pkg']`
   - registered ref: `source` 字段 = `"user"`，`path` 指向 `$HOME_DIR/user/valid-skill-pkg`
   - 文件状态: SKILL **不被拷走**，依然在 `$HOME_DIR/user/valid-skill-pkg/` 就地

## 自动化脚本

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
SEEDS_ROOT="$(cd "$SEED_DIR/../.." && pwd)"  # seeds/user/<name>/ → seeds/
SEED_NAME="home-user-basic"
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

# 1. 把 _common/valid-skill-pkg 拷进 $HOME_DIR/user/valid-skill-pkg/
# (basename = "valid-skill-pkg" 与 frontmatter name 一致)
src=$(awk '/^source:/{print $2}' "$SEED_DIR/_seeds.manifest")
case "$src" in
  _common/*) ;;
  *) fail "unsupported _seeds.manifest source: $src" ;;
esac
mkdir -p "$HOME_DIR/user/valid-skill-pkg"
cp -R "$SEEDS_ROOT/$src"/. "$HOME_DIR/user/valid-skill-pkg/"

# 2. 跑 stage_user_skills
uv run --no-sync python "$SEEDS_ROOT/user/_helpers/run_user_staging.py" \
  --home "$HOME_DIR" \
  > "$LOG" 2>&1

# 3. PASS 判据
grep -q "STAGED_NAMES=\['valid-skill-pkg'\]" "$LOG" \
  || fail "expected STAGED_NAMES=['valid-skill-pkg']; got: $(grep STAGED_NAMES= "$LOG" || true)"

grep -q "source': 'user'" "$LOG" || fail "ref source should be 'user'"

# SKILL 仍在原地（user 源不复制）
[[ -f "$HOME_DIR/user/valid-skill-pkg/SKILL.md" ]] \
  || fail "SKILL.md should remain in place at \$HOME/user/valid-skill-pkg/"

echo "PASS: user seed ${SEED_NAME}"
```
