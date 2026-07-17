# -*- coding: utf-8 -*-
"""
文件名: resolve.py
作者: JQQ
创建日期: 2026/7/17
最后修改日期: 2026/7/17
版权: 2023 JQQ. All rights reserved.
依赖: 无第三方
描述:
  中文: 人机面（CLI / REPL）的 MCP server 寻址解析——**全仓唯一** name→bundle_id 解析处（#143 / R4）。
  English: The human-facing (CLI/REPL) MCP-server target resolver — the one and only name→bundle_id site.

分层契约（协议 ``computer-management/sdk-api-guidance.md §5.1``，Discussion #23 终审 R4）:

- **库层公开 API**（``aremove_server`` / ``astart_client`` / ``astop_client`` / ``aunmount_server_by_id`` …）
  **一律收 bundle_id，无 name 启发式**——「入参即身份」。name→id 启发式若放库层，每个外部集成方都会各继承一次
  不可靠推断：name 空间与 id 空间在缺省派生下（``bundle_id = normalize_name(name)``）大面积重叠，重叠处
  「先按 name 再回退按 id」不可靠（``A(name=foo, id=foo_1)`` 与 ``B(name=bar, id=foo)`` 共存时，按 name 先命中
  A ⇒ **永远拿不到 B**）。故启发式只应存在于**可交互报错**的人机面。
- **人机面**：本模块。未命中 / 多命中 **MUST 报错，MUST NOT 静默成功**——假成功回执（打印「已停止」而 server
  仍在跑）用户无从察觉，正是 #143 要根治的 P0。

该用户可见语义 **MUST 双端（python / rust）逐字一致**（rust 镜像 rust-sdk#141）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from a2c_smcp.computer.settings.recovery import collect_enabled_bundled_servers
from a2c_smcp.types import BUNDLE_ID
from a2c_smcp.utils.bundle_id import is_valid_bundle_id, resolve_bundle_id
from a2c_smcp.utils.logger import get_logger

if TYPE_CHECKING:  # pragma: no cover - 仅类型，避免与 computer.py 循环导入
    from a2c_smcp.computer.computer import Computer

logger = get_logger(__name__)

# 纯运行期投影（无声明、非 ledger bundled）的归属显示值：ad-hoc ``amount_server`` 挂进来的东西。
_RUNTIME_ATTRIBUTION = "runtime"


@dataclass(frozen=True)
class ServerCandidate:
    """一个可寻址的 MCP server 候选（寻址三元组）/ An addressable MCP server candidate。

    ``attribution`` 是**给人看的归属**：声明面 origin（``user`` / ``project`` / ``local`` / ``embed`` /
    ``flag`` / ``policy``）、``plugin:<plugin>``、或 ``runtime``（纯运行期投影）。协议 §5.1-3 要求多命中报错时
    **同时**打印 bundle_id + display name + 归属——只列 bundle_id 用户分不清哪个是自己的。
    """

    bundle_id: BUNDLE_ID
    name: str
    attribution: str


class TargetNotFoundError(Exception):
    """token 既非已知 display name，也非已注册的合法 bundle_id（协议 §5.1-2 后半 / §5.1-5）。"""

    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(f"No MCP server matches {token!r}")


class AmbiguousTargetError(Exception):
    """token 作为 display name 命中多条（同名合法共存，协议 §5.6）——列候选要求用户改用 bundle_id 重试。

    **MUST NOT** 以字典序最小等任意规则「确定性地选一个」（§5.1-4）：那是把不确定的错变成确定的错。
    """

    def __init__(self, token: str, candidates: tuple[ServerCandidate, ...]) -> None:
        self.token = token
        self.candidates = candidates
        super().__init__(f"{len(candidates)} MCP servers are named {token!r}")


def collect_candidates(comp: Computer, *, settings_flag_path: Path | None = None) -> tuple[ServerCandidate, ...]:
    """汇集当前一切可寻址的 server 候选，**按 bundle_id 为键**合并三源 / Collect addressable candidates。

    查找空间 = **运行期活跃集 ∪ 声明面**（#143 决策 1）。取并集而非单取运行期，是为了让
    :meth:`~a2c_smcp.computer.computer.Computer.aremove_server` 的档 1-4 全部可达——它本身就是双空间语义
    （声明优先、无声明则看运行期投影）。若只取运行期，「手改 mcp.json 但未重载」的声明会被本解析器判为
    「未找到」，而 ``aremove_server`` 本可删掉它 ⇒ 回归。

    归属推导（三源，优先序自上而下）:

    1. **声明面** :meth:`~a2c_smcp.computer.computer.Computer.resolve_mcp_declarations` —— 自带 ``origin``，
       恒 ∈ ``{user, project, local, embed, flag, policy}``（结构性非-plugin）。
    2. **ledger** :func:`~a2c_smcp.computer.settings.recovery.collect_enabled_bundled_servers` —— 恰好补上
       声明面的结构性缺口：``origin == plugin`` **不进 resolve**，plugin bundled server 走 transient
       ``amount_server`` 挂载。命中 ⇒ ``plugin:<plugin>``。
    3. **运行期** ``mcp_manager.server_configs()`` —— 兜底补前两者都没有的纯 transient 投影（ad-hoc
       ``amount_server``），归属 ``runtime``。

    .. note::
       **DRY 接缝声明**：:meth:`Computer.ainventory` 做同类 join，但其 join key 是 **display name**（已知缺陷，
       同名会退化误标 plugin，迁 bundle_id 挂 **#144**）。本函数从一开始就 **bundle_id join**，不复制该缺陷；
       两处收敛属 #144 范围，不在 #143 内。

    :param settings_flag_path: 全局 ``--settings <file>`` flag 层路径（flag-aware 账本视图）。
    """
    from a2c_smcp.computer.cli.commands import resolved_settings

    by_id: dict[BUNDLE_ID, ServerCandidate] = {}

    # 1. 声明面（携 origin）——最权威的「谁被声明了、以何 origin」。
    declarations = comp.resolve_mcp_declarations(env=os.environ)
    for name, srv in declarations.servers.items():
        bundle_id = resolve_bundle_id(srv.config)
        by_id[bundle_id] = ServerCandidate(bundle_id=bundle_id, name=name, attribution=srv.origin.value)

    # 2. ledger 派生的 enabled plugin bundled server（补声明面的 origin==plugin 结构性缺席；已按 bundle_id 去重）。
    try:
        declared = resolved_settings(os.environ, flag_path=settings_flag_path)
        for record in collect_enabled_bundled_servers(comp.skill_home, declared):
            bundle_id = resolve_bundle_id(record.config)
            if bundle_id not in by_id:  # 用户自己的声明胜出（§2.5 用户主权）——显示它能操作的那条真相。
                by_id[bundle_id] = ServerCandidate(
                    bundle_id=bundle_id, name=record.config.name, attribution=f"plugin:{record.plugin}",
                )
    except Exception as e:  # noqa: BLE001 - 读层容错去连坐（#155）：账本不可读只丢 plugin 归属标注，不该让
        # 寻址整体瘫痪（用户的 user-scope server 与 plugin 账本无关，不连坐）。降级**必须留痕**——静默降级
        # 会让「归属显示 runtime 而非 plugin:x」变成查无实据的怪事。
        logger.warning("resolve: plugin 归属推导降级（账本不可读）/ ownership derivation degraded: %s", e)

    # 3. 运行期活跃集兜底：补纯 transient 投影（无声明、非 bundled）。
    if comp.mcp_manager is not None:
        for cfg in comp.mcp_manager.server_configs():
            bundle_id = resolve_bundle_id(cfg)
            if bundle_id not in by_id:
                by_id[bundle_id] = ServerCandidate(bundle_id=bundle_id, name=cfg.name, attribution=_RUNTIME_ATTRIBUTION)

    return tuple(by_id.values())


def resolve_target(token: str, candidates: tuple[ServerCandidate, ...]) -> BUNDLE_ID:
    """把用户敲的 token 解析为 **bundle_id**，严格按协议 §5.1 / Resolve a user token to a bundle_id。

    步骤序（**顺序有意义**，勿重排）:

    1. token 按 **display name** 反查，**唯一命中** → 其 bundle_id；
    2. **0 命中** ∧ token 是**合法且已注册**的 bundle_id → token 本身（语法合法 ≠ 存在：未注册须报「未找到」，
       否则 ``stop <合法但不存在的 id>`` 会一路走到底层的静默 no-op ⇒ 假成功复活）；
    3. **多命中** → :class:`AmbiguousTargetError`（列候选，要求改用 bundle_id 重试）；
    4. 其余 → :class:`TargetNotFoundError`。

    .. warning::
       **步骤 4（协议）：MUST NOT 以字典序最小等任意规则「确定性地选一个」**——那是把不确定的错变成确定的错。

    .. note::
       **已知缺口（协议 §5.1 步骤序，#143 决策 4，待协议裁决后双端同步修）**：步骤 2 的门是**「0 命中」**，
       故多命中时够不到步骤 2。⇒ ``A(name='foo', 缺省派生 id='foo')`` 与 ``B(name='foo', 显式 id='bundle_x')``
       合法共存（§5.6）时，A 的 bundle_id 恰等于那个冲突的名字，用户照「请用 bundle_id 重试」再敲 ``foo``
       仍是 name 多命中 ⇒ **A 永远不可寻址**。修它属改协议明文，且该语义 MUST 双端逐字一致 ⇒ 不单端发明。
       缺口由 ``tests/unit_tests/computer/cli/test_resolve.py`` 的 xfail 用例钉住。

    :raises AmbiguousTargetError: token 作为 display name 命中多条。
    :raises TargetNotFoundError: token 既非已知 name 也非已注册的合法 bundle_id。
    """
    name_hits = tuple(c for c in candidates if c.name == token)
    if len(name_hits) == 1:
        return name_hits[0].bundle_id
    if len(name_hits) > 1:
        raise AmbiguousTargetError(token, name_hits)
    if is_valid_bundle_id(token):
        for cand in candidates:
            if cand.bundle_id == token:
                return token
    raise TargetNotFoundError(token)
