# -*- coding: utf-8 -*-
# filename: test_enable_rollback_conformance.py
# @Time    : 2026/11/23
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
enable 失败回滚 conformance（#289；协议 runtime-contract §2.4 / conformance-tests §5）/ Enable rollback conformance.

协议依据：protocol#83 / Discussion #82 裁决 **C（条件化）**，protocol PR#84 合入 develop（`a5afb26`）：

- **预检失败（零写）**：被写 scope 的 raw pre-state 保持——absent 不写 false、false 保持、true 保持；
  有效 prior-true 时既有 skills / server / 能力不动，诊断明示「既有启用状态已保留」。
- **写后失败**：raw 精确恢复 pre-state；本次 stage 的 skill 撤销、操作前既有条目保持 pre-state；
  本次 attempt 新挂的 server 按 §4.9.1-2 三档处理（可回收 / 只移除本 plugin 贡献），操作前实例不连坐。
- **场景映射**（conformance-tests.md §5）：预检 ①prior-absent / ①prior-false / ②同 scope prior-true /
  ③跨 scope（user=true + project absent → 合并视图仍 enabled、bundled server verdict 回 ENABLED）；
  写后 ④scope 精确恢复 + attempt-only 撤销 / ④b 跨 scope 写后失败。
- **诊断**：失败 MUST 结构化且不吞原异常；回滚前 ∧ 回滚后合并视图均 enabled → `EnableFailedRetainedError`。

构造方式：公开 ``enable_plugin`` + 真实 settings 文件；register 失败经 ``_FakeMCP`` 注入（生产可达的
callback 边界）；verdict 用真 ``Computer.reconcile_governance``（受信 origin 免信任层、project origin 落
PENDING），配 project-only 正对照证明空 pending 非恒真。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.installer import (
    EnableFailedRetainedError,
    PluginInstallError,
    _attempt_server_modes,
    enable_plugin,
)
from a2c_smcp.computer.settings.recovery import BundledServerRecord, collect_enabled_bundled_servers
from a2c_smcp.computer.settings.schema import SettingsScope
from a2c_smcp.computer.settings.scope import resolve_settings, user_settings_path, workdir_project_settings_path
from a2c_smcp.computer.settings.store import save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.utils.bundle_id import resolve_bundle_id

_SRC = {"type": "git", "url": "https://example.com/acme.git"}
_STAGE = "a2c_smcp.computer.settings.installer.stage_marketplace_skills"
_PID = "audit@acme"

# 夹具身份对（#153 + 协议 conformance §2.0）：display name 含 `.` → bundle_id 折 `_`，两者**必须分叉**。
FIGMA_NAME, FIGMA_BID = "figma.mcp", "figma_mcp"


# ── 辅助（与 test_install_enable_separation.py 同构）/ helpers ───────────────
def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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


def _declare_entry_skills(home: Path, mp: str, plugin: str, skills: object) -> None:
    """给 marketplace 条目写 ``skills`` 声明（#259 声明态判据）。"""
    path = marketplace_skill_dir(home, mp) / ".tfrobot-plugin" / "marketplace.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in manifest["plugins"]:
        if entry.get("name") == plugin:
            entry["skills"] = skills
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _record(plugin_root: Path, *, scope: str = "user", servers: Sequence[str] = ()) -> dict:
    rec: dict = {
        "scope": scope,
        "installPath": str(plugin_root),
        "version": "1.2.0",
        "commitSha": "abc123",
        "installedAt": "2026-07-06T00:00:00Z",
    }
    rec["mcpServers"] = list(servers)
    return rec


def _seed_installed(home: Path, plugins: dict[str, list[dict]]) -> None:
    save_installed_plugins({"version": 1, "plugins": plugins}, home=home)


def _seed_user_intent(env: dict[str, str], *, enabled: bool | None = None) -> None:
    settings: dict = {"installedPlugins": [_PID]}
    if enabled is not None:
        settings["enabledPlugins"] = {_PID: enabled}
    _write_json(user_settings_path(env), settings)


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
    """记录 MCP 注入回调；身份一律 bundle_id（#153）。

    register 失败时会**先把 config 写进 existing 再抛**——模拟生产 ``Computer.aunmount_server_by_id``
    路径前的真实残留：``MCPServerManager._adecide_add_or_update`` 先登记 ``_servers_config`` 再启动，
    启动失败不回滚登记（``_amount_raw`` 只回滚 ``_active_raw``）。回滚必须能清掉它。
    """

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing: set[str] = set(existing or ())  # bundle_id 集
        self.registered: list[str] = []  # bundle_id
        self.removed: list[str] = []  # bundle_id
        self.fail_on: str | None = None  # 该 display name 的 server 注册时抛错
        self.remove_error: Exception | None = None

    def existing_bundle_ids(self) -> set[str]:
        return set(self.existing)

    async def register(self, cfg) -> None:
        bundle_id = resolve_bundle_id(cfg)
        if self.fail_on is not None and cfg.name == self.fail_on:
            self.existing.add(bundle_id)  # 已登记、启动失败 → 残留
            raise RuntimeError(f"register {cfg.name} boom")
        self.registered.append(bundle_id)
        self.existing.add(bundle_id)

    async def remove(self, bundle_id: str) -> None:
        self.removed.append(bundle_id)
        self.existing.discard(bundle_id)
        if self.remove_error is not None:
            raise self.remove_error


def _enable_kwargs(mcp: _FakeMCP, **extra: Any) -> dict[str, Any]:
    return {
        "existing_bundle_ids": mcp.existing_bundle_ids,
        "register_server": mcp.register,
        "remove_server": mcp.remove,
        **extra,
    }


async def _noop_remove(_bundle_id: str) -> None:
    return None


def _active_skill_names(reg: SkillRegistry) -> list[str]:
    """**公开投影**（``client:get_skills`` 的数据源 = ``SkillRegistry.active_refs``；不读孤儿/私有条目）。"""
    return sorted(str(ref["name"]) for ref in reg.active_refs())


def _break_skills_only(root: Path) -> None:
    """删除约定 ``skills/`` 容器但**保留 mcp-servers/**：预检失败、bundled server 仍可被 verdict 收集。"""
    shutil.rmtree(root / "skills")


def _break_skill_staging(root: Path) -> None:
    """保留 SKILL.md 文件、去掉 frontmatter ``description``：预检放行、真实 staging 零注册（写后失败）。"""
    (root / "skills" / "lint" / "SKILL.md").write_text("---\nname: lint\n---\nbody\n", encoding="utf-8")


def _assert_not_retained(err: BaseException) -> None:
    assert not isinstance(err, EnableFailedRetainedError), f"非 retained 场景不得包装: {err!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# ① 预检失败：prior-absent / prior-false
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_preflight_prior_absent_leaves_key_absent(tmp_path: Path, monkeypatch) -> None:
    """§5 ①a：pre-state absent → 失败后该 key **不存在**（不得写 false）、零投影、零回收、settings 文件都不建。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])  # 声明态 → 删容器才会被预检抓到
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _break_skills_only(root)
    mcp = _FakeMCP()
    reg = SkillRegistry()

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))

    _assert_not_retained(ei.value)
    assert not user_settings_path(env).exists(), "预检零写 → 不得创建 user settings 文件"
    assert _active_skill_names(reg) == [], "公开投影（get_skills）不得出现"
    assert ei.value.details["reason"] == "enable-failed", "非 retained 出口也保证机读 reason"
    assert mcp.registered == [] and mcp.removed == [], "预检失败零挂载零回收"


@pytest.mark.asyncio
async def test_preflight_prior_false_keeps_false(tmp_path: Path, monkeypatch) -> None:
    """§5 ①b：pre-state 显式 false → 精确保持 false，零副作用。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=False)
    _break_skills_only(root)
    mcp = _FakeMCP()

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    _assert_not_retained(ei.value)
    assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is False
    assert mcp.registered == [] and mcp.removed == []


# ═══════════════════════════════════════════════════════════════════════════════
# ② 预检失败：同 scope prior-true
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_preflight_prior_true_same_scope_keeps_state(tmp_path: Path, monkeypatch) -> None:
    """§5 ②：同 scope 原为 true 的重复 enable 预检失败 → key 保持 true、投影不变、既有 server 不摘、诊断 retained。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=True)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    reg, mcp = SkillRegistry(), _FakeMCP()

    await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))  # 首次成功：技能活跃、server 已挂
    assert reg.resolve("audit:lint") is not None and FIGMA_BID in mcp.existing
    before = _active_skill_names(reg)  # 公开投影（get_skills 源）
    _break_skills_only(root)

    with pytest.raises(EnableFailedRetainedError) as ei:
        await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))

    err = ei.value
    assert "existing enabled state retained" in str(err)
    assert err.details["enabledStateRetained"] is True
    assert err.details["reason"] == "enable-failed-retained"
    assert isinstance(err.__cause__, PluginInstallError), "原异常不得被吞"
    assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is True
    assert _active_skill_names(reg) == before, "公开投影（get_skills）与操作前一致"
    assert mcp.removed == [], "既有 server 不得被摘"


# ═══════════════════════════════════════════════════════════════════════════════
# ③ 预检失败：跨 scope（user=true / project absent）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_preflight_cross_scope_user_true_project_absent_keeps_merged_view(tmp_path: Path, monkeypatch) -> None:
    """§5 ③（raw 层）：project 预检失败 → project key 仍 absent、user=true 不动、合并视图仍 enabled（origin=user）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=True)
    _break_skills_only(root)
    project_settings = workdir_project_settings_path(proj)

    with pytest.raises(EnableFailedRetainedError):
        await enable_plugin(
            _PID, SkillRegistry(), home, scope="project", project_path=str(proj), env=env, **_enable_kwargs(_FakeMCP()),
        )

    assert not project_settings.exists(), "project 预检失败 MUST 保持 absent（零写，文件都不建）"
    assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is True

    resolved = resolve_settings(env=env, cwd=proj)
    records = collect_enabled_bundled_servers(home, resolved.settings, env=env, per_scope_layers=resolved.per_scope_layers)
    assert [r.enabled_origin for r in records] == [SettingsScope.USER], "合并视图仍是 user 激活（非 project）"

    # 正对照：project=true（更高 scope）才把 origin 抬到 PROJECT —— 证明上一条不是无常量。
    _write_json(project_settings, {"enabledPlugins": {_PID: True}})
    resolved2 = resolve_settings(env=env, cwd=proj)
    records2 = collect_enabled_bundled_servers(home, resolved2.settings, env=env, per_scope_layers=resolved2.per_scope_layers)
    assert [r.enabled_origin for r in records2] == [SettingsScope.PROJECT]


@pytest.mark.asyncio
async def test_preflight_cross_scope_verdict_enabled(tmp_path: Path) -> None:
    """§5 ③（公开投影）：失败后 client:get_skills 与操作前一致；bundled server verdict = ENABLED（不落 PENDING）。"""
    home, env = _home(tmp_path), _env(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=True)

    async with Computer(name="t", skill_home=home, project_root=proj, env=env) as comp:
        before = sorted(str(ref["name"]) for ref in comp.get_skills())
        before_cfg = [cfg.name for cfg in comp.active_server_configs()]  # client:get_config 面
        assert before == ["audit:lint"]
        _break_skills_only(root)
        with pytest.raises(EnableFailedRetainedError):
            await enable_plugin(
                _PID, comp.skill_registry, home, scope="project", project_path=str(proj), env=env, **_enable_kwargs(_FakeMCP()),
            )
        assert sorted(str(ref["name"]) for ref in comp.get_skills()) == before
        assert [cfg.name for cfg in comp.active_server_configs()] == before_cfg, "client:get_config 与操作前一致"

        mounted: list[str] = []

        async def _register(cfg: Any, record: BundledServerRecord) -> None:
            mounted.append(cfg.name)

        report = await comp.reconcile_governance(existing_bundle_ids=lambda: set(), register_server=_register)
        assert report.pending_bundled_servers == [], "origin 回 user → verdict ENABLED（不得落 PENDING）"
        assert mounted == [FIGMA_NAME]


@pytest.mark.asyncio
async def test_project_only_enable_keeps_verdict_pending_positive_control(tmp_path: Path) -> None:
    """正对照：project-only enable（origin=Project）的 bundled server MUST 落 PENDING —— 证明上文空 pending 非恒真。"""
    home, env = _home(tmp_path), _env(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env)  # 只装未启用

    async with Computer(name="t", skill_home=home, project_root=proj, env=env) as comp:
        await enable_plugin(_PID, comp.skill_registry, home, scope="project", project_path=str(proj), env=env)
        mounted: list[str] = []

        async def _register(cfg: Any, record: BundledServerRecord) -> None:
            mounted.append(cfg.name)

        report = await comp.reconcile_governance(existing_bundle_ids=lambda: set(), register_server=_register)
        assert [r.plugin_id for r in report.pending_bundled_servers] == [_PID]
        assert report.pending_bundled_servers[0].enabled_origin is SettingsScope.PROJECT
        assert mounted == []


# ═══════════════════════════════════════════════════════════════════════════════
# ④ 写后失败：prior-absent / prior-false / 同 scope prior-true
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_write_after_prior_absent_restores_snapshot(tmp_path: Path, monkeypatch) -> None:
    """§5 ④：写 true + stage 后 register 失败 → key 删回 absent、本次 stage 撤销、attempt（含失败者）清理。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha", "beta"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha", "beta"])]})
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP()
    mcp.fail_on = "beta"
    reg = SkillRegistry()

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(mcp))

    _assert_not_retained(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError), "原异常不被吞"
    assert "beta" in str(ei.value)
    assert ei.value.details["reason"] == "enable-failed", "非 retained 归一的机读 reason（CHANGELOG 契约）"
    settings_path = user_settings_path(env)
    assert settings_path.exists(), "写后失败用例必须真正经过步骤②（写 true 已创建文件）"
    assert _PID not in _read_settings(settings_path).get("enabledPlugins", {})
    assert _active_skill_names(reg) == [], "公开投影（get_skills）撤销本次 stage 的 skill"
    assert collect_enabled_bundled_servers(home, {"installedPlugins": [_PID]}) == [], "config 投影不出现"
    assert mcp.removed == ["alpha", "beta"], "attempt 集合（含失败者 beta）逐个清理"


@pytest.mark.asyncio
async def test_write_after_prior_false_keeps_false(tmp_path: Path, monkeypatch) -> None:
    """§5 ④：pre-state 显式 false → 写后失败精确恢复 false（不得删键/改 true）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=False)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP()
    mcp.fail_on = FIGMA_NAME

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    _assert_not_retained(ei.value)
    assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is False
    assert mcp.removed == [FIGMA_BID]


@pytest.mark.asyncio
async def test_write_after_prior_true_same_scope_keeps_state(tmp_path: Path, monkeypatch) -> None:
    """§5 ④：同 scope prior-true 写后失败（真实 staging 零注册）→ key 保持 true、skills 不变、诊断 retained。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=True)
    _break_skill_staging(root)  # 预检放行（SKILL.md 在）、真实 staging 零注册
    reg = SkillRegistry()

    with pytest.raises(EnableFailedRetainedError) as ei:
        await enable_plugin(_PID, reg, home, env=env, **_enable_kwargs(_FakeMCP()))

    assert "staging registered none" in str(ei.value), "必须命中写后 staging 失败（而非预检）"
    assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is True
    assert reg.resolve("audit:lint") is None


@pytest.mark.asyncio
async def test_write_after_failure_keeps_preexisting_mounted_servers(tmp_path: Path, monkeypatch) -> None:
    """回滚基线 = 操作前快照：操作前已挂载的 server 不摘；只清本次 attempt（含失败者）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha", "beta"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha", "beta"])]})
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP(existing={"alpha"})  # alpha 操作前已挂载（用户 / 他 plugin 带入）
    mcp.fail_on = "beta"

    with pytest.raises(PluginInstallError):
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    assert mcp.removed == ["beta"], "alpha 操作前已存在 → 不得连坐；失败者 beta 纳入清理"
    assert "alpha" in mcp.existing


# ═══════════════════════════════════════════════════════════════════════════════
# ④b 写后失败：跨 scope（user=true / project absent）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_write_after_cross_scope_verdict_enabled(tmp_path: Path) -> None:
    """§5 ④b：project 写入 true 后 apply 失败 → project key 删除、合并视图 true、verdict 回 ENABLED。"""
    home, env = _home(tmp_path), _env(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=True)
    project_settings = workdir_project_settings_path(proj)

    async with Computer(name="t", skill_home=home, project_root=proj, env=env) as comp:
        before = sorted(str(ref["name"]) for ref in comp.get_skills())
        before_cfg = [cfg.name for cfg in comp.active_server_configs()]  # client:get_config 面
        _break_skill_staging(root)

        with pytest.raises(EnableFailedRetainedError) as ei:
            await enable_plugin(
                _PID, comp.skill_registry, home, scope="project", project_path=str(proj), env=env, **_enable_kwargs(_FakeMCP()),
            )

        assert "staging registered none" in str(ei.value)
        assert project_settings.exists(), "写后失败用例必须真正经过步骤②（project 文件已创建）"
        assert _PID not in _read_settings(project_settings).get("enabledPlugins", {}), "project key MUST 删除"
        assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is True
        assert sorted(str(ref["name"]) for ref in comp.get_skills()) == before
        assert [cfg.name for cfg in comp.active_server_configs()] == before_cfg, "client:get_config 与操作前一致"

        mounted: list[str] = []

        async def _register(cfg: Any, record: BundledServerRecord) -> None:
            mounted.append(cfg.name)

        report = await comp.reconcile_governance(existing_bundle_ids=lambda: set(), register_server=_register)
        assert report.pending_bundled_servers == [], "origin 回 user → verdict ENABLED（非 PENDING）"
        assert mounted == [FIGMA_NAME]


# ═══════════════════════════════════════════════════════════════════════════════
# 诊断锚点：必须用**本次操作**的 project/local 路径，不得固定读 runtime cwd
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_retained_diagnostic_anchors_at_operation_path(tmp_path: Path, monkeypatch) -> None:
    """runtime cwd=A（project=false）与操作 path=B（project absent）分列：诊断 MUST 锚 B（user=true）→ retained。

    若误锚 A，会得到「合并视图 disabled」而漏报 retained（rust 复审 🔴 同款）。
    """
    home, env = _home(tmp_path), _env(tmp_path)
    config_a, proj_b = tmp_path / "config-a", tmp_path / "proj-b"
    config_a.mkdir()
    proj_b.mkdir()
    monkeypatch.chdir(config_a)  # runtime cwd = A
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=True)
    # A 的 project=false：若诊断错误地锚 A → 合并视图 disabled；B 原 absent。
    _write_json(workdir_project_settings_path(config_a), {"enabledPlugins": {_PID: False}})
    _break_skills_only(root)

    with pytest.raises(EnableFailedRetainedError):
        await enable_plugin(
            _PID, SkillRegistry(), home, scope="project", project_path=str(proj_b), env=env, **_enable_kwargs(_FakeMCP()),
        )

    assert _read_settings(workdir_project_settings_path(config_a))["enabledPlugins"][_PID] is False, "A 不得被触碰"
    assert not workdir_project_settings_path(proj_b).exists(), "B 原 absent，失败后仍 absent"
    assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 合并视图：不得由被写 scope 的 raw 值单独推断（§2.4 回滚第 2 层）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    ("user_value", "project_value", "expect_retained"),
    [
        (False, True, True),   # user=false + project=true → 合并视图 enabled
        (True, False, False),  # user=true + project=false → 合并视图 disabled
    ],
    ids=["user-false-project-true", "user-true-project-false"],
)
@pytest.mark.asyncio
async def test_retained_uses_merged_view_not_raw_scope_value(
    tmp_path: Path, monkeypatch, user_value: bool, project_value: bool, expect_retained: bool,
) -> None:
    """retained 判据 = `user < project < local` 合并视图（写前 ∧ 回滚后），不是被写 scope 的 raw 值。"""
    home, env = _home(tmp_path), _env(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)  # user scope 的诊断锚 = runtime cwd
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _write_json(user_settings_path(env), {"installedPlugins": [_PID], "enabledPlugins": {_PID: user_value}})
    _write_json(workdir_project_settings_path(proj), {"enabledPlugins": {_PID: project_value}})
    _break_skills_only(root)

    err: PluginInstallError | None = None
    try:
        await enable_plugin(_PID, SkillRegistry(), home, scope="user", env=env, **_enable_kwargs(_FakeMCP()))
    except PluginInstallError as exc:  # noqa: PERF203 - 断言集中在下方
        err = exc

    assert err is not None
    assert isinstance(err, EnableFailedRetainedError) is expect_retained
    assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is user_value
    assert _read_settings(workdir_project_settings_path(proj))["enabledPlugins"][_PID] is project_value


# ═══════════════════════════════════════════════════════════════════════════════
# 诊断透传：#296 的 error_code / reason 不得被 retained 包装遮住
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_retained_preserves_source_error_code_and_reason(tmp_path: Path, monkeypatch) -> None:
    """prior-true + 清单改名拒绝（MarketplaceNameChangeError）→ retained 包装保留 validation / manifest-name-changed。"""
    from a2c_smcp.computer.skills.marketplace_identity import MarketplaceNameChangeError

    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_user_intent(env, enabled=True)

    async def _renamed(*_args: object, **_kwargs: object):
        raise MarketplaceNameChangeError(
            "manifest renamed upstream",
            details={"name": "acme", "manifestName": "acme-renamed", "baselineManifestName": "acme"},
        )

    monkeypatch.setattr(_STAGE, _renamed)

    with pytest.raises(EnableFailedRetainedError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    err = ei.value
    assert err.error_code == "validation"
    assert err.details["reason"] == "manifest-name-changed"  # #296 的源 reason 优先，不被兜底值覆盖
    assert err.details["manifestName"] == "acme-renamed"
    assert err.details["enabledStateRetained"] is True
    assert isinstance(err.__cause__, MarketplaceNameChangeError)
    assert _read_settings(user_settings_path(env))["enabledPlugins"][_PID] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 回滚自愈：回滚步骤自身失败不得顶替原异常 / 不得阻断 raw 恢复
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_rollback_survives_classification_callback_failure(tmp_path: Path, monkeypatch) -> None:
    """`non_plugin_bundle_ids` 求值抛错 → 降级 unknown_owner，不阻断清理与 raw 恢复，原异常仍上抛。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha"])]})
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP()
    mcp.fail_on = "alpha"

    def _boom() -> set[str]:
        raise RuntimeError("classification boom")

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(
            _PID, SkillRegistry(), home, env=env, non_plugin_bundle_ids=_boom, **_enable_kwargs(mcp),
        )

    assert "register alpha boom" in str(ei.value)
    assert mcp.removed == ["alpha"], "分类失败不得阻断 attempt 清理"
    assert _PID not in _read_settings(user_settings_path(env)).get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_rollback_survives_remove_server_failure(tmp_path: Path, monkeypatch) -> None:
    """`remove_server` 抛错 → 只 WARN；raw 仍精确恢复，原异常仍上抛（不被回滚顶替）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha"])]})
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP()
    mcp.fail_on = "alpha"
    mcp.remove_error = OSError("unmount boom")

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(mcp))

    assert "register alpha boom" in str(ei.value)
    assert mcp.removed == ["alpha"]
    assert _PID not in _read_settings(user_settings_path(env)).get("enabledPlugins", {})


# ═══════════════════════════════════════════════════════════════════════════════
# 安全 / 边界（#289 审查 🔴 / 🟡4 / 🟡5 / 🟡6）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_enable_failure_diagnostic_redacts_pydantic_input_value(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """register 回调抛 pydantic ``ValidationError``（校验的是**已渲染**配置，可能含解析后 secret）→
    结构化诊断只报字段/条数，secret 不得进 message / details / WARN 日志（协议 §6）。"""
    from pydantic import TypeAdapter, ValidationError

    from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
    from a2c_smcp.computer.settings import installer as installer_mod

    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha"])]})
    _seed_user_intent(env, enabled=True)  # prior-true → 走 retained WARN 路径
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    installer_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=installer_mod.logger.name)
    try:
        async def _leaky_register(cfg: Any) -> None:
            # 模拟 materialize 校验已渲染配置：url 里是解析后的 secret。
            TypeAdapter(MCPServerConfig).validate_python(
                {"name": "leaky", "type": "streamable-http", "server_parameters": {"url": "SECRET_TOKEN_abc"}},
            )

        with pytest.raises(EnableFailedRetainedError) as ei:
            await enable_plugin(
                _PID, SkillRegistry(), home, env=env,
                existing_bundle_ids=lambda: set(), register_server=_leaky_register, remove_server=_noop_remove,
            )
    finally:
        installer_mod.logger.removeHandler(caplog.handler)

    assert isinstance(ei.value.__cause__, ValidationError)
    assert "SECRET_TOKEN_abc" not in str(ei.value)
    assert "SECRET_TOKEN_abc" not in caplog.text
    assert "validation error(s)" in str(ei.value), "须报字段与条数（可诊断），而非回显 input_value"
    assert ei.value.details["enabledStateRetained"] is True


@pytest.mark.asyncio
async def test_unprintable_exception_does_not_mask_original(tmp_path: Path, monkeypatch) -> None:
    """异常 ``__str__`` 自身抛错 → 诊断降级为 ``<unprintable …>``，**原异常仍上抛**（错误转换不得可抛）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha"])]})
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    class _BoomError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str boom")

    async def _bad_register(_cfg: Any) -> None:
        raise _BoomError()

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(
            _PID, SkillRegistry(), home, env=env,
            existing_bundle_ids=lambda: set(), register_server=_bad_register, remove_server=_noop_remove,
        )

    assert "<unprintable _BoomError>" in str(ei.value)
    assert isinstance(ei.value.__cause__, _BoomError)


@pytest.mark.asyncio
async def test_default_provider_honours_ambient_policy(tmp_path: Path, monkeypatch) -> None:
    """policy 强停（``enabledPlugins: false``）压过 user=true → 有效 disabled，**不得误报 retained**（#289 终审 🟡7）。"""
    from a2c_smcp.computer.settings import policy as policy_mod

    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_: {"enabledPlugins": {_PID: False}})
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=True)
    _break_skills_only(root)

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, **_enable_kwargs(_FakeMCP()))

    assert not isinstance(ei.value, EnableFailedRetainedError), "policy 强停 ⇒ 有效态 disabled，报 retained 即反向误报"


@pytest.mark.asyncio
async def test_reconcile_remount_failure_log_redacts_validation_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """治理重挂失败的 WARNING 不得回显 pydantic ``input_value``（#289 终审 🔴2 的下游落点）。"""
    from pydantic import TypeAdapter

    home, env = _home(tmp_path), _env(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _seed_user_intent(env, enabled=True)

    async with Computer(name="t", skill_home=home, project_root=proj, env=env) as comp:
        async def _leaky_register(cfg: Any, record: BundledServerRecord) -> None:
            TypeAdapter(MCPServerConfig).validate_python(
                {"name": "leaky", "type": "streamable-http", "server_parameters": {"url": "SECRET-REMOTE"}},
            )

        package_logger = logging.getLogger("a2c_smcp")
        package_logger.addHandler(caplog.handler)
        caplog.set_level(logging.DEBUG, logger="a2c_smcp")
        try:
            report = await comp.reconcile_governance(existing_bundle_ids=lambda: set(), register_server=_leaky_register)
        finally:
            package_logger.removeHandler(caplog.handler)

        assert report.remounted_servers == []

    assert "failed to remount bundled server" in caplog.text
    assert "SECRET-REMOTE" not in caplog.text, "重挂失败日志不得回显 ValidationError input_value"


@pytest.mark.parametrize("prior", [None, True], ids=["prior-absent", "prior-true"])
@pytest.mark.asyncio
async def test_write_true_failure_is_structured(tmp_path: Path, monkeypatch, prior: bool | None) -> None:
    """写 ``enabledPlugins=true`` 自身失败（磁盘故障，原子零净写）→ 结构化错误、原异常在 `__cause__`；
    prior-true 时包装 retained（写前视图已 enabled）。"""
    from a2c_smcp.computer.settings import installer as installer_mod

    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_user_intent(env, enabled=prior)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk boom")

    monkeypatch.setattr(installer_mod, "_write_enabled_plugin", _boom)

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env)

    assert isinstance(ei.value.__cause__, OSError)
    assert "disk boom" in str(ei.value)
    assert isinstance(ei.value, EnableFailedRetainedError) is (prior is True)


@pytest.mark.asyncio
async def test_diagnostic_provider_failure_degrades_without_masking(tmp_path: Path, monkeypatch) -> None:
    """provider 抛错 → 诊断降级为「未 retained」，**原 enable 异常仍上抛**（不得二次抛 / 被顶替）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _declare_entry_skills(home, "acme", "audit", ["skills"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    _break_skills_only(root)

    def _boom(_anchor: str | None) -> dict[str, Any]:
        raise RuntimeError("provider boom")

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(_PID, SkillRegistry(), home, env=env, resolved_settings=_boom, **_enable_kwargs(_FakeMCP()))

    assert "installed content is missing or unusable" in str(ei.value)  # 原预检异常
    assert not isinstance(ei.value, EnableFailedRetainedError)


@pytest.mark.asyncio
async def test_existing_bundle_ids_callback_failure_is_structured(tmp_path: Path, monkeypatch) -> None:
    """宿主 ``existing_bundle_ids`` 回调抛错也属 enable 阶段异常 → 结构化 + `__cause__`（#289 审查 🟡5）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=[FIGMA_BID])]})
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    def _boom() -> set[str]:
        raise RuntimeError("existing boom")

    async def _register(_cfg: Any) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("register must not be reached")

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(
            _PID, SkillRegistry(), home, env=env,
            existing_bundle_ids=_boom, register_server=_register, remove_server=_noop_remove,
        )

    assert "existing boom" in str(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert not user_settings_path(env).exists(), "回调失败发生在写之前 → 零写"


@pytest.mark.asyncio
async def test_rollback_restores_skill_ref_payload(tmp_path: Path, monkeypatch) -> None:
    """本次 stage 经 register_or_update 改写既有条目内容后失败 → 回滚按快照**回填 ref**（path/description）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    reg = SkillRegistry()
    source = f"{SOURCE_MARKETPLACE}:acme"
    reg.register({"name": "audit:lint", "source": source, "path": "/old/lint", "description": "old"})

    async def _stage_updates_then_fails(name, stage_source, registry, stage_home, **_kwargs):
        registry.register_or_update({"name": "audit:lint", "source": source, "path": "/new/lint", "description": "new"})
        raise RuntimeError("stage boom")

    monkeypatch.setattr(_STAGE, _stage_updates_then_fails)

    with pytest.raises(PluginInstallError):
        await enable_plugin(_PID, reg, home, env=env)

    restored = reg.resolve("audit:lint")
    assert restored is not None
    assert restored["path"] == "/old/lint" and restored["description"] == "old", "回滚须复原操作前 ref 内容"


# ═══════════════════════════════════════════════════════════════════════════════
# §4.9.1-2 三档分类（纯函数）
# ═══════════════════════════════════════════════════════════════════════════════
def test_attempt_server_modes_classification() -> None:
    """attempt 集合按 §4.9.1-2 分三档：可回收 / 他 plugin 依赖 / 用户声明；无判据时 unknown_owner。"""
    ledger_plugins = {
        _PID: [_record(Path("/x"), servers=["own", "shared"])],
        "other@acme": [_record(Path("/y"), servers=["shared"])],
    }

    modes = _attempt_server_modes(
        ["own", "shared", "user_own"],
        ledger_plugins=ledger_plugins,
        plugin_id=_PID,
        user_declared={"user_own"},
    )
    assert modes == {
        "own": "reclaim",
        "shared": "remove_plugin_contribution",
        "user_own": "remove_plugin_contribution",
    }

    unknown = _attempt_server_modes(
        ["own", "shared"], ledger_plugins=ledger_plugins, plugin_id=_PID, user_declared=None,
    )
    assert unknown == {"own": "remove_plugin_contribution_unknown_owner", "shared": "remove_plugin_contribution_unknown_owner"}


# ═══════════════════════════════════════════════════════════════════════════════
# server 残留：register 失败后 manager 已登记 config，必须能经运行期 unmount 清掉
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_amount_raw_failure_residual_cleared_by_unmount(tmp_path: Path) -> None:
    """`_amount_raw` 启动失败只回滚 `_active_raw`；manager 登记残留须由 `aunmount_server_by_id` 清掉。"""

    class _StubManager:
        def __init__(self) -> None:
            self.configs: dict[str, MCPServerConfig] = {}

        async def aadd_or_aupdate_server(self, cfg: MCPServerConfig, *, start: bool = True) -> None:
            self.configs[resolve_bundle_id(cfg)] = cfg
            raise RuntimeError("start boom")

        async def aremove_server(self, bundle_id: str) -> None:
            self.configs.pop(bundle_id, None)

        def server_configs(self) -> list[MCPServerConfig]:
            return list(self.configs.values())

    comp = Computer(name="t", skill_home=tmp_path / "home")
    comp.mcp_manager = _StubManager()  # type: ignore[assignment] - 只验挂载/停摘接缝，绕过 boot
    stub: _StubManager = comp.mcp_manager  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="start boom"):
        await comp.amount_server(_stdio("residual.mcp"))

    assert len(stub.server_configs()) == 1, "登记残留是既存事实（manager 先登记后启动）"
    bundle_id = next(iter(stub.configs))
    await comp.aunmount_server_by_id(bundle_id)
    assert stub.server_configs() == [], "运行期 unmount 必须能清掉失败登记"


# ═══════════════════════════════════════════════════════════════════════════════
# 接线级：真 Computer + build_mcp_callbacks → durable 声明逐字节不变（#289 终审 🟡6）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_enable_write_after_failure_keeps_durable_declarations(tmp_path: Path, monkeypatch) -> None:
    """写后失败只做**运行期** unmount：真 ``Computer`` + ``build_mcp_callbacks`` 下，用户 ``mcp.json`` 与账本
    逐字节不变（若 ``remove_server`` 被接到 durable 删除，本用例转红）。"""
    from a2c_smcp.computer.cli.commands import build_mcp_callbacks
    from a2c_smcp.computer.mcp_clients.model import MCPServerConfig as _Cfg
    from a2c_smcp.computer.settings.mcp_config import user_mcp_config_path

    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["alpha"], skills=["lint"])
    _seed_installed(home, {_PID: [_record(root, servers=["alpha"])]})
    _seed_user_intent(env)  # prior absent → 失败后删键
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    # 用户自己的 durable 声明（user mcp.json）：enable 回滚 MUST NOT 触碰它
    durable = user_mcp_config_path(env)
    _write_json(durable, {"servers": {"alpha": _stdio("alpha")}})
    durable_bytes = durable.read_bytes()
    ledger = home / "installed_plugins.json"
    ledger_bytes = ledger.read_bytes()

    class _StubManager:
        """只验挂载/停摘接缝：登记 config 后启动失败（生产残留形态），不 spawn 真进程。"""

        def __init__(self) -> None:
            self.configs: dict[str, _Cfg] = {}
            self.removed: list[str] = []

        async def aadd_or_aupdate_server(self, cfg: _Cfg, *, start: bool = True) -> None:
            self.configs[resolve_bundle_id(cfg)] = cfg
            raise RuntimeError("start boom")

        async def aremove_server(self, bundle_id: str) -> None:
            self.removed.append(bundle_id)
            self.configs.pop(bundle_id, None)

        def server_configs(self) -> list[_Cfg]:
            return list(self.configs.values())

        async def aclose(self) -> None:  # Computer.__aexit__ → shutdown → aclose
            return None

    async with Computer(name="t", skill_home=home, project_root=proj, env=env) as comp:
        stub = _StubManager()
        comp.mcp_manager = stub  # type: ignore[assignment] - 只验接缝，不拉真进程
        cbs = build_mcp_callbacks(comp)

        with pytest.raises(PluginInstallError):
            await enable_plugin(
                _PID, comp.skill_registry, home, env=env,
                existing_bundle_ids=cbs.existing_bundle_ids,
                register_server=cbs.register_server,
                remove_server=cbs.remove_server,
                non_plugin_bundle_ids=cbs.non_plugin_bundle_ids,
            )

        assert stub.removed == ["alpha"], "回滚对本次 attempt 调运行期 unmount"
        assert stub.configs == {}, "失败登记残留被清掉"

    assert durable.read_bytes() == durable_bytes, "用户 durable mcp.json 必须逐字节不变"
    assert ledger.read_bytes() == ledger_bytes, "账本必须逐字节不变"


# ═══════════════════════════════════════════════════════════════════════════════
# 写 true 已落盘后失败 → 必须 best-effort 恢复 pre-state（#289 终审 🟡5）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("prior", [None, False], ids=["prior-absent", "prior-false"])
@pytest.mark.asyncio
async def test_write_true_failure_after_replace_restores_prestate(tmp_path: Path, monkeypatch, prior: bool | None) -> None:
    """模拟 ``os.replace`` 成功后收尾抛错：写已落盘 → 失败分支必须恢复 pre-state（absent 删键 / false 写回）。"""
    from a2c_smcp.computer.settings import installer as installer_mod

    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {_PID: [_record(root)]})
    _seed_user_intent(env, enabled=prior)
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))

    orig_write = installer_mod._write_enabled_plugin

    def _write_then_fail(plugin_id: str, value: bool | None, scope: str, project_path: str | None, env2) -> None:
        orig_write(plugin_id, value, scope, project_path, env2)
        if value is True:  # 写已落盘，随后抛错（os.close 失败形态）
            raise OSError("close failed after replace")

    monkeypatch.setattr(installer_mod, "_write_enabled_plugin", _write_then_fail)

    with pytest.raises(PluginInstallError):
        await enable_plugin(_PID, SkillRegistry(), home, env=env)

    enabled = _read_settings(user_settings_path(env)).get("enabledPlugins", {})
    if prior is None:
        assert _PID not in enabled, "pre-state absent → 必须删键（不得留下本次写入的 true）"
    else:
        assert enabled[_PID] is False


@pytest.mark.asyncio
async def test_rollback_skills_restores_remaining_entries_after_one_failure(tmp_path: Path) -> None:
    """逐 name 自守卫：一条 SKILL 恢复失败不得放弃其余条目（#289 终审 🟡6）。"""
    from a2c_smcp.computer.settings.installer import _rollback_enable_skills

    reg = SkillRegistry()
    source = f"{SOURCE_MARKETPLACE}:acme"
    reg.register({"name": "audit:a", "source": source, "path": "/a", "description": "new-a"})
    reg.register({"name": "audit:b", "source": source, "path": "/b", "description": "new-b"})
    skills_before = {
        "audit:a": ({"name": "audit:a", "source": source, "path": "/a", "description": "old-a"}, False),
        "audit:b": ({"name": "audit:b", "source": source, "path": "/b", "description": "old-b"}, False),
    }
    orig = reg.register_or_update

    def _fail_one(ref) -> bool:
        if ref.get("name") == "audit:a":
            raise RuntimeError("restore boom")
        return orig(ref)

    reg.register_or_update = _fail_one  # type: ignore[method-assign]

    _rollback_enable_skills(reg, "acme", "audit", skills_before)

    restored_b = reg.resolve("audit:b")
    assert restored_b is not None and restored_b["description"] == "old-b", "其余条目必须继续恢复"
