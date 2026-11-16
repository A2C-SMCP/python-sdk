# -*- coding: utf-8 -*-
# filename: test_plugin_name_uniqueness.py
# @Time    : 2026/11/16
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
跨 marketplace 同名 plugin 安装层拦截（#257）/ Cross-marketplace bare-name uniqueness。

协议依据 / Protocol: a2c-smcp-protocol ``docs/specification/skill.md`` §1.2 note / §1.5（`<plugin>` 裸名跨
marketplace 全局唯一，`<plugin>@<marketplace>` 是完整安装 id）。
验收锚 / Acceptance anchor: ``conformance-tests.md`` §5「跨 marketplace 同名 plugin 安装」——
已安装 ``alpha@mp-a`` 后 install ``alpha@mp-b`` MUST 在安装层拒绝；``alpha@mp-a`` 及其 SKILL 保持原
``source``；``installedPlugins`` 不新增 ``alpha@mp-b``。

测试意图 / Test intentions:
- install 前置：同名不同 marketplace → 结构化 ``validation`` 拒绝，且拒绝**先于**迁移 / settings / 账本任何写
  （含 legacy 账本 + 无意图键的存量 home）。
- enable 防御：存量双装下 enable 任一 pid 均 fail-closed，且 ``enabledPlugins`` / stage / 挂载零副作用。
- 注册点守卫（存量双装 boot 重扫）：保留先注册者、记 ERROR、后注册源不覆盖。
- 负对照：同 pid 重装幂等；同 marketplace 刷新不误拒。
- 夹具 **merge-aware**（``load_known_marketplaces`` → 改 → ``save_known_marketplaces``），两 marketplace 共存。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import a2c_smcp.computer.skills.registry as registry_mod
from a2c_smcp.computer.settings.installer import PluginInstallError, enable_plugin, install_plugin
from a2c_smcp.computer.settings.recovery import recover_marketplace_skills
from a2c_smcp.computer.settings.scope import resolve_settings, user_settings_path, workdir_project_settings_path
from a2c_smcp.computer.settings.store import (
    load_installed_plugins,
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
)
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.registry import SkillRegistry

_SRC = {"type": "git", "url": "https://example.com/acme.git"}


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """project/local 层锚定 cwd（#116）——chdir 隔离，避免读进真实仓库 ``.tfrobot/``。"""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def capture_registry_errors(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """捕获 registry 模块 ERROR（项目 logger 关闭 propagate，直接挂 handler）。"""
    registry_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR)
    try:
        yield caplog
    finally:
        registry_mod.logger.removeHandler(caplog.handler)


# ── 辅助 / helpers ───────────────────────────────────────────────────────────
def _home(tmp_path: Path) -> Path:
    h = tmp_path / "skill-home"
    h.mkdir()
    return h


def _env(tmp_path: Path) -> dict[str, str]:
    """重定向 XDG_CONFIG_HOME → tmp，隔离 user settings.json 写。"""
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _skill_md(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\nlicense: MIT\n---\n# {name}\nbody\n"


def _setup_marketplace(home: Path, mp: str, plugin: str, *, skill: str, description: str) -> Path:
    """造 catalog 树（相对源 plugin）+ skills/<skill>/SKILL.md，并 **合并** seed known_marketplaces。"""
    catalog = marketplace_skill_dir(home, mp)
    manifest = {
        "name": mp,
        "owner": {"name": "X"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": plugin, "source": plugin, "version": "1.0.0"}],
    }
    _write_json(catalog / ".tfrobot-plugin" / "marketplace.json", manifest)
    plugin_root = catalog / "plugins" / plugin
    skill_dir = plugin_root / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_skill_md(skill, description), encoding="utf-8")
    known = load_known_marketplaces(home=home)
    known["marketplaces"][mp] = {"source": _SRC, "installLocation": str(catalog.resolve()), "commitSha": "abc123"}
    save_known_marketplaces(known, home=home)
    return plugin_root


def _record(plugin_root: Path, *, scope: str = "user") -> dict:
    return {"scope": scope, "installPath": str(plugin_root), "version": "1.0.0", "mcpServers": []}


def _seed_ledger(home: Path, plugins: dict[str, list[dict]]) -> None:
    save_installed_plugins({"version": 1, "plugins": plugins}, home=home)


def _read_settings(env: dict[str, str]) -> dict:
    path = user_settings_path(env)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ── install 前置：跨 marketplace 同名拒绝 ─────────────────────────────────────
async def test_install_second_marketplace_same_bare_name_rejected(tmp_path: Path) -> None:
    """conformance §5：install `alpha@mp-b` MUST 拒绝；mp-a 及其 SKILL 原 source 不变；意图不新增。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    _setup_marketplace(home, "mp-a", "alpha", skill="foo", description="from-a")
    _setup_marketplace(home, "mp-b", "alpha", skill="foo", description="from-b")

    reg = SkillRegistry()
    await install_plugin("alpha@mp-a", reg, home, env=env)
    await enable_plugin("alpha@mp-a", reg, home, env=env)
    got = reg.resolve("alpha:foo")
    assert got is not None
    assert got["source"] == "marketplace:mp-a"
    before = _read_settings(env)

    with pytest.raises(PluginInstallError) as ei:
        await install_plugin("alpha@mp-b", reg, home, env=env)

    assert ei.value.error_code == "validation"
    assert ei.value.details["conflictWith"] == "alpha@mp-a"
    message = str(ei.value)
    assert "alpha@mp-a" in message
    assert "uninstall" in message

    # 零副作用：意图 / 账本 / 注册表均不变
    assert _read_settings(env) == before
    assert "alpha@mp-b" not in load_installed_plugins(home=home)["plugins"]
    after = reg.resolve("alpha:foo")
    assert after is not None
    assert after["source"] == "marketplace:mp-a"
    assert after["description"] == "from-a"


async def test_reinstall_same_pid_still_idempotent(tmp_path: Path) -> None:
    """负对照：同一 pid 重装幂等放行，意图只留一条。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    _setup_marketplace(home, "mp-a", "alpha", skill="foo", description="from-a")
    reg = SkillRegistry()

    await install_plugin("alpha@mp-a", reg, home, env=env)
    record = await install_plugin("alpha@mp-a", reg, home, env=env)

    assert record["scope"] == "user"
    assert _read_settings(env)["installedPlugins"] == ["alpha@mp-a"]
    assert list(load_installed_plugins(home=home)["plugins"]) == ["alpha@mp-a"]


async def test_legacy_ledger_blocks_before_migration_marker(tmp_path: Path) -> None:
    """legacy 账本（``bundledMcpServers``）也参与冲突扫描，且拒绝先于迁移写标记键。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    plugin_root = _setup_marketplace(home, "mp-b", "alpha", skill="foo", description="from-b")
    # v0.2.x 账本：带 bundledMcpServers 的旧格式记录（drop_legacy=True 会整条丢弃）
    _seed_ledger(
        home,
        {"alpha@mp-a": [{"scope": "user", "installPath": str(plugin_root), "bundledMcpServers": ["figma"]}]},
    )
    assert "installedPlugins" not in _read_settings(env)  # 迁移尚未发生

    with pytest.raises(PluginInstallError) as ei:
        await install_plugin("alpha@mp-b", SkillRegistry(), home, env=env)

    assert ei.value.details["conflictWith"] == "alpha@mp-a"
    assert "installedPlugins" not in _read_settings(env)  # 拒绝先于 migrate_legacy_installs 写标记键


async def test_project_scope_intent_participates_in_conflict(tmp_path: Path) -> None:
    """merged 视图：project 层 ``installedPlugins`` 声明同样能挡住 user 层安装。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    _setup_marketplace(home, "mp-b", "alpha", skill="foo", description="from-b")
    _write_json(workdir_project_settings_path(tmp_path), {"installedPlugins": ["alpha@mp-a"]})

    with pytest.raises(PluginInstallError) as ei:
        await install_plugin("alpha@mp-b", SkillRegistry(), home, env=env)

    assert ei.value.details["conflictWith"] == "alpha@mp-a"


# ── enable 防御：存量双装 fail-closed + 零副作用 ──────────────────────────────
@pytest.mark.parametrize("target", ["alpha@mp-a", "alpha@mp-b"])
async def test_enable_rejected_before_side_effects_in_double_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """存量双装：enable 任一 pid 都被拒（fail-closed），enabledPlugins / stage / 挂载零副作用。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root_a = _setup_marketplace(home, "mp-a", "alpha", skill="foo", description="from-a")
    root_b = _setup_marketplace(home, "mp-b", "alpha", skill="foo", description="from-b")
    _write_json(
        user_settings_path(env),
        {"installedPlugins": ["alpha@mp-a", "alpha@mp-b"], "enabledPlugins": {"alpha@mp-a": True}},
    )
    _seed_ledger(home, {"alpha@mp-a": [_record(root_a)], "alpha@mp-b": [_record(root_b)]})
    before = _read_settings(env)

    staged: list[dict] = []

    async def _spy_stage(*args, **kwargs):
        staged.append({"args": args, "kwargs": kwargs})
        return []

    monkeypatch.setattr("a2c_smcp.computer.settings.installer.stage_marketplace_skills", _spy_stage)
    mounted: list[object] = []

    async def _register(cfg) -> None:
        mounted.append(cfg)

    reg = SkillRegistry()
    with pytest.raises(PluginInstallError) as ei:
        await enable_plugin(
            target,
            reg,
            home,
            env=env,
            existing_bundle_ids=lambda: set(),
            register_server=_register,
        )

    assert ei.value.error_code == "validation"
    assert staged == []
    assert mounted == []
    assert reg.active_refs() == []
    assert _read_settings(env) == before  # enabledPlugins 未被写


# ── 存量双装 boot 重扫：注册点保留先到者 ─────────────────────────────────────
async def test_boot_rescan_double_install_keeps_first_registrant(
    tmp_path: Path, capture_registry_errors: pytest.LogCaptureFixture,
) -> None:
    """存量双装（两意图 + 两 enabled + 两账本）boot 重扫：先注册者 mp-a 胜出，mp-b 被拒并记 ERROR。"""
    home = _home(tmp_path)
    env = _env(tmp_path)
    root_a = _setup_marketplace(home, "mp-a", "alpha", skill="foo", description="from-a")
    root_b = _setup_marketplace(home, "mp-b", "alpha", skill="foo", description="from-b")
    _write_json(
        user_settings_path(env),
        {"installedPlugins": ["alpha@mp-a", "alpha@mp-b"], "enabledPlugins": {"alpha@mp-a": True, "alpha@mp-b": True}},
    )
    _seed_ledger(home, {"alpha@mp-a": [_record(root_a)], "alpha@mp-b": [_record(root_b)]})
    declared = resolve_settings(env=env).settings

    reg = SkillRegistry()
    await recover_marketplace_skills(reg, home, declared, env=env)

    got = reg.resolve("alpha:foo")
    assert got is not None
    assert got["source"] == "marketplace:mp-a"  # 排序先到的 mp-a 胜出
    assert got["description"] == "from-a"
    messages = [r.getMessage() for r in capture_registry_errors.records if r.levelno == logging.ERROR]
    assert any("alpha@mp-b" in m and "plugin uninstall" in m for m in messages), messages

    # 幂等：重扫不改变结果
    await recover_marketplace_skills(reg, home, declared, env=env)
    again = reg.resolve("alpha:foo")
    assert again is not None
    assert again["source"] == "marketplace:mp-a"
