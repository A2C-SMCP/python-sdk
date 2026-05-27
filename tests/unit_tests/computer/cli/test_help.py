# -*- coding: utf-8 -*-
# filename: test_help.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
分组 help 渲染单元测试（v0.2.1 #68）/ Grouped help rendering unit tests（§10.2）。
"""

from __future__ import annotations

import pytest

from a2c_smcp.computer.cli.help import render_help


def test_help_lists_namespaces(capsys: pytest.CaptureFixture[str]) -> None:
    render_help()
    out = capsys.readouterr().out
    for ns in ("server", "marketplace", "plugin", "skill", "settings"):
        assert ns in out


def test_help_namespace_detail(capsys: pytest.CaptureFixture[str]) -> None:
    render_help("marketplace")
    out = capsys.readouterr().out
    assert "marketplace" in out  # 标题 "marketplace commands"


def test_help_unknown_namespace(capsys: pytest.CaptureFixture[str]) -> None:
    render_help("bogus")
    assert "unknown" in capsys.readouterr().out.lower()


def test_help_plugin_detail_real_commands(capsys: pytest.CaptureFixture[str]) -> None:
    render_help("plugin")
    out = capsys.readouterr().out
    # #69 落地：真命令行（非骨架占位），含 install/enable/gc
    for verb in ("install", "enable", "gc"):
        assert verb in out
    assert "#69" not in out  # 骨架占位已替换


def test_help_settings_detail_real_commands(capsys: pytest.CaptureFixture[str]) -> None:
    render_help("settings")
    out = capsys.readouterr().out
    for verb in ("show", "get", "set", "edit"):
        assert verb in out
    assert "#69" not in out
