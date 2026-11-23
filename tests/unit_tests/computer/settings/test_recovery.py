# -*- coding: utf-8 -*-
# filename: test_recovery.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
治理启动恢复单元测试（#117；#123 起对齐协议 v0.3.0 §4.8）/ Governance boot-recovery unit tests。

镜像 rust-sdk recovery.rs 的 hermetic 套件（无 git、无网络：预置 catalog clone 树 + ``refresh=False``
就地复用）。测试意图 / Test intentions（v0.3.0：安装集 = declared ``installedPlugins``；活跃 =
installed ∧ ``enabledPlugins[pid] is True``，缺省翻转）:
- ``recover_marketplace_skills``：installed ∧ enabled plugin 的 bundled SKILL 恢复 + 幂等；
  absent/``false`` 惰性不复活（缺省翻转 + disable 负向）；意图无条目不恢复（uninstall 负向，账本仅派生缓存）；
  known_marketplaces 缺记录 / clone 缺失且源不可达 → ``failed_marketplaces`` 降级不抛；空 home noop。
  重物化（账本删除重建）见 test_install_enable_separation.py。
- ``collect_enabled_bundled_servers``：含归属（plugin/marketplace）纯函数输出（§4.8.3）；
  跨 plugin 同名 server 首见去重；installPath 缺失 / JSON 损坏 → WARN 跳过不阻断。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.recovery import (
    GovernanceRecoveryReport,
    MarketplaceDeclarationError,
    MarketplaceSourceConflict,
    _plugin_enable_origin,
    attach_marketplace_declaration_errors,
    collect_enabled_bundled_servers,
    declaration_marketplace,
    detect_marketplace_source_conflicts,
    marketplace_declaration_errors,
    recover_marketplace_skills,
    sanitize_marketplace_name,
    sanitize_marketplace_text,
)
from a2c_smcp.computer.settings.schema import SCOPE_ORDER, SettingsScope, SettingsValidationError
from a2c_smcp.computer.settings.store import (
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
)
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.utils.bundle_id import resolve_bundle_id

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
        "mcpServers": list(servers),
    }


# ── recover_marketplace_skills ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_recover_restages_enabled_installed_plugin_and_idempotent(tmp_path: Path) -> None:
    """installed ∧ enabled=true plugin → bundled SKILL 恢复进 Registry；二次调用幂等。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {"audit@acme": [_record(root)]})
    reg = SkillRegistry()
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert "audit@acme" in report.restored_plugins
    assert "audit:lint" in report.restored_skills
    assert reg.resolve("audit:lint") is not None
    assert report.failed_marketplaces == [] and report.skipped_disabled == []

    # 幂等：registry 不重复注册
    report2 = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))
    assert "audit:lint" in report2.restored_skills
    assert len(reg) == 1


@pytest.mark.asyncio
async def test_recover_skips_disabled_plugin_and_collect_skips(tmp_path: Path) -> None:
    """enabledPlugins=false → skipped_disabled、SKILL 不复活、collect 同步跳过（disable 负向）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["figma"], skills=["lint"])
    _seed_installed(home, {"audit@acme": [_record(root, servers=["figma"])]})
    reg = SkillRegistry()
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": False}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.skipped_disabled == ["audit@acme"]
    assert report.restored_skills == [] and report.restored_plugins == []
    assert reg.resolve("audit:lint") is None
    assert collect_enabled_bundled_servers(home, declared, env=_env(tmp_path)) == []


@pytest.mark.asyncio
async def test_recover_without_intent_is_noop(tmp_path: Path) -> None:
    """安装意图无条目（uninstall 后）→ 即使 catalog 树 / 账本仍在也不恢复（意图是唯一权威，账本仅派生缓存）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])  # 树在
    _seed_installed(home, {"audit@acme": [_record(root)]})  # 账本残留（未 gc）
    reg = SkillRegistry()

    report = await recover_marketplace_skills(reg, home, {"enabledPlugins": {"audit@acme": True}}, env=_env(tmp_path))

    assert report.restored_plugins == [] and report.restored_skills == []
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_recover_degrades_when_marketplace_record_absent(tmp_path: Path) -> None:
    """known_marketplaces 缺该 marketplace 记录 → failed_marketplaces 降级、不抛、不阻断。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    _seed_installed(home, {"audit@acme": [_record(root)]})
    reg = SkillRegistry()
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

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
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.failed_marketplaces == ["acme"]
    assert report.restored_skills == []


@pytest.mark.asyncio
async def test_local_only_pid_without_at_is_skipped(tmp_path: Path) -> None:
    """账本 pid 无 ``@marketplace`` 段（本地-only 形态）→ 恢复与 collect 均静默跳过、不炸、不误纳入。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["figma"], skills=["lint"])
    _seed_installed(home, {"localplugin": [_record(root, servers=["figma"])]})
    reg = SkillRegistry()

    report = await recover_marketplace_skills(reg, home, {}, env=_env(tmp_path))

    assert report.restored_plugins == [] and report.restored_skills == []
    assert report.skipped_disabled == [] and report.failed_marketplaces == []
    assert len(reg) == 0
    assert collect_enabled_bundled_servers(home, {}, env=_env(tmp_path)) == []


@pytest.mark.asyncio
async def test_recover_revives_orphaned_skill(tmp_path: Path) -> None:
    """已 orphan 的 bundled SKILL（同会话 disable 内存态）经 recover 重扫复活回活跃集（register_or_update 语义）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {"audit@acme": [_record(root)]})
    reg = SkillRegistry()
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}
    await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))
    assert reg.mark_orphan("audit:lint") is True
    assert reg.resolve("audit:lint") is None  # 孤儿不可解析

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert "audit:lint" in report.restored_skills
    assert reg.resolve("audit:lint") is not None  # 复活


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
def _declared_enabled(*pids: str) -> dict:
    """installed ∧ enabled=true 的 declared 视图速造（v0.3.0 门控两键）。"""
    return {"installedPlugins": list(pids), "enabledPlugins": {pid: True for pid in pids}}


def test_collect_returns_enabled_bundled_servers_with_ownership(tmp_path: Path) -> None:
    """installed ∧ enabled plugin 的 bundled server 可查询，且归属（plugin/marketplace/installPath）为纯函数输出（§4.8.3）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["figma", "blender"])
    _seed_installed(home, {"audit@acme": [_record(root, servers=["figma", "blender"])]})

    records = collect_enabled_bundled_servers(home, _declared_enabled("audit@acme"), env=_env(tmp_path))

    assert {r.config.name for r in records} == {"figma", "blender"}
    for r in records:
        assert r.plugin_id == "audit@acme"
        assert r.plugin == "audit" and r.marketplace == "acme"
        assert r.install_path == root


def test_collect_dedupes_same_server_name_across_plugins(tmp_path: Path) -> None:
    """跨 plugin 同名 server → 首见保留去重且**归属为首见者**（账本插入序，与 rust "first seen wins" 一致）。"""
    home = _home(tmp_path)
    root_a = _setup_catalog(home, "acme", "audit", servers=["shared"])
    root_b = _setup_catalog(home, "beta", "fmt", servers=["shared"])
    _seed_installed(
        home,
        {"audit@acme": [_record(root_a, servers=["shared"])], "fmt@beta": [_record(root_b, servers=["shared"])]},
    )

    records = collect_enabled_bundled_servers(home, _declared_enabled("audit@acme", "fmt@beta"), env=_env(tmp_path))

    assert len(records) == 1
    assert records[0].config.name == "shared"
    assert records[0].plugin_id == "audit@acme"  # 首见胜：归属锁定账本序首个
    assert records[0].install_path == root_a


def test_collect_keeps_same_name_distinct_bundle_id(tmp_path: Path) -> None:
    """跨 plugin **同 display 名 + 显式异 bundle_id** → 两条都保留（去重键 = bundle_id，非 name）。

    English: same display name but explicitly distinct bundle_id ⇒ both kept (dedup key is bundle_id, not name).

    #150 R5②/site4：display 名允许碰撞、``bundle_id`` 才是身份（协议 no-double-open 同键）。现码按
    ``config.name`` 去重会把两个**不同身份**的 server 误并成一条（server-name-as-identity Bug，本用例即其红灯守卫）；
    去重键改 ``resolve_bundle_id(config)`` 后二者共存。
    """
    home = _home(tmp_path)
    root_a = _setup_catalog(home, "acme", "audit", servers=[])
    root_b = _setup_catalog(home, "beta", "fmt", servers=[])
    # 同 display 名 "shared"，两 plugin 根不同 → 文件名不碰撞；显式异 bundle_id → 两个不同身份的 server。
    _write_json(root_a / "mcp-servers" / "shared.json", {**_stdio("shared"), "bundle_id": "shared-a"})
    _write_json(root_b / "mcp-servers" / "shared.json", {**_stdio("shared"), "bundle_id": "shared-b"})
    _seed_installed(
        home,
        {"audit@acme": [_record(root_a, servers=["shared"])], "fmt@beta": [_record(root_b, servers=["shared"])]},
    )

    records = collect_enabled_bundled_servers(home, _declared_enabled("audit@acme", "fmt@beta"), env=_env(tmp_path))

    # 现码红（按 name 并成 1）→ 去重键改 bundle_id 后绿（两条都保留）。
    assert len(records) == 2
    assert {resolve_bundle_id(r.config) for r in records} == {"shared-a", "shared-b"}
    assert all(r.config.name == "shared" for r in records)  # display 名碰撞合法、非身份


def test_collect_enumerates_multi_scope_records_with_dedup(tmp_path: Path) -> None:
    """单 pid 多 scope record（user + project 不同 installPath）→ 逐 record 枚举 + 跨 record 同名首见去重。"""
    home = _home(tmp_path)
    root_user = _setup_catalog(home, "acme", "audit", servers=["figma"])
    root_proj = home / "alt-install" / "audit"
    _write_json(root_proj / "mcp-servers" / "figma.json", _stdio("figma"))
    _write_json(root_proj / "mcp-servers" / "extra.json", _stdio("extra"))
    _seed_installed(
        home,
        {
            "audit@acme": [
                _record(root_user, servers=["figma"]),
                _record(root_proj, scope="project", servers=["figma", "extra"]),
            ],
        },
    )

    records = collect_enabled_bundled_servers(home, _declared_enabled("audit@acme"), env=_env(tmp_path))

    assert {r.config.name for r in records} == {"figma", "extra"}
    figma = next(r for r in records if r.config.name == "figma")
    assert figma.install_path == root_user  # 首见（user record 在前）胜
    extra = next(r for r in records if r.config.name == "extra")
    assert extra.install_path == root_proj


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

    records = collect_enabled_bundled_servers(
        home, _declared_enabled("audit@acme", "fmt@beta", "ghost@acme"), env=_env(tmp_path),
    )

    assert {r.config.name for r in records} == {"figma"}


# ── _plugin_enable_origin ─────────────────────────────────────────────────────
def _layers(**overrides: dict) -> dict[SettingsScope, dict]:
    """速造 per-scope layers dict（全部 scope 默认空，用 scope 名字符串覆盖）"""
    empty: dict[SettingsScope, dict] = {s: {} for s in SCOPE_ORDER}
    for key, v in overrides.items():
        empty[SettingsScope(key)] = v
    return empty


def test_enable_origin_user_true_returns_user() -> None:
    """user scope enabledPlugins[pid]=true → 返回 USER"""
    layers = _layers(user={"enabledPlugins": {"x@mp": True}})
    assert _plugin_enable_origin(layers, "x@mp") is SettingsScope.USER


def test_enable_origin_user_true_project_true_returns_project() -> None:
    """user=true + project=true → 返回 PROJECT（更高 scope 胜）"""
    layers = _layers(
        user={"enabledPlugins": {"x@mp": True}},
        project={"enabledPlugins": {"x@mp": True}},
    )
    assert _plugin_enable_origin(layers, "x@mp") is SettingsScope.PROJECT


def test_enable_origin_user_true_project_false_returns_none() -> None:
    """user=true + project=false → 返回 None（被显式禁用覆盖）"""
    layers = _layers(
        user={"enabledPlugins": {"x@mp": True}},
        project={"enabledPlugins": {"x@mp": False}},
    )
    assert _plugin_enable_origin(layers, "x@mp") is None


def test_enable_origin_only_project_true_returns_project() -> None:
    """仅 project scope enabledPlugins[pid]=true → 返回 PROJECT"""
    layers = _layers(project={"enabledPlugins": {"x@mp": True}})
    assert _plugin_enable_origin(layers, "x@mp") is SettingsScope.PROJECT


def test_enable_origin_all_absent_returns_none() -> None:
    """全部 scope 无 enabledPlugins[pid] → 返回 None"""
    layers = _layers()
    assert _plugin_enable_origin(layers, "x@mp") is None


def test_enable_origin_local_overrides_project() -> None:
    """project=true + local=true → 返回 LOCAL（最高 scope 胜）"""
    layers = _layers(
        project={"enabledPlugins": {"x@mp": True}},
        local={"enabledPlugins": {"x@mp": True}},
    )
    assert _plugin_enable_origin(layers, "x@mp") is SettingsScope.LOCAL


def test_enable_origin_local_false_overrides_all() -> None:
    """user=true + project=true + local=false → 返回 None（local 显式禁用胜）"""
    layers = _layers(
        user={"enabledPlugins": {"x@mp": True}},
        project={"enabledPlugins": {"x@mp": True}},
        local={"enabledPlugins": {"x@mp": False}},
    )
    assert _plugin_enable_origin(layers, "x@mp") is None


# ── BundledServerRecord.enabled_origin ─────────────────────────────────────────
def test_collect_sets_enabled_origin_from_per_scope_layers(tmp_path: Path) -> None:
    """per_scope_layers 传入时 record 的 enabled_origin 被正确赋值"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["figma"])
    _seed_installed(home, {"audit@acme": [_record(root, servers=["figma"])]})

    layers: dict[SettingsScope, dict] = {s: {} for s in SCOPE_ORDER}
    layers[SettingsScope.PROJECT] = {"enabledPlugins": {"audit@acme": True}}

    # merged declared 须与 layers 一致：_plugin_boot_active 读取 merged 视图，
    # per_scope_layers 仅用于 origin 判定
    declared = _declared_enabled("audit@acme")
    records = collect_enabled_bundled_servers(
        home, declared, env=_env(tmp_path), per_scope_layers=layers,
    )
    assert len(records) == 1
    assert records[0].enabled_origin is SettingsScope.PROJECT


def test_collect_enabled_origin_none_without_per_scope_layers(tmp_path: Path) -> None:
    """未传 per_scope_layers 时 enabled_origin 保持 None（向后兼容）"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=["figma"])
    _seed_installed(home, {"audit@acme": [_record(root, servers=["figma"])]})

    records = collect_enabled_bundled_servers(
        home, _declared_enabled("audit@acme"), env=_env(tmp_path),
    )
    assert len(records) == 1
    assert records[0].enabled_origin is None


@pytest.mark.asyncio
async def test_recover_rematerialize_failure_log_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#263 七轮：boot 重物化失败的 WARN 不得回显 source 里的无 scheme query token。"""
    from a2c_smcp.computer.skills.sources import SkillSourceError

    home = _home(tmp_path)
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": {"type": "git", "url": f"file://{tmp_path}/repo"}}}},
        home=home,
    )
    # installPath 不存在 → `ledger_entry_fully_materialized` 为假 → 进入重物化分支
    _seed_installed(home, {"audit@acme": [_record(tmp_path / "gone" / "audit")]})
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}
    # catalog 树存在 → 跳过 clone、直达 ② 重物化
    catalog = marketplace_skill_dir(home, "acme")
    (catalog / ".tfrobot-plugin").mkdir(parents=True)
    (catalog / ".tfrobot-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "acme", "owner": {"name": "X"}, "plugins": [{"name": "audit", "source": "audit"}]}),
        encoding="utf-8",
    )

    async def boom(*args: object, **kwargs: object) -> tuple[Path, str | None]:
        raise SkillSourceError("x/r.git?token=SECRET_BOOT#f", "boom")

    monkeypatch.setattr("a2c_smcp.computer.settings.recovery.materialize_plugin", boom)
    caplog.set_level(logging.WARNING)
    from a2c_smcp.computer.settings import recovery as recovery_mod

    recovery_logger = recovery_mod.logger
    # 钉住 ambient 级别：WARN 在宿主把 `A2C_SMCP_LOG_LEVEL` 设成 error 时不会生成 record。
    a2c_logger = logging.getLogger("a2c_smcp")
    prev_level = a2c_logger.level
    a2c_logger.setLevel(logging.WARNING)
    recovery_logger.addHandler(caplog.handler)  # 项目 logger 关闭 propagate，直接挂 handler
    try:
        await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))
    finally:
        recovery_logger.removeHandler(caplog.handler)
        a2c_logger.setLevel(prev_level)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "re-materialize" in text  # 正对照：确实走到了该分支
    assert "token=SECRET_BOOT" not in text


@pytest.mark.asyncio
async def test_recover_degrades_on_manifest_rename_without_touching_capabilities(tmp_path: Path, monkeypatch) -> None:
    """
    #260/§5.14：恢复路径观测到清单改名 → **WARN 降级**（不阻断 boot、不影响其余 marketplace）。

    断言三者同时成立（不只断言日志）：未恢复该源（``restored_*`` 为空）、既有**能力保留**（上一轮恢复的 SKILL
    仍在活跃集）、且降级被记账（``failed_marketplaces``）。
    """
    from a2c_smcp.computer.skills.marketplace_identity import MarketplaceNameChangeError

    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {"audit@acme": [_record(root)]})
    reg = SkillRegistry()
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}
    # 先跑一次成功恢复，建立「既有能力」基线
    await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))
    assert reg.resolve("audit:lint") is not None

    async def _rename(*_args: object, **_kwargs: object) -> list[str]:
        raise MarketplaceNameChangeError("manifest renamed upstream")

    monkeypatch.setattr("a2c_smcp.computer.settings.recovery.stage_marketplace_skills", _rename)

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.failed_marketplaces == ["acme"]
    assert report.restored_skills == [] and report.restored_plugins == []
    assert reg.resolve("audit:lint") is not None, "降级不得摘掉既有能力"


# ── #287：声明源 / known 源比对（只诊断、不换源）─────────────────────────────
def _intent_only(plugin: str, mp: str) -> dict:
    """只含安装/启用意图的 declared（不含 ``extraKnownMarketplaces``）。"""
    pid = f"{plugin}@{mp}"
    return {"installedPlugins": [pid], "enabledPlugins": {pid: True}}


def _with_declarations(declared: dict, *entries: tuple[str, str]) -> dict:
    """给 declared 追加 ``extraKnownMarketplaces`` 声明（name, url）。"""
    return {**declared, "extraKnownMarketplaces": {name: {"source": {"type": "git", "url": url}} for name, url in entries}}


@pytest.mark.asyncio
async def test_recover_reports_declared_source_conflict_and_keeps_registered_source(tmp_path: Path) -> None:
    """#287：声明源 ≠ known 源 → 报一条冲突（两侧脱敏展示值），仍按 known 旧源恢复、不降级、不改写账本。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {"audit@acme": [_record(root)]})
    reg = SkillRegistry()
    declared = _with_declarations(_intent_only("audit", "acme"), ("acme", "https://example.com/other.git"))

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert report.marketplace_source_conflicts == [
        MarketplaceSourceConflict(
            marketplace="acme",
            declared_source_display="https://example.com/other.git",
            known_source_display="https://example.com/acme.git",
        )
    ]
    # 行为不变：旧源能力照常恢复、不降级、known 记录不被改成声明源
    assert "audit:lint" in report.restored_skills
    assert report.failed_marketplaces == []
    assert load_known_marketplaces(home=home)["marketplaces"]["acme"]["source"] == _SRC


@pytest.mark.asyncio
async def test_recover_no_conflict_when_declared_source_is_identical(tmp_path: Path) -> None:
    """正对照：声明源与 known 源为**同一 URL 字符串** → 不产生冲突。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"])
    _seed_installed(home, {"audit@acme": [_record(root)]})
    declared = _with_declarations(_intent_only("audit", "acme"), ("acme", _SRC["url"]))

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))

    assert report.marketplace_source_conflicts == []
    assert "audit:lint" in report.restored_skills


@pytest.mark.asyncio
async def test_recover_reports_ssh_https_equivalent_as_conflict(tmp_path: Path) -> None:
    """§5.14 fail-closed：ssh / https 等价写法**不归一** → 报冲突（诊断-only，不换源）。"""
    home = _home(tmp_path)
    catalog = marketplace_skill_dir(home, "acme")
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "acme": {
                    "source": {"type": "git", "url": "git@example.com:acme.git"},
                    "installLocation": str(catalog.resolve()),
                    "commitSha": "abc123",
                }
            },
        },
        home=home,
    )
    _seed_installed(home, {"audit@acme": [_record(root)]})
    reg = SkillRegistry()
    declared = _with_declarations(_intent_only("audit", "acme"), ("acme", "https://example.com/acme.git"))

    report = await recover_marketplace_skills(reg, home, declared, env=_env(tmp_path))

    assert len(report.marketplace_source_conflicts) == 1
    conflict = report.marketplace_source_conflicts[0]
    # scp-like 无法解析 → 展示面 fail-closed 占位符（同一权威判定，不做等价归一）
    assert conflict.known_source_display == "<redacted-git-source>"
    assert conflict.declared_source_display == "https://example.com/acme.git"
    assert "audit:lint" in report.restored_skills, "冲突只诊断，旧源能力照常恢复"


@pytest.mark.asyncio
async def test_recover_fails_closed_when_source_is_not_recognizable(tmp_path: Path) -> None:
    """
    §5.14 fail-closed：任一侧 source 不可识别（非 ``git``）→ **视为不同源**。

    钉住「同源判据是 :func:`same_source` 单一权威、而不是 ``==`` 字面相等」：两侧 dict **逐字节相同**，
    但都不是 git source ⇒ 无法证明同源 ⇒ 报冲突（协议明许 SDK 不做等价归一，宁可诊断也不静默认定同源）。
    """
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    weird = {"type": "file", "url": "https://example.com/acme.git"}
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": weird, "installLocation": str(root.parent.resolve())}}},
        home=home,
    )
    _seed_installed(home, {"audit@acme": [_record(root)]})
    declared = {**_intent_only("audit", "acme"), "extraKnownMarketplaces": {"acme": {"source": dict(weird)}}}

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))

    assert len(report.marketplace_source_conflicts) == 1
    conflict = report.marketplace_source_conflicts[0]
    assert conflict.marketplace == "acme"
    assert conflict.declared_source_display == conflict.known_source_display == "https://example.com/acme.git"


@pytest.mark.asyncio
async def test_recover_skips_conflict_for_marketplace_without_install_intent(tmp_path: Path) -> None:
    """#287：只检测 ``installedPlugins`` 意图涉及的市场——未涉及者即使声明源不同也不报。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "acme": {"source": _SRC, "installLocation": str(root.parent.resolve())},
                "ghost": {"source": {"type": "git", "url": "https://example.com/ghost.git"}, "installLocation": "/nope"},
            },
        },
        home=home,
    )
    _seed_installed(home, {"audit@acme": [_record(root)]})
    declared = _with_declarations(
        _intent_only("audit", "acme"),
        ("acme", _SRC["url"]),  # 同源，无冲突
        ("ghost", "https://example.com/ghost-moved.git"),  # 异源，但 ghost 无安装意图
    )

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))

    assert report.marketplace_source_conflicts == []


@pytest.mark.asyncio
async def test_recover_no_conflict_when_known_record_has_no_source(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """
    known 记录在、但 ``source`` 缺失/非 Mapping ⇒ **没有可用的已注册源**：不报冲突（交 ``failed_marketplaces``）。

    与 stage 循环的可用性判据同源（``not isinstance(source, Mapping)`` → degraded）。否则同一次恢复会同时打
    「沿用已注册源」与「已降级」两条互斥诊断。
    """
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"installLocation": str(root.parent.resolve())}}},
        home=home,
    )
    _seed_installed(home, {"audit@acme": [_record(root)]})
    declared = _with_declarations(_intent_only("audit", "acme"), ("acme", "https://example.com/other.git"))

    from a2c_smcp.computer.settings import recovery as recovery_mod

    recovery_mod.logger.addHandler(caplog.handler)
    try:
        caplog.set_level(logging.WARNING)
        report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))
    finally:
        recovery_mod.logger.removeHandler(caplog.handler)

    assert report.marketplace_source_conflicts == []
    assert report.failed_marketplaces == ["acme"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "keeping the registered source" not in text, "无可用已注册源时不得声称沿用已注册源"


@pytest.mark.asyncio
async def test_recover_no_conflict_when_known_source_is_not_mapping(tmp_path: Path) -> None:
    """known 记录的 ``source`` 是**非映射值**（如裸字符串）同样视为「无可用已注册源」→ 不报冲突。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": "https://example.com/acme.git", "installLocation": "/nope"}}},
        home=home,
    )
    _seed_installed(home, {"audit@acme": [_record(root)]})
    declared = _with_declarations(_intent_only("audit", "acme"), ("acme", "https://example.com/other.git"))

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))

    assert report.marketplace_source_conflicts == []
    assert report.failed_marketplaces == ["acme"]


@pytest.mark.asyncio
async def test_recover_lazy_marketplace_without_usable_source_reports_nothing(tmp_path: Path) -> None:
    """
    全惰性市场（installed_disabled ∧ 账本完好）+ 无可用已注册源 → **两处报告都不出现**。

    文档不得声称「无 known 记录一律进 ``failed_marketplaces``」：stage 循环对全惰性市场在查 known **之前**
    就 ``continue``（零动作是设计），故既无冲突也无降级。
    """
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    _seed_installed(home, {"audit@acme": [_record(root)]})
    declared = {**_with_declarations({}, ("acme", "https://example.com/other.git")), "installedPlugins": ["audit@acme"],
                "enabledPlugins": {"audit@acme": False}}

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))

    assert report.skipped_disabled == ["audit@acme"]
    assert report.marketplace_source_conflicts == []
    assert report.failed_marketplaces == []


@pytest.mark.asyncio
async def test_recover_no_conflict_when_known_record_absent(tmp_path: Path) -> None:
    """无 known 记录 → 无可比对的「已注册源」，不报冲突（仍走既有 ``failed_marketplaces`` 降级）。"""
    home = _home(tmp_path)
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    _seed_installed(home, {"audit@acme": [_record(root)]})
    declared = _with_declarations(_intent_only("audit", "acme"), ("acme", "https://example.com/other.git"))

    report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))

    assert report.marketplace_source_conflicts == []
    assert report.failed_marketplaces == ["acme"]


@pytest.mark.asyncio
async def test_recover_empty_intent_reports_no_conflict(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """空 intent 早退行为不变：不检测、无冲突、**不产生任何 WARNING**。"""
    home = _home(tmp_path)
    _setup_catalog(home, "acme", "audit", skills=["lint"])
    declared = _with_declarations({}, ("acme", "https://example.com/other.git"))

    from a2c_smcp.computer.settings import recovery as recovery_mod

    recovery_mod.logger.addHandler(caplog.handler)  # 项目 logger 关闭 propagate，直接挂 handler
    try:
        caplog.set_level(logging.WARNING)
        report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))
    finally:
        recovery_mod.logger.removeHandler(caplog.handler)

    assert report.marketplace_source_conflicts == []
    assert report.failed_marketplaces == []
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_recover_source_conflict_diagnostics_are_redacted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """
    #287：冲突的 report 字段与 recovery 日志（汇总 WARN + 逐条 DEBUG）均不得回显 URL 凭据。

    **两侧都带凭据**：只给 known 侧注入凭据的话，「declared 侧直接取原值」的变异会存活。
    """
    home = _home(tmp_path)
    catalog = marketplace_skill_dir(home, "acme")
    root = _setup_catalog(home, "acme", "audit", skills=["lint"], seed_known=False)
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "acme": {
                    "source": {"type": "git", "url": "https://alice:s3cret@example.com/acme.git"},
                    "installLocation": str(catalog.resolve()),
                }
            },
        },
        home=home,
    )
    _seed_installed(home, {"audit@acme": [_record(root)]})
    declared = _with_declarations(_intent_only("audit", "acme"), ("acme", "https://bob:t0ken@example.com/other.git"))

    from a2c_smcp.computer.settings import recovery as recovery_mod

    a2c_logger = logging.getLogger("a2c_smcp")  # 逐条诊断是 DEBUG，需放开包级 logger 等级才能被捕获
    prev_level = a2c_logger.level
    a2c_logger.setLevel(logging.DEBUG)
    recovery_mod.logger.addHandler(caplog.handler)  # 项目 logger 关闭 propagate，直接挂 handler
    try:
        caplog.set_level(logging.DEBUG)
        report = await recover_marketplace_skills(SkillRegistry(), home, declared, env=_env(tmp_path))
    finally:
        recovery_mod.logger.removeHandler(caplog.handler)
        a2c_logger.setLevel(prev_level)

    conflict = report.marketplace_source_conflicts[0]
    assert conflict.known_source_display == "https://example.com/acme.git"
    assert conflict.declared_source_display == "https://example.com/other.git"
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("s3cret", "alice", "t0ken", "bob"):
        assert secret not in conflict.known_source_display and secret not in conflict.declared_source_display
        assert secret not in text, f"report / 日志不得回显 {secret}"
    # 正对照：确实走到了冲突诊断分支（WARN 汇总 + 逐条 DEBUG）
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("different source" in message for message in warnings)
    # 汇总行带得出「该处理哪个市场」（默认 INFO 级别下逐条 DEBUG 看不见），但**不含两侧 URL**
    summary = next(message for message in warnings if "different source" in message)
    assert "acme" in summary
    assert "https://" not in summary
    assert any(r.levelno == logging.DEBUG and "acme" in r.getMessage() for r in caplog.records)


def test_detect_marketplace_source_conflicts_redacts_ledger_key() -> None:
    """
    纯函数契约：``marketplace`` 字段是**账本键脱敏值**（#261/#263/#281 口径）。

    经 recovery 不可达（pid 上游已过严格 kebab 校验，见 :func:`detect_marketplace_source_conflicts` 注释），
    故在此直接钉住纯函数契约——否则「去掉 redact_ledger_key」会是个杀不死的变异。
    """
    ledger_key = "https://alice:s3cret@example.com/mp"
    known = {ledger_key: {"source": {"type": "git", "url": "https://example.com/a.git"}}}
    declared = {"extraKnownMarketplaces": {ledger_key: {"source": {"type": "git", "url": "https://example.com/b.git"}}}}

    conflicts = detect_marketplace_source_conflicts([ledger_key], declared, known)

    assert len(conflicts) == 1
    assert "s3cret" not in conflicts[0].marketplace
    assert "alice" not in conflicts[0].marketplace
    assert conflicts[0].marketplace == "https://example.com/mp"


@pytest.mark.parametrize(
    ("declared", "known"),
    [
        pytest.param({"extraKnownMarketplaces": {"acme": "not-a-mapping"}}, {"acme": {"source": _SRC}}, id="decl-not-mapping"),
        pytest.param({"extraKnownMarketplaces": {"acme": {"source": _SRC}}}, {"acme": "not-a-mapping"}, id="known-not-mapping"),
        pytest.param({"extraKnownMarketplaces": "not-a-mapping"}, {"acme": {"source": _SRC}}, id="decls-not-mapping"),
    ],
)
def test_detect_marketplace_source_conflicts_tolerates_malformed_entries(declared: dict, known: dict) -> None:
    """形状不合（声明条目 / known 记录 / ``extraKnownMarketplaces`` 整体）→ 跳过，不抛、不报冲突。"""
    assert detect_marketplace_source_conflicts(["acme"], declared, known) == []


@pytest.mark.parametrize(
    "decl",
    [
        pytest.param({}, id="decl-no-source-key"),
        pytest.param({"source": "oops"}, id="decl-source-not-mapping"),
        pytest.param({"source": None}, id="decl-source-none"),
    ],
)
def test_detect_conflicts_fail_closed_on_malformed_declared_source(decl: dict) -> None:
    """
    纯函数契约：**已注册源可用**但**声明源不可识别** ⇒ fail-closed 报冲突（展示面占位符）。

    与「known 侧不可用 → 跳过」刻意不对称：known 侧不可用意味着没有可比对象（交 ``failed_marketplaces``）；
    声明侧不可用意味着用户确实声明了什么、却无法证明同源（§5.14「无法确定时 MUST fail-closed」）。
    """
    known = {"acme": {"source": {"type": "git", "url": "https://example.com/acme.git"}}}
    declared = {"extraKnownMarketplaces": {"acme": decl}}

    conflicts = detect_marketplace_source_conflicts(["acme"], declared, known)

    assert len(conflicts) == 1
    assert conflicts[0].marketplace == "acme"
    assert conflicts[0].known_source_display == "https://example.com/acme.git"
    assert conflicts[0].declared_source_display == "<redacted-git-source>"


# ── #293：被 settings 校验丢弃的非法声明 / dropped invalid declarations ───────
_SECRET_URL = "https://bob:t0ken@example.com/repo.git"


def _verr(field: str, reason: str, scope: SettingsScope = SettingsScope.USER, source_path: str | None = None) -> SettingsValidationError:
    return SettingsValidationError(scope=scope, field=field, reason=reason, source_path=source_path)


@contextlib.contextmanager
def _capture_recovery_logs(level: int) -> Iterator[list[logging.LogRecord]]:
    """
    捕获 recovery logger 的记录，**显式钉住包级 logger 等级**并在 finally 还原。

    包级 logger 的有效级别由 ``A2C_SMCP_LOG_LEVEL`` 在 import 时固定（默认 INFO）——不钉住的话，
    断言 DEBUG 记录会在 CI（INFO）假红、在本机（父目录 gitignored ``.env`` 设 debug）假绿。
    项目 logger ``propagate=False``，故直接挂 handler 而非用 caplog 的默认路径。
    """
    from a2c_smcp.computer.settings import recovery as recovery_mod

    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    a2c_logger = logging.getLogger("a2c_smcp")
    prev_level = a2c_logger.level
    a2c_logger.setLevel(level)
    sink = _Sink(level=level)
    recovery_mod.logger.addHandler(sink)
    try:
        yield records
    finally:
        recovery_mod.logger.removeHandler(sink)
        a2c_logger.setLevel(prev_level)


def test_sanitize_marketplace_name_uses_segment_funnel() -> None:
    """市场名是**单个不可信值**：走段漏斗（path 段里的凭据也挡），比散文脱敏更严（#261 口径）。"""
    assert sanitize_marketplace_name(_SECRET_URL) == "https://example.com/repo.git"
    # path 段携凭据 —— 散文脱敏只去 userinfo、会保留 path，段漏斗 fail-closed
    assert sanitize_marketplace_name("https://host/team/alice:PW@example.com/repo.git") == "<redacted-git-source>"
    # 合法 kebab 名逐字不变（负对照：不得把正常名字改成占位符）
    assert sanitize_marketplace_name("acme") == "acme"


def test_sanitize_marketplace_text_keeps_prose_shape() -> None:
    """``reason`` 是散文：逐 token 脱敏，保留句式（不得整句替换成占位符）。"""
    got = sanitize_marketplace_text(f"malformed git url: {_SECRET_URL}")
    assert got == "malformed git url: https://example.com/repo.git"
    assert "t0ken" not in got and "bob" not in got


def test_declaration_marketplace_keeps_whole_rest_for_name_errors() -> None:
    """
    名称型错误：``<name>`` 之后没有子字段，**整个剩余段**就是名字——不能按后缀启发式切。

    ``acme.source.url`` 作为**非法市场名**时，其 field 与「合法名 acme 的 .source.url 子字段错误」逐字相同；
    区分二者的唯一判据是 reason（与 rust ``declaration_marketplace`` 同款）。
    """
    assert declaration_marketplace(
        "extraKnownMarketplaces.acme.source.url", "marketplace name must be strict kebab (1-64)",
    ) == "acme.source.url"
    assert declaration_marketplace("extraKnownMarketplaces.acme.source.url", "malformed git url") == "acme"


@pytest.mark.parametrize(
    "field,reason,expected_marketplace,expected_field",
    [
        ("extraKnownMarketplaces", "expected object, got str", None, "extraKnownMarketplaces"),
        ("extraKnownMarketplaces.acme", "entry must be object, got list", "acme", "extraKnownMarketplaces.acme"),
        ("extraKnownMarketplaces.acme.source", "missing or non-object 'source'", "acme", "extraKnownMarketplaces.acme.source"),
        (
            "extraKnownMarketplaces.acme.source.type",
            "only source.type == 'git' is supported",
            "acme",
            "extraKnownMarketplaces.acme.source.type",
        ),
        ("extraKnownMarketplaces.acme.source.url", "malformed git url", "acme", "extraKnownMarketplaces.acme.source.url"),
        ("extraKnownMarketplaces.acme.autoUpdate", "must be bool, got str", "acme", "extraKnownMarketplaces.acme.autoUpdate"),
    ],
)
def test_marketplace_declaration_errors_shapes(
    field: str, reason: str, expected_marketplace: str | None, expected_field: str,
) -> None:
    """四形态 + 顶层整字段：1 error → 1 entry，scope 保真，``marketplace`` 供 #294 免解析 ``field``。"""
    got = marketplace_declaration_errors([_verr(field, reason)])
    assert got == [
        MarketplaceDeclarationError(
            scope=SettingsScope.USER, marketplace=expected_marketplace, field=expected_field, reason=reason, source_path=None,
        ),
    ]


def test_marketplace_declaration_errors_filters_other_fields_and_preserves_order() -> None:
    """只收 ``extraKnownMarketplaces`` 子树（含 ``.`` 边界）；保序、不聚合——同市场跨层各一条。"""
    errors = [
        _verr("enabledPlugins.BAD KEY", "key must be '<plugin>@<marketplace>' (strict kebab)"),
        _verr("extraKnownMarketplacesBackup.acme", "unrelated"),  # 裸 startswith 会误收，这里必须被挡
        _verr("extraKnownMarketplaces.acme.source", "missing or non-object 'source'", SettingsScope.USER),
        _verr("extraKnownMarketplaces.acme.source.url", "malformed git url", SettingsScope.LOCAL),
    ]
    got = marketplace_declaration_errors(errors)
    assert [(e.marketplace, e.scope) for e in got] == [("acme", SettingsScope.USER), ("acme", SettingsScope.LOCAL)]


def test_marketplace_declaration_errors_redacts_and_scrubs() -> None:
    """
    凭据形态市场名 / 带凭据的 reason / 控制字符：report 四字段全部脱敏（#295 只管 settings show）。

    **path 段凭据**是关键判别输入：散文脱敏（`redact_git_urls_in_text`）对它只去 userinfo、保留 path，
    只有段漏斗（`redact_ledger_key`）才 fail-closed。名字段若误用散文脱敏，本用例必须转红。
    """
    evil_name = f"{_SECRET_URL}"
    path_cred_name = "https://host/team/alice:PW@example.com/repo.git"
    dirty_path = "/tmp/ev‮il/settings\x1b[31m.json"
    errors = [
        _verr(
            f"extraKnownMarketplaces.{evil_name}",
            "marketplace name must be strict kebab (1-64)",
            SettingsScope.PROJECT,
            source_path=dirty_path,
        ),
        _verr(
            f"extraKnownMarketplaces.{path_cred_name}",
            "marketplace name must be strict kebab (1-64)",
        ),
        # 控制字符 / 方向覆写 / 行分隔：手工构造（schema 当前不会产出），纵深防御。
        _verr("extraKnownMarketplaces.evil‮gnp.exe FAKE", "expected object, got array\nFAKE_LINE\x1b[31m‮REV"),
    ]
    got = marketplace_declaration_errors(errors)
    assert len(got) == 3
    # 正对照：原始错误里确实带凭据（userinfo / path 段）与控制字符
    assert "t0ken" in errors[0].field
    assert "PW" in errors[1].field and "alice" in errors[1].field
    assert "‮" in errors[2].field and "\n" in errors[2].reason

    userinfo, path_cred, scrubbed = got
    assert "t0ken" not in userinfo.marketplace and "bob" not in userinfo.marketplace
    assert "t0ken" not in userinfo.field
    # 段漏斗 fail-closed：path 段凭据整值占位，绝不回显
    assert path_cred.marketplace == "<redacted-git-source>"
    assert "PW" not in path_cred.field and "alice" not in path_cred.field
    # source_path：非 userinfo 形态 ⇒ 路径轴处理（控制字符 / 方向覆写仍须清掉，路径保持可读）
    assert userinfo.source_path == "/tmp/ev il/settings [31m.json"
    for error in got:
        for blob in (error.marketplace or "", error.field, error.reason, error.source_path or ""):
            for needle in ("‮", " ", "\n", "\x1b", "t0ken", "PW"):
                assert needle not in blob, (needle, blob)


def test_attach_marketplace_declaration_errors_logs_summary_at_default_info() -> None:
    """
    WARN 汇总（带脱敏市场名）+ 逐条 DEBUG。

    默认日志级别是 INFO ⇒ 汇总必须落在 WARNING，否则用户看不见（#293 验收「默认 INFO 可见」）。
    """
    report = GovernanceRecoveryReport()
    errors = [
        _verr(f"extraKnownMarketplaces.{_SECRET_URL}.source.url", f"malformed git url: {_SECRET_URL}"),
        _verr("extraKnownMarketplaces.acme.source", "missing or non-object 'source'", SettingsScope.LOCAL),
    ]
    with _capture_recovery_logs(logging.DEBUG) as captured:
        attach_marketplace_declaration_errors(report, errors)

    assert len(report.marketplace_declaration_errors) == 2
    warnings = [r.getMessage() for r in captured if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    summary = warnings[0]
    assert "2 invalid marketplace declaration(s)" in summary
    assert "https://example.com/repo.git" in summary  # 脱敏后的市场名被点名
    assert "acme" in summary
    assert "t0ken" not in summary and "bob" not in summary
    # 逐条 DEBUG
    debug_lines = [r.getMessage() for r in captured if r.levelno == logging.DEBUG]
    assert len(debug_lines) == 2, debug_lines
    assert all("t0ken" not in line and "bob" not in line for line in debug_lines), debug_lines


def test_attach_marketplace_declaration_errors_dedups_names_in_summary() -> None:
    """同市场跨层两条错误：**逐条计数 2**，但 WARN 汇总的市场名去重（只点一次名）。"""
    report = GovernanceRecoveryReport()
    errors = [
        _verr("extraKnownMarketplaces.acme.source", "missing or non-object 'source'", SettingsScope.USER),
        _verr("extraKnownMarketplaces.acme.source.url", "malformed git url", SettingsScope.LOCAL),
    ]
    with _capture_recovery_logs(logging.DEBUG) as captured:
        attach_marketplace_declaration_errors(report, errors)

    assert [e.scope for e in report.marketplace_declaration_errors] == [SettingsScope.USER, SettingsScope.LOCAL]
    summary = next(r.getMessage() for r in captured if r.levelno == logging.WARNING)
    assert "2 invalid marketplace declaration(s)" in summary
    assert summary.count("acme") == 1, summary  # 名字去重，不重复点名


def test_attach_marketplace_declaration_errors_is_silent_without_errors() -> None:
    """负对照：无错误 ⇒ 报告空、零日志（不得每次 boot 都刷一条「0 条错误」）。"""
    report = GovernanceRecoveryReport()
    with _capture_recovery_logs(logging.DEBUG) as captured:
        attach_marketplace_declaration_errors(report, [])

    assert report.marketplace_declaration_errors == []
    assert captured == []


def test_source_path_keeps_readable_paths() -> None:
    """
    ``source_path`` 负对照（防过度遮蔽）：**无法排除 userinfo** 的路径才整值占位，其余保留可读性。

    这三条都是**架构师点名的负对照**：Windows 盘符（`C:` 不是 userinfo 分隔符）、普通 `%` 路径、
    `?` 尾部（由路径轴原语遮蔽而非整值占位）。另加「段内有 `@` 但冒号在其后」的普通路径。
    """
    windows = "C:\\repos\\alice@corp\\settings.json"
    (kept,) = marketplace_declaration_errors([_verr("extraKnownMarketplaces.acme.source", "boom", source_path=windows)])
    assert kept.source_path == windows

    pct = "/home/100%25done/settings.json"
    (also_kept,) = marketplace_declaration_errors([_verr("extraKnownMarketplaces.acme.source", "boom", source_path=pct)])
    assert also_kept.source_path == pct

    colon_after_at = "/home/alice@corp/notes:2024/x"
    (still_kept,) = marketplace_declaration_errors(
        [_verr("extraKnownMarketplaces.acme.source", "boom", source_path=colon_after_at)],
    )
    assert still_kept.source_path == colon_after_at

    query = "/srv/x/settings.json?token=SECRET"
    (masked,) = marketplace_declaration_errors([_verr("extraKnownMarketplaces.acme.source", "boom", source_path=query)])
    assert masked.source_path == "/srv/x/settings.json<redacted-source>"
    assert "SECRET" not in masked.source_path


@pytest.mark.parametrize(
    "dirty,secret",
    [
        # 干净 userinfo（架构师裁决：containing userinfo 即整值占位，诊断可读性只对无 userinfo 路径保留）
        ("/home/alice:pw@example.com/a2c/settings.json", "pw"),
        # 控制 / 方向覆写字符穿插：scrub 换空格 / escape 造 `\` 段边界
        ("/home/alice‮:pw@example.com/settings.json", "pw"),
        ("/home/alice:pw‮@example.com/settings.json", "pw"),
        ("/home/alice\x1b:pw@example.com/settings.json", "pw"),
        ("/home/alice:pw\x1b@example.com/settings.json", "pw"),
        # 首个 `@` 在 `:` 之前（只看首个 @ 会漏）
        ("/home/alice@corp:pw@example.com/settings.json", "pw"),
        ("/home/alice@corp:pw\x1b@example.com/settings.json", "pw"),
        # `#` / `?` 夹在 `:` 与 `@` 之间（原语把 `#`/`?` 之后当尾部）
        ("/home/alice:my-secret#x@example.com/settings.json", "my-secret"),
        ("/home/alice:my-secret?x@example.com/settings.json", "my-secret"),
        # 段内空白：原语正则要求 `:` 前非空白
        ("/home/alice:my pw pass@example.com/settings.json", "my pw pass"),
        # 凭据内的字面分隔符：逐段判定会被拆开
        ("/home/alice:pw\\x@example.com/settings.json", "pw"),
        ("/home/alice:pw/x@example.com/settings.json", "pw"),
        # percent-encoded（含编码出的分隔符 / 前导 `@`）
        ("/home/alice:pw%2Fsecret%40example.com/settings.json", "pw"),
        ("/home/alice%40corp:pw%40example.com/settings.json", "pw"),
        ("/home/alice%3Apw%40example.com/settings.json", "pw"),
        ("/home/alice%253Apw%2540example.com/settings.json", "pw"),
        # 四层编码（3 轮不够）
        ("/home/alice%2525253Apw%25252540example.com/settings.json", "pw"),
        # NFKC 全角
        ("/home/alice：pw＠example.com/settings.json", "pw"),
    ],
)
def test_source_path_fails_closed_on_userinfo_shape(dirty: str, secret: str) -> None:
    """
    ``source_path`` 的 fail-closed 判据：剥盘符后「最后一个 `@` 在第一个 `:` 之后」，在原文与每一轮
    ``NFKC → percent-decode`` 形态上判一次；命中即整值占位。

    判别力（逐项实测过）：只看首个 `@`、只判原文、不做 NFKC、逐段判定、只在原串上找字面 `:`/`@` ——
    任一退化都会有对应向量转红。每条向量的 `secret` 既是正对照（原串确实带它）也是负断言（输出不得含它）。
    """
    assert secret in dirty  # 正对照：原始路径确实带该凭据片段
    (out,) = marketplace_declaration_errors([_verr("extraKnownMarketplaces.acme.source", "boom", source_path=dirty)])
    assert out.source_path == "<redacted-git-source>"
    assert secret not in out.source_path


def test_source_path_with_control_chars_but_no_userinfo_is_still_scrubbed() -> None:
    """负对照（防过度遮蔽）：无 `:`/`@` 关系的控制字符路径仍按字符清洗保留可读性，不做整值占位。"""
    dirty = "/tmp/ev\u202eil/settings\x1b[31m.json"
    (out,) = marketplace_declaration_errors([_verr("extraKnownMarketplaces.acme.source", "boom", source_path=dirty)])
    assert out.source_path == "/tmp/ev il/settings [31m.json"
    dirty = "/tmp/ev\u202eil/settings\x1b[31m.json"
    (out,) = marketplace_declaration_errors([_verr("extraKnownMarketplaces.acme.source", "boom", source_path=dirty)])
    assert out.source_path == "/tmp/ev il/settings [31m.json"
