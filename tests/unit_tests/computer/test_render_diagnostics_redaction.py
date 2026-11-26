# -*- coding: utf-8 -*-
# filename: test_render_diagnostics_redaction.py
# @Time    : 2026/11/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""渲染/校验失败日志的 secret 脱敏（#289 审查 🔴2）：ERROR 日志不得回显 pydantic ``input_value`` 或 traceback。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from a2c_smcp.computer import computer as computer_mod
from a2c_smcp.computer.computer import Computer


@pytest.mark.asyncio
async def test_render_validation_failure_log_redacts_input_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """渲染结果（可能含已解析 secret）校验失败 → ERROR 日志只报字段/条数；原异常仍原样上抛（库内可追溯）。"""
    comp = Computer(name="t", skill_home=tmp_path / "home")

    async def _leaky_render(raw: dict[str, Any], resolve: Any, *, variables: Any = None) -> dict[str, Any]:
        # 模拟「渲染后配置含 secret 结构」：url 被解析成结构化值，concrete model 校验必失败。
        return {"name": "leaky", "type": "streamable", "server_parameters": {"url": {"token": "SECRET-STRUCT"}}}

    monkeypatch.setattr(comp._config_render, "arender", _leaky_render)
    from pydantic import TypeAdapter

    from a2c_smcp.computer.mcp_clients.model import MCPServerConfig

    with pytest.raises(ValidationError) as raw:
        TypeAdapter(MCPServerConfig).validate_python(await _leaky_render({}, None))
    assert "SECRET-STRUCT" in str(raw.value)  # 前提：裸文本确实泄露（否则本用例无判别力）

    computer_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR, logger=computer_mod.logger.name)
    try:
        with pytest.raises(ValidationError) as ei:
            await comp._arender_and_validate_server(
                {"name": "leaky", "type": "streamable", "server_parameters": {"url": "https://api.example.com/mcp"}},
            )
    finally:
        computer_mod.logger.removeHandler(caplog.handler)

    assert "动态渲染/校验MCP配置失败" in caplog.text
    assert "SECRET-STRUCT" not in caplog.text
    assert "validation error(s) in [" in caplog.text
    # 库内调用方仍拿得到原始异常（只收敛日志面）
    assert isinstance(ei.value, ValidationError)


@pytest.mark.asyncio
async def test_boot_up_log_redacts_downstream_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """§5.13 boot 的真实启动路径：`ainitialize` 内 materialize 失败的 ValidationError 在 **boot 收口日志**里
    也不得回显 ``input_value``（#289 终审 🔴2：只堵渲染处不够，下游会再记一次）。"""
    from mcp import StdioServerParameters

    from a2c_smcp.computer.mcp_clients.model import StdioServerConfig

    comp = Computer(
        name="t",
        skill_home=tmp_path / "home",
        mcp_servers={StdioServerConfig(name="leaky", server_parameters=StdioServerParameters(command="node"))},
    )

    async def _leaky_render(raw: dict[str, Any], resolve: Any, *, variables: Any = None) -> dict[str, Any]:
        # 渲染后配置含 secret 结构：command 被解析成 dict，concrete model 校验必失败（union 分支的 input_value
        # 会带出整个 server_parameters，含 env）。
        return {
            "name": "leaky",
            "type": "stdio",
            "server_parameters": {"command": {"token": "SECRET-BOOT"}, "env": {"API_TOKEN": "s3cr3t-boot-env"}},
        }

    monkeypatch.setattr(comp._config_render, "arender", _leaky_render)

    # 挂到**包根** logger（子 logger 的 propagate 到它为止）：manager / computer 等下游任何一处记录
    # 原始 ValidationError 都会落进 caplog，避免「只测我方 logger」的盲区。
    package_logger = logging.getLogger("a2c_smcp")
    package_logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="a2c_smcp")
    try:
        with pytest.raises(ValidationError):
            await comp.boot_up()
    finally:
        package_logger.removeHandler(caplog.handler)

    assert "boot 初始化失败" in caplog.text
    assert "SECRET-BOOT" not in caplog.text
    assert "s3cr3t-boot-env" not in caplog.text
