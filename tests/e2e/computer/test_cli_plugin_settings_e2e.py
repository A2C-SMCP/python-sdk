# -*- coding: utf-8 -*-
# filename: test_cli_plugin_settings_e2e.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``plugin`` / ``settings`` 非交互 Typer 子命令 E2E（v0.2.1 #69）/ Plugin & settings non-interactive CLI E2E。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.3 / §4.5 / §4.6 验收（S16）。

以 ``subprocess`` 跑真 ``a2c-computer`` 进程（确定性、无 TTY、无 MCP spawn；A2C_SKILL_HOME / XDG_* → tmp 隔离）:
- marketplace add --trust → plugin install（**ledger-only**：非交互无 MCP 回调 → 不挂载 bundled server）→ list →
  enable → info（§4.6 非交互姿态：MCP 挂载延到下次 REPL boot 经批准框落地）；
- settings set/get/show 五级 scope；flag/policy **只读** → 退出码 1；plugin install 非法 id → 退出码 1。

说明 / Note：plugin install **外来 MCP 同名硬抛**（§10.6）与 **MCP 批准框 y/a/n + 重启幂等**（§9.2）须 live
Computer + MCP 注入回调（仅 REPL；非交互为 ledger-only、无冲突闸门），已由单测充分覆盖
（``tests/unit_tests/computer/cli/test_plugin.py`` / ``test_mcp_approval.py``）；此处聚焦非交互真进程行为。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

_GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_bare(tmp_path: Path) -> Path:
    """裸仓：marketplace ``acme`` → plugin ``audit``（带 1 skill + bundled MCP server ``figma-mcp``）。"""
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
    srv = work / "plugins" / "audit" / "mcp-servers" / "figma-mcp.json"
    srv.parent.mkdir(parents=True, exist_ok=True)
    srv.write_text(json.dumps({"name": "figma-mcp", "type": "stdio", "server_parameters": {"command": "cat"}}), encoding="utf-8")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "acme.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def _cli_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "A2C_SKILL_HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def _run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """非交互调用 a2c-computer 子命令（console script 优先，回退 python -c main()）。"""
    exe = shutil.which("a2c-computer")
    cmd = [exe, *args] if exe else [sys.executable, "-c", "from a2c_smcp.computer.cli.main import main; main()", *args]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.e2e
@requires_git
def test_plugin_install_enable_list_noninteractive(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    bare = _make_bare(tmp_path)

    # marketplace add --trust → clone catalog
    r = _run_cli(env, "marketplace", "add", f"file://{bare}", "--name", "acme", "--trust", "--json")
    assert r.returncode == 0, r.stderr + r.stdout

    # plugin install（非交互 ledger-only：无 MCP 回调 → 不挂载、记账本）
    r = _run_cli(env, "plugin", "install", "audit@acme", "--json")
    assert r.returncode == 0, r.stderr + r.stdout

    # plugin list → 含 audit@acme
    r = _run_cli(env, "plugin", "list", "--json")
    assert r.returncode == 0 and "audit@acme" in r.stdout

    # plugin enable / info
    assert _run_cli(env, "plugin", "enable", "audit@acme", "--json").returncode == 0
    r = _run_cli(env, "plugin", "info", "audit@acme", "--json")
    assert r.returncode == 0 and "audit@acme" in r.stdout


@pytest.mark.e2e
def test_settings_set_get_show_and_readonly_noninteractive(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)

    assert _run_cli(env, "settings", "set", "strictKnownMarketplaces", "true", "--json").returncode == 0
    r = _run_cli(env, "settings", "get", "strictKnownMarketplaces", "--scope", "user", "--json")
    assert r.returncode == 0 and "true" in r.stdout.lower()

    # merged show 反映写入
    r = _run_cli(env, "settings", "show", "--scope", "user", "--json")
    assert r.returncode == 0 and "strictKnownMarketplaces" in r.stdout

    # flag / policy 只读 → 退出码 1
    assert _run_cli(env, "settings", "set", "deniedMcpServers", '["x"]', "--scope", "policy", "--json").returncode == 1
    assert _run_cli(env, "settings", "set", "foo", "bar", "--scope", "flag", "--json").returncode == 1


@pytest.mark.e2e
def test_plugin_install_invalid_id_noninteractive(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    assert _run_cli(env, "plugin", "install", "no-delimiter", "--json").returncode == 1
