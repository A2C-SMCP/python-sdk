# -*- coding: utf-8 -*-
# filename: mcp_config.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``.tfrobot/mcp.json`` MCP server 定义层（A2C 原生 schema）多 scope 加载 + 批准门控（v0.2.1 #64）
``.tfrobot/mcp.json`` MCP server definition layer (A2C-native schema) multi-scope load + approval gate.

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §9.1（A2C 原生 schema、为何不复用标准
``.mcp.json``）/ §9.2（全套 CC 批准门控）/ §5.1（active-workdir 单根）/ §5.5（MCP 定义合并顺序）。

本模块是 **MCP 定义/门控的纯逻辑层**（无 git / 无 MCP manager / 无网络）。职责三件：
1. **多 scope 加载合并** ``mcp.json`` —— 顺序 ``policy > active-local > active-project > user > flag``，
   **无能力层并集**（敏感面隔离，区别于 settings.json 的能力发现层 §5.1 (A)）；server 按 name **整体替换**
   （配置是原子单元、非深合并），记录最高定义 scope 为 ``origin``。
2. **批准门控判定**（:func:`mcp_server_status`）—— 据 resolved settings（#56 已落地的 MCP 门控字段）+
   plugin-bundled 账本（#63 ``installed_plugins.json``）算 ``enabled/disabled/pending``。
3. **批准写助手** —— 批准/拒绝写 **local scope**（§9.2，个人决定不污染共享层），镜像
   :func:`a2c_smcp.computer.settings.installer._write_enabled_plugin` 的持锁原子 RMW。

This is the pure logic layer for MCP definitions/gating (no git / MCP manager / network).

**显式划界 / Deferred boundaries**：
- **取值渲染**（``envFile`` 加载 / ``${env:}`` / ``${workspaceFolder}`` / inputs 解析链 / keyring / 明文
  state）归 **#65**：本模块产出**带占位符**的定义，``ResolvedMcpServer.ext``（``envFile`` 等 VS Code 扩展）
  + 未渲染占位符是交给 #65 的 handoff。**绝不在此渲染**（§9.1 安全铁律：值不离 Computer）。
- **批准框 TTY 交互**（``[a]/[y]/[n]``）/ ``--approve-all-mcp`` flag / 非交互 pending→skip+WARN 接线归
  **#69**：本模块只提供 :class:`McpApprovalStatus` 判定 + 三个写助手原语。
- ``allowManagedMcpServersOnly``（§9.2）#56 schema 未引入，v0.2.1 范围外；本模块只用
  ``allowedMcpServers`` / ``deniedMcpServers``。
- ``managed-mcp.json`` v0.2.1 仅读 layer-3 文件（per-platform managed dir），remote/MDM stub，对齐
  :mod:`a2c_smcp.computer.settings.policy` 的 settings 姿态。

**容错姿态**：``mcp.json`` 是**人/团队编辑文件**，故**字段级容错**（单 server / input 畸形 → drop + 记
:class:`~a2c_smcp.computer.settings.schema.SettingsValidationError`，**不 abort**）——刻意区别于 #63
``manifest.py`` 对 plugin-bundled server 的**硬抛**（那是 install 原子前置条件）。照 §5.6 人编文件容错。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig, MCPServerInput
from a2c_smcp.computer.settings.policy import LINUX_MANAGED_DIR, MACOS_MANAGED_DIR, WINDOWS_MANAGED_DIR
from a2c_smcp.computer.settings.schema import (
    FIELD_ALLOWED_MCP_SERVERS,
    FIELD_DENIED_MCP_SERVERS,
    FIELD_DISABLED_MCPJSON_SERVERS,
    FIELD_ENABLE_ALL_PROJECT_MCP,
    FIELD_ENABLED_MCPJSON_SERVERS,
    SettingsScope,
    SettingsValidationError,
)
from a2c_smcp.computer.settings.scope import (
    apply_write,
    load_settings_file,
    resolve_user_config_dir,
    workdir_local_settings_path,
    workdir_settings_dir,
)
from a2c_smcp.computer.settings.store import atomic_write_json, file_lock, load_installed_plugins
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 常量 / Constants
# ---------------------------------------------------------------------------
MCP_CONFIG_FILENAME = "mcp.json"  # user / project scope（§9.1）
MCP_LOCAL_CONFIG_FILENAME = "mcp.local.json"  # active workdir local scope（不入 git）
MANAGED_MCP_FILENAME = "managed-mcp.json"  # policy scope（企业下发）

# server 定义里的 VS Code 风格扩展字段：非 A2C ``MCPServerConfig`` 字段（其 ``extra="forbid"``），校验前
# 剥离、原样保留交 #65 渲染消费 / VS Code-style extension keys, stripped before validation, kept for #65.
_VSCODE_EXT_KEYS: frozenset[str] = frozenset({"envFile"})

# 预信任 origin scope（用户/老 flag/企业自己加的 server，不弹批准框，§9.2）；project/local = 工作区共享、
# 受门控 / Pre-trusted origin scopes (no approval prompt); project/local = workspace-shared, gated.
_TRUSTED_ORIGINS: frozenset[SettingsScope] = frozenset({SettingsScope.USER, SettingsScope.FLAG, SettingsScope.POLICY})

# 单点构造校验入口（照 manifest.py 范式）/ Single dict→model adapters.
_MCP_SERVER_ADAPTER: TypeAdapter[MCPServerConfig] = TypeAdapter(MCPServerConfig)
_MCP_INPUT_ADAPTER: TypeAdapter[MCPServerInput] = TypeAdapter(MCPServerInput)


class McpConfigError(Exception):
    """MCP 定义/批准写助手的契约违例（如批准写缺 active workdir）/ Contract violation in MCP config helpers."""


class McpApprovalStatus(StrEnum):
    """单个 MCP server 的批准门控状态（§9.2）/ Approval-gate status of one MCP server。"""

    ENABLED = "enabled"  # 已批准 / 预信任 / bundled 免批准 → 可连接
    DISABLED = "disabled"  # 显式拒绝 / 企业拒绝名单 / 不在白名单 → 不连接
    PENDING = "pending"  # 工作区共享且未决 → 启动时弹批准框（#69）


@dataclass(frozen=True, slots=True)
class ResolvedMcpServer:
    """
    合并解析后的单个 MCP server 定义 / A merged-and-resolved single MCP server definition。

    ``config`` 是校验后的 A2C :class:`MCPServerConfig`（**含占位符、未渲染**）；``ext`` 是剥离出的 VS Code
    扩展字段（如 ``envFile``，交 #65 渲染消费）；``origin`` 为最高定义 scope；``trusted_origin`` 决定是否免
    批准门控（user/flag/policy 免，project/local 受门控）。
    """

    name: str
    config: MCPServerConfig
    ext: dict[str, Any]
    origin: SettingsScope
    trusted_origin: bool


@dataclass(frozen=True, slots=True)
class ResolvedMcpConfig:
    """
    多 scope 合并后的 MCP 定义视图 / The multi-scope merged MCP definition view。

    ``servers`` 按 name 索引；``inputs`` 为去重后的 input **定义**（取值/渲染归 #65）；``errors`` 汇总字段级
    校验错误（不阻断、供诊断呈现，照 §5.6）。
    """

    servers: dict[str, ResolvedMcpServer]
    inputs: list[MCPServerInput] = field(default_factory=list)
    errors: list[SettingsValidationError] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 路径解析（复用 scope.py 的路径根）/ Path resolution (reuses scope.py roots)
# ---------------------------------------------------------------------------
def user_mcp_config_path(env: Mapping[str, str] | None = None) -> Path:
    """user scope ``$XDG_CONFIG_HOME/a2c/mcp.json`` 路径 / Path to the user-scope mcp.json。"""
    return resolve_user_config_dir(env) / MCP_CONFIG_FILENAME


def workdir_mcp_config_path(workdir: Path) -> Path:
    """project scope ``<workdir>/.tfrobot/mcp.json`` 路径（入 git、团队共享）/ project-scope mcp.json path。"""
    return workdir_settings_dir(workdir) / MCP_CONFIG_FILENAME


def workdir_mcp_local_config_path(workdir: Path) -> Path:
    """local scope ``<workdir>/.tfrobot/mcp.local.json`` 路径（不入 git）/ local-scope mcp.local.json path。"""
    return workdir_settings_dir(workdir) / MCP_LOCAL_CONFIG_FILENAME


def _default_managed_dir(platform: str) -> Path:
    """按平台选 managed 目录（镜像 :func:`policy.default_policy_sources` 的分支）/ Per-platform managed dir。"""
    if platform == "darwin":
        return MACOS_MANAGED_DIR
    if platform == "win32":
        return WINDOWS_MANAGED_DIR
    return LINUX_MANAGED_DIR


def managed_mcp_config_path(platform: str | None = None) -> Path:
    """policy scope ``<managed-dir>/managed-mcp.json`` 路径 / Path to the policy-scope managed-mcp.json。"""
    return _default_managed_dir(platform or sys.platform) / MANAGED_MCP_FILENAME


# ---------------------------------------------------------------------------
# 单文件加载（容错）/ Single-file load (tolerant)
# ---------------------------------------------------------------------------
def _err(scope: SettingsScope, fld: str, reason: str, source: str | None) -> SettingsValidationError:
    """构造一条字段级校验错误（缩短重复构造 + 控行宽）/ Build one field-level validation error."""
    return SettingsValidationError(scope=scope, field=fld, reason=reason, source_path=source)


def load_mcp_config_file(path: Path, scope: SettingsScope) -> tuple[dict[str, Any], list[SettingsValidationError]]:
    """
    读取并容错规整单个 ``mcp.json`` 文件为 ``{servers:{}, inputs:[]}`` / Load + tolerantly coerce one mcp.json。

    缺失 → ``({servers:{}, inputs:[]}, [])``；JSON 损坏 / 根非对象 → 空 + 一条错误（**不**备份、**不**清盘，
    照 §5.6 人编文件姿态）；``servers`` 非对象 / ``inputs`` 非数组 → 该字段判空 + 记错（其余仍用）。
    Missing → empty; corrupt / non-object root → empty + one error (file untouched); wrong-typed
    ``servers`` / ``inputs`` → that field emptied + error.
    """
    empty: dict[str, Any] = {"servers": {}, "inputs": []}
    p = Path(path)
    src = str(p)
    if not p.exists():
        return dict(empty), []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("MCP config %s unreadable/corrupt, ignored (kept on disk): %s", p, exc)
        return dict(empty), [_err(scope, "<file>", f"unreadable or corrupt JSON: {exc}", src)]
    if not isinstance(raw, dict):
        return dict(empty), [_err(scope, "<root>", "mcp config root must be an object", src)]

    errors: list[SettingsValidationError] = []
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        if servers is not None:
            errors.append(_err(scope, "servers", "'servers' must be an object", src))
        servers = {}
    inputs = raw.get("inputs")
    if not isinstance(inputs, list):
        if inputs is not None:
            errors.append(_err(scope, "inputs", "'inputs' must be an array", src))
        inputs = []
    return {"servers": servers, "inputs": inputs}, errors


# ---------------------------------------------------------------------------
# 校验单元（字段级容错）/ Validation units (field-level tolerant)
# ---------------------------------------------------------------------------
def _validate_server(
    name: str,
    sdef: Any,
    scope: SettingsScope,
    source: str | None,
) -> tuple[ResolvedMcpServer | None, list[SettingsValidationError]]:
    """
    校验单个 server 定义 → :class:`ResolvedMcpServer`（畸形 → ``None`` + 错误，**不抛**）/ Validate one server。

    map **key 即 server 身份**（VS Code/CC 风格）：注入 ``name=<key>``；若 ``sdef`` 内显式 ``name`` 与 key
    冲突 → 判废。剥离 :data:`_VSCODE_EXT_KEYS` 入 ``ext``（交 #65），其余校验为 ``MCPServerConfig``。
    """
    fld = f"servers.{name}"
    if not isinstance(sdef, Mapping):
        return None, [_err(scope, fld, "server definition must be an object", source)]
    ext = {k: v for k, v in sdef.items() if k in _VSCODE_EXT_KEYS}
    body = {k: v for k, v in sdef.items() if k not in _VSCODE_EXT_KEYS}
    if "name" in body and body["name"] != name:
        reason = f"server 'name' field {body['name']!r} != map key {name!r} (the map key is the canonical identity)"
        return None, [_err(scope, fld, reason, source)]
    body["name"] = name  # key 即身份 / the map key is the identity.
    try:
        cfg = _MCP_SERVER_ADAPTER.validate_python(body)
    except ValidationError as exc:
        return None, [_err(scope, fld, f"invalid MCP server config: {exc}", source)]
    return ResolvedMcpServer(name=name, config=cfg, ext=ext, origin=scope, trusted_origin=scope in _TRUSTED_ORIGINS), []


def _validate_input(idef: Any, scope: SettingsScope, source: str | None) -> tuple[MCPServerInput | None, list[SettingsValidationError]]:
    """校验单个 input 定义 → :class:`MCPServerInput`（畸形 → ``None`` + 错误，**不抛**）/ Validate one input def。"""
    raw_id = idef.get("id") if isinstance(idef, Mapping) else None
    fld = f"inputs.{raw_id}" if isinstance(raw_id, str) else "inputs.<unknown>"
    try:
        inp = _MCP_INPUT_ADAPTER.validate_python(idef)
    except ValidationError as exc:
        return None, [_err(scope, fld, f"invalid input definition: {exc}", source)]
    return inp, []


# ---------------------------------------------------------------------------
# 多 scope 解析 / Multi-scope resolution
# ---------------------------------------------------------------------------
def resolve_mcp_config(
    *,
    active_workdir: Path | None = None,
    env: Mapping[str, str] | None = None,
    flag_config_path: Path | None = None,
    managed_mcp_path: Path | None = None,
    platform: str | None = None,
) -> ResolvedMcpConfig:
    """
    多 scope 加载合并 ``.tfrobot/mcp.json`` + 字段级校验 / Multi-scope load + merge + validate mcp.json。

    合并顺序 low → high = ``[flag, user, active-project, active-local, policy]``（即优先级
    ``policy > local > project > user > flag``，§9.1/§5.5）；**无能力层并集**（敏感面隔离，区别于
    settings.json）。``active_workdir is None`` → project/local 全空，仅 user + flag + policy + 默认。

    - **servers**：按 name **整体替换**（高 scope 同名整体覆盖；server config 是原子单元、**非**深合并），
      ``origin`` = 最高定义 scope，``trusted_origin`` = origin ∈ {user, flag, policy}。
    - **inputs**：按 ``id`` 去重、高 scope 胜（缺 ``id`` 的条目各自保留以便逐条报错）。
    - 单 server / input 畸形 → drop + :class:`SettingsValidationError`，**不 abort**（§5.6 人编文件容错）。

    :param active_workdir: 当前绑定任务的单根（project/local 来源）；``None`` = 空闲。
    :param env: 环境映射（解析 user config dir），默认 ``os.environ``。
    :param flag_config_path: ``--config @file`` 老接口文件（最低优先级，§5.5）。
    :param managed_mcp_path: policy scope ``managed-mcp.json`` 覆盖路径（缺省按平台推导；便于测试）。
    :param platform: 平台标识（缺省 ``sys.platform``）；仅在 ``managed_mcp_path`` 未给时用于推导 managed 目录。
    """
    managed_path = managed_mcp_path if managed_mcp_path is not None else managed_mcp_config_path(platform)

    # (scope, path) 层，低优先级在前 / layers, lowest priority first.
    layers: list[tuple[SettingsScope, Path]] = []
    if flag_config_path is not None:
        layers.append((SettingsScope.FLAG, Path(flag_config_path)))
    layers.append((SettingsScope.USER, user_mcp_config_path(env)))
    if active_workdir is not None:
        layers.append((SettingsScope.PROJECT, workdir_mcp_config_path(active_workdir)))
        layers.append((SettingsScope.LOCAL, workdir_mcp_local_config_path(active_workdir)))
    layers.append((SettingsScope.POLICY, managed_path))

    errors: list[SettingsValidationError] = []
    # 累积原始定义（低→高，后者覆盖前者）；server 整体替换 + 记 origin / source / Raw accumulation.
    raw_servers: dict[str, tuple[Any, SettingsScope, str]] = {}
    raw_inputs: dict[str, tuple[Any, SettingsScope, str]] = {}
    noid = 0

    for scope, path in layers:
        data, errs = load_mcp_config_file(path, scope)
        errors.extend(errs)
        src = str(path)
        for srv_name, sdef in data["servers"].items():
            raw_servers[srv_name] = (sdef, scope, src)  # 高 scope 整体覆盖 → origin = 最高 / high wins.
        for idef in data["inputs"]:
            iid = idef.get("id") if isinstance(idef, Mapping) and isinstance(idef.get("id"), str) else None
            key = iid if iid is not None else f"<noid-{noid}>"
            if iid is None:
                noid += 1
            raw_inputs[key] = (idef, scope, src)  # 同 id 高 scope 胜 / same id, high wins.

    servers: dict[str, ResolvedMcpServer] = {}
    for srv_name, (sdef, scope, src) in raw_servers.items():
        resolved, errs = _validate_server(srv_name, sdef, scope, src)
        errors.extend(errs)
        if resolved is not None:
            servers[srv_name] = resolved

    inputs: list[MCPServerInput] = []
    for _key, (idef, scope, src) in raw_inputs.items():
        resolved_input, errs = _validate_input(idef, scope, src)
        errors.extend(errs)
        if resolved_input is not None:
            inputs.append(resolved_input)

    return ResolvedMcpConfig(servers=servers, inputs=inputs, errors=errors)


# ---------------------------------------------------------------------------
# 批准门控判定 / Approval-gate decision（§9.2）
# ---------------------------------------------------------------------------
def _str_list(settings: Mapping[str, Any], key: str) -> list[str]:
    """从 resolved settings 取字符串数组字段（非 list → ``[]``）/ Read a string-array field (non-list → [])."""
    value = settings.get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def mcp_server_status(name: str, *, settings: Mapping[str, Any], bundled: set[str], trusted_origin: bool) -> McpApprovalStatus:
    """
    判定单个 MCP server 的批准状态（§9.2，**顺序即优先级**）/ Decide one server's approval status。

    优先级（先到先决）/ Priority (first match wins):
    1. ``deniedMcpServers``（企业拒绝名单，policy）→ ``DISABLED``。
    2. ``allowedMcpServers`` 非空（企业白名单，policy）且不在其中 → ``DISABLED``。
    3. ``disabledMcpjsonServers`` → ``DISABLED``（**disabled 优先** over enabled）。
    4. plugin-bundled（``bundled`` 并集，#63 显式 install = trusted）→ ``ENABLED``（免批准）。
    5. ``trusted_origin``（user/flag/policy scope 自定义 server）→ ``ENABLED``（用户自己加的，不弹框）。
    6. ``enabledMcpjsonServers`` → ``ENABLED``。
    7. ``enableAllProjectMcpServers is True`` → ``ENABLED``。
    8. 否则（工作区共享且未决）→ ``PENDING``。

    :param settings: 六层合并后的 resolved settings（含 #56 落地的 MCP 门控字段）。
    :param bundled: 已安装 plugin 携带的 bundled MCP server name 并集（见 :func:`bundled_mcp_server_names`）。
    :param trusted_origin: 该 server 是否来自预信任 scope（见 :attr:`ResolvedMcpServer.trusted_origin`）。
    """
    if name in _str_list(settings, FIELD_DENIED_MCP_SERVERS):
        return McpApprovalStatus.DISABLED
    allowed = _str_list(settings, FIELD_ALLOWED_MCP_SERVERS)
    if allowed and name not in allowed:
        return McpApprovalStatus.DISABLED
    if name in _str_list(settings, FIELD_DISABLED_MCPJSON_SERVERS):
        return McpApprovalStatus.DISABLED
    if name in bundled:
        return McpApprovalStatus.ENABLED
    if trusted_origin:
        return McpApprovalStatus.ENABLED
    if name in _str_list(settings, FIELD_ENABLED_MCPJSON_SERVERS):
        return McpApprovalStatus.ENABLED
    if settings.get(FIELD_ENABLE_ALL_PROJECT_MCP) is True:
        return McpApprovalStatus.ENABLED
    return McpApprovalStatus.PENDING


def gate_mcp_servers(
    resolved: ResolvedMcpConfig,
    settings: Mapping[str, Any],
    bundled: set[str],
) -> dict[str, McpApprovalStatus]:
    """对全部已解析 server 套 :func:`mcp_server_status` / Apply the gate to all resolved servers。"""
    return {
        name: mcp_server_status(name, settings=settings, bundled=bundled, trusted_origin=srv.trusted_origin)
        for name, srv in resolved.servers.items()
    }


def bundled_mcp_server_names(home: Path | None = None, env: Mapping[str, str] | None = None) -> set[str]:
    """
    已安装 plugin 携带的 bundled MCP server name 并集（#64↔#63 账本接缝）/ Union of installed plugins' bundled servers。

    读 ``installed_plugins.json``（#63 物化账本）全记录的 ``bundledMcpServers`` 字段——这些 server 因用户**显式
    install plugin**而 trusted、门控免批准（§9.2 ``mcp_server_status`` 第 4 档）。
    """
    out: set[str] = set()
    for records in load_installed_plugins(home=home, env=env)["plugins"].values():
        for record in records:
            out.update(record.get("bundledMcpServers", []) or [])
    return out


# ---------------------------------------------------------------------------
# 批准写助手（写 local scope）/ Approval write helpers (write to local scope)（§9.2）
# ---------------------------------------------------------------------------
def _require_active_workdir(active_workdir: Path | None) -> Path:
    """批准写须落 local scope = active workdir；缺则 fail-fast（镜像 installer scope 守卫）/ local-scope guard。"""
    if not active_workdir:
        raise McpConfigError("MCP approval write requires an active workdir (local scope); none provided")
    return Path(active_workdir)


def _append_local_mcp_array(active_workdir: Path | None, field_name: str, name: str) -> None:
    """
    把 ``name`` 追加进 local ``settings.local.json`` 的某 MCP 数组字段（持锁原子 RMW + dedup）/ Append to a local array。

    复用 store 原语：:func:`file_lock`（旁车 ``.lock``）+ :func:`load_settings_file`（容错）+ :func:`apply_write`
    （数组**整体替换**写语义，§5.4）+ :func:`atomic_write_json`（``header=None``——人编意图层无写保护头/version）。
    锁内读-改-写杜绝并发丢更新。
    """
    wd = _require_active_workdir(active_workdir)
    path = workdir_local_settings_path(wd)
    with file_lock(path):
        existing, _errors = load_settings_file(path, SettingsScope.LOCAL)
        current = [v for v in existing.get(field_name, []) if isinstance(v, str)]
        if name not in current:
            current.append(name)
        updated = apply_write(existing, {field_name: current})
        atomic_write_json(path, updated)


def approve_mcp_server(name: str, *, active_workdir: Path | None) -> None:
    """批准框 ``[y]es``：追加 ``enabledMcpjsonServers`` 到 local scope（§9.2）/ Approve: append to enabled list (local)。"""
    _append_local_mcp_array(active_workdir, FIELD_ENABLED_MCPJSON_SERVERS, name)


def deny_mcp_server(name: str, *, active_workdir: Path | None) -> None:
    """批准框 ``[n]o``：追加 ``disabledMcpjsonServers`` 到 local scope（§9.2）/ Deny: append to disabled list (local)。"""
    _append_local_mcp_array(active_workdir, FIELD_DISABLED_MCPJSON_SERVERS, name)


def approve_all_project_mcp(*, active_workdir: Path | None) -> None:
    """批准框 ``[a]ll``：``enableAllProjectMcpServers=true`` 写 local scope（§9.2）/ Approve-all: set the bool (local)。"""
    wd = _require_active_workdir(active_workdir)
    path = workdir_local_settings_path(wd)
    with file_lock(path):
        existing, _errors = load_settings_file(path, SettingsScope.LOCAL)
        updated = apply_write(existing, {FIELD_ENABLE_ALL_PROJECT_MCP: True})
        atomic_write_json(path, updated)
