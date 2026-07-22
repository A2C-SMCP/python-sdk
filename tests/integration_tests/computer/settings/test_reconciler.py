# -*- coding: utf-8 -*-
# filename: test_reconciler.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Reconciler 本地裸仓集成测试（v0.2.1 #62）—— 端到端真实 git clone/reclone/prune
Reconciler local bare-repo integration tests: end-to-end real git clone/reclone/prune.

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §7.1/§7.3。

测试意图 / Test intentions（用本地 ``git init --bare`` 裸仓作 file:// 源，无网络依赖）:
- reconcile 端到端：声明 marketplace → clone → 按 ``enabledPlugins`` 仅注册启用 plugin 的 SKILL
  （未启用 plugin 不入册）+ 写 known_marketplaces.json。
- sourceChanged：声明源换库 → wipe 旧 clone + 重 clone → 注册新库 SKILL、旧库 SKILL 注销。
- prune：声明移除 marketplace → list 孤儿 + 清 clone 树 + 删物化条目 + 注销 SKILL。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.reconciler import list_orphan_marketplaces, prune_marketplaces, reconcile
from a2c_smcp.computer.settings.store import load_known_marketplaces
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry

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


def _make_bare(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """建一个含 ``files`` 的工作仓 + 其裸仓镜像；返回裸仓路径（作 file:// 源）。"""
    work = tmp_path / f"{name}-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    _write(work, files)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / f"{name}.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


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


def _one_plugin_files(plugin: str, skill: str) -> dict[str, str]:
    """单 plugin（裸名经 pluginRoot 解析）单 skill 的标准 marketplace 布局。"""
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": plugin, "source": plugin}],
    }
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        f"plugins/{plugin}/skills/{skill}/SKILL.md": _skill_md(skill),
    }


def _marketplace_two_plugins() -> dict[str, str]:
    """两 plugin（audit / fmt）；用于验证 enabledPlugins 过滤。"""
    manifest = {
        "name": "acme-skills",
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": "audit"}, {"name": "fmt", "source": "fmt"}],
    }
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugins/audit/skills/lint/SKILL.md": _skill_md("lint"),
        "plugins/fmt/skills/format/SKILL.md": _skill_md("format"),
    }


# ── reconcile 端到端 / end-to-end ────────────────────────────────────────────
@requires_git
async def test_reconcile_clones_and_registers_only_enabled_plugin(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_two_plugins())
    home = _home(tmp_path)
    reg = SkillRegistry()
    declared = {
        "extraKnownMarketplaces": {"acme-skills": {"source": _src(_url(bare))}},
        "installedPlugins": ["audit@acme-skills", "fmt@acme-skills"],  # 两个都安装（v0.3.0 双意图）
        "enabledPlugins": {"audit@acme-skills": True},  # 仅启用 audit；fmt 不启用
    }

    report = await reconcile(reg, home, declared, env=_env(home))

    assert report.installed == ["acme-skills"]
    assert "audit:lint" in report.registered_skills
    assert reg.resolve("audit:lint") is not None
    assert reg.resolve("fmt:format") is None  # 未启用 plugin 不入册
    # known_marketplaces.json 物化
    kmf = load_known_marketplaces(home=home)["marketplaces"]
    assert "acme-skills" in kmf
    assert kmf["acme-skills"]["source"] == _src(_url(bare))


@requires_git
async def test_reconcile_source_changed_switches_repo(tmp_path: Path) -> None:
    bare1 = _make_bare(tmp_path, "repo1", _one_plugin_files("p", "old-skill"))
    bare2 = _make_bare(tmp_path, "repo2", _one_plugin_files("p", "new-skill"))
    home = _home(tmp_path)
    reg = SkillRegistry()
    enabled = {"installedPlugins": ["p@acme-skills"], "enabledPlugins": {"p@acme-skills": True}}

    await reconcile(reg, home, {"extraKnownMarketplaces": {"acme-skills": {"source": _src(_url(bare1))}}, **enabled}, env=_env(home))
    assert reg.resolve("p:old-skill") is not None

    # 声明换源 → sourceChanged 重 clone
    report = await reconcile(
        reg, home, {"extraKnownMarketplaces": {"acme-skills": {"source": _src(_url(bare2))}}, **enabled}, env=_env(home)
    )

    assert report.updated == ["acme-skills"]
    assert reg.resolve("p:new-skill") is not None  # 新库 SKILL 注册
    assert reg.resolve("p:old-skill") is None  # 旧库 SKILL 注销
    assert load_known_marketplaces(home=home)["marketplaces"]["acme-skills"]["source"] == _src(_url(bare2))


@requires_git
async def test_prune_removes_clone_tree_and_record(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _one_plugin_files("p", "s1"))
    home = _home(tmp_path)
    reg = SkillRegistry()
    await reconcile(
        reg,
        home,
        {
            "extraKnownMarketplaces": {"acme-skills": {"source": _src(_url(bare))}},
            "installedPlugins": ["p@acme-skills"],
            "enabledPlugins": {"p@acme-skills": True},
        },
        env=_env(home),
    )
    clone = marketplace_skill_dir(home, "acme-skills")
    assert clone.exists() and reg.resolve("p:s1") is not None

    # 不再声明 → 孤儿 → prune
    declared_empty = {"extraKnownMarketplaces": {}}
    assert list_orphan_marketplaces(home, declared_empty, env=_env(home)) == ["acme-skills"]

    removed = prune_marketplaces(["acme-skills"], reg, home, env=_env(home))

    assert removed == ["acme-skills"]
    assert not clone.exists()  # clone 树清除
    assert "acme-skills" not in load_known_marketplaces(home=home)["marketplaces"]  # 物化条目删除
    assert reg.resolve("p:s1") is None  # SKILL 注销
