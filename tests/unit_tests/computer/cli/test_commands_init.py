# -*- coding: utf-8 -*-
# filename: test_commands_init.py
# @Time    : 2026/07/16
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``cli.commands`` 跨命令接缝单元测试 / Unit tests for the cross-command seams in ``cli.commands``。

测试意图 / Test intentions:
- **#157** ``format_settings_errors``：settings 校验错误 → 人读警示行的**纯函数**。boot 批准流程与
  ``settings show`` / ``settings get`` 共用。抽纯函数的理由即「文案与 scope/field 拼装可单测」——故此处
  正面钉死三段拼装与空输入行为，杜绝未来重构把「呈现」半程静默回退成吞错误（呈现行为在
  ``run_mcp_approval`` 这类 ``Session``-泛型异步副作用函数里无法直接断言）。
  对拍 rust ``cli/commands/mod.rs::format_settings_errors_pins_scope_field_reason``。
"""

from __future__ import annotations

import pytest

from a2c_smcp.computer.cli.commands import format_settings_errors
from a2c_smcp.computer.settings.schema import SettingsScope, SettingsValidationError


def test_format_settings_errors_pins_scope_field_reason() -> None:
    """三段（scope / field / reason）全部出现且按 ``⚠ settings.json[{scope}]: {field} — {reason}`` 拼装。"""
    errors = [
        SettingsValidationError(
            scope=SettingsScope.PROJECT,
            field="enableAllProjectMcpServers",
            reason="approval-gate field not allowed in the project scope (filtered)",
        ),
        SettingsValidationError(
            scope=SettingsScope.USER,
            field="allowedMcpServers",
            reason="policy-only field not allowed outside the policy scope (filtered)",
        ),
    ]
    lines = format_settings_errors(errors)
    assert lines == [
        "⚠ settings.json[project]: enableAllProjectMcpServers — approval-gate field not allowed in the project scope (filtered)",
        "⚠ settings.json[user]: allowedMcpServers — policy-only field not allowed outside the policy scope (filtered)",
    ]


def test_format_settings_errors_empty_input_is_silent() -> None:
    """无错误 → 无输出（正常启动不得平白多出噪音行）/ no errors → no noise。"""
    assert format_settings_errors([]) == []


# ── #295：marketplace 声明错误的展示面脱敏 ─────────────────────────────────────
def test_format_settings_errors_sanitizes_marketplace_declaration_fields() -> None:
    """
    凭据形态市场名 / 带凭据的 reason / 控制字符：行内**四类**全部不可见，前缀保留可定位。

    向量镜像 rust#267（`format_settings_errors_sanitizes_marketplace_declaration_fields`）：非 marketplace
    子树原样，marketplace 子树走与恢复报告同一脱敏漏斗（段漏斗 + 散文脱敏 + Cf/Cc 清洗）。
    """
    secret = "https://user:PW_SECRET@example.com/repo.git"
    lines = format_settings_errors(
        [
            SettingsValidationError(
                scope=SettingsScope.USER,
                field=f"extraKnownMarketplaces.{secret}.source.url\u202e",
                reason=f"malformed git url: {secret}\nFAKE\x1b",
            )
        ]
    )
    assert len(lines) == 1
    assert "extraKnownMarketplaces." in lines[0]  # 前缀保留（便于定位）
    for needle in ("PW_SECRET", "\n", "\x1b", "\u202e"):
        assert needle not in lines[0], f"未清洗 {needle!r}: {lines[0]}"
    # 负对照：userinfo 剥离但结构可读（不是整条占位）——段漏斗只遮凭据段。
    assert "https://example.com/repo.git" in lines[0]


def test_format_settings_errors_sanitizes_marketplace_reason_text() -> None:
    """
    reason 轴（message 轴）：现网校验器 reason 全静态，故用**构造错误**钉住脱敏口径。

    与 field 轴分列，确保「只脱敏 field、漏掉 reason」的变异可被单独杀死。
    """
    lines = format_settings_errors(
        [
            SettingsValidationError(
                scope=SettingsScope.LOCAL,
                field="extraKnownMarketplaces.acme.source.url",
                reason="malformed git url: https://alice:s3cret@example.com/repo.git\x1b[31m",
            )
        ]
    )
    assert len(lines) == 1
    assert "s3cret" not in lines[0] and "alice:" not in lines[0]
    assert "https://example.com/repo.git" in lines[0]
    assert "\x1b" not in lines[0]


@pytest.mark.parametrize(
    ("name", "forbidden", "expected"),
    [
        # query / fragment 里的凭据整体丢弃，只留可定位的名字主体。
        ("https://example.com/repo.git?token=abc123#frag", ("abc123", "token=", "frag"), "https://example.com/repo.git"),
        # path 段凭据 → 名字整值 fail-closed（段漏斗口径；散文脱敏只会去 userinfo、保留 path）。
        (
            "https://host/team/alice:PW@example.com/repo.git",
            ("PW", "alice:"),
            "<redacted-git-source>",
        ),
    ],
)
def test_format_settings_errors_redacts_marketplace_name_shapes(
    name: str, forbidden: tuple[str, ...], expected: str,
) -> None:
    """名字段脱敏口径（#295 验收）：userinfo 剥离 / path 凭据整值占位 / query-fragment 丢弃。"""
    lines = format_settings_errors(
        [
            SettingsValidationError(
                scope=SettingsScope.USER,
                field=f"extraKnownMarketplaces.{name}",
                reason="marketplace name must be strict kebab (1-64)",
            )
        ]
    )
    assert len(lines) == 1
    for needle in forbidden:
        assert needle not in lines[0], f"未遮蔽 {needle!r}: {lines[0]}"
    assert f"extraKnownMarketplaces.{expected}" in lines[0]


@pytest.mark.parametrize("name", ["Bad_Name", "repo@2.git", "alice@corp", "100%25done"])
def test_format_settings_errors_preserves_clean_invalid_marketplace_names(name: str) -> None:
    """干净非法名（非凭据形态）逐字可读——负对照，反「一律遮蔽」的过度矫正。"""
    lines = format_settings_errors(
        [
            SettingsValidationError(
                scope=SettingsScope.USER,
                field=f"extraKnownMarketplaces.{name}",
                reason="marketplace name must be strict kebab (1-64)",
            )
        ]
    )
    assert lines == [
        f"⚠ settings.json[user]: extraKnownMarketplaces.{name} — marketplace name must be strict kebab (1-64)"
    ]


def test_print_unknown_subcommand_escapes_markup(capsys: pytest.CaptureFixture[str]) -> None:
    """#295（复审 🟡）：REPL 未知子命令提示的 ``sub`` 是用户原样输入——四个分派器共用同一转义点。"""
    from a2c_smcp.computer.cli.commands import print_unknown_subcommand

    print_unknown_subcommand("plugin", "evil[/]x")  # 不得抛 MarkupError

    out = "".join(capsys.readouterr().out.split())
    assert "unknownpluginsubcommand:evil[/]x" in out


# ── #304 item 1：enabledPlugins 非法键的整值名字漏斗 ───────────────────────────
_ENABLED_PLUGINS_REASON = "key must be '<plugin>@<marketplace>' (strict kebab)"


@pytest.mark.parametrize(
    ("key", "forbidden"),
    [
        # 字面 userinfo（单 / 双 `@`）→ 整值 fail-closed。
        ("alice:pw@mp", ("alice:", "pw@mp")),
        ("alice:pw@example.com@mp", ("alice:", "pw@")),
        # 带点号：**整段**过漏斗，禁止按 `.` / `:` / `@` 二次拆分（拆了就会漏出半段）。
        ("a.alice:pw@mp", ("alice:", "pw@mp")),
        ("alice:pw@mp.extra", ("alice:", "pw@mp")),
    ],
)
def test_format_settings_errors_redacts_enabled_plugins_credentials(
    key: str, forbidden: tuple[str, ...],
) -> None:
    """#304 item 1：非法键**整值**当单一不可信名字过 userinfo 漏斗（不拆分、不「含 `:` 即遮」）。"""
    lines = format_settings_errors(
        [
            SettingsValidationError(
                scope=SettingsScope.USER,
                field=f"enabledPlugins.{key}",
                reason=_ENABLED_PLUGINS_REASON,
            )
        ]
    )

    assert lines == [
        f"⚠ settings.json[user]: enabledPlugins.<redacted-git-source> — {_ENABLED_PLUGINS_REASON}"
    ]
    for needle in forbidden:
        assert needle not in lines[0], f"未遮蔽 {needle!r}: {lines[0]}"


@pytest.mark.parametrize("key", ["Bad_Name@mp", "alice@corp@mp", "evil[/]a[/]b", "clean-plugin@clean-mp"])
def test_format_settings_errors_preserves_clean_enabled_plugin_keys(key: str) -> None:
    """干净非法键逐字可读——负对照，反「一律遮蔽」的过度矫正。"""
    lines = format_settings_errors(
        [SettingsValidationError(scope=SettingsScope.USER, field=f"enabledPlugins.{key}", reason=_ENABLED_PLUGINS_REASON)]
    )

    assert lines == [f"⚠ settings.json[user]: enabledPlugins.{key} — {_ENABLED_PLUGINS_REASON}"]


def test_format_settings_errors_redacts_enabled_plugin_key_on_value_error() -> None:
    """值非 bool 分支同样走漏斗（**构造型**：真实校验器的合法键是 strict kebab、产不出凭据形态，
    此用例只钉「两处错误共用同一漏斗」的代码形状）。"""
    lines = format_settings_errors(
        [
            SettingsValidationError(
                scope=SettingsScope.USER,
                field="enabledPlugins.alice:pw@mp",
                reason="value must be bool, got str",
            )
        ]
    )

    assert "alice:" not in lines[0]
    assert "<redacted-git-source>" in lines[0]


def test_format_settings_errors_scrubs_bidi_in_enabled_plugin_key() -> None:
    """名字漏斗自带 Cf 清洗：U+202E（方向覆写）不得透传到展示行（ESC 会被段漏斗转义成 `\\x1b`，
    杀不死去 scrub 的变异，故用 Cf 族）。"""
    lines = format_settings_errors(
        [
            SettingsValidationError(
                scope=SettingsScope.USER,
                field="enabledPlugins.good\u202emp",
                reason=_ENABLED_PLUGINS_REASON,
            )
        ]
    )

    assert "\u202e" not in lines[0]


@pytest.mark.parametrize(
    ("key", "forbidden"),
    [
        # #304 验收点名的四类形态：URL / scp-like / path / 被空白劈开；外加 query-fragment。
        ("https://alice:s3cret@example.com/repo.git@mp", ("alice:", "s3cret")),
        ("alice:pw@host:repo.git@mp", ("alice:", "pw@")),
        ("/home/alice:pw@host/repo@mp", ("alice:", "pw@")),
        ("alice:my pw@corp", ("alice:my", "pw@corp")),
        ("https://example.com/repo.git?token=abc123#frag@mp", ("token=", "abc123", "frag")),
    ],
)
def test_format_settings_errors_redacts_enabled_plugin_credential_shapes(
    key: str, forbidden: tuple[str, ...],
) -> None:
    """#304 item 1 验收向量：凭据形态非法键的 userinfo / query / fragment 均不出现在展示行。"""
    line = format_settings_errors(
        [SettingsValidationError(scope=SettingsScope.USER, field=f"enabledPlugins.{key}", reason=_ENABLED_PLUGINS_REASON)]
    )[0]

    for needle in forbidden:
        assert needle not in line, f"未遮蔽 {needle!r}: {line}"
