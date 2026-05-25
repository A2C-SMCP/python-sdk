# -*- coding: utf-8 -*-
# filename: test_path.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
路径工具单元测试 / Unit tests for path utilities

测试意图 / Test intentions:
- is_within：相等 / 嵌套 / 兄弟 / 越界（parent 在 child 下）/ 相对路径（纯词法）
- 纯词法语义：不解析符号链接、不规范化 ``..``（与 docstring 契约一致）
"""

from pathlib import Path

import pytest

from a2c_smcp.utils import is_within
from a2c_smcp.utils.path import is_within as is_within_direct


def test_reexport_identity() -> None:
    """``a2c_smcp.utils`` 再导出与子模块同一对象 / re-export is the same object."""
    assert is_within is is_within_direct


@pytest.mark.parametrize(
    ("path", "parent", "expected"),
    [
        (Path("/a/b"), Path("/a/b"), True),  # 相等 → 视为「在其下」
        (Path("/a/b/c"), Path("/a/b"), True),  # 嵌套
        (Path("/a/b/c/d"), Path("/a"), True),  # 多级嵌套
        (Path("/a/b"), Path("/a/c"), False),  # 兄弟
        (Path("/a"), Path("/a/b"), False),  # 越界：parent 反在 child 下
        (Path("/ab"), Path("/a"), False),  # 前缀字符串相同但非路径子级
        (Path("a/b"), Path("a"), True),  # 相对路径（纯词法亦可判定）
        (Path("a/b"), Path("x"), False),  # 相对路径越界
    ],
)
def test_is_within(path: Path, parent: Path, expected: bool) -> None:
    assert is_within(path, parent) is expected


def test_is_within_is_lexical_not_normalizing() -> None:
    """纯词法：不规范化 ``..``——``/a/b/../c`` 按字面段判定仍在 ``/a`` 下。"""
    assert is_within(Path("/a/b/../c"), Path("/a")) is True
