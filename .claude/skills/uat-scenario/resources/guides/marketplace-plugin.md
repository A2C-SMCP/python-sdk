# 场景设计指南：Marketplace / Plugin 类

适用于涉及 marketplace 增删改查、plugin 生命周期管理、strict 模式验证的 UAT 场景。

## 核心特征

- **CLI-only 为主**：大部分操作无需 Server/Computer/Agent，直接 CLI 子命令
- **Git 仓库依赖**：需要本地裸仓库（`file://`）模拟 marketplace 源
- **manifest 结构敏感**：marketplace.json 的 plugins[].source 必填，否则 staging 跳过
- **配置组合验证**：strict=true/false、auto-update=true/false 等需覆盖

## 设计原则

### 1. 测试仓库先行

每个场景必须先搭建测试仓库。使用本地裸仓库（零网络依赖）：

```markdown
### 测试仓库结构

work/
├── .tfrobot-plugin/
│   └── marketplace.json     # 必须有，plugins[].source 必填
└── plugins/
    └── hello/
        └── skills/greet/SKILL.md
```

**marketplace.json 最小格式**：

```json
{"version": "1.0.0", "plugins": [{"name": "hello", "source": "./plugins/hello"}]}
```

> **常见踩坑**：`source` 字段缺失 → staging 跳过 → `skills: 0`。必须在场景中标注。

### 2. 环境隔离必做

所有命令使用独立的 `A2C_SKILL_HOME`：

```bash
A2C_SKILL_HOME=/tmp/a2c-uat-skill-home-$$
```

用 PID 后缀避免 zsh `rm -rf *` 确认弹窗。清理时直接 `rm -rf /tmp/a2c-uat-skill-home-*`。

### 3. 退出码断言 + 输出断言双验证

CLI 命令测试必须同时验证：

| 验证维度 | 正向用例 | 反向用例 |
| -------- | -------- | -------- |
| 退出码 | 0 | 1（或非零） |
| 输出内容 | JSON 包含预期字段 | JSON 包含 error 字段和具体错误信息 |

**退出码获取**：tmux `get-command-result` 的 `exit code` 字段，或
`capture-pane` 中的 `TMUX_MCP_DONE_$status`。

### 4. CRUD 生命周期编排

marketplace/plugin 场景按 CRUD 生命周期编排：

```
Phase 1: Create（add --trust → 验证 list 可见）
Phase 2: Read（list → info → skill list）
Phase 3: Update（refresh → set auto-update）
Phase 4: Error Cases（重复添加 → 不带 trust → 无效 URL）
Phase 5: Delete（remove → 验证 list 已无）
```

**状态复用**：Phase 1 的 add 结果直接用于 Phase 2/3，最后 Phase 5 清理。

### 5. Strict 模式的组合覆盖

strict 模式需要覆盖以下组合：

| strict | entry.skills | plugin.json skills | 预期行为 |
| ------ | ------------ | ------------------ | -------- |
| true   | 有           | 有                 | entry 追加到 plugin |
| true   | 无           | 有                 | 仅 plugin skills |
| false  | 有           | 无                 | entry 替换全部 |
| false  | 有           | 有                 | 硬错误（冲突） |

每个组合是一个独立用例，需要不同的测试仓库。

### 6. 幂等性保证

每个场景的清理步骤必须保证：

1. `marketplace remove` 清除所有添加的 marketplace
2. `rm -rf $A2C_SKILL_HOME` 清除 clone 目录和物化文件
3. 验证：再次 `marketplace list` 应为空

## 验证清单补充项

- [ ] 测试仓库搭建脚本完整且可复现
- [ ] marketplace.json 的 source 字段已正确填写
- [ ] A2C_SKILL_HOME 使用 PID 隔离
- [ ] 每个用例同时验证退出码和输出内容
- [ ] CRUD 生命周期编排合理（Create → Read → Update → Delete）
- [ ] Strict 模式覆盖了关键组合
- [ ] 清理步骤可恢复到初始状态
