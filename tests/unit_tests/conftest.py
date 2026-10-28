# -*- coding: utf-8 -*-
# filename: conftest.py
# @Time    : 2026/10/28
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
单元测试全局夹具 / Global unit-test fixtures.

v0.5.0（protocol#64）：Computer 稳定身份持久化在 ``$XDG_CONFIG_HOME/a2c/computer-id``（未设置则为
``~/.config/a2c``）。任何触发 ``SMCPComputerClient.connect`` / ``_ensure_computer_id`` 的测试都必须
隔离该目录，否则会读写开发者真实身份文件、并在本机首次运行后留下跨测试共享状态。
Since v0.5.0 the Computer identity is persisted under ``$XDG_CONFIG_HOME``; isolate it per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_a2c_identity_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每个单测独占一个 XDG 配置根（身份文件写入 ``<tmp>/a2c/computer-id``）。

    Yields:
        Path: 本用例的 ``XDG_CONFIG_HOME``。
    """
    xdg_home = tmp_path / "xdg-config"
    xdg_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    yield xdg_home
