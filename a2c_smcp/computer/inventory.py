# -*- coding: utf-8 -*-
# filename: inventory.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 级 MCP server 归属 + 活跃 inventory 查询的 SDK-facing 元数据类型（#121，对齐 rust-sdk #97）。
SDK-facing ownership + active-inventory metadata for MCP servers (#121, mirrors rust-sdk #97).

这些类型是 **SDK-facing、不进 Agent-facing ``client:*`` wire**。

协议依据 / Protocol: a2c-smcp-protocol **v0.2.3** ``computer-management/runtime-contract.md`` §4.8
（#93 client-owns-MCP-config 边界）。§4.8 要求：重建后的能力归属元数据 MUST 为 boot 的**纯函数**输出
（意图 + resolved location + manifest 重推导，每次 boot 可复现，**不**依赖任何调用方持有的内存 ownership
map）；且 enabled bundled server **即使进程未拉起**也 MUST **可查询**。本模块的类型正是该「可查询归属」的
载体，供 client（如 ``tfrobot-client``）的 Skill / MCP tab 直接消费——判定某 server 是否可从普通 MCP tab
编辑 / 启停，无需读 SDK ledger、无需解析 plugin manifest、无需持内存 ownership map。

**刻意不进协议 wire**：归属 / 生命周期字段仅在 SDK 表面（:meth:`Computer.list_mcp_servers_with_metadata`），
**不**加入 ``client:*`` 事件数据结构——Agent 侧协议表面与能力归属无关（Agent-User 能力等价，不给协议加角色
门控字段）。序列化 camelCase 对齐 rust #96 JSON 示例（``managedBy`` / ``pluginId`` / ``canEditFromMcpTab`` …），
与 rust serde ``rename_all = "camelCase"`` 逐字一致（``serialize_by_alias`` 令 ``model_dump*`` 默认即 camelCase）。
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _InventoryModel(BaseModel):
    """camelCase 序列化基类：wire 形态 camelCase（对齐 rust serde），Python 侧仍 snake_case 构造/访问。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        frozen=True,
    )


class McpLifecycle(_InventoryModel):
    """面向 UI 入口权限的生命周期能力 / lifecycle capabilities for UI entry gating。

    由归属纯函数派生（:meth:`McpUserOwnership.lifecycle` / :meth:`McpPluginOwnership.lifecycle`），client 据此
    决定 MCP tab 能否编辑 / 启停：user server 可从 MCP tab 全权管理；plugin bundled server 只读展示、引导到
    Marketplace 管理。
    """

    # 管理入口常量 / manage-from constants（与 rust ``McpLifecycle`` 关联常量同名同义）。
    MANAGE_FROM_MCP: ClassVar[str] = "mcp"
    MANAGE_FROM_MARKETPLACE: ClassVar[str] = "marketplace"

    can_edit_from_mcp_tab: bool = Field(description="是否可在普通 MCP tab 编辑 / 删除 / can edit from the MCP tab")
    can_start_from_mcp_tab: bool = Field(description="是否可在普通 MCP tab 启停 / can start-stop from the MCP tab")
    manage_from: str = Field(description='管理入口："mcp"（用户）/ "marketplace"（插件）/ where this server is managed from')


class McpUserOwnership(_InventoryModel):
    """用户配置 / client 传入的 MCP server（配置态权威在 client 用户配置）/ user-owned。"""

    type: Literal["user"] = "user"

    def lifecycle(self) -> McpLifecycle:
        """user → 全权（可编辑 / 可启停，入口 ``mcp``）/ full control from the MCP tab。"""
        return McpLifecycle(
            can_edit_from_mcp_tab=True,
            can_start_from_mcp_tab=True,
            manage_from=McpLifecycle.MANAGE_FROM_MCP,
        )


class McpPluginOwnership(_InventoryModel):
    """已启用 marketplace plugin 派生的 bundled MCP server / plugin-owned bundled server。

    ``plugin_id`` = ``<plugin>@<marketplace>``（与 ``installed_plugins.json`` 的 map 键、
    :class:`~a2c_smcp.computer.settings.recovery.BundledServerRecord` 严格同构）。归属为 ledger + manifest 的
    纯函数推导（§4.8.3），不含运行期状态。
    """

    type: Literal["plugin"] = "plugin"
    marketplace: str = Field(description="marketplace 名 / marketplace name")
    plugin: str = Field(description="plugin 名 / plugin name")
    plugin_id: str = Field(description="plugin id：<plugin>@<marketplace> / plugin id")

    def lifecycle(self) -> McpLifecycle:
        """plugin → 只读（禁编辑 / 禁启停，入口 ``marketplace``）/ read-only, managed from Marketplace。"""
        return McpLifecycle(
            can_edit_from_mcp_tab=False,
            can_start_from_mcp_tab=False,
            manage_from=McpLifecycle.MANAGE_FROM_MARKETPLACE,
        )


# 判别联合，对齐 rust ``#[serde(tag = "type")]`` / #96 示例 ``managedBy``：
# user → {"type":"user"}；plugin → {"type":"plugin","marketplace":…,"plugin":…,"pluginId":…}。
McpOwnership: TypeAlias = Annotated[McpUserOwnership | McpPluginOwnership, Field(discriminator="type")]


class McpServerWithMetadata(_InventoryModel):
    """一条 MCP server 的**活跃 inventory** 条目 + 归属 / 生命周期元数据 / one active-inventory entry。

    :meth:`Computer.list_mcp_servers_with_metadata` 的返回元素。合并两个来源（client 无需自己拼）：

    - 运行期已物化的 server（``Computer.mcp_servers``）——用户配置 or client 经 hooks 物化的 plugin bundled；
    - ledger 派生的**已启用但尚未物化**的 plugin bundled server（§4.8：进程未拉起也须可观测）。

    ``disabled`` 取自 server 配置本身（:class:`~a2c_smcp.computer.mcp_clients.model.BaseMCPServerConfig`
    ``disabled`` 旗）；``managed_by`` 决定 ``lifecycle``。**不**含运行期「进程是否已启动」状态——那由
    ``MCPServerManager.get_server_status`` 单独提供，本 inventory 只承载「有哪些 + 归谁 + 能否从 MCP tab 管」
    这一稳定归属视图（对齐 #96 示例四字段）。
    """

    name: str = Field(description="server 名（inventory 主键）/ server name")
    disabled: bool = Field(description="是否禁用（配置态）/ disabled flag from config")
    managed_by: McpOwnership = Field(description="归属：用户 vs 插件 / ownership")
    lifecycle: McpLifecycle = Field(description="由归属派生的生命周期能力 / lifecycle capabilities derived from ownership")

    @classmethod
    def assemble(cls, name: str, *, disabled: bool, managed_by: McpOwnership) -> McpServerWithMetadata:
        """由 ``name`` + ``disabled`` + 归属组装（``lifecycle`` 从归属派生）/ assemble; lifecycle derived from ownership。"""
        return cls(name=name, disabled=disabled, managed_by=managed_by, lifecycle=managed_by.lifecycle())
