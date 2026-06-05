# -*- coding: utf-8 -*-
# filename: test_schema.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
settings schema 字段级容错校验单元测试（v0.2.1）
Unit tests for settings schema field-level tolerant validation (v0.2.1)

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §5.3 / §5.3.1 / §5.6。

测试意图 / Test intentions:
- 未知顶层字段 passthrough（静默保留）；``$schema`` 保留
- policy-only 字段（allowedMcpServers/deniedMcpServers）在非 policy scope → 过滤 + 记 ValidationError；
  policy scope → 保留
- 已知字段逐条过滤：enabledPlugins / extraKnownMarketplaces 单项非法剔除、其余保留
- 标量 / 数组类型错 → 回退默认 + 记错
- 形态校验器：git url / <plugin>@<mp> key / marketplace 名
- 非 dict 根 → 空 + 记错
"""

import pytest

from a2c_smcp.computer.settings.schema import (
    SettingsScope,
    is_valid_enabled_plugin_key,
    is_valid_git_url,
    is_valid_marketplace_name,
    validate_settings,
)


# ---------------------------------------------------------------------------
# 形态校验器 / shape validators
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,ok",
    [
        ("git@github.com:team/skills.git", True),
        ("https://github.com/team/skills.git", True),
        ("http://example.com/x.git", True),
        ("ssh://git@host:22/team/skills.git", True),
        ("git://host/path", True),
        ("file:///abs/path/repo", True),
        ("not a url", False),
        ("ftp://host/x", False),
        ("", False),
        ("   ", False),
    ],
)
def test_is_valid_git_url(url: str, ok: bool) -> None:
    assert is_valid_git_url(url) is ok


@pytest.mark.parametrize(
    "key,ok",
    [
        ("frontend-design@my-team-skills", True),
        ("a@b", True),
        ("foo", False),  # 缺 @
        ("foo@", False),
        ("@mp", False),
        ("Foo@mp", False),  # 大写非 kebab
        ("foo@my_mp", False),  # 下划线非 kebab
        ("-foo@mp", False),  # 首字符 -
    ],
)
def test_is_valid_enabled_plugin_key(key: str, ok: bool) -> None:
    assert is_valid_enabled_plugin_key(key) is ok


@pytest.mark.parametrize(
    "name,ok",
    [("my-team-skills", True), ("a", True), ("Foo", False), ("a--b", False), ("a_b", False), ("", False)],
)
def test_is_valid_marketplace_name(name: str, ok: bool) -> None:
    assert is_valid_marketplace_name(name) is ok


# ---------------------------------------------------------------------------
# passthrough / $schema
# ---------------------------------------------------------------------------
def test_unknown_top_level_field_passthrough() -> None:
    raw = {"futureUnknownField": {"x": 1}, "anotherOne": [1, 2, 3]}
    cleaned, errors = validate_settings(raw, SettingsScope.USER)
    assert cleaned == raw  # 未知键原样保留
    assert errors == []


def test_schema_field_preserved_not_consumed() -> None:
    raw = {"$schema": "https://a2c-smcp.dev/schemas/computer-settings-0.2.1.json"}
    cleaned, errors = validate_settings(raw, SettingsScope.USER)
    assert cleaned == raw
    assert errors == []


def test_non_dict_root_falls_back_empty() -> None:
    cleaned, errors = validate_settings(["not", "an", "object"], SettingsScope.USER)
    assert cleaned == {}
    assert len(errors) == 1
    assert errors[0].field == "<root>"


# ---------------------------------------------------------------------------
# policy-only 越权 / policy-only overreach
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scope", [SettingsScope.USER, SettingsScope.PROJECT, SettingsScope.LOCAL])
def test_policy_only_fields_filtered_outside_policy(scope: SettingsScope) -> None:
    raw = {"allowedMcpServers": ["a"], "deniedMcpServers": ["b"], "trustedMarketplaces": ["mp"]}
    cleaned, errors = validate_settings(raw, scope)
    assert "allowedMcpServers" not in cleaned
    assert "deniedMcpServers" not in cleaned
    assert cleaned["trustedMarketplaces"] == ["mp"]  # 非 policy-only 字段保留
    bad_fields = {e.field for e in errors}
    assert bad_fields == {"allowedMcpServers", "deniedMcpServers"}


def test_policy_only_fields_kept_in_policy_scope() -> None:
    raw = {"allowedMcpServers": ["a"], "deniedMcpServers": ["b"]}
    cleaned, errors = validate_settings(raw, SettingsScope.POLICY)
    assert cleaned == raw
    assert errors == []


# ---------------------------------------------------------------------------
# enabledPlugins 逐条过滤 / per-entry filtering
# ---------------------------------------------------------------------------
def test_enabled_plugins_per_entry_filtering() -> None:
    raw = {
        "enabledPlugins": {
            "good@mp": True,
            "also-good@mp": False,
            "badshape": True,  # 缺 @ → 过滤
            "nonbool@mp": "yes",  # 值非 bool → 过滤
        }
    }
    cleaned, errors = validate_settings(raw, SettingsScope.USER)
    assert cleaned["enabledPlugins"] == {"good@mp": True, "also-good@mp": False}
    bad = {e.field for e in errors}
    assert bad == {"enabledPlugins.badshape", "enabledPlugins.nonbool@mp"}


def test_enabled_plugins_non_object_dropped() -> None:
    cleaned, errors = validate_settings({"enabledPlugins": ["nope"]}, SettingsScope.USER)
    assert "enabledPlugins" not in cleaned
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# extraKnownMarketplaces 逐条过滤 + 规范化 / per-entry filter + normalize
# ---------------------------------------------------------------------------
def test_extra_marketplaces_valid_entry_normalized() -> None:
    raw = {
        "extraKnownMarketplaces": {
            "my-team-skills": {
                "source": {"type": "git", "url": "git@github.com:team/skills.git", "extra": "dropped"},
                "autoUpdate": True,
                "junk": 1,  # 顶层多余子键被丢
            }
        }
    }
    cleaned, errors = validate_settings(raw, SettingsScope.USER)
    assert errors == []
    assert cleaned["extraKnownMarketplaces"]["my-team-skills"] == {
        "source": {"type": "git", "url": "git@github.com:team/skills.git"},
        "autoUpdate": True,
    }


@pytest.mark.parametrize(
    "name,entry,bad_field",
    [
        ("Bad_Name", {"source": {"type": "git", "url": "git@h:p"}}, "extraKnownMarketplaces.Bad_Name"),
        ("ok-mp", {"source": {"type": "svn", "url": "x"}}, "extraKnownMarketplaces.ok-mp.source.type"),
        ("ok-mp", {"source": {"type": "git", "url": "garbage"}}, "extraKnownMarketplaces.ok-mp.source.url"),
        ("ok-mp", {"noSource": 1}, "extraKnownMarketplaces.ok-mp.source"),
        ("ok-mp", {"source": {"type": "git", "url": "git@h:p"}, "autoUpdate": "yes"}, "extraKnownMarketplaces.ok-mp.autoUpdate"),
    ],
)
def test_extra_marketplaces_bad_entry_filtered(name: str, entry: dict, bad_field: str) -> None:
    cleaned, errors = validate_settings({"extraKnownMarketplaces": {name: entry}}, SettingsScope.USER)
    assert cleaned.get("extraKnownMarketplaces", {}) == {}  # 唯一项被剔除
    assert any(e.field == bad_field for e in errors)


# ---------------------------------------------------------------------------
# 标量 / 数组类型校验 / scalar & array typing
# ---------------------------------------------------------------------------
def test_bool_field_type_error_dropped() -> None:
    cleaned, errors = validate_settings({"strictKnownMarketplaces": "true"}, SettingsScope.USER)
    assert "strictKnownMarketplaces" not in cleaned
    assert len(errors) == 1


def test_string_array_element_filtering() -> None:
    cleaned, errors = validate_settings({"trustedMarketplaces": ["a", 1, "b", None]}, SettingsScope.USER)
    assert cleaned["trustedMarketplaces"] == ["a", "b"]
    assert len(errors) == 2


def test_string_array_non_list_dropped() -> None:
    cleaned, errors = validate_settings({"trustedMarketplaces": "a,b"}, SettingsScope.USER)
    assert "trustedMarketplaces" not in cleaned
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# permissions.additionalDirectories
# ---------------------------------------------------------------------------
def test_permissions_additional_directories_absolute_only() -> None:
    raw = {"permissions": {"additionalDirectories": ["/abs/ok", "relative/no", 42], "unknownSub": "kept"}}
    cleaned, errors = validate_settings(raw, SettingsScope.USER)
    assert cleaned["permissions"]["additionalDirectories"] == ["/abs/ok"]
    assert cleaned["permissions"]["unknownSub"] == "kept"  # permissions 下未知子键 passthrough
    assert len(errors) == 2  # relative + 非 string


def test_source_path_backfilled_into_errors() -> None:
    _, errors = validate_settings({"strictKnownMarketplaces": 1}, SettingsScope.USER, source_path="/x/settings.json")
    assert errors[0].source_path == "/x/settings.json"
