---
name: create-skill
description: 创建符合规范的新 Claude Skill，包含完整结构、模板和目录组织。当用户需要创建新 skill、为 Claude Code 添加功能或搭建 skill 脚手架时使用。Creates new Claude Skills with proper structure and templates.
---

# Create Skill

帮助创建符合 Claude Code 规范的新 Skill。

## 核心原则

1. **代码胜于文档** — 能引用项目中现成的代码示例，就不在 SKILL 中摘录，直接用 Markdown 链接到文件
2. **模式优于步骤** — SKILL 重在阐述「为何如此做」和最佳实践，具体操作通过引用示例文件传达
3. **无实践不成 SKILL** — 如果待创建的 SKILL 在当前项目中尚无示例文档或实际代码，说明它尚未经过项目验证，**应拒绝创建**并向用户说明原因
4. **分步执行为主线** — SKILL 主体内容是分步描述执行流程，每步引用参考文件并说明预期输出

## 执行流程

### 第一步：需求确认与可行性检查

1. 理解用户要创建的 Skill 功能和使用场景
2. **关键检查**：在项目中搜索与该 Skill 相关的示例代码、配置文件或文档
   - 如果找不到任何相关示例，向用户说明：「该 SKILL 在当前项目中尚无实践案例，建议先在项目中实际操作一次，积累示例后再封装为 SKILL」
   - 如果找到相关示例，记录这些文件路径，后续步骤中引用
3. 确认 Skill 名称，遵循命名规范：小写字母 + 连字符（如 `fix-issue`、`create-skill`）

### 第二步：创建目录结构

在 `.claude/skills/` 下创建 Skill 目录：

```bash
mkdir -p .claude/skills/{skill-name}
```

目录结构参考现有 Skill：

```
.claude/
├── commands/
│   └── fix-issue.md          # 现有命令式 Skill 示例
└── skills/
    └── create-skill/
        └── SKILL.md           # 入口文件（必需）
```

> 注意：本项目同时存在 `commands/`（单文件命令）和 `skills/`（目录式 Skill）两种形式。简单的单步命令可放在 `commands/`，复杂的多步工作流放在 `skills/`。

### 第三步：编写 SKILL.md

SKILL.md 必须包含 YAML frontmatter 和主体内容。

**Frontmatter 格式**：

```yaml
---
name: skill-name          # 小写 + 连字符，最长 64 字符
description: 中文描述功能和触发场景。English description follows.  # 最长 1024 字符
---
```

**主体内容结构** — 以分步执行流程为主线：

```markdown
# Skill 标题

## 核心原则（可选）
说明为何如此设计，关键约束是什么

## 执行流程
### 第一步：...
1. 做什么
2. 参考：[示例文件](mdc:{baseDir}/path/to/example)
3. 预期输出：...

### 第二步：...
...

## 强制约束（可选）
不可违反的规则清单
```

**编写要点**：

- 引用项目中的现有文件作为示例，而非在 SKILL 中复制代码
- 每一步明确说明：要做什么、参考哪个文件、输出什么
- 如果 Skill 涉及同步/异步双版本（本项目常见模式），必须在流程中体现
- description 使用中英双语：中文在前，英文在后

### 第四步：适配项目约定

根据 [CLAUDE.md](mdc:CLAUDE.md) 中的项目约定，检查 SKILL 是否需要覆盖以下场景：

- **同步/异步一致性**：Server 和 Agent 模块的双版本同步更新
- **协议双文件**：`smcp.py` 和 `model.py` 的同步更新
- **测试镜像结构**：测试目录与源码目录保持一致
- **事件系统三步更新**：常量 → 处理方法 → Mock 服务器

如果 Skill 的操作涉及上述任一场景，必须在执行流程中加入对应检查步骤。

### 第五步：验证

创建完成后，逐项检查：

- [ ] SKILL.md 位于 `.claude/skills/{skill-name}/` 目录
- [ ] YAML frontmatter 包含 `name` 和 `description`
- [ ] `name` 使用小写字母和连字符
- [ ] `description` 中英双语，清晰说明功能和触发场景
- [ ] 主体以分步执行流程为骨架
- [ ] 每步引用了项目中的实际文件作为示例
- [ ] 所有路径使用正斜杠
- [ ] 未在 SKILL 中大段摘录代码，而是链接到源文件

## 现有 Skill 参考

- [fix-issue](mdc:.claude/commands/fix-issue.md) — 问题修复工作流，展示了分阶段执行、强制约束、TDD 驱动等模式

## 强制约束

- **无示例不创建**：项目中没有相关实践案例的 SKILL，拒绝创建
- **不摘录只引用**：SKILL 中不大段复制项目代码，用链接指向源文件
- **不堆砌参考**：每个引用都嵌在具体执行步骤中，服务于「下一步该做什么」
- **命名规范**：小写 + 连字符，不加 `skill-` 前缀，不用下划线或大写
