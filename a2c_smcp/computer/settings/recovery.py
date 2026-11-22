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

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.installer import materialize_plugin
from a2c_smcp.computer.settings.reconciler import (
    declared_installed_plugin_ids,
    ledger_entry_fully_materialized,
    ledger_record_materialized,
)
from a2c_smcp.computer.settings.schema import (
    FIELD_EXTRA_KNOWN_MARKETPLACES,
    SCOPE_ORDER,
    SettingsScope,
    SettingsValidationError,
    is_marketplace_declaration_field,
    scrub_diagnostic_chars,
)
from a2c_smcp.computer.settings.scope import (
    load_settings_file,
    user_settings_path,
    workdir_local_settings_path,
    workdir_project_settings_path,
)
from a2c_smcp.computer.settings.store import (
    InstalledPluginsFile,
    known_marketplace_source,
    load_installed_plugins,
    load_known_marketplaces,
    update_installed_plugins,
)
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.manifest import PluginManifestError, load_bundled_servers
from a2c_smcp.computer.skills.marketplace_identity import (
    MarketplaceNameChangeError,
    display_source,
    redact_ledger_key,
    same_source,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.sources import safe_exception_text
from a2c_smcp.computer.skills.staging import DEFAULT_GIT_TIMEOUT, stage_marketplace_skills
from a2c_smcp.utils.bundle_id import resolve_bundle_id
from a2c_smcp.utils.git_url import redact_git_urls_in_text, sanitize_path_for_diagnostics
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
    - ``scope_normalized``：重物化时**无任何层线索**、记录归一 user scope 的 pid（#125 任务 1 显式标注；
      有线索时经 :func:`_infer_record_scopes` 推回原 scope，不入此列）。
    - ``pending_bundled_servers``：project scope enabledPlugins 激活 → 回落审批门 PENDING 的 bundled
      server 记录（#159 Option B / 协议 §2.2）；client 据此展示审批 UI。
    - ``marketplace_source_conflicts``：**声明源 ≠ 已注册源**的 marketplace（#287 / §5.14，**只诊断不换源**）。
      本报告是观测信号而非降级信号——冲突项照常按 known 旧源恢复，**不**进 ``failed_marketplaces``。
    - ``marketplace_declaration_errors``：settings 校验阶段**被整条丢弃**的非法 ``extraKnownMarketplaces``
      声明（#293，镜像 rust 同名同义字段）。与 ``marketplace_source_conflicts`` **正交、不重复计数**：后者
      比对的是**合法**声明与已注册源，本字段是声明**自身不可用**（typo / 缺 ``source`` / 非 git URL）——
      被校验丢弃后连「可比声明」都不存在，故两个列表不可能对同一事实各记一次。恢复源 / 降级语义均不变。
      仅由 :meth:`Computer.reconcile_governance` 从 ``ResolvedSettings.errors`` 填充；直接调用
      :func:`recover_marketplace_skills` 时恒空（该入口拿到的 ``declared`` 已过校验）。
    """

    restored_plugins: list[str] = field(default_factory=list)
    restored_skills: list[str] = field(default_factory=list)
    remounted_servers: list[str] = field(default_factory=list)
    failed_marketplaces: list[str] = field(default_factory=list)
    skipped_disabled: list[str] = field(default_factory=list)
    rematerialized: list[str] = field(default_factory=list)
    scope_normalized: list[str] = field(default_factory=list)
    pending_bundled_servers: list[BundledServerRecord] = field(default_factory=list)
    marketplace_source_conflicts: list[MarketplaceSourceConflict] = field(default_factory=list)
    marketplace_declaration_errors: list[MarketplaceDeclarationError] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BundledServerRecord:
    """enabled bundled MCP server 的可查询记录（归属 = boot 纯函数输出，§4.8.3）/ Queryable bundled-server record。

    ``config`` 从账本 ``installPath`` 经 :func:`load_bundled_servers` **每次 boot 重新解析**（不信任
    存储态，§5.8 精神）；``plugin``/``marketplace`` 供 client 挂载时携带 D2 渲染上下文
    （#137 ③ 起走 transient ``Computer.amount_server(cfg, plugin=, marketplace=)``——治理投影不回写 mcp.json）。
    """

    plugin_id: str
    plugin: str
    marketplace: str
    install_path: Path
    config: MCPServerConfig
    # v0.3.2 (#159 Option B)：激活该 plugin 的 enabledPlugins 来源 scope（None = 未传 per_scope_layers 或无法判定）
    enabled_origin: SettingsScope | None = None


# 阶段二重挂回调：携归属记录（供 client 带 plugin/marketplace 上下文注册，D2 ${input:} 前缀回退解析）。
# Phase-2 remount callback carrying ownership context (unlike installer's context-free RegisterServer).
RegisterBundledServer = Callable[[MCPServerConfig, BundledServerRecord], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MarketplaceSourceConflict:
    """
    声明源与已注册源不一致（#287，镜像 rust-sdk#295）/ Declared-vs-registered marketplace source mismatch.

    **只诊断、不换源**（runtime-contract §2.3 派生方向 + §5.14「MUST NOT 静默替换」）：boot 仍按
    ``known_marketplaces.json`` 的**已注册源**恢复，本记录只让用户看见不一致。换源唯一的合法路径是显式
    ``marketplace remove`` → ``add``。

    三个字段**全部是展示值**，不可用于任何再克隆 / 比较：

    - ``marketplace``：账本键经 :func:`~a2c_smcp.computer.skills.marketplace_identity.redact_ledger_key`
      （known 键可手编，可能是携带凭据的 URL 残形，#261/#263/#281 同口径）。
    - ``declared_source_display`` / ``known_source_display``：两侧 ``source`` 经
      :func:`~a2c_smcp.computer.skills.marketplace_identity.display_source`（去 userinfo / query / fragment，
      path 段含凭据样文本一律占位符）。
    """

    marketplace: str
    declared_source_display: str
    known_source_display: str


@dataclass(frozen=True, slots=True)
class MarketplaceDeclarationError:
    """
    被 settings 校验**整条丢弃**的非法 ``extraKnownMarketplaces`` 声明（#293，镜像 rust 同名同字段）/
    An invalid ``extraKnownMarketplaces`` declaration dropped by settings validation.

    校验语义**不变**（非法条目照旧逐条丢弃、不阻断启动）；本结构只让「有声明但非法」在恢复报告里可见。
    与 :class:`MarketplaceSourceConflict` **正交**：后者是**合法声明**与已注册源不一致（只诊断不换源）；
    本结构是声明**自身不可用**（漏 scheme / ``owner/repo`` 简写 / 非 git / 缺 ``source``）。被丢弃的声明
    根本不在 ``declared`` 视图里，故 :func:`detect_marketplace_source_conflicts` 看不到它——两个列表对同一
    事实不可能各记一次。

    ``field`` / ``reason`` / ``source_path`` 均已经过脱敏与控制字符清洗（见
    :func:`sanitized_marketplace_error`）：``field`` 保留 ``extraKnownMarketplaces.`` 前缀便于定位，
    其后的市场名段按 :func:`~a2c_smcp.computer.skills.marketplace_identity.redact_ledger_key` 口径展示
    （市场名是不可信手编值，可含凭据形态 URL）。**三字段与 ``marketplace`` 全是展示值**，不可用于任何
    再克隆 / 比较 / 去重。
    """

    scope: SettingsScope
    marketplace: str | None
    field: str
    reason: str
    source_path: str | None = None


def sanitize_marketplace_name(name: str) -> str:
    """
    单个不可信**名字**（市场名段 / 子字段残段）→ 诊断展示值（#293）/
    A single untrusted name → display-safe diagnostics value.

    走 :func:`~a2c_smcp.computer.skills.marketplace_identity.redact_ledger_key`（#261/#263/#281 单一
    权威：含 ``://`` 走 :func:`~a2c_smcp.utils.git_url.diagnostic_url_for_display`，其余走
    :func:`~a2c_smcp.utils.git_url.sanitize_segment_for_diagnostics`）。该原语只转义 Cc/Cs，
    Cf（方向覆写 / 行分隔）原样透出，故再叠
    :func:`~a2c_smcp.computer.settings.schema.scrub_diagnostic_chars`。

    **比 rust 更严是有意的**：rust 对名字也用散文脱敏（``redact_git_urls_in_text``，只去 userinfo、
    保留 path），而账本键/市场名是**单个不可信值**，path 段里的 ``alice:PW@example.com`` 会照原样回显
    （#261 的口径）。名字走段漏斗，散文（``reason``）才走散文脱敏。

    刻意**不**改 ``utils.git_url`` 的 ``_UNSAFE_CHAR_CATEGORIES``：那是全仓展示面的转义器，口径已冻结
    （「Cf 与 rust 口径一致不处理」指 ``escape_unsafe_chars``），本函数只服务 settings 诊断面。
    """
    return scrub_diagnostic_chars(redact_ledger_key(name))


def sanitize_marketplace_text(text: str) -> str:
    """
    诊断**散文**（``reason``）→ 展示值：Git URL token 级脱敏 + 控制 / 方向覆写字符清洗（#293）/
    Prose (``reason``) → display-safe value.

    与 rust ``sanitize_marketplace_text`` 同语义（rust 走 ``redact_git_urls_in_text``）：
    :func:`~a2c_smcp.utils.git_url.redact_git_urls_in_text` 逐 token 脱敏、保留散文结构（不会像段漏斗
    那样把整句替换成一个占位符），再经 ``scrub_diagnostic_chars`` 清掉 Cf 族。
    """
    return scrub_diagnostic_chars(redact_git_urls_in_text(text))


def sanitized_marketplace_error(error: SettingsValidationError) -> MarketplaceDeclarationError | None:
    """
    单条 settings 校验错误 → 脱敏 :class:`MarketplaceDeclarationError`；非 marketplace 子树返回 ``None``。

    .. note::
       与 rust 的**落点差异**（有意，见设计文档 §6）：rust 在 ``validate_settings`` 出口即脱敏，顺带修好了
       ``settings show``；本仓 `settings show` 的凭据回显是独立安全单
       [#295](https://github.com/A2C-SMCP/python-sdk/issues/295)，故本函数**只**在采集处调用，
       ``validate_settings`` 出口与原 ``format_settings_errors`` 行为不变。``#295`` 落地时可直接复用本函数。
    """
    if not is_marketplace_declaration_field(error.field):
        return None
    # field 保留前缀：前缀本身含 `.`，整段喂给 URL 脱敏器会被误判为 scheme 的一部分（与 rust 同因）。
    prefix = f"{FIELD_EXTRA_KNOWN_MARKETPLACES}."
    if error.field.startswith(prefix):
        field = f"{FIELD_EXTRA_KNOWN_MARKETPLACES}.{sanitize_marketplace_name(error.field[len(prefix):])}"
    else:
        # 无法剥离前缀（整字段错误）→ 整段脱敏（fail-safe，与 rust 同）。
        field = sanitize_marketplace_text(error.field)
    reason = sanitize_marketplace_text(error.reason)
    return MarketplaceDeclarationError(
        scope=error.scope,
        marketplace=declaration_marketplace(field, reason),
        field=field,
        reason=reason,
        # 路径按**路径轴**脱敏：先清控制 / 方向覆写字符，再走
        # :func:`~a2c_smcp.utils.git_url.sanitize_path_for_diagnostics`（#263 四轮：`/` 与 `\` 都当段边界、
        # 段内普通 `@` 保留、authority 位置的 `TOKEN@host` 与 `?`/`#` 尾部整段遮蔽）——它**不是** URL /
        # scp-like 脱敏器，Windows 路径与 `alice@corp` 不会像后者那样被整条吞掉。
        source_path=None
        if error.source_path is None
        else sanitize_path_for_diagnostics(scrub_diagnostic_chars(error.source_path)),
    )


def declaration_marketplace(field: str, reason: str) -> str | None:
    """
    从（已脱敏的）字段路径解析 marketplace 名 / Parse the marketplace name out of the dotted field.

    ``extraKnownMarketplaces.<name>[.source[.type|.url]|.autoUpdate]``：

    - **名称非法**时校验器只记到 ``<name>``，此时**整个剩余段就是名称**——不能按后缀启发式切分，
      因为非法名本身可能就是 ``acme.source.url``。判据与 rust 同：``reason`` 以 kebab 文案开头。
    - 合法名 + 子字段错误 → 剥最长已知后缀。
    - 顶层字段类型错误（无 ``<name>`` 段）→ ``None``。

    已知边界：名字型错误之外的非法名若本身以这些后缀结尾，展示名会被截短——只**少显示**字符，
    不会多显示，故无泄漏面。

    :param field: **已脱敏**的点路径（据此解析，保证 ``marketplace`` 与 ``field`` 同源）。
    :param reason: **已清洗**的校验原因（用于名称型分支判定）。
    """
    prefix = f"{FIELD_EXTRA_KNOWN_MARKETPLACES}."
    if not field.startswith(prefix):
        return None
    rest = field[len(prefix):]
    if not rest:
        return None
    if reason.startswith("marketplace name must be strict kebab"):
        return rest
    for suffix in (".source.url", ".source.type", ".source", ".autoUpdate"):
        if rest.endswith(suffix):
            return rest[: -len(suffix)] or None
    return rest


def marketplace_declaration_errors(
    errors: Iterable[SettingsValidationError],
) -> list[MarketplaceDeclarationError]:
    """
    从 settings 校验错误中筛出 ``extraKnownMarketplaces`` 声明错误并脱敏（纯函数、保序、1:1）/
    Filter + sanitize the ``extraKnownMarketplaces`` declaration errors.

    **不聚合**：一条错误 → 一条报告（同一个市场在 user / local 两层各错一条即两条）——分层语义是用户
    2026-11-22 裁决的硬要求：user 层非法、local 层合法时，合并视图仍有可用声明，聚合成「该市场无声明」
    会误导。**不改**校验 / 丢弃语义。

    :param errors: :attr:`~a2c_smcp.computer.settings.scope.ResolvedSettings.errors`。
    """
    collected: list[MarketplaceDeclarationError] = []
    for error in errors:
        sanitized = sanitized_marketplace_error(error)
        if sanitized is not None:
            collected.append(sanitized)
    return collected


def attach_marketplace_declaration_errors(
    report: GovernanceRecoveryReport,
    errors: Iterable[SettingsValidationError],
) -> None:
    """
    把 settings 校验错误接入恢复报告并打诊断日志（#293 的**单一接线点**）/
    Attach the settings validation errors to the recovery report and log them.

    编排方（:meth:`Computer.reconcile_governance`）在阶段一返回后**无条件**调用——空安装意图时
    :func:`recover_marketplace_skills` 会早退，故该回填不能放进它里面。
    """
    report.marketplace_declaration_errors = marketplace_declaration_errors(errors)
    _log_marketplace_declaration_errors(report.marketplace_declaration_errors)


def _log_marketplace_declaration_errors(errors: Sequence[MarketplaceDeclarationError]) -> None:
    """声明错误日志：WARN 汇总（带脱敏市场名）+ 逐条 DEBUG / Summary WARN + per-item DEBUG."""
    if not errors:
        return
    for error in errors:
        logger.debug(
            "governance recovery: invalid marketplace declaration at scope %s (%s): %s [%s]",
            error.scope.value,
            error.field,
            error.reason,
            error.source_path or "<unknown>",
        )
    names = sorted({error.marketplace for error in errors if error.marketplace})
    # 汇总行**带脱敏后的市场名**：默认日志级别是 INFO，只给计数的话用户不知道该修哪一个
    # （与 `_log_marketplace_source_conflicts` 同口径）。
    logger.warning(
        "governance recovery: %d invalid marketplace declaration(s) in settings were dropped by validation and did "
        "NOT participate in recovery (%s); existing registrations are kept unchanged — fix the declaration to change "
        "the source (invalid declarations are never silently substituted, runtime-contract §5.14)",
        len(errors),
        ", ".join(names) if names else "no marketplace name resolved",
    )


def detect_marketplace_source_conflicts(
    marketplaces: Iterable[str],
    declared: Mapping[str, Any],
    known: Mapping[str, Any],
) -> list[MarketplaceSourceConflict]:
    """
    比对声明源与已注册源，列出不一致的 marketplace（纯函数、零 IO）/ Detect declared-vs-registered mismatches.

    判定口径（#287 用户裁决 2026-11-20）：

    - **只看传进来的 marketplace**——调用方只传 ``installedPlugins`` 意图涉及的集合，无意图项不检测。
    - 无声明（``extraKnownMarketplaces`` 缺该键 / 条目非映射）→ 跳过：没有可比的"声明源"。
    - 无**可用**的 ``known_marketplaces`` 记录（记录缺失 / 记录非映射 / ``source`` 缺失或非映射）→ 跳过：
      没有可比的"已注册源"。该情形**不由本函数报告**——非惰性市场由 stage 循环记入 ``failed_marketplaces``
      （降级）；**全惰性市场**（``installed_disabled`` ∧ 账本完好）在 stage 循环里先 ``continue`` 零动作，
      因此两处报告都不出现。判据与 stage 循环共用
      :func:`~a2c_smcp.computer.settings.store.known_marketplace_source`，否则同一次恢复会同时打
      「沿用已注册源」与「已降级」两条互斥诊断。
    - **同源判据 = :func:`~a2c_smcp.computer.skills.marketplace_identity.same_source`**（SDK 单一权威，与
      ``reconciler.reconcile`` 同一函数）：只做字符串级比较，``ssh://`` / ``https://`` / scp-like / ``.git``
      后缀等等价写法**不归一** → 视为不同源（§5.14「无法确定时 MUST fail-closed」）。
    - 声明条目存在但 ``source`` 缺失/畸形 → ``same_source`` 返回 ``False`` → 同样报冲突（fail-closed，
      与 ``reconcile`` 的 ``sourceChanged`` 分支同姿态）。

    .. note::
       上面这条 fail-closed 只在调用方传入**未经 schema 校验**的 ``declared`` 时可达。boot 生产路径的
       ``declared`` 来自 :func:`~a2c_smcp.computer.settings.scope.resolve_settings`，其
       ``_validate_extra_marketplaces``（``settings/schema.py``）会把 ``source`` 缺失 / 非 ``git`` / URL 畸形
       的声明**整条丢弃**——此时本函数看到的是「无声明」，既不报冲突也无从诊断。

       该静默已由 #293 收口：被丢弃的声明进入
       :attr:`GovernanceRecoveryReport.marketplace_declaration_errors`（见
       :func:`marketplace_declaration_errors`），与冲突列表**分开计数**。本函数**只**比对合法声明，
       不改。

    :param marketplaces: 待检测的 marketplace 名（调用方保证 = 安装意图涉及集）。
    :param declared: 合并声明视图（取 ``extraKnownMarketplaces``）。
    :param known: ``known_marketplaces.json`` 的 ``marketplaces`` 映射。
    :return: 按 marketplace 名稳定排序的冲突列表（字段均为脱敏展示值）。
    """
    raw = declared.get("extraKnownMarketplaces")
    declarations: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    conflicts: list[MarketplaceSourceConflict] = []
    for marketplace in sorted(set(marketplaces)):
        decl = declarations.get(marketplace)
        if not isinstance(decl, Mapping):
            continue
        known_source = known_marketplace_source(known, marketplace)
        if known_source is None:
            # 与 stage 循环**同一可用性判据**（:func:`~a2c_smcp.computer.settings.store.known_marketplace_source`）：
            # 没有可用的「已注册源」⇒ 该市场走 ``failed_marketplaces`` 降级路径。此处若仍报冲突，同一次恢复会
            # 同时打「沿用已注册源」与「已降级」两条互斥诊断。
            continue
        declared_source = decl.get("source")
        if same_source(known_source, declared_source):
            continue
        # marketplace 走账本键脱敏：进入本函数的名字上游已过 `is_valid_enabled_plugin_key`（严格 kebab）⇒
        # 当前恒为恒等映射，保留它是与 #261/#263/#281 展示口径一致 + 纵深防御（不把账本键当可信值）。
        conflicts.append(
            MarketplaceSourceConflict(
                marketplace=redact_ledger_key(marketplace),
                declared_source_display=display_source(declared_source),
                known_source_display=display_source(known_source),
            ),
        )
    return conflicts


def _log_marketplace_source_conflicts(conflicts: Sequence[MarketplaceSourceConflict]) -> None:
    """冲突诊断日志：WARN 汇总（不带不可信值）+ 逐条 DEBUG（脱敏展示值）/ Summary WARN + per-item DEBUG."""
    if not conflicts:
        return
    for conflict in conflicts:
        logger.debug(
            "governance recovery: marketplace %r is declared from a different source "
            "(declared %s, registered %s); keeping the registered source",
            conflict.marketplace,
            conflict.declared_source_display,
            conflict.known_source_display,
        )
    # 汇总行**带脱敏后的 marketplace 名**：默认日志级别是 INFO、CLI 展示面留 follow-up，只给计数的话用户
    # 根本不知道该处理哪一个。两侧 URL 仍只在逐条 DEBUG 里（同为脱敏展示值）。
    logger.warning(
        "governance recovery: %d marketplace(s) are declared from a different source than their registration (%s); "
        "keeping the registered source and its existing capabilities (declared-source changes are diagnostic only "
        "— switch by explicit marketplace remove / add, runtime-contract §5.14)",
        len(conflicts),
        ", ".join(conflict.marketplace for conflict in conflicts),
    )


def _plugin_boot_active(declared: Mapping[str, Any], pid: str) -> bool:
    """boot-active 门控：仅显式 ``enabledPlugins[pid] is True`` 激活（v0.3.0 缺省翻转——absent/false 均惰性，与 rust 逐字一致）。"""
    enabled = declared.get("enabledPlugins")
    if isinstance(enabled, Mapping):
        return enabled.get(pid) is True
    return False


def _plugin_enable_origin(
    per_scope_layers: Mapping[SettingsScope, Mapping[str, Any]],
    pid: str,
) -> SettingsScope | None:
    """返回激活该 plugin 的最高 scope（按 SCOPE_ORDER 优先级），用于 #159 Option B 审批门分叉。

    ``enabledPlugins`` 三态合并（协议 §2.4）：absent=无意见、true=启用、false=显式禁用。
    逐 scope（低→高）检查 raw 层：遇到 ``false`` → 被显式禁用 → 返回 None；
    遇到 ``true`` → 该 scope 是当前激活来源 → 继续检查更高 scope 覆盖。
    最终返回最后一个 ``true`` 的 scope（即最高激活 scope）；全部 absent 返回 None。

    :param per_scope_layers: per-scope 校验后 raw 层（如 :class:`ResolvedSettings.per_scope_layers`）。
    :param pid: plugin id（``<plugin>@<marketplace>``）。
    :return: 最高激活 scope，或 None（被禁用或未在任何 scope 启用）。
    """
    origin: SettingsScope | None = None
    for s in SCOPE_ORDER:
        layer = per_scope_layers.get(s)
        if not isinstance(layer, Mapping):
            continue
        enabled = layer.get("enabledPlugins")
        if not isinstance(enabled, Mapping):
            continue
        val = enabled.get(pid)
        if val is False:
            origin = None  # 显式禁用覆盖一切
        elif val is True:
            origin = s  # 该 scope 激活
    return origin


def _split_pid(pid: str) -> tuple[str, str] | None:
    """``<plugin>@<marketplace>`` → (plugin, marketplace)；无 ``@``（本地-only 形态）→ None 跳过。"""
    plugin, sep, marketplace = pid.partition("@")
    if not sep or not plugin or not marketplace:
        return None
    return plugin, marketplace


def _intent_layer_snapshot(env: Mapping[str, str] | None, cwd: Path) -> dict[str, Mapping[str, Any]]:
    """
    user / project / local 三层 settings 的**原始分层**快照（merged 视图丢失层归属，scope 推回需逐层看）/ Raw layers。

    project/local 锚定 ``cwd``（#116 单锚点模型）：异地项目的层不可见——该盲区由归一 fallback + WARN 兜底。
    复用 :func:`~a2c_smcp.computer.settings.scope.load_settings_file`（缺失文件 → ``({}, [])``，纯读零副作用）。
    """
    user_data, _ = load_settings_file(user_settings_path(env), SettingsScope.USER)
    project_data, _ = load_settings_file(workdir_project_settings_path(cwd), SettingsScope.PROJECT)
    local_data, _ = load_settings_file(workdir_local_settings_path(cwd), SettingsScope.LOCAL)
    return {"user": user_data, "project": project_data, "local": local_data}


def _infer_record_scopes(pid: str, layers: Mapping[str, Mapping[str, Any]], cwd: Path) -> list[tuple[str, str | None]]:
    """
    由分层线索推回账本记录 scope——enable/disable「写入层 = 安装 scope」契约（installer）的逆运算（#125 任务 1）。

    线索规则 / Clue rules:
    - project/local 层：``enabledPlugins[pid] is True`` **或** ``installedPlugins`` 声明含 pid（声明式复现）
      → ``(scope, str(cwd))``。纯 ``false`` **不算**——它可能是团队对 user-scope 安装的独立禁用覆盖（§2.4
      三态合并），当作 install-scope 线索会凭空捏造记录，此后 CLI enable 逐 scope 写会把团队 ``false``
      覆写为 ``true``（隔离审查 🟡#3）。误差方向不对称：漏推回 → fallback user、disable 语义靠合并仍成立
      （安全）；错捏造 → enable 反向覆写治理意图（不安全）。
    - user 层：``enabledPlugins`` 含 pid 键即算（含 ``false``——与 fallback 同落点，无捏造风险）；
      user 层 ``installedPlugins`` 是 install 恒写的全局意图，无区分度、不算。
    - 多层命中 → 多条记录（user→project→local 稳定序，多 scope 不塌缩）；全无线索 → ``[]``
      （调用方 fallback ``[("user", None)]`` + ``scope_normalized`` 归一标注）。
    """
    out: list[tuple[str, str | None]] = []
    for scope_name in ("user", "project", "local"):
        data = layers.get(scope_name) or {}
        enabled = data.get("enabledPlugins")
        if scope_name == "user":
            clue = isinstance(enabled, Mapping) and pid in enabled
        else:
            clue = isinstance(enabled, Mapping) and enabled.get(pid) is True
            if not clue:
                installed = data.get("installedPlugins")
                clue = isinstance(installed, list) and pid in installed
        if clue:
            out.append((scope_name, str(cwd) if scope_name != "user" else None))
    return out


def _sweep_dead_records(pid: str, home: Path, env: Mapping[str, str] | None) -> None:
    """
    重建成功后清扫该 pid 下失效的残留记录（派生缓存卫生，非资产删除、不违 §4.8.4）。

    动机：重物化按线索重建后若残留失效 scope 记录，CLI enable/disable 会循环逐 scope 误写死层
    （如已不可推回的异地 project 层）。判据与重物化触发同源
    （:func:`~a2c_smcp.computer.settings.reconciler.ledger_record_materialized`：目录在 ∧ bundled JSON
    可解析——「目录在但 JSON 损坏」的记录同样清扫，隔离审查 🟡#4 判据对称）。
    """

    def _mut(data: InstalledPluginsFile, _pid: str = pid) -> None:
        records = data["plugins"].get(_pid)
        if not isinstance(records, list):
            return
        alive = [r for r in records if ledger_record_materialized(r)]
        if alive:
            data["plugins"][_pid] = alive

    update_installed_plugins(_mut, home=home, env=env)


async def recover_marketplace_skills(
    registry: SkillRegistry,
    home: Path,
    declared: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
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
    known_marketplaces 无**可用**记录（缺失 / 记录非映射 / ``source`` 非映射）→ 同样降级。**绝不抛、不阻断其余**。
    注意「全惰性市场」（无活跃 plugin ∧ 账本完好）在查 known **之前**就 ``continue``（零动作是设计）——它们
    既不入 ``failed_marketplaces`` 也不产生声明源冲突。

    声明源诊断（#287，镜像 rust-sdk#295）：对 ``installedPlugins`` 意图涉及的每个 marketplace，比对
    ``declared["extraKnownMarketplaces"][name]["source"]`` 与 ``known_marketplaces[name]["source"]``——
    不同则记入 :attr:`GovernanceRecoveryReport.marketplace_source_conflicts`（展示值脱敏）+ WARN 汇总 +
    逐条 DEBUG。**只诊断**：恢复仍用已知的**已注册源**，``failed_marketplaces`` 与降级语义不变。理由：
    boot 自动换源即 §5.14 禁止的"静默替换"；换源只经显式 ``marketplace remove`` → ``add``。

    scope 推回（#125 任务 1）：重物化前经 :func:`_infer_record_scopes` 扫描 user / cwd 锚定 project/local
    三层的 ``enabledPlugins`` 条目（及 project/local 的 ``installedPlugins`` 声明）作 scope 线索——
    enable/disable「写入层 = 安装 scope」契约的逆运算；多层线索重建多条记录，重建后清扫死残留记录。
    已知盲区（文档化，rust 同构）：**cwd 不可见的异地 project/local 层线索无法推回**——无线索时归一
    user scope + WARN + ``report.scope_normalized`` 显式标注；完美复原需 committed 记录承载，属可选
    pin-lock 扩展（§4.9.2）范畴。

    :param env: 环境映射（解析 user config dir），默认 ``os.environ``。
    :param cwd: project/local 锚定目录（#134），``None`` = ``os.getcwd()``。

    :param registry: 目标 :class:`SkillRegistry`（恢复注册进当前活跃集）。
    :param home: SKILL Home 绝对根。
    :param declared: 合并声明视图（取 ``installedPlugins`` + ``enabledPlugins`` 两键作权威门控；#287 起**另只读**
        ``extraKnownMarketplaces`` 作声明源诊断，缺该键 ⇒ 不产生 ``marketplace_source_conflicts``）。
        本函数拿到的视图**已过 settings 校验**，非法声明已被丢弃、错误只存在于
        :attr:`ResolvedSettings.errors`——因此 #293 的 ``marketplace_declaration_errors`` **不在此填充**，
        由 :meth:`Computer.reconcile_governance` 无条件回填（镜像 rust；直接调用本函数时该字段恒空）。
    :param env: 环境映射（账本路径解析 + git 子进程），默认 ``os.environ``。
    :param timeout: 单次 git 操作超时（仅 clone 缺失重建时可能触发）。
    """
    report = GovernanceRecoveryReport()
    intent = sorted(declared_installed_plugin_ids(declared))
    if not intent:
        return report
    ledger = load_installed_plugins(home=home, env=env).get("plugins", {})
    known = load_known_marketplaces(home=home, env=env).get("marketplaces", {})
    resolved_cwd = cwd if cwd is not None else Path.cwd()
    layers = _intent_layer_snapshot(env, resolved_cwd)  # scope 推回线索快照（循环外一次，#125 任务 1）

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

    # #287：声明源 vs 已注册源诊断。放在 stage 循环**外**——循环内降级分支多且会 ``continue``，而「全惰性
    # 零动作」的市场同样应被诊断（口径 = 意图涉及即检测）。**只读**：不改 ``source`` / ``failed_marketplaces``，
    # 恢复仍按 known 旧源（§2.3 派生方向 + §5.14 MUST NOT 静默替换）。
    report.marketplace_source_conflicts = detect_marketplace_source_conflicts(by_marketplace, declared, known)
    _log_marketplace_source_conflicts(report.marketplace_source_conflicts)

    for marketplace, plugins in sorted(by_marketplace.items()):
        active_plugins = {p for p, is_active in plugins.items() if is_active}
        # ∀ 语义（fully）：混合健康度（一条健康 + 一条损坏/死路径）也触发重物化——健康 scope 幂等重建、
        # 损坏残留由 sweep 清扫（隔离审查 🟡#4：∃ 语义会让损坏 scope 记录每次 boot 被 WARN-skip 却永不修复）。
        needs_materialize = sorted(p for p in plugins if not ledger_entry_fully_materialized(ledger.get(f"{p}@{marketplace}")))
        if not active_plugins and not needs_materialize:
            continue  # 全惰性且账本完好 → 零动作（installed_disabled 静止态）

        source = known_marketplace_source(known, marketplace)
        if source is None:
            # 记录缺失 / 记录非映射 / source 非映射 同属「无可用已注册源」（与 :func:`detect_marketplace_source_conflicts`
            # 同一判据）——文案不再断言「记录缺失」，账本可被手编成半形。
            logger.warning(
                "governance recovery: marketplace %r has no usable known_marketplaces record (missing or malformed "
                "source); its plugins %s degraded (not restored)",
                marketplace,
                sorted(plugins),
            )
            report.failed_marketplaces.append(marketplace)
            continue

        # ① 保证 catalog 物化（空 filter = 仅 clone、零 SKILL 注册）——重物化与 skills stage 的共同前置。
        #    降级代理判定（rust 同构）：stage 失败降级不抛；事后 clone 树仍缺 = 源不可达且无本地物化。
        if not marketplace_skill_dir(home, marketplace).is_dir():
            try:
                await stage_marketplace_skills(
                    marketplace, source, registry, home, plugin_filter=set(), refresh=False, timeout=timeout, env=env,
                )
            except MarketplaceNameChangeError as e:
                # §5.14 改名拒绝：重建被拒 → 该源降级（能力本已缺失），不阻断其余 marketplace、不影响 boot。
                logger.warning(
                    "governance recovery: marketplace %r rebuild rejected (%s); degraded", marketplace, e.reason,
                )
                report.failed_marketplaces.append(marketplace)
                continue
            if not marketplace_skill_dir(home, marketplace).is_dir():
                logger.warning("governance recovery: marketplace %r clone missing and rebuild failed, degraded", marketplace)
                report.failed_marketplaces.append(marketplace)
                continue

        # ② 先重物化、后 stage（§2.4 半态防御）：意图有、账本缺（或 installPath 全失效）→ 由
        #    (marketplace, plugin) 纯函数重建账本（§4.9 删除无损）。物化 ≠ 激活——installed_disabled 也
        #    重建（保 enable 廉价、账本可查询）；冲突预检传 None（boot 无 live manager；重挂阶段自有
        #    existing 名跳过护栏）。**物化失败的活跃 plugin 整体保持 installed_disabled**（摘出 stage
        #    filter，skills 不进投影——否则 skill 已亮、bundled server 不可查询即 rust-sdk#102 半态）。
        #    scope 经分层线索推回（#125 任务 1）；无线索 → 归一 user + WARN + scope_normalized 标注。
        for plugin in needs_materialize:
            pid = f"{plugin}@{marketplace}"
            targets = _infer_record_scopes(pid, layers, resolved_cwd)
            normalized = not targets
            if normalized:
                targets = [("user", None)]
            try:
                for scope_name, project_path in targets:
                    await materialize_plugin(
                        pid, home, scope=scope_name, project_path=project_path, refresh=False, timeout=timeout, env=env,
                    )
                report.rematerialized.append(pid)
            except Exception as e:  # noqa: BLE001 - 失败降级铁律：WARN 跳过，不阻断 boot
                logger.warning(
                    "governance recovery: re-materialize %r failed; kept installed_disabled (skills not staged): %s",
                    pid,
                    safe_exception_text(e),
                )
                active_plugins.discard(plugin)
                continue
            if normalized:
                report.scope_normalized.append(pid)
                logger.warning(
                    "governance recovery: no scope clue for %r; ledger record normalized to user scope "
                    "(original install scope unrecoverable without committed pin-lock, §4.9.2)",
                    pid,
                )
            _sweep_dead_records(pid, home, env)

        # ③ stage skills（仅活跃且物化完好的 plugin）
        if active_plugins:
            try:
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
            except MarketplaceNameChangeError as e:
                # #260/§5.14：恢复路径观测到清单改名 → 拒绝且**不换内容**；该源本轮不恢复，其余继续。
                logger.warning(
                    "governance recovery: marketplace %r skill restore rejected (%s); degraded", marketplace, e.reason,
                )
                report.failed_marketplaces.append(marketplace)
                continue
            report.restored_skills.extend(names)
            report.restored_plugins.extend(sorted(f"{plugin}@{marketplace}" for plugin in active_plugins))

    return report


def collect_enabled_bundled_servers(
    home: Path,
    declared: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    per_scope_layers: Mapping[SettingsScope, Mapping[str, Any]] | None = None,
) -> list[BundledServerRecord]:
    """
    阶段二输入（纯读、无锁、无副作用）：枚举 enabled installed plugin 的 bundled MCP server / Phase-2 pure collect。

    协议 §4.8 blockquote 的"SDK MUST 使 enabled bundled server 可查询"落点：client 据此物化进自己的
    MCP 配置模型，或经 :meth:`Computer.reconcile_governance` 传 hooks 显式重挂。门控 = **installed ∧
    enabled**（v0.3.0 §4.8.1：pid ∈ merged ``installedPlugins`` 且 ``enabledPlugins[pid] is True``；
    ``installed_disabled`` 不可见——不进活跃投影，§2.4）。跨 plugin/scope 按 **bundle_id**
    （身份，非 display name）**首见保留去重**（账本插入序）：同 display 名但显式异 bundle_id 是两个不同身份的
    server，各自保留（协议 data-structures.md no-double-open 同键，#150 site4）。``installPath`` 缺失 /
    bundled JSON 损坏 → WARN 跳过该记录，不阻断其余、不抛。

    与 rust ``settings/recovery.rs`` 同构（modulo rust-sdk 待跟修：rust 侧去重键 name→bundle_id 镜像本改动，
    追踪于 Epic A2C-SMCP/python-sdk#147）——在缺省路径（name≡bundle_id）下二者等价，仅「同名异 bundle_id」裂缝上分叉。

    :param home: SKILL Home 绝对根。
    :param declared: 合并声明视图（``installedPlugins`` + ``enabledPlugins`` 两键）。
    :param env: 环境映射（账本路径解析），默认 ``os.environ``。
    :param per_scope_layers: per-scope 校验后 raw 层（#159 Option B / 协议 §2.2）。
        ``None`` = 不填充 ``BundledServerRecord.enabled_origin``（向后兼容）。
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
                # 去重键 = bundle_id（身份），**非** display name——协议 data-structures.md §BundleID no-double-open
                # 同键：同 display 名但显式异 bundle_id 是两个不同身份的 server，MUST 各自保留（#150 site4 根治）。
                # 跨 SDK：rust settings/recovery.rs 同款 name→bundle_id 去重键待跟修（镜像 follow-up 追踪于 Epic #147）。
                bid = resolve_bundle_id(config)
                if bid in seen:
                    logger.debug("governance recovery: bundled server bundle_id %r duplicated across plugins/scopes, first seen wins", bid)
                    continue
                seen.add(bid)
                origin = None
                if per_scope_layers is not None:
                    origin = _plugin_enable_origin(per_scope_layers, pid)
                out.append(
                    BundledServerRecord(
                        plugin_id=pid, plugin=plugin, marketplace=marketplace,
                        install_path=root, config=config, enabled_origin=origin,
                    ),
                )
    return out
