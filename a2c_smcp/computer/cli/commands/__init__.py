# -*- coding: utf-8 -*-
# filename: __init__.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
CLI 命令核心（REPL 与 Typer 非交互共用）/ CLI command core shared by the REPL and non-interactive Typer surfaces。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.2 / §4.4 / §12（S15，#68）。

本包把 marketplace / skill 命令的业务逻辑（包裹 staging / reconciler / store / installer / registry 既有后端）
抽成纯函数式 handler，签名取**显式资源**（``registry`` / ``home`` / ``env`` + flags）而非整个 Computer，便于
隔离单测；REPL 经各模块的 ``repl_dispatch`` 适配器把 ``Computer`` 的活跃 registry / home / session 绑定进去，
Typer 子命令则构造轻量上下文（不 boot Computer、不连 socket）。

本模块只放 **跨命令共享的接缝**：:func:`build_mcp_callbacks`——从 ``Computer`` 装配 installer / 卸载级联所需的
MCP 注入回调（``existing_bundle_ids`` / ``register_server`` / ``remove_server``，身份一律 **bundle_id**，
数据源为**运行期权威配置集**，#153）。plugin / settings 命令（#69）将复用本接缝。
This package holds the command business logic as pure-ish handlers (explicit resources, not the whole Computer)
so they unit-test in isolation. Only the cross-command seam lives here: :func:`build_mcp_callbacks`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from a2c_smcp.utils.bundle_id import resolve_bundle_id

if TYPE_CHECKING:  # 仅类型，避免运行时循环导入 / type-only to dodge runtime import cycle
    from a2c_smcp.computer.computer import Computer
    from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
    from a2c_smcp.computer.settings.installer import ExistingBundleIds, NonPluginBundleIds, RegisterServer, RemoveServer
    from a2c_smcp.computer.settings.schema import SettingsValidationError
    from a2c_smcp.computer.settings.scope import ResolvedSettings, SettingsAxes


@dataclass(frozen=True, slots=True)
class McpCallbacks:
    """installer / 卸载级联所需的 MCP 注入回调 / The MCP injection callbacks installer/cascade needs。"""

    existing_bundle_ids: ExistingBundleIds
    register_server: RegisterServer
    remove_server: RemoveServer
    non_plugin_bundle_ids: NonPluginBundleIds


def build_mcp_callbacks(comp: Computer) -> McpCallbacks:
    """
    从 ``Computer`` 装配 installer / uninstall 级联所需的 MCP 注入回调 / Wire MCP callbacks from a live Computer。

    - ``existing_bundle_ids``：当前**运行期活跃** server 的 bundle_id 集（依赖预检用）；
    - ``register_server``：**运行期挂载**一个 ``MCPServerConfig``（enable / 治理重挂用）；
    - ``remove_server``：按 **bundle_id** 运行期停摘 server（disable / uninstall 按 §4.9.1-2 回收；enable
      写后失败对**本次 attempt 全部** server 调用，含 register 失败者）。实现只做运行期 unmount，不删 durable 声明；
    - ``non_plugin_bundle_ids``：**声明面**中 ``origin != plugin`` 的 bundle_id 集（§4.9.1-2 回收判据的
      「X 非用户声明」项，#164）——与 ``existing_bundle_ids`` **判然不同**，勿混用：前者是「谁被声明了（且非 plugin
      带入）」，后者是「谁挂起来了」。协议**明禁**用裸活跃集判回收（无 origin ⇒ flag/embed/plugin 三条挂载路径
      可观测同形 ⇒ 连坐停摘用户 / 宿主自有 server）。

    设计 §12.2：marketplace remove 的级联卸载（→ :func:`installer.uninstall_plugin`）与 #69 的 plugin
    enable/disable/install/uninstall 共用此接缝，避免各处重复装配。

    **#153 数据源 = 运行期权威配置集**：``existing`` 取 ``comp.mcp_manager.server_configs()``
    （:meth:`MCPServerManager.server_configs`），**不是** ``comp.mcp_servers``——后者是**构造期快照**，
    仅 ``Computer.__init__`` 赋值一次，而 CLI 恒传 ``mcp_servers=set()``、所有 server 走 ``amount_server`` /
    ``aadd_or_aupdate_server`` 挂载 ⇒ 快照恒空 ⇒ 依赖预检把「已满足」全判成「未满足」（协议 §2.5-4 明禁）。
    亦**不用** :meth:`Computer.active_server_configs`：它 fail-closed 省略缺 raw 记录的 server，用于预检会漏判。

    **#137 ③ transient 分流**：本接缝**唯一**消费方是 plugin enable/disable/install/uninstall 与 marketplace
    级联卸载——皆**治理投影**（依赖声明的真相在 ledger，非用户此刻声明），故走 transient
    :meth:`Computer.amount_server` / :meth:`Computer.aunmount_server_by_id`，**不回写** mcp.json（否则 disable
    后复活 + scope 漂移，见 #138）。用户显式 ``server add``/``rm`` 是另一条（REPL）durable 路径，与此无关。
    """

    def _existing() -> set[str]:
        if comp.mcp_manager is None:  # pre-boot：无活跃集 → 空（预检只提示，不影响正确性）
            return set()
        return {resolve_bundle_id(cfg) for cfg in comp.mcp_manager.server_configs()}

    async def _register(cfg: MCPServerConfig) -> None:
        await comp.amount_server(cfg)

    async def _remove(bundle_id: str) -> None:
        await comp.aunmount_server_by_id(bundle_id)

    def _non_plugin() -> set[str]:
        # 声明面（含 flag/embed 层，携 origin）；每次重算、零持久态（§2.5-5 禁落盘为快照）。
        return {resolve_bundle_id(srv.config) for srv in comp.resolve_mcp_declarations().servers.values()}

    return McpCallbacks(
        existing_bundle_ids=_existing,
        register_server=_register,
        remove_server=_remove,
        non_plugin_bundle_ids=_non_plugin,
    )


def files_only_non_plugin_bundle_ids(env: Mapping[str, str] | None = None) -> NonPluginBundleIds:
    """
    **无 Computer** 的非交互进程的非-plugin 声明面 / The non-plugin declaration surface for Computer-less processes。

    非交互 ``plugin uninstall|disable|gc`` / ``marketplace remove`` 是 **ledger-only**（无 MCP 回调、不挂载、
    ``remove_server=None`` ⇒ 回收判据结果实际未被消费，见 :func:`~a2c_smcp.computer.settings.installer.uninstall_plugin`）。
    该进程**既无宿主构造入参（embed）、也无 ``--mcp-config``（flag 仅存在于 ``run``）** ⇒ 声明面 = durable scopes，
    这不是「漏传两层」而是「那两层在此进程确实不存在」。REPL / boot 路径请用 :func:`build_mcp_callbacks`。
    """
    from a2c_smcp.computer.settings.mcp_config import non_plugin_declared_bundle_ids

    return lambda: non_plugin_declared_bundle_ids(env=env)


# ── 跨命令解析 / 视图接缝（marketplace / skill / plugin / settings 共用）/ shared parse & view seams ──
def settings_axes_of(comp: Any) -> SettingsAxes:
    """取 Computer（或同形替身）的 settings 原始轴——**单一来源**（#296）/ Raw settings axes of a Computer-like object.

    优先 ``_settings_axes()``（``Computer`` 提供，含 env/cwd/flag/policy，与治理恢复同源）；自定义
    ``--computer-factory`` 未实现该方法时退化为 getattr 组合（env / cwd / flag，policy 由消费方按 env 现解析）。
    REPL 全部分派器（marketplace / settings / plugin）统一经本函数取轴，避免同一 REPL 内 add 与
    list/info/remove/refresh 读写不同账本。
    """
    from a2c_smcp.computer.settings.scope import SettingsAxes

    axes_fn = getattr(comp, "_settings_axes", None)
    if callable(axes_fn):
        axes: SettingsAxes = axes_fn()
        return axes
    resolve_cwd = getattr(comp, "_resolve_cwd", None)
    return SettingsAxes(
        env=getattr(comp, "env", None),
        cwd=resolve_cwd() if callable(resolve_cwd) else None,
        flag_settings_path=getattr(comp, "_flag_settings_path", None),
    )


def make_settings_provider(
    env: Mapping[str, str] | None,
    *,
    flag_path: Path | None = None,
    cwd: Path | None = None,
    policy_settings: Mapping[str, Any] | None = None,
) -> Callable[[str | None], Mapping[str, Any]]:
    """
    #289：enable 失败诊断的合并 settings provider 工厂（单一实现，REPL / 一次性 CLI 共用）/ Shared diagnostic provider.

    ``enable_plugin`` 用它在写前与回滚写后各现算一次合并视图，判定「回滚前后是否均 enabled」并给出 retained
    诊断。参数 = **诊断锚点**（本次操作 scope 的 project/local 路径；``None`` = user scope 操作 → ``cwd``，
    REPL 传实例 cwd、一次性 CLI 传进程 cwd）。与写锚（settings 文件路径）分离；provider 只读，不驱动任何
    破坏性动作。``policy_settings=None`` = 按 env 现解析（:func:`resolved_settings` 既有语义）。
    """

    def _provider(anchor: str | None) -> Mapping[str, Any]:
        return resolved_settings(
            env,
            flag_path=flag_path,
            cwd=Path(anchor) if anchor else cwd,
            policy_settings=policy_settings,
        )

    return _provider


def resolved_settings_provider_of(comp: Any) -> Callable[[str | None], Mapping[str, Any]]:
    """
    #289：enable 失败诊断 provider（**实例轴**，flag/policy 感知）/ Instance-axis diagnostic provider.

    与 :func:`settings_axes_of` 同源（#296）：REPL 的 marketplace / settings / plugin 各面必须看到同一五层视图。
    层组合统一委托 :func:`make_settings_provider`（勿在此另写副本）。
    """
    axes = settings_axes_of(comp)
    return make_settings_provider(
        axes.env, flag_path=axes.flag_settings_path, cwd=axes.cwd, policy_settings=axes.policy_settings,
    )


def flag_value(args: list[str], flag: str) -> str | None:
    """取 ``--flag value`` 形态的值（**不支持** ``--flag=value``，REPL 简化）/ Extract a ``--flag value`` pair。

    四个命令模块（marketplace / skill / plugin / settings）的 REPL dispatcher 共用，避免 4 处分叉。
    """
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            return args[idx + 1]
    return None


def resolved_settings_with_errors(
    env: Mapping[str, str] | None,
    *,
    flag_path: Path | None = None,
    cwd: Path | None = None,
    policy_settings: Mapping[str, Any] | None = None,
) -> ResolvedSettings:
    """五层合并 settings **连同校验错误**（含 policy first-source-wins；#116 锚 cwd）/ Merged settings **with errors**。

    :func:`resolved_settings` 是本函数的薄包装（只取 ``.settings``）。需要向用户**呈现**越权/畸形字段的
    调用方（boot 批准流程、``settings show``）用本函数拿完整 :class:`ResolvedSettings`。
    Callers that must surface filtered/malformed fields use this; :func:`resolved_settings` wraps it.

    函数内 lazy import 沿用本仓 dodge-cycle 范式（settings.scope / settings.policy 不反向依赖 cli，无环，
    仅避免 ``import cli.commands`` 拉重）。
    """
    from a2c_smcp.computer.settings.policy import resolve_policy_settings
    from a2c_smcp.computer.settings.scope import resolve_settings

    return resolve_settings(
        env=env,
        cwd=cwd,
        flag_settings_path=flag_path,
        # #296：显式注入的 policy 视图（REPL 实例轴）优先；``None`` = 按 env 现解析（既有入口行为）。
        policy_settings=policy_settings if policy_settings is not None else resolve_policy_settings(env=env),
    )


def resolved_settings(
    env: Mapping[str, str] | None,
    *,
    flag_path: Path | None = None,
    cwd: Path | None = None,
    policy_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """五层合并 settings（含 policy first-source-wins；#116 project/local 锚定进程 cwd）/ Merged settings incl. policy。

    plugin（``enabledPlugins`` / gc 声明视图）与 settings（merged show / get）共用。policy 层承载企业
    allowed/deniedMcpServers（POLICY_ONLY 字段，批准门控须读到），故统一注入。

    **丢弃校验错误**——只在「不向用户呈现诊断」的声明视图用；要呈现的走
    :func:`resolved_settings_with_errors` + :func:`format_settings_errors`（#157）。
    Drops validation errors; use the ``_with_errors`` variant where diagnostics must surface.
    """
    return resolved_settings_with_errors(env, flag_path=flag_path, cwd=cwd, policy_settings=policy_settings).settings


def format_settings_errors(errors: Sequence[SettingsValidationError]) -> list[str]:
    """把 settings 校验错误格式化为人读警示行（**纯函数**，供 boot 批准流程与 ``settings show`` 共用）/ format。

    #157：scope 越权过滤（policy-only / 审批门 enable 方向判据）**静默丢弃字段**——若连错误也不呈现，用户
    只会看到「我的 settings 莫名不生效」（协议 §2.1 要求**响亮失败**，``SettingsValidationError`` 的契约亦
    自称「经 ``settings show`` / 诊断命令呈现」）。抽为纯函数以便**单测文案与 scope/field 拼装**，杜绝未来
    重构把「呈现」半程静默回退成吞错误——呈现行为在 ``run_mcp_approval`` 这类 ``Session``-泛型异步副作用
    函数里无法直接断言。对拍 rust ``cli/commands/mod.rs::format_settings_errors``。

    **#295：marketplace 声明错误在此统一脱敏**——``field`` 的市场名段是**原始 JSON 键**（校验器直接拼点路径），
    可含 userinfo 凭据 / ``[`` ``]`` 等富文本标记。与恢复报告（:func:`~a2c_smcp.computer.settings.recovery.
    marketplace_declaration_errors`）**同一脱敏口径**（段漏斗 + 散文脱敏 + Cf/Cc 清洗），覆盖本函数的全部
    消费面（``settings show`` / ``settings get`` / boot 批准流程 ``run_mcp_approval``）。非 marketplace 子树原样保留
    （本单不扩面，残余挂 #304）。``escape()`` 不在此层做：**每个 sink 自己在打印处转义**（``plugin.py`` 的
    ``run_mcp_approval`` 与 ``settings.py`` 的 ``_emit_errors`` 均已转义），避免双重转义——新增 sink 必须自带
    ``escape()``。
    Marketplace declaration errors are redacted here, mirroring rust's ``format_settings_errors``.
    """
    # 函数内 lazy import 沿用本模块既有范式（见 :func:`resolved_settings_with_errors`）：settings.recovery
    # 拉 mcp_clients.model / skills 等重依赖，不该让每个 import ``cli.commands`` 的命令模块都付这份成本。
    from a2c_smcp.computer.settings.recovery import sanitized_marketplace_error

    lines: list[str] = []
    for e in errors:
        sanitized = sanitized_marketplace_error(e)
        field, reason = (sanitized.field, sanitized.reason) if sanitized is not None else (e.field, e.reason)
        lines.append(f"⚠ settings.json[{e.scope.value}]: {field} — {reason}")
    return lines


def print_unknown_subcommand(kind: str, sub: str) -> None:
    """REPL 未知子命令提示（#295）：``sub`` 是用户**原样输入**，必须 ``escape``。

    未转义时 rich 抛 ``MarkupError``；REPL 顶层兜底（``interactive_impl`` 的 ``except Exception``）若也未转义
    就会二次抛出，令交互模式直接 traceback 退出。四个分派器（settings / plugin / skill / marketplace）共用本
    函数——新增分派器同样走它，避免再漏一处。
    """
    # 函数内惰性导入：``commands`` 是每个命令模块都会 import 的包 init，不应把 rich / console 拉进所有导入链。
    from rich.markup import escape

    from a2c_smcp.computer.utils import console as console_util

    console_util.console.print(f"[yellow]unknown {escape(kind)} subcommand: {escape(sub)}[/yellow]")
