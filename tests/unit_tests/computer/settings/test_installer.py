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
- uninstall：**按判据**回收 + 注销（**仅账本记录全部移除时**，#262）+ 删记录 + 删独占外部 clone（共享 catalog 保留）/
  --keep-servers / 未安装 no-op。
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


def _ext_plugin_root(home: Path, mp: str, plugin: str) -> Path:
    """plugin 独占外部 clone 落点 ``<home>/marketplace/.plugins/<mp>/<plugin>``（#258 唯一可删归属）。"""
    return home / SOURCE_MARKETPLACE / ".plugins" / mp / plugin


def _seed_registry(home: Path, mp: str, plugin: str) -> SkillRegistry:
    """预置**非空** registry（#262 验收第 3 条）：活跃 ``<plugin>:lint`` + 孤儿 ``<plugin>:old``。

    scoped uninstall 的技能拆除断言必须建立在非空 registry 上——空 registry 下 ``unregister`` 天然
    no-op，「多 scope 卸载误删技能」的回归会完全失明（#262 验收明令禁止空 registry + ``_fake_stage``）。
    ``register`` 失败是「记 ERROR + 返回 False」**不抛**（§1.5），故必须断言返回值——否则 seed 静默失败时
    ``len(reg) == 0`` 型断言会永真通过，退化成新的空 registry 失明。
    """
    reg = SkillRegistry()
    path = str(marketplace_skill_dir(home, mp).resolve())

    def _ref(name: str) -> dict:
        return {"name": name, "source": f"{SOURCE_MARKETPLACE}:{mp}", "path": path, "description": "d"}

    assert reg.register(_ref(f"{plugin}:lint")), "seed 失败：活跃技能未入册"
    assert reg.register(_ref(f"{plugin}:old")), "seed 失败：孤儿技能未入册"
    reg.mark_orphan(f"{plugin}:old")
    return reg


def _setup_catalog_multi(home: Path, mp: str, entries: list[tuple[str, str]], *, commit: str = "abc123") -> dict[str, Path]:
    """多 plugin catalog：``entries = (plugin, source)``（相对路径，裸名经 ``pluginRoot`` 展开）；返回各 plugin 根。"""
    catalog = marketplace_skill_dir(home, mp)
    manifest = {
        "name": mp,
        "owner": {"name": "X"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": p, "source": s, "version": "1.2.0"} for p, s in entries],
    }
    _write_json(catalog / ".tfrobot-plugin" / "marketplace.json", manifest)
    roots: dict[str, Path] = {}
    for plugin, source in entries:
        rel = source[2:] if source.startswith("./") else f"plugins/{source}"
        root = catalog / rel
        skill_dir = root / "skills" / f"{plugin}-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {plugin}-skill\ndescription: d\n---\n", encoding="utf-8")
        roots[plugin] = root
    save_known_marketplaces(
        {"version": 1, "marketplaces": {mp: {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": commit}}},
        home=home,
    )
    return roots


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
    assert install_path.exists()  # #258：共享 catalog 内相对源落点保留（marketplace 级缓存）
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


# ── #258：相对源卸载保留共享 catalog / 多 scope / 外部 clone 归属 ─────────────
async def test_uninstall_relative_source_keeps_parent_child_and_same_source_258(tmp_path: Path, monkeypatch) -> None:
    """#258：卸载 alpha 不得删除父子（beta）与同源兄弟（gamma）文件；卸载后可重装成功。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    roots = _setup_catalog_multi(home, "acme", [("alpha", "./plugin"), ("beta", "./plugin/nested"), ("gamma", "./plugin")])
    assert roots["alpha"] == roots["gamma"], "同源 → 同一 installPath"
    assert roots["beta"].is_relative_to(roots["alpha"]), "beta 位于 alpha 子树"
    _seed_installed(home, {f"{p}@acme": [{"scope": "user", "installPath": str(r)}] for p, r in roots.items()})
    monkeypatch.setattr(_STAGE, _fake_stage([]))

    assert await uninstall_plugin("alpha@acme", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env) is True

    assert roots["alpha"].exists() and roots["beta"].exists() and roots["gamma"].exists()
    ipf = load_installed_plugins(home=home)["plugins"]
    assert "alpha@acme" not in ipf and "beta@acme" in ipf and "gamma@acme" in ipf

    record = await install_plugin("alpha@acme", SkillRegistry(), home, env=env, existing_bundle_ids=lambda: set())
    assert Path(record["installPath"]).resolve() == roots["alpha"].resolve(), "卸载后可重装且落点不变"


async def test_uninstall_disjoint_relative_sources_control_258(tmp_path: Path) -> None:
    """#258 对照：installPath 不相交的 plugin 卸载互不影响（两者均保留，归 marketplace 级清理）。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    roots = _setup_catalog_multi(home, "acme", [("alpha", "./plugins/alpha"), ("beta", "./plugins/beta")])
    _seed_installed(home, {f"{p}@acme": [{"scope": "user", "installPath": str(r)}] for p, r in roots.items()})

    assert await uninstall_plugin("alpha@acme", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env) is True

    assert roots["alpha"].exists() and roots["beta"].exists()
    assert "beta@acme" in load_installed_plugins(home=home)["plugins"]


async def test_uninstall_deletes_external_clone_only_258(tmp_path: Path) -> None:
    """#258：url/github 类独占外部 clone 仍删除；同 marketplace 的 catalog 树保留。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    (ext / "pkg").mkdir(parents=True)
    catalog = marketplace_skill_dir(home, "acme") / "plugins" / "audit"
    catalog.mkdir(parents=True)
    (catalog / "keep.txt").write_text("x", encoding="utf-8")
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(ext)}]})

    assert await uninstall_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env) is True

    assert not ext.exists(), "独占外部 clone 删除"
    assert catalog.exists() and (catalog / "keep.txt").exists(), "catalog 共享树保留"


async def test_uninstall_git_subdir_deletes_whole_external_clone_258(tmp_path: Path) -> None:
    """#258：``git-subdir`` 的 installPath 是外部 clone 的子目录 → 删除整个 clone 根。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    sub = ext / "pkg" / "sub"
    sub.mkdir(parents=True)
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(sub)}]})

    assert await uninstall_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env) is True

    assert not ext.exists(), "subdir installPath → 删整个独占 clone 根"


async def test_uninstall_scoped_keeps_external_clone_until_last_record_258(tmp_path: Path) -> None:
    """#258：指定 scope 卸载且其余 scope 记录仍在 → 外部 clone 保留；最后一条记录卸载时才删。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext)},
                {"scope": "project", "projectPath": str(workdir), "installPath": str(ext)},
            ]
        },
    )

    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="project", keep_servers=True,
    ) is True
    assert ext.exists(), "其余 scope 仍依赖同一 clone → 保留"
    assert [r["scope"] for r in load_installed_plugins(home=home)["plugins"]["audit@acme"]] == ["user"]
    # #262：retained 记录仍在 → 技能（含孤儿）原样保留，不得被无条件注销
    assert reg.resolve("audit:lint") is not None
    assert reg.is_orphan("audit:old")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True
    assert not ext.exists(), "最后一条记录卸载 → 删除独占 clone"
    assert len(reg) == 0, "全部 scope 移除 → 技能（含孤儿）一并注销"


async def test_uninstall_scoped_source_change_deletes_unreferenced_external_clone_258(tmp_path: Path) -> None:
    """#258 🟡1：跨 scope 源类型变化（user=Git 源 / project=相对源）→ 先卸 user 时即删不再被引用的 ext clone。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    catalog_root = marketplace_skill_dir(home, "acme") / "plugin"
    catalog_root.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext)},
                {"scope": "project", "projectPath": str(workdir), "installPath": str(catalog_root)},
            ]
        },
    )

    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True
    assert not ext.exists(), "retained（project）在 catalog 内、不引用 ext clone → 先卸 user 即删，不留泄漏"
    remaining = load_installed_plugins(home=home)["plugins"]["audit@acme"]
    assert [r["scope"] for r in remaining] == ["project"]
    assert reg.resolve("audit:lint") is not None, "#262：retained 记录仍在 → 技能保留"

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="project", keep_servers=True,
    ) is True
    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"]
    assert len(reg) == 0, "最后一份记录移除 → 技能（含孤儿）一并注销"


@pytest.mark.parametrize("bad_install_path", ["/tmp/bad\u0000path", "relative/path"])
async def test_uninstall_scoped_unknown_retained_path_treated_as_unreferenced_258(
    tmp_path: Path, bad_install_path: str,
) -> None:
    """#258 架构师审查 🟡4：与 rust 终版对齐——retained 记录 installPath 损坏/相对 → 视为不引用，clone 删除。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext)},
                {"scope": "project", "projectPath": str(workdir), "installPath": bad_install_path},
            ]
        },
    )

    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True

    assert not ext.exists(), "无法识别的 retained 路径不算引用（与 rust 终版一致）"
    assert [r["scope"] for r in load_installed_plugins(home=home)["plugins"]["audit@acme"]] == ["project"]
    # #262：技能保留判据 = 账本记录存在性（与文件归属谓词**无关**）——installPath 损坏不改变「其余 scope 仍装」
    assert reg.resolve("audit:lint") is not None
    assert reg.is_orphan("audit:old")


@pytest.mark.parametrize("record_shape", ["missing", "empty", "non-str"])
async def test_uninstall_scoped_missing_retained_install_path_treated_as_unreferenced_258(
    tmp_path: Path, record_shape: str,
) -> None:
    """#258 架构师审查 🟡2：retained 记录缺 / 空 / 非 str installPath → 与损坏路径同口径，不阻塞删除。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project_record: dict = {"scope": "project", "projectPath": str(workdir)}
    if record_shape == "empty":
        project_record["installPath"] = ""
    elif record_shape == "non-str":
        project_record["installPath"] = None
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(ext)}, project_record]})

    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True

    assert not ext.exists()
    assert [r["scope"] for r in load_installed_plugins(home=home)["plugins"]["audit@acme"]] == ["project"]
    # #262：同上——installPath 缺失/畸形不影响技能保留（判据只看记录存在性）
    assert reg.resolve("audit:lint") is not None
    assert reg.is_orphan("audit:old")


# ── #262：多 scope 卸载后的 skills 拆除语义（裁决 A = 保留）────────────────────
async def test_uninstall_scoped_keeps_skills_until_last_record_removed_262(tmp_path: Path) -> None:
    """#262 裁决 A：有 retained 记录时 registry 不动（active + 孤儿）、server 不回收；全部记录移除才清理。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext), "mcpServers": [FIGMA_BID]},
                {"scope": "project", "projectPath": str(workdir), "installPath": str(ext), "mcpServers": [FIGMA_BID]},
            ]
        },
    )
    reg = _seed_registry(home, "acme", "audit")
    mcp = _FakeMCP()  # #275：uninstall 不接 ``existing_bundle_ids``，此处的 ``existing=`` 曾是死构造实参

    # 只卸 project：其余 scope（user）仍装 → 技能（含孤儿）原样、server 不回收
    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="project", remove_server=mcp.remove,
    ) is True
    assert reg.resolve("audit:lint") is not None, "retained 记录仍在 → 活跃技能保留"
    assert reg.is_orphan("audit:old"), "retained 记录仍在 → 孤儿原样保留（既不注销也不复活）"
    assert mcp.removed == [], "retained 记录仍声明依赖 → server 按 retained 判据保留"
    assert [r["scope"] for r in load_installed_plugins(home=home)["plugins"]["audit@acme"]] == ["user"]

    # 卸最后一份记录：技能（含孤儿）完整注销 + server 按 §4.9.1-2 回收
    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", remove_server=mcp.remove,
    ) is True
    assert len(reg) == 0, "全部 scope 移除 → active + 孤儿一并注销（#257 闭环）"
    assert mcp.removed == [FIGMA_BID], "最后一个依赖者卸载 → server 回收"


async def test_uninstall_specified_scope_clears_skills_when_last_record_262(tmp_path: Path) -> None:
    """#262 判据是「账本记录全部移除」而**非**「未指定 scope」——单 scope 账本指定该 scope 卸载也要清技能。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(ext)}]})
    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True

    assert len(reg) == 0


async def test_uninstall_full_clears_orphans_nonempty_registry_262(tmp_path: Path) -> None:
    """#262 回归锁（#257 闭环不因保留语义失效）：全量卸载（``scope=None``）连孤儿一并注销。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(ext)}]})
    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin("audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env) is True

    assert len(reg) == 0


# ── #275：legacy 账本 scoped 卸载残留 + uninstall 失败序 ─────────────────────
@pytest.mark.parametrize(
    ("retained_extra", "expect_purged"),
    [
        pytest.param({}, False, id="live-record"),
        pytest.param({"bundledMcpServers": [FIGMA_NAME]}, True, id="legacy-record"),
    ],
)
async def test_uninstall_scoped_legacy_record_is_not_retained_275(
    tmp_path: Path, retained_extra: dict, expect_purged: bool,
) -> None:
    """
    #275：「存活记录」= 非本次目标 ∧ **非 legacy**（协议 §4.9.1-4）。

    旧格式记录在写盘时被 :func:`~a2c_smcp.computer.settings.store._drop_legacy_mcp_dep_records` 整条 sweep，
    故它**不得**被当成「其余 scope 仍安装」的存活证据——否则账本/意图/enabled 三面已清空，而同进程 registry
    仍留技能（可硬挡换源 enable）、独占外部 clone 无人引用（uninstall 已 no-op、gc 只遍历账本 pid ⇒ 磁盘泄漏）。

    ``live-record`` 是同一场景的**正对照**：正常记录存活 ⇒ 技能 / clone 原样保留（#262 裁决 A 不变）。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext)},
                {"scope": "project", "projectPath": str(workdir), "installPath": str(ext), **retained_extra},
            ]
        },
    )
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}})
    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True

    settings = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    if expect_purged:
        assert "audit@acme" not in load_installed_plugins(home=home)["plugins"], "legacy 记录被 sweep ⇒ 账本终态为空"
        assert len(reg) == 0, "无存活记录 ⇒ 活跃 + 孤儿技能一并注销"
        assert not ext.exists(), "无存活记录 ⇒ 独占 clone 回收（不得留持久磁盘泄漏）"
        assert settings.get("installedPlugins") == []
        assert "audit@acme" not in settings.get("enabledPlugins", {})
    else:
        assert [r["scope"] for r in load_installed_plugins(home=home)["plugins"]["audit@acme"]] == ["project"]
        assert reg.resolve("audit:lint") is not None, "存活记录仍在 ⇒ 活跃技能保留"
        assert reg.is_orphan("audit:old"), "存活记录仍在 ⇒ 孤儿原样保留"
        assert ext.exists(), "存活记录仍引用该 clone ⇒ 保留"


async def test_uninstall_remove_server_failure_keeps_skills_and_ledger_275(tmp_path: Path) -> None:
    """
    #275 失败序：skills 注销后置到 ``remove_server`` **之后**、账本写仍居末位（§4.9.1-3 / §5.5）。

    停摘失败（``McpStartGateClosed`` / 状态机 / 传输异常）时不得留下「账本说已安装、技能已丢」半态：账本、意图、
    技能一律原样保留，重试可自愈——已删的 clone 在重试时按「缺失即视为已删除」收敛，不阻塞。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(ext), "mcpServers": [FIGMA_BID]}]})
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}})
    reg = _seed_registry(home, "acme", "audit")

    async def _boom(_bundle_id: str) -> None:
        raise RuntimeError("mcp teardown failed")

    with pytest.raises(RuntimeError, match="mcp teardown failed"):
        await uninstall_plugin(
            "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=_boom,
        )

    assert reg.resolve("audit:lint") is not None, "停摘失败不得先丢活跃技能（否则账本在、技能丢）"
    assert reg.is_orphan("audit:old"), "停摘失败不得先丢孤儿"
    assert load_installed_plugins(home=home, drop_legacy=False)["plugins"]["audit@acme"], "账本记录保留"
    settings = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert settings.get("installedPlugins") == ["audit@acme"], "安装意图保留"
    assert settings.get("enabledPlugins", {}).get("audit@acme") is True, "enabled 条目保留（三面均未动）"

    # 重试收敛：clone 已在失败那轮删除（缺失删除视为成功），不得阻塞
    mcp = _FakeMCP()
    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=mcp.remove,
    ) is True
    assert mcp.removed == [FIGMA_BID]
    assert len(reg) == 0
    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"]


@pytest.mark.parametrize(
    ("targeted_declares", "retained_extra", "expect_reclaimed"),
    [
        pytest.param(True, {"mcpServers": [FIGMA_BID]}, False, id="live-record"),
        pytest.param(
            True, {"mcpServers": [FIGMA_BID], "bundledMcpServers": [FIGMA_NAME]}, True, id="legacy-mixed-record",
        ),
        pytest.param(
            False,
            {"mcpServers": [FIGMA_BID], "bundledMcpServers": [FIGMA_NAME]},
            True,
            id="legacy-sole-declarer",
        ),
    ],
)
async def test_uninstall_scoped_legacy_record_does_not_block_server_reclaim_275(
    tmp_path: Path, targeted_declares: bool, retained_extra: dict, expect_reclaimed: bool,
) -> None:
    """
    #275 第三消费者（server 回收）：``retained`` 同时喂给 ``other_plugin_mcp_deps(..., retained_records=...)``。

    本次写盘即被 sweep 的 legacy 记录**不得**被当作「仍有依赖者」——否则该记录随后从账本消失，X 再无认领者
    却未被回收（正是 #275 要根治的泄漏形态，只是换到 server 面）。夹具用**混合记录**（``bundledMcpServers``
    与 ``mcpServers`` 并存；手编 / 半迁移形态）才能让回收面也被同一存活判据钉住；``live-record`` 是负对照。

    ``legacy-sole-declarer``（#275 复审 🟡2）：被 sweep 的 legacy 记录是 X 的**唯一**声明者——回收候选必须取
    ``doomed``（含非目标 legacy）并集，只取 ``targeted`` 会让 X 无人认领却仍挂在运行期（泄漏）。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    targeted_record = {"scope": "user", "installPath": str(ext)}
    if targeted_declares:
        targeted_record["mcpServers"] = [FIGMA_BID]
    _seed_installed(
        home,
        {
            "audit@acme": [
                targeted_record,
                {"scope": "project", "projectPath": str(workdir), "installPath": str(ext), **retained_extra},
            ]
        },
    )
    reg = _seed_registry(home, "acme", "audit")
    mcp = _FakeMCP()

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", remove_server=mcp.remove,
    ) is True

    assert mcp.removed == ([FIGMA_BID] if expect_reclaimed else []), (
        "legacy 记录写盘即被 sweep ⇒ 不得阻断 server 回收" if expect_reclaimed else "存活记录仍声明依赖 ⇒ server 保留"
    )


async def test_uninstall_scoped_legacy_record_clone_path_still_reclaimed_275(tmp_path: Path) -> None:
    """
    #275 复审 🔴：**删除候选**必须取 ``doomed``（``targeted`` ∪ 非目标 legacy），不能只取 ``targeted``。

    形态：目标 scope 记录指向**共享 catalog**（删除侧按归属校验返回 ``None``），唯一指向**独占 clone** 的是
    非目标 legacy 记录。``retained`` 门控说可删，但删除循环若只遍历 ``targeted``，它拿不到那条 legacy 的
    ``installPath`` ⇒ clone 永久留盘（账本已空 ⇒ 再 uninstall 是 no-op、gc 只遍历账本 pid）。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    catalog_root = marketplace_skill_dir(home, "acme") / "plugins" / "audit"
    catalog_root.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(catalog_root), "bundledMcpServers": [FIGMA_NAME]},
                {
                    "scope": "project",
                    "projectPath": str(workdir),
                    "installPath": str(ext),
                    "bundledMcpServers": [FIGMA_NAME],
                },
            ]
        },
    )
    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True

    assert not ext.exists(), "非目标 legacy 记录指向的独占 clone 必须回收（候选取 doomed，非 targeted）"
    assert catalog_root.exists(), "共享 catalog 内的落点按归属校验保留（删除侧不自作主张）"
    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"], "账本终态为空"
    assert len(reg) == 0, "无存活记录 ⇒ 技能（含孤儿）注销"


async def test_uninstall_scoped_legacy_record_clears_project_enabled_275(tmp_path: Path) -> None:
    """
    #275 复审 🟡1：被 sweep 的非目标 legacy 记录的 ``projectPath`` 层 ``enabledPlugins`` 也要清。

    漏清的后果：条目留在项目层（可入 git）。之后重装该 plugin 会**立即激活**，违反 §2.4「install 不激活」。
    故 enabled 清理面与预检面都取 ``doomed`` 而非 ``targeted``。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project_settings = workdir_project_settings_path(workdir)
    _write_json(project_settings, {"enabledPlugins": {"audit@acme": True}})
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext), "bundledMcpServers": [FIGMA_NAME]},
                {
                    "scope": "project",
                    "projectPath": str(workdir),
                    "installPath": str(ext),
                    "bundledMcpServers": [FIGMA_NAME],
                },
            ]
        },
    )
    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True

    settings = json.loads(project_settings.read_text(encoding="utf-8"))
    assert "audit@acme" not in settings.get("enabledPlugins", {}), (
        "doomed legacy 记录的 project 层 enabled 条目必须清（否则重装即激活，违反 §2.4）"
    )


async def test_uninstall_scoped_legacy_project_path_prechecked_before_destructive_steps_275(tmp_path: Path) -> None:
    """
    #275 复审 🟡1（预检半边）：预检面同样取 ``doomed``。

    既然被 sweep 的 legacy 记录的 ``projectPath`` 层会被清理，它就必须与 ``targeted`` 的 projectPath 一样进入
    :func:`_precheck_settings_writes`——否则畸形目标键会在「已删树 / 已注销技能」之后才被发现，而重试因账本
    已空走 no-op ⇒ 永远清不干净（#246 的原缺陷形态）。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _write_json(workdir_project_settings_path(workdir), {"enabledPlugins": ["not", "an", "object"]})
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext), "bundledMcpServers": [FIGMA_NAME]},
                {
                    "scope": "project",
                    "projectPath": str(workdir),
                    "installPath": str(ext),
                    "bundledMcpServers": [FIGMA_NAME],
                },
            ]
        },
    )
    reg = _seed_registry(home, "acme", "audit")

    with pytest.raises(PluginInstallError):
        await uninstall_plugin(
            "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
        )

    assert ext.exists(), "预检 fail-closed ⇒ 树未被删（零副作用）"
    assert reg.resolve("audit:lint") is not None, "技能未动"
    assert load_installed_plugins(home=home, drop_legacy=False)["plugins"]["audit@acme"], "账本未动"


async def test_uninstall_retained_nonempty_does_not_precheck_swept_legacy_project_path_275(tmp_path: Path) -> None:
    """
    #275 复审 🟡1（反向用例）：``retained`` 非空时**不得**把被 sweep 的 legacy 记录 projectPath 纳入预检。

    末段 enabled 清理被 ``not remaining``（等价 ``not retained``）门控——有存活记录时那层**根本不会被写**。
    若预检无条件取 ``doomed``，就会为一个不会写的层 fail-closed：同一命令增量前成功、增量后失败，且重试
    （账本已在）不自愈。故预检面与清理门控同源：``retained`` 非空按 ``targeted``。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    local_dir = tmp_path / "local-proj"
    local_dir.mkdir()
    legacy_dir = tmp_path / "legacy-proj"
    legacy_dir.mkdir()
    malformed_project = workdir_project_settings_path(legacy_dir)
    malformed = '{"enabledPlugins": ["not", "an", "object"]}'
    malformed_project.parent.mkdir(parents=True, exist_ok=True)
    malformed_project.write_text(malformed, encoding="utf-8")
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext)},
                {"scope": "local", "projectPath": str(local_dir), "installPath": str(ext)},
                {
                    "scope": "project",
                    "projectPath": str(legacy_dir),
                    "installPath": str(ext),
                    "bundledMcpServers": [FIGMA_NAME],
                },
            ]
        },
    )
    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True, "retained 非空 ⇒ 不写 project/local 层 ⇒ 其畸形不得 fail-closed"

    assert malformed_project.read_text(encoding="utf-8") == malformed, "该层未被读写（通道未被碰）"
    remaining = load_installed_plugins(home=home)["plugins"]["audit@acme"]
    assert [r["scope"] for r in remaining] == ["local"], "存活记录保留；被 sweep 的 legacy 记录消失"
    assert reg.resolve("audit:lint") is not None, "存活记录仍在 ⇒ 技能保留"


async def test_uninstall_pure_legacy_record_does_not_reclaim_by_display_name_275(tmp_path: Path) -> None:
    """
    #275 复审 🟡2：纯 legacy 记录（**仅** ``bundledMcpServers``）不产生任何停摘候选。

    旧格式记的是 display **name**（协议 §4.9.1-1 MUST NOT 当身份用）。若将来给 ``ledger_mcp_deps_of`` 加
    「缺失 ``mcpServers`` 回退 ``bundledMcpServers``」，旧 name 会被当 ``bundle_id`` 停摘——本用例是这条的锁。
    """
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.mkdir(parents=True)
    _seed_installed(
        home,
        {"audit@acme": [{"scope": "user", "installPath": str(ext), "bundledMcpServers": [FIGMA_NAME]}]},
    )
    reg = _seed_registry(home, "acme", "audit")
    mcp = _FakeMCP()

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=mcp.remove,
    ) is True

    assert mcp.removed == [], "display name 不得被当作 bundle_id 停摘（§4.9.1-1）"
    assert len(reg) == 0
    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"]


async def test_uninstall_reclaim_list_taken_from_ledger_before_tree_removal_275(
    tmp_path: Path, monkeypatch,
) -> None:
    """
    #275 / §4.9.1-3 机制断言：停摘名单在删树**之前**取得，且只依赖账本字段。

    钉死两种容易退化的实现失误：
    1. **取得时机**——``ledger_mcp_deps_of`` 被调用时独占 clone 仍在盘上（= 早于 ② 删树）；
    2. **字段来源**——盘上 ``mcp-servers/`` 声明的是另一个 server（诱饵），但 ``remove_server`` 收到的必须是
       账本 ``mcpServers`` 里的 bundle_id。若有人改成「删树后从 installPath 重解析」，两条同时失守。

    成功路径的完整正对照也在此：``remove_server`` 收到账本 bundle_ids ∧ skills（含孤儿）注销 ∧ 账本/意图清空。
    """
    from a2c_smcp.computer.settings import installer as installer_module

    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    _write_json(ext / "mcp-servers" / "decoy.json", _stdio("decoy.mcp"))  # 盘上诱饵：bundle_id 应为 decoy_mcp
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(ext), "mcpServers": [FIGMA_BID]}]})
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}})
    reg = _seed_registry(home, "acme", "audit")
    mcp = _FakeMCP()

    real_ledger_deps = installer_module.ledger_mcp_deps_of
    seen: list[tuple[set[str], bool]] = []

    def _spy(records: object) -> set[str]:
        deps = real_ledger_deps(records)
        seen.append((deps, ext.exists()))
        return deps

    monkeypatch.setattr(installer_module, "ledger_mcp_deps_of", _spy)

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=mcp.remove,
    ) is True

    assert seen == [({FIGMA_BID}, True)], "停摘名单必须在删树之前、从账本字段取得"
    assert mcp.removed == [FIGMA_BID], "回收的是账本声明的 bundle_id（盘上 decoy 不参与）"
    assert not ext.exists(), "独占 clone 已回收"
    assert len(reg) == 0, "skills（含孤儿）已注销"
    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"], "账本记录已删"
    settings = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert settings.get("installedPlugins") == [], "安装意图已删"


async def test_uninstall_malformed_install_path_does_not_raise_258(tmp_path: Path) -> None:
    """#258 🟡2：损坏账本（installPath 含 NUL）→ 纯词法归属判定为 clone 外（非独占）→ 保留文件、卸载仍完成。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": "/tmp/a\u0000b"}]})

    assert await uninstall_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env) is True
    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"]


async def test_uninstall_refuses_symlinked_clone_keeps_ledger_258(tmp_path: Path) -> None:
    """#258 复审三轮 🔴1/🟡2：独占 clone 为软链（拒删）→ 抛 PluginInstallError，账本与意图保留。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("x", encoding="utf-8")
    ext = _ext_plugin_root(home, "acme", "audit")
    ext.parent.mkdir(parents=True)
    try:
        ext.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": str(ext), "mcpServers": ["figma"]}]})
    _write_json(user_settings_path(env), {"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}})
    reg = SkillRegistry()
    reg.register_or_update({"name": "audit:lint", "source": f"{SOURCE_MARKETPLACE}:acme", "path": str(target)})
    removed_servers: list[str] = []

    async def _remove_server(bundle_id: str) -> None:
        removed_servers.append(bundle_id)

    with pytest.raises(PluginInstallError, match=r"failed to remove exclusive clone for 'audit@acme'.*symlinked external clone"):
        await uninstall_plugin(
            "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, remove_server=_remove_server,
        )

    assert "audit@acme" in load_installed_plugins(home=home)["plugins"], "删除失败不得删账本"
    assert load_installed_plugins(home=home)["plugins"]["audit@acme"][0]["installPath"] == str(ext), "installPath 不变"
    user = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["audit@acme"], "删除失败不得删安装意图"
    assert user["enabledPlugins"]["audit@acme"] is True, "删除失败不得清 enabled"
    assert reg.resolve("audit:lint") is not None, "删除失败不得注销 skill（承重顺序）"
    assert removed_servers == [], "删除失败不得摘除 bundled server（承重顺序）"
    assert (target / "keep").exists() and ext.is_symlink()


async def test_uninstall_scoped_symlink_component_retained_blocks_deletion_258(tmp_path: Path) -> None:
    """#258 复审 🟡5：retained 记录 installPath 含软链组件 → 纯词法仍判 clone 内 → 门控保留 clone。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    ext = _ext_plugin_root(home, "acme", "audit")
    (ext / "pkg").mkdir(parents=True)
    target = tmp_path / "target"
    (target / "sub").mkdir(parents=True)
    (ext / "pkg").rmdir()
    try:
        (ext / "pkg").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_installed(
        home,
        {
            "audit@acme": [
                {"scope": "user", "installPath": str(ext)},
                {"scope": "project", "projectPath": str(workdir), "installPath": str(ext / "pkg" / "sub")},
            ]
        },
    )

    reg = _seed_registry(home, "acme", "audit")

    assert await uninstall_plugin(
        "audit@acme", reg, home, non_plugin_bundle_ids=lambda: set(), env=env, scope="user", keep_servers=True,
    ) is True

    assert ext.exists(), "retained 记录的软链组件路径按词法仍引用 clone → 不得删除"
    assert [r["scope"] for r in load_installed_plugins(home=home)["plugins"]["audit@acme"]] == ["project"]
    assert reg.resolve("audit:lint") is not None, "#262：retained 记录仍在 → 技能保留"
    assert reg.is_orphan("audit:old")


async def test_uninstall_keeps_installpath_outside_external_clone_258(tmp_path: Path) -> None:
    """#258：账本 installPath 不在独占外部 clone（盘外/历史）→ 文件保留、账本照删、不抛。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    outside = tmp_path / "outside-home"
    outside.mkdir()
    (outside / "keepme").write_text("x", encoding="utf-8")
    _seed_installed(home, {"evil@acme": [{"scope": "user", "installPath": str(outside)}]})

    assert await uninstall_plugin("evil@acme", SkillRegistry(), home, non_plugin_bundle_ids=lambda: set(), env=env) is True

    assert outside.exists() and (outside / "keepme").exists(), "非独占路径 → 按设计保留"
    assert "evil@acme" not in load_installed_plugins(home=home)["plugins"]


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
        existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register, remove_server=mcp.remove,
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
        existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register, remove_server=mcp.remove,
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


async def test_enable_register_without_remove_server_raises(tmp_path: Path) -> None:
    """护栏：enable 给了 register_server 但缺 remove_server → 抛（否则写后失败无法摘除本次 attempt，§2.4）。"""
    home = _home(tmp_path)
    mcp = _FakeMCP()
    with pytest.raises(PluginInstallError, match="remove_server is required"):
        await enable_plugin(
            "audit@acme", SkillRegistry(), home, env=_env(tmp_path),
            existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register,
        )


async def test_enable_converts_manifest_rename_rejection_to_structured_error(tmp_path: Path, monkeypatch) -> None:
    """
    #260 终审 🟡4 端到端锁：`enable_plugin` 里 `MarketplaceNameChangeError → PluginInstallError` 的转换
    必须真被执行（删掉转换会变成裸异常 traceback），且回滚已还原 `enabledPlugins`。
    """
    from a2c_smcp.computer.skills.marketplace_identity import MarketplaceNameChangeError

    home = _home(tmp_path)
    env = _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[])
    audit_path = str(marketplace_skill_dir(home, "acme") / "plugins" / "audit")
    _seed_installed(home, {"audit@acme": [{"scope": "user", "installPath": audit_path}]})

    async def _renamed(*_args: object, **_kwargs: object):
        raise MarketplaceNameChangeError(
            "manifest renamed upstream",
            details={"name": "acme", "manifestName": "acme-renamed", "baselineManifestName": "acme"},
        )

    monkeypatch.setattr(_STAGE, _renamed)

    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin("audit@acme", SkillRegistry(), home, env=env)

    err = ei.value
    assert err.error_code == "validation"
    assert err.details["reason"] == "manifest-name-changed"
    assert err.details["manifestName"] == "acme-renamed"
    # #289：转换经统一出口后原异常仍挂在 __cause__（不吞；prior absent → 不包装 retained）
    assert isinstance(err.__cause__, MarketplaceNameChangeError)
    # 回滚：enabledPlugins 不得留下 True
    settings = json.loads(user_settings_path(env).read_text(encoding="utf-8")) if user_settings_path(env).exists() else {}
    assert (settings.get("enabledPlugins") or {}).get("audit@acme") is not True
