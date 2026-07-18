# -*- coding: utf-8 -*-
# filename: test_computer_inventory.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 级 MCP server 归属 + 活跃 inventory 查询测试（#121，协议 §4.8；#123 起 enabled 门控 = installed ∧ true；对齐 rust-sdk #97）。

测试意图 / Test intentions（hermetic：预置双账本 + catalog 树，零 git 零网络；fixture 与
``test_computer_governance_recovery.py`` 同构）:
- AC1 装+启用 plugin、同一 skill_home 重建 Computer、boot(skills-only) 后：inventory 同时返回
  user server（managedBy=user 全权）与 plugin bundled server（managedBy=plugin 只读 + 正确
  marketplace/plugin/pluginId），后者虽未物化仍出现（§4.8「进程未拉起也可观测」）；
- AC2 disable / 卸载（账本移除）后：bundled server 不再出现在 inventory；
- AC3 本地-only pid（无 ``@``）不采集（与 ``collect_enabled_bundled_servers`` 门控一致）；
- 合并语义（#144 起 **bundle_id 为键**）：同 bundle_id 时运行期条目优先（disabled 取运行期配置）；归属 F1 纯推导
  （``∃ origin != plugin 的声明 ⇒ user，否则 plugin``）；异 bundle_id 同名 server 合法共存不塌陷；结果按
  bundle_id 排序稳定输出。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inventory import McpPluginOwnership, McpUserOwnership
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig
from a2c_smcp.computer.settings.store import save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.utils.bundle_id import resolve_bundle_id

_SRC = {"type": "git", "url": "https://example.com/acme.git"}


# ── fixture 辅助（与 test_computer_governance_recovery.py 同构）/ helpers ─────
def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _seed_home(
    tmp_path: Path,
    *,
    servers: Sequence[str] = (),
    disabled_servers: Sequence[str] = (),
    pid: str = "audit@acme",
) -> tuple[Path, Path]:
    """预置 skill_home：catalog 树（acme/audit）+ known_marketplaces + installed 账本。返回 (home, plugin_root)。"""
    home = tmp_path / "skill-home"
    home.mkdir()
    catalog = marketplace_skill_dir(home, "acme")
    _write_json(
        catalog / ".tfrobot-plugin" / "marketplace.json",
        {
            "name": "acme",
            "owner": {"name": "X"},
            "metadata": {"pluginRoot": "./plugins"},
            "plugins": [{"name": "audit", "source": "audit", "version": "1.2.0"}],
        },
    )
    plugin_root = catalog / "plugins" / "audit"
    plugin_root.mkdir(parents=True, exist_ok=True)
    all_servers = [*servers, *disabled_servers]
    for sname in all_servers:
        server_def = {"name": sname, "type": "stdio", "server_parameters": {"command": "node"}, "disabled": sname in disabled_servers}
        _write_json(plugin_root / "mcp-servers" / f"{sname}.json", server_def)
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc123"}}},
        home=home,
    )
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                pid: [
                    {
                        "scope": "user",
                        "installPath": str(plugin_root),
                        "version": "1.2.0",
                        "commitSha": "abc123",
                        "installedAt": "2026-07-06T00:00:00Z",
                        "mcpServers": all_servers,
                    },
                ],
            },
        },
        home=home,
    )
    return home, plugin_root


def _isolate_declared_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 declared 视图来源：XDG → tmp、cwd → tmp（防读真实 user/project settings）。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)


def _user_stdio_server(name: str) -> StdioServerConfig:
    """构造一条禁用的用户 stdio server（配置态即可，disabled 免 boot 拉起进程）/ a disabled user server。"""
    return StdioServerConfig(name=name, disabled=True, server_parameters=StdioServerParameters(command="node"))


# ── AC1：boot 后 user + plugin 归属并存，未物化 bundled 仍可观测 ───────────────
@pytest.mark.asyncio
async def test_inventory_boot_reports_user_and_plugin_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """boot(skills-only) 后：user server 全权；plugin bundled server 未物化仍出现且只读（§4.8 可观测）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])

    # auto_connect=False：与 rust 测试同构（Computer::new(.., false, false)），boot 不主动拉起 disabled server。
    async with Computer(name="t", mcp_servers={_user_stdio_server("user-fs")}, auto_connect=False, skill_home=home) as comp:
        inv = comp.list_mcp_servers_with_metadata()

        # 用户 server：managedBy=user，可从 MCP tab 全权管理（入口 mcp）。
        user = next(e for e in inv if e.name == "user-fs")
        assert user.managed_by == McpUserOwnership()
        assert user.disabled, "禁用旗应透传"
        assert user.lifecycle.can_edit_from_mcp_tab
        assert user.lifecycle.can_start_from_mcp_tab
        assert user.lifecycle.manage_from == "mcp"

        # plugin bundled server：boot(hooks=None) 未物化，仍经 ledger 派生出现，带完整归属 + 只读生命周期。
        plugin = next(e for e in inv if e.name == "audit-mcp")
        assert plugin.managed_by == McpPluginOwnership(marketplace="acme", plugin="audit", plugin_id="audit@acme")
        assert not plugin.disabled
        assert not plugin.lifecycle.can_edit_from_mcp_tab
        assert not plugin.lifecycle.can_start_from_mcp_tab
        assert plugin.lifecycle.manage_from == "marketplace"

        # 结果按 bundle_id 排序（bundle_id 唯一全序，name 可碰撞非全序）——#144。
        assert [e.bundle_id for e in inv] == sorted(e.bundle_id for e in inv)


# ── AC2：disable / 卸载后 bundled server 不再出现 ──────────────────────────────
@pytest.mark.asyncio
async def test_inventory_excludes_disabled_plugin_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """user scope ``enabledPlugins=false`` → bundled server 不出现在 inventory（enabled 门控）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])
    _write_json(tmp_path / "cfg" / "a2c" / "settings.json", {"enabledPlugins": {"audit@acme": False}})

    async with Computer(name="t", skill_home=home) as comp:
        assert not any(e.name == "audit-mcp" for e in comp.list_mcp_servers_with_metadata())


@pytest.mark.asyncio
async def test_inventory_excludes_uninstalled_plugin_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """卸载（账本移除该 plugin 记录）后以同一 home 重建 Computer → bundled server 不再出现。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])
    # 未 boot（无迁移）→ 显式 seed v0.3.0 双意图（installed ∧ enabled）。
    _write_json(tmp_path / "cfg" / "a2c" / "settings.json", {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}})

    # 卸载前：inventory 含 plugin bundled server（未 boot 亦可查询——纯函数投影）。
    comp_a = Computer(name="a", skill_home=home)
    assert any(e.name == "audit-mcp" for e in comp_a.list_mcp_servers_with_metadata())

    # 卸载的账本落点 = installed_plugins.json 移除记录（v0.3.0 意图条目亦会删；此处仅移账本即须熄灯——
    # 无记录 = 无 config 可投影）。
    save_installed_plugins({"version": 1, "plugins": {}}, home=home)

    comp_b = Computer(name="b", skill_home=home)
    assert not any(e.name == "audit-mcp" for e in comp_b.list_mcp_servers_with_metadata())


# ── AC3：本地-only pid（无 ``@``）不采集 ──────────────────────────────────────
@pytest.mark.asyncio
async def test_inventory_skips_local_only_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pid 无 ``@marketplace`` 段（本地-only 形态）→ 其 bundled server 不采集（与 recovery 门控一致）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"], pid="local-audit")

    comp = Computer(name="t", skill_home=home)
    assert not any(e.name == "audit-mcp" for e in comp.list_mcp_servers_with_metadata())


# ── 合并语义：同 bundle_id 去重、运行期条目优先、F1 用户主权 ──────────────────────
@pytest.mark.asyncio
async def test_inventory_user_declaration_wins_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """user server 与 plugin bundled **同 bundle_id**（同 display 名、缺省派生）→ 一条，managedBy=user（F1 用户主权）。

    #144 前此处按 name 命中 bundled 集误标 plugin（只读，用户改不了自己的 server）。F1 纯推导：
    ``∃ origin != plugin 的声明 ⇒ user`` —— 用户 embed 声明胜出，即便与某 plugin 依赖同 bundle_id（§2.5 用户主权）。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])

    # 运行期物化一条同名 server（缺省派生 ⇒ bundle_id == "audit-mcp" == bundled 侧；disabled=True，bundled 侧 disabled=False）。
    async with Computer(name="t", mcp_servers={_user_stdio_server("audit-mcp")}, auto_connect=False, skill_home=home) as comp:
        inv = [e for e in comp.list_mcp_servers_with_metadata() if e.bundle_id == "audit-mcp"]
        assert len(inv) == 1, "同 bundle_id 去重：运行期条目优先，不重复补入"
        assert inv[0].disabled, "disabled 应取运行期配置（运行期条目优先）"
        assert inv[0].managed_by == McpUserOwnership(), "F1：用户 embed 声明胜出 ⇒ user 主权（可编辑），非 plugin 只读"
        assert inv[0].lifecycle.can_edit_from_mcp_tab


# ── #144：异 bundle_id 同名 server 合法共存不塌陷 + 各自正确归属 ─────────────────
@pytest.mark.asyncio
async def test_inventory_coexisting_same_name_distinct_bundle_ids_not_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 display 名、**异 bundle_id** 的 user server 与 plugin bundled server 合法共存（§5.6）→ 两条独立条目。

    这正是 #144 根治的双缺陷：旧 name-join 会把二者去重塌成一条（缺陷 #2），并把用户自己的 server 误标
    plugin（只读，缺陷 #3）。迁 bundle_id 后二者身份分明、各自正确归属。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])  # plugin bundled：name=audit-mcp，缺省 bundle_id=audit-mcp

    # 用户 embed 一条同 display 名但**显式异 bundle_id** 的 server（disabled 免 boot 拉起进程）。
    user_cfg = StdioServerConfig(
        name="audit-mcp", bundle_id="user-audit", disabled=True, server_parameters=StdioServerParameters(command="node")
    )
    async with Computer(name="t", mcp_servers={user_cfg}, auto_connect=False, skill_home=home) as comp:
        inv = [e for e in comp.list_mcp_servers_with_metadata() if e.name == "audit-mcp"]
        by_bid = {e.bundle_id: e for e in inv}

        # 两条独立身份都在（不塌陷）。
        assert set(by_bid) == {"user-audit", "audit-mcp"}, "异 bundle_id 同名 server 不得塌成一条"

        # 用户 server（bundle_id=user-audit）：F1 → user，可编辑（不被误标 plugin）。
        assert by_bid["user-audit"].managed_by == McpUserOwnership()
        assert by_bid["user-audit"].lifecycle.can_edit_from_mcp_tab

        # plugin bundled server（bundle_id=audit-mcp）：仅 plugin 声明 → plugin，只读。
        assert by_bid["audit-mcp"].managed_by == McpPluginOwnership(marketplace="acme", plugin="audit", plugin_id="audit@acme")
        assert not by_bid["audit-mcp"].lifecycle.can_edit_from_mcp_tab


# ── #144：每条条目暴露 bundle_id（客户端关联回 get_config / {bundle_id}__tool），wire camelCase ──
@pytest.mark.asyncio
async def test_inventory_entry_exposes_bundle_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每条 inventory 条目暴露 ``bundle_id``（缺陷 #1：客户端据此关联回 A2C server），wire 出线 camelCase ``bundleId``。

    用户 server 用**显式异值 bundle_id**（``name="user-fs"`` ≠ ``bundle_id="user-filesystem"``）作反致盲锚：
    若代码错写成 ``bundle_id=cfg.name``，本断言会失败（缺省派生下 name≡bundle_id 会同值致盲）。
    """
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])
    user_cfg = StdioServerConfig(
        name="user-fs", bundle_id="user-filesystem", disabled=True, server_parameters=StdioServerParameters(command="node")
    )

    async with Computer(name="t", mcp_servers={user_cfg}, auto_connect=False, skill_home=home) as comp:
        inv = comp.list_mcp_servers_with_metadata()

        user = next(e for e in inv if e.name == "user-fs")
        assert user.bundle_id == "user-filesystem", "身份取 resolve_bundle_id（显式值），非 display name"
        assert user.bundle_id == resolve_bundle_id(user_cfg)  # 与统一解析器一致
        plugin = next(e for e in inv if e.name == "audit-mcp")
        assert plugin.bundle_id == "audit-mcp"

        # wire 出线 camelCase（对齐 rust serde / #96 示例键名），且 name 与 bundleId 各自独立出线。
        v = json.loads(user.model_dump_json())
        assert v["bundleId"] == "user-filesystem"
        assert v["name"] == "user-fs"


@pytest.mark.asyncio
async def test_inventory_includes_dynamically_added_user_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """boot 后经 ``aadd_or_aupdate_server`` 动态挂载的 user server 必须出现（CLI 主路径；防构造期快照漏报）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])

    async with Computer(name="t", auto_connect=False, skill_home=home) as comp:
        await comp.aadd_or_aupdate_server(_user_stdio_server("dyn-user"))
        inv = comp.list_mcp_servers_with_metadata()

        dyn = next(e for e in inv if e.name == "dyn-user")
        assert dyn.managed_by == McpUserOwnership()
        assert dyn.disabled, "禁用旗应透传（取运行期活跃配置）"
        # 动态挂载不影响 bundled 观测面。
        assert any(e.name == "audit-mcp" for e in inv)


@pytest.mark.asyncio
async def test_inventory_marks_remounted_bundled_server_as_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """boot 后经 ``reconcile_governance(hooks)`` 重挂的 bundled server：源一（运行期活跃集）命中仍标 plugin 且仅一条。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])

    async with Computer(name="t", auto_connect=False, skill_home=home) as comp:

        async def register(cfg, record) -> None:
            # #137 ③：治理重挂 = 投影，经 transient amount_server（治理投影不落声明面；durable 面属用户 CRUD）。
            await comp.amount_server(cfg, plugin=record.plugin, marketplace=record.marketplace)

        report = await comp.reconcile_governance(
            # #153：身份 = bundle_id + 数据源 = 运行期权威集（`comp.mcp_servers` 是构造期快照，CLI 下恒空，
            # 协议 §2.5-4 明禁）——与生产 build_mcp_callbacks 同构。
            existing_bundle_ids=lambda: (
                {resolve_bundle_id(c) for c in comp.mcp_manager.server_configs()} if comp.mcp_manager else set()
            ),
            register_server=register,
            declared={"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}},
        )
        assert report.remounted_servers == ["audit-mcp"]

        entries = [e for e in comp.list_mcp_servers_with_metadata() if e.name == "audit-mcp"]
        assert len(entries) == 1, "重挂后运行期条目与 ledger 条目按 bundle_id 去重为一条"
        assert entries[0].managed_by == McpPluginOwnership(marketplace="acme", plugin="audit", plugin_id="audit@acme")


@pytest.mark.asyncio
async def test_inventory_passes_through_bundled_disabled_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bundled server 定义自带 ``disabled=true`` → 源二（未物化补入）disabled 旗透传。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, disabled_servers=["audit-mcp"])
    # 未 boot（无迁移）→ 显式 seed v0.3.0 双意图。
    _write_json(tmp_path / "cfg" / "a2c" / "settings.json", {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}})

    comp = Computer(name="t", skill_home=home)
    entry = next(e for e in comp.list_mcp_servers_with_metadata() if e.name == "audit-mcp")
    assert entry.disabled, "bundled 配置的 disabled 旗应透传"
    assert isinstance(entry.managed_by, McpPluginOwnership)
