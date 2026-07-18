# -*- coding: utf-8 -*-
# filename: test_inventory.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
MCP server 归属 + 活跃 inventory 元数据类型测试（#121，对齐 rust-sdk #97 ``inventory.rs``）/ Inventory type tests。

测试意图 / Test intentions（与 rust ``inventory.rs`` 单测逐一同构）:
- user 归属：序列化 camelCase（``{"type":"user"}``）+ 全权生命周期（可编辑/可启停，入口 ``mcp``）；
- plugin 归属：序列化 camelCase（``managedBy``/``pluginId`` 对齐协议 §4.8 / rust #96 示例）+ 只读生命周期；
- roundtrip：``McpServerWithMetadata`` 序列化↔反序列化闭环。
"""

from __future__ import annotations

import json

from a2c_smcp.computer.inventory import (
    McpLifecycle,
    McpPluginOwnership,
    McpServerWithMetadata,
    McpUserOwnership,
)


def test_user_ownership_serializes_camelcase_and_grants_full_lifecycle() -> None:
    """user 归属：lifecycle 全权（true/true/"mcp"），JSON 键名 camelCase 且判别值 type=user。"""
    entry = McpServerWithMetadata.assemble("everything", bundle_id="everything", disabled=False, managed_by=McpUserOwnership())
    assert entry.lifecycle.can_edit_from_mcp_tab
    assert entry.lifecycle.can_start_from_mcp_tab
    assert entry.lifecycle.manage_from == McpLifecycle.MANAGE_FROM_MCP

    v = json.loads(entry.model_dump_json())
    assert v["name"] == "everything"
    assert v["bundleId"] == "everything"
    assert v["disabled"] is False
    assert v["managedBy"]["type"] == "user"
    assert v["lifecycle"]["canEditFromMcpTab"] is True
    assert v["lifecycle"]["canStartFromMcpTab"] is True
    assert v["lifecycle"]["manageFrom"] == "mcp"


def test_plugin_ownership_serializes_camelcase_and_is_read_only() -> None:
    """plugin 归属：lifecycle 只读（false/false/"marketplace"），JSON 逐字对齐 #96 示例键名。"""
    entry = McpServerWithMetadata.assemble(
        "audit-mcp",
        bundle_id="audit-mcp",
        disabled=False,
        managed_by=McpPluginOwnership(marketplace="acme", plugin="audit", plugin_id="audit@acme"),
    )
    assert not entry.lifecycle.can_edit_from_mcp_tab
    assert not entry.lifecycle.can_start_from_mcp_tab
    assert entry.lifecycle.manage_from == McpLifecycle.MANAGE_FROM_MARKETPLACE

    # 对齐 #96 JSON 示例键名 / mirror the #96 example.
    v = json.loads(entry.model_dump_json())
    assert v["bundleId"] == "audit-mcp"
    assert v["managedBy"]["type"] == "plugin"
    assert v["managedBy"]["marketplace"] == "acme"
    assert v["managedBy"]["plugin"] == "audit"
    assert v["managedBy"]["pluginId"] == "audit@acme"
    assert v["lifecycle"]["manageFrom"] == "marketplace"


def test_metadata_roundtrips_through_serialization() -> None:
    """camelCase JSON 序列化 → 反序列化 roundtrip 闭环（判别联合正确还原 plugin 分支）。"""
    entry = McpServerWithMetadata.assemble(
        "audit-mcp",
        bundle_id="audit-mcp",
        disabled=True,
        managed_by=McpPluginOwnership(marketplace="acme", plugin="audit", plugin_id="audit@acme"),
    )
    back = McpServerWithMetadata.model_validate_json(entry.model_dump_json())
    assert back == entry
    assert isinstance(back.managed_by, McpPluginOwnership)
    assert back.bundle_id == "audit-mcp"
