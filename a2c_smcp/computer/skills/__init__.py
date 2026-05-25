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
- :mod:`a2c_smcp.computer.skills.staging`  —— 多 source 物化（mcp 源 + manager.list_skill_resources）（S6，#59）
- :mod:`a2c_smcp.computer.skills.sandbox`  —— 包根内 `safe_join`+`realpath` 沙箱 + `.skillenv` forbidden + name 寻址防越权（S2，#55）

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §1 / §4 / §6 / §8 / §9.2。
"""

from __future__ import annotations

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
    user_skill_dir,
)
from a2c_smcp.computer.skills.naming import (
    MCP_SEGMENT,
    ParsedSkillName,
    SkillNameError,
    SkillNameKind,
    is_valid_skill_name,
    normalize_mcp_server_segment,
    parse_skill_name,
    synthesize_marketplace_name,
    synthesize_mcp_name,
    synthesize_user_name,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.sandbox import (
    DEFAULT_SKILL_FILE,
    FORBIDDEN_SKILL_FILES,
    SkillSandboxError,
    SkillSandboxReason,
    ensure_within_size_cap,
    resolve_skill_resource,
)
from a2c_smcp.computer.skills.staging import SkillStagingError, parse_skill_frontmatter, stage_mcp_skills

__all__ = [
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
    "user_skill_dir",
    # naming
    "MCP_SEGMENT",
    "ParsedSkillName",
    "SkillNameError",
    "SkillNameKind",
    "is_valid_skill_name",
    "normalize_mcp_server_segment",
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
    # staging
    "SkillStagingError",
    "parse_skill_frontmatter",
    "stage_mcp_skills",
]
