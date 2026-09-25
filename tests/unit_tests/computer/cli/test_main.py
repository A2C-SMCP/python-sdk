"""
合并版 CLI 单测，包含基础与扩展用例
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

import a2c_smcp.computer.cli.main as cli_main
from a2c_smcp.computer.cli.main import _interactive_loop
from a2c_smcp.computer.computer import Computer
from a2c_smcp.smcp import LEAVE_OFFICE_EVENT


class DummyInteractive:
    called: bool = False
    last_comp: Any | None = None
    last_init_client: Any | None = None

    @classmethod
    async def coro(cls, comp: Any, init_client: Any | None = None, **_: Any) -> None:  # matches _interactive_loop signature (+ #69 kwargs)
        cls.called = True
        cls.last_comp = comp
        cls.last_init_client = init_client


class FakeComputer:
    """A lightweight fake that matches Computer's init signature and async context manager."""

    def __init__(
        self,
        name: str,
        inputs: set[Any] | None = None,
        mcp_servers: set[Any] | None = None,
        auto_connect: bool = True,
        auto_reconnect: bool = True,
        confirm_callback: Callable[[str, str, str, dict], bool] | None = None,
        input_resolver: Any | None = None,
        registered_workdirs: Any | None = None,
        mcp_flag_config: Any | None = None,
        flag_settings_path: Any | None = None,
    ) -> None:
        self.init_args = {
            "inputs": inputs,
            "mcp_servers": mcp_servers,
            "auto_connect": auto_connect,
            "auto_reconnect": auto_reconnect,
            "confirm_callback": confirm_callback,
            "input_resolver": input_resolver,
            "registered_workdirs": registered_workdirs,
            "mcp_flag_config": mcp_flag_config,
            "flag_settings_path": flag_settings_path,
        }
        # #208：记录 with_mcp_start_concurrency 的安装（缺省应为空 ⇒ 保持串行）
        self.concurrency_calls: list[int] = []

    def with_mcp_start_concurrency(self, max_concurrency: int) -> FakeComputer:
        self.concurrency_calls.append(max_concurrency)
        return self

    async def __aenter__(self) -> FakeComputer:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


def test_run_impl_forwards_init_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_run_impl`` 必须把 ``init_connection`` 透传给 ``_interactive_loop``（承重接线，无其它覆盖）。

    ``init_connection`` 是改名重连唯一的数据来源；把这一行删掉 mypy 不报、其余用例也不会红（假客户端
    在 ``_run_impl`` 路径上会把 kwargs 吞掉）——改名会静默退化成「未记录连接参数」提示。
    """
    from a2c_smcp.computer.socketio.client import SMCPComputerClient

    monkeypatch.setattr(cli_main, "Computer", FakeComputer, raising=True)

    # 只替掉 I/O：`_run_impl` 用真实客户端建连，否则会真去连 boot:7000
    async def _noop(self: Any, *a: Any, **kw: Any) -> None:
        return None

    for name in ("connect", "join_office", "leave_office", "emit_update_config"):
        monkeypatch.setattr(SMCPComputerClient, name, _noop)

    captured: dict[str, Any] = {}

    async def _spy(comp: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "_interactive_loop", _spy, raising=True)

    cli_main._run_impl(
        auto_connect=False,
        auto_reconnect=False,
        url="http://boot:7000",
        namespace="/tf-custom",
        auth="token:t",
        headers="X-TF:1",
        computer_factory=None,
        mcp_config=None,
    )

    assert captured.get("init_client") is not None, "应已用启动参数建好客户端"
    assert captured.get("init_connection") == {
        "url": "http://boot:7000",
        "namespace": "/tf-custom",
        "auth": {"token": "t"},
        "headers": {"X-TF": "1"},
    }, f"启动参数须原样透传（改名重连的唯一来源），实得 {captured.get('init_connection')!r}"


def test_run_impl_uses_default_computer_when_no_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch Computer to our fake and _interactive_loop to a dummy coro
    monkeypatch.setattr(cli_main, "Computer", FakeComputer, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", DummyInteractive.coro, raising=True)

    # Call implementation with no factory and no side-effect options
    cli_main._run_impl(
        auto_connect=True,
        auto_reconnect=True,
        url=None,
        namespace=None,
        auth=None,
        headers=None,
        computer_factory=None,
        mcp_config=None,
    )

    assert DummyInteractive.called is True
    assert isinstance(DummyInteractive.last_comp, FakeComputer)
    assert DummyInteractive.last_comp.init_args["auto_connect"] is True
    assert DummyInteractive.last_comp.init_args["auto_reconnect"] is True


def test_run_impl_uses_resolved_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prepare a factory that returns our FakeComputer
    calls: dict[str, Any] = {"count": 0}

    def factory(**kwargs: Any) -> FakeComputer:
        calls["count"] += 1
        return FakeComputer(**kwargs)

    # Patch resolver to return our factory; patch interactive loop to avoid blocking
    monkeypatch.setattr(cli_main, "resolve_import_target", lambda s: factory, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", DummyInteractive.coro, raising=True)

    cli_main._run_impl(
        auto_connect=False,
        auto_reconnect=False,
        url=None,
        namespace=None,
        auth=None,
        headers=None,
        computer_factory="some.module:factory",
        mcp_config=None,
    )

    assert calls["count"] == 1
    assert isinstance(DummyInteractive.last_comp, FakeComputer)
    assert DummyInteractive.last_comp.init_args["auto_connect"] is False
    assert DummyInteractive.last_comp.init_args["auto_reconnect"] is False


def test_run_impl_factory_not_callable_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make resolve_import_target return a non-callable
    monkeypatch.setattr(cli_main, "resolve_import_target", lambda s: object(), raising=True)
    # Patch Computer fallback to our FakeComputer
    monkeypatch.setattr(cli_main, "Computer", FakeComputer, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", DummyInteractive.coro, raising=True)

    cli_main._run_impl(
        auto_connect=True,
        auto_reconnect=True,
        url=None,
        namespace=None,
        auth=None,
        headers=None,
        computer_factory="x.y:bad",
        mcp_config=None,
    )

    assert isinstance(DummyInteractive.last_comp, FakeComputer)


def test_run_impl_resolve_error_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_: str) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(cli_main, "resolve_import_target", _raise, raising=True)
    monkeypatch.setattr(cli_main, "Computer", FakeComputer, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", DummyInteractive.coro, raising=True)

    cli_main._run_impl(
        auto_connect=True,
        auto_reconnect=True,
        url=None,
        namespace=None,
        auth=None,
        headers=None,
        computer_factory="x.y:z",
        mcp_config=None,
    )

    assert isinstance(DummyInteractive.last_comp, FakeComputer)


class FakePromptSession:
    """Feed scripted inputs to the interactive loop."""

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


@pytest.mark.asyncio
async def test_interactive_help_and_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = [
        "help",
        "exit",
    ]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


@pytest.mark.asyncio
async def test_server_add_exception_and_rm_with_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖 server add 的异常打印分支，以及 rm 时已连接触发 emit 分支。"""

    # server 配置文件
    server_file = tmp_path / "server.json"
    server_file.write_text(
        json.dumps(
            {
                "name": "s2",
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

    # 指令：先连接，再尝试 add 触发异常，再 rm 触发已连接 emit
    commands = [
        "socket connect http://localhost:9001",
        f"server add @{server_file}",
        "server rm s2",
        "exit",
    ]

    # 准备 comp 与补丁
    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)

    async def _raise_add(*args: Any, **kwargs: Any) -> None:  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(comp, "aadd_or_aupdate_server", _raise_add)
    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    await _interactive_loop(comp)


@pytest.mark.asyncio
async def test_inputs_load_usage_and_success_with_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖 inputs load 的用法提示与成功路径（含 emit）。"""
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(
        json.dumps(
            [
                {"id": "J1", "type": "promptString", "description": "d", "default": "v"},
            ],
        ),
        encoding="utf-8",
    )

    commands = [
        "inputs load",  # 触发用法提示
        "socket connect http://localhost:9002",
        f"inputs load @{inputs_file}",  # 成功并触发 emit
        "exit",
    ]

    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


@pytest.mark.asyncio
async def test_socket_connect_guided_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖交互式 socket connect 的参数解析失败分支。"""
    commands = [
        "socket connect",
        "http://localhost:9003",
        "bad_auth_kv",  # 无效，触发 parse_kv_pairs 异常
        "exit",
    ]

    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


@pytest.mark.asyncio
async def test_inputs_value_print_json_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """通过让 console.print_json 抛异常覆盖 repr 回退分支。"""
    import a2c_smcp.computer.cli.utils as cli_utils

    commands = [
        'inputs add {"id":"Z","type":"promptString","description":"d"}',
        'inputs value set Z {"x":1}',  # 设置为字典
        "inputs value get Z",  # 获取时让 print_json 抛错
        "exit",
    ]

    def _raise_print_json(*args: Any, **kwargs: Any) -> None:  # noqa: ANN001
        raise ValueError("no json")

    monkeypatch.setattr(cli_utils.console, "print_json", _raise_print_json, raising=True)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


# ── `--mcp-config` 形状硬切 + fail-fast（#154）/ shape hard-cut + fail-fast ──────
#
# 历史 `test_run_impl_inputs_and_servers_single_object` / `..._loads_inputs_and_servers_from_files` **已删除**：
# 它们钉的正是本次要切掉的契约（`--config` 收「裸 server 对象 / 数组」+ 独立 `--inputs`）。替代守卫 = 下列
# fail-fast 用例 + `tests/integration_tests/computer/cli/test_mcp_flag_config.py`（真实构造路径消费 inputs 段，F7）。
def _run_with_mcp_config(raw: str) -> None:
    cli_main._run_impl(
        auto_connect=False,
        auto_reconnect=False,
        url=None,
        namespace=cli_main.SMCP_NAMESPACE,
        auth=None,
        headers=None,
        computer_factory=None,
        mcp_config=raw,
    )


def test_run_impl_rejects_legacy_bare_server_mcp_config(tmp_path: Path) -> None:
    """旧「裸 server 对象」格式 → fail-fast(2)，且提示含新形状与「去掉 name 字段」指引。"""
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"name": "solo", "type": "stdio", "server_parameters": {"command": "echo"}}), encoding="utf-8")
    with pytest.raises(typer.Exit) as ei:
        _run_with_mcp_config(str(p))
    assert ei.value.exit_code == 2


def test_run_impl_rejects_legacy_server_array_mcp_config(tmp_path: Path) -> None:
    """旧「server 数组」格式 → fail-fast(2)。"""
    p = tmp_path / "old-arr.json"
    p.write_text(json.dumps([{"name": "a", "type": "stdio", "server_parameters": {"command": "echo"}}]), encoding="utf-8")
    with pytest.raises(typer.Exit) as ei:
        _run_with_mcp_config(str(p))
    assert ei.value.exit_code == 2


def test_run_impl_rejects_unreadable_mcp_config(tmp_path: Path) -> None:
    """路径不存在 / JSON 损坏 → fail-fast(2)（旧 `--config` 在此静默降级启动）。"""
    with pytest.raises(typer.Exit) as ei:
        _run_with_mcp_config(str(tmp_path / "nope.json"))
    assert ei.value.exit_code == 2


def test_run_impl_mcp_config_invalid_fails_before_connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    **置于 connect 之前**的守卫：坏 flag 文件 MUST NOT 留下已连接 socket / 已 boot 的 Computer。

    历史 `--config` 解析在 `await init_client.connect(...)` **之后**且吞异常 ⇒ 坏文件会连上再静默降级。
    变异验证：把 `_mcp_flag_path(...)` 移回 `_amain` 的 connect 之后 → 本例转红（唯一钉住「校验位置」的守卫）。
    """
    connected: list[str] = []

    class _Client:
        def __init__(self, **_: Any) -> None: ...
        async def connect(self, *a: Any, **kw: Any) -> None:
            connected.append("yes")

    constructed: list[str] = []

    class _Comp(FakeComputer):
        def __init__(self, **kw: Any) -> None:
            constructed.append("yes")
            super().__init__(**kw)

    monkeypatch.setattr(cli_main, "SMCPComputerClient", _Client, raising=True)
    monkeypatch.setattr(cli_main, "Computer", _Comp, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", DummyInteractive.coro, raising=True)

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(typer.Exit) as ei:
        cli_main._run_impl(
            auto_connect=False,
            auto_reconnect=False,
            url="http://example.invalid",  # 有 url ⇒ 若校验在 connect 之后，必已连接
            namespace=cli_main.SMCP_NAMESPACE,
            auth=None,
            headers=None,
            computer_factory=None,
            mcp_config=str(bad),
        )
    assert ei.value.exit_code == 2
    assert connected == [], "坏 --mcp-config 不得留下已连接的 socket（校验须先于 connect）"
    assert constructed == [], "坏 --mcp-config 不得留下已 boot 的 Computer"


def test_run_impl_hands_mcp_flag_path_to_computer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `--mcp-config` 经 `Computer(mcp_flag_config=)` 注入（boot 声明式输入），且 `_run_impl` **不**自行急切挂载。

    `@file` 前缀被剥离。搭档守卫 = 集成测试 C1（真正验证 flag 层被 resolve 消费）——本例只钉「交接」。
    """
    monkeypatch.setattr(cli_main, "Computer", FakeComputer, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", DummyInteractive.coro, raising=True)

    good = tmp_path / "flag-mcp.json"
    good.write_text(
        json.dumps({"servers": {"figma.mcp": {"type": "stdio", "server_parameters": {"command": "node"}}}, "inputs": []}), encoding="utf-8",
    )

    _run_with_mcp_config("@" + str(good))  # `@file` 语法
    comp = DummyInteractive.last_comp
    assert comp.init_args["mcp_flag_config"] == good  # `@` 已剥离
    assert comp.init_args["mcp_servers"] == set()  # CLI 非嵌入式宿主 ⇒ embed 层恒空


def test_run_cli_options_renamed_and_inputs_removed() -> None:
    """
    参数面契约：`--mcp-config`/`-c` 在，`--config` / `--inputs` / `-i` **不在**——root 与 run **双查**。

    **程序化查参**而非查渲染 help：rich/typer 按终端宽度换行，help 文本断行会让子串断言 flaky。
    """
    import typer.main as typer_main

    cmd = typer_main.get_command(cli_main.app)
    run_cmd = cmd.commands["run"]  # type: ignore[attr-defined]
    for name, target in (("run", run_cmd), ("root", cmd)):
        opts = {o for p in target.params for o in p.opts}
        assert "--mcp-config" in opts, f"{name}: --mcp-config 缺失"
        assert "-c" in opts, f"{name}: -c 短参缺失"
        assert "--config" not in opts, f"{name}: 旧 --config 未删"
        assert "--inputs" not in opts, f"{name}: --inputs 未删"
        assert "-i" not in opts, f"{name}: -i 未删"
        # #208：并发策略入口 root + run **双声明**（同 --mcp-config 的既有形态）
        assert "--concurrency" in opts, f"{name}: --concurrency 缺失"


def test_root_level_mcp_config_reaches_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``a2c-computer --mcp-config <file> run``（flag 置于**子命令之前**）MUST 被消费 —— 不得静默丢弃。

    隔离审查 🔴2：``--mcp-config`` 在根回调与 ``run`` 上各声明一份，但根回调**只在无子命令时**消费它；
    显式带 ``run`` 时根的值既不入 ``_RootState``、``run`` 也不回读 ⇒ **静默丢弃**（连 fail-fast 都碰不到）。
    实测 develop 上 ``a2c-computer --config bad.json run`` 会响亮报 "No such option"（exit 2），本 PR
    若不修则退化为 exit 1 + 一个与 ``--mcp-config`` 毫无关系的 OSError ⇒ **本 PR 自引入的失败模式回归**。

    ``--settings`` 同形（既有缺陷，一并修）：它同样 root+run 双声明，root 那份对 ``run`` 从来无效。
    """
    seen: dict[str, Any] = {}

    def _capture(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(cli_main, "_run_impl", _capture, raising=True)

    good = tmp_path / "flag-mcp.json"
    good.write_text(json.dumps({"servers": {}, "inputs": []}), encoding="utf-8")
    st = tmp_path / "flag-settings.json"
    st.write_text(json.dumps({}), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["--mcp-config", str(good), "--settings", str(st), "run"])
    assert result.exit_code == 0, result.output
    assert seen.get("mcp_config") == str(good), "根级 --mcp-config 未透传到 run（静默丢弃）"
    assert seen.get("settings_file") == str(st), "根级 --settings 未透传到 run（静默丢弃）"


def test_root_level_mcp_config_still_fails_fast_on_bad_file(tmp_path: Path) -> None:
    """根级 ``--mcp-config`` 的坏文件同样 fail-fast(2)——静默丢弃会让 fail-fast 形同虚设（🔴2 的后果面）。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(cli_main.app, ["--mcp-config", str(bad), "run"])
    assert result.exit_code == 2, f"根级坏 --mcp-config 未 fail-fast；output={result.output!r}"


def test_settings_help_no_longer_claims_lowest_priority() -> None:
    """
    `--settings` 帮助文案订正：flag 是**次高**、不是「最低优先级」（该文案一直是错的，实现从来是次高）。

    root 与 run **两份都查**——只查一份会让另一份烂掉（本仓 `--settings` 确有两份声明）。
    """
    import typer.main as typer_main

    cmd = typer_main.get_command(cli_main.app)
    run_cmd = cmd.commands["run"]  # type: ignore[attr-defined]
    for name, target in (("run", run_cmd), ("root", cmd)):
        helps = {p.name: (p.help or "") for p in target.params}
        for flag in ("settings_file", "mcp_config"):
            assert "最低优先级" not in helps.get(flag, ""), f"{name}.{flag}: 仍写「最低优先级」"
        assert "次高" in helps.get("settings_file", ""), f"{name}: --settings 未写明次高"
        assert "次高" in helps.get("mcp_config", ""), f"{name}: --mcp-config 未写明次高"


@pytest.mark.asyncio
async def test_cover_remaining_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖 interactive_impl.py 中剩余未命中的分支。"""
    # #137 ②：REPL `server add` 现为 durable 落盘——隔离 cwd/XDG 到 tmp，防写真实仓库 .tfrobot/。
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    # 为 inputs update @file 准备文件（列表）
    upd_file = tmp_path / "upd.json"
    upd_file.write_text(
        json.dumps(
            [
                {"id": "U1", "type": "promptString", "description": "d"},
                {"id": "U2", "type": "promptString", "description": "d2"},
            ],
        ),
        encoding="utf-8",
    )

    # 命令序列
    commands = [
        # 添加 server 后立刻 mcp，覆盖 servers 循环
        '{"cmd":"server add inline"}',  # 占位，下一行是真正的 add
        'server add {"name":"m1","type":"stdio","disabled":true,"forbidden_tools":[],"tool_meta":{},'
        '"server_parameters":{"command":"echo","args":[],"env":null,"cwd":null,"encoding":"utf-8","encoding_error_handler":"strict"}}',
        "mcp",
        # start/stop 时 manager 未初始化
        "start one",
        "stop one",
        # inputs add 用法
        "inputs add",
        # inputs update 用法 + @file 列表
        "inputs update",
        f"inputs update @{upd_file}",
        # inputs rm 用法 + rm 不存在
        "inputs rm",
        "inputs rm NOPE",
        # inputs get 用法
        "inputs get",
        # inputs value 顶层用法 + set 缺少参数 + set 不存在 id + get 不存在值
        "inputs value",
        "inputs value set",
        "inputs value set NOPE 1",
        "inputs value get NOPE",
        # inputs value 未知子命令
        "inputs value what",
        # socket connect 引导但 URL 为空，触发 URL required
        "socket connect",
        "",
        # socket join 带参数但尚未连接
        "socket join o1 c1",
        # socket leave 在未连接
        "socket leave",
        "exit",
    ]

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


@pytest.mark.asyncio
async def test_interactive_misc_and_file_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖更多 interactive_impl 分支：
    - 空输入跳过
    - tools/mcp 打印
    - server add 使用 @file + 随后 rm
    - inputs add 使用 @file（数组）与 update 使用单对象
    - inputs value 边界：缺少参数、指定 id 清理、JSON 载荷
    - socket 再次 connect 走 already-connected 分支
    - socket join/leave 的未连接/未加入分支
    - 未知子命令（server/socket/notify）与 render 内联 JSON
    - start/stop 单个名称（manager 初始化后触发路径）
    """
    # #137 ②：REPL `server add` 现为 durable 落盘——隔离 cwd/XDG 到 tmp，防写真实仓库 .tfrobot/。
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)

    # 预备文件：server 与 inputs
    server_file = tmp_path / "server.json"
    server_file.write_text(
        json.dumps(
            {
                "name": "s1",
                "type": "stdio",
                "disabled": True,  # 避免真实启动
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

    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(
        json.dumps(
            [
                {"id": "I1", "type": "promptString", "description": "d1", "default": "x"},
                {"id": "I2", "type": "pickString", "description": "d2", "options": ["a", "b"], "default": "a"},
            ],
        ),
        encoding="utf-8",
    )

    # 指令脚本
    commands = [
        "",  # 空输入
        "tools",
        "mcp",
        f"server add @{server_file}",
        "server rm s1",
        f"inputs add @{inputs_file}",  # 数组 add
        'inputs update {"id":"I1","type":"promptString","description":"d1u","default":"y"}',  # 单对象 update
        "inputs value get",  # 缺失 id
        "inputs value rm",  # 缺失 id
        'inputs value set I1 {"k":1}',  # JSON 载荷
        "inputs value clear I1",  # 指定 id 清理
        "socket connect http://localhost:9000",  # 连接一次
        "socket connect http://localhost:9000",  # 已连接分支
        "socket join",  # 缺少参数
        "socket leave",  # 未加入房间
        "server unknownsub",
        "socket unknown",
        "notify unknown",
        'render {"a":1}',  # 内联 JSON 渲染
        # 初始化 manager 后测试 start/stop 单个名称分支
        "exit",
    ]

    # 打补丁：Session/patch_stdout/SMCP 客户端与 tools 列表
    class LocalFakeClient(FakeSMCPClient):
        pass

    # 我们需要在交互开始前让 comp.manager 初始化，以便稍后可以测试 start/stop 单个名称
    # 这里分两段会话：第一段跑上述命令到 exit，然后第二段在 manager 初始化后再跑 start/stop name

    monkeypatch.setattr(cli_main, "SMCPComputerClient", LocalFakeClient)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)

    # stub 工具列表
    async def _fake_tools() -> list[dict[str, Any]]:
        return [{"name": "t1", "description": "d", "return_schema": {}}]

    monkeypatch.setattr(comp, "aget_available_tools", _fake_tools)

    await _interactive_loop(comp)

    # 第二段：初始化 manager 后测试 start/stop <name> 分支（即使失败也能走异常打印分支）
    await comp.boot_up()
    commands2 = [
        "start xxx",
        "stop xxx",
        "exit",
    ]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands2))
    await _interactive_loop(comp)


def test_root_no_color_triggers_console_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖 _root 的 no_color 分支，并确保调用 _run_impl。"""
    called: dict[str, Any] = {"ok": False}

    def _stub_run_impl(**kwargs: Any) -> None:  # noqa: ANN003
        called["ok"] = True

    class Ctx:
        invoked_subcommand = None

    monkeypatch.setattr(cli_main, "_run_impl", _stub_run_impl, raising=True)

    # 验证不会抛异常，且 _run_impl 被调用
    cli_main._root(Ctx(), no_color=True)  # 其它参数用默认值
    assert called["ok"] is True


def test_run_impl_accepts_valid_mcp_flag_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `--mcp-config` 合法 mcp.json 形状 → 正常 boot 进 REPL（本例只走通路径；层序/消费见 Group A + 集成 C1）。

    取代已删除的 `test_run_impl_loads_inputs_and_servers_from_files`（它钉的是被切掉的「server 数组 + 独立
    --inputs」契约）。`inputs` 段现由 flag 层 mcp.json 承载、经 `run_mcp_approval` 与其余 scope 同路消费。
    """
    # #137 ②：REPL 路径可能 durable 落盘 → 隔离 cwd/XDG 到 tmp，防写真实仓库 .tfrobot/。
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(["exit"]))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    flag_file = tmp_path / "flag-mcp.json"
    flag_file.write_text(
        json.dumps(
            {
                "servers": {
                    # 键含 `.` ⇒ bundle_id `s1_srv` ≠ name（conformance §2.0 分叉；`-` 不会被折叠，故不用 `-`）
                    "s1.srv": {
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
                },
                "inputs": [
                    {"id": "VA", "type": "promptString", "description": "d", "default": "1"},
                    {"id": "VB", "type": "pickString", "description": "d", "options": ["x", "y"], "default": "x"},
                ],
            },
        ),
        encoding="utf-8",
    )

    # 不提供 url，避免网络
    cli_main._run_impl(
        auto_connect=False,
        auto_reconnect=False,
        url=None,
        namespace=cli_main.SMCP_NAMESPACE,
        auth=None,
        headers=None,
        computer_factory=None,
        mcp_config=str(flag_file),
    )


def test_run_impl_cli_params_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖 _run_impl 在解析 auth/headers 失败时的异常分支。"""
    # 立即退出
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(["exit"]))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())
    # 使用假的 Socket 客户端避免真实连接
    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)

    # 传入无效的 kv 字符串（缺少冒号），触发 parse_kv_pairs 抛错，从而走 except 分支
    cli_main._run_impl(
        auto_connect=False,
        auto_reconnect=False,
        url="http://localhost:7777",
        namespace=cli_main.SMCP_NAMESPACE,
        auth="invalid",  # 无效
        headers="also_invalid",  # 无效
        computer_factory=None,
        mcp_config=None,
    )


@pytest.mark.asyncio
async def test_inputs_cli_crud_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖 inputs 子命令：add/update/rm/get/list，并在连接状态下触发配置更新通知。"""
    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)

    # 使用 socket connect 建立连接，随后执行 inputs 的 CRUD 命令
    commands = [
        "socket connect http://localhost:7000",
        # add 单条
        'inputs add {"id":"USER","type":"promptString","description":"d","default":"a"}',
        # get + list
        "inputs get USER",
        "inputs list",
        # update 批量（数组）
        'inputs update [{"id":"USER","type":"promptString","description":"d2","default":"b"},'
        ' {"id":"REG","type":"pickString","description":"r",'
        ' "options":[{"label":"us","value":"us"},{"label":"eu","value":"eu"}],"default":"us"}]',
        "inputs list",
        # rm
        "inputs rm USER",
        "inputs list",
        "exit",
    ]

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)

    last: FakeSMCPClient = FakeSMCPClient.last  # type: ignore[assignment]
    # 至少在 add/update/rm 期间触发了多次更新通知
    assert last.updated >= 3


@pytest.mark.asyncio
async def test_socket_connect_guided_inputs_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证在未提供 URL 的情况下，交互式引导输入 URL/Auth/Headers，并正确解析传给 connect(auth=..., headers=...).
    """
    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)

    # 触发引导式：先输入命令，再依次回应 URL、Auth、Headers，然后退出
    commands = [
        "socket connect",
        "http://localhost:8000",
        "token:abc123",
        "app:demo,ver:1.0",
        "exit",
    ]

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)

    # 断言 FakeSMCPClient 收到了期望的参数
    last: FakeSMCPClient = FakeSMCPClient.last  # type: ignore[assignment]
    assert last.connected is True
    assert last.connect_args is not None
    assert last.connect_args["url"] == "http://localhost:8000"
    assert last.connect_args["auth"] == {"token": "abc123"}
    assert last.connect_args["headers"] == {"app": "demo", "ver": "1.0"}


def test_run_with_cli_url_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证通过 run(url=..., auth=..., headers=...) 启动时，会自动连接并传入解析后的参数，随后进入交互并退出。
    """
    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)

    # 进入交互后立即退出
    commands = [
        "exit",
    ]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    # 走 `_run_impl`（纯实现函数）而非被 @app.command 装饰的 `run`：后者的形参由 Typer 解析（#154 起还需
    # `ctx` 兜底回读根级 flag），直呼会让未传的形参保留 OptionInfo —— 这正是 `_run_impl` 存在的理由，
    # 本文件其余用例亦皆走它。
    cli_main._run_impl(
        auto_connect=False,
        auto_reconnect=False,
        url="http://service:1234",
        namespace=cli_main.SMCP_NAMESPACE,
        auth="token:abc",
        headers="h1:v1,h2:v2",
        computer_factory=None,
        mcp_config=None,
    )

    last: FakeSMCPClient = FakeSMCPClient.last  # type: ignore[assignment]
    assert last.connected is True
    assert last.connect_args == {
        "url": "http://service:1234",
        "auth": {"token": "abc"},
        "headers": {"h1": "v1", "h2": "v2"},
        "namespaces": [cli_main.SMCP_NAMESPACE],
    }


@pytest.mark.asyncio
async def test_server_add_and_status_without_auto_connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # #137 ②：REPL `server add` 现为 durable 落盘——隔离 cwd/XDG 到 tmp，防写真实仓库 .tfrobot/。
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    # Minimal stdio server config (disabled=true to avoid start operations later)
    stdio_cfg = {
        "name": "test-stdio",
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
    }

    commands = [
        f"server add {stdio_cfg}",
        "mcp",
        "status",
        "exit",
    ]

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


@pytest.mark.asyncio
async def test_unknown_and_status_manager_uninitialized(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = [
        "unknown",
        "status",
        "exit",
    ]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


@pytest.mark.asyncio
async def test_server_rm_without_name_and_add_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = [
        "server rm",
        "server add {invalid}",
        "exit",
    ]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


@pytest.mark.asyncio
async def test_start_stop_all_with_manager_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await comp.boot_up()

    commands = [
        "start all",
        "stop all",
        "exit",
    ]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    await _interactive_loop(comp)


def test_concurrency_flag_reaches_computer_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """#208：``--concurrency N`` 必须在 ``boot_up``（``async with comp``）**之前**安装到 Computer。

    只断言「参数被接收」是不够的——漏装则并发策略静默退化为串行（默认行为合法，故无报错），
    且 ``--concurrency`` 只在构造后、boot 前这一个窗口可装（boot 之后再装会 fail-closed 抛错）。
    """
    monkeypatch.setattr(cli_main, "Computer", FakeComputer, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", DummyInteractive.coro, raising=True)

    cli_main._run_impl(
        auto_connect=True,
        auto_reconnect=True,
        url=None,
        namespace=None,
        auth=None,
        headers=None,
        computer_factory=None,
        mcp_config=None,
        concurrency=5,
    )

    assert DummyInteractive.last_comp.concurrency_calls == [5], "--concurrency 须在 boot 前安装到 Computer"


def test_concurrency_flag_absent_keeps_serial_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省**不传** ``--concurrency`` → 不调用策略安装 ⇒ 保持既有逐项串行（行为不变）。"""
    monkeypatch.setattr(cli_main, "Computer", FakeComputer, raising=True)
    monkeypatch.setattr(cli_main, "_interactive_loop", DummyInteractive.coro, raising=True)

    cli_main._run_impl(
        auto_connect=True,
        auto_reconnect=True,
        url=None,
        namespace=None,
        auth=None,
        headers=None,
        computer_factory=None,
        mcp_config=None,
        concurrency=None,
    )

    assert DummyInteractive.last_comp.concurrency_calls == []


@pytest.mark.asyncio
async def test_start_all_prints_per_item_receipts(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """#208：``start all`` 逐项回执——单项失败只体现在该项，不完全不截断其余项的回执。"""
    from a2c_smcp.computer.mcp_clients.manager import StartOutcome

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await comp.boot_up()
    assert comp.mcp_manager is not None
    monkeypatch.setattr(
        comp.mcp_manager,
        "_servers_config",
        {"a": MagicMock(disabled=False), "b": MagicMock(disabled=False), "c": MagicMock(disabled=False)},
    )
    calls: list[list[str]] = []

    async def _fake_batch(ids: list[str]) -> list[StartOutcome]:
        calls.append(list(ids))
        return [StartOutcome("a", None), StartOutcome("b", RuntimeError("boom")), StartOutcome("c", None)]

    monkeypatch.setattr(comp.mcp_manager, "astart_clients_batch", _fake_batch)

    commands = ["start all", "exit"]
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    await _interactive_loop(comp)
    out = capsys.readouterr().out

    assert calls == [["a", "b", "c"]], "start all 须经统一批量 API 且覆盖全部未禁用项"
    assert "a 已启动" in out and "c 已启动" in out, "成功项逐项回执"
    assert "b 启动失败" in out and "boom" in out, "失败项逐项回执"
    assert "1 个服务器启动失败" in out, "批次收敛后输出失败汇总"


@pytest.mark.asyncio
async def test_inputs_load_and_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(
        json.dumps(
            [
                {"id": "VAR1", "type": "promptString", "description": "v", "default": "abc"},
                {"id": "CHOICE", "type": "pickString", "description": "d", "options": ["x", "y"], "default": "x"},
            ],
        ),
        encoding="utf-8",
    )

    any_file = tmp_path / "any.json"
    any_file.write_text(json.dumps({"k": "${input:VAR1}", "c": "${input:CHOICE}"}), encoding="utf-8")

    commands = [
        f"inputs load @{inputs_file}",
        f"render @{any_file}",
        "exit",
    ]

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)


class FakeSMCPClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        self.connected = False
        self.office_id: str | None = None
        self.joined_args: tuple[str, str] | None = None
        self.updated = 0
        self.computer = kwargs.get("computer")
        self.namespace = kwargs.get("namespace")
        self.disconnects = 0
        # 记录最后一个实例，便于断言
        FakeSMCPClient.last = self  # type: ignore[attr-defined]
        self.connect_args: dict[str, Any] | None = None

    async def connect(
        self,
        url: str,
        auth: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        namespaces: list[str] | None = None,
    ) -> None:
        self.connected = True
        args: dict[str, Any] = {"url": url, "auth": auth, "headers": headers}
        if namespaces is not None:
            args["namespaces"] = namespaces
        self.connect_args = args

    async def join_office(self, office_id: str) -> None:
        # 形参须与真实 ``SmcpComputerClient.join_office`` 一致（名字经 ``computer.name`` 走线，不是形参）。
        assert self.connected
        self.office_id = office_id
        self.joined_args = (office_id, self.computer.name)

    async def leave_office(self, office_id: str) -> None:
        assert self.connected
        self.office_id = None

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    async def emit_update_config(self) -> None:
        self.updated += 1


@pytest.mark.asyncio
async def test_socket_and_notify_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main, "SMCPComputerClient", FakeSMCPClient)

    commands = [
        "notify update",
        "socket connect http://localhost:7000",
        "socket join office-1 compA",
        "notify update",
        "socket leave",
        "exit",
    ]

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    comp = Computer(name="test_main_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)

    # 桩的 join_office 形参已对齐真实客户端（见 FakeSMCPClient）——否则这里的 join 会抛 TypeError 被
    # 笼统 except 吞掉，socket join 分支实际从未被走到（本断言即该假绿的回归守卫）。
    assert FakeSMCPClient.last.joined_args == ("office-1", "compA"), FakeSMCPClient.last.joined_args
    assert FakeSMCPClient.last.office_id is None, "socket leave 应已退房"


class _ReconnectClient(FakeSMCPClient):
    """记录**实例序列**与连接参数，用于断言「改名 ⇒ 换新连接」。"""

    instances: list[_ReconnectClient] = []
    # 类级开关：模拟「尽力而为」两步（离开旧房 / 断开）失败——两者都不应阻断改名重连
    fail_leave = False
    fail_disconnect = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        super().__init__(*args, **kwargs)
        self.joins: list[str] = []
        self.leaves: list[str] = []
        # **实例级**失败开关（默认关）：由 _arm_failures_on_second_connection 只武装改名那一跳，
        # 若做成类级开关会在后续所有实例上持续生效，令「失败后仍可重试」无法被断言。
        self.fail_connect = False
        self.fail_join = False
        _ReconnectClient.instances.append(self)

    async def connect(  # type: ignore[override]
        self,
        url: str,
        auth: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        namespaces: list[str] | None = None,
    ) -> None:
        if self.fail_connect:
            raise RuntimeError("connect boom")
        await super().connect(url, auth=auth, headers=headers, namespaces=namespaces)

    async def join_office(self, office_id: str) -> None:
        if self.fail_join:
            # **一次性**：模拟「目标房同名被占」这类可换房/换名重试的拒绝。若持续失败，就断言不了
            # 「失败后仍可在保留的连接上直接重试」。
            self.fail_join = False
            raise RuntimeError("Name already taken in room")
        await super().join_office(office_id)
        self.joins.append(office_id)

    async def leave_office(self, office_id: str) -> None:
        if _ReconnectClient.fail_leave:
            raise RuntimeError("leave boom")
        await super().leave_office(office_id)
        self.leaves.append(office_id)

    async def disconnect(self) -> None:
        if _ReconnectClient.fail_disconnect:
            raise RuntimeError("disconnect boom")
        await super().disconnect()


def _arm_failures_on_second_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connect: bool = False,
    join: bool = False,
) -> None:
    """只让**第 2 条**连接（即改名那一次重连）的 ``connect`` / ``join_office`` 失败。

    首条连接（``socket connect``）必须正常建立，否则走不到改名分支。
    """
    original_init = _ReconnectClient.__init__

    def _init_then_arm(self: _ReconnectClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if len(_ReconnectClient.instances) > 1:  # 本实例即第 2 条及以后
            self.fail_connect = connect
            self.fail_join = join

    monkeypatch.setattr(_ReconnectClient, "__init__", _init_then_arm)


def _arm_init_failure_on_second_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """让**第 2 条**连接的**构造器**抛错（模拟构造期失败）。

    构造与建连同处一个 ``except``（都在第 3 步相位内），故构造抛错也必须回滚 ``comp.name``——若把
    构造挪到 ``try`` 之外，``comp.name`` 会停在一个从未生效的新名上，且**其余用例都不会红**。
    """
    original_init = _ReconnectClient.__init__

    def _init_then_maybe_boom(self: _ReconnectClient, *args: Any, **kwargs: Any) -> None:
        if _ReconnectClient.instances:  # 本实例即第 2 条及以后
            raise RuntimeError("init boom")
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(_ReconnectClient, "__init__", _init_then_maybe_boom)


def _reset_reconnect_client() -> None:
    _ReconnectClient.instances = []


async def _drive_socket_commands(commands: list[str], monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Computer:
    monkeypatch.setattr(cli_main, "SMCPComputerClient", _ReconnectClient)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(commands))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())
    comp = Computer(name="rename_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp, **kwargs)
    return comp


@pytest.mark.asyncio
async def test_first_join_and_same_name_move_do_not_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """首次入房（本连接尚未声明身份）与**同名换房**都**不**触发重连——只有改名才换连接。

    正对照：若实现按「``requested != comp.name``」判改名，首次入房（``comp.name`` 为默认值）会误触发
    一次无谓的断连重连，本断言随之变红。
    """
    _reset_reconnect_client()

    comp = await _drive_socket_commands(
        [
            "socket connect http://localhost:7000",
            "socket join office-1 alice",  # 首次入房：任意名可
            "socket join office-2 alice",  # 同名换房：协议允许
            "exit",
        ],
        monkeypatch,
    )

    assert len(_ReconnectClient.instances) == 1, f"同名换房不得重连，实得 {len(_ReconnectClient.instances)} 个客户端"
    assert _ReconnectClient.instances[0].joins == ["office-1", "office-2"]
    assert _ReconnectClient.instances[0].disconnects == 0
    assert comp.name == "alice"


@pytest.mark.asyncio
async def test_rename_reconnects_with_new_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """改名 ⇒ 换新连接（协议 events.md:610：身份在 sid 生命周期内不可变）。"""
    _reset_reconnect_client()

    comp = await _drive_socket_commands(
        [
            "socket connect http://localhost:7000?token=abc",
            "socket join office-1 alice",
            "socket join office-2 bob",  # 改名 ⇒ 1/4 离开 → 2/4 断开 → 3/4 重连 → 4/4 入房
            "exit",
        ],
        monkeypatch,
    )

    assert len(_ReconnectClient.instances) == 2, "改名必须换新连接"
    old, new = _ReconnectClient.instances
    assert old.disconnects == 1, "旧连接应被断开"
    assert old.leaves == ["office-1"], "应先离开旧房（让对端收到 notify:leave_office）"
    assert new is not old and new.connected, "新连接须已建立"
    assert new.joins == ["office-2"], "入房应落在**新**连接上"
    assert comp.name == "bob", "改名生效后 comp.name 应为新名"
    # 重放须**逐字段全等**：auth / headers / namespaces 在 `--auth token:...` 部署下是承重的，
    # 只断言 url 时改坏它们不会变红。
    assert new.connect_args == {
        "url": "http://localhost:7000?token=abc",
        "auth": None,
        "headers": None,
        "namespaces": [cli_main.SMCP_NAMESPACE],
    }, f"重连须原样重放连接参数，实得 {new.connect_args!r}"


@pytest.mark.asyncio
async def test_rename_replays_auto_connect_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--auto-connect`` 路径：改名重连须重放**启动参数**（url / auth / headers / namespace）。

    该路径的连接参数由 ``main.py`` 经 ``init_connection`` 注入，与交互式 ``socket connect`` 是两条
    独立来源；只覆盖后者会让「启动参数没接上」这一类回归完全无感。
    """
    _reset_reconnect_client()

    boot_client = _ReconnectClient(
        computer=Computer(name="rename_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False),
        namespace="/tf-custom",
    )
    await boot_client.connect("http://boot:7000", auth={"token": "t"}, headers={"X-TF": "1"}, namespaces=["/tf-custom"])

    comp = await _drive_socket_commands(
        [
            "socket join office-1 alice",
            "socket join office-2 bob",  # 改名 ⇒ 重连
            "exit",
        ],
        monkeypatch,
        init_client=boot_client,
        init_connection={
            "url": "http://boot:7000",
            "namespace": "/tf-custom",
            "auth": {"token": "t"},
            "headers": {"X-TF": "1"},
        },
    )

    assert len(_ReconnectClient.instances) == 2, "改名必须换新连接"
    new = _ReconnectClient.instances[1]
    assert new.connect_args == {
        "url": "http://boot:7000",
        "auth": {"token": "t"},
        "headers": {"X-TF": "1"},
        "namespaces": ["/tf-custom"],
    }, f"须重放启动参数，实得 {new.connect_args!r}"
    assert new.namespace == "/tf-custom", "新客户端须绑定同一 namespace"
    assert comp.name == "bob"


@pytest.mark.asyncio
async def test_rename_binds_real_client_to_effective_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """用**真实** ``SMCPComputerClient``（只替身 I/O 方法）断言改名后的新客户端绑定到**生效**命名空间。

    桩对象 ``__init__(*args, **kwargs)`` 对 ``namespace`` 无感，故「把 ``namespace=None`` 显式传给
    构造器 ⇒ 覆盖 ``SMCPComputerClient`` 的默认值 ``SMCP_NAMESPACE`` ⇒ 新连接是**哑连接**（handler 全
    注册在 ``'/'``、``emit`` 落到未连接命名空间）」这一类回归在纯桩下**物理上无法变红**。本用例专门
    用真类把这条语义钉死。
    """
    from a2c_smcp.computer.socketio.client import SMCPComputerClient

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(
        [
            "socket connect http://localhost:7000",  # 交互路径：未指定 namespace
            "socket join office-1 alice",
            "socket join office-2 bob",  # 改名 ⇒ 重连
            "exit",
        ],
    ))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    # 只替掉 I/O：不真的建 socket，也不注册 handler 之外的网络状态
    async def _fake_connect(self: Any, url: str, *args: Any, **kwargs: Any) -> None:
        self.connected = True

    async def _fake_join(self: Any, office_id: str) -> None:
        self.office_id = office_id

    async def _fake_disconnect(self: Any) -> None:
        self.connected = False

    monkeypatch.setattr(SMCPComputerClient, "connect", _fake_connect)
    monkeypatch.setattr(SMCPComputerClient, "join_office", _fake_join)
    monkeypatch.setattr(SMCPComputerClient, "disconnect", _fake_disconnect)

    built: list[Any] = []
    real_init = SMCPComputerClient.__init__

    def _recording_init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        built.append(self)

    monkeypatch.setattr(SMCPComputerClient, "__init__", _recording_init)

    comp = Computer(name="rename_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)

    assert len(built) == 2, f"改名应新建一条连接，实得 {len(built)} 个客户端"
    new = built[1]
    assert new.namespace == cli_main.SMCP_NAMESPACE, (
        f"新客户端须绑定生效命名空间（显式传 None 会覆盖默认值 ⇒ 哑连接）：{new.namespace!r}"
    )
    # handler 也必须落在同一命名空间（哑连接的特征是所有 handler 都在 '/'）
    assert cli_main.SMCP_NAMESPACE in new.handlers, f"handler 未落在生效命名空间：{list(new.handlers)}"
    assert "/" not in new.handlers, f"不得有 handler 落在默认命名空间 '/': {list(new.handlers)}"


@pytest.mark.asyncio
async def test_rename_without_recorded_connection_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """无连接参数可重放（如外部注入 client）⇒ 拒绝改名，且**不得**改动 ``comp.name``。"""
    _reset_reconnect_client()

    injected = _ReconnectClient(
        computer=Computer(name="rename_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False),
    )
    await injected.connect("http://localhost:7000")

    comp = await _drive_socket_commands(
        [
            "socket join office-1 alice",
            "socket join office-2 bob",
            "exit",
        ],
        monkeypatch,
        init_client=injected,
    )

    assert len(_ReconnectClient.instances) == 1, "无参数可重放时不得新建连接"
    assert injected.joins == ["office-1"], "改名请求不得落到 join"
    assert comp.name == "alice", "拒绝改名 ⇒ comp.name 保持已声明的名字"


@pytest.mark.asyncio
async def test_rename_rolls_back_name_when_reconnect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """**建连失败** ⇒ ``comp.name`` 回滚为原名（改名未生效），且不留悬空客户端。"""
    _reset_reconnect_client()
    _arm_failures_on_second_connection(monkeypatch, connect=True)

    comp = await _drive_socket_commands(
        [
            "socket connect http://localhost:7000",
            "socket join office-1 alice",
            "socket join office-2 bob",  # 改名 ⇒ 重连失败
            "socket join office-3 carol",  # 失败后无可用连接 ⇒ 应被「请先连接」拦下
            "exit",
        ],
        monkeypatch,
    )

    assert len(_ReconnectClient.instances) == 2, "改名应尝试建新连接（失败）"
    assert not _ReconnectClient.instances[1].connected, "重连失败 ⇒ 新连接未建立"
    assert _ReconnectClient.instances[1].joins == [], "重连失败 ⇒ 不得入房"
    assert comp.name == "alice", "重连失败 ⇒ 名字回滚（改名未生效）"
    assert _ReconnectClient.instances[0].disconnects == 1, "旧连接已断开"


@pytest.mark.asyncio
async def test_rename_rolls_back_name_when_constructor_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """**构造器抛错**（第 3 步相位的防御面）同样必须回滚 ``comp.name``。"""
    _reset_reconnect_client()
    _arm_init_failure_on_second_connection(monkeypatch)

    comp = await _drive_socket_commands(
        [
            "socket connect http://localhost:7000",
            "socket join office-1 alice",
            "socket join office-2 bob",  # 改名 ⇒ 构造第 2 条连接时抛错
            "socket join office-3 carol",  # 失败后无可用连接 ⇒ 应被「请先连接」拦下
            "exit",
        ],
        monkeypatch,
    )

    assert len(_ReconnectClient.instances) == 1, "构造失败 ⇒ 第 2 条连接未登记"
    assert comp.name == "alice", "构造失败 ⇒ 名字必须回滚（构造与建连同属第 3 步相位）"
    assert _ReconnectClient.instances[0].disconnects == 1, "旧连接已断开"


@pytest.mark.asyncio
async def test_rename_keeps_new_connection_when_join_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """**建连成功、入房被拒**是另一个相位：必须**保留**那条连接，不得丢弃（🔴 回归守卫）。

    丢弃它会有三重代价：连接泄漏（客户端 + 服务端 sid 会话驻留）、``Computer`` 的 weakref 指向孤儿、
    以及 CLI 自锁为「请先连接」——而那条连接其实活着。且新会话尚未声明任何身份（服务端回滚 name），
    用户直接换个名字重试即可，**无需**再重连。
    """
    _reset_reconnect_client()
    _arm_failures_on_second_connection(monkeypatch, join=True)

    comp = await _drive_socket_commands(
        [
            "socket connect http://localhost:7000",
            "socket join office-1 alice",
            "socket join office-2 bob",  # 改名 ⇒ 重连成功但入房被拒
            "socket join office-3 bob",  # 关键：仍可**直接**重试（无「请先连接」死态）
            "exit",
        ],
        monkeypatch,
    )

    assert len(_ReconnectClient.instances) == 2, "入房被拒不得再建连接"
    new = _ReconnectClient.instances[1]
    assert new.connected, "入房被拒后新连接必须仍然活着（不得丢弃）"
    assert new.disconnects == 0, "入房被拒不得把新连接断开"
    # 第 4 条命令能落到 join：证明 CLI 没自锁为「未连接」（旧实现会把 smcp_client 置 None）
    assert new.joins == ["office-3"], f"应能直接在保留的连接上重试，实得 {new.joins!r}"
    assert comp.name == "bob", "入房被拒但连接已换 ⇒ 名字保持新名（与 #203 自动重放口径一致）"


@pytest.mark.asyncio
async def test_socket_leave_clears_intent_even_when_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``socket leave`` 在「有意图但不可达」（未连接 / 回放在途）时**仍须退房**：本地意图必须被清掉。

    #223 回归点：此前以 ``_client_in_office``（意图 ∧ 在册）为门 ⇒ 不可达时**不调** ``leave_office``
    ⇒ 本地 ``office_id`` 留着 ⇒ 重连后的自动回房把用户刚退掉的房又回了一遍。现在判据只剩**文案**职责，
    客户端 ``leave_office`` 无条件清本地意图、仅在 namespace 在册时才真发 ``server:leave_office``。

    .. important::
       **真实断线窗口不经过本分支**——实测（真 ASGI 服务端 + 真客户端，掐 WebSocket）断开后
       socketio 同时清空 ``namespaces`` **与** ``connected``，故窗口内先命中命令入口的「未连接」守卫。
       本用例的 ``connected=True`` + 命名空间空是**合成**状态，覆盖 ``_client_in_office`` 的防御语义。
       真实窗口由下一条用例（``connected=False``）覆盖。

    本用例用**真实**客户端（桩对象没有 ``_in_office``，走不到这条语义）。
    """
    from a2c_smcp.computer.socketio.client import SMCPComputerClient

    sent: list[str] = []

    async def _spy_emit(self: Any, event: str, data: Any = None, *a: Any, **kw: Any) -> None:
        sent.append(event)

    monkeypatch.setattr(SMCPComputerClient, "emit", _spy_emit)

    comp = Computer(name="leave_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    client = SMCPComputerClient(computer=comp)
    # 重连窗口：desired 在、命名空间**不在册** ⇒ `_in_office()` 为假（#203 刻意保留 desired）
    client.office_id = "office-old"
    client.connected = True
    assert client.office_id is not None and not client.namespaces, "前置：desired 在、不在册"
    assert not client._in_office(), "前置：该窗口内 `_in_office()` 必须为假"

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(["socket leave", "exit"]))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    printed: list[str] = []
    import a2c_smcp.computer.cli.interactive_impl as impl_mod

    monkeypatch.setattr(impl_mod.console, "print", lambda message="", *a, **kw: printed.append(str(message)))

    await _interactive_loop(comp, init_client=client)

    assert LEAVE_OFFICE_EVENT not in sent, f"不可达时不得发包（服务端会话已随连接销毁），实得 {sent!r}"
    assert client.office_id is None, "本地意图必须被清掉（否则重连后又会被自动拉回）"
    assert any("已清除本地入房意图" in line for line in printed), f"应如实提示只清了本地意图：{printed!r}"

    # 正对照：命名空间在册（可上报）⇒ 真发 server:leave_office + 文案「已离开房间」
    # （防止上面断言被「永远只清本地」的坏实现同样满足）
    client.office_id = "office-old"
    client.namespaces[client.namespace] = "eio-sid"
    assert client._in_office(), "前置：正对照须满足 `_in_office()`"
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(["socket leave", "exit"]))
    await _interactive_loop(comp, init_client=client)
    assert LEAVE_OFFICE_EVENT in sent, f"可上报时必须真的退房，实得 {sent!r}"
    assert client.office_id is None
    assert any("已离开房间" in line for line in printed), f"在房时文案应为「已离开房间」：{printed!r}"


@pytest.mark.asyncio
async def test_socket_leave_in_real_disconnect_window_clears_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """**真实**断线窗口（``connected=False`` + desired 保留）下 ``socket leave`` 必须清掉本地意图。

    #223：此前该分支只打印「未连接」并**跳过**退房 ⇒ 本地 ``office_id`` 留着 ⇒ 重连后自动回房把用户
    刚退掉的房又回了一遍。现在提示语改为「已清除本地入房意图，重连后不再自动回房」，且不发包（无连接
    可用，服务端会话已随连接销毁 ⇒ 没有可退的成员关系；发包与否由客户端 ``leave_office`` 决定，
    单测 ``test_office_membership.py`` 覆盖）。
    """
    from a2c_smcp.computer.socketio.client import SMCPComputerClient

    sent: list[str] = []

    async def _spy_emit(self: Any, event: str, data: Any = None, *a: Any, **kw: Any) -> None:
        sent.append(event)

    monkeypatch.setattr(SMCPComputerClient, "emit", _spy_emit)

    comp = Computer(name="leave_c2", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    client = SMCPComputerClient(computer=comp)
    client.office_id = "office-old"  # desired 仍被 #203 保留
    assert not client.connected and not client.namespaces, "前置：真实断线窗口"

    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(["socket leave", "exit"]))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    printed: list[str] = []
    import a2c_smcp.computer.cli.interactive_impl as impl_mod

    monkeypatch.setattr(impl_mod.console, "print", lambda message="", *a, **kw: printed.append(str(message)))

    await _interactive_loop(comp, init_client=client)

    assert LEAVE_OFFICE_EVENT not in sent, f"断线时不得发包，实得 {sent!r}"
    assert client.office_id is None, "本地意图必须清掉（否则重连后自动回房把刚退的房又回一遍）"
    assert any("已清除本地入房意图" in line for line in printed), f"应如实提示：{printed!r}"


@pytest.mark.asyncio
async def test_rename_tolerates_leave_and_disconnect_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """改名路径里 ``leave_office`` / ``disconnect`` 失败**降级为告警**，不阻断重连（尽力而为的设计裁决）。"""
    _reset_reconnect_client()
    _ReconnectClient.fail_leave = True
    _ReconnectClient.fail_disconnect = True
    try:
        comp = await _drive_socket_commands(
            [
                "socket connect http://localhost:7000",
                "socket join office-1 alice",
                "socket join office-2 bob",  # 改名 ⇒ 离开/断开都失败，但仍应重连成功
                "exit",
            ],
            monkeypatch,
        )
    finally:
        _ReconnectClient.fail_leave = False
        _ReconnectClient.fail_disconnect = False

    assert len(_ReconnectClient.instances) == 2, "离开/断开失败不得阻断重连"
    assert _ReconnectClient.instances[1].joins == ["office-2"], "重连后的入房应照常发生"
    assert comp.name == "bob"


@pytest.mark.asyncio
async def test_join_denied_after_rename_does_not_report_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """入房被拒时**不得**打印「已加入房间」——成功提示只在该事件真的入房后出现。"""
    _reset_reconnect_client()
    _arm_failures_on_second_connection(monkeypatch, join=True)

    printed: list[str] = []
    monkeypatch.setattr(cli_main, "SMCPComputerClient", _ReconnectClient)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(
        [
            "socket connect http://localhost:7000",
            "socket join office-1 alice",
            "socket join office-2 bob",
            "exit",
        ],
    ))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    import a2c_smcp.computer.cli.interactive_impl as impl_mod

    def _record(message: Any = "", *args: Any, **kwargs: Any) -> None:
        printed.append(str(message))

    monkeypatch.setattr(impl_mod.console, "print", _record)
    comp = Computer(name="rename_c", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    await _interactive_loop(comp)

    # 只有**首次**入房成功；改名那次入房被拒 ⇒ 成功提示恰好一条（不多不少）
    joined = [line for line in printed if "已加入房间 / Joined office" in line]
    assert len(joined) == 1, f"入房被拒不得谎报成功（应只有首条入房报成功）：{printed!r}"
    assert any("加入房间失败" in line for line in printed), f"应如实报告入房失败：{printed!r}"


# ---------------------------------------------------------------------------
# 回归：CLI `--namespace` 必须透传到 SMCPComputerClient，并贯穿事件处理器注册。
# Regression: CLI `--namespace` must propagate to SMCPComputerClient and drive
# event handler registration. This test would fail on code prior to the fix
# for the `--namespace` wire-up bug (handlers stayed on hardcoded `/smcp`).
# ---------------------------------------------------------------------------


def test_cli_namespace_flag_propagates_to_client_handler_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    中文：驱动真实 ``SMCPComputerClient``（只打桩 ``connect``），断言 CLI 传入的
    ``--namespace /tf-custom`` 落到客户端构造器，并且所有事件处理器在该命名空间注册。
    修复前：CLI 只把 namespace 传给 ``connect(namespaces=[...])``，构造器绑死在
    ``/smcp``，因此 ``handlers['/smcp']`` 有 5 项事件、``handlers['/tf-custom']``
    空缺 —— 该断言会失败。

    English: Drive a real ``SMCPComputerClient`` with ``connect`` stubbed, and
    assert that the CLI's ``--namespace /tf-custom`` reaches the constructor and
    that every event handler is registered under that namespace. Pre-fix, the
    CLI only passed namespace to ``connect(namespaces=[...])`` while the client
    constructor stayed pinned on ``/smcp`` — so ``handlers['/smcp']`` held the
    five client:* handlers and ``handlers['/tf-custom']`` was absent, making
    this assertion fail.
    """
    from a2c_smcp.computer.socketio.client import SMCPComputerClient
    from a2c_smcp.smcp import (
        CANCEL_TOOL_CALL_NOTIFICATION,
        GET_BLOB_EVENT,
        GET_CONFIG_EVENT,
        GET_DESKTOP_EVENT,
        GET_RESOURCES_EVENT,
        GET_SKILL_EVENT,
        GET_SKILLS_EVENT,
        GET_TOOLS_EVENT,
        PUT_BLOB_EVENT,
        SMCP_NAMESPACE,
        TOOL_CALL_EVENT,
    )

    custom_ns = "/tf-custom"
    created_clients: list[SMCPComputerClient] = []

    original_init = SMCPComputerClient.__init__

    def spy_init(self: SMCPComputerClient, *a: Any, **kw: Any) -> None:
        original_init(self, *a, **kw)
        created_clients.append(self)

    async def noop_connect(self: SMCPComputerClient, *a: Any, **kw: Any) -> None:
        """不做真实网络连接 / no-op connect to avoid real network"""
        return None

    # 关闭 join_office 的服务端往返 / Short-circuit join_office
    async def noop_join(self: SMCPComputerClient, office_id: str) -> None:
        self.office_id = office_id

    async def noop_leave(self: SMCPComputerClient, office_id: str) -> None:
        self.office_id = None

    async def noop_update(self: SMCPComputerClient) -> None:
        return None

    monkeypatch.setattr(SMCPComputerClient, "__init__", spy_init)
    monkeypatch.setattr(SMCPComputerClient, "connect", noop_connect)
    monkeypatch.setattr(SMCPComputerClient, "join_office", noop_join)
    monkeypatch.setattr(SMCPComputerClient, "leave_office", noop_leave)
    monkeypatch.setattr(SMCPComputerClient, "emit_update_config", noop_update)

    # 立即退出交互 / Exit interactive loop immediately
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession(["exit"]))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: no_patch_stdout())

    # 同上：走纯实现函数 `_run_impl`，不直呼被 @app.command 装饰的 `run`（其形参由 Typer 解析）。
    cli_main._run_impl(
        auto_connect=False,
        auto_reconnect=False,
        url="http://localhost:1",
        namespace=custom_ns,
        auth=None,
        headers=None,
        computer_factory=None,
        mcp_config=None,
    )

    # 至少应创建过一个客户端 / at least one client must have been created
    assert created_clients, "CLI did not construct SMCPComputerClient"
    client = created_clients[0]

    # 1) 构造器接收到自定义 namespace / constructor received the custom namespace
    assert client.namespace == custom_ns, (
        f"Expected client namespace to be {custom_ns!r}, got {client.namespace!r}. "
        "This means the CLI failed to forward --namespace into SMCPComputerClient(...)."
    )

    # 2) 事件处理器必须全部注册在自定义 namespace 下 / all handlers bound to custom ns
    assert custom_ns in client.handlers, (
        f"Expected handlers registered under {custom_ns!r}, "
        f"but found namespaces: {list(client.handlers.keys())!r}"
    )
    assert SMCP_NAMESPACE not in client.handlers, (
        "Handlers must NOT be registered under the default /smcp when CLI specifies a "
        f"different namespace. Got: {list(client.handlers.keys())!r}"
    )

    registered = set(client.handlers[custom_ns].keys())
    assert registered == {
        TOOL_CALL_EVENT,
        GET_TOOLS_EVENT,
        GET_CONFIG_EVENT,
        GET_DESKTOP_EVENT,
        GET_RESOURCES_EVENT,
        GET_BLOB_EVENT,
        PUT_BLOB_EVENT,  # v0.4.0 #196：上行写入通道
        GET_SKILLS_EVENT,
        GET_SKILL_EVENT,
        CANCEL_TOOL_CALL_NOTIFICATION,  # #96：notify:tool_call_cancel 接收处理器
        # #203：引擎级 namespace 生命周期钩子（自动重连后回房 / 断连清意图）
        "connect",
        "disconnect",
        "__disconnect_final",
    }, f"Unexpected event handlers under {custom_ns!r}: {registered!r}"


# ---------------------------------------------------------------------------
# #167 子问题 2：python -m a2c_smcp.computer.cli.main 必须有 __main__ 守卫，
# 否则导入后静默 exit 0 而 CLAUDE.md 将其列为受支持入口。
# ---------------------------------------------------------------------------
def test_main_module_has_name_main_guard() -> None:
    """#167：cli/main.py 必须有 ``if __name__ == "__main__": main()`` 守卫。

    若缺失则 ``python -m a2c_smcp.computer.cli.main run`` 导入后静默 exit 0。
    """
    source = Path(cli_main.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    guard_line = 'if __name__ == "__main__":'
    # 全文件搜索守卫（不限定尾部 N 行，避免守卫后加代码误报）
    guard_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == guard_line:
            guard_idx = i
            break
    assert guard_idx is not None, (
        f"cli/main.py MUST contain {guard_line!r} so that `python -m` invokes main()"
    )
    # 守卫后紧跟 main() 调用
    assert guard_idx + 1 < len(lines), "Guard must not be the last line"
    assert lines[guard_idx + 1].strip() == "main()", (
        f"Expected 'main()' immediately after the guard, got: {lines[guard_idx + 1].strip()!r}"
    )
