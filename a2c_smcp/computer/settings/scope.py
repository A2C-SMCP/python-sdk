# -*- coding: utf-8 -*-
# filename: scope.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
五级 scope 路径解析 + 读/写两套合并语义 + active-workdir / 能力层解析（v0.2.1）
Five-level scope path resolution + read/write merge customizers + active-workdir / capability resolution.

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §5.0 / §5.1 / §5.4。

合并语义（⚠️ 读 / 写相反，照 Claude Code ``settingsMergeCustomizer``）/ Merge semantics:

**读合并**（启动加载、多 scope 叠加，low → high）/ Read merge (startup, low → high):
- object/map → **递归深合并**（逐层按 key 取并集，仅同名叶子由高 scope 覆盖；嵌套对象继续向下、
  **不整体替换**——``extraKnownMarketplaces[].source`` 关键用例）/ deep recursive merge.
- array → **拼接去重** ``uniq([*low, *high])``（低 scope 在前）/ concat + dedup, low first.
- scalar → 高 scope 整体覆盖 / scalar overwrite (highest scope wins).

**写回单 scope**（CLI 改单文件、语义相反）/ Single-scope write-back (inverse):
- array → **直接替换**（不拼接）/ replace wholesale.
- 删字段 → 写 :data:`DELETE`（= 删 key）/ deletion via the :data:`DELETE` sentinel.

scope 分层（low → high）/ Scope layering (§5.1)：
能力发现层（全部登记目录并集，仅 ``enabledPlugins`` / ``extraKnownMarketplaces``）< user（主）
< project（active workdir 单根）< local（active workdir 单根）< flag < policy。
**无 active workdir** 时 project/local 全空，仅 user + 能力层（+ flag/policy）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2c_smcp.computer.settings.schema import (
    CAPABILITY_FIELDS,
    SettingsScope,
    SettingsValidationError,
    validate_settings,
)
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import resolve_xdg_first

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 路径常量 / Path constants
# ---------------------------------------------------------------------------
XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"

# workspace 工作目录下的治理子目录与文件名 / Per-workdir governance dir & filenames.
TFROBOT_DIRNAME = ".tfrobot"
PROJECT_SETTINGS_FILENAME = "settings.json"
LOCAL_SETTINGS_FILENAME = "settings.local.json"
USER_SETTINGS_FILENAME = "settings.json"

_A2C_CONFIG_SUBDIR = "a2c"


def resolve_user_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """
    解析 user scope 配置目录 / Resolve the user-scope config dir。

    优先 ``$XDG_CONFIG_HOME/a2c``（XDG_CONFIG_HOME 须为绝对路径，否则按 XDG 规范忽略），
    回退 ``~/.config/a2c``。XDG-first 解析复用 :func:`a2c_smcp.utils.path.resolve_xdg_first`
    （与 SKILL Home 同范式，区别：config 用 ``XDG_CONFIG_HOME``、data 用 ``XDG_DATA_HOME``）。
    Prefers ``$XDG_CONFIG_HOME/a2c`` (must be absolute), falls back to ``~/.config/a2c``.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    return resolve_xdg_first(
        environ,
        XDG_CONFIG_HOME_ENV,
        _A2C_CONFIG_SUBDIR,
        fallback=Path.home() / ".config" / _A2C_CONFIG_SUBDIR,
    )


def user_settings_path(env: Mapping[str, str] | None = None) -> Path:
    """user scope settings.json 路径 / Path to the user-scope settings.json。"""
    return resolve_user_config_dir(env) / USER_SETTINGS_FILENAME


def workdir_settings_dir(workdir: Path) -> Path:
    """工作目录治理子目录 ``<workdir>/.tfrobot`` / The per-workdir ``.tfrobot`` dir。"""
    return Path(workdir) / TFROBOT_DIRNAME


def workdir_project_settings_path(workdir: Path) -> Path:
    """project scope 路径 ``<workdir>/.tfrobot/settings.json``（入 git）/ project-scope path。"""
    return workdir_settings_dir(workdir) / PROJECT_SETTINGS_FILENAME


def workdir_local_settings_path(workdir: Path) -> Path:
    """local scope 路径 ``<workdir>/.tfrobot/settings.local.json``（不入 git）/ local-scope path。"""
    return workdir_settings_dir(workdir) / LOCAL_SETTINGS_FILENAME


# ---------------------------------------------------------------------------
# 合并原语 / Merge primitives
# ---------------------------------------------------------------------------
# 写回时表示「删 key」的哨兵（复刻 CC 的 ``undefined`` 语义）/ Deletion sentinel for write-back.
class _Delete:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        return "<DELETE>"


DELETE = _Delete()

ConflictHook = Callable[[str, Any, Any], None]


def _dedup_preserve_order(items: list[Any]) -> list[Any]:
    """去重保序（首次出现胜出；兼容不可哈希元素）/ Order-preserving dedup (first wins; unhashable-safe)."""
    out: list[Any] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def merge_read(low: Any, high: Any, *, on_conflict: ConflictHook | None = None, _path: str = "") -> Any:
    """
    读合并 customizer（深合并 / 数组拼接去重 / 标量高覆盖）/ Read-merge customizer。

    与 Claude Code ``settingsMergeCustomizer`` 等价：customizer 只特判数组（拼接去重），其余交给
    默认递归深合并；标量 / 类型不一致由高 scope 覆盖。``on_conflict(path, low, high)`` 在**叶子标量
    被不同值覆盖**时回调（供能力层跨目录同名告警，§5.4）；普通 scope 叠加传 ``None``（高覆盖低是
    既定语义、非冲突）。
    Equivalent to Claude Code's customizer (arrays special-cased to concat+dedup, the rest deep
    recursive merge); ``on_conflict`` fires only when a differing scalar leaf is overridden.
    """
    if isinstance(low, dict) and isinstance(high, dict):
        merged = dict(low)
        for key, hv in high.items():
            child_path = f"{_path}.{key}" if _path else key
            if key in merged:
                merged[key] = merge_read(merged[key], hv, on_conflict=on_conflict, _path=child_path)
            else:
                merged[key] = hv
        return merged
    if isinstance(low, list) and isinstance(high, list):
        # 数组：低在前拼接去重（不触发冲突回调——拼接是既定语义）。
        return _dedup_preserve_order([*low, *high])
    # 标量 / 类型不一致：高 scope 覆盖。
    if on_conflict is not None and low != high:
        on_conflict(_path, low, high)
    return high


def merge_layers(layers: Sequence[Mapping[str, Any]], *, on_conflict: ConflictHook | None = None) -> dict[str, Any]:
    """
    按 low → high 顺序折叠多层 settings（读合并）/ Fold multiple layers low → high (read merge)。

    :param layers: 已校验的各 scope dict，**低优先级在前**（如 ``[capability, user, project, local, flag, policy]``）。
    """
    result: dict[str, Any] = {}
    for layer in layers:
        result = merge_read(result, dict(layer), on_conflict=on_conflict)
    return result


def apply_write(existing: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    """
    写回单 scope（数组整体替换 + :data:`DELETE` 删 key，§5.4 写语义）/ Single-scope write-back。

    与 :func:`merge_read` **语义相反**：数组**不拼接、直接替换**；值为 :data:`DELETE` 的 key 被删除；
    嵌套对象继续递归（``settings set a.b.c`` 只动该叶子，不毁同级兄弟键）。返回**新 dict**（不就地改）。
    Inverse of :func:`merge_read`: arrays replace wholesale; keys mapped to :data:`DELETE` are
    removed; nested objects recurse. Returns a new dict (no in-place mutation).
    """
    result: dict[str, Any] = dict(existing)
    for key, value in updates.items():
        if isinstance(value, _Delete):
            result.pop(key, None)
            continue
        cur = result.get(key)
        if isinstance(cur, dict) and isinstance(value, Mapping):
            result[key] = apply_write(cur, value)
        else:
            # 标量 / 数组 / 类型切换：整体替换（数组不拼接）。
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# 文件加载 / File loading
# ---------------------------------------------------------------------------
def load_settings_file(path: Path, scope: SettingsScope) -> tuple[dict[str, Any], list[SettingsValidationError]]:
    """
    读取并字段级容错校验单个 settings 文件 / Load + field-level-validate one settings file。

    缺失文件 → ``({}, [])``；JSON 损坏 → ``({}, [error])``（整文件回退空、**不**备份、**不**清盘，
    照 CC「invalid file 不用但留盘」，§5.6）；正常 → :func:`validate_settings` 结果。
    Missing file → empty; corrupt JSON → empty + one error (file untouched on disk); otherwise
    the :func:`validate_settings` result.
    """
    p = Path(path)
    if not p.exists():
        return {}, []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Settings file %s unreadable/corrupt, ignored (kept on disk): %s", p, exc)
        return {}, [SettingsValidationError(scope=scope, field="<file>", reason=f"unreadable or corrupt JSON: {exc}", source_path=str(p))]
    return validate_settings(raw, scope, source_path=str(p))


def filter_capability_fields(settings: Mapping[str, Any]) -> dict[str, Any]:
    """只保留能力发现层字段（``enabledPlugins`` / ``extraKnownMarketplaces``）/ Keep only capability fields。"""
    return {k: v for k, v in settings.items() if k in CAPABILITY_FIELDS}


# ---------------------------------------------------------------------------
# 解析编排 / Resolution orchestration
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ResolvedSettings:
    """
    解析结果 / The resolved settings result。

    ``settings`` 为六层合并后的最终视图；``errors`` 汇总各层字段级校验错误（不阻断启动，供
    ``settings show`` / 诊断命令呈现，§5.6），已按出现顺序去重。
    ``settings`` is the merged view; ``errors`` aggregates field-level validation errors
    (non-fatal, deduped, surfaced via diagnostics).
    """

    settings: dict[str, Any]
    errors: list[SettingsValidationError] = field(default_factory=list)


def _build_capability_layer(
    registered_workdirs: Sequence[Path],
) -> tuple[dict[str, Any], list[SettingsValidationError]]:
    """
    能力发现层 (A)：跨**全部登记工作目录**取 ``enabledPlugins`` / ``extraKnownMarketplaces`` 并集。
    Capability layer (A): union of capability fields across all registered workdirs。

    单目录内 local 覆盖 project；跨目录按**登记顺序**后者覆盖前者并 WARN（§5.0「同名优先级」）。
    Within a workdir local overrides project; across workdirs later registration wins + WARN.
    """
    errors: list[SettingsValidationError] = []
    accumulated: dict[str, Any] = {}

    def _on_conflict(path: str, old: Any, new: Any) -> None:
        logger.warning(
            "Capability-layer conflict on %r across registered workdirs: %r overridden by later registration %r",
            path,
            old,
            new,
        )

    for workdir in registered_workdirs:
        proj, proj_errors = load_settings_file(workdir_project_settings_path(workdir), SettingsScope.PROJECT)
        local, local_errors = load_settings_file(workdir_local_settings_path(workdir), SettingsScope.LOCAL)
        errors.extend(proj_errors)
        errors.extend(local_errors)
        # 单目录内：local 覆盖 project（仅能力字段）。
        per_workdir = merge_read(filter_capability_fields(proj), filter_capability_fields(local))
        accumulated = merge_read(accumulated, per_workdir, on_conflict=_on_conflict)

    return accumulated, errors


def _dedup_errors(errors: Sequence[SettingsValidationError]) -> list[SettingsValidationError]:
    """按出现顺序去重（active workdir 文件在能力层 + B 层被读两次，错误天然重复）/ Order-preserving error dedup."""
    return list(dict.fromkeys(errors))


def resolve_settings(
    *,
    registered_workdirs: Sequence[Path] = (),
    active_workdir: Path | None = None,
    env: Mapping[str, str] | None = None,
    flag_settings_path: Path | None = None,
    policy_settings: Mapping[str, Any] | None = None,
) -> ResolvedSettings:
    """
    解析六层 settings 视图（能力层 / user / project / local / flag / policy）/ Resolve the six-layer view。

    两层模型（访谈定稿，§5.0 / §5.1）/ Two-layer model:
    - **(A) 能力发现层**：``enabledPlugins`` / ``extraKnownMarketplaces`` 跨**全部** ``registered_workdirs``
      取并集、置最低优先级（稳定能力面、不随 active 跳变）。
    - **(B) active-workdir 单根**：project/local **只取** ``active_workdir`` 的 ``.tfrobot/settings[.local].json``，
      不跨目录并集；**``active_workdir is None`` 时 project/local 全空**（仅 user + 能力层 + flag/policy）。

    :param registered_workdirs: workspace 持久登记的全部工作目录（能力层来源）/ all registered workdirs.
    :param active_workdir: 当前绑定任务的单根（project/local 来源）；``None`` = 空闲 / no active task.
    :param env: 环境映射（解析 user config dir），默认 ``os.environ`` / env mapping for the user config dir.
    :param flag_settings_path: ``--settings <file>`` 指定文件 / the ``--settings`` flag file, if any.
    :param policy_settings: policy scope 原始 dict（来自 :mod:`a2c_smcp.computer.settings.policy`
        的 first-source-wins 结果），在此按 POLICY scope 校验 / raw policy dict, validated here.
    """
    errors: list[SettingsValidationError] = []

    # (A) 能力发现层（最低）/ capability layer (lowest)
    capability_layer, cap_errors = _build_capability_layer(registered_workdirs)
    errors.extend(cap_errors)

    # user（主）/ user (primary)
    user_layer, user_errors = load_settings_file(user_settings_path(env), SettingsScope.USER)
    errors.extend(user_errors)

    # (B) active-workdir 单根 project/local / active-workdir single-root
    if active_workdir is not None:
        project_layer, proj_errors = load_settings_file(workdir_project_settings_path(active_workdir), SettingsScope.PROJECT)
        local_layer, local_errors = load_settings_file(workdir_local_settings_path(active_workdir), SettingsScope.LOCAL)
        errors.extend(proj_errors)
        errors.extend(local_errors)
    else:
        project_layer, local_layer = {}, {}

    # flag / flag (``--settings``)
    if flag_settings_path is not None:
        flag_layer, flag_errors = load_settings_file(flag_settings_path, SettingsScope.FLAG)
        errors.extend(flag_errors)
    else:
        flag_layer = {}

    # policy（最高）/ policy (highest) — 在此按 POLICY scope 校验 first-source-wins 结果。
    policy_layer, policy_errors = validate_settings(dict(policy_settings or {}), SettingsScope.POLICY)
    errors.extend(policy_errors)

    merged = merge_layers([capability_layer, user_layer, project_layer, local_layer, flag_layer, policy_layer])
    return ResolvedSettings(settings=merged, errors=_dedup_errors(errors))
