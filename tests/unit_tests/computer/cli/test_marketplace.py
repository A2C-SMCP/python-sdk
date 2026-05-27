# -*- coding: utf-8 -*-
# filename: test_marketplace.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``marketplace`` 命令 handler 单元测试（v0.2.1 #68）/ Marketplace command handler unit tests。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.2 / §10.5。

测试意图 / Test intentions（本地 ``git init --bare`` 裸仓作 file:// 源，无网络；tmp home + XDG 重定向隔离）:
- URL / name 归一（纯函数，无需 git）；
- add：trust 缺 flag 且无 confirm → 退出码 1、不落盘；confirm=yes → clone + 注册 + 落 known_marketplaces；
  confirm=no → 退出码 1；同名冲突 → 退出码 1；--trust 直通；
- list / info / set(auto-update) / remove（级联 prune）；
- ``--json`` 输出形态。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a2c_smcp.computer.cli.commands.marketplace import (
    default_marketplace_name,
    marketplace_add,
    marketplace_info,
    marketplace_list,
    marketplace_remove,
    marketplace_set,
    normalize_marketplace_url,
)
from a2c_smcp.computer.settings.store import load_known_marketplaces
from a2c_smcp.computer.skills.registry import SkillRegistry

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

_GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]


# ── 本地裸仓 + 环境隔离辅助 / local bare-repo + env-isolation helpers ──────────
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: a skill\nlicense: MIT\n---\n# {name}\nbody\n"


def _marketplace_files() -> dict[str, str]:
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": "audit", "version": "1.0.0"}],
    }
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugins/audit/skills/audit-code/SKILL.md": _skill_md("audit-code"),
        "plugins/audit/skills/lint/SKILL.md": _skill_md("lint"),
    }


def _make_bare(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    work = tmp_path / f"{name}-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    _write(work, files)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / f"{name}.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def _env(tmp_path: Path) -> dict[str, str]:
    """A2C_SKILL_HOME → 物化根；XDG_CONFIG_HOME → user settings（trust 落盘）/ isolate home + user settings。"""
    return {**os.environ, "A2C_SKILL_HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    return h


async def _yes(_target: str) -> bool:
    return True


async def _no(_target: str) -> bool:
    return False


# ── 纯函数：URL / name 归一 / pure normalization ──────────────────────────────
def test_normalize_marketplace_url() -> None:
    assert normalize_marketplace_url("https://github.com/team/skills.git") == "https://github.com/team/skills.git"
    assert normalize_marketplace_url("git@github.com:team/skills.git") == "git@github.com:team/skills.git"
    # owner/repo 简写 → GitHub https 糖
    assert normalize_marketplace_url("team/skills") == "https://github.com/team/skills.git"
    # 非法 → None
    assert normalize_marketplace_url("not a url at all !!") is None


def test_default_marketplace_name() -> None:
    assert default_marketplace_name("https://github.com/team/My_Skills.git") == "my-skills"
    assert default_marketplace_name("git@github.com:team/acme-skills.git") == "acme-skills"
    assert default_marketplace_name("file:///tmp/foo.git") == "foo"


# ── add：trust 门 / add: trust gate ───────────────────────────────────────────
@requires_git
@pytest.mark.asyncio
async def test_add_untrusted_no_confirm_no_flag_errors(tmp_path: Path) -> None:
    """非交互（confirm=None）且无 --trust → 退出码 1、不落盘 / refuse + no record。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=None)
    assert code == 1
    assert "acme" not in (load_known_marketplaces(home, env).get("marketplaces") or {})


@requires_git
@pytest.mark.asyncio
async def test_add_confirm_yes_clones_and_records(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=_yes)
    assert code == 0
    assert "acme" in (load_known_marketplaces(home, env).get("marketplaces") or {})
    # plugin audit 的 skills 已注册（marketplace 2 段名 <plugin>:<skill>）
    names = {str(r.get("name")) for r in reg.active_refs()}
    assert "audit:audit-code" in names and "audit:lint" in names


@requires_git
@pytest.mark.asyncio
async def test_add_confirm_no_aborts(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=_no)
    assert code == 1
    assert "acme" not in (load_known_marketplaces(home, env).get("marketplaces") or {})


@requires_git
@pytest.mark.asyncio
async def test_add_trust_flag_skips_prompt(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True, confirm=None)
    assert code == 0


@requires_git
@pytest.mark.asyncio
async def test_add_name_conflict_errors(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True) == 0
    # 同名再 add → 冲突退出码 1
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True) == 1


@requires_git
@pytest.mark.asyncio
async def test_add_already_trusted_skips_prompt(tmp_path: Path) -> None:
    """首个 add 以 --trust 落 trustedMarketplaces；同 url 再 add（另名）无 confirm 也直通。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True) == 0
    # 同 url、不同 name、confirm=None、trust=False → 因 url 已 trusted 而直通
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme-two", trust=False, confirm=None) == 0


# ── list / info / set / remove ───────────────────────────────────────────────
@requires_git
@pytest.mark.asyncio
async def test_list_and_set_auto_update(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True)
    assert marketplace_list(home, env) == 0
    assert marketplace_set(home, env, "acme", "auto-update", "true") == 0
    assert (load_known_marketplaces(home, env).get("marketplaces") or {})["acme"].get("autoUpdate") is True
    # 未知 key → 1
    assert marketplace_set(home, env, "acme", "bogus", "x") == 1


@requires_git
@pytest.mark.asyncio
async def test_info_and_remove(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True)
    assert marketplace_info(home, env, "acme") == 0
    assert marketplace_info(home, env, "nope") == 1
    # remove（无 installed plugin → 仅 prune clone）
    assert await marketplace_remove(reg, home, env, "acme", confirm=_yes) == 0
    assert "acme" not in (load_known_marketplaces(home, env).get("marketplaces") or {})


@requires_git
@pytest.mark.asyncio
async def test_json_output_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True, json_output=True)
    capsys.readouterr()  # 清空 add 的输出
    marketplace_list(home, env, json_output=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list) and data[0]["name"] == "acme" and data[0]["trusted"] is True
