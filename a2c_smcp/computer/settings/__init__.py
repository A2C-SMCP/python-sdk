# -*- coding: utf-8 -*-
# filename: __init__.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 意图 / 治理层 settings 子系统 / Computer intent & governance settings subsystem（v0.2.1）

承载 settings.json 的结构、五级 scope 合并、policy 子源选取与物化层文件 store——CLI
marketplace/plugin/skill 管理 UX 的"意图层 + 物化层"。reconciler / MCP 定义层为后续工单（#62 / #64）。
The intent + materialized layers of the CLI marketplace/plugin/skill management UX. Reconciler /
MCP-definition layer land in follow-up tickets.

已落地模块 / Landed modules：
- :mod:`a2c_smcp.computer.settings.schema` —— settings.json TypedDict + 字段级容错校验
  （passthrough / 无 version / policy-only 越权过滤）（S4，#56）
- :mod:`a2c_smcp.computer.settings.scope`  —— 五级 scope 路径解析 + 读/写两套合并 customizer +
  active-workdir 单根 / 能力层全局并集解析（S4，#56）
- :mod:`a2c_smcp.computer.settings.policy` —— policy 四子源 first-source-wins（remote stub + OS-MDM +
  managed-settings[+.d] + HKCU）（S4，#56）
- :mod:`a2c_smcp.computer.settings.store`  —— 物化文件（known_marketplaces / installed_plugins）原子写 +
  文件锁 + ``.corrupt-<ts>.bak`` 损坏恢复 + 写保护头（带 version）（S5，#58）

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §5。
"""

from __future__ import annotations

from a2c_smcp.computer.settings.policy import (
    PolicySource,
    default_policy_sources,
    load_macos_plist,
    load_managed_settings,
    load_windows_registry,
    resolve_policy_settings,
)
from a2c_smcp.computer.settings.schema import (
    BOOL_FIELDS,
    CAPABILITY_FIELDS,
    POLICY_ONLY_FIELDS,
    STRING_ARRAY_FIELDS,
    ComputerSettings,
    SettingsScope,
    SettingsValidationError,
    is_valid_enabled_plugin_key,
    is_valid_git_url,
    is_valid_marketplace_name,
    validate_settings,
)
from a2c_smcp.computer.settings.scope import (
    DELETE,
    ResolvedSettings,
    apply_write,
    filter_capability_fields,
    load_settings_file,
    merge_layers,
    merge_read,
    resolve_settings,
    resolve_user_config_dir,
    user_settings_path,
    workdir_local_settings_path,
    workdir_project_settings_path,
    workdir_settings_dir,
)
from a2c_smcp.computer.settings.store import (
    INSTALLED_PLUGINS_FILENAME,
    KNOWN_MARKETPLACES_FILENAME,
    MATERIALIZED_VERSION,
    WRITE_PROTECTION_HEADER,
    InstalledPluginRecord,
    InstalledPluginsFile,
    KnownMarketplacesFile,
    MarketplaceRecord,
    SettingsLockError,
    atomic_write_json,
    atomic_write_text,
    empty_installed_plugins,
    empty_known_marketplaces,
    file_lock,
    installed_plugins_path,
    known_marketplaces_path,
    load_installed_plugins,
    load_known_marketplaces,
    read_jsonc_with_recovery,
    save_installed_plugins,
    save_known_marketplaces,
    update_installed_plugins,
    update_known_marketplaces,
)

__all__ = [
    # schema
    "BOOL_FIELDS",
    "CAPABILITY_FIELDS",
    "POLICY_ONLY_FIELDS",
    "STRING_ARRAY_FIELDS",
    "ComputerSettings",
    "SettingsScope",
    "SettingsValidationError",
    "is_valid_enabled_plugin_key",
    "is_valid_git_url",
    "is_valid_marketplace_name",
    "validate_settings",
    # scope
    "DELETE",
    "ResolvedSettings",
    "apply_write",
    "filter_capability_fields",
    "load_settings_file",
    "merge_layers",
    "merge_read",
    "resolve_settings",
    "resolve_user_config_dir",
    "user_settings_path",
    "workdir_local_settings_path",
    "workdir_project_settings_path",
    "workdir_settings_dir",
    # policy
    "PolicySource",
    "default_policy_sources",
    "load_macos_plist",
    "load_managed_settings",
    "load_windows_registry",
    "resolve_policy_settings",
    # store
    "INSTALLED_PLUGINS_FILENAME",
    "KNOWN_MARKETPLACES_FILENAME",
    "MATERIALIZED_VERSION",
    "WRITE_PROTECTION_HEADER",
    "InstalledPluginRecord",
    "InstalledPluginsFile",
    "KnownMarketplacesFile",
    "MarketplaceRecord",
    "SettingsLockError",
    "atomic_write_json",
    "atomic_write_text",
    "empty_installed_plugins",
    "empty_known_marketplaces",
    "file_lock",
    "installed_plugins_path",
    "known_marketplaces_path",
    "load_installed_plugins",
    "load_known_marketplaces",
    "read_jsonc_with_recovery",
    "save_installed_plugins",
    "save_known_marketplaces",
    "update_installed_plugins",
    "update_known_marketplaces",
]
