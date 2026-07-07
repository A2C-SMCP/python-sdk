# -*- coding: utf-8 -*-
# filename: recovery.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
治理启动恢复（#117；#123 起对齐协议 v0.3.0 §4.8）/ Governance boot recovery。

**intent 驱动、additive-only、离线优先、installed ∧ enabled 门控**（与 rust-sdk ``settings/recovery.rs``
同构）：安装集取自 merged ``installedPlugins``（全局安装意图，§4.8.1），活跃集 = 已安装 ∧
``enabledPlugins[pid] is True``（**缺省翻转**：absent/false 均惰性，仅显式 true 激活）——
``installed_disabled`` 恢复为惰性、不进投影。账本 ``installed_plugins.json`` 是可弃派生缓存：意图有、
账本缺（或 installPath 全失效）→ boot 经 :func:`~a2c_smcp.computer.settings.installer.materialize_plugin`
**重物化**（§4.9「删除无损」；物化 ≠ 激活，installed_disabled 也重建账本，保 enable 廉价）。
bundled SKILL 经 :func:`~a2c_smcp.computer.skills.staging.stage_marketplace_skills`（``refresh=False``
就地复用 clone 树）恢复进 Registry；bundled MCP server 经 :func:`collect_enabled_bundled_servers`
纯函数**可查询**（含 plugin/marketplace 归属，§4.8.3），进程物化归 client（#93 client owns MCP config，
§4.8 blockquote）——boot 默认不拉进程，重挂由 client 显式经
:meth:`~a2c_smcp.computer.computer.Computer.reconcile_governance` 传 hooks 触发（设计 Y 同构契约）。

失败降级铁律：单 plugin / 单 marketplace / 单文件失败 → WARN + 记入报告，**不抛、不阻断 boot**
（§3 degraded / §5.2）。v0.2.x 存量（账本有、意图无）由 ``Computer.boot_up`` 先行
:func:`~a2c_smcp.computer.settings.installer.migrate_legacy_installs` 一次性迁移，本模块不感知旧语义。

已知限制（与 rust 同构文档化）：boot 内 declared 视图不含 ``--settings`` flag scope（Computer 无 flag
知识；CLI 接线时可显式传 flag-aware ``declared``）——跨重启可靠 disable 请写 user scope。

Intent-driven, additive-only, offline-first governance recovery mirroring rust-sdk (protocol v0.3.0):
the install set comes from the merged ``installedPlugins`` intent, the active set is installed AND
``enabledPlugins[pid] is True`` (default flipped), missing ledger entries are re-materialized from
intent, restored SKILLs land in the Registry, and enabled bundled MCP servers stay queryable with
ownership; process materialization stays with the client (#93).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.installer import materialize_plugin
from a2c_smcp.computer.settings.reconciler import declared_installed_plugin_ids
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

    - ``restored_plugins``：clone 可达且已尝试 stage 的活跃 plugin id（``<plugin>@<marketplace>``）；
      **非**"保证注册了 SKILL"（manifest 损坏时 stage 内部降级，权威清单看 ``restored_skills``）。
    - ``restored_skills``：本轮成功注册/刷新的 SKILL name 权威清单。
    - ``remounted_servers``：阶段二经 hooks 成功重挂的 server name（由编排方填充；本模块函数留空）。
    - ``failed_marketplaces``：源不可达 / clone 缺失且重建失败 / known 记录缺失（降级、不阻断）。
    - ``skipped_disabled``：``installed_disabled``（安装意图在、``enabledPlugins`` absent/false）惰性跳过的 pid
      （v0.3.0 缺省翻转，§2.4）。
    - ``rematerialized``：意图有、账本缺 → 本轮重物化成功重建账本的 pid（§4.9 删除无损）。
    """

    restored_plugins: list[str] = field(default_factory=list)
    restored_skills: list[str] = field(default_factory=list)
    remounted_servers: list[str] = field(default_factory=list)
    failed_marketplaces: list[str] = field(default_factory=list)
    skipped_disabled: list[str] = field(default_factory=list)
    rematerialized: list[str] = field(default_factory=list)


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
    """boot-active 门控：仅显式 ``enabledPlugins[pid] is True`` 激活（v0.3.0 缺省翻转——absent/false 均惰性，与 rust 逐字一致）。"""
    enabled = declared.get("enabledPlugins")
    if isinstance(enabled, Mapping):
        return enabled.get(pid) is True
    return False


def _split_pid(pid: str) -> tuple[str, str] | None:
    """``<plugin>@<marketplace>`` → (plugin, marketplace)；无 ``@``（本地-only 形态）→ None 跳过。"""
    plugin, sep, marketplace = pid.partition("@")
    if not sep or not plugin or not marketplace:
        return None
    return plugin, marketplace


def _ledger_materialized(records: Any) -> bool:
    """某 pid 的账本记录是否仍有效物化（至少一条记录的 ``installPath`` 目录存在）/ Whether the ledger entry is live。"""
    if not isinstance(records, list):
        return False
    for rec in records:
        install_path = rec.get("installPath") if isinstance(rec, Mapping) else None
        if isinstance(install_path, str) and install_path and Path(install_path).is_dir():
            return True
    return False


async def recover_marketplace_skills(
    registry: SkillRegistry,
    home: Path,
    declared: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> GovernanceRecoveryReport:
    """
    阶段一：从安装意图恢复活跃 plugin 的 bundled SKILL + 重物化缺失账本（幂等、additive-only）/ Phase 1。

    安装集 = merged ``installedPlugins``（§4.8.1；意图缺失/空 → noop——账本只是派生缓存，不参与决策，§2.3）。
    按 marketplace 分组 → :func:`stage_marketplace_skills`（``plugin_filter`` = **活跃**子集：installed ∧
    ``enabledPlugins[pid] is True``；``refresh=False`` 离线优先：clone 在则就地复用重扫，缺失才尝试 clone）→
    对账本缺失/installPath 全失效的 installed pid（含 installed_disabled）经
    :func:`~a2c_smcp.computer.settings.installer.materialize_plugin` 重物化（§4.9 删除无损；失败 WARN 跳过）。
    降级判定与 rust 同构：stage 内部吞错，事后 clone 树仍缺 → 该 marketplace 入 ``failed_marketplaces``；
    known_marketplaces 缺记录 → 同样降级。**绝不抛、不阻断其余**。

    :param registry: 目标 :class:`SkillRegistry`（恢复注册进当前活跃集）。
    :param home: SKILL Home 绝对根。
    :param declared: 合并声明视图（取 ``installedPlugins`` + ``enabledPlugins`` 两键作权威门控）。
    :param env: 环境映射（账本路径解析 + git 子进程），默认 ``os.environ``。
    :param timeout: 单次 git 操作超时（仅 clone 缺失重建时可能触发）。
    """
    report = GovernanceRecoveryReport()
    intent = sorted(declared_installed_plugin_ids(declared))
    if not intent:
        return report
    ledger = load_installed_plugins(home=home, env=env).get("plugins", {})
    known = load_known_marketplaces(home=home, env=env).get("marketplaces", {})

    # installed pid 按 marketplace 分组（plugin → 是否活跃）/ group intent pids by marketplace.
    by_marketplace: dict[str, dict[str, bool]] = {}
    for pid in intent:
        split = _split_pid(pid)
        if split is None:  # declared 经 schema 校验必有 @；防御保留
            continue
        active = _plugin_boot_active(declared, pid)
        if not active:
            report.skipped_disabled.append(pid)
        plugin, marketplace = split
        by_marketplace.setdefault(marketplace, {})[plugin] = active

    for marketplace, plugins in sorted(by_marketplace.items()):
        active_plugins = {p for p, is_active in plugins.items() if is_active}
        needs_materialize = sorted(p for p in plugins if not _ledger_materialized(ledger.get(f"{p}@{marketplace}")))
        if not active_plugins and not needs_materialize:
            continue  # 全惰性且账本完好 → 零动作（installed_disabled 静止态）

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
            plugin_filter=active_plugins,
            refresh=False,  # 离线优先：clone 在则零 git / offline-first
            timeout=timeout,
            env=env,
        )
        # 降级代理判定（rust 同构）：stage 失败降级不抛；事后 clone 树仍缺 = 源不可达且无本地物化。
        if not marketplace_skill_dir(home, marketplace).is_dir():
            logger.warning("governance recovery: marketplace %r clone missing and rebuild failed, degraded", marketplace)
            report.failed_marketplaces.append(marketplace)
            continue
        # 重物化：意图有、账本缺（或 installPath 全失效）→ 由 (marketplace, plugin) 纯函数重建账本
        # （§4.9 删除无损）。物化 ≠ 激活——installed_disabled 也重建（保 enable 廉价、账本可查询）；
        # 冲突预检传 None（boot 无 live manager；重挂阶段自有 existing 名跳过护栏）。
        for plugin in needs_materialize:
            pid = f"{plugin}@{marketplace}"
            try:
                await materialize_plugin(pid, home, refresh=False, timeout=timeout, env=env)
                report.rematerialized.append(pid)
            except Exception as e:  # noqa: BLE001 - 失败降级铁律：WARN 跳过，不阻断 boot
                logger.warning("governance recovery: re-materialize %r failed, degraded: %s", pid, e)
        report.restored_skills.extend(names)
        report.restored_plugins.extend(sorted(f"{plugin}@{marketplace}" for plugin in active_plugins))

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
    MCP 配置模型，或经 :meth:`Computer.reconcile_governance` 传 hooks 显式重挂。门控 = **installed ∧
    enabled**（v0.3.0 §4.8.1：pid ∈ merged ``installedPlugins`` 且 ``enabledPlugins[pid] is True``；
    ``installed_disabled`` 不可见——不进活跃投影，§2.4）。跨 plugin/scope 同名
    server **首见保留去重**（账本插入序，与 rust 一致）；``installPath`` 缺失 / bundled JSON 损坏 →
    WARN 跳过该记录，不阻断其余、不抛。

    :param home: SKILL Home 绝对根。
    :param declared: 合并声明视图（``installedPlugins`` + ``enabledPlugins`` 两键）。
    :param env: 环境映射（账本路径解析），默认 ``os.environ``。
    """
    out: list[BundledServerRecord] = []
    seen: set[str] = set()
    intent = declared_installed_plugin_ids(declared)
    installed = load_installed_plugins(home=home, env=env).get("plugins", {})
    for pid, records in installed.items():
        split = _split_pid(pid)
        if split is None:
            continue
        if pid not in intent or not _plugin_boot_active(declared, pid):
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
