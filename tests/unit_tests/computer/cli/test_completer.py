# -*- coding: utf-8 -*-
# filename: test_completer.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``A2CCompleter`` Tab 补全单元测试（v0.2.1 #68）/ Completer unit tests。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §10.3。

测试意图 / Test intentions:
- 行首 → namespace + 顶层命令词（含 plugin/settings 骨架）；
- namespace 后 → 子命令词；命令后 ``--`` → flag 集；
- 动态名：marketplace info → known_marketplaces 名；skill info → 当前 skill 名。
"""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.document import Document

from a2c_smcp.computer.cli.completer import A2CCompleter
from a2c_smcp.computer.settings.store import save_known_marketplaces


class _FakeComp:
    def __init__(self, home: Path) -> None:
        self.skill_home = home

    def get_skills(self) -> list[dict]:
        return [{"name": "audit:lint"}, {"name": "mcp:blender:render"}]


def _complete(completer: A2CCompleter, text: str) -> list[str]:
    return [c.text for c in completer.get_completions(Document(text, len(text)), None)]


def _comp(tmp_path: Path) -> A2CCompleter:
    save_known_marketplaces({"marketplaces": {"acme": {"source": {"type": "git", "url": "u"}, "installLocation": "x"}}}, home=tmp_path)
    return A2CCompleter(_FakeComp(tmp_path))


def test_root_namespaces(tmp_path: Path) -> None:
    out = _complete(_comp(tmp_path), "")
    for ns in ("marketplace", "skill", "plugin", "settings", "server"):
        assert ns in out


def test_root_prefix(tmp_path: Path) -> None:
    assert _complete(_comp(tmp_path), "mar") == ["marketplace"]


def test_subcommands(tmp_path: Path) -> None:
    out = _complete(_comp(tmp_path), "marketplace ")
    assert {"add", "list", "info", "remove", "refresh", "set"} <= set(out)
    assert _complete(_comp(tmp_path), "marketplace ad") == ["add"]


def test_flags(tmp_path: Path) -> None:
    out = _complete(_comp(tmp_path), "marketplace add --")
    assert {"--name", "--trust", "--auto-update", "--no-clone", "--json"} <= set(out)


def test_dynamic_marketplace_name(tmp_path: Path) -> None:
    assert "acme" in _complete(_comp(tmp_path), "marketplace info ")


def test_dynamic_skill_name(tmp_path: Path) -> None:
    out = _complete(_comp(tmp_path), "skill info ")
    assert "audit:lint" in out and "mcp:blender:render" in out


def test_plugin_subcommands_scaffold(tmp_path: Path) -> None:
    # plugin namespace 子命令词作骨架先行补全（命令体由 #69 落地）
    assert {"install", "enable", "disable"} <= set(_complete(_comp(tmp_path), "plugin "))
