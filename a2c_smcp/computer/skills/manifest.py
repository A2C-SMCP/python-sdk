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
- **strict mode 组件路径覆写 / 冲突检测**（§3.4/§3.5）归 #80——本模块按约定路径解析，不做 strict 校验。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.skills.sources import DEFAULT_PLUGIN_ROOT
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# manifest 布局常量（协议 marketplace-v1 §2.1/§3.1 + mcp-servers §1）/ manifest layout constants。
MARKETPLACE_MANIFEST_DIR = ".tfrobot-plugin"  # marketplace.json / plugin.json 所在目录（镜像 CC .claude-plugin）
MARKETPLACE_MANIFEST = "marketplace.json"  # 仓库级 manifest
PLUGIN_MANIFEST = "plugin.json"  # plugin 级 manifest（仅元数据）
MCP_SERVERS_SUBDIR = "mcp-servers"  # plugin 携带 MCP server 的文件式子树（§3.3）
MCP_INPUTS_FILENAME = "inputs.json"  # plugin 范围 inputs 池定义（枚举 server 时排除；入池归 #65）

# 单点构造校验入口：dict → MCPServerConfig union（按 type 分型，pydantic v2）/ Single dict→config adapter.
_MCP_SERVER_ADAPTER: TypeAdapter[MCPServerConfig] = TypeAdapter(MCPServerConfig)


class PluginManifestError(Exception):
    """manifest / mcp-servers 文件解析或校验失败（致命，§3.3：JSON 解析失败不降级）/ Manifest parse/validate failure (fatal)."""


# ── JSON 读取（leaf-local；与 staging 同语义，刻意不复用以保持 leaf）/ leaf-local JSON read ──────────
def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    """读 JSON 文件并要求根为对象 / Read a JSON file requiring an object root（失败 → :class:`PluginManifestError`）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PluginManifestError(f"{what} unreadable/invalid at {path}: {e}") from e
    if not isinstance(data, dict):
        raise PluginManifestError(f"{what} root is not an object: {path}")
    return data


# ── marketplace.json（仓库级）/ marketplace-level manifest ────────────────────
def read_marketplace_manifest(catalog_dir: Path) -> dict[str, Any]:
    """读 ``<catalog>/.tfrobot-plugin/marketplace.json``（缺失/损坏 → 抛）/ Read the marketplace manifest (raise if absent)。"""
    path = catalog_dir / MARKETPLACE_MANIFEST_DIR / MARKETPLACE_MANIFEST
    if not path.is_file():
        raise PluginManifestError(f"marketplace manifest not found: {path}")
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
        logger.warning("plugin manifest ignored (%s)", e)
        return {}


def resolve_plugin_version(entry: Mapping[str, Any], plugin_metadata: Mapping[str, Any], fallback_sha: str | None) -> str | None:
    """version 优先级：entry.version > plugin.json.version > git commit SHA（marketplace-v1 §4.2）/ Resolve plugin version。"""
    for src in (entry, plugin_metadata):
        v = src.get("version")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return fallback_sha


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
        raise PluginManifestError(f"invalid MCP server config at {path}: {e}") from e
    if cfg.name != path.stem:
        raise PluginManifestError(
            f"bundled MCP server filename stem {path.stem!r} != config name {cfg.name!r} at {path} "
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
