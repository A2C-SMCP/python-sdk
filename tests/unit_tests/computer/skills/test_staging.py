# -*- coding: utf-8 -*-
# filename: test_staging.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL staging（mcp 源）单元测试（v0.2.1 #59）

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §3（mounted/archive/resources）/ §4。

测试意图 / Test intentions:
- mounted / archive(tar.gz, zip) / resources 三模式各物化正确（文件落盘 + 注册 A2CSkillRef）
- archive sha256 不符 / 归档穿越成员 → 物化失败不入册、不抛、不逃逸
- 包根目录名校正为 frontmatter.name（§4）；缺 SKILL.md / 缺 frontmatter 必填 → 跳过
- resources 模式文本 + 二进制（blob）子资源还原正确
"""

import base64
import hashlib
import io
import json
import logging
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import BlobResourceContents, ReadResourceResult, Resource, TextResourceContents

import a2c_smcp.computer.skills.staging as staging_mod
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import _build_marketplace_ref, _partition_roots, stage_mcp_skills, stage_user_skills
from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE, redact_git_urls_in_text


# ── 测试替身 / doubles ───────────────────────────────────────────────────────
@pytest.fixture
def staging_logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """捕获 staging 模块日志（项目 logger 关闭 propagate，直接挂 handler）/ capture staging logs。"""
    staging_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        staging_mod.logger.removeHandler(caplog.handler)


class FakeManager:
    def __init__(
        self,
        pairs: list[tuple[str, Resource]],
        reads: dict[str, ReadResourceResult] | None = None,
        display_names: dict[str, str] | None = None,
    ) -> None:
        # pairs 的第一元 = bundle_id（manager 身份键）。
        self._pairs = pairs
        self._reads = reads or {}
        self._display_names = display_names or {}

    async def list_skill_resources(self, bundle_id: str | None = None) -> list[tuple[str, Resource]]:
        return [(s, r) for s, r in self._pairs if bundle_id is None or s == bundle_id]

    async def read_resource(self, server: str, uri: str) -> ReadResourceResult:
        return self._reads[str(uri)]

    def get_server_config(self, bundle_id: str) -> SimpleNamespace:
        """display ``name`` **刻意与 bundle_id 分叉**（#142）。

        SKILL ``<server>`` 段 = ``bundle_id`` 原样（skill.md §1.3）。display 名是纯展示、允许碰撞、
        永不做键——本 stub 默认返回一个既 **不等于** bundle_id、规范化后又 **必然不同**的名字（含空格 /
        括号），使本文件全部 mcp 用例成为「display 名泄漏进 name / source / 磁盘路径」的回归守卫：
        任何一处误取 display 名都会立刻在断言上炸开，而非悄悄同值蒙混过关。
        """
        return SimpleNamespace(name=self._display_names.get(bundle_id, f"{bundle_id} (display)"))


def _skill_md(name: str = "my-skill", description: str = "聚合 CSV", *, tags: list[str] | None = None) -> str:
    tags_line = ""
    if tags is not None:
        tags_line = "tags: []\n" if not tags else "tags:\n" + "".join(f"  - {t}\n" for t in tags)
    return (
        f"---\nname: {name}\ndescription: {description}\nlicense: MIT\n"
        f"allowed-tools:\n  - read\n  - write\n{tags_line}---\n# {name}\nbody\n"
    )


def _root(uri: str, meta: dict) -> Resource:
    return Resource(uri=uri, name=uri.rsplit("/", 1)[-1], _meta=meta)


def _make_targz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _fetch_returning(data: bytes):
    async def _fetch(url: str) -> bytes:
        return data

    return _fetch


# ── mounted ──────────────────────────────────────────────────────────────────
async def test_stage_mounted(tmp_path: Path) -> None:
    mount = tmp_path / "mount" / "my-skill"
    (mount / "scripts").mkdir(parents=True)
    (mount / "SKILL.md").write_text(_skill_md(), encoding="utf-8")
    (mount / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")

    res = _root("skill://h/my-skill", {"source": "mounted", "mount_dir": str(mount), "version": "1.2.0"})
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("tfrobot-tools", res)]), reg, home)

    assert names == ["mcp:tfrobot-tools:my-skill"]
    ref = reg.resolve("mcp:tfrobot-tools:my-skill")
    assert ref is not None
    assert ref["path"] == str(home / "mcp" / "tfrobot-tools" / "my-skill")
    assert (Path(ref["path"]) / "SKILL.md").is_file()
    assert (Path(ref["path"]) / "scripts" / "run.py").is_file()
    assert ref["source"] == "mcp:tfrobot-tools"
    assert ref["version"] == "1.2.0"
    assert ref["license"] == "MIT"
    assert ref["allowed_tools"] == ["read", "write"]
    assert ref["description"] == "聚合 CSV"


async def test_stage_mounted_tags_passthrough(tmp_path: Path) -> None:
    # mcp 源 frontmatter tags → ref["tags"] 透传（三源共用 _apply_frontmatter_optional_fields 单点；skill.md §1.5）
    mount = tmp_path / "mount" / "tagged"
    mount.mkdir(parents=True)
    (mount / "SKILL.md").write_text(_skill_md(name="tagged", tags=["finance", "report"]), encoding="utf-8")
    res = _root("skill://h/tagged", {"source": "mounted", "mount_dir": str(mount)})
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, home)

    assert names == ["mcp:srv:tagged"]
    ref = reg.resolve("mcp:srv:tagged")
    assert ref is not None
    assert ref["tags"] == ["finance", "report"]


def test_marketplace_ref_tags_passthrough() -> None:
    # marketplace 源 ref 构造：frontmatter tags → ref["tags"] 透传（与 mcp/user 同走 _apply_frontmatter_optional_fields）
    ref = _build_marketplace_ref(
        "acme-audit:audit", "acme-skills", {"name": "audit", "description": "d", "tags": ["sec", "audit"]}, "1.0.0", Path("/x")
    )
    assert ref["tags"] == ["sec", "audit"]


# ── archive ────────────────────────────────────────────────────────────────────
async def test_stage_archive_targz(tmp_path: Path) -> None:
    data = _make_targz({"SKILL.md": _skill_md().encode(), "scripts/run.py": b"print(1)"})
    sha = hashlib.sha256(data).hexdigest()
    res = _root(
        "skill://h/my-skill",
        {"source": "archive", "archive_uri": "https://x/s.tgz", "archive_format": "tar.gz", "archive_sha256": sha},
    )
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, home, archive_fetch=_fetch_returning(data))

    assert names == ["mcp:srv:my-skill"]
    ref = reg.resolve("mcp:srv:my-skill")
    assert ref is not None
    assert (Path(ref["path"]) / "SKILL.md").is_file()
    assert (Path(ref["path"]) / "scripts" / "run.py").read_text() == "print(1)"


async def test_stage_archive_zip(tmp_path: Path) -> None:
    data = _make_zip({"SKILL.md": _skill_md(name="zskill").encode(), "ref.txt": b"x"})
    res = _root("skill://h/zskill", {"source": "archive", "archive_uri": "https://x/s.zip", "archive_format": "zip"})
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, home, archive_fetch=_fetch_returning(data))

    assert names == ["mcp:srv:zskill"]
    assert (Path(reg.resolve("mcp:srv:zskill")["path"]) / "ref.txt").read_text() == "x"  # type: ignore[index]


async def test_stage_archive_sha256_mismatch_not_registered(tmp_path: Path) -> None:
    data = _make_targz({"SKILL.md": _skill_md().encode()})
    res = _root(
        "skill://h/my-skill",
        {"source": "archive", "archive_uri": "https://x/s.tgz", "archive_format": "tar.gz", "archive_sha256": "deadbeef"},
    )
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, home, archive_fetch=_fetch_returning(data))

    assert names == []  # 校验失败 → 不入册、不抛
    assert len(reg) == 0


async def test_stage_archive_path_traversal_rejected(tmp_path: Path) -> None:
    # 含 ../evil 的恶意归档 → 安全解包拒绝 → 不入册、不逃逸
    data = _make_targz({"SKILL.md": _skill_md().encode(), "../evil.txt": b"pwned"})
    res = _root("skill://h/my-skill", {"source": "archive", "archive_uri": "https://x/s.tgz", "archive_format": "tar.gz"})
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, home, archive_fetch=_fetch_returning(data))

    assert names == []
    assert not (tmp_path / "evil.txt").exists()  # 未逃逸到 staging 之外


async def test_stage_archive_symlink_member_rejected(tmp_path: Path) -> None:
    # 恶意 tar：含 symlink 成员（指向 /etc/passwd）→ 拒绝 → 不入册（PR 主打安全特性的回归用例）
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        md = _skill_md().encode()
        info = tarfile.TarInfo("SKILL.md")
        info.size = len(md)
        tar.addfile(info, io.BytesIO(md))
        link = tarfile.TarInfo("evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    res = _root("skill://h/my-skill", {"source": "archive", "archive_uri": "https://x/s.tgz", "archive_format": "tar.gz"})
    reg = SkillRegistry()

    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, tmp_path / "home", archive_fetch=_fetch_returning(buf.getvalue()))

    assert names == []
    assert len(reg) == 0


async def test_stage_archive_extracted_size_cap(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    # 解压累计超上限 → 中止 → 不入册（炸弹防护）。临时把上限调到极小以触发。
    monkeypatch.setattr(staging_mod, "MAX_EXTRACTED_BYTES", 8)
    data = _make_targz({"SKILL.md": _skill_md().encode()})  # 远超 8 字节
    res = _root("skill://h/my-skill", {"source": "archive", "archive_uri": "https://x/s.tgz", "archive_format": "tar.gz"})
    reg = SkillRegistry()
    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, tmp_path / "home", archive_fetch=_fetch_returning(data))
    assert names == []
    assert len(reg) == 0


async def test_stage_archive_download_size_cap(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(staging_mod, "MAX_ARCHIVE_DOWNLOAD_BYTES", 4)
    data = _make_targz({"SKILL.md": _skill_md().encode()})
    res = _root("skill://h/my-skill", {"source": "archive", "archive_uri": "https://x/s.tgz", "archive_format": "tar.gz"})
    reg = SkillRegistry()
    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, tmp_path / "home", archive_fetch=_fetch_returning(data))
    assert names == []


async def test_stage_archive_member_count_cap(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(staging_mod, "MAX_ARCHIVE_MEMBERS", 1)
    data = _make_targz({"SKILL.md": _skill_md().encode(), "a.txt": b"x"})  # 2 成员 > 1
    res = _root("skill://h/my-skill", {"source": "archive", "archive_uri": "https://x/s.tgz", "archive_format": "tar.gz"})
    reg = SkillRegistry()
    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, tmp_path / "home", archive_fetch=_fetch_returning(data))
    assert names == []


# ── resources ──────────────────────────────────────────────────────────────────
async def test_stage_resources_text_and_blob(tmp_path: Path) -> None:
    root_uri = "skill://h/my-skill"
    md_uri = "skill://h/my-skill/SKILL.md"
    bin_uri = "skill://h/my-skill/assets/logo.bin"
    root = _root(root_uri, {"source": "resources"})
    sub_md = Resource(uri=md_uri, name="SKILL.md")
    sub_bin = Resource(uri=bin_uri, name="logo.bin")
    reads = {
        md_uri: ReadResourceResult(contents=[TextResourceContents(uri=md_uri, text=_skill_md())]),
        bin_uri: ReadResourceResult(contents=[BlobResourceContents(uri=bin_uri, blob=base64.b64encode(b"\x00\x01\x02").decode())]),
    }
    mgr = FakeManager([("srv", root), ("srv", sub_md), ("srv", sub_bin)], reads=reads)
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(mgr, reg, home)

    assert names == ["mcp:srv:my-skill"]
    path = Path(reg.resolve("mcp:srv:my-skill")["path"])  # type: ignore[index]
    assert (path / "SKILL.md").is_file()
    assert (path / "assets" / "logo.bin").read_bytes() == b"\x00\x01\x02"


# ── 元数据 / 边界 ──────────────────────────────────────────────────────────────
async def test_frontmatter_name_canonicalizes_dir(tmp_path: Path) -> None:
    # URI 叶 old-leaf 与 frontmatter.name real-name 不一致 → 包根目录名校正为 real-name（§4）
    mount = tmp_path / "mount" / "whatever"
    mount.mkdir(parents=True)
    (mount / "SKILL.md").write_text(_skill_md(name="real-name"), encoding="utf-8")
    res = _root("skill://h/old-leaf", {"source": "mounted", "mount_dir": str(mount)})
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, home)

    assert names == ["mcp:srv:real-name"]
    assert reg.resolve("mcp:srv:real-name")["path"] == str(home / "mcp" / "srv" / "real-name")  # type: ignore[index]
    assert not (home / "mcp" / "srv" / "old-leaf").exists()


async def test_missing_skill_md_not_registered(tmp_path: Path) -> None:
    mount = tmp_path / "mount" / "my-skill"
    mount.mkdir(parents=True)
    (mount / "README.md").write_text("no skill md", encoding="utf-8")
    res = _root("skill://h/my-skill", {"source": "mounted", "mount_dir": str(mount)})
    reg = SkillRegistry()
    names = await stage_mcp_skills(FakeManager([("srv", res)]), reg, tmp_path / "home")
    assert names == []
    assert len(reg) == 0


async def test_in_run_name_collision_keeps_first(tmp_path: Path) -> None:
    # 同一 run 内两个不同 SKILL 合成同 name（frontmatter.name 撞）→ 第二个按 §1.5 拒绝、保留先到者，
    # 且不覆盖先到者磁盘文件（真冲突在 rename 落盘前拦截，验证 Item 1 修复）。
    mount_a = tmp_path / "mount" / "a"
    mount_a.mkdir(parents=True)
    (mount_a / "SKILL.md").write_text(_skill_md(name="dup", description="first"), encoding="utf-8")
    (mount_a / "a.txt").write_text("A", encoding="utf-8")
    mount_b = tmp_path / "mount" / "b"
    mount_b.mkdir(parents=True)
    (mount_b / "SKILL.md").write_text(_skill_md(name="dup", description="second"), encoding="utf-8")
    (mount_b / "b.txt").write_text("B", encoding="utf-8")

    res_a = _root("skill://h/a", {"source": "mounted", "mount_dir": str(mount_a)})
    res_b = _root("skill://h/b", {"source": "mounted", "mount_dir": str(mount_b)})
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", res_a), ("srv", res_b)]), reg, home)

    assert names == ["mcp:srv:dup"]  # 仅第一个入册
    assert len(reg) == 1
    ref = reg.resolve("mcp:srv:dup")
    assert ref is not None
    assert ref["description"] == "first"  # 保留先到者
    final = home / "mcp" / "srv" / "dup"
    assert (final / "a.txt").exists()  # 先到者磁盘文件未被覆盖
    assert not (final / "b.txt").exists()


# ── #142：<server> 段 = bundle_id（协议 skill.md §1.3；supersede #18 正交结论）──────
async def _mount_skill(tmp_path: Path, dirname: str, skill_name: str, description: str = "d") -> Resource:
    """建一个 mounted SKILL 包并返回其根 Resource / Build a mounted SKILL package, return its root Resource."""
    mount = tmp_path / "mount" / dirname
    mount.mkdir(parents=True)
    (mount / "SKILL.md").write_text(_skill_md(name=skill_name, description=description), encoding="utf-8")
    return _root(f"skill://h/{skill_name}", {"source": "mounted", "mount_dir": str(mount)})


async def test_mcp_segment_takes_bundle_id_not_display_name(tmp_path: Path) -> None:
    """``<server>`` 段 / ``source`` / 磁盘路径三处均取 bundle_id 原样，display 名不得泄漏（skill.md §1.3）。

    display 名 ``My Editor`` 若泄漏，旧行为会规范化成 ``My_Editor`` 而非 ``acme-editor``。
    """
    res = await _mount_skill(tmp_path, "e", "format")
    reg = SkillRegistry()
    home = tmp_path / "home"

    mgr = FakeManager([("acme-editor", res)], display_names={"acme-editor": "My Editor"})
    names = await stage_mcp_skills(mgr, reg, home)

    assert names == ["mcp:acme-editor:format"]
    ref = reg.resolve("mcp:acme-editor:format")
    assert ref is not None
    assert ref["source"] == "mcp:acme-editor"
    assert ref["path"] == str(home / "mcp" / "acme-editor" / "format")
    # display 名不得出现在磁盘布局里 / display name MUST NOT appear on disk
    assert not (home / "mcp" / "My_Editor").exists()


async def test_same_display_name_different_bundle_id_both_visible(tmp_path: Path) -> None:
    """验收 #2：两个 display 名相同、bundle_id 不同的合法共存 Server，各自 SKILL 均可见、互不拒绝。

    这是本 issue 的核心失效模式：旧实现下二者撞出同一 ``mcp:<display>:<skill>``，§1.5 被迫拒绝
    第二注册者 → 一个**合法** SKILL 对 Agent 永久隐身。
    """
    res_a = await _mount_skill(tmp_path, "a", "review", description="from A")
    res_b = await _mount_skill(tmp_path, "b", "review", description="from B")
    reg = SkillRegistry()
    home = tmp_path / "home"

    mgr = FakeManager(
        [("editor-a", res_a), ("editor-b", res_b)],
        # 同一个 display 名——协议允许（name 纯 display、允许碰撞）
        display_names={"editor-a": "Editor", "editor-b": "Editor"},
    )
    names = await stage_mcp_skills(mgr, reg, home)

    assert sorted(names) == ["mcp:editor-a:review", "mcp:editor-b:review"]
    assert len(reg) == 2  # 无一被「拒绝第二注册者」误杀
    assert reg.resolve("mcp:editor-a:review")["description"] == "from A"  # type: ignore[index]
    assert reg.resolve("mcp:editor-b:review")["description"] == "from B"  # type: ignore[index]
    # 磁盘亦不碰撞（旧实现下两者同为 <home>/mcp/Editor/review）
    assert (home / "mcp" / "editor-a" / "review" / "SKILL.md").is_file()
    assert (home / "mcp" / "editor-b" / "review" / "SKILL.md").is_file()


@pytest.mark.parametrize(
    ("bundle_id", "desc"),
    [
        ("acme-editor", "显式 bundle_id / explicit"),
        ("bundle_a1b2c3d4e5f60718", "hash-fallback（CJK / 全符号 name 派生）/ hash fallback"),
        ("My_Server", "auto-derive 保留大小写 / auto-derive, case preserved"),
        ("a" * 80, "超 64 字符：协议 §1.4 已删该上限，MUST 仍可见 / no length cap"),
    ],
)
async def test_bundle_id_shapes_enter_segment_verbatim(tmp_path: Path, bundle_id: str, desc: str) -> None:
    """验收 #3：显式 / hash-fallback / auto-derive / 超长 四种 bundle_id 形态均原样进段。"""
    res = await _mount_skill(tmp_path, "s", "summarize")
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([(bundle_id, res)]), reg, home)

    assert names == [f"mcp:{bundle_id}:summarize"], desc
    assert reg.resolve(f"mcp:{bundle_id}:summarize")["source"] == f"mcp:{bundle_id}"  # type: ignore[index]


async def test_mcp_name_synthesis_failure_skips_only_offender(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """mcp frontmatter ``name`` 非严格 kebab → 该 SKILL 判废（不入册 + 记 ERROR），**合法兄弟仍入册**。

    skill.md §1.5：装配 Registry 时合成失败的 SKILL 不入册、记 ERROR，**不**向 Agent 硬报错——
    batch 接口须对部分失败健壮，一颗坏苹果不得拖垮同 server 的其余 SKILL。
    """
    bad = await _mount_skill(tmp_path, "bad", "Bad-Leaf")  # 大写 → 非严格 kebab
    good = await _mount_skill(tmp_path, "good", "fine-skill")
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", bad), ("srv", good)]), reg, home)

    assert names == ["mcp:srv:fine-skill"]  # 合法兄弟不受牵连
    assert len(reg) == 1
    assert any("skill name synthesis failed" in r.getMessage() for r in staging_logs.records)
    # 判废者的 staging 目录已清理，不留半成品 / offender's staging dir cleaned up
    assert not (home / "mcp" / "srv" / "Bad-Leaf").exists()


async def test_resource_without_source_meta_skipped(tmp_path: Path) -> None:
    # 无 _meta.source 的 skill:// 资源（子资源 / 未声明）→ 非 SKILL 根 → 跳过
    plain = Resource(uri="skill://h/not-a-root", name="not-a-root")
    reg = SkillRegistry()
    names = await stage_mcp_skills(FakeManager([("srv", plain)]), reg, tmp_path / "home")
    assert names == []
    assert len(reg) == 0


# ── #188 根归属：URI 前缀优先于 _meta.source ────────────────────────────────────
async def test_covered_child_with_source_meta_not_materialized_as_root(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """#188 规则 1+2：provider 给根+子资源全部打 ``_meta.source`` → 被根前缀覆盖的子资源
    不再当独立根物化（无 ERROR 刷屏、无目录抖动）——归属判断优先于 meta 判断。"""
    root_uri = "skill://h/my-skill"
    sub1_uri = "skill://h/my-skill/SKILL.md"
    sub2_uri = "skill://h/my-skill/assets/logo.bin"
    root = _root(root_uri, {"source": "resources"})
    sub1 = _root(sub1_uri, {"source": "resources"})  # 不合规：子资源携带 source
    sub2 = _root(sub2_uri, {"source": "resources"})
    reads = {
        sub1_uri: ReadResourceResult(contents=[TextResourceContents(uri=sub1_uri, text=_skill_md())]),
        sub2_uri: ReadResourceResult(contents=[BlobResourceContents(uri=sub2_uri, blob=base64.b64encode(b"\x00\x01\x02").decode())]),
    }
    mgr = FakeManager([("srv", root), ("srv", sub1), ("srv", sub2)], reads=reads)
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(mgr, reg, home)

    assert names == ["mcp:srv:my-skill"]  # 仅真根注册一次
    assert len(reg) == 1
    path = Path(reg.resolve("mcp:srv:my-skill")["path"])  # type: ignore[index]
    assert (path / "SKILL.md").is_file()
    assert (path / "assets" / "logo.bin").read_bytes() == b"\x00\x01\x02"
    # 被覆盖资源：汇总 WARNING + 逐条 DEBUG；不得出现任何 ERROR（pre-fix 是 ERROR 刷屏）
    assert not any(r.levelno == logging.ERROR for r in staging_logs.records)
    assert any(r.levelno == logging.WARNING and "covered by other roots" in r.getMessage() for r in staging_logs.records)
    assert any(
        r.levelno == logging.DEBUG and sub1_uri in r.getMessage() and "covered by skill resource" in r.getMessage()
        for r in staging_logs.records
    )
    assert any(r.levelno == logging.DEBUG and sub2_uri in r.getMessage() for r in staging_logs.records)


async def test_nested_covered_roots_all_excluded(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """#188 嵌套链 A>B>C：B、C 均被排除（覆盖判定不看覆盖者自身是否被覆盖），仅 A 注册。

    三者均用 mounted：resources 父根会把 B/C 这类「目录型节点」也当子资源逐个 read——
    落盘为文件后再遇更深子资源会路径冲突，那是 provider 畸形形状的既存毒丸（与归属排除正交）；
    mounted 无子资源枚举，链式覆盖可干净表达。
    """
    mount_a = tmp_path / "mount" / "a"
    mount_a.mkdir(parents=True)
    (mount_a / "SKILL.md").write_text(_skill_md(name="a"), encoding="utf-8")
    mount_b = tmp_path / "mount" / "b"
    mount_b.mkdir(parents=True)
    (mount_b / "SKILL.md").write_text(_skill_md(name="b"), encoding="utf-8")
    mount_c = tmp_path / "mount" / "c"
    mount_c.mkdir(parents=True)
    (mount_c / "SKILL.md").write_text(_skill_md(name="c"), encoding="utf-8")

    a = _root("skill://h/a", {"source": "mounted", "mount_dir": str(mount_a)})
    b = _root("skill://h/a/b", {"source": "mounted", "mount_dir": str(mount_b)})
    c = _root("skill://h/a/b/c", {"source": "mounted", "mount_dir": str(mount_c)})
    mgr = FakeManager([("srv", a), ("srv", b), ("srv", c)])
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(mgr, reg, home)

    assert names == ["mcp:srv:a"]
    assert len(reg) == 1
    assert (Path(reg.resolve("mcp:srv:a")["path"]) / "SKILL.md").is_file()  # type: ignore[index]
    assert not any(r.levelno == logging.ERROR for r in staging_logs.records)
    assert any(r.levelno == logging.WARNING and "covered by other roots" in r.getMessage() for r in staging_logs.records)
    # 立即父语义：B→A、C→B（最长前缀覆盖者）
    assert any(
        r.levelno == logging.DEBUG
        and "skill://h/a/b covered by skill resource skill://h/a (immediate parent)" in r.getMessage()
        for r in staging_logs.records
    )
    assert any(
        r.levelno == logging.DEBUG
        and "skill://h/a/b/c covered by skill resource skill://h/a/b (immediate parent)" in r.getMessage()
        for r in staging_logs.records
    )


async def test_prefix_boundary_sibling_xy_not_covered(tmp_path: Path) -> None:
    """#188 前缀边界：``skill://h/x`` 不得覆盖 ``skill://h/xy``（必须整段 ``x/`` 为界）。"""
    m_x = tmp_path / "m" / "x"
    m_x.mkdir(parents=True)
    (m_x / "SKILL.md").write_text(_skill_md(name="x"), encoding="utf-8")
    m_xy = tmp_path / "m" / "xy"
    m_xy.mkdir(parents=True)
    (m_xy / "SKILL.md").write_text(_skill_md(name="xy"), encoding="utf-8")

    res_x = _root("skill://h/x", {"source": "mounted", "mount_dir": str(m_x)})
    res_xy = _root("skill://h/xy", {"source": "mounted", "mount_dir": str(m_xy)})
    reg = SkillRegistry()

    names = await stage_mcp_skills(FakeManager([("srv", res_x), ("srv", res_xy)]), reg, tmp_path / "home")

    assert sorted(names) == ["mcp:srv:x", "mcp:srv:xy"]
    assert len(reg) == 2


async def test_mounted_child_covered_by_resources_parent_uniform(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """#188 mode 统一：resources 父根下挂 mounted 子根 → 归属优先于 meta，子根不当根。"""
    parent_uri = "skill://h/p"
    child_uri = "skill://h/p/child"
    md_uri = "skill://h/p/SKILL.md"
    mount = tmp_path / "mount" / "child"
    mount.mkdir(parents=True)
    (mount / "SKILL.md").write_text(_skill_md(name="child"), encoding="utf-8")

    parent = _root(parent_uri, {"source": "resources"})
    child = _root(child_uri, {"source": "mounted", "mount_dir": str(mount)})
    sub_md = Resource(uri=md_uri, name="SKILL.md")
    reads = {
        md_uri: ReadResourceResult(contents=[TextResourceContents(uri=md_uri, text=_skill_md(name="parent"))]),
        child_uri: ReadResourceResult(contents=[TextResourceContents(uri=child_uri, text="covered")]),
    }
    mgr = FakeManager([("srv", parent), ("srv", child), ("srv", sub_md)], reads=reads)
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(mgr, reg, home)

    assert names == ["mcp:srv:parent"]  # mounted 子根被排除，不注册
    assert len(reg) == 1
    assert (Path(reg.resolve("mcp:srv:parent")["path"]) / "SKILL.md").is_file()  # type: ignore[index]
    assert not any(r.levelno == logging.ERROR for r in staging_logs.records)
    assert any(r.levelno == logging.DEBUG and child_uri in r.getMessage() for r in staging_logs.records)


async def test_independent_bad_root_still_errors_sibling_unaffected(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """#188 规则 3：真正独立的坏根（resources 无任何子资源）仍 ERROR + 跳过，且不阻断兄弟。"""
    good = await _mount_skill(tmp_path, "good", "fine-skill")
    bad = _root("skill://h/lonely", {"source": "resources"})  # 无子资源 → 物化失败
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(FakeManager([("srv", good), ("srv", bad)]), reg, home)

    assert names == ["mcp:srv:fine-skill"]  # 合法兄弟不受牵连
    assert len(reg) == 1
    assert any(r.levelno == logging.ERROR and "materialize failed" in r.getMessage() for r in staging_logs.records)
    assert not (home / "mcp" / "srv" / "lonely").exists()


def test_partition_roots_leafless_guard_and_order() -> None:
    """#188 纯函数：leaf-less 候选不得覆盖别人；未被覆盖的 leaf-less 保留在 roots（ERROR 分支可达）；保序。"""
    leafless = _root("skill://h", {"source": "resources"})
    a = _root("skill://h/a", {"source": "resources"})
    sub_a = _root("skill://h/a/SKILL.md", {"source": "resources"})  # 被 a 覆盖
    plain = Resource(uri="skill://h/a/notes.md", name="notes.md")  # 无 source：非候选、不覆盖、不被排除

    roots, covered = _partition_roots([leafless, a, sub_a, plain])

    assert [str(r.uri) for r in roots] == ["skill://h", "skill://h/a"]  # 保序；leafless 未被覆盖
    assert covered == {"skill://h/a/SKILL.md": "skill://h/a"}


def test_partition_roots_nested_chain_and_duplicate_uri() -> None:
    """#188 纯函数：嵌套 A>B>C 记立即父（B→A、C→B）；重复 URI 不自覆盖（去重属范围外，保持现状）。"""
    a = _root("skill://h/a", {"source": "resources"})
    b = _root("skill://h/a/b", {"source": "resources"})
    c = _root("skill://h/a/b/c", {"source": "resources"})
    dup = _root("skill://h/a", {"source": "resources"})

    roots, covered = _partition_roots([a, b, c, dup])

    assert [str(r.uri) for r in roots] == ["skill://h/a", "skill://h/a"]  # 重复副本都留在 roots
    assert covered == {"skill://h/a/b": "skill://h/a", "skill://h/a/b/c": "skill://h/a/b"}


# ── user 源 DropIn（就地发现，不 staging，#60）────────────────────────────────
def _write_user_skill(
    root: Path, skill_dir_name: str, *, fm_name: str | None = None, description: str = "do thing", tags: list[str] | None = None
) -> Path:
    """在发现根下写一个 ``<skill_dir_name>/SKILL.md`` / write a DropIn skill dir under a root。"""
    d = root / skill_dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        _skill_md(name=skill_dir_name if fm_name is None else fm_name, description=description, tags=tags), encoding="utf-8"
    )
    return d


def test_user_global_only_in_place(tmp_path: Path) -> None:
    # 仅 $A2C_SKILL_HOME/user/ 全局源：就地发现、不复制；name=basename、source=user、无 uri
    home = tmp_path / "home"
    _write_user_skill(home / "user", "alpha", description="第一")
    _write_user_skill(home / "user", "beta", description="第二")
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert sorted(names) == ["alpha", "beta"]
    ref = reg.resolve("alpha")
    assert ref is not None
    assert ref["source"] == "user"
    assert "uri" not in ref  # user 源协议表面不带 skill://
    assert ref["path"] == str((home / "user" / "alpha").resolve())  # 就地包根，未复制
    assert ref["description"] == "第一"
    assert ref["license"] == "MIT"
    assert ref["allowed_tools"] == ["read", "write"]


def test_user_tags_passthrough(tmp_path: Path) -> None:
    # user 源 frontmatter tags → ref["tags"] 透传（三源共用单点之二）
    home = tmp_path / "home"
    _write_user_skill(home / "user", "tagged", tags=["report"])
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert names == ["tagged"]
    ref = reg.resolve("tagged")
    assert ref is not None
    assert ref["tags"] == ["report"]


def test_user_no_tags_key_absent(tmp_path: Path) -> None:
    # frontmatter 无 tags → A2CSkillRef 键**整个缺席**（非 null；skill.md §1.5 wire 契约）
    home = tmp_path / "home"
    _write_user_skill(home / "user", "plain")
    reg = SkillRegistry()

    stage_user_skills(reg, home)

    ref = reg.resolve("plain")
    assert ref is not None
    assert "tags" not in ref


def test_user_tags_empty_list_passthrough(tmp_path: Path) -> None:
    # `tags: []` 显式空列表 = 合法 list[str]（0–10 条含 0）→ 填 []，与「键缺席」语义不同
    home = tmp_path / "home"
    d = home / "user" / "empty-tags"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: empty-tags\ndescription: d\ntags: []\n---\n# body\n", encoding="utf-8")
    reg = SkillRegistry()

    stage_user_skills(reg, home)

    ref = reg.resolve("empty-tags")
    assert ref is not None
    assert ref["tags"] == []


@pytest.mark.parametrize(
    "tags_yaml",
    ["tags: not-a-list", "tags: [ok, 1]", "tags: 123", "tags:"],
)
def test_user_tags_malformed_omitted_with_debug(tmp_path: Path, staging_logs: pytest.LogCaptureFixture, tags_yaml: str) -> None:
    # 畸形 tags（非 list / 混合元素 / 非 str / None）→ 省略该字段 + DEBUG 日志，SKILL 照常注册（skill.md §1.5：
    # 纯透传不校验，不触发任何拒绝路径）。**勿照抄 allowed-tools 强制规范化**（非 list 也包 [str(t)]）。
    home = tmp_path / "home"
    d = home / "user" / "tagged"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: tagged\ndescription: d\n{tags_yaml}\n---\n# body\n", encoding="utf-8")
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert names == ["tagged"]  # 照常注册
    ref = reg.resolve("tagged")
    assert ref is not None
    assert "tags" not in ref  # 键整个缺席（非 null）
    assert any(r.levelno == logging.DEBUG and "not list[str]" in r.getMessage() for r in staging_logs.records)


def test_user_home_only_no_workdir_dimension(tmp_path: Path) -> None:
    # #116：user 源仅扫 <home>/user/ 单根；workdir 维度 SKILL 已下沉 MCP 服务（.tfrobot/skills 不再被发现）
    home = tmp_path / "home"
    _write_user_skill(home / "user", "global-skill")
    wd = tmp_path / "ws"
    _write_user_skill(wd / ".tfrobot" / "skills", "proj-a")
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert names == ["global-skill"]
    assert reg.resolve("proj-a") is None  # workdir DropIn 不再进 user 源


def test_user_basename_not_kebab_skipped(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    # 目录 basename 非严格 kebab → 跳过 + ERROR；合法兄弟仍入册（部分失败健壮）
    home = tmp_path / "home"
    _write_user_skill(home / "user", "Bad_Name")  # 含大写 + 下划线 → 非 kebab
    _write_user_skill(home / "user", "good-one")
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert names == ["good-one"]
    assert len(reg) == 1
    assert any(r.levelno == logging.ERROR and "name invalid" in r.getMessage() for r in staging_logs.records)


def test_user_basename_credential_like_name_redacted(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """非法目录名分支不得回显凭据形状（#281）：scp-like 目录名（`user@host:path`）整条路径遮蔽；
    合法兄弟仍入册（失败隔离正对照）。"""
    home = tmp_path / "home"
    _write_user_skill(home / "user", "deploy-token@example.com:org")
    _write_user_skill(home / "user", "good-one")
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert names == ["good-one"]
    text = _no_secret(staging_logs.records)
    assert "name invalid" in text  # 正对照：确实走了该分支
    assert "deploy-token" not in text and "example.com:org" not in text
    assert "<redacted-git-source>" in text  # 占位符正向断言（删掉脱敏参数也会转红）


def test_user_basename_control_chars_are_escaped(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """非法目录名里的控制字符 / 空白类必须转义或整段遮蔽（#281 复审）：换行可伪造日志行，空白会把
    userinfo 劈成两半而两半都不命中规则。"""
    home = tmp_path / "home"
    for bad in ("alice:pw\n@host", "bad\tname", "alice:pw @host", "alice:pw @host", "alice:pw　@host"):
        d = home / "user" / bad
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")

    names = stage_user_skills(SkillRegistry(), home)

    assert names == []
    msgs = [r.getMessage() for r in staging_logs.records if r.levelno == logging.ERROR]
    assert len(msgs) == 5 and all("name invalid" in m for m in msgs)
    text = " | ".join(msgs)  # 用非空白分隔，避免把 join 用的换行误当泄漏
    assert "\n" not in text and "\t" not in text  # 换行 / TAB 已转义成可见形式
    assert "alice:pw" not in text  # 被控制字符 / 空白劈开的 userinfo 不得残留
    assert "<redacted-git-source>" in text
    # parent 落点逐条可读（basename 遮蔽不牺牲定位）——逐记录断言：拼接文本会被任一条「本来就干净」的
    # 记录掩盖（M18 变异即因此存活）。
    parent = str(home / "user")
    assert all(parent in m for m in msgs), [m for m in msgs if parent not in m]


def test_user_skill_md_unreadable_log_masks_home_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staging_logs: pytest.LogCaptureFixture
) -> None:
    """`_build_user_ref` 不可读分支的路径轴脱敏真实用例（#281）：home 根含 userinfo 段时不得回显。"""
    home = tmp_path / "alice:pw@host"
    _write_user_skill(home / "user", "toctou")
    orig_read = Path.read_text

    def boom(self: Path, *a: object, **k: object) -> str:
        if self.name == "SKILL.md":
            raise OSError("simulated unreadable after discovery (TOCTOU)")
        return orig_read(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)

    names = stage_user_skills(SkillRegistry(), home)

    assert names == []
    text = _no_secret(staging_logs.records)
    assert "unreadable" in text  # 正对照：确实走了该分支
    assert "pw@" not in text and "alice:" not in text
    assert "<redacted-git-source>" in text  # 占位符正向断言


def test_user_missing_description_log_masks_home_credential(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """`_build_user_ref` 缺 description 分支的路径轴脱敏真实用例（#281）。"""
    home = tmp_path / "alice:pw@host"
    d = home / "user" / "no-desc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: no-desc\n---\n# body\n", encoding="utf-8")

    names = stage_user_skills(SkillRegistry(), home)

    assert names == []
    text = _no_secret(staging_logs.records)
    assert "missing required 'description'" in text  # 正对照：确实走了该分支
    assert "pw@" not in text and "alice:" not in text
    assert "<redacted-git-source>" in text  # 占位符正向断言


def test_user_basename_query_tail_redacted(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """basename 天然不含分隔符：`foo?token=…` / `foo#frag` 必须由 path 轴遮蔽（#281 复审 🔴——
    单值漏斗的 path-token 谓词不触发，直接把 basename 交给它会回退成原样回显）。"""
    home = tmp_path / "home"
    for bad in ("foo?token=SECRET_Q", "foo#frag"):
        d = home / "user" / bad
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")

    names = stage_user_skills(SkillRegistry(), home)

    assert names == []
    text = _no_secret(staging_logs.records)
    assert "name invalid" in text
    assert "SECRET_Q" not in text and "frag" not in text
    assert "foo<redacted-source>" in text  # 正向：尾部被 path 轴截断


def test_user_basename_parent_position_kept_readable(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """parent 走 path 轴、只有凭据段被遮（#281 复审 🟡3）：普通段 `build:2024` 必须仍可见——
    若把 parent 也交给单值漏斗，整条路径会被整串遮掉。"""
    home = tmp_path / "build:2024" / "alice:pw@host"
    d = home / "user" / "Bad_Name"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")

    names = stage_user_skills(SkillRegistry(), home)

    assert names == []
    msgs = [r.getMessage() for r in staging_logs.records if r.levelno == logging.ERROR]
    assert len(msgs) == 1 and "name invalid" in msgs[0]
    assert "build:2024" in msgs[0]  # 非凭据 parent 段可见
    assert "<redacted-source>" in msgs[0]  # 凭据段被遮
    assert "pw@" not in msgs[0] and "alice:" not in msgs[0]


@pytest.mark.parametrize(
    "fm_name, secret, expected",
    [
        ("https://alice:FM_PW@example.com/r.git", "FM_PW", "https://example.com/r.git"),  # userinfo URL → 严格展示
        ("deploy-token@example.com:org", "deploy-token", "<redacted-git-source>"),  # scp-like → 占位符
    ],
    ids=["userinfo-url", "scp-like"],
)
def test_user_frontmatter_name_mismatch_debug_is_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture, fm_name: str, secret: str, expected: str
) -> None:
    """`_build_user_ref` frontmatter.name 不一致的 DEBUG 分支（#281）：name 可带凭据，不得回显。

    scp-like 形态是复审补口：path 轴不识别 `user@host:path`，只有散文轴能遮。
    """
    home = tmp_path / "home"
    _write_user_skill(home / "user", "real-dir", fm_name=fm_name)

    names = stage_user_skills(SkillRegistry(), home)

    assert names == ["real-dir"]
    text = _no_secret(staging_logs.records)
    assert "basename is authoritative" in text  # 正对照：确实走了该分支
    assert secret not in text
    assert expected in text  # 正向断言：脱敏结果就是这条形态（删掉脱敏会转红）


def test_user_dropin_debug_paths_are_redacted(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """发现期 DEBUG 日志（根下直放 SKILL.md / wrapper 深层 SKILL.md）同样不得回显凭据路径（#281 复审）。"""
    home = tmp_path / "alice:pw@host"
    root = home / "user"
    (root / "deploy-token@example.com:org" / "sub").mkdir(parents=True)
    (root / "SKILL.md").write_text(_skill_md(), encoding="utf-8")  # 根下直放 → 第一条 DEBUG
    (root / "deploy-token@example.com:org" / "sub" / "SKILL.md").write_text(_skill_md(), encoding="utf-8")

    names = stage_user_skills(SkillRegistry(), home)

    assert names == []
    text = _no_secret(staging_logs.records)
    assert "directly under root" in text and "one-level depth" in text  # 正对照：两条 DEBUG 都执行
    assert "pw@" not in text and "alice:" not in text
    assert "deploy-token" not in text
    assert "<redacted-git-source>" in text


def test_frontmatter_yaml_error_log_is_redacted(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """PyYAML 错误文本可能带回出错行片段（远端 SKILL.md 的凭据 URL）——日志必须脱敏（#281）。"""
    home = tmp_path / "home"
    d = home / "user" / "bad-yaml"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: [unclosed\nsource: https://alice:PW_YAML@example.com/r.git\n---\n# body\n",
        encoding="utf-8",
    )

    names = stage_user_skills(SkillRegistry(), home)

    assert names == []  # frontmatter 解析失败 → 缺 description → 跳过（失败隔离）
    text = _no_secret(staging_logs.records)
    assert "YAML parse failed" in text  # 正对照：确实命中 PyYAML 错误分支
    assert "source: https://example" in text  # 正向：出错行确实被回显（且已被脱敏），不是整段丢弃
    assert "PW_YAML" not in text and "alice:" not in text


def test_user_deeper_skill_md_ignored_with_debug(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    # 深于一级的 SKILL.md（<root>/a/b/SKILL.md）→ 忽略 + DEBUG；根下一级的仍发现（验收第 2 条）
    home = tmp_path / "home"
    root = home / "user"
    _write_user_skill(root, "ok-skill")
    nested = root / "wrapper" / "inner"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(_skill_md(name="inner", description="too deep"), encoding="utf-8")
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert names == ["ok-skill"]  # inner（二级）被忽略
    assert any(r.levelno == logging.DEBUG and "not at one-level depth" in r.getMessage() for r in staging_logs.records)


def test_user_missing_description_skipped(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    # frontmatter 缺 description（A2CSkillRef 必填）→ 跳过 + ERROR
    home = tmp_path / "home"
    d = home / "user" / "no-desc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: no-desc\n---\n# body\n", encoding="utf-8")
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert names == []
    assert len(reg) == 0
    assert any(r.levelno == logging.ERROR and "missing required 'description'" in r.getMessage() for r in staging_logs.records)


def test_user_frontmatter_name_mismatch_basename_wins(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    # frontmatter.name 与目录 basename 不一致 → basename 权威（就地不可改名）+ DEBUG
    home = tmp_path / "home"
    _write_user_skill(home / "user", "real-dir", fm_name="other-name")
    reg = SkillRegistry()

    names = stage_user_skills(reg, home)

    assert names == ["real-dir"]  # 目录 basename 胜，非 frontmatter.name
    assert reg.resolve("other-name") is None
    assert any(r.levelno == logging.DEBUG and "basename is authoritative" in r.getMessage() for r in staging_logs.records)


def test_user_rescan_idempotent_updates(tmp_path: Path) -> None:
    # 重扫幂等：已注册 → update（不重复、不报错），内容变更被刷新
    home = tmp_path / "home"
    skill = _write_user_skill(home / "user", "iter-skill", description="v1")
    reg = SkillRegistry()

    first = stage_user_skills(reg, home)
    assert first == ["iter-skill"]
    assert reg.resolve("iter-skill")["description"] == "v1"  # type: ignore[index]

    (skill / "SKILL.md").write_text(_skill_md(name="iter-skill", description="v2"), encoding="utf-8")
    second = stage_user_skills(reg, home)

    assert second == ["iter-skill"]
    assert len(reg) == 1  # 未重复注册
    assert reg.resolve("iter-skill")["description"] == "v2"  # 刷新生效  # type: ignore[index]


def test_user_missing_roots_tolerated(tmp_path: Path) -> None:
    # 发现根不存在（home/user 没建）→ 返回空、不抛
    home = tmp_path / "nonexistent-home"
    reg = SkillRegistry()
    names = stage_user_skills(reg, home)
    assert names == []
    assert len(reg) == 0


def test_user_skill_md_unreadable_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staging_logs: pytest.LogCaptureFixture) -> None:
    # 发现后 SKILL.md 不可读（TOCTOU）→ _build_user_ref 的 OSError 分支 → 跳过 + ERROR、不抛
    home = tmp_path / "home"
    _write_user_skill(home / "user", "toctou")
    reg = SkillRegistry()

    orig_read = Path.read_text

    def boom(self: Path, *a: object, **k: object) -> str:
        if self.name == "SKILL.md":
            raise OSError("simulated unreadable after discovery (TOCTOU)")
        return orig_read(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)  # 发现走 is_file()，不读；仅 _build_user_ref 读 → 触发分支

    names = stage_user_skills(reg, home)

    assert names == []
    assert len(reg) == 0
    assert any(r.levelno == logging.ERROR and "unreadable" in r.getMessage() for r in staging_logs.records)


def test_user_home_rescan_no_spurious_warning(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    # home 单根重复扫描（幂等 register_or_update）→ 不产生任何 WARN（#116 后无跨根遮蔽语义）
    home = tmp_path / "home"
    _write_user_skill(home / "user", "uniq")
    reg = SkillRegistry()

    first = stage_user_skills(reg, home)
    second = stage_user_skills(reg, home)

    assert first == ["uniq"] and second == ["uniq"]
    assert len(reg) == 1
    assert not any(r.levelno == logging.WARNING for r in staging_logs.records)


# ── #261 marketplace URL 展示脱敏：异常 / 日志 / clone 入参 ────────────────────
_CRED_CLONE_URL = "https://user:FAKE_TOKEN@127.0.0.1:1/org/repo.git"
_CRED_SSH_URL = "ssh://alice:FAKE_TOKEN@127.0.0.1:1/org/repo.git"


def _no_secret(records: list[logging.LogRecord] | object) -> str:
    """把所有日志记录（含 exc_info 渲染出的 traceback 文本）拼成文本，供负断言 / Log text incl. traceback."""
    chunks: list[str] = []
    for r in records:  # type: ignore[union-attr]
        chunks.append(r.getMessage())
        if r.exc_info is not None:
            chunks.append("".join(logging.Formatter().formatException(r.exc_info)))
        if r.exc_text:
            chunks.append(r.exc_text)
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_run_git_failure_redacts_argv_and_stderr(tmp_path: Path) -> None:
    """真实 git 失败：异常消息不得含 argv / stderr 中的凭据 URL。"""
    dest = tmp_path / "dest"
    with pytest.raises(staging_mod.SkillStagingError) as ei:
        await staging_mod._run_git(["clone", "--", _CRED_CLONE_URL, str(dest)], timeout=10, env=None)
    msg = str(ei.value)
    assert "FAKE_TOKEN" not in msg
    assert "https://127.0.0.1:1/org/repo.git" in msg


@pytest.mark.asyncio
async def test_marketplace_clone_failure_log_redacts(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """marketplace clone 失败日志（真实 clone 分支）：不含凭据，含占位符。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "demo", {"type": "git", "url": _CRED_CLONE_URL}, SkillRegistry(), home, timeout=10
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "FAKE_TOKEN" not in text
    assert "https://127.0.0.1:1/org/repo.git" in text


@pytest.mark.asyncio
async def test_marketplace_clone_fallback_log_redacts(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """SSH→HTTPS 回退日志：ssh 原始 URL 的 userinfo 不得落日志。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "demo", {"type": "git", "url": _CRED_SSH_URL}, SkillRegistry(), home, timeout=10
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "FAKE_TOKEN" not in text
    assert "ssh://127.0.0.1:1/org/repo.git" in text
    assert "https://127.0.0.1/org/repo.git" in text


@pytest.mark.asyncio
async def test_marketplace_invalid_source_log_redacts(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """source 非法日志：不插值 source dict / 异常全文，只记脱敏后的 name（复审 🟡）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "demo", {"type": "weird", "url": _CRED_CLONE_URL}, SkillRegistry(), home, timeout=10
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "FAKE_TOKEN" not in text
    assert "demo" in text
    assert "127.0.0.1" not in text


def test_scp_like_token_in_error_text_is_placeholder() -> None:
    """scp-like 在错误/日志文本中整体占位符（strict 语义，与 rust 一致）。"""
    out = redact_git_urls_in_text("git clone alice@127.0.0.1:org/repo.git failed")
    assert "alice@" not in out
    assert REDACTED_GIT_SOURCE in out


@pytest.mark.asyncio
async def test_marketplace_clone_passes_raw_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """存储 / git 入参不变：clone 收到的是原始 URL（含 userinfo），展示层才脱敏。"""
    captured: dict[str, list[str]] = {}

    async def fake_run_git(args: object, *, timeout: float, env: object) -> str:
        captured["args"] = list(args)  # type: ignore[arg-type]
        return ""

    monkeypatch.setattr(staging_mod, "_run_git", fake_run_git)
    await staging_mod._git_clone_marketplace(_CRED_CLONE_URL, tmp_path / "dest", timeout=5, env=None)
    assert _CRED_CLONE_URL in captured["args"]


@pytest.mark.asyncio
async def test_marketplace_refresh_pull_failure_log_redacts(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """refresh 腿（git pull 失败 → 全量重 clone）：WARN/ERROR 均不得含凭据（rust 同款边界）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    clone_dir = staging_mod.marketplace_skill_dir(home, "demo")
    clone_dir.mkdir(parents=True)
    _git_identity = ["-c", "user.email=t@example.com", "-c", "user.name=T", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], cwd=str(clone_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", *_git_identity, "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=str(clone_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", _CRED_CLONE_URL],
        cwd=str(clone_dir),
        check=True,
        capture_output=True,
    )

    names = await staging_mod.stage_marketplace_skills(
        "demo", {"type": "git", "url": _CRED_CLONE_URL}, SkillRegistry(), home, refresh=True, timeout=10
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "FAKE_TOKEN" not in text
    warnings = [r for r in staging_logs.records if r.levelno == logging.WARNING]
    assert warnings, text
    assert any("pull" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_marketplace_invalid_name_log_redacts(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """账本键被手编成携带 URL 凭据的串：非法 name 日志必须脱敏（rust 同款用例）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "x=https://alice:PW_LEAK@example.com/repo.git",
        {"type": "git", "url": "https://example.com/r.git"},
        SkillRegistry(),
        home,
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "PW_LEAK" not in text
    assert "alice" not in text


@pytest.mark.asyncio
async def test_marketplace_invalid_source_log_hides_non_url_secret(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """非 URL 形态 secret（如裸 `glpat-...`）不得经 SkillSourceError 全文进入日志。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "demo", {"type": "git", "url": "glpat-SECRET_TOKEN"}, SkillRegistry(), home, timeout=10
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "glpat-SECRET_TOKEN" not in text


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX os.fsencode 才抛 UnicodeEncodeError；Windows 会真实尝试网络 clone（复审 🟡2）",
)
@pytest.mark.asyncio
async def test_marketplace_surrogate_name_and_url_degrade_without_raising(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """孤立代理项（手编 ledger / JSON 转义可达）：只记日志、不抛，展示层占位符。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    names = await staging_mod.stage_marketplace_skills(
        "x=\ud800", {"type": "git", "url": "https://example.com/r.git"}, SkillRegistry(), home
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "x=" in text
    assert "\ud800" not in text, "日志不得含未转义的孤立代理项"

    names = await staging_mod.stage_marketplace_skills(
        "demo", {"type": "git", "url": "https://example.com/\ud800"}, SkillRegistry(), home, timeout=10
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "git spawn failed" in text
    assert "\ud800" not in text


@pytest.mark.asyncio
async def test_marketplace_nul_url_degrades_without_raising(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """NUL 字节 URL 触发 ValueError 臂（spawn 守卫；复审 🟡1）：返回 [] 且日志含 git spawn failed。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "demo", {"type": "git", "url": "https://example.com/\x00"}, SkillRegistry(), home, timeout=10
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "git spawn failed" in text


# ── #263 plugin source / staging 回显脱敏（构造期 SkillSourceError + 日志面）────────
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_plugin_source_errors_log_redacts_all_vectors(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """rust#154 五向量：plugin staging 失败日志（name / plugin_name / str(e)）均不得含凭据。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        """{"plugins":[
            {"name":"ftp","source":{"source":"url","url":"ftp://cnb:FAKE_TOKEN@example.com/org/repo.git?token=QUERY#FRAGMENT"}},
            {"name":"prefix","source":"key=https://user:PW_PREFIX@example.com/org/repo.git"},
            {"name":"scp","source":{"source":"github","repo":"git@example.com:org/repo.git"}},
            {"name":"x=https://alice:PW_NAME@example.com/repo.git","source":{"source":"url","url":"bogus"}},
            {"name":"local-leak","source":"./x=https://alice:PW_LOCAL@example.com/repo.git"}
        ]}""",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "staging failed, skipped" in text
    for secret in (
        "cnb",
        "FAKE_TOKEN",
        "QUERY",
        "FRAGMENT",
        "user:",
        "PW_PREFIX",
        "PW_NAME",
        "PW_LOCAL",
        "git@example.com",
    ):
        assert secret not in text


@pytest.mark.asyncio
async def test_plugin_entry_missing_name_with_credentials_is_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """entry 缺 name 且 entry 内嵌凭据 URL：日志不得回显整包 entry。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        '{"plugins":[{"source":{"source":"url","url":"https://alice:PW_ENTRY@example.com/r.git"}}]}',
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "plugin entry invalid" in text
    assert "PW_ENTRY" not in text
    assert "alice:" not in text


@pytest.mark.asyncio
async def test_plugin_root_not_found_redacts_path_credentials(tmp_path: Path) -> None:
    """相对源解析出的路径内嵌凭据（rust#154 向量⑤）：「plugin root not found」不得回显凭据。"""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    with pytest.raises(staging_mod.SkillStagingError) as ei:
        await staging_mod.locate_plugin_root(
            "acme",
            "local-leak",
            {"source": "./x=https://alice:PW_PATH@example.com/repo.git"},
            catalog_dir,
            "./plugins",
            tmp_path / "home",
            None,
        )
    msg = str(ei.value)
    assert "PW_PATH" not in msg
    assert "alice:" not in msg


@pytest.mark.asyncio
async def test_plugin_clone_passes_raw_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git 入参不变：plugin clone 的 argv 仍是原始凭据 URL（只有展示面脱敏）。"""
    from a2c_smcp.computer.skills.sources import GitCloneSpec

    captured: dict[str, list[str]] = {}

    async def fake_run_git(args: object, *, timeout: float, env: object) -> str:
        captured["args"] = list(args)  # type: ignore[arg-type]
        return "deadbeef" * 5

    monkeypatch.setattr(staging_mod, "_run_git", fake_run_git)
    await staging_mod._git_clone_plugin(
        GitCloneSpec(url=_CRED_CLONE_URL), tmp_path / "dest", timeout=5, env=None
    )
    assert _CRED_CLONE_URL in captured["args"]


# ── #263 / #272：entry 名 / 非对象 entry / 路径穿越 ────────────────────────────
@pytest.mark.asyncio
async def test_non_object_plugin_entry_log_is_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """非对象 plugin entry（字符串形态）同样按凭据脱敏后再记日志。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        '{"plugins":["https://alice:PW_ENTRY_RAW@example.com/r.git"]}', encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "non-object plugin entry" in text
    assert "PW_ENTRY_RAW" not in text
    assert "alice:" not in text


@pytest.mark.parametrize("bad", ["../../evil", "/tmp/evil", "Foo", "a/b", ".hidden", "x" * 65, "tab\tname"])
def test_entry_plugin_name_rejects_traversal_and_non_kebab(bad: str) -> None:
    """#272：entry name 直接作路径段（且 clone 前 rmtree）⇒ 必须严格 kebab（协议 §4.1）。"""
    with pytest.raises(staging_mod.SkillStagingError) as ei:
        staging_mod._entry_plugin_name({"name": bad})
    assert "kebab" in str(ei.value)


def test_entry_plugin_name_accepts_kebab() -> None:
    assert staging_mod._entry_plugin_name({"name": "data-toolkit"}) == "data-toolkit"


@pytest.mark.asyncio
async def test_traversal_entry_name_cannot_delete_outside_dir(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """#272 回归锁：恶意 name（绝对路径 / 越界相对路径）不得触发 home 外目录删除。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    # 名字从 `<home>/marketplace/.plugins/acme/` 上跳四级正好落到 `tmp_path`，指向下面的 sentinel 目录。
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        '{"plugins":[{"name":"../../../../sentinel","source":{"source":"url","url":"https://example.com/r.git"}}]}',
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("keep", encoding="utf-8")

    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    assert (sentinel / "keep.txt").is_file()
    assert "plugin entry invalid" in _no_secret(staging_logs.records)


@pytest.mark.asyncio
async def test_locate_plugin_root_rejects_escaping_install_path(tmp_path: Path) -> None:
    """#272 纵深防御：即使上游绕过 name 校验，越界 install 路径也在 clone/rmtree 之前被拒。"""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    with pytest.raises(staging_mod.SkillStagingError) as ei:
        await staging_mod.locate_plugin_root(
            "acme",
            "../../../../escape",
            {"source": {"source": "url", "url": "https://example.com/r.git"}},
            catalog_dir,
            "./plugins",
            tmp_path / "home",
            None,
        )
    assert "escapes the skill home" in str(ei.value)


def test_no_secret_helper_covers_exc_info() -> None:
    """`_no_secret` 必须覆盖 exc_info / exc_text（traceback 同样属回显面）。"""
    try:
        raise RuntimeError("https://alice:PW_EXC@example.com/r.git")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "msg", None, sys.exc_info())
    assert "PW_EXC" in _no_secret([rec])
    rec2 = logging.LogRecord("t", logging.ERROR, __file__, 1, "msg", None, None)
    rec2.exc_text = "Traceback ... https://alice:PW_EXC@example.com/r.git"
    assert "PW_EXC" in _no_secret([rec2])


@pytest.mark.parametrize("bad", [".", "", "..", " ", "a/b", "\\", "C:"])
def test_entry_plugin_name_rejects_dot_and_empty_forms(bad: str) -> None:
    with pytest.raises(staging_mod.SkillStagingError):
        staging_mod._entry_plugin_name({"name": bad})


@pytest.mark.asyncio
async def test_bare_token_entry_is_not_echoed(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """非对象 entry 只记类型名：裸 token（非 URL 形态）无从脱敏，因此根本不回显整值。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text('{"plugins":["glpat-SECRET_TOKEN"]}', encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "non-object plugin entry" in text
    assert "str" in text  # 记录类型名
    assert "glpat-SECRET_TOKEN" not in text


@pytest.mark.asyncio
async def test_traversal_entry_name_never_reaches_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staging_logs: pytest.LogCaptureFixture
) -> None:
    """恶意 name 在 `marketplace add/refresh` 的 stage 路径上绝不会走到 `_git_clone_plugin`（不会 rmtree）。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        '{"plugins":[{"name":"../../../../sentinel","source":{"source":"url","url":"https://example.com/r.git"}}]}',
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    called: list[object] = []

    async def fake_clone_plugin(*args: object, **kwargs: object) -> Path:
        called.append(args)
        raise AssertionError("clone must not be reached for invalid entry name")

    monkeypatch.setattr(staging_mod, "_git_clone_plugin", fake_clone_plugin)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    assert called == []
    assert "plugin entry invalid" in _no_secret(staging_logs.records)


@pytest.mark.asyncio
async def test_unexpected_staging_error_traceback_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staging_logs: pytest.LogCaptureFixture
) -> None:
    """兜底 `except Exception` 分支：格式化后的 traceback 也必须脱敏（不得靠 logging 直接渲染 exc_info）。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        '{"plugins":[{"name":"audit","source":{"source":"url","url":"https://example.com/r.git"}}]}', encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    async def boom(*args: object, **kwargs: object):
        raise RuntimeError("unexpected failure for https://alice:PW_TRACE@example.com/r.git?token=QUERY")

    monkeypatch.setattr(staging_mod, "_stage_one_plugin", boom)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "unexpected staging error" in text
    # 正对照：traceback 本身必须仍在日志里（脱敏不能靠「整段丢弃」蒙混）
    assert "Traceback (most recent call last)" in text and "RuntimeError" in text
    assert "PW_TRACE" not in text
    assert "token=QUERY" not in text


@pytest.mark.asyncio
async def test_plugin_root_message_redacts_query_fragment_without_userinfo(tmp_path: Path) -> None:
    """staging 侧同样要以原始 source 回显：向量**不含 userinfo**（否则 scp-like 兜底会掩盖问题）。"""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    with pytest.raises(staging_mod.SkillStagingError) as ei:
        await staging_mod.locate_plugin_root(
            "acme",
            "ghost",
            {"source": "./x=https://example.com/repo.git?token=QUERY#frag"},
            catalog_dir,
            "./plugins",
            tmp_path / "home",
            None,
        )
    msg = str(ei.value)
    assert "token=QUERY" not in msg
    assert "#frag" not in msg


@pytest.mark.asyncio
async def test_mcp_materialize_failure_traceback_is_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """#263 复审：`stage_mcp_skills` 物化失败分支的 traceback 同样不得带凭证（fetcher 的 archive URL）。"""
    res = Resource(
        uri="skill://host/my-skill/SKILL.md",  # type: ignore[arg-type]
        name="my-skill",
        mimeType="text/markdown",
        _meta={"source": "archive", "archive_uri": "https://alice:PW_ARCH@example.com/s.tar.gz?token=QUERY", "archive_format": "tar.gz"},
    )

    class BoomManager(FakeManager):
        async def read_resource(self, server: str, uri: str) -> ReadResourceResult:
            raise RuntimeError("unused")

    async def boom_fetch(url: str):
        raise RuntimeError(f"download failed for {url}")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_mcp_skills(
        BoomManager([("bid", res)]), SkillRegistry(), home, archive_fetch=boom_fetch
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "materialize failed" in text
    assert "Traceback (most recent call last)" in text  # 正对照：traceback 仍在
    assert "PW_ARCH" not in text
    assert "token=QUERY" not in text


# ── #263 五轮复审：plugin 源 SKILL.md 不可读 / root_uri 回显 ─────────────────────
@pytest.mark.asyncio
async def test_plugin_source_skill_md_unreadable_log_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staging_logs: pytest.LogCaptureFixture
) -> None:
    """plugin（相对）源内嵌凭据 URL 时，SKILL.md 读失败的 OSError 全文不得进日志。"""
    bad = "x=https://alice:PW_UNREAD@example.com/repo.git"
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "audit", "source": f"./plugins/{bad}"}]}), encoding="utf-8"
    )
    skill_md = repo / "plugins" / bad / "skills" / "etl" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(_skill_md(), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    def boom(_text: str):
        raise OSError(f"[Errno 13] Permission denied: '{skill_md}'")

    monkeypatch.setattr(staging_mod, "parse_skill_frontmatter", boom)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "SKILL.md unreadable" in text  # 正对照：确实走到了该分支
    assert "PW_UNREAD" not in text
    assert "alice:" not in text


@pytest.mark.asyncio
async def test_mcp_skill_root_uri_logs_are_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """mcp 源各跳过分支的 root_uri 回显统一脱敏（凭据形态 URI 不得原样落日志）。"""
    # 无 path segment → 命中「skill root URI has no path segment」分支（该分支此前原样回显 root_uri）
    res = Resource(
        uri="skill://alice:PW_URI@host",  # type: ignore[arg-type]
        name="leak",
        mimeType="text/markdown",
        _meta={"source": "mounted"},
    )

    class Manager(FakeManager):
        pass

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_mcp_skills(Manager([("bid", res)]), SkillRegistry(), home)
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "no path segment" in text  # 正对照：确实命中了该分支
    assert "PW_URI" not in text
    assert "alice:" not in text


# ── #263 六轮复审：其余 root_uri 分支 / 非 UTF-8 SKILL.md / scp-like 源 ──────────
_CRED_ROOT = "skill://alice:PW_ROOT@host/leak"


@pytest.mark.asyncio
async def test_skill_md_missing_branch_root_uri_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """`staged SKILL missing` 分支的 root_uri 回显脱敏。"""
    mount = tmp_path / "mount" / "nofile"
    mount.mkdir(parents=True)  # 无 SKILL.md
    res = Resource(
        uri=_CRED_ROOT,  # type: ignore[arg-type]
        name="leak",
        mimeType="text/markdown",
        _meta={"source": "mounted", "mount_dir": str(mount)},
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_mcp_skills(FakeManager([("bid", res)]), SkillRegistry(), home)
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "staged SKILL missing" in text
    assert "PW_ROOT" not in text


@pytest.mark.asyncio
async def test_frontmatter_missing_branch_root_uri_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """`frontmatter missing required name/description` 分支脱敏。"""
    mount = tmp_path / "mount" / "nodesc"
    mount.mkdir(parents=True)
    (mount / "SKILL.md").write_text("---\nname: x\n---\nbody\n", encoding="utf-8")
    res = Resource(
        uri=_CRED_ROOT,  # type: ignore[arg-type]
        name="leak",
        mimeType="text/markdown",
        _meta={"source": "mounted", "mount_dir": str(mount)},
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_mcp_skills(FakeManager([("bid", res)]), SkillRegistry(), home)
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "frontmatter missing" in text
    assert "PW_ROOT" not in text


@pytest.mark.asyncio
async def test_non_utf8_skill_md_is_isolated_and_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """非 UTF-8 SKILL.md：单 SKILL 失败隔离（不得中止整个 run），日志脱敏。"""
    mount_bad = tmp_path / "mount" / "bad"
    mount_bad.mkdir(parents=True)
    (mount_bad / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
    mount_ok = tmp_path / "mount" / "good"
    mount_ok.mkdir(parents=True)
    (mount_ok / "SKILL.md").write_text(_skill_md(name="good"), encoding="utf-8")

    bad = Resource(
        uri="skill://host/bad",  # type: ignore[arg-type]
        name="bad",
        mimeType="text/markdown",
        _meta={"source": "mounted", "mount_dir": str(mount_bad)},
    )
    good = Resource(
        uri="skill://host/good",  # type: ignore[arg-type]
        name="good",
        mimeType="text/markdown",
        _meta={"source": "mounted", "mount_dir": str(mount_ok)},
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_mcp_skills(FakeManager([("bid", bad), ("bid", good)]), SkillRegistry(), home)
    # 失败隔离正对照：好的 SKILL 仍被注册
    assert names == ["mcp:bid:good"], names
    text = _no_secret(staging_logs.records)
    assert "SKILL.md unreadable" in text
    # #281：精确文案正向断言（原 `"\xff" not in text` 恒真——UnicodeDecodeError.__str__ 只含 ASCII `0xff`）。
    assert "<undecodable content (non-UTF-8)>" in text
    assert "codec can't decode" not in text


@pytest.mark.asyncio
async def test_scp_like_source_unreadable_log_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staging_logs: pytest.LogCaptureFixture
) -> None:
    """scp-like 相对 source（`deploy-token@example.com:org/repo.git`）在 SKILL.md 读失败时也不得漏。"""
    bad = "deploy-token@example.com:org/repo.git"
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "audit", "source": f"./plugins/{bad}"}]}), encoding="utf-8"
    )
    skill_md = repo / "plugins" / bad / "skills" / "etl" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(_skill_md(), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    def boom(_text: str):
        raise OSError(f"[Errno 13] Permission denied: '{skill_md}'")

    monkeypatch.setattr(staging_mod, "parse_skill_frontmatter", boom)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "SKILL.md unreadable" in text
    assert "deploy-token" not in text
    assert "example.com:org" not in text


@pytest.mark.asyncio
async def test_name_synthesis_failed_branch_root_uri_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """`skill name synthesis failed` 分支的 root_uri 回显脱敏。"""
    mount = tmp_path / "mount" / "badname"
    mount.mkdir(parents=True)
    (mount / "SKILL.md").write_text("---\nname: 'Not Kebab!'\ndescription: d\n---\nbody\n", encoding="utf-8")
    # leaf 带 userinfo 形态：`skill_dir.name` 会进日志，未脱敏时即泄漏
    res = Resource(
        uri="skill://host/alice:pw@host",  # type: ignore[arg-type]
        name="leak",
        mimeType="text/markdown",
        _meta={"source": "mounted", "mount_dir": str(mount)},
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_mcp_skills(FakeManager([("bid", res)]), SkillRegistry(), home)
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "name synthesis failed" in text
    assert "pw@" not in text and "alice:" not in text


@pytest.mark.asyncio
async def test_duplicate_synthesized_branch_root_uri_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """`duplicate synthesized SKILL name` 分支的 root_uri 回显脱敏。"""
    resources: list[Resource] = []
    for tag in ("a", "b"):
        mount = tmp_path / "mount" / tag
        mount.mkdir(parents=True)
        (mount / "SKILL.md").write_text(_skill_md(name="dup-skill"), encoding="utf-8")
        resources.append(Resource(
            uri=f"skill://alice:PW_ROOT_{tag}@host/{tag}",  # type: ignore[arg-type]
            name=tag,
            mimeType="text/markdown",
            _meta={"source": "mounted", "mount_dir": str(mount)},
        ))
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_mcp_skills(FakeManager([("bid", r) for r in resources]), SkillRegistry(), home)
    assert len(names) == 1  # 保留先到者
    text = _no_secret(staging_logs.records)
    assert "duplicate synthesized" in text
    assert "PW_ROOT" not in text


@pytest.mark.asyncio
async def test_covered_root_debug_log_is_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """covered-root 跳过分支的 DEBUG 不得原样回显带凭据的 uri / coverer。"""
    mount_c = tmp_path / "mount" / "coverer"
    mount_c.mkdir(parents=True)
    (mount_c / "SKILL.md").write_text(_skill_md(name="coverer"), encoding="utf-8")
    mount_r = tmp_path / "mount" / "covered"
    mount_r.mkdir(parents=True)
    (mount_r / "SKILL.md").write_text(_skill_md(name="covered"), encoding="utf-8")

    coverer = Resource(
        uri="skill://alice:PW_COV@host/a/b",  # type: ignore[arg-type]
        name="coverer",
        mimeType="text/markdown",
        _meta={"source": "mounted", "mount_dir": str(mount_c)},
    )
    covered = Resource(
        uri="skill://alice:PW_COV@host/a",  # type: ignore[arg-type]
        name="covered",
        mimeType="text/markdown",
        _meta={"source": "mounted", "mount_dir": str(mount_r)},
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    staging_logs.set_level(logging.DEBUG)
    names = await staging_mod.stage_mcp_skills(FakeManager([("bid", coverer), ("bid", covered)]), SkillRegistry(), home)
    assert names == ["mcp:bid:covered"]  # 短前缀是根，长前缀（/a/b）被覆盖、不进册
    text = _no_secret(staging_logs.records)
    assert "covered by skill resource" in text  # 正对照：DEBUG 分支确实执行
    assert "PW_COV" not in text


# ── #263 七轮复审：解码分支用例 / 未脱敏分支 ────────────────────────────────────
def test_user_skill_non_utf8_is_isolated(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """user 源非 UTF-8 SKILL.md：跳过该 SKILL、日志脱敏，且好 SKILL 仍注册（失败隔离）。"""
    home = tmp_path / "home"
    bad = home / "user" / "bad-skill"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
    _write_user_skill(home / "user", "good-skill")

    names = staging_mod.stage_user_skills(SkillRegistry(), home)
    assert names == ["good-skill"], names
    text = _no_secret(staging_logs.records)
    assert "user SKILL" in text and "unreadable" in text
    # #281：精确文案正向断言（原 `"\xff" not in text` 恒真——解码错误文本只含 ASCII `0xff`）。
    assert "<undecodable content (non-UTF-8)>" in text
    assert "codec can't decode" not in text  # 不泄漏解码细节


@pytest.mark.asyncio
async def test_marketplace_plugin_non_utf8_skill_is_isolated(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """marketplace plugin 源非 UTF-8 SKILL.md：该 SKILL 跳过，同 marketplace 的其它 SKILL 仍注册。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "audit", "source": "./plugins/audit"}]}), encoding="utf-8"
    )
    skills = repo / "plugins" / "audit" / "skills"
    (skills / "bad").mkdir(parents=True)
    (skills / "bad" / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
    (skills / "good").mkdir(parents=True)
    (skills / "good" / "SKILL.md").write_text(_skill_md(name="good"), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == ["audit:good"], names
    text = _no_secret(staging_logs.records)
    assert "SKILL.md unreadable" in text
    # #281：精确文案正向断言（原 `"\xff" not in text` 恒真）。
    assert "<undecodable content (non-UTF-8)>" in text
    assert "codec can't decode" not in text


@pytest.mark.asyncio
async def test_marketplace_synthesis_failed_skill_dir_name_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """marketplace plugin 分支的 `skill_dir.name` 回显：userinfo / 尾部 query 两种形态都不得漏。

    `foo?token=…` 是**只有 path 轴能遮**的形态（返回到纯值漏斗会转红）；`alice:pw@host` 覆盖整值遮。
    """
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "audit", "source": "./plugins/audit"}]}), encoding="utf-8"
    )
    for bad in ("alice:pw@host", "foo?token=SECRET_Q"):
        bad_dir = repo / "plugins" / "audit" / "skills" / bad
        bad_dir.mkdir(parents=True)
        (bad_dir / "SKILL.md").write_text(_skill_md(name="Not Kebab!"), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )
    assert names == []
    text = _no_secret(staging_logs.records)
    assert "name synthesis failed" in text
    assert "pw@" not in text and "alice:" not in text
    assert "SECRET_Q" not in text
    assert "<redacted-git-source>" in text  # 占位符正向断言（单值漏斗对 `alice:pw@host` 整值遮）
    assert "foo<redacted-source>" in text  # 尾部 query 由 path 轴截断（回退即转红）


@pytest.mark.asyncio
async def test_marketplace_skill_dir_name_credential_like_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """marketplace clone（远端内容）的 skill 目录名同样不可信（#281 复审）：scp-like 名称不得原样入日志。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "audit", "source": "./plugins/audit"}]}), encoding="utf-8"
    )
    bad_dir = repo / "plugins" / "audit" / "skills" / "deploy-token@example.com:org"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text("---\nname: x\n---\n# body\n", encoding="utf-8")  # 缺 description
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )

    assert names == []
    text = _no_secret(staging_logs.records)
    assert "missing frontmatter 'description'" in text  # 正对照：确实走了该分支
    assert "deploy-token" not in text and "example.com:org" not in text
    assert "<redacted-git-source>" in text  # 占位符正向断言


@pytest.mark.asyncio
async def test_marketplace_skill_dir_name_query_tail_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """marketplace 的 basename 同样无分隔符（#281 复审 🔴）：`foo?token=…` 必须被 path 轴遮蔽。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "audit", "source": "./plugins/audit"}]}), encoding="utf-8"
    )
    bad_dir = repo / "plugins" / "audit" / "skills" / "foo?token=SECRET_Q"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text("---\nname: x\n---\n# body\n", encoding="utf-8")  # 缺 description
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )

    assert names == []
    text = _no_secret(staging_logs.records)
    assert "missing frontmatter 'description'" in text
    assert "SECRET_Q" not in text
    assert "foo<redacted-source>" in text  # 正向：尾部被 path 轴截断


@pytest.mark.asyncio
async def test_marketplace_skill_md_unreadable_dir_name_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """`SKILL.md unreadable` 分支的目录名回显（#281 复审 🟡2）：凭据形态目录名不得原样入日志。

    向量用 `foo?token=…`（**只有 path 轴能遮**的形态）：落点若退回纯 `sanitize_value_for_diagnostics`
    会原样回显，本用例即转红——`deploy-token@example.com:org` 这类值漏斗本来就能遮，测不出回退。
    """
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "audit", "source": "./plugins/audit"}]}), encoding="utf-8"
    )
    bad_dir = repo / "plugins" / "audit" / "skills" / "foo?token=SECRET_Q"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")  # 触发 unreadable 分支
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )

    assert names == []
    text = _no_secret(staging_logs.records)
    assert "SKILL.md unreadable" in text  # 正对照：确实走了该分支
    assert "SECRET_Q" not in text
    assert "foo<redacted-source>" in text  # 正向：尾部 query 被 path 轴截断（回退成纯值漏斗即转红）


@pytest.mark.asyncio
async def test_marketplace_duplicate_skill_dir_path_redacted(
    tmp_path: Path, staging_logs: pytest.LogCaptureFixture
) -> None:
    """`duplicate marketplace SKILL` 分支的 skill_dir 路径回显（#281 复审 🟡2）：
    clone 根含 scp-like 凭据形态时，path-only 轴会漏遮，值漏斗必须整串遮。"""
    repo = tmp_path / "repo"
    (repo / ".tfrobot-plugin").mkdir(parents=True)
    (repo / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps(
            {"plugins": [{"name": "audit", "source": "./plugins/a"}, {"name": "audit", "source": "./plugins/b"}]}
        ),
        encoding="utf-8",
    )
    for p in ("a", "b"):
        d = repo / "plugins" / p / "skills" / "etl"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_skill_md(name="etl"), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    home = tmp_path / "deploy-token@example.com:org"  # clone 根带 scp-like 段
    home.mkdir(parents=True, exist_ok=True)
    names = await staging_mod.stage_marketplace_skills(
        "acme", {"type": "git", "url": f"file://{repo}"}, SkillRegistry(), home, timeout=30
    )

    assert names == ["audit:etl"]  # 保留先到者
    text = _no_secret(staging_logs.records)
    assert "duplicate marketplace SKILL name" in text  # 正对照：确实走了该分支
    assert "deploy-token" not in text and "example.com:org" not in text
    assert "<redacted-git-source>" in text
