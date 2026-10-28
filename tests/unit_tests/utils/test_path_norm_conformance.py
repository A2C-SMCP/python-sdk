# -*- coding: utf-8 -*-
# filename: test_path_norm_conformance.py
# @Author  : JQQ
# @Software: PyCharm

"""
``normalize_path`` 数据向量一致性测试 / ``normalize_path`` conformance vectors.

协议依据 / Protocol: a2c-smcp-protocol ``docs/specification/fixtures/canonical_path_conformance_vectors.json``
（protocol#65，core-governance §6 数据向量）。本文件为协议仓向量的**逐字节 vendored 副本**；
任一 ``normalize_path(input, platform)`` MUST 对每条向量产出 ``expected``，或在向量给出 ``error``
时以 ``invalid_path`` 失败。纯函数与宿主 OS 无关。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.utils.path_norm import (
    InvalidPathError,
    normalize_path,
    to_native_path,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "canonical_path_conformance_vectors.json"
_VECTORS: list[dict[str, Any]] = json.loads(_FIXTURE.read_text(encoding="utf-8"))["vectors"]


def test_vector_fixture_shape() -> None:
    """向量集规模锁定：POSIX 18 + Windows 31 = 49，其中 21 条 invalid_path."""
    assert len(_VECTORS) == 49
    assert sum(1 for v in _VECTORS if v["platform"] == "posix") == 18
    assert sum(1 for v in _VECTORS if v["platform"] == "windows") == 31
    assert sum(1 for v in _VECTORS if "error" in v) == 21


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["id"] for v in _VECTORS])
def test_conformance_vector(vector: dict[str, Any]) -> None:
    """逐条对拍：expected 或 invalid_path / Per-vector comparison."""
    if "error" in vector:
        assert vector["error"] == "invalid_path"
        with pytest.raises(InvalidPathError) as exc_info:
            normalize_path(vector["input"], vector["platform"])
        assert exc_info.value.reason == "invalid_path"
    else:
        assert normalize_path(vector["input"], vector["platform"]) == vector["expected"]


def test_windows_drive_must_be_ascii_letter() -> None:
    """协议 ``[A-Za-z]``；``str.isalpha`` 的非 ASCII 字母不算盘符."""
    for bad in ("é:\\x", "中:/x", "Ω:/x"):
        with pytest.raises(InvalidPathError):
            normalize_path(bad, "windows")


class TestNativePath:
    """``canonical_path`` ↔ 宿主原生路径转换 / Canonical ↔ native path conversion."""

    @pytest.mark.parametrize(
        ("canonical", "expected"),
        [
            ("/C:/", "C:\\"),
            ("/C:/Users/u/a.txt", "C:\\Users\\u\\a.txt"),
            ("/D:/Data/ABC.Txt", "D:\\Data\\ABC.Txt"),
        ],
    )
    def test_windows_canonical_to_native(self, canonical: str, expected: str) -> None:
        assert to_native_path(canonical, "windows") == expected

    def test_posix_native_is_identity(self) -> None:
        assert to_native_path("/home/u/a.txt", "posix") == "/home/u/a.txt"

    def test_windows_native_requires_canonical_drive(self) -> None:
        with pytest.raises(InvalidPathError):
            to_native_path("/Users/a.txt", "windows")
