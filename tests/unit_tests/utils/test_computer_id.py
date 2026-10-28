# -*- coding: utf-8 -*-
# filename: test_computer_id.py
# @Time    : 2026/10/28
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``computer_id`` 形态判定单测（``a2c_smcp/utils/computer_id.py``）—— protocol#64 / PR #74。

协议要求：整串匹配、**长度恰为 36**、小写规范 UUID、拒绝 nil / max；版本位不校验（接收方视为
不透明）。前导/尾随空白、`\\n`、`\\r\\n` 一律 400——正则锚点语义（Python `$` 匹配结尾换行）不得
成为漏洞。
"""

from __future__ import annotations

import pytest

from a2c_smcp.utils.computer_id import MAX_UUID, NIL_UUID, is_canonical_computer_id

VALID = "abcdef01-2345-4678-8abc-def012345678"


@pytest.mark.parametrize(
    "value",
    [
        VALID,
        # 版本位非 4/7：协议要求接收方视为不透明 ⇒ 必须接受
        "12345678-1234-9234-8234-123456789abc",
        "00000000-0000-4000-8000-000000000001",
    ],
)
def test_canonical_values_are_accepted(value: str) -> None:
    assert is_canonical_computer_id(value) is True


@pytest.mark.parametrize(
    "value",
    [
        NIL_UUID,
        MAX_UUID,
        VALID.upper(),
        VALID.replace("-", ""),
        # protocol#64 / PR #74：整串 + 恰 36 字符，空白与换行一律拒绝
        VALID + "\n",
        VALID + "\r\n",
        " " + VALID,
        VALID + " ",
        VALID + "\t",
        VALID[:-1],
        VALID + "0",
        "not-a-uuid",
        "",
    ],
)
def test_malformed_or_forbidden_values_are_rejected(value: str) -> None:
    assert is_canonical_computer_id(value) is False


@pytest.mark.parametrize("value", [None, 123, 1.5, b"abcdef01-2345-4678-8abc-def012345678", ["x"], {"x": 1}])
def test_non_str_is_rejected(value: object) -> None:
    assert is_canonical_computer_id(value) is False
