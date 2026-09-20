"""
文件名: interactive_impl.py
作者: JQQ
创建日期: 2025/9/25
最后修改日期: 2025/9/25
版权: 2023 JQQ. All rights reserved.
依赖: prompt_toolkit, rich
描述:
  中文: CLI 交互循环的实现模块，支持依赖注入（会话、stdout 补丁、SMCP 客户端）。
  English: Implementation module for CLI interactive loop with DI (session, stdout patch, SMCP client).
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol, TypedDict

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer
from pydantic import TypeAdapter

from a2c_smcp.computer.cli.banner import render_banner
from a2c_smcp.computer.cli.commands import marketplace as mp_cmd
from a2c_smcp.computer.cli.commands import plugin as plugin_cmd
from a2c_smcp.computer.cli.commands import settings as settings_cmd
from a2c_smcp.computer.cli.commands import skill as skill_cmd
from a2c_smcp.computer.cli.help import render_help
from a2c_smcp.computer.cli.resolve import (
    AmbiguousTargetError,
    TargetNotFoundError,
    collect_candidates,
    resolve_target,
)
from a2c_smcp.computer.cli.utils import console, parse_kv_pairs, print_mcp_config, print_status, print_tools
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import MCPServerInput as MCPServerInputModel
from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.smcp import MCPServerConfig as SMCPServerConfigDict
from a2c_smcp.smcp import MCPServerInput as SMCPServerInputDict
from a2c_smcp.smcp import ToolCallReq as SMCPToolCallReq
from a2c_smcp.utils.bundle_id import resolve_bundle_id

# 定义上下文管理器类型
ContextManager = AbstractContextManager[None]


# 定义上下文管理器工厂函数的协议
class PatchStdoutCtx(Protocol):
    def __call__(self, *, raw: bool = False) -> ContextManager: ...


class _Session(Protocol):
    async def prompt_async(self, *_: str, **__: Any) -> str: ...


def _resolve_or_report(comp: Computer, token: str, *, settings_flag_path: Path | None) -> str | None:
    """人机面寻址：token → **bundle_id**，未命中 / 多命中打印诊断并返回 ``None``（#143 / R4）。

    库层公开 API 一律收 bundle_id、无 name 启发式（协议 ``sdk-api-guidance.md §5.1``），故 REPL 收到的
    ``<name|bundle_id>`` **必须**在此解析后再下传。返回 ``None`` = 调用方不得继续执行该动词——**绝不**静默成功。

    Resolve a REPL token to a bundle_id; on miss/ambiguity print diagnostics and return None (never fake success).
    """
    try:
        return resolve_target(token, collect_candidates(comp, settings_flag_path=settings_flag_path))
    except AmbiguousTargetError as e:
        # §5.1-3：列出每个候选的 bundle_id + display name + 归属（只列 bundle_id 用户分不清哪个是自己的）。
        console.print(
            f"[yellow]⚠ 有 {len(e.candidates)} 个 server 叫 '{token}' / {len(e.candidates)} servers named '{token}':[/yellow]",
        )
        width = max(len(c.bundle_id) for c in e.candidates)
        for cand in sorted(e.candidates, key=lambda c: c.bundle_id):
            console.print(f"[yellow]   {cand.bundle_id:<{width}}  {cand.name}  ({cand.attribution})[/yellow]")
        console.print("[yellow]请用 bundle_id 重试 / Retry with a bundle_id[/yellow]")
    except TargetNotFoundError:
        console.print(f"[red]❌ 未找到服务器 '{token}' / Server '{token}' not found[/red]")
    return None


def _resolve_lifecycle_target(comp: Computer, token: str, *, verb: str, settings_flag_path: Path | None) -> str | None:
    """为 ``start`` / ``stop`` 解析 token，且**额外要求命中的 bundle_id 当前真的挂载着**（#143 决策 1 补丁）。

    决策 1 的查找空间是「运行期 ∪ 声明面」（为让 ``server rm`` 的档 1-4 可达）。副作用：仅**声明**而尚未挂载
    的 server（如本次启动未过审批门的 project 声明）也会解析成功。但 ``start``/``stop`` 操作的是**运行期进程**，
    对未挂载目标：``astop_client`` 会静默 no-op → 打印「✅ 停止完成」（本 Issue 要根治的假回执）；``astart_client``
    会抛内部 ``Unknown server bundle_id=...``（把内部 id 概念漏给用户）。故解析后再判「是否已挂载」，未挂载则
    诚实陈述而非假成功/漏内部错。

    .. note::
       这两条路径是决策 1 引入的、协议 §5.1-1「在**活跃配置集**反查」未覆盖的态（协议此处 MUST 双端逐字一致）。
       已开 **#171** 求协议把查找空间改为「活跃集 ∪ 声明面」并明确本态行为，裁决后双端同步。

    :returns: 已挂载 → 其 bundle_id；未命中 / 多命中 / 已声明未挂载 → ``None``（打印诊断，调用方不得执行）。
    """
    bundle_id = _resolve_or_report(comp, token, settings_flag_path=settings_flag_path)
    if bundle_id is None:
        return None
    mounted = comp.mcp_manager is not None and any(
        resolve_bundle_id(cfg) == bundle_id for cfg in comp.mcp_manager.server_configs()
    )
    if mounted:
        return bundle_id
    if verb == "stop":
        console.print(f"[yellow]⚠ 服务器 '{token}' 尚未挂载，无需停止 / Server '{token}' not mounted; nothing to stop[/yellow]")
    else:
        console.print(
            f"[yellow]⚠ 服务器 '{token}' 已声明但未挂载 / Server '{token}' declared but not mounted[/yellow]",
        )
        console.print("[yellow]  提示：它可能在 mcp.json 里但本次启动未过批准门 / it may be pending the approval gate[/yellow]")
    return None


class ConnectionArgs(TypedDict):
    """一次连接的参数——改名重连所需的最小集合。

    由 ``socket connect`` 分支记录；``--auto-connect`` 启动路径经 ``init_connection`` 注入。

    ``namespace`` 存的是**生效**命名空间（**不留 ``None``**）：``SMCPComputerClient`` 的默认值只在
    该参数**缺席**时生效，显式传 ``None`` 会把它覆盖成 ``None`` ⇒ 新连接的所有 handler 注册到默认
    命名空间 ``'/'``、``emit`` 落到一个未连接的命名空间——一条「哑连接」（入房必抛
    ``BadNamespaceError``，且客户端自己无法恢复）。故记录方须填入实际生效值。

    The **effective** namespace (never ``None``): ``SMCPComputerClient``'s default only applies when
    the argument is *absent*; passing ``None`` explicitly overrides it and yields a dumb connection
    whose handlers all land on ``'/'``.
    """

    url: str
    namespace: str
    auth: dict[str, Any] | None
    headers: dict[str, Any] | None


def _client_in_office(client: Any) -> bool:
    """客户端是否**真实**在房间里——优先用客户端自己的判据，桩对象回退到 ``office_id``。

    ``office_id`` 是 *desired*：重连窗口内它被刻意保留（#203），此时 namespace 并未在册、并不真在
    房里。客户端已把正确判据固化在 ``SmcpComputerClient._in_office()``（其文档明确要求据此判断），
    CLI 沿用同一判据以免在断线窗口里对一个并不在的房间发 leave。
    """
    probe = getattr(client, "_in_office", None)
    if callable(probe):
        return bool(probe())
    return bool(getattr(client, "office_id", None))


async def _rename_via_reconnect(
    *,
    smcp_client: Any,
    smcp_client_cls: type[Any],
    comp: Computer,
    connection: ConnectionArgs,
    office_id: str,
    new_name: str,
) -> tuple[Any, bool]:
    """改名 = **换新连接**（协议 events.md:610——身份在 sid 生命周期内不可变）。

    同一 sid 声明不同的 ``name`` 会被服务端以 ``403`` 拒（``faq.md:162`` 给出的补救即「**重连或换
    sid**」）。CLI 保留「一条命令换名」的体感，代价是这条命令实际做四步：

    1. 离开旧房——**尽力而为**：``server:leave_office`` 的唯一作用是让旧房对端收到
       ``notify:leave_office``；随后 ``disconnect()`` 无论如何都会清掉服务端成员关系，故其失败降级为
       告警而非中止（中止只会把用户卡在「服务器抖一下就改不了名」）。
    2. 断开当前连接；
    3. 以新名重连（新 sid ⇒ 新会话，服务端不再有既有身份可冲突）；
    4. 入房。

    两个**相位**分开收敛（不可合并）：

    - 第 3 步失败 ⇒ 新旧连接都没了。``comp.name`` 回滚为原名后抛异常，调用方把 ``smcp_client`` 置
      ``None``——此时 CLI「未连接」是**实情**。
    - 第 4 步失败 ⇒ 连接**已建成**，故**保留**它并返回 ``joined=False``（不抛）。丢弃它会让 CLI
      自锁为「请先连接」而那条连接其实活着（连接泄漏 + 服务端 sid 会话驻留 + 体感掉线）；且新会话
      尚未声明任何身份（服务端会回滚 name），用户直接换个名字/换个房重试即可，无需再重连。

    :returns: ``(新的 smcp_client, 是否已入房)``。仅在**第 3 步**失败时抛异常。
    """
    # 步号恒定 1-4（即便无旧房可退也占位）：跳号会让用户以为流程出错。
    # Fixed 1-4 numbering — a skipped step would read as a bug.
    console.print(f"[dim]  1/4 离开 {smcp_client.office_id if _client_in_office(smcp_client) else '（未在房间，跳过）'}[/dim]")
    if _client_in_office(smcp_client):
        try:
            await smcp_client.leave_office(smcp_client.office_id)
        except Exception as e:  # noqa: BLE001 - 通知性步骤：最终状态由 disconnect 决定，故降级为告警
            console.print(f"[yellow]  ⚠ 离开旧房失败（不影响改名）/ leave_office failed: {e}[/yellow]")

    old_name = comp.name
    console.print("[dim]  2/4 断开当前连接[/dim]")
    try:
        await smcp_client.disconnect()
    except Exception as e:  # noqa: BLE001 - 断开失败不应阻止后续重连尝试（旧连接无论如何都要弃用）
        console.print(f"[yellow]  ⚠ 断开失败（继续重连）/ disconnect failed: {e}[/yellow]")

    console.print(f"[dim]  3/4 以新名 {new_name} 重连[/dim]")
    comp.name = new_name
    try:
        # 构造与建连同处一个 `except`：构造器抛错（防御面）也必须回滚名字，否则「第 3 步失败 ⇒
        # 回滚」的不变量漏一个出口，``comp.name`` 会停在一个从未生效的新名上。
        new_client: Any = smcp_client_cls(computer=comp, namespace=connection["namespace"])
        await new_client.connect(
            connection["url"],
            auth=connection["auth"],
            headers=connection["headers"],
            namespaces=[connection["namespace"]],
        )
    except Exception:
        comp.name = old_name  # 改名未生效 ⇒ 名字回到连接时的值，与「未改名」状态一致
        raise

    console.print(f"[dim]  4/4 加入 {office_id}（{new_name}）[/dim]")
    try:
        await new_client.join_office(office_id)
    except Exception as e:
        # 连接已建成 ⇒ 保留它（见 docstring 的相位说明）。comp.name 保持 new_name：那条连接日后若
        # 经 #203 自动重放，用的就是 ``computer.name``，故名字须与「服务端可能落定的身份」一致。
        console.print(f"[red]加入房间失败 / Failed to join office: {e}[/red]")
        return new_client, False
    return new_client, True


async def interactive_loop(
    comp: Computer,
    *,
    session_factory: type[PromptSession],
    patch_stdout_ctx: PatchStdoutCtx,
    smcp_client_cls: type[Any],
    init_client: Any | None = None,
    init_connection: ConnectionArgs | None = None,
    completer: Completer | None = None,
    approve_all_mcp: bool = False,
    settings_flag_path: Path | None = None,
) -> None:
    """
    中文: 交互循环的可注入实现；从 main.py 传入 PromptSession 工厂、patch_stdout 上下文与 SMCP 客户端类。
    English: DI-friendly interactive loop; main.py passes PromptSession factory, patch_stdout ctx and SMCP client class.

    completer: 可选 Tab 补全器（逐次传入 ``prompt_async``，trust y/N 等子提示不带补全）/ optional Tab completer.
    approve_all_mcp / settings_flag_path: 全局 flag ``--approve-all-mcp`` / ``--settings <file>``，透传给启动期
    MCP 批准框（#69 Group B）。``settings_flag_path`` 旧名 ``mcp_flag_config`` 已更名——它是 **settings.json**，
    旧名主动误导（#154）；flag 层 **mcp.json**（``--mcp-config``）不走本参数，而是注入 :class:`Computer`。

    init_connection: ``--auto-connect`` 启动路径的连接参数（``main.py`` 已用它建好 ``init_client``）。
    ``socket join`` 的**改名**需要一条新连接（协议 events.md:610 —— 身份在 sid 生命周期内不可变），
    故参数须随客户端一并注入；交互式 ``socket connect`` 会自行记录。
    Connection parameters of the ``--auto-connect`` client, replayed when a rename needs a fresh connection.
    """
    session = session_factory()
    smcp_client = init_client
    # 改名重连所需的连接参数（``socket connect`` 记录 / 启动路径注入）。
    connection: ConnectionArgs | None = init_connection
    # ``attempted_name`` = **本连接已尝试声明过的名字**（``None`` = 本连接从未尝试入房）。把
    # 「首次入房（任意名可）」「同名换房（直接 join）」「改名（须新连接）」三态分开。
    #
    # 刻意在**发起 join 之前**置位，而不是成功之后：客户端在传输层失败时会**保留入房意图**，并在
    # #203 自动重连时用 ``computer.name`` 重放（``client.py`` 的 desired 重放路径）——「成功才置位」
    # 会让 CLI 的信念落后于服务端（服务端已落定该名字，CLI 却以为本连接还没有身份），此后任何改名
    # 都被 403，而普通路径失败不触发重连 ⇒ 只能重启进程。提前置位是**保守高估**：最坏多走一次无谓
    # 重连，比 403 死锁安全。它也与「普通路径改写 ``comp.name`` 之前必先经改名分支」自洽。
    # Set *before* the attempt, not on success — the client keeps the intent across a transport failure
    # and replays ``computer.name`` on auto-reconnect (#203), so "success-only" would let the CLI's
    # belief lag the server and wedge on 403 forever. Over-estimating costs one redundant reconnect.
    attempted_name: str | None = None

    async def _join_office(office_id: str, requested_name: str) -> bool:
        """``socket join`` 的三态分发：首次入房 / 同名换房 / 改名（换新连接）。

        :returns: 是否**已入房**。改名路径下连接可能已换成新连接但入房被拒 ⇒ 返回 ``False``
            （调用方不得打印成功），但 ``smcp_client`` 已是那条可用连接；仅在**建连失败**时抛异常。
        """
        nonlocal smcp_client, attempted_name
        if smcp_client is None:
            # 调用点已有「未连接」守卫；此处是闭包自身的前置条件（建连失败会把 client 置 None）
            raise RuntimeError("未连接 / Not connected")
        if attempted_name is not None and requested_name != attempted_name:
            if connection is None:
                # 无语录可重放 ⇒ 只能拒绝（绝不静默改 comp.name，那会让后续 join 全部 403）
                raise RuntimeError(
                    "改名需要重建连接，但本次会话未记录连接参数——请先 `socket connect <url>` 再改名"
                    " / Renaming needs a new connection but no connection parameters were recorded",
                )
            console.print("[dim]改名需新连接（协议 403 ⇒ 重连或换 sid）[/dim]")
            attempted_name = requested_name
            try:
                smcp_client, joined = await _rename_via_reconnect(
                    smcp_client=smcp_client,
                    smcp_client_cls=smcp_client_cls,
                    comp=comp,
                    connection=connection,
                    office_id=office_id,
                    new_name=requested_name,
                )
            except Exception:
                # 建连失败 ⇒ 新旧连接都没了：不留悬空引用，声明状态清零
                smcp_client = None
                attempted_name = None
                raise
            return joined

        comp.name = requested_name
        attempted_name = requested_name
        await smcp_client.join_office(office_id)
        return True

    console.print("[bold]进入交互模式，输入 help 查看命令 / Enter interactive mode, type 'help' for commands[/bold]")
    if not console.is_terminal and not console.no_color:
        console.print(
            "[yellow]检测到控制台可能不支持 ANSI 颜色。若在 PyCharm 中运行，请在 Run/Debug 配置中启用 'Emulate terminal in "
            "output console'；或者使用 --no-color 关闭彩色输出。[/yellow]",
        )
    # zero-state 引导 banner（§10.1）。刻意读裸 ``_skill_home``（可能为 None）而非公开 ``skill_home`` property：
    # 后者会 ensure_skill_home() 强制解析并创建目录，而 banner 仅需"未启动=None→物化计数按 0"语义，不应在
    # 此处产生副作用（尤其未经 boot_up 的单测路径）。/ read raw _skill_home to avoid forcing resolution.
    render_banner(comp, comp._skill_home, os.environ)

    # 启动期 MCP 批准框（§9.2，#69 Group B）：解析 .tfrobot/mcp.json 定义层 → 门控 → 挂载 ENABLED / 弹 PENDING。
    # 非 TTY（管道/CI）→ 传 session=None 走 skip+WARN / --approve-all-mcp 分支（避免 prompt_async 在无终端下 EOF）。
    try:
        approval_session = session if sys.stdin.isatty() else None
        await plugin_cmd.run_mcp_approval(comp, approval_session, approve_all=approve_all_mcp, settings_flag_path=settings_flag_path)
    except Exception as e:  # pragma: no cover - 批准框失败不阻断进入 REPL
        console.print(f"[yellow]⚠ MCP 批准门控初始化失败 / MCP approval init failed: {e}[/yellow]")

    # #117 治理重挂（设计 Y client 接线）：boot 已恢复 bundled SKILL；CLI 作为参考 client 经公共 API
    # reconcile_governance(hooks) 重挂 enabled bundled MCP server。时序刻意在批准框之后——mcp.json 的
    # 显式用户配置先挂先占名（同名 bundled 被 skip，用户配置胜）；bundled 免批准（§5.10）。
    try:
        await plugin_cmd.run_governance_remount(comp, settings_flag_path=settings_flag_path)
    except Exception as e:  # pragma: no cover - 重挂失败不阻断进入 REPL
        console.print(f"[yellow]⚠ 治理重挂失败 / governance remount failed: {e}[/yellow]")

    while True:
        try:
            with patch_stdout_ctx(raw=True):
                raw = (await session.prompt_async("a2c> ", completer=completer)).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[cyan]Bye[/cyan]")
            break

        if raw == "":
            continue

        low = raw.lower()
        if low in {"help", "?"} or low.startswith("help "):
            # 分组 help（§10.2）：help 列 namespace，help <ns> 列该组命令 / grouped help, migrated to help.py
            help_parts = raw.split()
            render_help(help_parts[1] if len(help_parts) > 1 else None)
            continue

        parts = raw.split()
        cmd = parts[0].lower()

        try:
            if cmd in {"quit", "exit"}:
                break

            elif cmd == "status":
                print_status(comp)

            elif cmd == "tools":
                tools = await comp.aget_available_tools()
                print_tools(tools)

            elif cmd == "mcp":
                servers: dict[str, dict] = {}
                for cfg in comp.mcp_servers:
                    servers[cfg.name] = json.loads(json.dumps(cfg.model_dump(mode="json")))
                inputs = [json.loads(json.dumps(i.model_dump(mode="json"))) for i in comp.inputs]
                print_mcp_config({"servers": servers, "inputs": inputs})

            elif cmd == "desktop":
                # 中文: 解析可选参数：size 与 window_uri（顺序不固定，数字视为 size，其它作为 URI）
                # English: Parse optional args: size and window_uri (order-agnostic; digits -> size, else -> URI)
                size: int | None = None
                window_uri: str | None = None
                for arg in parts[1:]:
                    if size is None and arg.isdigit():
                        try:
                            size = int(arg)
                        except Exception:
                            size = None
                    elif window_uri is None:
                        window_uri = arg

                try:
                    desktops = await comp.get_desktop(size=size, window_uri=window_uri)
                    # 直接以 JSON 输出，便于上层消费 / print as JSON for easy consumption
                    console.print_json(data=desktops)
                except Exception as e:
                    console.print(f"[red]获取桌面失败 / Failed to get desktop: {e}[/red]")

            elif cmd == "server" and len(parts) >= 2:
                sub = parts[1].lower()
                payload = raw.split(" ", 2)[2] if len(parts) >= 3 else ""
                if sub == "add":
                    if payload.startswith("@"):
                        data = json.loads(Path(payload[1:]).read_text(encoding="utf-8"))
                    else:
                        data = json.loads(payload)
                    validated: dict[str, Any] = TypeAdapter(SMCPServerConfigDict).validate_python(data)
                    try:
                        await comp.aadd_or_aupdate_server(validated, session=session)
                        console.print("[green]✅ 服务器配置已添加/更新并正在启动 / Server config added/updated and starting[/green]")
                        if smcp_client:
                            await smcp_client.emit_update_config()
                    except Exception as e:
                        console.print(f"[red]❌ 添加/更新服务器失败 / Failed to add/update server: {e}[/red]")
                elif sub in {"rm", "remove"}:
                    if len(parts) < 3:
                        console.print("[yellow]用法: server rm <name|bundle_id>[/yellow]")
                    else:
                        # #143：先解析再下传——历史直接把 token 当 bundle_id 交 aremove_server，name≠bundle_id
                        # 时落档⑤ no-op 却照打「已移除配置」= 静默假成功。
                        bundle_id = _resolve_or_report(comp, parts[2], settings_flag_path=settings_flag_path)
                        if bundle_id is not None:
                            await comp.aremove_server(bundle_id)
                            console.print("[green]已移除配置 / Removed[/green]")
                            if smcp_client:
                                await smcp_client.emit_update_config()
                else:
                    console.print("[yellow]未知的 server 子命令 / Unknown subcommand[/yellow]")

            elif cmd == "start" and len(parts) >= 2:
                target = parts[1]
                if not comp.mcp_manager:
                    console.print("[yellow]Manager 未初始化[/yellow]")
                else:
                    try:
                        if target == "all":
                            # `all` 是关键字而非 server 标识 → 先短路，不进解析。
                            await comp.mcp_manager.astart_all()
                            console.print("[green]✅ 所有服务器启动完成 / All servers started[/green]")
                        else:
                            bundle_id = _resolve_lifecycle_target(comp, target, verb="start", settings_flag_path=settings_flag_path)
                            if bundle_id is not None:
                                await comp.mcp_manager.astart_client(bundle_id)
                                console.print(f"[green]✅ 服务器 '{target}' 启动完成 / Server '{target}' started[/green]")
                    except Exception as e:
                        console.print(f"[red]❌ 启动服务器失败 / Failed to start server: {e}[/red]")

            elif cmd == "stop" and len(parts) >= 2:
                target = parts[1]
                if not comp.mcp_manager:
                    console.print("[yellow]Manager 未初始化[/yellow]")
                else:
                    try:
                        if target == "all":
                            # `all` 是关键字而非 server 标识 → 先短路，不进解析。
                            await comp.mcp_manager.astop_all()
                            console.print("[green]✅ 所有服务器停止完成 / All servers stopped[/green]")
                        else:
                            # #143：``_astop_client`` 用 ``pop(bundle_id, None)`` 静默吞 miss（与 rust 逐行同构，
                            # 刻意不动，见 R4）——假成功必须在此拦住：未命中/已声明未挂载不下传、不打印成功。
                            bundle_id = _resolve_lifecycle_target(comp, target, verb="stop", settings_flag_path=settings_flag_path)
                            if bundle_id is not None:
                                await comp.mcp_manager.astop_client(bundle_id)
                                console.print(f"[green]✅ 服务器 '{target}' 停止完成 / Server '{target}' stopped[/green]")
                    except Exception as e:
                        console.print(f"[red]❌ 停止服务器失败 / Failed to stop server: {e}[/red]")

            elif cmd == "inputs" and len(parts) >= 2:
                sub = parts[1].lower()
                if sub == "load":
                    if len(parts) < 3 or not parts[2].startswith("@"):
                        console.print("[yellow]用法: inputs load @file.json[/yellow]")
                    else:
                        data = json.loads(Path(parts[2][1:]).read_text(encoding="utf-8"))
                        raw_items = TypeAdapter(list[SMCPServerInputDict]).validate_python(data)
                        models: set[MCPServerInputModel] = {TypeAdapter(MCPServerInputModel).validate_python(item) for item in raw_items}
                        comp.update_inputs(models)
                        console.print("[green]Inputs 已更新 / Inputs updated[/green]")
                        if smcp_client:
                            await smcp_client.emit_update_config()
                elif sub == "add":
                    if len(parts) < 3:
                        console.print("[yellow]用法: inputs add <json|@file.json>[/yellow]")
                    else:
                        payload = raw.split(" ", 2)[2]
                        if payload.startswith("@"):  # 文件里可为单个或数组
                            data = json.loads(Path(payload[1:]).read_text(encoding="utf-8"))
                        else:
                            data = json.loads(payload)
                        if isinstance(data, list):
                            items = TypeAdapter(list[SMCPServerInputDict]).validate_python(data)
                            for item in items:
                                comp.add_or_update_input(TypeAdapter(MCPServerInputModel).validate_python(item))
                        else:
                            item = TypeAdapter(SMCPServerInputDict).validate_python(data)
                            comp.add_or_update_input(TypeAdapter(MCPServerInputModel).validate_python(item))
                        console.print("[green]Input(s) 已添加/更新 / Added/Updated[/green]")
                        if smcp_client:
                            await smcp_client.emit_update_config()
                elif sub in {"update"}:
                    if len(parts) < 3:
                        console.print("[yellow]用法: inputs update <json|@file.json>[/yellow]")
                    else:
                        payload = raw.split(" ", 2)[2]
                        if payload.startswith("@"):  # 文件里可为单个或数组
                            data = json.loads(Path(payload[1:]).read_text(encoding="utf-8"))
                        else:
                            data = json.loads(payload)
                        if isinstance(data, list):
                            items = TypeAdapter(list[SMCPServerInputDict]).validate_python(data)
                            for item in items:
                                comp.add_or_update_input(TypeAdapter(SMCPServerInputDict).validate_python(item))
                        else:
                            item = TypeAdapter(SMCPServerInputDict).validate_python(data)
                            comp.add_or_update_input(item)
                        console.print("[green]Input(s) 已添加/更新 / Added/Updated[/green]")
                        if smcp_client:
                            await smcp_client.emit_update_config()
                elif sub in {"rm", "remove"}:
                    if len(parts) < 3:
                        console.print("[yellow]用法: inputs rm <id>[/yellow]")
                    else:
                        ok = comp.remove_input(parts[2])
                        if ok:
                            console.print("[green]已移除 / Removed[/green]")
                            if smcp_client:
                                await smcp_client.emit_update_config()
                        else:
                            console.print("[yellow]不存在的 id / Not found[/yellow]")
                elif sub == "get":
                    if len(parts) < 3:
                        console.print("[yellow]用法: inputs get <id>[/yellow]")
                    else:
                        input_get_items = comp.get_input(parts[2])
                        if input_get_items is None:
                            console.print("[yellow]不存在的 id / Not found[/yellow]")
                        else:
                            console.print_json(data=input_get_items.model_dump(mode="json"))
                elif sub == "list":
                    input_items = [i.model_dump(mode="json") for i in comp.inputs]
                    console.print_json(data=input_items)
                elif sub == "value":
                    if len(parts) < 3:
                        console.print("[yellow]用法: inputs value <list|get|set|rm|clear> ...[/yellow]")
                    else:
                        vsub = parts[2].lower()
                        if vsub == "list":
                            values = comp.list_input_values()
                            console.print_json(data=values or {})
                        elif vsub == "get":
                            if len(parts) < 4:
                                console.print("[yellow]用法: inputs value get <id>[/yellow]")
                            else:
                                val = comp.get_input_value(parts[3])
                                if val is None:
                                    console.print("[yellow]未找到或尚未解析 / Not found or not resolved yet[/yellow]")
                                else:
                                    try:
                                        console.print_json(data=val)
                                    except Exception:
                                        console.print(repr(val))
                        elif vsub == "set":
                            if len(parts) < 4:
                                console.print("[yellow]用法: inputs value set <id> [<json|text>][/yellow]")
                                console.print("[dim]提示: 如果不提供值，将使用 default 值 / Hint: omit value to use default[/dim]")
                            else:
                                target_id = parts[3]
                                # 中文: 如果只有 4 个部分（inputs value set <id>），则尝试使用 default 值
                                # English: If only 4 parts (inputs value set <id>), try to use default value
                                if len(parts) == 4:
                                    # 获取 input 定义 / Get input definition
                                    input_def = comp.get_input(target_id)
                                    if input_def is None:
                                        console.print("[yellow]不存在的 id / Not found[/yellow]")
                                        continue
                                    from a2c_smcp.computer.mcp_clients.model import MCPServerCommandInput

                                    if isinstance(input_def, MCPServerCommandInput):
                                        console.print(
                                            f"[yellow]Input '{target_id}' 是 command 类型，不支持 default 值 / is command type, "
                                            f"no default support[/yellow]"
                                        )
                                        console.print("[dim]用法: inputs value set <id> <json|text>[/dim]")
                                        continue
                                    if input_def.default is None:
                                        console.print(f"[yellow]Input '{target_id}' 没有 default 值 / has no default value[/yellow]")
                                        console.print("[dim]用法: inputs value set <id> <json|text>[/dim]")
                                        continue
                                    data = input_def.default
                                    console.print(f"[dim]使用 default 值 / Using default value: {repr(data)}[/dim]")
                                else:
                                    payload = raw.split(" ", 4)[4]
                                    try:
                                        data = json.loads(payload)
                                    except Exception:
                                        data = payload
                                ok = comp.set_input_value(target_id, data)
                                if ok:
                                    console.print("[green]已设置 / Set[/green]")
                                else:
                                    console.print("[yellow]不存在的 id / Not found[/yellow]")
                        elif vsub in {"rm", "remove"}:
                            if len(parts) < 4:
                                console.print("[yellow]用法: inputs value rm <id>[/yellow]")
                            else:
                                ok = comp.remove_input_value(parts[3])
                                console.print("[green]已删除 / Removed[/green]" if ok else "[yellow]无此缓存 / No such cache[/yellow]")
                        elif vsub == "clear":
                            target_id = parts[3] if len(parts) >= 4 else None
                            comp.clear_input_values(target_id)
                            console.print("[green]缓存已清理 / Cache cleared[/green]")
                        else:
                            console.print("[yellow]未知的 inputs value 子命令 / Unknown subcommand[/yellow]")
                else:
                    console.print("[yellow]未知的 inputs 子命令 / Unknown subcommand[/yellow]")

            elif cmd == "socket" and len(parts) >= 2:
                sub = parts[1].lower()
                if sub == "connect":
                    if smcp_client and getattr(smcp_client, "connected", False):
                        console.print("[yellow]已连接 / Already connected[/yellow]")
                    else:
                        url_val: str | None = parts[2] if len(parts) >= 3 else None
                        if not url_val:
                            with patch_stdout_ctx(raw=True):
                                url_val = (await session.prompt_async("URL: ")).strip()
                        if not url_val:
                            console.print("[yellow]URL 不能为空 / URL required[/yellow]")
                            continue

                        if len(parts) < 3:
                            with patch_stdout_ctx(raw=True):
                                auth_str = (await session.prompt_async("Auth (key:value, 可留空): ")).strip()
                            with patch_stdout_ctx(raw=True):
                                headers_str = (await session.prompt_async("Headers (key:value, 可留空): ")).strip()
                        else:
                            auth_str = ""
                            headers_str = ""

                        try:
                            auth = parse_kv_pairs(auth_str)
                            headers = parse_kv_pairs(headers_str)
                        except Exception as e:
                            console.print(f"[red]参数解析失败 / Parse error: {e}[/red]")
                            continue

                        smcp_client = smcp_client_cls(computer=comp)
                        await smcp_client.connect(url_val, auth=auth, headers=headers)
                        # 记录连接参数供改名重连重放；新连接 ⇒ 身份声明状态清零（新 sid = 新会话）。
                        # namespace 记**生效值**（本分支构造客户端时未指定 ⇒ 即默认 SMCP_NAMESPACE）；
                        # 记成 None 会在重连时显式覆盖掉默认值，把新连接变成哑连接（见 ConnectionArgs）。
                        connection = {
                            "url": url_val,
                            "namespace": SMCP_NAMESPACE,
                            "auth": auth,
                            "headers": headers,
                        }
                        attempted_name = None
                        console.print("[green]已连接 / Connected[/green]")
                elif sub == "join":
                    if not smcp_client or not getattr(smcp_client, "connected", False):
                        console.print("[yellow]请先连接 / Connect first[/yellow]")
                    elif len(parts) < 4:
                        console.print("[yellow]用法: socket join <office_id> <computer_name>[/yellow]")
                    else:
                        # 指定 computer_name 即声明本连接的运行时名字；若与本连接已尝试过的名字不同，
                        # 服务端会以 403 拒（协议 events.md:610）⇒ 改走「换新连接」（见 _join_office）。
                        # 成功提示只在该事件**真的入房**后打印：改名路径可能已换成新连接但入房被拒
                        # （此时 _join_office 返回 False 并已打印拒因），打印「已加入」会谎报。
                        try:
                            joined = await _join_office(parts[2], parts[3])
                        except RuntimeError as e:
                            console.print(f"[red]{e}[/red]")
                        except Exception as e:
                            console.print(f"[red]加入房间失败 / Failed to join office: {e}[/red]")
                        else:
                            if joined:
                                console.print("[green]已加入房间 / Joined office[/green]")
                elif sub == "leave":
                    if not smcp_client or not getattr(smcp_client, "connected", False):
                        console.print("[yellow]未连接 / Not connected[/yellow]")
                    elif not _client_in_office(smcp_client):
                        # 判据与改名路径的「1/4」统一为 `_client_in_office`（#203 重连窗口内 desired
                        # 被刻意保留，此时并不真在房里；沿用 desired 会对未在册的 namespace 发
                        # ``leave_office`` ⇒ 抛 BadNamespaceError ⇒ 只打印笼统「执行失败」，且用户
                        # 以为退成了、重连后又被自动拉回旧房）。
                        console.print("[yellow]未加入房间 / Not in any office[/yellow]")
                    else:
                        await smcp_client.leave_office(smcp_client.office_id)
                        console.print("[green]已离开房间 / Left office[/green]")
                else:
                    console.print("[yellow]未知的 socket 子命令 / Unknown subcommand[/yellow]")

            elif cmd == "notify" and len(parts) >= 2:
                sub = parts[1].lower()
                if sub == "update":
                    if not smcp_client:
                        console.print("[yellow]未连接 Socket.IO，已跳过 / Not connected, skip[/yellow]")
                    else:
                        await smcp_client.emit_update_config()
                        console.print("[green]已触发配置更新通知 / Update notification emitted[/green]")
                else:
                    console.print("[yellow]未知的 notify 子命令 / Unknown subcommand[/yellow]")

            elif cmd == "render":
                payload = raw.split(" ", 1)[1] if len(parts) >= 2 else ""
                if payload.startswith("@"):
                    data = json.loads(Path(payload[1:]).read_text(encoding="utf-8"))
                else:
                    data = json.loads(payload)
                rendered = await comp._config_render.arender(
                    data,
                    lambda x: comp._input_resolver.aresolve_by_id(x, session=session),
                )
                console.print_json(data=rendered)

            elif cmd == "tc":
                # 中文: 工具调用调试命令，参数需为与 Socket.IO 请求一致的 JSON（参见 a2c_smcp/smcp.py 的 ToolCallReq）
                # English: Tool call debug command. Argument must be a JSON matching Socket.IO request (see ToolCallReq in a2c_smcp/smcp.py)
                if len(parts) < 2:
                    console.print("[yellow]用法: tc <json|@file.json>[/yellow]")
                    continue
                payload = raw.split(" ", 1)[1]
                try:
                    if payload.startswith("@"):
                        data = json.loads(Path(payload[1:]).read_text(encoding="utf-8"))
                    else:
                        data = json.loads(payload)

                    # 中文: 使用 TypedDict 校验与规范化请求结构
                    # English: Validate and normalize request using TypedDict
                    req = TypeAdapter(SMCPToolCallReq).validate_python(data)

                    # 前置检查：需要已有活跃的 MCP 管理器
                    # Pre-check: require active MCP manager
                    if not comp.mcp_manager:
                        console.print(
                            "[yellow]MCP 管理器未初始化。请先添加并启动服务器 (server add/start) / MCP manager not initialized."
                            " Add and start a server first.[/yellow]",
                        )
                        continue

                    # 从请求中提取字段并调用
                    # Extract fields and execute
                    req_id: str = req["req_id"]
                    tool_name: str = req["tool_name"]
                    parameters: dict = req.get("params", {}) or {}
                    # ToolCallReq.timeout 定义为 int（秒）。转为 float 传入底层以兼容。
                    # ToolCallReq.timeout defined as int (seconds). Convert to float.
                    timeout_val = req.get("timeout")
                    timeout: float | None = float(timeout_val) if isinstance(timeout_val, (int, float)) else None

                    result = await comp.aexecute_tool(req_id, tool_name, parameters, timeout)

                    # 结果输出：优先以 JSON 打印
                    # Output result: prefer JSON
                    try:
                        if hasattr(result, "model_dump"):
                            console.print_json(data=result.model_dump(mode="json"))
                        else:
                            # 尝试通用序列化
                            console.print_json(data=json.loads(json.dumps(result, default=lambda o: getattr(o, "__dict__", str(o)))))
                    except Exception:
                        console.print(repr(result))
                except Exception as e:
                    console.print(f"[red]❌ 工具调用失败 / Tool call failed: {e}[/red]")

            elif cmd == "history":
                # 中文: 显示最近的调用历史。可选参数 n 指定返回条数，默认显示全部（最多10条）。
                # English: Show recent call history. Optional n limits number of returned entries (default all, up to 10).
                try:
                    n: int | None = None
                    if len(parts) >= 2:
                        try:
                            n = int(parts[1])
                        except Exception:
                            n = None
                    history = await comp.aget_tool_call_history()
                    tc_items = list(history)[-n:] if n is not None and n > 0 else list(history)
                    console.print_json(data=tc_items)
                except Exception as e:  # pragma: no cover
                    console.print(f"[red]❌ 读取历史失败 / Failed to read history: {e}[/red]")

            elif cmd == "marketplace" and len(parts) >= 2:
                # v0.2.1 SKILL marketplace 管理（S15，#68）；委托 commands/marketplace.py，变更后触发去抖 emit。
                await mp_cmd.repl_dispatch(comp, parts, session=session)

            elif cmd == "skill" and len(parts) >= 2:
                # v0.2.1 跨源 SKILL 只读查询（list / info，§4.4）；直读 Computer 活跃 registry。
                skill_cmd.repl_dispatch(comp, parts)

            elif cmd == "plugin" and len(parts) >= 2:
                # v0.2.1 plugin 管理（install/uninstall/enable/disable/list/info/gc，§4.3，S16 #69）；变更后触发去抖 emit。
                await plugin_cmd.repl_dispatch(comp, parts, session=session)

            elif cmd == "settings" and len(parts) >= 2:
                # v0.2.1 settings 意图层增删改查（show/get/set/edit，§4.5，S16 #69）。
                await settings_cmd.repl_dispatch(comp, parts, session=session)

            else:
                console.print("[yellow]未知命令 / Unknown command[/yellow]")
        except Exception as e:  # pragma: no cover
            console.print(f"[red]执行失败 / Failed: {e}[/red]")
