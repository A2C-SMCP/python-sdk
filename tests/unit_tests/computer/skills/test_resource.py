# -*- coding: utf-8 -*-
# filename: test_resource.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 消费字节视图单元测试 / Unit tests for the SKILL consumed-bytes view (v0.2.1, #66)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §9；blob-transfer.md §3 / §5。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §4.2 / §4.4 / §5.1。

测试意图 / Test intentions:
- SKILL.md 入口剥 frontmatter（消费字节 = body）；其它资源原始字节；
- ``total_size`` / ``sha256`` 基于消费字节（mint 与 serve 一致的根）；
- ``too_large`` 守卫基于 stat（不读超大文件），仅 mint 期生效；
- MIME 推断 + ``is_text`` 判定；切片 / read_all 语义；
- 沙箱失败（traversal / forbidden / not_found）原样抛 SkillSandboxError（不在此映射协议错误码）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from a2c_smcp.computer.skills.resource import (
    DEFAULT_SKILL_MIME,
    looks_textual,
    resolve_skill_view,
)
from a2c_smcp.computer.skills.sandbox import SkillSandboxError

_SKILL_MD = "---\nname: demo\nversion: 1.0.0\n---\n# Title\n\nhello body\n"
_SKILL_BODY = "# Title\n\nhello body\n"  # 闭合 --- 行之后 / after the closing fence line
_NOTE = b"raw note bytes\n"


@pytest.fixture
def pkg(tmp_path: Path) -> Path:
    """含 SKILL.md / 子资源 / .skillenv 的包根。"""
    root = tmp_path / "demo-skill"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (root / "references" / "note.txt").write_bytes(_NOTE)
    (root / "data.json").write_text('{"k": 1}\n', encoding="utf-8")
    (root / ".skillenv").write_text("SECRET=1\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# SKILL.md 入口：frontmatter 剥离 + 消费字节基准
# ---------------------------------------------------------------------------
def test_skill_md_default_strips_frontmatter(pkg: Path) -> None:
    view = resolve_skill_view(pkg, None)  # 缺省 → SKILL.md
    expected = _SKILL_BODY.encode("utf-8")
    assert view.rel_path == "SKILL.md"
    assert view.is_entry is True
    assert view.mime == DEFAULT_SKILL_MIME
    assert view.is_text is True
    assert view.total_size == len(expected)
    assert view.sha256 == hashlib.sha256(expected).hexdigest()
    assert view.read_all() == expected


def test_skill_md_explicit_rel_path_same_as_default(pkg: Path) -> None:
    a = resolve_skill_view(pkg, "SKILL.md")
    b = resolve_skill_view(pkg, None)
    assert a.sha256 == b.sha256 and a.total_size == b.total_size


def test_skill_md_total_size_excludes_frontmatter(pkg: Path) -> None:
    """消费字节 < 原始文件字节（frontmatter 被剥离）。"""
    raw_size = (pkg / "SKILL.md").stat().st_size
    view = resolve_skill_view(pkg, "SKILL.md")
    assert view.total_size < raw_size


# ---------------------------------------------------------------------------
# 其它资源：原始字节
# ---------------------------------------------------------------------------
def test_subresource_raw_bytes(pkg: Path) -> None:
    view = resolve_skill_view(pkg, "references/note.txt")
    assert view.is_entry is False
    assert view.total_size == len(_NOTE)
    assert view.sha256 == hashlib.sha256(_NOTE).hexdigest()
    assert view.read_all() == _NOTE
    assert view.mime == "text/plain"
    assert view.is_text is True


def test_json_subresource_is_textual(pkg: Path) -> None:
    view = resolve_skill_view(pkg, "data.json")
    assert view.is_text is True  # application/json ∈ 文本类允许集


# ---------------------------------------------------------------------------
# too_large 守卫（仅 mint 期；基于 stat，不读文件）
# ---------------------------------------------------------------------------
def test_too_large_subresource_rejected_by_stat(pkg: Path) -> None:
    view_size = (pkg / "references" / "note.txt").stat().st_size
    with pytest.raises(SkillSandboxError) as exc:
        resolve_skill_view(pkg, "references/note.txt", too_large_cap=view_size - 1)
    assert exc.value.reason == "too_large"
    assert exc.value.total_size == view_size


def test_too_large_skill_md_rejected(pkg: Path) -> None:
    with pytest.raises(SkillSandboxError) as exc:
        resolve_skill_view(pkg, "SKILL.md", too_large_cap=1)
    assert exc.value.reason == "too_large"


def test_within_cap_ok(pkg: Path) -> None:
    view = resolve_skill_view(pkg, "references/note.txt", too_large_cap=10_000)
    assert view.total_size == len(_NOTE)


def test_skill_md_second_guard_on_replace_inflation(tmp_path: Path) -> None:
    """二次 too_large 守卫：原始 ≤ cap，但 errors='replace' 把非法 UTF-8 膨胀成 U+FFFD 致 body > cap。

    罕见但唯一能让 SKILL.md 剥离后 body 超过原始文件字节的路径（每个非法字节 → 3 字节 U+FFFD）。
    守护 resource.py 剥离后再校验的防御分支。
    """
    root = tmp_path / "infl"
    root.mkdir()
    # frontmatter 16 字节 + 100 个非法 UTF-8 字节 → 原始 116；剥离后 body = 100×U+FFFD = 300 字节
    (root / "SKILL.md").write_bytes(b"---\nname: d\n---\n" + b"\xff" * 100)
    raw_size = (root / "SKILL.md").stat().st_size
    cap = 200  # raw(116) ≤ cap < stripped(300)
    assert raw_size <= cap
    with pytest.raises(SkillSandboxError) as exc:
        resolve_skill_view(root, "SKILL.md", too_large_cap=cap)
    assert exc.value.reason == "too_large"
    assert exc.value.total_size == 300  # 剥离后 body 字节（非原始文件字节）


def test_no_cap_never_too_large(pkg: Path) -> None:
    """get_blob 解析期（cap=None）不做 too_large 守卫。"""
    view = resolve_skill_view(pkg, "references/note.txt")  # too_large_cap 默认 None
    assert view.total_size == len(_NOTE)


# ---------------------------------------------------------------------------
# 沙箱失败原样抛出（错误码映射在上层 handler）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rel", ["../outside", "../../etc/passwd", "references/../../escape"])
def test_traversal_raises_sandbox_error(pkg: Path, rel: str) -> None:
    with pytest.raises(SkillSandboxError) as exc:
        resolve_skill_view(pkg, rel)
    assert exc.value.reason == "traversal"


def test_skillenv_forbidden(pkg: Path) -> None:
    with pytest.raises(SkillSandboxError) as exc:
        resolve_skill_view(pkg, ".skillenv")
    assert exc.value.reason == "forbidden"


def test_missing_not_found(pkg: Path) -> None:
    with pytest.raises(SkillSandboxError) as exc:
        resolve_skill_view(pkg, "nope.txt")
    assert exc.value.reason == "not_found"


# ---------------------------------------------------------------------------
# 切片语义
# ---------------------------------------------------------------------------
def test_slice_mid_and_eof(pkg: Path) -> None:
    view = resolve_skill_view(pkg, "references/note.txt")
    assert view.slice(0, 3) == _NOTE[:3]
    assert view.slice(view.total_size, 10) == b""  # EOF probe
    assert view.slice(4, 999) == _NOTE[4:]  # auto-truncate


def test_slice_negative_and_oob(pkg: Path) -> None:
    view = resolve_skill_view(pkg, "references/note.txt")
    with pytest.raises(ValueError, match="non-negative"):
        view.slice(-1, 1)
    with pytest.raises(ValueError, match="> total_size"):
        view.slice(view.total_size + 1, 1)


# ---------------------------------------------------------------------------
# looks_textual helper
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("text/plain", True),
        ("text/markdown", True),
        ("application/json", True),
        ("application/yaml", True),
        ("image/png", False),
        ("application/octet-stream", False),
    ],
)
def test_looks_textual(mime: str, expected: bool) -> None:
    assert looks_textual(mime) is expected
