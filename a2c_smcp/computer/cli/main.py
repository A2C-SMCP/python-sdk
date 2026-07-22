"""
文件名: main.py
作者: JQQ
创建日期: 2025/9/18
最后修改日期: 2025/9/22
版权: 2023 JQQ. All rights reserved.
依赖: typer, rich, prompt_toolkit
描述:
  中文: A2C 计算机客户端的命令行入口，提供持续运行模式与基础交互命令。
  English: CLI entry for A2C Computer client. Provides persistent run mode and basic interactive commands.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from a2c_smcp.computer.cli.commands import files_only_non_plugin_bundle_ids
from a2c_smcp.computer.cli.commands import marketplace as mp_cmd
from a2c_smcp.computer.cli.commands import plugin as plugin_cmd
from a2c_smcp.computer.cli.commands import settings as settings_cmd
from a2c_smcp.computer.cli.commands import skill as skill_cmd
from a2c_smcp.computer.cli.completer import A2CCompleter
from a2c_smcp.computer.cli.interactive_impl import interactive_loop as _interactive_loop_impl
from a2c_smcp.computer.cli.utils import (
    parse_kv_pairs,
    resolve_import_target,
)
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inputs.resolver import InputResolver
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerInput as MCPServerInputModel,
)
from a2c_smcp.computer.skills.home import ensure_skill_home
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.computer.utils import console as console_util
from a2c_smcp.smcp import SMCP_NAMESPACE

app = typer.Typer(add_completion=False, help="A2C Computer CLI")
# 使用全局 Console（引用模块属性，便于后续动态切换）
console = console_util.console

# flag scope 的**文件对**（协议 §2.5-3）：`--mcp-config`（flag 层 mcp.json）+ `--settings`（flag 层 settings.json），
# 与其余 scope 的双文件形态对称。二者均在根回调与 `run` 上各声明一份（根回调 `invoke_without_command=True` 时
# **就是** run 路径，故 `a2c-computer --mcp-config x.json` 必须可用）⇒ help 文案抽常量，杜绝两份漂移。
# The flag-scope **file pair**; declared on both the root callback and `run` (the root callback *is* the run path
# when no subcommand is given) ⇒ help text is a shared constant so the two copies cannot drift.
_MCP_CONFIG_HELP = (
    "额外 mcp.json（flag scope，含 servers/inputs；优先级次高、仅低于 policy）。支持 @file 语法或直接文件路径 / "
    "extra mcp.json (flag scope, with servers/inputs; second-highest precedence, below policy only)"
)
_SETTINGS_HELP = (
    "额外 settings.json（flag scope，优先级次高、仅低于 policy）/ extra settings.json (flag scope, second-highest "
    "precedence, below policy only)"
)


# ------------------------------
# 根级上下文（#97）：把根回调采集的全局 flag 透传给 settings 子命令
# Root context (#97): carry callback-gathered global flags to the `settings` subcommands
# ------------------------------
@dataclass(frozen=True, slots=True)
class _RootState:
    """根回调采集、供 ``settings`` 子命令读取的根级上下文（经 ``ctx.obj`` 在 Click 子上下文间继承）。

    #97：``--settings`` 此前仅透传给 ``run`` 路径，``settings`` 子命令读不到 → flag scope
    恒返回 ``{}``。统一收口到 ``ctx.obj``。#116：project/local scope 锚定进程 cwd，
    不再有 ``--add-dir`` / workdir 状态。
    Root-level context gathered by the callback and read by `settings` subcommands via inherited ``ctx.obj``.

    **#154（隔离审查 🔴2）**：flag scope 的**文件对**（``--settings`` + ``--mcp-config``）在根回调与 ``run``
    上各声明一份，但根那份此前**只在无子命令时**被消费 ⇒ ``a2c-computer --mcp-config x.json run``（flag 置于
    子命令之前）会把文件**静默丢弃**（连 fail-fast 都碰不到，用户落进空 REPL）。二者现统一收口到本状态，
    由 ``run`` 兜底回读（``run`` 自身的显式值优先）。
    """

    flag_path: Path | None = None  # --settings <file>（flag 层 settings.json）
    mcp_config: str | None = None  # --mcp-config <file>（flag 层 mcp.json；未剥 `@`、未校验）


def _root_state(ctx: typer.Context) -> _RootState:
    """从 Typer 上下文取根状态；缺失（未经根回调，如单测直呼）时回退空状态。/ Read root state, empty fallback。"""
    obj = ctx.obj
    return obj if isinstance(obj, _RootState) else _RootState()


def _mcp_flag_path(raw: str | None) -> Path | None:
    """
    ``--mcp-config`` → 校验过的 :class:`Path`；形状不符 **fail-fast** / Validate the flag file's shape, fail-fast。

    **为何在此 fail-fast，而非交给** :func:`~a2c_smcp.computer.settings.mcp_config.load_mcp_config_file`：后者按 §5.6
    对**全部五个 durable scope** 共用地容错（人编文件：损坏 → 空视图 + 诊断 + 保留原文件），在那里 fail-fast 会让一份
    陈旧的 user-scope ``mcp.json`` 阻断启动。不对称是**刻意**的：durable 文件是**环境既有**的 ⇒ 容错；flag 文件是
    **操作员此刻在命令行上点名**的 ⇒ 形状不符意味着他要的**每个 server 都没加载**，静默降级会把人丢进一个空的 REPL、
    而那行红字早已滚出屏幕。

    这也是对历史 ``--config`` 那两个裸 ``except Exception`` + ``# pragma: no cover`` 吞异常块的**刻意反转**——
    拼错的路径本会静默降级启动。退出码 2 = Click 的 usage-error 惯例。

    **v0.3.0 破坏性变更**：文件形状由旧 ``--config`` 的「裸 server 对象 / server 数组」硬切为 ``mcp.json`` 形状
    ``{"servers": {...}, "inputs": [...]}``（协议 §2.5-3：flag scope 的文件对 = ``--mcp-config`` + ``--settings``）。
    无存量用户，按通用口径不做兼容设计。
    """
    if not isinstance(raw, str):  # 直呼 _run_impl 时 OptionInfo 可能泄漏 → 守卫（同 settings_file 姿态）
        return None
    path = Path(raw[1:] if raw.startswith("@") else raw)  # ``@file`` 是可选糖，与裸路径等价
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        console.print(f"[red]✗ 无法读取 --mcp-config 文件 / cannot read --mcp-config file: {path}: {e}[/red]")
        raise typer.Exit(2) from e
    if isinstance(data, Mapping) and ("servers" in data or "inputs" in data):
        return path
    # 形状不符：尽量说清「实际是什么」+ 给出可照抄的改写形状。
    shape = "server 数组 / an array of servers" if isinstance(data, list) else f"{type(data).__name__}"
    if isinstance(data, Mapping):
        shape = "裸 server 对象（无 servers/inputs 键）/ a bare server object (no servers/inputs key)"
    console.print(
        f"[red]✗ --mcp-config 文件格式无效 / invalid --mcp-config file: {path}[/red]\n"
        f"[red]  期望 mcp.json 形状 {{'servers': {{...}}, 'inputs': [...]}}，实际为 {shape}。[/red]\n"
        f"[red]  Expected the mcp.json shape {{'servers': {{...}}, 'inputs': [...]}}.[/red]\n"
        "[yellow]  提示：v0.3.0 起 --config 的裸 server 格式已废止，server 身份由 map key 承载"
        "（**去掉 body 里的 name 字段**，否则会因 name≠key 被丢弃）：[/yellow]\n"
        '[yellow]  Hint: the legacy bare-server format is removed; the map key is the identity (drop the "name" field):[/yellow]\n'
        '[dim]  {"servers": {"<server-name>": {"type": "stdio", "server_parameters": {...}}}, "inputs": []}[/dim]',
    )
    raise typer.Exit(2)


# ------------------------------
# Computer 工厂函数类型标注
# ------------------------------
# 中文:
#  - 该类型表示一个可调用对象（函数或类构造器），用于创建 Computer 或其子类的实例。
#  - 参数签名需与 Computer.__init__ 兼容；你可以据此在你自己的工厂函数上添加类型注释。
# English:
#  - This type denotes a callable (function or class constructor) used to create a Computer or subclass instance.
#  - The parameter signature must be compatible with Computer.__init__; use it for your own factory annotations.
ComputerFactory = Callable[
    [
        set["MCPServerInputModel"] | None,
        bool,
        bool,
        Callable[[str, str, str, dict], bool] | None,
        InputResolver | None,
    ],
    Computer,
]


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    auto_connect: bool = typer.Option(True, help="是否自动连接 / Auto connect"),
    auto_reconnect: bool = typer.Option(True, help="是否自动重连 / Auto reconnect"),
    url: str | None = typer.Option(None, help="Socket.IO 服务器URL，例如 https://host:port"),
    namespace: str = typer.Option(
        SMCP_NAMESPACE,
        "--namespace",
        help="Socket.IO 命名空间（默认: /smcp）/ Namespace to connect (default: /smcp)",
    ),
    auth: str | None = typer.Option(None, help="认证参数，形如 key:value,foo:bar"),
    headers: str | None = typer.Option(None, help="请求头参数，形如 key:value,foo:bar"),
    computer_factory: str | None = typer.Option(
        None,
        "--computer-factory",
        help=(
            "指定用于构建 Computer 的导入路径 (模块:属性 或 模块.属性)。\n"
            "例如 my_pkg.my_mod:build_computer 或 my_pkg.my_mod.MySubComputer。\n"
            "不支持以 '.' 开头的相对导入；模块解析相对于运行 a2c-computer 时的工作目录可导入包环境。"
        ),
    ),
    no_color: bool = typer.Option(
        False,
        "--no-color",
        help="关闭彩色输出（PyCharm控制台不渲染ANSI时可使用） / Disable ANSI colors",
    ),
    json_output: bool = typer.Option(False, "--json", help="非交互默认 JSON 输出（子命令亦各带 --json）/ JSON output"),
    mcp_config: str | None = typer.Option(None, "--mcp-config", "-c", help=_MCP_CONFIG_HELP),
    settings_file: str | None = typer.Option(None, "--settings", help=_SETTINGS_HELP),
    approve_all_mcp: bool = typer.Option(
        False, "--approve-all-mcp", help="启动期全批 pending MCP server（仅本次、不落盘）/ approve all pending MCP (this run)",
    ),
) -> None:
    """
    根级入口：
    - 若未指定子命令，则等价于执行 `run`，保持 `a2c-computer` 和 `a2c-computer run` 两种用法都可用。
    - 若指定了子命令，则不做处理，交给子命令。
    """
    # 根据 no_color 动态调整全局 Console
    if no_color:
        global console
        console_util.set_no_color(True)
        # 重新绑定本地引用
        console = console_util.console

    # #97：采集根级上下文（--settings flag scope 文件）存入 ctx.obj，供 settings 子命令读取。
    # Click 子上下文默认继承父 ctx.obj，故无论是否带子命令都先填充（无子命令时 run 路径仍走显式参数）。
    ctx.obj = _RootState(
        flag_path=Path(settings_file) if isinstance(settings_file, str) else None,
        mcp_config=mcp_config if isinstance(mcp_config, str) else None,
    )

    if ctx.invoked_subcommand is None:
        # 注意：不要直接调用被 @app.command 装饰的 run()，否则未传入的参数会保留 OptionInfo 默认值
        # 这里改为调用纯实现函数 _run_impl。
        _run_impl(
            auto_connect=auto_connect,
            auto_reconnect=auto_reconnect,
            url=url,
            namespace=namespace,
            auth=auth,
            headers=headers,
            computer_factory=computer_factory,
            mcp_config=mcp_config,
            approve_all_mcp=approve_all_mcp,
            settings_file=settings_file,
        )


async def _interactive_loop(
    comp: Computer,
    init_client: SMCPComputerClient | None = None,
    *,
    approve_all_mcp: bool = False,
    settings_flag_path: Path | None = None,
) -> None:
    """
    中文: 兼容外部引用的包装器，委托到 interactive_impl，并注入依赖。
    English: Backward-compatible wrapper that delegates to interactive_impl with dependencies injected.

    ``settings_flag_path`` = ``--settings <file>``（flag 层 **settings.json**）。旧名 ``mcp_flag_config`` 已更名：
    它从来不是 mcp.json，那个名字主动误导（#154）。flag 层 **mcp.json**（``--mcp-config``）走另一条路——注入
    :class:`Computer`（boot 声明式输入），不经本参数。
    """
    await _interactive_loop_impl(
        comp,
        session_factory=PromptSession,
        patch_stdout_ctx=patch_stdout,
        smcp_client_cls=SMCPComputerClient,
        init_client=init_client,
        completer=A2CCompleter(comp),
        approve_all_mcp=approve_all_mcp,
        settings_flag_path=settings_flag_path,
    )


def _run_impl(
    *,
    auto_connect: bool,
    auto_reconnect: bool,
    url: str | None,
    namespace: str | None,
    auth: str | None,
    headers: str | None,
    computer_factory: str | None,
    mcp_config: str | None,
    approve_all_mcp: bool = False,
    settings_file: str | None = None,
) -> None:
    """
    纯实现函数：不要在此处使用 Typer 的 Option 默认值，避免 OptionInfo 泄露到运行时。
    Both CLI (@app.command) 与回调 (@app.callback) 在需要时调用本函数。

    approve_all_mcp / settings_file / mcp_config 来自全局 flag ``--approve-all-mcp`` / ``--settings`` /
    ``--mcp-config``：透传启动期 MCP 批准框 + flag scope 的**文件对**（settings.json + mcp.json，§2.5-3）。

    ``--mcp-config`` **不在此急切挂载**（#154）：它是 flag 层 mcp.json，经 :class:`Computer` 注入、由
    :func:`~a2c_smcp.computer.cli.commands.plugin.run_mcp_approval` 与其余 scope **同一条**解析/门控/挂载路径处理。
    历史 ``--config`` 在此 ``json.loads`` → ``amount_server`` 直挂，**绕开 scope 合并、origin 记录与审批门**——
    正是 origin 缺失（§2.5-5 MUST）与 §4.9.1-2 连坐停摘用户 server 的根源。
    """
    # 形状 fail-fast **先于任何副作用**：历史解析在 ``connect`` 之后（旧 :255 vs :275），坏文件会留下一个已连接的
    # socket 和一个已 boot 的 Computer。/ Validate before any side effect (the old parse ran post-connect).
    flag_mcp_path = _mcp_flag_path(mcp_config)

    async def _amain() -> None:
        # 初始化空配置，后续通过交互动态维护 / init with empty config, then manage dynamically
        # 解析工厂：默认使用 Computer 构造函数；若提供 --computer-factory，则动态导入。
        comp_factory_obj: Any = Computer
        if computer_factory:
            try:
                comp_factory_obj = resolve_import_target(computer_factory)
            except Exception as e:  # pragma: no cover
                console.print(f"[red]解析 --computer-factory 失败: {e} / Failed to resolve computer factory: {e}[/red]")
                comp_factory_obj = Computer

        # 类型提示：comp_factory_obj 应满足 ComputerFactory，可是运行时仅作 best-effort 校验
        if not callable(comp_factory_obj):  # pragma: no cover
            console.print("[red]计算机构造目标不可调用，将回退到默认 Computer[/red]")
            comp_factory_obj = Computer

        # inputs / mcp_servers 恒空：CLI **不是**嵌入式宿主 —— 所有 server 经声明面（mcp.json 各 scope，含
        # ``--mcp-config`` flag 层）解析后由审批门挂载，或经 REPL 运行期挂载。``mcp_servers`` 是 embed 层的
        # 声明面（§2.5-3），CLI 下恒空是正确表现、非缺陷（见 ``Computer.__init__``）。
        comp = comp_factory_obj(
            name="friday_hands",
            inputs=set(),
            mcp_servers=set(),
            auto_connect=auto_connect,
            auto_reconnect=auto_reconnect,
            mcp_flag_config=flag_mcp_path,
        )
        async with comp:
            init_client: SMCPComputerClient | None = None
            if url:
                try:
                    auth_dict = parse_kv_pairs(auth)
                    headers_dict = parse_kv_pairs(headers)
                except Exception as e:
                    console.print(f"[red]启动参数解析失败 / Failed to parse CLI params: {e}[/red]")
                    auth_dict = None
                    headers_dict = None
                # 将 CLI 指定的 namespace 透传给客户端实例，确保事件处理器绑定到正确命名空间
                # Pass CLI-specified namespace to the client instance so event handlers bind to the right namespace
                effective_namespace = namespace or SMCP_NAMESPACE
                init_client = SMCPComputerClient(computer=comp, namespace=effective_namespace)
                # 通过 CLI 指定命名空间，确保连接时建立对应 namespace 会话
                await init_client.connect(url, auth=auth_dict, headers=headers_dict, namespaces=[effective_namespace])
                console.print("[green]已通过启动参数连接到 Socket.IO / Connected via CLI options[/green]")

            # `--mcp-config` 的 servers/inputs 不在此加载：它是 flag 层 mcp.json，已注入 Computer，由
            # `run_mcp_approval` 与 user/project/local/policy 各层**同路**解析（含 inputs 入池）+ 过审批门 + 挂载。
            # settings_file 直接调用 run() 时可能泄漏 OptionInfo → isinstance 守卫（同 computer_factory 容错姿态）。
            await _interactive_loop(
                comp,
                init_client=init_client,
                approve_all_mcp=approve_all_mcp is True,
                settings_flag_path=Path(settings_file) if isinstance(settings_file, str) else None,
            )

    asyncio.run(_amain())


@app.command()
def run(
    ctx: typer.Context,
    auto_connect: bool = typer.Option(True, help="是否自动连接 / Auto connect"),
    auto_reconnect: bool = typer.Option(True, help="是否自动重连 / Auto reconnect"),
    url: str | None = typer.Option(None, help="Socket.IO 服务器URL，例如 https://host:port"),
    namespace: str = typer.Option(
        SMCP_NAMESPACE,
        "--namespace",
        help="Socket.IO 命名空间（默认: /smcp）/ Namespace to connect (default: /smcp)",
    ),
    auth: str | None = typer.Option(None, help="认证参数，形如 key:value,foo:bar"),
    headers: str | None = typer.Option(None, help="请求头参数，形如 key:value,foo:bar"),
    computer_factory: str | None = typer.Option(
        None,
        "--computer-factory",
        help=(
            "指定用于构建 Computer 的导入路径 (模块:属性 或 模块.属性)。\n"
            "例如 my_pkg.my_mod:build_computer 或 my_pkg.my_mod.MySubComputer。\n"
            "不支持以 '.' 开头的相对导入；模块解析相对于运行 a2c-computer 时的工作目录可导入包环境。"
        ),
    ),
    mcp_config: str | None = typer.Option(None, "--mcp-config", "-c", help=_MCP_CONFIG_HELP),
    settings_file: str | None = typer.Option(None, "--settings", help=_SETTINGS_HELP),
    approve_all_mcp: bool = typer.Option(
        False, "--approve-all-mcp", help="启动期全批 pending MCP server（仅本次、不落盘）/ approve all pending MCP (this run)",
    ),
) -> None:
    """
    中文: 启动计算机并进入持续运行模式。servers 与 inputs 经 mcp.json 各 scope（含 ``--mcp-config`` flag 层）声明。
    English: Boot the computer and enter the persistent loop. Servers/inputs come from mcp.json scopes.
    """
    # flag 文件对兜底回读根级（隔离审查 🔴2）：``a2c-computer --mcp-config x.json run`` 里 flag 被 Click 归给
    # 根回调，``run`` 自身收到 None ⇒ 不回读就**静默丢弃**（连 fail-fast 都碰不到）。``run`` 自身显式值优先。
    st = _root_state(ctx)
    mcp_config = mcp_config if isinstance(mcp_config, str) else st.mcp_config
    settings_file = settings_file if isinstance(settings_file, str) else (str(st.flag_path) if st.flag_path else None)
    _run_impl(
        auto_connect=auto_connect,
        auto_reconnect=auto_reconnect,
        url=url,
        namespace=namespace,
        auth=auth,
        headers=headers,
        computer_factory=computer_factory,
        mcp_config=mcp_config,
        approve_all_mcp=approve_all_mcp,
        settings_file=settings_file,
    )


# ---------------------------------------------------------------------------
# 非交互 Typer 子命令（marketplace / skill，S15 #68）/ Non-interactive Typer subcommands
# ---------------------------------------------------------------------------
# 非交互形态不 boot Computer、不连 socket：以 env 解析 SKILL Home + 新建 registry，直接读写物化层 / staging。
# trust 缺 confirm（confirm=None）→ marketplace add 须显式 --trust，否则退出码 1（§4.6 / §11）。
marketplace_app = typer.Typer(help="SKILL marketplaces (git sources)")
skill_app = typer.Typer(help="Skills cross-source query (list / info)")
# plugin / settings（S16 #69）：非交互无 live Computer → plugin install/enable 走 ledger-only（register=None），
# MCP server 挂载延到下次 REPL boot 经批准框落地（§4.6）。settings 直读写物化意图层。
plugin_app = typer.Typer(help="Plugins — skill+mcp bundles (install / enable / list ...)")
settings_app = typer.Typer(help="settings.json intent layer (show / get / set / edit)")


@marketplace_app.command("add")
def _mp_add(
    git_url: str = typer.Argument(..., help="git URL 或 owner/repo 简写 / git URL or owner/repo shorthand"),
    name: str | None = typer.Option(None, "--name", help="marketplace 名（默认从 URL 派生）/ name (derived from URL by default)"),
    trust: bool = typer.Option(False, "--trust", help="跳过 trust 确认（非交互必需）/ skip trust prompt (required non-interactively)"),
    auto_update: bool = typer.Option(False, "--auto-update", help="启用 per-source 自动更新 / enable per-source auto-update"),
    no_clone: bool = typer.Option(False, "--no-clone", help="仅注册意图、不 clone（debug）/ register intent only (debug)"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出 / JSON output"),
) -> None:
    """添加 marketplace（非交互须 --trust）/ Add a marketplace (requires --trust non-interactively)."""
    code = asyncio.run(
        mp_cmd.marketplace_add(
            SkillRegistry(), ensure_skill_home(), os.environ, git_url,
            name=name, trust=trust, auto_update=auto_update, no_clone=no_clone, confirm=None, json_output=json_output,
        ),
    )
    raise typer.Exit(code)


@marketplace_app.command("list")
def _mp_list(json_output: bool = typer.Option(False, "--json", help="JSON 输出 / JSON output")) -> None:
    """列出已知 marketplace / List known marketplaces."""
    raise typer.Exit(mp_cmd.marketplace_list(ensure_skill_home(), os.environ, json_output=json_output))


@marketplace_app.command("info")
def _mp_info(name: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")) -> None:
    """marketplace 详情 / Marketplace detail."""
    raise typer.Exit(mp_cmd.marketplace_info(ensure_skill_home(), os.environ, name, json_output=json_output))


@marketplace_app.command("remove")
def _mp_remove(
    name: str = typer.Argument(...),
    keep_plugins: bool = typer.Option(False, "--keep-plugins", help="保留 installed plugin（标记孤儿）/ keep installed plugins (orphaned)"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """移除 marketplace（默认级联卸载其下 plugin）/ Remove a marketplace (cascade by default)."""
    code = asyncio.run(
        mp_cmd.marketplace_remove(
            SkillRegistry(), ensure_skill_home(), os.environ, name,
            keep_plugins=keep_plugins, confirm=None, mcp_cbs=None, json_output=json_output,
        ),
    )
    raise typer.Exit(code)


@marketplace_app.command("refresh")
def _mp_refresh(
    target: str = typer.Argument("all", help="marketplace 名或 all / a name or 'all'"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """刷新 marketplace（git pull / 重 clone + 对账）/ Refresh marketplaces."""
    code = asyncio.run(
        mp_cmd.marketplace_refresh(SkillRegistry(), ensure_skill_home(), os.environ, target, json_output=json_output),
    )
    raise typer.Exit(code)


@marketplace_app.command("set")
def _mp_set(
    name: str = typer.Argument(...),
    assignment: str = typer.Argument(..., help="auto-update=<bool>"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """设置 per-source 旗（仅 auto-update=<bool>）/ Set a per-source flag (auto-update only)."""
    key, _, value = assignment.partition("=")
    raise typer.Exit(mp_cmd.marketplace_set(ensure_skill_home(), os.environ, name, key, value, json_output=json_output))


@skill_app.command("list")
def _skill_list(
    source: str | None = typer.Option(None, "--source", help="过滤来源 mp|mcp|user / filter by source"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """跨源列出可见 SKILL（非交互重建 registry；mcp 源需 live 服务器 → REPL）/ List skills (registry rebuilt offline)."""
    reg = asyncio.run(skill_cmd.rebuild_registry(ensure_skill_home(), os.environ))
    raise typer.Exit(skill_cmd.skill_list(reg, source=source, json_output=json_output))


@skill_app.command("info")
def _skill_info(name: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")) -> None:
    """SKILL 详情 / Skill detail."""
    reg = asyncio.run(skill_cmd.rebuild_registry(ensure_skill_home(), os.environ))
    raise typer.Exit(skill_cmd.skill_info(reg, name, json_output=json_output))


# ── plugin 子命令（非交互 ledger-only：无 MCP 回调，挂载延到下次 REPL boot）/ plugin subcommands ──
@plugin_app.command("install")
def _plugin_install(
    plugin_id: str = typer.Argument(..., help="<plugin>@<marketplace>"),
    scope: str = typer.Option("user", "--scope", help="user|project|local"),
    version: str | None = typer.Option(None, "--version", help="锁版本（git tag/SHA）/ pin version"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """安装 plugin（非交互 ledger-only；外来 MCP 同名硬抛退出码 1）/ Install a plugin (ledger-only)."""
    code = asyncio.run(
        plugin_cmd.plugin_install(
            SkillRegistry(), ensure_skill_home(), os.environ, plugin_id, scope=scope, version=version, json_output=json_output,
        ),
    )
    raise typer.Exit(code)


@plugin_app.command("uninstall")
def _plugin_uninstall(
    plugin_id: str = typer.Argument(...),
    keep_servers: bool = typer.Option(False, "--keep-servers"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """卸载 plugin / Uninstall a plugin."""
    code = asyncio.run(
        plugin_cmd.plugin_uninstall(
            SkillRegistry(), ensure_skill_home(), os.environ, plugin_id,
            non_plugin_bundle_ids=files_only_non_plugin_bundle_ids(os.environ),
            keep_servers=keep_servers, json_output=json_output,
        ),
    )
    raise typer.Exit(code)


@plugin_app.command("enable")
def _plugin_enable(plugin_id: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")) -> None:
    """启用 plugin（非交互 ledger-only）/ Enable a plugin (ledger-only)."""
    code = asyncio.run(
        plugin_cmd.plugin_enable(SkillRegistry(), ensure_skill_home(), os.environ, plugin_id, json_output=json_output),
    )
    raise typer.Exit(code)


@plugin_app.command("disable")
def _plugin_disable(plugin_id: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")) -> None:
    """禁用 plugin = 整 plugin 下线 / Disable a plugin (whole-plugin offline)."""
    code = asyncio.run(
        plugin_cmd.plugin_disable(
            SkillRegistry(), ensure_skill_home(), os.environ, plugin_id,
            non_plugin_bundle_ids=files_only_non_plugin_bundle_ids(os.environ),
            json_output=json_output,
        ),
    )
    raise typer.Exit(code)


@plugin_app.command("list")
def _plugin_list(
    available: bool = typer.Option(
        False, "--available", help="[deprecated] 已弃用：v0.3.0 起默认列全部已安装 / no-op, listing all is the default",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出 installed plugin / List installed plugins."""
    raise typer.Exit(plugin_cmd.plugin_list(ensure_skill_home(), os.environ, available=available, json_output=json_output))


@plugin_app.command("info")
def _plugin_info(plugin_id: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")) -> None:
    """plugin 详情 / Plugin detail."""
    raise typer.Exit(plugin_cmd.plugin_info(ensure_skill_home(), os.environ, plugin_id, json_output=json_output))


@plugin_app.command("gc")
def _plugin_gc(
    prune_dangling: bool = typer.Option(
        False, "--prune-dangling", help="连带 prune 悬挂安装意图（意图 ∖ 账本 ∧ 不可达）/ also prune dangling intents",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """清理孤儿 plugin + 诊断悬挂意图（非交互删权威意图须显式 --prune-dangling）/ GC orphans + diagnose dangling."""
    code = asyncio.run(
        plugin_cmd.plugin_gc(
            SkillRegistry(), ensure_skill_home(), os.environ,
            non_plugin_bundle_ids=files_only_non_plugin_bundle_ids(os.environ),
            confirm=None, prune_dangling=prune_dangling, json_output=json_output,
        ),
    )
    raise typer.Exit(code)


# ── settings 子命令 / settings subcommands ────────────────────────────────────
# 四个 settings 子命令经 ``ctx: typer.Context`` 取根状态（#97）：show/get 需 flag_path 才能读 flag scope；
# project|local 锚定进程 cwd（#116），无需额外状态（flag/policy 本就只读，无 flag_path）。
@settings_app.command("show")
def _settings_show(
    ctx: typer.Context,
    scope: str = typer.Option("merged", "--scope", help="user|project|local|flag|policy|merged"),
    json_output: bool = typer.Option(True, "--json", help="JSON 输出（默认开）/ JSON output (default on)"),
) -> None:
    """展示某 scope 的 settings（默认 merged）/ Show settings (default merged)."""
    st = _root_state(ctx)
    raise typer.Exit(
        settings_cmd.settings_show(
            ensure_skill_home(), os.environ, scope=scope,
            flag_path=st.flag_path, json_output=json_output,
        ),
    )


@settings_app.command("get")
def _settings_get(
    ctx: typer.Context,
    key: str = typer.Argument(...),
    scope: str = typer.Option("merged", "--scope"),
    json_output: bool = typer.Option(True, "--json"),
) -> None:
    """读取单字段 / Read a single field."""
    st = _root_state(ctx)
    raise typer.Exit(
        settings_cmd.settings_get(
            ensure_skill_home(), os.environ, key, scope=scope,
            flag_path=st.flag_path, json_output=json_output,
        ),
    )


@settings_app.command("set")
def _settings_set(
    key: str = typer.Argument(...),
    value: str = typer.Argument(..., help="JSON 优先，回退字面字符串 / JSON preferred, else literal"),
    scope: str = typer.Option("user", "--scope", help="user|project|local（flag/policy 只读）"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """写单字段（flag/policy 只读）/ Write a single field (flag/policy read-only)."""
    raise typer.Exit(
        settings_cmd.settings_set(
            ensure_skill_home(), os.environ, key, value, scope=scope,
            json_output=json_output,
        ),
    )


@settings_app.command("edit")
def _settings_edit(
    scope: str = typer.Option("user", "--scope", help="user|project|local"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """用 $EDITOR 打开该层 settings.json（非交互无 reconcile）/ Open settings.json in $EDITOR."""
    code, _post = settings_cmd.settings_edit(
        ensure_skill_home(), os.environ, scope=scope, json_output=json_output,
    )
    raise typer.Exit(code)


app.add_typer(marketplace_app, name="marketplace")
app.add_typer(skill_app, name="skill")
app.add_typer(plugin_app, name="plugin")
app.add_typer(settings_app, name="settings")


# 为 console_scripts 兼容提供入口
def main() -> None:  # pragma: no cover
    # 使用 Typer 应用入口，而不是直接调用命令函数
    # 直接调用被 @app.command 装饰的函数会传入 OptionInfo 默认值，导致参数类型错误
    app()
