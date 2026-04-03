---
name: release
description: 指导 A2C-SMCP Python SDK 的版本发布流程，包括版本号确认、CI 检查、GitHub Release 创建和发布监控。Guide the release workflow for A2C-SMCP Python SDK, including version confirmation, CI checks, GitHub Release creation, and publish monitoring.
---

# Release

你是发布工程师，负责指导用户完成 A2C-SMCP Python SDK 的版本发布。发布通过创建 GitHub Release 触发 [publish workflow](mdc:.github/workflows/publish.yml)，自动构建并发布到 TestPyPI 或 PyPI。

## 输入

发布意图或版本号: $ARGUMENTS

## 核心概念

- **TestPyPI 发布**：创建 GitHub Release 时勾选 `prerelease`，workflow 发布到 TestPyPI
- **PyPI 正式发布**：创建 GitHub Release 时不勾选 `prerelease`，且 target 为 `main`，workflow 发布到 PyPI
- **两个环境都需要 Reviewer 审批**：`testpypi` 和 `pypi` 环境均配置了 Required Reviewers（JIAQIA），创建 Release 后需要到 GitHub Actions 页面通过 Review 才能完成发布
- **版本管理**：使用 [bump-my-version](mdc:.bumpversion.toml) 管理，支持 PEP 440 格式（major.minor.patch + a/b/rc/dev/post）

## 执行流程

### 第一步：确认当前版本与发布目标

1. 读取当前版本号：

```bash
grep 'current_version' .bumpversion.toml
```

2. 查看最近的 tag 和版本历史：

```bash
git tag --sort=-v:refname | head -10
```

3. 与用户确认发布目标：
   - **发布环境**：TestPyPI（测试）还是 PyPI（正式）？
   - **版本策略**：参考 [.bumpversion.toml](mdc:.bumpversion.toml) 中的配置，可用的 bump 部分包括：
     - `major` / `minor` / `patch` — 主版本 / 次版本 / 补丁版本
     - `pre_l` — 预发布标签递进：a → b → rc → 正式
     - `pre_n` — 预发布序号递增（如 rc1 → rc2）
     - `dev_n` — 开发版本号
   - **示例版本演进**：
     - 当前 `0.1.2` → `bump minor` → `0.2.0`
     - 当前 `0.1.2` → `bump patch` + `bump pre_l` → `0.1.3a1`（alpha 预发布）
     - 当前 `0.1.3a1` → `bump pre_l` → `0.1.3b1` → `bump pre_l` → `0.1.3rc1` → `bump pre_l` → `0.1.3`

> **等待用户确认版本号和发布环境后再继续。**

### 第二步：确保 CI 流水线通过

1. 检查 main 分支上最新的 CI 运行状态：

```bash
gh run list --branch main --limit 5
```

2. 如果有失败的 workflow，查看详情：

```bash
gh run view <run-id>
```

3. **所有 CI 必须通过才能继续发布**。如果有失败，告知用户并停止流程。

参考 workflow 定义：
- [tests.yml](mdc:.github/workflows/tests.yml) — 单元测试和 lint
- [e2e-tests.yml](mdc:.github/workflows/e2e-tests.yml) — 端到端测试

### 第三步：执行版本 Bump

1. 确保工作目录干净（无未提交的更改）：

```bash
git status
```

2. 执行 bump-my-version（根据用户确认的策略）：

```bash
uv run bump-my-version bump <part>
# 例如: uv run bump-my-version bump minor
# 例如: uv run bump-my-version bump pre_l
```

3. bump-my-version 会自动更新以下文件并创建 commit + tag：
   - `pyproject.toml` 中的 `version` 字段
   - `a2c_smcp/__init__.py` 中的 `__version__`
   - 创建格式为 `v{new_version}` 的 git tag

4. 验证 bump 结果：

```bash
git log --oneline -1
git tag --sort=-v:refname | head -3
grep 'current_version' .bumpversion.toml
```

### 第四步：推送代码和 Tag

```bash
git push origin main --follow-tags
```

> **注意**：如果当前不在 main 分支，正式发布（PyPI）要求 target 为 main。TestPyPI 发布无此限制但建议同样从 main 发布。

### 第五步：创建 GitHub Release

根据发布环境选择不同的命令：

**TestPyPI（测试发布）**：

```bash
gh release create v<version> --title "v<version>" --generate-notes --prerelease
```

**PyPI（正式发布）**：

```bash
gh release create v<version> --title "v<version>" --generate-notes
```

> 关键区别：`--prerelease` 标志决定 [publish.yml](mdc:.github/workflows/publish.yml) 发布到 TestPyPI 还是 PyPI。

### 第六步：提醒用户审批

创建 Release 后，publish workflow 会自动触发，但会在 environment review 步骤暂停等待审批。

1. 获取触发的 workflow run：

```bash
gh run list --workflow=publish.yml --limit 3
```

2. **提醒用户**：

> ⚠️ 发布 workflow 已触发，但需要 Reviewer 审批才能继续。
> 请前往 GitHub Actions 页面审批部署：
> https://github.com/A2C-SMCP/python-sdk/actions
>
> `testpypi` 和 `pypi` 环境均需要审批通过后才能发布。

### 第七步：监控发布结果

1. 等待用户确认已完成审批后，监控 workflow 状态：

```bash
gh run watch <run-id>
```

或查看状态：

```bash
gh run view <run-id>
```

2. 发布成功后，验证包已上传：

**TestPyPI**：
```bash
pip index versions a2c-smcp --index-url https://test.pypi.org/simple/
```

**PyPI**：
```bash
pip index versions a2c-smcp
```

3. 向用户报告发布结果：
   - workflow 运行状态
   - 包版本是否可在目标 registry 上找到
   - 发布链接：TestPyPI `https://test.pypi.org/project/a2c-smcp/` 或 PyPI `https://pypi.org/project/a2c-smcp/`

## 强制约束

- **禁止跳过 CI 检查**：CI 未全部通过时不得发布
- **禁止跳过用户确认**：版本号和发布环境必须经用户确认
- **禁止忘记审批提醒**：两个环境都需要 Reviewer 审批，必须明确提醒用户
- **Tag 必须匹配版本**：publish workflow 会验证 tag 与 pyproject.toml 版本一致，不匹配会导致发布失败
- **正式发布必须从 main**：PyPI 发布要求 `target_commitish == 'main'`
