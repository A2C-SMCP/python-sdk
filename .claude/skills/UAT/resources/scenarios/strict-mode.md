# 场景：strict-mode

## 测试目标

验证 marketplace strict 模式：strict=true 时 entry.skills + plugin.json.skills 追加合并；
strict=false 时 marketplace entry 是唯一组件权威（plugin.json 不得声明组件字段）；
strict=false + plugin.json 声明组件字段时冲突降级（plugin 跳过，marketplace 仍添加）。

## 类型

CLI-only（strict 模式在 `marketplace add` 时解析，非运行时）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用

## strict 模式核心语义

> 来源：`docs/design-0.2.1-cli-marketplace-ux.md` §3.4 / §3.5；`a2c_smcp/computer/skills/manifest.py`

| 场景 | 行为 |
|---|---|
| 只有 `<plugin>/skills/` 约定目录 | 自动发现（始终扫描，不受 strict 影响） |
| marketplace entry 写了 `skills` + `plugin.json` 存在 + `strict=true`（默认） | entry.skills + plugin.json.skills 追加合并 |
| marketplace entry 写了 `skills` + `plugin.json` 存在 + `strict=false` | **冲突降级**：plugin 跳过，marketplace 仍添加 |
| marketplace entry 写了 `skills` + 无 `plugin.json` | entry 就是 manifest，entry.skills 是唯一来源 |
| marketplace entry 写了 `skills` + `plugin.json` **不声明组件** + `strict=false` | 仅取 entry.skills（plugin.json 无组件字段，不冲突） |

> **关键路径**: plugin.json 必须位于 `<plugin>/.tfrobot-plugin/plugin.json`，不是 `<plugin>/plugin.json`。
> **冲突检测字段**: `commands`, `agents`, `hooks`, `skills`, `mcpServers`, `lspServers`（全六字段，前向兼容）。

## 测试仓库搭建

> **复用 seed**: 本场景使用 3 个 marketplace seed：
> - S-01: `seeds/marketplace/strict-true-merge`（marketplace 名 `strict-true-merge`）
> - S-02: `seeds/marketplace/strict-false-clean`（marketplace 名 `strict-false-clean`）
> - S-03: `seeds/marketplace/strict-false-conflict`（marketplace 名 `strict-false-conflict`）

搭建脚本（在 tmux 中通过 Bash 执行）：

```bash
SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
TMPDIR=$(mktemp -d) && echo "TMPDIR=$TMPDIR"

# ── 仓库 A: strict=true 追加合并 ──
bash "$SEEDS_ROOT/marketplace/_helpers/init_bare_repo.sh" \
  "$SEEDS_ROOT/marketplace/strict-true-merge" "$TMPDIR/work-a" "$TMPDIR/bare-a.git"
echo "BARE_A=file://$TMPDIR/bare-a.git"

# ── 仓库 B: strict=false + plugin.json 无组件 ──
bash "$SEEDS_ROOT/marketplace/_helpers/init_bare_repo.sh" \
  "$SEEDS_ROOT/marketplace/strict-false-clean" "$TMPDIR/work-b" "$TMPDIR/bare-b.git"
echo "BARE_B=file://$TMPDIR/bare-b.git"

# ── 仓库 C: strict=false + plugin.json 声明组件（冲突降级） ──
bash "$SEEDS_ROOT/marketplace/_helpers/init_bare_repo.sh" \
  "$SEEDS_ROOT/marketplace/strict-false-conflict" "$TMPDIR/work-c" "$TMPDIR/bare-c.git"
echo "BARE_C=file://$TMPDIR/bare-c.git"
```

## 环境变量

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$
mkdir -p $A2C_SKILL_HOME
```

> 使用 `rm -rf $A2C_SKILL_HOME && mkdir -p $A2C_SKILL_HOME` 清理（避免 zsh glob 确认弹窗）。

## 测试用例

### S-01: strict=true 追加合并（entry.skills + plugin.json.skills）

- **优先级**: P0
- **步骤**:
  1. 清理环境：`rm -rf $A2C_SKILL_HOME && mkdir -p $A2C_SKILL_HOME`
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add file://<BARE_A> --trust --json`
  3. 捕获输出，记录 marketplace 名称和 skills 数量
  4. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer skill list --source mp --json`
  5. 捕获输出
- **预期结果**:
  - add 退出码 0，skills 数量为 3
  - skill list 显示 3 个 skill：
    - audit:greet（来自 plugin 默认 `skills/` 目录）
    - audit:review（来自 entry.skills 指定的 `extra-skills/`）
    - audit:scan（来自 plugin.json.skills 指定的 `more-skills/`）
  - 所有 skill 的 source 以 "marketplace" 开头

### S-02: strict=false + plugin.json 无组件 → 仅取 entry.skills

- **优先级**: P0
- **步骤**:
  1. 清理环境：`rm -rf $A2C_SKILL_HOME && mkdir -p $A2C_SKILL_HOME`
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add file://<BARE_B> --trust --json`
  3. 捕获输出
  4. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer skill list --source mp --json`
  5. 捕获输出
- **预期结果**:
  - add 退出码 0
  - skill list 显示 2 个 skill：
    - audit:greet（来自 plugin 默认 `skills/` 目录，始终扫描）
    - audit:review（来自 entry.skills 指定的 `extra-skills/`）
  - 无 scan skill（plugin.json 为 `{}`，不提供额外 skill 目录）

### S-03: strict=false + plugin.json 组件冲突降级

- **优先级**: P0
- **步骤**:
  1. 清理环境：`rm -rf $A2C_SKILL_HOME && mkdir -p $A2C_SKILL_HOME`
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add file://<BARE_C> --trust --json`
  3. 捕获输出（含 stderr）
- **预期结果**:
  - 退出码 0（降级处理，非硬错误）
  - stderr 包含 "conflicting manifests" 和 "strict=false" 和 "plugin.json declares components"
  - skills 数量为 0（冲突 plugin 被跳过，不入册）
  - marketplace 已添加（`marketplace list` 可见），但 `installedPlugins` 为空

## 清理

```bash
rm -rf /tmp/a2c-uat-skill-home
rm -rf $BASE
```

## 日志收集

CLI-only 场景下日志即 tmux pane 输出。每个用例执行后必须 `capture-pane` 保存完整输出。
