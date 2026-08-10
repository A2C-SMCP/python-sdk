"""
合并版 CLI 单测，包含基础与扩展用例
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import a2c_smcp.computer.cli.main as cli_main
from a2c_smcp.computer.cli.main import _interactive_loop
from a2c_smcp.computer.computer import Computer


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

    async def __aenter__(self) -> FakeComputer:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


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
        ' {"id":"REG","type":"pickString","description":"r","options":["us","eu"],"default":"us"}]',
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

    async def join_office(self, office_id: str, computer_name: str) -> None:
        assert self.connected
        self.office_id = office_id
        self.joined_args = (office_id, computer_name)

    async def leave_office(self, office_id: str) -> None:
        assert self.connected
        self.office_id = None

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
        GET_SKILLS_EVENT,
        GET_SKILL_EVENT,
        CANCEL_TOOL_CALL_NOTIFICATION,  # #96：notify:tool_call_cancel 接收处理器
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
