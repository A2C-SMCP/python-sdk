# -*- coding: utf-8 -*-
# filename: test_recovery.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
治理启动恢复单元测试（#117，协议 v0.2.3 §4.8）/ Governance boot-recovery unit tests。

镜像 rust-sdk recovery.rs 的 hermetic 套件（无 git、无网络：预置 catalog clone 树 + ``refresh=False``
就地复用）。测试意图 / Test intentions:
- ``recover_marketplace_skills``：enabled installed plugin 的 bundled SKILL 恢复 + 幂等；
  ``enabledPlugins=false`` 跳过不复活（disable 负向）；账本无记录不恢复（uninstall 负向）；
  known_marketplaces 缺记录 / clone 缺失且源不可达 → ``failed_marketplaces`` 降级不抛；空 home noop。
- ``collect_enabled_bundled_servers``：含归属（plugin/marketplace）纯函数输出（§4.8.3）；
  跨 plugin 同名 server 首见去重；installPath 缺失 / JSON 损坏 → WARN 跳过不阻断。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.recovery import (
    collect_enabled_bundled_servers,
    recover_marketplace_skills,
)
from a2c_smcp.computer.settings.store import save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry

_SRC = {"type": "git", "url": "https://example.com/acme.git"}


# ── 辅助 / helpers ───────────────────────────────────────────────────────────
def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _env(tmp_path: Path) -> dict[str, str]:
    """重定向 XDG_CONFIG_HOME → tmp（隔离 user settings 读写）。"""
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _stdio(name: str, command: str = "node") -> dict:
    return {"name": name, "type": "stdio", "server_parameters": {"command": command}}


def _skill_md(name: str, description: str = "a skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\nlicense: MIT\n---\n# {name}\nbody\n"


def _setup_catalog(
    home: Path,
    mp: str,
    plugin: str,
    *,
    servers: Sequence[str] = (),
    skills: Sequence[str] = (),
    seed_known: bool = True,
) -> Path:
    """预置 catalog clone 树（marketplace.json + plugin 的 mcp-servers/ + skills/）+ 可选 seed known_marketplaces。

    返回 plugin 根 ``<catalog>/plugins/<plugin>``。``refresh=False`` 下真实 staging 就地复用此树（零 git）。
    """
    catalog = marketplace_skill_dir(home, mp)
    manifest = {
        "name": mp,
        "owner": {"name": "X"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": plugin, "source": plugin, "version": "1.2.0"}],
    }
    _write_json(catalog / ".tfrobot-plugin" / "marketplace.json", manifest)
    plugin_root = catalog / "plugins" / plugin
    plugin_root.mkdir(parents=True, exist_ok=True)
    for sname in servers:
        _write_json(plugin_root / "mcp-servers" / f"{sname}.json", _stdio(sname))
    for sk in skills:
        p = plugin_root / "skills" / sk / "SKILL.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_skill_md(sk), encoding="utf-8")
    if seed_known:
        save_known_marketplaces(
            {"version": 1, "marketplaces": {mp: {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc123"}}},
            home=home,
        )
    return plugin_root


def _seed_installed(home: Path, plugins: dict[str, list[dict]]) -> None:
    save_installed_plugins({"version": 1, "plugins": plugins}, home=home)


def _record(plugin_root: Path, *, scope: str = "user", servers: Sequence[str] = ()) -> dict:
    return {
        "scope": scope,
        "installPath": str(plugin_root),
        "version": "1.2.0",
        "commitSha": "abc123",
        "installedAt": "2026-07-06T00:00:00Z",
        "bundledMcpServers": list(servers),
    }


# ── recover_marketplace_skills ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_recover_restages_enabled_installed_plugin_and_idempotent(tmp_path: Path) -> None:
    """enabled（缺省即启用）installed plugin → bundled SKILL 恢复进 Registry；二次调用幂等。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {"audit@acme": [_record(root)]})
    reg = SkillRegistry()

    report = await recover_marketplace_skills(reg, home, {"enabledPlugins": {}}, env=_env(tmp_path))

    assert "audit@acme" in report.restored_plugins
    assert "audit:lint" in report.restored_skills
    assert reg.resolve("audit:lint") is not None
    assert report.failed_marketplaces == [] and report.skipped_disabled == []

    # 幂等：显式 true 亦启用；registry 不重复注册
    report2 = await recover_marketplace_skills(reg, home, {"enabledPlugins": {"audit@acme": True}}, env=_env(tmp_path))
    assert "audit:lint" in report2.restored_skills
    assert len(reg) == 1


@pytest.mark.asyncio
async def test_recover_skips_disabled_plugin_and_collect_skips(tmp_path: Path) -> None:
    """enabledPlugins=false → skipped_disabled、SKILL 不复活、collect 同步跳过（disable 负向）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["figma"], skills=["lint"])
    _seed_installed(home, {"audit@acme": [_record(root, servers=["figma"])]})
    reg = SkillRegistry()
    declared = {"enabledPlugins": {"audit@acme": False}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.skipped_disabled == ["audit@acme"]
    assert report.restored_skills == [] and report.restored_plugins == []
    assert reg.resolve("audit:lint") is None
    assert collect_enabled_bundled_servers(home, declared, env=_env(tmp_path)) == []


@pytest.mark.asyncio
async def test_recover_without_ledger_record_is_noop(tmp_path: Path) -> None:
    """账本无记录（uninstall 后）→ 即使 catalog 树仍在也不恢复（uninstall 负向；ledger 驱动）。"""
    home = _home(tmp_path)
    _setup_catalog(home, "acme", "audit", skills=["lint"])  # 树在、账本空
    reg = SkillRegistry()

    report = await recover_marketplace_skills(reg, home, {}, env=_env(tmp_path))

    assert report.restored_plugins == [] and report.restored_skills == []
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_recover_degrades_when_marketplace_record_absent(tmp_path: Path) -> None:
    """known_marketplaces 缺该 marketplace 记录 → failed_marketplaces 降级、不抛、不阻断。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    _seed_installed(home, {"audit@acme": [_record(root)]})
    reg = SkillRegistry()

    report = await recover_marketplace_skills(reg, home, {}, env=_env(tmp_path))

    assert report.failed_marketplaces == ["acme"]
    assert report.restored_skills == []


@pytest.mark.asyncio
async def test_recover_degrades_when_clone_missing_and_unreachable(tmp_path: Path) -> None:
    """clone 树缺失且源不可达（file:// 不存在路径，git 快速失败离线安全）→ failed_marketplaces 降级。"""
    home = _home(tmp_path)
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": {"type": "git", "url": f"file://{tmp_path}/no-such-repo"}}}},
        home=home,
    )
    _seed_installed(home, {"audit@acme": [_record(tmp_path / "gone")]})
    reg = SkillRegistry()

    report = await recover_marketplace_skills(reg, home, {}, env=_env(tmp_path))

    assert report.failed_marketplaces == ["acme"]
    assert report.restored_skills == []


@pytest.mark.asyncio
async def test_recover_empty_home_is_noop(tmp_path: Path) -> None:
    """空 home（双账本皆无）→ 空报告 noop。"""
    home = _home(tmp_path)
    reg = SkillRegistry()

    report = await recover_marketplace_skills(reg, home, {}, env=_env(tmp_path))

    assert report.restored_plugins == []
    assert report.restored_skills == []
    assert report.failed_marketplaces == []
    assert report.skipped_disabled == []
    assert len(reg) == 0


# ── collect_enabled_bundled_servers ──────────────────────────────────────────
def test_collect_returns_enabled_bundled_servers_with_ownership(tmp_path: Path) -> None:
    """enabled plugin 的 bundled server 可查询，且归属（plugin/marketplace/installPath）为纯函数输出（§4.8.3）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["figma", "blender"])
    _seed_installed(home, {"audit@acme": [_record(root, servers=["figma", "blender"])]})

    records = collect_enabled_bundled_servers(home, {}, env=_env(tmp_path))

    assert {r.config.name for r in records} == {"figma", "blender"}
    for r in records:
        assert r.plugin_id == "audit@acme"
        assert r.plugin == "audit" and r.marketplace == "acme"
        assert r.install_path == root


def test_collect_dedupes_same_server_name_across_plugins(tmp_path: Path) -> None:
    """跨 plugin 同名 server → 首见保留去重（与 rust 一致）。"""
    home = _home(tmp_path)
    root_a = _setup_catalog(home, "acme", "audit", servers=["shared"])
    root_b = _setup_catalog(home, "beta", "fmt", servers=["shared"])
    _seed_installed(
        home,
        {"audit@acme": [_record(root_a, servers=["shared"])], "fmt@beta": [_record(root_b, servers=["shared"])]},
    )

    records = collect_enabled_bundled_servers(home, {}, env=_env(tmp_path))

    assert len(records) == 1
    assert records[0].config.name == "shared"


def test_collect_skips_missing_install_path_and_corrupt_json(tmp_path: Path) -> None:
    """installPath 缺失 / mcp-servers JSON 损坏 → WARN 跳过该 plugin，不阻断其余、不抛。"""
    home = _home(tmp_path)
    good_root = _setup_catalog(home, "acme", "audit", servers=["figma"])
    corrupt_root = _setup_catalog(home, "beta", "fmt")
    (corrupt_root / "mcp-servers").mkdir(parents=True, exist_ok=True)
    (corrupt_root / "mcp-servers" / "bad.json").write_text("{not json", encoding="utf-8")
    no_path_record = {"scope": "user", "version": "1.0.0"}  # 无 installPath
    _seed_installed(
        home,
        {
            "audit@acme": [_record(good_root, servers=["figma"])],
            "fmt@beta": [_record(corrupt_root)],
            "ghost@acme": [no_path_record],
        },
    )

    records = collect_enabled_bundled_servers(home, {}, env=_env(tmp_path))

    assert {r.config.name for r in records} == {"figma"}
