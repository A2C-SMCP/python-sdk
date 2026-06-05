# 场景：marketplace-ops

## 测试目标

验证 `a2c-computer marketplace` 子命令的完整生命周期：添加、列出、查看详情、刷新、删除。

## 类型

CLI-only（不需要 Server/Computer/Agent 多进程）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用
3. 测试 marketplace Git 仓库已准备（见下方「测试仓库搭建」）

## 测试仓库搭建

> **复用 seed**: 本场景使用 `seeds/marketplace/valid-single-plugin` seed。
> marketplace 名: `uat-seed-mp`，plugin: `foo`，skill: `foo:valid-skill-pkg`。

搭建脚本（在 tmux 中通过 Bash 执行）：

```bash
SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
SEED="$SEEDS_ROOT/marketplace/valid-single-plugin"
TMPDIR=$(mktemp -d) && WORK="$TMPDIR/work" && BARE="$TMPDIR/test-mp.git"
bash "$SEEDS_ROOT/marketplace/_helpers/init_bare_repo.sh" "$SEED" "$WORK" "$BARE"
echo "BARE_URL=file://$BARE"
```

## 环境变量

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$   # 使用 PID 隔离，避免 zsh rm -rf * 确认
mkdir -p $A2C_SKILL_HOME
```

> 使用独立 SKILL_HOME 避免污染用户真实配置。用 PID 后缀避免 zsh glob 确认弹窗。
> 测试完成后整体 `rm -rf /tmp/a2c-uat-skill-home-*` 清理。

## 测试用例

### M-01: marketplace add --trust（添加 marketplace）

- **优先级**: P0
- **步骤**:
  1. 清理环境：`rm -rf /tmp/a2c-uat-skill-home/*`
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add <BARE_URL> --trust --json`
  3. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 输出包含 `name` 字段，值为 `"uat-seed-mp"`
  - JSON 输出包含 `trusted` = `true`
  - JSON 输出包含 `url` = `<BARE_URL>`
  - `/tmp/a2c-uat-skill-home/marketplace/` 下出现 `uat-seed-mp/` 目录
  - `uat-seed-mp/` 目录下有 `.git/`（clone 成功）

### M-02: marketplace list（列出 marketplace）

- **优先级**: P0
- **前置**: M-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace list --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 输出为数组，长度 ≥ 1
  - 第一个元素包含 `name` = `"uat-seed-mp"`
  - 包含 `trusted` = `true` 字段
  - 包含 `autoUpdate` 字段（布尔值）
  - 包含 `url` = `<BARE_URL>` 字段

### M-03: marketplace info（查看详情）

- **优先级**: P0
- **前置**: M-01 成功
- **步骤**:
  1. 从 M-01 输出获取 marketplace 名称（预期 `uat-seed-mp`）
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace info uat-seed-mp --json`
  3. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 输出包含以下字段且值非空：
    - `name` = `"uat-seed-mp"`
    - `url` = `<BARE_URL>`
    - `installLocation`（字符串，指向本地 clone 路径）
    - `commitSha`（40 字符 hex 字符串）
    - `autoUpdate`（布尔值）
    - `trusted` = `true`
    - `lastUpdated`（ISO 8601 时间戳）
  - `installedPlugins` 为数组（初始可为空 `[]` 或含已安装 plugin）

### M-04: marketplace refresh（刷新 marketplace）

- **优先级**: P0
- **前置**: M-01 成功
- **步骤**:
  1. 记录 M-03 中的 `commitSha`（记为 `SHA_BEFORE`）
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace refresh uat-seed-mp --json`
  3. 捕获输出
  4. 再次执行 `marketplace info uat-seed-mp --json` 获取 `commitSha`（记为 `SHA_AFTER`）
- **预期结果**:
  - refresh 退出码 0
  - JSON 输出包含刷新结果（`name` = `"uat-seed-mp"`）
  - `SHA_AFTER` = `SHA_BEFORE`（bare repo 无新提交，SHA 不变；证明 git pull 不报错）

### M-05: marketplace set auto-update（设置自动更新）

- **优先级**: P1
- **前置**: M-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace set uat-seed-mp auto-update=true --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - 设置持久化（再次 `info` 可见 auto-update=true）

### M-06: marketplace add（不带 --trust 应失败）

- **优先级**: P1
- **步骤**:
  1. 清理环境：`rm -rf /tmp/a2c-uat-skill-home/*`
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add <BARE_URL> --json`
  3. 捕获输出（含 stderr）
- **预期结果**:
  - 退出码 1（非交互模式下必须 --trust）
  - stderr 或 stdout 包含 `"trust"` 关键词
  - `/tmp/a2c-uat-skill-home/marketplace/` 下**不**出现新目录（未添加）

### M-07: marketplace remove（删除 marketplace）

- **优先级**: P0
- **前置**: M-01 成功
- **步骤**:
  1. 记录 clone 路径（从 M-03 info 的 `installLocation` 获取）
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace remove uat-seed-mp --json`
  3. 捕获输出
  4. 验证：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace list --json`
  5. 捕获输出
  6. 验证 clone 路径已不存在：`test ! -d <installLocation>`
- **预期结果**:
  - remove 退出码 0
  - `marketplace list` 返回空数组 `[]`
  - clone 目录已被清理（`<installLocation>` 不存在）

### M-08: marketplace add 重复添加

- **优先级**: P1
- **前置**: M-01 成功
- **步骤**:
  1. 再次执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add <BARE_URL> --trust --json`
  2. 捕获输出（含 stderr）
- **预期结果**:
  - 退出码 1
  - stderr 或 stdout 包含 `"already exists"` 或 `"already registered"` 或 `"duplicate"` 关键词
  - 原有 marketplace 数据不受影响（`marketplace list` 仍返回 1 条）

## 清理

```bash
rm -rf /tmp/a2c-uat-skill-home
```

## 日志收集

CLI-only 场景下日志即 tmux pane 输出。每个用例执行后必须 `capture-pane` 保存完整输出。
