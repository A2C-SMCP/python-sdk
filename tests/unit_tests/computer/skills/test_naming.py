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
- MCP <server> 段规范化（skill.md §1.3 表格，不实现 claude.ai 特例）
- 三源合成 + 规范化越界判废 + parse/synthesize 往返一致
"""

import pytest

from a2c_smcp.computer.skills.naming import (
    MAX_SEGMENT_LEN,
    ParsedSkillName,
    SkillNameError,
    is_valid_skill_name,
    normalize_mcp_server_segment,
    parse_skill_name,
    synthesize_marketplace_name,
    synthesize_mcp_name,
    synthesize_user_name,
)


# ---------------------------------------------------------------------------
# normalize_mcp_server_segment —— skill.md §1.3 规范化表格
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tfrobot-tools", "tfrobot-tools"),  # 合规字符原样
        ("my.api server", "my_api_server"),  # 点 + 空格 → _
        ("My_Server", "My_Server"),  # 大小写 + 下划线保留
        ("服务器", "___"),  # 3 个非 ASCII → 3 个 _
        ("claude.ai my.server", "claude_ai_my_server"),  # 不做 claude.ai 特例折叠
    ],
)
def test_normalize_mcp_server_segment(raw: str, expected: str) -> None:
    assert normalize_mcp_server_segment(raw) == expected


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
        "mcp::leaf",  # mcp server 段空
        "mcp:srv:Leaf",  # mcp leaf 大写
        "mcp:bad server:x",  # mcp server 含空格（规范化前的非法字符集）
        "mcp:bad/srv:x",  # mcp server 含 /
    ],
)
def test_parse_invalid_raises_4016(name: str) -> None:
    with pytest.raises(SkillNameError) as excinfo:
        parse_skill_name(name)
    assert excinfo.value.name == name
    assert is_valid_skill_name(name) is False


def test_segment_length_boundary() -> None:
    """各段 1–64：64 字符合法，65 字符非法（skill.md §1.4）。"""
    ok = "a" * MAX_SEGMENT_LEN
    too_long = "a" * (MAX_SEGMENT_LEN + 1)
    assert parse_skill_name(ok).skill == ok
    with pytest.raises(SkillNameError):
        parse_skill_name(too_long)


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


def test_synthesize_mcp_name_normalizes_server() -> None:
    assert synthesize_mcp_name("tfrobot-tools", "code-review") == "mcp:tfrobot-tools:code-review"
    # server 含点/空格 → 规范化后嵌入
    assert synthesize_mcp_name("my.api", "csv-aggregator") == "mcp:my_api:csv-aggregator"


@pytest.mark.parametrize(
    ("server", "skill"),
    [
        ("", "code-review"),  # 规范化后长度 0 → 判废（skill.md §1.5）
        ("a" * (MAX_SEGMENT_LEN + 1), "code-review"),  # 规范化后 > 64 → 判废
        ("tfrobot-tools", "Bad-Leaf"),  # leaf 非严格 kebab
    ],
)
def test_synthesize_mcp_name_rejects(server: str, skill: str) -> None:
    with pytest.raises(SkillNameError):
        synthesize_mcp_name(server, skill)


def test_parse_synthesize_roundtrip() -> None:
    """合成 → 解析往返一致（规范化后的 server 段被 lexer 接受）。"""
    name = synthesize_mcp_name("my.api", "x")
    parsed = parse_skill_name(name)
    assert parsed.kind == "mcp"
    assert parsed.server == "my_api"
    assert parsed.skill == "x"

    mp = synthesize_marketplace_name("acme-audit", "audit")
    assert parse_skill_name(mp) == ParsedSkillName(raw=mp, kind="marketplace", skill="audit", plugin="acme-audit")
