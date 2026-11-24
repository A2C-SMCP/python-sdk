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
    assert "\\[" not in out  # 无双重转义（字面 \[/] 也算失败；与其他 escape 用例同口径）


def _validation_error_with_secret(secret: str) -> Exception:
    """构造带 ``input_value=<secret>`` 的 pydantic ValidationError（模拟 #289 的泄露形态）。"""
    from pydantic import BaseModel

    class _Payload(BaseModel):
        token: int  # 传字符串即触发 int_parsing，input_value 就是那个 secret

    try:
        _Payload(token=secret)  # type: ignore[arg-type]
    except Exception as exc:  # ValidationError
        return exc
    raise AssertionError("expected ValidationError")


@pytest.mark.asyncio
async def test_interactive_degradations_escape_markup_and_redact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#295 + #289 合并面：三处 catch-all 的**真实调用点**都要 markup 安全且不回显 pydantic payload。

    - 审批门控 → :func:`_print_degraded`（`escape(exception_text(exc))`）；
    - 治理重挂 → develop 的内联 `escape(exception_text(e))`；
    - 命中命令 → `_repl_failure_line(e)`。
    任何一处写回裸 ``console.print(f"...{e}")`` 都会让 `MarkupError` 二次抛出、REPL traceback；
    任何一处写回 ``escape(str(e))`` 则会把 pydantic ``input_value``（渲染后 secret）回显（#289 🔴 回归）。
    """
    from rich.errors import MarkupError

    async def _markup_boom(*_a: Any, **_k: Any) -> None:
        raise MarkupError("closing tag '[/]' at position 0 doesn't match any open tag")

    async def _dispatch_boom(*_a: Any, **_k: Any) -> None:
        raise _validation_error_with_secret("SUPER_SECRET_TOKEN")

    monkeypatch.setattr("a2c_smcp.computer.cli.commands.plugin.run_mcp_approval", _markup_boom)
    # 治理重挂抛 pydantic：覆盖 develop 内联行的 `exception_text` 脱敏（不能回显 input_value）。
    monkeypatch.setattr("a2c_smcp.computer.cli.commands.plugin.run_governance_remount", _dispatch_boom)
    # 命中命令抛 pydantic：覆盖 `_repl_failure_line` 的脱敏 + escape。
    monkeypatch.setattr("a2c_smcp.computer.cli.commands.settings.repl_dispatch", _dispatch_boom)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(["settings get x"]))
    # 与文件内其余 `_interactive_loop` 用例一致：禁用真 `patch_stdout`（prompt_toolkit 进程级
    # `AppSession.output` 会永久绑定本用例的捕获流，污染后续真 `patch_stdout` 用例）。
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())
    comp = Computer(name="test_im_deg", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)

    await _interactive_loop(comp)  # 三处兜底均不得抛出

    out = "".join(capsys.readouterr().out.split())
    assert "MCP批准门控初始化失败" in out
    assert "治理重挂失败" in out
    assert "执行失败" in out
    assert "[/]" in out  # MarkupError 原文可见（rich 已还原转义）
    assert "\\[" not in out  # 无双重转义
    assert "SUPER_SECRET_TOKEN" not in out  # #289：两处 pydantic 兜底均不回显 input_value
