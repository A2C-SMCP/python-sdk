# `_common/valid-skill-pkg`

**Axis**: CM-01

**形态**: happy（well-formed 最小可用 SKILL 包）

**期望被派生使用方式**:

- **mcp**: `server_resources_ok.py` 启动时 `shutil.copytree` 到 mktemp 工作目录，作为
  `resources/read` 内容源；`server_resources_no_subs.py` 复用同源。
- **marketplace**: `valid-single-plugin/plugins/foo/skills/foo-skill/_seeds.manifest`
  指向本目录，acceptance 装配时拷入。
- **user**: `home-user-basic/_seeds.manifest` 指向本目录，acceptance 装配时拷入
  临时 SKILL_HOME。

**SKILL.md 关键字段**:

- `name`: valid-skill-pkg
- `description`: 非空，含触发关键词
- `license` / `version` / `allowed-tools` / `compatibility` / `metadata.axis` 均完备

**包结构**:

```
valid-skill-pkg/
├── SKILL.md
├── scripts/run.py
├── references/usage.md
├── README.md
└── acceptance.md
```

**已派生引用**:

- seeds/mcp/server_resources_ok.py
- seeds/mcp/server_resources_no_subs.py
- seeds/marketplace/valid-single-plugin/plugins/foo/skills/foo-skill/
- seeds/user/home-user-basic/
