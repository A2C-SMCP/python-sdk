# 场景：plugin-management

## 测试目标

验证 `a2c-computer plugin` 子命令的完整生命周期：安装、列出、查看详情、禁用、启用、卸载、
垃圾回收，以及不同 scope 和 --keep-servers 选项。

## 类型

CLI-only（不需要 Server/Computer/Agent 多进程）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用
3. 测试 marketplace Git 仓库已准备（见下方「测试仓库搭建」）

## 测试仓库搭建

> **复用 seed**: `seeds/marketplace/plugin-with-bundled-mcp`
> plugin: `foo`，skill: `foo:valid-skill-pkg`，捆绑 MCP: `figma-mcp`

搭建脚本（在 tmux 中通过 Bash 执行）：

```bash
SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
TMPDIR=$(mktemp -d) && WORK="$TMPDIR/work" && BARE="$TMPDIR/test-mp.git"
bash "$SEEDS_ROOT/marketplace/_helpers/init_bare_repo.sh" \
  "$SEEDS_ROOT/marketplace/plugin-with-bundled-mcp" "$WORK" "$BARE"
echo "BARE_URL=file://$BARE"
# marketplace 名称由 bare repo 目录名决定（此处为 test-mp），非 marketplace.json 的 name 字段
# 后续用例中 MP_NAME 应取 marketplace add 返回的 JSON 中 added 字段的值
```

**重要**：marketplace 名称由 bare repo 目录名决定（如 `test-mp`），**不是** seed 的
`marketplace.json` 中 `name` 字段（`mp-bundled-mcp`）。用例中的 `$MP_NAME` 应取
`marketplace add --json` 返回的 `added` 字段值。

## 环境变量

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$
mkdir -p $A2C_SKILL_HOME
```

## 测试用例

### P-01: plugin install（安装 plugin）

- **优先级**: P0
- **前置**: marketplace 已添加
- **步骤**:
  1. 清理环境：`rm -rf $A2C_SKILL_HOME && mkdir -p $A2C_SKILL_HOME`
  2. 添加 marketplace：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add <BARE_URL> --trust --json`
     - 从输出中提取 `$MP_NAME`（`added` 字段值，通常为 `test-mp`）
  3. 安装 plugin：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin install foo@$MP_NAME --json`
  4. 捕获输出
- **预期结果**:
  - 退出码 0
  - 输出包含安装成功信息
  - 含 scope、mcpServers（声明依赖的 bundle_id：figma-mcp）

### P-02: plugin list（列出已安装 plugin）

- **优先级**: P0
- **前置**: P-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin list --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 包含 `foo@$MP_NAME`
  - enabled 为 true

### P-03: plugin info（查看 plugin 详情）

- **优先级**: P0
- **前置**: P-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin info foo@$MP_NAME --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 包含 id: "foo@$MP_NAME"
  - 包含 enabled、records（含 scope、installPath、version、mcpServers）

### P-04: plugin disable（禁用 plugin）

- **优先级**: P0
- **前置**: P-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin disable foo@$MP_NAME --json`
  2. 捕获输出
  3. 验证：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin list --available --json`
  4. 捕获输出
- **预期结果**:
  - disable 退出码 0
  - `plugin list --available` 显示 foo@$MP_NAME，enabled 为 false
  - `plugin list`（不带 --available）不显示 foo@$MP_NAME

### P-05: plugin enable（重新启用 plugin）

- **优先级**: P0
- **前置**: P-04 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin enable foo@$MP_NAME --json`
  2. 捕获输出
  3. 验证：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin list --json`
  4. 捕获输出
- **预期结果**:
  - enable 退出码 0
  - `plugin list` 显示 foo@$MP_NAME，enabled 为 true

### P-06: plugin uninstall（卸载 plugin）

- **优先级**: P0
- **前置**: P-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin uninstall foo@$MP_NAME --json`
  2. 捕获输出
  3. 验证：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin list --json`
  4. 捕获输出
- **预期结果**:
  - uninstall 退出码 0
  - `plugin list` 不再包含 foo@$MP_NAME
  - 捆绑的 MCP server 也被移除
- **注意**: uninstall 会清空 marketplace clone 中的 plugin 目录；后续用例如需重新安装，需先执行 `git -C $A2C_SKILL_HOME/marketplace/$MP_NAME checkout -- .` 恢复

### P-07: plugin gc（垃圾回收孤立 plugin）

- **优先级**: P1
- **步骤**:
  1. 安装 plugin：先执行 P-01 的 marketplace add + plugin install
  2. 手动编辑 **user scope 设置文件** `~/.config/a2c/settings.json`，从 enabledPlugins 中移除 foo@$MP_NAME 条目：
     ```bash
     SETTINGS="$HOME/.config/a2c/settings.json"
     python3 -c "
     import json, pathlib
     p = pathlib.Path('$SETTINGS')
     d = json.loads(p.read_text()) if p.exists() else {}
     d.pop('enabledPlugins', None)
     p.write_text(json.dumps(d, indent=2))
     "
     ```
  3. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin gc --json`
  4. 捕获输出
- **预期结果**:
  - gc 退出码 0
  - 输出包含移除的孤立 plugin 信息
  - 再次 `plugin list` 为空

### P-08: plugin install --scope（指定 scope 安装）

- **优先级**: P1
- **前置**: marketplace 已添加（P-01 步骤 2 完成）
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin install foo@$MP_NAME --scope user --json`
  2. 捕获输出
  3. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin info foo@$MP_NAME --json`
  4. 捕获输出
- **预期结果**:
  - install 退出码 0
  - info 显示 records 中含 scope: "user"

### P-09: plugin uninstall --keep-servers（卸载但保留 MCP server）

- **优先级**: P1
- **前置**: P-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer plugin uninstall foo@$MP_NAME --keep-servers --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - 输出包含卸载成功信息
  - 捆绑的 MCP server（figma-mcp）未被移除

## 清理

```bash
rm -rf /tmp/a2c-uat-skill-home
rm -rf $WORK_DIR
```

## 日志收集

CLI-only 场景下日志即 tmux pane 输出。每个用例执行后必须 `capture-pane` 保存完整输出。
