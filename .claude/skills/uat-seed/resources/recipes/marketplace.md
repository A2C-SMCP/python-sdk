# Recipe: `marketplace/` — Git 仓库种子

> 每条种子是一份**完整可 clone 的 marketplace 仓库工作树**（含 `.tfrobot-plugin/`），
> acceptance 期间临时 `git init` + `git push` 到本地 bare 库，再以 `file://` URL 喂给
> `a2c-computer marketplace add`。

## 目录形态

```
seeds/marketplace/
├── _helpers/
│   ├── init_bare_repo.sh         ← 把工作树变成临时 bare repo + 输出 file:// URL
│   └── run_marketplace_add.py    ← 直接驱动 marketplace stage 的最小 harness
├── <name>/                        ← 一份完整工作树
│   ├── .tfrobot-plugin/
│   │   └── marketplace.json
│   ├── plugins/
│   │   └── <plugin>/
│   │       ├── .tfrobot-plugin/plugin.json
│   │       ├── skills/<skill>/SKILL.md          ← cp -r from _common/<x>
│   │       └── mcp-servers/<mcp>.json
│   ├── README.md
│   └── acceptance.sh
```

## marketplace.json 模板

```json
{
  "schema": "tfrobot-marketplace/v1",
  "name": "uat-seed-<name>",
  "metadata": {
    "pluginRoot": "./plugins"
  },
  "plugins": [
    {
      "name": "<plugin>",
      "source": { "type": "localPath", "path": "./plugins/<plugin>" },
      "strict": true,
      "skills": []
    }
  ]
}
```

每条种子按其 axis 调整：

- `strict-true-clean`: `strict: true`，plugin.json 不声明组件 → 无冲突
- `strict-false-conflict`: `strict: false`，plugin.json 声明 `skills` 组件 → 冲突
- `entry-skills-override`: `plugins[].skills` 指向**非默认**路径（如 `./alt-skills/`）
- `plugin-source-git-subdir`: `source = { type: "gitSubdir", url, subdir }`
- ...

## plugin.json 模板

```json
{
  "schema": "tfrobot-plugin/v1",
  "name": "<plugin>",
  "version": "1.0.0",
  "description": "Seed plugin for UAT.",
  "skills": [
    "./skills/<skill>"
  ]
}
```

按 axis 调整：strict 冲突种子里 `skills` 显式列出条目；happy 种子里也可以省略 `skills`
让 Computer 走默认路径扫描。

## SKILL 的派生（关键：不复制原料）

**`plugins/<plugin>/skills/<skill>/`** 内容**必须**通过 acceptance 准备时从
`seeds/_common/<x>/` 拷贝过来——种子目录里**留**一个 `_seeds.manifest` 描述派生关系：

```
plugins/<plugin>/skills/<skill>/_seeds.manifest
---
source: _common/valid-skill-pkg
```

为减少 git 体积、保证单一定义源，**推荐**两种做法二选一：

1. **目录内只放 `_seeds.manifest`**：acceptance 启动时按 manifest 拷贝 `_common/<x>` 进
   `plugins/<plugin>/skills/<skill>/`，然后 `git init` 提交 + push bare
2. **目录内已 `cp -r` 好 `_common/<x>` 内容**（不留 manifest）：直接 `git init` 提交 +
   push bare

第 1 种"单一定义源"严格，但 audit 启动稍复杂；第 2 种简单但 `_common` 改了不会自动同步
→ 必须配合 README "已派生引用"列表 + audit 比对脚本。

**默认推荐第 1 种**（manifest 模式）。

## 标准 acceptance.sh

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
SEED_DIR="$(cd "$(dirname "$0")" && pwd)"        # seeds/marketplace/<name>/
SEED_NAME="$(basename "$SEED_DIR")"
SEEDS_ROOT="$(cd "$SEED_DIR/.." && pwd)"
TMPDIR="$(mktemp -d -t "a2c-mp-${SEED_NAME}.XXXXXX")"
WORKTREE="$TMPDIR/wt"
BARE="$TMPDIR/${SEED_NAME}.git"
LOG="$TMPDIR/run.log"

cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT INT TERM

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. 把工作树拷贝到 tmp，按 manifest 装配 _common 原料
cp -r "$SEED_DIR" "$WORKTREE"
find "$WORKTREE" -name "_seeds.manifest" | while read -r m; do
  src=$(awk '/^source:/{print $2}' "$m")
  dst=$(dirname "$m")
  rm -f "$m"
  cp -r "$SEEDS_ROOT/../_common/$(basename "$src")"/. "$dst/"
done

# 2. git init → bare push（_helpers/init_bare_repo.sh 封装下面这一坨）
bash "$SEEDS_ROOT/_helpers/init_bare_repo.sh" "$WORKTREE" "$BARE"
URL="file://$BARE"

# 3. 临时 SKILL_HOME + a2c-computer marketplace add
export A2C_SKILL_HOME="$TMPDIR/skill-home"
mkdir -p "$A2C_SKILL_HOME"

# 走 CLI（happy 种子）
if a2c-computer marketplace add "$URL" --trust > "$LOG" 2>&1; then
  # 4a. happy 期望：注册成功、SKILL Home 下出现物化产物
  grep -q "registered marketplace" "$LOG" || fail "expected registration log; got: $(tail -20 "$LOG")"
  [[ -d "$A2C_SKILL_HOME/marketplace/$SEED_NAME" ]] || fail "marketplace home dir not found"
else
  # 4b. failure 种子：检查非零退出 + 期望错误关键字（按 axis 写）
  grep -q "<expected error keyword for axis>" "$LOG" || fail "expected axis-specific error keyword"
fi

echo "PASS: marketplace seed ${SEED_NAME}"
```

`_helpers/init_bare_repo.sh`：

```bash
#!/usr/bin/env bash
# usage: init_bare_repo.sh <worktree> <bare-out>
set -Eeuo pipefail
WT="$1"; BARE="$2"
git init --bare "$BARE" >/dev/null
git -C "$WT" -c init.defaultBranch=main init >/dev/null 2>&1 || true
git -C "$WT" add -A
git -C "$WT" -c user.email=seed@uat -c user.name=seed commit -m "uat seed snapshot" >/dev/null 2>&1 || true
git -C "$WT" branch -M main 2>/dev/null || true
git -C "$WT" push "$BARE" HEAD:refs/heads/main >/dev/null 2>&1
echo "$BARE"
```

## 命名一览

| name | axis | 形态简介 |
|---|---|---|
| `valid-single-plugin` | MK-VAL-01 | 1 plugin 1 skill happy |
| `valid-multi-plugin` | MK-VAL-02 | 多 plugin happy |
| `strict-true-clean` | MK-STR-01 | strict=true 无冲突 |
| `strict-false-conflict` | MK-STR-02 | strict=false + 声明组件 → 硬错 |
| `entry-skills-override` | MK-OVR-01 | entry.skills 覆写 |
| `plugin-source-localpath` | MK-SRC-01 | localPath |
| `plugin-source-git-subdir` | MK-SRC-02 | git-subdir sparse clone |
| `plugin-source-url` | MK-SRC-03 | url 独立 clone |
| `plugin-source-github` | MK-SRC-04 | github 独立 clone |
| `plugin-source-cnb` | MK-SRC-05 | cnb 独立 clone |
| `missing-marketplace-json` | MK-ERR-01 | 根 marketplace.json 缺失 |
| `malformed-plugin-json` | MK-ERR-02 | plugin.json JSON 错 |
| `unknown-marketplace` | MK-ERR-03 | 未登记触发 trust 决策 |
| `disabled-plugins` | MK-FLT-01 | enabledPlugins 过滤 |

## 创建检查清单

- [ ] 工作树根有 `.tfrobot-plugin/marketplace.json`
- [ ] 每个 plugin 有 `.tfrobot-plugin/plugin.json`
- [ ] SKILL 内容通过 `_seeds.manifest` 引用 `_common/<x>`（或显式 cp 后在 README 标注）
- [ ] failure 种子的违规点**只有一处**（与 axis 对齐）
- [ ] acceptance.sh 自包含：临时 SKILL_HOME / 临时 bare repo / 完整清理
- [ ] happy 种子 acceptance 检查注册成功 + SKILL Home 产物
- [ ] failure 种子 acceptance 检查非零退出 + axis-specific 错误关键字
- [ ] `seeds/README.md` 索引登记

## 演进规则

- 改 `_common/<x>` 后**必须**重跑所有引用它的 marketplace 种子 acceptance
- 新增 plugin source 类型（如未来加 `gitlab`）→ 先在 `failure-axes.md` 加 MK-SRC-NN，
  再创建对应种子
- `strict` 模式细分维度（如 #80 双路径）：staging soft-degrade vs installer hard-fail
  应**分两条种子**而不是一条
