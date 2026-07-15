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
    - mcp ``<server>`` 段 **= server 的 ``bundle_id`` 原样**（``[A-Za-z0-9_-]``、无 ``.``、无连续 ``__``、
      大小写保留、**无长度上限**）；见 §1.3 与 :mod:`a2c_smcp.utils.bundle_id`。
      The mcp ``<server>`` segment is the server's ``bundle_id`` verbatim (no length cap).
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

# mcp <server> 段（= bundle_id）字符集（大小写保留）/ mcp <server> (= bundle_id) charset (case preserved).
_MCP_SERVER_SEG_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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
    ``server`` 仅 mcp，其值 = 该 server 的 ``bundle_id``）。Agent 仍 **MUST** 把 name 当不透明字符串
    ——本结构仅供 Computer 内部（Registry / staging）使用。
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
    """mcp ``<server>`` 段（= bundle_id）是否合规 / Whether the mcp ``<server>`` (= bundle_id) segment is valid.

    判据即 BundleID 字符集（skill.md §1.3 / data-structures.md §BundleID）：非空 + ``[A-Za-z0-9_-]``
    + 无连续 ``__``（``__`` 是 BundleID 与工具名的保留分隔符）。

    **无长度上限**：§1.4 的「1–64」随「``<server>`` = bundle_id」删除，§1.5 亦删掉「长度 > 64 → 判废」
    一行——BundleID 规范不设长度上限，§1.3 断言 bundle_id「是 lexer 字符集的严格子集，**直接合法**」。
    卡 64 会让 name 超长的 server 其 SKILL 对 Agent 隐身，即本模块要消灭的失效模式（#142）。
    No length cap: the bound was dropped from §1.4/§1.5 when ``<server>`` became ``bundle_id``.
    """
    return bool(segment) and "__" not in segment and _MCP_SERVER_SEG_RE.match(segment) is not None


def parse_skill_name(name: str) -> ParsedSkillName:
    """
    SKILL name lexer：段数消歧 + 逐段字符集校验 / Lexer: segment-count disambiguation + per-segment charset.

    协议依据 skill.md §1.4 消歧规则：
    Protocol skill.md §1.4 disambiguation:

    - 段数 ∉ {1, 2, 3} → 非法 / segment count ∉ {1, 2, 3} → invalid
    - 1 段 → user（缺 ``:`` 的裸名**合法**，不得因缺 ``:`` 报错）/ 1 seg → user (bare name accepted)
    - 2 段 → marketplace ``<plugin>:<skill>``
    - 3 段 → mcp，首段 **MUST** 字面 ``mcp``；``<server>`` 段 = bundle_id 字符集（§1.3，无长度上限）
      / 3 seg → mcp, first segment MUST be literal ``mcp``; ``<server>`` follows the bundle_id charset
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
            raise SkillNameError(name, "mcp <server> segment must be a valid bundle_id: [A-Za-z0-9_-], no '__'")
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


def synthesize_mcp_name(bundle_id: str, skill: str) -> str:
    """
    合成 mcp 源 name / Synthesize an mcp-source name：``mcp:<bundle_id>:<skill>``（3 段）。

    ``bundle_id`` **原样**进段、不做任何规范化（skill.md §1.3）——它已是 A2C server 的唯一身份，
    由 :func:`a2c_smcp.utils.bundle_id.resolve_bundle_id` 在 Computer 注册边界解析后恒有值、恒合法。
    取 bundle_id（而非可碰撞的 display ``name``）令 mcp 形态 name **构造上不碰撞**：no-double-open
    保证同一 Computer 内 bundle_id 唯一，故不再有「两个合法 Server 撞名 → 拒绝其一 → SKILL 隐身」。
    ``bundle_id`` goes in verbatim (no normalization); its uniqueness makes mcp names collision-free
    by construction.

    此处校验是**防御性**的（正常链路传入的 bundle_id 恒合法）：非法即调用方 bug——
    按 skill.md §1.5 判废（不入册、记 ERROR），而非静默产出畸形 name。

    :raises SkillNameError: ``bundle_id`` 不符 BundleID 字符集，或 ``skill`` 非严格 kebab（→ 不入册）。
    """
    if not _is_valid_mcp_server_segment(bundle_id):
        raise SkillNameError(
            f"{MCP_SEGMENT}{SEPARATOR}{bundle_id}{SEPARATOR}{skill}",
            "mcp <server> must be a valid bundle_id: non-empty, [A-Za-z0-9_-], no consecutive '__'",
        )
    if not _is_strict_kebab(skill):
        raise SkillNameError(
            f"{MCP_SEGMENT}{SEPARATOR}{bundle_id}{SEPARATOR}{skill}",
            "mcp <skill> leaf must be strict kebab",
        )
    return f"{MCP_SEGMENT}{SEPARATOR}{bundle_id}{SEPARATOR}{skill}"
