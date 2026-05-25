# -*- coding: utf-8 -*-
# filename: test_home.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL Home 解析 / 布局 / 防御性写单元测试（v0.2.1）
Unit tests for SKILL Home resolution / layout / defensive write (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §4 / §9.2。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §2.3。

测试意图 / Test intentions:
- 解析优先级：A2C_SKILL_HOME > $XDG_DATA_HOME/a2c/skills > ~/.a2c/skills
- XDG_DATA_HOME 相对值按 XDG 规范忽略；A2C_SKILL_HOME 空白忽略；~ 展开
- 跨用户隔离对齐 CC：不做 path deny-list（系统目录不再 fail-fast），隔离交给 OS 权限
- ensure_skill_home 以 0o700 防御性创建（POSIX 校验权限位）；resolve 不落盘
- <source>/<...>/<skill>/ 布局助手
"""

import os
from pathlib import Path

import pytest

from a2c_smcp.computer.skills.home import (
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
# 跨用户隔离对齐 CC：不再 path deny-list（系统路径解析不抛错，隔离交给 OS）
# CC-aligned isolation: no path deny-list (resolving a system path no longer raises)
# ---------------------------------------------------------------------------
def test_resolve_no_longer_rejects_system_paths() -> None:
    # Route A：解析层不再对 /var/lib 等系统目录 fail-fast（隔离交给 OS 权限 / 部署层）。
    # 仅校验「解析返回该路径」，不实际创建（避免触碰真实系统目录）。
    assert resolve_skill_home({"A2C_SKILL_HOME": "/var/lib/a2c-skills"}) == Path("/var/lib/a2c-skills").resolve()


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
# ensure_skill_home：创建 + 0o700 防御性写
# ---------------------------------------------------------------------------
def test_ensure_skill_home_creates_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "skill-home"
    home = ensure_skill_home({"A2C_SKILL_HOME": str(target)})
    assert home == target.resolve()
    assert home.is_dir()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 权限位语义；Windows 由 AppData ACL 承担")
def test_ensure_skill_home_is_private_0700(tmp_path: Path) -> None:
    target = tmp_path / "private-home"
    home = ensure_skill_home({"A2C_SKILL_HOME": str(target)})
    mode = home.stat().st_mode & 0o777
    assert mode == SKILL_HOME_MODE  # 0o700：owner rwx，group/other 无权限
