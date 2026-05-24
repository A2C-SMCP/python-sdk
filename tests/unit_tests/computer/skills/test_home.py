# -*- coding: utf-8 -*-
# filename: test_home.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL Home 解析 / 隔离 / 布局单元测试（v0.2.1）
Unit tests for SKILL Home resolution / isolation / layout (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §4 / §9.2。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §2.3。

测试意图 / Test intentions:
- 解析优先级：A2C_SKILL_HOME > $XDG_DATA_HOME/a2c/skills > ~/.a2c/skills
- XDG_DATA_HOME 相对值按 XDG 规范忽略；A2C_SKILL_HOME 空白忽略；~ 展开
- 隔离铁律 fail-fast：系统 / 跨用户共享目录（含 macOS /private 别名）→ SkillHomeIsolationError
- <source>/<...>/<skill>/ 布局助手
- resolve 不落盘；ensure_skill_home mkdir -p
"""

from pathlib import Path

import pytest

from a2c_smcp.computer.skills.home import (
    SOURCE_MARKETPLACE,
    SOURCE_MCP,
    SOURCE_USER,
    SkillHomeIsolationError,
    ensure_skill_home,
    marketplace_skill_dir,
    mcp_skill_dir,
    resolve_skill_home,
    user_skill_dir,
    validate_skill_home_isolation,
)


# ---------------------------------------------------------------------------
# 解析优先级 / resolution priority
# ---------------------------------------------------------------------------
def test_env_override_wins(tmp_path: Path) -> None:
    custom = tmp_path / "custom-home"
    env = {"A2C_SKILL_HOME": str(custom), "XDG_DATA_HOME": str(tmp_path / "xdg")}
    assert resolve_skill_home(env) == custom.resolve()


def test_xdg_used_when_no_override(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    env = {"XDG_DATA_HOME": str(xdg)}
    assert resolve_skill_home(env) == (xdg / "a2c" / "skills").resolve()


def test_xdg_relative_ignored_falls_back_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # XDG_DATA_HOME 为相对路径 → 按 XDG 规范视为未设置 → 回退 ~/.a2c/skills
    env = {"XDG_DATA_HOME": "relative/data"}
    assert resolve_skill_home(env) == (tmp_path / ".a2c" / "skills").resolve()


def test_fallback_to_dotdir_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_skill_home({}) == (tmp_path / ".a2c" / "skills").resolve()


def test_override_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    env = {"A2C_SKILL_HOME": "~/my-skills"}
    assert resolve_skill_home(env) == (tmp_path / "my-skills").resolve()


def test_blank_override_ignored(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    env = {"A2C_SKILL_HOME": "   ", "XDG_DATA_HOME": str(xdg)}
    assert resolve_skill_home(env) == (xdg / "a2c" / "skills").resolve()


def test_resolve_does_not_create_directory(tmp_path: Path) -> None:
    target = tmp_path / "not-created-yet"
    resolve_skill_home({"A2C_SKILL_HOME": str(target)})
    assert not target.exists()


# ---------------------------------------------------------------------------
# 隔离铁律 fail-fast / isolation invariant
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "system_path",
    [
        "/var/lib/a2c-skills",  # 协议明示反例
        "/usr/share/a2c/skills",
        "/etc/a2c",
        "/opt/a2c/skills",
        "/private/var/lib/a2c-skills",  # macOS /private 别名等价 /var/lib
        "/private",  # macOS /private 是 / 的别名 → 剥离后即根，必须拦截（根判定须在剥离后）
        "/",  # 文件系统根
    ],
)
def test_validate_rejects_system_dirs(system_path: str) -> None:
    with pytest.raises(SkillHomeIsolationError):
        validate_skill_home_isolation(Path(system_path))


def test_validate_accepts_user_dir(tmp_path: Path) -> None:
    # tmp_path（linux /tmp/... 或 macOS /private/var/folders/...）非系统共享目录 → 通过
    validate_skill_home_isolation(tmp_path.resolve())


def test_resolve_rejects_system_override() -> None:
    with pytest.raises(SkillHomeIsolationError):
        resolve_skill_home({"A2C_SKILL_HOME": "/var/lib/a2c-skills"})


# ---------------------------------------------------------------------------
# 布局助手 / layout helpers
# ---------------------------------------------------------------------------
def test_layout_helpers() -> None:
    home = Path("/home/u/.a2c/skills")
    assert mcp_skill_dir(home, "tfrobot-tools", "code-review") == home / SOURCE_MCP / "tfrobot-tools" / "code-review"
    assert user_skill_dir(home, "my-helper") == home / SOURCE_USER / "my-helper"
    assert (
        marketplace_skill_dir(home, "acme-skills", "acme-audit", "audit")
        == home / SOURCE_MARKETPLACE / "acme-skills" / "acme-audit" / "audit"
    )


# ---------------------------------------------------------------------------
# ensure_skill_home
# ---------------------------------------------------------------------------
def test_ensure_skill_home_creates_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "skill-home"
    home = ensure_skill_home({"A2C_SKILL_HOME": str(target)})
    assert home == target.resolve()
    assert home.is_dir()
