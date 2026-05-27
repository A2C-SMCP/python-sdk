# -*- coding: utf-8 -*-
# filename: test_marketplace.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``marketplace`` / ``skill`` CLI 集成测试（v0.2.1 #68）/ Marketplace + skill CLI integration tests。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.2 / §4.4 / §4.6 / §10.5。

测试意图 / Test intentions（本地裸仓 file:// 源，无网络；tmp home + XDG 重定向；走真实 Typer/ REPL 全栈）:
- Typer 非交互：``marketplace add --trust`` → ``list`` → ``info`` → ``remove`` 退出码 + 物化落盘；
  非交互缺 ``--trust`` → 退出码 1（§4.6 / §11）；``skill list --source mp --json`` 跨进程重建 registry 列出。
- REPL：``marketplace add`` 触发 trust ``[y/N]`` → ``y`` → clone → ``skill list`` 见 marketplace 源 skill。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import a2c_smcp.computer.cli.main as cli_main
from a2c_smcp.computer.cli.main import _interactive_loop
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.store import load_known_marketplaces

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

_GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: a skill\nlicense: MIT\n---\n# {name}\nbody\n"


def _make_bare(tmp_path: Path) -> Path:
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": "audit", "version": "1.0.0"}],
    }
    files = {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugins/audit/skills/audit-code/SKILL.md": _skill_md("audit-code"),
    }
    work = tmp_path / "acme-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    for rel, content in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "acme.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def _set_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("A2C_SKILL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


# ── Typer 非交互全栈 / Typer non-interactive full stack ───────────────────────
@requires_git
def test_typer_add_list_info_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, tmp_path)
    url = f"file://{_make_bare(tmp_path)}"
    runner = CliRunner()

    r = runner.invoke(cli_main.app, ["marketplace", "add", url, "--name", "acme", "--trust"])
    assert r.exit_code == 0, r.output
    assert "acme" in (load_known_marketplaces(tmp_path / "home").get("marketplaces") or {})

    assert runner.invoke(cli_main.app, ["marketplace", "list", "--json"]).exit_code == 0
    assert runner.invoke(cli_main.app, ["marketplace", "info", "acme"]).exit_code == 0
    assert runner.invoke(cli_main.app, ["marketplace", "remove", "acme"]).exit_code == 0
    assert "acme" not in (load_known_marketplaces(tmp_path / "home").get("marketplaces") or {})


@requires_git
def test_typer_add_missing_trust_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非交互缺 --trust → 退出码 1、不落盘（§4.6 / §11）/ missing --trust exits 1, nothing recorded。"""
    _set_env(monkeypatch, tmp_path)
    url = f"file://{_make_bare(tmp_path)}"
    r = CliRunner().invoke(cli_main.app, ["marketplace", "add", url, "--name", "acme"])
    assert r.exit_code == 1
    assert "acme" not in (load_known_marketplaces(tmp_path / "home").get("marketplaces") or {})


@requires_git
def test_typer_skill_list_rebuilds_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """新进程 registry 为空 → skill list 离线重扫已物化 marketplace clone 列出 marketplace 源 skill。"""
    _set_env(monkeypatch, tmp_path)
    url = f"file://{_make_bare(tmp_path)}"
    runner = CliRunner()
    assert runner.invoke(cli_main.app, ["marketplace", "add", url, "--name", "acme", "--trust"]).exit_code == 0

    r = runner.invoke(cli_main.app, ["skill", "list", "--source", "mp", "--json"])
    assert r.exit_code == 0, r.output
    names = {row["name"] for row in json.loads(r.output)}
    assert "audit:audit-code" in names


# ── REPL trust 流程 / REPL trust flow ─────────────────────────────────────────
class _FakePromptSession:
    def __init__(self, commands: list[str]) -> None:
        self._commands = commands

    async def prompt_async(self, *_: str, **__: Any) -> str:
        if not self._commands:
            raise EOFError
        return self._commands.pop(0)


@contextmanager
def _no_patch_stdout():  # type: ignore[no-untyped-def]
    yield


@requires_git
@pytest.mark.asyncio
async def test_repl_add_trust_yes_then_skill_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, tmp_path)
    url = f"file://{_make_bare(tmp_path)}"
    # 脚本：add 触发 trust [y/N] → "y" 被 confirm 消费 → list → skill list → exit
    commands = [f"marketplace add {url} --name acme", "y", "marketplace list", "skill list", "exit"]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: _FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: _no_patch_stdout())

    comp = Computer(name="repl-mp", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    try:
        await _interactive_loop(comp)
        assert "acme" in (load_known_marketplaces(tmp_path / "home").get("marketplaces") or {})
        names = {str(r.get("name")) for r in comp.skill_registry.active_refs()}
        assert "audit:audit-code" in names
    finally:
        await comp._skill_debouncer.aclose()  # 排空 mark_skills_dirty 触发的去抖任务
