# UAT 种子库目录布局规范

> 本文档是种子库**目录结构**的权威定义。所有 recipe 都引用本文。

## 顶层

```
.claude/skills/UAT/resources/seeds/
├── README.md                       ← 顶层索引（每条种子一行）
├── _common/                         ← 跨源共享 SKILL 包原料
├── mcp/                             ← 可执行 MCP Server 种子
├── marketplace/                     ← Git 仓库种子
└── user/                            ← 就地静态目录种子
```

## `_common/` — SKILL 包原料库

```
_common/
├── README.md
├── valid-skill-pkg/                ← well-formed 最小可用 SKILL 包
│   ├── SKILL.md                    ← frontmatter: name/description 完整
│   ├── scripts/run.py              ← 可选，但建议有以触发 scripts 路径
│   ├── references/usage.md         ← 可选
│   ├── assets/icon.svg             ← 可选
│   └── acceptance.md
├── multi-file-skill-pkg/           ← 多文件、多目录的真实包（测试 cursor 翻页 / 大批量）
├── invalid-missing-desc/           ← frontmatter 缺 description
├── invalid-bad-name/               ← name 非 kebab-case（如 camelCase）
├── invalid-name-mismatch/          ← name 与目录 basename 不一致
├── deep-nested/                    ← <root>/a/b/SKILL.md（user 源应忽略）
└── ...
```

**规则**：

- 每个子目录是**一份完整的 SKILL 包**（不论合法/非法），保持"就地可读"
- 必带 `acceptance.md` 做静态校验
- 命名直接反映"做什么场景的原料"，**不**带源前缀（`_common` 自身就是跨源）

## `mcp/` — 可执行 MCP Server 种子

```
mcp/
├── README.md
├── _archives/                              ← 归档预制（archive 模式用）
│   ├── build.sh                            ← 重建脚本
│   ├── manifest.json                       ← {name -> sha256, source: _common/<x>, axis: <bomb/traversal/...>}
│   ├── valid-1.0.0.tar.gz                  ← 由 build.sh 从 _common/valid-skill-pkg/ 打包
│   ├── valid-1.0.0.zip
│   ├── bad-sha.tar.gz                      ← 与 acceptance 里宣称的 sha256 不一致
│   ├── tar-bomb.tar.gz                     ← 解压 > MAX_EXTRACTED_BYTES
│   ├── tar-too-many-members.tar.gz         ← 成员数 > MAX_ARCHIVE_MEMBERS
│   ├── path-traversal.tar.gz               ← 含 ../ 路径成员
│   ├── symlink-escape.tar.gz               ← 含 symlink 成员
│   └── README.md                           ← 每个归档对应哪个攻击面
├── _http_fixture.py                        ← 启动本地 HTTP 服务，serve _archives/
├── server_mounted_ok.py                    ← happy: mounted 模式
├── server_mounted_ok.acceptance.sh
├── server_mounted_missing_dir.py           ← failure: mount_dir 字段缺
├── server_mounted_missing_dir.acceptance.sh
├── server_mounted_nonexistent.py           ← failure: mount_dir 字段有但目录不存在
├── server_mounted_nonexistent.acceptance.sh
├── server_archive_ok.py                    ← happy: archive 模式
├── server_archive_ok.acceptance.sh
├── server_archive_bad_sha.py
├── server_archive_bad_sha.acceptance.sh
├── server_archive_bomb.py
├── server_archive_bomb.acceptance.sh
├── server_archive_path_traversal.py
├── server_archive_path_traversal.acceptance.sh
├── server_resources_ok.py                  ← happy: resources 模式
├── server_resources_ok.acceptance.sh
├── server_resources_no_subs.py             ← failure: 声明 resources 但 list 不返子资源
├── server_resources_no_subs.acceptance.sh
├── server_resources_path_escape.py         ← failure: 子 URI 解算出 ../ 路径
├── server_resources_path_escape.acceptance.sh
├── server_mixed_modes.py                   ← happy 组合: 一个 server 暴露三模式
├── server_mixed_modes.acceptance.sh
├── server_cursor_paginated.py              ← happy: 多页 cursor
├── server_cursor_paginated.acceptance.sh
├── server_no_resources_cap.py              ← failure: 不声明 resources 能力（触发 4015）
├── server_no_resources_cap.acceptance.sh
├── server_name_collision.py                ← failure: 两 SKILL 合成同 name
└── server_name_collision.acceptance.sh
```

**规则**：

- **每个 server 一个独立 .py 文件**，文件名 = 种子 name
- **acceptance 与 server 同名 + `.acceptance.sh`**：成对存在
- 启动协议：`python <server>.py --port <P>` → 端口起来 + stdout 打印 `MCP_READY port=<P>`
- 退出协议：SIGTERM 时清理临时目录、关闭 sockets
- `--port 0` 自动分配，将选定端口写到 `/tmp/a2c-uat-seed-<name>.port`

## `marketplace/` — Git 仓库种子

```
marketplace/
├── README.md
├── _helpers/
│   └── init_bare_repo.sh                   ← 把种子目录 git init + commit + 输出 file:// URL
├── valid-single-plugin/                    ← happy: 1 plugin 1 skill
│   ├── .tfrobot-plugin/
│   │   └── marketplace.json
│   ├── plugins/
│   │   └── foo/
│   │       ├── .tfrobot-plugin/plugin.json
│   │       ├── skills/foo-skill/SKILL.md     ← 从 _common/valid-skill-pkg/ 拷
│   │       └── mcp-servers/foo-mcp.json
│   ├── README.md
│   └── acceptance.sh
├── valid-multi-plugin/
├── strict-true-clean/
├── strict-false-conflict/                  ← strict=false + plugin.json 声明组件 → 硬错
├── entry-skills-override/                  ← entry.skills 覆写到非默认路径
├── plugin-source-localpath/
├── plugin-source-git-subdir/
├── plugin-source-url/
├── plugin-source-github/
├── plugin-source-cnb/
├── missing-marketplace-json/
├── malformed-plugin-json/
├── unknown-marketplace/                    ← 未注册 → 触发 trust 决策
└── disabled-plugins/                       ← enabledPlugins 过滤
```

**规则**：

- 每个子目录是一份**完整可 clone 的 marketplace 仓库的工作树**（含 `.tfrobot-plugin/`）
- **不**在仓库里直接 `git init`（避免嵌套 git）；`_helpers/init_bare_repo.sh` 在
  audit / scenario setup 时按需把当前目录复制到 `/tmp/<name>-bare.git`，做成裸库后
  返回 `file:///tmp/<name>-bare.git` URL
- `acceptance.sh` 必须**自包含**：自起一份临时 SKILL_HOME，跑完清理

## `user/` — 就地静态目录种子

```
user/
├── README.md
├── home-user-basic/                        ← 准备拷进 <home>/user/<basic>/ 的目录
│   ├── SKILL.md
│   ├── scripts/run.py
│   ├── README.md
│   └── acceptance.md
├── workdir-basic/                          ← 准备拷进 <workdir>/.tfrobot/skills/<basic>/
├── override-low-vs-high/
│   ├── home-user/<same-name>/              ← 同名，<home>/user 版本（低优先级）
│   ├── workdir-A/<same-name>/              ← workdir A 版本（中优先级）
│   ├── workdir-B/<same-name>/              ← workdir B 版本（高优先级）
│   ├── README.md
│   └── acceptance.md
├── invalid-name-camelcase/
├── invalid-deep-nested/                    ← <root>/a/b/SKILL.md → 应被忽略
└── missing-description/
```

**规则**：

- 单源种子：单一 SKILL 目录 + `acceptance.md`
- 多源对比种子（如 override-low-vs-high）：每个层级一份子目录 + 顶层 `acceptance.md`
  说明拷贝路径与期望最终被采纳的版本

## 命名规则汇总

| Source | 命名风格 | 例 |
|---|---|---|
| `mcp/` 脚本 | snake_case（Python 模块名） | `server_archive_bad_sha.py` |
| `mcp/` 归档 | kebab-case | `tar-bomb.tar.gz` |
| `marketplace/` | kebab-case | `strict-false-conflict/` |
| `user/` | kebab-case | `override-low-vs-high/` |
| `_common/` | kebab-case | `invalid-missing-desc/` |

## 索引登记格式

`seeds/README.md` 顶层索引按 source 分组，每行：

```markdown
| <name> | <mode 或 N/A> | <failure-axis 或 happy> | <acceptance 路径> | <引用 scenarios，可空> |
```

`mode` 列仅对 `mcp` 有意义（mounted/archive/resources/mixed/cursor）；其他源填 `-`。

## 演进规则

- 新种子先入 `seeds/README.md` 索引登记一行（**先登记后创建**——发现冲突早）
- 废弃种子：从索引删除 → 检查是否有 scenario 引用 → 有则先改 scenario → 再 `rm -rf`
- 重命名：先索引改 → grep 引用更新 → 最后改文件名
