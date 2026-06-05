# -*- coding: utf-8 -*-
# filename: test_skill.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``skill`` 命令 handler 单元测试（v0.2.1 #68）/ Skill command handler unit tests。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.4。

测试意图 / Test intentions（预置 SkillRegistry 注册三源 + 一个孤儿，不触网）:
- list 跨源扁平：name / source / enabled / orphan；``--source mp|mcp|user`` 过滤；
- info：命中（含孤儿）/ 未命中；``--json`` 形态。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2c_smcp.computer.cli.commands.skill import skill_info, skill_list
from a2c_smcp.computer.skills.registry import SkillRegistry


def _ref(name: str, source: str, root: Path) -> dict:
    return {"name": name, "source": source, "path": str(root), "description": f"{name} desc"}


def _registry(tmp_path: Path) -> SkillRegistry:
    reg = SkillRegistry()
    assert reg.register(_ref("my-skill", "user", tmp_path / "u"))  # user 1 段
    assert reg.register(_ref("audit:lint", "marketplace:acme", tmp_path / "m"))  # marketplace 2 段
    assert reg.register(_ref("mcp:blender:render", "mcp:blender", tmp_path / "x"))  # mcp 3 段
    assert reg.register(_ref("audit:gone", "marketplace:acme", tmp_path / "g"))
    reg.mark_orphan("audit:gone")  # 孤儿（从 active 排除，但 skill list 仍展示并标记）
    return reg


def test_list_all_sources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reg = _registry(tmp_path)
    assert skill_list(reg, json_output=True) == 0
    data = json.loads(capsys.readouterr().out)
    by_name = {r["name"]: r for r in data}
    assert set(by_name) == {"my-skill", "audit:lint", "mcp:blender:render", "audit:gone"}
    assert by_name["audit:gone"]["orphan"] is True and by_name["audit:gone"]["enabled"] is False
    assert by_name["my-skill"]["orphan"] is False


def test_list_source_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reg = _registry(tmp_path)
    skill_list(reg, source="mp", json_output=True)
    mp = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in mp} == {"audit:lint", "audit:gone"}

    skill_list(reg, source="mcp", json_output=True)
    mcp = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in mcp} == {"mcp:blender:render"}

    skill_list(reg, source="user", json_output=True)
    user = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in user} == {"my-skill"}


def test_list_unknown_source(tmp_path: Path) -> None:
    assert skill_list(_registry(tmp_path), source="bogus") == 1


def test_info_found_and_orphan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reg = _registry(tmp_path)
    assert skill_info(reg, "audit:lint", json_output=True) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["source"] == "marketplace:acme" and found["enabled"] is True
    # 孤儿也可 info（all_refs 含孤儿）
    assert skill_info(reg, "audit:gone", json_output=True) == 0
    orphan = json.loads(capsys.readouterr().out)
    assert orphan["orphan"] is True


def test_info_not_found(tmp_path: Path) -> None:
    assert skill_info(_registry(tmp_path), "no-such-skill") == 1
