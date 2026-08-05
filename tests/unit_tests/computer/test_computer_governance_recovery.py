# -*- coding: utf-8 -*-
# filename: test_computer_governance_recovery.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 级治理启动恢复测试（#117，协议 v0.2.3 §4.8）/ Computer-level governance recovery tests。

测试意图 / Test intentions（hermetic：预置 catalog clone 树 + 双账本，零 git 零网络）:
- ``boot_up`` 从既有 skill_home 恢复 enabled plugin 的 bundled SKILL（§4.8.1/2；conformance §2.4 重启恢复）；
- ``reconcile_governance(hooks)`` 显式重挂：register 收到含归属上下文的调用 + 幂等（设计 Y client 契约）；
- register 抛错不阻断其余（失败隔离）；existing 名冲突 skip 不覆盖（additive-only，用户配置胜）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.recovery import BundledServerRecord
from a2c_smcp.computer.settings.schema import SettingsScope
from a2c_smcp.computer.settings.store import save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import marketplace_skill_dir

# 夹具身份对（#153 隔离审查 🔴2 + 协议 conformance §2.0）：原 "figma"/"blender" 的 bundle_id 恰等于自身
# ⇒ `resolve_bundle_id(record.config)` 换成 `record.config.name` 也测不出来（变异存活）。含 `.` 令两者分叉。
FIGMA_NAME, FIGMA_BID = "figma.mcp", "figma_mcp"
BLENDER_NAME, BLENDER_BID = "blender.3d", "blender_3d"


_SRC = {"type": "git", "url": "https://example.com/acme.git"}
# v0.3.0（#123）：活跃 = installed ∧ enabledPlugins=true（缺省翻转）→ hooks 用例显式给足两键意图。
_DECLARED_ENABLED = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}


# ── fixture 辅助（与 test_recovery.py 同构）/ helpers ─────────────────────────
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
        server_def = {"name": sname, "type": "stdio", "server_parameters": {"command": "node"}}
        _write_json(plugin_root / "mcp-servers" / f"{sname}.json", server_def)
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
                "audit@acme": [
                    {
                        "scope": "user",
                        "installPath": str(plugin_root),
                        "version": "1.2.0",
                        "commitSha": "abc123",
                        "installedAt": "2026-07-06T00:00:00Z",
                        "mcpServers": list(servers),
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


# ── boot_up 恢复（skills-only，设计 Y）────────────────────────────────────────
@pytest.mark.asyncio
async def test_boot_up_restores_bundled_skills_from_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 skill_home 重建 Computer → boot 后 bundled SKILL 重现于 get_skills（conformance §2.4 重启恢复正向）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])

    async with Computer(name="t", skill_home=home) as comp:
        names = {ref["name"] for ref in comp.get_skills()}
        assert "audit:lint" in names


@pytest.mark.asyncio
async def test_boot_up_does_not_restore_disabled_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """user scope enabledPlugins=false → boot 不复活（disable 负向 + scope 门控）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_json(tmp_path / "cfg" / "a2c" / "settings.json", {"enabledPlugins": {"audit@acme": False}})

    async with Computer(name="t", skill_home=home) as comp:
        names = {ref["name"] for ref in comp.get_skills()}
        assert "audit:lint" not in names


# ── reconcile_governance(hooks) 显式重挂（client 契约）────────────────────────
@pytest.mark.asyncio
async def test_reconcile_governance_remounts_via_hooks_and_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hooks 重挂：register 收到 (config, 归属记录)；inject_inputs 先于 register；二次调用幂等。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, plugin_root = _seed_home(tmp_path, servers=[FIGMA_NAME], skills=["lint"])

    calls: list[tuple[str, str, str]] = []
    injected: list[Path] = []

    async def register(cfg, record: BundledServerRecord) -> None:
        calls.append((cfg.name, record.plugin, record.marketplace))

    async def inject(record: BundledServerRecord) -> None:
        injected.append(record.install_path)

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance(
            existing_bundle_ids=lambda: set(),
            register_server=register,
            inject_inputs=inject,
            declared=_DECLARED_ENABLED,
        )
        assert report.remounted_servers == [FIGMA_BID]  # 值域 = bundle_id（#153）
        assert calls == [(FIGMA_NAME, "audit", "acme")]  # register 回调收 config（display 名）
        assert injected == [plugin_root]

        report2 = await comp.reconcile_governance(
            existing_bundle_ids=lambda: set(),
            register_server=register,
            inject_inputs=inject,
            declared=_DECLARED_ENABLED,
        )
        assert report2.remounted_servers == [FIGMA_BID]  # 幂等：结果一致


@pytest.mark.asyncio
async def test_reconcile_governance_injects_once_per_plugin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 plugin 根下多个 bundled server → inject_inputs 仅调一次，所有 server 均重挂。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, plugin_root = _seed_home(tmp_path, servers=[BLENDER_NAME, FIGMA_NAME], skills=["lint"])

    mounted: list[str] = []
    injected: list[Path] = []

    async def register(cfg, record) -> None:
        mounted.append(cfg.name)

    async def inject(record) -> None:
        injected.append(record.install_path)

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance(
            existing_bundle_ids=lambda: set(),
            register_server=register,
            inject_inputs=inject,
            declared=_DECLARED_ENABLED,
        )
        assert sorted(mounted) == [BLENDER_NAME, FIGMA_NAME]
        assert sorted(report.remounted_servers) == [BLENDER_BID, FIGMA_BID]
        assert injected == [plugin_root]  # 每 plugin 根仅一次


@pytest.mark.asyncio
async def test_reconcile_governance_register_failure_non_blocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """register 抛错 → 不阻断（不抛出），失败 server 不入 remounted，skills 恢复不受影响。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=[FIGMA_NAME], skills=["lint"])

    async def register(cfg, record) -> None:
        raise RuntimeError("mount boom")

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance(
            existing_bundle_ids=lambda: set(),
            register_server=register,
            declared=_DECLARED_ENABLED,
        )
        assert report.remounted_servers == []
        assert "audit:lint" in report.restored_skills


@pytest.mark.asyncio
async def test_reconcile_governance_conflict_skips_existing_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """existing 已有同名 server → skip 不覆盖（additive-only，用户配置胜），register 不被调用。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=[FIGMA_NAME], skills=["lint"])

    calls: list[str] = []

    async def register(cfg, record) -> None:
        calls.append(cfg.name)

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance(
            existing_bundle_ids=lambda: {FIGMA_BID},
            register_server=register,
            declared=_DECLARED_ENABLED,
        )
        assert calls == []
        assert report.remounted_servers == []


# ── #159 Option B：project scope enabledPlugins → PENDING ──────────────────
def _write_settings(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


@pytest.mark.asyncio
async def test_project_scope_enabled_plugin_bundled_server_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """project scope enabledPlugins=true 激活 → bundled server 落 PENDING，不被挂载"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _plugin_root = _seed_home(tmp_path, servers=[FIGMA_NAME])

    # user scope：只声明安装意图；project scope：声明启用
    user_dir = tmp_path / "cfg" / "a2c"
    project_dir = tmp_path / ".tfrobot"
    _write_settings(user_dir / "settings.json", {"installedPlugins": ["audit@acme"]})
    _write_settings(project_dir / "settings.json", {"enabledPlugins": {"audit@acme": True}})

    mounted: list[str] = []

    async def register(cfg, record: BundledServerRecord) -> None:
        mounted.append(cfg.name)

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance(
            existing_bundle_ids=lambda: set(),
            register_server=register,
            # 不传 declared → 走 _resolve_declared_settings() → project scope 读入
        )
        # project 激活 → register 不被调用
        assert mounted == []
        assert report.remounted_servers == []
        # PENDING 记录入 report
        assert len(report.pending_bundled_servers) == 1
        assert report.pending_bundled_servers[0].plugin_id == "audit@acme"
        assert report.pending_bundled_servers[0].enabled_origin == SettingsScope.PROJECT


@pytest.mark.asyncio
async def test_user_scope_enabled_plugin_bundled_server_auto_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """user scope enabledPlugins=true 激活 → bundled server 免批准正常挂载（回归守卫）"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _plugin_root = _seed_home(tmp_path, servers=[FIGMA_NAME])

    # user scope：安装意图 + 启用
    user_dir = tmp_path / "cfg" / "a2c"
    _write_settings(user_dir / "settings.json", {
        "installedPlugins": ["audit@acme"],
        "enabledPlugins": {"audit@acme": True},
    })

    mounted: list[str] = []

    async def register(cfg, record: BundledServerRecord) -> None:
        mounted.append(cfg.name)

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance(
            existing_bundle_ids=lambda: set(),
            register_server=register,
        )
        # user 激活 → register 正常调用
        assert mounted == [FIGMA_NAME]
        assert report.remounted_servers == [FIGMA_BID]
        assert report.pending_bundled_servers == []
