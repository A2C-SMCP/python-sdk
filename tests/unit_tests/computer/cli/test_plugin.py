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

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from a2c_smcp.computer.cli.commands import plugin as plugin_cmd
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import MCPServerPromptStringInput
from a2c_smcp.computer.settings.scope import user_settings_path, workdir_project_settings_path
from a2c_smcp.computer.settings.store import (
    load_installed_plugins,
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
)
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.utils.bundle_id import resolve_bundle_id

_SRC = {"type": "git", "url": "https://example.com/acme.git"}
_STAGE = "a2c_smcp.computer.settings.installer.stage_marketplace_skills"

# 夹具身份对（#153 隔离审查 🔴2 + 协议 conformance §2.0）：原夹具 "figma-mcp" 的 bundle_id **恰等于自身**
# （normalize_name 字符集含 `-` 且不折 `-`）⇒ 「误用 name 当身份」的实现无法被鉴别（Epic #147 陷阱 (b)
# 第五例）。改用含 `.` 的 display 名令两者分叉。/ Forked so name-as-identity bugs cannot pass.
FIGMA_NAME, FIGMA_BID = "figma.mcp", "figma_mcp"


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    隔离 cwd / Isolate the process cwd。

    #153 起 disable / gc 经回收判据读 **cwd 锚定**的 ``.tfrobot/mcp[.local].json``（project/local scope，
    #116）。不 chdir 则读进真实仓库的工作区配置 → 断言随开发者本地环境漂移（#137 同款；本文件的泄漏由
    #153 隔离审查 🟡 实测发现）。
    """
    monkeypatch.chdir(tmp_path)


# ── 辅助（镜像 test_installer.py）/ helpers mirroring test_installer.py ─────────
def _flat(text: str) -> str:
    """去掉全部空白：rich 会按终端宽度折行（长 token 会被从中间折断），人类输出断言不得依赖换行位置。"""
    return "".join(text.split())


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
    """造 catalog 树（相对源 plugin）+ marketplace.json + plugin mcp-servers/，**合并** seed known_marketplaces。

    #257：合并而非替换——跨 marketplace 同名 plugin 场景须同测试内共存两个 marketplace
    （范式同 ``test_mcp_dependency_model.py``）。
    """
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
    known = load_known_marketplaces(home=home)
    known["marketplaces"][mp] = {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc"}
    save_known_marketplaces(known, home=home)
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
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
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
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP(existing={FIGMA_BID})  # 本地已有同 bundle_id（身份 = bundle_id，非 display 名）

    code = await plugin_cmd.plugin_install(
        reg, home, env, "audit@acme",
        existing_bundle_ids=mcp.existing_bundle_ids, json_output=True,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["mcpServers"] == [FIGMA_BID]  # wire 值 = bundle_id
    assert "audit@acme" in load_installed_plugins(home=home, env=env)["plugins"]
    assert "audit@acme" in json.loads(user_settings_path(env).read_text(encoding="utf-8"))["installedPlugins"]


@pytest.mark.asyncio
async def test_install_invalid_id_exit1(tmp_path: Path) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    assert await plugin_cmd.plugin_install(reg, home, env, "nodelim", json_output=True) == 1


@pytest.mark.asyncio
async def test_install_second_marketplace_same_bare_name_exit1_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#257 / conformance §5：同名不同 marketplace 的 install → 退出码 1 + JSON ``error=validation``。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _setup_catalog(home, "mp-a", "alpha", servers=[])
    _setup_catalog(home, "mp-b", "alpha", servers=[])
    monkeypatch.setattr(_STAGE, _fake_stage())

    assert await plugin_cmd.plugin_install(reg, home, env, "alpha@mp-a", json_output=True) == 0
    capsys.readouterr()  # 丢弃首个成功输出

    code = await plugin_cmd.plugin_install(reg, home, env, "alpha@mp-b", json_output=True)
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation"
    assert payload["conflictWith"] == "alpha@mp-a"
    assert "alpha@mp-a" in payload["message"]
    # 意图未被写入第二个 pid
    assert json.loads(user_settings_path(env).read_text(encoding="utf-8"))["installedPlugins"] == ["alpha@mp-a"]


@pytest.mark.asyncio
async def test_enable_shadowed_by_other_marketplace_exit1_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """enable 被另一 marketplace 同名技能遮蔽 → 退出码 1 + JSON 结构化 validation 与可执行补救（#257 复审 🟡10）。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _setup_catalog(home, "mp-a", "alpha", servers=[])
    _setup_catalog(home, "mp-b", "alpha", servers=[])  # 补救指向的 marketplace 须真实存在（复审 🟡5）
    monkeypatch.setattr(_STAGE, _fake_stage())
    assert "mp-b" in load_known_marketplaces(home=home)["marketplaces"]
    # 预置 `marketplace add mp-b` 全量 stage 的效果：同名技能已由 mp-b 注册
    reg.register({"name": "alpha:lint", "source": "marketplace:mp-b", "path": "/abs/b/lint", "description": "from-b"})

    assert await plugin_cmd.plugin_install(reg, home, env, "alpha@mp-a", json_output=True) == 0
    capsys.readouterr()

    code = await plugin_cmd.plugin_enable(reg, home, env, "alpha@mp-a", json_output=True)
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation"
    assert payload["conflictWith"] == "alpha@mp-b"
    assert "marketplace remove mp-b" in payload["message"]  # 冲突方未安装 → 不得给 no-op 的 uninstall


@pytest.mark.asyncio
async def test_enable_identity_collapse_json_reports_bundle_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#271：identity collapse 的 CLI JSON 用 ``bundleId``，且**不得**出现 ``conflictWith``。

    ``conflictWith`` 是 ``<plugin>@<marketplace>`` pid 的专属键（跨 marketplace 同名冲突）；身份坍缩给
    ``bundleId``。二者若混用，下游按 ``conflictWith`` 解析 pid 的消费者会拿到裸 bundle_id。
    """
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    root = _setup_catalog(home, "acme", "audit", servers=[])
    for name in ("alpha", "beta"):
        _write_json(
            root / "mcp-servers" / f"{name}.json",
            {"name": name, "type": "stdio", "server_parameters": {"command": "node"}, "bundle_id": "shared"},
        )
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(root)}]}}, home=home
    )
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"]})
    monkeypatch.setattr(_STAGE, _fake_stage())

    code = await plugin_cmd.plugin_enable(reg, home, env, "audit@acme", json_output=True)

    assert code == plugin_cmd.EXIT_USER_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation"
    assert payload["bundleId"] == "shared"
    assert "conflictWith" not in payload
    assert "different configs under the same install root" in payload["message"]


def test_plugin_error_filters_reserved_detail_keys(capsys: pytest.CaptureFixture[str]) -> None:
    """``details`` 的保留键必须被过滤，不得改写类别码/文案/退出码（#257 架构师审查 🟡3）。"""
    from a2c_smcp.computer.cli.commands.plugin import _plugin_error
    from a2c_smcp.computer.settings.installer import PluginInstallError

    err = PluginInstallError(
        "boom",
        error_code="validation",
        details={
            "code": 0,
            "json_output": True,
            "error_code": "hijacked",
            "msg": "HIJACKED",  # `_err` 的位置参数名——漏过滤会在 except 内抛 TypeError（复审 🟡2）
            "error": "HIJACKED",
            "message": "HIJACKED",
            "conflictWith": "alpha@mp-a",
        },
    )
    assert _plugin_error(err, json_output=True) == plugin_cmd.EXIT_USER_ERROR  # 退出码不被 details["code"] 改写
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation"  # details["error"] 未覆盖类别码
    assert payload["message"] == "boom"  # details["message"] 未覆盖人读文案
    assert payload["conflictWith"] == "alpha@mp-a"  # 普通键原样透出
    for reserved in ("code", "json_output", "error_code", "msg"):
        assert reserved not in payload, f"reserved detail key {reserved!r} must be filtered"


# ── enable / disable：scope 从 ledger 读（不默认 user）/ scope read from ledger ──
@pytest.mark.asyncio
async def test_enable_reads_scope_from_ledger_writes_project_not_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    plugin_root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
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
        reg, home, env, "audit@acme", existing_bundle_ids=mcp.existing_bundle_ids,
        register_server=mcp.register, remove_server=mcp.remove,
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
    plugin_root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(plugin_root), "mcpServers": [FIGMA_NAME]}]}},
        home=home,
    )
    mcp = _FakeMCP()
    code = await plugin_cmd.plugin_disable(reg, home, env, "audit@acme", non_plugin_bundle_ids=lambda: set(), remove_server=mcp.remove)
    assert code == 0
    assert mcp.removed == [FIGMA_NAME]  # 整 plugin 下线：摘 bundled server
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["enabledPlugins"]["audit@acme"] is False


# ── uninstall / list / info / gc ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_uninstall_not_installed_exit1(tmp_path: Path) -> None:
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    assert await plugin_cmd.plugin_uninstall(reg, home, env, "ghost@acme", non_plugin_bundle_ids=lambda: set()) == 1


def test_list_and_info(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": "/x", "mcpServers": [FIGMA_NAME]}]}},
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
    assert await plugin_cmd.plugin_gc(reg, home, env, non_plugin_bundle_ids=lambda: set(), json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == []


# ── _plugin_inject_inputs_cb：D2 入池消歧（#69 Group A）/ inject prefixed inputs ─
@pytest.mark.asyncio
async def test_inject_inputs_cb_prefixes_and_injects(tmp_path: Path) -> None:
    home = _home(tmp_path)
    plugin_root = _setup_catalog(
        home, "acme", "audit", servers=[FIGMA_NAME],
        inputs=[{"id": "figma_token", "type": "promptString", "description": "tok", "password": True}],
    )
    injected: list[Any] = []

    class _Comp:
        def add_or_update_input(self, inp: Any) -> None:
            injected.append(inp)

        def list_inputs(self) -> tuple[Any, ...]:
            # 返回**累积池**而非恒空——#155 的坍缩前检要读它；恒空会让「与既有池撞名」这一支永远测不到。
            return tuple(injected)

    cb = plugin_cmd._plugin_inject_inputs_cb(_Comp(), "audit", "acme")
    await cb(plugin_root)
    assert len(injected) == 1
    assert injected[0].id == "audit@acme/figma_token"  # 前缀化（§9.3 D2）


@pytest.mark.asyncio
async def test_inject_inputs_cb_skips_collisions_without_partial_injection(tmp_path: Path) -> None:
    """#155：plugin inputs 撞 env 名 → 肇事对全跳过、无辜项照常注入，**不中途 abort**。

    ``_inject_plugin_inputs`` 是 install / enable / 治理重挂三条路径的 DRY 单点；裸循环在此撞名会
    partial-inject 并把异常抛给上游 catch-all（enable 路径只 catch PluginInstallError ⇒ 会穿透，
    留下「inputs 注入一半 + plugin 未启用」）。
    """
    home = _home(tmp_path)
    plugin_root = _setup_catalog(
        home, "acme", "audit", servers=[FIGMA_NAME],
        # 前缀化后 `audit@acme/tok-a` 与 `audit@acme/tok_a` 同映射到 A2C_SMCP_audit_acme_tok_a
        inputs=[
            {"id": "tok-a", "type": "promptString", "description": "d"},
            {"id": "tok_a", "type": "promptString", "description": "d"},
            {"id": "safe", "type": "promptString", "description": "d"},
        ],
    )
    injected: list[Any] = []

    class _Comp:
        def add_or_update_input(self, inp: Any) -> None:
            injected.append(inp)

        def list_inputs(self) -> tuple[Any, ...]:
            return tuple(injected)

    cb = plugin_cmd._plugin_inject_inputs_cb(_Comp(), "audit", "acme")
    await cb(plugin_root)  # 不得抛

    assert {i.id for i in injected} == {"audit@acme/safe"}, f"肇事对未全跳过 / 无辜项被连坐：{[i.id for i in injected]}"


def test_inject_inputs_skips_candidate_colliding_with_existing_pool(capsys: pytest.CaptureFixture[str]) -> None:
    """#155 **跨批次**坍缩：候选与**池内既有** id 撞名 → 只跳候选、既有不动、无辜项照常、**不抛**。

    守卫 ``_inject_inputs_collision_safe`` 的 ``existing`` 项——**承重墙且此前无人看守**：把它丢掉
    （只做批次内前检）后本仓相关用例全绿，而本用例转红并复现原始 🔴（EnvNameCollisionError 逸出 +
    部分注入）。判据参照 rubric 5.2：实现替换为空，测试必须能红。

    生产可达（两条真实路径）：
      ① ``run_mcp_approval``（mcp.json 裸 id）先注入、``run_governance_remount``（plugin 前缀 id）后注入：
         用户 mcp.json 写 ``audit_acme_token`` + plugin ``audit@acme`` 声明 ``token`` → 同映射；
      ② 两个 plugin ``a-b@mp`` / ``a_b@mp`` 各自声明同名 input → 先后 enable。

    **必须用真实 Computer**：两处 stub 的 ``list_inputs()`` 都从**空池**起步 ⇒ ``existing`` 分支对它们
    物理上不可达（这也是补 stub 的 ``list_inputs()`` 并**没有**让该分支变可测的原因）。
    """
    # 既有池：mcp.json 声明的裸 id；候选：plugin 前缀 id —— 二者同映射 A2C_SMCP_audit_acme_token
    comp = Computer(name="t", inputs={MCPServerPromptStringInput(id="audit_acme_token", description="d")})
    incoming = [
        MCPServerPromptStringInput(id="audit@acme/token", description="d"),  # 与既有撞名
        MCPServerPromptStringInput(id="audit@acme/safe", description="d"),  # 无辜项
    ]

    plugin_cmd._inject_inputs_collision_safe(comp, incoming, source="plugin audit@acme")  # 不得抛

    pool = {i.id for i in comp.list_inputs()}
    assert "audit@acme/token" not in pool, "撞名候选未被拦下"
    assert "audit_acme_token" in pool, "池内既有项被误伤（无法追溯撤回，MUST 不动）"
    assert "audit@acme/safe" in pool, f"无辜项被连坐丢弃：{pool}"

    # 消息据实分述：不得把「池内既有、其实好好的」那条说成已跳过——否则用户去改错对象
    out = capsys.readouterr().out
    assert "本次跳过" in out and "池内既有" in out, f"跨批次消息未分述归属：{out}"


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

    def __init__(
        self,
        home: Path,
        registry: SkillRegistry,
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.skill_home = home
        self.skill_registry = registry
        self._registered_workdirs: tuple[Path, ...] = ()
        self.active_workdir: Path | None = None
        self.dirty = 0
        self.mcp_manager = _FakeManager()
        self.injected: list[Any] = []
        self.env = env
        self._cwd = cwd
        self.declared_servers: dict[str, Any] = {}  # resolve_mcp_declarations 的声明面（#289 测试用）

    def resolve_mcp_declarations(self) -> Any:
        return SimpleNamespace(servers=self.declared_servers)

    def _resolve_cwd(self) -> Path | None:
        return self._cwd

    @property
    def mcp_servers(self) -> tuple[Any, ...]:
        """构造期快照：CLI 恒传 ``mcp_servers=set()`` ⇒ **恒空**，与生产一致（勿让它随挂载增长）。"""
        return ()

    def mark_skills_dirty(self) -> None:
        self.dirty += 1

    def add_or_update_input(self, inp: Any) -> None:
        self.injected.append(inp)

    def list_inputs(self) -> tuple[Any, ...]:
        return tuple(self.injected)  # 累积池：#155 坍缩前检的数据源

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
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    comp = _ReplComp(home, reg)
    await plugin_cmd.repl_dispatch(comp, ["plugin", "install", "audit@acme"], session=None)
    assert comp.dirty == 0  # skills 无变化 → 不触发去抖 emit
    assert "audit@acme" in load_installed_plugins(home=home)["plugins"]
    assert comp.mcp_manager.server_configs() == ()  # 不实时挂载（enable 才点亮）
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_repl_install_project_scope_uses_instance_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#296：REPL ``plugin install --scope project`` 的 ``projectPath`` 取**实例 cwd**（非进程 cwd）。

    install 的**安装意图恒写 user scope**（全局一次的事实），``scope``/``project_path`` 只进账本记录归档；
    故此处断言账本 ``projectPath`` 与实例 cwd 同源（与 settings / marketplace 守卫的 cwd 轴一致）。
    """
    monkeypatch.chdir(tmp_path)  # 进程 cwd ≠ 实例 cwd
    home, reg = _home(tmp_path), SkillRegistry()
    _setup_catalog(home, "acme", "audit", servers=[])
    monkeypatch.setattr(_STAGE, _fake_stage(register_skill=False))
    project = tmp_path / "project"
    comp = _ReplComp(home, reg, cwd=project)

    await plugin_cmd.repl_dispatch(comp, ["plugin", "install", "audit@acme", "--scope", "project"], session=None)

    records = load_installed_plugins(home=home)["plugins"]["audit@acme"]
    assert any(rec.get("projectPath") == str(project) for rec in records), records
    assert all(rec.get("projectPath") != str(tmp_path) for rec in records), "不得用进程 cwd"


@pytest.mark.asyncio
async def test_repl_install_user_scope_uses_instance_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#296：REPL ``plugin install``（user scope）写**实例 env** 的 user settings，不写 ambient。"""
    ambient_cfg = tmp_path / "ambient-cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(ambient_cfg))
    instance_env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "instance-cfg")}
    home, reg = _home(tmp_path), SkillRegistry()
    _setup_catalog(home, "acme", "audit", servers=[])
    monkeypatch.setattr(_STAGE, _fake_stage(register_skill=False))
    comp = _ReplComp(home, reg, env=instance_env)

    await plugin_cmd.repl_dispatch(comp, ["plugin", "install", "audit@acme"], session=None)

    data = json.loads(user_settings_path(instance_env).read_text(encoding="utf-8"))
    assert data["installedPlugins"] == ["audit@acme"]
    assert not user_settings_path(os.environ).exists(), "不得写 ambient user settings"


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
    _seed_recovery_home(home, servers=[FIGMA_NAME], skills=["lint"])

    async with Computer(name="t", skill_home=home) as comp:
        recorded: list[tuple[str, str | None, str | None]] = []

        starts: list[bool] = []

        async def fake_register(
            server: Any,
            *,
            session: Any = None,
            plugin: str | None = None,
            marketplace: str | None = None,
            start: bool = True,
        ) -> None:
            recorded.append((server.name, plugin, marketplace))
            # #208：治理重挂回调必须**只挂载不启动**——启动统一收在挂载循环之后经批量启动器
            # （共享 Computer 级并发上限）；此处把该契约钉住，防止回退成循环内融合启动。
            starts.append(start)

        monkeypatch.setattr(comp, "amount_server", fake_register)  # #137 ③：治理重挂经 transient amount_server
        await plugin_cmd.run_governance_remount(comp)

        assert recorded == [(FIGMA_NAME, "audit", "acme")]
        assert starts == [False]


@pytest.mark.asyncio
async def test_run_governance_remount_flag_declared_disables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """flag-aware declared 生效于阶段二：``--settings`` 文件里 enabledPlugins=false → 不重挂。"""
    from a2c_smcp.computer.computer import Computer

    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[FIGMA_NAME], skills=["lint"])
    flag_file = tmp_path / "flag-settings.json"
    flag_file.write_text(json.dumps({"enabledPlugins": {"audit@acme": False}}), encoding="utf-8")

    async with Computer(name="t", skill_home=home) as comp:
        recorded: list[str] = []

        async def fake_register(server: Any, *, session: Any = None, plugin: str | None = None, marketplace: str | None = None) -> None:
            recorded.append(server.name)

        monkeypatch.setattr(comp, "amount_server", fake_register)  # #137 ③：治理重挂经 transient amount_server
        await plugin_cmd.run_governance_remount(comp, settings_flag_path=flag_file)

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
    _seed_recovery_home(home, servers=[FIGMA_NAME], skills=["lint"])

    # auto_connect=False：config-only 挂载（不起子进程），与 #150 的 F7 范式一致（test_client.py:169-216）。
    async with Computer(name="t", mcp_servers=set(), auto_connect=False, skill_home=home) as comp:
        user_cfg = TypeAdapter(MCPServerConfig).validate_python(_stdio(FIGMA_NAME))
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
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    workdir = Path.cwd()
    # 模拟 project scope 安装+启用后账本丢失：仅 settings 意图在
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"]})
    _write_json(workdir_project_settings_path(workdir), {"enabledPlugins": {"audit@acme": True}})
    declared = {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}}

    report = await recover_marketplace_skills(reg, home, declared, env=env)
    assert report.rematerialized == ["audit@acme"]

    mcp = _FakeMCP()
    code = await plugin_cmd.plugin_disable(reg, home, env, "audit@acme", non_plugin_bundle_ids=lambda: set(), remove_server=mcp.remove)

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

    code = await plugin_cmd.plugin_gc(reg, home, env, non_plugin_bundle_ids=lambda: set(), json_output=True, prune_dangling=True)

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"] == ["orphan@acme"]
    assert out["dangling"] == [{"id": "ghost@nowhere", "reason": "marketplace-not-added"}]
    assert out["prunedIntents"] == ["ghost@nowhere"]
    assert out["recoverable"] == []
    assert orphan_path.exists()  # #258：catalog 内相对源落点保留（账本/意图仍删）
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

    code = await plugin_cmd.plugin_gc(reg, home, env, non_plugin_bundle_ids=lambda: set(), confirm=_confirm, prune_dangling=True)

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

    code = await plugin_cmd.plugin_gc(reg, home, env, non_plugin_bundle_ids=lambda: set(), json_output=True)  # prune_dangling 缺省 False

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

    code = await plugin_cmd.plugin_gc(reg, home, env, non_plugin_bundle_ids=lambda: set(), json_output=True, prune_dangling=True)

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

    code = await plugin_cmd.plugin_gc(reg, home, env, non_plugin_bundle_ids=lambda: set(), confirm=_confirm, prune_dangling=True)

    assert code == 0
    assert orphan_path.exists()  # #258：catalog 共享树保留；孤儿账本已删（下行断言）
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


# ── #263 plugin source 非法：结构化错误 + 脱敏（无 traceback）─────────────────────
def _seed_bad_source_catalog(home: Path, *, source: object) -> None:
    """造一个 catalog，其 plugin entry 的 source 非法（可携带凭据）。"""
    catalog = marketplace_skill_dir(home, "acme")
    _write_json(
        catalog / ".tfrobot-plugin" / "marketplace.json",
        {
            "name": "acme",
            "owner": {"name": "X"},
            "metadata": {"pluginRoot": "./plugins"},
            "plugins": [{"name": "audit", "source": source}],
        },
    )
    known = load_known_marketplaces(home=home)
    known["marketplaces"]["acme"] = {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc"}
    save_known_marketplaces(known, home=home)


@pytest.mark.asyncio
async def test_install_invalid_source_json_error_is_redacted_and_rolls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """非法 source（含凭据）→ 结构化 JSON 错误（非 traceback），消息脱敏，且「失败 = 零变更」。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _seed_bad_source_catalog(home, source="key=https://alice:PW_CLI@example.com/r.git")

    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme", json_output=True)
    raw = capsys.readouterr().out
    assert code == 1
    assert "PW_CLI" not in _flat(raw) and "alice:" not in _flat(raw)
    payload = json.loads(raw)
    assert "https://example.com/r.git" in payload["message"]
    assert "source invalid" in payload["message"]
    # #278 负向锁：source 非法不附 remove/add 指引（remove/add 修不了上游非法配置，会误导）
    assert "marketplaceremove" not in _flat(payload["message"])
    assert "pluginenable" not in _flat(payload["message"])
    # 原子回滚：不留下安装意图
    assert "audit@acme" not in load_installed_plugins(home=home, env=env)["plugins"]


@pytest.mark.asyncio
async def test_install_invalid_source_human_error_is_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """人类可读路径同一口径：红字错误、无 traceback、无凭据。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _seed_bad_source_catalog(home, source={"source": "url", "url": "glpat-SECRET_TOKEN"})

    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)
    assert code == 1
    assert "glpat-SECRET_TOKEN" not in out
    assert "sourceinvalid" in out


@pytest.mark.asyncio
async def test_install_invalid_source_with_rich_markup_does_not_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#263 回归锁：source 文本里的 rich 标记（如 `[/red]`）不得让结构化错误路径抛 MarkupError。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _seed_bad_source_catalog(
        home, source={"source": "url", "url": "bogus", "note": "[/red][link=https://evil]x[/link]"}
    )

    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)
    assert code == 1
    assert "sourceinvalid" in out
    assert "[/red]" in out  # 作为字面文本渲染，而非被 rich 解析


@pytest.mark.asyncio
async def test_enable_missing_install_path_exit1_with_recovery_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#259：账本记录 installPath 缺失 → 退出码 1，JSON message 点名死路径 + 恢复指引（不再静默「启用成功」）。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(root), "mcpServers": []}]}},
        home=home,
    )
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"]})
    monkeypatch.setattr(_STAGE, _fake_stage())
    shutil.rmtree(root)  # 直接构造损坏态（不依赖卸载行为）

    code = await plugin_cmd.plugin_enable(reg, home, env, "audit@acme", json_output=True)

    assert code == plugin_cmd.EXIT_USER_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "error"  # 无新增机读码（架构师裁决，对齐 rust Precondition）
    assert str(root) in payload["message"]  # 点名死路径
    assert "enabledPlugins is unchanged" in payload["message"]
    assert "marketplace remove acme" in payload["message"] and "plugin install audit@acme" in payload["message"]


@pytest.mark.asyncio
async def test_enable_prior_true_failure_reports_retained_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#289：prior-true 再 enable 失败 → ``--json`` 明示 enabledStateRetained（人读消息同带 retained 后缀），
    且 `enabledPlugins` 精确保持 true（协议 §2.4 / Discussion#82 裁决 C）。"""
    monkeypatch.chdir(tmp_path)  # 诊断锚缺省 = runtime cwd；隔离仓库根的 .tfrobot（#289 审查 🟡7）
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(root), "mcpServers": []}]}},
        home=home,
    )
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}})
    monkeypatch.setattr(_STAGE, _fake_stage())
    shutil.rmtree(root)  # 预检失败（零写）

    code = await plugin_cmd.plugin_enable(reg, home, env, "audit@acme", json_output=True)

    assert code == plugin_cmd.EXIT_USER_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "error"  # 无新增 error_code（保留既有契约）
    assert "existing enabled state retained" in payload["message"]
    assert payload["enabledStateRetained"] is True
    assert payload["reason"] == "enable-failed-retained"
    assert json.loads(user_settings_path(env).read_text(encoding="utf-8"))["enabledPlugins"]["audit@acme"] is True


@pytest.mark.asyncio
async def test_plugin_enable_forwards_diagnostic_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#289 审查 🟡3：``plugin_enable`` 必须把注入的 provider 传给库层（锚点随 scope 记录传给 provider）。"""
    from a2c_smcp.computer.settings.scope import resolve_settings

    monkeypatch.chdir(tmp_path)
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(root), "mcpServers": []}]}},
        home=home,
    )
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}})
    monkeypatch.setattr(_STAGE, _fake_stage())
    shutil.rmtree(root)
    anchors: list[str | None] = []

    def _provider(anchor: str | None) -> Mapping[str, Any]:
        anchors.append(anchor)
        return resolve_settings(env=env, cwd=Path(anchor) if anchor else None).settings

    code = await plugin_cmd.plugin_enable(reg, home, env, "audit@acme", resolved_settings=_provider, json_output=True)

    assert code == plugin_cmd.EXIT_USER_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabledStateRetained"] is True
    assert anchors == [None], "user scope 的诊断锚 = None（由 provider 绑定 runtime 缺省）"


def test_resolved_settings_provider_of_uses_instance_axes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#289 审查 🟡3：provider 必须取**实例轴**（flag 层可见；None 锚 = 实例 cwd，不是进程 cwd）。"""
    from a2c_smcp.computer.cli.commands import resolved_settings_provider_of
    from a2c_smcp.computer.settings.scope import SettingsAxes

    monkeypatch.chdir(tmp_path)  # 进程 cwd = 空白根（无 .tfrobot）
    instance_cwd = tmp_path / "instance"
    instance_cwd.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    env = _env(tmp_path)
    flag = tmp_path / "flag-settings.json"
    _write_json(user_settings_path(env), {"enabledPlugins": {"audit@acme": True}})
    # flag 层（高于 project/local）独有 other@acme：证明 provider 取的是**实例轴**而非仅文件三层。
    _write_json(flag, {"enabledPlugins": {"other@acme": True}})
    _write_json(workdir_project_settings_path(instance_cwd), {"enabledPlugins": {"audit@acme": False}})
    _write_json(workdir_project_settings_path(other), {"enabledPlugins": {"audit@acme": True}})

    class _Comp:
        def _settings_axes(self) -> SettingsAxes:
            return SettingsAxes(env=env, cwd=instance_cwd, flag_settings_path=flag, policy_settings={})

    provider = resolved_settings_provider_of(_Comp())

    merged = provider(None)
    assert merged["enabledPlugins"]["other@acme"] is True, "flag 层（实例轴）必须可见"
    # None 锚 → 实例 cwd 的 project=false 压过 user=true；若错锚进程 cwd（无 project 层）会得到 user 的 true。
    assert merged["enabledPlugins"]["audit@acme"] is False
    # 显式锚 → 该路径的 project/local（#289 诊断锚 = 本次操作 path）。
    assert provider(str(other))["enabledPlugins"]["audit@acme"] is True


def test_resolved_settings_provider_of_applies_policy(tmp_path: Path) -> None:
    """#289 审查 🟡8：policy 层必须进 provider 视图（admin 强停时不得误报 retained）。"""
    from a2c_smcp.computer.cli.commands import resolved_settings_provider_of
    from a2c_smcp.computer.settings.scope import SettingsAxes

    env = _env(tmp_path)
    _write_json(user_settings_path(env), {"enabledPlugins": {"audit@acme": True}})

    class _Comp:
        def _settings_axes(self) -> SettingsAxes:
            return SettingsAxes(
                env=env, cwd=None, flag_settings_path=None,
                policy_settings={"enabledPlugins": {"audit@acme": False}},
            )

    merged = resolved_settings_provider_of(_Comp())(None)

    assert merged["enabledPlugins"]["audit@acme"] is False, "policy（最高层）强停必须压过 user=true"


def test_build_mcp_callbacks_remove_server_is_runtime_only() -> None:
    """#289 审查 🟡10：``build_mcp_callbacks.remove_server`` 必须接**运行期** unmount（durable 删除会毁用户声明）。"""
    from a2c_smcp.computer.cli.commands import build_mcp_callbacks

    class _Comp:
        mcp_manager = None
        declared_servers: dict[str, Any] = {}

        def __init__(self) -> None:
            self.unmounted: list[str] = []
            self.durable_removed: list[str] = []

        def resolve_mcp_declarations(self) -> Any:
            return SimpleNamespace(servers=self.declared_servers)

        async def aunmount_server_by_id(self, bundle_id: str) -> None:
            self.unmounted.append(bundle_id)

        async def aremove_server(self, bundle_id: str) -> None:
            self.durable_removed.append(bundle_id)  # durable 路径（写 mcp.json）——MUST NOT 被 enable 回滚使用

    comp = _Comp()
    cbs = build_mcp_callbacks(comp)

    asyncio.run(cbs.remove_server("b1"))

    assert comp.unmounted == ["b1"]
    assert comp.durable_removed == [], "enable 回滚 STOP 在运行期 unmount；durable 声明不动"


def test_one_shot_enable_reads_root_settings_flag_for_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#289 审查 🟡3：一次性 CLI 的根级 ``--settings`` 必须进 retained 判定（flag-only true → 报 retained）。

    直呼 Typer 命令函数（而非 CliRunner）：rich Console 绑定导入期 stdout，CliRunner 捕不到 JSON；
    函数体正是「根状态 → provider」的接线所在。
    """
    import typer

    from a2c_smcp.computer.cli import main as cli_main

    home, env = _home(tmp_path), _env(tmp_path)
    root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(root), "mcpServers": []}]}},
        home=home,
    )
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"]})  # user 层无 enabled 条目
    flag = tmp_path / "flag-settings.json"
    _write_json(flag, {"enabledPlugins": {"audit@acme": True}})  # 有效 enabled 只来自 flag 层
    monkeypatch.setattr(_STAGE, _fake_stage())
    monkeypatch.setenv("A2C_SKILL_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", env["XDG_CONFIG_HOME"])
    shutil.rmtree(root)

    ctx = SimpleNamespace(obj=cli_main._RootState(flag_path=flag))
    with pytest.raises(typer.Exit) as ei:
        cli_main._plugin_enable(ctx=ctx, plugin_id="audit@acme", json_output=True)

    assert ei.value.exit_code == 1
    lines = capsys.readouterr().out.splitlines()
    # 日志也落在 stdout；``console.print_json`` 是**多行** pretty JSON → 从首个独立的 ``{`` 起整体解析。
    start = next(i for i, line in enumerate(lines) if line.strip() == "{")
    payload = json.loads("\n".join(lines[start:]))
    assert payload["enabledStateRetained"] is True
    assert payload["reason"] == "enable-failed-retained"


@pytest.mark.asyncio
async def test_repl_enable_passes_instance_axis_provider_and_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#289 审查 🟡3：REPL dispatcher 必须透传 non_plugin_bundle_ids + 实例轴 provider（删掉即红）。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    captured: dict[str, Any] = {}

    async def _fake_enable(registry: Any, home: Any, env: Any, plugin_id: str, **kwargs: Any) -> int:
        captured.update(kwargs)
        captured["plugin_id"] = plugin_id
        return plugin_cmd.EXIT_OK

    monkeypatch.setattr(plugin_cmd, "plugin_enable", _fake_enable)
    comp = _ReplComp(_home(tmp_path), SkillRegistry(), env={"XDG_CONFIG_HOME": str(tmp_path / "cfg")}, cwd=tmp_path)
    from mcp import StdioServerParameters

    from a2c_smcp.computer.mcp_clients.model import StdioServerConfig

    comp.declared_servers = {
        "declared.mcp": SimpleNamespace(
            config=StdioServerConfig(name="declared.mcp", server_parameters=StdioServerParameters(command="node")),
        ),
    }

    await plugin_cmd.repl_dispatch(comp, ["plugin", "enable", "audit@acme"], session=None)

    assert captured["plugin_id"] == "audit@acme"
    # 两个回调同为 ``Callable[[], set[str]]``，仅断言 callable 无法区分「交叉接错」——按数据源分别断言。
    assert captured["non_plugin_bundle_ids"]() == {"declared_mcp"}, "must be the declaration surface (origin != plugin)"
    assert captured["existing_bundle_ids"]() == set(), "must be the runtime mounted set (no servers mounted)"
    provider = captured["resolved_settings"]
    assert callable(provider), "实例轴 provider 必须透传"
    assert isinstance(provider(None), Mapping)


@pytest.mark.asyncio
async def test_enable_multi_scope_any_dead_record_exits1_without_partial_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#259 裁决 ∀：CLI 逐 scope 循环在第一次调用即失败——健康 scope 也不得被启用（无部分成功）。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    healthy = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    dead = tmp_path / "dead-project-copy"
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                "audit@acme": [
                    {"scope": "user", "installPath": str(healthy), "mcpServers": []},
                    {"scope": "project", "installPath": str(dead), "projectPath": str(tmp_path / "proj"), "mcpServers": []},
                ]
            },
        },
        home=home,
    )
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"]})
    monkeypatch.setattr(_STAGE, _fake_stage())

    code = await plugin_cmd.plugin_enable(reg, home, env, "audit@acme", json_output=True)

    assert code == plugin_cmd.EXIT_USER_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert str(dead) in payload["message"]  # 点名死路径
    assert "enabledPlugins" not in json.loads(user_settings_path(env).read_text(encoding="utf-8"))  # 零部分启用


@pytest.mark.asyncio
async def test_install_staging_error_is_structured_not_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#263 三轮复审：`SkillStagingError` / `PluginManifestError` 也不再以 traceback 逃出 CLI。"""
    from a2c_smcp.computer.skills.staging import SkillStagingError

    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _seed_bad_source_catalog(home, source={"source": "url", "url": "https://example.com/r.git"})

    async def boom(*args: object, **kwargs: object) -> tuple[Path, str | None]:
        raise SkillStagingError("git clone failed for https://alice:PW_STAGE@example.com/r.git?token=Q")

    monkeypatch.setattr("a2c_smcp.computer.settings.installer.locate_plugin_root", boom)
    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)
    assert code == 1
    assert "pluginstagingfailed" in out
    assert "PW_STAGE" not in out
    assert "token=Q" not in out
    # #278 负向锁：独占 clone（git 源）失败**不得**给任何恢复指引。`marketplace remove` 会级联卸载该
    # marketplace 的全部插件并撤销 trust，却修不好 clone / 上游 subdir / 网络问题；`plugin uninstall` 同样是死路
    # ——安装失败不写账本，uninstall 无记录即 no-op（退出码 1、不删残留 clone），再 install 复用同一坏 clone。
    assert "marketplaceremove" not in out
    assert "pluginuninstall" not in out and "plugininstall" not in out and "pluginenable" not in out


# ── #278 catalog 子树被删：install 失败附恢复指引（不再只丢一句「root not found」）───────
@pytest.mark.asyncio
async def test_install_missing_catalog_subtree_exits1_with_recovery_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#278：相对源子树被 0.5.0 旧卸载删除 → 真实 `locate_plugin_root` 走「root not found」分支。

    退出码 1、无 traceback，并给出 remove→add→重装(+enable) 的恢复指引；「失败 = 零变更」不变。
    """
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    plugin_root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    shutil.rmtree(plugin_root)  # 只删子树，catalog 与清单仍在（#278 描述的损坏态；不 monkeypatch）

    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)

    assert code == plugin_cmd.EXIT_USER_ERROR
    assert "Traceback" not in out
    assert "pluginrootnotfoundafterresolve" in out  # 根因仍点名
    assert "Recovery:" in out
    assert "marketplaceremoveacme" in out  # 第 1 步：移除重 clone
    # 第 2/3 步断言**邻接串**：#278 复审 🟡4——共享指引的 own-clone 半句也含 "plugin install audit@acme"，
    # 只断言单句会被那一半满足，删掉 catalog 子句的重装步骤仍全绿。
    assert "'marketplaceadd<source>',then'plugininstallaudit@acme'" in out
    # 新尾段补齐共享指引没覆盖的三件事（复审 🟡1/🟡2）
    assert "upstreamrepository" in out  # 先确认上游确有该路径
    assert "--trust" in out  # 非交互 add 必带
    assert "cascades" in out  # remove 的级联代价
    assert "--keep-plugins" in out  # 禁令
    assert "pluginenableaudit@acme" in out  # install 不激活 ⇒ 恢复后须 enable
    # 原子回滚：**意图**（user settings 的 installedPlugins）被撤销。账本 installed_plugins.json 在物化成功前
    # 根本不会被写，拿它断言是恒真的空转（复审 🟡3）。
    assert "audit@acme" not in json.loads(user_settings_path(env).read_text(encoding="utf-8")).get("installedPlugins", [])


@pytest.mark.asyncio
async def test_install_missing_catalog_subtree_json_error_has_recovery_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#278 同场景 `--json`：**不新增机读码**（仍 `error`/`message` 两键），指引落在 message 内。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    plugin_root = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    shutil.rmtree(plugin_root)

    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme", json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == plugin_cmd.EXIT_USER_ERROR
    assert set(payload) == {"error", "message"}  # 无新检测码（#267 裁决）
    assert payload["error"] == "error"
    message = _flat(payload["message"])
    assert "pluginrootnotfoundafterresolve" in message
    assert "marketplaceremoveacme" in message
    # 邻接串（复审 🟡4）：own-clone 半句也含 "plugin install audit@acme"，单句断言锁不住 catalog 链的重装步骤
    assert "'marketplaceadd<source>',then'plugininstallaudit@acme'" in message
    assert "--trust" in message
    assert "pluginenableaudit@acme" in message


@pytest.mark.asyncio
async def test_install_git_source_root_missing_gets_no_remove_add_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#278 负向锁（复审 🟡3）：**git 源 + 根缺失文案**同样不给 remove/add。

    与「clone 失败」用例互补——那例靠文案前缀就能排除，单独删 `isinstance(LocalPluginSource)` 半段仍全绿；
    本例文案与相对源根缺失**逐字相同**，只由 source 类型区分。
    """
    from a2c_smcp.computer.skills.staging import SkillStagingError

    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _seed_bad_source_catalog(home, source={"source": "git-subdir", "url": "https://example.com/r.git", "path": "plugins/audit"})

    async def boom(*args: object, **kwargs: object) -> tuple[Path, str | None]:
        raise SkillStagingError("plugin root not found after resolve: plugins/audit")

    monkeypatch.setattr("a2c_smcp.computer.settings.installer.locate_plugin_root", boom)
    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)

    assert code == plugin_cmd.EXIT_USER_ERROR
    assert "pluginrootnotfoundafterresolve" in out  # 症状与目标场景逐字相同
    assert "marketplaceremove" not in out  # 但 source 是独占 clone ⇒ 不给 remove/add
    assert "pluginuninstall" not in out and "plugininstall" not in out


@pytest.mark.asyncio
async def test_install_local_escape_gets_no_remove_add_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#278 负向锁：相对源**越界声明**（根并非缺失）不附 remove/add——重 clone 拿到的还是同一份坏清单。"""
    from a2c_smcp.computer.skills.staging import SkillStagingError

    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])  # source 为相对路径的 Local 条目

    async def boom(*args: object, **kwargs: object) -> tuple[Path, str | None]:
        raise SkillStagingError("relative plugin source escapes marketplace clone: '../evil'")

    monkeypatch.setattr("a2c_smcp.computer.settings.installer.locate_plugin_root", boom)
    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)

    assert code == plugin_cmd.EXIT_USER_ERROR
    assert "pluginstagingfailed" in out  # 主错误仍完整
    assert "marketplaceremove" not in out
    assert "pluginuninstall" not in out and "plugininstall" not in out


@pytest.mark.asyncio
async def test_install_manifest_error_is_structured_not_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#263 四轮：manifest 层错误（materialize 内）同样归一为结构化错误。"""
    from a2c_smcp.computer.skills.manifest import PluginManifestError

    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _seed_bad_source_catalog(home, source={"source": "url", "url": "https://example.com/r.git"})

    async def boom(*args: object, **kwargs: object) -> tuple[Path, str | None]:
        raise PluginManifestError("invalid MCP server config at /x/y: boom")

    monkeypatch.setattr("a2c_smcp.computer.settings.installer.locate_plugin_root", boom)
    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)
    assert code == 1
    assert "pluginmanifestinvalid" in out
    # #278 负向锁：manifest 分支不附 remove/add 指引（载荷本身损坏，重 clone 同一份还是坏的）
    assert "marketplaceremove" not in out
    assert "pluginenable" not in out


@pytest.mark.asyncio
async def test_install_corrupt_catalog_manifest_is_structured_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#263 四轮：预检阶段 `read_marketplace_manifest` 损坏同样不得 traceback（此前在 try 之外）。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    catalog = marketplace_skill_dir(home, "acme")
    catalog.mkdir(parents=True)
    (catalog / ".tfrobot-plugin").mkdir(parents=True)
    (catalog / ".tfrobot-plugin" / "marketplace.json").write_text("{ broken", encoding="utf-8")
    known = load_known_marketplaces(home=home)
    known["marketplaces"]["acme"] = {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc"}
    save_known_marketplaces(known, home=home)

    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)
    assert code == 1
    assert "manifestinvalid" in out


@pytest.mark.asyncio
async def test_install_schemeless_query_source_token_is_masked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#263 七轮 🔴：installer 的 `SkillSourceError` 分支也要走 safe_exception_text（无 scheme query token）。"""
    home, env, reg = _home(tmp_path), _env(tmp_path), SkillRegistry()
    _seed_bad_source_catalog(home, source="x/r.git?token=SECRET_Q#f")

    code = await plugin_cmd.plugin_install(reg, home, env, "audit@acme")
    out = _flat(capsys.readouterr().out)
    assert code == 1
    assert "sourceinvalid" in out
    assert "token=SECRET_Q" not in out
    assert "#f" not in out


def test_plugin_error_keeps_machine_reason(capsys: pytest.CaptureFixture[str]) -> None:
    """
    #260 独立审查 🟡8 回归锁：机读键 ``reason`` 必须透出——plugin 的 ``_err`` 把 ``code`` 当退出码保留参数，
    改名拒绝若用 ``code`` 承载 reason 会在 enable 路径被过滤掉；三条路径（marketplace add/refresh、
    plugin enable）统一用 ``reason``。
    """
    from a2c_smcp.computer.cli.commands.plugin import _plugin_error
    from a2c_smcp.computer.settings.installer import PluginInstallError

    err = PluginInstallError(
        "manifest renamed upstream",
        error_code="validation",
        details={"reason": "manifest-name-changed", "name": "acme"},
    )
    from a2c_smcp.computer.cli.commands import plugin as plugin_cmd

    assert _plugin_error(err, json_output=True) == plugin_cmd.EXIT_USER_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation"
    assert payload["reason"] == "manifest-name-changed"
    assert payload["name"] == "acme"


# ── #293：run_governance_remount 携带声明校验错误（CLI 展示归 #294）─────────────
@pytest.mark.asyncio
async def test_run_governance_remount_carries_declaration_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：CLI 的显式 ``declared=`` 路径必须携带 settings 校验错误。

    判别力：改回 ``resolved_settings(...)``（只取 ``.settings``）会让 report 的该字段恒空——#294 的展示
    面就会永远拿到空列表，而单测仍绿（这正是要防的假绿形态）。

    **刻意不预置 bundled server**：本用例只断言 report 与 skills，预置 server 会让 ``amount_server`` 真起
    ``node`` 子进程（各用例 ~120s 连接超时），既不 hermetic 又拖垮 unit suite。
    """
    from a2c_smcp.computer.settings.schema import SettingsScope

    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[], skills=["lint"])
    _write_json(
        user_settings_path({"XDG_CONFIG_HOME": str(tmp_path / "cfg")}),
        {
            "installedPlugins": ["audit@acme"],
            "enabledPlugins": {"audit@acme": True},
            "extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "owner/repo"}}},
        },
    )

    async with Computer(name="t", skill_home=home) as comp:
        report = await plugin_cmd.run_governance_remount(comp)

    assert [(e.marketplace, e.scope) for e in report.marketplace_declaration_errors] == [("acme", SettingsScope.USER)]
    # 恢复行为不变（声明错误只是观测）
    assert report.restored_skills == ["audit:lint"]
    assert report.failed_marketplaces == []


@pytest.mark.asyncio
async def test_run_governance_remount_flag_declaration_error_scope_is_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293：``--settings`` flag 层的非法声明**照样上报**（``scope == FLAG``）。

    有意差异（设计文档 §6）：rust 把 Flag 从 report 字段过滤掉（其 CLI 启动边界另有一处打印 + 抑制开关）；
    python 的报告口径是「全部声明错误」（用户 2026-11-22 裁决），且已接受同一次启动出现多条同义提示。
    同 ``carries_declaration_errors``：不预置 bundled server（避免真起 ``node`` 子进程）。
    """
    from a2c_smcp.computer.settings.schema import SettingsScope

    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[], skills=["lint"])
    flag_file = tmp_path / "flag-settings.json"
    flag_file.write_text(
        json.dumps({"extraKnownMarketplaces": {"acme": {"source": {"type": "git", "url": "owner/repo"}}}}),
        encoding="utf-8",
    )

    async with Computer(name="t", skill_home=home) as comp:
        report = await plugin_cmd.run_governance_remount(comp, settings_flag_path=flag_file)

    assert [(e.marketplace, e.scope) for e in report.marketplace_declaration_errors] == [("acme", SettingsScope.FLAG)]


@pytest.mark.asyncio
async def test_run_governance_remount_holds_project_enabled_bundled_server_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #293（用户裁决）：project-scope 激活的 bundled server 经 CLI 重挂路径落 **PENDING**、不自动挂载。

    触发序列：仓库提交 ``.tfrobot/settings.json`` 且 ``enabledPlugins={"audit@acme": true}``（入 git、随仓库
    分发）→ ``run_mcp_approval`` 不覆盖 bundled 声明面（无批准框）→ 本路径必须靠 #159 Option B 的信任层
    拦住，否则仓库作者一句 enable 就能让 bundled server 免批准直连。
    判别力：不取 ``.per_scope_layers`` 时 ``recorded == [FIGMA_NAME]``（被挂载）、pending 为空，本用例转红。
    """
    from a2c_smcp.computer.settings.schema import SettingsScope

    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[FIGMA_NAME], skills=["lint"])
    _write_json(tmp_path / ".tfrobot" / "settings.json", {"enabledPlugins": {"audit@acme": True}})

    async with Computer(name="t", skill_home=home) as comp:
        recorded: list[str] = []

        async def fake_register(server: Any, **kwargs: Any) -> None:
            recorded.append(server.name)

        # 即使门失守也**不真起 node 子进程**（否则用例要等 120s 连接超时）
        monkeypatch.setattr(comp, "amount_server", fake_register)
        report = await plugin_cmd.run_governance_remount(comp)

    assert recorded == []
    assert [r.plugin_id for r in report.pending_bundled_servers] == ["audit@acme"]
    assert report.pending_bundled_servers[0].enabled_origin is SettingsScope.PROJECT


# ── #294：治理恢复三类信号的 CLI 展示 ────────────────────────────────────────
_DECL_SCOPE_ANCHOR = "settings.json["
_CONFLICT_ANCHOR = "declared source differs"
_DEGRADED_ANCHOR = "degraded during"
_RECOVERY_WARN_ANCHOR = "governance recovery:"


def _decl_error(scope: str = "user") -> Any:
    from a2c_smcp.computer.settings.recovery import MarketplaceDeclarationError
    from a2c_smcp.computer.settings.schema import SettingsScope

    return MarketplaceDeclarationError(
        scope=SettingsScope(scope),
        marketplace="acme",
        field="extraKnownMarketplaces.acme.source",
        reason="missing or non-object 'source'",
    )


def _conflict() -> Any:
    from a2c_smcp.computer.settings.recovery import MarketplaceSourceConflict

    return MarketplaceSourceConflict(
        marketplace="acme",
        declared_source_display="https://bob:t0ken@example.com/other.git",
        known_source_display="https://example.com/acme.git",
    )


def test_marketplace_declaration_error_line_pins_wording() -> None:
    """
    #294 声明错误行：``settings.json[<scope>]`` 锚点 + field/reason 原样（#293 已脱敏）。

    **scope 用非默认值**（``flag``）：python 有意不过滤 flag 层声明错误（设计 §6.1），把该分支也在
    CLI 行上钉住；否则把 ``error.scope.value`` 写死成 ``"user"`` 的变异无人能杀。
    """
    line = plugin_cmd.marketplace_declaration_error_line(_decl_error("flag"))
    assert line == (
        "⚠ settings.json[flag]: extraKnownMarketplaces.acme.source — missing or non-object 'source' "
        "(declaration ignored; existing registration unchanged)"
    )
    assert "https://" not in line  # 不带声明源 URL


def test_marketplace_source_conflict_line_is_name_only() -> None:
    """
    #294 冲突行**只展示脱敏市场名**（name-only，与 rust 对拍）。

    判别力：夹具的两侧 display 都**带凭据形态**——若实现改成打印 source，本用例立刻看到 `t0ken` / `https://`。
    """
    conflict = _conflict()
    assert "t0ken" in conflict.declared_source_display  # 正对照：夹具确实带凭据
    line = plugin_cmd.marketplace_source_conflict_line(conflict)
    assert "acme" in line and _CONFLICT_ANCHOR in line
    assert "t0ken" not in line
    assert "https://" not in line


def test_marketplace_degraded_line_keeps_existing_wording() -> None:
    """#294 降级行沿用 #287 起既有文案（回归锁：改写会打破用户已习惯的提示）。"""
    line = plugin_cmd.marketplace_degraded_line("acme")
    assert line == "⚠ marketplace 'acme' degraded during governance recovery (skills/servers not restored)"


def _report_with(*, decl: bool = False, conflict: bool = False, failed: bool = False) -> Any:
    from a2c_smcp.computer.settings.recovery import GovernanceRecoveryReport

    report = GovernanceRecoveryReport()
    if decl:
        report.marketplace_declaration_errors = [_decl_error()]
    if conflict:
        report.marketplace_source_conflicts = [_conflict()]
    if failed:
        report.failed_marketplaces = ["acme"]
    return report


@pytest.mark.asyncio
async def test_run_governance_remount_prints_three_categories_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#294：三类同时出现时按 **rust 同序**（降级 → 源冲突 → 声明错误）各打一行，锚点互不相同、不合并。"""
    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[], skills=["lint"])
    report = _report_with(decl=True, conflict=True, failed=True)

    async with Computer(name="t", skill_home=home) as comp:
        async def fake_reconcile(**kwargs: Any) -> Any:
            return report

        monkeypatch.setattr(comp, "reconcile_governance", fake_reconcile)
        returned = await plugin_cmd.run_governance_remount(comp)

    assert returned is report
    out = capsys.readouterr().out
    # 折行无关断言（复用本文件 `_flat()` 约定）：rich 会按终端宽度折行，长 token 甚至会被从中间折断，
    # 故**不得**依赖物理行/换行位置。用 `⚠` 出现次数计条数（续行不含该字符），锚点在去空白文本上比对。
    assert out.count("⚠") == 3, out
    flat = _flat(out)
    # 锚点自身也含空格 ⇒ 与 `_flat` 同归一后再比（否则 "degradedduring" 永远找不到）
    positions = [flat.find(_flat(anchor)) for anchor in (_DEGRADED_ANCHOR, _CONFLICT_ANCHOR, _DECL_SCOPE_ANCHOR)]
    assert all(pos >= 0 for pos in positions), positions  # 三类都在（各一次，由上面的 ⚠ 计数保证）
    assert positions == sorted(positions), positions  # 顺序 = rust：降级 → 源冲突 → 声明错误
    # name-only：CLI 行不得出现任何来源 URL
    assert "https://" not in out


@pytest.mark.asyncio
async def test_run_governance_remount_prints_one_line_per_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """
    #294 契约「**每条记录一行**」：同一类塞 2 条 ⇒ `⚠` 计数 2 且两条文案都在（不合并/不聚合计数）。

    判别力：若把循环改成只打首条、或把同类多条合并成一句计数，本用例转红。
    """
    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[], skills=["lint"])
    report = _report_with()
    from a2c_smcp.computer.settings.recovery import MarketplaceDeclarationError
    from a2c_smcp.computer.settings.schema import SettingsScope

    report.marketplace_declaration_errors = [
        MarketplaceDeclarationError(
            scope=SettingsScope.USER,
            marketplace=None,
            field="extraKnownMarketplaces.alpha.source",
            reason="missing or non-object 'source'",
        ),
        MarketplaceDeclarationError(
            scope=SettingsScope.LOCAL,
            marketplace=None,
            field="extraKnownMarketplaces.beta.source.url",
            reason="malformed git url",
        ),
    ]

    async with Computer(name="t", skill_home=home) as comp:
        async def fake_reconcile(**kwargs: Any) -> Any:
            return report

        monkeypatch.setattr(comp, "reconcile_governance", fake_reconcile)
        await plugin_cmd.run_governance_remount(comp)

    out = capsys.readouterr().out
    assert out.count("⚠") == 2, out
    flat = _flat(out)
    assert "settings.json[user]" in flat and "extraKnownMarketplaces.alpha.source" in flat
    assert "settings.json[local]" in flat and "extraKnownMarketplaces.beta.source.url" in flat


@pytest.mark.asyncio
async def test_run_governance_remount_silent_when_no_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#294 零回归：三类皆空时**不**新增任何输出（不得每启动刷空提示）。"""
    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[], skills=["lint"])

    async with Computer(name="t", skill_home=home) as comp:
        async def fake_reconcile(**kwargs: Any) -> Any:
            return _report_with()

        monkeypatch.setattr(comp, "reconcile_governance", fake_reconcile)
        await plugin_cmd.run_governance_remount(comp)

    out = capsys.readouterr().out
    for anchor in (_DECL_SCOPE_ANCHOR, _CONFLICT_ANCHOR, _DEGRADED_ANCHOR):
        assert anchor not in out
    # 更强：夹具无 bundled server ⇒ 本路径唯一的 console 输出就是这三类信号；三类皆空时必须**完全无输出**
    assert out.strip() == "", out


@pytest.mark.asyncio
async def test_run_governance_remount_escapes_markup_on_reachable_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """
    #294：`escape()` 是**承重的**——rich 会把 `[<...>]` 当样式标签吞掉，未转义时**文字会消失**。

    可达的注入点是**声明行**：`_validate_extra_marketplaces` 用原始 JSON 键拼 `field`，而
    `sanitize_marketplace_name` 保留 `[` / `]`（实测 `evil[/]name` 原样通过），故手编 settings 键即可注入。
    两条断言各钉一件事（去掉 `escape()` 都会转红，且**不是**靠崩溃而是靠文本保真）：
      ① `settings.json[user]` 里的 `[user]` 不会被当标签吃掉；
      ② `field` 里的 `[/]` 原样保留（未转义时渲染成 `evilname`）。

    另外两条行（冲突名 / 降级名）**当前生产路径不可达** `[`（分别经严格 kebab / `redact_ledger_key` 过滤），
    但 `run_governance_remount` 的契约是「消费任意 report」——显示层必须对 report 里的值稳健，故同样钉住；
    这属于纵深防御，注释里不谎称它们可达。
    """
    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[], skills=["lint"])
    report = _report_with()
    from a2c_smcp.computer.settings.recovery import MarketplaceDeclarationError, MarketplaceSourceConflict
    from a2c_smcp.computer.settings.schema import SettingsScope

    report.marketplace_declaration_errors = [
        MarketplaceDeclarationError(
            scope=SettingsScope.USER,
            marketplace=None,
            field="extraKnownMarketplaces.evil[/]name",
            reason="missing or non-object 'source'",
        ),
    ]
    report.marketplace_source_conflicts = [
        MarketplaceSourceConflict(
            marketplace="conflict[bold]mp",
            declared_source_display="https://example.com/other.git",
            known_source_display="https://example.com/acme.git",
        ),
    ]
    report.failed_marketplaces = ["degraded[/]mp"]

    async with Computer(name="t", skill_home=home) as comp:
        async def fake_reconcile(**kwargs: Any) -> Any:
            return report

        monkeypatch.setattr(comp, "reconcile_governance", fake_reconcile)
        await plugin_cmd.run_governance_remount(comp)  # 未转义时 rich 不会抛、但会吞字

    out = _flat(capsys.readouterr().out)  # rich 会把长 token 从中间折断 ⇒ 去空白后再比
    assert "settings.json[user]" in out  # ① 声明行：scope 文本没被当标签吃掉
    assert "evil[/]name" in out  # ② 声明行：field 原文逐字可见
    assert "conflict[bold]mp" in out  # ③ 冲突行（纵深防御：契约允任意 report）
    assert "degraded[/]mp" in out  # ④ 降级行（同上）


@pytest.mark.asyncio
async def test_run_governance_remount_real_settings_prints_signals_distinct_from_recovery_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """
    #294：真实落盘 settings 走完整链路，且 **CLI 行与 recovery WARN 可按文案区分**。

    夹具同时制造两类信号：
    - `badmp` 声明写成 `owner/repo` 简写 → settings 校验整条丢弃（#293 进 report）⇒ 声明错误行；
    - `acme` 声明合法但源与已注册源不同 ⇒ 冲突行（恢复仍按旧源）。

    渠道事实（实测）：项目 logger 的 `StreamHandler(sys.stdout)` 在 **import 时**绑定 stdout，而 rich
    `Console` 每次打印动态取 `sys.stdout` ⇒ `capsys` 能拿到 **CLI 打印**、**拿不到库日志**。故
    「capsys 里没有 `governance recovery:`」近乎恒真，**真正的判别力**是 `⚠` 计数（把 CLI 行改回
    `logger.warning` 时子弹行消失）；库 WARN 的存在性另由挂在 recovery logger 上的 `_Sink` 正对照钉住。
    """
    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[], skills=["lint"])
    _write_json(
        user_settings_path({"XDG_CONFIG_HOME": str(tmp_path / "cfg")}),
        {
            "installedPlugins": ["audit@acme"],
            "enabledPlugins": {"audit@acme": True},
            "extraKnownMarketplaces": {
                "acme": {"source": {"type": "git", "url": "https://example.com/other.git"}},
                "badmp": {"source": {"type": "git", "url": "owner/repo"}},
            },
        },
    )

    from a2c_smcp.computer.settings import recovery as recovery_mod

    warn_records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                warn_records.append(record.getMessage())

    sink = _Sink(level=logging.WARNING)
    a2c_logger = logging.getLogger("a2c_smcp")
    prev_level = a2c_logger.level
    a2c_logger.setLevel(logging.WARNING)
    try:
        async with Computer(name="t", skill_home=home) as comp:
            # sink 必须在 `__aenter__`（boot_up 也会跑一次 reconcile_governance 并打同样的汇总 WARN）
            # **之后**再挂——否则正对照被 boot 的 WARN 满足，无法证明「重挂路径的 WARN 仍在」。
            warn_records.clear()
            recovery_mod.logger.addHandler(sink)
            try:
                report = await plugin_cmd.run_governance_remount(comp)
            finally:
                recovery_mod.logger.removeHandler(sink)
    finally:
        a2c_logger.setLevel(prev_level)

    # 报告侧（接线确实把真实 settings 的错误/冲突带出来了）
    assert [e.marketplace for e in report.marketplace_declaration_errors] == ["badmp"]
    assert [c.marketplace for c in report.marketplace_source_conflicts] == ["acme"]

    out = capsys.readouterr().out
    assert out.count("⚠") == 2, out
    flat = _flat(out)
    assert _flat(_CONFLICT_ANCHOR) in flat  # 冲突行
    assert _flat(_DECL_SCOPE_ANCHOR) in flat  # 声明错误行
    assert flat.find(_flat(_CONFLICT_ANCHOR)) < flat.find(_flat(_DECL_SCOPE_ANCHOR))  # rust 同序
    assert "badmp" in flat  # 声明错误点名到具体市场名
    # name-only：CLI 行不得出现来源 URL（两侧 source 只在 recovery 的 DEBUG 里）
    assert "https://" not in out
    # 渠道事实：库日志不经 capsys（见 docstring）——这条是结构性的，保留作「CLI 行没被改回日志」的显式断言。
    assert _RECOVERY_WARN_ANCHOR not in out

    # 双向渠道区分之②（正对照）：库 WARN **仍然存在**（#294 不回收 WARN，用户接受同次启动多条同义提示），
    # 且它的文案里不含任何 CLI 锚点 —— 两套文案锚点互不重叠，任何人都能一眼分辨来源。
    assert warn_records, "recovery WARN 应仍在（本单不回收日志渠道）"
    assert any(_RECOVERY_WARN_ANCHOR in msg and "acme" in msg for msg in warn_records), warn_records
    for msg in warn_records:
        assert _DECL_SCOPE_ANCHOR not in msg
        assert _CONFLICT_ANCHOR not in msg
        assert _DEGRADED_ANCHOR not in msg


# ── #295 复审 🟡1：治理重挂成功行的 bundled server 名 markup 安全 ─────────────────
@pytest.mark.asyncio
async def test_run_governance_remount_register_line_escapes_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """marketplace 可分发 `mcp-servers/evil[/]x.json`（`name` 同为 `evil[/]x`，stem==name 校验通过）。

    `_register` 的成功行未 escape 时抛 `MarkupError`，被 `reconcile_governance` 的 per-server 兜底吞成
    WARN——server 已挂（start=False）却**永不批量启动**。本用例走真实回调：不抛 + 成功行原文可见。
    """
    from types import SimpleNamespace

    _isolate_env(tmp_path, monkeypatch)
    home = _home(tmp_path)
    _seed_recovery_home(home, servers=[], skills=["lint"])
    report = _report_with()

    async with Computer(name="t", skill_home=home) as comp:
        async def _noop_amount(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(comp, "amount_server", _noop_amount)

        async def fake_reconcile(**kwargs: Any) -> Any:
            cfg = SimpleNamespace(name="evil[/]x")
            record = SimpleNamespace(plugin="p", marketplace="m", plugin_id="evil[/]x@p@m", install_path=home)
            await kwargs["register_server"](cfg, record)  # 不得抛 MarkupError
            return report

        monkeypatch.setattr(comp, "reconcile_governance", fake_reconcile)
        await plugin_cmd.run_governance_remount(comp)

    flat = _flat(capsys.readouterr().out)
    assert "restoredbundledMCPserver'evil[/]x'" in flat
    assert "evil[/]x@p@m" in flat


@pytest.mark.asyncio
async def test_repl_unknown_subcommand_escapes_markup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#295（复审 🟡5）：plugin 分派器的未知子命令提示走共享转义点（此前只测过干净 `bogus`）。"""
    from types import SimpleNamespace

    comp = SimpleNamespace(skill_home=tmp_path, skill_registry=None, mcp_manager=None)

    await plugin_cmd.repl_dispatch(comp, ["plugin", "evil[/]x"], session=None)  # 不得抛

    out = "".join(capsys.readouterr().out.split())
    assert "unknownpluginsubcommand:evil[/]x" in out
