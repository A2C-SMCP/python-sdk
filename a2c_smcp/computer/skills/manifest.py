# -*- coding: utf-8 -*-
# filename: manifest.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Plugin manifest 文件式解析：marketplace.json + plugin.json + mcp-servers/<n>.json（v0.2.1 #63）
Plugin manifest file parsing: marketplace.json + plugin.json + mcp-servers/<n>.json (v0.2.1 #63).

协议依据 / Protocol: tfrobot-marketplace protocol-v1 §3（marketplace.json 字段）/ §4（plugin entry / version
                      优先级）/ §5（plugin source 5 类）；mcp-servers 协议 §1/§2（文件式、文件名=name）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §3.2–3.5 / §3.3（bundled MCP server）。

本模块是 **manifest 纯解析器唯一权威层**（无 git / 无 registry / 无 MCP manager 副作用），供 plugin
install/enable（:mod:`a2c_smcp.computer.settings.installer`）与 :mod:`~a2c_smcp.computer.skills.staging`
（marketplace git staging）**共同消费**——单向 ``staging → manifest``，无重复解析器。**刻意保持 leaf**：本模块
**绝不** import :mod:`~a2c_smcp.computer.skills.staging`（staging 顶层 import ``settings.schema``；且 staging 现
import 本模块，故反向耦合即构成真环）。
This is the single authoritative manifest parser (no git / registry / MCP side effects), consumed by both the
plugin installer and skills.staging (one-way ``staging → manifest``). Kept strictly leaf — it MUST NOT import
skills.staging (which now imports this module, so a back-edge would be a real cycle).

**显式延后 / Deferred**：
- **bundled MCP server inputs.json 入池消歧**（§9.3 D2 前缀）归 #65——本模块**枚举 server 时排除**
  ``inputs.json``，不解析 inputs 池。

**strict mode 组件路径覆写 / 冲突检测**（§4.3/§4.4，loading-behavior §1/§4/§6；#80）由本模块的纯函数
:func:`entry_is_strict` / :func:`check_strict_conflict` / :func:`resolve_skill_override_dirs` 承载，供 staging
（marketplace 注册）与 installer（plugin install 硬失败）共同消费。本 SDK 仅消费 ``skills`` + ``mcp-servers``，
冲突检测仍按规范判**全六组件字段**（:data:`COMPONENT_FIELDS`），但覆写**路径扫描仅 ``skills``**。

**SKILL 发现容器集单一权威**（#271）：约定 ``skills/`` + 声明覆写的合成由
:func:`plugin_skill_container_dirs` 单点承载，staging（装载扫描）与 installer（enable 预检计数）共同消费。
:mod:`~a2c_smcp.computer.skills.validator` **刻意不复用**——它要对每项覆写产出 file/loc 级 error（越界 / 缺失），
而装载路径是 WARN 跳过，二者语义不同（见该模块对应注释）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.skills.sources import DEFAULT_PLUGIN_ROOT, safe_exception_text
from a2c_smcp.utils.git_url import (
    escape_unsafe_chars,
    redact_git_urls_in_text,
    sanitize_path_for_diagnostics,
)
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import is_within

logger = get_logger(__name__)

# manifest 布局常量（协议 marketplace-v1 §2.1/§3.1 + mcp-servers §1）/ manifest layout constants。
MARKETPLACE_MANIFEST_DIR = ".tfrobot-plugin"  # marketplace.json / plugin.json 所在目录（镜像 CC .claude-plugin）
MARKETPLACE_MANIFEST = "marketplace.json"  # 仓库级 manifest
PLUGIN_MANIFEST = "plugin.json"  # plugin 级 manifest（仅元数据）
MCP_SERVERS_SUBDIR = "mcp-servers"  # plugin 携带 MCP server 的文件式子树（§3.3）
MCP_INPUTS_FILENAME = "inputs.json"  # plugin 范围 inputs 池定义（枚举 server 时排除；入池归 #65）
SKILLS_SUBDIR = "skills"  # plugin 内 SKILL 子树约定目录（SKILL 协议 §2；单一权威，staging 再导出）

# 单点构造校验入口：dict → MCPServerConfig union（按 type 分型，pydantic v2）/ Single dict→config adapter.
_MCP_SERVER_ADAPTER: TypeAdapter[MCPServerConfig] = TypeAdapter(MCPServerConfig)


class PluginManifestError(Exception):
    """manifest / mcp-servers 文件解析或校验失败（致命，§3.3：JSON 解析失败不降级）/ Manifest parse/validate failure (fatal)."""


# ── JSON 读取（leaf-local；与 staging 同语义，刻意不复用以保持 leaf）/ leaf-local JSON read ──────────
def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    """读 JSON 文件并要求根为对象 / Read a JSON file requiring an object root（失败 → :class:`PluginManifestError`）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PluginManifestError(
            f"{what} unreadable/invalid at {sanitize_path_for_diagnostics(str(path))}: "
            f"{safe_exception_text(e)}"
        ) from e
    if not isinstance(data, dict):
        raise PluginManifestError(f"{what} root is not an object: {sanitize_path_for_diagnostics(str(path))}")
    return data


# ── marketplace.json（仓库级）/ marketplace-level manifest ────────────────────
def read_marketplace_manifest(catalog_dir: Path) -> dict[str, Any]:
    """读 ``<catalog>/.tfrobot-plugin/marketplace.json``（缺失/损坏 → 抛）/ Read the marketplace manifest (raise if absent)。"""
    path = catalog_dir / MARKETPLACE_MANIFEST_DIR / MARKETPLACE_MANIFEST
    if not path.is_file():
        raise PluginManifestError(f"marketplace manifest not found: {sanitize_path_for_diagnostics(str(path))}")
    return _read_json_object(path, what="marketplace manifest")


def iter_plugin_entries(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """取 ``manifest.plugins`` 中的对象条目（非数组 → []，非对象条目跳过）/ Object entries under ``manifest.plugins``。"""
    plugins = manifest.get("plugins")
    if not isinstance(plugins, Sequence) or isinstance(plugins, str | bytes):
        return []
    return [e for e in plugins if isinstance(e, Mapping)]


def find_plugin_entry(manifest: Mapping[str, Any], plugin_name: str) -> Mapping[str, Any] | None:
    """按 ``entry.name == plugin_name`` 定位 plugin 条目（marketplace-v1 §4.1）/ Locate a plugin entry by name。"""
    for entry in iter_plugin_entries(manifest):
        name = entry.get("name")
        if isinstance(name, str) and name.strip() == plugin_name:
            return entry
    return None


def plugin_root_base(manifest: Mapping[str, Any]) -> str:
    """取 ``metadata.pluginRoot``（缺省 :data:`DEFAULT_PLUGIN_ROOT`）/ Resolve the pluginRoot base prefix。"""
    md = manifest.get("metadata")
    if isinstance(md, Mapping):
        pr = md.get("pluginRoot")
        if isinstance(pr, str) and pr.strip():
            return pr.strip()
    return DEFAULT_PLUGIN_ROOT


# ── plugin.json（plugin 级，仅元数据）/ plugin-level metadata ──────────────────
def read_plugin_metadata(plugin_root: Path) -> dict[str, Any]:
    """读 ``<plugin>/.tfrobot-plugin/plugin.json``（best-effort，缺失/损坏 → ``{}``）/ Read plugin.json metadata best-effort。"""
    path = plugin_root / MARKETPLACE_MANIFEST_DIR / PLUGIN_MANIFEST
    if not path.is_file():
        return {}
    try:
        return _read_json_object(path, what="plugin manifest")
    except PluginManifestError as e:
        # plugin.json 仅供 version / 显示名兜底，损坏不致命（SKILL/server 由路径推导）。
        logger.warning("plugin manifest ignored (%s)", safe_exception_text(e))
        return {}


def resolve_plugin_version(entry: Mapping[str, Any], plugin_metadata: Mapping[str, Any], fallback_sha: str | None) -> str | None:
    """version 优先级：entry.version > plugin.json.version > git commit SHA（marketplace-v1 §4.2）/ Resolve plugin version。"""
    for src in (entry, plugin_metadata):
        v = src.get("version")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return fallback_sha


# ── strict mode + 组件路径覆写（marketplace-v1 §4.3/§4.4 + loading-behavior.md §1/§4/§6；#80）─────
# strict=false 下 plugin.json 不得再声明的组件字段全集（loading-behavior §6.2）。本 SDK 仅消费 skills +
# mcp-servers，但冲突检测按规范判**全六字段**（前向兼容 CC 私有组件，防作者「以为生效实际被覆盖」）。
COMPONENT_FIELDS: tuple[str, ...] = ("commands", "agents", "hooks", "skills", "mcpServers", "lspServers")


def entry_is_strict(entry: Mapping[str, Any]) -> bool:
    """marketplace 条目 ``strict`` 语义（§4.4，默认 ``True``）/ Whether the entry is in strict mode。

    缺省 / 非 bool → ``True``（loading-behavior §4：strict=true 是更宽松、容错的默认模式）。
    """
    val = entry.get("strict")
    return val if isinstance(val, bool) else True


def _json_truthy(value: object) -> bool:
    """JSON 真值判据（对齐 rust ``json_truthy``）：``null``/``false``/``0``/``""``/``[]``/``{}`` 为假，其余为真。

    **刻意私有**（#271）：本模块只对外暴露按字段名判定的 :func:`entry_declares_component`，不导出通用真值
    函数——真值口径是「声明态判据」的内部细节，散出去的通用工具会被误用到非 JSON 来源的值上。
    """
    if value is None or value is False:
        return False
    if isinstance(value, bool):  # True 在此处恒真（False 已返回）
        return True
    if isinstance(value, (int, float)):
        return value != 0
    # 只覆盖 **JSON 的五种容器/标量**；``tuple`` 刻意不在列（JSON 无 tuple）——空 tuple 走末尾 fallback
    # 取真，与 :func:`entry_declares_component` 的「非 JSON 类型一律 fail-closed」承诺一致（审查 🟡1）。
    if isinstance(value, (str, list, dict)):
        return len(value) > 0
    return True


def entry_declares_component(entry: Mapping[str, Any], key: str) -> bool:
    """声明态判据的**唯一权威**：``entry[key]`` 按原始 JSON 真值是否算「已声明」（#271，镜像 rust
    ``manifest.rs`` 的 ``entry_declares_component``）/ Single authority for "is this component declared?".

    供 :func:`manifest_declared_components`（plugin.json 组件全集）与 installer 的 SKILL 声明判据共用，
    避免两处各写一份真值口径——历史上它们一个用 Python 真值、一个用 JSON 真值，靠注释维系等价。

    按**原始 JSON 真值**判而非「能归一出可用路径列表」：``skills: [123]`` / ``{"x": 1}`` 属「声明了但不可
    投影」（应走失败路径），``skills: ""`` / ``[]`` 才算未声明。

    **非 JSON 类型**（本仓不可达——调用方一律传 ``json.loads`` 结果）一律算「已声明」（fail-closed 取真），
    见 :func:`_json_truthy` 的末尾 fallback。
    """
    return _json_truthy(entry.get(key))


def manifest_declared_components(plugin_manifest: Mapping[str, Any]) -> list[str]:
    """plugin.json 中**真值**组件字段名列表（空 / 缺 / falsy 不算声明）/ Truthy component fields declared in plugin.json.

    真值口径委托 :func:`entry_declares_component`（#271 单点）。对 **JSON 解码值**与旧实现
    ``if plugin_manifest.get(f)`` 逐值等价；仅非 JSON falsy 类型（``set()`` / 自定义对象）由「未声明」
    变为「已声明」——本仓调用方（installer / staging / validator）全部读 ``json.loads`` 结果，不可达。
    """
    return [f for f in COMPONENT_FIELDS if entry_declares_component(plugin_manifest, f)]


def check_strict_conflict(entry: Mapping[str, Any], plugin_manifest: Mapping[str, Any]) -> None:
    """strict=false 且 plugin.json 声明组件 → 抛 conflicting manifests（§4.4 / loading-behavior §6.2）。

    strict=false 语义为「marketplace 条目是组件定义全集」；若 plugin 仓库 ``plugin.json`` 也声明组件，二者权威冲突
    → **拒绝加载（硬错误）**，而非静默合并。strict=true（默认）恒不冲突（plugin.json 权威 + 条目追加合并）。
    """
    if entry_is_strict(entry):
        return
    declared = manifest_declared_components(plugin_manifest)
    if declared:
        raise PluginManifestError(
            f"conflicting manifests: strict=false but plugin.json declares components {declared} "
            "(marketplace-v1 §4.4: the marketplace entry is the authoritative component set)",
        )


def _as_str_path_list(value: Any) -> list[str]:
    """归一 ``string | array<string>`` → list[str]（非 str 项**静默跳过**，与 :func:`iter_plugin_entries` 对畸形
    manifest 子项的 silent-skip 姿态一致）/ Normalize string|array<string>; non-str items silently skipped。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [v for v in value if isinstance(v, str)]
    return []


def skill_override_raw_paths(
    entry: Mapping[str, Any],
    plugin_manifest: Mapping[str, Any],
    *,
    strict: bool,
) -> list[str]:
    """归一后的 ``skills`` 覆写**原始**路径列表（未做存在性 / 越界过滤）/ Normalized raw skills override paths.

    源 = ``entry.skills``（恒取）+ ``plugin_manifest.skills``（仅 strict=true），``string | array<string>``
    归一、非 str 项静默跳过（:func:`_as_str_path_list` 同姿态）。:func:`resolve_skill_override_dirs`
    （装载路径，容错跳过）与 :mod:`~a2c_smcp.computer.skills.validator`（校验路径，逐项报错）共同消费本
    函数——归一规则单一权威 / Consumed by both the tolerant loader and the strict validator.
    """
    raw: list[str] = _as_str_path_list(entry.get("skills"))
    if strict:
        raw.extend(_as_str_path_list(plugin_manifest.get("skills")))
    return raw


def resolve_skill_override_dirs(
    entry: Mapping[str, Any],
    plugin_manifest: Mapping[str, Any],
    plugin_root: Path,
    *,
    strict: bool,
) -> list[Path]:
    """解析 ``skills`` 组件路径覆写为**容器目录**列表（§4.3，追加进约定 ``skills/`` 之外）/ Resolve skills override container dirs。

    源 = ``entry.skills``（恒取）+ ``plugin_manifest.skills``（仅 strict=true；strict=false 时 plugin 已无组件，
    取空集亦无害——冲突在 :func:`check_strict_conflict` 已拦）。每项相对 ``plugin_root`` 解析后 ``is_within`` 防穿越
    （越界记 ERROR 跳过，**不抛**），非目录记 WARNING 跳过；override 间按 resolve 后路径去重保序（与约定 ``skills/``
    的去重交扫描方）。返回的是**容器**目录（其下 ``<name>/SKILL.md`` 为单个 SKILL，与约定 ``skills/`` 同构）。
    """
    raw = skill_override_raw_paths(entry, plugin_manifest, strict=strict)

    plugin_root_resolved = plugin_root.resolve()
    out: list[Path] = []
    seen_paths: set[Path] = set()
    for rel in raw:
        cand = (plugin_root / rel).resolve()
        if not is_within(cand, plugin_root_resolved):
            logger.error(
                "skills override path %r escapes plugin root %s, skipped",
                redact_git_urls_in_text(rel),
                sanitize_path_for_diagnostics(str(plugin_root)),
            )
            continue
        if cand in seen_paths:
            continue
        if not cand.is_dir():
            logger.warning(
                "skills override path %r is not a directory at %s, skipped",
                redact_git_urls_in_text(rel),
                sanitize_path_for_diagnostics(str(cand)),
            )
            continue
        seen_paths.add(cand)
        out.append(cand)
    return out


def plugin_skill_container_dirs(
    entry: Mapping[str, Any],
    plugin_manifest: Mapping[str, Any],
    plugin_root: Path,
    *,
    strict: bool,
) -> list[Path]:
    """SKILL 发现容器集（单一权威）/ SKILL discovery container dirs — single authority.

    约定 ``<plugin 根>/skills`` 恒在首位，其后追加 :func:`resolve_skill_override_dirs` 解析出的声明覆写容器
    （§4.3）。与 rust ``plugin_skill_container_dirs`` 同构，供 staging（装载扫描）与 installer（enable 预检
    计数）共同消费，避免两套口径漂移（#271）。

    **不去重、不校验约定容器存在性**：去重由消费方按 resolve 后路径做（约定径与覆写径可能同径），存在性由
    各自扫描 / 计数路径决定（:mod:`~a2c_smcp.computer.skills.validator` 另有逐项诊断，不走本函数）。
    """
    return [
        plugin_root / SKILLS_SUBDIR,
        *resolve_skill_override_dirs(entry, plugin_manifest, plugin_root, strict=strict),
    ]


# ── mcp-servers/<n>.json（plugin 携带 MCP server，文件式）/ bundled MCP server files ──────────
def enumerate_bundled_server_files(plugin_root: Path) -> list[Path]:
    """
    列 ``<plugin>/mcp-servers/*.json``（根下**一级**，排除 ``inputs.json``，``sorted`` 确定序）/ List bundled server files。

    无 ``mcp-servers/`` 目录 → ``[]``（plugin 不携带 MCP server，合法）。``inputs.json`` 是 plugin inputs 池
    定义、非 server 配置，枚举时排除（入池归 #65）。
    """
    d = plugin_root / MCP_SERVERS_SUBDIR
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix == ".json" and p.name != MCP_INPUTS_FILENAME)


def parse_bundled_server(path: Path) -> MCPServerConfig:
    """
    解析单个 ``mcp-servers/<name>.json`` → 校验后的 :class:`MCPServerConfig` / Parse one bundled server file。

    强制**文件名（去 ``.json``）== 配置内 ``name``**（mcp-servers 协议 §1：文件名即 server 身份）；不一致 / 校验
    失败 → :class:`PluginManifestError`（致命，install 原子前置条件）。
    """
    data = _read_json_object(path, what="bundled MCP server")
    try:
        cfg = _MCP_SERVER_ADAPTER.validate_python(data)
    except ValidationError as e:
        # #263 复审：pydantic 默认 str 会带 `input_value=…`（env / headers 里可能是 token）→ 只报字段与条数。
        details = e.errors(include_input=False, include_url=False)
        fields = ", ".join(
            f"{escape_unsafe_chars('.'.join(str(part) for part in err.get('loc', ())) or '<root>')}:{err.get('type', 'invalid')}"
            for err in details
        )
        raise PluginManifestError(
            f"invalid MCP server config at {sanitize_path_for_diagnostics(str(path))}: "
            f"{len(details)} validation error(s) in [{fields}]"
        ) from e
    if cfg.name != path.stem:
        raise PluginManifestError(
            f"bundled MCP server filename stem {redact_git_urls_in_text(path.stem)!r} != "
            f"config name {redact_git_urls_in_text(str(cfg.name))!r} "
            f"at {sanitize_path_for_diagnostics(str(path))} "
            "(mcp-servers protocol §1: filename = server identity)",
        )
    return cfg


def load_bundled_servers(plugin_root: Path) -> list[MCPServerConfig]:
    """
    枚举并解析一个 plugin 的全部 bundled MCP server 配置 / Enumerate + parse all bundled MCP server configs。

    **任一文件解析/校验失败即抛** :class:`PluginManifestError`——这是 plugin install「冲突预检前先全量解析、
    畸形则原子失败」的承重前置（§10.6：不留半装）。无 server → ``[]``。
    """
    return [parse_bundled_server(p) for p in enumerate_bundled_server_files(plugin_root)]
