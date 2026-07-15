# -*- coding: utf-8 -*-
# filename: test_naming.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 命名 lexer / 合成单元测试（v0.2.1 裸名模型）
Unit tests for the SKILL name lexer & synthesis (v0.2.1 bare-name model)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §1（命名）/ §4016。

测试意图 / Test intentions:
- 段数消歧：1 段 user / 2 段 marketplace / 3 段 mcp（首段字面 mcp）
- 缺 ``:`` 的 user 裸名**被接受**（不报错）—— 0.2.1 关键回归点
- 非法 name → SkillNameError（映射协议 4016）
- MCP <server> 段 = bundle_id 原样（skill.md §1.3，#142：不再规范化 display 名）
- 三源合成 + 段字符集判废 + parse/synthesize 往返一致
"""

import pytest

from a2c_smcp.computer.skills.naming import (
    MAX_SEGMENT_LEN,
    ParsedSkillName,
    SkillNameError,
    is_valid_skill_name,
    parse_skill_name,
    synthesize_marketplace_name,
    synthesize_mcp_name,
    synthesize_user_name,
)


# ---------------------------------------------------------------------------
# parse_skill_name —— 合法形态消歧
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "kind", "skill", "plugin", "server"),
    [
        ("my-helper", "user", "my-helper", None, None),  # 1 段裸名（缺 : 接受）
        ("a", "user", "a", None, None),  # 单字符 user 名
        ("a1-b2-c3", "user", "a1-b2-c3", None, None),
        ("acme-audit:audit", "marketplace", "audit", "acme-audit", None),  # 2 段
        ("mcp:tfrobot-tools:code-review", "mcp", "code-review", None, "tfrobot-tools"),  # 3 段
        ("mcp:My_Server:lint", "mcp", "lint", None, "My_Server"),  # mcp server 大小写/下划线合法
    ],
)
def test_parse_valid(name: str, kind: str, skill: str, plugin: str | None, server: str | None) -> None:
    parsed = parse_skill_name(name)
    assert parsed == ParsedSkillName(raw=name, kind=kind, skill=skill, plugin=plugin, server=server)
    assert is_valid_skill_name(name) is True


def test_bare_user_name_not_rejected_for_missing_colon() -> None:
    """0.2.1 关键回归：删除了「缺 : → 4016」误判，user 裸名必须接受。"""
    parsed = parse_skill_name("standalone-skill")
    assert parsed.kind == "user"
    assert parsed.skill == "standalone-skill"


# ---------------------------------------------------------------------------
# parse_skill_name —— 非法形态 → SkillNameError(4016)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "",  # 空串（1 段但空 leaf）
        "a:b:c:d",  # 4 段 ∉ {1,2,3}
        "foo:bar:baz",  # 3 段但首段 ≠ mcp
        "MySkill",  # 大写（非严格 kebab）
        "-leading",  # 以 - 始
        "trailing-",  # 以 - 末
        "double--hyphen",  # 连续 --
        "a::b",  # 空中段
        ":x",  # 空首段（2 段 marketplace plugin 空）
        "x:",  # 空尾段
        "plugin:Skill",  # marketplace skill 大写
        "mcp::leaf",  # mcp server 段空（bundle_id 恒非空）
        "mcp:srv:Leaf",  # mcp leaf 大写
        "mcp:bad server:x",  # mcp server 含空格（非 bundle_id 字符集）
        "mcp:bad/srv:x",  # mcp server 含 /
        "mcp:bad.srv:x",  # mcp server 含 .（bundle_id MUST NOT 含 `.`）
        "mcp:a__b:x",  # mcp server 含 __（bundle_id MUST NOT 含连续下划线）
    ],
)
def test_parse_invalid_raises_4016(name: str) -> None:
    with pytest.raises(SkillNameError) as excinfo:
        parse_skill_name(name)
    assert excinfo.value.name == name
    assert is_valid_skill_name(name) is False


def test_segment_length_boundary() -> None:
    """user / marketplace / mcp-leaf 段 1–64：64 字符合法，65 字符非法（skill.md §1.4）。"""
    ok = "a" * MAX_SEGMENT_LEN
    too_long = "a" * (MAX_SEGMENT_LEN + 1)
    assert parse_skill_name(ok).skill == ok
    with pytest.raises(SkillNameError):
        parse_skill_name(too_long)


def test_mcp_server_segment_has_no_length_cap() -> None:
    """mcp ``<server>`` 段（= bundle_id）**无长度上限**（skill.md §1.3/§1.4，#142）。

    §1.4 的「1–64」上限已随「``<server>`` = bundle_id」删除，§1.5 亦删掉「长度 > 64 → 判废」失效路径：
    BundleID 规范本身不设长度上限，且 §1.3 断言 bundle_id「是 lexer 字符集的严格子集，**直接合法**」。
    若此处仍卡 64，name 超 64 字符的 server 其 SKILL 会对 Agent 隐身——正是本 issue 要消灭的失效模式。
    """
    long_bundle_id = "a" * (MAX_SEGMENT_LEN + 16)
    parsed = parse_skill_name(f"mcp:{long_bundle_id}:x")
    assert parsed.kind == "mcp"
    assert parsed.server == long_bundle_id


# ---------------------------------------------------------------------------
# 合成 / synthesis
# ---------------------------------------------------------------------------
def test_synthesize_user_name() -> None:
    assert synthesize_user_name("my-helper") == "my-helper"
    with pytest.raises(SkillNameError):
        synthesize_user_name("My-Helper")


def test_synthesize_marketplace_name() -> None:
    assert synthesize_marketplace_name("acme-audit", "audit") == "acme-audit:audit"
    with pytest.raises(SkillNameError):
        synthesize_marketplace_name("Acme", "audit")
    with pytest.raises(SkillNameError):
        synthesize_marketplace_name("acme", "Audit")


@pytest.mark.parametrize(
    ("bundle_id", "desc"),
    [
        ("tfrobot-tools", "auto-derive 常规 / plain auto-derive"),
        ("My_Server", "大小写 + 下划线保留 / case & underscore preserved"),
        ("bundle_a1b2c3d4e5f60718", "hash fallback"),
        ("a" * (MAX_SEGMENT_LEN + 16), "超 64：协议 §1.4 已删该上限 / no length cap"),
    ],
)
def test_synthesize_mcp_name_takes_bundle_id_verbatim(bundle_id: str, desc: str) -> None:
    """``<server>`` 段 = bundle_id **原样**（skill.md §1.3，#142）——不再规范化。"""
    assert synthesize_mcp_name(bundle_id, "code-review") == f"mcp:{bundle_id}:code-review", desc


@pytest.mark.parametrize(
    ("bundle_id", "skill", "why"),
    [
        ("", "code-review", "空段：bundle_id 恒非空，空值即调用方 bug"),
        ("my.api", "csv-aggregator", "含 `.`：bundle_id 字符集禁 `.`（#142 起不再规范化兜底）"),
        ("my api", "csv-aggregator", "含空格：非 bundle_id 字符集"),
        ("a__b", "code-review", "含连续 `__`：`__` 是 bundle_id 与工具名的保留分隔符"),
        ("tfrobot-tools", "Bad-Leaf", "leaf 非严格 kebab"),
    ],
)
def test_synthesize_mcp_name_rejects(bundle_id: str, skill: str, why: str) -> None:
    with pytest.raises(SkillNameError):
        synthesize_mcp_name(bundle_id, skill)


def test_parse_synthesize_roundtrip() -> None:
    """合成 → 解析往返一致（bundle_id 段被 lexer 原样接受）。"""
    name = synthesize_mcp_name("my_api", "x")
    parsed = parse_skill_name(name)
    assert parsed.kind == "mcp"
    assert parsed.server == "my_api"
    assert parsed.skill == "x"

    mp = synthesize_marketplace_name("acme-audit", "audit")
    assert parse_skill_name(mp) == ParsedSkillName(raw=mp, kind="marketplace", skill="audit", plugin="acme-audit")
