# 场景：skill-discovery

## 测试目标

验证 `a2c-computer skill` 子命令的多源 skill 发现能力：marketplace、user drop-in、MCP 三种来源的
列出和详情查看，以及完整链路下的三级渐进披露（get_skills → get_skill → get_blob）。

## 类型

混合 — D-01~D-04 为 CLI-only，D-05 为完整链路

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用
3. 测试 marketplace Git 仓库已准备（D-01/D-04 需要）

## 测试仓库搭建

> **复用 seed**:
> - D-01/D-04: `seeds/marketplace/valid-single-plugin`（marketplace 名 `uat-seed-mp`，skill `foo:valid-skill-pkg`）
> - D-02: `seeds/user/home-user-basic`（skill `valid-skill-pkg`，source="user"）

### Marketplace seed 搭建

```bash
SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
TMPDIR=$(mktemp -d) && WORK="$TMPDIR/work" && BARE="$TMPDIR/test-mp.git"
bash "$SEEDS_ROOT/marketplace/_helpers/init_bare_repo.sh" \
  "$SEEDS_ROOT/marketplace/valid-single-plugin" "$WORK" "$BARE"
echo "BARE_URL=file://$BARE"
```

### User seed 搭建（D-02 前执行）

```bash
SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
SKILL_HOME=/tmp/a2c-uat-skill-home
mkdir -p "$SKILL_HOME/user/valid-skill-pkg"
cp -R "$SEEDS_ROOT/_common/valid-skill-pkg"/. "$SKILL_HOME/user/valid-skill-pkg/"
```

## 环境变量

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$
mkdir -p $A2C_SKILL_HOME
```

## 测试用例

### D-01: skill list --source mp（marketplace 技能列表）

- **优先级**: P0
- **前置**: 测试仓库已搭建
- **步骤**:
  1. 清理环境：`rm -rf $A2C_SKILL_HOME/*`
  2. 添加 marketplace：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add <BARE_URL> --trust --json`
  3. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer skill list --source mp --json`
  4. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 包含 `foo:valid-skill-pkg` skill，source 以 "marketplace" 开头
  - enabled 为 true，orphan 为 false

### D-02: skill list --source user（用户 drop-in 技能列表）

- **优先级**: P0
- **步骤**:
  1. 搭建 user seed（见上方「User seed 搭建」）
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer skill list --source user --json`
  3. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 包含 `valid-skill-pkg`，source 为 "user"
  - 显示为 orphan（无对应 plugin）

### D-03: skill list --source mcp（MCP 技能列表）

- **优先级**: P1
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer skill list --source mcp --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - MCP 源技能需要活跃 MCP server 连接，非交互 CLI 模式下返回空列表或 "No skills visible"

### D-04: skill info（查看单个技能详情）

- **优先级**: P0
- **前置**: D-01 成功（至少一个 marketplace skill 可见）
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer skill info foo:valid-skill-pkg --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 包含 name: "foo:valid-skill-pkg"
  - 包含 source、description、path、enabled 字段
  - version 为 "1.0.0"，license 为 "MIT"（如果 SKILL.md 中声明了）

### D-05: 渐进披露（get_skills → get_skill → get_blob）

- **优先级**: P0
- **类型**: 完整链路（需要 Server + Computer + Agent 三进程）
- **引用 seed**: `seeds/_helpers/skill-discovery` 提供 Agent 驱动脚本
- **前置**:
  1. D-01 成功（Computer 至少有一个 marketplace skill 含文本资源）
  2. 完整链路环境按 `resources/test-env-setup.md` 中"完整链路场景环境"搭建
  3. Computer 连接成功并加入 office
- **步骤**:
  运行 Agent 驱动脚本（自动执行 D-05-1~D-05-4）：

  ```bash
  cd /Users/liulonggang/PycharmProjects/python-sdk && \
  uv run python .claude/skills/UAT/resources/seeds/_helpers/skill-discovery/agent_skill_driver.py \
    --port-file /tmp/a2c-uat-port \
    --office-id skill-uat-office \
    --computer-name <computer_name> \
    --skill-name foo:valid-skill-pkg \
    2>&1 | tee /tmp/a2c-uat-logs/agent.log
  ```
- **预期结果**:
  - D-05-1: `get_skills` 返回 skill 引用列表，每项含 name、source、description
  - D-05-2: `get_skill` 返回完整 skill 内容（小资源 inline，frontmatter 已剥离）
  - D-05-3: `get_skill` + rel_path 返回子资源（inline 或 blob handle）
  - D-05-4: A2CSkillRef 4 必选字段契约（name / source / path / description）全部满足

## 清理

```bash
rm -rf /tmp/a2c-uat-skill-home
rm -rf $WORK_DIR
```

CLI-only 用例（D-01~D-04）完成后即可清理。D-05 完整链路用例需额外清理 tmux session 和端口文件。

## 日志收集

- D-01~D-04（CLI-only）：每个用例执行后 `capture-pane` 保存 pane 输出
- D-05（完整链路）：三端 pane 都必须 capture-pane，每端至少 50 行
