# -*- coding: utf-8 -*-
# filename: test_banner.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
启动 banner 单元测试（v0.2.1 #68）/ Startup banner unit tests（§10.1）。

zero-state（0 plugins AND 0 servers）→ 引导框；否则一行摘要。
"""

from __future__ import annotations

import pytest

from a2c_smcp.computer.cli.banner import render_banner


class _Comp:
    def __init__(self, servers: int) -> None:
        self.mcp_servers = {object() for _ in range(servers)}


def test_zero_state(capsys: pytest.CaptureFixture[str]) -> None:
    render_banner(_Comp(0), None, None)
    out = capsys.readouterr().out
    assert "0 plugins" in out and "marketplace add" in out  # 引导框含 next steps


def test_non_zero_one_liner(capsys: pytest.CaptureFixture[str]) -> None:
    render_banner(_Comp(2), None, None)
    out = capsys.readouterr().out
    assert "2 servers" in out and "Next steps" not in out
