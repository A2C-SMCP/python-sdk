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
import logging
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import BlobResourceContents, ReadResourceResult, Resource, TextResourceContents

import a2c_smcp.computer.skills.staging as staging_mod
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import stage_mcp_skills, stage_user_skills


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
        r.levelno == logging.DEBUG and sub1_uri in r.getMessage() and "covered by root" in r.getMessage()
        for r in staging_logs.records
    )
    assert any(r.levelno == logging.DEBUG and sub2_uri in r.getMessage() for r in staging_logs.records)


async def test_nested_covered_roots_all_excluded(tmp_path: Path, staging_logs: pytest.LogCaptureFixture) -> None:
    """#188 嵌套链 A>B>C：B、C 均被排除（覆盖判定不看覆盖者自身是否被覆盖），仅 A 注册。"""
    a_uri = "skill://h/a"
    b_uri = "skill://h/a/b"
    c_uri = "skill://h/a/b/c"
    md_uri = "skill://h/a/SKILL.md"
    a = _root(a_uri, {"source": "resources"})
    b = _root(b_uri, {"source": "resources"})
    c = _root(c_uri, {"source": "resources"})
    sub_md = Resource(uri=md_uri, name="SKILL.md")
    # B/C 是「目录型」节点，也在 A 的子资源前缀内 → 必须齐备 reads，否则父根物化 KeyError 误红
    reads = {
        md_uri: ReadResourceResult(contents=[TextResourceContents(uri=md_uri, text=_skill_md(name="a"))]),
        b_uri: ReadResourceResult(contents=[TextResourceContents(uri=b_uri, text="b")]),
        c_uri: ReadResourceResult(contents=[TextResourceContents(uri=c_uri, text="c")]),
    }
    mgr = FakeManager([("srv", a), ("srv", b), ("srv", c), ("srv", sub_md)], reads=reads)
    reg = SkillRegistry()
    home = tmp_path / "home"

    names = await stage_mcp_skills(mgr, reg, home)

    assert names == ["mcp:srv:a"]
    assert len(reg) == 1
    assert (Path(reg.resolve("mcp:srv:a")["path"]) / "SKILL.md").is_file()  # type: ignore[index]
    assert not any(r.levelno == logging.ERROR for r in staging_logs.records)
    assert any(r.levelno == logging.WARNING and "covered by other roots" in r.getMessage() for r in staging_logs.records)
    assert any(r.levelno == logging.DEBUG and b_uri in r.getMessage() for r in staging_logs.records)
    assert any(r.levelno == logging.DEBUG and c_uri in r.getMessage() for r in staging_logs.records)


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


# ── user 源 DropIn（就地发现，不 staging，#60）────────────────────────────────
def _write_user_skill(root: Path, skill_dir_name: str, *, fm_name: str | None = None, description: str = "do thing") -> Path:
    """在发现根下写一个 ``<skill_dir_name>/SKILL.md`` / write a DropIn skill dir under a root。"""
    d = root / skill_dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_md(name=skill_dir_name if fm_name is None else fm_name, description=description), encoding="utf-8")
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
