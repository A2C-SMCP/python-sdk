# -*- coding: utf-8 -*-
# filename: test_render_vars.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
render 变量替换单元测试（v0.2.1 #65，§9.1）/ Render variable-substitution unit tests。

测试意图 / Test intentions:
- ``${env:VAR}`` 命中 / 缺失（空串 + WARN）；预定义 ``${workspaceFolder}``/``${userHome}``/``${pathSeparator}``；
- ``${input:}`` 仍走回调；混合串拼接；未知占位符保持原样；递归 dict/list。
"""

from __future__ import annotations

import logging

import pytest

from a2c_smcp.computer.inputs.render import ConfigRender


@pytest.fixture(autouse=True)
def attach_project_logger_to_caplog(caplog):
    logger = logging.getLogger("a2c_smcp")
    prev_level, prev_prop = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(prev_level)
        logger.propagate = prev_prop


async def _noop_resolve(_id: str):  # pragma: no cover - 仅当含 ${input:} 时调用
    return f"<{_id}>"


@pytest.mark.asyncio
async def test_env_hit() -> None:
    out = await ConfigRender.arender_str("L=${env:LOG_LEVEL}", _noop_resolve, env={"LOG_LEVEL": "debug"})
    assert out == "L=debug"


@pytest.mark.asyncio
async def test_env_missing_empty_and_warn(caplog) -> None:
    caplog.set_level("WARNING", logger="a2c_smcp")
    out = await ConfigRender.arender_str("L=${env:NOPE}", _noop_resolve, env={})
    assert out == "L="
    assert any("环境变量未定义" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_predefined_vars() -> None:
    vars_ = {"userHome": "/home/u", "pathSeparator": ":"}
    out = await ConfigRender.arender_str("${userHome}${pathSeparator}x", _noop_resolve, variables=vars_)
    assert out == "/home/u:x"


@pytest.mark.asyncio
async def test_predefined_missing_empty_and_warn(caplog) -> None:
    caplog.set_level("WARNING", logger="a2c_smcp")
    out = await ConfigRender.arender_str("${userHome}", _noop_resolve, variables={})
    assert out == ""
    assert any("预定义变量未提供" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_input_still_resolves() -> None:
    async def resolver(_id: str):
        return "RESOLVED"

    out = await ConfigRender.arender_str("v=${input:figma}", resolver)
    assert out == "v=RESOLVED"


@pytest.mark.asyncio
async def test_mixed_input_env_predefined() -> None:
    async def resolver(_id: str):
        return "TOK"

    out = await ConfigRender.arender_str(
        "${input:t}@${env:HOST}:${userHome}",
        resolver,
        variables={"userHome": "/home/u"},
        env={"HOST": "localhost"},
    )
    assert out == "TOK@localhost:/home/u"


@pytest.mark.asyncio
async def test_unknown_placeholder_left_as_is() -> None:
    out = await ConfigRender.arender_str("keep ${unknownVar} here", _noop_resolve)
    assert out == "keep ${unknownVar} here"


@pytest.mark.asyncio
async def test_recursive_dict_and_list() -> None:
    cr = ConfigRender()
    data = {"env": {"LOG": "${env:LV}", "HOME": "${userHome}"}, "args": ["${env:LV}"]}
    out = await cr.arender(data, _noop_resolve, variables={"userHome": "/home/u"}, env={"LV": "info"})
    assert out == {"env": {"LOG": "info", "HOME": "/home/u"}, "args": ["info"]}


@pytest.mark.asyncio
async def test_single_env_placeholder_returns_string() -> None:
    out = await ConfigRender.arender_str("${env:X}", _noop_resolve, env={"X": "val"})
    assert out == "val"


# ---------------------------------------------------------------------------
# #116 概念瘦身：${workspaceFolder} 退出预定义变量 / #116: ${workspaceFolder} no longer predefined
# ---------------------------------------------------------------------------
def test_predefined_vars_slimmed_to_two() -> None:
    """#116: 预定义变量仅剩 userHome / pathSeparator。"""
    from a2c_smcp.computer.inputs.render import _PREDEFINED_VARS

    assert _PREDEFINED_VARS == ("userHome", "pathSeparator")


@pytest.mark.asyncio
async def test_workspace_folder_left_verbatim_even_if_supplied() -> None:
    """#116: 即便调用方在 variables 提供 workspaceFolder，也按未知占位符原样保留。"""
    out = await ConfigRender.arender_str("${workspaceFolder}/x", _noop_resolve, variables={"workspaceFolder": "/work"})
    assert out == "${workspaceFolder}/x"
