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
    is_marketplace_declaration_field,
    is_valid_enabled_plugin_key,
    is_valid_git_url,
    is_valid_marketplace_name,
    scrub_diagnostic_chars,
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
        # #270：file: 一律不要求 host，带 host 形态同样合法
        ("file://host/path/repo", True),
        # #270：userinfo 里的凭据不使 URL 非法（host 存在即可）；query 合法（不进派生名即可）
        ("ssh://alice:PW@example.com/repo", True),
        ("https://host/repo?token=SECRET", True),
        ("ssh://[::1]:22/repo", True),
        # #270 hostless：ssh/git/http(s) 必须有非空 host（否则凭据段会滑进派生名）
        ("ssh:///alice:PW_LEAK@example.com", False),
        ("git:///x", False),
        ("https:///x", False),
        ("http:///x", False),
        ("ssh://user@:22/path", False),
        ("ssh://", False),
        ("file://", False),
        # host 槽位填「非 host 占位串」同样拒绝：否则 hostless 攻击串只要填个非空 junk 就复活
        ("ssh:// /alice:PW_LEAK@example.com", False),
        ("ssh://-/x", False),
        ("ssh://_/x", False),
        ("ssh://%20/x", False),
        ("ssh://./x", False),
        ("https://ho st/repo", False),
        # 下划线 / 子域 / IDN / 尾点 FQDN / IPv6 zone-id 是真实主机名形态，不误伤
        ("ssh://my_host/x", True),
        ("https://sub.example.com/x", True),
        ("https://例子.测试/repo", True),
        ("https://example.com./repo", True),
        ("ssh://[fe80::1%25eth0]/x", True),
        # percent-encoded host / 非法 IPv6：fail-closed（有意拒绝，见 CHANGELOG）
        ("https://ex%61mple.com/x", False),
        ("ssh://[a:b]/x", False),
        # file: 全例外——端口形态也不校验（`file://C:\repos\repo` 在收紧前一直合法）
        ("file://C:\\repos\\repo", True),
        # 非法端口 / 畸形 IPv6：fail-closed（比展示层更严）
        ("https://host:abc/x", False),
        ("file://[bad", False),
        # scp-like 按整串判定：尾随换行不再视为合法
        ("git@host:p\n", False),
        ("not a url", False),
        ("ftp://host/x", False),
        ("", False),
        ("   ", False),
        # 既有边界：不 strip 入参（归一在 normalize_marketplace_url 做）
        (" https://host/x", False),
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
    # #157：enable 方向判据（拒 project）；#196：landingRoot（写目标重定向面，协议 blob-transfer.md §7）；
    # #236：readRoots（读授权扩张面，协议 security.md §读取通道安全 / computer-management §7 不变量 #7）。
    # **不含** disabledMcpjsonServers —— DENY 方向 fail-safe。
    assert TRUSTED_SCOPE_ONLY_FIELDS == frozenset(
        {"enabledMcpjsonServers", "enableAllProjectMcpServers", "landingRoot", "readRoots"}
    )
    assert BOOL_FIELDS == frozenset({"strictKnownMarketplaces", "enableAllProjectMcpServers"})
    assert len(STRING_ARRAY_FIELDS) == 7


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
        # #270：hostless URL 在 settings 加载层同样 fail-closed（整条剔除，文件不被改写）
        (
            "ok-mp",
            {"source": {"type": "git", "url": "ssh:///alice:PW_LEAK@example.com"}},
            "extraKnownMarketplaces.ok-mp.source.url",
        ),
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


def test_permissions_additional_directories_syntactic_predicate_unchanged() -> None:
    """#304 item 3：``additionalDirectories`` **保持语法判据**（不做谓词合并、不按宿主平台过滤）.

    盘符形态在 POSIX 宿主上照样保留（语法绝对即通过）；verbatim ``\\\\?\\`` / 前导空白被拒。
    该字段运行期无消费者，故「外来平台形态」不会进入任何授权集合。
    """
    raw = {"permissions": {"additionalDirectories": ["D:\\data", "\\\\?\\C:\\x", "  /abs"]}}
    cleaned, errors = validate_settings(raw, SettingsScope.USER)

    assert cleaned["permissions"]["additionalDirectories"] == ["D:\\data"]
    assert len(errors) == 2


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

    def test_leading_whitespace_trimmed_and_activated(self) -> None:
        """#304 item 3（对齐 rust ``trim()``）：**前导**空白此前令字段整字段判废.

        旧判据 ``_is_absolute_path`` 对 ``/srv/...`` 这类 POSIX 路径用 ``startswith("/")``，
        前导空格使其失败；现在 trim 后生效。
        """
        cleaned, errors = validate_settings({"landingRoot": "  /srv/landing"}, SettingsScope.USER)
        assert cleaned["landingRoot"] == "/srv/landing"
        assert errors == []

    def test_trailing_whitespace_retargets_to_trimmed_root(self) -> None:
        """**尾随**空白此前**原样生效**，现在写入根静默改为 trim 后的路径.

        这是 0.5.1 的升级面：带尾随空白的配置不是「从判废变为生效」，而是写入目录被规范化——
        POSIX 上旧写入根是带空格的目录（其中产物不会迁移）；Windows 上 Win32 本就会剥掉分量末尾
        空格/点（真实差异是 ``.part`` 拒绝集条目恢复）。
        """
        cleaned, errors = validate_settings({"landingRoot": "/srv/landing \t"}, SettingsScope.USER)
        assert cleaned["landingRoot"] == "/srv/landing"
        assert errors == []

    @pytest.mark.parametrize("sep", ["\x1c", "\x1d", "\x1e", "\x1f"])
    def test_trim_matches_rust_white_space_set(self, sep: str) -> None:
        """trim 逐字节等价 rust ``str::trim()``：U+001C–U+001F **不**算空白（前导 + 尾随两个方向）.

        ``str.strip()`` 会剥这四个信息分隔符（``isspace()`` 为真、``White_Space`` 为假），而 Rust
        ``trim()`` 不会。两个方向都要钉：**前导**分歧是「python 启用 / rust 判废（fail-closed）」，
        **尾随**分歧是「两侧都启用、写入根不同」（静默，更难察觉）。
        U+3000（IDEOGRAPHIC SPACE）属 ``White_Space``，两侧都剥。
        """
        # 前导 SEP：不被 trim → 仍非绝对 → 整字段判废（rust 同）
        cleaned, errors = validate_settings({"landingRoot": f"{sep}/srv/landing"}, SettingsScope.USER)
        assert "landingRoot" not in cleaned
        assert len(errors) == 1

        # 尾随 SEP：不被 trim → 原样存值（`str.strip()` 变异会剥掉它，写入根静默变化）
        cleaned, errors = validate_settings({"landingRoot": f"/srv/landing{sep}"}, SettingsScope.USER)
        assert cleaned["landingRoot"] == f"/srv/landing{sep}"
        assert errors == []

        cleaned, errors = validate_settings({"landingRoot": "\u3000/srv/landing\u3000"}, SettingsScope.USER)
        assert cleaned["landingRoot"] == "/srv/landing"
        assert errors == []

    def test_whitespace_only_dropped(self) -> None:
        """trim 后为空 → 仍按非绝对路径判废（不因 trim 放宽空值）。"""
        cleaned, errors = validate_settings({"landingRoot": " \t\n "}, SettingsScope.USER)
        assert "landingRoot" not in cleaned
        assert len(errors) == 1

    def test_syntactic_predicate_is_deliberately_not_merged(self) -> None:
        """**刻意的不对称**（用户 2026-11-23 裁决：不做谓词合并）.

        两半合起来才能钉住语法判据：① 盘符形态在 POSIX 宿主照样通过（``normalize_path`` 会拒）；
        ② ``normalize_path`` 能接受的 verbatim ``\\\\?\\C:\\...`` 反而被拒。若后人「顺手」把
        ``landingRoot`` 也改用 ``normalize_path``，① 立刻红。
        """
        cleaned, errors = validate_settings({"landingRoot": "D:\\a2c\\landing"}, SettingsScope.USER)
        assert cleaned["landingRoot"] == "D:\\a2c\\landing"  # 语法绝对即通过，不按宿主平台过滤
        assert errors == []

        cleaned, errors = validate_settings({"landingRoot": "\\\\?\\C:\\a2c\\landing"}, SettingsScope.USER)
        assert "landingRoot" not in cleaned  # normalize 接受的形态反而被语法判据拒
        assert len(errors) == 1

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


# ---------------------------------------------------------------------------
# #293：声明子树谓词 + 诊断字符清洗 / declaration-subtree predicate + scrub
# ---------------------------------------------------------------------------
class TestIsMarketplaceDeclarationField:
    """``extraKnownMarketplaces`` 子树判定（镜像 rust ``is_marketplace_declaration_field``）。"""

    @pytest.mark.parametrize(
        "field",
        [
            "extraKnownMarketplaces",  # 整字段类型错
            "extraKnownMarketplaces.acme",
            "extraKnownMarketplaces.acme.source",
            "extraKnownMarketplaces.acme.source.url",
            "extraKnownMarketplaces.acme.source.type",
            "extraKnownMarketplaces.acme.autoUpdate",
        ],
    )
    def test_hits_prefix_and_subtree(self, field: str) -> None:
        assert is_marketplace_declaration_field(field) is True

    @pytest.mark.parametrize(
        "field",
        [
            # 裸 startswith 会把这两个误收进来——`.` 边界承重。
            "extraKnownMarketplacesBackup.acme",
            "extraKnownMarketplacesBackup",
            "enabledPlugins.BAD KEY",
            "installedPlugins[0]",
            "",
        ],
    )
    def test_misses_unrelated_fields(self, field: str) -> None:
        assert is_marketplace_declaration_field(field) is False


class TestScrubDiagnosticChars:
    """控制 / 方向覆写字符 → 空格（终端行倒序显示与伪造换行的唯一止血点）。"""

    def test_replaces_bidi_and_line_separators(self) -> None:
        # 正对照：原串确实含这些字符，否则「输出干净」可能只是输入本就干净。
        raw = "evil‮gnp.exe FAKE\nLINE\x1b[31m﻿"
        for needle in ("‮", " ", "\n", "\x1b", "﻿"):
            assert needle in raw
        scrubbed = scrub_diagnostic_chars(raw)
        for needle in ("‮", " ", "\n", "\x1b", "﻿"):
            assert needle not in scrubbed
        assert scrubbed.startswith("evil gnp.exe FAKE LINE ")

    def test_clean_text_is_returned_unchanged(self) -> None:
        """正/负对照：无危险字符时逐字不变（不得吞掉正常空白与标点）。"""
        assert scrub_diagnostic_chars("acme.source.url — 普通文本") == "acme.source.url — 普通文本"
