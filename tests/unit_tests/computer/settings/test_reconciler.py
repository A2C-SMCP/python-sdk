# -*- coding: utf-8 -*-
# filename: test_reconciler.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Reconciler 单元测试（v0.2.1 #62）—— additive-only 四分支 + gc/prune 决策逻辑（mock 掉 git staging）
Reconciler unit tests: additive-only four branches + gc/prune (stage_marketplace_skills mocked).

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §7.1/§7.2/§7.3。

测试意图 / Test intentions（不依赖 git——monkeypatch ``stage_marketplace_skills``，只验 reconciler 编排决策）:
- 四分支：missing→clone(refresh=False) / sourceChanged→wipe+reclone / autoUpdate→pull(refresh=True) /
  orphan(materialized∖declared)→**完全不动**（stage 不被调用、物化记录不变）；附 up_to_date / failed。
- plugin_filter = installed ∧ ``enabledPlugins[id] is True``（v0.3.0 §4.8.1，#123）且属本 marketplace。
- 声明视图提取过滤非法 marketplace 名 / 非对象条目 / 非法 pid 条目。
- 孤儿清理：list/prune marketplace（clone 树 + 外部 plugin 树 + 物化条目 + Registry SKILL）；
  list/gc plugin（孤儿 = 账本 pid ∉ ``installedPlugins``；installPath + 物化条目 + Registry SKILL +
  bundled MCP 回调）；installPath 越界守卫。
"""

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.settings.reconciler import (
    ReconcileReport,
    declared_installed_plugin_ids,
    declared_marketplace_names,
    gc_plugins,
    list_orphan_marketplaces,
    list_orphan_plugins,
    prune_marketplaces,
    reconcile,
)
from a2c_smcp.computer.settings.store import (
    load_installed_plugins,
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
)
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry

_SRC_A = {"type": "git", "url": "https://example.com/team/a.git"}
_SRC_B = {"type": "git", "url": "https://example.com/team/b.git"}
_RECONCILER = "a2c_smcp.computer.settings.reconciler.stage_marketplace_skills"


# ── 辅助 / helpers ───────────────────────────────────────────────────────────
def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _ext_dir(home: Path, mp: str) -> Path:
    return home / SOURCE_MARKETPLACE / ".plugins" / mp


def _make_clone(home: Path, mp: str, *, sentinel: bool = True) -> Path:
    """造一个假 clone 树（含 sentinel 文件用于断言 wipe/保留）/ Fake clone tree with a sentinel file."""
    d = marketplace_skill_dir(home, mp)
    d.mkdir(parents=True, exist_ok=True)
    if sentinel:
        (d / "SENTINEL").write_text("x", encoding="utf-8")
    return d


def _seed_known(home: Path, records: dict[str, dict]) -> None:
    data = {
        "version": 1,
        "marketplaces": {
            mp: {"source": src, "installLocation": str(marketplace_skill_dir(home, mp).resolve())} for mp, src in records.items()
        },
    }
    save_known_marketplaces(data, home=home)


def _reg_skill(reg: SkillRegistry, name: str, marketplace: str, path: Path) -> None:
    reg.register({"name": name, "source": f"{SOURCE_MARKETPLACE}:{marketplace}", "path": str(path.resolve())})


def _fake_stage(
    calls: list[dict[str, Any]],
    *,
    succeed: bool = True,
    skills: list[str] | None = None,
) -> Callable[..., Awaitable[list[str]]]:
    """替身：记录入参；``succeed`` 时 mkdir clone 树（模拟 clone 成功）并返回 ``skills``，否则返回空。"""

    async def _stage(
        name: str,
        source: Mapping[str, Any],
        registry: SkillRegistry,
        home: Path,
        *,
        plugin_filter: set[str] | None = None,
        auto_update: bool = False,
        refresh: bool = False,
        timeout: float = 0.0,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        calls.append(
            {"name": name, "source": source, "plugin_filter": plugin_filter, "auto_update": auto_update, "refresh": refresh}
        )
        if succeed:
            marketplace_skill_dir(home, name).mkdir(parents=True, exist_ok=True)
            return list(skills or [])
        return []

    return _stage


# ── 四分支 / four branches ───────────────────────────────────────────────────
async def test_reconcile_missing_clones(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls, skills=["audit:lint"]))
    declared = {
        "extraKnownMarketplaces": {"acme": {"source": _SRC_A}},
        "installedPlugins": ["audit@acme"],
        "enabledPlugins": {"audit@acme": True},
    }

    report = await reconcile(SkillRegistry(), home, declared)

    assert isinstance(report, ReconcileReport)
    assert report.installed == ["acme"]
    assert report.updated == [] and report.up_to_date == [] and report.failed == []
    assert report.registered_skills == ["audit:lint"]
    assert calls[0]["refresh"] is False
    assert calls[0]["plugin_filter"] == {"audit"}


async def test_reconcile_source_changed_wipes_and_reclones(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    clone = _make_clone(home, "acme")  # 旧 clone + sentinel
    ext = _ext_dir(home, "acme")  # 外部 plugin 树（git-subdir / standalone url plugin 落点）
    ext.mkdir(parents=True)
    (ext / "plug").mkdir()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))
    declared = {"extraKnownMarketplaces": {"acme": {"source": _SRC_B}}}  # 源已变更

    report = await reconcile(SkillRegistry(), home, declared)

    assert report.updated == ["acme"]
    assert not (clone / "SENTINEL").exists()  # 旧 catalog clone 被 wipe
    assert not ext.exists()  # 外部 plugin 树同样被 wipe（sourceChanged 清两处）
    assert clone.exists()  # mock 重新 clone
    assert calls[0]["source"] == _SRC_B


async def test_reconcile_autoupdate_pulls_without_wipe(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    clone = _make_clone(home, "acme")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))
    declared = {"extraKnownMarketplaces": {"acme": {"source": _SRC_A, "autoUpdate": True}}}

    report = await reconcile(SkillRegistry(), home, declared)

    assert report.updated == ["acme"]
    assert calls[0]["refresh"] is True
    assert calls[0]["auto_update"] is True
    assert (clone / "SENTINEL").exists()  # 未 wipe（autoUpdate 走 pull）


async def test_reconcile_up_to_date_no_refresh(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    _make_clone(home, "acme")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))
    declared = {"extraKnownMarketplaces": {"acme": {"source": _SRC_A}}}

    report = await reconcile(SkillRegistry(), home, declared)

    assert report.up_to_date == ["acme"]
    assert calls[0]["refresh"] is False


async def test_reconcile_explicit_refresh_pulls_existing(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    _make_clone(home, "acme")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))
    declared = {"extraKnownMarketplaces": {"acme": {"source": _SRC_A}}}

    report = await reconcile(SkillRegistry(), home, declared, refresh=True)

    assert report.updated == ["acme"]
    assert calls[0]["refresh"] is True


async def test_reconcile_orphan_left_untouched(tmp_path: Path, monkeypatch) -> None:
    """materialized∖declared：不进循环、stage 不被调用、物化记录 + clone 树 + Registry 全不动。"""
    home = _home(tmp_path)
    _seed_known(home, {"legacy": _SRC_A})
    clone = _make_clone(home, "legacy")
    reg = SkillRegistry()
    _reg_skill(reg, "leg:tool", "legacy", clone)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))

    report = await reconcile(reg, home, {"extraKnownMarketplaces": {}})  # 不再声明 legacy

    assert calls == []  # stage 完全未被调用
    assert report.installed == [] and report.updated == [] and report.failed == []
    assert (clone / "SENTINEL").exists()  # clone 树不动
    assert "legacy" in load_known_marketplaces(home=home)["marketplaces"]  # 物化记录不动
    assert reg.resolve("leg:tool") is not None  # Registry 不动


async def test_reconcile_failed_when_clone_absent_after_stage(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls, succeed=False))  # 模拟 clone 失败（不建目录）
    declared = {"extraKnownMarketplaces": {"acme": {"source": _SRC_A}}}

    report = await reconcile(SkillRegistry(), home, declared)

    assert report.failed == ["acme"]
    assert report.installed == [] and report.updated == []


async def test_reconcile_plugin_filter_installed_true_and_same_marketplace(tmp_path: Path, monkeypatch) -> None:
    """plugin_filter = installed ∧ true ∧ 本 marketplace（v0.3.0：enabled 但未安装的 ghost 不入）。"""
    home = _home(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))
    declared = {
        "extraKnownMarketplaces": {"acme": {"source": _SRC_A}},
        "installedPlugins": ["audit@acme", "fmt@acme", "x@other"],
        "enabledPlugins": {
            "audit@acme": True,  # installed ∧ 启用、本 mp → 入
            "fmt@acme": False,  # installed 但禁用 → 不入
            "x@other": True,  # installed ∧ 启用但别的 mp → 不入
            "ghost@acme": True,  # 启用但**未安装** → 不入（活跃集 = installed ∧ enabled）
            "bad-key-no-at": True,  # 非法 key → 忽略
        },
    }

    await reconcile(SkillRegistry(), home, declared)

    assert calls[0]["plugin_filter"] == {"audit"}


# ── 声明视图提取 / declared-view extraction ──────────────────────────────────
def test_declared_marketplace_names_filters_invalid() -> None:
    declared = {
        "extraKnownMarketplaces": {
            "good-mp": {"source": _SRC_A},
            "bad/name": {"source": _SRC_A},  # 非 kebab → 滤掉
            "not-object": "oops",  # 非对象 → 滤掉
        }
    }
    assert declared_marketplace_names(declared) == {"good-mp"}


def test_declared_installed_plugin_ids_filters_invalid() -> None:
    declared = {"installedPlugins": ["audit@acme", "disabled@acme", "no-at-sign", 42]}
    # 安装意图集与 enablement 正交（禁用项仍"已安装"）；滤掉非法形态 / 非字符串条目
    assert declared_installed_plugin_ids(declared) == {"audit@acme", "disabled@acme"}


def test_declared_helpers_handle_missing_keys() -> None:
    assert declared_marketplace_names({}) == set()
    assert declared_installed_plugin_ids({}) == set()
    assert declared_installed_plugin_ids({"installedPlugins": "not-a-list"}) == set()


# ── marketplace prune ───────────────────────────────────────────────────────
def test_list_and_prune_marketplaces(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A, "legacy": _SRC_B})
    acme_clone = _make_clone(home, "acme")
    legacy_clone = _make_clone(home, "legacy")
    _ext_dir(home, "legacy").mkdir(parents=True, exist_ok=True)
    (_ext_dir(home, "legacy") / "plug").mkdir()
    reg = SkillRegistry()
    _reg_skill(reg, "audit:lint", "acme", acme_clone)
    _reg_skill(reg, "leg:tool", "legacy", legacy_clone)
    declared = {"extraKnownMarketplaces": {"acme": {"source": _SRC_A}}}  # legacy 不再声明

    assert list_orphan_marketplaces(home, declared) == ["legacy"]

    removed = prune_marketplaces(["legacy"], reg, home)

    assert removed == ["legacy"]
    assert not legacy_clone.exists()  # clone 树清除
    assert not _ext_dir(home, "legacy").exists()  # 外部 plugin 树清除
    kmf = load_known_marketplaces(home=home)["marketplaces"]
    assert "legacy" not in kmf and "acme" in kmf  # 物化条目删除、acme 保留
    assert reg.resolve("leg:tool") is None  # 孤儿 SKILL 注销
    assert reg.resolve("audit:lint") is not None  # acme SKILL 保留
    assert acme_clone.exists()  # acme clone 树不动


def test_prune_skips_invalid_marketplace_name(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    removed = prune_marketplaces(["../escape"], SkillRegistry(), home)
    assert removed == []
    assert "acme" in load_known_marketplaces(home=home)["marketplaces"]


# ── plugin gc ────────────────────────────────────────────────────────────────
def _seed_installed(home: Path, plugins: dict[str, list[dict]]) -> None:
    save_installed_plugins({"version": 1, "plugins": plugins}, home=home)


@pytest.fixture(autouse=True)
def _isolate_cwd_and_user_config(tmp_path: Path, monkeypatch) -> None:
    """
    隔离 cwd + user config / Isolate cwd and user config。

    #153 起 :func:`gc_plugins` 经回收判据（``mcp_json_declared_bundle_ids`` → ``resolve_mcp_config``）读
    **cwd 锚定**的 ``.tfrobot/mcp[.local].json``（project/local scope，#116）与 user scope mcp.json。
    不隔离则读进真实仓库 / 开发者 home——本地一旦存在这些文件，断言即随环境漂移（#137 同款教训；本文件的
    泄漏由 #153 隔离审查 🟡 实测发现）。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


async def test_list_and_gc_plugins(tmp_path: Path) -> None:
    home = _home(tmp_path)
    audit_path = marketplace_skill_dir(home, "acme") / "audit"
    keep_path = marketplace_skill_dir(home, "acme") / "keep"
    audit_path.mkdir(parents=True)
    keep_path.mkdir(parents=True)
    _seed_installed(
        home,
        {
            "audit@acme": [{"scope": "user", "installPath": str(audit_path), "mcpServers": ["figma", "blender"]}],
            "keep@acme": [{"scope": "user", "installPath": str(keep_path)}],
        },
    )
    reg = SkillRegistry()
    _reg_skill(reg, "audit:lint", "acme", audit_path)
    _reg_skill(reg, "keep:do", "acme", keep_path)
    # v0.3.0：孤儿 = 账本 pid ∉ installedPlugins（enablement 正交、不参与判定）→ audit 不在安装意图 → 孤儿
    declared = {"installedPlugins": ["keep@acme"], "enabledPlugins": {"keep@acme": True}}

    assert list_orphan_plugins(home, declared) == ["audit@acme"]

    teardown: list[list[str]] = []

    async def _cb(servers: list[str]) -> None:
        teardown.append(servers)

    removed = await gc_plugins(["audit@acme"], reg, home, mcp_teardown=_cb)

    assert removed == ["audit@acme"]
    assert not audit_path.exists()  # installPath 树清除
    assert keep_path.exists()  # 保留
    ipf = load_installed_plugins(home=home)["plugins"]
    assert "audit@acme" not in ipf and "keep@acme" in ipf
    assert reg.resolve("audit:lint") is None  # 孤儿 plugin 的 SKILL 注销
    assert reg.resolve("keep:do") is not None  # 保留 plugin 的 SKILL 不动
    assert teardown == [["blender", "figma"]]  # 可回收的 MCP 依赖经回调下线（#153：判据过滤后 sorted，确定序）


async def test_gc_guards_installpath_outside_home(tmp_path: Path) -> None:
    home = _home(tmp_path)
    outside = tmp_path / "outside-home"  # 故意落在 SKILL Home 之外
    outside.mkdir()
    (outside / "keepme").write_text("x", encoding="utf-8")
    _seed_installed(home, {"evil@acme": [{"scope": "user", "installPath": str(outside)}]})

    removed = await gc_plugins(["evil@acme"], SkillRegistry(), home)

    assert removed == ["evil@acme"]
    assert outside.exists() and (outside / "keepme").exists()  # 越界守卫：拒删盘外目录
    assert "evil@acme" not in load_installed_plugins(home=home)["plugins"]  # 物化条目仍清除


async def test_gc_without_teardown_callback(tmp_path: Path) -> None:
    """无 mcp_teardown 回调时不报错、照常清理 / No callback → still cleans, no error。"""
    home = _home(tmp_path)
    p = marketplace_skill_dir(home, "acme") / "audit"
    p.mkdir(parents=True)
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(p), "mcpServers": ["x"]}]})

    removed = await gc_plugins(["audit@acme"], SkillRegistry(), home, mcp_teardown=None)

    assert removed == ["audit@acme"]
    assert not p.exists()


# ── 失败降级 / 防御兜底分支 / failure-degradation & defensive branches ────────
def test_safe_rmtree_oserror_does_not_abort_prune(tmp_path: Path, monkeypatch) -> None:
    """``_safe_rmtree`` 删除受阻（OSError）→ 记 WARN 降级、prune 不中断（「失败不阻断」铁律）。"""
    import a2c_smcp.computer.settings.reconciler as rec_mod

    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    _make_clone(home, "acme")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("disk busy")

    monkeypatch.setattr(rec_mod.shutil, "rmtree", _boom)

    removed = prune_marketplaces(["acme"], SkillRegistry(), home)

    # rmtree 抛错被降级吞掉，prune 仍走完：物化条目照常删除、不向上抛
    assert removed == ["acme"]
    assert "acme" not in load_known_marketplaces(home=home)["marketplaces"]


async def test_gc_skips_unknown_plugin_id(tmp_path: Path) -> None:
    """gc 入参含 installed_plugins.json 里不存在的 pid → 跳过（``records is None`` 守卫），不计入 removed。"""
    home = _home(tmp_path)
    _seed_installed(home, {"real@acme": [{"scope": "user", "installPath": str(marketplace_skill_dir(home, "acme"))}]})

    removed = await gc_plugins(["ghost@acme"], SkillRegistry(), home)

    assert removed == []
    assert "real@acme" in load_installed_plugins(home=home)["plugins"]  # 已存在项不受影响


async def test_gc_plugin_id_without_at_sign(tmp_path: Path) -> None:
    """畸形 pid（无 ``@``）→ 不做 SKILL 注销（``if marketplace:`` 假分支）、仍清 installPath + 账本，不崩。"""
    home = _home(tmp_path)
    p = marketplace_skill_dir(home, "x") / "noat"
    p.mkdir(parents=True)
    _seed_installed(home, {"noatsign": [{"scope": "user", "installPath": str(p)}]})

    removed = await gc_plugins(["noatsign"], SkillRegistry(), home)

    assert removed == ["noatsign"]
    assert not p.exists()
    assert "noatsign" not in load_installed_plugins(home=home)["plugins"]


# ── #125 任务 2/4：悬挂意图检测 + 账本失效判据（红灯阶段函数内 import 定位失败粒度）──
def _write_manifest(home: Path, mp: str, plugins: list[str]) -> Path:
    """造 catalog clone + marketplace.json（entry 集合可控，供 dangling 判据分档）。"""
    import json

    catalog = marketplace_skill_dir(home, mp)
    p = catalog / ".tfrobot-plugin" / "marketplace.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": mp,
        "owner": {"name": "X"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": pl, "source": pl, "version": "1.0.0"} for pl in plugins],
    }
    p.write_text(json.dumps(manifest), encoding="utf-8")
    return catalog


def test_list_dangling_marketplace_not_added(tmp_path: Path) -> None:
    """意图有、账本无、marketplace 不在 known_marketplaces → 最强 prune 信号（无自愈路径）。"""
    from a2c_smcp.computer.settings.reconciler import DANGLING_MARKETPLACE_NOT_ADDED, list_dangling_plugin_intents

    home = _home(tmp_path)
    declared = {"installedPlugins": ["audit@acme"]}

    assert list_dangling_plugin_intents(home, declared) == [("audit@acme", DANGLING_MARKETPLACE_NOT_ADDED)]


def test_list_dangling_catalog_missing(tmp_path: Path) -> None:
    """known 记录在、catalog clone 缺失 → 列入但 reason 分档（boot/refresh 可能自愈，裁量留给 confirm）。"""
    from a2c_smcp.computer.settings.reconciler import DANGLING_CATALOG_MISSING, list_dangling_plugin_intents

    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})  # 不建 clone 树
    declared = {"installedPlugins": ["audit@acme"]}

    assert list_dangling_plugin_intents(home, declared) == [("audit@acme", DANGLING_CATALOG_MISSING)]


def test_list_dangling_manifest_unreadable_and_entry_missing(tmp_path: Path) -> None:
    """clone 在但 manifest 损坏 → manifest-unreadable；manifest 合法但无 entry → entry-missing。"""
    from a2c_smcp.computer.settings.reconciler import (
        DANGLING_ENTRY_MISSING,
        DANGLING_MANIFEST_UNREADABLE,
        list_dangling_plugin_intents,
    )

    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A, "beta": _SRC_B})
    # acme：clone 在、manifest 畸形
    bad = marketplace_skill_dir(home, "acme") / ".tfrobot-plugin" / "marketplace.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    # beta：manifest 合法但不含 audit
    _write_manifest(home, "beta", ["other"])
    declared = {"installedPlugins": ["audit@acme", "audit@beta"]}

    assert list_dangling_plugin_intents(home, declared) == [
        ("audit@acme", DANGLING_MANIFEST_UNREADABLE),
        ("audit@beta", DANGLING_ENTRY_MISSING),
    ]


def test_list_dangling_excludes_materialized_and_recoverable(tmp_path: Path) -> None:
    """活账本 pid 与「静态可达但未物化」pid 均不列（后者下次 boot 自愈，不是 prune 对象）。"""
    from a2c_smcp.computer.settings.reconciler import list_dangling_plugin_intents

    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    _write_manifest(home, "acme", ["audit", "keep"])
    keep_path = marketplace_skill_dir(home, "acme") / "plugins" / "keep"
    keep_path.mkdir(parents=True)
    _seed_installed(home, {"keep@acme": [{"scope": "user", "installPath": str(keep_path)}]})
    declared = {"installedPlugins": ["keep@acme", "audit@acme"]}  # audit：无账本但三查全过 → recoverable

    assert list_dangling_plugin_intents(home, declared) == []


def test_list_dangling_covers_all_dead_installpath_records(tmp_path: Path) -> None:
    """有账本记录但 installPath 全死 ∧ 静态不可达 → 仍列入（账本残骸不挡诊断）。"""
    from a2c_smcp.computer.settings.reconciler import DANGLING_MARKETPLACE_NOT_ADDED, list_dangling_plugin_intents

    home = _home(tmp_path)
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(tmp_path / "gone")}]})
    declared = {"installedPlugins": ["audit@acme"]}

    assert list_dangling_plugin_intents(home, declared) == [("audit@acme", DANGLING_MARKETPLACE_NOT_ADDED)]


def test_ledger_entry_materialized_rejects_corrupt_bundled_json(tmp_path: Path) -> None:
    """#125 任务 4：判据 = 目录在 ∧ bundled JSON 可解析；「目录在、JSON 损坏」→ False（旧判据误判 True 的半态触发面）。"""
    from a2c_smcp.computer.settings.reconciler import ledger_entry_materialized

    corrupt_root = tmp_path / "corrupt"
    (corrupt_root / "mcp-servers").mkdir(parents=True)
    (corrupt_root / "mcp-servers" / "bad.json").write_text("{not json", encoding="utf-8")
    ok_root = tmp_path / "ok"
    ok_root.mkdir()

    assert ledger_entry_materialized([{"scope": "user", "installPath": str(corrupt_root)}]) is False
    assert ledger_entry_materialized([{"scope": "user", "installPath": str(ok_root)}]) is True  # 无 bundled server 合法
    assert ledger_entry_materialized([{"scope": "user", "installPath": str(tmp_path / "gone")}]) is False  # 目录缺失
    assert ledger_entry_materialized(None) is False  # 非 list 防御
