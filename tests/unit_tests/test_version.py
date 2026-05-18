# -*- coding: utf-8 -*-
# filename: test_version.py
# @Author  : JQQ
# @Software: PyCharm
"""
协议版本语义单元测试 / Unit tests for protocol version semantics

覆盖 a2c-smcp-protocol versioning.md §测试建议 兼容性矩阵 + parse 边界。
Covers the versioning.md §test-matrix compatibility table + parse boundaries.
"""

import pytest

from a2c_smcp.version import ProtocolVersion, is_compatible


@pytest.mark.parametrize(
    ("client", "server", "expected"),
    [
        # versioning.md §测试建议 矩阵 / the spec test matrix
        ("0.2.0", "0.2.0", True),  # 完全匹配 / exact
        ("0.2.1", "0.2.0", True),  # PATCH 差异 v0.x 自由 / PATCH free
        ("0.2.0", "0.3.0", False),  # MINOR 差异 v0.x / MINOR mismatch
        ("0.3.0", "0.2.0", False),  # MINOR 差异反向 / reverse MINOR mismatch
        ("1.0.0", "0.2.0", False),  # MAJOR 差异 / MAJOR mismatch
        ("1.0.0", "1.2.0", True),  # v1.0+ 向后兼容 / backward compatible
        ("1.2.0", "1.0.0", False),  # v1.0+ Client 更新 / client newer
        ("1.2.5", "1.2.0", True),  # v1.0+ PATCH 自由 / PATCH free
    ],
)
def test_is_compatible_matrix(client: str, server: str, expected: bool) -> None:
    assert is_compatible(ProtocolVersion.parse(client), ProtocolVersion.parse(server)) is expected


def test_parse_roundtrip() -> None:
    v = ProtocolVersion.parse("0.2.7")
    assert (v.major, v.minor, v.patch) == (0, 2, 7)
    assert str(v) == "0.2.7"


@pytest.mark.parametrize("bad", ["abc", "0.2", "0.2.0.1", "", "0..2", "1.x.0", "v0.2.0"])
def test_parse_invalid_raises_valueerror(bad: str) -> None:
    with pytest.raises(ValueError):
        ProtocolVersion.parse(bad)
