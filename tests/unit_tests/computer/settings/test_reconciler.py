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
- plugin_filter 由 ``enabledPlugins`` 推导（仅 ``true`` 且属本 marketplace 的 plugin）。
- 声明视图提取过滤非法 marketplace 名 / 非对象条目 / 非法 plugin key。
- 孤儿清理：list/prune marketplace（clone 树 + 外部 plugin 树 + 物化条目 + Registry SKILL）；
  list/gc plugin（installPath + 物化条目 + Registry SKILL + bundled MCP 回调）；installPath 越界守卫。
"""

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from a2c_smcp.computer.settings.reconciler import (
    ReconcileReport,
    declared_marketplace_names,
    declared_plugin_ids,
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
    declared = {"extraKnownMarketplaces": {"acme": {"source": _SRC_A}}, "enabledPlugins": {"audit@acme": True}}

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


async def test_reconcile_plugin_filter_only_true_and_same_marketplace(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))
    declared = {
        "extraKnownMarketplaces": {"acme": {"source": _SRC_A}},
        "enabledPlugins": {
            "audit@acme": True,  # 启用、本 mp → 入
            "fmt@acme": False,  # 禁用 → 不入
            "x@other": True,  # 启用但别的 mp → 不入
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


def test_declared_plugin_ids_filters_invalid() -> None:
    declared = {"enabledPlugins": {"audit@acme": True, "disabled@acme": False, "no-at-sign": True}}
    # 含 false（声明禁用，仍是"已声明"）；滤掉非法 key
    assert declared_plugin_ids(declared) == {"audit@acme", "disabled@acme"}


def test_declared_helpers_handle_missing_keys() -> None:
    assert declared_marketplace_names({}) == set()
    assert declared_plugin_ids({}) == set()


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


async def test_list_and_gc_plugins(tmp_path: Path) -> None:
    home = _home(tmp_path)
    audit_path = marketplace_skill_dir(home, "acme") / "audit"
    keep_path = marketplace_skill_dir(home, "acme") / "keep"
    audit_path.mkdir(parents=True)
    keep_path.mkdir(parents=True)
    _seed_installed(
        home,
        {
            "audit@acme": [{"scope": "user", "installPath": str(audit_path), "bundledMcpServers": ["figma", "blender"]}],
            "keep@acme": [{"scope": "user", "installPath": str(keep_path)}],
        },
    )
    reg = SkillRegistry()
    _reg_skill(reg, "audit:lint", "acme", audit_path)
    _reg_skill(reg, "keep:do", "acme", keep_path)
    declared = {"enabledPlugins": {"keep@acme": True}}  # audit 不再声明 → 孤儿

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
    assert teardown == [["figma", "blender"]]  # bundled MCP 经回调下线


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
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(p), "bundledMcpServers": ["x"]}]})

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
