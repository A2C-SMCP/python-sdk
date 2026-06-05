# -*- coding: utf-8 -*-
# filename: test_atomic_io.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
原子写工具单元测试 / Unit tests for a2c_smcp.utils.atomic_io.

抽取自 settings/store 与 blob/toolspool 的共用原语（fix-review #58）；重点验证落盘正确性、父目录
自动创建，以及中途失败时**不留半截文件 + 清理临时文件**（原 toolspool 弱实现欠缺、本实现硬化）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import a2c_smcp.utils.atomic_io as atomic_io_mod
from a2c_smcp.utils.atomic_io import atomic_write_bytes, atomic_write_text, unique_tmp_path


def test_unique_tmp_path_is_unique_and_suffixed() -> None:
    target = Path("/tmp/known_marketplaces.json")
    a = unique_tmp_path(target)
    b = unique_tmp_path(target)
    assert a != b  # 随机后缀互异
    assert a.name.endswith(".tmp")
    assert a.parent == target.parent


def test_atomic_write_bytes_roundtrip_and_parent_mkdir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "blob.bin"
    payload = b"\x00\x01\x02\xff binary"
    atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload  # 父目录自动创建 + 内容一致


def test_atomic_write_text_roundtrip_non_ascii(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    atomic_write_text(target, "中文 + emoji 🚀\n")
    assert target.read_text(encoding="utf-8") == "中文 + emoji 🚀\n"


def test_atomic_write_interrupted_leaves_no_half_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "f.txt"
    atomic_write_text(target, "ORIGINAL")  # 先有一个完好文件

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated disk failure during rename")

    # 原语用的是 atomic_io 自己的 os，patch 该模块的 os.replace。
    monkeypatch.setattr(atomic_io_mod.os, "replace", boom)
    with pytest.raises(OSError, match="simulated disk failure"):
        atomic_write_text(target, "NEW-SHOULD-NOT-LAND")

    assert target.read_text(encoding="utf-8") == "ORIGINAL"  # 目标保持原值（rename 失败、未被破坏）
    assert list(tmp_path.glob("*.tmp")) == []  # 临时文件被清理，不留垃圾
