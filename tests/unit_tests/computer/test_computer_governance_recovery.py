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
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.recovery import BundledServerRecord
from a2c_smcp.computer.settings.schema import SettingsScope
from a2c_smcp.computer.settings.store import (
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
)
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
async def test_reconcile_governance_collects_then_batch_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#208：治理恢复 = **先挂完所有、再经统一批量启动器一次启动**（共享 Computer 级并发上限）。

    三条契约一并钉住：① 挂载阶段 ``start=False``（不在循环内融合启动）；② 循环内**不**调批量 API；
    ③ 循环后**恰好一次**批量启动，且入参 = 全部重挂成功的 bundle_id（输入序）。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=[FIGMA_NAME], skills=["lint"])

    mounts: list[tuple[str, bool]] = []
    batch_calls: list[list[str]] = []

    async with Computer(name="t", auto_connect=True, skill_home=home) as comp:
        assert comp.mcp_manager is not None

        async def _fake_batch(ids: list[str]) -> list[Any]:
            batch_calls.append(list(ids))
            return []

        monkeypatch.setattr(comp.mcp_manager, "astart_clients_batch", _fake_batch)

        async def register(cfg, record: BundledServerRecord) -> None:
            mounts.append((cfg.name, False))
            assert batch_calls == [], "挂载循环内不得触发批量启动（须全部挂完再启）"
            await comp.amount_server(cfg, plugin=record.plugin, marketplace=record.marketplace, start=False)

        report = await comp.reconcile_governance(
            existing_bundle_ids=lambda: set(),
            register_server=register,
            declared=_DECLARED_ENABLED,
        )

    assert report.remounted_servers == [FIGMA_BID]
    assert mounts == [(FIGMA_NAME, False)]
    assert batch_calls == [[FIGMA_BID]], "循环后须经统一批量启动器启动，且入参为已重挂的 bundle_id"


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


# ── #165 Gap B：安全层（Gate 1-3）对 plugin bundled server 生效 ─────────────────
@pytest.mark.asyncio
async def test_bundled_server_denied_by_security_layer_not_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy deny → plugin bundled server（user scope enabled）不挂载。"""
    import a2c_smcp.computer.settings.policy as policy_mod

    _isolate_declared_env(tmp_path, monkeypatch)
    # 注入 policy 拒绝名单
    monkeypatch.setattr(
        policy_mod, "resolve_policy_settings",
        lambda **_: {"deniedMcpServers": [FIGMA_BID]},
    )
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
        # 安全层拒绝 → register 不被调用
        assert mounted == []
        assert report.remounted_servers == []
        # 不在 PENDING 中（协议 §2.3：安全层 DISABLED 不进 pending）
        assert report.pending_bundled_servers == []


@pytest.mark.asyncio
async def test_bundled_server_passes_security_layer_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 policy deny → plugin bundled server 正常挂载（回归守卫）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _plugin_root = _seed_home(tmp_path, servers=[FIGMA_NAME])

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
        assert mounted == [FIGMA_NAME]
        assert report.remounted_servers == [FIGMA_BID]
        assert report.pending_bundled_servers == []


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


# ── #287：声明源 / known 源诊断（只诊断、不换源）─────────────────────────────
_DECLARED_MOVED = {
    **_DECLARED_ENABLED,
    "extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "https://example.com/moved.git"}}},
}


@pytest.mark.asyncio
async def test_boot_diagnoses_declared_source_change_but_keeps_registered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#287：改声明源后走真实 ``reconcile_governance()`` → 报告冲突，仍按 known 旧源恢复、不降级、不改写账本。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _plugin_root = _seed_home(tmp_path, skills=["lint"])
    _write_settings(tmp_path / "cfg" / "a2c" / "settings.json", _DECLARED_MOVED)

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance()
        assert [c.marketplace for c in report.marketplace_source_conflicts] == ["acme"]
        conflict = report.marketplace_source_conflicts[0]
        assert conflict.declared_source_display == "https://example.com/moved.git"
        assert conflict.known_source_display == _SRC["url"]
        # 行为不变：旧源能力照常恢复、不降级
        assert "audit:lint" in report.restored_skills
        assert report.failed_marketplaces == []
        assert "audit:lint" in {ref["name"] for ref in comp.get_skills()}
    # known 记录保持旧源（只诊断、不换源）
    assert load_known_marketplaces(home=home)["marketplaces"]["acme"]["source"] == _SRC


@pytest.mark.asyncio
async def test_boot_no_conflict_when_declared_source_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正对照：声明源与 known 源为同一 URL 字符串 → 无冲突、恢复照常。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _plugin_root = _seed_home(tmp_path, skills=["lint"])
    _write_settings(
        tmp_path / "cfg" / "a2c" / "settings.json",
        {**_DECLARED_ENABLED, "extraKnownMarketplaces": {"acme": {"source": _SRC}}},
    )

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance()
        assert report.marketplace_source_conflicts == []
        assert "audit:lint" in report.restored_skills
        assert report.failed_marketplaces == []


@pytest.mark.asyncio
async def test_boot_up_summary_counts_declared_source_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """
    #287：``boot_up`` 的 recovery 汇总行纳入冲突数（仅诊断，不阻断启动）。

    夹具刻意取「**零恢复** + 有冲突」的形态（plugin installed 但 disabled → 全惰性静止态）——若汇总条件
    只看 ``restored_skills`` / ``failed_marketplaces``，本用例就**没有**任何汇总输出，冲突会静默无痕。
    这同时钉住「全惰性零动作的市场也要被诊断」这一设计决定。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _plugin_root = _seed_home(tmp_path, skills=["lint"])
    _write_settings(
        tmp_path / "cfg" / "a2c" / "settings.json",
        {**_DECLARED_MOVED, "enabledPlugins": {"audit@acme": False}},
    )

    from a2c_smcp.computer import computer as computer_mod

    # 项目 logger 关闭 propagate，直接挂 handler；取模块 logger（get_logger 会加包前缀，名字不可硬编码）。
    computer_mod.logger.addHandler(caplog.handler)
    try:
        caplog.set_level(logging.INFO)
        async with Computer(name="t", skill_home=home) as comp:
            report = await comp.reconcile_governance()
    finally:
        computer_mod.logger.removeHandler(caplog.handler)

    # 零恢复形态：无 SKILL 恢复、无降级，**只有**冲突——汇总行必须仍被打印
    assert report.restored_skills == [] and report.failed_marketplaces == []
    assert len(report.marketplace_source_conflicts) == 1
    summaries = [r.getMessage() for r in caplog.records if "governance recovery:" in r.getMessage()]
    assert any(
        "0 skills restored" in message and "0 marketplaces degraded" in message and "1 declared-source conflicts" in message
        for message in summaries
    ), summaries


# ── #293：settings 校验丢弃的非法声明进入恢复报告 ─────────────────────────────
# 四形态（均经**真实** resolve_settings → validate_settings，不手搓 ResolvedSettings）：
# 漏 scheme / owner-repo 简写 / 非 git / 缺 source。
_INVALID_DECLARATIONS = [
    pytest.param({"acme": {"source": {"type": "git", "url": "example.com/acme.git"}}}, "source.url", id="missing-scheme"),
    pytest.param({"acme": {"source": {"type": "git", "url": "owner/repo"}}}, "source.url", id="owner-repo-shorthand"),
    pytest.param({"acme": {"source": {"type": "hg", "url": "https://example.com/acme.git"}}}, "source.type", id="not-git"),
    pytest.param({"acme": {}}, "source", id="missing-source"),
]


def _user_settings_path(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "a2c" / "settings.json"


def _local_settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".tfrobot" / "settings.local.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("declarations,suffix", _INVALID_DECLARATIONS)
async def test_reconcile_governance_reports_invalid_declarations(
    declarations: dict, suffix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：非法声明被 settings 校验整条丢弃 → 报告可见；恢复行为**不变**。

    判别力：报告字段来自 ``ResolvedSettings.errors`` 而不是 ``declared``（被丢弃后 declared 里根本没有
    这个市场），因此改回「只读 declared.settings」会让这四条全红。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    settings_path = _user_settings_path(tmp_path)
    _write_settings(settings_path, {**_DECLARED_ENABLED, "extraKnownMarketplaces": declarations})

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance()

    assert len(report.marketplace_declaration_errors) == 1, report.marketplace_declaration_errors
    error = report.marketplace_declaration_errors[0]
    assert error.scope is SettingsScope.USER
    assert error.marketplace == "acme"
    assert error.field == f"extraKnownMarketplaces.acme.{suffix}"
    assert error.source_path == str(settings_path)
    # 行为不变：旧源照常恢复、不降级、不报冲突（被丢弃的声明无可比对象）、账本不改写
    assert "audit:lint" in report.restored_skills
    assert report.failed_marketplaces == []
    assert report.marketplace_source_conflicts == []
    # 账本源不被「非法声明」改写（stage 会更新 commitSha / lastUpdated 等派生元数据，故只钉 source）
    assert load_known_marketplaces(home=home)["marketplaces"]["acme"]["source"] == _SRC


@pytest.mark.asyncio
async def test_reconcile_governance_reports_invalid_declaration_without_install_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：**无安装意图**的市场同样要报（用户裁决 1：配置错误本身应可见）。

    判别力：``recover_marketplace_skills`` 在 ``not intent`` 时早退——若把回填放在它内部/早退之后，
    本用例必然拿到空列表。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home = tmp_path / "skill-home"
    home.mkdir()
    _write_settings(
        _user_settings_path(tmp_path),
        {"extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "owner/repo"}}}},
    )

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance()

    assert report.restored_skills == [] and report.restored_plugins == []
    assert [e.marketplace for e in report.marketplace_declaration_errors] == ["acme"]
    assert report.marketplace_declaration_errors[0].scope is SettingsScope.USER


@pytest.mark.asyncio
async def test_reconcile_governance_reports_whole_field_type_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：``extraKnownMarketplaces`` **整字段**类型错（非 object）→ 仍产生一条报告、``marketplace is None``、
    不崩（无人可归到具体市场；#294 据此决定不带名展示）。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_settings(
        _user_settings_path(tmp_path),
        {**_DECLARED_ENABLED, "extraKnownMarketplaces": "not-an-object"},
    )

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance()

    assert len(report.marketplace_declaration_errors) == 1
    error = report.marketplace_declaration_errors[0]
    assert error.marketplace is None
    assert error.field == "extraKnownMarketplaces"
    assert error.scope is SettingsScope.USER
    # 整字段判废不影响恢复
    assert "audit:lint" in report.restored_skills
    assert report.marketplace_source_conflicts == []


@pytest.mark.asyncio
async def test_reconcile_governance_rejects_unsupported_declared_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#293 契约：``declared=`` 只接受 ``None`` / ``Mapping`` / ``ResolvedSettings``；其余显式 ``TypeError``。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])

    async with Computer(name="t", skill_home=home) as comp:
        with pytest.raises(TypeError, match="ResolvedSettings"):
            await comp.reconcile_governance(declared=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reconcile_governance_user_invalid_local_valid_is_not_aggregated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：**逐条 + scope**，不得聚合成「该市场无声明」。

    user 层非法、local 层合法 → 合并视图仍有可用声明（local 的）；报告 user 那一条错误，且冲突检测按
    **local 的合法声明**进行（同源 → 0 冲突）。若实现把结论聚合成「无声明」，冲突/错误两侧都会错。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_settings(
        _user_settings_path(tmp_path),
        {**_DECLARED_ENABLED, "extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "owner/repo"}}}},
    )
    _write_settings(_local_settings_path(tmp_path), {"extraKnownMarketplaces": {"acme": {"source": _SRC}}})

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance()

    assert [(e.marketplace, e.scope) for e in report.marketplace_declaration_errors] == [("acme", SettingsScope.USER)]
    assert report.marketplace_source_conflicts == []
    assert "audit:lint" in report.restored_skills


@pytest.mark.asyncio
async def test_reconcile_governance_user_invalid_local_valid_different_source_reports_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：两个列表**按事实**分开计数——同名可各出现一次，不是重复计数。

    事实 1（local 层）：声明自身可用 → 与 known 不同源 ⇒ ``marketplace_source_conflicts`` 一条。
    事实 2（user 层）：声明被校验丢弃 ⇒ ``marketplace_declaration_errors`` 一条。
    压制任一条都会误导用户（前者说明「改了源但被拒」，后者说明「写错了根本没生效」）。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_settings(
        _user_settings_path(tmp_path),
        {**_DECLARED_ENABLED, "extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "owner/repo"}}}},
    )
    _write_settings(
        _local_settings_path(tmp_path),
        {"extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "https://example.com/other.git"}}}},
    )

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance()

    assert [e.scope for e in report.marketplace_declaration_errors] == [SettingsScope.USER]
    assert [c.marketplace for c in report.marketplace_source_conflicts] == ["acme"]
    # 恢复仍按 known 旧源；两者都不改写账本、不降级
    assert "audit:lint" in report.restored_skills
    assert report.failed_marketplaces == []


@pytest.mark.asyncio
async def test_reconcile_governance_explicit_resolved_settings_carries_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：显式 ``declared=`` 路径的契约——传 ``ResolvedSettings`` 才携带 settings 错误。

    用**真实** ``resolve_settings`` 产出（不手搓对象，否则「errors 传到了」只是自己构造出来的）；
    裸 ``Mapping`` 为遗留形态、恒空（锁定契约，防 #294 误以为裸 dict 也有诊断）。
    """
    from a2c_smcp.computer.settings.scope import resolve_settings

    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_settings(
        _user_settings_path(tmp_path),
        {**_DECLARED_ENABLED, "extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "owner/repo"}}}},
    )
    # env 缺省 = 进程 os.environ（monkeypatch 已把 XDG_CONFIG_HOME 指到 tmp）；cwd 决定 project/local 层。
    resolved = resolve_settings(cwd=tmp_path)
    # 真实解析确实产出了错误（否则本用例对「携带」毫无判别力）
    assert any(e.field.startswith("extraKnownMarketplaces") for e in resolved.errors)

    async with Computer(name="t", skill_home=home) as comp:
        carried = await comp.reconcile_governance(declared=resolved)
        legacy = await comp.reconcile_governance(declared=dict(resolved.settings))

    assert [e.marketplace for e in carried.marketplace_declaration_errors] == ["acme"]
    assert legacy.marketplace_declaration_errors == []
    # 两条路径的恢复行为相同（errors 只是观测，不参与决策）
    assert carried.restored_skills == legacy.restored_skills
    assert carried.failed_marketplaces == legacy.failed_marketplaces == []


@pytest.mark.asyncio
async def test_reconcile_governance_explicit_resolved_settings_applies_project_trust_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293（用户 2026-11-22 裁决）：显式 ``ResolvedSettings`` **也**取 ``.per_scope_layers``。

    丢弃它会让 ``enabled_origin`` 恒 ``None`` ⇒ project-scope 激活的 bundled server **绕过** #159 Option B
    的 PENDING 信任门、被自动挂载（仓库入 git 的共享声明不得免批准）。传 ``.per_scope_layers`` 后与
    ``declared=None`` 路径同门（rust 早已如此）。
    判别力：若改回不取 per-scope 层，``mounted`` 会变成 ``[FIGMA_NAME]``、``pending_bundled_servers`` 为空。
    """
    from a2c_smcp.computer.settings.scope import resolve_settings

    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=[FIGMA_NAME])
    # user：安装意图 + 非法声明；project：声明启用 → 必须落 PENDING 而非直挂
    _write_settings(
        _user_settings_path(tmp_path),
        {"installedPlugins": ["audit@acme"], "extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "owner/repo"}}}},
    )
    _write_settings(tmp_path / ".tfrobot" / "settings.json", {"enabledPlugins": {"audit@acme": True}})
    resolved = resolve_settings(cwd=tmp_path)
    assert resolved.per_scope_layers[SettingsScope.PROJECT]["enabledPlugins"] == {"audit@acme": True}

    mounted: list[str] = []

    async def register(cfg, record: BundledServerRecord) -> None:
        mounted.append(cfg.name)

    async with Computer(name="t", skill_home=home) as comp:
        report = await comp.reconcile_governance(
            existing_bundle_ids=lambda: set(),
            register_server=register,
            declared=resolved,
        )

    # project-origin → PENDING 审批门 → 不挂载
    assert mounted == []
    assert report.remounted_servers == []
    assert [r.plugin_id for r in report.pending_bundled_servers] == ["audit@acme"]
    assert report.pending_bundled_servers[0].enabled_origin is SettingsScope.PROJECT
    # 同一次调用里声明错误照样被带出来
    assert [e.marketplace for e in report.marketplace_declaration_errors] == ["acme"]


@pytest.mark.asyncio
async def test_reconcile_governance_redacts_credential_marketplace_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#293 脱敏：非法市场名是凭据形态 URL 时，报告与日志都不得回显原始凭据。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    secret = "https://bob:t0ken@example.com/repo.git"
    settings_path = _user_settings_path(tmp_path)
    _write_settings(
        settings_path,
        {**_DECLARED_ENABLED, "extraKnownMarketplaces": {secret: {"source": {"type": "git", "url": "https://example.com/acme.git"}}}},
    )

    from a2c_smcp.computer.settings import recovery as recovery_mod

    captured: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    sink = _Sink(level=logging.DEBUG)
    # 钉住 ambient 级别：汇总/逐条分别是 WARNING / DEBUG，宿主设 A2C_SMCP_LOG_LEVEL=error 时都收不到。
    a2c_logger = logging.getLogger("a2c_smcp")
    prev_level = a2c_logger.level
    a2c_logger.setLevel(logging.DEBUG)
    recovery_mod.logger.addHandler(sink)
    try:
        async with Computer(name="t", skill_home=home) as comp:
            report = await comp.reconcile_governance()
    finally:
        recovery_mod.logger.removeHandler(sink)
        a2c_logger.setLevel(prev_level)

    # 正对照：落盘文件里确实是凭据形态的 key
    assert "t0ken" in settings_path.read_text(encoding="utf-8")

    assert len(report.marketplace_declaration_errors) == 1
    error = report.marketplace_declaration_errors[0]
    assert error.marketplace == "https://example.com/repo.git"
    for blob in (error.marketplace or "", error.field, error.reason, error.source_path or ""):
        assert "t0ken" not in blob and "bob" not in blob
    assert captured, "应当至少有一条声明错误日志"
    joined = "\n".join(captured)
    assert "t0ken" not in joined and "bob" not in joined


@pytest.mark.asyncio
async def test_boot_up_summary_counts_declaration_errors_when_zero_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """
    #293：``boot_up`` 汇总条件纳入 ``marketplace_declaration_errors``。

    夹具取「**零恢复** + 有声明错误」（installed 但 disabled ⇒ 全惰性静止态）：若汇总条件只看
    ``restored_skills`` / ``failed_marketplaces`` / 冲突，本用例就**没有任何汇总输出**。
    断言只认 boot_up 专属文案（``"%d skills restored"``），避免与 recovery WARN 汇总撞车导致终态断言失明。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_settings(
        _user_settings_path(tmp_path),
        {
            **_DECLARED_ENABLED,
            "enabledPlugins": {"audit@acme": False},
            "extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "owner/repo"}}},
        },
    )

    from a2c_smcp.computer import computer as computer_mod

    # 包级 logger 等级由 `A2C_SMCP_LOG_LEVEL` 在 import 时固定（默认 INFO）——显式钉住，避免宿主设成
    # error/warning 时本用例假红（那时 `logger.info` 连 record 都不生成）。
    a2c_logger = logging.getLogger("a2c_smcp")
    prev_level = a2c_logger.level
    a2c_logger.setLevel(logging.INFO)
    computer_mod.logger.addHandler(caplog.handler)
    try:
        caplog.set_level(logging.INFO)
        async with Computer(name="t", skill_home=home) as comp:
            report = await comp.reconcile_governance()
    finally:
        computer_mod.logger.removeHandler(caplog.handler)
        a2c_logger.setLevel(prev_level)

    assert report.restored_skills == [] and report.failed_marketplaces == []
    assert report.marketplace_source_conflicts == []
    assert len(report.marketplace_declaration_errors) == 1
    summaries = [r.getMessage() for r in caplog.records if "skills restored" in r.getMessage()]
    assert any(
        "0 skills restored" in message and "1 marketplace declaration errors" in message
        for message in summaries
    ), summaries


@pytest.mark.asyncio
async def test_boot_up_summary_not_triggered_by_unrelated_settings_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """#293 负对照：``enabledPlugins`` 非法键等**非** marketplace 子树错误不得计入、不得触发汇总。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, skills=["lint"])
    _write_settings(
        _user_settings_path(tmp_path),
        {**_DECLARED_ENABLED, "enabledPlugins": {"audit@acme": False, "BAD KEY": True}},
    )
    # 正对照：夹具确实产出了一条**非** marketplace 子树的 settings 校验错误（否则「无汇总」可能只是因为
    # 压根没有错误，本用例就成了空转）。
    from a2c_smcp.computer.settings.scope import resolve_settings

    fixture_errors = [e.field for e in resolve_settings(cwd=tmp_path).errors]
    assert fixture_errors == ["enabledPlugins.BAD KEY"], fixture_errors

    from a2c_smcp.computer import computer as computer_mod

    a2c_logger = logging.getLogger("a2c_smcp")
    prev_level = a2c_logger.level
    a2c_logger.setLevel(logging.INFO)  # 同上：钉住 ambient 级别
    computer_mod.logger.addHandler(caplog.handler)
    try:
        caplog.set_level(logging.INFO)
        async with Computer(name="t", skill_home=home) as comp:
            report = await comp.reconcile_governance()
    finally:
        computer_mod.logger.removeHandler(caplog.handler)
        a2c_logger.setLevel(prev_level)

    assert report.marketplace_declaration_errors == []
    assert report.restored_skills == [] and report.failed_marketplaces == []
    assert report.marketplace_source_conflicts == []
    summaries = [r.getMessage() for r in caplog.records if "skills restored" in r.getMessage()]
    assert summaries == [], summaries
