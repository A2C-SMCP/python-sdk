# -*- coding: utf-8 -*-
# filename: test_install_enable_separation.py
# @Time    : 2026/07/07
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
plugin install/enable 分离（#123，协议 v0.3.0 runtime-contract §2.3/§2.4/§4.8）/ install ⊥ enable separation。

TDD 目标行为用例（settings 层；对应 conformance-tests §5「install ≠ activate」「enable 原子激活」等新增条目）:
- install：config-first 写 **user scope** ``installedPlugins``（全局安装意图）+ 物化账本；**不激活**（不 stage
  SKILL、不写 ``enabledPlugins``）→ ``installed_disabled``；物化失败**原子回滚**意图条目；冲突预检保留。
- enable：挂载失败 MUST 回滚 ``installed_disabled``（``enabledPlugins`` 恢复原值 + 撤销本次 stage/register）。
- uninstall：删 ``installedPlugins`` 条目 + 清 ``enabledPlugins`` 条目。
- recovery：缺省翻转——活跃集 = 已安装（intent）∧ ``enabledPlugins`` 合并后 ``true``；absent/false 均惰性；
  账本降级为派生缓存：intent 有、账本无 → boot 重物化（账本删除无损，§4.9）。
- migrate_legacy_installs：一次性迁移（标记 = user settings ``installedPlugins`` 键存在；显式 false 不迁 true）。
- schema：``installedPlugins`` 字段形态校验（非法条目过滤 + 记错）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.installer import (
    PluginInstallError,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from a2c_smcp.computer.settings.recovery import (
    collect_enabled_bundled_servers,
    recover_marketplace_skills,
)
from a2c_smcp.computer.settings.schema import SettingsScope, validate_settings
from a2c_smcp.computer.settings.scope import user_settings_path, workdir_local_settings_path, workdir_project_settings_path
from a2c_smcp.computer.settings.store import load_installed_plugins, save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import external_plugin_dir
from a2c_smcp.utils.bundle_id import resolve_bundle_id

_SRC = {"type": "git", "url": "https://example.com/acme.git"}
_STAGE = "a2c_smcp.computer.settings.installer.stage_marketplace_skills"
_PID = "audit@acme"

# 夹具身份对（#153 + 协议 conformance §2.0）：display name 含 `.` → bundle_id 折 `_`，两者**必须分叉**。
FIGMA_NAME, FIGMA_BID = "figma.mcp", "figma_mcp"


# ── 辅助（与 test_installer.py / test_recovery.py 同构）/ helpers ─────────────
def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _env(tmp_path: Path) -> dict[str, str]:
    """重定向 XDG_CONFIG_HOME → tmp，隔离 user settings.json 读写。"""
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _read_user_settings(env: dict[str, str]) -> dict:
    p = user_settings_path(env)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _stdio(name: str, command: str = "node") -> dict:
    return {"name": name, "type": "stdio", "server_parameters": {"command": command}}


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: a skill\nlicense: MIT\n---\n# {name}\nbody\n"


def _setup_catalog(
    home: Path,
    mp: str,
    plugin: str,
    *,
    servers: Sequence[str] = (),
    skills: Sequence[str] = (),
) -> Path:
    """造 catalog 树 + marketplace.json + plugin 的 mcp-servers/、skills/，并 seed known_marketplaces。"""
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
    save_known_marketplaces(
        {"version": 1, "marketplaces": {mp: {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc123"}}},
        home=home,
    )
    return plugin_root


def _record(plugin_root: Path, *, scope: str = "user", servers: Sequence[str] = ()) -> dict:
    rec: dict = {
        "scope": scope,
        "installPath": str(plugin_root),
        "version": "1.2.0",
        "commitSha": "abc123",
        "installedAt": "2026-07-06T00:00:00Z",
    }
    # 无条件写（生产 installer 亦然）：`mcpServers` 是新格式的正向标志，条件写会与旧格式记录难辨。
    rec["mcpServers"] = list(servers)
    return rec


def _seed_installed(home: Path, plugins: dict[str, list[dict]]) -> None:
    save_installed_plugins({"version": 1, "plugins": plugins}, home=home)


def _fake_stage(calls: list[dict], *, fail: bool = False, register_skill: bool = True):
    """替身 stage：记录入参；可选注册 ``<plugin>:lint``（供回滚断言）；可选抛错。"""

    async def _stage(name, source, registry, home, *, plugin_filter=None, auto_update=False, refresh=False, timeout=0.0, env=None):
        calls.append({"name": name, "plugin_filter": set(plugin_filter or ()), "refresh": refresh})
        if register_skill:
            for plugin in plugin_filter or set():
                registry.register_or_update(
                    {
                        "name": f"{plugin}:lint",
                        "source": f"{SOURCE_MARKETPLACE}:{name}",
                        "path": str(marketplace_skill_dir(home, name).resolve()),
                    },
                )
        if fail:
            raise RuntimeError("stage boom")
        return [f"{p}:lint" for p in plugin_filter or set()]

    return _stage


class _FakeMCP:
    """记录 MCP 注入回调调用；**身份一律 bundle_id**（#153）/ Records callbacks; identity is bundle_id。"""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing: set[str] = set(existing or ())  # bundle_id 集
        self.registered: list[str] = []  # bundle_id
        self.removed: list[str] = []  # bundle_id
        self.fail_on: str | None = None  # 该 display name 的 server 注册时抛错

    def existing_bundle_ids(self) -> set[str]:
        return set(self.existing)

    async def register(self, cfg) -> None:
        if self.fail_on is not None and cfg.name == self.fail_on:
            raise RuntimeError(f"register {cfg.name} boom")
        self.registered.append(resolve_bundle_id(cfg))
        self.existing.add(resolve_bundle_id(cfg))

    async def remove(self, bundle_id: str) -> None:
        self.removed.append(bundle_id)
        self.existing.discard(bundle_id)


# ── install：不激活 + config-first 意图（§2.4 操作表 install 行）─────────────
@pytest.mark.asyncio
async def test_install_only_writes_intent_and_ledger_without_activation(tmp_path: Path, monkeypatch) -> None:
    """install-only：写 user ``installedPlugins`` + 账本；不 stage SKILL、不写 enabledPlugins → installed_disabled。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    reg = SkillRegistry()

    record = await install_plugin(_PID, reg, home, env=env)

    # 物化照旧：账本 + bundled server 解析记录
    assert record["mcpServers"] == [FIGMA_BID]
    assert _PID in load_installed_plugins(home=home)["plugins"]
    # config-first：安装意图落 user scope settings.json
    settings = _read_user_settings(env)
    assert settings.get("installedPlugins") == [_PID]
    # 不激活：SKILL 不 stage、enabledPlugins 不写（installed_disabled 惰性）
    assert calls == []
    assert len(reg) == 0
    assert "enabledPlugins" not in settings


@pytest.mark.asyncio
async def test_install_project_scope_intent_still_lands_in_user_settings(tmp_path: Path, monkeypatch) -> None:
    """安装是全局一次的事实：``--scope project`` 只影响账本记录，intent 仍写 user scope（决策 #1）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _setup_catalog(home, "acme", "audit", servers=[])
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    record = await install_plugin(_PID, SkillRegistry(), home, scope="project", project_path=str(workdir), env=env)

    assert record["scope"] == "project"
    assert _read_user_settings(env).get("installedPlugins") == [_PID]
    assert not (workdir / ".tfrobot" / "settings.json").exists()  # 意图不泄漏进项目文件


@pytest.mark.asyncio
async def test_install_materialize_failure_rolls_back_intent(tmp_path: Path, monkeypatch) -> None:
    """物化失败（bundled JSON 畸形）→ 撤销刚写的 installedPlugins 条目 + 不写账本（决策 #3 原子回滚）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    plugin_root = _setup_catalog(home, "acme", "audit")
    (plugin_root / "mcp-servers").mkdir(parents=True, exist_ok=True)
    (plugin_root / "mcp-servers" / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    with pytest.raises(Exception):  # noqa: B017 - PluginManifestError 或其包装均可
        await install_plugin(_PID, SkillRegistry(), home, env=env)

    assert _PID not in _read_user_settings(env).get("installedPlugins", [])
    assert _PID not in load_installed_plugins(home=home)["plugins"]


@pytest.mark.asyncio
async def test_install_dependency_precheck_survives_separation(tmp_path: Path, monkeypatch) -> None:
    """
    依赖预检在 install⊥enable 分离后仍在，但**只提示不拒绝**（协议 §2.5-1，#153/D3）。

    原名 ``test_install_foreign_conflict_precheck_survives_separation``：断言「外来同名硬抛 + 意图/账本零变更」。
    D3 推翻该语义后，本例改为守护相反的不变量——**依赖已满足不得阻断安装**（意图与账本都要写成）。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    mcp = _FakeMCP(existing={FIGMA_BID})

    await install_plugin(_PID, SkillRegistry(), home, env=env, existing_bundle_ids=mcp.existing_bundle_ids)

    assert _PID in _read_user_settings(env).get("installedPlugins", [])
    assert _PID in load_installed_plugins(home=home)["plugins"]


# ── enable：原子激活失败回滚（§2.4「enable 原子性」blockquote）────────────────
@pytest.mark.asyncio
async def test_enable_mount_failure_rolls_back_to_installed_disabled(tmp_path: Path, monkeypatch) -> None:
    """enable 挂载失败 → 回滚 installed_disabled：enabledPlugins 恢复 absent、本次 stage/register 全撤销。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha", "beta"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha", "beta"])]})
    _write_json(user_settings_path(env), {"installedPlugins": [_PID]})
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP()
    mcp.fail_on = "beta"
    reg = SkillRegistry()

    with pytest.raises(RuntimeError, match="beta"):
        await enable_plugin(
            _PID, reg, home, env=env,
            existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register, remove_server=mcp.remove,
        )

    settings = _read_user_settings(env)
    assert _PID not in settings.get("enabledPlugins", {})  # 原 absent → 回滚删键
    assert mcp.removed == ["alpha"]  # 已挂 alpha 撤销
    assert reg.resolve("audit:lint") is None  # 本次 stage 的 skill 撤销
    assert settings.get("installedPlugins") == [_PID]  # installation 保留


@pytest.mark.asyncio
async def test_enable_mount_failure_restores_previous_false(tmp_path: Path, monkeypatch) -> None:
    """enable 失败且原值为显式 false → 恢复 false（不残留 true、不误删原禁用意图）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: False}})
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    mcp = _FakeMCP()
    mcp.fail_on = FIGMA_NAME

    with pytest.raises(RuntimeError, match="figma"):
        await enable_plugin(
            _PID, SkillRegistry(), home, env=env,
            existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register, remove_server=mcp.remove,
        )

    assert _read_user_settings(env)["enabledPlugins"][_PID] is False


# ── uninstall：删意图 + 清 enabled 条目（§2.4 uninstall 行）───────────────────
@pytest.mark.asyncio
async def test_uninstall_clears_intent_and_enabled_entries(tmp_path: Path, monkeypatch) -> None:
    """uninstall → installedPlugins 删 pid + enabledPlugins 清条目 + 账本删除（回 available）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    mcp = _FakeMCP()

    ok = await uninstall_plugin(_PID, SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=mcp.remove)

    assert ok is True
    settings = _read_user_settings(env)
    assert settings.get("installedPlugins") == []
    assert _PID not in settings.get("enabledPlugins", {})
    assert _PID not in load_installed_plugins(home=home)["plugins"]


# ── recovery：缺省翻转 + 活跃集 = installed ∧ enabled（§4.8.1）────────────────
@pytest.mark.asyncio
async def test_recover_installed_without_enabled_is_lazy(tmp_path: Path) -> None:
    """installed_disabled（intent 有、enabledPlugins absent）→ 惰性：不 stage、入 skipped_disabled（缺省翻转）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    reg = SkillRegistry()

    report = await recover_marketplace_skills(reg, home, {"installedPlugins": [_PID]}, env=_env(tmp_path))

    assert report.restored_plugins == [] and report.restored_skills == []
    assert report.skipped_disabled == [_PID]
    assert reg.resolve("audit:lint") is None


@pytest.mark.asyncio
async def test_recover_requires_intent_not_ledger(tmp_path: Path) -> None:
    """账本有、intent 无（孤儿派生缓存）→ 即使 enabledPlugins=true 也不激活（活跃集 = installed ∧ enabled）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    reg = SkillRegistry()
    declared = {"installedPlugins": [], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.restored_plugins == [] and report.restored_skills == []
    assert reg.resolve("audit:lint") is None
    assert collect_enabled_bundled_servers(home, declared, env=_env(tmp_path)) == []


@pytest.mark.asyncio
async def test_recover_installed_and_enabled_restores(tmp_path: Path) -> None:
    """installed_enabled（intent 含 ∧ enabledPlugins=true）→ SKILL 恢复（正向契约不变）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    reg = SkillRegistry()
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert _PID in report.restored_plugins
    assert "audit:lint" in report.restored_skills
    assert reg.resolve("audit:lint") is not None


@pytest.mark.asyncio
async def test_recover_rematerializes_missing_ledger_from_intent(tmp_path: Path) -> None:
    """账本删除 → boot 从 installedPlugins 重物化重建（§4.9「删除无损」；conformance §5 账本删除条目）。"""
    home = _home(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])  # 账本刻意不 seed
    reg = SkillRegistry()
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.rematerialized == [_PID]
    rebuilt = load_installed_plugins(home=home)["plugins"][_PID][0]
    assert rebuilt["installPath"].replace("\\", "/").endswith("marketplace/acme/plugins/audit")
    assert rebuilt["mcpServers"] == [FIGMA_BID]
    assert "audit:lint" in report.restored_skills  # enabled → 重物化后照常激活
    assert collect_enabled_bundled_servers(home, declared, env=_env(tmp_path))[0].config.name == FIGMA_NAME


@pytest.mark.asyncio
async def test_recover_rematerializes_disabled_installs_without_activation(tmp_path: Path) -> None:
    """installed_disabled 的重物化也执行（installed = materialized 不变量；enable 无需重 clone），但不激活。"""
    home = _home(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    reg = SkillRegistry()
    declared = {"installedPlugins": [_PID]}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.rematerialized == [_PID]
    assert _PID in load_installed_plugins(home=home)["plugins"]  # 账本重建
    assert report.restored_skills == [] and reg.resolve("audit:lint") is None  # 仍惰性
    assert report.skipped_disabled == [_PID]


def test_collect_gates_on_installed_and_enabled(tmp_path: Path) -> None:
    """collect：intent 有但未启用 → 空；installed ∧ true → 可查询（§4.8 blockquote 的 enabled 门槛翻转）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})

    assert collect_enabled_bundled_servers(home, {"installedPlugins": [_PID]}, env=_env(tmp_path)) == []
    records = collect_enabled_bundled_servers(
        home, {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}, env=_env(tmp_path),
    )
    assert [r.config.name for r in records] == [FIGMA_NAME]


# ── migrate_legacy_installs：一次性迁移（迁移指南「保住既有用户现状」）─────────
# 红灯阶段函数尚不存在 → 函数内 import 使失败定位在单测粒度（绿灯后可上移模块顶）。
@pytest.mark.asyncio
async def test_migrate_writes_intent_and_enabled_true(tmp_path: Path, monkeypatch) -> None:
    """v0.2.x 存量（账本有、settings 全无）→ 迁 installedPlugins + user enabledPlugins=true（保住活跃态）。"""
    from a2c_smcp.computer.settings.installer import migrate_legacy_installs

    monkeypatch.chdir(tmp_path)  # 隔离 project/local 层（#116 cwd 锚定）
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})

    migrated = migrate_legacy_installs(home, env=env)

    assert migrated == [_PID]
    settings = _read_user_settings(env)
    assert settings["installedPlugins"] == [_PID]
    assert settings["enabledPlugins"][_PID] is True


@pytest.mark.asyncio
async def test_migrate_preserves_explicit_false(tmp_path: Path, monkeypatch) -> None:
    """升级前显式 false（v0.2.x 已禁用）→ 迁 installedPlugins 但不写 true（保持 installed_disabled）。"""
    from a2c_smcp.computer.settings.installer import migrate_legacy_installs

    monkeypatch.chdir(tmp_path)
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    _seed_installed(home, {_PID: [_record(root)]})
    _write_json(user_settings_path(env), {"enabledPlugins": {_PID: False}})

    migrate_legacy_installs(home, env=env)

    settings = _read_user_settings(env)
    assert settings["installedPlugins"] == [_PID]
    assert settings["enabledPlugins"][_PID] is False


@pytest.mark.asyncio
async def test_migrate_noop_when_marker_present(tmp_path: Path, monkeypatch) -> None:
    """user settings 已含 installedPlugins 键（哪怕空数组）= 已迁移 → 严格 no-op（决策 #2 防复活）。"""
    from a2c_smcp.computer.settings.installer import migrate_legacy_installs

    monkeypatch.chdir(tmp_path)
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    _seed_installed(home, {_PID: [_record(root)]})  # 账本残留（如 intent 手动删除后未 gc）
    _write_json(user_settings_path(env), {"installedPlugins": []})

    migrated = migrate_legacy_installs(home, env=env)

    assert migrated == []
    settings = _read_user_settings(env)
    assert settings["installedPlugins"] == []  # 不复活
    assert "enabledPlugins" not in settings


@pytest.mark.asyncio
async def test_migrate_writes_marker_even_with_empty_ledger_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    """空账本首跑也写标记键（[]），二次调用 no-op（只跑一次机制自洽）。"""
    from a2c_smcp.computer.settings.installer import migrate_legacy_installs

    monkeypatch.chdir(tmp_path)
    home = _home(tmp_path)
    env = _env(tmp_path)

    assert migrate_legacy_installs(home, env=env) == []
    assert _read_user_settings(env)["installedPlugins"] == []
    assert migrate_legacy_installs(home, env=env) == []  # 幂等


# ── 隔离审查回归（fix-review：2🔴 + 覆盖空洞）─────────────────────────────────
@pytest.mark.asyncio
async def test_recover_rematerialize_failure_keeps_installed_disabled(tmp_path: Path) -> None:
    """🔴#1：enabled + 账本缺 + 物化失败（bundled JSON 畸形）→ skills **不**进投影（无 rust-sdk#102 半态）、不抛。"""
    home = _home(tmp_path)
    plugin_root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    (plugin_root / "mcp-servers").mkdir(parents=True, exist_ok=True)
    (plugin_root / "mcp-servers" / "bad.json").write_text("{not json", encoding="utf-8")
    reg = SkillRegistry()
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.rematerialized == []
    assert report.restored_skills == [] and report.restored_plugins == []  # 整体保持 installed_disabled
    assert reg.resolve("audit:lint") is None  # skill 不亮（半态防御）
    assert _PID not in load_installed_plugins(home=home)["plugins"]  # 账本未重建


@pytest.mark.asyncio
async def test_headless_install_before_first_boot_preserves_legacy_actives(tmp_path: Path, monkeypatch) -> None:
    """🔴#2：升级后未经 boot 的 headless install 不得抢写迁移标记——install 前置迁移保住 v0.2.x 存量活跃态。"""
    monkeypatch.chdir(tmp_path)
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {"legacy@acme": [_record(root)]})  # v0.2.x 存量（settings 全无）
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    await install_plugin(_PID, SkillRegistry(), home, env=env)

    settings = _read_user_settings(env)
    assert settings["installedPlugins"] == ["legacy@acme", _PID]  # 迁移先行，存量未丢
    assert settings["enabledPlugins"] == {"legacy@acme": True}  # 存量保活跃；新装不写 enabled


@pytest.mark.asyncio
async def test_uninstall_scoped_keeps_intent_for_remaining_scopes(tmp_path: Path, monkeypatch) -> None:
    """🟡#5：指定 scope 卸载且其余 scope 记录仍在 → installedPlugins 与 enabledPlugins 条目保留（§2.4）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_installed(
        home,
        {_PID: [_record(root), {**_record(root), "scope": "project", "projectPath": str(workdir)}]},
    )
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    # #262：预置**非空** registry——空 registry 会让「多 scope 卸载误删技能」完全失明（验收第 3 条）。
    reg = SkillRegistry()
    reg.register(
        {
            "name": "audit:lint",
            "source": f"{SOURCE_MARKETPLACE}:acme",
            "path": str(marketplace_skill_dir(home, "acme").resolve()),
            "description": "d",
        },
    )

    ok = await uninstall_plugin(
        _PID, reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="project", keep_servers=True,
    )

    assert ok is True
    # #258：catalog 内相对源落点由归属谓词无条件保留；多 scope 门控回归锁见 test_installer.py 的 scoped external clone 用例。
    assert root.exists()
    remaining = load_installed_plugins(home=home)["plugins"][_PID]
    assert [r["scope"] for r in remaining] == ["user"]  # 仅删 project 记录
    assert reg.resolve("audit:lint") is not None  # #262：retained 记录仍在 → 技能保留
    settings = _read_user_settings(env)
    assert settings["installedPlugins"] == [_PID]  # 安装事实未消失 → 意图保留
    assert settings["enabledPlugins"][_PID] is True  # enabled 条目保留


@pytest.mark.asyncio
async def test_enable_mount_failure_restores_previous_true(tmp_path: Path, monkeypatch) -> None:
    """🟡#6：enable 失败且原值为显式 true（重复 enable / 另一 scope 已启用）→ 恢复 true，不误删。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha", "beta"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha", "beta"])]})
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    mcp = _FakeMCP()
    mcp.fail_on = "beta"

    with pytest.raises(RuntimeError, match="beta"):
        await enable_plugin(
            _PID, SkillRegistry(), home, env=env,
            existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register, remove_server=mcp.remove,
        )

    assert _read_user_settings(env)["enabledPlugins"][_PID] is True


# ── #125 任务 1：重物化 scope 混合线索推回（enable/disable scope 契约的逆运算）──
@pytest.mark.asyncio
async def test_recover_rematerialize_infers_project_scope_from_enabled_layer(tmp_path: Path, monkeypatch) -> None:
    """project 层 ``enabledPlugins[pid]`` 条目 = scope 线索 → 重建记录 scope=project + projectPath=cwd（非归一 user）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _write_json(workdir_project_settings_path(workdir), {"enabledPlugins": {_PID: True}})
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=env)

    assert report.rematerialized == [_PID]
    records = load_installed_plugins(home=home)["plugins"][_PID]
    assert [(r["scope"], r.get("projectPath")) for r in records] == [("project", str(Path.cwd()))]
    assert report.scope_normalized == []  # 有线索 → 不归一


@pytest.mark.asyncio
async def test_recover_rematerialize_multi_layer_clues_rebuild_multi_records(tmp_path: Path, monkeypatch) -> None:
    """多层线索（user false + local true）→ 重建多条记录（多 scope 不塌缩单条）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: False}})
    _write_json(workdir_local_settings_path(workdir), {"enabledPlugins": {_PID: True}})
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=env)

    assert report.rematerialized == [_PID]
    records = load_installed_plugins(home=home)["plugins"][_PID]
    by_scope = {r["scope"]: r.get("projectPath") for r in records}
    assert by_scope == {"user": None, "local": str(Path.cwd())}
    assert report.scope_normalized == []


@pytest.mark.asyncio
async def test_recover_rematerialize_no_clue_falls_back_user_and_reports(tmp_path: Path, monkeypatch, caplog) -> None:
    """无任何层线索 → 归一 user + WARN + ``report.scope_normalized`` 显式标注（issue #125 方向 b 兜底）。"""
    from a2c_smcp.computer.settings import recovery as recovery_mod

    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    declared = {"installedPlugins": [_PID]}

    # 项目 logger "a2c_smcp" 关闭 propagate → caplog.handler 直挂源模块 logger（同 test_window_uri 惯例）
    recovery_mod.logger.addHandler(caplog.handler)
    try:
        report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=env)
    finally:
        recovery_mod.logger.removeHandler(caplog.handler)

    assert report.rematerialized == [_PID]
    assert report.scope_normalized == [_PID]
    records = load_installed_plugins(home=home)["plugins"][_PID]
    assert [(r["scope"], r.get("projectPath")) for r in records] == [("user", None)]
    assert _PID in caplog.text and "normalized" in caplog.text


@pytest.mark.asyncio
async def test_recover_rematerialize_project_installed_declaration_is_clue(tmp_path: Path, monkeypatch) -> None:
    """project 层 ``installedPlugins`` 声明（声明式复现场景）亦是 scope 线索（installed_disabled 无 enabled 条目时）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _write_json(workdir_project_settings_path(workdir), {"installedPlugins": [_PID]})
    declared = {"installedPlugins": [_PID]}

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=env)

    assert report.rematerialized == [_PID]
    records = load_installed_plugins(home=home)["plugins"][_PID]
    assert [(r["scope"], r.get("projectPath")) for r in records] == [("project", str(Path.cwd()))]
    assert report.scope_normalized == []


@pytest.mark.asyncio
async def test_recover_rematerialize_replaces_dead_records_of_stale_scopes(tmp_path: Path, monkeypatch) -> None:
    """重建成功后清扫该 pid 下 installPath 已死的残留记录（防 CLI enable/disable 循环误写死 scope 层）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [{"scope": "project", "installPath": str(tmp_path / "gone"), "projectPath": str(workdir)}]})
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=env)

    assert report.rematerialized == [_PID]
    records = load_installed_plugins(home=home)["plugins"][_PID]
    assert [(r["scope"], r.get("projectPath")) for r in records] == [("user", None)]  # 死 project 记录被清扫


@pytest.mark.asyncio
async def test_uninstall_clears_cwd_visible_enabled_entries_without_ledger_projectpath(tmp_path: Path, monkeypatch) -> None:
    """uninstall 清理集并入 cwd：账本记录无 projectPath（归一后形态）时，cwd 可见 project 层条目也清净（防重装即激活）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    root = _setup_catalog(home, "acme", "audit")
    _seed_installed(home, {_PID: [_record(root)]})  # user record，无 projectPath
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    _write_json(workdir_project_settings_path(workdir), {"enabledPlugins": {_PID: True}})
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    ok = await uninstall_plugin(_PID, SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env)

    assert ok is True
    proj = json.loads(workdir_project_settings_path(workdir).read_text(encoding="utf-8"))
    assert _PID not in proj.get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_uninstall_does_not_create_tfrobot_dir_in_cwd(tmp_path: Path, monkeypatch) -> None:
    """cwd 无 ``.tfrobot/`` 时 uninstall 不得凭空创建（file_lock 会 mkdir——写调用必须先存在性守卫）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "bare"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    root = _setup_catalog(home, "acme", "audit")
    _seed_installed(home, {_PID: [_record(root)]})
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    ok = await uninstall_plugin(_PID, SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env)

    assert ok is True
    assert not (workdir / ".tfrobot").exists()  # 不制造垃圾目录/锁文件


@pytest.mark.asyncio
async def test_recover_rematerialize_ignores_project_false_override(tmp_path: Path, monkeypatch) -> None:
    """project 层纯 ``false`` 是禁用覆盖、非 install-scope 线索——不得捏造 project 记录（隔离审查 🟡#3：
    否则 CLI enable 逐 scope 写会把团队 ``false`` 覆写为 ``true``，反向覆写治理意图）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    _write_json(workdir_project_settings_path(workdir), {"enabledPlugins": {_PID: False}})  # 团队禁用覆盖
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: False}}  # merged：project false 获胜

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=env)

    assert report.rematerialized == [_PID]
    records = load_installed_plugins(home=home)["plugins"][_PID]
    assert [(r["scope"], r.get("projectPath")) for r in records] == [("user", None)]  # 不捏造 project 记录
    assert report.scope_normalized == []  # user 层有线索，非归一


@pytest.mark.asyncio
async def test_recover_repairs_mixed_health_records(tmp_path: Path, monkeypatch) -> None:
    """混合健康度（健康 user 记录 + 损坏 project 记录）也触发重物化并清扫损坏残留（隔离审查 🟡#4：
    ∃ 判据会让损坏 scope 记录每次 boot 被 WARN-skip 却永不修复——窄化半态回归口）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    stale = home / "stale-project-copy"
    (stale / "mcp-servers").mkdir(parents=True)
    (stale / "mcp-servers" / "bad.json").write_text("{not json", encoding="utf-8")
    _seed_installed(
        home,
        {_PID: [_record(root, servers=[FIGMA_BID]), {"scope": "project", "installPath": str(stale), "projectPath": "/elsewhere"}]},
    )
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=env)

    assert report.rematerialized == [_PID]  # ∀ 判据：混合健康度触发重物化
    records = load_installed_plugins(home=home)["plugins"][_PID]
    assert [(r["scope"], r["installPath"]) for r in records] == [("user", str(root))]  # 损坏 project 残留被清扫
    assert [r.config.name for r in collect_enabled_bundled_servers(home, declared, env=env)] == [FIGMA_NAME]  # 无半态


# ── #125 任务 2：悬挂意图 prune 执行入口（installer 是 settings 意图唯一写者）──
def test_prune_plugin_intent_clears_intent_enabled_and_dead_ledger(tmp_path: Path, monkeypatch) -> None:
    """prune：删 user 意图 + 清 user/cwd 可见层 enabled 条目 + 弹出死账本记录（针对悬挂意图的 uninstall 等价物）。"""
    from a2c_smcp.computer.settings.installer import prune_plugin_intent

    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}})
    _write_json(workdir_project_settings_path(workdir), {"enabledPlugins": {_PID: False}})
    _seed_installed(home, {_PID: [{"scope": "user", "installPath": str(tmp_path / "gone")}]})

    prune_plugin_intent(_PID, home, env=env)

    settings = _read_user_settings(env)
    assert _PID not in settings.get("installedPlugins", [])
    assert _PID not in settings.get("enabledPlugins", {})
    proj = json.loads(workdir_project_settings_path(workdir).read_text(encoding="utf-8"))
    assert _PID not in proj.get("enabledPlugins", {})
    assert _PID not in load_installed_plugins(home=home)["plugins"]


def test_prune_plugin_intent_runs_legacy_migration_first(tmp_path: Path, monkeypatch) -> None:
    """prune 写 installedPlugins 键前必须先跑一次性迁移（防标记误置永久丢弃 v0.2.x 存量活跃态，同 install/uninstall）。"""
    from a2c_smcp.computer.settings.installer import prune_plugin_intent

    monkeypatch.chdir(tmp_path)
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "legacy")
    _seed_installed(home, {"legacy@acme": [_record(root)], _PID: [{"scope": "user", "installPath": str(tmp_path / "gone")}]})
    # user settings 无 installedPlugins 键 = 迁移尚未发生

    prune_plugin_intent(_PID, home, env=env)

    settings = _read_user_settings(env)
    assert settings["installedPlugins"] == ["legacy@acme"]  # 迁移先行：存量迁入；目标 pid 已 prune
    assert settings["enabledPlugins"].get("legacy@acme") is True  # 存量活跃态保住
    assert _PID not in settings["enabledPlugins"]


def test_prune_plugin_intent_warns_on_residual_project_declaration(tmp_path: Path, monkeypatch, caplog) -> None:
    """pid 仍见于 project 层 ``installedPlugins`` 声明 → WARN 指明文件路径、不静默改写 committable 团队声明。"""
    from a2c_smcp.computer.settings import installer as installer_mod
    from a2c_smcp.computer.settings.installer import prune_plugin_intent

    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    proj_path = workdir_project_settings_path(workdir)
    _write_json(proj_path, {"installedPlugins": [_PID]})
    _write_json(user_settings_path(env), {"installedPlugins": [_PID]})

    # 项目 logger 关闭 propagate → caplog.handler 直挂源模块 logger（同 test_window_uri 惯例）
    installer_mod.logger.addHandler(caplog.handler)
    try:
        prune_plugin_intent(_PID, home, env=env)
    finally:
        installer_mod.logger.removeHandler(caplog.handler)

    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    assert proj.get("installedPlugins") == [_PID]  # committable 声明不动
    assert str(proj_path) in caplog.text  # WARN 指路人工处理


# ── #125 任务 4：账本失效判据补 bundled JSON 校验（半态防御闭环）──────────────
@pytest.mark.asyncio
async def test_recover_rematerializes_on_corrupt_bundled_json(tmp_path: Path) -> None:
    """账本 installPath 目录在、bundled JSON 损坏 → 判失效走重物化（catalog 完好 → 修复指回 catalog root）。"""
    home = _home(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    stale = home / "stale-copy"
    (stale / "mcp-servers").mkdir(parents=True)
    (stale / "mcp-servers" / "bad.json").write_text("{not json", encoding="utf-8")
    _seed_installed(home, {_PID: [{"scope": "user", "installPath": str(stale)}]})
    reg = SkillRegistry()
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.rematerialized == [_PID]
    rebuilt = load_installed_plugins(home=home)["plugins"][_PID]
    assert all(r["installPath"] != str(stale) for r in rebuilt)  # 修复：不再指向损坏副本
    assert "audit:lint" in report.restored_skills
    assert collect_enabled_bundled_servers(home, declared, env=_env(tmp_path))[0].config.name == FIGMA_NAME  # server 可查询（无半态）


@pytest.mark.asyncio
async def test_recover_corrupt_bundled_json_unrepairable_stays_disabled(tmp_path: Path) -> None:
    """catalog 自身损坏（重物化也失败）→ 整体保持 installed_disabled：skill 不亮、server 不可查询（半态消除）。"""
    home = _home(tmp_path)
    plugin_root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    (plugin_root / "mcp-servers").mkdir(parents=True, exist_ok=True)
    (plugin_root / "mcp-servers" / "bad.json").write_text("{not json", encoding="utf-8")
    _seed_installed(home, {_PID: [_record(plugin_root)]})  # 目录在、JSON 损坏——旧判据误判「已物化」
    reg = SkillRegistry()
    declared = {"installedPlugins": [_PID], "enabledPlugins": {_PID: True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.rematerialized == []
    assert report.restored_skills == [] and report.restored_plugins == []
    assert reg.resolve("audit:lint") is None  # skill 不单独点亮（rust-sdk#102 半态防御）
    assert collect_enabled_bundled_servers(home, declared, env=_env(tmp_path)) == []


# ── schema：installedPlugins 字段校验 ─────────────────────────────────────────
def test_schema_validates_installed_plugins_entries() -> None:
    """installedPlugins：数组元素须 ``<plugin>@<marketplace>`` 形态；非法条目过滤 + 记错（容错风格）。"""
    cleaned, errors = validate_settings(
        {"installedPlugins": ["audit@acme", "Bad Name", 42]},
        SettingsScope.USER,
    )
    assert cleaned["installedPlugins"] == ["audit@acme"]
    assert len(errors) == 2


def test_schema_installed_plugins_whole_field_type_error_drops() -> None:
    """整字段类型错（非数组）→ 判废回退（与其它已知字段同纪律）。"""
    cleaned, errors = validate_settings({"installedPlugins": {"audit@acme": True}}, SettingsScope.USER)
    assert "installedPlugins" not in cleaned
    assert len(errors) == 1


# ── #259：账本记录失效时 enable 预检（fail-fast + 保持 installed_disabled）──────
def _enable_kwargs(mcp: _FakeMCP) -> dict:
    """enable_plugin 的 MCP 注入回调组（``existing_bundle_ids`` 与 ``register_server`` 须成对）。"""
    return {
        "existing_bundle_ids": mcp.existing_bundle_ids,
        "register_server": mcp.register,
        "remove_server": mcp.remove,
    }


def _declare_entry_skills(home: Path, mp: str, plugin: str, skills: object) -> None:
    """给 marketplace 条目写 ``skills`` 声明（#259 声明态判据；支持畸形值以对齐 rust json_truthy）。"""
    path = marketplace_skill_dir(home, mp) / ".tfrobot-plugin" / "marketplace.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in manifest["plugins"]:
        if entry.get("name") == plugin:
            entry["skills"] = skills
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _seed_enabled_intent(env: dict[str, str], value: bool | None = None) -> None:
    """seed user settings：安装意图 + 可选 enabledPlugins 原值（None = 原 absent）。"""
    settings: dict = {"installedPlugins": [_PID]}
    if value is not None:
        settings["enabledPlugins"] = {_PID: value}
    _write_json(user_settings_path(env), settings)


@pytest.mark.asyncio
async def test_enable_missing_install_path_fails_before_any_side_effect(tmp_path: Path, monkeypatch) -> None:
    """#259：installPath 目录缺失 → PluginInstallError，零副作用、原 absent 保持 absent（installed_disabled）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_enabled_intent(env)
    shutil.rmtree(root)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp, reg = _FakeMCP(), SkillRegistry()

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))

    message = str(ei.value)
    assert str(root) in message  # 点名死路径
    assert "enabledPlugins is unchanged" in message  # 不断言可能为 true 的 installed_disabled（审查 🟡7）
    assert "marketplace remove acme" in message and "plugin install audit@acme" in message  # 可执行的恢复指引
    settings = _read_user_settings(env)
    assert _PID not in settings.get("enabledPlugins", {})  # 原 absent → 仍 absent
    assert settings.get("installedPlugins") == [_PID]  # 安装意图保留
    assert _PID in load_installed_plugins(home=home)["plugins"]  # 账本保留（可恢复）
    assert calls == []  # 未进入 stage（不会因重 clone 成功而部分成功）
    assert mcp.registered == []  # 未挂任何 server
    assert reg.resolve("audit:lint") is None  # 未点亮任何 skill


@pytest.mark.asyncio
async def test_enable_missing_install_path_preserves_explicit_false(tmp_path: Path, monkeypatch) -> None:
    """#259：原显式 false（用户停用意图）→ 失败后仍是 false，不被改写为 true。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_enabled_intent(env, value=False)
    shutil.rmtree(root)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert _read_user_settings(env)["enabledPlugins"][_PID] is False


@pytest.mark.asyncio
async def test_enable_skills_only_missing_root_fails(tmp_path: Path, monkeypatch) -> None:
    """#259：``mcpServers: []`` 的 skills-only 记录，根缺失同样失败（原实现返回成功且零技能）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})  # 无条件写空 mcpServers
    _seed_enabled_intent(env)
    shutil.rmtree(root)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError, match="install"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert calls == []
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_enable_declared_bundled_server_missing_fails(tmp_path: Path, monkeypatch) -> None:
    """#259：根在、但账本声明的 bundled server 文件被删 → 失败并点名 bundle_id（防静默少挂）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_enabled_intent(env)
    (root / "mcp-servers" / f"{FIGMA_NAME}.json").unlink()  # 根仍在：目录存在 ∧ JSON 可解析（空集）
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP()

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    assert FIGMA_BID in str(ei.value)  # 点名缺的 bundle_id
    assert calls == [] and mcp.registered == []
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_enable_external_clone_missing_fails_without_reclone(tmp_path: Path, monkeypatch) -> None:
    """#259：外部 clone 源（``.plugins/<mp>/<plugin>``）缺失同样失败，且失败发生在投影前（不重 clone）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = external_plugin_dir(home, "acme", "audit")
    assert not ext.exists()
    _seed_installed(home, {_PID: [{"scope": "user", "installPath": str(ext), "mcpServers": []}]})
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert str(ext) in str(ei.value)
    assert calls == []  # stage（会重 clone）根本没被调用 → 不可能「重 clone 后部分成功」


@pytest.mark.asyncio
async def test_enable_multi_scope_any_dead_record_fails_whole(tmp_path: Path, monkeypatch) -> None:
    """#259 / 架构师裁决：多 scope 用 ∀——任一记录不可物化即整体失败，健康 scope 也不得先被启用。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    healthy = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    dead = tmp_path / "dead-project-copy"
    _seed_installed(
        home,
        {
            _PID: [
                _record(healthy, scope="user", servers=[FIGMA_BID]),
                {"scope": "project", "installPath": str(dead), "projectPath": str(tmp_path / "proj"), "mcpServers": []},
            ]
        },
    )
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, scope="user", env=env, **_enable_kwargs(_FakeMCP()))

    assert str(dead) in str(ei.value)  # 点名死路径
    assert calls == []
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})  # 健康 scope 也未被写


@pytest.mark.asyncio
async def test_enable_root_exists_but_declared_skills_gone_fails(tmp_path: Path, monkeypatch) -> None:
    """#259 裁决：声明了 skills 但零可投影（skills 包被删）→ 失败，不返回「启用成功但零技能」。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])  # 声明态判据的触发条件
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    shutil.rmtree(root / "skills")  # 根仍在，skills 包全没
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert "SKILL" in str(ei.value)
    assert calls == []
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_enable_truly_empty_plugin_still_succeeds(tmp_path: Path, monkeypatch) -> None:
    """#259 裁决正对照：真正未声明任何内容的空 plugin（无 skills / 无 bundled server）启用仍成功。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")  # 无 servers、无 skills、无覆写声明
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls, register_skill=False))
    mcp = _FakeMCP()

    await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    assert _read_user_settings(env)["enabledPlugins"][_PID] is True
    assert mcp.registered == []


@pytest.mark.asyncio
async def test_enable_healthy_plugin_with_real_stage_succeeds(tmp_path: Path) -> None:
    """#259 正对照（真实 ``stage_marketplace_skills``，非替身）：完好 plugin 启用后 skill + server 一并入投影。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_enabled_intent(env)
    mcp, reg = _FakeMCP(), SkillRegistry()

    await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))

    assert reg.resolve("audit:lint") is not None  # 真 stage 注册了 skill
    assert mcp.registered == [FIGMA_BID]
    assert _read_user_settings(env)["enabledPlugins"][_PID] is True


@pytest.mark.asyncio
async def test_enable_mcp_only_plugin_with_placeholder_skills_dir_succeeds(tmp_path: Path, monkeypatch) -> None:
    """#259 审查 🔴1 正对照：MCP-only plugin 顺带提交 ``skills/README.md`` ≠ 声明 SKILL，启用仍成功。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "README.md").write_text("placeholder\n", encoding="utf-8")
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls, register_skill=False))
    mcp = _FakeMCP()

    await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    assert _read_user_settings(env)["enabledPlugins"][_PID] is True
    assert mcp.registered == [FIGMA_BID]


@pytest.mark.asyncio
async def test_enable_override_container_projects(tmp_path: Path, monkeypatch) -> None:
    """#259 审查 🟡6：``entry.skills`` 覆写容器（非约定 ``skills/``）有包 → 可投影，启用成功。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    extra = root / "extra" / "lint"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text(_skill_md("lint"), encoding="utf-8")
    _declare_entry_skills(home, "acme", "audit", ["extra"])
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    monkeypatch.setattr(_STAGE, _fake_stage([]))  # 替身注册 audit:lint（expects_skills 需非空注册）

    await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert _read_user_settings(env)["enabledPlugins"][_PID] is True


@pytest.mark.asyncio
async def test_enable_declared_override_missing_fails(tmp_path: Path, monkeypatch) -> None:
    """#259 审查 🟡6：声明了覆写容器但目录被删 → 声明态零投影，失败。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    _declare_entry_skills(home, "acme", "audit", ["extra"])  # 声明存在，容器从未建/已删
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError, match="declares SKILL packages"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert calls == []
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_enable_invalid_skill_frontmatter_rolls_back(tmp_path: Path) -> None:
    """#259 审查 🔴2：SKILL.md 在但 frontmatter 非法 → 预检放行、真实 stage 零注册 → ③.6 抛错并回滚。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    (root / "skills" / "lint" / "SKILL.md").write_text(  # 缺 description → 装载 ERROR 跳过
        "---\nname: lint\nlicense: MIT\n---\n# lint\nbody\n", encoding="utf-8",
    )
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    # 不 monkeypatch _STAGE：走真实 stage_marketplace_skills（其降级不抛，正是要收口的通道）
    reg, mcp = SkillRegistry(), _FakeMCP()

    with pytest.raises(PluginInstallError, match="staging registered none"):
        await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))

    assert reg.resolve("audit:lint") is None  # 零注册
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})  # 回滚到原值（absent）
    assert _PID in load_installed_plugins(home=home)["plugins"]  # 安装事实保留
    assert mcp.registered == []


@pytest.mark.asyncio
async def test_enable_record_without_install_path_reports_it(tmp_path: Path, monkeypatch) -> None:
    """#259 审查 🟡6：``installPath`` 缺失档位的消息可读（不是空白 / 不误报目录缺失）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    _seed_installed(home, {_PID: [{"scope": "user", "mcpServers": []}]})
    _seed_enabled_intent(env)
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    with pytest.raises(PluginInstallError, match="no installPath"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))


def _write_plugin_json(plugin_root: Path, obj: dict) -> None:
    """写 plugin.json（strict 组件声明 / 冲突场景用）。"""
    _write_json(plugin_root / ".tfrobot-plugin" / "plugin.json", obj)


def _set_entry_strict(home: Path, mp: str, plugin: str, strict: bool) -> None:
    """改 marketplace 条目的 ``strict``（§4.4 冲突场景用）。"""
    path = marketplace_skill_dir(home, mp) / ".tfrobot-plugin" / "marketplace.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in manifest["plugins"]:
        if entry.get("name") == plugin:
            entry["strict"] = strict
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.asyncio
async def test_enable_plugin_json_declares_servers_but_zero_projection_fails(tmp_path: Path, monkeypatch) -> None:
    """#259 架构师审查 🔴1：strict plugin.json 声明了 ``mcpServers`` 但零投影 → 失败（对齐 rust declared_components）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")  # 无 mcp-servers/
    _write_plugin_json(root, {"name": "audit", "version": "1.2.0", "mcpServers": ["./mcp-servers"]})
    _seed_installed(home, {_PID: [_record(root)]})  # 账本 mcpServers: []（install 解析为空）
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError, match="declares bundled MCP servers"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert calls == []
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_enable_plugin_json_declared_servers_with_files_succeeds(tmp_path: Path, monkeypatch) -> None:
    """#259 架构师审查 🔴1 正对照：同样声明 ``mcpServers`` 但文件在 → 可投影，启用成功。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _write_plugin_json(root, {"name": "audit", "version": "1.2.0", "mcpServers": ["./mcp-servers"]})
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_enabled_intent(env)
    monkeypatch.setattr(_STAGE, _fake_stage([], register_skill=False))
    mcp = _FakeMCP()

    await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    assert _read_user_settings(env)["enabledPlugins"][_PID] is True
    assert mcp.registered == [FIGMA_BID]


@pytest.mark.asyncio
async def test_enable_plugin_json_skills_strict_true_zero_projection_fails(tmp_path: Path, monkeypatch) -> None:
    """#271：strict=true（默认）+ ``plugin.json.skills`` 声明但零投影 → 预检失败。

    既有用例只覆盖 strict=false 的清单冲突档（走 ``manifests conflict``），走不进「声明了 SKILL 内容」这支；
    这里补 strict=true 下缺 ``skills/`` 容器的失败路径，与 ``test_enable_root_exists_but_declared_skills_gone_fails``
    （声明源是 ``entry.skills``）互为对照——两条声明源都归 :func:`entry_declares_component` 判。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")  # 无 skills/
    _write_plugin_json(root, {"name": "audit", "version": "1.2.0", "skills": ["skills"]})
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError, match="declares SKILL packages"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert calls == []  # 预检即失败，未进 stage
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_enable_staging_zero_registration_not_masked_by_old_active(tmp_path: Path) -> None:
    """#259 架构师审查 🟡2：旧活跃 refs 不得掩盖**本次** stage 零注册——判据取 stage 返回值。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    (root / "skills" / "lint" / "SKILL.md").write_text(  # 本次装载会跳过（缺 description）
        "---\nname: lint\nlicense: MIT\n---\n", encoding="utf-8",
    )
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    reg = SkillRegistry()
    # 旧投影：上一次成功 stage 留下的活跃 ref
    assert reg.register({"name": "audit:lint", "source": f"{SOURCE_MARKETPLACE}:acme", "path": str(root / "skills" / "lint")})

    with pytest.raises(PluginInstallError, match="staging registered none"):
        await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(_FakeMCP()))

    assert reg.resolve("audit:lint") is not None  # 预存条目不是本次新增 → 回滚不动它
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_enable_staging_zero_registration_restores_explicit_false(tmp_path: Path) -> None:
    """#259 架构师审查：③.6 失败回滚时，原显式 false 恢复 false。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    (root / "skills" / "lint" / "SKILL.md").write_text("---\nname: lint\nlicense: MIT\n---\n", encoding="utf-8")
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env, value=False)

    with pytest.raises(PluginInstallError, match="staging registered none"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert _read_user_settings(env)["enabledPlugins"][_PID] is False


@pytest.mark.asyncio
async def test_enable_staging_zero_registration_restore_write_failure_warns(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#271：回滚写本身失败时 → 记 WARN、**原异常不被顶替**，且 ③.6 文案不得声称一定已恢复。

    回滚是 best-effort（``except Exception`` 吞掉写失败后 ``raise`` 原异常）；消息若写死
    「enabledPlugins was restored」就是在撒谎。本用例钉住「WARN + 原异常仍上抛」+ 措辞带不确定性。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    (root / "skills" / "lint" / "SKILL.md").write_text("---\nname: lint\nlicense: MIT\n---\n", encoding="utf-8")
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    mcp = _FakeMCP()
    from a2c_smcp.computer.settings import installer as installer_mod

    orig_write = installer_mod._write_enabled_plugin
    writes: list[bool | None] = []

    def _flaky_write(plugin_id: str, value: bool | None, scope: str, project_path: str | None,
                     env: Mapping[str, str] | None) -> None:
        writes.append(value)
        if len(writes) > 1:  # 第一次 = 成功路径写 true；第二次 = 回滚恢复 → 磁盘故障
            raise OSError("no space left on device (test)")
        orig_write(plugin_id, value, scope, project_path, env)

    monkeypatch.setattr(installer_mod, "_write_enabled_plugin", _flaky_write)

    # ``a2c_smcp`` 根 logger 为 ``propagate=False``，caplog 的 root handler 收不到 → 直接挂到模块 logger
    # （仓库既有写法：test_plugin_name_uniqueness.py 的 ``capture_registry_errors``）。**级别也要设在同一个
    # logger 名上**：``a2c_smcp`` 的级别在 import 时由 ``A2C_SMCP_LOG_LEVEL`` 固定，只调 root 级别的话，
    # 环境里设了 ``A2C_SMCP_LOG_LEVEL=error`` 就会在 ``isEnabledFor`` 处把 WARNING 过滤掉 → 测试假红（审查 🟡3）。
    installer_mod.logger.addHandler(caplog.handler)
    # 用 ``logger.name`` 而不是手写点号路径：``get_logger`` 会再前缀一层 ``a2c_smcp.``（实测 name 形如
    # ``a2c_smcp.a2c_smcp.computer.…``），手写路径会设在另一个 logger 上、对 ``isEnabledFor`` 无效。
    caplog.set_level(logging.WARNING, logger=installer_mod.logger.name)
    try:
        with pytest.raises(PluginInstallError) as ei:
            await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))
    finally:
        installer_mod.logger.removeHandler(caplog.handler)

    assert writes == [True, None]  # 回滚确实尝试过恢复（prev absent → None）
    assert "failed to restore enabledPlugins" in caplog.text  # best-effort 失败只告警
    assert "no space left on device" in caplog.text  # 原因入日志
    assert "staging registered none" in str(ei.value)  # 原异常未被 OSError 顶替
    assert "was attempted but may not have succeeded" in str(ei.value)
    assert "was restored to its previous value" not in str(ei.value)  # 绝对化措辞已去掉
    # 回滚写失败 ⇒ enabledPlugins 仍是成功路径写下的 true（如实反映「没恢复」）
    assert _read_user_settings(env)["enabledPlugins"][_PID] is True


@pytest.mark.asyncio
async def test_enable_multi_scope_same_root_registers_server_once(tmp_path: Path, monkeypatch) -> None:
    """#259 架构师审查 🟡5：多 scope 同根记录聚合按 bundle_id 去重，不得重复 register。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _seed_installed(
        home,
        {
            _PID: [
                _record(root, scope="user", servers=[FIGMA_BID]),
                _record(root, scope="project", servers=[FIGMA_BID]),
            ]
        },
    )
    _seed_enabled_intent(env)
    monkeypatch.setattr(_STAGE, _fake_stage([], register_skill=False))
    mcp = _FakeMCP()

    await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    assert mcp.registered == [FIGMA_BID]  # 只挂一次


@pytest.mark.asyncio
async def test_enable_bundle_id_identity_collapse_within_record_fails(tmp_path: Path, monkeypatch) -> None:
    """#259 复审 🟡1/🟡4 + #271：同一记录内两文件显式同 bundle_id 但配置不同 → 身份坍缩，硬错。

    #271 两点：``details`` 的键是 ``bundleId``（``conflictWith`` 专属 pid 值域，塌缩场景没有第二方 pid）；
    同根异配置的文案必须点名「同根」+ 可区分字段（server 名），且**不得**回显命令 / env 等配置体。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    # 夹具刻意在**四种**配置体里各埋一个哨兵（command / args / env / headers）：只要消息回显任一配置体，
    # 对应哨兵就会出现在 message 里。仅放 command 不够——夹具为空时「不回显 env」的断言永远绿（审查 🟡1）。
    bodies = {
        "alpha": {
            "name": "alpha",
            "type": "stdio",
            "server_parameters": {
                "command": "node",
                "args": ["--token", "s3cr3t-args"],
                "env": {"API_TOKEN": "s3cr3t-env"},
            },
            "bundle_id": "shared",
        },
        "beta": {
            "name": "beta",
            "type": "streamable",
            "server_parameters": {
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer s3cr3t-hdr"},
            },
            "bundle_id": "shared",
        },
    }
    for name, body in bodies.items():
        _write_json(root / "mcp-servers" / f"{name}.json", body)
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP()

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    message = str(ei.value)
    assert ei.value.error_code == "validation"
    assert ei.value.details == {"bundleId": "shared"}
    assert "conflictWith" not in ei.value.details  # 该键专属 `<plugin>@<marketplace>` pid，不得混入 bundle_id
    assert "collapses distinct identities under the same install root" in message
    assert "different configs under the same install root" in message
    assert "'alpha'" in message and "'beta'" in message  # 可区分字段：server 名（= 文件 stem）
    # 配置体不得回显（可能含凭据）：command / args / env / headers 四个哨兵一个都不许出现
    for sentinel in ("s3cr3t", "s3cr3t-args", "s3cr3t-env", "s3cr3t-hdr", "--token", "Bearer", "API_TOKEN"):
        assert sentinel not in message, f"config body leaked into the message: {sentinel!r}"
    assert "node" not in message and "example.invalid" not in message
    assert calls == [] and mcp.registered == []
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [None, False], ids=["absent", "explicit-false"])
async def test_enable_bundle_id_identity_collapse_across_roots_fails(
    tmp_path: Path, monkeypatch, prior: bool | None
) -> None:
    """#259 复审 🟡1 + #271：异安装根的同 bundle_id（即使配置相同）也是身份坍缩 → 硬错，不静默首胜。

    #271 补齐与同记录兄弟用例对称的断言，并覆盖原始 ``enabledPlugins`` 的两档非 true 取值
    ——prior-true 的回落语义另案 #289，本用例刻意不钉死。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    root_a = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    root_b = tmp_path / "other-root"
    shutil.copytree(root_a / "mcp-servers", root_b / "mcp-servers")
    _seed_installed(
        home,
        {
            _PID: [
                _record(root_a, scope="user", servers=[FIGMA_BID]),
                _record(root_b, scope="project", servers=[FIGMA_BID]),
            ]
        },
    )
    _seed_enabled_intent(env, value=prior)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    reg, mcp = SkillRegistry(), _FakeMCP()

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))

    message = str(ei.value)
    assert ei.value.error_code == "validation"
    assert ei.value.details == {"bundleId": FIGMA_BID}
    assert "conflictWith" not in ei.value.details
    assert "collapses distinct identities" in message
    assert str(root_a) in message and str(root_b) in message
    assert "under the same install root" not in message  # 异根档沿用现文案，与同根档构成对照
    assert calls == [] and mcp.registered == [] and len(reg) == 0
    if prior is None:
        assert _PID not in _read_user_settings(env).get("enabledPlugins", {})
    else:
        assert _read_user_settings(env)["enabledPlugins"][_PID] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("decl", [[123], {"x": 1}], ids=["list-int", "dict"])
async def test_enable_malformed_skills_declaration_counts_as_declared(
    tmp_path: Path, monkeypatch, decl: object
) -> None:
    """#259 复审 🟡2 + #271：畸形 ``skills`` 声明按 JSON 真值算「已声明」→ 零投影失败。

    ``[123]``（list）与 ``{"x": 1}``（dict）同属「声明了但不可投影」——后者此前无任何用例，
    正好覆盖 ``_json_truthy`` 的 dict 分支（与 rust ``json_truthy`` 同口径）。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    _declare_entry_skills(home, "acme", "audit", decl)
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    with pytest.raises(PluginInstallError, match="declares SKILL packages"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))


@pytest.mark.asyncio
async def test_enable_empty_string_skills_declaration_is_not_a_declaration(tmp_path: Path, monkeypatch) -> None:
    """#259 复审 🟡2 反向：``skills: ""`` 按 JSON 真值为假 → 不算声明，空插件仍可启用。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    _declare_entry_skills(home, "acme", "audit", "")
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    monkeypatch.setattr(_STAGE, _fake_stage([], register_skill=False))

    await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert _read_user_settings(env)["enabledPlugins"][_PID] is True


@pytest.mark.asyncio
async def test_enable_load_bundled_servers_permission_error_is_structured(tmp_path: Path, monkeypatch) -> None:
    """#259 复审 🟡5：强制走**内层** ``except OSError``（物化判据通过后读取失败）→ 结构化错误、零副作用。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    from a2c_smcp.computer.settings import installer as installer_mod

    def _boom(_root: Path):
        raise PermissionError("denied by test")

    monkeypatch.setattr(installer_mod, "load_bundled_servers", _boom)  # 只影响 installer 的引用（判据走 reconciler）
    mcp = _FakeMCP()

    with pytest.raises(PluginInstallError, match="cannot be read"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    assert calls == [] and mcp.registered == []
    assert _PID not in _read_user_settings(env).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_enable_already_enabled_plugin_re_enables(tmp_path: Path) -> None:
    """#259 复审 🟡6 正对照：已启用 + 有效 SKILL.md 再 enable 成功（staged 非空，幂等不误伤）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    reg, mcp = SkillRegistry(), _FakeMCP()

    await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))  # 首次
    assert reg.resolve("audit:lint") is not None
    await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))  # 幂等重启用

    assert _read_user_settings(env)["enabledPlugins"][_PID] is True
    assert reg.resolve("audit:lint") is not None


@pytest.mark.asyncio
async def test_enable_restage_revives_orphaned_skill(tmp_path: Path) -> None:
    """#271 正对照（真 staging）：③ 的 re-stage 走 ``register_or_update``，孤儿同名条目必须翻回活跃。

    上一用例只证明「二次 enable 不丢 skill」；若 re-stage 被跳过（如擅自改成短路返回），skill 仍活跃、
    断言照样绿。这里以**孤儿**为基线，翻活只能来自本次 re-stage。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    reg = SkillRegistry()
    assert reg.register(
        {"name": "audit:lint", "source": f"{SOURCE_MARKETPLACE}:acme", "path": str(root / "skills" / "lint")}
    )
    reg.mark_orphan("audit:lint")  # 模拟 disable 后的隐藏态
    assert reg.resolve("audit:lint") is None and reg.is_orphan("audit:lint") is True

    await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(_FakeMCP()))

    assert reg.is_orphan("audit:lint") is False
    assert reg.resolve("audit:lint") is not None
    assert _read_user_settings(env)["enabledPlugins"][_PID] is True


@pytest.mark.asyncio
async def test_enable_strict_conflict_reported_as_structured_error(tmp_path: Path, monkeypatch) -> None:
    """#259 架构师审查 🟡6：strict=false 且 plugin.json 声明组件（staging 会整体拒绝）→ 预检即结构化错误。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    _set_entry_strict(home, "acme", "audit", False)
    _write_plugin_json(root, {"name": "audit", "skills": ["skills"]})
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    with pytest.raises(PluginInstallError, match="manifests conflict"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert calls == []


@pytest.mark.asyncio
async def test_enable_unreadable_bundled_dir_is_structured_error(tmp_path: Path, monkeypatch) -> None:
    """#259 架构师审查 🟡7：``load_bundled_servers`` 的 OSError（权限）不得裸逸出，归一为 PluginInstallError。"""
    if not hasattr(os, "geteuid") or os.geteuid() == 0:  # 仓库既有写法（Windows 无 geteuid；root 绕过权限位）
        pytest.skip("POSIX 权限位（非 root）才有效")
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_enabled_intent(env)
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    blocked = root / "mcp-servers"
    os.chmod(blocked, 0o000)
    try:
        with pytest.raises(PluginInstallError, match="cannot inspect|cannot be read"):
            await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))
    finally:
        os.chmod(blocked, 0o755)


@pytest.mark.asyncio
async def test_enable_corrupt_bundled_json_reports_unparsable(tmp_path: Path, monkeypatch) -> None:
    """#259 审查 🟡6：目录在、bundled JSON 损坏档位的消息可读（并归一为 PluginInstallError，不再裸抛）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit")
    (root / "mcp-servers").mkdir(parents=True, exist_ok=True)
    (root / "mcp-servers" / "bad.json").write_text("{not json", encoding="utf-8")
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_enabled_intent(env)
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    with pytest.raises(PluginInstallError, match="unparsable"):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))
