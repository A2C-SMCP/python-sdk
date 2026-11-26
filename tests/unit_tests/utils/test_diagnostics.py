# -*- coding: utf-8 -*-
# filename: test_diagnostics.py
# @Time    : 2026/11/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""诊断安全出口（#289 审查 🔴）：pydantic ``input_value`` 与连接参数 ``env`` / ``headers`` 的脱敏。"""

from __future__ import annotations

from typing import Any

import pytest
from mcp import StdioServerParameters
from mcp.client.session_group import StreamableHttpParameters
from pydantic import TypeAdapter, ValidationError

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.utils.diagnostics import loggable_params, pydantic_error_text


def _validation_error(payload: dict[str, Any]) -> ValidationError:
    with pytest.raises(ValidationError) as ei:
        TypeAdapter(MCPServerConfig).validate_python(payload)
    return ei.value


def test_pydantic_error_text_omits_input_value() -> None:
    """``ValidationError.__str__`` 会带 ``input_value=…``；安全出口只报字段/类型/条数。"""
    err = _validation_error({"name": "x", "type": "streamable-http", "server_parameters": {"url": "SECRET_TOKEN_abc"}})

    assert "SECRET_TOKEN_abc" in str(err)  # 前提：原文本确实泄露（否则本用例无判别力）
    text = pydantic_error_text(err)
    assert "SECRET_TOKEN_abc" not in text
    assert "validation error(s) in [" in text
    assert "type:literal_error" in text


def test_loggable_params_redacts_env_headers_command_and_args() -> None:
    """``env``/``headers`` 值、``command``、``args`` 逐项一律打码（键名与 args 条数保留）；host 仍可见。"""
    stdio = StdioServerParameters(command="node", args=["server.js", "--flag"], env={"API_TOKEN": "s3cr3t-env"})
    text = loggable_params(stdio)
    assert "s3cr3t-env" not in text
    assert '"API_TOKEN": "***"' in text
    assert "node" not in text and "server.js" not in text and "--flag" not in text  # ${input:} 可解析进任一位置
    assert '"command": "***"' in text
    assert '"args": ["***", "***"]' in text, "保留条数便于排障，值 fail-closed"

    http = StreamableHttpParameters(url="https://api.example.com/mcp", headers={"Authorization": "Bearer s3cr3t-hdr"})
    text = loggable_params(http)
    assert "s3cr3t-hdr" not in text
    assert '"Authorization": "***"' in text
    assert "api.example.com" in text


def test_loggable_params_redacts_args_carrying_rendered_secret() -> None:
    """``args`` 里的渲染后 token（``["--token", "ghp_…"]``）不得出现在日志（#289 终审 🔴1）。"""
    text = loggable_params(StdioServerParameters(command="npx", args=["--token", "ghp_SECRET_ABC"]))

    assert "ghp_SECRET_ABC" not in text
    assert "npx" not in text


def test_loggable_params_redacts_sequence_style_headers() -> None:
    """httpx 风格的 headers 序列 ``[(name, value)]``：保留 header 名，值打码。"""
    text = loggable_params({"url": "https://api.example.com/mcp", "headers": [("Authorization", "Bearer s3cr3t-seq")]})

    assert "s3cr3t-seq" not in text
    assert "Authorization: ***" in text


def test_loggable_params_redacts_url_userinfo_and_query() -> None:
    """URL 去 userinfo / query（token 常放这里）；host 仍可见。"""
    http = StreamableHttpParameters(url="https://alice:s3cr3t-pw@api.example.com/mcp?token=s3cr3t-query")

    text = loggable_params(http)

    assert "alice" not in text and "s3cr3t-pw" not in text and "s3cr3t-query" not in text
    assert "api.example.com" in text


def test_loggable_params_redacts_url_path_segments() -> None:
    """``${input:token}`` 可嵌入 URL path（``/v1/<secret>/sse``）→ 整段 path fail-closed 遮蔽（#289 终审 🔴2）。"""
    text = loggable_params({"url": "https://api.example.com/v1/SECRET-PATH/sse"})

    assert "SECRET-PATH" not in text
    assert "api.example.com" in text
    assert "<redacted-path>" in text


def test_loggable_params_never_raises() -> None:
    """日志安全出口不得抛（否则顶替原异常）。"""

    class _Boom:
        def __str__(self) -> str:
            raise RuntimeError("str boom")

    assert loggable_params(_Boom()).startswith("<unprintable")


def test_loggable_params_redacts_url_scheme_carrying_secret() -> None:
    """scheme 也可由 ``${input:}`` 渲染（``"${input:tok}://host"``）→ 非 allow-list scheme 整条遮蔽（终审 🟡4）。"""
    text = loggable_params({"url": "SECRET://api.example.com/mcp"})

    assert "SECRET" not in text
    assert text.count("***") >= 1


def test_loggable_params_fail_closed_on_unknown_scalars() -> None:
    """未知字符串键一律打码、数值/布尔放行（与模块文档承诺一致，终审 🟡3）。"""
    text = loggable_params({"mystery": "SECRET-VALUE", "timeout": 30, "terminate_on_close": True})

    assert "SECRET-VALUE" not in text
    assert '"timeout": 30' in text
    assert '"terminate_on_close": true' in text
