# -*- coding: utf-8 -*-
# filename: test_recovery.py
# @Time    : 2026/11/20
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
治理启动恢复 · 声明源诊断的真实 clone 集成测试（#287，镜像 rust-sdk#295）/ Real-clone recovery tests。

测试意图 / Test intentions（本地 ``git init --bare`` 裸仓作 file:// 源，无网络依赖）:
- 声明源与 known 源不同时，boot 恢复**仍按 known 旧源**克隆并注册其 SKILL——冲突只进报告，不换源。

为何用「不同 catalog 内容」而不是断言参数 / Why different catalog contents:
- ``stage_marketplace_skills`` 在 ``refresh=False`` 且 clone 已存在时走「就地复用」（staging.py:1776），
  传入的 ``source`` 根本不参与克隆；复用路径的 ``_record_known_marketplace(guard_identity=True)`` 还会因
  源不同拒写并跳过注册。故「是否误用声明源」**只有 clone 缺失时才可观测**——两个裸仓内容不同
  （``old-skill`` vs ``new-skill``）即可让变异转红。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.store import (
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
)
from a2c_smcp.computer.skills.home import marketplace_skill_dir

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

_GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]
_MP = "acme-skills"
_PLUGIN = "p"
_PID = f"{_PLUGIN}@{_MP}"


# ── 本地裸仓 fixture 辅助（与 tests/integration_tests/computer/settings/test_reconciler.py 同构）──
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: a skill\nlicense: MIT\n---\n# {name}\nbody\n"


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


def _one_plugin_files(plugin: str, skill: str) -> dict[str, str]:
    """单 plugin（裸名经 pluginRoot 解析）单 skill 的标准 marketplace 布局。"""
    manifest = {
        "name": _MP,
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": plugin, "source": plugin}],
    }
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        f"plugins/{plugin}/skills/{skill}/SKILL.md": _skill_md(skill),
    }


def _url(p: Path) -> str:
    return f"file://{p}"


def _seed_home(tmp_path: Path, registered_url: str) -> Path:
    """SKILL Home：只有 known_marketplaces 记录（**无** catalog clone → 恢复必须真克隆）+ 空账本。"""
    home = tmp_path / "skill-home"
    home.mkdir()
    save_known_marketplaces(
        {"version": 1, "marketplaces": {_MP: {"source": {"type": "git", "url": registered_url}}}},
        home=home,
    )
    save_installed_plugins(
        {"version": 1, "plugins": {_PID: [{"scope": "user", "installPath": str(tmp_path / "gone" / _PLUGIN)}]}},
        home=home,
    )
    return home


def _declared(declared_url: str) -> dict:
    return {
        "installedPlugins": [_PID],
        "enabledPlugins": {_PID: True},
        "extraKnownMarketplaces": {_MP: {"source": {"type": "git", "url": declared_url}}},
    }


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 declared 视图来源：XDG → tmp、cwd → tmp（防读真实 user/project settings）。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)


# ── #287：冲突只诊断，恢复仍用已注册源 ────────────────────────────────────────
@requires_git
@pytest.mark.asyncio
async def test_reconcile_governance_keeps_registered_source_on_declared_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """声明源改成另一个仓库 → 报告冲突，但恢复仍克隆**已注册**的旧源（内容取自旧库）。"""
    _isolate(tmp_path, monkeypatch)
    old_repo = _make_bare(tmp_path, "repo1", _one_plugin_files(_PLUGIN, "old-skill"))
    new_repo = _make_bare(tmp_path, "repo2", _one_plugin_files(_PLUGIN, "new-skill"))
    home = _seed_home(tmp_path, _url(old_repo))

    async with Computer(name="t", skill_home=home) as comp:
        # boot_up 已把安装意图迁进 settings 并**按 known 旧源**建好 clone；必须删掉它，本次调用才会真正
        # 因「clone 缺失」去拉取——否则复用分支（`staging.py:1776`）会让源实参不参与克隆，判别力归零。
        shutil.rmtree(marketplace_skill_dir(home, _MP))
        assert not marketplace_skill_dir(home, _MP).exists(), "clone 必须真的不存在"

        report = await comp.reconcile_governance(declared=_declared(_url(new_repo)))

        assert [c.marketplace for c in report.marketplace_source_conflicts] == [_MP]
        conflict = report.marketplace_source_conflicts[0]
        assert conflict.known_source_display == _url(old_repo)
        assert conflict.declared_source_display == _url(new_repo)
        # 恢复内容取自**已注册**旧源：old-skill 在、new-skill 不在
        assert f"{_PLUGIN}:old-skill" in report.restored_skills
        registered = {ref["name"] for ref in comp.get_skills()}
        assert f"{_PLUGIN}:old-skill" in registered
        assert f"{_PLUGIN}:new-skill" not in registered
        # 冲突不进入降级桶
        assert report.failed_marketplaces == []
    # known 记录保持旧源（只诊断、不换源）
    assert load_known_marketplaces(home=home)["marketplaces"][_MP]["source"] == {"type": "git", "url": _url(old_repo)}


# ── #293：非法声明被丢弃，恢复仍按已注册源 ────────────────────────────────────
@requires_git
@pytest.mark.asyncio
async def test_reconcile_governance_invalid_declaration_restores_registered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：``extraKnownMarketplaces`` 声明写坏（``owner/repo`` 简写）→ 报告声明错误，但恢复**仍按已注册源**。

    判别力：非法声明被 settings 校验整条丢弃后，若实现把它当成「用户想换源」去服用，克隆内容会取自
    声明（这里根本没有可解析的 URL，会直接降级）；用两个内容不同的裸仓 + 断言只有注册源的 skill 在，
    即可让「按声明源恢复」的变异转红。
    """
    _isolate(tmp_path, monkeypatch)
    registered_repo = _make_bare(tmp_path, "repo1", _one_plugin_files(_PLUGIN, "old-skill"))
    home = _seed_home(tmp_path, _url(registered_repo))
    # 声明写成 owner/repo 简写（校验必然判 malformed git url 并整条丢弃）
    (tmp_path / "cfg" / "a2c").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "a2c" / "settings.json").write_text(
        json.dumps({**_declared(_url(registered_repo)), "extraKnownMarketplaces": {_MP: {"source": {"type": "git", "url": "owner/repo"}}}}),
        encoding="utf-8",
    )

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance()

    assert [(e.marketplace, e.scope.value) for e in report.marketplace_declaration_errors] == [(_MP, "user")]
    assert report.marketplace_source_conflicts == []  # 无可比声明源，不是冲突
    # 恢复取自**已注册**源
    assert f"{_PLUGIN}:old-skill" in report.restored_skills
    assert report.failed_marketplaces == []
    assert load_known_marketplaces(home=home)["marketplaces"][_MP]["source"] == {"type": "git", "url": _url(registered_repo)}
