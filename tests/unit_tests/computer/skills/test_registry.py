# -*- coding: utf-8 -*-
# filename: test_registry.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SkillRegistry 单元测试（v0.2.1）/ Unit tests for SkillRegistry (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §1.5 / §6 / §8 / §9.2。

测试意图 / Test intentions:
- 注册 / O(1) 查找 / active 集合（排除孤儿）
- 孤儿标记 → 从 active + resolve 排除；恢复 → 重新纳入；同名孤儿再注册 = 恢复并替换
- 校验失败不入册且**不抛**（仅 ERROR 日志）：非法 name / 缺 path / 相对 path / 同名 active 冲突
- A2CSkillRef 含包根绝对路径（name 寻址唯一来源）
"""

import logging
from collections.abc import Iterator

import pytest

import a2c_smcp.computer.skills.registry as registry_mod
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.smcp import A2CSkillRef


def _ref(name: str, *, path: str | None = None, source: str = "user", description: str = "d") -> A2CSkillRef:
    """构造最小可注册 A2CSkillRef / Build a minimal registrable A2CSkillRef。"""
    ref: A2CSkillRef = {"name": name, "source": source, "description": description}
    if path is not None:
        ref["path"] = path
    elif name is not None:
        ref["path"] = f"/abs/skills/{name}"
    return ref


@pytest.fixture
def capture_errors(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """捕获 registry 模块 ERROR（项目 logger 关闭 propagate，直接挂 handler）。"""
    registry_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR)
    try:
        yield caplog
    finally:
        registry_mod.logger.removeHandler(caplog.handler)


# ---------------------------------------------------------------------------
# 注册 / 查找
# ---------------------------------------------------------------------------
def test_register_and_resolve() -> None:
    reg = SkillRegistry()
    ref = _ref("my-helper")
    assert reg.register(ref) is True
    assert reg.resolve("my-helper") == ref
    assert reg.active_refs() == [ref]
    assert "my-helper" in reg
    assert reg.is_orphan("my-helper") is False
    assert len(reg) == 1


def test_resolve_missing_returns_none() -> None:
    reg = SkillRegistry()
    assert reg.resolve("absent") is None
    assert "absent" not in reg


def test_multiple_sources_active() -> None:
    reg = SkillRegistry()
    assert reg.register(_ref("my-helper", source="user")) is True
    assert reg.register(_ref("acme-audit:audit", source="marketplace:acme-skills")) is True
    assert reg.register(_ref("mcp:tfrobot-tools:code-review", source="mcp:tfrobot-tools")) is True
    assert len(reg.active_refs()) == 3
    assert len(reg) == 3


# ---------------------------------------------------------------------------
# 校验失败不入册且不抛（仅 ERROR）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("ref", "case"),
    [
        (_ref("Bad Name"), "invalid-name-lexer"),
        ({"name": "no-path", "source": "user", "description": "d"}, "missing-path"),
        (_ref("rel-path", path="relative/dir"), "relative-path"),
        ({"source": "user", "path": "/abs/x", "description": "d"}, "missing-name"),
    ],
)
def test_validation_failure_not_registered_and_not_raised(ref: A2CSkillRef, case: str) -> None:
    reg = SkillRegistry()
    assert reg.register(ref) is False, case  # 不抛、返回 False
    assert len(reg) == 0, case  # 不入册


def test_validation_failure_logs_error(capture_errors: pytest.LogCaptureFixture) -> None:
    reg = SkillRegistry()
    reg.register(_ref("Bad Name"))
    assert any(r.levelno == logging.ERROR for r in capture_errors.records)


def test_duplicate_active_rejected_keeps_first() -> None:
    reg = SkillRegistry()
    first = _ref("my-helper", path="/abs/first")
    second = _ref("my-helper", path="/abs/second")
    assert reg.register(first) is True
    assert reg.register(second) is False  # 拒绝第二注册者
    assert reg.resolve("my-helper") == first  # 保留先到者
    assert len(reg) == 1


# ---------------------------------------------------------------------------
# 孤儿标记 / 恢复
# ---------------------------------------------------------------------------
def test_mark_orphan_excludes_from_active() -> None:
    reg = SkillRegistry()
    reg.register(_ref("my-helper"))
    assert reg.mark_orphan("my-helper") is True
    assert reg.resolve("my-helper") is None  # 对 get_skill/get_blob 等同不存在
    assert reg.active_refs() == []  # get_skills 排除孤儿
    assert reg.is_orphan("my-helper") is True
    assert "my-helper" in reg  # 仍保留以便恢复
    assert len(reg) == 1


def test_recover_orphan_reinstates() -> None:
    reg = SkillRegistry()
    ref = _ref("my-helper")
    reg.register(ref)
    reg.mark_orphan("my-helper")
    assert reg.recover("my-helper") is True
    assert reg.resolve("my-helper") == ref
    assert reg.is_orphan("my-helper") is False
    # 重复恢复（已活跃）/ 恢复不存在者 → False
    assert reg.recover("my-helper") is False
    assert reg.recover("absent") is False


def test_reregister_orphaned_recovers_with_new_ref() -> None:
    reg = SkillRegistry()
    reg.register(_ref("my-helper", path="/abs/old"))
    reg.mark_orphan("my-helper")
    new_ref = _ref("my-helper", path="/abs/new")
    assert reg.register(new_ref) is True  # source 回归 → 恢复并替换
    assert reg.resolve("my-helper") == new_ref
    assert reg.is_orphan("my-helper") is False


def test_mark_orphan_missing_returns_false() -> None:
    reg = SkillRegistry()
    assert reg.mark_orphan("absent") is False


# ---------------------------------------------------------------------------
# 注销
# ---------------------------------------------------------------------------
def test_unregister() -> None:
    reg = SkillRegistry()
    reg.register(_ref("my-helper"))
    assert reg.unregister("my-helper") is True
    assert "my-helper" not in reg
    assert reg.resolve("my-helper") is None
    assert len(reg) == 0
    assert reg.unregister("my-helper") is False  # 已不存在
