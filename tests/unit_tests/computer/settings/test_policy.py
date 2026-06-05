# -*- coding: utf-8 -*-
# filename: test_policy.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
policy scope 四子源 first-source-wins 单元测试（v0.2.1）
Unit tests for policy-scope four sub-sources first-source-wins (v0.2.1)

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §5.2 / §5.4。

测试意图 / Test intentions:
- first-source-wins：高优先级**有内容**者整体胜出，低优先级**不合并**（即便它有额外键）
- 空源跳过；皆空 → {}；单源 loader 抛错 → 跳过续下一源
- layer 3 read：managed-settings.json + managed-settings.d/*.json 深合并；缺失 → None
- remote 子源 v0.2.1 stub 恒 None（即便配置了 base env，也不联网）
- 各平台默认子源装配（darwin / win32 / linux）优先级
"""

import json
from pathlib import Path

import pytest

import a2c_smcp.computer.settings.policy as policy_mod
from a2c_smcp.computer.settings.policy import (
    PolicySource,
    default_policy_sources,
    load_macos_plist,
    load_managed_settings,
    load_remote_policy,
    load_windows_registry,
    resolve_policy_settings,
)


def _src(name: str, priority: int, payload: dict | None) -> PolicySource:
    return PolicySource(name=name, priority=priority, loader=lambda: payload)


# ---------------------------------------------------------------------------
# first-source-wins
# ---------------------------------------------------------------------------
def test_highest_priority_with_content_wins_no_merge() -> None:
    sources = [
        _src("p3-managed", 3, {"strictKnownMarketplaces": False, "extra": "low"}),
        _src("p2-mdm", 2, {"strictKnownMarketplaces": True}),  # 高优先级、不带 extra
    ]
    result = resolve_policy_settings(sources=sources)
    # p2 整体胜出，p3 的 extra 不被合并进来（first-source-wins，非 merge）。
    assert result == {"strictKnownMarketplaces": True}


def test_empty_sources_skipped_until_content() -> None:
    sources = [
        _src("p1-remote", 1, None),
        _src("p2", 2, {}),  # 空 dict 视为无内容
        _src("p3", 3, {"trustedMarketplaces": ["mp"]}),
    ]
    assert resolve_policy_settings(sources=sources) == {"trustedMarketplaces": ["mp"]}


def test_all_empty_returns_empty_dict() -> None:
    sources = [_src("a", 1, None), _src("b", 2, {})]
    assert resolve_policy_settings(sources=sources) == {}


def test_no_sources_returns_empty_dict() -> None:
    assert resolve_policy_settings(sources=[]) == {}


def test_loader_raising_is_skipped() -> None:
    def _boom() -> dict:
        raise RuntimeError("registry blew up")

    sources = [
        PolicySource(name="p2-explodes", priority=2, loader=_boom),
        _src("p3-ok", 3, {"strictKnownMarketplaces": True}),
    ]
    assert resolve_policy_settings(sources=sources) == {"strictKnownMarketplaces": True}


def test_priority_order_independent_of_list_order() -> None:
    # 列表乱序，按 priority 升序选取。
    sources = [
        _src("p3", 3, {"k": "p3"}),
        _src("p1", 1, None),
        _src("p2", 2, {"k": "p2"}),
    ]
    assert resolve_policy_settings(sources=sources) == {"k": "p2"}


# ---------------------------------------------------------------------------
# layer 3：managed-settings.json + .d/*.json
# ---------------------------------------------------------------------------
def test_load_managed_settings_missing_returns_none(tmp_path: Path) -> None:
    assert load_managed_settings(tmp_path / "nonexistent") is None


def test_load_managed_settings_base_only(tmp_path: Path) -> None:
    (tmp_path / "managed-settings.json").write_text(json.dumps({"strictKnownMarketplaces": True}), encoding="utf-8")
    assert load_managed_settings(tmp_path) == {"strictKnownMarketplaces": True}


def test_load_managed_settings_dropin_deep_merge(tmp_path: Path) -> None:
    (tmp_path / "managed-settings.json").write_text(
        json.dumps({"trustedMarketplaces": ["a"], "strictKnownMarketplaces": False}), encoding="utf-8"
    )
    d = tmp_path / "managed-settings.d"
    d.mkdir()
    (d / "10-team.json").write_text(json.dumps({"trustedMarketplaces": ["b"]}), encoding="utf-8")
    (d / "20-override.json").write_text(json.dumps({"strictKnownMarketplaces": True}), encoding="utf-8")

    merged = load_managed_settings(tmp_path)
    assert merged is not None
    # 数组拼接去重（read 语义），标量被 drop-in 覆盖。
    assert merged["trustedMarketplaces"] == ["a", "b"]
    assert merged["strictKnownMarketplaces"] is True


def test_load_managed_settings_dropin_only(tmp_path: Path) -> None:
    d = tmp_path / "managed-settings.d"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"trustedMarketplaces": ["only"]}), encoding="utf-8")
    assert load_managed_settings(tmp_path) == {"trustedMarketplaces": ["only"]}


# ---------------------------------------------------------------------------
# remote stub
# ---------------------------------------------------------------------------
def test_remote_policy_is_stub_even_with_base_env() -> None:
    assert load_remote_policy({}) is None
    assert load_remote_policy({"A2C_BASE_API_URL": "https://api.example.com"}) is None


# ---------------------------------------------------------------------------
# 默认子源装配 / default source assembly
# ---------------------------------------------------------------------------
def test_default_sources_darwin() -> None:
    names = {s.name: s.priority for s in default_policy_sources("darwin", {})}
    assert names == {"remote": 1, "macos-plist": 2, "managed-settings": 3}


def test_default_sources_win32() -> None:
    names = {s.name: s.priority for s in default_policy_sources("win32", {})}
    assert names == {"remote": 1, "hklm-registry": 2, "managed-settings": 3, "hkcu-registry": 4}


def test_default_sources_linux() -> None:
    names = {s.name: s.priority for s in default_policy_sources("linux", {})}
    assert names == {"remote": 1, "managed-settings": 3}


# ---------------------------------------------------------------------------
# _read_json_file 降级路径（经 load_managed_settings 触达）/ degradation paths
# ---------------------------------------------------------------------------
def test_managed_settings_corrupt_base_returns_none(tmp_path: Path) -> None:
    (tmp_path / "managed-settings.json").write_text("{not valid json", encoding="utf-8")
    assert load_managed_settings(tmp_path) is None  # 损坏 base、无 dropin → None


def test_managed_settings_non_object_root_returns_none(tmp_path: Path) -> None:
    (tmp_path / "managed-settings.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_managed_settings(tmp_path) is None  # 非对象根 → None


def test_managed_settings_corrupt_dropin_skipped_base_kept(tmp_path: Path) -> None:
    (tmp_path / "managed-settings.json").write_text(json.dumps({"strictKnownMarketplaces": True}), encoding="utf-8")
    d = tmp_path / "managed-settings.d"
    d.mkdir()
    (d / "10-bad.json").write_text("{broken", encoding="utf-8")  # 损坏 drop-in 跳过
    (d / "20-good.json").write_text(json.dumps({"trustedMarketplaces": ["x"]}), encoding="utf-8")
    merged = load_managed_settings(tmp_path)
    assert merged == {"strictKnownMarketplaces": True, "trustedMarketplaces": ["x"]}


# ---------------------------------------------------------------------------
# load_macos_plist 四态（darwin 可测，plistlib 造样本）/ four states
# ---------------------------------------------------------------------------
def test_macos_plist_missing_returns_none(tmp_path: Path) -> None:
    assert load_macos_plist(tmp_path / "absent.plist") is None


def test_macos_plist_valid_parsed(tmp_path: Path) -> None:
    import plistlib

    p = tmp_path / "policy.plist"
    p.write_bytes(plistlib.dumps({"strictKnownMarketplaces": True, "trustedMarketplaces": ["mp"]}))
    assert load_macos_plist(p) == {"strictKnownMarketplaces": True, "trustedMarketplaces": ["mp"]}


def test_macos_plist_corrupt_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "bad.plist"
    p.write_bytes(b"this is not a plist at all")
    assert load_macos_plist(p) is None


def test_macos_plist_non_dict_root_returns_none(tmp_path: Path) -> None:
    import plistlib

    p = tmp_path / "list.plist"
    p.write_bytes(plistlib.dumps([1, 2, 3]))
    assert load_macos_plist(p) is None


# ---------------------------------------------------------------------------
# load_windows_registry 非 Windows 早返回（darwin/linux 上即覆盖）/ non-win32 early return
# ---------------------------------------------------------------------------
def test_windows_registry_non_windows_returns_none() -> None:
    # 测试运行于 darwin/linux：sys.platform != "win32" → 立即 None（不触 winreg）。
    assert load_windows_registry("HKEY_LOCAL_MACHINE") is None
    assert load_windows_registry("HKEY_CURRENT_USER") is None


# ---------------------------------------------------------------------------
# default_policy_sources 的 lambda loader 真被调用（端到端 first-source-wins）/ lambdas actually wired
# ---------------------------------------------------------------------------
def test_darwin_default_sources_plist_wins_over_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    # 验证 darwin 装配：priority-2 源 loader 真走 load_macos_plist；plist 有内容 → 胜过 managed(p3)。
    monkeypatch.setattr(policy_mod, "load_macos_plist", lambda: {"k": "plist"})
    monkeypatch.setattr(policy_mod, "load_managed_settings", lambda _dir: {"k": "managed"})
    assert resolve_policy_settings(platform="darwin", env={}) == {"k": "plist"}


def test_darwin_default_sources_managed_used_when_plist_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy_mod, "load_macos_plist", lambda: None)
    monkeypatch.setattr(policy_mod, "load_managed_settings", lambda _dir: {"k": "managed"})
    assert resolve_policy_settings(platform="darwin", env={}) == {"k": "managed"}
