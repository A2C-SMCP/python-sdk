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
- install 端到端（v0.3.0 #123 不激活）：真 clone catalog → 解析 bundled server + 写 installedPlugins 意图 +
  installed_plugins.json；**不**注册 skills、不挂 server → enable 才原子点亮。
- uninstall 端到端：删独占外部 clone（共享 catalog 保留）+ 注销 skills + 级联 remove server + 删账本记录 + 清双意图。
- install→enable→disable→enable：**不重 clone**（哨兵保留）+ skill 复活 + server 重挂。
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


def _plugin_external_files(plugin_url: str) -> dict[str, str]:
    """marketplace 清单：plugin ``audit`` 的 source 是独立 git url（installPath 落外部 ``.plugins`` clone）。"""
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": {"source": "url", "url": plugin_url}, "version": "2.0.0"}],
    }
    return {".tfrobot-plugin/marketplace.json": json.dumps(manifest)}


def _plugin_repo_files() -> dict[str, str]:
    """独立 plugin 仓库内容：1 skill + 1 bundled server。"""
    server = {"name": "figma", "type": "stdio", "server_parameters": {"command": "echo"}}
    return {
        "skills/lint/SKILL.md": _skill_md("lint"),
        "mcp-servers/figma.json": json.dumps(server),
    }


def _plugin_subdir_repo_files() -> dict[str, str]:
    """git-subdir 形态的 plugin 仓库：plugin 根在 ``pkg/sub/`` 下。"""
    server = {"name": "figma", "type": "stdio", "server_parameters": {"command": "echo"}}
    return {
        "pkg/sub/skills/lint/SKILL.md": _skill_md("lint"),
        "pkg/sub/mcp-servers/figma.json": json.dumps(server),
    }


def _plugin_git_subdir_files(plugin_url: str) -> dict[str, str]:
    """marketplace 清单：plugin ``audit`` 的 source 是 ``git-subdir``（installPath 落外部 clone 的 subdir）。"""
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [
            {
                "name": "audit",
                "source": {"source": "git-subdir", "url": plugin_url, "path": "pkg/sub"},
                "version": "2.1.0",
            }
        ],
    }
    return {".tfrobot-plugin/marketplace.json": json.dumps(manifest)}


def _shared_subtree_files() -> dict[str, str]:
    """父子 / 同源相对源布局：alpha 与 gamma 共用 ``./plugin``，beta 为 ``./plugin/nested``。"""
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [
            {"name": "alpha", "source": "./plugin", "version": "1.0.0"},
            {"name": "beta", "source": "./plugin/nested", "version": "1.0.0"},
            {"name": "gamma", "source": "./plugin", "version": "1.0.0"},
        ],
    }
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugin/skills/alpha-skill/SKILL.md": _skill_md("alpha-skill"),
        "plugin/nested/skills/beta-skill/SKILL.md": _skill_md("beta-skill"),
    }


# ── install 端到端 / end-to-end ──────────────────────────────────────────────
@requires_git
async def test_install_end_to_end(tmp_path: Path) -> None:
    """v0.3.0：install 端到端 = 真 clone 物化 + 双账写入，零激活；enable 才点亮 skills + server。"""
    bare = _make_bare(tmp_path, "acme", _plugin_files())
    home = _home(tmp_path)
    env = _env(home)
    await _add_marketplace(home, bare, env)
    reg = SkillRegistry()
    captured: list[str] = []

    async def _register(cfg) -> None:
        captured.append(cfg.name)

    record = await install_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set())

    assert record["mcpServers"] == ["figma"]
    assert record["version"] == "1.2.0"
    assert reg.resolve("audit:lint") is None  # install ≠ activate：skill 不注册
    assert "audit@acme-skills" in load_installed_plugins(home=home)["plugins"]
    settings = json.loads((home / "cfg" / "a2c" / "settings.json").read_text(encoding="utf-8"))
    assert settings["installedPlugins"] == ["audit@acme-skills"]  # config-first 意图
    assert "enabledPlugins" not in settings

    # enable：真 staging 注册 skill + 重挂 server（原子点亮）
    await enable_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set(), register_server=_register)
    assert reg.resolve("audit:lint") is not None
    assert captured == ["figma"]


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

    await install_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set())
    await enable_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set(), register_server=_register)
    install_path = Path(load_installed_plugins(home=home)["plugins"]["audit@acme-skills"][0]["installPath"])
    assert install_path.exists() and reg.resolve("audit:lint") is not None

    ok = await uninstall_plugin("audit@acme-skills", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=_remove)

    assert ok is True
    assert removed == ["figma"]  # 级联 stop+remove
    assert install_path.exists()  # #258：共享 catalog 内相对源落点保留（marketplace 级缓存）
    assert reg.resolve("audit:lint") is None  # skills 注销
    assert "audit@acme-skills" not in load_installed_plugins(home=home)["plugins"]  # 账本记录删除
    settings = json.loads((home / "cfg" / "a2c" / "settings.json").read_text(encoding="utf-8"))
    assert settings["installedPlugins"] == []  # 意图删除（回 available）
    assert "audit@acme-skills" not in settings.get("enabledPlugins", {})  # enabled 条目清除

    # 验收：卸载后可重新安装成功，且落点不变（修复前 `plugin root not found after resolve`）。
    record = await install_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set())
    assert Path(record["installPath"]).resolve() == install_path.resolve()


@requires_git
async def test_uninstall_relative_source_keeps_siblings_end_to_end_258(tmp_path: Path) -> None:
    """#258：真 clone 下父子 / 同源兄弟文件在卸载后保留，剩余账本完好，并可重装。"""
    bare = _make_bare(tmp_path, "acme-shared", _shared_subtree_files())
    home = _home(tmp_path)
    env = _env(home)
    await _add_marketplace(home, bare, env)

    paths: dict[str, Path] = {}
    for pid in ("alpha@acme-skills", "beta@acme-skills", "gamma@acme-skills"):
        record = await install_plugin(pid, SkillRegistry(), home, env=env, existing_bundle_ids=lambda: set())
        paths[pid] = Path(record["installPath"]).resolve()
    assert paths["alpha@acme-skills"] == paths["gamma@acme-skills"], "同源 → 同一 installPath"
    assert paths["beta@acme-skills"].is_relative_to(paths["alpha@acme-skills"]), "beta 位于 alpha 子树"

    assert await uninstall_plugin(
        "alpha@acme-skills", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env,
    ) is True

    assert paths["alpha@acme-skills"].exists(), "共享 catalog 树 MUST NOT 被卸载删除"
    assert (paths["beta@acme-skills"] / "skills" / "beta-skill" / "SKILL.md").exists()
    assert set(load_installed_plugins(home=home)["plugins"]) == {"beta@acme-skills", "gamma@acme-skills"}

    record = await install_plugin("alpha@acme-skills", SkillRegistry(), home, env=env, existing_bundle_ids=lambda: set())
    assert Path(record["installPath"]).resolve() == paths["alpha@acme-skills"]


@requires_git
async def test_uninstall_external_clone_end_to_end_258(tmp_path: Path) -> None:
    """#258：url 源（独占外部 clone）卸载时删整个 clone，catalog 保留。"""
    plugin_bare = _make_bare(tmp_path, "audit-plugin", _plugin_repo_files())
    catalog_bare = _make_bare(tmp_path, "acme-external", _plugin_external_files(_url(plugin_bare)))
    home = _home(tmp_path)
    env = _env(home)
    await _add_marketplace(home, catalog_bare, env)

    record = await install_plugin("audit@acme-skills", SkillRegistry(), home, env=env, existing_bundle_ids=lambda: set())
    ext_root = home / "marketplace" / ".plugins" / "acme-skills" / "audit"
    assert Path(record["installPath"]).resolve() == ext_root.resolve()
    catalog_manifest = home / "marketplace" / "acme-skills" / ".tfrobot-plugin" / "marketplace.json"
    assert catalog_manifest.exists()

    assert await uninstall_plugin(
        "audit@acme-skills", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env,
    ) is True

    assert not ext_root.exists(), "独占外部 clone 删除"
    assert catalog_manifest.exists(), "catalog 共享树保留"


@requires_git
async def test_uninstall_git_subdir_deletes_whole_clone_end_to_end_258(tmp_path: Path) -> None:
    """#258 🟡5：真 ``git-subdir`` clone 的 installPath 是 ext root 的 subdir → 卸载删整个 clone 根。"""
    plugin_bare = _make_bare(tmp_path, "audit-subdir", _plugin_subdir_repo_files())
    catalog_bare = _make_bare(tmp_path, "acme-subdir", _plugin_git_subdir_files(_url(plugin_bare)))
    home = _home(tmp_path)
    env = _env(home)
    await _add_marketplace(home, catalog_bare, env)

    record = await install_plugin("audit@acme-skills", SkillRegistry(), home, env=env, existing_bundle_ids=lambda: set())
    ext_root = home / "marketplace" / ".plugins" / "acme-skills" / "audit"
    assert Path(record["installPath"]).resolve() == (ext_root / "pkg" / "sub").resolve()
    assert ext_root.exists()

    assert await uninstall_plugin(
        "audit@acme-skills", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env,
    ) is True

    assert not ext_root.exists(), "subdir installPath → 删整个独占 clone 根"


@requires_git
async def test_install_enable_disable_enable_no_reclone(tmp_path: Path) -> None:
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

    await install_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set())
    await enable_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set(), register_server=_register)
    assert reg.resolve("audit:lint") is not None
    captured.clear()
    # 哨兵：植入 catalog clone 内 plugin 根，用于检测 enable 是否误重 clone（重 clone 会 wipe）。
    sentinel = marketplace_skill_dir(home, "acme-skills") / "plugins" / "audit" / "SENTINEL"
    sentinel.write_text("x", encoding="utf-8")

    # disable：摘 server + orphan skill
    await disable_plugin("audit@acme-skills", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=_remove)
    assert reg.is_orphan("audit:lint") and removed == ["figma"]

    # enable：复活 skill + 重挂 server，且不重 clone
    await enable_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set(), register_server=_register)

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
        await install_plugin("audit@acme-skills", reg, home, env=env, existing_bundle_ids=lambda: set())

    # 原子失败：未挂 server、未注册 skill、未写 installed 记录、意图回滚
    assert captured == []
    assert reg.resolve("audit:lint") is None
    assert "audit@acme-skills" not in load_installed_plugins(home=home)["plugins"]
    settings_file = home / "cfg" / "a2c" / "settings.json"
    if settings_file.exists():
        assert "audit@acme-skills" not in json.loads(settings_file.read_text(encoding="utf-8")).get("installedPlugins", [])
