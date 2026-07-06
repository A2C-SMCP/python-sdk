# -*- coding: utf-8 -*-
# filename: test_computer_inventory.py
# @Time    : 2026/07/06
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 级 MCP server 归属 + 活跃 inventory 查询测试（#121，协议 v0.2.3 §4.8；对齐 rust-sdk #97）。

测试意图 / Test intentions（hermetic：预置双账本 + catalog 树，零 git 零网络；fixture 与
``test_computer_governance_recovery.py`` 同构）:
- AC1 装+启用 plugin、同一 skill_home 重建 Computer、boot(skills-only) 后：inventory 同时返回
  user server（managedBy=user 全权）与 plugin bundled server（managedBy=plugin 只读 + 正确
  marketplace/plugin/pluginId），后者虽未物化仍出现（§4.8「进程未拉起也可观测」）；
- AC2 disable / 卸载（账本移除）后：bundled server 不再出现在 inventory；
- AC3 本地-only pid（无 ``@``）不采集（与 ``collect_enabled_bundled_servers`` 门控一致）；
- 合并语义：同名时运行期条目优先（disabled 取运行期配置）且按 name 命中 bundled 集标 plugin；
  结果按 name 排序稳定输出。
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

_SRC = {"type": "git", "url": "https://example.com/acme.git"}


# ── fixture 辅助（与 test_computer_governance_recovery.py 同构）/ helpers ─────
def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _seed_home(tmp_path: Path, *, servers: Sequence[str] = (), pid: str = "audit@acme") -> tuple[Path, Path]:
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
    for sname in servers:
        server_def = {"name": sname, "type": "stdio", "server_parameters": {"command": "node"}}
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
                        "bundledMcpServers": list(servers),
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

        # 结果按 name 排序（稳定可测输出）。
        assert [e.name for e in inv] == sorted(e.name for e in inv)


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

    # 卸载前：inventory 含 plugin bundled server（未 boot 亦可查询——纯函数投影）。
    comp_a = Computer(name="a", skill_home=home)
    assert any(e.name == "audit-mcp" for e in comp_a.list_mcp_servers_with_metadata())

    # 卸载效果 = installed_plugins.json 移除记录（uninstall 的账本落点）。
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


# ── 合并语义：同名去重、运行期条目优先 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_inventory_merge_dedupes_by_name_runtime_entry_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户配置与 bundled 同名 → 仅一条；disabled 取运行期配置（运行期优先），归属按 name 命中标 plugin（文档化退化）。"""
    _isolate_declared_env(tmp_path, monkeypatch)
    home, _ = _seed_home(tmp_path, servers=["audit-mcp"])

    # 运行期物化一条同名 server（disabled=True；bundled 侧 disabled=False）。
    async with Computer(name="t", mcp_servers={_user_stdio_server("audit-mcp")}, auto_connect=False, skill_home=home) as comp:
        inv = [e for e in comp.list_mcp_servers_with_metadata() if e.name == "audit-mcp"]
        assert len(inv) == 1, "同名去重：运行期条目优先，不重复补入"
        assert inv[0].disabled, "disabled 应取运行期配置（运行期条目优先）"
        assert isinstance(inv[0].managed_by, McpPluginOwnership), "name 命中 bundled 集 → 标 plugin（文档化退化）"
