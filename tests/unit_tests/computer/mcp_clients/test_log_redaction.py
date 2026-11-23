# -*- coding: utf-8 -*-
# filename: test_log_redaction.py
# @Time    : 2026/11/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""连接失败日志的 secret 脱敏（#289 审查 🔴1）：``params`` 是 materializer **渲染后**配置，原样回显即泄密。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from mcp.client.session_group import StreamableHttpParameters

from a2c_smcp.computer.mcp_clients import base_client as base_client_mod
from a2c_smcp.computer.mcp_clients.base_client import BaseMCPClient
from a2c_smcp.computer.mcp_clients.http_client import HttpMCPClient
from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient


@pytest.mark.asyncio
async def test_connect_failure_log_redacts_rendered_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """``_afail_connect`` 的 WARNING 不得回显渲染后的 headers / URL query（此前 ``truncate(self.params)`` 会）。"""
    client = HttpMCPClient(
        StreamableHttpParameters(
            url="https://api.example.com/v1/SECRET-PATH/sse?token=s3cr3t-query",
            headers={"Authorization": "Bearer s3cr3t-hdr"},
        ),
    )
    client.log_label = "alpha"

    text = await _connect_failure_log(client, monkeypatch, caplog)

    assert "MCP connect failed" in text
    assert "alpha" in text, "打码后仍须能定位是哪个 server（非 secret 身份，终审 🟡7）"
    assert "SECRET-PATH" not in text, "URL path 段同样可能承载渲染后 secret（终审 🔴2）"
    assert "s3cr3t-hdr" not in text and "s3cr3t-query" not in text
    assert '"Authorization": "***"' in text  # 键名保留，便于定位是哪个 header 涉及的连接


async def _connect_failure_log(client: BaseMCPClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> str:
    """驱动 ``_afail_connect`` 的 WARNING（关闭拆除/收敛副作用，只读日志）。"""
    task = asyncio.create_task(asyncio.sleep(0))
    await task  # 已完成的代际：跳过 force-kill 分支
    client._session_keep_alive_task = task

    async def _noop_close(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(client, "_close_task", _noop_close)
    monkeypatch.setattr(client, "_spawn_convergence", lambda *_args, **_kwargs: None)

    base_client_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=base_client_mod.logger.name)
    try:
        await client._afail_connect(task, "initialize() failed")
    finally:
        base_client_mod.logger.removeHandler(caplog.handler)
    return caplog.text


@pytest.mark.asyncio
async def test_connect_failure_log_redacts_rendered_args(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """stdio 的 ``command`` / ``args`` 同样可能含渲染后 secret（``${input:}`` 逐字段展开）→ 必须打码。"""
    from mcp import StdioServerParameters

    client = StdioMCPClient(StdioServerParameters(command="npx", args=["--token", "ghp_SECRET_ABC"]))

    text = await _connect_failure_log(client, monkeypatch, caplog)

    assert "MCP connect failed" in text
    assert "ghp_SECRET_ABC" not in text and "npx" not in text
    assert '"args": ["***", "***"]' in text


def test_debug_params_short_circuits_when_debug_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEBUG 关闭时不序列化（#289 终审 🟡10：f-string 无法惰性，需显式守卫）。"""
    client = HttpMCPClient(
        StreamableHttpParameters(url="https://api.example.com/mcp", headers={"Authorization": "Bearer s3cr3t-hdr"}),
    )
    monkeypatch.setattr(base_client_mod.logger, "isEnabledFor", lambda _level: False)
    assert client._debug_params().startswith("<omitted")

    monkeypatch.setattr(base_client_mod.logger, "isEnabledFor", lambda _level: True)
    text = client._debug_params()
    assert "s3cr3t-hdr" not in text
    assert '"Authorization": "***"' in text
