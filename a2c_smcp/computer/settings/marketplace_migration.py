# -*- coding: utf-8 -*-
# filename: marketplace_migration.py
# @Time    : 2026/11/22
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Marketplace 存量注册名迁移（#268 / D3）/ Legacy marketplace registration-name migration.

设计依据 / Design: ``docs/design-0.5.1-marketplace-name-migration.md``（P0 已按 2026-11-22 裁决回填）。

职责边界 / Scope:
- **检测 / 计划 / 项级预检 / 执行**全部在本模块（单一权威）；CLI 只做参数解析与展示。
- 权威面（改写）：user ``installedPlugins`` / user ``enabledPlugins`` / ``trustedMarketplaces`` /
  ``blockedMarketplaces`` / ``extraKnownMarketplaces``（仅 ``--update-declarations``）/ plugin-scoped input 值 /
  keyring secret。project/local 一律**只报告**。
- 派生面（重建 / 搬迁）：``known_marketplaces`` 新记录、catalog clone、``.plugins`` 外树、账本记录。

硬约束 / Hard rules:
- 默认 dry-run：检测/计划/项级预检**零写入**；strict 只读（损坏即抛、不 rename、不降级）。
- 冲突（涉及任一选中项）→ **整体 abort、零写入、退出码 1**；项级失败（keyring 不可用 / 目标键异值 /
  rename 失败）→ 该项不继续写、其余继续、退出码 3。
- forward-only：无 journal / 备份 / ``--rollback``；崩溃后靠状态自描述 + 重跑续做（§7.2/§7.4）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from a2c_smcp.computer.inputs.plugin_pool import prefix_input_id
from a2c_smcp.computer.inputs.secret_store import SecretStore, SecretStoreError, keyring_available
from a2c_smcp.computer.inputs.value_store import ValueStore, resolve_value_store_path
from a2c_smcp.computer.mcp_clients.model import MCPServerInput, MCPServerPromptStringInput
from a2c_smcp.computer.settings import store
from a2c_smcp.computer.settings.installer import (
    _bare_name_clash_pids,
    _write_enabled_plugin,
    _write_installed_plugin,
    materialize_plugin,
)
from a2c_smcp.computer.settings.raw import (
    SettingsFileCorruptError,
    list_field_or_empty,
    mapping_field_or_empty,
    read_raw_object,
)
from a2c_smcp.computer.settings.reconciler import ledger_entry_fully_materialized
from a2c_smcp.computer.settings.schema import (
    FIELD_BLOCKED_MARKETPLACES,
    FIELD_ENABLED_PLUGINS,
    FIELD_EXTRA_KNOWN_MARKETPLACES,
    FIELD_INSTALLED_PLUGINS,
    FIELD_TRUSTED_MARKETPLACES,
    is_valid_enabled_plugin_key,
    is_valid_marketplace_name,
)
from a2c_smcp.computer.settings.scope import (
    DELETE,
    apply_write,
    user_settings_path,
    workdir_local_settings_path,
    workdir_project_settings_path,
)
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.manifest import (
    MCP_INPUTS_FILENAME,
    MCP_SERVERS_SUBDIR,
    find_plugin_entry,
    iter_plugin_entries,
    plugin_root_base,
    read_marketplace_manifest,
)
from a2c_smcp.computer.skills.marketplace_identity import (
    MANIFEST_NAME_BASELINE_FIELD,
    MarketplaceIdentityError,
    display_source,
    is_safe_registration_path_segment,
    manifest_marketplace_name,
    read_manifest_marketplace_name,
    redact_ledger_key,
    same_source,
    source_identity,
)
from a2c_smcp.computer.skills.sources import GitCloneSpec, LocalPluginSource, resolve_plugin_source
from a2c_smcp.computer.skills.staging import external_plugin_dir, external_plugins_dir
from a2c_smcp.utils.git_url import sanitize_path_for_diagnostics, sanitize_text_for_diagnostics
from a2c_smcp.utils.path import is_within

# 迁移项状态 / Migration item actions.
ACTION_RENAME = "rename"
ACTION_NOOP = "noop"
ACTION_UNRESOLVED = "unresolved"
ACTION_BLOCKED = "blocked"
ACTION_ABORTED = "aborted"
ACTION_NOT_SELECTED = "not-selected"

# 机器可读原因码 / Machine-readable reasons（§3.3 / §4.2 / §5.2）.
REASON_INVALID_RECORD = "invalid-record"
REASON_CATALOG_MISSING = "catalog-missing"
REASON_MANIFEST_UNREADABLE = "manifest-unreadable"
REASON_INVALID_MANIFEST_NAME = "invalid-manifest-name"
REASON_UNSAFE_OLD_NAME = "unsafe-old-name"
REASON_SYMLINK_CATALOG = "symlink-catalog"
REASON_BASELINE_MISMATCH = "baseline-mismatch"
REASON_CATALOG_LOST = "catalog-lost"
REASON_AMBIGUOUS_CATALOG = "ambiguous-catalog"
REASON_TARGET_DIR_EXISTS = "target-dir-exists"
REASON_TARGET_EXTERNAL_TREE_EXISTS = "target-external-tree-exists"
REASON_KEYRING_UNAVAILABLE = "keyring-unavailable"
REASON_SECRET_INPUTS_UNREADABLE = "secret-inputs-unreadable"
REASON_PLUGIN_ROOT_UNREADABLE = "plugin-root-unreadable"
REASON_SECRET_TARGET_CONFLICT = "secret-target-conflict"
REASON_VALUE_TARGET_CONFLICT = "value-target-conflict"
REASON_VALUE_STORE_CORRUPT = "value-store-corrupt"
REASON_RENAME_FAILED = "rename-failed"
REASON_LEDGER_REBUILD_FAILED = "ledger-rebuild-failed"
REASON_EXTRA_KNOWN_TARGET_CONFLICT = "extra-known-target-conflict"
REASON_NAME_TAKEN = "name-taken-by-other-source"
REASON_SECRET_READ_FAILED = "secret-read-failed"
REASON_PROJECT_PATH_MISSING = "project-path-missing"

# follow-up 效应码 / Follow-up effect codes（§9）.
EFFECT_WILL_BE_INACTIVE = "will-be-inactive"
EFFECT_STALE_TRUST = "stale-trust"
EFFECT_BLOCKED_INVALID = "blocked-reference-invalid"
EFFECT_DECLARATION_RESURRECT = "reconcile-will-resurrect"

# 退出码 / Exit codes（与 CLI 层的 0/1/3 对齐；模块层不依赖 CLI 以免反向 import）.
EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_PARTIAL = 3

# 冲突涉及项的通配符（进程内语义）/ Marker used when a conflict is command-wide.
_CONFLICT_ALL = "*"


class _ItemFailure(RuntimeError):
    """项级执行失败（被 :func:`execute_plan` 捕获，不跨项传播）/ Single-item failure."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SecretRekey:
    """一条 secret 重键计划 / One secret rekey plan."""

    old_key: str
    new_key: str
    value: str = field(repr=False)  # 明文 secret 绝不进 repr（print(plan) / 断言失败 dump）


@dataclass
class MigrationItem:
    """单个 marketplace 的迁移计划/结果 / One marketplace's migration plan & result."""

    old: str
    new: str | None = None
    action: str = ACTION_NOOP
    reason: str | None = None
    source: str | None = None
    plugins: list[str] = field(default_factory=list)
    actions: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    follow_ups: list[dict[str, Any]] = field(default_factory=list)
    selected: bool = True

    # ── 内部状态（不序列化）/ internal (not serialized) ──
    manifest: dict[str, Any] = field(default_factory=dict, repr=False)
    old_dir: Path | None = field(default=None, repr=False)
    old_source: object = field(default=None, repr=False)
    already_renamed: bool = False
    baseline_derived: bool = False
    merge: bool = False
    # 计划面
    installed_add: list[str] = field(default_factory=list)
    installed_remove: list[str] = field(default_factory=list)
    enabled_set: dict[str, bool] = field(default_factory=dict)
    enabled_remove: list[str] = field(default_factory=list)
    trust_rewrite: bool = False
    blocked_rewrite: bool = False
    declarations_rewrite: bool = False
    value_pairs: list[tuple[str, str, Any]] = field(default_factory=list)
    secret_pairs: list[SecretRekey] = field(default_factory=list, repr=False)
    secret_cleanup: list[SecretRekey] = field(default_factory=list, repr=False)
    ledger_rebuild: list[tuple[str, str, str | None]] = field(default_factory=list)
    declarations_migrated: bool = False
    # 迁移本身完成，但有插件未能重建（旧账本保留）→ 退出码 3，不报「全绿」。
    incomplete: bool = False

    def visible_actions(self) -> dict[str, Any]:
        """可展示的执行面计数（#268 单一权威）/ Execution-surface counters shown in reports.

        只有真正会写盘的动作（rename）或**已尝试执行**的 aborted 才展示；blocked / unresolved /
        not-selected 一律空，避免被误读为「会执行」。JSON 与人读表格共用本方法。
        """
        return dict(self.actions) if self.action in {ACTION_RENAME, ACTION_ABORTED} else {}

    def serializable(self) -> dict[str, Any]:
        """
        JSON 形态（§5.3）/ JSON payload（§5.3）.

        **所有自由文本走脱敏原语**（账本键可能被手编成携带凭据的串）：old/new 走 ``redact_ledger_key``，
        warning / suggested 走散文漏斗，路径走 ``sanitize_path_for_diagnostics``；指针在构建时已用
        脱敏名（见 :func:`_collect_follow_ups`）。
        """
        shown_old = redact_ledger_key(self.old)
        shown_new = redact_ledger_key(self.new) if self.new else None
        renaming = self.action == ACTION_RENAME and self.selected
        return {
            "old": shown_old,
            "new": shown_new,
            "action": self.action,
            "reason": self.reason,
            "source": self.source,
            "plugins": [
                {
                    "id": f"{plugin}@{shown_old}",
                    "newId": f"{plugin}@{shown_new}" if (renaming and shown_new) else None,
                    "action": "move" if renaming else "none",
                }
                for plugin in self.plugins
            ],
            "actions": self.visible_actions(),
            "warnings": [sanitize_text_for_diagnostics(w) for w in self.warnings],
            "followUps": [
                {
                    "file": sanitize_path_for_diagnostics(str(follow.get("file", ""))),
                    "pointer": sanitize_text_for_diagnostics(str(follow.get("pointer", ""))),
                    "effect": follow.get("effect"),
                    "suggested": sanitize_text_for_diagnostics(str(follow.get("suggested", ""))),
                }
                for follow in self.follow_ups
            ],
            "incomplete": self.incomplete,
        }


@dataclass
class MigrationPlan:
    """一次迁移的完整计划/报告 / Full plan & report for one migration run."""

    items: list[MigrationItem] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    corrupt: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    update_declarations: bool = False
    applied: bool = False
    apply_requested: bool = False
    cwd: Path | None = None


# ---------------------------------------------------------------------------
# 状态加载（strict 只读）/ Strict read-only state loading
# ---------------------------------------------------------------------------
@dataclass
class _State:
    """检测所需的全量只读快照 / Read-only snapshot used by detection."""

    known: store.KnownMarketplacesFile
    installed: store.InstalledPluginsFile
    user: dict[str, Any]
    layers: list[tuple[str, Path, dict[str, Any]]]
    values: dict[str, Any]
    values_path: Path
    corrupt: list[dict[str, str]]


def _empty_state() -> _State:
    return _State(
        known=store.empty_known_marketplaces(),
        installed=store.empty_installed_plugins(),
        user={},
        layers=[],
        values={},
        values_path=Path(),
        corrupt=[],
    )


def _corrupt_entry(path: Path, exc: BaseException) -> dict[str, str]:
    return {"path": str(path), "reason": str(exc)}


def _load_state(home: Path, env: Mapping[str, str] | None, cwd: Path | None) -> _State:
    """
    严格只读加载全部将被读/写的状态（D13）/ Strictly load every state file the migration may rewrite.

    任一文件损坏 → 收集到 ``corrupt``（命令级 abort、零写入），**绝不**走容错 loader（后者会 rename）。
    """
    state = _empty_state()
    try:
        state.known = store.load_known_marketplaces_strict(home, env)
    except SettingsFileCorruptError as exc:
        state.corrupt.append(_corrupt_entry(Path(exc.path), exc))
    try:
        state.installed = store.load_installed_plugins_strict(home, env)
    except SettingsFileCorruptError as exc:
        state.corrupt.append(_corrupt_entry(Path(exc.path), exc))

    user_path = user_settings_path(env)
    try:
        state.user = read_raw_object(user_path) or {}
    except SettingsFileCorruptError as exc:
        state.corrupt.append(_corrupt_entry(Path(exc.path), exc))

    anchors = {Path(cwd) if cwd is not None else Path.cwd()}
    for records in state.installed.get("plugins", {}).values():
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, Mapping):
                project_path = record.get("projectPath")
                if isinstance(project_path, str) and project_path:
                    anchors.add(Path(project_path))
    for anchor in sorted(anchors):
        for scope_name, path in (
            ("project", workdir_project_settings_path(anchor)),
            ("local", workdir_local_settings_path(anchor)),
        ):
            if not path.exists():
                continue
            try:
                state.layers.append((scope_name, path, read_raw_object(path) or {}))
            except SettingsFileCorruptError as exc:
                state.corrupt.append(_corrupt_entry(Path(exc.path), exc))

    state.values_path = resolve_value_store_path(env)
    try:
        raw_values = store.read_jsonc_strict(state.values_path)
    except SettingsFileCorruptError as exc:
        state.corrupt.append(_corrupt_entry(Path(exc.path), exc))
        raw_values = None
    state.values = dict(raw_values) if isinstance(raw_values, dict) else {}
    return state


# ---------------------------------------------------------------------------
# 引用与插件集 / References & plugin sets
# ---------------------------------------------------------------------------
def _pid_parts(pid: str) -> tuple[str, str] | None:
    """``<plugin>@<marketplace>`` → ``(plugin, marketplace)``；非法 → ``None``。"""
    if not isinstance(pid, str) or not is_valid_enabled_plugin_key(pid):
        return None
    plugin, _, marketplace = pid.partition("@")
    return plugin, marketplace


def _string_list(obj: Mapping[str, Any], key: str, *, path: Path) -> list[Any]:
    """容错取数组字段（形状不符抛 :class:`SettingsFileCorruptError`；元素不过滤）。"""
    return list_field_or_empty(obj, key, path=path)


def _string_map(obj: Mapping[str, Any], key: str, *, path: Path) -> dict[str, Any]:
    """容错取对象字段（形状不符抛 :class:`SettingsFileCorruptError`）。"""
    return mapping_field_or_empty(obj, key, path=path)


def _declared_pids(state: _State) -> set[str]:
    """全部 layer 的安装意图 pid 并集（读语义数组拼接去重）/ Union of every layer's install intents."""
    out: set[str] = set()
    layers = [state.user, *(data for _scope, _path, data in state.layers)]
    for data in layers:
        raw = data.get(FIELD_INSTALLED_PLUGINS)
        if isinstance(raw, list):
            out.update(item for item in raw if isinstance(item, str) and is_valid_enabled_plugin_key(item))
    return out


def _ledger_pids(state: _State) -> set[str]:
    return {pid for pid in state.installed.get("plugins", {}) if isinstance(pid, str) and is_valid_enabled_plugin_key(pid)}


def _user_enabled(state: _State, env: Mapping[str, str] | None) -> dict[str, bool]:
    """user 层 ``enabledPlugins``（形状不符抛错）/ User-layer enabled map."""
    return {
        key: value
        for key, value in _string_map(state.user, FIELD_ENABLED_PLUGINS, path=user_settings_path(env)).items()
        if isinstance(value, bool)
    }


def _user_string_array(state: _State, env: Mapping[str, str] | None, key: str) -> list[Any]:
    return _string_list(state.user, key, path=user_settings_path(env))


# ---------------------------------------------------------------------------
# 检测 / Detection
# ---------------------------------------------------------------------------
def _is_safe_path_segment(name: str) -> bool:
    """旧名能否安全地作为目录段（单一权威在 marketplace_identity）/ Whether the old name is safe."""
    return is_safe_registration_path_segment(name)


def _find_renamed_target(known: Mapping[str, Any], old: str, source: object, home: Path) -> list[str]:
    """同源且清单名 == 注册名的其它键（崩溃恢复的「已改名」判据）/ Same-source already-renamed keys."""
    matches: list[str] = []
    for name, record in known.items():
        if name == old or not isinstance(record, Mapping):
            continue
        if not _is_safe_path_segment(name):
            continue
        if not same_source(record.get("source"), source):
            continue
        clone_dir = marketplace_skill_dir(home, name)
        if not clone_dir.is_dir() or clone_dir.is_symlink():
            continue
        try:
            manifest_name = read_manifest_marketplace_name(clone_dir)
        except MarketplaceIdentityError:
            # 清单不可读时的次选证据：记录自身的 baseline 锚定在该名字上（baseline_derived 崩溃窗口）。
            if record.get(MANIFEST_NAME_BASELINE_FIELD) == name:
                matches.append(name)
            continue
        if manifest_name == name:
            matches.append(name)
    return sorted(matches)


def _classify(old: str, record: object, known: Mapping[str, Any], home: Path) -> MigrationItem:
    """把一条 ``known_marketplaces`` 注册归类（§3）/ Classify one registration."""
    item = MigrationItem(old=old)
    if not isinstance(record, Mapping):
        item.action = ACTION_UNRESOLVED
        item.reason = REASON_INVALID_RECORD
        return item
    source = record.get("source")
    item.old_source = source
    item.source = display_source(source)
    if not _is_safe_path_segment(old):
        item.action = ACTION_UNRESOLVED
        item.reason = REASON_UNSAFE_OLD_NAME
        return item

    old_dir = marketplace_skill_dir(home, old)
    item.old_dir = old_dir
    if old_dir.is_symlink():
        item.action = ACTION_UNRESOLVED
        item.reason = REASON_SYMLINK_CATALOG
        return item
    if not old_dir.is_dir():
        # P2 中断的反向窗口：内容已在同源的新名下、旧键还没删 → 收敛收尾（§7.3）。
        renamed = _find_renamed_target(known, old, source, home)
        if len(renamed) == 1:
            new_dir = marketplace_skill_dir(home, renamed[0])
            try:
                item.manifest = read_marketplace_manifest(new_dir)
            except Exception:  # noqa: BLE001 - 清单不可读 → 只能接受 baseline 次选证据
                item.manifest = {}
            baseline_evidence = isinstance(record.get(MANIFEST_NAME_BASELINE_FIELD), str) and (
                record.get(MANIFEST_NAME_BASELINE_FIELD) == renamed[0]
            )
            if item.manifest or baseline_evidence:
                item.new = renamed[0]
                item.action = ACTION_RENAME
                item.already_renamed = True
                if not item.manifest:
                    item.baseline_derived = True
                item.warnings.append(
                    f"directory already at {renamed[0]!r} but the old registration is still present; "
                    "the remaining references will be migrated (crash-recovery convergence)"
                )
                return item
        item.action = ACTION_UNRESOLVED
        item.reason = REASON_CATALOG_MISSING
        return item

    try:
        manifest = read_marketplace_manifest(old_dir)
    except Exception:  # noqa: BLE001 - 清单缺失/畸形统一归不可读
        # §3.4 次选判据：本地清单不可读但 baseline 存在且 ≠ 旧名 → 按 baseline 补齐 K→M（保守：只迁名，
        # 不依赖清单做重建；执行期以旧目录直搬并把重建标 incomplete）。
        baseline_fallback = record.get(MANIFEST_NAME_BASELINE_FIELD)
        if (
            isinstance(baseline_fallback, str)
            and baseline_fallback
            and baseline_fallback != old
            and is_valid_marketplace_name(baseline_fallback)
        ):
            item.new = baseline_fallback
            item.action = ACTION_RENAME
            item.baseline_derived = True
            item.manifest = {}
            item.warnings.append(
                "marketplace manifest is unreadable; using manifestNameBaseline as the secondary judgement "
                "(ledger rebuild will be skipped)"
            )
            return item
        item.action = ACTION_UNRESOLVED
        item.reason = REASON_MANIFEST_UNREADABLE
        return item
    try:
        manifest_name = manifest_marketplace_name(manifest)
    except MarketplaceIdentityError:
        item.action = ACTION_UNRESOLVED
        item.reason = REASON_INVALID_MANIFEST_NAME
        return item

    item.manifest = manifest
    baseline = record.get(MANIFEST_NAME_BASELINE_FIELD)
    if isinstance(baseline, str) and baseline and baseline != manifest_name:
        # #260 §5.14：refresh 本就拒绝的改名，迁移不得隐式接受。
        item.action = ACTION_BLOCKED
        item.reason = REASON_BASELINE_MISMATCH
        return item
    if manifest_name == old:
        item.action = ACTION_NOOP
        return item
    if not is_valid_marketplace_name(manifest_name):  # manifest_marketplace_name 已校验，防御纵深
        item.action = ACTION_UNRESOLVED
        item.reason = REASON_INVALID_MANIFEST_NAME
        return item
    item.new = manifest_name
    item.action = ACTION_RENAME
    return item


def _pid_plugin(pid: str) -> str | None:
    parts = _pid_parts(pid)
    return parts[0] if parts else None


def _pid_marketplace(pid: str) -> str | None:
    parts = _pid_parts(pid)
    return parts[1] if parts else None


def _pids_for(pids: Iterable[str], marketplace: str) -> list[str]:
    """筛出 ``@<marketplace>`` 结尾的合法 pid（升序）/ Valid pids belonging to one marketplace."""
    return sorted(pid for pid in pids if _pid_marketplace(pid) == marketplace)


def _external_tree_count(home: Path, item: MigrationItem) -> int:
    """外树项数（merge 且目标树已在 → 0，实际不搬）/ Number of external plugin trees to move."""
    if item.merge and external_plugins_dir(home, item.new or "").is_dir():
        return 0
    old_ext = external_plugins_dir(home, item.old)
    if not old_ext.is_dir():
        return 0
    return sum(1 for child in old_ext.iterdir() if child.is_dir())


def _attach_references(item: MigrationItem, state: _State, env: Mapping[str, str] | None, home: Path) -> None:
    """计算一条迁移的引用面（pid / 启用键 / trust / blocked / 声明）/ Compute its reference surfaces."""
    declared = _declared_pids(state)
    ledger = _ledger_pids(state)
    pids = set(_pids_for(declared | ledger, item.old))
    item.plugins = sorted({plugin for plugin in (_pid_plugin(pid) for pid in pids) if plugin})

    user_installed = _pids_for(_declared_pids_of_layer(state.user), item.old)
    user_enabled = _user_enabled(state, env)
    item.installed_add = sorted(f"{_pid_plugin(pid)}@{item.new}" for pid in user_installed if _pid_plugin(pid))
    item.installed_remove = sorted(user_installed)
    item.enabled_set = {
        f"{_pid_plugin(pid)}@{item.new}": value
        for pid, value in user_enabled.items()
        if _pid_marketplace(pid) == item.old and _pid_plugin(pid)
    }
    item.enabled_remove = sorted(pid for pid in user_enabled if _pid_marketplace(pid) == item.old)
    item.actions = {
        "installedPlugins": len(item.installed_add),
        "enabledPlugins": len(item.enabled_set),
        "trustedMarketplaces": "add+remove" if item.trust_rewrite else "noop",
        "blockedMarketplaces": "add+remove" if item.blocked_rewrite else "noop",
        "extraKnownMarketplaces": "report",
        "inputValues": 0,
        "secrets": 0,
        "catalog": "rename",
        "externalTrees": 0,  # 冲突检测后统一计算（merge 与否在 attach 时未知）
        "ledger": "rebuild" if _pids_for(ledger, item.old) else "noop",
    }


def _declared_pids_of_layer(obj: Mapping[str, Any]) -> set[str]:
    raw = obj.get(FIELD_INSTALLED_PLUGINS)
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str) and is_valid_enabled_plugin_key(item)}


def _attach_scope_surfaces(item: MigrationItem, state: _State, env: Mapping[str, str] | None, update_declarations: bool) -> None:
    """trust / blocked / 声明的改写面（仅 user scope）/ User-scope trust/blocked/declaration surfaces."""
    user_path = user_settings_path(env)
    trusted = _user_string_array(state, env, FIELD_TRUSTED_MARKETPLACES)
    blocked = _user_string_array(state, env, FIELD_BLOCKED_MARKETPLACES)
    item.trust_rewrite = any(entry == item.old for entry in trusted)
    item.blocked_rewrite = any(entry == item.old for entry in blocked)
    declarations = _string_map(state.user, FIELD_EXTRA_KNOWN_MARKETPLACES, path=user_path)
    declared_old = declarations.get(item.old)
    item.declarations_rewrite = bool(update_declarations and isinstance(declared_old, Mapping))
    if update_declarations and item.old in declarations and not isinstance(declared_old, Mapping):
        # 手编声明不是对象：既不迁移也不删除（无声删除用户声明是不可接受的）。
        item.warnings.append(
            f"extraKnownMarketplaces[{item.old!r}] is not an object; the declaration is left untouched"
        )


# ---------------------------------------------------------------------------
# 冲突检测（§5.1）/ Conflict detection
# ---------------------------------------------------------------------------
def _conflict(code: str, *, names: Sequence[str], target: str, involves: Sequence[str]) -> dict[str, Any]:
    return {"code": code, "names": list(names), "target": target, "involves": list(involves)}


def _merge_scenario(item: MigrationItem, known: Mapping[str, Any], home: Path) -> bool:
    """目标名已由**同源**注册且目录内容一致 → 合并场景（不是冲突）/ Same-source merge scenario."""
    existing = known.get(item.new) if item.new else None
    old_record = known.get(item.old)
    if not isinstance(existing, Mapping) or not isinstance(old_record, Mapping):
        return False
    if not same_source(existing.get("source"), old_record.get("source")):
        return False
    new_dir = marketplace_skill_dir(home, item.new or "")
    if not new_dir.is_dir() or new_dir.is_symlink():
        return False
    try:
        return read_manifest_marketplace_name(new_dir) == item.new
    except MarketplaceIdentityError:
        return False


def _detect_conflicts(items: list[MigrationItem], state: _State, home: Path, env: Mapping[str, str] | None) -> list[dict[str, Any]]:
    """C1–C9 预检（全量 legacy 集上判定，不看 ``--name`` 过滤）/ Plan-level conflicts over the full set."""
    conflicts: list[dict[str, Any]] = []
    renames = [item for item in items if item.action == ACTION_RENAME and item.new]

    by_target: dict[str, list[str]] = {}
    for item in renames:
        by_target.setdefault(item.new or "", []).append(item.old)
    for target, olds in by_target.items():
        if len(olds) > 1:
            conflicts.append(_conflict("duplicate-target", names=sorted(olds), target=target, involves=sorted(olds)))

    declared = _declared_pids(state)
    ledger = _ledger_pids(state)
    user_enabled = _user_enabled(state, env)
    scan_pids = declared | ledger
    source_pids = {f"{plugin}@{item.old}" for item in renames for plugin in item.plugins}
    candidates = scan_pids - source_pids

    extra_declared: dict[str, Any] = {}
    if state.user:
        try:
            extra_declared = _string_map(state.user, FIELD_EXTRA_KNOWN_MARKETPLACES, path=user_settings_path(env))
        except SettingsFileCorruptError:
            extra_declared = {}

    for item in renames:
        new = item.new or ""
        existing = state.known["marketplaces"].get(new)
        old_record = state.known["marketplaces"].get(item.old)
        old_source = old_record.get("source") if isinstance(old_record, Mapping) else None
        if existing is not None and not same_source(existing.get("source") if isinstance(existing, Mapping) else None, old_source):
            conflicts.append(_conflict("name-taken-by-other-source", names=[item.old, new], target=new, involves=[item.old]))
        elif existing is not None and _merge_scenario(item, state.known["marketplaces"], home):
            item.merge = True
            item.warnings.append(f"target name {new!r} is already registered from the same source; merging references")
        elif item.already_renamed and item.baseline_derived and existing is not None:
            # 清单不可读 → _merge_scenario 跑不了；§3.4 证据已确认「同源 + 目录已在新名」→ 视为 merge。
            item.merge = True

        new_dir = marketplace_skill_dir(home, new)
        if new_dir.exists() or new_dir.is_symlink():
            if not item.merge:
                conflicts.append(_conflict("target-dir-exists", names=[item.old, new], target=new, involves=[item.old]))
        external_new = external_plugins_dir(home, new)
        external_old = external_plugins_dir(home, item.old)
        if external_new.exists() and (not item.merge or external_old.exists()):
            # merge 场景下两棵外树同时存在无法合并（rename 不能覆盖）→ 计划期就 abort，避免
            # dry-run exit 0 / apply exit 3 的永久 wedge。
            conflicts.append(
                _conflict("target-external-tree-exists", names=[item.old, new], target=new, involves=[item.old])
            )

        for plugin in item.plugins:
            old_pid = f"{plugin}@{item.old}"
            new_pid = f"{plugin}@{new}"
            if old_pid in user_enabled and new_pid in user_enabled and user_enabled[old_pid] != user_enabled[new_pid]:
                conflicts.append(_conflict("enabled-state-conflict", names=[old_pid, new_pid], target=new_pid, involves=[item.old]))
            clashes = _bare_name_clash_pids(new_pid, candidates)
            if clashes:
                conflicts.append(_conflict("bare-name-clash", names=[new_pid, *clashes], target=new_pid, involves=[item.old]))

        if item.declarations_rewrite:
            old_decl = extra_declared.get(item.old)
            new_decl = extra_declared.get(new)
            if new_decl is not None and not isinstance(new_decl, Mapping):
                # 目标键存在但不是对象 → 不覆盖手编内容，计划期 abort。
                conflicts.append(
                    _conflict("extra-known-target-conflict", names=[item.old, new], target=new, involves=[item.old])
                )
            elif isinstance(new_decl, Mapping) and isinstance(old_decl, Mapping):
                if not same_source(new_decl.get("source"), old_decl.get("source")):
                    conflicts.append(
                        _conflict("extra-known-target-conflict", names=[item.old, new], target=new, involves=[item.old])
                    )
            elif isinstance(new_decl, Mapping) and old_decl is None:
                item.declarations_rewrite = False  # 无旧声明可迁：不新建
    return conflicts


def blocking_conflicts(plan: MigrationPlan) -> list[dict[str, Any]]:
    """涉及任一选中项的冲突（整体 abort 的判据）/ Conflicts involving a selected item."""
    selected = {item.old for item in plan.items if item.selected}
    return [
        conflict
        for conflict in plan.conflicts
        if any(name in selected for name in conflict.get("involves", []))
    ]


# ---------------------------------------------------------------------------
# 项级预检（P0.5，零写入）/ Per-item preflight
# ---------------------------------------------------------------------------
def _plugin_root_offline(item: MigrationItem, plugin: str, home: Path, catalog_dir: Path) -> Path | None:
    """按清单条目离线定位 plugin 根（不 clone）；无法定位 → ``None``。"""
    entry = find_plugin_entry(item.manifest, plugin)
    if entry is None:
        return None
    try:
        resolved = resolve_plugin_source(entry.get("source"), plugin_root=plugin_root_base(item.manifest))
    except Exception:  # noqa: BLE001 - source 畸形 → 视为不可定位（候选派生失败交由调用方处置）
        return None
    if isinstance(resolved, LocalPluginSource):
        root = (catalog_dir / resolved.rel_path).resolve()
        return root if is_within(root, catalog_dir.resolve()) else None
    if isinstance(resolved, GitCloneSpec):
        # 崩溃恢复窗口 4：catalog 已搬、外树也已搬（`_rename_external` 之后被杀）时，根在 `<new>` 下；
        # 反之在 `<old>` 下。两处都探测，取实际存在的那个（都不存在则返回旧路径，由调用方判 blocked/skip）。
        roots = [external_plugin_dir(home, item.old, plugin)]
        if item.new:
            roots.append(external_plugin_dir(home, item.new, plugin))
        home_root = home.resolve()

        def _within_home(candidate: Path) -> Path | None:
            # 纵深防御（#309 🔴N1）：名字与 subdir 都源自不可信 manifest，解析后必须仍在 home 内。
            resolved_candidate = candidate.resolve()
            return resolved_candidate if is_within(resolved_candidate, home_root) else None

        for base in roots:
            root = base / resolved.subdir if resolved.subdir else base
            if not root.is_dir():
                continue
            inside = _within_home(root)
            if inside is not None:
                return inside
        fallback = roots[0] / resolved.subdir if resolved.subdir else roots[0]
        return _within_home(fallback)
    return None


_BOOL_ADAPTER: TypeAdapter[bool] = TypeAdapter(bool)


def _raw_password_hint(entry: object) -> bool:
    """
    原始条目是否**可能**声明了 password（与运行时 pydantic 同口径）/ Whether it may declare a password.

    ``"on"`` / ``"y"`` / ``1.0`` 等运行时会被解析为 True 的形态也算「可能」；**无法解析**（dict/list 等）
    同样按「可能」处理——宁可保守 abort，也不能把可能是密钥的条目当非密钥跳过。
    """
    if not isinstance(entry, Mapping) or "password" not in entry:
        return False
    value = entry["password"]
    if value is None:
        return False
    try:
        return bool(_BOOL_ADAPTER.validate_python(value))
    except ValidationError:
        return True


def _read_input_defs(root: Path) -> list[dict[str, Any]] | None:
    """
    读 plugin 的 ``mcp-servers/inputs.json``（逐条校验）/ Read & per-entry validate input defs.

    缺失 → ``[]``；**可证明不可能承载 secret** 的畸形条目（``pickString`` / ``command``，或未声明
    password 的 ``promptString``）按运行时 loader 同姿态跳过；只有「可能声明了 password / 类型未知」的
    畸形条目才返回 ``None``（整文件视为不可读 → 已装 plugin 保守 abort）。文件不可读 / JSON 畸形 /
    ``inputs`` 非数组 → ``None``。
    """
    path = root / MCP_SERVERS_SUBDIR / MCP_INPUTS_FILENAME
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    defs = data.get("inputs", []) if isinstance(data, Mapping) else data
    if not isinstance(defs, list):
        return None
    out: list[dict[str, Any]] = []
    adapter: TypeAdapter[MCPServerInput] = TypeAdapter(MCPServerInput)
    for entry in defs:
        try:
            parsed = adapter.validate_python(entry)
        except ValidationError:
            if _raw_password_hint(entry):
                return None
            raw_type = entry.get("type") if isinstance(entry, Mapping) else None
            # `type` 来自不可信 manifest，可能是 list/dict（不可哈希）——先判类型再比对，绝不直接放集合里。
            if raw_type is None or (isinstance(raw_type, str) and raw_type in {"pickString", "command", "promptString"}):
                continue  # 可证明非密钥（password 只有 promptString 有；promptString 无 password 提示）
            return None
        is_password = isinstance(parsed, MCPServerPromptStringInput) and bool(parsed.password)
        out.append({"id": parsed.id, "password": is_password})
    return out


def _candidate_inputs(item: MigrationItem, state: _State, home: Path) -> tuple[list[tuple[str, str, bool]], str | None]:
    """
    派生 input 候选（D14）/ Derive input candidates from manifest + ledger roots.

    **D14 carve-out（#301 裁决）**：**不在新清单**的 plugin（含 ``baseline_derived`` 下全部账本 plugin）
    没有 rename 目标 ⇒ 不枚举、不重键、不阻断——其旧 input 值 / secret 原样保留（旧键在改名后成为孤儿，
    但不会被任何新 pid 读取），F10 全量适用。plugin **已装（账本有记录）且仍在新清单**但根 /
    ``inputs.json`` 不可读时 D14 原样 fail-closed（无法证明无 password input ⇒ 整项 abort、零写入）；
    **未安装**的清单内 plugin 不可定位只告警、不阻断。

    :return: ``(candidates, blocked_reason)``；``blocked_reason`` 非空 → 该项 abort/blocked（零写入）。
    """
    if item.already_renamed and item.new:
        catalog_dir = marketplace_skill_dir(home, item.new)
    else:
        catalog_dir = item.old_dir or marketplace_skill_dir(home, item.old)
    ledger = state.installed.get("plugins", {})
    ledger_plugins = {
        plugin
        for pid in ledger
        if isinstance(pid, str) and _pid_marketplace(pid) == item.old and (plugin := _pid_plugin(pid))
    }
    # 枚举集：**strip 后**仍须是合法 plugin 名（严格 kebab）。`find_plugin_entry` 按 strip 匹配，故带空白的
    # 合法条目按在清单处理；**任意非空串**（绝对路径 / `../`）不得进入枚举——否则 `_plugin_root_offline`
    # 的 GitCloneSpec 分支会把不可信名拼进 `external_plugin_dir` 并读到 home 外（#272 路径穿越不变量，
    # #309 🔴N1）。「在清单」的最终判定仍由 `find_plugin_entry` 负责（carve-out 侧同口径）。
    manifest_plugins: set[str] = set()
    for entry in iter_plugin_entries(item.manifest):
        name = entry.get("name")
        if isinstance(name, str) and is_valid_marketplace_name(name.strip()):
            manifest_plugins.add(name.strip())
        elif name is not None:
            # 空名 / 非字符串名 / 非法名不可信：只给固定文案，不回显原值（与 staging 的 entry name 校验同口径）。
            item.warnings.append("a plugin entry in the new manifest has an invalid name; skipped for inputs")
    candidates: list[tuple[str, str, bool]] = []

    def _collect(plugin: str, root: Path) -> str | None:
        if not root.is_dir():
            return REASON_PLUGIN_ROOT_UNREADABLE if plugin in ledger_plugins else None
        defs = _read_input_defs(root)
        if defs is None:
            item.warnings.append(
                f"plugin {plugin!r} has unreadable or ambiguous input definitions at "
                f"{sanitize_path_for_diagnostics(str(root / MCP_SERVERS_SUBDIR / MCP_INPUTS_FILENAME))}; "
                "cannot prove it has no secret"
            )
            return REASON_SECRET_INPUTS_UNREADABLE if plugin in ledger_plugins else None
        for definition in defs:
            candidates.append((plugin, definition["id"], bool(definition["password"])))
        return None

    for plugin in sorted(p for p in manifest_plugins if isinstance(p, str)):
        root = _plugin_root_offline(item, plugin, home, catalog_dir)
        if root is None:
            # 相对源越界 / source 畸形：清单内条目不可定位。已安装 → 保守 abort；未安装 → 不计候选。
            if plugin in ledger_plugins:
                return [], REASON_PLUGIN_ROOT_UNREADABLE
            item.warnings.append(f"plugin {plugin!r} in the new manifest has no resolvable local root; skipped for inputs")
            continue
        blocked = _collect(plugin, root)
        if blocked:
            return [], blocked

    # D14 carve-out（#301 裁决）：不在新清单的 plugin 没有 rename 目标 ⇒ 不枚举、不重键、不阻断。
    # 只登记一条诊断（幂等），其旧 input 值 / secret 原样保留。
    for pid in ledger:
        parts = _pid_parts(pid) if isinstance(pid, str) else None
        if not parts or parts[1] != item.old:
            continue
        if find_plugin_entry(item.manifest, parts[0]) is not None:
            continue
        message = (
            f"plugin {parts[0]!r} is not present in the new manifest; its plugin-scoped input values/secrets "
            "are left unchanged"
        )
        if message not in item.warnings:
            item.warnings.append(message)
    return candidates, None


def _map_install_path(path: Path, home: Path, old: str, new: str) -> Path:
    """把旧名根下的 installPath 映射到新名根（目录搬迁后的真实位置）/ Map an installPath across the rename."""
    mappings = (
        (marketplace_skill_dir(home, old).resolve(), marketplace_skill_dir(home, new).resolve()),
        (external_plugins_dir(home, old).resolve(), external_plugins_dir(home, new).resolve()),
    )
    resolved = path.resolve()
    for old_root, new_root in mappings:
        try:
            return new_root / resolved.relative_to(old_root)
        except ValueError:
            continue
    return path


def _item_preflight(item: MigrationItem, state: _State, home: Path, env: Mapping[str, str] | None, store_secrets: SecretStore) -> None:
    """P0.5：secret 候选派生 / keyring 可用性 / 目标键冲突（全部零写入）。"""
    if item.action != ACTION_RENAME or not item.new:
        return
    # project/local 记录缺合法 projectPath 无法安全重建：**预检期** blocked（dry-run 即 exit 3、零写入），
    # 而不是等到 P2 才 abort（那时 known[new]/目录已写，成半迁移）。
    # 窄对齐（#301 item 1）：只有 `_rebuild_ledger` **真会重建**的记录才阻断——plugin 已移出新清单 /
    # baseline_derived（manifest 未知）/ 记录缺 installPath 时 P2 只保留旧账本（F10），不得把整个
    # marketplace 阻断；判据与 P2 共用 `_record_needs_project_path`，两侧不可漂移。
    # 所有「保留旧账本」诊断先只**登记**，等所有会整项阻断的预检通过后再落：D14 的 secret 候选派生
    # 仍可能整项 blocked（无法证明无 secret）、projectPath 预检也可能 blocked——那时并未保留任何东西，
    # 报告里不能留下自相矛盾的「keeping」文案（#309 🟡2）。
    kept_plugins: list[str] = []
    kept_notes: list[str | None] = []
    has_ledger_records = False
    for pid, records in state.installed.get("plugins", {}).items():
        parts = _pid_parts(pid) if isinstance(pid, str) else None
        if not parts or parts[1] != item.old:
            continue
        if records:
            has_ledger_records = True
        # 以下判据与 `_rebuild_ledger` **逐条同构**（#301 裁决②）：静态可知的「保留旧账本」形态在计划期
        # 即置 incomplete，使 dry-run 退出码 == apply（§12.2）。目录存在性属运行期事实，不在此列。
        if not isinstance(records, list):
            kept_notes.append(f"plugin {parts[0]!r} ledger records are not an array; keeping them untouched")
            continue
        if find_plugin_entry(item.manifest, parts[0]) is None:
            kept_plugins.append(parts[0])
            continue
        for record in records:
            if not isinstance(record, Mapping):
                kept_notes.append(None)
                continue
            if _ledger_install_path(record) is None:
                kept_notes.append(f"plugin {parts[0]!r} ledger record has no installPath; keeping it untouched")
                continue
            if _record_needs_project_path(record):
                item.action = ACTION_BLOCKED
                item.reason = REASON_PROJECT_PATH_MISSING
                item.warnings.append(
                    f"plugin {parts[0]!r} has a {_norm_ledger_scope(record)} ledger record without a valid projectPath; "
                    "fix or remove that record before migrating"
                )
                return

    candidates, blocked = _candidate_inputs(item, state, home)
    if blocked:
        item.action = ACTION_BLOCKED
        item.reason = blocked
        return

    values = state.values
    accounts: dict[str, int] = {"inputValues": 0, "secrets": 0}
    secret_candidates: list[tuple[str, str]] = []
    for plugin, input_id, is_password in candidates:
        old_key = prefix_input_id(plugin, item.old, input_id)
        new_key = prefix_input_id(plugin, item.new, input_id)
        if is_password:
            secret_candidates.append((old_key, new_key))
            continue
        if old_key not in values:
            continue
        old_value = values[old_key]
        if new_key in values and values[new_key] != old_value:
            item.action = ACTION_BLOCKED
            item.reason = REASON_VALUE_TARGET_CONFLICT
            item.warnings.append(f"target input value key {new_key!r} already exists with a different value")
            return
        # 无论目标键是否已存在（同值 = 已迁移）都记录 pair：P1 复检、P3 删旧前再验证。
        item.value_pairs.append((old_key, new_key, old_value))
        accounts["inputValues"] += 1

    if secret_candidates:
        if not keyring_available():
            item.action = ACTION_BLOCKED
            item.reason = REASON_KEYRING_UNAVAILABLE
            item.warnings.append(
                "keyring is unavailable and this marketplace has password inputs; cannot prove no old secret exists"
            )
            return
        for old_key, new_key in secret_candidates:
            try:
                value = store_secrets.get_strict(old_key)
            except SecretStoreError as exc:
                item.action = ACTION_BLOCKED
                item.reason = REASON_SECRET_READ_FAILED
                item.warnings.append(f"cannot read secret {old_key!r}: {exc}")
                return
            if value is None:
                continue
            try:
                existing = store_secrets.get_strict(new_key)
            except SecretStoreError as exc:
                item.action = ACTION_BLOCKED
                item.reason = REASON_SECRET_READ_FAILED
                item.warnings.append(f"cannot read target secret {new_key!r}: {exc}")
                return
            if existing is not None and existing != value:
                item.action = ACTION_BLOCKED
                item.reason = REASON_SECRET_TARGET_CONFLICT
                item.warnings.append(f"target secret key {new_key!r} already exists with a different value")
                return
            item.secret_pairs.append(SecretRekey(old_key=old_key, new_key=new_key, value=value))
            accounts["secrets"] += 1

    for plugin in kept_plugins:
        _note_kept_ledger_record(item, plugin)
    for message in kept_notes:
        _note_ledger_record_kept(item, message)
    # 账本有记录、旧注册 source 非映射、且没有可合并的 known[new] 映射记录 ⇒ P2 的 `materialize_plugin`
    # 必因 source 形态失败并保留旧记录（#309 🟡3 裁决）：计划期即预告，dry-run 与 apply 同判。
    if (
        has_ledger_records
        and not isinstance(item.old_source, Mapping)
        and not isinstance(state.known.get("marketplaces", {}).get(item.new or ""), Mapping)
    ):
        _note_ledger_record_kept(
            item,
            "registration source is not an object; plugin ledger records cannot be re-materialized",
        )
    item.actions.update(accounts)
    if item.already_renamed:
        item.actions["catalog"] = "noop"


# ---------------------------------------------------------------------------
# 报告面（project/local、声明、env）/ Report-only surfaces
# ---------------------------------------------------------------------------
def _collect_follow_ups(item: MigrationItem, state: _State, env: Mapping[str, str] | None, update_declarations: bool) -> None:
    """把 project/local 的旧名引用与 user 未迁移声明写进 followUps（§9）。"""
    if item.action != ACTION_RENAME or not item.new:
        return
    for scope_name, path, data in state.layers:
        try:
            enabled = _string_map(data, FIELD_ENABLED_PLUGINS, path=path)
        except SettingsFileCorruptError:
            enabled = {}
        for pid, value in enabled.items():
            parts = _pid_parts(pid) if isinstance(pid, str) else None
            if parts and parts[1] == item.old:
                new_pid = f"{parts[0]}@{item.new}"
                effect = EFFECT_WILL_BE_INACTIVE if value else "will-stay-disabled"
                item.follow_ups.append(
                    {
                        "file": str(path),
                        "pointer": f"/{FIELD_ENABLED_PLUGINS}/{pid}",
                        "effect": effect,
                        "suggested": f"add \"{FIELD_ENABLED_PLUGINS}\": {{\"{new_pid}\": {str(value).lower()}}} (scope: {scope_name})",
                    }
                )
        for key, effect in (
            (FIELD_TRUSTED_MARKETPLACES, EFFECT_STALE_TRUST),
            (FIELD_BLOCKED_MARKETPLACES, EFFECT_BLOCKED_INVALID),
        ):
            try:
                members = _string_list(data, key, path=path)
            except SettingsFileCorruptError:
                members = []
            if any(entry == item.old for entry in members):
                item.follow_ups.append(
                    {
                        "file": str(path),
                        "pointer": f"/{key}/{item.old}",
                        "effect": effect,
                        "suggested": f"replace {item.old!r} with {item.new!r} in {key} (scope: {scope_name})",
                    }
                )
        for key in (FIELD_INSTALLED_PLUGINS,):
            try:
                members = _string_list(data, key, path=path)
            except SettingsFileCorruptError:
                members = []
            for pid in members:
                parts = _pid_parts(pid) if isinstance(pid, str) else None
                if parts and parts[1] == item.old:
                    item.follow_ups.append(
                        {
                            "file": str(path),
                            "pointer": f"/{key}/{pid}",
                            "effect": EFFECT_WILL_BE_INACTIVE,
                            "suggested": f"replace {pid!r} with {parts[0]}@{item.new} (scope: {scope_name})",
                        }
                    )
        try:
            declarations = _string_map(data, FIELD_EXTRA_KNOWN_MARKETPLACES, path=path)
        except SettingsFileCorruptError:
            declarations = {}
        if item.old in declarations:
            item.follow_ups.append(
                {
                    "file": str(path),
                    "pointer": f"/{FIELD_EXTRA_KNOWN_MARKETPLACES}/{item.old}",
                    "effect": EFFECT_DECLARATION_RESURRECT,
                    "suggested": f"rename the declaration key {item.old!r} → {item.new!r} (scope: {scope_name})",
                }
            )

    user_path = user_settings_path(env)
    try:
        user_declarations = _string_map(state.user, FIELD_EXTRA_KNOWN_MARKETPLACES, path=user_path)
    except SettingsFileCorruptError:
        user_declarations = {}
    if item.old in user_declarations and not (update_declarations and item.declarations_rewrite):
        item.follow_ups.append(
            {
                "file": str(user_path),
                "pointer": f"/{FIELD_EXTRA_KNOWN_MARKETPLACES}/{item.old}",
                "effect": EFFECT_DECLARATION_RESURRECT,
                "suggested": f"rename the declaration key {item.old!r} → {item.new!r}, or rerun with --update-declarations",
            }
        )
    item.actions["extraKnownMarketplaces"] = "rewrite(user)" if item.declarations_rewrite else "report"


# ---------------------------------------------------------------------------
# 计划构建 / Plan building
# ---------------------------------------------------------------------------
def build_plan(
    home: Path,
    env: Mapping[str, str] | None = None,
    *,
    names: Sequence[str] | None = None,
    update_declarations: bool = False,
    cwd: Path | None = None,
) -> MigrationPlan:
    """严格只读地构建迁移计划（dry-run 与 apply 共用；apply 前会重新调用）/ Build the plan strictly read-only."""
    state = _load_state(home, env, cwd)
    plan = MigrationPlan(update_declarations=update_declarations, cwd=cwd)
    plan.corrupt = state.corrupt
    if state.corrupt:
        return plan

    known_map = state.known["marketplaces"]
    items = [_classify(old, record, known_map, home) for old, record in sorted(known_map.items())]
    plan.items = items
    try:
        for item in items:
            if item.action == ACTION_RENAME:
                # 顺序承重：actions 的 trust/blocked 列由 rewrite 旗派生，必须先算 scope 面。
                _attach_scope_surfaces(item, state, env, update_declarations)
                _attach_references(item, state, env, home)
        plan.conflicts = _detect_conflicts(items, state, home, env)
        for item in items:
            if item.action != ACTION_RENAME:
                continue
            if item.merge:
                item.actions["catalog"] = "noop"
            item.actions["externalTrees"] = _external_tree_count(home, item)
    except SettingsFileCorruptError as exc:
        plan.corrupt.append(_corrupt_entry(Path(exc.path), exc))
        return plan

    known_names = set(known_map)
    if names:
        unknown = sorted({name for name in names if name not in known_names})
        if unknown:
            plan.errors.append(
                "unknown marketplace name(s) for --name: "
                + ", ".join(sanitize_text_for_diagnostics(repr(name)) for name in unknown)
            )
        selected = {name for name in names if name in known_names}
    else:
        selected = known_names
    for item in items:
        item.selected = item.old in selected
        if not item.selected and item.action != ACTION_NOOP:
            item.action = ACTION_NOT_SELECTED

    store_secrets = SecretStore()
    try:
        for item in items:
            if item.selected and item.action == ACTION_RENAME:
                _precheck_claim(item, known_map, home)
                if item.action == ACTION_RENAME:
                    _item_preflight(item, state, home, env, store_secrets)
                    _collect_follow_ups(item, state, env, update_declarations)
    except SettingsFileCorruptError as exc:
        plan.corrupt.append(_corrupt_entry(Path(exc.path), exc))
        return plan
    return plan


def plan_exit_code(plan: MigrationPlan) -> int:
    """计划/结果对应的退出码（0/1/3）/ Exit code for the plan or the post-apply result."""
    if plan.corrupt:
        return EXIT_PARTIAL
    if plan.errors or blocking_conflicts(plan):
        return EXIT_CONFLICT
    if any(item.selected and item.action in {ACTION_UNRESOLVED, ACTION_BLOCKED, ACTION_ABORTED} for item in plan.items):
        return EXIT_PARTIAL
    if any(item.selected and item.incomplete for item in plan.items):
        # 迁移完成但插件未重建（旧账本保留）：不能报「全绿」。
        return EXIT_PARTIAL
    if any(item.selected and item.follow_ups for item in plan.items):
        return EXIT_PARTIAL
    return EXIT_OK


def plan_payload(plan: MigrationPlan) -> dict[str, Any]:
    """JSON 输出形态（§5.3）/ JSON payload（§5.3）."""
    exit_code = plan_exit_code(plan)
    if plan.corrupt or blocking_conflicts(plan) or plan.errors:
        status = "aborted"
    elif plan.applied:
        status = "partial" if exit_code == EXIT_PARTIAL else "applied"
    else:
        status = "planned"
    return {
        "command": "marketplace migrate",
        "dryRun": not plan.apply_requested,
        "applyRequested": plan.apply_requested,
        "applied": plan.applied,
        "status": status,
        "exitCode": exit_code,
        "updateDeclarations": plan.update_declarations,
        "items": [item.serializable() for item in plan.items],
        "conflicts": [
            {
                "code": conflict.get("code"),
                "names": [redact_ledger_key(name) for name in conflict.get("names", [])],
                "target": redact_ledger_key(conflict.get("target", "")),
            }
            for conflict in plan.conflicts
        ],
        "corrupt": [
            {
                "path": sanitize_path_for_diagnostics(str(entry.get("path", ""))),
                "reason": sanitize_text_for_diagnostics(str(entry.get("reason", ""))),
            }
            for entry in plan.corrupt
        ],
        "errors": [sanitize_text_for_diagnostics(message) for message in plan.errors],
    }


# ---------------------------------------------------------------------------
# 执行 / Execution（P1 → P2 → P3 → P4）
# ---------------------------------------------------------------------------
def _update_user_scope(
    env: Mapping[str, str] | None,
    mutator: Callable[[dict[str, Any], Path], dict[str, Any]],
) -> None:
    """user settings 单次 raw RMW（raw 保真、形状不符抛错；无变更则不写）/ One raw RMW over user settings."""
    path = user_settings_path(env)
    with store.file_lock(path):
        existing = read_raw_object(path) or {}
        updates = mutator(existing, path)
        if not updates:
            return
        store.atomic_write_json(path, apply_write(existing, updates))


def _add_user_references(item: MigrationItem, env: Mapping[str, str] | None) -> None:
    """P1：user 侧 trust / blocked / 声明**加新名**（旧名保持）/ Additive user-scope references."""

    new_name = item.new
    if not new_name:
        raise _ItemFailure(REASON_INVALID_RECORD, "internal: rename item without a target name")

    def _mutate(existing: dict[str, Any], path: Path) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if item.trust_rewrite:
            trusted = _string_list(existing, FIELD_TRUSTED_MARKETPLACES, path=path)
            if item.new not in trusted:
                updates[FIELD_TRUSTED_MARKETPLACES] = [*trusted, item.new]
        if item.blocked_rewrite:
            blocked = _string_list(existing, FIELD_BLOCKED_MARKETPLACES, path=path)
            if item.new not in blocked:
                updates[FIELD_BLOCKED_MARKETPLACES] = [*blocked, item.new]
        if item.declarations_rewrite:
            declarations = _string_map(existing, FIELD_EXTRA_KNOWN_MARKETPLACES, path=path)
            old_entry = declarations.get(item.old)
            if isinstance(old_entry, Mapping):
                current = declarations.get(new_name)
                if current is None:
                    updates[FIELD_EXTRA_KNOWN_MARKETPLACES] = {new_name: dict(old_entry)}
                    item.declarations_migrated = True
                elif not isinstance(current, Mapping):
                    raise _ItemFailure(REASON_EXTRA_KNOWN_TARGET_CONFLICT, "extraKnownMarketplaces target is not an object")
                elif not same_source(current.get("source"), old_entry.get("source")):
                    raise _ItemFailure(REASON_EXTRA_KNOWN_TARGET_CONFLICT, "extraKnownMarketplaces target conflict")
                else:
                    item.declarations_migrated = True
        return updates

    _update_user_scope(env, _mutate)


def _remove_user_references(item: MigrationItem, env: Mapping[str, str] | None) -> None:
    """P3：user 侧删除旧名引用（trust / blocked / 声明）/ Remove old user-scope references."""

    new_name = item.new
    if not new_name:
        raise _ItemFailure(REASON_INVALID_RECORD, "internal: rename item without a target name")

    def _mutate(existing: dict[str, Any], path: Path) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if item.trust_rewrite:
            trusted = [entry for entry in _string_list(existing, FIELD_TRUSTED_MARKETPLACES, path=path) if entry != item.old]
            updates[FIELD_TRUSTED_MARKETPLACES] = trusted
        if item.blocked_rewrite:
            blocked = [entry for entry in _string_list(existing, FIELD_BLOCKED_MARKETPLACES, path=path) if entry != item.old]
            updates[FIELD_BLOCKED_MARKETPLACES] = blocked
        if item.declarations_rewrite and item.declarations_migrated:
            declarations = _string_map(existing, FIELD_EXTRA_KNOWN_MARKETPLACES, path=path)
            if item.old in declarations:
                updates[FIELD_EXTRA_KNOWN_MARKETPLACES] = {item.old: DELETE}
        return updates

    _update_user_scope(env, _mutate)


def _source_unchanged(planned: object, current: object) -> bool:
    """
    旧注册的 source 是否与**计划快照**相同（#298 终审 🟡2）/ Whether the source is unchanged from the plan.

    不能用 ``same_source``：它要求可证明同源，``{"type":"git"}`` 缺 url / 裸字符串等不可识别形态会恒 False，
    把「未变但不可识别」误判成换源。这里：两侧都可识别 → 规范化相等；两侧都不可识别 → **原值相等**；
    一侧可识别另一侧不可 → 视为变化（fail-closed）。
    """
    planned_id = source_identity(planned)
    current_id = source_identity(current)
    if planned_id is None or current_id is None:
        return planned_id is None and current_id is None and planned == current
    return planned_id == current_id


def _write_known_new(item: MigrationItem, home: Path, env: Mapping[str, str] | None) -> None:
    """
    P2 第一步：写 ``known[new]``（baseline = new；旧键暂留）/ Write the new record (baseline reset).

    **写入期复检**（§7.5）：并发出现的不同源 ``known[new]`` / 旧注册换源 → abort，绝不静默覆盖。
    merge（目标已同源注册）时**以目标记录为权威**：只补 baseline，不改写 commitSha / lastUpdated 等元数据
    （否则会把较新的目标记录回退成旧值），无变化时不落盘。
    """
    new_name = item.new or ""
    current = store.load_known_marketplaces_strict(home, env)["marketplaces"]
    old_record = current.get(item.old)
    if not isinstance(old_record, Mapping):
        raise _ItemFailure(REASON_INVALID_RECORD, f"registration {item.old!r} disappeared during the migration")
    if not _source_unchanged(item.old_source, old_record.get("source")):
        raise _ItemFailure(REASON_NAME_TAKEN, "the old registration source changed during the migration")
    existing = current.get(new_name)
    if isinstance(existing, Mapping):
        if not same_source(existing.get("source"), old_record.get("source")):
            raise _ItemFailure(REASON_NAME_TAKEN, f"target name {new_name!r} was registered from a different source")
        if existing.get(MANIFEST_NAME_BASELINE_FIELD) == new_name:
            return  # 目标记录已就绪：不重写、不制造 mtime 变化

        def _mut_merge(data: store.KnownMarketplacesFile) -> None:
            record = data["marketplaces"].get(new_name)
            if not isinstance(record, Mapping):
                raise _ItemFailure(REASON_NAME_TAKEN, "target registration disappeared during the migration")
            updated = dict(record)
            updated[MANIFEST_NAME_BASELINE_FIELD] = new_name
            data["marketplaces"][new_name] = updated  # type: ignore[assignment]

        store.update_known_marketplaces_strict(_mut_merge, home, env)
        return

    def _mutate(data: store.KnownMarketplacesFile) -> None:
        marketplaces = data["marketplaces"]
        current_old = marketplaces.get(item.old)
        if not isinstance(current_old, Mapping):
            raise _ItemFailure(REASON_INVALID_RECORD, f"registration {item.old!r} disappeared during the migration")
        if isinstance(marketplaces.get(new_name), Mapping):
            raise _ItemFailure(REASON_NAME_TAKEN, f"target name {new_name!r} appeared during the migration")
        record: dict[str, Any] = dict(current_old)
        record["installLocation"] = str(marketplace_skill_dir(home, new_name).resolve())
        record[MANIFEST_NAME_BASELINE_FIELD] = new_name
        marketplaces[new_name] = record  # type: ignore[assignment]

    store.update_known_marketplaces_strict(_mutate, home, env)


def _delete_known_old(item: MigrationItem, home: Path, env: Mapping[str, str] | None) -> None:
    """P2 收尾：删 ``known[old]``（仅在目录搬迁 + 重建完成之后）/ Drop the old record after convergence."""

    def _mutate(data: store.KnownMarketplacesFile) -> None:
        data["marketplaces"].pop(item.old, None)

    store.update_known_marketplaces_strict(_mutate, home, env)


def _precheck_claim(item: MigrationItem, known_map: Mapping[str, Any], home: Path) -> None:
    """
    计划期预判「可认领目录」（§7.4）：dry-run 的退出码必须 == apply 会得到的退出码。

    P2 会先写 ``known[new]``，因此这里把 ``new`` 预置进 known 视图后复用同一候选判据；0 个 →
    ``catalog-lost``，多个 → ``ambiguous-catalog``（都标 **unresolved**，§7.4；dry-run 即 exit 3、零写入）。
    """
    if item.already_renamed or not item.new:
        return
    if item.baseline_derived:
        old_dir = marketplace_skill_dir(home, item.old)
        if not old_dir.is_dir() or old_dir.is_symlink():
            item.action = ACTION_UNRESOLVED
            item.reason = REASON_CATALOG_MISSING
            item.warnings.append("baseline-derived migration requires the registered clone directory; none found")
        return
    new_dir = marketplace_skill_dir(home, item.new)
    if new_dir.exists() or new_dir.is_symlink():
        return
    old_record = known_map.get(item.old)
    if not isinstance(old_record, Mapping):
        return
    probe: dict[str, Any] = dict(known_map)
    probe.setdefault(item.new, {"source": old_record.get("source")})
    candidates = _claim_candidates(home, probe, item.old, item.new)
    if len(candidates) == 1:
        return
    item.action = ACTION_UNRESOLVED
    if not candidates:
        item.reason = REASON_CATALOG_LOST
        item.warnings.append("no claimable catalog directory for the new registration")
    else:
        item.reason = REASON_AMBIGUOUS_CATALOG
        item.warnings.append(f"ambiguous claimable catalog directories: {[str(c) for c in candidates]}")


def _claim_candidates(home: Path, known: Mapping[str, Any], old: str, new: str) -> list[Path]:
    """§7.4 可认领目录（修正判据：允许 ``D.name == old``）/ Claimable directories (§7.4)."""
    root = home / SOURCE_MARKETPLACE
    if not root.is_dir():
        return []
    known_names = set(known)
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        # 跳过所有点前缀目录：`.plugins`（外树命名空间）+ `.staging-*` / `.trash-*`（refresh 临时树，
        # 内容与清单名都可能与目标一致；把它们当正主 rename 或计为歧义候选都会误判）。
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
            continue
        if not (child.name == old or child.name not in known_names):
            continue
        try:
            if read_manifest_marketplace_name(child) != new:
                continue
        except MarketplaceIdentityError:
            continue
        out.append(child)
    return out


def _rename_catalog(item: MigrationItem, home: Path, env: Mapping[str, str] | None) -> None:
    """
    P2：``dir(old) → dir(new)``（§7.4 认领判据是**主路径**，不是兜底）/ Rename the catalog via the claim predicate.

    ``_write_known_new`` 已先写 ``known[new]``（同源），因此这里按 §7.4 搜「可认领目录」：
    **允许 ``D.name == old``**（崩溃主窗口里目录还叫旧名）+ 清单名 == new + 非 symlink；恰好 1 个才搬，
    0 个 → ``catalog-lost``，多个 → ``ambiguous-catalog``（不猜）。``known[new]`` 意外缺失时才退回
    「逐个验证旧目录」的保守分支。
    """
    new_dir = marketplace_skill_dir(home, item.new or "")
    old_dir = marketplace_skill_dir(home, item.old)
    if new_dir.exists() or new_dir.is_symlink():
        if item.merge or (item.baseline_derived and item.already_renamed):
            return
        known = store.load_known_marketplaces_strict(home, env)["marketplaces"]
        if _merge_scenario(item, known, home):
            item.merge = True
            return
        raise _ItemFailure(REASON_TARGET_DIR_EXISTS, f"target catalog directory {new_dir} already exists")

    if item.baseline_derived:
        # 清单不可读 → 旧目录不构成 §7.4 候选；**先**无条件搬注册的旧目录，绝不按清单名搬游离目录。
        if not old_dir.is_dir() or old_dir.is_symlink():
            raise _ItemFailure(REASON_CATALOG_LOST, "registered clone directory is missing or is a symlink")
        old_dir.rename(new_dir)
        return

    known = store.load_known_marketplaces_strict(home, env)["marketplaces"]
    new_record = known.get(item.new or "")
    old_record = known.get(item.old)
    if (
        isinstance(new_record, Mapping)
        and isinstance(old_record, Mapping)
        and same_source(new_record.get("source"), old_record.get("source"))
    ):
        candidates = _claim_candidates(home, known, item.old, item.new or "")
        if len(candidates) == 1:
            candidates[0].rename(new_dir)
            return
        if not candidates:
            raise _ItemFailure(REASON_CATALOG_LOST, "no claimable catalog directory for the new registration")
        raise _ItemFailure(REASON_AMBIGUOUS_CATALOG, f"claimable catalog directories: {[str(c) for c in candidates]}")

    # known[new] 意外缺失（外部改动 / 记录被删）：只接受「旧目录 + 清单名 == 目标名」这一确定形态。
    if old_dir.is_dir() and not old_dir.is_symlink():
        try:
            manifest_name = read_manifest_marketplace_name(old_dir)
        except MarketplaceIdentityError as exc:
            raise _ItemFailure(REASON_MANIFEST_UNREADABLE, str(exc)) from exc
        if manifest_name != item.new:
            raise _ItemFailure(REASON_MANIFEST_UNREADABLE, "catalog manifest changed during the migration")
        old_dir.rename(new_dir)
        return
    raise _ItemFailure(REASON_CATALOG_LOST, "catalog directory is missing and cannot be attributed")


def _rename_external(item: MigrationItem, home: Path) -> None:
    """P2：``.plugins/<old> → .plugins/<new>``（缺失则跳过）/ Rename the external plugin tree."""
    old_ext = external_plugins_dir(home, item.old)
    new_ext = external_plugins_dir(home, item.new or "")
    if new_ext.exists():
        if item.merge and not old_ext.exists():
            return
        raise _ItemFailure(REASON_TARGET_EXTERNAL_TREE_EXISTS, f"target external tree {new_ext} already exists")
    if old_ext.is_dir():
        old_ext.rename(new_ext)


def _norm_ledger_scope(record: Mapping[str, Any]) -> str:
    """账本记录的 scope 归一（缺省 / 非 str / 空 → user，与 materialize 写入口径一致）。"""
    value = record.get("scope")
    return value if isinstance(value, str) and value else "user"


def _norm_project_path(record: Mapping[str, Any]) -> str | None:
    """账本记录的 projectPath 归一（非 str / 空 → None）。"""
    value = record.get("projectPath")
    return value if isinstance(value, str) and value else None


def _ledger_install_path(record: object) -> str | None:
    """账本记录的合法 ``installPath``（非 str / 空 → ``None``；P2 重建前置，P0.5 用同一判据）。"""
    if not isinstance(record, Mapping):
        return None
    value = record.get("installPath")
    return value if isinstance(value, str) and value else None


def _record_needs_project_path(record: object) -> bool:
    """
    该记录是否会在 P2 重建时因缺 projectPath 触发 abort（P0.5 与 P2 共用的**单一判据**，#301 item 1）/
    Whether rebuilding this record would hit the projectPath abort.

    只判记录自身；「plugin 是否仍在新清单」由调用方前置判定。成立集合刻意**精确等于**
    :func:`_rebuild_ledger` 中 raise 的可达集合（按代码顺序：``records`` 是数组 → plugin 在清单 → 记录是
    Mapping → ``installPath`` 为非空字符串 → ``scope`` ∈ {project, local} 且缺 ``projectPath``）。
    目录是否存在属**运行期**事实，且 P2 在 raise **之后**才检查它——谓词刻意不看目录（否则两处判据会随
    运行期状态漂移）；``installPath`` 目录缺失的记录同样命中本谓词，由随后的 raise / keep 分支决定。
    """
    if not isinstance(record, Mapping):
        return False
    if _ledger_install_path(record) is None:
        return False
    return _norm_ledger_scope(record) in {"project", "local"} and not _norm_project_path(record)


def _note_ledger_record_kept(item: MigrationItem, message: str | None) -> None:
    """
    保留旧账本记录的统一诊断（P0.5 计划期预告 + P2 实作共用，幂等去重）/ Record a kept ledger entry.

    这些形态（plugin 出清单 / records 非数组 / 记录非对象 / 缺 installPath）在**计划期即可确定**，
    故 P0.5 也据置 ``incomplete``：dry-run 的退出码与 apply 一致（设计 §12.2 / §4.2 F10）。
    ``message=None`` 仅置位（P2 对该分支本就不产 warning）。
    """
    if message is not None and message not in item.warnings:
        item.warnings.append(message)
    item.incomplete = True
    item.reason = item.reason or REASON_LEDGER_REBUILD_FAILED


def _note_kept_ledger_record(item: MigrationItem, plugin: str) -> None:
    """plugin 不在新清单 → F10 保留旧账本（P0.5 与 P2 共用）。"""
    _note_ledger_record_kept(item, f"plugin {plugin!r} not present in the new manifest; keeping its old ledger record")


async def _rebuild_ledger(item: MigrationItem, home: Path, env: Mapping[str, str] | None) -> None:
    """P2：以新 pid 重建账本记录（成功者才在 P4 删旧）/ Rebuild ledger records under the new pid."""
    installed = store.load_installed_plugins_strict(home, env)["plugins"]
    new_records: dict[str, list[Any]] = {
        pid: list(records) for pid, records in installed.items() if isinstance(records, list)
    }
    for pid, records in sorted(installed.items()):
        parts = _pid_parts(pid) if isinstance(pid, str) else None
        if not parts or parts[1] != item.old:
            continue
        if not isinstance(records, list):
            _note_ledger_record_kept(
                item, f"plugin {parts[0]!r} ledger records are not an array; keeping them untouched"
            )
            continue
        plugin = parts[0]
        new_pid = f"{plugin}@{item.new}"
        if find_plugin_entry(item.manifest, plugin) is None:
            _note_kept_ledger_record(item, plugin)
            continue
        for record in records:
            if not isinstance(record, Mapping):
                _note_ledger_record_kept(item, None)
                continue
            install_path = _ledger_install_path(record)
            if install_path is None:
                _note_ledger_record_kept(
                    item, f"plugin {plugin!r} ledger record has no installPath; keeping it untouched"
                )
                continue
            scope = _norm_ledger_scope(record)
            project_path = _norm_project_path(record)
            # P0.5 已用同一判据前置（`_record_needs_project_path`）；这里只有**竞态**才可达
            # （预检之后账本被并发写入这样的记录）——保留为纵深防御（#301 item 9 裁决）。
            if _record_needs_project_path(record):
                raise _ItemFailure(
                    REASON_LEDGER_REBUILD_FAILED,
                    f"plugin {plugin!r} has a {scope} ledger record without a valid projectPath",
                )
            # 崩溃重跑：目标记录已完整物化 → 跳过重建（不改写 installedAt、不因瞬态失败误判为 incomplete）。
            already = [
                existing
                for existing in new_records.get(new_pid, [])
                if isinstance(existing, Mapping)
                and _norm_ledger_scope(existing) == scope
                and _norm_project_path(existing) == project_path
            ]
            if already and ledger_entry_fully_materialized(already):
                item.ledger_rebuild.append((pid, scope, project_path))
                continue
            mapped = _map_install_path(Path(install_path), home, item.old, item.new or "")
            if not mapped.is_dir():
                item.warnings.append(
                    f"plugin {plugin!r} install root is missing ({install_path}); keeping its old ledger record"
                )
                item.incomplete = True
                item.reason = item.reason or REASON_LEDGER_REBUILD_FAILED
                continue
            version = record.get("version")
            try:
                rebuilt = await materialize_plugin(
                    new_pid,
                    home,
                    scope=scope,
                    project_path=project_path,
                    version=version if isinstance(version, str) else None,
                    env=env,
                    allow_clone=False,  # 迁移全程离线：定位失败 → 跳过并保留旧账本（绝不 rmtree/重 clone）
                )
            except Exception as exc:  # noqa: BLE001 - 单条重建失败 → 保留旧记录（F10）
                item.warnings.append(
                    f"plugin {plugin!r} could not be re-materialized ({type(exc).__name__}); "
                    "keeping its old ledger record"
                )
                item.incomplete = True
                item.reason = item.reason or REASON_LEDGER_REBUILD_FAILED
                continue
            new_records.setdefault(new_pid, []).append(rebuilt)
            item.ledger_rebuild.append((pid, scope, project_path))


def _drop_old_ledger(item: MigrationItem, home: Path, env: Mapping[str, str] | None) -> None:
    """P4：只删「重建成功」对应的旧账本记录（F10）/ Drop only the rebuilt old ledger records.

    - scope / projectPath 用与重建时**同一归一值**比较（缺失 scope 的旧记录也能删）。
    - 没有任何可删记录时直接返回：不凭空创建空 ``installed_plugins.json`` / ``.lock``。
    """
    if not item.ledger_rebuild:
        return
    plugins = store.load_installed_plugins_strict(home, env)["plugins"]

    def _matches(record: object, scope: str, project_path: str | None) -> bool:
        return (
            isinstance(record, Mapping)
            and _norm_ledger_scope(record) == scope
            and _norm_project_path(record) == project_path
        )

    has_match = False
    for old_pid, scope, project_path in item.ledger_rebuild:
        records = plugins.get(old_pid)
        if isinstance(records, list) and any(_matches(record, scope, project_path) for record in records):
            has_match = True
            break
    if not has_match:
        return

    def _mutate(data: store.InstalledPluginsFile) -> None:
        current_plugins = data["plugins"]
        for old_pid, scope, project_path in item.ledger_rebuild:
            records = current_plugins.get(old_pid)
            if not isinstance(records, list):
                continue
            kept = [record for record in records if not _matches(record, scope, project_path)]
            if kept:
                current_plugins[old_pid] = kept
            else:
                current_plugins.pop(old_pid, None)

    store.update_installed_plugins_strict(_mutate, home, env)


def _rekey_values(item: MigrationItem, env: Mapping[str, str] | None, *, write_new: bool) -> None:
    """
    input 值单次原子写：P1 只加新键；P3 只删旧键（§4.1 F5 + §7.1）/ One atomic write per phase.

    **写入期复检**（§7.5）：目标键已存在且值不同 → abort（绝不覆盖并发写入的用户值）；P3 删旧键前
    再验证新键存在且值相符，否则保留旧键并 abort。
    """
    path = resolve_value_store_path(env)
    try:
        current = store.read_jsonc_strict(path)
    except SettingsFileCorruptError as exc:
        raise _ItemFailure(REASON_VALUE_STORE_CORRUPT, str(exc)) from exc
    data: dict[str, Any] = dict(current) if isinstance(current, Mapping) else {}
    changed = False
    if write_new:
        for old_key, new_key, value in item.value_pairs:
            if old_key not in data or data[old_key] != value:
                raise _ItemFailure(
                    REASON_VALUE_TARGET_CONFLICT,
                    f"source input value {old_key!r} changed during the migration; aborting before writing the new key",
                )
            if new_key in data:
                if data[new_key] != value:
                    raise _ItemFailure(
                        REASON_VALUE_TARGET_CONFLICT,
                        f"target input value {new_key!r} changed during the migration; refusing to overwrite",
                    )
                continue
            data[new_key] = value
            changed = True
    else:
        for old_key, new_key, value in item.value_pairs:
            if new_key not in data or data[new_key] != value:
                raise _ItemFailure(
                    REASON_VALUE_TARGET_CONFLICT,
                    f"target input value {new_key!r} is missing or changed; keeping the old key",
                )
            if old_key not in data or data[old_key] != value:
                raise _ItemFailure(
                    REASON_VALUE_TARGET_CONFLICT,
                    f"source input value {old_key!r} changed during the migration; keeping the old key",
                )
            # 上面的复检已保证 old_key 存在（含 null 值）——不再重复判定（#301 item 7）。
            data.pop(old_key, None)
            changed = True
    if changed:
        ValueStore(env).replace_all(data)


def _rekey_secrets(item: MigrationItem, store_secrets: SecretStore, *, write_new: bool) -> None:
    """secret 重键：P1 写新并回读；P3 删旧（§4.2 F6 + §7.5 写入期复检）。"""

    def _strict_get(key: str) -> str | None:
        try:
            return store_secrets.get_strict(key)
        except SecretStoreError as exc:
            raise _ItemFailure(REASON_KEYRING_UNAVAILABLE, str(exc)) from exc

    if write_new:
        for pair in item.secret_pairs:
            if _strict_get(pair.old_key) != pair.value:
                raise _ItemFailure(
                    REASON_SECRET_TARGET_CONFLICT,
                    f"source secret {pair.old_key!r} changed during the migration; aborting before writing the new key",
                )
            existing = _strict_get(pair.new_key)
            if existing is not None:
                if existing != pair.value:
                    raise _ItemFailure(
                        REASON_SECRET_TARGET_CONFLICT,
                        f"target secret key {pair.new_key!r} changed during the migration; refusing to overwrite",
                    )
                continue
            if not store_secrets.set(pair.new_key, pair.value):
                raise _ItemFailure(REASON_KEYRING_UNAVAILABLE, "failed to write the new secret key")
            item.secret_cleanup.append(pair)
            if _strict_get(pair.new_key) != pair.value:
                raise _ItemFailure(REASON_KEYRING_UNAVAILABLE, "the new secret key did not read back")
    else:
        for pair in item.secret_pairs:
            if _strict_get(pair.new_key) != pair.value:
                raise _ItemFailure(
                    REASON_SECRET_TARGET_CONFLICT,
                    f"target secret key {pair.new_key!r} is missing or changed; keeping the old key",
                )
            if _strict_get(pair.old_key) != pair.value:
                raise _ItemFailure(
                    REASON_SECRET_TARGET_CONFLICT,
                    f"source secret {pair.old_key!r} changed during the migration; keeping the old key",
                )
            if not store_secrets.delete(pair.old_key):
                item.warnings.append(f"old secret key {pair.old_key!r} could not be deleted (new key is in place)")


async def _apply_item(item: MigrationItem, home: Path, env: Mapping[str, str] | None) -> None:
    """执行单项 P1 → P2 → P3 → P4（任何失败都抛 :class:`_ItemFailure`）。"""
    secrets = SecretStore()
    try:
        # ── P1：新侧权威面（旧侧保持）──
        for pid in item.installed_add:
            _write_installed_plugin(pid, True, env)
        for pid, value in item.enabled_set.items():
            _write_enabled_plugin(pid, value, "user", None, env)
        _add_user_references(item, env)
        _rekey_values(item, env, write_new=True)
        _rekey_secrets(item, secrets, write_new=True)

        # ── P2：账本新记录 → 目录搬迁 → 账本重建 ──
        _write_known_new(item, home, env)
        _rename_catalog(item, home, env)
        _rename_external(item, home)
        await _rebuild_ledger(item, home, env)

        # ── P3：撤旧侧（known[old] 必须留到 P3/P4 之后：否则 P3 中断后重跑只见 known[new] 报 noop，
        #    旧引用再无入口归因——§7.3「P3 中 → 重跑 P3 补齐」）──
        for pid in item.installed_remove:
            _write_installed_plugin(pid, False, env)
        for pid in item.enabled_remove:
            _write_enabled_plugin(pid, None, "user", None, env)
        _remove_user_references(item, env)
        _rekey_values(item, env, write_new=False)
        _rekey_secrets(item, secrets, write_new=False)

        # ── P4：删旧账本记录（仅重建成功者）──
        _drop_old_ledger(item, home, env)

        # P2 收尾：搬迁 + 重建 + 撤旧全部完成，才删 known[old]（F7）。
        _delete_known_old(item, home, env)
    except Exception:
        # 本次已写的新 secret 尽力清理——**仅当旧键仍在**（P3 已删旧键时，新键是唯一副本，
        # 绝不能因 P4 失败而销毁）。已写的文件保持（forward-only，不逐项回滚）。
        for pair in item.secret_cleanup:
            if secrets.get(pair.old_key) is not None:
                secrets.delete(pair.new_key)
        raise


async def execute_plan(plan: MigrationPlan, home: Path, env: Mapping[str, str] | None = None) -> MigrationPlan:
    """
    执行申请（**写入前重新检测 + 重新预检**，不接受外部陈旧计划）/ Execute with a fresh plan.

    设计 §7.2/§7.5：调用方持有的 ``plan`` 只提供选择集（``selected``）与 ``--update-declarations``；
    真正驱动写入的是**此处重新构建**的计划——dry-run 与 apply 之间的磁盘变化（并发写目标键、目录出现、
    注册换源）会在新一轮 P0.5 预检里被发现，而不是按陈旧计划覆盖。结果回填到传入的 ``plan`` 供渲染。
    """
    selected = [item.old for item in plan.items if item.selected]
    if not selected:
        plan.applied = True
        return plan
    fresh = build_plan(
        home, env, names=selected, update_declarations=plan.update_declarations, cwd=plan.cwd
    )
    plan.items = fresh.items
    plan.conflicts = fresh.conflicts
    plan.corrupt = fresh.corrupt
    plan.errors = fresh.errors
    plan.applied = False
    if plan.corrupt or plan.errors or blocking_conflicts(plan):
        return plan
    for item in plan.items:
        if not item.selected or item.action != ACTION_RENAME:
            continue
        try:
            await _apply_item(item, home, env)
        except _ItemFailure as exc:
            item.action = ACTION_ABORTED
            item.reason = exc.reason
            item.warnings.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - 项级失败不跨项传播
            item.action = ACTION_ABORTED
            item.reason = REASON_RENAME_FAILED
            item.warnings.append(f"unexpected failure: {type(exc).__name__}: {exc}")
    plan.applied = True
    return plan
