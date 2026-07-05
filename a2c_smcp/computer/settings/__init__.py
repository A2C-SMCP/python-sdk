# -*- coding: utf-8 -*-
# filename: __init__.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 意图 / 治理层 settings 子系统 / Computer intent & governance settings subsystem（v0.2.1）

承载 settings.json 的结构、五级 scope 合并、policy 子源选取、物化层文件 store、启动对账 reconciler 与
``.tfrobot/mcp.json`` MCP 定义层/批准门控——CLI marketplace/plugin/skill 管理 UX 的"意图层 + 物化层 + 对账层
+ MCP 定义/门控层"。
The intent + materialized + reconcile + MCP-definition/gating layers of the CLI marketplace/plugin/skill
management UX.

已落地模块 / Landed modules：
- :mod:`a2c_smcp.computer.settings.schema` —— settings.json TypedDict + 字段级容错校验
  （passthrough / 无 version / policy-only 越权过滤）（S4，#56）
- :mod:`a2c_smcp.computer.settings.scope`  —— 五级 scope 路径解析 + 读/写两套合并 customizer +
  active-workdir 单根 / 能力层全局并集解析（S4，#56）
- :mod:`a2c_smcp.computer.settings.policy` —— policy 四子源 first-source-wins（remote stub + OS-MDM +
  managed-settings[+.d] + HKCU）（S4，#56）
- :mod:`a2c_smcp.computer.settings.store`  —— 物化文件（known_marketplaces / installed_plugins）原子写 +
  文件锁 + ``.corrupt-<ts>.bak`` 损坏恢复 + 写保护头（带 version）（S5，#58）
- :mod:`a2c_smcp.computer.settings.reconciler` —— 启动对账（additive-only 四分支）+ 孤儿清理
  （marketplace prune / plugin gc）（S9，#62）

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §5 / §7。
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

# 注意 / NB：:mod:`...settings.reconciler` 与 :mod:`...settings.installer` **刻意不在此 re-export**——它们依赖
# :mod:`...skills.staging`，而 staging 顶层又 import :mod:`...settings.schema`（触发本 __init__）。
# 若在此急切导入 reconciler/installer 会与 staging 的半初始化态构成循环（`_EXTERNAL_PLUGINS_NS` 未定义）。
# 同理 :mod:`...settings.mcp_config` 亦**不 re-export**——它 import :mod:`...mcp_clients.model`（→ vrl 传递依赖），
# 在此急切导入会无谓加重 ``import ...settings``（与 installer/manifest 同姿态，不成环但不必拉重）。
# 消费方请直接 ``from a2c_smcp.computer.settings.reconciler import reconcile`` /
# ``...installer import install_plugin`` / ``...mcp_config import resolve_mcp_config`` 等。
# reconciler / installer / mcp_config are intentionally NOT re-exported here; import them directly from submodules.

__all__ = [
    # schema
    "BOOL_FIELDS",
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
