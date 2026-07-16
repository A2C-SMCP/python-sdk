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
