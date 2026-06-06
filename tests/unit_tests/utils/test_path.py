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
- resolve_xdg_first：绝对 XDG 取 ``$VAR/<subpath>`` / 相对值按未设置回退 / 未设置回退 / ~ 展开
"""

from pathlib import Path

import pytest

from a2c_smcp.utils import is_within, resolve_xdg_first
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


# ---------------------------------------------------------------------------
# resolve_xdg_first（XDG-first 解析）/ XDG-first resolution
# ---------------------------------------------------------------------------
def test_xdg_absolute_used_with_subpath(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    assert resolve_xdg_first({"XDG_X": str(xdg)}, "XDG_X", "a2c", "skills", fallback=tmp_path / "fb") == (
        xdg / "a2c" / "skills"
    ).resolve()


def test_xdg_relative_value_ignored_uses_fallback(tmp_path: Path) -> None:
    # 相对值按 XDG 规范视为未设置 → 回退。
    fb = tmp_path / "fb" / "a2c"
    assert resolve_xdg_first({"XDG_X": "relative/path"}, "XDG_X", "a2c", fallback=fb) == fb.resolve()


def test_xdg_unset_uses_fallback(tmp_path: Path) -> None:
    fb = tmp_path / "fb"
    assert resolve_xdg_first({}, "XDG_X", "a2c", fallback=fb) == fb.resolve()


def test_xdg_blank_value_uses_fallback(tmp_path: Path) -> None:
    fb = tmp_path / "fb"
    assert resolve_xdg_first({"XDG_X": "   "}, "XDG_X", fallback=fb) == fb.resolve()


def test_xdg_fallback_tilde_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_xdg_first({}, "XDG_X", fallback=Path("~/.config/a2c")) == (tmp_path / ".config" / "a2c").resolve()
