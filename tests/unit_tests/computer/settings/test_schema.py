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
- **#157**：审批门 enable 方向判据（enabledMcpjsonServers/enableAllProjectMcpServers）在 project scope →
  过滤 + 记错（自我批准闭环）；受信 scope（user/**local**/flag/policy）→ 保留；DENY 方向
  （disabledMcpjsonServers）任意 scope 照常生效（防过度矫正）。协议 §2.1，对拍 rust-sdk#143
- 已知字段逐条过滤：enabledPlugins / extraKnownMarketplaces 单项非法剔除、其余保留
- 标量 / 数组类型错 → 回退默认 + 记错
- 形态校验器：git url / <plugin>@<mp> key / marketplace 名
- 非 dict 根 → 空 + 记错
"""

import pytest

from a2c_smcp.computer.settings.schema import (
    BOOL_FIELDS,
    FIELD_DISABLED_MCPJSON_SERVERS,
    POLICY_ONLY_FIELDS,
    STRING_ARRAY_FIELDS,
    TRUSTED_SCOPE_ONLY_FIELDS,
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
# #157：审批门 enable 方向判据的 scope 越权 / approval-gate ENABLE-direction overreach
# 协议 guides/mcp-approval-gate-alignment.md §2.1（MUST）。对拍 rust-sdk#143 schema.rs test mod。
# ---------------------------------------------------------------------------
def test_trusted_scope_only_fields_filtered_in_project_scope() -> None:
    """project scope（入 git、随仓库分发）供给档⑤/⑥ → 过滤 + 记错（杜绝自我批准，档④ 同构面）。"""
    raw = {"enabledMcpjsonServers": ["s"], "enableAllProjectMcpServers": True, "trustedMarketplaces": ["mp"]}
    cleaned, errors = validate_settings(raw, SettingsScope.PROJECT)
    assert "enabledMcpjsonServers" not in cleaned
    assert "enableAllProjectMcpServers" not in cleaned
    assert cleaned["trustedMarketplaces"] == ["mp"]  # 非本类目字段保留
    assert {e.field for e in errors} == {"enabledMcpjsonServers", "enableAllProjectMcpServers"}
    assert all("settings.local.json" in e.reason for e in errors)  # 文案给出可操作去向


@pytest.mark.parametrize(
    "scope",
    [SettingsScope.USER, SettingsScope.LOCAL, SettingsScope.FLAG, SettingsScope.POLICY],
)
def test_trusted_scope_only_fields_kept_in_trusted_scopes(scope: SettingsScope) -> None:
    """★ 陷阱 1 守护：受信供给方 = user/local/flag/policy（**含 LOCAL**，§2.1 表）——MUST 原样保留。

    LOCAL 尤为关键：三个批准写助手（approve/deny/approve-all）**只写 local scope**，若把 local 判为不受信，
    每次批准都会在读回时被自己过滤掉、**批准永远不生效**。读面与写面 MUST 对称。
    NB: this deliberately differs from mcp_config._TRUSTED_ORIGINS ({USER, FLAG, POLICY}) — the mcp.json
    declaration face — which excludes LOCAL. Do not conflate the two.
    """
    raw = {"enabledMcpjsonServers": ["s"], "enableAllProjectMcpServers": True}
    cleaned, errors = validate_settings(raw, scope)
    assert cleaned["enabledMcpjsonServers"] == ["s"], f"{scope} 是受信供给方，MUST 保留"
    assert cleaned["enableAllProjectMcpServers"] is True, f"{scope} 是受信供给方，MUST 保留"
    assert errors == [], f"{scope} 不应记错，实得 {errors}"


@pytest.mark.parametrize(
    "scope",
    [SettingsScope.USER, SettingsScope.PROJECT, SettingsScope.LOCAL, SettingsScope.FLAG, SettingsScope.POLICY],
)
def test_disabled_mcpjson_allowed_from_any_scope(scope: SettingsScope) -> None:
    """★ **防过度矫正**：``disabledMcpjsonServers`` 是 **DENY** 方向，§2.1 表第 3 行明定**任意 scope 可供给**。

    fail-safe——仓库禁自己的 server 无安全影响，更严格永远安全。当前「碰巧正确」（无约束≠有意放行），本测把
    该**意图**钉死：后人若顺手把它一起收进 TRUSTED_SCOPE_ONLY_FIELDS「保持一致」，此测立刻红。
    Guards the DENY direction against over-tightening.
    """
    assert FIELD_DISABLED_MCPJSON_SERVERS not in TRUSTED_SCOPE_ONLY_FIELDS, (
        "DENY 方向 MUST NOT 进 enable-方向类目（§2.1 表第 3 行 fail-safe）"
    )
    raw = {"disabledMcpjsonServers": ["srv"]}
    cleaned, errors = validate_settings(raw, scope)
    assert cleaned["disabledMcpjsonServers"] == ["srv"], f"{scope}：DENY 方向任意 scope 可供给（更严格永远安全）"
    assert errors == [], f"{scope} 不应记错"


def test_field_sets_exact() -> None:
    """字段集合内容精确（对拍 rust ``test_field_sets_exact``）——防「顺手增删」漂移。"""
    assert POLICY_ONLY_FIELDS == frozenset({"allowedMcpServers", "deniedMcpServers"})
    # #157：enable 方向判据（拒 project）；#196：landingRoot（写目标重定向面，协议 blob-transfer.md §7）。
    # **不含** disabledMcpjsonServers —— DENY 方向 fail-safe。
    assert TRUSTED_SCOPE_ONLY_FIELDS == frozenset(
        {"enabledMcpjsonServers", "enableAllProjectMcpServers", "landingRoot"}
    )
    assert BOOL_FIELDS == frozenset({"strictKnownMarketplaces", "enableAllProjectMcpServers"})
    assert len(STRING_ARRAY_FIELDS) == 6


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


# ---------------------------------------------------------------------------
# #161：policy scope 经 validate_settings 检测字段类型错 / type-error detection for policy scope
# ---------------------------------------------------------------------------
def test_validate_settings_policy_scope_detects_type_errors() -> None:
    """policy scope 中字段类型错（如 allowedMcpServers 不是 array）→ 过滤 + 记错。"""
    raw = {
        "allowedMcpServers": "not-a-list",  # 应为 array
        "deniedMcpServers": ["legit-deny"],
        "trustedMarketplaces": ["mp"],
    }
    cleaned, errors = validate_settings(raw, SettingsScope.POLICY)
    # allowedMcpServers 类型错 → 从 cleaned 中剔除
    assert "allowedMcpServers" not in cleaned
    # deniedMcpServers 类型正确 → 保留
    assert cleaned.get("deniedMcpServers") == ["legit-deny"]
    # 非 policy-only 字段正常保留
    assert cleaned.get("trustedMarketplaces") == ["mp"]
    # 类型错产出 errors
    assert len(errors) >= 1
    assert any("allowedMcpServers" in e.field for e in errors)


# ---------------------------------------------------------------------------
# landingRoot（v0.4.0 #196，client:put_blob 落盘根）
# ---------------------------------------------------------------------------
class TestLandingRoot:
    """顶层键 ``landingRoot``：字符串 + 绝对路径；project scope 供给 MUST 拒绝（协议 blob-transfer.md §7）。"""

    def test_valid_absolute_path_kept_in_user_scope(self) -> None:
        cleaned, errors = validate_settings({"landingRoot": "/var/lib/a2c/landing"}, SettingsScope.USER)
        assert cleaned["landingRoot"] == "/var/lib/a2c/landing"
        assert errors == []

    def test_windows_drive_path_valid(self) -> None:
        cleaned, errors = validate_settings({"landingRoot": "D:\\a2c\\landing"}, SettingsScope.USER)
        assert cleaned["landingRoot"] == "D:\\a2c\\landing"
        assert errors == []

    @pytest.mark.parametrize("bad", ["relative/path", "./here", "", 123, True, ["/abs"]])
    def test_relative_or_non_string_dropped(self, bad: object) -> None:
        cleaned, errors = validate_settings({"landingRoot": bad}, SettingsScope.USER)
        assert "landingRoot" not in cleaned  # 整字段判废（fail-closed）
        assert len(errors) == 1

    def test_project_scope_filtered_and_recorded(self) -> None:
        """协议 MUST：project settings 入 git 随仓库分发，不得重定向写入沙箱目标（§7 不变量 #6）。"""
        cleaned, errors = validate_settings({"landingRoot": "/tmp/evil"}, SettingsScope.PROJECT)
        assert "landingRoot" not in cleaned
        assert len(errors) == 1
        assert "project scope" in errors[0].reason

    @pytest.mark.parametrize(
        "scope",
        [SettingsScope.USER, SettingsScope.LOCAL, SettingsScope.FLAG, SettingsScope.POLICY],
    )
    def test_trusted_scopes_kept(self, scope: SettingsScope) -> None:
        cleaned, errors = validate_settings({"landingRoot": "/srv/landing"}, scope)
        assert cleaned["landingRoot"] == "/srv/landing"
        assert errors == []
