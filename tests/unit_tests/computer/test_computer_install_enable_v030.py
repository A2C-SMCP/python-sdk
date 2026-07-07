# -*- coding: utf-8 -*-
# filename: test_computer_install_enable_v030.py
# @Time    : 2026/07/07
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 级 install/enable 分离行为（#123，协议 v0.3.0 §2.4/§4.8）/ Computer-level v0.3.0 lifecycle tests。

测试意图 / Test intentions（hermetic：预置 catalog 树 + 账本 + user settings，零 git 零网络）:
- boot 一次性迁移：v0.2.x 存量（账本有、settings 无 installedPlugins 键）→ 迁 intent + enabledPlugins=true，
  升级前活跃态保住（skills 恢复）；升级前显式 false → 迁 intent、保持禁用。
- installed_disabled 重启惰性：标记键在、未启用 → boot 不投影（conformance §5「重启恢复（installed_disabled）」）。
- installed_enabled 重启恢复：intent ∧ true → 能力重现（conformance §5「重启恢复（enabled）」）。
- 标记键防复活：intent 空数组 + 账本残留 → 不迁回、不激活。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.store import save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import marketplace_skill_dir

_SRC = {"type": "git", "url": "https://example.com/acme.git"}
_PID = "audit@acme"


# ── fixture 辅助（与 test_computer_governance_recovery.py 同构）──────────────
def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: a skill\nlicense: MIT\n---\n# {name}\nbody\n"


def _seed_home(tmp_path: Path, *, servers: Sequence[str] = (), skills: Sequence[str] = ()) -> tuple[Path, Path]:
    """预置 skill_home：catalog 树（acme/audit）+ known_marketplaces + installed 账本。返回 (home, plugin_root)。"""
    home = tmp_path / "skill-home"
    home.mkdir()
    catalog = marketplace_skill_dir(home, "acme")
    _write_json(
        catalog / ".tfrobot-plugin" / "marketplace.json",
        {
            "name": "acme",
            "owner": {"name": "X"},
            "metadata": {"pluginRoot": "./plugins"},
            "plugins": [{"name": "audit", "source": "audit", "version": "1.2.0"}],
        },
    )
    plugin_root = catalog / "plugins" / "audit"
    plugin_root.mkdir(parents=True, exist_ok=True)
    for sname in servers:
        _write_json(plugin_root / "mcp-servers" / f"{sname}.json", {"name": sname, "type": "stdio", "server_parameters": {"command": "node"}})
    for sk in skills:
        p = plugin_root / "skills" / sk / "SKILL.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_skill_md(sk), encoding="utf-8")
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc123"}}},
        home=home,
    )
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                _PID: [
                    {
                        "scope": "user",
                        "installPath": str(plugin_root),
                        "version": "1.2.0",
                        "commitSha": "abc123",
                        "installedAt": "2026-07-06T00:00:00Z",
                        "bundledMcpServers": list(servers),
                    },
                ],
            },
        },
        home=home,
    )
    return home, plugin_root


def _isolate_declared_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 declared 视图来源：XDG → tmp、cwd → tmp（防读真实 user/project settings）。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)


def _user_settings_file(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "a2c" / "settings.json"


def _read_user_settings(tmp_path: Path) -> dict:
    p = _user_settings_file(tmp_path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ── boot 一次性迁移（迁移指南「保住既有用户现状」）────────────────────────────
@pytest.mark.asyncio
async def test_boot_migrates_legacy_ledger_installs_and_stays_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.2.x 存量升级首个 boot：迁 installedPlugins + enabledPlugins=true，升级前活跃的 skills 保住。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])

    async with Computer(name="t", skill_home=home) as comp:
        names = {ref["name"] for ref in comp.get_skills()}
        assert "audit:lint" in names

    settings = _read_user_settings(tmp_path)
    assert settings.get("installedPlugins") == [_PID]
    assert settings.get("enabledPlugins", {}).get(_PID) is True


@pytest.mark.asyncio
async def test_boot_migration_preserves_explicit_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """升级前显式禁用（false）：迁 intent 但不翻 true，boot 后保持惰性。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_json(_user_settings_file(tmp_path), {"enabledPlugins": {_PID: False}})

    async with Computer(name="t", skill_home=home) as comp:
        names = {ref["name"] for ref in comp.get_skills()}
        assert "audit:lint" not in names

    settings = _read_user_settings(tmp_path)
    assert settings.get("installedPlugins") == [_PID]
    assert settings.get("enabledPlugins", {}).get(_PID) is False


# ── 三态重启恢复（conformance §5）────────────────────────────────────────────
@pytest.mark.asyncio
async def test_boot_installed_disabled_stays_lazy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """installed_disabled（intent 有、未启用）重启 → 仍惰性：skills 不进投影（缺省翻转生效）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_json(_user_settings_file(tmp_path), {"installedPlugins": [_PID]})

    async with Computer(name="t", skill_home=home) as comp:
        names = {ref["name"] for ref in comp.get_skills()}
        assert "audit:lint" not in names


@pytest.mark.asyncio
async def test_boot_installed_enabled_restores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """installed_enabled（intent ∧ true）重启 → 能力重现（正向契约不变）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_json(_user_settings_file(tmp_path), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})

    async with Computer(name="t", skill_home=home) as comp:
        names = {ref["name"] for ref in comp.get_skills()}
        assert "audit:lint" in names


@pytest.mark.asyncio
async def test_boot_marker_prevents_resurrection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """intent 空数组（已迁移标记）+ 账本残留 → 不迁回、不激活（意图是唯一权威，账本只是派生缓存）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_json(_user_settings_file(tmp_path), {"installedPlugins": []})

    async with Computer(name="t", skill_home=home) as comp:
        names = {ref["name"] for ref in comp.get_skills()}
        assert "audit:lint" not in names

    settings = _read_user_settings(tmp_path)
    assert settings.get("installedPlugins") == []
    assert "enabledPlugins" not in settings
