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
from a2c_smcp.computer.inputs.secret_store import SecretStoreError
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

    def __init__(
        self, data: dict[str, str] | None = None, *, available: bool = True, fail_read: bool = False
    ) -> None:
        self.data = dict(data or {})
        self.available = available
        self.fail_read = fail_read
        self.ops: list[tuple[str, str]] = []

    def get(self, key: str) -> str | None:
        return self.data.get(key) if self.available else None

    def get_strict(self, key: str) -> str | None:
        if not self.available:
            raise SecretStoreError("keyring is unavailable")
        if self.fail_read:
            raise SecretStoreError("simulated backend read failure")
        return self.data.get(key)

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
    assert plan.items[0].actions["externalTrees"] == 1


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
    item = next(i for i in plan.items if i.old == "acme")

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

    # 非空 actions + blocked：serializable() 必须抑制（避免「同值致盲」，夹具本身 actions 为空也算不出）。
    manual = mm.MigrationItem(
        old="acme", new="acme-skills", action=mm.ACTION_BLOCKED, actions={"catalog": "rename"}
    )
    assert manual.serializable()["actions"] == {}


def test_ambiguous_claim_is_detected_in_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _catalog(home, "stray", _manifest("acme-skills"))
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_UNRESOLVED and item.reason == mm.REASON_AMBIGUOUS_CATALOG
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
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    # records 非数组同属静态可知的 keep 形态：计划期即预告（dry-run 与 apply 同码）
    assert plan.items[0].incomplete is True
    assert plan.items[0].reason == mm.REASON_LEDGER_REBUILD_FAILED
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)
    _apply(plan, home, env)
    assert plan.items[0].incomplete is True
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_help_and_completer_include_migrate() -> None:
    from a2c_smcp.computer.cli import completer
    from a2c_smcp.computer.cli.help import FLAGS, NAMESPACE_COMMANDS, SUBCOMMANDS

    assert any("marketplace migrate" in row[0] for row in NAMESPACE_COMMANDS["marketplace"])
    assert "migrate" in SUBCOMMANDS["marketplace"]
    assert FLAGS[("marketplace", "migrate")] == ["--name", "--apply", "--update-declarations", "--json"]
    assert ("marketplace", "migrate") in completer._DYNAMIC_MARKETPLACE


def test_actions_report_trust_and_blocked_rewrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """trust/blocked 的实际改写必须在 actions 里体现（不能是 noop）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    plan = mm.build_plan(home, env, cwd=work)
    actions = mm.plan_payload(plan)["items"][0]["actions"]
    assert actions["trustedMarketplaces"] == "add+remove"
    assert actions["blockedMarketplaces"] == "add+remove"


def test_human_table_suppresses_actions_for_non_executed_items() -> None:
    from a2c_smcp.computer.cli.commands.marketplace import _display_actions

    actions = {"catalog": "rename", "installedPlugins": 3, "trustedMarketplaces": "add+remove"}
    hidden = mm.MigrationItem(old="acme", new="acme-skills", action=mm.ACTION_NOT_SELECTED, selected=False, actions=dict(actions))
    blocked = mm.MigrationItem(old="acme", new="acme-skills", action=mm.ACTION_BLOCKED, actions=dict(actions))
    renamed = mm.MigrationItem(old="acme", new="acme-skills", action=mm.ACTION_RENAME, actions=dict(actions))
    aborted = mm.MigrationItem(old="acme", new="acme-skills", action=mm.ACTION_ABORTED, actions=dict(actions))

    unresolved = mm.MigrationItem(old="acme", new="acme-skills", action=mm.ACTION_UNRESOLVED, actions=dict(actions))
    noop = mm.MigrationItem(old="acme", action=mm.ACTION_NOOP, actions=dict(actions))
    assert _display_actions(hidden) == {}
    assert _display_actions(blocked) == {}
    assert _display_actions(unresolved) == {}
    assert _display_actions(noop) == {}
    # 正对照：实际执行面（rename / aborted）必须展示。
    assert _display_actions(renamed)["catalog"] == "rename"
    assert _display_actions(aborted)["trustedMarketplaces"] == "add+remove"


def test_unknown_name_errors_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env, home, _work, _fake = _legacy_setup(tmp_path, monkeypatch)
    secret = "https://alice:s3cret@example.com/x.git"
    assert asyncio.run(marketplace_migrate(home, env, names=[secret], json_output=True)) == mm.EXIT_CONFLICT
    out = capsys.readouterr().out
    assert "s3cret" not in out
    assert "s3cret" not in json.dumps(mm.plan_payload(mm.build_plan(home, env, names=[secret])))


def test_partially_malformed_inputs_still_rekey_passwords(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """废弃 pickString 条目（options 为旧字符串数组）可证明非密钥 → 跳过，不影响 token 重键。"""
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={
            "figma": [
                {"id": "token", "description": "t", "password": True},
                {"type": "pickString", "description": "p", "options": ["a", "b"]},
            ]
        },
    )
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    _apply(plan, home, env)
    assert fake.data == {"figma@acme-skills/token": "s3cret"}


def test_ambiguous_password_input_definition_blocks_with_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "token", "type": "promptString", "password": "true"}]},  # 缺 description
    )
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_SECRET_INPUTS_UNREADABLE
    assert any("unreadable or ambiguous input definitions" in warning for warning in item.warnings)
    _assert_unchanged(before, tmp_path)


def test_unhashable_input_type_does_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """不可信 manifest 的 `type` 可为 list/dict：必须保守 abort，绝不以 TypeError 崩掉整次迁移。"""
    env, home, work, _fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "token", "description": "t", "password": True}]},
    )
    _write_json(
        marketplace_skill_dir(home, "acme") / "plugins" / "figma" / "mcp-servers" / "inputs.json",
        {"inputs": [{"id": "tok", "type": ["promptString"]}]},
    )
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_SECRET_INPUTS_UNREADABLE
    assert any("unreadable or ambiguous input definitions" in warning for warning in item.warnings)
    assert asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True)) == mm.EXIT_PARTIAL


@pytest.mark.parametrize("raw_password", ["on", "y", 1.0])
def test_loose_password_truthiness_is_treated_as_maybe_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_password: Any
) -> None:
    """pydantic 会把 "on"/"y"/1.0 解析为 True：畸形条目不得被当成非密钥跳过。"""
    env, home, work, _fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "token", "description": "t", "password": True}]},
    )
    _write_json(
        marketplace_skill_dir(home, "acme") / "plugins" / "figma" / "mcp-servers" / "inputs.json",
        {"inputs": [{"id": "token", "type": "promptString", "password": raw_password}]},
    )
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_BLOCKED
    assert plan.items[0].reason == mm.REASON_SECRET_INPUTS_UNREADABLE
    assert any("unreadable or ambiguous input definitions" in warning for warning in plan.items[0].warnings)
    _assert_unchanged(before, tmp_path)


def test_render_migration_rows_assertion(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    from a2c_smcp.computer.cli.commands import marketplace as marketplace_cli
    from a2c_smcp.computer.cli.commands.marketplace import _render_migration

    actions = {"catalog": "rename", "installedPlugins": 1, "trustedMarketplaces": "add+remove"}
    blocked = mm.MigrationItem(
        old="aaa", new="aaa-new", action=mm.ACTION_BLOCKED, reason="baseline-mismatch", actions=dict(actions)
    )
    unresolved = mm.MigrationItem(
        old="ccc", new="ccc-new", action=mm.ACTION_UNRESOLVED, reason="ambiguous-catalog", actions=dict(actions)
    )
    renamed = mm.MigrationItem(old="bbb", new="bbb-new", action=mm.ACTION_RENAME, actions=dict(actions))
    plan = mm.MigrationPlan(items=[blocked, unresolved, renamed])
    # 不修改全局 console 的 _width（Rich getter 会把计算宽度永久钉住）；换局部实例。
    monkeypatch.setattr(marketplace_cli, "console", Console(width=220))
    _render_migration(plan, applied=False)
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "aaa" in line or "bbb" in line or "ccc" in line]
    blocked_line = next(line for line in lines if "aaa" in line)
    unresolved_line = next(line for line in lines if "ccc" in line)
    renamed_line = next(line for line in lines if "bbb" in line)
    assert "rename" not in blocked_line and "add+remove" not in blocked_line
    assert "rename" not in unresolved_line and "add+remove" not in unresolved_line
    assert "rename" in renamed_line and "add+remove" in renamed_line


# ── 架构师 PR #298 审查整改（2026-11-23）──────────────────────────────────
def test_plan_is_rebuilt_before_apply_and_concurrent_value_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入前重新检测：plan 之后并发写入目标键（异值）→ abort，不覆盖、不删旧值。"""
    env, home, work, _fake = _legacy_setup(
        tmp_path, monkeypatch, values={"figma@acme/region": "eu"}, inputs={"figma": [{"id": "region", "description": "r"}]}
    )
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME

    # 并发写入目标键（不同值）。
    _write_values(env, {"figma@acme/region": "eu", "figma@acme-skills/region": "CONCURRENT"})
    _apply(plan, home, env)

    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_VALUE_TARGET_CONFLICT
    assert _read_values(env)["figma@acme-skills/region"] == "CONCURRENT"
    assert _read_values(env)["figma@acme/region"] == "eu"
    assert marketplace_skill_dir(home, "acme").is_dir()


def test_write_time_value_recheck_direct(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_values(env, {"figma@acme/region": "eu", "figma@acme-skills/region": "CONCURRENT"})
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        value_pairs=[("figma@acme/region", "figma@acme-skills/region", "eu")],
    )
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._rekey_values(item, env, write_new=True)
    assert excinfo.value.reason == mm.REASON_VALUE_TARGET_CONFLICT


def test_write_time_secret_recheck_aborts() -> None:
    fake = _FakeSecrets({"old/token": "expected", "new/token": "different"})
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        secret_pairs=[mm.SecretRekey(old_key="old/token", new_key="new/token", value="expected")],
    )
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._rekey_secrets(item, fake, write_new=True)
    assert excinfo.value.reason == mm.REASON_SECRET_TARGET_CONFLICT
    assert fake.data["new/token"] == "different"


def test_write_time_known_source_change_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source("https://example.com/DIFFERENT.git"),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
    }
    _write_known(home, env, known)
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._write_known_new(item, home, env)
    assert excinfo.value.reason == mm.REASON_NAME_TAKEN


def test_merge_keeps_target_record_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    target = marketplace_skill_dir(home, "acme-skills")
    _catalog(home, "acme-skills", _manifest("acme-skills"))
    _external_tree(home, "acme-skills", "remote", None)
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(target),
        "commitSha": "NEWSHA",
        "lastUpdated": "2026-06-01T00:00:00Z",
        "autoUpdate": True,
        # 故意**不写** baseline：强制走 _mut_merge（只补 baseline），检验元数据不被旧记录回退。
    }
    _write_known(home, env, known)

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    record = store.load_known_marketplaces(home, env)["marketplaces"]["acme-skills"]
    assert record["commitSha"] == "NEWSHA"
    assert record["lastUpdated"] == "2026-06-01T00:00:00Z"
    assert record["autoUpdate"] is True
    assert record["manifestNameBaseline"] == "acme-skills"
    item = plan.items[0]
    assert item.actions["catalog"] == "noop"
    assert item.actions["externalTrees"] == 0  # merge 且目标外树已在 → 不搬


def test_merge_write_known_new_is_noop_when_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目标记录已就绪（baseline 正确）时 `_write_known_new` 不落盘（字节 + mtime 不变）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    target = marketplace_skill_dir(home, "acme-skills")
    _catalog(home, "acme-skills", _manifest("acme-skills"))
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(target),
        "commitSha": "NEWSHA",
        "manifestNameBaseline": "acme-skills",
    }
    _write_known(home, env, known)
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.merge

    known_path = store.known_marketplaces_path(home, env)
    known_bytes = known_path.read_bytes()
    known_mtime = known_path.stat().st_mtime_ns
    mm._write_known_new(item, home, env)

    assert known_path.read_bytes() == known_bytes
    assert known_path.stat().st_mtime_ns == known_mtime


def test_keyring_read_failure_blocks_with_distinct_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        secrets={"figma@acme/token": "s3cret"},
        inputs={"figma": [{"id": "token", "description": "t", "password": True}]},
    )
    _patch_keyring(monkeypatch, _FakeSecrets({"figma@acme/token": "s3cret"}, fail_read=True))
    before = _snapshot(tmp_path)
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_SECRET_READ_FAILED
    assert any("cannot read secret" in warning for warning in item.warnings)
    _assert_unchanged(before, tmp_path)


def test_drop_old_ledger_normalizes_missing_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    root = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(home, env, {"figma@acme": [{"installPath": str(root), "mcpServers": []}]})  # 无 scope
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "figma@acme" not in ledger and "figma@acme-skills" in ledger


def test_rebuild_skips_already_materialized_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    new_root = marketplace_skill_dir(home, "acme-skills") / "plugins" / "figma"
    _write_ledger(
        home,
        env,
        {
            "figma@acme": [_record(home, "acme", "figma")],
            "figma@acme-skills": [
                {
                    "scope": "user",
                    "installPath": str(new_root),
                    "installedAt": "ORIGINAL",
                    "lastUpdated": "ORIGINAL",
                    "mcpServers": [],
                }
            ],
        },
    )
    # 模拟 P2 崩溃后的状态：目录已在 new、known[old] 未删。
    marketplace_skill_dir(home, "acme").rename(marketplace_skill_dir(home, "acme-skills"))
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {"source": _known_source(), "installLocation": str(marketplace_skill_dir(home, "acme-skills"))}
    _write_known(home, env, known)
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert ledger["figma@acme-skills"][0]["installedAt"] == "ORIGINAL"
    assert "figma@acme" not in ledger


def test_double_apply_is_byte_and_mtime_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    assert asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True)) == 0

    def _snapshot_with_mtime() -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(tmp_path)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

    before = _snapshot_with_mtime()
    assert asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True)) == 0
    assert _snapshot_with_mtime() == before


def test_known_key_order_does_not_change_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir()
    for name, target in (("aa", "aa-new"), ("bb", "bb-new")):
        _catalog(home, name, _manifest(target))
    entries = {
        "aa": {"source": _known_source("https://example.com/aa.git"), "installLocation": str(marketplace_skill_dir(home, "aa"))},
        "bb": {"source": _known_source("https://example.com/bb.git"), "installLocation": str(marketplace_skill_dir(home, "bb"))},
    }
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    _patch_keyring(monkeypatch, _FakeSecrets())

    _write_known(home, env, dict(entries))
    first = [(i.old, i.new, i.action) for i in mm.build_plan(home, env, cwd=work).items]
    _write_known(home, env, dict(reversed(list(entries.items()))))
    second = [(i.old, i.new, i.action) for i in mm.build_plan(home, env, cwd=work).items]
    assert first == second == [("aa", "aa-new", "rename"), ("bb", "bb-new", "rename")]


def test_external_tree_rename_failure_aborts_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    manifest = _manifest(
        "acme-skills",
        plugins=[{"name": "remote", "source": {"source": "url", "url": "https://example.com/r.git"}}],
    )
    _catalog(home, "acme", manifest)
    ext = _external_tree(home, "acme", "remote", None)
    _write_ledger(
        home,
        env,
        {"remote@acme": [{"scope": "user", "installPath": str(ext), "installedAt": "t", "lastUpdated": "t", "mcpServers": []}]},
    )
    _write_user(env, {"installedPlugins": ["remote@acme"], "enabledPlugins": {"remote@acme": True}})

    real_rename = Path.rename

    def flaky_rename(self: Path, target: Path) -> Path:
        if ".plugins" in str(self):
            raise OSError("busy")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)

    item = plan.items[0]
    assert item.action == mm.ACTION_ABORTED
    assert (home / "marketplace" / ".plugins" / "acme" / "remote").is_dir()
    assert "acme" in store.load_known_marketplaces(home, env)["marketplaces"]


def test_enabled_false_is_copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": False}})
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    assert _user(env)["enabledPlugins"] == {"figma@acme-skills": False}


def test_info_human_table_shows_manifest_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env, home, _work, _fake = _legacy_setup(tmp_path, monkeypatch)
    assert marketplace_info(home, env, "acme") == mm.EXIT_OK
    out = capsys.readouterr().out
    assert "manifestName" in out and "acme-skills" in out


def test_info_does_not_read_outside_skill_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(tmp_path)
    home = _home(env)
    (home / "marketplace").mkdir(parents=True, exist_ok=True)  # 让 `..` 可被解析（真实 home 必有）
    outside = home / "outside"  # `home/marketplace/../outside`
    _write_json(
        outside / ".tfrobot-plugin" / "marketplace.json",
        _manifest("leaked-name"),
    )
    _write_known(
        home,
        env,
        {"../outside": {"source": _known_source(), "installLocation": str(outside)}},
    )
    assert marketplace_info(home, env, "../outside", json_output=True) == mm.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifestName"] is None
    assert payload["legacyName"] is False
    assert "leaked-name" not in json.dumps(payload)


def test_no_ledger_file_is_not_created_when_nothing_to_drop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    assert not store.installed_plugins_path(home, env).exists()


def test_baseline_secondary_judgement_when_manifest_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, baseline="acme-skills")
    # 破坏清单：不可读，但 baseline 存在且 ≠ 旧名 → §3.4 次选判据。
    manifest_path = marketplace_skill_dir(home, "acme") / ".tfrobot-plugin" / "marketplace.json"
    manifest_path.write_text("{broken", encoding="utf-8")

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_RENAME and item.baseline_derived
    assert any("manifestNameBaseline as the secondary judgement" in warning for warning in item.warnings)

    _apply(plan, home, env)
    item = next(i for i in plan.items if i.old == "acme")
    assert marketplace_skill_dir(home, "acme-skills").is_dir()
    assert not marketplace_skill_dir(home, "acme").exists()
    assert _user(env)["installedPlugins"] == ["figma@acme-skills"]
    assert item.incomplete is True  # 清单不可读 → 不做账本重建


def test_rekey_values_p3_verifies_target_before_deleting_old(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_values(env, {"figma@acme/region": "eu"})
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        value_pairs=[("figma@acme/region", "figma@acme-skills/region", "eu")],
    )
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._rekey_values(item, env, write_new=False)
    assert excinfo.value.reason == mm.REASON_VALUE_TARGET_CONFLICT
    assert _read_values(env)["figma@acme/region"] == "eu"


def test_drop_old_ledger_does_not_create_missing_file(tmp_path: Path) -> None:
    env = _env(tmp_path)
    home = _home(env)
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        ledger_rebuild=[("figma@acme", "user", None)],
    )
    mm._drop_old_ledger(item, home, env)
    assert not store.installed_plugins_path(home, env).exists()


def test_execute_plan_refuses_blocking_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主直调 execute_plan 时也必须拒绝阻塞冲突（不能只靠 CLI handler 的守卫）。"""
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir()
    _catalog(home, "aa", _manifest("shared"))
    _catalog(home, "bb", _manifest("shared"))
    _write_known(
        home,
        env,
        {
            "aa": {"source": _known_source("https://example.com/a.git"), "installLocation": str(marketplace_skill_dir(home, "aa"))},
            "bb": {"source": _known_source("https://example.com/b.git"), "installLocation": str(marketplace_skill_dir(home, "bb"))},
        },
    )
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    _patch_keyring(monkeypatch, _FakeSecrets())
    plan = mm.build_plan(home, env, cwd=work)
    before = _snapshot(tmp_path)
    result = _apply(plan, home, env)
    assert result.items is not None
    assert mm.plan_exit_code(result) == mm.EXIT_CONFLICT
    _assert_unchanged(before, tmp_path)


# ── 架构师 PR #298 delta 复审整改（2026-11-23 终审）──────────────────────
def test_baseline_derived_never_claims_stray_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """baseline 次选路径：必须搬注册的 old_dir，绝不把清单名相同的游离目录当候选。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, baseline="acme-skills")
    _catalog(home, "stray", _manifest("acme-skills"))  # 清单名相同、不在 known 的游离目录
    manifest_path = marketplace_skill_dir(home, "acme") / ".tfrobot-plugin" / "marketplace.json"
    manifest_path.write_text("{broken", encoding="utf-8")

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.baseline_derived and item.action == mm.ACTION_RENAME
    _apply(plan, home, env)

    assert marketplace_skill_dir(home, "acme-skills").is_dir()
    assert not marketplace_skill_dir(home, "acme").exists()
    assert marketplace_skill_dir(home, "stray").is_dir(), "游离目录不得被搬走"
    assert (marketplace_skill_dir(home, "stray") / ".tfrobot-plugin" / "marketplace.json").is_file()


def test_old_value_changed_before_p1_aborts(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_values(env, {"figma@acme/region": "CHANGED"})
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        value_pairs=[("figma@acme/region", "figma@acme-skills/region", "eu")],
    )
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._rekey_values(item, env, write_new=True)
    assert excinfo.value.reason == mm.REASON_VALUE_TARGET_CONFLICT
    assert _read_values(env) == {"figma@acme/region": "CHANGED"}


def test_old_value_changed_before_p3_aborts(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_values(env, {"figma@acme/region": "CHANGED", "figma@acme-skills/region": "eu"})
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        value_pairs=[("figma@acme/region", "figma@acme-skills/region", "eu")],
    )
    with pytest.raises(mm._ItemFailure):
        mm._rekey_values(item, env, write_new=False)
    assert _read_values(env)["figma@acme/region"] == "CHANGED"


def test_old_secret_changed_before_p1_and_p3_aborts() -> None:
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        secret_pairs=[mm.SecretRekey(old_key="old/token", new_key="new/token", value="expected")],
    )
    fake = _FakeSecrets({"old/token": "changed"})
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._rekey_secrets(item, fake, write_new=True)
    assert excinfo.value.reason == mm.REASON_SECRET_TARGET_CONFLICT
    assert "new/token" not in fake.data

    fake2 = _FakeSecrets({"old/token": "changed", "new/token": "expected"})
    with pytest.raises(mm._ItemFailure):
        mm._rekey_secrets(item, fake2, write_new=False)
    assert fake2.data["old/token"] == "changed"


def test_unrecognizable_source_unchanged_still_migrates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`{"type":"git"}` 缺 url（不可识别）但未变 → 不得误判换源。"""
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir()
    _catalog(home, "acme", _manifest("acme-skills"))
    _write_known(
        home,
        env,
        {"acme": {"source": {"type": "git"}, "installLocation": str(marketplace_skill_dir(home, "acme"))}},
    )
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    _patch_keyring(monkeypatch, _FakeSecrets())
    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    _apply(plan, home, env)
    assert "acme-skills" in store.load_known_marketplaces(home, env)["marketplaces"]


def test_source_changed_from_plan_snapshot_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme"]["source"] = {"type": "git"}  # 不可识别形态，但与原值不同 → 视为变化
    _write_known(home, env, known)
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._write_known_new(item, home, env)
    assert excinfo.value.reason == mm.REASON_NAME_TAKEN


def test_invalid_scope_is_normalized_to_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    root = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(home, env, {"figma@acme": [{"scope": 5, "installPath": str(root), "mcpServers": []}]})
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert ledger["figma@acme-skills"][0]["scope"] == "user"
    assert "figma@acme" not in ledger


def test_project_record_without_project_path_blocks_in_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    root = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(home, env, {"figma@acme": [{"scope": "project", "installPath": str(root), "mcpServers": []}]})
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_PROJECT_PATH_MISSING
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL  # dry-run 即 exit 3
    _assert_unchanged(before, tmp_path)

    assert asyncio.run(marketplace_migrate(home, env, apply=True, json_output=True)) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)
    assert "figma@acme" in store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]


def test_baseline_derived_crash_window_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """baseline 次选路径的崩溃续跑：清单不可读时以 known[new].manifestNameBaseline 作为证据。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, baseline="acme-skills")
    marketplace_skill_dir(home, "acme").rename(marketplace_skill_dir(home, "acme-skills"))
    (marketplace_skill_dir(home, "acme-skills") / ".tfrobot-plugin" / "marketplace.json").write_text(
        "{broken", encoding="utf-8"
    )
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
        "manifestNameBaseline": "acme-skills",
    }
    _write_known(home, env, known)
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.already_renamed and item.baseline_derived and item.action == mm.ACTION_RENAME
    _apply(plan, home, env)

    assert set(store.load_known_marketplaces(home, env)["marketplaces"]) == {"acme-skills"}
    assert _user(env)["installedPlugins"] == ["figma@acme-skills"]
    assert plan.items[0].incomplete is True


def test_p3_secret_target_missing_aborts_and_keeps_old() -> None:
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        secret_pairs=[mm.SecretRekey(old_key="old/token", new_key="new/token", value="expected")],
    )
    fake = _FakeSecrets({"old/token": "expected"})  # 新键被并发删除
    with pytest.raises(mm._ItemFailure):
        mm._rekey_secrets(item, fake, write_new=False)
    assert fake.data["old/token"] == "expected"


def test_null_old_value_missing_is_detected(tmp_path: Path) -> None:
    """旧值恰为 null：并发删键不能被 `data.get(old) == None` 当成「未变」。"""
    env = _env(tmp_path)
    _write_values(env, {})
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        value_pairs=[("figma@acme/region", "figma@acme-skills/region", None)],
    )
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._rekey_values(item, env, write_new=True)
    assert excinfo.value.reason == mm.REASON_VALUE_TARGET_CONFLICT
    assert "figma@acme-skills/region" not in _read_values(env)


def test_p3_target_value_exists_but_differs_aborts(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_values(env, {"figma@acme/region": "eu", "figma@acme-skills/region": "DIFFERENT"})
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        value_pairs=[("figma@acme/region", "figma@acme-skills/region", "eu")],
    )
    with pytest.raises(mm._ItemFailure):
        mm._rekey_values(item, env, write_new=False)
    assert _read_values(env)["figma@acme/region"] == "eu"
    # 目标键也不得被覆盖（#301 item 8）：P3 只删旧，绝不改目标。
    assert _read_values(env)["figma@acme-skills/region"] == "DIFFERENT"


def test_p3_target_secret_exists_but_differs_aborts() -> None:
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        secret_pairs=[mm.SecretRekey(old_key="old/token", new_key="new/token", value="expected")],
    )
    fake = _FakeSecrets({"old/token": "expected", "new/token": "DIFFERENT"})
    with pytest.raises(mm._ItemFailure):
        mm._rekey_secrets(item, fake, write_new=False)
    assert fake.data["old/token"] == "expected"
    assert fake.data["new/token"] == "DIFFERENT"  # 目标 secret 同样不得被覆盖（#301 item 8）


def test_project_record_rebuild_preserves_project_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    root = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(
        home,
        env,
        {"figma@acme": [{"scope": "project", "projectPath": str(work), "installPath": str(root), "mcpServers": []}]},
    )
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    record = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]["figma@acme-skills"][0]
    assert record["scope"] == "project"
    assert record["projectPath"] == str(work)


def test_empty_scope_is_normalized_to_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    root = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(home, env, {"figma@acme": [{"scope": "", "installPath": str(root), "mcpServers": []}]})
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert ledger["figma@acme-skills"][0]["scope"] == "user"
    assert "figma@acme" not in ledger


def test_source_unchanged_normalizes_whitespace_url() -> None:
    planned = {"type": "git", "url": " https://x "}
    current = {"type": "git", "url": "https://x"}
    assert mm._source_unchanged(planned, current) is True
    assert mm._source_unchanged(planned, {"type": "git", "url": "https://y"}) is False


def test_source_added_after_plan_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir()
    _catalog(home, "acme", _manifest("acme-skills"))
    _write_known(home, env, {"acme": {"installLocation": str(marketplace_skill_dir(home, "acme"))}})
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    _patch_keyring(monkeypatch, _FakeSecrets())
    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme"]["source"] = _known_source()  # 计划时无 source，写入时被补上
    _write_known(home, env, known)
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._write_known_new(item, home, env)
    assert excinfo.value.reason == mm.REASON_NAME_TAKEN


def test_baseline_evidence_requires_old_record_to_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """候选 known[new].baseline==new 但旧记录 baseline 不同 → 不得据此续跑（unresolved）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, baseline="some-other-name")
    marketplace_skill_dir(home, "acme").rename(marketplace_skill_dir(home, "acme-skills"))
    (marketplace_skill_dir(home, "acme-skills") / ".tfrobot-plugin" / "marketplace.json").write_text(
        "{broken", encoding="utf-8"
    )
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme-skills"] = {
        "source": _known_source(),
        "installLocation": str(marketplace_skill_dir(home, "acme-skills")),
        "manifestNameBaseline": "acme-skills",
    }
    _write_known(home, env, known)
    plan = mm.build_plan(home, env, cwd=work)
    item = next(i for i in plan.items if i.old == "acme")
    assert item.action == mm.ACTION_UNRESOLVED
    assert item.reason in {mm.REASON_CATALOG_MISSING, mm.REASON_MANIFEST_UNREADABLE}


# ── #301 P0.5 窄对齐 / 竞态纵深防御（2026-11-24 裁决）────────────────────────
def test_project_record_without_project_path_for_removed_plugin_keeps_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin 已移出新清单：按 F10 保留旧账本、迁移其余、exit 3，不得整 marketplace 阻断（#301 item 1）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    root = marketplace_skill_dir(home, "acme") / "plugins" / "ghost"
    root.mkdir(parents=True)
    _write_ledger(home, env, {"ghost@acme": [{"scope": "project", "installPath": str(root), "mcpServers": []}]})
    _write_user(
        env,
        {
            "installedPlugins": ["ghost@acme"],
            "enabledPlugins": {"ghost@acme": True},
            "trustedMarketplaces": ["acme"],
            "blockedMarketplaces": ["acme"],
        },
    )
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_RENAME  # 不再整项 blocked
    assert item.incomplete is True  # manifest 固定 ⇒ 计划期即可确定（dry-run 与 apply 同码）
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    assert any("not present in the new manifest" in warning for warning in item.warnings)
    _assert_unchanged(before, tmp_path)  # dry-run 零写入

    _apply(plan, home, env)
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "ghost@acme" in ledger and "ghost@acme-skills" not in ledger
    assert marketplace_skill_dir(home, "acme-skills").is_dir()
    assert set(store.load_known_marketplaces(home, env)["marketplaces"]) == {"acme-skills"}
    # 「迁移其余」= settings 四个引用面都改指新名（不只账本保留）
    user = _user(env)
    assert user["installedPlugins"] == ["ghost@acme-skills"]
    assert user["enabledPlugins"] == {"ghost@acme-skills": True}
    assert user["trustedMarketplaces"] == ["acme-skills"]
    assert user["blockedMarketplaces"] == ["acme-skills"]
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_project_record_without_project_path_for_baseline_derived_keeps_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """baseline_derived（清单不可读 ⇒ 全部 plugin 视为已出清单）：同样按 F10 保留旧账本、迁移其余、exit 3。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, baseline="acme-skills", with_ledger=False)
    root = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(home, env, {"figma@acme": [{"scope": "local", "installPath": str(root), "mcpServers": []}]})
    _write_user(
        env,
        {
            "installedPlugins": ["figma@acme"],
            "enabledPlugins": {"figma@acme": True},
            "trustedMarketplaces": ["acme"],
            "blockedMarketplaces": ["acme"],
        },
    )
    (marketplace_skill_dir(home, "acme") / ".tfrobot-plugin" / "marketplace.json").write_text("{broken", encoding="utf-8")
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.baseline_derived and item.action == mm.ACTION_RENAME
    assert item.incomplete is True
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)  # dry-run 零写入
    _apply(plan, home, env)

    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "figma@acme" in ledger and "figma@acme-skills" not in ledger
    assert marketplace_skill_dir(home, "acme-skills").is_dir()
    assert not marketplace_skill_dir(home, "acme").exists()
    user = _user(env)  # 「迁移其余」：引用面照常改指新名
    assert user["installedPlugins"] == ["figma@acme-skills"]
    assert user["enabledPlugins"] == {"figma@acme-skills": True}
    assert user["trustedMarketplaces"] == ["acme-skills"]
    assert user["blockedMarketplaces"] == ["acme-skills"]


def test_project_record_without_project_path_and_install_path_keeps_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin 仍在清单但记录缺 installPath：P2 只保留旧记录、不 raise ⇒ 预检不得阻断（谓词第二半）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    _write_ledger(home, env, {"figma@acme": [{"scope": "project", "mcpServers": []}]})
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    # 静态可知的 keep 形态在计划期即预告：dry-run 退出码与 apply 一致（§12.2 / #301 裁决②）
    assert plan.items[0].incomplete is True
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    assert any("has no installPath" in warning for warning in plan.items[0].warnings)
    _assert_unchanged(before, tmp_path)
    _apply(plan, home, env)

    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "figma@acme" in ledger and "figma@acme-skills" not in ledger
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_non_mapping_ledger_record_is_predicted_in_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """记录非对象：P2 只保留、不 raise ⇒ 计划期即置 incomplete（dry-run 与 apply 同码）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    _write_ledger(home, env, {"figma@acme": ["not-a-record"]})  # type: ignore[list-item]
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    assert plan.items[0].incomplete is True
    assert plan.items[0].reason == mm.REASON_LEDGER_REBUILD_FAILED
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)

    _apply(plan, home, env)
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_removed_plugin_inputs_and_secret_are_not_rekeyed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D14 carve-out（#301 裁决）：已移出新清单的 plugin 无 rename 目标 ⇒ 不枚举/不重键/不阻断。"""
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        with_ledger=False,
        inputs={
            "figma": [{"id": "region", "description": "r"}],
            "ghost": [
                {"id": "region", "description": "r"},
                {"id": "token", "description": "t", "password": True},
            ],
        },
        values={"figma@acme/region": "eu", "ghost@acme/region": "gz"},
        secrets={"ghost@acme/token": "s3cret"},
    )
    _write_ledger(home, env, {"ghost@acme": [_record(home, "acme", "ghost")]})
    _write_user(
        env,
        {
            "installedPlugins": ["ghost@acme", "figma@acme"],
            "enabledPlugins": {"ghost@acme": True, "figma@acme": True},
        },
    )

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_RENAME
    assert any("left unchanged" in warning for warning in item.warnings)
    _apply(plan, home, env)

    # 已移除 plugin：旧 secret 键/值原样保留，keyring 无任何写/删
    assert fake.data == {"ghost@acme/token": "s3cret"}
    assert fake.ops == []
    # 它的 input 值也不重键
    values = _read_values(env)
    assert values["ghost@acme/region"] == "gz" and "ghost@acme-skills/region" not in values
    # 清单内 plugin 正常迁移
    assert values["figma@acme-skills/region"] == "eu" and "figma@acme/region" not in values
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "ghost@acme" in ledger and "ghost@acme-skills" not in ledger
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


def test_in_manifest_plugin_missing_root_still_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D14 正对照：plugin 仍在新清单但根不可定位 ⇒ 整项 blocked、零写入（carve-out 不得扩大）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False, inputs={})
    missing = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(home, env, {"figma@acme": [{"scope": "user", "installPath": str(missing), "mcpServers": []}]})
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_PLUGIN_ROOT_UNREADABLE
    assert not any("keeping its old ledger record" in warning for warning in item.warnings)
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _apply(plan, home, env)
    _assert_unchanged(before, tmp_path)


@pytest.mark.parametrize(
    "ghost_records",
    [
        {"not": "a list"},
        ["not-a-record"],
        [{"scope": "project", "mcpServers": []}],
    ],
    ids=["records-not-array", "record-not-object", "record-missing-installPath"],
)
def test_removed_plugin_does_not_mask_in_manifest_project_path_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ghost_records: object
) -> None:
    """同一 marketplace：不可重建的 plugin 只登记 keep，但整项被阻断时不得落任何「keeping」诊断（🟡2）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    figma_root = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(
        home,
        env,
        {
            "ghost@acme": ghost_records,  # type: ignore[dict-item]  # 已出清单 + 三种静态 keep 形态
            "figma@acme": [{"scope": "project", "installPath": str(figma_root), "mcpServers": []}],
        },
    )
    _write_user(env, {"installedPlugins": ["ghost@acme", "figma@acme"], "enabledPlugins": {}})
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_BLOCKED and item.reason == mm.REASON_PROJECT_PATH_MISSING
    # 整项被阻断 ⇒ 不执行、也未保留任何账本：所有「keep」诊断都必须推迟到阻断预检之后（此处一条都不落）
    assert not any("keeping" in warning for warning in item.warnings)
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _apply(plan, home, env)
    _assert_unchanged(before, tmp_path)


def test_rebuild_ledger_race_project_path_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P0.5 已同判据前置；`_rebuild_ledger` 的 raise 保留为**竞态**纵深防御（#301 item 9 裁决）。"""
    env, home, _work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    root = marketplace_skill_dir(home, "acme") / "plugins" / "figma"
    _write_ledger(home, env, {"figma@acme": [{"scope": "project", "installPath": str(root), "mcpServers": []}]})
    ledger_path = store.installed_plugins_path(home, env)
    before = ledger_path.read_bytes()
    item = mm.MigrationItem(old="acme", new="acme-skills", action=mm.ACTION_RENAME, manifest=_manifest("acme-skills"))

    with pytest.raises(mm._ItemFailure) as excinfo:
        asyncio.run(mm._rebuild_ledger(item, home, env))
    assert excinfo.value.reason == mm.REASON_LEDGER_REBUILD_FAILED
    assert ledger_path.read_bytes() == before


def test_record_needs_project_path_predicate_contract() -> None:
    """谓词自足性：缺/空 `installPath` 一律 False——P0.5 与 P2 共用的单一判据自己守边界（#301 item 1）。"""
    assert mm._record_needs_project_path({"scope": "project"}) is False
    assert mm._record_needs_project_path({"scope": "project", "installPath": "", "projectPath": ""}) is False
    assert mm._record_needs_project_path({"scope": "project", "installPath": "/p"}) is True
    assert mm._record_needs_project_path({"scope": "project", "installPath": "/p", "projectPath": "/w"}) is False


def test_whitespace_manifest_entry_is_enumerated_as_in_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清单条目名带空白：`find_plugin_entry` strip 后匹配即「在清单」——枚举与 carve-out 同一判据（#309 🔴1）。"""
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        with_ledger=False,
        inputs={"figma": [{"id": "token", "description": "t", "password": True}]},
        secrets={"figma@acme/token": "s3cret"},
    )
    _catalog(home, "acme", _manifest("acme-skills", plugins=[{"name": " figma", "source": "figma"}]))
    _write_ledger(home, env, {"figma@acme": [_record(home, "acme", "figma")]})
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_RENAME
    assert not any("not present in the new manifest" in warning for warning in item.warnings)
    assert not any("left unchanged" in warning for warning in item.warnings)

    _apply(plan, home, env)
    # 正常枚举并重键：绝不因条目名带空白而静默漏迁（旧键删、新键在）
    assert ("set", "figma@acme-skills/token") in fake.ops
    assert ("delete", "figma@acme/token") in fake.ops
    assert fake.data == {"figma@acme-skills/token": "s3cret"}


@pytest.mark.parametrize("kind", ["dotdot", "absolute"], ids=["dotdot", "absolute"])
def test_manifest_entry_traversal_never_reads_outside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """不可信清单条目名（`../` / 绝对路径）不得进入枚举：不读 home 外 inputs.json、keyring 零写删（#309 🔴N1）。"""
    evil_dir = tmp_path / "evil"
    _write_json(
        evil_dir / "mcp-servers" / "inputs.json",
        {"inputs": [{"id": "token", "description": "t", "password": True}]},
    )
    bad_name = "../../../../evil" if kind == "dotdot" else str(evil_dir)
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch, with_ledger=False)
    _catalog(
        home,
        "acme",
        _manifest(
            "acme-skills",
            plugins=[{"name": bad_name, "source": {"source": "url", "url": "https://example.com/e.git"}}],
        ),
    )
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    fake = _FakeSecrets({f"{bad_name}@acme/token": "s3cret"})  # 若被枚举，就会读旧值并重键
    _patch_keyring(monkeypatch, fake)

    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    _apply(plan, home, env)

    # 恶意名未进入枚举 ⇒ 不读 home 外 inputs.json ⇒ keyring 零 set/delete、旧 secret 原样
    assert fake.ops == []
    assert fake.data == {f"{bad_name}@acme/token": "s3cret"}


def test_bare_string_source_ledger_keep_predicted_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧注册 source 为裸字符串（非映射）：P2 必重建失败 ⇒ 计划期即预告 incomplete（#309 🟡3 裁决）。"""
    env, home, work, _fake = _legacy_setup(tmp_path, monkeypatch)
    known = store.load_known_marketplaces(home, env)["marketplaces"]
    known["acme"]["source"] = "https://example.com/acme.git"
    _write_known(home, env, known)
    before = _snapshot(tmp_path)

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_RENAME
    assert item.incomplete is True
    assert item.reason == mm.REASON_LEDGER_REBUILD_FAILED
    assert any("cannot be re-materialized" in warning for warning in item.warnings)
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
    _assert_unchanged(before, tmp_path)

    _apply(plan, home, env)
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "figma@acme" in ledger and "figma@acme-skills" not in ledger
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


@pytest.mark.parametrize("baseline", [None, "acme-skills"], ids=["removed-from-manifest", "baseline-derived"])
def test_removed_plugin_missing_root_still_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline: str | None
) -> None:
    """carve-out 原始动机（#309 🟡6）：无 rename 目标的 plugin 根不存在 ⇒ 仍 rename + incomplete、不 blocked。"""
    env, home, work, fake = _legacy_setup(
        tmp_path,
        monkeypatch,
        baseline=_MISSING if baseline is None else baseline,
        with_ledger=False,
        secrets={"ghost@acme/token": "s3cret"},
    )
    if baseline is not None:
        (marketplace_skill_dir(home, "acme") / ".tfrobot-plugin" / "marketplace.json").write_text(
            "{broken", encoding="utf-8"
        )
    missing = marketplace_skill_dir(home, "acme") / "plugins" / "ghost"  # 根目录不存在
    _write_ledger(home, env, {"ghost@acme": [{"scope": "project", "installPath": str(missing), "mcpServers": []}]})
    _write_user(env, {"installedPlugins": ["ghost@acme"], "enabledPlugins": {"ghost@acme": True}})

    plan = mm.build_plan(home, env, cwd=work)
    item = plan.items[0]
    assert item.action == mm.ACTION_RENAME  # 不因根缺失整项 blocked
    assert item.incomplete is True
    _apply(plan, home, env)

    assert marketplace_skill_dir(home, "acme-skills").is_dir()
    assert not marketplace_skill_dir(home, "acme").exists()
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "ghost@acme" in ledger and "ghost@acme-skills" not in ledger
    # 未枚举 ⇒ keyring 零写/删，旧 secret 原样保留
    assert fake.ops == []
    assert fake.data == {"ghost@acme/token": "s3cret"}
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL


# ── #301 item 2 / 5 / 7：P3 null、无 source 正路、死分支 ─────────────────────
def test_p3_null_old_value_missing_aborts(tmp_path: Path) -> None:
    """P3：计划值为 null 且旧键缺失 → 不得被 `data.get(old) == None` 当成「未变」（#301 item 2）。"""
    env = _env(tmp_path)
    _write_values(env, {"figma@acme-skills/region": None})
    path = resolve_value_store_path(env)
    before = path.read_bytes()
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        value_pairs=[("figma@acme/region", "figma@acme-skills/region", None)],
    )
    with pytest.raises(mm._ItemFailure) as excinfo:
        mm._rekey_values(item, env, write_new=False)
    assert excinfo.value.reason == mm.REASON_VALUE_TARGET_CONFLICT
    assert path.read_bytes() == before


def test_p3_null_value_equal_deletes_old(tmp_path: Path) -> None:
    """P3 正向：目标与旧键同为 null → 视为同值，删旧键、保留目标（item 7 去死分支后仍成立）。"""
    env = _env(tmp_path)
    _write_values(env, {"figma@acme/region": None, "figma@acme-skills/region": None})
    item = mm.MigrationItem(
        old="acme",
        new="acme-skills",
        action=mm.ACTION_RENAME,
        value_pairs=[("figma@acme/region", "figma@acme-skills/region", None)],
    )
    mm._rekey_values(item, env, write_new=False)
    data = _read_values(env)
    assert "figma@acme/region" not in data
    assert "figma@acme-skills/region" in data and data["figma@acme-skills/region"] is None


def test_missing_source_key_plans_and_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """known 记录完全无 `source` 键：plan+apply 正常改名（防「planned is None 即 abort」误伤，item 5）。"""
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir()
    _catalog(home, "acme", _manifest("acme-skills"))
    _write_known(home, env, {"acme": {"installLocation": str(marketplace_skill_dir(home, "acme"))}})
    _write_user(env, {"installedPlugins": [], "enabledPlugins": {}})
    _patch_keyring(monkeypatch, _FakeSecrets())

    plan = mm.build_plan(home, env, cwd=work)
    assert plan.items[0].action == mm.ACTION_RENAME
    _apply(plan, home, env)
    assert set(store.load_known_marketplaces(home, env)["marketplaces"]) == {"acme-skills"}
    assert marketplace_skill_dir(home, "acme-skills").is_dir()
    assert mm.plan_exit_code(plan) == mm.EXIT_OK


def test_missing_source_key_with_ledger_keeps_old_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 `source` 的 known 记录无法驱动重物化（`_resolve_marketplace_source` 要求映射）：落通用重建失败
    分支 → 保留旧账本、其余照迁、exit 3（item 5 × F10 交互）——不是「plugin 不在清单」那条 F10 分支。"""
    env = _env(tmp_path)
    home = _home(env)
    work = tmp_path / "work"
    work.mkdir()
    _catalog(home, "acme", _manifest("acme-skills"))
    (marketplace_skill_dir(home, "acme") / "plugins" / "figma").mkdir(parents=True)
    _write_ledger(home, env, {"figma@acme": [_record(home, "acme", "figma")]})
    _write_known(home, env, {"acme": {"installLocation": str(marketplace_skill_dir(home, "acme"))}})
    _write_user(env, {"installedPlugins": ["figma@acme"], "enabledPlugins": {"figma@acme": True}})
    _patch_keyring(monkeypatch, _FakeSecrets())

    plan = mm.build_plan(home, env, cwd=work)
    _apply(plan, home, env)
    ledger = store.load_installed_plugins(home, env, drop_legacy=False)["plugins"]
    assert "figma@acme" in ledger and "figma@acme-skills" not in ledger
    assert set(store.load_known_marketplaces(home, env)["marketplaces"]) == {"acme-skills"}
    assert plan.items[0].incomplete is True
    assert plan.items[0].reason == mm.REASON_LEDGER_REBUILD_FAILED
    assert any("could not be re-materialized" in warning for warning in plan.items[0].warnings)
    assert mm.plan_exit_code(plan) == mm.EXIT_PARTIAL
