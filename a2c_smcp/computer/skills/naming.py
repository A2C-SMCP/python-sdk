# -*- coding: utf-8 -*-
# filename: naming.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 命名合成与 lexer（v0.2.1 协议，裸名模型）
SKILL name synthesis & lexer (v0.2.1 protocol, bare-name model)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §1（命名）；
                      docs/specification/error-handling.md §4016（Invalid Skill Name）。

A2C-SMCP 用**全局唯一的合成 name** 作为协议主键。自 0.2.1 起 name **跨工具对齐裸名**
（放弃旧版「强制前缀化」），按 source 分三形态、靠**段数**消歧：
A2C-SMCP uses a globally-unique synthesized name as the protocol primary key. Since 0.2.1
names align to bare cross-tool form (the old forced-prefix model is dropped); three shapes
by source, disambiguated by **segment count**:

============== =========================== =====
Source         name 形态 / shape            段数
============== =========================== =====
user           ``<skill>``                  1
marketplace    ``<plugin>:<skill>``         2
mcp            ``mcp:<server>:<skill>``     3
============== =========================== =====

- ``:`` 是协议层 reserved separator；mcp 是唯一保留字面首段前缀的 source（与 2 段 marketplace 区分）。
  ``:`` is the reserved separator; mcp is the only source retaining a literal leading prefix.
- 字符集 / charsets（skill.md §1.4）：
    - ``<skill>`` / ``<plugin>`` 段为**严格 kebab**（``[a-z0-9-]``，不以 ``-`` 始末、无连续 ``--``、长 1–64）。
      strict kebab leaf segments.
    - mcp ``<server>`` 段为规范化字符集 ``[A-Za-z0-9_-]``（大小写保留，长 1–64）；见 §1.3。
      normalized mcp server charset (case preserved).
- 非法 name → :class:`SkillNameError`，由 ``client:get_skill`` 处理器映射为协议 ``4016``；
  装配 Registry 时合成失败的 SKILL 不入册（记 ERROR，不向 Agent 硬报错，skill.md §1.5）。
  Illegal name raises :class:`SkillNameError`, mapped to protocol ``4016`` by the
  ``client:get_skill`` handler; on Registry assembly a SKILL whose synthesis fails is skipped
  (logged ERROR, no hard error to the Agent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# 段最大长度（skill.md §1.4：各段 1–64）/ Max per-segment length (skill.md §1.4: 1–64).
MAX_SEGMENT_LEN = 64

# 协议层 reserved separator / Protocol-reserved separator.
SEPARATOR = ":"

# mcp source 专属字面首段 / Literal leading segment reserved for the mcp source.
MCP_SEGMENT = "mcp"

# 严格 kebab（leaf / plugin 段）：小写 alnum，单连字符分隔，无首尾/连续连字符。
# Strict kebab (leaf / plugin segments): lowercase alnum, single-hyphen separated.
_STRICT_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# mcp <server> 段规范化字符集（大小写保留）/ Normalized mcp <server> charset (case preserved).
_MCP_SERVER_SEG_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 规范化替换：非 [A-Za-z0-9_-] → "_" / Normalization: non-[A-Za-z0-9_-] → "_".
_MCP_NORMALIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")

SkillNameKind = Literal["user", "marketplace", "mcp"]


class SkillNameError(ValueError):
    """
    SKILL name 格式非法 / SKILL name is malformed.

    映射协议 ``4016 Invalid Skill Name``（error-handling.md §4016，``details.name`` 透传非法 name）。
    Maps to protocol ``4016 Invalid Skill Name`` (``details.name`` carries the offending name).

    本异常**不**自带协议码常量（保持 naming 模块对 ``a2c_smcp.smcp`` 零依赖）；由调用方
    （``client:get_skill`` 处理器 / Registry 装配）按需映射 / 吞掉。
    This exception does not embed the protocol-code constant (keeps naming dependency-free of
    ``a2c_smcp.smcp``); callers map / swallow it as appropriate.
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"invalid skill name {name!r}: {reason}")


@dataclass(frozen=True, slots=True)
class ParsedSkillName:
    """
    lexer 解析结果 / Result of the name lexer.

    ``kind`` 标明 source 形态；按形态填充对应段（``skill`` 恒有；``plugin`` 仅 marketplace；
    ``server`` 仅 mcp）。Agent 仍 **MUST** 把 name 当不透明字符串——本结构仅供 Computer 内部
    （Registry / staging）使用。
    ``kind`` identifies the source shape; segments are populated accordingly (``skill`` always
    present; ``plugin`` marketplace-only; ``server`` mcp-only). For Computer-internal use only.
    """

    raw: str
    kind: SkillNameKind
    skill: str
    plugin: str | None = None
    server: str | None = None


def _is_strict_kebab(segment: str) -> bool:
    """段是否为严格 kebab 且长度合规 / Whether segment is strict kebab within length bounds."""
    return len(segment) <= MAX_SEGMENT_LEN and _STRICT_KEBAB_RE.match(segment) is not None


def _is_valid_mcp_server_segment(segment: str) -> bool:
    """mcp <server> 段是否合规 / Whether the mcp <server> segment is valid."""
    return 1 <= len(segment) <= MAX_SEGMENT_LEN and _MCP_SERVER_SEG_RE.match(segment) is not None


def normalize_mcp_server_segment(name: str) -> str:
    """
    MCP <server> 段规范化 / Normalize the MCP <server> segment.

    与 Claude Code ``normalizeNameForMCP()`` 通用规则等价：非 ``[A-Za-z0-9_-]`` → ``_``，
    大小写保留、不截断。**不**实现 Claude Code 的 ``"claude.ai "`` 前缀折叠 + trim 特例
    （Anthropic 平台特化，违反 A2C 协议中立性）。协议依据 skill.md §1.3。
    Equivalent to Claude Code ``normalizeNameForMCP()`` general rule (non-[A-Za-z0-9_-] → ``_``,
    case preserved, no truncation); does NOT implement the ``"claude.ai "`` prefix special case.

    注意 / Note：本函数仅做字符替换，**不**校验结果长度。空串 / 超长由 :func:`synthesize_mcp_name`
    在合成时按 skill.md §1.5 判废。
    Pure character substitution; emptiness / over-length are rejected by
    :func:`synthesize_mcp_name` at synthesis time per skill.md §1.5.
    """
    return _MCP_NORMALIZE_RE.sub("_", name)


def parse_skill_name(name: str) -> ParsedSkillName:
    """
    SKILL name lexer：段数消歧 + 逐段字符集校验 / Lexer: segment-count disambiguation + per-segment charset.

    协议依据 skill.md §1.4 消歧规则：
    Protocol skill.md §1.4 disambiguation:

    - 段数 ∉ {1, 2, 3} → 非法 / segment count ∉ {1, 2, 3} → invalid
    - 1 段 → user（缺 ``:`` 的裸名**合法**，不得因缺 ``:`` 报错）/ 1 seg → user (bare name accepted)
    - 2 段 → marketplace ``<plugin>:<skill>``
    - 3 段 → mcp，首段 **MUST** 字面 ``mcp`` / 3 seg → mcp, first segment MUST be literal ``mcp``
    - 任一段不符字符集 → 非法 / any segment failing its charset → invalid

    :raises SkillNameError: name 格式非法（映射协议 4016）/ malformed name (maps to protocol 4016).
    """
    segments = name.split(SEPARATOR)
    count = len(segments)

    if count == 1:
        skill = segments[0]
        if not _is_strict_kebab(skill):
            raise SkillNameError(name, "user name must be a strict-kebab 1-segment bare name")
        return ParsedSkillName(raw=name, kind="user", skill=skill)

    if count == 2:
        plugin, skill = segments
        if not _is_strict_kebab(plugin) or not _is_strict_kebab(skill):
            raise SkillNameError(name, "marketplace name must be <plugin>:<skill> with strict-kebab segments")
        return ParsedSkillName(raw=name, kind="marketplace", skill=skill, plugin=plugin)

    if count == 3:
        head, server, skill = segments
        if head != MCP_SEGMENT:
            raise SkillNameError(name, "3-segment names are reserved for the mcp source (first segment must be 'mcp')")
        if not _is_valid_mcp_server_segment(server):
            raise SkillNameError(name, "mcp <server> segment must match [A-Za-z0-9_-]{1,64}")
        if not _is_strict_kebab(skill):
            raise SkillNameError(name, "mcp <skill> leaf must be strict kebab")
        return ParsedSkillName(raw=name, kind="mcp", skill=skill, server=server)

    raise SkillNameError(name, f"segment count {count} ∉ {{1, 2, 3}}")


def is_valid_skill_name(name: str) -> bool:
    """非抛出版校验 / Non-raising validity check（便于 ``client:get_skill`` 入参快速门控）。"""
    try:
        parse_skill_name(name)
    except SkillNameError:
        return False
    return True


def synthesize_user_name(skill: str) -> str:
    """
    合成 user 源裸名 / Synthesize a bare user-source name：``<skill>``（1 段）。

    :raises SkillNameError: ``skill`` 非严格 kebab（skill.md §1.5 → 不入册）。
    """
    if not _is_strict_kebab(skill):
        raise SkillNameError(skill, "user <skill> must be strict kebab")
    return skill


def synthesize_marketplace_name(plugin: str, skill: str) -> str:
    """
    合成 marketplace 源裸名 / Synthesize a bare marketplace-source name：``<plugin>:<skill>``（2 段）。

    marketplace 名**不进** name（由 ``A2CSkillRef.source = marketplace:<repo>`` 承载溯源）；
    跨 marketplace 同名 ``<plugin>`` 的冲突在安装层 ``<plugin>@<marketplace>`` 拦截（skill.md §1.2）。
    The marketplace name is NOT part of ``name`` (provenance lives in ``source``).

    :raises SkillNameError: ``plugin`` / ``skill`` 非严格 kebab（skill.md §1.5 → 不入册）。
    """
    if not _is_strict_kebab(plugin):
        raise SkillNameError(f"{plugin}{SEPARATOR}{skill}", "marketplace <plugin> must be strict kebab")
    if not _is_strict_kebab(skill):
        raise SkillNameError(f"{plugin}{SEPARATOR}{skill}", "marketplace <skill> must be strict kebab")
    return f"{plugin}{SEPARATOR}{skill}"


def synthesize_mcp_name(server: str, skill: str) -> str:
    """
    合成 mcp 源 name / Synthesize an mcp-source name：``mcp:<normalized-server>:<skill>``（3 段）。

    ``server`` 先经 :func:`normalize_mcp_server_segment` 规范化；规范化后长度 = 0 或 > 64 →
    判废（skill.md §1.5：拒绝该 server 全部 SKILL 注册，记 ERROR）。
    ``server`` is normalized first; a normalized length of 0 or > 64 is rejected (skill.md §1.5).

    :raises SkillNameError: 规范化 server 越界或 ``skill`` 非严格 kebab（→ 不入册）。
    """
    normalized = normalize_mcp_server_segment(server)
    if not _is_valid_mcp_server_segment(normalized):
        raise SkillNameError(
            f"{MCP_SEGMENT}{SEPARATOR}{normalized}{SEPARATOR}{skill}",
            "normalized mcp <server> must match [A-Za-z0-9_-]{1,64} (empty or >64 rejected)",
        )
    if not _is_strict_kebab(skill):
        raise SkillNameError(
            f"{MCP_SEGMENT}{SEPARATOR}{normalized}{SEPARATOR}{skill}",
            "mcp <skill> leaf must be strict kebab",
        )
    return f"{MCP_SEGMENT}{SEPARATOR}{normalized}{SEPARATOR}{skill}"
