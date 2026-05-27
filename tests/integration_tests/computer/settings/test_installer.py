# -*- coding: utf-8 -*-
# filename: test_installer.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Plugin installer 本地裸仓集成测试（v0.2.1 #63）—— 真 git clone + 真 skill 注册 + bundled MCP 解析
Plugin installer local bare-repo integration tests: real git clone + real skill registration + bundled MCP parse.

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.3 / §6.2 / §10.6。

测试意图 / Test intentions（本地 ``git init --bare`` 裸仓作 file:// 源，无网络；plugin 同时含
``skills/<s>/SKILL.md`` + ``mcp-servers/<n>.json``；MCP 注入用录制式替身捕获校验后的 MCPServerConfig）:
- install 端到端：真 clone catalog → 注册 plugin skills + 解析 bundled server + 写 installed_plugins.json。
- uninstall 端到端：删 installPath 树 + 注销 skills + 级联 remove server + 删账本记录。
- install→disable→enable：**不重 clone**（哨兵保留）+ skill 复活 + server 重挂。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.installer import (
    PluginInstallError,
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from a2c_smcp.computer.settings.store import load_installed_plugins
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import stage_marketplace_skills

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
    # A2C_SKILL_HOME 供物化路径；XDG_CONFIG_HOME 重定向 settings.json（隔离 disable/enable 写真实 ~/.config）。
    return {**os.environ, "A2C_SKILL_HOME": str(home), "XDG_CONFIG_HOME": str(home / "cfg")}


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _plugin_files() -> dict[str, str]:
    """单 plugin（audit）含 1 skill（lint）+ 1 bundled MCP server（figma）的标准 marketplace 布局。"""
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": "audit", "version": "1.2.0"}],
    }
    server = {"name": "figma", "type": "stdio", "server_parameters": {"command": "echo"}}
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugins/audit/skills/lint/SKILL.md": _skill_md("lint"),
        "plugins/audit/mcp-servers/figma.json": json.dumps(server),
    }


async def _add_marketplace(home: Path, bare: Path, env: dict[str, str]) -> None:
    """模拟 ``marketplace add``：clone catalog + 写 known_marketplaces.json（plugin_filter=空 → 不注册 skill）。"""
    await stage_marketplace_skills("acme-skills", _src(_url(bare)), SkillRegistry(), home, plugin_filter=set(), env=env)


# ── install 端到端 / end-to-end ──────────────────────────────────────────────
@requires_git
async def test_install_end_to_end(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _plugin_files())
    home = _home(tmp_path)
    env = _env(home)
    await _add_marketplace(home, bare, env)
    reg = SkillRegistry()
    captured: list[str] = []

    async def _register(cfg) -> None:
        captured.append(cfg.name)

    record = await install_plugin(
        "audit@acme-skills", reg, home, env=env, existing_server_names=lambda: set(), register_server=_register,
    )

    assert record["bundledMcpServers"] == ["figma"]
    assert record["version"] == "1.2.0"
    assert reg.resolve("audit:lint") is not None  # 真 skill 注册
    assert captured == ["figma"]  # bundled server 解析+注册
    assert "audit@acme-skills" in load_installed_plugins(home=home)["plugins"]


@requires_git
async def test_uninstall_end_to_end(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _plugin_files())
    home = _home(tmp_path)
    env = _env(home)
    await _add_marketplace(home, bare, env)
    reg = SkillRegistry()
    removed: list[str] = []

    async def _register(cfg) -> None:
        pass

    async def _remove(name: str) -> None:
        removed.append(name)

    await install_plugin("audit@acme-skills", reg, home, env=env, existing_server_names=lambda: set(), register_server=_register)
    install_path = Path(load_installed_plugins(home=home)["plugins"]["audit@acme-skills"][0]["installPath"])
    assert install_path.exists() and reg.resolve("audit:lint") is not None

    ok = await uninstall_plugin("audit@acme-skills", reg, home, env=env, remove_server=_remove)

    assert ok is True
    assert removed == ["figma"]  # 级联 stop+remove
    assert not install_path.exists()  # installPath 树清除
    assert reg.resolve("audit:lint") is None  # skills 注销
    assert "audit@acme-skills" not in load_installed_plugins(home=home)["plugins"]  # 账本记录删除


@requires_git
async def test_install_disable_enable_no_reclone(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _plugin_files())
    home = _home(tmp_path)
    env = _env(home)
    await _add_marketplace(home, bare, env)
    reg = SkillRegistry()
    captured: list[str] = []
    removed: list[str] = []

    async def _register(cfg) -> None:
        captured.append(cfg.name)

    async def _remove(name: str) -> None:
        removed.append(name)

    await install_plugin("audit@acme-skills", reg, home, env=env, existing_server_names=lambda: set(), register_server=_register)
    assert reg.resolve("audit:lint") is not None
    # 哨兵：植入 catalog clone 内 plugin 根，用于检测 enable 是否误重 clone（重 clone 会 wipe）。
    sentinel = marketplace_skill_dir(home, "acme-skills") / "plugins" / "audit" / "SENTINEL"
    sentinel.write_text("x", encoding="utf-8")

    # disable：摘 server + orphan skill
    await disable_plugin("audit@acme-skills", reg, home, env=env, remove_server=_remove)
    assert reg.is_orphan("audit:lint") and removed == ["figma"]
    captured.clear()

    # enable：复活 skill + 重挂 server，且不重 clone
    await enable_plugin("audit@acme-skills", reg, home, env=env, existing_server_names=lambda: set(), register_server=_register)

    assert sentinel.exists()  # 未重 clone（哨兵保留 → 复用既有 clone 树）
    assert reg.resolve("audit:lint") is not None  # 孤儿复活
    assert captured == ["figma"]  # server 重挂
    settings = json.loads((home / "cfg" / "a2c" / "settings.json").read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["audit@acme-skills"] is True


# ── strict mode 冲突 install 硬失败（§4.4；#80）────────────────────────────────
def _strict_false_conflict_files() -> dict[str, str]:
    """audit entry strict=false 但 plugin.json 声明组件（skills）→ conflicting manifests。"""
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": "audit", "version": "1.2.0", "strict": False}],
    }
    server = {"name": "figma", "type": "stdio", "server_parameters": {"command": "echo"}}
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugins/audit/.tfrobot-plugin/plugin.json": json.dumps({"name": "audit", "skills": "x"}),
        "plugins/audit/skills/lint/SKILL.md": _skill_md("lint"),
        "plugins/audit/mcp-servers/figma.json": json.dumps(server),
    }


@requires_git
async def test_install_strict_false_conflict_hard_fails_atomically(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _strict_false_conflict_files())
    home = _home(tmp_path)
    env = _env(home)
    await _add_marketplace(home, bare, env)
    reg = SkillRegistry()
    captured: list[str] = []

    async def _register(cfg) -> None:
        captured.append(cfg.name)

    # 早检拦截 → PluginInstallError（硬错误，conflicting manifests）
    with pytest.raises(PluginInstallError, match="conflicting manifests"):
        await install_plugin(
            "audit@acme-skills", reg, home, env=env, existing_server_names=lambda: set(), register_server=_register,
        )

    # 原子失败：未挂 server、未注册 skill、未写 installed 记录
    assert captured == []
    assert reg.resolve("audit:lint") is None
    assert "audit@acme-skills" not in load_installed_plugins(home=home)["plugins"]
