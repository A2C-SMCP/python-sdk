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

测试使用本地裸仓库（`file://` 零网络），结构如下：

```
work/
├── .tfrobot-plugin/
│   └── marketplace.json     # 必须有，且 plugins[].source 必填
└── plugins/
    └── hello/
        └── skills/
            └── greet/
                └── SKILL.md
```

`marketplace.json` 最小格式（**source 字段必填**，否则 plugin staging 跳过）：

```json
{
  "version": "1.0.0",
  "plugins": [
    {
      "name": "hello",
      "source": "./plugins/hello"
    }
  ]
}
```

搭建脚本（在 tmux 中通过 Bash 执行）：

```bash
WORK_DIR=$(mktemp -d) && WORK="$WORK_DIR/work" && mkdir -p "$WORK" && cd "$WORK"
git init -q -b main && git config user.name "test" && git config user.email "test@test.com"
mkdir -p .tfrobot-plugin
cat > .tfrobot-plugin/marketplace.json << 'EOF'
{"version":"1.0.0","plugins":[{"name":"hello","source":"./plugins/hello"}]}
EOF
mkdir -p plugins/hello/skills/greet
cat > plugins/hello/skills/greet/SKILL.md << 'EOF'
---
name: greet
description: A test greeting skill
---
# Greet
Hello test skill.
EOF
git add -A . && git commit -q -m "init"
BARE="$WORK_DIR/test-mp.git" && git clone -q --bare . "$BARE"
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
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add <GIT_URL> --trust --json`
  3. 捕获输出
- **预期结果**:
  - 退出码 0
  - 输出包含成功添加信息（JSON 模式下返回 marketplace 名称和状态）
  - `/tmp/a2c-uat-skill-home/marketplace/` 下出现对应目录

### M-02: marketplace list（列出 marketplace）

- **优先级**: P0
- **前置**: M-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace list --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - JSON 输出包含刚添加的 marketplace
  - 显示 trusted/cloned 状态

### M-03: marketplace info（查看详情）

- **优先级**: P0
- **前置**: M-01 成功
- **步骤**:
  1. 从 M-01 输出获取 marketplace 名称
  2. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace info <NAME> --json`
  3. 捕获输出
- **预期结果**:
  - 退出码 0
  - 显示 marketplace 详情（skills 列表、strict 模式、auto-update 状态等）

### M-04: marketplace refresh（刷新 marketplace）

- **优先级**: P0
- **前置**: M-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace refresh <NAME> --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - 显示刷新成功信息（git pull 或 re-clone 完成）

### M-05: marketplace set auto-update（设置自动更新）

- **优先级**: P1
- **前置**: M-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace set <NAME> auto-update=true --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 0
  - 设置持久化（再次 `info` 可见 auto-update=true）

### M-06: marketplace add（不带 --trust 应失败）

- **优先级**: P1
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add <GIT_URL> --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 1（非交互模式下必须 --trust）
  - 输出包含 trust 确认相关错误信息

### M-07: marketplace remove（删除 marketplace）

- **优先级**: P0
- **前置**: M-01 成功
- **步骤**:
  1. 执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace remove <NAME> --json`
  2. 捕获输出
  3. 验证：再次 `marketplace list` 确认已移除
- **预期结果**:
  - 退出码 0
  - marketplace 从列表消失
  - clone 目录被清理

### M-08: marketplace add 重复添加

- **优先级**: P1
- **前置**: M-01 成功
- **步骤**:
  1. 再次执行：`A2C_SKILL_HOME=/tmp/a2c-uat-skill-home uv run a2c-computer marketplace add <GIT_URL> --trust --json`
  2. 捕获输出
- **预期结果**:
  - 退出码 1（已存在）
  - 输出包含已存在提示

## 清理

```bash
rm -rf /tmp/a2c-uat-skill-home
```

## 日志收集

CLI-only 场景下日志即 tmux pane 输出。每个用例执行后必须 `capture-pane` 保存完整输出。
