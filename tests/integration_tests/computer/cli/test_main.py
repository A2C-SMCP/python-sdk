# -*- coding: utf-8 -*-
# filename: test_main_integration.py
# 基于真实 stdio MCP Server 的 CLI 集成测试
from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters
from typer.testing import CliRunner

import a2c_smcp.computer.cli.main as cli_main
from a2c_smcp.computer.cli.main import _interactive_loop
from a2c_smcp.computer.computer import Computer


class FakePromptSession:
    def __init__(self, commands: list[str]) -> None:
        self._commands = commands

    async def prompt_async(self, *_: str, **__: Any) -> str:
        if not self._commands:
            raise EOFError
        return self._commands.pop(0)


@contextmanager
def no_patch_stdout():
    yield


@pytest.mark.asyncio
async def test_cli_with_real_stdio(
    stdio_params: StdioServerParameters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    集成测试：通过 CLI 交互完成以下流程（使用真实 stdio MCP server 参数）：
    1) 添加 server 配置（disabled=false）
    2) 启动该 server
    3) 列出工具与状态
    4) 停止该 server
    5) 退出
    期望：流程执行无异常。
    """
    # #137 ②：REPL `server add` 现为 durable 落盘——隔离 cwd/XDG 到 tmp，防写真实仓库 .tfrobot/。
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    server_cfg = {
        "name": "it-stdio",
        "type": "stdio",
        "disabled": False,
        "forbidden_tools": [],
        "tool_meta": {},
        "server_parameters": json.loads(stdio_params.model_dump_json()),
    }

    commands = [
        f"server add {json.dumps(server_cfg)}",
        "start it-stdio",
        "tools",
        "status",
        "stop it-stdio",
        "exit",
    ]

    # Patch interactive IO
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)

    await _interactive_loop(comp)


class FakeSMCPClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        self.connected = False
        self.connect_args: dict[str, Any] | None = None
        FakeSMCPClient.last = self  # type: ignore[attr-defined]

    async def connect(self, url: str, auth: dict[str, Any] | None = None, headers: dict[str, Any] | None = None) -> None:
        self.connected = True
        self.connect_args = {"url": url, "auth": auth, "headers": headers}


@pytest.mark.asyncio
async def test_cli_socket_connect_guided_inputs_without_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    集成层面验证 CLI 的交互式引导输入 URL/Auth/Headers 的行为，但不依赖真实网络。
    """
    # Patch client to fake
    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)

    commands = [
        "socket connect",
        "http://127.0.0.1:9000",
        "apikey:xyz",
        "app:demo,build:42",
        "exit",
    ]

    # Patch interactive IO
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)

    last: FakeSMCPClient = FakeSMCPClient.last  # type: ignore[assignment]
    assert last.connected is True
    assert last.connect_args == {
        "url": "http://127.0.0.1:9000",
        "auth": {"apikey": "xyz"},
        "headers": {"app": "demo", "build": "42"},
    }


# ------------------------------
# 测试 --computer-factory 集成路径（通过 Typer CLI）
# ------------------------------


class _DummyInteractive:
    called: bool = False
    last_comp: Any | None = None

    @classmethod
    async def coro(cls, comp: Any, init_client: Any | None = None, **kwargs: Any) -> None:  # noqa: ARG003
        # **kwargs 吸收 CLI 演进新增的关键字参数（如 #69 的 approve_all_mcp）；本替身仅捕获调用，不断言其值。
        # **kwargs absorbs CLI-evolving keyword args (e.g. #69's approve_all_mcp); this double only captures the call.
        cls.called = True
        cls.last_comp = comp


class _FakeComputer:
    """轻量 Computer 替身，匹配构造参数与异步上下文协议。"""

    def __init__(
        self,
        name: str,
        inputs: set[Any] | None = None,
        mcp_servers: set[Any] | None = None,
        auto_connect: bool = True,
        auto_reconnect: bool = True,
        confirm_callback: Callable[[str, str, str, dict], bool] | None = None,
        input_resolver: Any | None = None,
        registered_workdirs: Any | None = None,  # #69/S16：CLI --add-dir → registered_workdirs，替身需接受

        mcp_flag_config: Any | None = None) -> None:
        self.init_args = {
            "name": name,
            "inputs": inputs,
            "mcp_servers": mcp_servers,
            "auto_connect": auto_connect,
            "auto_reconnect": auto_reconnect,
            "confirm_callback": confirm_callback,
            "input_resolver": input_resolver,
            "registered_workdirs": registered_workdirs,
        }

    async def __aenter__(self) -> _FakeComputer:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


@pytest.fixture(autouse=True)
def _reset_dummy_interactive() -> None:
    _DummyInteractive.called = False
    _DummyInteractive.last_comp = None


def test_cli_root_with_computer_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """根路径（无子命令）携带 --computer-factory 时应调用解析的工厂。"""
    runner = CliRunner()

    # 工厂: 返回 _FakeComputer，并计数
    calls: dict[str, Any] = {"count": 0}

    def factory(**kwargs: Any) -> _FakeComputer:
        calls["count"] += 1
        return _FakeComputer(**kwargs)

    monkeypatch.setattr(cli_main, "resolve_import_target", lambda s: factory, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", _DummyInteractive.coro, raising=True)

    result = runner.invoke(cli_main.app, ["--computer-factory", "pkg.mod:factory"])  # noqa: S603

    assert result.exit_code == 0
    assert calls["count"] == 1
    assert _DummyInteractive.called is True
    assert isinstance(_DummyInteractive.last_comp, _FakeComputer)


def test_cli_run_with_computer_factory_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """当解析到的目标不可调用时，CLI 应回退到默认 Computer；为便于断言，替换为 _FakeComputer。"""
    runner = CliRunner()

    monkeypatch.setattr(cli_main, "resolve_import_target", lambda s: object(), raising=True)
    monkeypatch.setattr(cli_main, "Computer", _FakeComputer, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", _DummyInteractive.coro, raising=True)

    result = runner.invoke(
        cli_main.app,
        [
            "run",
            "--computer-factory",
            "x.y:bad",
            "--auto-connect",
            "--auto-reconnect",
        ],
    )

    assert result.exit_code == 0
    assert _DummyInteractive.called is True
    assert isinstance(_DummyInteractive.last_comp, _FakeComputer)
    assert _DummyInteractive.last_comp.init_args["auto_connect"] is True
    assert _DummyInteractive.last_comp.init_args["auto_reconnect"] is True


# ------------------------------
# #97：根选项 --settings / --add-dir 透传到 settings 子命令
# Root options --settings / --add-dir must reach the `settings` subcommands
# ------------------------------
# 接线层回归：Typer 包装器 _settings_show/_get/_set 此前调 handler 时未传 flag_path /
# active_workdir，导致 `settings show --scope flag` 恒返回 {}（merged 视图也丢 flag 字段），
# 且 `settings set --scope project` 因缺 active_workdir 误报。以下 spy 测试确定性验证「根上
# 下文是否抵达 handler」，绕开 logging 写 stdout 的输出污染。/ Wiring regression for #97.


def _spy_handler(recorded: dict[str, Any]) -> Callable[..., int]:
    """返回一个记录入参 kwargs 的 handler 替身（返回退出码 0）/ A spy recording call kwargs."""

    def _spy(*_args: Any, **kwargs: Any) -> int:
        recorded.update(kwargs)
        return 0

    return _spy


def test_root_settings_flag_propagates_to_settings_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--settings <f> settings show --scope flag` 应把 flag_path 透传给 settings_show。"""
    runner = CliRunner()
    flag_file = tmp_path / "flag.json"
    flag_file.write_text(json.dumps({"testFlag": True, "flagKey": "flagValue"}), encoding="utf-8")
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(cli_main.settings_cmd, "settings_show", _spy_handler(recorded), raising=True)

    result = runner.invoke(cli_main.app, ["--settings", str(flag_file), "settings", "show", "--scope", "flag", "--json"])  # noqa: S603

    assert result.exit_code == 0
    assert recorded.get("flag_path") == flag_file  # 修前为 None（未透传）→ 红


def test_root_settings_flag_propagates_to_settings_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--settings <f> settings get <k> --scope flag` 应把 flag_path 透传给 settings_get。"""
    runner = CliRunner()
    flag_file = tmp_path / "flag.json"
    flag_file.write_text(json.dumps({"flagKey": "flagValue"}), encoding="utf-8")
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(cli_main.settings_cmd, "settings_get", _spy_handler(recorded), raising=True)

    result = runner.invoke(cli_main.app, ["--settings", str(flag_file), "settings", "get", "flagKey", "--scope", "flag"])  # noqa: S603

    assert result.exit_code == 0
    assert recorded.get("flag_path") == flag_file


def test_root_settings_flag_propagates_to_merged_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """merged 视图（默认 scope）同样需要 flag_path 才能合入 flag 层字段。"""
    runner = CliRunner()
    flag_file = tmp_path / "flag.json"
    flag_file.write_text(json.dumps({"testFlag": True}), encoding="utf-8")
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(cli_main.settings_cmd, "settings_show", _spy_handler(recorded), raising=True)

    result = runner.invoke(cli_main.app, ["--settings", str(flag_file), "settings", "show", "--json"])  # noqa: S603

    assert result.exit_code == 0
    assert recorded.get("flag_path") == flag_file


# #116 概念瘦身：--add-dir 已移除 / #116 slimming: --add-dir removed
def test_add_dir_option_removed(tmp_path: Path) -> None:
    """#116: `--add-dir` 不再是合法选项，传入应报未知选项错误。"""
    runner = CliRunner()
    env = {**os.environ, "A2C_SKILL_HOME": str(tmp_path / "skill"), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}

    result = runner.invoke(  # noqa: S603
        cli_main.app, ["--add-dir", str(tmp_path), "settings", "show"], env=env,
    )

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_root_settings_flag_scope_real_output(tmp_path: Path) -> None:
    """端到端复现 G-08：`--settings <f> settings show --scope flag --json` 应输出 flag 文件内容（非 {}）。"""
    runner = CliRunner()
    flag_file = tmp_path / "flag.json"
    payload = {"testFlag": True, "flagKey": "flagValue"}
    flag_file.write_text(json.dumps(payload), encoding="utf-8")
    # 隔离 SKILL Home / XDG，避免触碰真实用户目录 / isolate home to avoid touching real user dirs
    env = {**os.environ, "A2C_SKILL_HOME": str(tmp_path / "skill"), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}

    result = runner.invoke(  # noqa: S603
        cli_main.app, ["--settings", str(flag_file), "settings", "show", "--scope", "flag", "--json"], env=env,
    )

    assert result.exit_code == 0
    out = result.stdout
    parsed = json.loads(out[out.index("{"):])  # 跳过可能的日志行（无花括号）→ 取尾部 JSON
    assert parsed == payload  # 修前为 {} → 红
