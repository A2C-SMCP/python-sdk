# Recipe: `user/` — 就地 DropIn 种子

> 每条种子是一个**准备好被原样拷进 `$A2C_SKILL_HOME/user/` 或 `<workdir>/.tfrobot/skills/`**
> 的目录。user 源**不复制进 SKILL Home**——它就地发现，所以种子的角色是"现成的可
> 拷贝资产"。

## 目录形态

### 单源种子

```
seeds/user/<name>/
├── SKILL.md                    ← 通常通过 _seeds.manifest 派生 _common/<x>
├── scripts/run.py              ← 可选
├── _seeds.manifest             ← (推荐) 派生关系说明
├── README.md
└── acceptance.md               ← user 源以 acceptance.md 为主（含自动化片段 + 手动确认项）
```

`_seeds.manifest`：

```
source: _common/valid-skill-pkg
target: home-user                # 或 workdir-N（N 从 1 起表示登记序）
```

### 多源对比种子（如 override-low-vs-high）

```
seeds/user/override-low-vs-high/
├── home-user/<skill>/          ← 准备拷进 <home>/user/<skill>/
│   ├── SKILL.md
│   └── _seeds.manifest         (source: _common/valid-skill-pkg)
├── workdir-1/<skill>/          ← 准备拷进 <workdir-1>/.tfrobot/skills/<skill>/
│   ├── SKILL.md                ← 同名但内容不同（如 description 不同）
│   └── _seeds.manifest
├── workdir-2/<skill>/          ← 准备拷进 <workdir-2>/.tfrobot/skills/<skill>/
│   ├── SKILL.md
│   └── _seeds.manifest
├── README.md                   ← 说明三份的差异 + 期望生效优先级
└── acceptance.md
```

## SKILL.md 形态

happy：直接派生 `_common/valid-skill-pkg`，无需修改。

invalid：派生 `_common/invalid-*`（按 axis 对应）。例如：

- `user/invalid-name-camelcase/` → `_seeds.manifest: source: _common/invalid-bad-name`
- `user/missing-description/` → `_seeds.manifest: source: _common/invalid-missing-desc`
- `user/invalid-deep-nested/` → 不派生 `_common/deep-nested`（user 源的深嵌套测试需要
  特殊目录结构，直接写在种子内）

注意 user 源的 SKILL **name = 目录 basename**（设计 §5.0），所以："SKILL.md frontmatter
里的 name 是参考值，目录 basename 才是 ID"——`invalid-name-camelcase/` 目录用 camelCase
**目录名**而不是改 frontmatter name 才能触发 SkillNameError。

## 多源对比（override）SKILL.md 区分

`override-low-vs-high` 三份 SKILL.md 共用 name（= 目录 basename，三处都得叫同一个），
但 frontmatter 其他字段（如 description / version）做区分，便于 acceptance 判
"实际生效的是哪一份"：

```yaml
# home-user/<skill>/SKILL.md
---
name: <skill>
description: "low-priority version from <home>/user"
version: "0.1.0"
---
```

```yaml
# workdir-1/<skill>/SKILL.md
---
name: <skill>
description: "mid-priority version from workdir-1"
version: "0.2.0"
---
```

```yaml
# workdir-2/<skill>/SKILL.md
---
name: <skill>
description: "high-priority version from workdir-2"
version: "0.3.0"
---
```

期望：最终 registry 中该 SKILL 的 `description` = "high-priority ..."，`version` = "0.3.0"。

## acceptance.md 模板

```markdown
# Acceptance: `user/<name>`

**Axis**: US-XX

**期望被测行为**（三件套，参 `guides/acceptance-design.md`）：

1. **协议契约**: <skill.md §X 引用>
2. **SDK 实现**: <staging.py:stage_user_skills / _build_user_ref 等>
3. **可观测信号**:
   - Computer 日志: `<expected substring>`
   - registry 状态: `<expected ref shape>` 或 `<absent>`
   - 文件状态: `<没有任何拷贝 - user 源就地发现>`

## 自动化（单源 happy 示例）

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
SEED_NAME="$(basename "$SEED_DIR")"
SEEDS_ROOT="$(cd "$SEED_DIR/.." && pwd)"
TMPDIR="$(mktemp -d -t "a2c-user-${SEED_NAME}.XXXXXX")"
HOME_DIR="$TMPDIR/skill-home"
LOG="$TMPDIR/run.log"

cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT INT TERM

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. 准备 SKILL_HOME：把种子按 _seeds.manifest 派生到 home/user/<skill>/
mkdir -p "$HOME_DIR/user/<skill>"
src=$(awk '/^source:/{print $2}' "$SEED_DIR/_seeds.manifest")
cp -r "$SEEDS_ROOT/../_common/$(basename "$src")"/. "$HOME_DIR/user/<skill>/"

# 2. 直接驱动 stage_user_skills（_helpers/run_user_staging.py）
export A2C_SKILL_HOME="$HOME_DIR"
python "$SEEDS_ROOT/../mcp/_helpers/run_user_staging.py" --home "$HOME_DIR" > "$LOG" 2>&1

# 3. 判 PASS
# happy: registry 里有该 SKILL 且 source=user
grep -q "registered user SKILL <skill>" "$LOG" || fail "expected registration log"

echo "PASS: user seed ${SEED_NAME}"
```

## 手动确认项（如有）

- [ ] SKILL Home 目录被正确隔离到 mktemp 临时目录
- [ ] 跑完 audit 后 mktemp 已清理（无 `/tmp/a2c-user-*` 残留）
```

## 多源对比 acceptance 关键差异

对 `override-low-vs-high`：

```bash
# 1. 临时 SKILL_HOME
HOME_DIR="$TMPDIR/skill-home"
# 2. 临时 workdir × 2
WD1="$TMPDIR/wd1"; WD2="$TMPDIR/wd2"
mkdir -p "$HOME_DIR/user" "$WD1/.tfrobot/skills" "$WD2/.tfrobot/skills"
# 3. 派生：三处都按 _seeds.manifest 拷
cp -r "$SEED_DIR/home-user"/. "$HOME_DIR/user/"
cp -r "$SEED_DIR/workdir-1"/. "$WD1/.tfrobot/skills/"
cp -r "$SEED_DIR/workdir-2"/. "$WD2/.tfrobot/skills/"
# 4. 触发 staging，传入 workdirs=[$WD1, $WD2]（登记序）
python .../run_user_staging.py --home "$HOME_DIR" --workdirs "$WD1" "$WD2" > "$LOG" 2>&1
# 5. PASS 判据
# - 最终生效 description 含 "high-priority"（来自 workdir-2）
# - 出现 WARN 日志说明覆盖发生
grep -q "high-priority version from workdir-2" "$LOG" || fail "wrong priority winner"
grep -q "WARN.*overridden" "$LOG" || fail "expected override warning"
```

## 命名一览

| name | axis | 形态简介 |
|---|---|---|
| `home-user-basic` | US-VAL-01 | `<home>/user` 下一个 happy SKILL |
| `workdir-basic` | US-VAL-02 | 单个 workdir 下一个 happy SKILL |
| `override-low-vs-high` | US-OVR-01 | 三层（home-user / workdir-1 / workdir-2）同名覆盖 |
| `invalid-name-camelcase` | US-ERR-01 | 目录名用 camelCase（不是改 frontmatter） |
| `missing-description` | US-ERR-02 | 派生 `_common/invalid-missing-desc` |
| `invalid-deep-nested` | US-ERR-03 | `<root>/a/b/SKILL.md`（直接写在种子内） |

## 创建检查清单

- [ ] 单源种子：用 `_seeds.manifest` 引用 `_common/<x>` 而不是内嵌副本
- [ ] 多源种子：每层都有独立子目录 + 各自 `_seeds.manifest`；frontmatter 字段做区分以便判生效者
- [ ] 失败种子：违规点**只有一处**，与 axis 对齐
- [ ] acceptance.md 含自动化 bash 片段 + 手动确认项
- [ ] 自动化片段把派生 / staging 全在 `$TMPDIR` 内完成，**不**污染真实 `$A2C_SKILL_HOME`
- [ ] `seeds/README.md` 索引登记

## 演进规则

- user 源不直接复制原料（与 marketplace 类似）：依赖 `_common/`
- `invalid-deep-nested` 是少数**不派生**的种子之一，因为其结构 `<root>/a/b/SKILL.md` 是
  user 源专属语义；保持直接写
- 新增 workdir 行为差异（如 active workdir 切换不影响发现）应另开种子（如
  `active-workdir-irrelevant`）
