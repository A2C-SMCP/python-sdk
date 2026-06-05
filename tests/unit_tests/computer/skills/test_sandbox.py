# -*- coding: utf-8 -*-
# filename: test_sandbox.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 包根沙箱单元测试（v0.2.1，安全反例红→绿）
Unit tests for the SKILL package-root sandbox (v0.2.1 security counter-examples).

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §9 安全模型；error-handling.md §4017。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §5.3。

测试意图 / Test intentions:
- traversal：`..` / 绝对路径 / 反斜杠 / 盘符 / symlink 逃逸 → 4017 traversal
- forbidden：`.skillenv` 命中（存在与不存在**同 reason**、嵌套、大小写不敏感、symlink 指向）→ 4017 forbidden
- not_found：包内不存在 / 目录 → 4017 not_found
- happy：缺省 SKILL.md / 子资源 / `.` 段规整
- too_large：ensure_within_size_cap 超上限 → 4017 too_large（带 total_size）
- name 寻址防越权：API 只收 root，无 name 形参（结构性不变量）
"""

import inspect
import os
from pathlib import Path

import pytest

from a2c_smcp.computer.skills.sandbox import (
    DEFAULT_SKILL_FILE,
    FORBIDDEN_SKILL_FILES,
    SkillSandboxError,
    ensure_within_size_cap,
    resolve_skill_resource,
)


@pytest.fixture()
def skill_root(tmp_path: Path) -> Path:
    """构造一个最小 SKILL 包根：SKILL.md + references/x.txt + .skillenv。"""
    root = tmp_path / "home" / "mcp" / "server" / "demo-skill"
    (root / "references").mkdir(parents=True)
    (root / DEFAULT_SKILL_FILE).write_text("# Demo Skill\nbody\n", encoding="utf-8")
    (root / "references" / "x.txt").write_text("ref body\n", encoding="utf-8")
    (root / ".skillenv").write_text("SECRET=token\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# traversal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rel_path",
    [
        "../../etc/passwd",  # 父级逃逸
        "../sibling.txt",  # 单级父级
        "/etc/passwd",  # 绝对路径
        "..\\windows",  # 反斜杠（非 POSIX 分隔符）
        "references\\..\\..\\x",  # 反斜杠走私
        "C:/Windows/system32",  # 盘符
        "references/../../outside",  # 词法 .. 段
    ],
)
def test_traversal_rejected(skill_root: Path, rel_path: str) -> None:
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, rel_path)
    assert ei.value.reason == "traversal"
    assert ei.value.rel_path == rel_path


def test_symlink_escape_is_traversal(skill_root: Path, tmp_path: Path) -> None:
    """包内 symlink 指向包外 → realpath 容器校验失败 → traversal。"""
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("top secret\n", encoding="utf-8")
    link = skill_root / "leak"
    os.symlink(secret, link)
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, "leak")
    assert ei.value.reason == "traversal"


# ---------------------------------------------------------------------------
# forbidden（.skillenv 不泄漏存在性）
# ---------------------------------------------------------------------------
def test_skillenv_forbidden_when_exists(skill_root: Path) -> None:
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, ".skillenv")
    assert ei.value.reason == "forbidden"


def test_skillenv_forbidden_when_absent_same_reason(skill_root: Path) -> None:
    """不存在的 .skillenv 与存在的返回同一 reason（不泄漏存在性）。"""
    (skill_root / ".skillenv").unlink()  # 删掉，确保物理不存在
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, ".skillenv")
    assert ei.value.reason == "forbidden"


def test_skillenv_forbidden_nested(skill_root: Path) -> None:
    """嵌套路径命中 .skillenv basename → forbidden（无需该文件存在）。"""
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, "references/.skillenv")
    assert ei.value.reason == "forbidden"


def test_skillenv_forbidden_case_insensitive(skill_root: Path) -> None:
    """大小写变体（防大小写不敏感 FS 绕过）→ forbidden。"""
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, ".SKILLENV")
    assert ei.value.reason == "forbidden"


def test_symlink_to_skillenv_forbidden(skill_root: Path) -> None:
    """包内 symlink 指向 .skillenv → realpath 后 basename 复检 → forbidden。"""
    link = skill_root / "alias.txt"
    os.symlink(skill_root / ".skillenv", link)
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, "alias.txt")
    assert ei.value.reason == "forbidden"


def test_custom_forbidden_names(skill_root: Path) -> None:
    """forbidden_names 可注入额外凭证类文件名（协议「Computer 判定的凭证类文件」扩展口）。"""
    (skill_root / "credentials.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, "credentials.json", forbidden_names={".skillenv", "credentials.json"})
    assert ei.value.reason == "forbidden"
    # 默认集合下同一文件可正常解析（确认确属注入扩展而非内置）
    assert resolve_skill_resource(skill_root, "credentials.json") == (skill_root / "credentials.json").resolve()


# ---------------------------------------------------------------------------
# not_found
# ---------------------------------------------------------------------------
def test_missing_file_not_found(skill_root: Path) -> None:
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, "missing.md")
    assert ei.value.reason == "not_found"


def test_directory_is_not_found(skill_root: Path) -> None:
    """目录不是可读资源 → not_found。"""
    with pytest.raises(SkillSandboxError) as ei:
        resolve_skill_resource(skill_root, "references")
    assert ei.value.reason == "not_found"


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------
def test_default_resolves_skill_md(skill_root: Path) -> None:
    assert resolve_skill_resource(skill_root, None) == (skill_root / DEFAULT_SKILL_FILE).resolve()


def test_empty_rel_path_resolves_skill_md(skill_root: Path) -> None:
    assert resolve_skill_resource(skill_root, "") == (skill_root / DEFAULT_SKILL_FILE).resolve()


def test_subresource_resolves(skill_root: Path) -> None:
    assert resolve_skill_resource(skill_root, "references/x.txt") == (skill_root / "references" / "x.txt").resolve()


def test_dot_segments_normalized(skill_root: Path) -> None:
    """`.` 段被 PurePosixPath 规整，合法解析（区别于 `..`）。"""
    assert resolve_skill_resource(skill_root, "./references/./x.txt") == (skill_root / "references" / "x.txt").resolve()


# ---------------------------------------------------------------------------
# too_large
# ---------------------------------------------------------------------------
def test_size_within_cap_returns_size(skill_root: Path) -> None:
    target = resolve_skill_resource(skill_root, "references/x.txt")
    size = ensure_within_size_cap(target, cap=1024)
    assert size == target.stat().st_size


def test_size_exceeds_cap_too_large(skill_root: Path) -> None:
    target = resolve_skill_resource(skill_root, "references/x.txt")
    real_size = target.stat().st_size
    with pytest.raises(SkillSandboxError) as ei:
        ensure_within_size_cap(target, cap=0, rel_path="references/x.txt")
    assert ei.value.reason == "too_large"
    assert ei.value.total_size == real_size
    assert ei.value.rel_path == "references/x.txt"


# ---------------------------------------------------------------------------
# name 寻址防越权（结构性不变量）/ name-addressing invariant
# ---------------------------------------------------------------------------
def test_api_takes_root_not_name() -> None:
    """API 只收包根 root，**无** name 形参——结构上杜绝从 name 推导 FS 路径（skill.md §9.2）。"""
    params = inspect.signature(resolve_skill_resource).parameters
    assert "root" in params
    assert "name" not in params


def test_default_forbidden_files_contains_skillenv() -> None:
    assert ".skillenv" in FORBIDDEN_SKILL_FILES
