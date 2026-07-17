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
- **取值渲染**（``envFile`` 加载 / ``${env:}`` / inputs 解析链 / keyring / 明文
  state）归 **#65**：本模块产出**带占位符**的定义，``ResolvedMcpServer.ext``（``envFile`` 等 VS Code 扩展）
  + 未渲染占位符是交给 #65 的 handoff。**绝不在此渲染**（§9.1 安全铁律：值不离 Computer）。
- **批准框 TTY 交互**（``[a]/[y]/[n]``）/ ``--approve-all-mcp`` flag / 非交互 pending→skip+WARN 接线归
  **#69**：本模块只提供 :class:`McpApprovalStatus` 判定 + 三个写助手原语。#69 接线另须把
  :attr:`ResolvedMcpConfig.errors`（含畸形 server/input 被 drop —— 如非 ``envFile`` 的 VS Code 扩展撞
  ``MCPServerConfig`` 的 ``extra="forbid"``）**纳入启动 WARN 输出**，否则被 drop 的 server 对用户静默不可见。
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
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
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
    SCOPE_ORDER,
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
from a2c_smcp.computer.settings.store import atomic_write_json, file_lock
from a2c_smcp.utils.bundle_id import resolve_bundle_id
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

# 预信任 origin scope（用户 / CLI 显式传入 / 宿主构造 / 企业自己加的 server，不弹批准框，§9.2 + 审批门档④）；
# project/local = 工作区共享（入 git、随仓库分发）⇒ 受门控。
# Pre-trusted origin scopes (no approval prompt); project/local = workspace-shared (git-tracked) ⇒ gated.
#
# ``EMBED`` 受信依据（Discussion #32 裁决 / §2.5-3）：宿主构造入参 ``Computer(mcp_servers=...)`` 是**代码级
# 显式意图**，与 ``flag`` 同属「调用方显式受信层」。注意受信**不等于**豁免——embed 条目仍**进门迭代**，故
# 档①②③（policy 拒绝名单 / 白名单 / 通用禁用开关）对其适用（用户/管理员保留最终关停权）；只有 project 信任门
# （``enabledMcpjsonServers`` / ``enableAllProjectMcpServers``，档⑤⑥）因档④已放行而不可达。
_TRUSTED_ORIGINS: frozenset[SettingsScope] = frozenset(
    {SettingsScope.USER, SettingsScope.EMBED, SettingsScope.FLAG, SettingsScope.POLICY},
)

# embed 层的 ``source`` 标签：非文件来源，故不是路径 / Source label for the embed layer (not a file path).
_EMBED_SOURCE = "<embed:Computer(mcp_servers=...)>"

# 单点构造校验入口（照 manifest.py 范式）/ Single dict→model adapters.
_MCP_SERVER_ADAPTER: TypeAdapter[MCPServerConfig] = TypeAdapter(MCPServerConfig)
_MCP_INPUT_ADAPTER: TypeAdapter[MCPServerInput] = TypeAdapter(MCPServerInput)


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
    扩展字段（如 ``envFile``，交 #65 渲染消费），以 :class:`~types.MappingProxyType` **只读视图**承载——
    frozen dataclass 只防字段重绑定、不冻 dict 内容，故下沉只读视图杜绝下游原地改污染；``origin`` 为最高定义
    scope；``trusted_origin`` 决定是否免批准门控（user/flag/policy 免，project/local 受门控）。
    """

    name: str
    config: MCPServerConfig
    ext: Mapping[str, Any]
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
    # ext 以只读视图承载（frozen dataclass 不冻 dict 内容；下游 #65 只读消费）/ ext as a read-only view.
    server = ResolvedMcpServer(name=name, config=cfg, ext=MappingProxyType(ext), origin=scope, trusted_origin=scope in _TRUSTED_ORIGINS)
    return server, []


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
def _embed_layer(embed_servers: Iterable[MCPServerConfig]) -> dict[str, Any]:
    """
    宿主构造入参 → mcp.json 形状的 embed 层 / Embedded-constructor args → an mcp.json-shaped embed layer。

    ``Computer(mcp_servers={...})`` 收的是**已校验的模型**，而合并管线吃 raw dict（各层同构、复用
    :func:`_validate_server`）⇒ 此处 ``model_dump(mode="json")`` 回落 raw。往返无损：``model_config``
    的 ``populate_by_name`` 令 by-alias 输出（如 ``envFile``）可被重新吸收。

    identity 由 map key 承载（与文件层一致）：key = ``cfg.name``。embed 层**无 inputs**——构造入参
    ``Computer(inputs=...)`` 是另一条通路（直接入 ``_inputs`` 池），不经本层。
    """
    return {"servers": {cfg.name: cfg.model_dump(mode="json") for cfg in embed_servers}, "inputs": []}


def resolve_mcp_config(
    *,
    env: Mapping[str, str] | None = None,
    flag_config_path: Path | None = None,
    embed_servers: Iterable[MCPServerConfig] | None = None,
    managed_mcp_path: Path | None = None,
    platform: str | None = None,
) -> ResolvedMcpConfig:
    """
    多 scope 加载合并 ``.tfrobot/mcp.json`` + 字段级校验 / Multi-scope load + merge + validate mcp.json。

    合并顺序**派生自** :data:`~a2c_smcp.computer.settings.schema.SCOPE_ORDER`（协议 §2.5-3 唯一权威，
    低 → 高 ``user < project < local < embed < flag < policy``），与 settings.json 的
    :func:`~a2c_smcp.computer.settings.scope.resolve_settings` **同序**——协议明令两套来源 MUST 一致。
    **无能力层并集**（敏感面隔离，区别于 settings.json）。#116：project/local 无条件锚定进程
    ``os.getcwd()`` 的 ``.tfrobot/mcp[.local].json``（cwd 恒存在；文件缺失 → 层不贡献）。

    **本函数的 origin 恒 ∈ 非-plugin**（无 plugin 入参、``SettingsScope`` 无 ``PLUGIN`` 成员，见
    :data:`~a2c_smcp.computer.settings.schema.SCOPE_ORDER` 的「为何无 PLUGIN 成员」）。这是**结构性**保证，
    不是约定——故本函数产出即协议 §2.5-5 的「带 origin 的运行期权威配置集」中的非-plugin 部分，
    :func:`non_plugin_declared_bundle_ids` 据此无需过滤。

    - **servers**：按 name **整体替换**（高 scope 同名整体覆盖；server config 是原子单元、**非**深合并），
      ``origin`` = 最高定义 scope，``trusted_origin`` = origin ∈ :data:`_TRUSTED_ORIGINS`。
    - **inputs**：按 ``id`` 去重、高 scope 胜（缺 ``id`` 的条目各自保留以便逐条报错）。
    - 单 server / input 畸形 → drop + :class:`SettingsValidationError`，**不 abort**（§5.6 人编文件容错）。

    :param env: 环境映射（解析 user config dir），默认 ``os.environ``。
    :param flag_config_path: ``--mcp-config <file>``（flag 层 mcp.json，含 ``servers``/``inputs``；
        **次高优先级、仅低于 policy**，§2.5-3）。历史 ``--config`` 老接口曾把本层排**最低**，协议已废止该形态。
    :param embed_servers: 宿主构造入参 ``Computer(mcp_servers=...)``（embed 层，§2.5-3）。
    :param managed_mcp_path: policy scope ``managed-mcp.json`` 覆盖路径（缺省按平台推导；便于测试）。
    :param platform: 平台标识（缺省 ``sys.platform``）；仅在 ``managed_mcp_path`` 未给时用于推导 managed 目录。
    """
    managed_path = managed_mcp_path if managed_mcp_path is not None else managed_mcp_config_path(platform)

    # 各 scope 的数据源（未排序；顺序由 SCOPE_ORDER 唯一决定）；project/local 锚 cwd（#116）。
    # Per-scope sources (unordered here — SCOPE_ORDER alone decides precedence); cwd-anchored.
    cwd = Path(os.getcwd())
    file_sources: dict[SettingsScope, Path] = {
        SettingsScope.USER: user_mcp_config_path(env),
        SettingsScope.PROJECT: workdir_mcp_config_path(cwd),
        SettingsScope.LOCAL: workdir_mcp_local_config_path(cwd),
        SettingsScope.POLICY: managed_path,
    }
    if flag_config_path is not None:
        file_sources[SettingsScope.FLAG] = Path(flag_config_path)

    errors: list[SettingsValidationError] = []
    # 累积原始定义（低→高，后者覆盖前者）；server 整体替换 + 记 origin / source / Raw accumulation.
    raw_servers: dict[str, tuple[Any, SettingsScope, str]] = {}
    raw_inputs: dict[str, tuple[Any, SettingsScope, str]] = {}
    noid = 0

    for scope in SCOPE_ORDER:
        if scope is SettingsScope.EMBED:
            if not embed_servers:
                continue
            data, src = _embed_layer(embed_servers), _EMBED_SOURCE
        else:
            path = file_sources.get(scope)
            if path is None:
                continue
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


def non_plugin_declared_bundle_ids(
    *,
    env: Mapping[str, str] | None = None,
    flag_config_path: Path | None = None,
    embed_servers: Iterable[MCPServerConfig] | None = None,
) -> set[str]:
    """
    §4.9.1-2 回收判据「X 非用户声明」项的**完整**数据源 / The complete source for the criterion's 2nd term。

    = 运行期权威配置集中 ``origin != plugin`` 的 bundle_id 集（协议 §2.5-5 + Discussion #32 裁决）。
    即 :func:`resolve_mcp_config` 的全部产出——**每条非-plugin 挂载路径都在其中留下携带正确 origin 的条目**：
    durable scopes（user/project/local/policy）、flag（``--mcp-config``）、embed（``Computer(mcp_servers=...)``）。

    **``origin != plugin`` 无需过滤——由构造保证**：本函数与 :func:`resolve_mcp_config` 均**无 plugin 入参**，
    且 ``SettingsScope`` **无 ``PLUGIN`` 成员**（见 :data:`~a2c_smcp.computer.settings.schema.SCOPE_ORDER`），
    故 plugin origin 在此**物理上不可能出现**。plugin 声明基线层不经本 resolve（经 transient ``amount_server``
    从 manifest 挂载）。

    ⚠️ **两条被协议钉死为 MUST NOT 的歧路**（§4.9.1-2，本仓实测证伪，勿再尝试）：

    1. **MUST NOT 用裸 manager 活跃集**（无 origin ⇒ flag / embed / plugin 三条挂载路径在其中**可观测同形**
       ⇒ 连坐停摘用户 / 宿主自有 server）；
    2. **MUST NOT 用「活跃集 ∖ 全 plugin 声明集」差集**（回收候选**必然**落在 plugin 声明集内，该差集对其
       **恒空**，判定退化为死代码——#153 期间已实现并实测：守卫仍红）。

    :param env: 环境映射（解析 user config dir），默认 ``os.environ``。
    :param flag_config_path: ``--mcp-config <file>``（flag 层 mcp.json）。**MUST 传**——漏传会让经 flag
        挂载的用户 server 退回「非用户声明」而被连坐（正是 #153 遗留缺口的形状）。
    :param embed_servers: 宿主构造入参 ``Computer(mcp_servers=...)``（embed 层）。同上，**MUST 传**。
    """
    snapshot = resolve_mcp_config(env=env, flag_config_path=flag_config_path, embed_servers=embed_servers)
    return {resolve_bundle_id(srv.config) for srv in snapshot.servers.values()}


# ---------------------------------------------------------------------------
# 批准门控判定 / Approval-gate decision（§9.2）
# ---------------------------------------------------------------------------
def _str_list(settings: Mapping[str, Any], key: str) -> list[str]:
    """从 resolved settings 取字符串数组字段（非 list → ``[]``）/ Read a string-array field (non-list → [])."""
    value = settings.get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def mcp_server_status(name: str, *, settings: Mapping[str, Any], trusted_origin: bool) -> McpApprovalStatus:
    """
    判定单个 MCP server 的批准状态（**顺序即优先级**）/ Decide one server's approval status。

    优先级（先到先决）/ Priority (first match wins):
    1. ``deniedMcpServers``（企业拒绝名单，policy）→ ``DISABLED``。
    2. ``allowedMcpServers`` 非空（企业白名单，policy）且不在其中 → ``DISABLED``。
    3. ``disabledMcpjsonServers`` → ``DISABLED``（**disabled 优先** over enabled）。
    4. ``trusted_origin``（user/embed/flag/policy origin）→ ``ENABLED``（用户/宿主/CLI/企业自己加的，不弹框）。
    5. ``enabledMcpjsonServers`` → ``ENABLED``。
    6. ``enableAllProjectMcpServers is True`` → ``ENABLED``。
    7. 否则（工作区共享且未决）→ ``PENDING``。

    **档④ 与 embed**（Discussion #32 裁决）：``embed`` 已入 :data:`_TRUSTED_ORIGINS` ⇒ 命中档④。注意它**进门
    迭代**、只是在档④ 被放行——故档①②③（policy 拒绝名单 / 白名单 / 通用禁用开关）**对 embed 适用**
    （用户/管理员保留最终关停权；embed 无 plugin 那样的整体 enable/disable 兜底，不可豁免）；档⑤⑥ 的 project
    信任门对 embed 不可达。

    **#148（P0 安全）**：历史「plugin-bundled 名集免批准」档位（`if name in bundled → ENABLED`）**已删除**。
    其唯一可达路径 = project/local 声明的 server 借用某已装 plugin 的 server 名 = 100% 借名跳过审批门。
    审批门 **MUST NOT 依赖物化账本的名集**，plugin 声明 **MUST NOT 进入迭代**（其可信性由 install ∧ enable 门
    保证，不走 settings 信任面）——见协议 ``runtime-contract.md §5 item 10`` 与
    ``guides/mcp-approval-gate-alignment.md``。

    **F8 由结构保证、无需过滤器**（#154 裁决）：本函数只判 :func:`resolve_mcp_config` 的产出，而该 resolve
    **无 plugin 入参**、``SettingsScope`` **无 ``PLUGIN`` 成员**（见
    :data:`~a2c_smcp.computer.settings.schema.SCOPE_ORDER`）⇒ plugin origin **物理上不可能进入迭代**。
    在此写 ``if origin is PLUGIN: continue`` 会是**永假守卫**（死代码），且新增一个无生产者的 ``PLUGIN``
    成员正是档④ 那个「进门后豁免」形状复活的诱因。**验收信号取缺席**（「成员不存在」比「文档说别用」可靠）。

    :param settings: 多层合并后的 resolved settings（含 #56 落地的 MCP 门控字段）。
    :param trusted_origin: 该 server 是否来自预信任 scope（见 :attr:`ResolvedMcpServer.trusted_origin`）。
    """
    if name in _str_list(settings, FIELD_DENIED_MCP_SERVERS):
        return McpApprovalStatus.DISABLED
    allowed = _str_list(settings, FIELD_ALLOWED_MCP_SERVERS)
    if allowed and name not in allowed:
        return McpApprovalStatus.DISABLED
    if name in _str_list(settings, FIELD_DISABLED_MCPJSON_SERVERS):
        return McpApprovalStatus.DISABLED
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
) -> dict[str, McpApprovalStatus]:
    """对全部已解析 server 套 :func:`mcp_server_status` / Apply the gate to all resolved servers。"""
    return {
        name: mcp_server_status(name, settings=settings, trusted_origin=srv.trusted_origin)
        for name, srv in resolved.servers.items()
    }


# ---------------------------------------------------------------------------
# 批准写助手（写 local scope）/ Approval write helpers (write to local scope)（§9.2）
# ---------------------------------------------------------------------------
def _local_settings_write_path() -> Path:
    """批准写落点：``<cwd>/.tfrobot/settings.local.json``（#116：cwd 恒存在，无 fail-fast）/ local write path。"""
    return workdir_local_settings_path(Path(os.getcwd()))


def _append_local_mcp_array(field_name: str, name: str) -> None:
    """
    把 ``name`` 追加进 local ``settings.local.json`` 的某 MCP 数组字段（持锁原子 RMW + dedup）/ Append to a local array。

    复用 store 原语：:func:`file_lock`（旁车 ``.lock``）+ :func:`load_settings_file`（容错）+ :func:`apply_write`
    （数组**整体替换**写语义，§5.4）+ :func:`atomic_write_json`（``header=None``——人编意图层无写保护头/version）。
    锁内读-改-写杜绝并发丢更新。
    """
    path = _local_settings_write_path()
    with file_lock(path):
        existing, _errors = load_settings_file(path, SettingsScope.LOCAL)
        current = [v for v in existing.get(field_name, []) if isinstance(v, str)]
        if name not in current:
            current.append(name)
        updated = apply_write(existing, {field_name: current})
        atomic_write_json(path, updated)


def approve_mcp_server(name: str) -> None:
    """批准框 ``[y]es``：追加 ``enabledMcpjsonServers`` 到 local scope（§9.2）/ Approve: append to enabled list (local)。"""
    _append_local_mcp_array(FIELD_ENABLED_MCPJSON_SERVERS, name)


def deny_mcp_server(name: str) -> None:
    """批准框 ``[n]o``：追加 ``disabledMcpjsonServers`` 到 local scope（§9.2）/ Deny: append to disabled list (local)。"""
    _append_local_mcp_array(FIELD_DISABLED_MCPJSON_SERVERS, name)


def approve_all_project_mcp() -> None:
    """批准框 ``[a]ll``：``enableAllProjectMcpServers=true`` 写 local scope（§9.2）/ Approve-all: set the bool (local)。"""
    path = _local_settings_write_path()
    with file_lock(path):
        existing, _errors = load_settings_file(path, SettingsScope.LOCAL)
        updated = apply_write(existing, {FIELD_ENABLE_ALL_PROJECT_MCP: True})
        atomic_write_json(path, updated)


# ---------------------------------------------------------------------------
# scope-aware 持久写层（单个 server 定义的 upsert / remove）/ Scope-aware durable write layer（#136）
# ---------------------------------------------------------------------------
# 对齐 rust-sdk ``settings/config/write_target.rs``（``WriteScope`` + 写目标解析）与 ``config/crud.rs``
# （``ConfigEdit`` / ``update_config``）。**纯逻辑写层**：只按 name 写/删 raw 未渲染定义，不接 Computer /
# MCP manager / 网络（bundle_id→name 解析、运行期物化归 ②）。落盘复用批准写助手同款原语（``file_lock`` +
# ``load_mcp_config_file`` + ``atomic_write_json``）。#135 双路径地基。
# Aligns rust ``write_target.rs`` / ``config/crud.rs``. Pure logic write layer: writes/removes a
# single raw (un-rendered) server definition by name; no Computer / MCP manager / network wiring.


class McpWriteScope(StrEnum):
    """
    可写 scope 子集（逐字对齐 rust ``WriteScope``）/ The writable scope subset (mirrors rust ``WriteScope``)。

    映射到 :class:`SettingsScope` 的三个**可写**层与落点：``LOCAL`` → ``<cwd>/.tfrobot/mcp.local.json``
    （不入 git）、``PROJECT`` → ``<cwd>/.tfrobot/mcp.json``（入 git、团队共享）、``USER`` →
    ``$XDG_CONFIG_HOME/a2c/mcp.json``（全局）。``policy`` / ``flag`` 是**只读**来源，不在此列。
    """

    LOCAL = "local"
    PROJECT = "project"
    USER = "user"


class McpWriteTargetError(RuntimeError):
    """
    写目标非法（对应 rust ``WriteTargetError::Synthesized``）/ Illegal write target。

    唯一触发点（#148 / 指南 §5）：:meth:`Computer.aremove_server` 试图 **durable 删除**一个「运行期活跃却
    无任何用户侧 mcp.json 声明」的 server —— 它是 plugin / 治理**投影**（其真相在 ``installed_plugins.json``
    账本，应经 ``plugin uninstall`` 整体停用，而非经 mcp.json CRUD 单独打掉，否则产生半态）。**写层不再**因
    「同 bundle_id 已由 plugin 提供」拒写 upsert（用户覆盖权，见 :func:`upsert_mcp_server`）。
    """


class McpConfigCorruptError(RuntimeError):
    """
    写目标 ``mcp.json`` **结构损坏**、拒绝覆盖以免销毁既有内容 / Refuse to clobber a structurally-corrupt target。

    :func:`load_mcp_config_file` 对损坏文件（不可解析 JSON / 根非对象 / ``servers`` 非对象 / ``inputs`` 非数组）
    按契约返回**空规整视图 + 错误**且**保留原文件不清盘**。写层若无视这些错误直接以 ``{servers, inputs}`` 整体
    覆盖，会**永久销毁**用户在该文件里的其余 server 定义 / inputs（且无备份）。故 :func:`upsert_mcp_server` 遇
    结构性错误**抛此异常、绝不写**（读层容错、写层 fail-fast——二者不冲突：容错是"读时不因一处坏而全废"，
    fail-fast 是"写时不盲目覆盖不可解析的目标"）。由 ② / REPL 捕获并提示用户修复。
    """


@dataclass(frozen=True, slots=True)
class McpUpsertResult:
    """
    :func:`upsert_mcp_server` 的结果 / The result of :func:`upsert_mcp_server`。

    ``scope`` 为**实际落盘** scope（新声明 = 请求 scope；改已有 = 其 origin scope，见函数文档）；``changed``
    表示内容是否**真的**变化（旧定义 == 新 body → ``False`` 且不落盘、不 churn），供 ② 决定是否 bump config
    revision（对齐 rust「仅内容真变才 bump」）。
    """

    scope: McpWriteScope
    changed: bool


# McpWriteScope ↔ SettingsScope 双向映射（值等价，纯语义收窄）/ Bidirectional map (values are equal).
_WRITE_SCOPE_TO_SETTINGS: dict[McpWriteScope, SettingsScope] = {
    McpWriteScope.USER: SettingsScope.USER,
    McpWriteScope.PROJECT: SettingsScope.PROJECT,
    McpWriteScope.LOCAL: SettingsScope.LOCAL,
}
_SETTINGS_TO_WRITE_SCOPE: dict[SettingsScope, McpWriteScope] = {v: k for k, v in _WRITE_SCOPE_TO_SETTINGS.items()}

# remove 扫描的可写 scope（policy 只读、不删；顺序仅影响遍历，不影响结果）/ Writable scopes scanned by remove.
_WRITABLE_SCOPES: tuple[McpWriteScope, ...] = (McpWriteScope.USER, McpWriteScope.PROJECT, McpWriteScope.LOCAL)


def is_writable_origin(scope: SettingsScope) -> bool:
    """
    该 origin scope 的声明是否**可被 SDK 删改** / Whether declarations of this origin scope are writable by the SDK。

    可写 = ``user`` / ``project`` / ``local``（有 ``mcp.json`` 落点）；只读 = ``policy``（企业托管）、``flag``
    （``--mcp-config`` 命令行文件）、``embed``（宿主构造入参——根本不在盘上）。

    **纯委托** :data:`_SETTINGS_TO_WRITE_SCOPE`（可写性的单一权威），**勿写 DRY 副本**：判据分叉会让写层与
    「能不能删」的判定不一致（#142 ``is_valid_bundle_id`` 同款教训）。

    **注意「只读」≠「优先级低」**：``flag``/``embed`` 优先级**次高**（§2.5-3）却不可写——二者正交。
    """
    return scope in _SETTINGS_TO_WRITE_SCOPE


def mcp_write_path(scope: McpWriteScope, *, env: Mapping[str, str] | None = None) -> Path:
    """
    某写 scope 的 ``mcp.json`` 落点路径 / The ``mcp.json`` write path for a given scope。

    project/local 锚定进程 ``os.getcwd()``（#116，cwd 恒存在）；user 走 ``$XDG_CONFIG_HOME/a2c``（``env``
    覆盖，便于测试隔离）。复用既有路径原语 :func:`user_mcp_config_path` / :func:`workdir_mcp_config_path` /
    :func:`workdir_mcp_local_config_path`。
    """
    if scope is McpWriteScope.USER:
        return user_mcp_config_path(env)
    cwd = Path(os.getcwd())
    if scope is McpWriteScope.PROJECT:
        return workdir_mcp_config_path(cwd)
    return workdir_mcp_local_config_path(cwd)  # LOCAL


def _write_servers_map(path: Path, servers: dict[str, Any], inputs: list[Any]) -> None:
    """
    原子重写单个 ``mcp.json`` 的规整视图 ``{servers, inputs}`` / Atomically rewrite one mcp.json's normalized view。

    经 :func:`load_mcp_config_file` 规整（未知顶层 key 如 ``$schema`` 不保留——``mcp.json`` 的 SDK 权威形状
    即 ``{servers, inputs}``）；``atomic_write_json`` 无 ``header``（``mcp.json`` 是人编意图层，无写保护头/version，
    照批准写助手姿态）。**调用方须已持 :func:`file_lock`**。
    """
    atomic_write_json(path, {"servers": servers, "inputs": inputs})


def upsert_mcp_server(
    name: str,
    body: Mapping[str, Any],
    *,
    scope: McpWriteScope,
    env: Mapping[str, str] | None = None,
) -> McpUpsertResult:
    """
    持锁原子 upsert **单个** MCP server 定义到指定 / origin scope / Locked atomic upsert of one server def。

    对齐 rust ``add_or_update_server_in_scope``：

    - **写 raw 未渲染 ``body``**（保留 ``${input:}`` / ``${env:}``，**D1 铁律：绝不写渲染后 secret**，§9.1 值不
      离 Computer）；``servers.<name>`` **整体替换**（配置是原子单元，**非**深合并——刻意绕开 :func:`apply_write`
      的嵌套 dict 递归合并）。
    - **改已有 server 恒落其 origin scope**：先经 :func:`resolve_mcp_config` **快照**查 ``name`` 的 origin，若其
      origin ∈ 可写 scope（user/project/local）则写回**该 scope**（忽略 ``scope`` 入参）；``scope`` 入参**仅对
      新声明**生效，杜绝跨 scope 漂移。origin 判定基于**锁外快照**（轻微 TOCTOU：并发进程可在快照与取锁间改动
      声明；实际写在锁内原子）。**注意**：若 origin 为只读 **policy**（``managed-mcp.json``，读优先级最高）则回落
      写请求 scope——但该写会被 policy 定义在 :func:`resolve_mcp_config` 中**遮蔽**（``changed`` 为 ``True`` 而有效
      配置不变，属 rust parity 行为：policy 非可写目标、等同新声明）。

      ⚠️ **本处的 origin 快照 MUST NOT 串入 ``flag_config_path`` / ``embed_servers``**（#154）：``:608`` 刻意只
      解析 durable 层（user/project/local/policy）。理由——flag/embed 均**非可写目标**
      （``_SETTINGS_TO_WRITE_SCOPE`` 无其条目），把它们喂进来只会让 ``target`` 回落到请求 scope 并**额外**造出
      一条被 flag/embed 遮蔽的重复声明（写成功、有效配置不变），比现状更坏。**已知代价**（follow-up）：REPL
      ``server add`` 因此看不见 flag 层，写 user scope 报成功、却被 flag 层静默遮蔽——与 policy 同形，但 policy
      至少已在上文写明「遮蔽」。
    - **不因「同 bundle_id 已由 plugin 提供」而拒写**（#148 / 指南 §5）：用户在 mcp.json 声明同 bundle_id 的 server
      正是优先序赋予的**覆盖权**（用户侧 scope > plugin 声明基线），历史「bundled 名拒写」短路**已删除**。SDK MAY
      在别处提示「该 bundle_id 已由 plugin 提供、你的声明将覆盖它」，但**写层不阻止**。
    - **损坏目标拒写**：目标 ``mcp.json`` 结构损坏（不可解析 / 根非对象 / ``servers``|``inputs`` 类型错）→ 抛
      :class:`McpConfigCorruptError`，**绝不覆盖**（否则销毁既有内容，见该异常文档）。
    - **内容未变 = no-op**：目标 scope 现存定义与 ``body`` 逐字相等 → 不落盘、``changed=False``（对齐 rust 仅
      真变才 bump revision）。
    - **规整副作用**：落盘经 :func:`load_mcp_config_file` 规整为 ``{servers, inputs}``——目标文件的**未知顶层
      key**（如 IDE 补全用的 ``$schema``）在改写时**不保留**（``mcp.json`` 的 SDK 权威形状即 ``{servers, inputs}``）。

    :param name: server 身份（``mcp.json`` map key）/ the server identity (mcp.json map key).
    :param body: **未渲染** server 定义体（不含 map key；``name`` 由 key 承载）/ the un-rendered server body.
    :param scope: 请求写 scope（仅新声明生效）/ requested write scope (honored only for a new declaration).
    :param env: 环境映射（解析 user config dir + origin 快照），默认 ``os.environ``。
    :raises McpConfigCorruptError: 目标 ``mcp.json`` 结构损坏（拒绝覆盖以免销毁既有内容）。
    :returns: :class:`McpUpsertResult`（实际落盘 scope + 是否真写）。
    """
    # 改已有恒落 origin scope；新声明（或 origin 只读 policy/flag）落请求 scope / existing → origin, else → requested.
    target = scope
    existing = resolve_mcp_config(env=env).servers.get(name)
    if existing is not None:
        origin_write = _SETTINGS_TO_WRITE_SCOPE.get(existing.origin)
        if origin_write is not None:
            target = origin_write

    raw_body = dict(body)  # 脱离调用方映射；raw、未渲染 / detach caller's mapping; raw, un-rendered.
    path = mcp_write_path(target, env=env)
    settings_scope = _WRITE_SCOPE_TO_SETTINGS[target]
    with file_lock(path):
        data, errors = load_mcp_config_file(path, settings_scope)  # 容错读 {servers, inputs}
        if errors:
            # 结构损坏：读层已"空视图 + 保留原文件"；写层绝不整体覆盖（否则销毁既有 server/inputs、无备份）。
            reasons = "; ".join(e.reason for e in errors)
            raise McpConfigCorruptError(f"refuse to upsert {name!r}: target {path} is structurally corrupt: {reasons}")
        servers = dict(data["servers"])
        if servers.get(name) == raw_body:
            return McpUpsertResult(scope=target, changed=False)  # 内容未变 → 不写、不 churn
        servers[name] = raw_body  # 整体替换（非深合并）；其余 server（含读层未校验的原样条目）逐字保留。
        _write_servers_map(path, servers, data["inputs"])
    return McpUpsertResult(scope=target, changed=True)


def remove_mcp_server(name: str, *, env: Mapping[str, str] | None = None) -> bool:
    """
    从**所有可写 scope** 删除同名 server 声明 / Remove a server declaration from all writable scopes。

    对齐 rust ``remove_server`` 的 name 删声明段（bundle_id→name 解析 + bundled 身份拒删归 ②，那里有 Computer
    上下文）。逐 scope（user/project/local，**不含 policy**）持锁原子删 ``servers.<name>``（``mcp.json`` name-keyed，
    真删干净）；无匹配 scope → 跳过（不创建目录/锁、不 normalize）；全无匹配 → no-op。

    **损坏 scope 跳过而非覆盖**：某 scope 的 ``mcp.json`` 结构损坏时 :func:`load_mcp_config_file` 返回空视图 +
    错误且保留原文件；此处**跳过该 scope + WARN**（best-effort，不重写以免销毁其余内容/丢弃畸形 inputs），继续
    其他 scope（区别于 :func:`upsert_mcp_server` 的单目标 fail-fast——remove 是跨 scope 尽力清理）。损坏 scope 里
    若仍声明着该 server，删除对它是"不彻底"的，但保住用户文件优先——用户修好文件后重跑 remove 即可清净。

    :param name: 待删 server 名 / the server name to remove.
    :param env: 环境映射（解析 user config dir），默认 ``os.environ``。
    :returns: 是否从至少一个 scope 删除了声明 / whether it removed a declaration from ≥1 scope。
    """
    removed = False
    for wscope in _WRITABLE_SCOPES:
        path = mcp_write_path(wscope, env=env)
        if not path.exists():
            continue  # 该 scope 无 mcp.json → 无可删，不创建 .lock/目录、不触碰 / skip untouched.
        settings_scope = _WRITE_SCOPE_TO_SETTINGS[wscope]
        with file_lock(path):
            data, errors = load_mcp_config_file(path, settings_scope)
            if errors:
                # 结构损坏 → 跳过、不重写（保住既有内容）；best-effort 跨 scope 清理容忍局部不彻底。
                logger.warning("MCP config %s corrupt, skipped during remove of %r (kept on disk)", path, name)
                continue
            if name not in data["servers"]:
                continue  # 该 scope 未声明 → no-op（不重写、不 normalize）/ not declared here → no-op.
            servers = {k: v for k, v in data["servers"].items() if k != name}
            _write_servers_map(path, servers, data["inputs"])
            removed = True
    return removed
