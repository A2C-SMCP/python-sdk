# -*- coding: utf-8 -*-
# filename: test_mcp_dependency_model.py
# @Time    : 2026/07/16
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
plugin ↔ MCP Server 依赖模型 + 账本回收判据单元测试（#153，D3+F1）
Plugin↔MCP-Server dependency model + ledger reclaim criterion unit tests.

协议 / Protocol: a2c-smcp-protocol ``computer-management/runtime-contract.md`` §2.5（依赖关系而非所有关系）、
§4.9.1（账本 ``mcpServers`` 纯 bundle_id 数组 + 回收判据 + 停摘自足 + 旧格式丢弃重建）、§5.6（合法共存）；
``conformance-tests.md`` §2.0（夹具 name/bundle_id 分叉）。共识 Discussion #23 终审 D3 / F1。

测试意图 / Test intentions:
- **回收判据五场景**（§4.9.1-2「回收 X ⟺ 无其他 plugin 声明依赖 X ∧ X 非用户声明」）：
  ① 独占依赖 → 卸载回收；② 用户 mcp.json 也声明 → **永不连坐**；③ 他 plugin 仍依赖 → 保留；
  ④ 最后一个依赖者卸载 → 回收（**传递性无泄漏**——删 provenance 的正当性所系）；⑤ 事后用户声明 → 不回收。
- **scoped uninstall**：自己剩余 scope 记录仍算依赖者 → 不回收。
- **依赖模型（D3）**：同 bundle_id 已有 = 依赖已满足 → **MUST NOT 拒绝**；display 同名异 bundle_id = 合法共存；
  enable 时依赖已满足 → 复用既有实例、不重挂覆盖。
- **账本 schema（F1）**：``mcpServers`` 存 **bundle_id** 非 display name；旧格式 ``bundledMcpServers`` 丢弃重建。
- **gc**：孤儿清理同样过回收判据（用户自有 server 不被连坐）。

夹具铁律（协议 conformance §2.0 + #150）：**display name 与 bundle_id 必须分叉**——本模块 server 名一律带 ``.``
（``figma.mcp`` → bundle_id ``figma_mcp``），令「误用 name 当身份」的实现无法蒙混通过。
"""

import json
import os
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.installer import (
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from a2c_smcp.computer.settings.mcp_config import non_plugin_declared_bundle_ids
from a2c_smcp.computer.settings.reconciler import NonPluginBundleIds, gc_plugins
from a2c_smcp.computer.settings.store import (
    load_installed_plugins,
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
)
from a2c_smcp.computer.skills.home import SOURCE_MARKETPLACE, marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry

_SRC = {"type": "git", "url": "https://example.com/acme.git"}
_STAGE = "a2c_smcp.computer.settings.installer.stage_marketplace_skills"

# 夹具身份对：display name 含 `.` → normalize_name 折成 `_`，故 name ≠ bundle_id（协议 conformance §2.0）。
FIGMA_NAME, FIGMA_BID = "figma.mcp", "figma_mcp"
SHARED_NAME, SHARED_BID = "shared.fs", "shared_fs"


# ── 辅助 / helpers ───────────────────────────────────────────────────────────
def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _env(tmp_path: Path) -> dict[str, str]:
    """重定向 XDG_CONFIG_HOME → tmp，隔离 user settings.json / mcp.json 写。"""
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _stdio(name: str, command: str = "node") -> dict:
    return {"name": name, "type": "stdio", "server_parameters": {"command": command}}


def _declared(
    env: dict[str, str],
    *,
    flag: Path | None = None,
    embed: list[MCPServerConfig] | None = None,
) -> NonPluginBundleIds:
    """
    回收判据「X 非用户声明」项的数据源，**与生产同构** / The criterion's data source, mirroring production。

    生产侧 = :func:`~a2c_smcp.computer.cli.commands.build_mcp_callbacks` 的 ``non_plugin_bundle_ids``
    （包 ``Computer.resolve_mcp_declarations()``）。此处直呼同一底层函数，故测试与生产读**同一判据面**——
    传桩（如 ``lambda: set()``）会让「用户/宿主自有 server 永不连坐」这条守卫**永真**，正是本仓踩过四次的假绿。

    :param flag: ``--mcp-config`` flag 层 mcp.json（四景 ①④）。
    :param embed: 宿主构造入参 ``Computer(mcp_servers=...)``（四景 ②）。
    """
    return lambda: non_plugin_declared_bundle_ids(env=env, flag_config_path=flag, embed_servers=embed)


def _setup_catalog(home: Path, mp: str, plugin: str, *, servers: list[str], commit: str = "abc123") -> Path:
    """造 catalog 树（相对源 plugin，无 git）+ marketplace.json + plugin mcp-servers/ + seed known_marketplaces。"""
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
    # 合并而非替换：多 marketplace 场景（③④ 共享依赖）须共存。
    known = load_known_marketplaces(home=home)
    known["marketplaces"][mp] = {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": commit}
    save_known_marketplaces(known, home=home)
    return plugin_root


def _user_mcp_json(tmp_path: Path, servers: dict[str, dict]) -> None:
    """在 user scope mcp.json 声明 server（= 用户自有声明，回收判据第二项的唯一来源）。"""
    _write_json(tmp_path / "cfg" / "a2c" / "mcp.json", {"servers": servers, "inputs": []})


def _fake_stage(calls: list[dict] | None = None):
    async def _stage(name, source, registry, home, *, plugin_filter=None, auto_update=False, refresh=False, timeout=0.0, env=None):
        if calls is not None:
            calls.append({"name": name, "plugin_filter": set(plugin_filter or ())})
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
    """记录 MCP 注入回调调用；**身份一律 bundle_id**（#153：账本存 bundle_id ⇒ 停摘链收 bundle_id）。"""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing: set[str] = set(existing or ())  # bundle_id 集
        self.registered: list[str] = []  # 本次 register 的 bundle_id（由 cfg 解析）
        self.removed: list[str] = []  # 被停摘的 bundle_id

    def existing_bundle_ids(self) -> set[str]:
        return set(self.existing)

    async def register(self, cfg) -> None:
        from a2c_smcp.utils.bundle_id import resolve_bundle_id

        bid = resolve_bundle_id(cfg)
        self.registered.append(bid)
        self.existing.add(bid)

    async def remove(self, bundle_id: str) -> None:
        self.removed.append(bundle_id)
        self.existing.discard(bundle_id)


async def _install(home: Path, env: dict, pid: str, mcp: _FakeMCP) -> dict:
    plugin, mp = pid.split("@")
    return await install_plugin(pid, SkillRegistry(), home, env=env, existing_bundle_ids=mcp.existing_bundle_ids)


# ── F1：账本 schema —— 存 bundle_id 而非 display name ─────────────────────────
async def test_ledger_stores_bundle_id_not_display_name(tmp_path: Path, monkeypatch) -> None:
    """§4.9.1-1：``mcpServers`` 只记 bundle_id，MUST NOT 记 display name（夹具 name≠bundle_id 方能鉴别）。"""
    monkeypatch.chdir(tmp_path)  # 隔离 project/local scope（resolve_mcp_config 锚 cwd，#116）
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())

    record = await install_plugin("audit@acme", SkillRegistry(), home, env=env, existing_bundle_ids=lambda: set())

    assert record["mcpServers"] == [FIGMA_BID]  # bundle_id，非 "figma.mcp"
    assert "bundledMcpServers" not in record  # 旧字段不存在
    assert load_installed_plugins(home=home)["plugins"]["audit@acme"][0]["mcpServers"] == [FIGMA_BID]


async def test_ledger_writes_empty_array_when_no_deps(tmp_path: Path, monkeypatch) -> None:
    """无 bundled server → ``mcpServers: []``（无条件写，与 rust#139 磁盘格式逐字节一致）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[])
    monkeypatch.setattr(_STAGE, _fake_stage())

    record = await install_plugin("audit@acme", SkillRegistry(), home, env=env)

    assert record["mcpServers"] == []


# ── F1：回收判据五场景（§4.9.1-2）────────────────────────────────────────────
async def test_scenario1_sole_dependent_uninstall_reclaims(tmp_path: Path, monkeypatch) -> None:
    """① A 引入 X、无他人依赖、非用户声明 → 卸 A 回收 X。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)

    await uninstall_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove)

    assert mcp.removed == [FIGMA_BID]  # 停摘身份 = bundle_id


async def test_scenario2_user_declared_server_is_never_collateral(tmp_path: Path, monkeypatch) -> None:
    """② 用户 mcp.json 声明 X + A 依赖 X → 卸 A **不回收**（用户自有 server 永不连坐）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _user_mcp_json(tmp_path, {FIGMA_NAME: _stdio(FIGMA_NAME)})  # 用户自有同 bundle_id
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP(existing={FIGMA_BID})
    await _install(home, env, "audit@acme", mcp)

    await uninstall_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove)

    assert mcp.removed == []  # 用户声明 → 非本 plugin 可回收之物


async def test_scenario3_and_4_shared_dependency_no_leak(tmp_path: Path, monkeypatch) -> None:
    """③ A+B 共依赖 X → 卸 A 保留；④ 续卸 B → 回收（**传递性无泄漏**，删 provenance 的正当性所系）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _setup_catalog(home, "beta", "review", servers=[FIGMA_NAME])  # 同 bundle_id，另一 plugin
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)
    await _install(home, env, "review@beta", mcp)

    # ③ 卸 A —— B 仍声明依赖 X → 保留
    await uninstall_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove)
    assert mcp.removed == []

    # ④ 续卸 B —— 最后一个依赖者 → 回收，X 不泄漏
    await uninstall_plugin("review@beta", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove)
    assert mcp.removed == [FIGMA_BID]


async def test_scenario5_user_declares_after_install_blocks_reclaim(tmp_path: Path, monkeypatch) -> None:
    """⑤ A 引入 X 后用户又在 mcp.json 声明 X → 卸 A 不回收（判据是**现时**事实，非安装时点快照）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)  # 安装时用户尚未声明

    _user_mcp_json(tmp_path, {FIGMA_NAME: _stdio(FIGMA_NAME)})  # 事后声明

    await uninstall_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove)

    assert mcp.removed == []


# ── conformance §5：回收判据 origin 四景对拍（#164 / Discussion #32 裁决）─────────
#
# **#153 遗留缺口的转正**：原 `test_runtime_only_user_server_is_never_collateral`（xfail）钉的是
# 「用户经 `--config @file` / 内嵌构造挂的 server 不落 mcp.json ⇒ 被判『非用户声明』⇒ plugin 卸载连坐停摘」。
# 该 xfail 现**转正为下列四景**：`--config` 已归一为 `--mcp-config`（flag 层 mcp.json）、构造入参已成 embed 层，
# 二者均在 resolve 输出中携带正确 origin ⇒ 判据可分辨。
#
# ⚠️ **注意四景**不再**用 `_FakeMCP(existing=...)` 模拟「用户挂了 X」**——那是**裸运行期活跃集**，正是协议
# §4.9.1-2 钉死的歧路①（无 origin ⇒ flag/embed/plugin 三条挂载路径可观测同形）。用它做夹具会让守卫测的是
# 一个协议禁止的判据面。四景改注入**真实的 flag 文件 / embed 构造入参**。
def _flag_mcp(tmp_path: Path, servers: dict) -> Path:
    """写一份 flag 层 mcp.json（模拟 ``a2c-computer run --mcp-config <file>``）。"""
    p = tmp_path / "flag-mcp.json"
    _write_json(p, {"servers": servers, "inputs": []})
    return p


async def test_vector1_flag_mounted_server_is_never_collateral(tmp_path: Path, monkeypatch) -> None:
    """
    **四景①**：X 经 flag（``--mcp-config``）挂载 → 卸载声明依赖 X 的 plugin **不回收** X。

    origin=flag ⇒ 落 `non_plugin_declared_bundle_ids` ⇒ 「X 非用户声明」为假 ⇒ 不回收。
    **变异验证**：把判据数据源退回「只读 mcp.json 声明面」（即 `_declared(env)` 去掉 `flag=`）→ 本例必红。
    """
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    flag = _flag_mcp(tmp_path, {FIGMA_NAME: _stdio(FIGMA_NAME)})  # 用户经命令行点名声明 figma.mcp
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)

    await uninstall_plugin(
        "audit@acme", SkillRegistry(), home,
        non_plugin_bundle_ids=_declared(env, flag=flag), env=env, remove_server=mcp.remove,
    )

    assert mcp.removed == [], "经 --mcp-config 挂载的用户 server 被连坐停摘（协议 §4.9.1-2 四景①）"


async def test_vector2_embed_mounted_server_is_never_collateral(tmp_path: Path, monkeypatch) -> None:
    """
    **四景②**：X 经宿主构造入参挂载（``Computer(mcp_servers=...)``，origin=embed）→ **不回收**。

    宿主自有 server 不因 plugin 卸载连坐（§2.5-3：embed 是非-plugin origin）。
    **变异验证**：`_declared(env, embed=...)` 去掉 `embed=` → 本例必红。
    """
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    embed = [TypeAdapter(MCPServerConfig).validate_python(_stdio(FIGMA_NAME))]  # 宿主构造挂载
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)

    await uninstall_plugin(
        "audit@acme", SkillRegistry(), home,
        non_plugin_bundle_ids=_declared(env, embed=embed), env=env, remove_server=mcp.remove,
    )

    assert mcp.removed == [], "宿主构造入参挂载的 server 被连坐停摘（协议 §4.9.1-2 四景②）"


async def test_vector3_plugin_only_server_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    """
    **四景③**：X 仅由 plugin 声明（无其他 plugin 依赖、无任何非-plugin 声明）→ **回收**。

    这一景是四景里唯一「该回收」的——它守住判据**不会因为①②的修法而全面失灵**（否则 server 永久泄漏）。
    plugin 声明**不进 resolve**（结构性缺席，见 schema.SCOPE_ORDER）⇒ X ∉ 非-plugin 集 ⇒ 回收。
    """
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)

    await uninstall_plugin(
        "audit@acme", SkillRegistry(), home,
        non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove,
    )

    assert mcp.removed == [FIGMA_BID], "无人依赖 ∧ 无非-plugin 声明的 server 未被回收 ⇒ 泄漏（四景③）"


async def test_vector4_mixed_origin_same_bundle_id_is_never_collateral(tmp_path: Path, monkeypatch) -> None:
    """
    **四景④**：同 ``bundle_id`` 混源碰撞（plugin 声明依赖 X ∧ 用户经 flag 也声明 X，flag > plugin）→ **不回收**。

    用户覆盖权（§2.5-3「用户主权」）：即便某 plugin 也声明依赖同一 bundle_id，用户那条声明令 X 永不连坐。
    与①的区别：①的 X 只有 flag 一个来源；④ 的 X **两个来源并存**，考的是「plugin 也声明」不会盖过「非 plugin 声明」。
    """
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])  # plugin 声明依赖 figma.mcp
    monkeypatch.setattr(_STAGE, _fake_stage())
    flag = _flag_mcp(tmp_path, {FIGMA_NAME: _stdio(FIGMA_NAME, command="user-cmd")})  # 用户也声明同 bundle_id
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)

    await uninstall_plugin(
        "audit@acme", SkillRegistry(), home,
        non_plugin_bundle_ids=_declared(env, flag=flag), env=env, remove_server=mcp.remove,
    )

    assert mcp.removed == [], "混源（plugin + flag）同 bundle_id 被连坐停摘（协议 §4.9.1-2 四景④）"


async def test_disable_both_dependents_keeps_dep_resident(tmp_path: Path, monkeypatch) -> None:
    """
    驻留行为守卫（协议字面推论，rust-sdk#139 须同口径）：A、B 均声明 X，**二者都 disable 后 X 仍驻留**。

    §4.9.1-2 的「其他 plugin」= 账本 **installed** 记录（**含 disabled**，字面为「无其他 plugin **声明依赖**」）。
    故 disable A 时 B 的记录替 X 挡住回收，disable B 时 A 的记录同样挡住 ⇒ X 驻留至 uninstall。
    这是**保守侧**：X 多活一会儿，但不泄漏（最后一个依赖者 uninstall 时回收，见场景③④）。
    """
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _setup_catalog(home, "beta", "review", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)
    await _install(home, env, "review@beta", mcp)

    await disable_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove)
    await disable_plugin("review@beta", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove)

    assert mcp.removed == []  # 两者账本记录仍在 → 互相替对方挡住回收（驻留，非泄漏）


async def test_scoped_uninstall_keeps_dep_for_remaining_scope(tmp_path: Path, monkeypatch) -> None:
    """scoped uninstall：自己剩余 scope 记录仍声明依赖 → 不回收（判据须排除本次移除的记录、而非整个 pid）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    install_path = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                "audit@acme": [
                    {"scope": "user", "installPath": str(install_path), "mcpServers": [FIGMA_BID]},
                    {"scope": "project", "installPath": str(install_path), "projectPath": str(tmp_path), "mcpServers": [FIGMA_BID]},
                ],
            },
        },
        home=home,
    )
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP()

    await uninstall_plugin(
        "audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, scope="user", remove_server=mcp.remove,
    )

    assert mcp.removed == []  # project scope 记录仍依赖 X


async def test_disable_applies_reclaim_criterion(tmp_path: Path, monkeypatch) -> None:
    """disable 与 uninstall 同判据（§4.9.1-2 明列二者）：用户声明的 X 不因 disable 被摘。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _user_mcp_json(tmp_path, {FIGMA_NAME: _stdio(FIGMA_NAME)})
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP(existing={FIGMA_BID})
    await _install(home, env, "audit@acme", mcp)

    await disable_plugin("audit@acme", SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, remove_server=mcp.remove)

    assert mcp.removed == []


# ── D3：依赖模型（§2.5 / §5.6）────────────────────────────────────────────────
async def test_install_same_bundle_id_is_dependency_satisfied_not_conflict(tmp_path: Path, monkeypatch) -> None:
    """§2.5-1：同 bundle_id 已存在 = 依赖已满足 → **MUST NOT 拒绝**，正常安装并写账本。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP(existing={FIGMA_BID})  # 本地已有（用户自有或他 plugin 带入）

    record = await install_plugin("audit@acme", SkillRegistry(), home, env=env, existing_bundle_ids=mcp.existing_bundle_ids)

    assert record["mcpServers"] == [FIGMA_BID]  # 不抛、正常写账本
    assert "audit@acme" in load_installed_plugins(home=home)["plugins"]
    user = json.loads((tmp_path / "cfg" / "a2c" / "settings.json").read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["audit@acme"]  # 意图已写 = 真的装上了


async def test_install_same_display_name_different_bundle_id_coexists(tmp_path: Path, monkeypatch) -> None:
    """§5.6：display name 相同、bundle_id 不同 = 合法共存，MUST NOT 视为冲突。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    # 已有 server 的 display 名相同，但显式 bundle_id 不同 → 两个身份
    mcp = _FakeMCP(existing={"other_identity"})

    record = await install_plugin("audit@acme", SkillRegistry(), home, env=env, existing_bundle_ids=mcp.existing_bundle_ids)

    assert record["mcpServers"] == [FIGMA_BID]


async def test_enable_reuses_existing_instance_instead_of_remounting(tmp_path: Path, monkeypatch) -> None:
    """§2.5-1「复用既有实例」：enable 时依赖已满足 → **不重挂**（否则覆盖用户既有 server 配置）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME, SHARED_NAME])
    monkeypatch.setattr(_STAGE, _fake_stage())
    mcp = _FakeMCP()
    await _install(home, env, "audit@acme", mcp)
    mcp.existing.add(FIGMA_BID)  # 用户/他 plugin 已挂 figma_mcp

    await enable_plugin(
        "audit@acme", SkillRegistry(), home, env=env,
        existing_bundle_ids=mcp.existing_bundle_ids, register_server=mcp.register, remove_server=mcp.remove,
    )

    assert mcp.registered == [SHARED_BID]  # figma_mcp 依赖已满足 → 复用，不重挂


# ── F1：旧格式丢弃重建（§4.9.1-4）────────────────────────────────────────────
def test_legacy_ledger_record_is_discarded_on_load(tmp_path: Path) -> None:
    """§4.9.1-4：检测到旧格式（``bundledMcpServers`` name 数组）→ 整条丢弃（交 reconcile 从意图重建）。"""
    home = _home(tmp_path)
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": "/x", "bundledMcpServers": [FIGMA_NAME]}]}},
        home=home,
    )

    loaded = load_installed_plugins(home=home)

    assert "audit@acme" not in loaded["plugins"]  # 整条丢弃，MUST NOT 做 name→bundle_id 映射迁移


def test_legacy_ledger_pids_survive_for_intent_migration(tmp_path: Path) -> None:
    """
    ⚠️ **顺序依赖守卫**：v0.2.x→v0.3.0 意图迁移 MUST 能读到旧格式记录的 pid（``drop_legacy=False``）。

    两个迁移方向相反、互为前提：①「意图迁移」从**账本 pid** 回填 ``installedPlugins``；②「账本格式迁移」丢弃
    旧记录并**从该意图重建**。若 ② 的丢弃对 ① 也生效，v0.2.x 存量用户**带 MCP server 的 plugin 会连 pid 一起
    消失**（无 bundled server 的记录无该键、不受影响）⇒ 意图无从回填 ⇒ 升级后 plugin 静默全丢。
    本例实测曾红（`test_boot_migrates_legacy_ledger_installs_and_stays_active` 同时红）。
    """
    home = _home(tmp_path)
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": "/x", "bundledMcpServers": [FIGMA_NAME]}]}},
        home=home,
    )

    assert "audit@acme" not in load_installed_plugins(home=home)["plugins"]  # 默认读法：丢弃
    assert "audit@acme" in load_installed_plugins(home=home, drop_legacy=False)["plugins"]  # 迁移读法：pid 仍在


def test_legacy_and_new_records_mixed_only_legacy_dropped(tmp_path: Path) -> None:
    """混合账本：仅旧格式记录被丢，新格式记录保留（丢弃粒度 = 记录级）。"""
    home = _home(tmp_path)
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                "old@acme": [{"scope": "user", "installPath": "/x", "bundledMcpServers": [FIGMA_NAME]}],
                "new@acme": [{"scope": "user", "installPath": "/y", "mcpServers": [FIGMA_BID]}],
            },
        },
        home=home,
    )

    plugins = load_installed_plugins(home=home)["plugins"]

    assert "old@acme" not in plugins
    assert plugins["new@acme"][0]["mcpServers"] == [FIGMA_BID]


# ── gc：孤儿清理同样过回收判据 ───────────────────────────────────────────────
async def test_gc_does_not_reclaim_user_declared_server(tmp_path: Path, monkeypatch) -> None:
    """gc 是 uninstall 的批量形态 → 同判据；用户自有 server MUST NOT 被孤儿清理连坐。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    install_path = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    _user_mcp_json(tmp_path, {FIGMA_NAME: _stdio(FIGMA_NAME)})
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(install_path), "mcpServers": [FIGMA_BID]}]}},
        home=home,
    )
    torn: list[list[str]] = []

    async def _teardown(ids: list[str]) -> None:
        torn.append(ids)

    await gc_plugins(["audit@acme"], SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, mcp_teardown=_teardown)

    assert torn == []  # 用户声明 → 不回收


async def test_gc_reclaims_unreferenced_server(tmp_path: Path, monkeypatch) -> None:
    """gc 对照组：无人依赖 ∧ 非用户声明 → 回收（守卫非永真）。"""
    monkeypatch.chdir(tmp_path)
    home, env = _home(tmp_path), _env(tmp_path)
    install_path = _setup_catalog(home, "acme", "audit", servers=[FIGMA_NAME])
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(install_path), "mcpServers": [FIGMA_BID]}]}},
        home=home,
    )
    torn: list[list[str]] = []

    async def _teardown(ids: list[str]) -> None:
        torn.append(ids)

    await gc_plugins(["audit@acme"], SkillRegistry(), home, non_plugin_bundle_ids=_declared(env), env=env, mcp_teardown=_teardown)

    assert torn == [[FIGMA_BID]]
