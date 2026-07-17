# -*- coding: utf-8 -*-
# filename: test_installer.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Plugin installer 单元测试（v0.2.1 #63）—— install/uninstall/enable/disable 编排（mock git staging + 注入 MCP）
Plugin installer unit tests: install/uninstall/enable/disable orchestration (stage mocked, MCP injected).

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §3.3/§4.3/§6.2/§7.2。
协议 / Protocol: runtime-contract §2.5（依赖模型）/ §4.9.1（账本 ``mcpServers`` + 回收判据），#153/D3+F1。

测试意图 / Test intentions（不依赖 git——用相对路径 plugin（locate_plugin_root 无 git）+ monkeypatch
``stage_marketplace_skills`` + 注入回调记录 MCP 调用；settings 写经 ``XDG_CONFIG_HOME`` 重定向隔离）:
- install（v0.3.0 #123 不激活）：happy 写 installedPlugins 意图 + 账本（不 stage、不挂 server）/ **依赖已满足
  不拒绝**（D3）/ 重装幂等 / 重装失败不丢既有意图与账本 / 未添加 mp / catalog 未 clone / 非法 id。
  install-only 目标行为与回滚细节另见 test_install_enable_separation.py。
- uninstall：**按判据**回收 + 注销 + 删记录 + 删 installPath 树 / --keep-servers / 未安装 no-op。
- disable：写 enabledPlugins=false + 按判据回收 + orphan skill（可复原）/ scope 路由 / 非法 scope。
- enable：写 true + 复活 skill + 挂载未满足的依赖 / 未安装抛 / **依赖已满足 → 复用不覆盖**（D3）。

.. note::
   原「外来同名硬抛 + 原子零变更」「自有同名（``owned``）幂等放行」两组用例已按 D3 改写——协议裁定 plugin 与
   MCP Server 是**依赖关系**，同 ``bundle_id`` 已有 = 依赖已满足，MUST NOT 拒绝，故「冲突」与「所有权」概念
   双双退役。回收判据五场景（用户自有永不连坐 / 共享依赖无泄漏）见 test_mcp_dependency_model.py。
"""

import json
import os
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.installer import (
    PluginInstallError,
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from a2c_smcp.computer.settings.scope import user_settings_path, workdir_project_settings_path
from a2c_smcp.computer.settings.store import load_installed_plugins, save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.utils.bundle_id import resolve_bundle_id

_SRC = {"type": "git", "url": "https://example.com/acme.git"}
_STAGE = "a2c_smcp.computer.settings.installer.stage_marketplace_skills"

# 夹具身份对（#153 + 协议 conformance §2.0）：display name 含 `.` → normalize_name 折成 `_`，故 **name ≠
# bundle_id**。原夹具用 "figma"（两者恰好同值）令「误用 name 当身份」的实现无法被鉴别——Epic #147 记录的同类
# 致盲已四例。/ Fixture identity pair kept forked so name-as-identity bugs cannot pass.
FIGMA_NAME, FIGMA_BID = "figma.mcp", "figma_mcp"


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch) -> None:
    """
    隔离 cwd / Isolate the process cwd。

    #153 起 uninstall/disable 的回收判据经 ``user_declared_bundle_ids`` → ``resolve_mcp_config`` 读
    **cwd 锚定**的 ``.tfrobot/mcp[.local].json``（project/local scope，#116）。不 chdir 则读进真实仓库的
    工作区配置——开发者本地一旦有该文件，断言即随环境漂移（#137 同款教训）。
    """
    monkeypatch.chdir(tmp_path)


# ── 辅助 / helpers ───────────────────────────────────────────────────────────
def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _env(tmp_path: Path) -> dict[str, str]:
    """重定向 XDG_CONFIG_HOME → tmp，隔离 user settings.json 写（防写真实 ~/.config）。"""
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _stdio(name: str, command: str = "node") -> dict:
    return {"name": name, "type": "stdio", "server_parameters": {"command": command}}


def _setup_catalog(home: Path, mp: str, plugin: str, *, servers: list[str], commit: str = "abc123") -> Path:
    """
    造真实 catalog 树（相对源 plugin，locate_plugin_root 无 git）+ marketplace.json + plugin mcp-servers/，
    并 seed known_marketplaces。返回 plugin 根 ``<catalog>/plugins/<plugin>``。
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
    save_known_marketplaces(
        {"version": 1, "marketplaces": {mp: {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": commit}}},
        home=home,
    )
    return plugin_root


def _seed_installed(home: Path, plugins: dict[str, list[dict]]) -> None:
    save_installed_plugins({"version": 1, "plugins": plugins}, home=home)


def _fake_stage(calls: list[dict], *, fail: bool = False, register_skill: bool = True):
    """替身 stage：记录入参；可选注册 ``<plugin>:lint`` SKILL（供 orphan/recover/rollback 断言）；可选抛错。"""

    async def _stage(name, source, registry, home, *, plugin_filter=None, auto_update=False, refresh=False, timeout=0.0, env=None):
        calls.append({"name": name, "plugin_filter": set(plugin_filter or ()), "refresh": refresh})
        if register_skill:
            for plugin in plugin_filter or set():
                registry.register_or_update(
                    {
                        "name": f"{plugin}:lint",
                        "source": f"{SOURCE_MARKETPLACE}:{name}",
                        "path": str(marketplace_skill_dir(home, name).resolve()),
                    },
                )
        if fail:
            raise RuntimeError("stage boom")
        return [f"{p}:lint" for p in plugin_filter or set()]

    return _stage


class _FakeMCP:
    """记录 MCP 注入回调调用；**身份一律 bundle_id**（#153：账本记 bundle_id ⇒ 预检/停摘链收 bundle_id）。"""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing: set[str] = set(existing or ())  # bundle_id 集
        self.registered: list[str] = []  # bundle_id
        self.removed: list[str] = []  # bundle_id
        self.fail_on: str | None = None  # 该 display name 的 server 注册时抛错

    def existing_bundle_ids(self) -> set[str]:
        return set(self.existing)

    async def register(self, cfg) -> None:
        if self.fail_on is not None and cfg.name == self.fail_on:
            raise RuntimeError(f"register {cfg.name} boom")
        self.registered.append(resolve_bundle_id(cfg))
        self.existing.add(resolve_bundle_id(cfg))

    async def remove(self, bundle_id: str) -> None:
        self.removed.append(bundle_id)
        self.existing.discard(bundle_id)


# ── install ──────────────────────────────────────────────────────────────────
async def test_install_happy_writes_intent_and_ledger(tmp_path: Path, monkeypatch) -> None:
    """happy：记录字段完整（scope/version/commitSha/installPath/bundled）+ 意图落 user；不激活（v0.3.0）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    calls: list[dict] = []
    monkeypatch.setattr(_STAGE, _fake_stage(calls))
    mcp = _FakeMCP()
    reg = SkillRegistry()

    record = await install_plugin("audit@acme", reg, home, env=env, existing_bundle_ids=mcp.existing_bundle_ids)

    assert record["scope"] == "user"
    assert record["version"] == "1.2.0"  # entry.version 胜
    assert record["commitSha"] == "abc123"
    assert record["mcpServers"] == [FIGMA_BID]
    assert record["installPath"].replace("\\", "/").endswith("marketplace/acme/plugins/audit")
    assert calls == []  # 不 stage（install ≠ activate）
    assert reg.resolve("audit:lint") is None
    ipf = load_installed_plugins(home=home)["plugins"]
    assert ipf["audit@acme"][0]["mcpServers"] == [FIGMA_BID]
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["audit@acme"]


async def test_install_dependency_satisfied_does_not_reject(tmp_path: Path, monkeypatch) -> None:
    """
    D3：同 bundle_id 已存在 = **依赖已满足** → 正常安装（协议 §2.5-1 MUST NOT 拒绝）。

    本例取代原 ``test_install_foreign_conflict_is_atomic``：「外来同名硬抛、原子失败、name 即身份、无
    rename/force 逃生口」那套裁决已被协议**正面推翻**（Discussion #23 / D3）——plugin 与 MCP Server 是
    **依赖关系**，本地已有即依赖满足，复用而非新建。判据五场景另见 test_mcp_dependency_model.py。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    mcp = _FakeMCP(existing={FIGMA_BID})  # 本地已有同 bundle_id
    reg = SkillRegistry()

    record = await install_plugin("audit@acme", reg, home, env=env, existing_bundle_ids=mcp.existing_bundle_ids)

    assert record["mcpServers"] == [FIGMA_BID]
    assert "audit@acme" in load_installed_plugins(home=home)["plugins"]  # 写了账本 = 真装上了
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["audit@acme"]  # 意图已写（非「原子回滚」）


async def test_install_reinstall_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """幂等重物化：已有账本记录 + 同 bundle_id 已挂 → 正常覆盖记录（原 ``owned`` 白名单概念随 D3 退役）。"""
    home = _home(tmp_path)
    install_path = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(install_path), "mcpServers": [FIGMA_BID]}]})
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    mcp = _FakeMCP(existing={FIGMA_BID})
    reg = SkillRegistry()

    record = await install_plugin("audit@acme", reg, home, env=_env(tmp_path), existing_bundle_ids=mcp.existing_bundle_ids)

    assert record["mcpServers"] == [FIGMA_BID]


async def test_install_ledger_only_without_injection(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    reg = SkillRegistry()

    record = await install_plugin("audit@acme", reg, home, env=_env(tmp_path))  # 无注入 → 跳过冲突预检

    assert record["mcpServers"] == [FIGMA_BID]  # 解析+记录（即使未注册 server）
    assert "audit@acme" in load_installed_plugins(home=home)["plugins"]


async def test_install_invalid_plugin_id_raises(tmp_path: Path) -> None:
    with pytest.raises(PluginInstallError, match="invalid plugin id"):
        await install_plugin("no-at-sign", SkillRegistry(), _home(tmp_path))


async def test_install_unknown_marketplace_raises(tmp_path: Path) -> None:
    with pytest.raises(PluginInstallError, match="not added"):
        await install_plugin("audit@acme", SkillRegistry(), _home(tmp_path))


async def test_install_catalog_not_cloned_raises(tmp_path: Path) -> None:
    home = _home(tmp_path)
    save_known_marketplaces({"version": 1, "marketplaces": {"acme": {"source": _SRC, "installLocation": "x"}}}, home=home)
    with pytest.raises(PluginInstallError, match="catalog not cloned"):
        await install_plugin("audit@acme", SkillRegistry(), home)


async def test_install_reinstall_failure_preserves_existing(tmp_path: Path, monkeypatch) -> None:
    """重装失败原子性：物化中途失败时，既有安装意图 / 账本记录 / 活跃 skill 全不丢（仅撤销本次新增）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    mcp = _FakeMCP()
    reg = SkillRegistry()
    await _install(home, tmp_path, monkeypatch, mcp, reg, servers=[FIGMA_NAME])  # 首装+enable：figma + audit:lint 活跃
    assert reg.resolve("audit:lint") is not None and mcp.registered == [FIGMA_BID]

    # 重装：plugin 新增一个畸形 bundled JSON（zeta）→ 物化失败；意图/账本/live 态须保持首装状态
    plugin_root = marketplace_skill_dir(home, "acme") / "plugins" / "audit"
    (plugin_root / "mcp-servers" / "zeta.json").write_text("{not json", encoding="utf-8")
    mcp.registered.clear()
    mcp.removed.clear()

    with pytest.raises(Exception, match="zeta"):
        await install_plugin("audit@acme", reg, home, env=env, existing_bundle_ids=mcp.existing_bundle_ids)

    assert reg.resolve("audit:lint") is not None  # 既有 skill 未被误注销
    assert mcp.removed == []  # 自有 figma 未被误摘
    assert FIGMA_BID in mcp.existing  # figma 仍挂在 manager
    rec = load_installed_plugins(home=home)["plugins"]["audit@acme"][0]
    assert rec["mcpServers"] == [FIGMA_BID]  # 账本仍为首装记录（本次失败未改写）
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["audit@acme"]  # 重装失败不误删既有意图（was_present → 不回滚）


# ── uninstall ─────────────────────────────────────────────────────────────────
async def _install(home: Path, tmp_path: Path, monkeypatch, mcp: _FakeMCP, reg: SkillRegistry, *, servers: list[str]) -> Path:
    """安装 + 启用一个 plugin 作为 uninstall/disable/enable 的活跃前置（v0.3.0 install 不激活，须显式 enable）。"""
    install_path = _setup_catalog(home, "acme", "audit", servers=servers)
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    env = _env(tmp_path)
    await install_plugin("audit@acme", reg, home, env=env, existing_bundle_ids=mcp.existing_bundle_ids)
    await enable_plugin(
        "audit@acme", reg, home, env=env,
        existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register, remove_server=mcp.remove,
    )
    return install_path


async def test_uninstall_default_cascades(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    env = _env(tmp_path)
    mcp = _FakeMCP()
    reg = SkillRegistry()
    install_path = await _install(home, tmp_path, monkeypatch, mcp, reg, servers=[FIGMA_NAME])
    assert install_path.exists()
    mcp.removed.clear()

    ok = await uninstall_plugin("audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=mcp.remove)

    assert ok is True
    assert mcp.removed == [FIGMA_BID]  # 级联 stop+remove
    assert reg.resolve("audit:lint") is None  # skills 注销
    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"]  # 记录删除
    assert not install_path.exists()  # installPath 树清除
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["installedPlugins"] == []  # v0.3.0：安装意图删除（回 available）
    assert "audit@acme" not in user.get("enabledPlugins", {})  # enabledPlugins 条目清除


async def test_uninstall_keep_servers_skips_teardown(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    env = _env(tmp_path)
    mcp = _FakeMCP()
    reg = SkillRegistry()
    await _install(home, tmp_path, monkeypatch, mcp, reg, servers=[FIGMA_NAME])
    mcp.removed.clear()

    ok = await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, keep_servers=True, remove_server=mcp.remove,
    )

    assert ok is True
    assert mcp.removed == []  # 保留 server
    assert reg.resolve("audit:lint") is None
    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"]


async def test_uninstall_not_installed_is_noop(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert await uninstall_plugin("ghost@acme", SkillRegistry(), _home(tmp_path), non_plugin_bundle_ids=lambda: set(), env=env) is False
    assert not user_settings_path(env).exists()  # 真 no-op：零写盘（迁移不被 no-op 路径触发，审查 N1）


# ── disable ───────────────────────────────────────────────────────────────────
async def test_disable_writes_flag_detaches_servers_orphans_skills(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    env = _env(tmp_path)
    mcp = _FakeMCP()
    reg = SkillRegistry()
    await _install(home, tmp_path, monkeypatch, mcp, reg, servers=[FIGMA_NAME])
    mcp.removed.clear()

    await disable_plugin("audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=mcp.remove)

    settings = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["audit@acme"] is False
    assert mcp.removed == [FIGMA_BID]  # 停+摘 bundled server
    assert reg.is_orphan("audit:lint") is True  # 隐藏（孤儿）
    assert reg.resolve("audit:lint") is None  # 活跃集排除
    assert "audit:lint" in reg  # 物化层保留（仍注册、可复原）


async def test_disable_project_scope_writes_workdir_settings(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    env = _env(tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _setup_catalog(home, "acme", "audit", servers=[])
    audit_path = str(marketplace_skill_dir(home, "acme") / "plugins" / "audit")
    _seed_installed(home, {"audit@acme": [{"scope": "project", "installPath": audit_path}]})
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    await disable_plugin(
        "audit@acme", SkillRegistry(), home, scope="project", project_path=str(workdir), non_plugin_bundle_ids=lambda: set(), env=env,
    )

    settings = json.loads(workdir_project_settings_path(workdir).read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["audit@acme"] is False


async def test_disable_project_scope_requires_project_path(tmp_path: Path) -> None:
    with pytest.raises(PluginInstallError, match="project_path"):
        await disable_plugin(
            "audit@acme", SkillRegistry(), _home(tmp_path), non_plugin_bundle_ids=lambda: set(), scope="project", env=_env(tmp_path),
        )


async def test_disable_managed_scope_rejected(tmp_path: Path) -> None:
    with pytest.raises(PluginInstallError, match="writable"):
        await disable_plugin(
            "audit@acme", SkillRegistry(), _home(tmp_path), non_plugin_bundle_ids=lambda: set(), scope="managed", env=_env(tmp_path),
        )


# ── enable ────────────────────────────────────────────────────────────────────
async def test_enable_recovers_skills_and_remounts_servers(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    env = _env(tmp_path)
    mcp = _FakeMCP()
    reg = SkillRegistry()
    await _install(home, tmp_path, monkeypatch, mcp, reg, servers=[FIGMA_NAME])
    await disable_plugin("audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=mcp.remove)
    assert reg.is_orphan("audit:lint")
    mcp.registered.clear()

    await enable_plugin(
        "audit@acme", reg, home, env=env,
        existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register,
    )

    settings = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["audit@acme"] is True
    assert reg.resolve("audit:lint") is not None  # 孤儿复活
    assert mcp.registered == [FIGMA_BID]  # server 重挂


async def test_enable_not_installed_raises(tmp_path: Path) -> None:
    with pytest.raises(PluginInstallError, match="not installed"):
        await enable_plugin("ghost@acme", SkillRegistry(), _home(tmp_path), env=_env(tmp_path))


async def test_enable_dependency_satisfied_reuses_and_succeeds(tmp_path: Path, monkeypatch) -> None:
    """
    D3：enable 时同 bundle_id 已在活跃集 → **复用既有实例、skip register**，enable 照常成功（§2.5-1）。

    本例取代原 ``test_enable_foreign_conflict_leaves_settings_untouched``（「外来同名 → 硬抛、settings 原子未写」）
    ——该语义已被协议推翻。关键断言是 **不覆盖**：既有 server 是用户的或他 plugin 的，覆盖它即毁其配置。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    # 夹具须完整（含 known_marketplaces）：D3 前本例硬抛在冲突门、根本走不到 stage 阶段；现在 enable 会一路
    # 跑完 re-stage，故需要真 marketplace 记录。
    install_path = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(install_path), "mcpServers": [FIGMA_BID]}]})
    monkeypatch.setattr(_STAGE, _fake_stage([]))
    mcp = _FakeMCP(existing={FIGMA_BID})  # 依赖已被既有实例满足

    await enable_plugin(
        "audit@acme", SkillRegistry(), home, env=env,
        existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register,
    )

    assert mcp.registered == []  # 复用既有实例，MUST NOT 覆盖
    settings = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["audit@acme"] is True  # enable 成功（不再因「同名」失败）


async def test_enable_register_without_existing_names_raises(tmp_path: Path) -> None:
    """护栏：enable 给了 register_server 但缺 existing_bundle_ids → 抛（先于读记录/写 settings）。"""
    home = _home(tmp_path)
    mcp = _FakeMCP()
    with pytest.raises(PluginInstallError, match="existing_bundle_ids is required"):
        await enable_plugin("audit@acme", SkillRegistry(), home, env=_env(tmp_path), register_server=mcp.register)
