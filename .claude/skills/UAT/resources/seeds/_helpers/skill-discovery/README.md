# `_helpers/skill-discovery`

**用途**: 供 `skill-discovery` UAT 场景 D-05 用例的 Agent 驱动脚本

**提供**:
- `agent_skill_driver.py` — 可复用的 Agent 测试驱动，自动执行 D-05 渐进披露用例

**前置条件**:

Computer 至少有一个已注册的 SKILL（通过 marketplace 或 user drop-in）：
- marketplace: 使用 `seeds/marketplace/valid-single-plugin` → 注册 `foo:valid-skill-pkg`
- user: 使用 `seeds/user/home-user-basic` → 注册 `valid-skill-pkg`

**使用方式**:

1. 启动完整链路环境，Computer 有已注册 SKILL

2. Computer 加入 office 后运行 Agent 驱动：
   ```bash
   uv run python agent_skill_driver.py \
     --port-file /tmp/a2c-uat-port \
     --office-id skill-uat-office \
     --computer-name <computer_name> \
     --skill-name foo:valid-skill-pkg
   ```

**覆盖的用例**:
- D-05-1: get_skills — 发现 SKILL 列表
- D-05-2: get_skill — 获取入口 SKILL.md（文本内联，frontmatter 已剥离）
- D-05-3: get_skill + rel_path — 获取子资源（内联或 blob handle）
- D-05-4: A2CSkillRef 4 必选字段契约（name / source / path / description）

**注意**:
- D-01~D-04 是 CLI-only 测试，不需要 Agent driver
- `--skill-name` 可省略，driver 会自动从 get_skills 响应中选取第一个 SKILL
