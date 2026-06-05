# -*- coding: utf-8 -*-
# filename: schema.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
意图层 settings.json 结构与字段校验（v0.2.1）
Intent-layer settings.json schema & field validation (v0.2.1)

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §5.3 / §5.3.1 / §5.6。

settings.json 是 Computer 的**意图 / 治理层**（plugin 启停、marketplace 声明、MCP 门控、trust
policy），**不**承载 MCP server 定义 / inputs（那些在 ``.tfrobot/mcp.json``，§9.1）。
settings.json is the Computer's **intent / governance** layer; it does NOT carry MCP server
definitions / inputs (those live in ``.tfrobot/mcp.json``).

前向兼容模型（复刻 Claude Code，§5.6）/ Forward-compat model (mirrors Claude Code):
- **全字段可选 + 顶层 passthrough**：未知顶层键**静默保留**、不报错、不剥离（升降级都不丢）。
  All fields optional + top-level passthrough: unknown keys are silently preserved.
- **无 version 字段**：人编文件不背版本负担；``version`` 只在 CLI 维护的物化文件（§6）。
  No ``version`` field: human-edited files carry no version burden.
- **字段级容错**：单个已知键 / 单个 map 项 / 单个数组元素校验失败 → **逐条过滤**（回退默认）、
  其余保留、错误收进 :class:`SettingsValidationError` 列表（照 CC：错误不阻断启动、不整文件作废）。
  Field-level tolerance: a single bad known key / map entry / array element is filtered out
  per-item, the rest is kept, and the error is collected — never invalidating the whole file.
- **scope 越权**：``allowedMcpServers`` / ``deniedMcpServers`` 是 **policy-only**；出现在非 policy
  scope → 过滤不用 + 记 ValidationError（杜绝用户态自我提权）。
  Scope overreach: policy-only fields appearing in a non-policy scope are filtered + recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NotRequired, TypedDict

from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)


class SettingsScope(StrEnum):
    """
    settings 来源 scope（低 → 高，high 覆盖 low）/ Settings source scope (low → high)。

    ``CAPABILITY`` 是 A2C 特有的**能力发现层**（最低优先级）：跨**全部登记工作目录**取
    ``enabledPlugins`` / ``extraKnownMarketplaces`` 并集，让 Agent 能力面稳定、不随 active
    workdir 跳变（§5.0/§5.1）。其余五级 = Claude Code 完整对齐（user/project/local/flag/policy）。
    ``CAPABILITY`` is the A2C-specific capability-discovery layer (lowest); the other five align
    with Claude Code.
    """

    CAPABILITY = "capability"  # 能力发现层（最低）/ capability-discovery (lowest)
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
    FLAG = "flag"
    POLICY = "policy"  # 最高 / highest


# ---------------------------------------------------------------------------
# 已知字段名（camelCase，对齐 CC / JSON schema）/ Known field names (camelCase)
# ---------------------------------------------------------------------------
FIELD_SCHEMA = "$schema"
FIELD_EXTRA_KNOWN_MARKETPLACES = "extraKnownMarketplaces"
FIELD_ENABLED_PLUGINS = "enabledPlugins"
FIELD_STRICT_KNOWN_MARKETPLACES = "strictKnownMarketplaces"
FIELD_TRUSTED_MARKETPLACES = "trustedMarketplaces"
FIELD_BLOCKED_MARKETPLACES = "blockedMarketplaces"
FIELD_ENABLE_ALL_PROJECT_MCP = "enableAllProjectMcpServers"
FIELD_ENABLED_MCPJSON_SERVERS = "enabledMcpjsonServers"
FIELD_DISABLED_MCPJSON_SERVERS = "disabledMcpjsonServers"
FIELD_ALLOWED_MCP_SERVERS = "allowedMcpServers"
FIELD_DENIED_MCP_SERVERS = "deniedMcpServers"
FIELD_PERMISSIONS = "permissions"

# 仅能在能力发现层取并集的字段（§5.0 (A) 层）/ Fields contributed by the capability layer.
CAPABILITY_FIELDS: frozenset[str] = frozenset({FIELD_ENABLED_PLUGINS, FIELD_EXTRA_KNOWN_MARKETPLACES})

# policy-only 字段：出现在非 policy scope → 过滤 + 记错（§5.6）。
# Policy-only fields: filtered + recorded if seen outside the policy scope.
POLICY_ONLY_FIELDS: frozenset[str] = frozenset({FIELD_ALLOWED_MCP_SERVERS, FIELD_DENIED_MCP_SERVERS})

# 字符串数组字段（读合并：拼接去重；写回：整体替换，§5.4）/ String-array fields.
STRING_ARRAY_FIELDS: frozenset[str] = frozenset(
    {
        FIELD_TRUSTED_MARKETPLACES,
        FIELD_BLOCKED_MARKETPLACES,
        FIELD_ENABLED_MCPJSON_SERVERS,
        FIELD_DISABLED_MCPJSON_SERVERS,
        FIELD_ALLOWED_MCP_SERVERS,
        FIELD_DENIED_MCP_SERVERS,
    }
)

# 标量布尔字段（读合并：取最高 scope）/ Scalar bool fields (highest scope wins).
BOOL_FIELDS: frozenset[str] = frozenset({FIELD_STRICT_KNOWN_MARKETPLACES, FIELD_ENABLE_ALL_PROJECT_MCP})

# ---------------------------------------------------------------------------
# 形态正则 / Shape regexes
# ---------------------------------------------------------------------------
# marketplace / plugin 名：严格 kebab（§3.1 名称字符集；与 naming.py leaf 一致）。
# marketplace / plugin name: strict kebab.
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# enabledPlugins key 形态 ``<plugin>@<marketplace>``（§5.3.1）/ key shape.
_PLUGIN_AT_MP_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@[a-z0-9]+(?:-[a-z0-9]+)*$")

# git url 形态校验（§5.3.1）：scp-like ``user@host:path`` 或 URL scheme（ssh/git/http/https/file）。
# git url shape: scp-like or URL scheme.
_GIT_SCP_LIKE_RE = re.compile(r"^[\w.+-]+@[\w.-]+:.+$")
_GIT_URL_SCHEME_RE = re.compile(r"^(?:ssh|git|https?|file)://.+$")

MAX_NAME_LEN = 64


def is_valid_marketplace_name(name: str) -> bool:
    """marketplace / plugin 名是否合规（严格 kebab，1–64）/ Whether a marketplace/plugin name is valid."""
    return isinstance(name, str) and 1 <= len(name) <= MAX_NAME_LEN and _KEBAB_RE.match(name) is not None


def is_valid_enabled_plugin_key(key: str) -> bool:
    """``enabledPlugins`` key 是否形如 ``<plugin>@<marketplace>`` / Whether the key matches ``<plugin>@<mp>``."""
    if not isinstance(key, str) or _PLUGIN_AT_MP_RE.match(key) is None:
        return False
    plugin, _, marketplace = key.partition("@")
    return len(plugin) <= MAX_NAME_LEN and len(marketplace) <= MAX_NAME_LEN


def is_valid_git_url(url: str) -> bool:
    """git url 形态是否合规（scp-like 或 ssh/git/http(s)/file scheme）/ Whether a git url is well-formed."""
    if not isinstance(url, str) or not url.strip():
        return False
    return _GIT_URL_SCHEME_RE.match(url) is not None or _GIT_SCP_LIKE_RE.match(url) is not None


# ---------------------------------------------------------------------------
# TypedDict 结构（IDE / 文档用；运行时校验走 validate_settings）/ TypedDict shape
# ---------------------------------------------------------------------------
class GitSource(TypedDict):
    """marketplace git 源 / marketplace git source（``extraKnownMarketplaces[].source``）。"""

    type: str  # 固定 "git" / fixed "git"
    url: str


class MarketplaceEntry(TypedDict):
    """``extraKnownMarketplaces`` 单条声明 / a single marketplace declaration。"""

    source: GitSource
    autoUpdate: NotRequired[bool]


class PermissionsBlock(TypedDict, total=False):
    """``permissions`` 块（§5.3.1）/ the ``permissions`` block。"""

    additionalDirectories: list[str]


class ComputerSettings(TypedDict, total=False):
    """
    settings.json 完整结构（全可选；未知键 passthrough，不在此声明）/ Full settings.json shape.

    本 TypedDict **不含 version**（§5.6）；仅供类型提示，运行时容错校验见 :func:`validate_settings`。
    No ``version`` field; for typing only — runtime tolerant validation lives in
    :func:`validate_settings`.
    """

    extraKnownMarketplaces: dict[str, MarketplaceEntry]
    enabledPlugins: dict[str, bool]
    strictKnownMarketplaces: bool
    trustedMarketplaces: list[str]
    blockedMarketplaces: list[str]
    enableAllProjectMcpServers: bool
    enabledMcpjsonServers: list[str]
    disabledMcpjsonServers: list[str]
    allowedMcpServers: list[str]
    deniedMcpServers: list[str]
    permissions: PermissionsBlock


# ---------------------------------------------------------------------------
# 字段级容错校验 / Field-level tolerant validation
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SettingsValidationError:
    """
    单条 settings 校验错误（不阻断启动，经 ``settings show`` / 诊断命令呈现，§5.6）。
    A single non-fatal settings validation error (surfaced via ``settings show`` / diagnostics).

    ``field`` 为点路径（如 ``extraKnownMarketplaces.my-mp.source.url``）；``scope`` 标明来源层；
    ``source_path`` 为产生该错误的文件（可空，便于诊断定位）。
    ``field`` is a dotted path; ``scope`` identifies the layer; ``source_path`` is the offending
    file (optional, aids diagnosis).
    """

    scope: SettingsScope
    field: str
    reason: str
    source_path: str | None = None


# 哨兵：表示「整字段判废、回退默认」/ Sentinel: "drop the whole field, fall back to default".
class _Drop:
    __slots__ = ()


_DROP = _Drop()


def _err(scope: SettingsScope, field: str, reason: str) -> SettingsValidationError:
    return SettingsValidationError(scope=scope, field=field, reason=reason)


def _validate_bool(key: str, value: Any, scope: SettingsScope) -> tuple[Any, list[SettingsValidationError]]:
    if isinstance(value, bool):
        return value, []
    return _DROP, [_err(scope, key, f"expected bool, got {type(value).__name__}")]


def _validate_string_array(key: str, value: Any, scope: SettingsScope) -> tuple[Any, list[SettingsValidationError]]:
    if not isinstance(value, list):
        return _DROP, [_err(scope, key, f"expected array, got {type(value).__name__}")]
    cleaned: list[str] = []
    errors: list[SettingsValidationError] = []
    for idx, item in enumerate(value):
        if isinstance(item, str):
            cleaned.append(item)
        else:
            errors.append(_err(scope, f"{key}[{idx}]", f"expected string, got {type(item).__name__}"))
    return cleaned, errors


def _validate_enabled_plugins(key: str, value: Any, scope: SettingsScope) -> tuple[Any, list[SettingsValidationError]]:
    if not isinstance(value, dict):
        return _DROP, [_err(scope, key, f"expected object, got {type(value).__name__}")]
    cleaned: dict[str, bool] = {}
    errors: list[SettingsValidationError] = []
    for plugin_key, enabled in value.items():
        if not is_valid_enabled_plugin_key(plugin_key):
            errors.append(_err(scope, f"{key}.{plugin_key}", "key must be '<plugin>@<marketplace>' (strict kebab)"))
            continue
        if not isinstance(enabled, bool):
            errors.append(_err(scope, f"{key}.{plugin_key}", f"value must be bool, got {type(enabled).__name__}"))
            continue
        cleaned[plugin_key] = enabled
    return cleaned, errors


def _validate_extra_marketplaces(key: str, value: Any, scope: SettingsScope) -> tuple[Any, list[SettingsValidationError]]:
    if not isinstance(value, dict):
        return _DROP, [_err(scope, key, f"expected object, got {type(value).__name__}")]
    cleaned: dict[str, dict] = {}
    errors: list[SettingsValidationError] = []
    for name, entry in value.items():
        path = f"{key}.{name}"
        if not is_valid_marketplace_name(name):
            errors.append(_err(scope, path, "marketplace name must be strict kebab (1-64)"))
            continue
        if not isinstance(entry, dict):
            errors.append(_err(scope, path, f"entry must be object, got {type(entry).__name__}"))
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            errors.append(_err(scope, f"{path}.source", "missing or non-object 'source'"))
            continue
        if source.get("type") != "git":
            errors.append(_err(scope, f"{path}.source.type", "only source.type == 'git' is supported"))
            continue
        if not is_valid_git_url(source.get("url", "")):
            errors.append(_err(scope, f"{path}.source.url", "malformed git url"))
            continue
        auto_update = entry.get("autoUpdate")
        if auto_update is not None and not isinstance(auto_update, bool):
            errors.append(_err(scope, f"{path}.autoUpdate", f"must be bool, got {type(auto_update).__name__}"))
            continue
        # 仅保留规范化后的已知子字段（source / autoUpdate）；其余子键静默丢（爆炸半径 0，§3.2）。
        normalized: dict[str, Any] = {"source": {"type": "git", "url": source["url"]}}
        if auto_update is not None:
            normalized["autoUpdate"] = auto_update
        cleaned[name] = normalized
    return cleaned, errors


def _validate_permissions(key: str, value: Any, scope: SettingsScope) -> tuple[Any, list[SettingsValidationError]]:
    if not isinstance(value, dict):
        return _DROP, [_err(scope, key, f"expected object, got {type(value).__name__}")]
    cleaned: dict[str, Any] = {}
    errors: list[SettingsValidationError] = []
    for sub_key, sub_value in value.items():
        if sub_key == "additionalDirectories":
            if not isinstance(sub_value, list):
                errors.append(_err(scope, f"{key}.additionalDirectories", "expected array"))
                continue
            dirs: list[str] = []
            for idx, item in enumerate(sub_value):
                if not isinstance(item, str):
                    errors.append(_err(scope, f"{key}.additionalDirectories[{idx}]", "expected string"))
                elif not item.startswith("/") and not re.match(r"^[A-Za-z]:[\\/]", item):
                    # 须绝对路径（POSIX ``/`` 或 Windows 盘符）；相对路径判废（§5.3.1）。
                    errors.append(_err(scope, f"{key}.additionalDirectories[{idx}]", "must be an absolute path"))
                else:
                    dirs.append(item)
            cleaned["additionalDirectories"] = dirs
        else:
            # permissions 下未知子键 passthrough（与顶层 passthrough 同纪律）。
            cleaned[sub_key] = sub_value
    return cleaned, errors


# 字段 → 校验器映射（缺席 = 未知字段，passthrough）/ field → validator map (absent = unknown, passthrough).
_FIELD_VALIDATORS = {
    FIELD_EXTRA_KNOWN_MARKETPLACES: _validate_extra_marketplaces,
    FIELD_ENABLED_PLUGINS: _validate_enabled_plugins,
    FIELD_STRICT_KNOWN_MARKETPLACES: _validate_bool,
    FIELD_TRUSTED_MARKETPLACES: _validate_string_array,
    FIELD_BLOCKED_MARKETPLACES: _validate_string_array,
    FIELD_ENABLE_ALL_PROJECT_MCP: _validate_bool,
    FIELD_ENABLED_MCPJSON_SERVERS: _validate_string_array,
    FIELD_DISABLED_MCPJSON_SERVERS: _validate_string_array,
    FIELD_ALLOWED_MCP_SERVERS: _validate_string_array,
    FIELD_DENIED_MCP_SERVERS: _validate_string_array,
    FIELD_PERMISSIONS: _validate_permissions,
}


def validate_settings(
    raw: Any,
    scope: SettingsScope,
    *,
    source_path: str | None = None,
) -> tuple[dict[str, Any], list[SettingsValidationError]]:
    """
    对单个 scope 的原始 settings dict 做**字段级容错**校验（§5.6）/ Field-level tolerant validation.

    语义（照 Claude Code，错误不阻断启动）/ Semantics (Claude Code; never blocks startup):

    - 非 dict 根 → 返回 ``({}, [error])``（整层判废，回退空）。Non-dict root → empty + error.
    - ``$schema``：原样保留、CLI 不消费 / preserved verbatim, not consumed.
    - **policy-only 字段在非 policy scope** → 过滤 + 记错 / filtered + recorded.
    - 已知字段：经对应校验器**逐条过滤**非法 map 项 / 数组元素；整字段类型错 → 回退默认（剔除）。
      Known fields: per-item filtered; whole-field type error → dropped (falls back to default).
    - 未知字段：**静默保留**（passthrough）/ unknown fields: silently preserved.

    返回 ``(cleaned, errors)``——``cleaned`` 供 :mod:`a2c_smcp.computer.settings.scope` 跨 scope
    合并；磁盘文件**不**被本函数改写（passthrough 的「仍留在文件」由「不重写」保证）。
    Returns ``(cleaned, errors)`` for cross-scope merge; the on-disk file is never rewritten here.

    :param raw: 单个 settings 文件 parse 后的对象 / parsed object of one settings file.
    :param scope: 该文件所属 scope / the scope this file belongs to.
    :param source_path: 文件路径（用于错误溯源，可空）/ file path for error provenance (optional).
    """
    if not isinstance(raw, dict):
        return {}, [SettingsValidationError(scope=scope, field="<root>", reason="settings root must be an object", source_path=source_path)]

    cleaned: dict[str, Any] = {}
    errors: list[SettingsValidationError] = []

    for key, value in raw.items():
        if key == FIELD_SCHEMA:
            cleaned[key] = value  # 编辑器补全用，CLI 不消费 / for editors only.
            continue
        if key in POLICY_ONLY_FIELDS and scope is not SettingsScope.POLICY:
            errors.append(_err(scope, key, "policy-only field not allowed outside the policy scope (filtered)"))
            continue
        validator = _FIELD_VALIDATORS.get(key)
        if validator is None:
            cleaned[key] = value  # passthrough 未知字段 / unknown field passthrough.
            continue
        result, field_errors = validator(key, value, scope)
        errors.extend(_with_source(field_errors, source_path))
        if result is _DROP:
            continue
        cleaned[key] = result

    return cleaned, errors


def _with_source(errors: list[SettingsValidationError], source_path: str | None) -> list[SettingsValidationError]:
    """把 ``source_path`` 回填进一批错误（校验器内部不知道文件路径）/ Backfill the source path into errors."""
    if source_path is None:
        return errors
    return [
        SettingsValidationError(scope=e.scope, field=e.field, reason=e.reason, source_path=source_path)
        for e in errors
    ]
