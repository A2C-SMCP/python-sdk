# -*- coding: utf-8 -*-
# filename: test_render_log_redaction.py
# @Time    : 2026/11/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""输入解析失败日志的脱敏（#289 终审 🟡9）：自定义 resolver 的异常消息可能携带输入值。"""

from __future__ import annotations

import logging

import pytest

from a2c_smcp.computer.inputs import render as render_mod
from a2c_smcp.computer.inputs.render import ConfigRender


@pytest.mark.asyncio
async def test_resolver_failure_log_redacts_value(caplog: pytest.LogCaptureFixture) -> None:
    async def _boom(_input_id: str) -> str:
        # 通用异常的脱敏边界：URL / 路径 token 可证；**纯裸 token 无法证**（`exception_text` 不臆测，
        # 见 `docs` 说明）——故用可证向量（URL userinfo/query）钉住日志出口。
        raise RuntimeError("vault rejected https://alice:s3cr3t-pw@vault.example.com/x?token=s3cr3t-query")

    render_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR, logger=render_mod.logger.name)
    try:
        await ConfigRender().arender({"name": "x", "server_parameters": {"args": ["${input:x}"]}}, _boom)
    finally:
        render_mod.logger.removeHandler(caplog.handler)

    assert "解析输入失败" in caplog.text
    assert "s3cr3t-pw" not in caplog.text and "s3cr3t-query" not in caplog.text
    assert "unprintable" not in caplog.text  # 走常规脱敏文本，而非兜底
