# -*- coding: utf-8 -*-
"""
文件名: test_interactive_more.py
作者: JQQ
创建日期: 2025/9/29
最后修改日期: 2025/9/29
版权: 2023 JQQ. All rights reserved.
依赖: pytest
描述:
  中文: 覆盖 `interactive_impl.py` 剩余分支：tools、server @file、socket 已连接/未加入、inputs 变体与 inputs value JSON 文本两类。
  English: Cover remaining branches in `interactive_impl.py`: tools, server @file, socket already-connected/not-joined,
    inputs variants, and inputs value JSON vs text.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import a2c_smcp.computer.cli.main as cli_main
from a2c_smcp.computer.cli.main import _interactive_loop
from a2c_smcp.computer.computer import Computer


class FakePromptSession:
    """中文: 将脚本化命令注入交互循环。English: Feed scripted inputs to interactive loop."""

    def __init__(self, commands: list[str]) -> None:
        self._commands = commands

    async def prompt_async(self, *_: str, **__: Any) -> str:  # noqa: D401
        if not self._commands:
            raise EOFError
        return self._commands.pop(0)


@contextmanager
def no_patch_stdout():
    """No-op context manager to replace patch_stdout() in tests."""
    yield


class _Client:
    """中文: 覆盖 socket 分支需要的最小客户端桩对象。English: Minimal client stub for socket branches."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        self.connected = False
        self.office_id: str | None = None
        self.updated = 0
        _Client.last = self  # type: ignore[attr-defined]

    async def connect(self, url: str, auth: dict | None = None, headers: dict | None = None) -> None:  # noqa: D401
        self.connected = True
        self._url = url
        self._auth = auth
        self._headers = headers

    async def join_office(self, office_id: str) -> None:  # noqa: D401
        # 形参须与真实 ``SmcpComputerClient.join_office`` 一致（名字经 ``computer.name`` 走线，
        # 不是形参）——旧桩多一个 ``computer_name`` 形参，令 CLI 的调用抛 TypeError 被笼统 except
        # 吞掉，socket join 分支实际从未被走到。
        assert self.connected
        self.office_id = office_id

    async def leave_office(self, office_id: str) -> None:  # noqa: D401
        assert self.connected
        self.office_id = None

    async def emit_update_config(self) -> None:  # noqa: D401
        self.updated += 1


@pytest.mark.asyncio
async def test_tools_and_inputs_update_single_and_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    - tools: 覆盖 aget_available_tools 分支
    - inputs add @file (数组) 与 update (单条)
    - inputs get 不存在与存在
    - inputs value set: JSON 与 纯文本 两类
    """
    # #137 ②：REPL `server add` 现为 durable 落盘——隔离 cwd/XDG 到 tmp，防写真实仓库 .tfrobot/。
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "SMCPComputerClient", _Client)

    # 通过文件提供 inputs 数组
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(
        json.dumps(
            [
                {"id": "A", "type": "promptString", "description": "d1", "default": "x"},
                {"id": "B", "type": "pickString", "description": "d2", "options": ["1", "2"], "default": "1"},
            ],
        ),
        encoding="utf-8",
    )

    # 服务器配置文件（用于 server add @file 覆盖）
    server_file = tmp_path / "server.json"
    server_file.write_text(
        json.dumps(
            {
                "name": "s1",
                "type": "stdio",
                "disabled": True,
                "forbidden_tools": [],
                "tool_meta": {},
                "server_parameters": {
                    "command": "echo",
                    "args": [],
                    "env": None,
                    "cwd": None,
                    "encoding": "utf-8",
                    "encoding_error_handler": "strict",
                },
            },
        ),
        encoding="utf-8",
    )

    # 提前连接一次以覆盖 "已连接" 分支
    pre_connect = [
        "socket connect http://localhost:9999",
        "tools",
        # inputs add @file (数组)
        f"inputs add @{inputs_file}",
        # get 不存在
        "inputs get NOT_EXIST",
        # update 单条（覆盖 update 的非数组路径）
        'inputs update {"id":"A","type":"promptString","description":"dx","default":"y"}',
        # value set JSON
        'inputs value set A {"k":1}',
        # value set 文本
        "inputs value set A ptext",
        # server add @file 与通知
        f"server add @{server_file}",
        # socket 已连接提示
        "socket connect http://localhost:9999",
        # leave 未加入
        "socket leave",
        "exit",
    ]

    # Monkeypatch 输入与 patch_stdout
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(pre_connect))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    # 注入 tools 的桩实现
    comp = Computer(name="test_im_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)

    async def _fake_tools() -> list[dict[str, Any]]:
        return [{"name": "t1", "description": "desc", "return_schema": {}}]

    monkeypatch.setattr(comp, "aget_available_tools", _fake_tools)

    await _interactive_loop(comp)

    # 断言 server add 时触发了配置更新
    last: _Client = _Client.last  # type: ignore[assignment]
    assert last.updated >= 1


@pytest.mark.asyncio
async def test_server_rm_and_start_stop_single_with_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    - 覆盖 server rm <name> 的 happy path
    - 覆盖 start/stop <name> 的异常路径（触发 except 分支）
    """
    monkeypatch.setattr(cli_main, "SMCPComputerClient", _Client)

    commands = [
        # 初始化 manager 后再进入交互
        "exit",
    ]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_im_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await comp.boot_up()

    # 准备第二段命令：rm + start/stop 单个 + 异常
    cmds2 = [
        "server rm not-exist",  # aremove_server 安静返回，不影响覆盖
        "start one",
        "stop one",
        "exit",
    ]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(cmds2))

    # 注入 manager 的异常行为
    class BadMgr(type(comp.mcp_manager)):  # type: ignore[misc]
        async def astart_client(self, name: str) -> None:  # type: ignore[override]
            raise RuntimeError("boom-start")

        async def astop_client(self, name: str) -> None:  # type: ignore[override]
            raise RuntimeError("boom-stop")

    comp.mcp_manager.__class__ = BadMgr  # type: ignore[attr-defined]

    await _interactive_loop(comp)


def test_print_degraded_escapes_markup(capsys: pytest.CaptureFixture[str]) -> None:
    """#295（复审 🟡）：降级兜底自身不得因 markup 再抛（``except`` 内的错误转换不得可抛）。

    ``MarkupError`` 的正文本身就含 ``[/]``——未转义时「批准门控初始化失败」这行会二次抛出，
    逸出到 ``_interactive_loop`` 外层（``main`` 无捕获）令 REPL 直接 traceback 退出。
    """
    from rich.errors import MarkupError

    from a2c_smcp.computer.cli.interactive_impl import _print_degraded

    _print_degraded("MCP 批准门控初始化失败 / approval init failed", MarkupError("closing tag '[/]' doesn't match any open tag"))

    out = "".join(capsys.readouterr().out.split())
    assert "[/]" in out  # 异常原文可见，且打印本身不抛


@pytest.mark.asyncio
async def test_interactive_degradations_route_to_safe_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """#295（复审 🟡4）：三处 catch-all 兜底的**接线**——必须走 `_print_degraded`（其自身 escape 由 helper 单测钉住）。

    `MarkupError` 正文自带 `[/]`：兜底行若改回裸 `console.print(f"...{e}")` 会再抛，逸出 `_interactive_loop`
    （`main` 无捕获）令交互模式 traceback 退出。本用例走真实调用点：审批门控 / 治理重挂 / 命中命令各抛一次。
    """
    from rich.errors import MarkupError

    import a2c_smcp.computer.cli.interactive_impl as interactive_impl

    seen: list[tuple[str, str, str]] = []

    def _record(message: str, exc: BaseException, *, style: str = "yellow") -> None:
        seen.append((message, style, type(exc).__name__))

    monkeypatch.setattr(interactive_impl, "_print_degraded", _record)

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise MarkupError("closing tag '[/]' at position 0 doesn't match any open tag")

    monkeypatch.setattr("a2c_smcp.computer.cli.commands.plugin.run_mcp_approval", _boom)
    monkeypatch.setattr("a2c_smcp.computer.cli.commands.plugin.run_governance_remount", _boom)
    monkeypatch.setattr("a2c_smcp.computer.cli.commands.settings.repl_dispatch", _boom)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(["settings get x"]))
    comp = Computer(name="test_im_deg", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)

    await _interactive_loop(comp)  # 三处兜底均不得抛出

    # 断言接线的**语义**（消息文本 + style + 被捕获异常类型），而非仅次数。
    assert [message for message, _style, _type in seen] == [
        "⚠ MCP 批准门控初始化失败 / MCP approval init failed",
        "⚠ 治理重挂失败 / governance remount failed",
        "执行失败 / Failed",
    ]
    assert [style for _message, style, _type in seen] == ["yellow", "yellow", "red"]
    assert all(exc_type == "MarkupError" for _message, _style, exc_type in seen)
