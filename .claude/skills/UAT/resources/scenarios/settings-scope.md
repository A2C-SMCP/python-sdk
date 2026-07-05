# 场景：settings-scope

## 测试目标

验证 `a2c-computer settings` 子命令的五级 scope 体系：user / project / local / flag / policy，
包括 show、get、set 操作，scope merge 语义，以及只读 scope 的错误处理。

## 类型

CLI-only（不需要 Server/Computer/Agent 多进程）

## 前置条件

1. `uv sync --all-groups` 已执行
2. `a2c-computer` 命令可用

## 环境变量

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$
mkdir -p $A2C_SKILL_HOME
```

## 测试用例

### G-01: settings show merged（默认合并视图）

- **优先级**: P0
- **步骤**:
  1. 清理环境：`rm -rf $A2C_SKILL_HOME/*`
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings show --json`
  3. 捕获输出
- **预期结果**:
  - 退出码 0
  - 输出为有效 JSON（合并后的 settings 对象）
  - 包含默认配置项（如 marketplaces、enabledPlugins 等）

### G-02: settings show --scope user（查看 user scope）

- **优先级**: P0
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings show --scope user --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 仅包含 user scope 的设置（初始可能为空对象 `{}`）

### G-03: settings get（获取单个 key）

- **优先级**: P0
- **前置**: 已在 user scope 设置一个 key（先执行 G-04）
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings set strictKnownMarketplaces true --scope user --json`
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings get strictKnownMarketplaces --json`
  3. 捕获输出
- **预期结果**:
  - set 退出码 0
  - get 退出码 0
  - get 输出包含 `strictKnownMarketplaces: true`

### G-04: settings set --scope user（设置值到 user scope）

- **优先级**: P0
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings set strictKnownMarketplaces true --scope user --json`
  2. 捕获输出
  3. 验证：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings show --scope user --json`
  4. 捕获输出
- **预期结果**:
  - set 退出码 0，输出含 scope/key/value 信息
  - show --scope user 输出包含 strictKnownMarketplaces: true

### G-05: settings set --scope project（锚定进程 cwd，#116）

- **优先级**: P1
- **步骤**:
  1. 创建临时目录并切入：`mkdir -p /tmp/a2c-uat-proj && cd /tmp/a2c-uat-proj`
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run --project <python-sdk 路径> a2c-computer settings set strictKnownMarketplaces true --scope project --json`
  3. 捕获输出并检查 `/tmp/a2c-uat-proj/.tfrobot/settings.json`
- **预期结果**:
  - 退出码 0，输出含 scope/key/value 信息
  - `<cwd>/.tfrobot/settings.json` 生成且包含 `"strictKnownMarketplaces": true`
  - #116 起 project/local scope 无条件锚定进程 cwd（`--add-dir` 已移除，不再有 "requires an active workdir" 错误）

### G-06: scope merge 验证（覆盖后合并）

- **优先级**: P1
- **前置**: G-04 成功
- **步骤**:
  1. user scope 已设置 strictKnownMarketplaces: true
  2. 修改：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings set strictKnownMarketplaces false --scope user --json`
  3. 获取合并视图：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings get strictKnownMarketplaces --json`
  4. 捕获输出
- **预期结果**:
  - set 退出码 0
  - get 返回 strictKnownMarketplaces: false（合并视图反映最新值）

### G-07: 只读 scope 错误（policy / flag scope 不可写）

- **优先级**: P0
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings set testKey value --scope policy --json`
  2. 捕获输出
  3. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer settings set testKey value --scope flag --json`
  4. 捕获输出
- **预期结果**:
  - 两条命令均退出码 1
  - 输出包含 "read-only" 或 "writable" 相关错误信息

### G-08: flag scope with --settings（通过文件传入 flag scope）

- **优先级**: P1
- **步骤**:
  1. 创建临时 flag settings 文件：
     ```bash
     echo '{"testFlag": true, "flagKey": "flagValue"}' > /tmp/a2c-uat-flag-settings.json
     ```
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer --settings /tmp/a2c-uat-flag-settings.json settings show --scope flag --json`
  3. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 包含 testFlag: true 和 flagKey: "flagValue"
  - 注意：`--settings` 是 root 级 flag，必须在子命令之前

## 清理

```bash
rm -rf /tmp/a2c-uat-skill-home
rm -f /tmp/a2c-uat-flag-settings.json
```

## 日志收集

CLI-only 场景下日志即 tmux pane 输出。每个用例执行后必须 `capture-pane` 保存完整输出。
