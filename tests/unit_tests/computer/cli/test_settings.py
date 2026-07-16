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


def test_set_project_writes_cwd_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#116: project scope 锚定进程 cwd，无 workdir 状态也可写（不再 exit 1）。"""
    home, env = _home(tmp_path), _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert settings_cmd.settings_set(home, env, "strictKnownMarketplaces", "true", scope="project", json_output=True) == 0
    data = json.loads((tmp_path / ".tfrobot" / "settings.json").read_text(encoding="utf-8"))
    assert data["strictKnownMarketplaces"] is True


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


def test_show_merged_surfaces_scope_overreach_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#157：merged 视图里被 scope 越权过滤的字段须有解释——打 **stderr**，stdout 保持机读 JSON。

    ★ 「stderr 而非 stdout」是硬约束：``settings show`` 的 stdout 恒为 JSON（``| jq`` 消费），若把警示打进
    stdout，``json.loads`` 立刻碎（见同文件 ``test_show_merged_default``）。本测正向钉死该分流。
    Diagnostics go to stderr; stdout stays machine-readable JSON.
    """
    home, env = _home(tmp_path), _env(tmp_path)
    wd = tmp_path / "proj"
    (wd / ".tfrobot").mkdir(parents=True)
    (wd / ".tfrobot" / "settings.json").write_text(
        json.dumps({"enableAllProjectMcpServers": True, "strictKnownMarketplaces": True}), encoding="utf-8",
    )
    monkeypatch.chdir(wd)
    capsys.readouterr()

    assert settings_cmd.settings_show(home, env, scope="merged", json_output=True) == 0
    captured = capsys.readouterr()

    data = json.loads(captured.out)  # stdout 仍是纯 JSON（未被警示污染）
    assert "enableAllProjectMcpServers" not in data  # 越权字段已过滤出 merged 视图
    assert data.get("strictKnownMarketplaces") is True  # 同文件的合法字段照常保留
    assert "enableAllProjectMcpServers" in captured.err  # 解释在 stderr
    assert "settings.local.json" in captured.err  # 且给出可操作去向


def test_show_single_scope_surfaces_overreach_not_only_merged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#157（fix-review 🔴1）：**单 scope 读**同样须呈现越权错误，不能只有 merged 接线。

    ``settings show --scope project`` 正是用户排查「我的 settings 莫名不生效」时最先跑的命令——若此处吞
    错误，字段静默蒸发、诊断回路断裂。对拍 rust ``read_scope_with_errors``（对所有 scope 返回 errors）。
    """
    home, env = _home(tmp_path), _env(tmp_path)
    wd = tmp_path / "proj"
    (wd / ".tfrobot").mkdir(parents=True)
    (wd / ".tfrobot" / "settings.json").write_text(json.dumps({"enableAllProjectMcpServers": True}), encoding="utf-8")
    monkeypatch.chdir(wd)
    capsys.readouterr()

    assert settings_cmd.settings_show(home, env, scope="project", json_output=True) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {}  # 越权字段已过滤（stdout 仍纯 JSON）
    assert "enableAllProjectMcpServers" in captured.err  # ★ 单 scope 也有解释


def test_get_overreach_field_explains_instead_of_bare_not_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#157（fix-review 🔴1）：越权字段被过滤后，``settings get`` 的「not set in scope」是**主动误导**
    （文件里明明写了）——须同时在 stderr 给出真正原因。
    """
    home, env = _home(tmp_path), _env(tmp_path)
    wd = tmp_path / "proj"
    (wd / ".tfrobot").mkdir(parents=True)
    (wd / ".tfrobot" / "settings.json").write_text(json.dumps({"enableAllProjectMcpServers": True}), encoding="utf-8")
    monkeypatch.chdir(wd)
    capsys.readouterr()

    code = settings_cmd.settings_get(home, env, "enableAllProjectMcpServers", scope="project", json_output=True)
    captured = capsys.readouterr()
    assert code == 1  # 确实读不到（已被过滤）
    assert "enableAllProjectMcpServers" in captured.err  # 但用户被告知**为什么**
    assert "settings.local.json" in captured.err


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


# ── repl_dispatch（REPL 解析胶水层；fix-review #2/#3）/ REPL parse glue ─────────
class _FakeComp:
    """settings repl_dispatch 所需最小 fake（repl 用 os.environ 作 env，故 XDG 经 monkeypatch 隔离）。"""

    def __init__(self, home: Path) -> None:
        self.skill_home = home
        self._registered_workdirs: tuple[Path, ...] = ()
        self.active_workdir: Path | None = None
        self.dirty = 0

    def mark_skills_dirty(self) -> None:
        self.dirty += 1


@pytest.mark.asyncio
async def test_repl_set_reconstructs_spaced_json_and_stops_at_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    comp = _FakeComp(tmp_path / "home")
    # value 含空格（["a", "b"] 被空格切成 2 token）+ 尾随 --scope → 重建含空格 JSON、停在真 flag token（Group C）
    parts = ["settings", "set", "trustedMarketplaces", '["a",', '"b"]', "--scope", "user"]
    await settings_cmd.repl_dispatch(comp, parts, session=None)
    data = json.loads(user_settings_path(os.environ).read_text(encoding="utf-8"))
    assert data["trustedMarketplaces"] == ["a", "b"]


@pytest.mark.asyncio
async def test_repl_set_value_with_doubledash_substring_not_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    comp = _FakeComp(tmp_path / "home")
    # value 字面含 "--scope" 子串（非独立 flag token）→ 不应被截断（旧 _strip_scope_flag 的 bug，Group C 修复）
    parts = ["settings", "set", "note", "a--scope-thing", "--scope", "user"]
    await settings_cmd.repl_dispatch(comp, parts, session=None)
    data = json.loads(user_settings_path(os.environ).read_text(encoding="utf-8"))
    assert data["note"] == "a--scope-thing"  # 完整保留，不在 "--scope" 子串处截断


@pytest.mark.asyncio
async def test_repl_set_emit_key_marks_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    comp = _FakeComp(tmp_path / "home")
    await settings_cmd.repl_dispatch(comp, ["settings", "set", "enabledMcpjsonServers", '["x"]', "--json"], session=None)
    assert comp.dirty == 1  # 能力/MCP 字段写入 → 触发去抖 emit


@pytest.mark.asyncio
async def test_repl_get_and_unknown_subcommand_no_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    comp = _FakeComp(tmp_path / "home")
    # 缺失键 get（退出码 1，不抛）+ 未知子命令（no-op，不抛）
    await settings_cmd.repl_dispatch(comp, ["settings", "get", "neverset"], session=None)
    await settings_cmd.repl_dispatch(comp, ["settings", "bogus"], session=None)
    assert comp.dirty == 0
