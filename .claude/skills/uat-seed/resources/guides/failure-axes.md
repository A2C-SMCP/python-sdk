# 失败维度分类（按协议条款 + SDK 触发点）

> 本文是失败种子的**唯一权威清单**。每条失败种子的"期望被测行为"必须能引用本表的
> 某一行。新增失败维度时先扩本表（含协议引用 / SDK 触发点 / 期望行为），再创建种子。

## 分类原则

1. **每条失败维度必须可追溯到协议条款**——不能凭空臆造"应该失败"
2. **每条失败维度必须可追溯到 SDK 触发点**——code 中实际抛错/记日志/跳过的位置
3. **期望被测行为分三层**：协议契约（应当如何） + SDK 实现（如何兑现） + 可观测信号
   （日志 / 状态 / 终端输出）

---

## MCP source —— mounted 模式

| Axis ID | 名字后缀 | 协议依据 | SDK 触发点 | 期望被测行为 |
|---|---|---|---|---|
| MC-MNT-01 | `mounted_missing_dir` | skill.md §3 A: `mount_dir` 必备 | `staging.py:_materialize_mounted` `raise SkillStagingError("mounted source missing 'mount_dir'")` | Computer 日志 ERROR `mounted source missing 'mount_dir'`；该 SKILL 被跳过；其他 SKILL 不受影响 |
| MC-MNT-02 | `mounted_nonexistent` | skill.md §3 A: 目录可达 | `staging.py:_materialize_mounted` `raise SkillStagingError(f"mounted source dir not found: {mount_dir!r}")` | Computer 日志 ERROR `mounted source dir not found`；跳过；不影响其他 |
| MC-MNT-03 | `mounted_symlink_in_tree` | skill.md §3 A: 不留符号链接（防绕过沙箱） | `staging.py:_materialize_mounted` 复制逻辑应解链 | staging 目录内**不**含符号链接（resolve 后是真实文件） |

## MCP source —— archive 模式

| Axis ID | 名字后缀 | 协议依据 | SDK 触发点 | 期望被测行为 |
|---|---|---|---|---|
| MC-ARC-01 | `archive_missing_uri` | skill.md §3 B: `archive_uri` 必备 | `_materialize_archive` `raise SkillStagingError("archive source missing 'archive_uri'")` | ERROR + 跳过 |
| MC-ARC-02 | `archive_bad_format` | skill.md §3 B: `archive_format` ∈ {tar.gz, zip} | `_materialize_archive` `raise SkillStagingError(f"unsupported archive_format: ...")` | ERROR + 跳过 |
| MC-ARC-03 | `archive_bad_sha` | skill.md §3 B: `archive_sha256` 完整性校验 | `_materialize_archive` `raise SkillStagingError(f"archive sha256 mismatch: ...")` | ERROR `archive sha256 mismatch`；不解压；跳过 |
| MC-ARC-04 | `archive_bomb` | skill.md §1.5 batch 健壮性 + 防 zip bomb | `_materialize_archive` 解压逐成员累计大小 > `MAX_EXTRACTED_BYTES` → `raise SkillStagingError("archive exceeds extracted size limit ...")` | ERROR；不写满磁盘；跳过 |
| MC-ARC-05 | `archive_too_many_members` | 防 tar 海量小文件 | `_materialize_archive` 成员数 > `MAX_ARCHIVE_MEMBERS` → ERROR | ERROR + 跳过 |
| MC-ARC-06 | `archive_path_traversal` | skill.md §1.5 安全 + protocol 沙箱要求 | `_materialize_archive` 检测 `member.name` 含 `../` 或绝对路径 → ERROR | ERROR `archive member escapes staging dir`；不落盘任何成员；跳过 |
| MC-ARC-07 | `archive_symlink_escape` | 防 symlink 突破 sandbox | `_materialize_archive` `raise SkillStagingError(f"archive contains link member (rejected)")` | ERROR；跳过 |
| MC-ARC-08 | `archive_oversize_download` | 防 HTTP 流量打满 | `_default_archive_fetch` 累计字节 > `MAX_ARCHIVE_DOWNLOAD_BYTES` → ERROR | ERROR；不解压；跳过 |
| MC-ARC-09 | `archive_uri_unreachable` | skill.md §3 B: 端点可达 | `_default_archive_fetch` aiohttp 异常 → 上层包装为 SkillStagingError | ERROR；跳过 |

## MCP source —— resources 模式

| Axis ID | 名字后缀 | 协议依据 | SDK 触发点 | 期望被测行为 |
|---|---|---|---|---|
| MC-RES-01 | `resources_no_subs` | skill.md §3 C: 子资源逐个 read | `_materialize_resources` 遍历 sub_resources 无写入 → `raise SkillStagingError("resources-mode SKILL has no sub-resources ...")` | ERROR；跳过 |
| MC-RES-02 | `resources_path_escape` | skill.md §3 C: 按相对路径**安全**写入 | `_resolved_member_target` 检测越界 → 抛错 | ERROR；不落盘越界文件；跳过 |
| MC-RES-03 | `resources_subs_carry_source_meta` | skill.md §3 子资源**不应**带 `_meta.source` | Computer 端：子资源不应被当根处理 | （这是 protocol violation；Computer 行为：把该子资源当成另一个根，按其声明的 mode 物化——种子在 server 端故意写入，audit 期望 Computer 日志能识别异常或冗余处理） |

## MCP source —— 通用

| Axis ID | 名字后缀 | 协议依据 | SDK 触发点 | 期望被测行为 |
|---|---|---|---|---|
| MC-GEN-01 | `no_resources_cap` | skill.md §1.5 / events.md `4015` | `list_skill_resources` 检测 server 未声明 `resources` 能力 → 跳过（不抛） | 该 server 的所有 SKILL 都不被物化；其他 server 不受影响；`client:get_resources` 对该 server 返回 `4015` |
| MC-GEN-02 | `cursor_paginated` | skill.md §12 Computer 完整消费 cursor | `manager.list_skill_resources` 循环 cursor 直到末尾 | 多页都被收齐；`_MAX_SKILL_LIST_PAGES` 上限测试单独一条种子 |
| MC-GEN-03 | `cursor_exceed_max_pages` | 防恶意 server 无限翻页 | `_MAX_SKILL_LIST_PAGES` 超限 → 截断 + WARN | WARN 日志；前 N 页 SKILL 注册；后续被丢弃 |
| MC-GEN-04 | `name_collision` | skill.md §1.5 保留先到者 | `_finalize_and_register` `name in seen_this_run` → 拒第二注册者 | ERROR `duplicate synthesized SKILL name within staging run`；先到者保留；后到者被清理 |
| MC-GEN-05 | `frontmatter_missing` | skill.md §3: `SKILL.md` frontmatter 权威 | `_finalize_and_register` `if not frontmatter.get("name") or ... description` → 跳过 | ERROR；staging 临时目录清理 |
| MC-GEN-06 | `skill_md_missing` | skill.md §2: SKILL.md 必存在 | `_finalize_and_register` `if not skill_md.is_file()` → 跳过 | ERROR；清理 |

## Marketplace 源

| Axis ID | 名字后缀 | 协议依据 | SDK 触发点 | 期望被测行为 |
|---|---|---|---|---|
| MK-VAL-01 | `valid-single-plugin` | marketplace SKILL v1 §2 / plugin v1 | happy path：`stage_marketplace` 全流程成功 | clone OK / plugin 扫到 / SKILL 注册 / `known_marketplaces.json` 写入 |
| MK-VAL-02 | `valid-multi-plugin` | 同上 | 同上，多 plugin | 全部注册 |
| MK-STR-01 | `strict-true-clean` | marketplace v1 §4.4 strict | `entry_is_strict` 默认 True | strict 模式无冲突时正常 |
| MK-STR-02 | `strict-false-conflict` | marketplace v1 §4.4 strict=false + plugin.json 声明组件 → 硬错 | `check_strict_conflict` → `raise PluginInstallError` | installer ERROR 硬失败；staging 退化软降级（依 #80 双路径） |
| MK-OVR-01 | `entry-skills-override` | marketplace v1 §4.3 entry.skills override | `resolve_skill_override_dirs` 找到非默认路径 | SKILL 从 override 路径加载，不是 `<plugin>/skills/` |
| MK-SRC-01 | `plugin-source-localpath` | marketplace v1 §4: LocalPath | `resolve_plugin_source` → `LocalPluginSource` | 在 clone 内定位 |
| MK-SRC-02 | `plugin-source-git-subdir` | git-subdir | sparse clone | 子目录 sparse clone 成功 |
| MK-SRC-03 | `plugin-source-url` | url 独立 clone | clone 到 `<home>/marketplace/.plugins/...` | 独立 clone 成功 |
| MK-SRC-04 | `plugin-source-github` | github 独立 clone | 同上 | 独立 clone 成功 |
| MK-SRC-05 | `plugin-source-cnb` | cnb 独立 clone | 同上 | 独立 clone 成功 |
| MK-ERR-01 | `missing-marketplace-json` | marketplace v1 §2: marketplace.json 必存在 | `read_marketplace_manifest` `raise PluginManifestError` | ERROR；该 marketplace 整体失败；不影响其他 marketplace |
| MK-ERR-02 | `malformed-plugin-json` | plugin v1: plugin.json 合法 | `read_plugin_metadata` JSON / schema 错 → ERROR | 该 plugin 跳过；同 marketplace 其他 plugin 继续 |
| MK-ERR-03 | `unknown-marketplace` | 信任模型 | `marketplace add` 走 trust 决策 | 未登记走 trust prompt（CLI 路径）/ 拒绝（headless） |
| MK-FLT-01 | `disabled-plugins` | enabledPlugins 过滤 | reconciler 按 `enabledPlugins ∩ installed` | 未启用 plugin 不被 mount |

## User 源

| Axis ID | 名字后缀 | 协议依据 | SDK 触发点 | 期望被测行为 |
|---|---|---|---|---|
| US-VAL-01 | `home-user-basic` | 设计 §5.0 user DropIn | `stage_user_skills` 在 `<home>/user/` 扫到 | 注册成功，`source = "user"` |
| US-VAL-02 | `workdir-basic` | 设计 §5.0 跨目录全局并集 | `stage_user_skills` 在 `<workdir>/.tfrobot/skills/` 扫到 | 注册成功 |
| US-OVR-01 | `override-low-vs-high` | 设计 §5.0 优先级 user < workdir 登记序 | 后者覆盖前者 + WARN | 最终生效者 = 最高优先级；WARN 日志 |
| US-ERR-01 | `invalid-name-camelcase` | skill.md frontmatter name 约束 | `synthesize_user_name` 校验 → `SkillNameError` | ERROR + 跳过；不影响其他 |
| US-ERR-02 | `missing-description` | skill.md §3 description 必填 | `_build_user_ref` 检查 → 跳过 | ERROR + 跳过 |
| US-ERR-03 | `invalid-deep-nested` | 设计 §5.0 仅根下一级 | `_iter_user_skill_dirs` 仅返根下一级 | 深嵌套的 SKILL.md 被静默忽略（DEBUG） |

## `_common/`（不是失败维度，是 SKILL 包形态库）

| ID | 名字 | 形态 | 用途 |
|---|---|---|---|
| CM-01 | `valid-skill-pkg` | well-formed 最小 SKILL | 所有 happy path 的原料 |
| CM-02 | `multi-file-skill-pkg` | 多文件 + 多目录的真实 SKILL | cursor 翻页、归档大小、文件计数 |
| CM-03 | `invalid-missing-desc` | frontmatter 无 description | 失败种子原料（被 mcp/marketplace/user 三源派生） |
| CM-04 | `invalid-bad-name` | frontmatter name 非 kebab | 失败种子原料 |
| CM-05 | `invalid-name-mismatch` | frontmatter name ≠ 目录 basename | 协议 §4 校正期望测试 |
| CM-06 | `deep-nested` | `<root>/a/b/SKILL.md` | user-source 深嵌套忽略 |

---

## 新增失败维度的流程

1. 先在 `a2c-smcp-protocol/docs/specification/skill.md` 找对应条款；找不到 → 该失败
   不应存在（或先在协议层加条款）
2. 在 SDK 找触发点；触发点不存在 → 协议有规定但 SDK 没实现 → 这是 SDK bug 而不是
   "失败维度种子"，应走 `/fix-issue` 而非 `/uat-seed`
3. 协议 + SDK 都到位 → 在本表追加一行（含 ID + 协议引用 + SDK 触发点 + 期望被测行为）
4. 在对应 source recipe 里追加该失败种子的 acceptance 模板（若与现有模板差异较大）
5. 走 `/uat-seed create <source> <name>`

## 期望被测行为的"三件套"模板

每条 acceptance 都应在期望里覆盖：

```markdown
**期望被测行为**：

1. **协议契约**：[skill.md §X 的原文引用]
2. **SDK 实现**：[函数名 + 日志/异常关键字]
3. **可观测信号**：
   - Computer 日志（grep 关键字）：`<exact substring>`
   - Computer 状态（`/skills` 列表 / SKILL Home staging 目录）：`<expected absent or specific shape>`
   - 进程行为：`<不崩溃 / 其他 SKILL 正常>`
```
