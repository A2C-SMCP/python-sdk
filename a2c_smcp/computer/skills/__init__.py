# -*- coding: utf-8 -*-
# filename: __init__.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer SKILL 子系统 / Computer SKILL subsystem（v0.2.1）

对标 Claude Code marketplace 的本地 SKILL 管理：SKILL Home / 命名 lexer / Registry / 三源
staging / 意图层 reconciler / 沙箱。已落地模块：
Mirrors Claude Code marketplace local SKILL management. Landed modules:

- :mod:`a2c_smcp.computer.skills.home`     —— SKILL Home 解析 + `0o700` 防御性写（隔离交 OS，对齐 CC）+ 安装目录布局（S1，#54）
- :mod:`a2c_smcp.computer.skills.naming`   —— name 合成 + 段数消歧 lexer + MCP server 段规范化（S1，#54）
- :mod:`a2c_smcp.computer.skills.registry` —— `name → A2CSkillRef` O(1) 物化索引 + 孤儿标记/恢复（S3，#57）
- :mod:`a2c_smcp.computer.skills.staging`  —— 三源 staging（mcp #59 + user #60 + marketplace git #61）
- :mod:`a2c_smcp.computer.skills.sources`  —— marketplace plugin source 5 类解析 + github/cnb 简写糖归一化（S8，#61）
- :mod:`a2c_smcp.computer.skills.sandbox`  —— 包根内 `safe_join`+`realpath` 沙箱 + `.skillenv` forbidden + name 寻址防越权（S2，#55）
- :mod:`a2c_smcp.computer.skills.resource` —— 沙箱解析 → 消费字节视图（mint/serve 字节基准一致：sha256/total_size）（S13，#66）
- :mod:`a2c_smcp.computer.skills.debouncer`—— 缓存失效 + 300ms debounce + 单次 emit（事件触发链，S14，#67）
- :mod:`a2c_smcp.computer.skills.watcher`  —— user 源 DropIn 文件 watcher（watchdog 递归监 SKILL.md + markInternalWrite）（S14，#67）

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §1 / §4 / §6 / §8 / §9.2。
"""

from __future__ import annotations

from a2c_smcp.computer.skills.debouncer import (
    DEFAULT_DEBOUNCE_MS,
    SkillEventDebouncer,
)
from a2c_smcp.computer.skills.home import (
    SKILL_HOME_ENV,
    SKILL_HOME_MODE,
    SOURCE_MARKETPLACE,
    SOURCE_MCP,
    SOURCE_USER,
    ensure_skill_home,
    marketplace_skill_dir,
    mcp_skill_dir,
    resolve_skill_home,
    user_dropin_root,
    user_skill_dir,
)
from a2c_smcp.computer.skills.naming import (
    MCP_SEGMENT,
    ParsedSkillName,
    SkillNameError,
    SkillNameKind,
    is_valid_skill_name,
    parse_skill_name,
    synthesize_marketplace_name,
    synthesize_mcp_name,
    synthesize_user_name,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.resource import (
    DEFAULT_SKILL_MIME,
    SkillResourceView,
    looks_textual,
    resolve_skill_view,
)
from a2c_smcp.computer.skills.sandbox import (
    DEFAULT_SKILL_FILE,
    FORBIDDEN_SKILL_FILES,
    SkillSandboxError,
    SkillSandboxReason,
    ensure_within_size_cap,
    resolve_skill_resource,
)
from a2c_smcp.computer.skills.sources import (
    DEFAULT_PLUGIN_ROOT,
    GitCloneSpec,
    LocalPluginSource,
    ResolvedPluginSource,
    SkillSourceError,
    marketplace_clone_url,
    normalize_repo_shorthand,
    resolve_plugin_source,
)
from a2c_smcp.computer.skills.staging import (
    SkillStagingError,
    parse_skill_frontmatter,
    stage_marketplace_skills,
    stage_mcp_skills,
    stage_user_skills,
    strip_skill_frontmatter,
)
from a2c_smcp.computer.skills.watcher import (
    DEFAULT_INTERNAL_WRITE_TTL_S,
    SkillFileWatcher,
)

__all__ = [
    # debouncer / watcher（事件触发链：缓存失效 + 300ms debounce + 文件监控）
    "DEFAULT_DEBOUNCE_MS",
    "DEFAULT_INTERNAL_WRITE_TTL_S",
    "SkillEventDebouncer",
    "SkillFileWatcher",
    # home
    "SKILL_HOME_ENV",
    "SKILL_HOME_MODE",
    "SOURCE_MARKETPLACE",
    "SOURCE_MCP",
    "SOURCE_USER",
    "ensure_skill_home",
    "marketplace_skill_dir",
    "mcp_skill_dir",
    "resolve_skill_home",
    "user_dropin_root",
    "user_skill_dir",
    # naming
    "MCP_SEGMENT",
    "ParsedSkillName",
    "SkillNameError",
    "SkillNameKind",
    "is_valid_skill_name",
    "parse_skill_name",
    "synthesize_marketplace_name",
    "synthesize_mcp_name",
    "synthesize_user_name",
    # registry
    "SkillRegistry",
    # sandbox
    "DEFAULT_SKILL_FILE",
    "FORBIDDEN_SKILL_FILES",
    "SkillSandboxError",
    "SkillSandboxReason",
    "ensure_within_size_cap",
    "resolve_skill_resource",
    # resource（消费字节视图：mint/serve 一致）
    "DEFAULT_SKILL_MIME",
    "SkillResourceView",
    "looks_textual",
    "resolve_skill_view",
    # sources（marketplace plugin source 5 类解析 + 简写糖归一化）
    "DEFAULT_PLUGIN_ROOT",
    "GitCloneSpec",
    "LocalPluginSource",
    "ResolvedPluginSource",
    "SkillSourceError",
    "marketplace_clone_url",
    "normalize_repo_shorthand",
    "resolve_plugin_source",
    # staging
    "SkillStagingError",
    "parse_skill_frontmatter",
    "stage_marketplace_skills",
    "stage_mcp_skills",
    "stage_user_skills",
    "strip_skill_frontmatter",
]
