# -*- coding: utf-8 -*-
# filename: test_settings.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``settings`` 命令 handler 单元测试（v0.2.1 #69 Group D，§4.5/§5.4）/ Settings command handler unit tests。

测试意图 / Test intentions（XDG_CONFIG_HOME 重定向隔离 user settings）:
- set：JSON 优先解析 / 数组**整体替换**（非追加）/ flag·policy 只读退出码 1；
- get：merged / 指定 scope 读单字段；缺失键退出码 1；
- show：merged 默认 / 指定 scope；非法 scope 退出码 1；
- edit：$EDITOR（桩）打开 → 返回 (0, reconcile_cb)；flag/policy 只读退出码 1。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from a2c_smcp.computer.cli.commands import settings as settings_cmd
from a2c_smcp.computer.settings.scope import user_settings_path


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


# ── set ─────────────────────────────────────────────────────────────────────
def test_set_json_scalar_and_get_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    assert settings_cmd.settings_set(home, env, "strictKnownMarketplaces", "true", scope="user", json_output=True) == 0
    capsys.readouterr()
    assert settings_cmd.settings_get(home, env, "strictKnownMarketplaces", scope="user", json_output=True) == 0
    assert json.loads(capsys.readouterr().out) == {"strictKnownMarketplaces": True}  # JSON true → bool


def test_set_array_whole_replace(tmp_path: Path) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    settings_cmd.settings_set(home, env, "trustedMarketplaces", '["a","b"]', scope="user")
    settings_cmd.settings_set(home, env, "trustedMarketplaces", '["c"]', scope="user")  # 整体替换、非追加
    data = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert data["trustedMarketplaces"] == ["c"]  # §5.4 数组整体替换


def test_set_literal_string_fallback(tmp_path: Path) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    settings_cmd.settings_set(home, env, "customField", "not-json-value", scope="user")
    data = json.loads(user_settings_path(env).read_text(encoding="utf-8"))
    assert data["customField"] == "not-json-value"  # 非 JSON → 字面字符串


@pytest.mark.parametrize("scope", ["flag", "policy"])
def test_set_readonly_scopes_exit1(tmp_path: Path, scope: str) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    assert settings_cmd.settings_set(home, env, "deniedMcpServers", '["x"]', scope=scope, json_output=True) == 1


def test_set_project_without_active_workdir_exit1(tmp_path: Path) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    assert settings_cmd.settings_set(home, env, "foo", "1", scope="project", active_workdir=None, json_output=True) == 1


# ── get / show ──────────────────────────────────────────────────────────────
def test_get_missing_key_exit1(tmp_path: Path) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    assert settings_cmd.settings_get(home, env, "neverset", scope="user", json_output=True) == 1


def test_show_unknown_scope_exit1(tmp_path: Path) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    assert settings_cmd.settings_show(home, env, scope="bogus", json_output=True) == 1


def test_show_merged_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    settings_cmd.settings_set(home, env, "strictKnownMarketplaces", "true", scope="user")
    capsys.readouterr()
    assert settings_cmd.settings_show(home, env, scope="merged", json_output=True) == 0
    assert json.loads(capsys.readouterr().out).get("strictKnownMarketplaces") is True


# ── edit ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_edit_opens_editor_and_returns_reconcile_cb(tmp_path: Path) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    called = {"n": 0}

    async def _reconcile() -> None:
        called["n"] += 1

    # editor="true" → 子进程退出 0、不改文件；返回 (0, reconcile_cb)
    code, post_save = settings_cmd.settings_edit(home, env, scope="user", editor="true", reconcile_cb=_reconcile)
    assert code == 0
    assert post_save is _reconcile
    assert user_settings_path(env).exists()  # 空骨架已建
    await post_save()
    assert called["n"] == 1


@pytest.mark.parametrize("scope", ["flag", "policy"])
def test_edit_readonly_scopes_exit1(tmp_path: Path, scope: str) -> None:
    home, env = _home(tmp_path), _env(tmp_path)
    code, post_save = settings_cmd.settings_edit(home, env, scope=scope, editor="true", json_output=True)
    assert code == 1
    assert post_save is None
