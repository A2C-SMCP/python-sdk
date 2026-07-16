# -*- coding: utf-8 -*-
# filename: test_plugin.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``plugin`` 命令 handler 单元测试（v0.2.1 #69）/ Plugin command handler unit tests。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.3 / §10.6（S16）。

测试意图 / Test intentions（相对源 plugin → locate_plugin_root 无 git；monkeypatch stage；XDG 重定向隔离）:
- install（v0.3.0 #123 不激活）：happy（写 installedPlugins 意图 + 账本；**不**挂 server、不 stage skills）/
  **依赖已满足 → 退出码 0 正常安装**（#153/D3，原「外来同名硬抛 + error code」已退役）/ 非法 id 退出码 1；
- enable/disable：**scope 从 ledger 读**（seed project scope → 写 project settings，不写 user）；
- uninstall / list（默认列全部已安装，enabled 两态）/ info / gc（孤儿 = 账本 ∉ installedPlugins）退出码与输出形态；
- ``_plugin_inject_inputs_cb``：读 ``mcp-servers/inputs.json`` → 前缀化 → 注入 fake comp 池（#69 Group A）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.cli.commands import plugin as plugin_cmd
from a2c_smcp.computer.settings.scope import user_settings_path, workdir_project_settings_path
from a2c_smcp.computer.settings.store import load_installed_plugins, save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.utils.bundle_id import resolve_bundle_id

_SRC = {"type": "git", "url": "https://example.com/acme.git"}
_STAGE = "a2c_smcp.computer.settings.installer.stage_marketplace_skills"


# ── 辅助（镜像 test_installer.py）/ helpers mirroring test_installer.py ─────────
def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _stdio(name: str, command: str = "node") -> dict:
    return {"name": name, "type": "stdio", "server_parameters": {"command": command}}


def _setup_catalog(home: Path, mp: str, plugin: str, *, servers: list[str], inputs: list[dict] | None = None) -> Path:
    """造 catalog 树（相对源 plugin）+ marketplace.json + plugin mcp-servers/，seed known_marketplaces。"""
    catalog = marketplace_skill_dir(home, mp)
    manifest = {
        "name": mp,
        "owner": {"name": "X"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": plugin, "source": plugin, "version": "1.2.0"}],
    }
    _write_json(catalog / ".tfrobot-plugin" / "marketplace.json", manifest)
    plugin_root = catalog / "plugins" / plugin
    plugin_root.mkdir(parents=True, exist_ok=True)
    for sname in servers:
        _write_json(plugin_root / "mcp-servers" / f"{sname}.json", _stdio(sname))
    if inputs is not None:
        _write_json(plugin_root / "mcp-servers" / "inputs.json", {"inputs": inputs})
    save_known_marketplaces(
        {"version": 1, "marketplaces": {mp: {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc"}}},
        home=home,
    )
    return plugin_root


def _fake_stage(register_skill: bool = True):
    async def _stage(name, source, registry, home, *, plugin_filter=None, auto_update=False, refresh=False, timeout=0.0, env=None):
        if register_skill:
            for plugin in plugin_filter or set():
                registry.register_or_update(
                    {
                        "name": f"{plugin}:lint",
                        "source": f"{SOURCE_MARKETPLACE}:{name}",
                        "path": str(marketplace_skill_dir(home, name).resolve()),
                    },
                )
        return [f"{p}:lint" for p in plugin_filter or set()]

    return _stage


class _FakeMCP:
    """记录 MCP 注入回调调用 / records injected MCP callback invocations。"""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing: set[str] = set(existing or ())
        self.registered: list[Any] = []
        self.removed: list[str] = []

    def existing_bundle_ids(self) -> set[str]:
        return set(self.existing)

    async def register(self, cfg: Any) -> None:
        self.registered.append(cfg)

    async def remove(self, name: str) -> None:
        self.removed.append(name)


# ── install ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_install_happy_writes_intent_and_ledger_without_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """v0.3.0：install 只写 installedPlugins 意图 + 账本 → installed_disabled（无挂载、无 skills）。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _setup_catalog(home, "acme", "audit", servers=["figma-mcp"])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP()

    code = await plugin_cmd.plugin_install(
        reg, home, env, "audit@acme",
        existing_bundle_ids=mcp.existing_bundle_ids, json_output=True,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["state"] == "installed_disabled"
    assert "audit@acme" in load_installed_plugins(home=home, env=env)["plugins"]
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["audit@acme"]
    assert "enabledPlugins" not in user  # 不激活、不写启用意图
    assert len(reg) == 0  # skills 不投影


@pytest.mark.asyncio
async def test_install_dependency_satisfied_exit0_and_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """
    同 bundle_id 已有 = 依赖已满足 → **退出码 0 + 正常安装**（协议 §2.5-1，#153/D3）。

    原名 ``test_install_foreign_name_conflict_exit1_json_error``：断言退出码 1 + JSON
    ``error=mcp_server_name_conflict``。该错误码连同 ``MCPServerNameConflictError`` 已随 D3 退役——
    plugin 与 MCP Server 是依赖关系，本地已有即依赖满足，复用而非拒绝。
    """
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _setup_catalog(home, "acme", "audit", servers=["figma-mcp"])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP(existing={"figma-mcp"})  # 本地已有同 bundle_id

    code = await plugin_cmd.plugin_install(
        reg, home, env, "audit@acme",
        existing_bundle_ids=mcp.existing_bundle_ids, json_output=True,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["mcpServers"] == ["figma-mcp"]
    assert "audit@acme" in load_installed_plugins(home=home, env=env)["plugins"]
    assert "audit@acme" in json.loads(user_settings_path(env).read_text(encoding="utf-8"))["installedPlugins"]


@pytest.mark.asyncio
async def test_install_invalid_id_exit1(tmp_path: Path) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    assert await plugin_cmd.plugin_install(reg, home, env, "nodelim", json_output=True) == 1


# ── enable / disable：scope 从 ledger 读（不默认 user）/ scope read from ledger ──
@pytest.mark.asyncio
async def test_enable_reads_scope_from_ledger_writes_project_not_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    plugin_root = _setup_catalog(home, "acme", "audit", servers=["figma-mcp"])
    monkeypatch.setattr(_STAGE, _fake_stage())
    wd = tmp_path / "proj"
    wd.mkdir()
    # seed installed record at PROJECT scope（projectPath=wd）→ enable 必须写 project settings、不写 user
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "project", "projectPath": str(wd), "installPath": str(plugin_root)}]}},
        home=home,
    )
    mcp = _FakeMCP()
    code = await plugin_cmd.plugin_enable(
        reg, home, env, "audit@acme", existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register,
    )
    assert code == 0
    # enabledPlugins[audit@acme]=true 写 project scope（active workdir），user scope 不动
    proj = json.loads(workdir_project_settings_path(wd).read_text(encoding="utf-8"))
    assert proj["enabledPlugins"]["audit@acme"] is True
    user_ep = json.loads(user_settings_path(env).read_text()).get("enabledPlugins", {}) if user_settings_path(env).exists() else {}
    assert "audit@acme" not in user_ep  # 未写 user scope


@pytest.mark.asyncio
async def test_enable_not_installed_exit1(tmp_path: Path) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    assert await plugin_cmd.plugin_enable(reg, home, env, "ghost@acme") == 1


@pytest.mark.asyncio
async def test_disable_reads_scope_from_ledger(tmp_path: Path) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    plugin_root = _setup_catalog(home, "acme", "audit", servers=["figma-mcp"])
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(plugin_root), "mcpServers": ["figma-mcp"]}]}},
        home=home,
    )
    mcp = _FakeMCP()
    code = await plugin_cmd.plugin_disable(reg, home, env, "audit@acme", remove_server=mcp.remove)
    assert code == 0
    assert mcp.removed == ["figma-mcp"]  # 整 plugin 下线：摘 bundled server
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["enabledPlugins"]["audit@acme"] is False


# ── uninstall / list / info / gc ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_uninstall_not_installed_exit1(tmp_path: Path) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    assert await plugin_cmd.plugin_uninstall(reg, home, env, "ghost@acme") == 1


def test_list_and_info(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": "/x", "mcpServers": ["figma-mcp"]}]}},
        home=home,
    )
    # v0.3.0 缺省翻转：无 enabledPlugins 条目 = installed_disabled——默认列表仍可见，但 enabled=False
    assert plugin_cmd.plugin_list(home, env, json_output=True) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == "audit@acme" and rows[0]["enabled"] is False
    assert plugin_cmd.plugin_info(home, env, "audit@acme", json_output=True) == 0
    assert plugin_cmd.plugin_info(home, env, "ghost@acme", json_output=True) == 1


def test_list_shows_all_installed_with_enabled_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """v0.3.0：默认 list 列全部已安装（install-only 必须可见）；enabled 列仅显式 true 为 ✓。"""
    home, env = _home(tmp_path), _env(tmp_path)
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                "audit@acme": [{"scope": "user", "installPath": "/x"}],
                "fmt@acme": [{"scope": "user", "installPath": "/y"}],
            },
        },
        home=home,
    )
    from a2c_smcp.computer.settings.scope import apply_write
    from a2c_smcp.computer.settings.scope import user_settings_path as _usp
    from a2c_smcp.computer.settings.store import atomic_write_json

    atomic_write_json(_usp(env), apply_write({}, {"enabledPlugins": {"audit@acme": False, "fmt@acme": True}}))
    plugin_cmd.plugin_list(home, env, json_output=True)
    rows = {r["id"]: r["enabled"] for r in json.loads(capsys.readouterr().out)}
    assert rows == {"audit@acme": False, "fmt@acme": True}  # disabled 不再被默认隐藏
    plugin_cmd.plugin_list(home, env, available=True, json_output=True)
    rows_avail = {r["id"]: r["enabled"] for r in json.loads(capsys.readouterr().out)}
    assert rows_avail == rows  # --available 兼容 no-op（旧"含 disabled"已成默认）


@pytest.mark.asyncio
async def test_gc_no_orphans(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    # v0.3.0：孤儿判据 = 账本 ∉ installedPlugins → user settings 声明安装意图 audit@acme → 非孤儿
    from a2c_smcp.computer.settings.scope import apply_write
    from a2c_smcp.computer.settings.scope import user_settings_path as _usp
    from a2c_smcp.computer.settings.store import atomic_write_json

    atomic_write_json(_usp(env), apply_write({}, {"installedPlugins": ["audit@acme"]}))
    save_installed_plugins({"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": "/x"}]}}, home=home)
    assert await plugin_cmd.plugin_gc(reg, home, env, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == []


# ── _plugin_inject_inputs_cb：D2 入池消歧（#69 Group A）/ inject prefixed inputs ─
@pytest.mark.asyncio
async def test_inject_inputs_cb_prefixes_and_injects(tmp_path: Path) -> None:
    home = _home(tmp_path)
    plugin_root = _setup_catalog(
        home, "acme", "audit", servers=["figma-mcp"],
        inputs=[{"id": "figma_token", "type": "promptString", "description": "tok", "password": True}],
    )
    injected: list[Any] = []

    class _Comp:
        def add_or_update_input(self, inp: Any) -> None:
            injected.append(inp)

    cb = plugin_cmd._plugin_inject_inputs_cb(_Comp(), "audit", "acme")
    await cb(plugin_root)
    assert len(injected) == 1
    assert injected[0].id == "audit@acme/figma_token"  # 前缀化（§9.3 D2）


# ── repl_dispatch（REPL 解析胶水层；fix-review #2）/ REPL parse glue ────────────
class _FakeManager:
    """``Computer.mcp_manager`` 的最小替身：**运行期权威配置集**（#153 的 existing 数据源）。"""

    def __init__(self) -> None:
        self._servers: list[Any] = []

    def server_configs(self) -> tuple[Any, ...]:
        return tuple(self._servers)


class _ReplComp:
    """
    plugin repl_dispatch 所需最小 fake（build_mcp_callbacks + register/inject 闭包所需接口）。

    **形状须与生产同构**（#153 / Epic #147 桩陷阱）：运行期挂载落 ``mcp_manager``（权威），而 ``mcp_servers``
    是**构造期快照**——CLI 下恒空。旧版本此桩让 ``mcp_servers`` 随 ``amount_server`` 增长，把生产中**恒假**的
    前提固化为真，使读死快照的 bug 在测试里看起来是活的。
    """

    def __init__(self, home: Path, registry: SkillRegistry) -> None:
        self.skill_home = home
        self.skill_registry = registry
        self._registered_workdirs: tuple[Path, ...] = ()
        self.active_workdir: Path | None = None
        self.dirty = 0
        self.mcp_manager = _FakeManager()
        self.injected: list[Any] = []

    @property
    def mcp_servers(self) -> tuple[Any, ...]:
        """构造期快照：CLI 恒传 ``mcp_servers=set()`` ⇒ **恒空**，与生产一致（勿让它随挂载增长）。"""
        return ()

    def mark_skills_dirty(self) -> None:
        self.dirty += 1

    def add_or_update_input(self, inp: Any) -> None:
        self.injected.append(inp)

    async def amount_server(self, cfg: Any, *, session: Any = None, plugin: Any = None, marketplace: Any = None) -> None:
        # #137 ③：plugin enable/install remount 经 build_mcp_callbacks → transient amount_server（治理投影）。
        self.mcp_manager._servers.append(cfg)

    async def aunmount_server_by_id(self, bundle_id: str) -> None:
        # #153：停摘链收 bundle_id（账本记 bundle_id）；#137 ③ transient（停进程不删声明）。
        self.mcp_manager._servers = [s for s in self.mcp_manager._servers if resolve_bundle_id(s) != bundle_id]


@pytest.mark.asyncio
async def test_repl_install_is_lazy_no_mount_no_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.3.0：REPL install 不激活——不挂载 bundled server、skills 无变化不 mark dirty；账本 + 意图照写。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    home, reg = _home(tmp_path), SkillRegistry()
    _setup_catalog(home, "acme", "audit", servers=["figma-mcp"])
    monkeypatch.setattr(_STAGE, _fake_stage())
    comp = _ReplComp(home, reg)
    await plugin_cmd.repl_dispatch(comp, ["plugin", "install", "audit@acme"], session=None)
    assert comp.dirty == 0  # skills 无变化 → 不触发去抖 emit
    assert "audit@acme" in load_installed_plugins(home=home)["plugins"]
    assert comp.mcp_manager.server_configs() == ()  # 不实时挂载（enable 才点亮）
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_repl_list_and_unknown_subcommand_no_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    comp = _ReplComp(_home(tmp_path), SkillRegistry())
    await plugin_cmd.repl_dispatch(comp, ["plugin", "list"], session=None)  # 空 → 无错
    await plugin_cmd.repl_dispatch(comp, ["plugin", "bogus"], session=None)  # 未知子命令 → no-op
    assert comp.dirty == 0


# ── run_governance_remount（#117 设计 Y CLI 参考接线）/ boot-time governance remount ──
def _seed_recovery_home(home: Path, *, servers: list[str], skills: list[str]) -> Path:
    """预置治理恢复态：catalog 树（acme/audit，含 mcp-servers/ + skills/）+ 双账本。返回 plugin 根。"""
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
        _write_json(plugin_root / "mcp-servers" / f"{sname}.json", _stdio(sname))
    for sk in skills:
        p = plugin_root / "skills" / sk / "SKILL.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nname: {sk}\ndescription: d\n---\nbody\n", encoding="utf-8")
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": _SRC, "installLocation": str(catalog.resolve())}}},
        home=home,
    )
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                "audit@acme": [
                    {"scope": "user", "installPath": str(plugin_root), "version": "1.2.0", "mcpServers": list(servers)},
                ],
            },
        },
        home=home,
    )
    return plugin_root


def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)


@pytest.mark.asyncio
async def test_run_governance_remount_wires_ownership_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 接线断言：register 经 transient comp.amount_server 携正确 plugin/marketplace 归属上下文（#137 ③）。"""
    from a2c_smcp.computer.computer import Computer

    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=["figma-mcp"], skills=["lint"])

    async with Computer(name="t", skill_home=home) as comp:
        recorded: list[tuple[str, str | None, str | None]] = []

        async def fake_register(server: Any, *, session: Any = None, plugin: str | None = None, marketplace: str | None = None) -> None:
            recorded.append((server.name, plugin, marketplace))

        monkeypatch.setattr(comp, "amount_server", fake_register)  # #137 ③：治理重挂经 transient amount_server
        await plugin_cmd.run_governance_remount(comp)

        assert recorded == [("figma-mcp", "audit", "acme")]


@pytest.mark.asyncio
async def test_run_governance_remount_flag_declared_disables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """flag-aware declared 生效于阶段二：``--settings`` 文件里 enabledPlugins=false → 不重挂。"""
    from a2c_smcp.computer.computer import Computer

    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=["figma-mcp"], skills=["lint"])
    flag_file = tmp_path / "flag-settings.json"
    flag_file.write_text(json.dumps({"enabledPlugins": {"audit@acme": False}}), encoding="utf-8")

    async with Computer(name="t", skill_home=home) as comp:
        recorded: list[str] = []

        async def fake_register(server: Any, *, session: Any = None, plugin: str | None = None, marketplace: str | None = None) -> None:
            recorded.append(server.name)

        monkeypatch.setattr(comp, "amount_server", fake_register)  # #137 ③：治理重挂经 transient amount_server
        await plugin_cmd.run_governance_remount(comp, flag_config=flag_file)

        assert recorded == []


@pytest.mark.asyncio
async def test_run_governance_remount_skips_dependency_satisfied_from_runtime_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    治理重挂的 existing 取自**运行期权威配置集**：同 bundle_id 已挂 → 依赖已满足 → skip 不覆盖（#153）。

    **F7 真实构造路径**（协议 conformance §2.0 / Epic #147）：用户 server 经 ``amount_server`` 挂载——这正是
    生产里 server 到达 Computer 的唯一途径（CLI 恒 ``mcp_servers=set()``）。原用例用 ``comp._mcp_servers.add()``
    直接塞构造期快照建立前提，而该快照在生产中**恒空**：于是「读死快照」的 bug 在测试里看起来是活的，
    这条守卫也就从未真正守过任何东西。
    """
    from pydantic import TypeAdapter

    from a2c_smcp.computer.computer import Computer
    from a2c_smcp.computer.mcp_clients.model import MCPServerConfig

    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=["figma-mcp"], skills=["lint"])

    # auto_connect=False：config-only 挂载（不起子进程），与 #150 的 F7 范式一致（test_client.py:169-216）。
    async with Computer(name="t", mcp_servers=set(), auto_connect=False, skill_home=home) as comp:
        user_cfg = TypeAdapter(MCPServerConfig).validate_python(_stdio("figma-mcp"))
        recorded: list[str] = []

        async def fake_register(server: Any, *, session: Any = None, plugin: str | None = None, marketplace: str | None = None) -> None:
            recorded.append(server.name)

        # 先按真实路径挂载用户 server（占住 bundle_id），再 monkeypatch 掉 amount_server 观测治理重挂。
        await comp.amount_server(user_cfg)
        assert comp.mcp_servers == ()  # 构造期快照恒空 —— 对照组：读它必然判「未满足」
        monkeypatch.setattr(comp, "amount_server", fake_register)  # #137 ③：治理重挂经 transient amount_server

        await plugin_cmd.run_governance_remount(comp)

        assert recorded == []  # 依赖已满足 → 复用既有实例，MUST NOT 覆盖用户配置


# ── #125 任务 1：危害链回归——重物化推回后 disable 跨 boot 有效 ─────────────────
@pytest.mark.asyncio
async def test_disable_effective_across_boot_after_rematerialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """project 装+enable → 账本丢失 → recover 推回 project 记录 → disable 写 project 层 false（merged 不再 enabled）。"""
    from a2c_smcp.computer.settings.recovery import recover_marketplace_skills

    _isolate_env(tmp_path, monkeypatch)
    home, env, reg = _home(tmp_path), dict(os.environ), SkillRegistry()
    _setup_catalog(home, "acme", "audit", servers=["figma-mcp"])
    workdir = Path.cwd()
    # 模拟 project scope 安装+启用后账本丢失：仅 settings 意图在
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"]})
    _write_json(workdir_project_settings_path(workdir), {"enabledPlugins": {"audit@acme": True}})
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}

    report = await recover_marketplace_skills(reg, home, declared, env=env)
    assert report.rematerialized == ["audit@acme"]

    mcp = _FakeMCP()
    code = await plugin_cmd.plugin_disable(reg, home, env, "audit@acme", remove_server=mcp.remove)

    assert code == 0
    proj = json.loads(workdir_project_settings_path(workdir).read_text(encoding="utf-8"))
    assert proj["enabledPlugins"]["audit@acme"] is False  # 写对层（project，而非归一后的 user）
    assert plugin_cmd._enabled_plugins_view(home, env).get("audit@acme") is not True  # merged 视图不再 enabled


# ── #125 任务 2：plugin gc 扩展（悬挂意图诊断/prune + 权威性不对称安全阀）────────
def _seed_orphan_and_dangling(home: Path, env: dict[str, str]) -> Path:
    """孤儿（账本 ∖ 意图，live installPath）+ 悬挂（意图 ∖ 账本，marketplace 未添加）。返回孤儿 installPath。"""
    orphan_path = marketplace_skill_dir(home, "acme") / "plugins" / "orphan"
    orphan_path.mkdir(parents=True, exist_ok=True)
    save_installed_plugins(
        {"version": 1, "plugins": {"orphan@acme": [{"scope": "user", "installPath": str(orphan_path)}]}},
        home=home,
    )
    _write_json(user_settings_path(env), {"installedPlugins": ["ghost@nowhere"], "enabledPlugins": {"ghost@nowhere": True}})
    return orphan_path


@pytest.mark.asyncio
async def test_gc_prunes_dangling_json_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON 契约：``removed`` 键不变；增 ``dangling``（诊断+reason）/``prunedIntents``（实际删）/``recoverable``。"""
    _isolate_env(tmp_path, monkeypatch)
    home, env, reg = _home(tmp_path), dict(os.environ), SkillRegistry()
    orphan_path = _seed_orphan_and_dangling(home, env)

    code = await plugin_cmd.plugin_gc(reg, home, env, json_output=True, prune_dangling=True)

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"] == ["orphan@acme"]
    assert out["dangling"] == [{"id": "ghost@nowhere", "reason": "marketplace-not-added"}]
    assert out["prunedIntents"] == ["ghost@nowhere"]
    assert out["recoverable"] == []
    assert not orphan_path.exists()
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert "ghost@nowhere" not in user.get("installedPlugins", [])  # 意图已 prune
    assert "ghost@nowhere" not in user.get("enabledPlugins", {})


@pytest.mark.asyncio
async def test_gc_confirm_combined_and_abort_keeps_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm 收到孤儿+悬挂组合描述（悬挂条目带 reason 后缀）；拒绝 → 零变更、退出码 1。"""
    _isolate_env(tmp_path, monkeypatch)
    home, env, reg = _home(tmp_path), dict(os.environ), SkillRegistry()
    orphan_path = _seed_orphan_and_dangling(home, env)
    got: list[str] = []

    async def _confirm(items: list[str]) -> bool:
        got.extend(items)
        return False

    code = await plugin_cmd.plugin_gc(reg, home, env, confirm=_confirm, prune_dangling=True)

    assert code == 1
    assert any("orphan@acme" in s for s in got)
    assert any("ghost@nowhere" in s and "dangling" in s for s in got)  # 悬挂条目带标注
    assert orphan_path.exists()  # 零变更
    assert "orphan@acme" in load_installed_plugins(home=home)["plugins"]
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["ghost@nowhere"]


@pytest.mark.asyncio
async def test_gc_prune_dangling_off_by_default_diagnose_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """权威性不对称：非交互（Typer 默认）悬挂意图**只诊断不删**——删权威意图需显式 ``--prune-dangling``。"""
    _isolate_env(tmp_path, monkeypatch)
    home, env, reg = _home(tmp_path), dict(os.environ), SkillRegistry()
    _write_json(user_settings_path(env), {"installedPlugins": ["ghost@nowhere"]})

    code = await plugin_cmd.plugin_gc(reg, home, env, json_output=True)  # prune_dangling 缺省 False

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dangling"] == [{"id": "ghost@nowhere", "reason": "marketplace-not-added"}]
    assert out["prunedIntents"] == []
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["ghost@nowhere"]  # 意图原样保留


# ── #125 任务 3：plugin list --available 弃用提示 ──────────────────────────────
def test_list_available_prints_deprecation_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--available 为兼容 no-op：非 JSON 模式打弃用提示；不带 flag 时不打。"""
    home, env = _home(tmp_path), _env(tmp_path)
    save_installed_plugins({"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": "/x"}]}}, home=home)

    plugin_cmd.plugin_list(home, env, available=True)
    assert "deprecated" in capsys.readouterr().out

    plugin_cmd.plugin_list(home, env)
    assert "deprecated" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_gc_prune_residual_committable_declaration_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """悬挂意图仅来自 committable project 层声明 → prune 不改写该文件、不计入 prunedIntents（隔离审查 🟡#1：
    误报会让自动化「prune 到干净」永不收敛），归 residualDeclarations 显式暴露。"""
    _isolate_env(tmp_path, monkeypatch)
    home, env, reg = _home(tmp_path), dict(os.environ), SkillRegistry()
    proj_path = workdir_project_settings_path(Path.cwd())
    _write_json(proj_path, {"installedPlugins": ["ghost@nowhere"]})
    before = proj_path.read_text(encoding="utf-8")

    code = await plugin_cmd.plugin_gc(reg, home, env, json_output=True, prune_dangling=True)

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dangling"] == [{"id": "ghost@nowhere", "reason": "marketplace-not-added"}]
    assert out["prunedIntents"] == []  # 未真正移除，不得计入
    assert out["residualDeclarations"] == ["ghost@nowhere"]
    assert proj_path.read_text(encoding="utf-8") == before  # committable 声明未被改写


@pytest.mark.asyncio
async def test_gc_confirm_accept_removes_orphans_and_prunes_dangling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm 通过（返回 True）→ 孤儿删除 + 悬挂真 prune 双双生效（隔离审查 🟡#6 正路覆盖）。"""
    _isolate_env(tmp_path, monkeypatch)
    home, env, reg = _home(tmp_path), dict(os.environ), SkillRegistry()
    orphan_path = _seed_orphan_and_dangling(home, env)

    async def _confirm(items: list[str]) -> bool:
        return True

    code = await plugin_cmd.plugin_gc(reg, home, env, confirm=_confirm, prune_dangling=True)

    assert code == 0
    assert not orphan_path.exists()  # 孤儿已删
    assert "orphan@acme" not in load_installed_plugins(home=home)["plugins"]
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert "ghost@nowhere" not in user.get("installedPlugins", [])  # 悬挂已 prune
    assert "ghost@nowhere" not in user.get("enabledPlugins", {})


def test_list_available_json_stdout_stays_pure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--available + --json：弃用提示走 logger，stdout 保持纯 JSON 可解析（隔离审查 🟡#5 验收补缺）。"""
    home, env = _home(tmp_path), _env(tmp_path)
    save_installed_plugins({"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": "/x"}]}}, home=home)

    plugin_cmd.plugin_list(home, env, available=True, json_output=True)

    out = capsys.readouterr().out
    rows = json.loads(out)  # stdout 可整体解析 = 纯 JSON
    assert rows[0]["id"] == "audit@acme"
    assert "deprecated" not in out
