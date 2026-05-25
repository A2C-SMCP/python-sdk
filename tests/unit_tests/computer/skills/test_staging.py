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
import tarfile
import zipfile
from pathlib import Path

from mcp.types import BlobResourceContents, ReadResourceResult, Resource, TextResourceContents

from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import stage_mcp_skills


# ── 测试替身 / doubles ───────────────────────────────────────────────────────
class FakeManager:
    def __init__(self, pairs: list[tuple[str, Resource]], reads: dict[str, ReadResourceResult] | None = None) -> None:
        self._pairs = pairs
        self._reads = reads or {}

    async def list_skill_resources(self, server_name: str | None = None) -> list[tuple[str, Resource]]:
        return [(s, r) for s, r in self._pairs if server_name is None or s == server_name]

    async def read_resource(self, server: str, uri: str) -> ReadResourceResult:
        return self._reads[str(uri)]


def _skill_md(name: str = "my-skill", description: str = "聚合 CSV") -> str:
    return f"---\nname: {name}\ndescription: {description}\nlicense: MIT\nallowed-tools:\n  - read\n  - write\n---\n# {name}\nbody\n"


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


async def test_resource_without_source_meta_skipped(tmp_path: Path) -> None:
    # 无 _meta.source 的 skill:// 资源（子资源 / 未声明）→ 非 SKILL 根 → 跳过
    plain = Resource(uri="skill://h/not-a-root", name="not-a-root")
    reg = SkillRegistry()
    names = await stage_mcp_skills(FakeManager([("srv", plain)]), reg, tmp_path / "home")
    assert names == []
    assert len(reg) == 0
