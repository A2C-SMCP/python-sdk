# -*- coding: utf-8 -*-
# filename: test_staging_marketplace.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
marketplace git 源 staging 本地裸仓集成测试（v0.2.1 #61）

协议依据 / Protocol: tfrobot-marketplace docs/marketplace/protocol-v1.md §2/§3/§5/§6；
SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §3/§4.2；
                   docs/design-0.2.1-skill-computer-management.md §2.2。

测试意图 / Test intentions（用本地 ``git init --bare`` 裸仓作 file:// 源，无网络依赖）:
- eager clone：相对路径 / 裸名 plugin 经 pluginRoot 解析 → 扫描 skills/ → 注册（含 known_marketplaces.json 物化）
- refresh：``git pull`` 拉取新增 skill；改写历史致 ff-only 失败 → 全量重 clone 恢复
- git-subdir：``--filter=tree:0 --sparse`` 部分克隆子目录作 plugin（cone 外目录不检出）
- 失败降级：坏 source 不入 Registry、不抛、不阻断其余（marketplace 间 / marketplace 内 plugin 间）
- plugin_filter（#62 注入点）：仅物化指定 plugin
- _ssh_to_https：SSH→HTTPS 回退 URL 改写（纯函数，无需 git）
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.store import load_known_marketplaces
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import _ssh_to_https, stage_marketplace_skills

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

_GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]


# ── 本地裸仓 fixture 辅助 / local bare-repo helpers ──────────────────────────
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _skill_md(name: str, description: str = "a skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\nlicense: MIT\n---\n# {name}\nbody\n"


def _make_bare(tmp_path: Path, name: str, files: dict[str, str], *, allow_filter: bool = False) -> tuple[Path, Path]:
    """建一个含 ``files`` 的工作仓 + 其裸仓镜像；返回 (工作仓, 裸仓)。工作仓 origin 指向裸仓（便于 push）。"""
    work = tmp_path / f"{name}-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    _write(work, files)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / f"{name}.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(work, "remote", "add", "origin", str(bare))
    if allow_filter:  # file:// 部分克隆（--filter）需源仓显式允许
        _git(bare, "config", "uploadpack.allowFilter", "true")
        _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    return work, bare


def _head_sha(repo: Path) -> str:
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _url(p: Path) -> str:
    return f"file://{p}"


def _src(url: str) -> dict[str, str]:
    return {"type": "git", "url": url}


def _env(home: Path) -> dict[str, str]:
    return {**os.environ, "A2C_SKILL_HOME": str(home)}


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _marketplace_files() -> dict[str, str]:
    """标准 marketplace 布局：pluginRoot=./plugins，plugin `audit`（裸名）+ `fmt`（./ 相对路径）。"""
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [
            {"name": "audit", "source": "audit", "version": "1.0.0"},
            {"name": "fmt", "source": "./plugins/fmt"},
        ],
    }
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugins/audit/skills/audit-code/SKILL.md": _skill_md("audit-code"),
        "plugins/audit/skills/lint/SKILL.md": _skill_md("lint"),
        "plugins/fmt/skills/format/SKILL.md": _skill_md("format"),
    }


# ── eager clone ───────────────────────────────────────────────────────────────
@requires_git
async def test_eager_clone_registers_relative_plugins(tmp_path: Path) -> None:
    _, bare = _make_bare(tmp_path, "acme", _marketplace_files())
    home = _home(tmp_path)
    reg = SkillRegistry()

    names = await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, env=_env(home))

    assert set(names) == {"audit:audit-code", "audit:lint", "fmt:format"}
    ref = reg.resolve("audit:audit-code")
    assert ref is not None
    assert ref["source"] == "marketplace:acme-skills"
    assert ref["version"] == "1.0.0"  # entry.version 优先
    skill_path = Path(ref["path"])
    assert skill_path.is_dir()
    assert (skill_path / "SKILL.md").is_file()
    # 落在 <home>/marketplace/acme-skills/ clone 树内（就地，不复制）
    assert str(skill_path).startswith(str((home / "marketplace" / "acme-skills").resolve()))

    # fmt 无 entry.version → 回退 commit SHA（非 None）
    fmt = reg.resolve("fmt:format")
    assert fmt is not None
    assert fmt.get("version")

    # known_marketplaces.json 物化记录
    kmf = load_known_marketplaces(home, _env(home))
    assert "acme-skills" in kmf["marketplaces"]
    rec = kmf["marketplaces"]["acme-skills"]
    assert rec["installLocation"] == str((home / "marketplace" / "acme-skills").resolve())
    assert rec.get("commitSha")
    assert rec["source"] == _src(_url(bare))


# ── refresh：pull 拉新 / reclone 兜底 ────────────────────────────────────────
@requires_git
async def test_refresh_pull_picks_up_new_skill(tmp_path: Path) -> None:
    work, bare = _make_bare(tmp_path, "acme", _marketplace_files())
    home = _home(tmp_path)
    reg = SkillRegistry()
    await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, env=_env(home))
    assert reg.resolve("audit:newskill") is None

    # 工作仓新增 skill → push 到裸仓（ff 推进）
    _write(work, {"plugins/audit/skills/newskill/SKILL.md": _skill_md("newskill")})
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "add newskill")
    _git(work, "push", "-q", "origin", "main")

    names = await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, refresh=True, env=_env(home))
    assert "audit:newskill" in names
    assert reg.resolve("audit:newskill") is not None


@requires_git
async def test_refresh_reclone_when_pull_fails(tmp_path: Path) -> None:
    work, bare = _make_bare(tmp_path, "acme", _marketplace_files())
    home = _home(tmp_path)
    reg = SkillRegistry()
    await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, env=_env(home))

    # 改写历史（amend）后强推 → 本地 clone 的 ff-only pull 失败 → 触发全量重 clone
    _write(work, {"plugins/audit/skills/rewritten/SKILL.md": _skill_md("rewritten")})
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "--amend", "-m", "rewritten history")
    _git(work, "push", "-q", "-f", "origin", "main")

    names = await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, refresh=True, env=_env(home))
    assert "audit:rewritten" in names
    assert reg.resolve("audit:rewritten") is not None


# ── git-subdir sparse clone ──────────────────────────────────────────────────
@requires_git
async def test_git_subdir_sparse_clone(tmp_path: Path) -> None:
    mono_files = {
        "README.md": "monorepo root",
        "other/junk.txt": "unrelated",
        "pkgs/adobe/.tfrobot-plugin/plugin.json": json.dumps({"name": "adobe", "version": "2.0.0"}),
        "pkgs/adobe/skills/photoshop/SKILL.md": _skill_md("photoshop"),
    }
    _, mono_bare = _make_bare(tmp_path, "mono", mono_files, allow_filter=True)
    mp_files = {
        ".tfrobot-plugin/marketplace.json": json.dumps(
            {
                "name": "curated",
                "owner": {"name": "Curator"},
                "plugins": [
                    {"name": "adobe", "source": {"source": "git-subdir", "url": _url(mono_bare), "path": "pkgs/adobe"}},
                ],
            },
        ),
    }
    _, mp_bare = _make_bare(tmp_path, "curated", mp_files)
    home = _home(tmp_path)
    reg = SkillRegistry()

    names = await stage_marketplace_skills("curated", _src(_url(mp_bare)), reg, home, env=_env(home))

    assert names == ["adobe:photoshop"]
    ref = reg.resolve("adobe:photoshop")
    assert ref is not None
    assert ref["version"] == "2.0.0"  # plugin.json version（entry 无 version）
    ext = home / "marketplace" / ".plugins" / "curated" / "adobe"
    assert (ext / "pkgs" / "adobe" / "skills" / "photoshop" / "SKILL.md").is_file()
    # sparse cone 外的 other/ 不应被检出
    assert not (ext / "other").exists()


# ── 失败降级 / failure isolation ─────────────────────────────────────────────
@requires_git
async def test_bad_marketplace_source_does_not_register_or_raise(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg = SkillRegistry()
    # 形态合法但克隆失败（仓库不存在）→ 记 ERROR、返回 []、不抛、不入册
    names = await stage_marketplace_skills("bad", _src(_url(tmp_path / "nonexistent.git")), reg, home, env=_env(home))
    assert names == []
    assert len(reg) == 0

    # 不阻断其余：随后一个好 marketplace 正常注册
    _, bare = _make_bare(tmp_path, "acme", _marketplace_files())
    names2 = await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, env=_env(home))
    assert "fmt:format" in names2


@requires_git
async def test_bad_plugin_does_not_block_siblings(tmp_path: Path) -> None:
    files = {
        ".tfrobot-plugin/marketplace.json": json.dumps(
            {
                "name": "mix",
                "owner": {"name": "x"},
                "metadata": {"pluginRoot": "./plugins"},
                "plugins": [
                    {"name": "good", "source": "good"},
                    {"name": "missing", "source": "does-not-exist"},  # 指向不存在目录 → 跳过
                    {"name": "nosrc"},  # 缺 source → 跳过
                ],
            },
        ),
        "plugins/good/skills/g1/SKILL.md": _skill_md("g1"),
    }
    _, bare = _make_bare(tmp_path, "mix", files)
    home = _home(tmp_path)
    reg = SkillRegistry()

    names = await stage_marketplace_skills("mix", _src(_url(bare)), reg, home, env=_env(home))
    assert names == ["good:g1"]


@requires_git
async def test_skill_missing_description_skipped(tmp_path: Path) -> None:
    files = {
        ".tfrobot-plugin/marketplace.json": json.dumps(
            {"name": "p", "owner": {"name": "x"}, "metadata": {"pluginRoot": "./plugins"}, "plugins": [{"name": "a", "source": "a"}]},
        ),
        "plugins/a/skills/ok/SKILL.md": _skill_md("ok"),
        "plugins/a/skills/bad/SKILL.md": "---\nname: bad\n---\nno description\n",
    }
    _, bare = _make_bare(tmp_path, "p", files)
    home = _home(tmp_path)
    reg = SkillRegistry()

    names = await stage_marketplace_skills("p", _src(_url(bare)), reg, home, env=_env(home))
    assert names == ["a:ok"]
    assert reg.resolve("a:bad") is None


# ── plugin_filter（#62 注入点）─────────────────────────────────────────────────
@requires_git
async def test_plugin_filter_restricts_registration(tmp_path: Path) -> None:
    _, bare = _make_bare(tmp_path, "acme", _marketplace_files())
    home = _home(tmp_path)
    reg = SkillRegistry()

    names = await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, plugin_filter={"audit"}, env=_env(home))
    assert set(names) == {"audit:audit-code", "audit:lint"}
    assert reg.resolve("fmt:format") is None


# ── _ssh_to_https（纯函数，无需 git）/ SSH→HTTPS rewrite ──────────────────────
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo.git"),
        ("ssh://git@github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        ("ssh://git@host.example.com:2222/team/p", "https://host.example.com/team/p"),
        ("https://github.com/owner/repo.git", None),  # 已是 https → 无回退
        ("file:///tmp/repo.git", None),
    ],
)
def test_ssh_to_https_rewrite(url: str, expected: str | None) -> None:
    assert _ssh_to_https(url) == expected


# ── 独立 plugin clone：url plain / sha 锁版本（_git_clone_plugin 非 subdir 分支）─
@requires_git
async def test_url_source_standalone_clone(tmp_path: Path) -> None:
    plugin_files = {
        ".tfrobot-plugin/plugin.json": json.dumps({"name": "extp", "version": "9.9.9"}),
        "skills/remote-skill/SKILL.md": _skill_md("remote-skill"),
    }
    _, plugin_bare = _make_bare(tmp_path, "extplugin", plugin_files)
    mp_files = {
        ".tfrobot-plugin/marketplace.json": json.dumps(
            {
                "name": "ext-mp",
                "owner": {"name": "x"},
                "plugins": [{"name": "extp", "source": {"source": "url", "url": _url(plugin_bare)}}],
            },
        ),
    }
    _, mp_bare = _make_bare(tmp_path, "extmp", mp_files)
    home = _home(tmp_path)
    reg = SkillRegistry()

    names = await stage_marketplace_skills("ext-mp", _src(_url(mp_bare)), reg, home, env=_env(home))

    assert names == ["extp:remote-skill"]
    ref = reg.resolve("extp:remote-skill")
    assert ref is not None
    assert ref["version"] == "9.9.9"  # plugin.json version
    ext = home / "marketplace" / ".plugins" / "ext-mp" / "extp"
    assert (ext / "skills" / "remote-skill" / "SKILL.md").is_file()


@requires_git
async def test_sha_locked_standalone_clone_pins_commit(tmp_path: Path) -> None:
    plugin_files = {
        ".tfrobot-plugin/plugin.json": json.dumps({"name": "lockp"}),  # 无 version → 回退 sha
        "skills/pinned/SKILL.md": _skill_md("pinned"),
    }
    work, plugin_bare = _make_bare(tmp_path, "lockplugin", plugin_files, allow_filter=True)
    pinned_sha = _head_sha(work)
    # 追加更晚的 commit 并推送 → tip != pinned_sha
    _write(work, {"skills/pinned/LATER.md": "later"})
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "later commit")
    _git(work, "push", "-q", "origin", "main")

    mp_files = {
        ".tfrobot-plugin/marketplace.json": json.dumps(
            {
                "name": "ext-mp",
                "owner": {"name": "x"},
                "plugins": [{"name": "lockp", "source": {"source": "url", "url": _url(plugin_bare), "sha": pinned_sha}}],
            },
        ),
    }
    _, mp_bare = _make_bare(tmp_path, "extmp", mp_files)
    home = _home(tmp_path)
    reg = SkillRegistry()

    names = await stage_marketplace_skills("ext-mp", _src(_url(mp_bare)), reg, home, env=_env(home))

    assert names == ["lockp:pinned"]
    ext = home / "marketplace" / ".plugins" / "ext-mp" / "lockp"
    assert _head_sha(ext) == pinned_sha  # 锁到指定 commit（非 tip）
    assert not (ext / "skills" / "pinned" / "LATER.md").exists()  # 更晚 commit 的文件不在
    ref = reg.resolve("lockp:pinned")
    assert ref is not None
    assert ref["version"] == pinned_sha  # 无 plugin.json version → 回退 HEAD sha


@requires_git
async def test_sha_locked_plugin_reused_on_refresh(tmp_path: Path) -> None:
    plugin_files = {
        ".tfrobot-plugin/plugin.json": json.dumps({"name": "lockp"}),
        "skills/pinned/SKILL.md": _skill_md("pinned"),
    }
    work, plugin_bare = _make_bare(tmp_path, "lockplugin", plugin_files, allow_filter=True)
    pinned_sha = _head_sha(work)
    mp_files = {
        ".tfrobot-plugin/marketplace.json": json.dumps(
            {
                "name": "ext-mp",
                "owner": {"name": "x"},
                "plugins": [{"name": "lockp", "source": {"source": "url", "url": _url(plugin_bare), "sha": pinned_sha}}],
            },
        ),
    }
    _, mp_bare = _make_bare(tmp_path, "extmp", mp_files)
    home = _home(tmp_path)
    reg = SkillRegistry()
    await stage_marketplace_skills("ext-mp", _src(_url(mp_bare)), reg, home, env=_env(home))

    # 在 ext clone 里留哨兵文件：sha 锁版本在 refresh=True 时应复用（不 rmtree 重 clone）→ 哨兵存活
    ext = home / "marketplace" / ".plugins" / "ext-mp" / "lockp"
    sentinel = ext / ".reuse-sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    await stage_marketplace_skills("ext-mp", _src(_url(mp_bare)), reg, home, refresh=True, env=_env(home))
    assert sentinel.exists()
    assert reg.resolve("lockp:pinned") is not None


# ── name 路径安全防御 / public-API name guard ────────────────────────────────
async def test_invalid_marketplace_name_rejected(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reg = SkillRegistry()
    # 非法 name 在任何 git 操作前被拦截：返回 []、不入册、不在 home 外建目录
    names = await stage_marketplace_skills("../evil", _src("file:///x.git"), reg, home, env=_env(home))
    assert names == []
    assert len(reg) == 0
    assert not (home.parent / "evil").exists()


# ── lastUpdated 语义：实际 clone/pull 才刷新，复用保留 ───────────────────────
@requires_git
async def test_last_updated_preserved_on_reuse_refreshed_on_pull(tmp_path: Path) -> None:
    from a2c_smcp.computer.settings.store import update_known_marketplaces

    _, bare = _make_bare(tmp_path, "acme", _marketplace_files())
    home = _home(tmp_path)
    reg = SkillRegistry()
    await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, env=_env(home))

    # 注入可辨识的旧 lastUpdated
    old = "2000-01-01T00:00:00Z"

    def _set_old(cur: dict) -> None:
        cur["marketplaces"]["acme-skills"]["lastUpdated"] = old
        return None

    update_known_marketplaces(_set_old, home, _env(home))

    # 复用既有 clone（refresh=False）→ lastUpdated 保留旧值
    await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, env=_env(home))
    rec = load_known_marketplaces(home, _env(home))["marketplaces"]["acme-skills"]
    assert rec["lastUpdated"] == old
    assert rec.get("commitSha")  # installLocation/commitSha 仍刷新（自愈）

    # refresh=True（实际 pull）→ lastUpdated 刷新
    await stage_marketplace_skills("acme-skills", _src(_url(bare)), reg, home, refresh=True, env=_env(home))
    assert load_known_marketplaces(home, _env(home))["marketplaces"]["acme-skills"]["lastUpdated"] != old
