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
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.installer import (
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
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_NAME])]})
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
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_NAME])]})
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
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_NAME])]})

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

    ok = await uninstall_plugin(
        _PID, SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env, scope="project", keep_servers=True,
    )

    assert ok is True
    remaining = load_installed_plugins(home=home)["plugins"][_PID]
    assert [r["scope"] for r in remaining] == ["user"]  # 仅删 project 记录
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
        {_PID: [_record(root, servers=[FIGMA_NAME]), {"scope": "project", "installPath": str(stale), "projectPath": "/elsewhere"}]},
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
