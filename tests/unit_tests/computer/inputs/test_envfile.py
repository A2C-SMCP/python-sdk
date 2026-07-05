# -*- coding: utf-8 -*-
# filename: test_envfile.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
envFile 加载 + 合并单元测试（v0.2.1 #65，§9.1）/ envFile load + merge unit tests。

测试意图 / Test intentions:
- ``load_env_file``：KEY=VALUE / 注释 / 引号 / export / 缺文件→{} / 无 '=' 行跳过；
- ``Computer._apply_env_file``：stdio 合并、显式 env 胜、非 stdio（sse）原样、无 envFile 原样；
- 端到端：envFile 路径含 ``${userHome}`` 等占位符经渲染后再加载（_arender_and_validate_server；
  #116 起 ``${workspaceFolder}`` 停产）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inputs.render import load_env_file


@pytest.fixture
def attach_logger_to_caplog(caplog):
    """让项目 logger "a2c_smcp" 的日志被 caplog 捕获（其禁用了 propagate）/ Capture project logger into caplog。"""
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


def test_load_env_file_parsing(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n\nA=1\nexport B=two\nQUOTED=\"q v\"\nSINGLE='s'\nNOEQ line\n",
        encoding="utf-8",
    )
    assert load_env_file(f) == {"A": "1", "B": "two", "QUOTED": "q v", "SINGLE": "s"}


def test_load_env_file_missing(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == {}


def _comp() -> Computer:
    return Computer(
        name="t",
        inputs=set(),
        mcp_servers=set(),
        auto_connect=False,
        auto_reconnect=False,
    )


def test_apply_env_file_explicit_wins(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=fromfile\nB=fromfile\n", encoding="utf-8")
    comp = _comp()
    rendered = {
        "name": "s",
        "type": "stdio",
        "server_parameters": {"command": "node", "env": {"A": "explicit"}},
        "envFile": str(tmp_path / ".env"),
    }
    out = comp._apply_env_file(rendered)
    # 显式 env 同名项胜；envFile 补充缺失项 / explicit wins, envFile fills the rest
    assert out["server_parameters"]["env"] == {"A": "explicit", "B": "fromfile"}


def test_apply_env_file_no_envfile_passthrough(tmp_path: Path) -> None:
    comp = _comp()
    rendered = {"name": "s", "type": "stdio", "server_parameters": {"command": "node", "env": {"A": "1"}}}
    assert comp._apply_env_file(rendered) == rendered


def test_apply_env_file_non_stdio_passthrough_and_warns(tmp_path: Path, caplog, attach_logger_to_caplog) -> None:
    caplog.set_level("WARNING", logger="a2c_smcp")
    comp = _comp()
    rendered = {"name": "s", "type": "sse", "server_parameters": {"url": "http://x"}, "envFile": str(tmp_path / ".env")}
    # sse/http 填 envFile → 行为仍 passthrough（原样返回）+ 记一条 WARN（#65 fix-review #4）
    assert comp._apply_env_file(rendered) == rendered
    assert any("非 stdio" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_render_then_envfile_with_env_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """envFile 路径占位符经渲染后再加载（#116：${workspaceFolder} 停产，改用 ${env:}/绝对路径）。"""
    (tmp_path / ".env").write_text("FROM_FILE=yes\n", encoding="utf-8")
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    comp = _comp()
    raw = {
        "name": "s",
        "type": "stdio",
        "server_parameters": {"command": "node", "env": {"EXPLICIT": "v"}},
        "envFile": "${env:WORK_DIR}/.env",
    }
    validated = await comp._arender_and_validate_server(raw)
    assert validated.server_parameters.env == {"EXPLICIT": "v", "FROM_FILE": "yes"}
    assert validated.env_file == str(tmp_path / ".env")  # 字段保留、路径已渲染
