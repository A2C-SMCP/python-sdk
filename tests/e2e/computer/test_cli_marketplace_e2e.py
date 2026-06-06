# -*- coding: utf-8 -*-
# filename: test_cli_marketplace_e2e.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``marketplace`` REPL E2E（pexpect 真进程，v0.2.1 #68）/ Marketplace REPL E2E via pexpect。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.2 / §10.5 验收。

测试意图 / Test intentions（本地 ``git init --bare`` 裸仓作 file:// 源，无网络；A2C_SKILL_HOME → tmp）:
- ``marketplace add <file://bare> --name acme`` → trust ``[y/N]`` 提示 → 送 ``y`` → clone →
  ``marketplace list`` 见 ``acme``。

说明：因启用 A2C_SKILL_HOME 后文件 watcher 常驻，pty 不再静默，故用直接 ``expect(PROMPT_RE)`` 锚定提示符，
不走 ``expect_prompt_stable`` 的静默窗口检测（后者在常驻输出下不稳）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect", reason="e2e tests require pexpect; install with `pip install pexpect`.")

from tests.e2e.computer.conftest import _spawn_cli  # noqa: E402
from tests.e2e.computer.utils import PROMPT_RE  # noqa: E402

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

_GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_bare(tmp_path: Path) -> Path:
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": "audit", "version": "1.0.0"}],
    }
    work = tmp_path / "acme-work"
    (work / ".tfrobot-plugin").mkdir(parents=True, exist_ok=True)
    (work / ".tfrobot-plugin" / "marketplace.json").write_text(json.dumps(manifest), encoding="utf-8")
    skill = work / "plugins" / "audit" / "skills" / "audit-code" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: audit-code\ndescription: a skill\nlicense: MIT\n---\n# audit-code\nbody\n", encoding="utf-8")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "acme.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


@pytest.fixture()
def _cli_with_home(tmp_path: Path) -> Iterator[tuple]:
    """A2C_SKILL_HOME / XDG_CONFIG_HOME 重定向到 tmp 后启动 CLI（_spawn_cli 复制 os.environ）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    saved = {k: os.environ.get(k) for k in ("A2C_SKILL_HOME", "XDG_CONFIG_HOME")}
    os.environ["A2C_SKILL_HOME"] = str(home)
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "cfg")
    try:
        with _spawn_cli() as child:
            child.expect(PROMPT_RE, timeout=60)  # 启动横幅后首个提示符 / first prompt after banner
            yield child, tmp_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.mark.e2e
@requires_git
def test_marketplace_add_trust_clone_list(_cli_with_home: tuple) -> None:
    child, tmp_path = _cli_with_home
    bare = _make_bare(tmp_path)

    child.sendline(f"marketplace add file://{bare} --name acme")
    child.expect([r"y/N", r"Trust this source"], timeout=30)  # trust 提示
    child.sendline("y")
    child.expect(r"acme", timeout=30)  # 成功行 "✓ added 'acme' — cloned, N skill(s)"
    child.expect(PROMPT_RE, timeout=30)

    child.sendline("marketplace list")
    idx = child.expect([r"acme", r"No marketplaces"], timeout=20)
    assert idx == 0  # acme 出现在列表
