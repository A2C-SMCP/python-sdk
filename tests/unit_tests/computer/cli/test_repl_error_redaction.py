# -*- coding: utf-8 -*-
# filename: test_repl_error_redaction.py
# @Time    : 2026/11/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""REPL 分发兜底的脱敏与转义（#289 终审 🔴1）::

``server add`` / ``inputs`` 的 wire TypedDict 校验发生在内层 ``try`` 之外——pydantic ``ValidationError`` 会
回显整个 payload（env / options 值），由 REPL 循环级兜底 ``_repl_failure_line`` 收口；``loc`` 中的用户可控 key
（``env.X[/red]``）必须先 ``escape`` 再交给 Rich。
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError
from rich.text import Text

from a2c_smcp.computer.cli.interactive_impl import _repl_failure_line
from a2c_smcp.smcp import MCPServerConfig as SMCPServerConfigDict


def _payload_error() -> ValidationError:
    with pytest.raises(ValidationError) as ei:
        TypeAdapter(SMCPServerConfigDict).validate_python(
            {
                "name": "x",
                "type": "stdio",
                # 缺 disabled / forbidden_tools / tool_meta（wire 必填）⇒ 错误 input_value 为整个 payload；
                # env 里的整形值是非法类型，其 loc 会带出用户可控 key。
                "server_parameters": {"command": "node", "env": {"X[/red]": 123, "API_TOKEN": "sk-live-SECRET"}},
            },
        )
    return ei.value


def test_repl_failure_line_redacts_payload_and_keeps_markup_valid() -> None:
    """前提（原文本确实泄露）+ 兜底文案不含 secret、且 Rich markup 合法（不抛 MarkupError）。"""
    err = _payload_error()
    assert "sk-live-SECRET" in str(err), "前提：裸 ValidationError 确实回显 payload（否则本用例无判别力）"

    line = _repl_failure_line(err)

    assert "sk-live-SECRET" not in line
    assert "\\[/red]" in line, "loc 中的用户可控 key 必须被 rich 转义"
    Text.from_markup(line)  # 未转义会抛 MarkupError
