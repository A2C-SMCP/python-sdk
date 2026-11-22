# -*- coding: utf-8 -*-
# filename: test_marketplace_migrate.py
# @Time    : 2026/11/22
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``marketplace migrate`` CLI 集成测试（#268 / D12）/ Migration CLI integration tests.

走真实 Typer / REPL 全栈（tmp home + XDG 重定向）；夹具直接构造 pre-#260 状态（不用 ``marketplace add``）。
覆盖：默认 dry-run 零写入 / ``--apply`` 全量迁移 / 冲突 exit 1 零写入 / keyring 不可用 exit 3 /
REPL 只 dry-run（``--apply`` 被拒）/ ``--name`` 未知 → exit 1。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import a2c_smcp.computer.cli.main as cli_main
from a2c_smcp.computer.cli.commands.marketplace import repl_dispatch
from a2c_smcp.computer.settings import marketplace_migration as mm
from a2c_smcp.computer.settings import store
from a2c_smcp.computer.settings.scope import user_settings_path
from a2c_smcp.computer.skills.home import marketplace_skill_dir


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _manifest(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "owner": {"name": "Test"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "figma", "source": "figma"}],
    }


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, conflict: bool = False) -> tuple[Path, Path]:
    home = tmp_path / "skill-home"
    work = tmp_path / "work"
    work.mkdir(parents=True)
    monkeypatch.setenv("A2C_SKILL_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(work)
    env = {"A2C_SKILL_HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "state")}

    manifest = _manifest("acme-skills") if not conflict else _manifest("shared")
    catalog = marketplace_skill_dir(home, "acme")
    _write_json(catalog / ".tfrobot-plugin" / "marketplace.json", manifest)
    _write_json(catalog / "plugins" / "figma" / "mcp-servers" / "inputs.json", {"inputs": [{"id": "region"}]})
    if conflict:
        other = marketplace_skill_dir(home, "other")
        _write_json(other / ".tfrobot-plugin" / "marketplace.json", _manifest("shared"))

    marketplaces: dict[str, Any] = {
        "acme": {
            "source": {"type": "git", "url": "https://example.com/acme.git"},
            "installLocation": str(catalog),
        }
    }
    if conflict:
        marketplaces["other"] = {
            "source": {"type": "git", "url": "https://example.com/other.git"},
            "installLocation": str(marketplace_skill_dir(home, "other")),
        }
    _write_json(store.known_marketplaces_path(home, env), {"version": 1, "marketplaces": marketplaces})
    _write_json(
        store.installed_plugins_path(home, env),
        {
            "version": 1,
            "plugins": {
                "figma@acme": [
                    {
                        "scope": "user",
                        "installPath": str(catalog / "plugins" / "figma"),
                        "installedAt": "t",
                        "lastUpdated": "t",
                        "mcpServers": [],
                    }
                ]
            },
        },
    )
    _write_json(
        user_settings_path(env),
        {
            "installedPlugins": ["figma@acme"],
            "enabledPlugins": {"figma@acme": True},
            "trustedMarketplaces": ["acme"],
        },
    )
    monkeypatch.setattr(mm, "keyring_available", lambda: True)
    return home, work


def test_typer_migrate_default_dry_run_then_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, _work = _setup(tmp_path, monkeypatch)
    runner = CliRunner()

    dry = runner.invoke(cli_main.app, ["marketplace", "migrate", "--json"])
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.output)
    assert payload["dryRun"] is True and payload["status"] == "planned"
    assert "acme-skills" not in store.load_known_marketplaces(home)["marketplaces"]

    applied = runner.invoke(cli_main.app, ["marketplace", "migrate", "--apply", "--json"])
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["status"] == "applied"
    assert "acme-skills" in store.load_known_marketplaces(home)["marketplaces"]
    ledger = store.load_installed_plugins(home, drop_legacy=False)["plugins"]
    assert "figma@acme-skills" in ledger and "figma@acme" not in ledger


def test_typer_migrate_conflict_exits_1_with_zero_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, _work = _setup(tmp_path, monkeypatch, conflict=True)
    before = store.known_marketplaces_path(home).read_bytes()
    result = CliRunner().invoke(cli_main.app, ["marketplace", "migrate", "--apply", "--json"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["status"] == "aborted"
    assert store.known_marketplaces_path(home).read_bytes() == before
    assert marketplace_skill_dir(home, "acme").is_dir()


def test_typer_migrate_keyring_unavailable_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, _work = _setup(tmp_path, monkeypatch)
    catalog = marketplace_skill_dir(home, "acme")
    _write_json(catalog / "plugins" / "figma" / "mcp-servers" / "inputs.json", {"inputs": [{"id": "token", "password": True}]})
    monkeypatch.setattr(mm, "keyring_available", lambda: False)
    before = store.known_marketplaces_path(home).read_bytes()

    result = CliRunner().invoke(cli_main.app, ["marketplace", "migrate", "--apply", "--json"])
    assert result.exit_code == 3, result.output
    assert store.known_marketplaces_path(home).read_bytes() == before
    assert marketplace_skill_dir(home, "acme").is_dir()


def test_typer_migrate_unknown_name_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli_main.app, ["marketplace", "migrate", "--name", "nope", "--json"])
    assert result.exit_code == 1, result.output


class _Comp:
    """REPL dispatch 所需的最小 Computer 替身。"""

    def __init__(self, home: Path) -> None:
        self.skill_home = home
        self.skill_registry = None
        self.dirty = False

    def mark_skills_dirty(self) -> None:
        self.dirty = True


def test_repl_migrate_apply_is_rejected(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, _work = _setup(tmp_path, monkeypatch)
    before = store.known_marketplaces_path(home).read_bytes()

    asyncio.run(repl_dispatch(_Comp(home), ["marketplace", "migrate", "--apply"], session=None))

    out = capsys.readouterr()
    assert "must be run in a shell" in (out.out + out.err)
    assert store.known_marketplaces_path(home).read_bytes() == before
    assert marketplace_skill_dir(home, "acme").is_dir()
