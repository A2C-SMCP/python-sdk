# -*- coding: utf-8 -*-
# filename: recovery.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
治理启动恢复（#117，协议 v0.2.3 runtime-contract §4.8）/ Governance boot recovery。

**ledger 驱动、additive-only、离线优先、enabled 门控**（与 rust-sdk ``settings/recovery.rs`` 同构）：
从双账本（``installed_plugins.json`` + ``known_marketplaces.json``）重建 enabled plugin 的活跃集——
bundled SKILL 经 :func:`~a2c_smcp.computer.skills.staging.stage_marketplace_skills`（``refresh=False``
就地复用 clone 树）恢复进 Registry；bundled MCP server 经 :func:`collect_enabled_bundled_servers`
纯函数**可查询**（含 plugin/marketplace 归属，§4.8.3），进程物化归 client（#93 client owns MCP config，
§4.8 blockquote）——boot 默认不拉进程，重挂由 client 显式经
:meth:`~a2c_smcp.computer.computer.Computer.reconcile_governance` 传 hooks 触发（设计 Y 同构契约）。

为何不走声明式 :func:`~a2c_smcp.computer.settings.reconciler.reconcile`：命令式 ``install_plugin``
**不写** ``enabledPlugins``（装即活跃），声明式对账恢复不了它；恢复以账本为"已安装"事实源、以
``enabledPlugins`` 合并视图为启用门控（boot-active = installed ∧ ``enabledPlugins[pid] != False``，
仅显式 false 禁用）。失败降级铁律：单 plugin / 单 marketplace / 单文件失败 → WARN + 记入报告，
**不抛、不阻断 boot**（§3 degraded / §5.2）。

已知限制（与 rust 同构文档化）：boot 内 declared 视图不含 ``--settings`` flag scope（Computer 无 flag
知识；CLI 接线时可显式传 flag-aware ``declared``）——跨重启可靠 disable 请写 user scope。

Ledger-driven, additive-only, offline-first governance recovery mirroring rust-sdk. Recovery reads the
two ledgers as installed-facts, gates by the merged ``enabledPlugins`` view (only explicit ``false``
disables), restores bundled SKILLs in place, and exposes enabled bundled MCP servers as a pure queryable
function with ownership; process materialization stays with the client (#93).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.store import load_installed_plugins, load_known_marketplaces
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.manifest import PluginManifestError, load_bundled_servers
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import DEFAULT_GIT_TIMEOUT, stage_marketplace_skills
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GovernanceRecoveryReport:
    """治理恢复报告（与 rust ``GovernanceRecoveryReport`` 同名同义，作观测 + 测试信号）/ Recovery report。

    - ``restored_plugins``：clone 可达且已尝试 stage 的 plugin id（``<plugin>@<marketplace>``）；
      **非**"保证注册了 SKILL"（manifest 损坏时 stage 内部降级，权威清单看 ``restored_skills``）。
    - ``restored_skills``：本轮成功注册/刷新的 SKILL name 权威清单。
    - ``remounted_servers``：阶段二经 hooks 成功重挂的 server name（由编排方填充；本模块函数留空）。
    - ``failed_marketplaces``：源不可达 / clone 缺失且重建失败 / known 记录缺失（降级、不阻断）。
    - ``skipped_disabled``：``enabledPlugins[pid] = false`` 刻意跳过的 plugin id。
    """

    restored_plugins: list[str] = field(default_factory=list)
    restored_skills: list[str] = field(default_factory=list)
    remounted_servers: list[str] = field(default_factory=list)
    failed_marketplaces: list[str] = field(default_factory=list)
    skipped_disabled: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BundledServerRecord:
    """enabled bundled MCP server 的可查询记录（归属 = boot 纯函数输出，§4.8.3）/ Queryable bundled-server record。

    ``config`` 从账本 ``installPath`` 经 :func:`load_bundled_servers` **每次 boot 重新解析**（不信任
    存储态，§5.8 精神）；``plugin``/``marketplace`` 供 client 挂载时携带 D2 渲染上下文
    （``Computer.aadd_or_aupdate_server(cfg, plugin=, marketplace=)``）。
    """

    plugin_id: str
    plugin: str
    marketplace: str
    install_path: Path
    config: MCPServerConfig


# 阶段二重挂回调：携归属记录（供 client 带 plugin/marketplace 上下文注册，D2 ${input:} 前缀回退解析）。
# Phase-2 remount callback carrying ownership context (unlike installer's context-free RegisterServer).
RegisterBundledServer = Callable[[MCPServerConfig, BundledServerRecord], Awaitable[None]]


def _plugin_boot_active(declared: Mapping[str, Any], pid: str) -> bool:
    """boot-active 门控：仅显式 ``enabledPlugins[pid] = false`` 视为禁用（缺省/true 皆启用，与 rust 逐字一致）。"""
    enabled = declared.get("enabledPlugins")
    if isinstance(enabled, Mapping):
        return enabled.get(pid) is not False
    return True


def _split_pid(pid: str) -> tuple[str, str] | None:
    """``<plugin>@<marketplace>`` → (plugin, marketplace)；无 ``@``（本地-only 形态）→ None 跳过。"""
    plugin, sep, marketplace = pid.partition("@")
    if not sep or not plugin or not marketplace:
        return None
    return plugin, marketplace


async def recover_marketplace_skills(
    registry: SkillRegistry,
    home: Path,
    declared: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> GovernanceRecoveryReport:
    """
    阶段一：从双账本恢复 enabled installed plugin 的 bundled SKILL（幂等、additive-only）/ Phase 1: restore skills。

    按 marketplace 分组 → :func:`stage_marketplace_skills`（``refresh=False`` 离线优先：clone 在则就地复用重扫，
    缺失才尝试 clone）。降级判定与 rust 同构：stage 内部吞错，事后 clone 树仍缺 → 该 marketplace 入
    ``failed_marketplaces``；known_marketplaces 缺记录 → 同样降级。**绝不抛、不阻断其余**。

    :param registry: 目标 :class:`SkillRegistry`（恢复注册进当前活跃集）。
    :param home: SKILL Home 绝对根。
    :param declared: ``enabledPlugins`` 合并声明视图（权威启用门控）。
    :param env: 环境映射（账本路径解析 + git 子进程），默认 ``os.environ``。
    :param timeout: 单次 git 操作超时（仅 clone 缺失重建时可能触发）。
    """
    report = GovernanceRecoveryReport()
    installed = load_installed_plugins(home=home, env=env).get("plugins", {})
    if not installed:
        return report
    known = load_known_marketplaces(home=home, env=env).get("marketplaces", {})

    # enabled installed plugin 按 marketplace 分组 / group boot-active plugins by marketplace.
    by_marketplace: dict[str, set[str]] = {}
    for pid in installed:
        split = _split_pid(pid)
        if split is None:
            logger.debug("governance recovery: plugin id %r has no '@marketplace' segment, skipped (local-only form)", pid)
            continue
        if not _plugin_boot_active(declared, pid):
            report.skipped_disabled.append(pid)
            continue
        plugin, marketplace = split
        by_marketplace.setdefault(marketplace, set()).add(plugin)

    for marketplace, plugins in sorted(by_marketplace.items()):
        record = known.get(marketplace)
        source = record.get("source") if isinstance(record, Mapping) else None
        if not isinstance(source, Mapping):
            logger.warning(
                "governance recovery: marketplace %r has no known_marketplaces record; its plugins %s degraded (not restored)",
                marketplace,
                sorted(plugins),
            )
            report.failed_marketplaces.append(marketplace)
            continue
        names = await stage_marketplace_skills(
            marketplace,
            source,
            registry,
            home,
            plugin_filter=set(plugins),
            refresh=False,  # 离线优先：clone 在则零 git / offline-first
            timeout=timeout,
            env=env,
        )
        # 降级代理判定（rust 同构）：stage 失败降级不抛；事后 clone 树仍缺 = 源不可达且无本地物化。
        if not marketplace_skill_dir(home, marketplace).is_dir():
            logger.warning("governance recovery: marketplace %r clone missing and rebuild failed, degraded", marketplace)
            report.failed_marketplaces.append(marketplace)
            continue
        report.restored_skills.extend(names)
        report.restored_plugins.extend(sorted(f"{plugin}@{marketplace}" for plugin in plugins))

    return report


def collect_enabled_bundled_servers(
    home: Path,
    declared: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> list[BundledServerRecord]:
    """
    阶段二输入（纯读、无锁、无副作用）：枚举 enabled installed plugin 的 bundled MCP server / Phase-2 pure collect。

    协议 §4.8 blockquote 的"SDK MUST 使 enabled bundled server 可查询"落点：client 据此物化进自己的
    MCP 配置模型，或经 :meth:`Computer.reconcile_governance` 传 hooks 显式重挂。跨 plugin/scope 同名
    server **首见保留去重**（账本插入序，与 rust 一致）；``installPath`` 缺失 / bundled JSON 损坏 →
    WARN 跳过该记录，不阻断其余、不抛。

    :param home: SKILL Home 绝对根。
    :param declared: ``enabledPlugins`` 合并声明视图。
    :param env: 环境映射（账本路径解析），默认 ``os.environ``。
    """
    out: list[BundledServerRecord] = []
    seen: set[str] = set()
    installed = load_installed_plugins(home=home, env=env).get("plugins", {})
    for pid, records in installed.items():
        split = _split_pid(pid)
        if split is None:
            continue
        if not _plugin_boot_active(declared, pid):
            continue
        plugin, marketplace = split
        for record in records:
            install_path = record.get("installPath") if isinstance(record, Mapping) else None
            if not install_path or not isinstance(install_path, str):
                logger.warning("governance recovery: plugin %r record has no installPath, bundled servers skipped", pid)
                continue
            root = Path(install_path)
            try:
                configs = load_bundled_servers(root)
            except PluginManifestError as e:
                logger.warning("governance recovery: plugin %r bundled servers unparsable at %s, skipped: %s", pid, root, e)
                continue
            for config in configs:
                if config.name in seen:
                    logger.debug("governance recovery: bundled server %r duplicated across plugins/scopes, first seen wins", config.name)
                    continue
                seen.add(config.name)
                out.append(
                    BundledServerRecord(plugin_id=pid, plugin=plugin, marketplace=marketplace, install_path=root, config=config),
                )
    return out
