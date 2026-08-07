# -*- coding: utf-8 -*-
# filename: test_computer_env_injection.py
# @Time    : 2026/08/07
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""Computer env/cwd 注入接缝测试（#134，对齐 rust-sdk#121）。

验证：嵌入式多实例场景下，``Computer(env=..., project_root=...)`` 使 ledger 与 settings
从同一 per-instance 锚点解析，消除 ownership/enablement 归属混源。

测试意图 / Test intentions（hermetic：纯 tempdir，零进程 ambient 依赖）:
- AC1 构造参数接缝：``Computer`` 接受 ``env`` / ``project_root`` 参数，缺省 ``None`` 保持向后兼容。
- AC2 env 注入贯通 settings 解析：``_resolve_declared_settings()`` 读取注入 XDG 而非进程 ambient。
- AC3 project_root 注入贯通 project/local 解析：注入 cwd 下 ``.tfrobot/settings.json`` 的
  ``enabledPlugins`` 被正确读取。
- AC4 双实例隔离：同一进程内两个 Computer 各注入不同 ``env``/``project_root``，归属视图互不干扰。
- AC5 list_mcp_servers_with_metadata 使用注入 env：不再硬编码 ``os.environ``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.store import save_installed_plugins, save_known_marketplaces
from a2c_smcp.computer.skills.home import marketplace_skill_dir


# ── helpers ──────────────────────────────────────────────────────────────
def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _make_config_home(base: Path) -> Path:
    """在 base 下创建 XDG-a2c 配置目录结构。"""
    d = base / "a2c"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_settings(config_home: Path, data: dict) -> Path:
    """写入 settings.json 到 config home。"""
    p = config_home / "settings.json"
    _write_json(p, data)
    return p


def _write_project_settings(project_root: Path, data: dict) -> Path:
    """写入 .tfrobot/settings.json 到 project root。"""
    p = project_root / ".tfrobot" / "settings.json"
    _write_json(p, data)
    return p


def _write_project_mcp_json(project_root: Path, data: dict) -> Path:
    """写入 .tfrobot/mcp.json 到 project root。"""
    p = project_root / ".tfrobot" / "mcp.json"
    _write_json(p, data)
    return p


def _seed_plugin(
    home: Path,
    *,
    pid: str = "audit@acme",
    servers: tuple[str, ...] = ("bundled-srv",),
) -> Path:
    """在 skill_home 下种植一个带 bundled MCP server 的已安装 plugin。"""
    catalog = marketplace_skill_dir(home, "acme")
    _write_json(
        catalog / ".tfrobot-plugin" / "marketplace.json",
        {
            "name": "acme",
            "owner": {"name": "X"},
            "metadata": {"pluginRoot": "./plugins"},
            "plugins": [{"name": "audit", "source": "audit", "version": "1.0.0"}],
        },
    )
    plugin_root = catalog / "plugins" / "audit"
    for sname in servers:
        _write_json(
            plugin_root / "mcp-servers" / f"{sname}.json",
            {"name": sname, "type": "stdio", "server_parameters": {"command": "echo"}, "disabled": False},
        )
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "acme": {
                    "source": {"type": "git", "url": "https://example.com/acme.git"},
                    "installLocation": str(catalog.resolve()),
                    "commitSha": "abc123",
                }
            },
        },
        home=home,
    )
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                pid: [{"installPath": str(plugin_root.resolve()), "version": "1.0.0", "source": "acme"}]
            },
        },
        home=home,
    )
    return plugin_root


# ── AC1: 构造参数接缝 / constructor seam ─────────────────────────────────

def test_constructor_accepts_env_and_project_root():
    """Computer 接受 env 和 project_root 可选参数，缺省不抛。"""
    # 缺省（向后兼容）：不传 env/project_root → 不抛
    c = Computer(name="test")
    assert c._env is None
    assert c._project_root is None


def test_constructor_stores_env_and_project_root():
    """显式注入的 env 和 project_root 被正确存储。"""
    env = {"XDG_CONFIG_HOME": "/fake/xdg", "HOME": "/fake/home"}
    root = Path("/fake/project")
    c = Computer(name="test", env=env, project_root=root)
    assert c._env is env
    assert c._project_root is root


# ── AC2: env 注入贯通 settings 解析 / env reaches settings resolution ────

def test_resolve_declared_settings_reads_injected_env(tmp_path: Path):
    """_resolve_declared_settings() 从注入的 XDG_CONFIG_HOME 而非进程 ambient 读取 user settings。"""
    # 1. 构造隔离环境：user settings 写 enabledPlugins
    config_home = _make_config_home(tmp_path / "xdg")
    _write_settings(config_home, {"enabledPlugins": {"audit@acme": True}})
    # 2. 在 project_root 下也创建一个 project settings（不同值，验证优先级/读取来源）
    project_root = tmp_path / "project"
    _write_project_settings(project_root, {"enabledPlugins": {"other@acme": True}})
    # 3. 种植 plugin 到 skill_home（不含 enabledPlugins 在 home 内——settings 从 config 轴读）
    home = tmp_path / "skill-home"
    _seed_plugin(home)
    # 4. 注入 env（指向隔离 XDG），project_root 指向隔离目录
    env = {"XDG_CONFIG_HOME": str(config_home.parent), "HOME": str(tmp_path / "fake-home")}
    c = Computer(name="test", env=env, project_root=project_root, skill_home=home)
    # 5. 解析 settings → user layer 读到的 enabledPlugins 应来自注入 env
    resolved = c._resolve_declared_settings()
    # 6. 断言：enabledPlugins 包含注入的 audit@acme
    enabled = resolved.settings.get("enabledPlugins", {})
    assert enabled.get("audit@acme") is True, (
        f"Expected enabledPlugins from injected XDG, got: {enabled}"
    )


# ── AC3: project_root 注入贯通 project/local 解析 ─────────────────────────

def test_resolve_declared_settings_reads_injected_project_root(tmp_path: Path):
    """project scope 的 enabledPlugins 从注入 project_root 而非进程 cwd 读取。"""
    project_root = tmp_path / "project"
    # 在 project_root/.tfrobot/settings.json 写入 enabledPlugins
    _write_project_settings(project_root, {"enabledPlugins": {"proj-plugin@hub": True}})
    # skill_home + plugin
    home = tmp_path / "skill-home"
    _seed_plugin(home, pid="proj-plugin@hub")
    # 注入 project_root；env 也隔离
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "fake-home")}
    c = Computer(name="test", env=env, project_root=project_root, skill_home=home)
    resolved = c._resolve_declared_settings()
    enabled = resolved.settings.get("enabledPlugins", {})
    assert enabled.get("proj-plugin@hub") is True, (
        f"Expected enabledPlugins from project scope at {project_root}, got: {enabled}"
    )


# ── AC4: 双实例隔离 / dual-instance isolation ─────────────────────────────

def test_dual_instance_env_isolation(tmp_path: Path):
    """两个 Computer 实例注入不同 env/project_root → 各自读各自的 enabledPlugins。"""
    # 实例 A
    xdg_a = _make_config_home(tmp_path / "xdg-a")
    _write_settings(xdg_a, {"enabledPlugins": {"plugin-a@acme": True}})
    proj_a = tmp_path / "project-a"
    home_a = tmp_path / "skill-home-a"
    _seed_plugin(home_a, pid="plugin-a@acme")

    # 实例 B
    xdg_b = _make_config_home(tmp_path / "xdg-b")
    _write_settings(xdg_b, {"enabledPlugins": {"plugin-b@acme": True}})
    proj_b = tmp_path / "project-b"
    home_b = tmp_path / "skill-home-b"
    _seed_plugin(home_b, pid="plugin-b@acme")

    env_a = {"XDG_CONFIG_HOME": str(xdg_a.parent), "HOME": str(tmp_path / "home-a")}
    env_b = {"XDG_CONFIG_HOME": str(xdg_b.parent), "HOME": str(tmp_path / "home-b")}

    ca = Computer(name="A", env=env_a, project_root=proj_a, skill_home=home_a)
    cb = Computer(name="B", env=env_b, project_root=proj_b, skill_home=home_b)

    # 各自解析的 enabledPlugins 不应互相干扰
    resolved_a = ca._resolve_declared_settings()
    resolved_b = cb._resolve_declared_settings()

    enabled_a = resolved_a.settings.get("enabledPlugins", {})
    enabled_b = resolved_b.settings.get("enabledPlugins", {})

    assert enabled_a.get("plugin-a@acme") is True
    assert "plugin-b@acme" not in enabled_a
    assert enabled_b.get("plugin-b@acme") is True
    assert "plugin-a@acme" not in enabled_b


# ── AC5: list_mcp_servers_with_metadata 使用注入 env ───────────────────────

def test_list_mcp_servers_with_metadata_uses_injected_env(tmp_path: Path):
    """list_mcp_servers_with_metadata 不再硬编码 os.environ——走注入 env 和 cwd。"""
    config_home = _make_config_home(tmp_path / "xdg")
    _write_settings(
        config_home,
        {
            "installedPlugins": ["audit@acme"],
            "enabledPlugins": {"audit@acme": True},
        },
    )
    project_root = tmp_path / "project"
    # 在注入 project_root 下写入 user mcp.json 声明（验证 cwd 贯通 → 归属判定正确）
    _write_project_mcp_json(
        project_root,
        {"servers": {"user-srv": {"type": "stdio", "server_parameters": {"command": "echo"}}}},
    )
    home = tmp_path / "skill-home"
    _seed_plugin(home, servers=("bundled-srv",))

    env = {"XDG_CONFIG_HOME": str(config_home.parent), "HOME": str(tmp_path / "fake-home")}
    c = Computer(name="test", env=env, project_root=project_root, skill_home=home)

    # 验证 resolve_mcp_declarations 正确读取注入 project_root 下的 user mcp.json
    declared = c.resolve_mcp_declarations(env=c._resolve_env(), cwd=c._resolve_cwd())
    assert "user-srv" in declared.servers, (
        f"Expected user-srv in declarations from injected project_root, got: {list(declared.servers.keys())}"
    )

    # pre-boot（manager 未建）时仍可查 inventory —— bundled server 应出现
    inventory = c.list_mcp_servers_with_metadata()

    # bundled-srv 的 bundle_id = normalize("bundled-srv") = "bundled-srv"
    bundled_bids = {item.bundle_id for item in inventory}
    assert "bundled-srv" in bundled_bids, (
        f"Expected bundled-srv in inventory (enabled via injected XDG), got: {bundled_bids}"
    )


# ── 向后兼容：缺省行为 = ambient ──────────────────────────────────────────

def test_default_none_uses_ambient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """env=None / project_root=None 时行为与进程 ambient 一致（向后兼容）。"""
    # 临时修改 os.environ 以验证缺省回退
    isolated_xdg = tmp_path / "iso-xdg"
    config_home = _make_config_home(isolated_xdg)
    _write_settings(config_home, {"enabledPlugins": {"ambient-plugin@acme": True}})
    proj = tmp_path / "iso-project"
    proj.mkdir()
    home = tmp_path / "skill-home"
    _seed_plugin(home, pid="ambient-plugin@acme")

    with monkeypatch.context() as m:
        m.setenv("XDG_CONFIG_HOME", str(isolated_xdg))
        m.setenv("HOME", str(tmp_path / "iso-home"))
        m.chdir(str(proj))

        c = Computer(name="test", skill_home=home)  # 不传 env/project_root
        resolved = c._resolve_declared_settings()
        enabled = resolved.settings.get("enabledPlugins", {})
        assert enabled.get("ambient-plugin@acme") is True
