# -*- coding: utf-8 -*-
# filename: test_marketplace_migration.py
# @Time    : 2026/11/22
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
存量 marketplace 注册名迁移（#268 / D3）单元测试 / Legacy marketplace name migration unit tests.

设计依据 / Design: ``docs/design-0.5.1-marketplace-name-migration.md``（P0 已按 2026-11-22 裁决回填）。

夹具纪律（§12.1）：**不用 ``marketplace add`` 构造 legacy**（#260 后 add 取清单名、``--name`` 仅期望
校验）——直接构造 pre-#260 的 ``known_marketplaces.json`` + clone 清单 + settings + 账本 + value/keyring。

覆盖：dry-run 零写入 / strict loader 损坏不 rename / 冲突整体 abort 零写入 / keyring 按项 abort 零写入
+ exit 3 / 目标键异值 abort / 幂等重跑 / project-local 字节不变 / §7.4 两个崩溃窗口 / baseline 重置与
拒绝 / 未安装无根 plugin 不 abort / `--name` 过滤与冲突阻塞范围 / C4 源 pid 排除 / `info` 字段脱敏。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.cli.commands.marketplace import marketplace_info, marketplace_migrate
from a2c_smcp.computer.inputs.value_store import resolve_value_store_path
from a2c_smcp.computer.settings import marketplace_migration as mm
from a2c_smcp.computer.settings import store
from a2c_smcp.computer.settings.raw import read_raw_object
from a2c_smcp.computer.settings.scope import (
    user_settings_path,
    workdir_local_settings_path,
    workdir_project_settings_path,
)
from a2c_smcp.computer.skills.home import marketplace_skill_dir

_MISSING = object()


# ── 夹具构造 / Fixtures ───────────────────────────────────────────────────────
def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "A2C_SKILL_HOME": str(tmp_path / "skill-home"),
    }


def _home(env: dict[str, str]) -> Path:
    return Path(env["A2C_SKILL_HOME"])


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest(name: str, plugins: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "owner": {"name": "Test"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": plugins if plugins is not None else [{"name": "figma", "source": "figma"}],
    }


def _catalog(
    home: Path,
    name: str,
    manifest: dict[str, Any],
    *,
    inputs: dict[str, list[dict[str, Any]]] | None = None,
) -> Path:
    root = marketplace_skill_dir(home, name)
    _write_json(root / ".tfrobot-plugin" / "marketplace.json", manifest)
    for plugin, defs in (inputs or {}).items():
        _write_json(root / "plugins" / plugin / "mcp-servers" / "inputs.json", {"inputs": defs})
    return root


def _external_tree(home: Path, marketplace: str, plugin: str, defs: list[dict[str, Any]] | None) -> Path:
    root = home / "marketplace" / ".plugins" / marketplace / plugin
    root.mkdir(parents=True, exist_ok=True)
    if defs is not None:
        _write_json(root / "mcp-servers" / "inputs.json", {"inputs": defs})
    return root


def _write_known(home: Path, env: dict[str, str], marketplaces: dict[str, Any]) -> None:
    _write_json(store.known_marketplaces_path(home, env), {"version": 1, "marketplaces": marketplaces})


def _known_source(url: str = "https://example.com/acme.git") -> dict[str, Any]:
    return {"type": "git", "url": url}


def _record(home: Path, old: str, plugin: str, new: str | None = None) -> dict[str, Any]:
    base = marketplace_skill_dir(home, old)
    if new is not None:
        base = marketplace_skill_dir(home, new)
    return {
        "scope": "user",
        "installPath": str(base / "plugins" / plugin),
        "version": "1.0.0",
        "installedAt": "2026-01-01T00:00:00Z",
        "lastUpdated": "2026-01-01T00:00:00Z",
        "mcpServers": [],
    }


def _write_ledger(home: Path, env: dict[str, str], plugins: dict[str, list[dict[str, Any]]]) -> None:
    _write_json(store.installed_plugins_path(home, env), {"version": 1, "plugins": plugins})


def _write_user(env: dict[str, str], data: dict[str, Any]) -> None:
    _write_json(user_settings_path(env), data)


def _write_values(env: dict[str, str], data: dict[str, Any]) -> None:
    _write_json(resolve_value_store_path(env), data)


def _read_values(env: dict[str, str]) -> dict[str, Any]:
    return _read_json(resolve_value_store_path(env))


def _user(env: dict[str, str]) -> dict[str, Any]:
    return read_raw_object(user_settings_path(env)) or {}


def _snapshot(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        out[rel] = path.read_bytes() if path.is_file() else None
    return out


def _assert_unchanged(before: dict[str, bytes | None], root: Path) -> None:
    assert _snapshot(root) == before


class _FakeSecrets:
    """内存版 keyring（monkeypatch 注入）。"""

    def __init__(self, data: dict[str, str] | None = None, *, available: bool = True) -> None:
        self.data = dict(data or {})
        self.available = available
        self.ops: list[tuple[str, str]] = []

    def get(self, key: str) -> str | None:
        return self.data.get(key) if self.available else None

    def set(self, key: str, value: str) -> bool:
        if not self.available:
            return False
        self.ops.append(("set", key))
        self.data[key] = value
        return True

    def delete(self, key: str) -> bool:
        self.ops.append(("delete", key))
        return self.data.pop(key, None) is not None


def _patch_keyring(monkeypatch: pytest.MonkeyPatch, fake: _FakeSecrets) -> None:
    monkeypatch.setattr(mm, "keyring_available", lambda: fake.available)
    monkeypatch.setattr(mm, "SecretStore", lambda: fake)


def _legacy_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    old: str = "acme",
    new: str = "acme-skills",
    baseline: object = _MISSING,
    with_settings: bool = True,
    with_ledger: bool = True,
    secrets: dict[str, str] | None = None,
    available: bool = True,
    values: dict[str, Any] | None = None,
    inputs: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, str], Path, Path, _FakeSecrets]:
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir(parents=True)

    manifest = _manifest(new)
    _catalog(home, old, manifest, inputs=inputs if inputs is not None else {"figma": [{"id": "region", "description": "r"}]})
    record: dict[str, Any] = {
        "source": _known_source(),
        "installLocation": str(marketplace_skill_dir(home, old)),
        "commitSha": "abc123",
        "lastUpdated": "2026-01-01T00:00:00Z",
    }
    if baseline is not _MISSING:
        record["manifestNameBaseline"] = baseline
    _write_known(home, env, {old: record})
    if with_ledger:
        _write_ledger(home, env, {f"figma@{old}": [_record(home, old, "figma")]})
    if with_settings:
        _write_user(
            env,
            {
                "installedPlugins": [f"figma@{old}"],
                "enabledPlugins": {f"figma@{old}": True},
                "trustedMarketplaces": [old],
                "blockedMarketplaces": [old],
            },
        )
    if values:
        _write_values(env, values)
    fake = _FakeSecrets(secrets, available=available)
    _patch_keyring(monkeypatch, fake)
    return env, home, work, fake


def _apply(plan: mm.MigrationPlan, home: Path, env: dict[str, str]) -> mm.MigrationPlan:
    return asyncio.run(mm.execute_plan(plan, home, env))


# ── dry-run / strict loader ──────────────────────────────────────────────────
def test_dry_run_is_zero_write_and_plans_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)

    assert [(item.old, item.new, item.action) for item in plan.items] == [("acme", "acme-skills", mm.ACTION_RENAME)]
    assert plan.conflicts == [] and plan.corrupt == []
    assert mm.plan_exit_code(plan) == mm.EXIT_OK
    _assert_unchanged(before, tmp_path)


def test_strict_loader_corrupt_never_renames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    corrupt = store.known_marketplaces_path(home, env)
    corrupt.write_text("{not json", encoding="utf-8")
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)

    assert plan.corrupt and str(corrupt) == plan.corrupt[0]["path"]
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)
    assert not list(home.glob("*.corrupt-*.bak"))


def test_apply_with_corrupt_state_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, _work, _fake = _legacy_setup(tmp_path, monkeypatch)
    store.known_marketplaces_path(home, env).write_text("[]", encoding="utf-8")
    before = _snapshot(tmp_path)

    code = asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True))

    assert code == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)


# ── 全量迁移 ────────────────────────────────────────────────────────────────
def test_apply_migrates_authority_and_derived_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        values={"figma@acme/region": "eu"},
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "region", "description": "r"}, {"id": "token", "description": "t", "password": True}]},
    )
    user = _user(env)
    user["extraKnownMarketplaces"] = {"acme": {"source": _known_source()}}
    _write_user(env, user)
    project = workdir_project_settings_path(work)
    _write_json(project, {"enabledPlugins": {"figma@acme": True}})
    project_bytes = project.read_bytes()

    plan = mm.build_plan(home, env, cwd=work, update_declarations=True)
    assert [item.action for item in plan.items] == [mm.ACTION_RENAME]
    _apply(plan, home, env)

    # 派生面：目录 / known / 账本
    assert not marketplace_skill_dir(home, "acme").exists()
    assert (marketplace_skill_dir(home, "acme-skills") / ".tfrobot-plugin" / "marketplace.json").is_file()
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    assert set(known) == {"acme-skills"}
    assert known["acme-skills"]["manifestNameBaseline"] == "acme-skills"
    assert known["acme-skills"]["installLocation"] == str(marketplace_skill_dir(home, "acme-skills").resolve())
    ledger = store.load_installed_plugins(home, env)["plugins"]
    assert "figma@acme-skills" in ledger and "figma@acme" not in ledger

    # 权威面：settings / value / secret
    settings = _user(env)
    assert settings["installedPlugins"] == ["figma@acme-skills"]
    assert settings["enabledPlugins"] == {"figma@acme-skills": True}
    assert settings["trustedMarketplaces"] == ["acme-skills"]
    assert settings["blockedMarketplaces"] == ["acme-skills"]
    assert list(settings["extraKnownMarketplaces"]) == ["acme-skills"]
    assert _read_values(env) == {"figma@acme-skills/region": "eu"}
    assert fake.data == {"figma@acme-skills/token": "s3cret"}

    # project/local 只报告：文件字节不变 + follow-up
    assert project.read_bytes() == project_bytes
    item = plan.items[0]
    assert any(follow["file"] == str(project) for follow in item.follow_ups)
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_conflict_aborts_command_with_zero_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _catalog(home, "acme", _manifest("shared"))
    _catalog(home, "other", _manifest("shared"))
    _write_known(
        home,
        env,
        {
            "acme": {"source": _known_source("https://example.com/a.git"), "installLocation": str(marketplace_skill_dir(home, "acme"))},
            "other": {"source": _known_source("https://example.com/b.git"), "installLocation": str(marketplace_skill_dir(home, "other"))},
        },
    )
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    _write_ledger(home, env, {"figma@acme": [_record(home, "acme", "figma")]})
    _patch_keyring(monkeypatch, _FakeSecrets())
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    assert [conflict["code"] for conflict in plan.conflicts] == ["duplicate-target"]
    assert mm.plan_exit_code(plan) == mm.EXIT_CONFLICT

    code = asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True))
    assert code == mm.EXIT_CONFLICT
    _assert_unchanged(before, tmp_path)


def test_keyring_unavailable_aborts_item_with_zero_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        available=False,
        inputs={"figma": [{"id": "token", "description": "t", "password": True}]},
    )
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_KEYRING_UNAVAILABLE
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL

    code = asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True))
    assert code == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)


def test_keyring_unavailable_without_password_inputs_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        available=False,
        inputs={"figma": [{"id": "region", "description": "r"}]},
    )
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    _apply(plan, home, env)
    assert "acme-skills" in store.load_known_marketplaces(home, env)["marketplaces"]


def test_uninstalled_rootless_external_plugin_does_not_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """架构师钉住项：从未安装且无根的外源 plugin 不计 secret 候选，不导致 abort。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, inputs={"figma": [{"id": "region", "description": "r"}]})
    manifest = _manifest("acme-skills", plugins=[
        {"name": "figma", "source": "figma"},
        {"name": "remote", "source": {"source": "url", "url": "https://example.com/remote.git"}},
    ])
    _catalog(home, "acme", manifest, inputs={"figma": [{"id": "region", "description": "r"}]})

    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    assert plan.items[0].reason is None


def test_secret_and_value_target_conflicts_abort_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        values={"figma@acme/region": "eu", "figma@acme-skills/region": "us"},
        secrets={"figma@acme/token": "old"},
        inputs={"figma": [{"id": "region", "description": "r"}, {"id": "token", "description": "t", "password": True}]},
    )
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_BLOCKED
    assert plan.items[0].reason == mm.REASON_VALUE_TARGET_CONFLICT
    _assert_unchanged(before, tmp_path)

    # 清掉 value 冲突后暴露 secret 冲突。
    _write_values(env, {"figma@acme/region": "eu"})
    fake = _FakeSecrets({"figma@acme/token": "old", "figma@acme-skills/token": "different"})
    _patch_keyring(monkeypatch, fake)
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_BLOCKED
    assert plan.items[0].reason == mm.REASON_SECRET_TARGET_CONFLICT


def test_idempotent_rerun_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    before = _snapshot(tmp_path)
    replan = mm.build_plan(home, env, cwd=work)
    assert all(item.action in {mm.ACTION_NOOP, mm.ACTION_NOT_SELECTED} for item in replan.items)
    _assert_unchanged(before, tmp_path)
    assert mm.plan_exit_code(replan) == mm.EXIT_OK


# ── 崩溃恢复窗口（§7.3 / §7.4）──────────────────────────────────────────────
def test_claim_window_renames_old_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    # 模拟 P2 中断：known[new] 已写（同源），目录还叫旧名。
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
        "manifestNameBaseline": "acme-skills",
    }
    _write_known(home, env, known)
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    _apply(plan, home, env)

    assert marketplace_skill_dir(home, "acme-skills").is_dir()
    assert not marketplace_skill_dir(home, "acme").exists()
    assert set(store.load_known_marketplaces(home, env)["marketplaces"]) == {"acme-skills"}


def test_already_renamed_window_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    # 模拟 P2 中断：目录已搬到新名、known[old] 未删。
    marketplace_skill_dir(home, "acme").rename(marketplace_skill_dir(home, "acme-skills"))
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
        "manifestNameBaseline": "acme-skills",
    }
    _write_known(home, env, known)

    plan = mm.build_plan(home, env, cwd=work)
    item = next(i for i in plan.items if i.old == "acme")
    assert item.action == mm.ACTION_RENAME and item.already_renamed
    _apply(plan, home, env)

    assert set(store.load_known_marketplaces(home, env)["marketplaces"]) == {"acme-skills"}
    assert _user(env)["installedPlugins"] == ["figma@acme-skills"]


def test_orphan_directory_is_not_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    # 旧目录消失、只剩一个名字不在 known 里、清单名为目标名的孤儿目录。
    marketplace_skill_dir(home, "acme").rename(home / "marketplace" / "stray")
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_UNRESOLVED and item.reason == mm.REASON_CATALOG_MISSING
    assert (home / "marketplace" / "stray").is_dir()
    _assert_unchanged(before, tmp_path)


# ── baseline ────────────────────────────────────────────────────────────────
def test_baseline_mismatch_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, baseline="acme")
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_BASELINE_MISMATCH
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)


def test_new_known_record_resets_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 旧记录**没有** baseline（pre-#260 legacy）：新记录必须补写为 new，而不是沿用/省略。
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, baseline=_MISSING)
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    record = store.load_known_marketplaces(home, env)["marketplaces"]["acme-skills"]
    assert record["manifestNameBaseline"] == "acme-skills"
    assert record["commitSha"] == "abc123"


# ── 声明面 / 过滤 / C4 ─────────────────────────────────────────────────────
def test_update_declarations_only_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    user = _user(env)
    user["extraKnownMarketplaces"] = {"acme": {"source": _known_source()}}
    _write_user(env, user)
    project = workdir_project_settings_path(work)
    _write_json(project, {"extraKnownMarketplaces": {"acme": {"source": _known_source()}}})
    project_bytes = project.read_bytes()

    plan = mm.build_plan(home, env, cwd=work, update_declarations=True)
    _apply(plan, home, env)

    assert list(_user(env)["extraKnownMarketplaces"]) == ["acme-skills"]
    assert project.read_bytes() == project_bytes
    assert any(follow["effect"] == mm.EFFECT_DECLARATION_RESURRECT for follow in plan.items[0].follow_ups)


def test_declarations_default_report_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    user = _user(env)
    user["extraKnownMarketplaces"] = {"acme": {"source": _known_source()}}
    _write_user(env, user)

    plan = mm.build_plan(home, env, cwd=work)
    assert list(_user(env)["extraKnownMarketplaces"]) == ["acme"]
    assert any(follow["effect"] == mm.EFFECT_DECLARATION_RESURRECT for follow in plan.items[0].follow_ups)


def test_name_filter_does_not_block_on_unselected_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _catalog(home, "aa", _manifest("shared"))
    _catalog(home, "bb", _manifest("shared"))
    _catalog(home, "cc", _manifest("cc-new"))
    _write_known(
        home,
        env,
        {
            "aa": {"source": _known_source("https://example.com/a.git"), "installLocation": str(marketplace_skill_dir(home, "aa"))},
            "bb": {"source": _known_source("https://example.com/b.git"), "installLocation": str(marketplace_skill_dir(home, "bb"))},
            "cc": {"source": _known_source("https://example.com/c.git"), "installLocation": str(marketplace_skill_dir(home, "cc"))},
        },
    )
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    _patch_keyring(monkeypatch, _FakeSecrets())

    plan = mm.build_plan(home, env, cwd=work, names=["cc"])
    assert mm.plan_exit_code(plan) == mm.EXIT_OK
    _apply(plan, home, env)
    assert "cc-new" in store.load_known_marketplaces(home, env)["marketplaces"]
    assert "aa" in store.load_known_marketplaces(home, env)["marketplaces"]

    blocked = mm.build_plan(home, env, cwd=work, names=["aa"])
    assert mm.plan_exit_code(blocked) == mm.EXIT_CONFLICT
    other = next(item for item in blocked.items if item.old == "bb")
    assert other.selected is False and other.action == mm.ACTION_NOT_SELECTED


def test_c4_source_pid_excluded_but_third_party_clash_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_settings=False, with_ledger=False)
    _write_ledger(home, env, {"figma@acme": [_record(home, "acme", "figma")]})
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.conflicts == []

    fresh_env, fresh_home, fresh_work, _fake2 = _legacy_setup(tmp_path / "second", monkeypatch, with_settings=False, with_ledger=False)
    other_root = fresh_home / "marketplace" / "other" / "plugins" / "figma"
    _write_ledger(
        fresh_home,
        fresh_env,
        {
            "figma@acme": [_record(fresh_home, "acme", "figma")],
            "figma@other": [{"scope": "user", "installPath": str(other_root), "mcpServers": []}],
        },
    )
    _write_user(fresh_env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    clash = mm.build_plan(fresh_home, fresh_env, cwd=fresh_work)
    assert [conflict["code"] for conflict in clash.conflicts] == ["bare-name-clash"]
    assert mm.plan_exit_code(clash) == mm.EXIT_CONFLICT


# ── 账本保留 / manifest 异常 ─────────────────────────────────────────────────
def test_plugin_missing_from_new_manifest_keeps_old_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    (marketplace_skill_dir(home, "acme") / "plugins" / "ghost").mkdir(parents=True)
    _write_ledger(home, env, {"ghost@acme": [_record(home, "acme", "ghost")]})
    _write_user(env, {"installedPlugins": ["ghost@acme"], "enabledPlugins": {"ghost@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "ghost@acme" in ledger and "ghost@acme-skills" not in ledger
    assert any("not present in the new manifest" in warning for warning in plan.items[0].warnings)
    assert plan.items[0].incomplete is True
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL  # 不静默报全绿


def test_invalid_manifest_name_is_unresolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _catalog(home, "acme", _manifest("Bad Name"))
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_UNRESOLVED
    assert plan.items[0].reason == mm.REASON_INVALID_MANIFEST_NAME
    _assert_unchanged(before, tmp_path)


# ── info 展示（§10.4）──────────────────────────────────────────────────────
def test_info_reports_manifest_name_and_legacy_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env, home, _work, _fake = _legacy_setup(tmp_path, monkeypatch)
    assert marketplace_info(home, env, "acme", json_output=True) == mm.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifestName"] == "acme-skills"
    assert payload["legacyName"] is True
    assert payload["manifestNameInvalid"] is False


def test_info_redacts_invalid_manifest_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env, home, _work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _catalog(home, "acme", _manifest("https://alice:s3cret@example.com/x.git"))
    assert marketplace_info(home, env, "acme", json_output=True) == mm.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifestName"] is None
    assert payload["manifestNameInvalid"] is True
    assert payload["legacyName"] is False
    assert "s3cret" not in json.dumps(payload)


def test_external_tree_is_renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    manifest = _manifest("acme-skills", plugins=[{"name": "remote", "source": {"source": "url", "url": "https://example.com/r.git"}}])
    _catalog(home, "acme", manifest)
    ext_root = _external_tree(home, "acme", "remote", [{"id": "region", "description": "r"}])
    _write_ledger(
        home,
        env,
        {
            "remote@acme": [
                {"scope": "user", "installPath": str(ext_root), "installedAt": "t", "lastUpdated": "t", "mcpServers": []}
            ]
        },
    )
    _write_user(env, {"installedPlugins": ["remote@acme"], "enabledPlugins": {"remote@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    assert not (home / "marketplace" / ".plugins" / "acme").exists()
    assert (home / "marketplace" / ".plugins" / "acme-skills" / "remote").is_dir()
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "remote@acme-skills" in ledger and "remote@acme" not in ledger
    assert ledger["remote@acme-skills"][0]["installPath"] == str(
        home / "marketplace" / ".plugins" / "acme-skills" / "remote"
    )


def test_name_taken_by_other_source_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source("https://example.com/DIFFERENT.git"),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
    }
    _write_known(home, env, known)

    plan = mm.build_plan(home, env, cwd=work)
    assert [conflict["code"] for conflict in plan.conflicts] == ["name-taken-by-other-source"]
    assert mm.plan_exit_code(plan) == mm.EXIT_CONFLICT


def test_enabled_state_conflict_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _write_user(
        env,
        {
            "installedPlugins": ["figma@acme"],
            "enabledPlugins": {"figma@acme": True, "figma@acme-skills": False},
        },
    )
    plan = mm.build_plan(home, env, cwd=work)
    assert [conflict["code"] for conflict in plan.conflicts] == ["enabled-state-conflict"]
    assert mm.plan_exit_code(plan) == mm.EXIT_CONFLICT


def test_target_external_tree_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _external_tree(home, "acme-skills", "leftover", None)
    plan = mm.build_plan(home, env, cwd=work)
    assert "target-external-tree-exists" in [conflict["code"] for conflict in plan.conflicts]


@pytest.mark.parametrize("which", ["installed", "user", "values"])
def test_strict_loader_covers_all_state_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    if which == "installed":
        store.installed_plugins_path(home, env).write_text("{broken", encoding="utf-8")
    elif which == "user":
        user_settings_path(env).write_text("[]", encoding="utf-8")
    else:
        resolve_value_store_path(env).parent.mkdir(parents=True, exist_ok=True)
        resolve_value_store_path(env).write_text("{broken", encoding="utf-8")
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    assert plan.corrupt
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)
    assert not list(home.glob("*.corrupt-*.bak"))


def test_cli_json_payload_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    assert asyncio.run(marketplace_migrate(home, env, json_output=True)) == mm.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "marketplace migrate"
    assert payload["dryRun"] is True and payload["status"] == "planned" and payload["exitCode"] == 0
    assert payload["items"][0]["old"] == "acme" and payload["items"][0]["action"] == "rename"
    assert payload["items"][0]["plugins"][0] == {
        "id": "figma@acme",
        "newId": "figma@acme-skills",
        "action": "move",
    }
    assert payload["applied"] is False

    assert asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True)) == mm.EXIT_OK
    applied = json.loads(capsys.readouterr().out)
    assert applied["dryRun"] is False and applied["status"] == "applied" and applied["applied"] is True


def test_project_trust_and_blocked_are_report_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    project = workdir_project_settings_path(work)
    _write_json(
        project,
        {
            "trustedMarketplaces": ["acme"],
            "blockedMarketplaces": ["acme"],
            "installedPlugins": ["figma@acme"],
        },
    )
    project_bytes = project.read_bytes()

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    assert project.read_bytes() == project_bytes
    effects = {follow["effect"] for follow in plan.items[0].follow_ups}
    assert {mm.EFFECT_STALE_TRUST, mm.EFFECT_BLOCKED_INVALID} <= effects
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_additive_writes_happen_before_removals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """「先加后减」：新 pid / 新启用键先写，旧键后删（崩溃窗口才不会熄灯）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    calls: list[tuple[str, str, object]] = []
    real_installed = mm._write_installed_plugin
    real_enabled = mm._write_enabled_plugin

    def spy_installed(pid: str, present: bool, env: Any, **kwargs: Any) -> bool:
        calls.append(("installed", pid, present))
        return real_installed(pid, present, env, **kwargs)

    def spy_enabled(pid: str, value: bool | None, scope: str, project_path: str | None, env: Any) -> None:
        calls.append(("enabled", pid, value))
        return real_enabled(pid, value, scope, project_path, env)

    monkeypatch.setattr(mm, "_write_installed_plugin", spy_installed)
    monkeypatch.setattr(mm, "_write_enabled_plugin", spy_enabled)
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    assert calls.index(("installed", "figma@acme-skills", True)) < calls.index(("installed", "figma@acme", False))
    assert calls.index(("enabled", "figma@acme-skills", True)) < calls.index(("enabled", "figma@acme", None))


def test_new_secret_is_written_before_old_is_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """「先写新 secret 再删旧」：删除旧键前必须已 set + 回读新键（secret 丢了不可恢复）。"""
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "token", "description": "t", "password": True}]},
    )
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    set_idx = fake.ops.index(("set", "figma@acme-skills/token"))
    delete_idx = fake.ops.index(("delete", "figma@acme/token"))
    assert set_idx < delete_idx


# ── 审查整改回归（2026-11-22 独立审查）────────────────────────────────────
def test_external_tree_already_renamed_window_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口 4：目录与外树都已搬到新名、known[old] 未删 → 预检必须找到新名下的外树（不得误 blocked）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    manifest = _manifest(
        "acme-skills",
        plugins=[{"name": "remote", "source": {"source": "url", "url": "https://example.com/r.git"}}],
    )
    _catalog(home, "acme", manifest)
    old_ext = _external_tree(home, "acme", "remote", [{"id": "region", "description": "r"}])
    record = {"scope": "user", "installPath": str(old_ext), "installedAt": "t", "lastUpdated": "t", "mcpServers": []}
    _write_ledger(home, env, {"remote@acme": [record]})
    _write_user(env, {"installedPlugins": ["remote@acme"], "enabledPlugins": {"remote@acme": True}})

    marketplace_skill_dir(home, "acme").rename(marketplace_skill_dir(home, "acme-skills"))
    old_ext.parent.rename(home / "marketplace" / ".plugins" / "acme-skills")
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
        "manifestNameBaseline": "acme-skills",
    }
    _write_known(home, env, known)

    plan = mm.build_plan(home, env, cwd=work)
    item = next(i for i in plan.items if i.old == "acme")
    assert item.already_renamed and item.action == mm.ACTION_RENAME
    _apply(plan, home, env)

    assert item.action == mm.ACTION_RENAME, item.warnings
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "remote@acme-skills" in ledger and "remote@acme" not in ledger
    assert (home / "marketplace" / ".plugins" / "acme-skills" / "remote").is_dir()


def test_p4_failure_keeps_the_only_secret_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P3 已删旧 secret 后 P4 失败：清理不得删掉新键（否则两份都没了）。"""
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "token", "description": "t", "password": True}]},
    )

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mm, "_drop_old_ledger", boom)
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    assert plan.items[0].action == mm.ACTION_ABORTED
    assert fake.data == {"figma@acme-skills/token": "s3cret"}, fake.data


def test_p2_failure_cleans_new_secret_and_keeps_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P3 之前失败：旧键仍在 → 清理本次新键，旧值不丢。"""
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "token", "description": "t", "password": True}]},
    )

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(mm, "_rename_catalog", boom)
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    assert plan.items[0].action == mm.ACTION_ABORTED
    assert fake.data == {"figma@acme/token": "s3cret"}, fake.data


def test_p3_failure_window_resumes_on_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """known[old] 必须留到 P3/P4 之后：P3 中断后重跑要能续做而不是报 noop。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    real_installed = mm._write_installed_plugin
    state = {"failed": False}

    def flaky(pid: str, present: bool, env: Any, **kwargs: Any) -> bool:
        if not present and pid == "figma@acme" and not state["failed"]:
            state["failed"] = True
            raise OSError("settings write failed")
        return real_installed(pid, present, env, **kwargs)

    monkeypatch.setattr(mm, "_write_installed_plugin", flaky)
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    assert plan.items[0].action == mm.ACTION_ABORTED
    assert "acme" in store.load_known_marketplaces(home, env)["marketplaces"]

    monkeypatch.setattr(mm, "_write_installed_plugin", real_installed)
    replan = mm.build_plan(home, env, cwd=work)
    item = next(i for i in replan.items if i.old == "acme")
    assert item.already_renamed
    _apply(replan, home, env)

    assert set(store.load_known_marketplaces(home, env)["marketplaces"]) == {"acme-skills"}
    assert _user(env)["installedPlugins"] == ["figma@acme-skills"]
    assert _user(env)["enabledPlugins"] == {"figma@acme-skills": True}


def test_payload_redacts_credential_like_ledger_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--json 的自由文本必须走脱敏：手编账本键里的凭据不得进 items/conflicts。"""
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    bad = "https://alice:s3cret@example.com/x.git"
    _write_known(home, env, {bad: {"source": _known_source(), "installLocation": str(home / "marketplace" / "x")}})
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    _patch_keyring(monkeypatch, _FakeSecrets())
    plan = mm.build_plan(home, env, cwd=work)
    payload = mm.plan_payload(plan)
    dumped = json.dumps(payload)
    assert "s3cret" not in dumped
    assert payload["items"][0]["old"] != bad


def test_merge_with_both_external_trees_is_a_plan_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """merge 场景下两棵外树并存 → 计划期冲突（避免 dry-run 0 / apply 3 的永久 wedge）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    manifest = _manifest(
        "acme-skills",
        plugins=[{"name": "remote", "source": {"source": "url", "url": "https://example.com/r.git"}}],
    )
    _catalog(home, "acme", manifest)
    _catalog(home, "acme-skills", manifest)
    _external_tree(home, "acme", "remote", None)
    _external_tree(home, "acme-skills", "remote", None)
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
    }
    _write_known(home, env, known)
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})

    plan = mm.build_plan(home, env, cwd=work)
    assert "target-external-tree-exists" in [conflict["code"] for conflict in plan.conflicts]
    assert mm.plan_exit_code(plan) == mm.EXIT_CONFLICT


def test_strict_loader_rejects_wrong_shaped_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _write_json(store.known_marketplaces_path(home, env), {"version": 1, "marketplaces": []})
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.corrupt
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)


def test_malformed_enabled_plugins_is_structured_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    # 全合规项（清单名 == 注册名），只让 user settings 的 enabledPlugins 畸形。
    _catalog(home, "acme", _manifest("acme"))
    _write_user(env, {"enabledPlugins": None})
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.corrupt
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_non_mapping_declaration_left_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    user = _user(env)
    user["extraKnownMarketplaces"] = {"acme": "https://example.com/acme.git"}
    _write_user(env, user)
    plan = mm.build_plan(home, env, cwd=work, update_declarations=True)
    assert plan.items[0].declarations_rewrite is False
    _apply(plan, home, env)
    assert _user(env)["extraKnownMarketplaces"] == {"acme": "https://example.com/acme.git"}


def test_non_mapping_target_declaration_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    user = _user(env)
    user["extraKnownMarketplaces"] = {"acme": {"source": _known_source()}, "acme-skills": "shorthand"}
    _write_user(env, user)
    plan = mm.build_plan(home, env, cwd=work, update_declarations=True)
    assert "extra-known-target-conflict" in [conflict["code"] for conflict in plan.conflicts]
    assert mm.plan_exit_code(plan) == mm.EXIT_CONFLICT


def test_claim_ignores_staging_and_trash_temp_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    staging = home / "marketplace" / ".staging-deadbeef"
    _catalog(home, ".staging-deadbeef", _manifest("acme-skills"))
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    assert staging.is_dir(), "staging 临时树不得被认领/搬走"
    assert marketplace_skill_dir(home, "acme-skills").is_dir()


def test_rebuild_never_clones_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sha 锁版本 + 本地 HEAD 不符：迁移必须跳过重建（保留旧账本 + exit 3），绝不 rmtree/重 clone。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    manifest = _manifest(
        "acme-skills",
        plugins=[{"name": "remote", "source": {"source": "url", "url": "file:///nonexistent/r.git", "sha": "a" * 40}}],
    )
    _catalog(home, "acme", manifest)
    ext = _external_tree(home, "acme", "remote", None)
    _write_ledger(
        home,
        env,
        {"remote@acme": [{"scope": "user", "installPath": str(ext), "installedAt": "t", "lastUpdated": "t", "mcpServers": []}]},
    )
    _write_user(env, {"installedPlugins": ["remote@acme"], "enabledPlugins": {"remote@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    moved = home / "marketplace" / ".plugins" / "acme-skills" / "remote"
    assert moved.is_dir(), "外树不得因重建失败被删除"
    assert plan.items[0].incomplete is True
    assert plan.items[0].reason == mm.REASON_LEDGER_REBUILD_FAILED
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_trust_writes_are_additive_before_old_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """trust/blocked/声明与 value store：P1 先加、P3 后删（不能合并成一次写）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, values={"figma@acme/region": "eu"})
    user = _user(env)
    user["extraKnownMarketplaces"] = {"acme": {"source": _known_source()}}
    _write_user(env, user)

    settings_writes: list[dict[str, Any]] = []
    real_write = store.atomic_write_json

    def spy_write(path: Path, obj: Any, **kwargs: Any) -> None:
        if str(path) == str(user_settings_path(env)):
            settings_writes.append(json.loads(json.dumps(obj)))
        return real_write(path, obj, **kwargs)

    monkeypatch.setattr(mm.store, "atomic_write_json", spy_write)

    value_writes: list[dict[str, Any]] = []
    real_replace = mm.ValueStore.replace_all

    def spy_replace(self: Any, data: Any) -> None:
        value_writes.append(dict(data))
        return real_replace(self, data)

    monkeypatch.setattr(mm.ValueStore, "replace_all", spy_replace)

    plan = mm.build_plan(home, env, cwd=work, update_declarations=True)
    _apply(plan, home, env)

    assert any(
        "acme" in write.get("trustedMarketplaces", []) and "acme-skills" in write.get("trustedMarketplaces", [])
        for write in settings_writes
    ), "P1 必须在新键就绪时仍保留旧名（避免崩溃窗口熄灯）"
    assert settings_writes[-1]["trustedMarketplaces"] == ["acme-skills"]
    assert any("acme" in write.get("extraKnownMarketplaces", {}) for write in settings_writes)

    assert len(value_writes) == 2, value_writes
    assert "figma@acme/region" in value_writes[0] and "figma@acme-skills/region" in value_writes[0]
    assert "figma@acme/region" not in value_writes[1]


# ── 第二轮复核整改回归（2026-11-22）───────────────────────────────────────
def test_removed_external_plugin_window_migrates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """外树窗口的 removed-plugin 变体：外树仍在 <old>，catalog 已搬 → 预检须能定位、apply 须补搬外树。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    initial = _manifest(
        "acme-skills",
        plugins=[{"name": "remote", "source": {"source": "url", "url": "https://example.com/r.git"}}],
    )
    _catalog(home, "acme", initial)
    ext_old = _external_tree(home, "acme", "remote", [{"id": "region", "description": "r"}])
    record = {"scope": "user", "installPath": str(ext_old), "installedAt": "t", "lastUpdated": "t", "mcpServers": []}
    _write_ledger(home, env, {"remote@acme": [record]})
    _write_user(env, {"installedPlugins": ["remote@acme"], "enabledPlugins": {"remote@acme": True}})

    # catalog 已搬、外树未搬；新清单**移除**了 remote。
    marketplace_skill_dir(home, "acme").rename(marketplace_skill_dir(home, "acme-skills"))
    _catalog(home, "acme-skills", _manifest("acme-skills", plugins=[]))
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
        "manifestNameBaseline": "acme-skills",
    }
    _write_known(home, env, known)

    plan = mm.build_plan(home, env, cwd=work)
    item = next(i for i in plan.items if i.old == "acme")
    assert item.action == mm.ACTION_RENAME and item.already_renamed
    _apply(plan, home, env)

    assert (home / "marketplace" / ".plugins" / "acme-skills" / "remote").is_dir()
    assert not (home / "marketplace" / ".plugins" / "acme").exists()
    assert set(store.load_known_marketplaces(home, env)["marketplaces"]) == {"acme-skills"}
    assert _user(env)["installedPlugins"] == ["remote@acme-skills"]
    assert item.incomplete is True  # plugin 已出清单：旧账本保留、exit 3


def test_strict_loader_rejects_null_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _write_json(store.known_marketplaces_path(home, env), {"version": 1, "marketplaces": None})
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.corrupt
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)


def test_password_string_values_are_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`password: "true"`（手编/宽松 JSON）按运行时口径归一为 True，必须重键 secret。"""
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "token", "description": "t", "password": "true"}]},
    )
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    _apply(plan, home, env)
    assert fake.data == {"figma@acme-skills/token": "s3cret"}


def test_payload_and_human_redact_corrupt_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from a2c_smcp.computer.cli.commands.marketplace import _render_migration

    plan = mm.MigrationPlan()
    plan.corrupt = [{"path": "/tmp/https://u:pw@host/x", "reason": "bad https://u:pw@host/y"}]
    payload = mm.plan_payload(plan)
    assert "pw@host" not in json.dumps(payload)
    _render_migration(plan, applied=False)
    assert "pw@host" not in capsys.readouterr().err

    item = mm.MigrationItem(old="acme", new="acme-skills", action=mm.ACTION_RENAME)
    item.warnings.append("see https://u:pw@host/z")
    plan2 = mm.MigrationPlan(items=[item])
    assert "pw@host" not in json.dumps(mm.plan_payload(plan2))
    _render_migration(plan2, applied=False)
    assert "pw@host" not in capsys.readouterr().err


def test_install_plugin_forwards_allow_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from a2c_smcp.computer.settings import installer as installer_mod

    env, home, _work, _fake = _legacy_setup(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_materialize(plugin_id: str, home_: Path, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(installer_mod, "materialize_plugin", fake_materialize)
    with pytest.raises(Exception):
        asyncio.run(installer_mod.install_plugin("figma@acme", None, home, allow_clone=False, env=env))
    assert captured.get("allow_clone") is False


def test_blocked_item_payload_has_no_execution_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, baseline="acme")
    plan = mm.build_plan(home, env, cwd=work)
    payload = mm.plan_payload(plan)
    assert payload["items"][0]["action"] == mm.ACTION_BLOCKED
    assert payload["items"][0]["actions"] == {}
    assert payload["items"][0]["plugins"] == []


def test_ambiguous_claim_is_detected_in_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _catalog(home, "stray", _manifest("acme-skills"))
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_AMBIGUOUS_CATALOG
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    code = asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True))
    assert code == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)


def test_secret_material_never_in_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        secret_pairs=[mm.SecretRekey(old_key="a", new_key="b", value="s3cret")],
    )
    item.secret_cleanup.append(mm.SecretRekey(old_key="a", new_key="b", value="s3cret"))
    assert "s3cret" not in repr(item)
    assert "s3cret" not in repr(item.secret_pairs[0])


def test_non_list_ledger_records_mark_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    _write_ledger(home, env, {"figma@acme": {"not": "a list"}})  # type: ignore[dict-item]
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    assert plan.items[0].incomplete is True
    assert plan.items[0].reason == mm.REASON_LEDGER_REBUILD_FAILED
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_help_and_completer_include_migrate() -> None:
    from a2c_smcp.computer.cli import completer
    from a2c_smcp.computer.cli.help import FLAGS, NAMESPACE_COMMANDS, SUBCOMMANDS

    assert any("marketplace migrate" in row[0] for row in NAMESPACE_COMMANDS["marketplace"])
    assert "migrate" in SUBCOMMANDS["marketplace"]
    assert FLAGS[("marketplace", "migrate")] == ["--name", "--apply", "--update-declarations", "--json"]
    assert ("marketplace", "migrate") in completer._DYNAMIC_MARKETPLACE
