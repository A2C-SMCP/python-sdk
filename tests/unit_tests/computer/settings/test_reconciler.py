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
    InstallPathRemovalError,
    ReconcileReport,
    declared_installed_plugin_ids,
    declared_marketplace_names,
    gc_plugins,
    is_external_plugin_install_path,
    list_orphan_marketplaces,
    list_orphan_plugins,
    prune_marketplaces,
    reconcile,
    remove_plugin_install_path,
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


async def test_reconcile_source_change_rejected_without_wipe(tmp_path: Path, monkeypatch) -> None:
    """
    #260 / §5.14：声明式**换源** MUST 以 ``validation`` 拒绝——不 wipe、不重 clone、不 stage，
    既有注册/内容/能力原样保留；显式 remove / re-add 才是换源路径。
    """
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

    assert report.validation == {"acme": "source-conflict"}
    assert report.updated == [] and report.installed == [] and report.failed == []
    assert calls == [], "换源被拒时不得进入 stage/重 clone"
    assert (clone / "SENTINEL").exists(), "旧 catalog clone 不得被 wipe"
    assert ext.exists() and (ext / "plug").exists(), "外部 plugin 树同样不得被 wipe"
    assert load_known_marketplaces(home=home)["marketplaces"]["acme"]["source"] == _SRC_A, "账本 source 不得被改"


async def test_reconcile_source_change_does_not_touch_symlinked_clone(tmp_path: Path, monkeypatch) -> None:
    """换源拒绝路径**完全不碰目录**：即便旧 clone 是软链（旧实现 wipe 失败会报 failed）也不做删除尝试。"""
    home = _home(tmp_path)
    real = marketplace_skill_dir(home, "other") / "plugins"
    sentinel = real / "sentinel"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("x", encoding="utf-8")
    link = marketplace_skill_dir(home, "acme")
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    _seed_known(home, {"acme": _SRC_A})
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))
    reg = SkillRegistry()
    _reg_skill(reg, "audit:lint", "acme", link)

    report = await reconcile(reg, home, {"extraKnownMarketplaces": {"acme": {"source": _SRC_B}}})

    assert report.validation == {"acme": "source-conflict"}
    assert report.failed == [] and report.updated == [] and report.installed == []
    assert calls == []
    assert link.is_symlink() and sentinel.exists(), "拒绝路径不得尝试删除（链接与目标均保留）"
    assert reg.resolve("audit:lint") is not None, "既有能力保持 active"


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
    # #258：audit 用 plugin 独占外部 clone（可删）；keep 用 catalog 内相对源落点（共享缓存，MUST 保留）。
    audit_path = _ext_dir(home, "acme") / "audit"
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

    removed = await gc_plugins(["audit@acme"], reg, home, non_plugin_bundle_ids=lambda: set(), mcp_teardown=_cb)

    assert removed == ["audit@acme"]
    assert not audit_path.exists()  # #258：plugin 独占外部 clone 删除
    assert keep_path.exists()  # #258：catalog 内共享 installPath 保留（marketplace 级缓存）
    ipf = load_installed_plugins(home=home)["plugins"]
    assert "audit@acme" not in ipf and "keep@acme" in ipf
    assert reg.resolve("audit:lint") is None  # 孤儿 plugin 的 SKILL 注销
    assert reg.resolve("keep:do") is not None  # 保留 plugin 的 SKILL 不动
    assert teardown == [["blender", "figma"]]  # 可回收的 MCP 依赖经回调下线（#153：判据过滤后 sorted，确定序）


def test_lexical_ownership_with_symlinked_install_path_component_258(tmp_path: Path) -> None:
    """#258 架构师 🔴：installPath 链上含软链组件（git-subdir 仓库可携带）→ 仍判为 clone 内（纯词法，不 resolve）。"""
    home = _home(tmp_path)
    ext = _ext_dir(home, "acme") / "audit"
    (ext / "pkg").mkdir(parents=True)
    target = tmp_path / "target"
    (target / "sub").mkdir(parents=True)
    (target / "keep").write_text("x", encoding="utf-8")
    (ext / "pkg").rmdir()
    try:
        (ext / "pkg").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    install_path = ext / "pkg" / "sub"

    assert is_external_plugin_install_path(install_path, home, "acme", "audit") is True, "词法命中，不受软链解析影响"
    assert remove_plugin_install_path(install_path, home, "acme", "audit") is True
    assert not ext.exists(), "删整个独占 clone 根"
    assert (target / "sub").exists() and (target / "keep").exists(), "不得跟随 installPath 软链删外部目标"


async def test_reconcile_source_change_never_calls_rmtree(tmp_path: Path, monkeypatch) -> None:
    """拒绝路径的机制断言：``reconcile`` 在换源分支**不得**调用任何 rmtree（旧实现 wipe 的承重墙）。"""
    import a2c_smcp.computer.settings.reconciler as rec_mod

    home = _home(tmp_path)
    _make_clone(home, "acme")
    _seed_known(home, {"acme": _SRC_A})
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(_RECONCILER, _fake_stage(calls))

    rmtree_calls: list[object] = []

    def _record(*args: object, **kwargs: object) -> None:
        rmtree_calls.append(args)
        raise OSError("must not be called")

    monkeypatch.setattr(rec_mod.shutil, "rmtree", _record)

    report = await reconcile(SkillRegistry(), home, {"extraKnownMarketplaces": {"acme": {"source": _SRC_B}}})

    assert report.validation == {"acme": "source-conflict"}
    assert rmtree_calls == [], "换源拒绝不得尝试删除任何目录"
    assert calls == []


async def test_gc_keeps_installpath_outside_home(tmp_path: Path) -> None:
    home = _home(tmp_path)
    outside = tmp_path / "outside-home"  # 故意落在 SKILL Home 之外
    outside.mkdir()
    (outside / "keepme").write_text("x", encoding="utf-8")
    _seed_installed(home, {"evil@acme": [{"scope": "user", "installPath": str(outside)}]})

    removed = await gc_plugins(["evil@acme"], SkillRegistry(), home, non_plugin_bundle_ids=lambda: set())

    assert removed == ["evil@acme"]
    # #258：盘外路径先被归属谓词拒绝，根本不进入 _safe_rmtree 的 canonicalize / Home 越界守卫。
    assert outside.exists() and (outside / "keepme").exists()
    assert "evil@acme" not in load_installed_plugins(home=home)["plugins"]  # 物化条目仍清除


async def test_gc_without_teardown_callback(tmp_path: Path) -> None:
    """无 mcp_teardown 回调时不报错、照常清理 / No callback → still cleans, no error。"""
    home = _home(tmp_path)
    p = _ext_dir(home, "acme") / "audit"  # #258：外部 clone 才在删除范围
    p.mkdir(parents=True)
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(p), "mcpServers": ["x"]}]})

    removed = await gc_plugins(["audit@acme"], SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), mcp_teardown=None)

    assert removed == ["audit@acme"]
    assert not p.exists()


# ── 失败降级 / 防御兜底分支 / failure-degradation & defensive branches ────────
def test_prune_oserror_keeps_known_entry_258(tmp_path: Path, monkeypatch) -> None:
    """#258 复审 🔴1：``_safe_rmtree`` 删除受阻（OSError）→ 保留 known 条目、不计入 removed、不向上抛。"""
    import a2c_smcp.computer.settings.reconciler as rec_mod

    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    _make_clone(home, "acme")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("disk busy")

    monkeypatch.setattr(rec_mod.shutil, "rmtree", _boom)

    removed = prune_marketplaces(["acme"], SkillRegistry(), home)

    # 未真正清空 ⇒ 不得删 known 条目（否则残留树脱离 orphan 发现面、永久不可清理），也不得报成功。
    assert removed == []
    assert "acme" in load_known_marketplaces(home=home)["marketplaces"]


async def test_gc_skips_unknown_plugin_id(tmp_path: Path) -> None:
    """gc 入参含 installed_plugins.json 里不存在的 pid → 跳过（``records is None`` 守卫），不计入 removed。"""
    home = _home(tmp_path)
    _seed_installed(home, {"real@acme": [{"scope": "user", "installPath": str(marketplace_skill_dir(home, "acme"))}]})

    removed = await gc_plugins(["ghost@acme"], SkillRegistry(), home, non_plugin_bundle_ids=lambda: set())

    assert removed == []
    assert "real@acme" in load_installed_plugins(home=home)["plugins"]  # 已存在项不受影响


async def test_gc_plugin_id_without_at_sign(tmp_path: Path) -> None:
    """畸形 pid（无 ``@``）→ 不做 SKILL 注销、**无法识别独占 clone ⇒ 文件保守保留**、账本照删，不崩。"""
    home = _home(tmp_path)
    p = marketplace_skill_dir(home, "x") / "noat"
    p.mkdir(parents=True)
    _seed_installed(home, {"noatsign": [{"scope": "user", "installPath": str(p)}]})

    removed = await gc_plugins(["noatsign"], SkillRegistry(), home, non_plugin_bundle_ids=lambda: set())

    assert removed == ["noatsign"]
    assert p.exists()  # #258：无 marketplace 段 → 无法确认独占归属，漏删优于误删
    assert "noatsign" not in load_installed_plugins(home=home)["plugins"]


def test_remove_plugin_install_path_boundaries_258(tmp_path: Path) -> None:
    """#258：删除谓词边界——仅 plugin 独占外部 clone（含 ``git-subdir`` 后代）可删；catalog / 兄弟 / 盘外 / 非法名保留。"""
    home = _home(tmp_path)
    ext_root = _ext_dir(home, "acme") / "audit"
    catalog = marketplace_skill_dir(home, "acme") / "plugins" / "audit"
    elsewhere = home / "elsewhere"
    sibling = _ext_dir(home, "acme") / "audit2"
    for d in (ext_root, catalog, elsewhere, sibling):
        d.mkdir(parents=True, exist_ok=True)

    # 保留（``None`` = 非独占路径，按设计保留）：共享 catalog / 盘外 / 兄弟 plugin / 父命名空间 /
    # 空与相对路径 / 非法 marketplace·plugin 名。
    assert remove_plugin_install_path(catalog, home, "acme", "audit") is None
    assert remove_plugin_install_path(elsewhere, home, "acme", "audit") is None
    assert remove_plugin_install_path(sibling, home, "acme", "audit") is None
    assert remove_plugin_install_path(_ext_dir(home, "acme"), home, "acme", "audit") is None
    assert remove_plugin_install_path(Path(""), home, "acme", "audit") is None
    assert remove_plugin_install_path(Path("relative/path"), home, "acme", "audit") is None
    for mp, plugin in (("", "audit"), ("..", "audit"), ("a/b", "audit"), ("Acme", "audit"), ("acme", "..")):
        assert remove_plugin_install_path(ext_root, home, mp, plugin) is None
    # 纯词法归一后落在根外 → None；NUL / 相对等无法识别的路径 → 归属判定 False（不 resolve）。
    assert remove_plugin_install_path(ext_root / ".." / "audit2", home, "acme", "audit") is None
    assert is_external_plugin_install_path(Path("/tmp/a\u0000b"), home, "acme", "audit") is False
    assert ext_root.exists() and catalog.exists() and sibling.exists()

    # 删除：``git-subdir`` 后代 installPath → 删整个独占 clone 根，catalog 不受影响。
    sub = ext_root / "pkg" / "sub"
    sub.mkdir(parents=True)
    assert remove_plugin_install_path(sub, home, "acme", "audit") is True
    assert not ext_root.exists()
    assert catalog.exists()

    # 删除：clone 根本身；路径缺失 → 词法命中、安全 no-op（不崩）。
    ext_root.mkdir(parents=True)
    assert remove_plugin_install_path(ext_root, home, "acme", "audit") is True
    assert not ext_root.exists()
    assert remove_plugin_install_path(ext_root, home, "acme", "audit") is True


def test_remove_plugin_install_path_refuses_symlinked_external_root_258(tmp_path: Path) -> None:
    """#258 🟡3：ext root 被软链重定向（如指向 Home 内共享 catalog）→ 拒绝删除，链接目标完好。"""
    home = _home(tmp_path)
    catalog = marketplace_skill_dir(home, "acme") / "plugins"
    sentinel = catalog / "sentinel" / "SKILL.md"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("x", encoding="utf-8")
    ext_root = _ext_dir(home, "acme") / "audit"
    ext_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        ext_root.symlink_to(catalog, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")

    assert is_external_plugin_install_path(ext_root, home, "acme", "audit") is True  # 纯词法命中 ext root
    with pytest.raises(InstallPathRemovalError, match="symlinked external clone"):
        remove_plugin_install_path(ext_root, home, "acme", "audit")  # 软链根拒删（消息含路径与原因）
    assert catalog.exists() and sentinel.exists(), "链接目标（共享 catalog）不得被删"
    assert ext_root.is_symlink()


def test_remove_plugin_install_path_deletes_under_symlinked_home_258(tmp_path: Path) -> None:
    """#258 复审 🟡4：home 本身是软链（或位于软链祖先下）→ 仍能正常删除，不得误拒（守卫不假阳性）。"""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    link_home = tmp_path / "home-link"
    try:
        link_home.symlink_to(real_home, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    ext = _ext_dir(link_home, "acme") / "audit"
    ext.mkdir(parents=True)
    (ext / "pkg").mkdir()

    assert remove_plugin_install_path(ext, link_home, "acme", "audit") is True
    assert not (real_home / "marketplace" / ".plugins" / "acme" / "audit").exists()


def test_safe_rmtree_refuses_path_outside_home_258(tmp_path: Path) -> None:
    """#258 架构师 🟡5：Home 越界守卫仍有真实用例——盘外真实目录拒删，Home 内缺失路径返回成功。"""
    import a2c_smcp.computer.settings.reconciler as rec_mod

    home = _home(tmp_path)
    outside = tmp_path / "outside-home"
    outside.mkdir()
    (outside / "keep").write_text("x", encoding="utf-8")

    assert rec_mod._safe_rmtree(outside, home) is False
    assert outside.exists() and (outside / "keep").exists()
    assert rec_mod._safe_rmtree(home / "missing", home) is True  # 不存在视为已清空


def test_safe_rmtree_refuses_symlink_target_258(tmp_path: Path) -> None:
    """#258 复审 🟡2：共享删除原语拒绝软链路径（prune 等调用点不再跟随软链删到链接目标）。"""
    home = _home(tmp_path)
    real = marketplace_skill_dir(home, "real") / "plugins"
    sentinel = real / "sentinel" / "SKILL.md"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("x", encoding="utf-8")
    link = marketplace_skill_dir(home, "acme")
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    _seed_known(home, {"acme": _SRC_A})

    removed = prune_marketplaces(["acme"], SkillRegistry(), home)

    assert removed == [], "未真正清空 ⇒ 不计入 removed（CLI 据此报失败）"
    assert real.exists() and sentinel.exists(), "不得跟随软链删除链接目标"
    assert link.is_symlink()
    assert "acme" in load_known_marketplaces(home=home)["marketplaces"], "known 条目保留，残留树仍可被 orphan 发现"


def test_safe_rmtree_refuses_symlinked_parent_component_258(tmp_path: Path) -> None:
    """#258 复审 🟡3：父组件（如 ``.plugins``）为软链时同样拒删，不得 resolve 后删到链接目标。"""
    import a2c_smcp.computer.settings.reconciler as rec_mod

    home = _home(tmp_path)
    target = home / "marketplace" / "elsewhere"
    (target / "acme" / "pkg").mkdir(parents=True)
    (target / "acme" / "pkg" / "keep").write_text("x", encoding="utf-8")
    ns = home / "marketplace" / ".plugins"
    ns.parent.mkdir(parents=True, exist_ok=True)
    try:
        ns.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")

    assert rec_mod._safe_rmtree(ns / "acme", home) is False
    assert (target / "acme" / "pkg" / "keep").exists(), "父组件软链不得被跟随"


def test_safe_rmtree_resolve_failure_returns_false_258(tmp_path: Path) -> None:
    """#258 复审三轮 🟡3：路径不可解析（软链环）→ ``_safe_rmtree`` 拒删返回 False，不逃逸异常。"""
    import a2c_smcp.computer.settings.reconciler as rec_mod

    home = _home(tmp_path)
    loop_a = home / "loop-a"
    loop_b = home / "loop-b"
    try:
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")

    assert rec_mod._safe_rmtree(loop_a / "x", home) is False
    assert loop_a.is_symlink() and loop_b.is_symlink(), "拒删不得先删链接本身"


def test_remove_plugin_install_path_resolve_failure_258(tmp_path: Path) -> None:
    """#258 复审四轮 🟡4/🟡5：ext root 为软链环 → 抛 InstallPathRemovalError（含路径与 resolve 原因）。"""
    home = _home(tmp_path)
    ext = _ext_dir(home, "acme") / "audit"
    ext.parent.mkdir(parents=True, exist_ok=True)
    try:
        ext.symlink_to(ext)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")

    with pytest.raises(InstallPathRemovalError, match="path resolve failed"):
        remove_plugin_install_path(ext, home, "acme", "audit")
    assert ext.is_symlink()


def test_prune_symlink_loop_keeps_known_entry_258(tmp_path: Path) -> None:
    """#258 复审四轮 🟡4：marketplace clone 为软链环 → prune 拒删、保留 known、链接仍在。"""
    home = _home(tmp_path)
    clone = marketplace_skill_dir(home, "acme")
    clone.parent.mkdir(parents=True, exist_ok=True)
    try:
        clone.symlink_to(clone)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    # 软链环下不能经 _seed_known（其 installLocation 会 resolve）→ 直接写 known。
    save_known_marketplaces({"version": 1, "marketplaces": {"acme": {"source": _SRC_A}}}, home=home)

    removed = prune_marketplaces(["acme"], SkillRegistry(), home)

    assert removed == []
    assert clone.is_symlink()
    assert "acme" in load_known_marketplaces(home=home)["marketplaces"]


def test_prune_failure_does_not_block_other_marketplaces_258(tmp_path: Path) -> None:
    """#258 复审 🔴1：任一 marketplace 清理失败不阻断其余名字（失败项保留 known，成功项照常移除）。"""
    home = _home(tmp_path)
    real = marketplace_skill_dir(home, "real") / "plugins"
    sentinel = real / "sentinel"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("x", encoding="utf-8")
    bad_link = marketplace_skill_dir(home, "bad")
    bad_link.parent.mkdir(parents=True, exist_ok=True)
    try:
        bad_link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    _make_clone(home, "good")
    _seed_known(home, {"bad": _SRC_A, "good": _SRC_B})

    removed = prune_marketplaces(["bad", "good"], SkillRegistry(), home)

    assert removed == ["good"]
    known = load_known_marketplaces(home=home)["marketplaces"]
    assert "bad" in known and "good" not in known
    assert sentinel.exists() and bad_link.is_symlink()


async def test_gc_keeps_ledger_when_clone_removal_fails_258(tmp_path: Path) -> None:
    """#258 复审三轮 🟡6：gc 删除独占 clone 失败 → 保留账本/SKILL、不计 removed、不触发 MCP teardown。"""
    home = _home(tmp_path)
    target = home / "target"
    target.mkdir()
    (target / "keep").write_text("x", encoding="utf-8")
    ext = _ext_dir(home, "acme") / "audit"
    ext.parent.mkdir(parents=True, exist_ok=True)
    try:
        ext.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(ext), "mcpServers": ["figma"]}]})
    reg = SkillRegistry()
    _reg_skill(reg, "audit:lint", "acme", ext)
    teardown: list[list[str]] = []

    async def _cb(servers: list[str]) -> None:
        teardown.append(servers)

    removed = await gc_plugins(["audit@acme"], reg, home, non_plugin_bundle_ids=lambda: set(), mcp_teardown=_cb)

    assert removed == []
    assert "audit@acme" in load_installed_plugins(home=home)["plugins"]
    assert reg.resolve("audit:lint") is not None, "删除失败不得注销 SKILL"
    assert teardown == [], "删除失败不得触发 bundled server 摘除"
    assert (target / "keep").exists()


async def test_gc_relative_source_catalog_path_is_retained_258(tmp_path: Path) -> None:
    """#258：gc 删账本与 SKILL，但**不删**共享 catalog 内的相对源 installPath（归 marketplace remove/prune）。"""
    home = _home(tmp_path)
    p = marketplace_skill_dir(home, "acme") / "plugins" / "orphan"
    (p / "skills" / "orphan-skill").mkdir(parents=True)
    (p / "skills" / "orphan-skill" / "SKILL.md").write_text("x", encoding="utf-8")
    _seed_installed(home, {"orphan@acme": [{"scope": "user", "installPath": str(p)}]})

    removed = await gc_plugins(["orphan@acme"], SkillRegistry(), home, non_plugin_bundle_ids=lambda: set())

    assert removed == ["orphan@acme"]
    assert p.exists() and (p / "skills" / "orphan-skill" / "SKILL.md").exists()
    assert "orphan@acme" not in load_installed_plugins(home=home)["plugins"]


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


async def test_reconcile_rename_rejection_goes_to_validation_not_failed(tmp_path: Path, monkeypatch) -> None:
    """
    审查 🟡7（协议 §5.14）：refresh 改名拒绝**不进入 degraded** —→ 归 ``validation``，不是 ``failed``
    （后者在调用方语义里等于「能力已降级 / 需回收」，而旧能力实际保持 active）。
    """
    from a2c_smcp.computer.skills.marketplace_identity import MarketplaceNameChangeError

    home = _home(tmp_path)
    _seed_known(home, {"acme": _SRC_A})
    _make_clone(home, "acme")

    async def _rename(*_args: object, **_kwargs: object) -> list[str]:
        raise MarketplaceNameChangeError("renamed upstream", details={"name": "acme"})

    monkeypatch.setattr("a2c_smcp.computer.settings.reconciler.stage_marketplace_skills", _rename)

    report = await reconcile(SkillRegistry(), home, {"extraKnownMarketplaces": {"acme": {"source": _SRC_A, "autoUpdate": True}}})

    assert report.validation == {"acme": "manifest-name-changed"}
    assert report.failed == [] and report.updated == [] and report.up_to_date == []
