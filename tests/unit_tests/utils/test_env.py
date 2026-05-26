# -*- coding: utf-8 -*-
# filename: test_env.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
env_truthy 单元测试 / Unit tests for env_truthy。

意图：统一真值集（1/true/yes/on，大小写 + 空白不敏感）；未设置/空白回退 default；其它值为 False；
默认读 os.environ、支持注入 env 映射。
"""

from __future__ import annotations

import pytest

from a2c_smcp.utils.env import env_truthy


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", "ON", "  true  ", "\tyes\n"])
def test_truthy_values(value: str) -> None:
    assert env_truthy("K", env={"K": value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "random", "2", "-1"])
def test_explicit_non_truthy_is_false(value: str) -> None:
    assert env_truthy("K", env={"K": value}) is False


def test_unset_returns_default() -> None:
    assert env_truthy("K", env={}) is False
    assert env_truthy("K", env={}, default=True) is True


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_returns_default(blank: str) -> None:
    assert env_truthy("K", env={"K": blank}) is False  # default False
    assert env_truthy("K", env={"K": blank}, default=True) is True  # 空白回退 default


def test_uses_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2C_TEST_ENV_TRUTHY", "yes")
    assert env_truthy("A2C_TEST_ENV_TRUTHY") is True
    monkeypatch.delenv("A2C_TEST_ENV_TRUTHY", raising=False)
    assert env_truthy("A2C_TEST_ENV_TRUTHY") is False
