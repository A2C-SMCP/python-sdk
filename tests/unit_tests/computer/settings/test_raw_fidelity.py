# -*- coding: utf-8 -*-
# filename: test_raw_fidelity.py
# @Time    : 2026/11/03
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
settings / mcp.json 写入 **raw 保真** 回归锁（#246，mirror rust-sdk#253）
Raw-fidelity regression lock for every settings / mcp.json writer (#246).

测试意图 / Test intentions：
- **raw 保真**：每条真写路径只改目标键，其余字段——包括被 :func:`validate_settings` 过滤掉的
  ``readRoots`` / ``landingRoot`` / ``enabledMcpjsonServers`` / ``enableAllProjectMcpServers``、
  policy-only 字段、非法 ``permissions`` 条目、未知顶层键——**逐字保留**。
  依据：``docs/design-0.2.1-cli-marketplace-ux.md`` §5.6「settings set 是单字段写、不整体重写文件」。
- **fail-closed**：目标文件 corrupt / 根非对象 → 可操作错误且**原文件字节不变**；目标键畸形同样拒绝
  （只有「键缺失」与「空文件」才当空值）。
- **A+（project ``readRoots``）**：写入期**保留**该键，读面继续拒绝其生效并给出诊断 + 写时 WARN。
  删除用户数据不是「让它不生效」的手段。

夹具约定与同目录其他测试一致：``tmp_path`` + 显式 ``env``（``XDG_CONFIG_HOME`` 重定向 user 层），
project/local 层锚定 ``monkeypatch.chdir`` 后的 cwd。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from a2c_smcp.computer.cli.commands import marketplace as marketplace_cmd
from a2c_smcp.computer.cli.commands import settings as settings_cmd
from a2c_smcp.computer.settings.installer import (
    PluginInstallError,
    _clear_enabled_entries_visible_layers,
    _write_enabled_plugin,
    _write_installed_plugin,
    migrate_legacy_installs,
    uninstall_plugin,
)
from a2c_smcp.computer.settings.mcp_config import (
    McpConfigCorruptError,
    McpWriteScope,
    approve_all_project_mcp,
    approve_mcp_server,
    deny_mcp_server,
    load_mcp_config_file,
    remove_mcp_server,
    upsert_mcp_server,
)
from a2c_smcp.computer.settings.raw import SettingsFileCorruptError, read_raw_object
from a2c_smcp.computer.settings.schema import SettingsScope
from a2c_smcp.computer.settings.scope import load_settings_file, resolve_settings, user_settings_path


# ── 辅助 / helpers ───────────────────────────────────────────────────────────
def _env(tmp_path: Path) -> dict[str, str]:
    """重定向 ``XDG_CONFIG_HOME`` → tmp，隔离 user 层 settings.json。"""
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _project_settings(wd: Path) -> Path:
    return wd / ".tfrobot" / "settings.json"


def _local_settings(wd: Path) -> Path:
    return wd / ".tfrobot" / "settings.local.json"


def _user_settings(env: dict[str, str]) -> Path:
    return user_settings_path(env)


# 被校验器过滤的字段全家桶（project scope）：四类 TRUSTED_SCOPE_ONLY + 非法 permissions 项 + 未知键。
# The full set of fields dropped by validate_settings in the project scope.
_PROJECT_RICH: dict = {
    "readRoots": ["/data/in"],
    "landingRoot": "/data/out",
    "enabledMcpjsonServers": ["bundle_a"],
    "enableAllProjectMcpServers": True,
    "permissions": {"additionalDirectories": ["/abs/ok", "relative/bad", 7]},
    "customUnknownKey": {"nested": [1, 2, 3]},
    "theme": "light",
}
_PROJECT_FILTERED_KEYS = (
    "readRoots",
    "landingRoot",
    "enabledMcpjsonServers",
    "enableAllProjectMcpServers",
    "permissions",
    "customUnknownKey",
)


# ── T1/T2/T3：settings_set 的 raw 保真 ──────────────────────────────────────
def test_settings_set_project_preserves_every_filtered_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#246 核心：project scope 改一个键 → 其余字段（含被过滤字段与未知键）逐字保留。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, _PROJECT_RICH)

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 0

    doc = _read_json(path)
    assert doc["theme"] == "dark"  # 目标键被改
    for key in _PROJECT_FILTERED_KEYS:  # 其余逐字保留
        assert doc[key] == _PROJECT_RICH[key], f"{key} 被写入路径丢弃或改写"


def test_settings_set_keeps_invalid_permissions_items_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非法 ``permissions.additionalDirectories`` 项（相对路径 / 非字符串）不得被静默删除。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    raw_permissions = {"additionalDirectories": ["relative/bad", 7, "/abs/ok"]}
    _write_json(path, {"permissions": raw_permissions, "theme": "light"})

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 0
    assert _read_json(path)["permissions"] == raw_permissions


def test_project_read_roots_survive_write_but_stay_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A+：project ``readRoots`` 写入期**保留**（不删用户数据），读面继续拒绝生效 + 写时 WARN。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"readRoots": ["/data/in"], "theme": "light"})
    capsys.readouterr()

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 0

    assert _read_json(path)["readRoots"] == ["/data/in"]  # 文件保留，不删

    resolved = resolve_settings(env=env, cwd=wd)
    assert "readRoots" not in resolved.settings  # 读面仍不生效
    assert any(e.field == "readRoots" for e in resolved.errors), "读面必须仍产生 readRoots 越权诊断"

    assert "readRoots" in capsys.readouterr().err, "写时须 WARN：该键已写入但不会生效"


def test_settings_set_warns_for_policy_only_field_in_writable_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """policy-only 字段写进 user/local/project 恒不生效 → 同样写入 + WARN（不删、不静默）。"""
    env = _env(tmp_path)
    capsys.readouterr()

    assert settings_cmd.settings_set(tmp_path, env, "allowedMcpServers", '["x"]', scope="user", json_output=True) == 0

    assert _read_json(_user_settings(env))["allowedMcpServers"] == ["x"]
    assert "allowedMcpServers" in capsys.readouterr().err


def test_inert_field_warning_emits_single_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """多个失效键合并为**一行** WARN（每次写只告警一次），且仍走 stderr 不污染 --json stdout。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(
        path,
        {"readRoots": ["/a"], "landingRoot": "/b", "allowedMcpServers": ["x"], "theme": "light"},
    )
    capsys.readouterr()

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 0

    captured = capsys.readouterr()
    # rich 会按终端宽度折行，故按**告警次数**（而非物理行数）断言「每次写只告警一次」。
    assert captured.err.count("will NOT take effect") == 1, captured.err
    for key in ("readRoots", "landingRoot", "allowedMcpServers"):
        assert key in captured.err
    assert json.loads(captured.out)["key"] == "theme"  # stdout 仍是干净 JSON


# ── T4：corrupt / 根非对象 → fail-closed（原文件字节不变）───────────────────
@pytest.mark.parametrize("payload", ["{ this is not valid json", "[]", '"a-bare-string"', "\x00\x01"])
def test_settings_set_fail_closed_on_unreadable_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """目标文件不可解析 / 根非对象 → 退出码 1 + 可操作错误；**原文件逐字节未动**。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 1
    assert path.read_text(encoding="utf-8") == payload  # 绝不静默覆盖


def test_settings_set_positive_control_same_fixture_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正对照：同夹具换成合法 JSON → 写入成功且文件确实变了（避免 T4 的「字节不变」空转）。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"theme": "light"})
    before = path.read_text(encoding="utf-8")

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 0
    assert path.read_text(encoding="utf-8") != before
    assert _read_json(path)["theme"] == "dark"


def test_settings_set_surfaces_actionable_error_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """错误须可操作：带路径 + 「文件未改动」。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken", encoding="utf-8")
    capsys.readouterr()

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)["error"]
    assert str(path) in payload
    assert "unchanged" in payload.lower()


# ── T6/T7：空文件 / 缺失文件仍可写 ──────────────────────────────────────────
@pytest.mark.parametrize("payload", ["", "   \n\t\n"])
def test_settings_set_treats_empty_file_as_empty_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """空文件（0 字节 / 纯空白）无数据可丢 → 视为 ``{}``，写入成功。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 0
    assert _read_json(path)["theme"] == "dark"


def test_settings_set_creates_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失文件 → 正常创建（现状不变）。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 0
    assert _read_json(path) == {"theme": "dark"}


# ── T5/T8：installer 三条真写路径 ───────────────────────────────────────────
def test_write_enabled_plugin_preserves_filtered_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_write_enabled_plugin``：只改 ``enabledPlugins[<pid>]``，其余字段逐字保留。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {**_PROJECT_RICH, "enabledPlugins": {"other@mp": False}})

    _write_enabled_plugin("acme@mp", True, "project", str(wd), env)

    doc = _read_json(path)
    assert doc["enabledPlugins"] == {"other@mp": False, "acme@mp": True}
    for key in _PROJECT_FILTERED_KEYS:
        assert doc[key] == _PROJECT_RICH[key], f"{key} 被 installer 丢弃"


def test_write_enabled_plugin_fail_closed_on_malformed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``enabledPlugins`` 非映射（含显式 null）→ fail-closed，原字节不变（现状是静默 return）。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"enabledPlugins": "not-a-map", "theme": "light"})
    before = path.read_text(encoding="utf-8")

    with pytest.raises(PluginInstallError):
        _write_enabled_plugin("acme@mp", None, "project", str(wd), env)  # 删除路径同样是写入
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("value", ["not-a-map", None, ["a"]])
def test_write_enabled_plugin_rejects_non_mapping_for_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"enabledPlugins": value})
    before = path.read_text(encoding="utf-8")

    with pytest.raises(PluginInstallError):
        _write_enabled_plugin("acme@mp", True, "project", str(wd), env)
    assert path.read_text(encoding="utf-8") == before


def test_write_installed_plugin_preserves_filtered_fields(tmp_path: Path) -> None:
    """``_write_installed_plugin``（user scope）：只改 ``installedPlugins``。"""
    env = _env(tmp_path)
    path = _user_settings(env)
    raw = {"installedPlugins": ["keep@mp"], "allowedMcpServers": ["x"], "customKey": {"a": 1}}
    _write_json(path, raw)

    _write_installed_plugin("other@mp", True, env)

    doc = _read_json(path)
    assert doc["installedPlugins"] == ["keep@mp", "other@mp"]
    assert doc["allowedMcpServers"] == ["x"]  # policy-only 越权字段不得被丢
    assert doc["customKey"] == {"a": 1}


@pytest.mark.parametrize("value", [{"a": 1}, "x", None])
def test_write_installed_plugin_fail_closed_on_malformed_target(tmp_path: Path, value: object) -> None:
    env = _env(tmp_path)
    path = _user_settings(env)
    _write_json(path, {"installedPlugins": value})
    before = path.read_text(encoding="utf-8")

    with pytest.raises(PluginInstallError):
        _write_installed_plugin("acme@mp", True, env)
    assert path.read_text(encoding="utf-8") == before


def test_migrate_legacy_installs_preserves_filtered_fields(tmp_path: Path) -> None:
    """``migrate_legacy_installs``（user scope）：写迁移标记键时不得抹掉其余字段。"""
    env = _env(tmp_path)
    home = tmp_path / "skill-home"
    home.mkdir()
    path = _user_settings(env)
    raw = {"allowedMcpServers": ["x"], "customKey": [1, 2], "permissions": {"additionalDirectories": ["rel/bad"]}}
    _write_json(path, raw)

    assert migrate_legacy_installs(home, env=env) == []  # 空账本 → 只写标记键 []

    doc = _read_json(path)
    assert doc["installedPlugins"] == []
    assert doc["allowedMcpServers"] == ["x"]
    assert doc["customKey"] == [1, 2]
    assert doc["permissions"] == {"additionalDirectories": ["rel/bad"]}


def test_migrate_legacy_installs_guard_sees_raw_marker(tmp_path: Path) -> None:
    """迁移标记判据读 **raw**：``installedPlugins`` 存在但类型非法时也算「已迁移」，不得重复迁入。"""
    env = _env(tmp_path)
    home = tmp_path / "skill-home"
    home.mkdir()
    path = _user_settings(env)
    _write_json(path, {"installedPlugins": "tampered", "customKey": 1})

    assert migrate_legacy_installs(home, env=env) == []  # 标记键在 → 严格 no-op
    assert _read_json(path)["installedPlugins"] == "tampered"  # 不被覆盖/修复


def test_migrate_legacy_installs_fail_closed_on_corrupt(tmp_path: Path) -> None:
    env = _env(tmp_path)
    home = tmp_path / "skill-home"
    home.mkdir()
    path = _user_settings(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken", encoding="utf-8")

    with pytest.raises(PluginInstallError):  # installer 层错误契约：翻译为 PluginInstallError
        migrate_legacy_installs(home, env=env)
    assert path.read_text(encoding="utf-8") == "{ broken"


# ── T9：marketplace trust 写 ────────────────────────────────────────────────
def test_record_trust_preserves_unknown_keys_and_entries(tmp_path: Path) -> None:
    """``_record_trust``：只追加数组元素，未知键与非字符串条目一律保留。"""
    env = _env(tmp_path)
    path = _user_settings(env)
    _write_json(path, {"trustedMarketplaces": ["a", 7], "customKey": {"x": 1}})

    marketplace_cmd._record_trust("b", env)

    doc = _read_json(path)
    assert doc["trustedMarketplaces"] == ["a", 7, "b"]  # 非字符串条目 7 不得被过滤
    assert doc["customKey"] == {"x": 1}


def test_revoke_trust_preserves_unknown_keys_and_entries(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = _user_settings(env)
    _write_json(path, {"trustedMarketplaces": ["a", 7, "b"], "customKey": {"x": 1}})

    marketplace_cmd._revoke_trust("a", env)

    doc = _read_json(path)
    assert doc["trustedMarketplaces"] == [7, "b"]
    assert doc["customKey"] == {"x": 1}


@pytest.mark.parametrize("value", [5, "x", None, {"a": 1}])
def test_record_trust_fail_closed_on_malformed_target(tmp_path: Path, value: object) -> None:
    env = _env(tmp_path)
    path = _user_settings(env)
    _write_json(path, {"trustedMarketplaces": value})
    before = path.read_text(encoding="utf-8")

    with pytest.raises(SettingsFileCorruptError):
        marketplace_cmd._record_trust("b", env)
    assert path.read_text(encoding="utf-8") == before


def test_record_trust_fail_closed_on_corrupt_file(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = _user_settings(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken", encoding="utf-8")

    with pytest.raises(SettingsFileCorruptError):
        marketplace_cmd._record_trust("b", env)
    assert path.read_text(encoding="utf-8") == "{ broken"


# ── T10：MCP 批准写助手（local scope；落点 = cwd/.tfrobot/settings.local.json）──────
def test_approve_mcp_server_preserves_filtered_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``approve_mcp_server``：只追加 local 数组，其余字段（含 policy-only 越权字段）保留。"""
    monkeypatch.chdir(tmp_path)
    path = _local_settings(tmp_path)
    _write_json(path, {"enabledMcpjsonServers": ["b1"], "allowedMcpServers": ["x"], "customKey": {"k": "v"}})

    approve_mcp_server("b2")

    doc = _read_json(path)
    assert doc["enabledMcpjsonServers"] == ["b1", "b2"]
    assert doc["allowedMcpServers"] == ["x"]
    assert doc["customKey"] == {"k": "v"}


def test_deny_mcp_server_preserves_non_string_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _local_settings(tmp_path)
    _write_json(path, {"disabledMcpjsonServers": ["b1", 9], "customKey": 1})

    deny_mcp_server("b2")

    doc = _read_json(path)
    assert doc["disabledMcpjsonServers"] == ["b1", 9, "b2"]  # 非字符串条目 9 保留
    assert doc["customKey"] == 1


def test_approve_all_project_mcp_preserves_filtered_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _local_settings(tmp_path)
    _write_json(path, {"allowedMcpServers": ["x"], "customKey": [1]})

    approve_all_project_mcp()

    doc = _read_json(path)
    assert doc["enableAllProjectMcpServers"] is True
    assert doc["allowedMcpServers"] == ["x"]
    assert doc["customKey"] == [1]


@pytest.mark.parametrize("value", ["x", 5, None])
def test_approve_mcp_server_fail_closed_on_malformed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _local_settings(tmp_path)
    _write_json(path, {"enabledMcpjsonServers": value})
    before = path.read_text(encoding="utf-8")

    with pytest.raises(McpConfigCorruptError):
        approve_mcp_server("b2")
    assert path.read_text(encoding="utf-8") == before


def test_approve_mcp_server_fail_closed_on_corrupt_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _local_settings(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken", encoding="utf-8")

    with pytest.raises(McpConfigCorruptError):
        approve_mcp_server("b2")
    assert path.read_text(encoding="utf-8") == "{ broken"


# ── T11：mcp.json 未知顶层键保真 ───────────────────────────────────────────
def _stdio(command: str = "node") -> dict:
    return {"type": "stdio", "server_parameters": {"command": command}}


def test_upsert_mcp_server_preserves_unknown_top_level_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``upsert_mcp_server`` 只改 ``servers``/``inputs``，``$schema`` 等未知顶层键逐字保留。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = wd / ".tfrobot" / "mcp.json"
    _write_json(path, {"$schema": "https://example/schema.json", "servers": {}, "inputs": [], "customTop": {"a": 1}})

    upsert_mcp_server("figma", _stdio(), scope=McpWriteScope.PROJECT, env=env, cwd=wd)

    doc = _read_json(path)
    assert doc["$schema"] == "https://example/schema.json"
    assert doc["customTop"] == {"a": 1}
    assert doc["servers"] == {"figma": _stdio()}


def test_remove_mcp_server_preserves_unknown_top_level_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = wd / ".tfrobot" / "mcp.json"
    _write_json(path, {"$schema": "s", "servers": {"figma": _stdio()}, "inputs": [], "customTop": 2})

    assert remove_mcp_server("figma", env=env, cwd=wd) is True

    doc = _read_json(path)
    assert doc["$schema"] == "s"
    assert doc["customTop"] == 2
    assert doc["servers"] == {}


def test_upsert_new_mcp_file_keeps_normalized_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """新文件仍产出规整 ``{servers, inputs}``（既有形状契约不破）。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)

    upsert_mcp_server("figma", _stdio(), scope=McpWriteScope.LOCAL, env=env, cwd=wd)

    assert _read_json(wd / ".tfrobot" / "mcp.local.json") == {"servers": {"figma": _stdio()}, "inputs": []}


# ── T12：幂等 ──────────────────────────────────────────────────────────────
def test_settings_set_is_content_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同值重复写：内容语义相等（不因保留字段而累积漂移）。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, _PROJECT_RICH)

    settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True)
    first = _read_json(path)
    settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True)

    assert _read_json(path) == first


# ── 独立审查回归（#246 审查 3🔴 + 覆盖空洞）─────────────────────────────────
# 非 UTF-8 字节（Windows 记事本「Unicode」存盘即 UTF-16LE 带 BOM）。解码失败抛
# UnicodeDecodeError（ValueError 子类，**非** OSError）——漏捕会让裸解码异常穿透全部错误翻译层。
_NON_UTF8 = b"\xff\xfe{\x00}\x00"


def test_read_raw_object_fail_closed_on_non_utf8(tmp_path: Path) -> None:
    """🔴1：非 UTF-8 文件必须归入 `SettingsFileCorruptError`，不是裸 `UnicodeDecodeError`。"""
    path = tmp_path / "settings.json"
    path.write_bytes(_NON_UTF8)

    with pytest.raises(SettingsFileCorruptError) as ei:
        read_raw_object(path)
    assert "unchanged" in str(ei.value).lower()


def test_settings_set_fail_closed_on_non_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴1：`settings_set` 走 `_err` 退出码 1，而非把 `UnicodeDecodeError` 泄漏成 traceback。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_NON_UTF8)

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 1
    assert path.read_bytes() == _NON_UTF8


def test_write_enabled_plugin_fail_closed_on_non_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴1：installer 层把非 UTF-8 翻译成 `PluginInstallError`（而非逃出 `UnicodeDecodeError`）。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_NON_UTF8)

    with pytest.raises(PluginInstallError):
        _write_enabled_plugin("acme@mp", True, "project", str(wd), env)
    assert path.read_bytes() == _NON_UTF8


def test_read_side_loaders_tolerate_non_utf8(tmp_path: Path) -> None:
    """读面容错契约同样覆盖非 UTF-8：不崩溃，返回空视图 + 一条错误、文件留盘。"""
    settings_path = tmp_path / "settings.json"
    settings_path.write_bytes(_NON_UTF8)
    data, errors = load_settings_file(settings_path, SettingsScope.USER)
    assert data == {}
    assert errors and errors[0].field == "<file>"

    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_bytes(_NON_UTF8)
    mcp_data, mcp_errors = load_mcp_config_file(mcp_path, SettingsScope.USER)
    assert mcp_data == {"servers": {}, "inputs": []}
    assert mcp_errors
    assert settings_path.read_bytes() == _NON_UTF8 and mcp_path.read_bytes() == _NON_UTF8


@pytest.mark.parametrize("value", ["yes", None, [], {}, 1])
def test_approve_all_project_mcp_fail_closed_on_malformed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    """🔴2：「无条件写 true」的写助手同样要过形状守卫，不得把畸形值静默换成 `true`。"""
    monkeypatch.chdir(tmp_path)
    path = _local_settings(tmp_path)
    _write_json(path, {"enableAllProjectMcpServers": value, "keep": 1})
    before = path.read_text(encoding="utf-8")

    with pytest.raises(McpConfigCorruptError):
        approve_all_project_mcp()
    assert path.read_text(encoding="utf-8") == before


def test_approve_all_project_mcp_allows_absent_and_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：键缺失、或既存合法 `false` 时都应写成功。"""
    monkeypatch.chdir(tmp_path)
    path = _local_settings(tmp_path)
    _write_json(path, {"enableAllProjectMcpServers": False, "keep": 1})

    approve_all_project_mcp()

    assert _read_json(path) == {"enableAllProjectMcpServers": True, "keep": 1}


def _seed_ledger(home: Path, pid: str) -> None:
    """造一份最小 installed_plugins 账本（migrate 的 pid 来源）。"""
    _write_json(home / "installed_plugins.json", {"version": 1, "plugins": {pid: [{"scope": "user", "installPath": "/tmp/x"}]}})


@pytest.mark.parametrize("value", ["garbage", None, [], ["a@mp"]])
def test_migrate_fail_closed_on_malformed_enabled_container(tmp_path: Path, value: object) -> None:
    """🔴3：`enabledPlugins` 整字段非对象 → fail-closed，不得被 `apply_write` 整体替换成新对象。"""
    env = _env(tmp_path)
    home = tmp_path / "skill-home"
    home.mkdir()
    _seed_ledger(home, "a@mp")
    path = _user_settings(env)
    _write_json(path, {"enabledPlugins": value})
    before = path.read_text(encoding="utf-8")

    with pytest.raises(PluginInstallError):
        migrate_legacy_installs(home, env=env)
    assert path.read_text(encoding="utf-8") == before


def test_migrate_does_not_overwrite_malformed_existing_entry(tmp_path: Path) -> None:
    """🔴3：该 pid 在**原始** `enabledPlugins` 里已存在（值畸形、被校验视图过滤）→ 不覆盖它。

    过去的判据只看校验后的 merged 视图 ⇒ 畸形值被过滤 ⇒ 误判「无条目」⇒ 回填 `true` 把用户原值冲掉。
    """
    env = _env(tmp_path)
    home = tmp_path / "skill-home"
    home.mkdir()
    _seed_ledger(home, "a@mp")
    path = _user_settings(env)
    _write_json(path, {"enabledPlugins": {"a@mp": "yes"}})

    assert migrate_legacy_installs(home, env=env) == ["a@mp"]

    doc = _read_json(path)
    assert doc["installedPlugins"] == ["a@mp"]  # 迁移本体照做
    assert doc["enabledPlugins"] == {"a@mp": "yes"}  # 原值逐字保留，不回填 true


def test_inert_field_warning_silent_when_nothing_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """WARN 负对照：无失效键（含 `$schema` 等未知键）时**不得**告警，否则会训练用户忽略它。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"theme": "light", "$schema": "https://example/schema.json", "disabledMcpjsonServers": ["x"]})
    capsys.readouterr()

    assert settings_cmd.settings_set(tmp_path, env, "theme", "dark", scope="project", json_output=True) == 0

    assert capsys.readouterr().err == ""


# ── 第二轮独立审查：🔴A / 🔴B / 🟡1 / 🟡2 / 🟡6 / 🟡11 ─────────────────────
def test_settings_set_object_over_non_object_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🟡6（对齐 rust `ensure_object_container`）：本次写对象、盘上该键是非对象 → 退出码 1，原文件不动。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"permissions": "invalid-not-an-object", "keep": 1})
    before = path.read_text(encoding="utf-8")

    code = settings_cmd.settings_set(
        tmp_path, env, "permissions", '{"additionalDirectories": ["/x"]}', scope="project", json_output=True
    )
    assert code == 1
    assert path.read_text(encoding="utf-8") == before  # 畸形值不被静默替换


def test_settings_set_object_over_object_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """🟡6 正对照：盘上该键本就是对象 → 正常写入（深合并）。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"permissions": {"additionalDirectories": ["/old"]}, "keep": 1})

    code = settings_cmd.settings_set(
        tmp_path, env, "permissions", '{"additionalDirectories": ["/new"]}', scope="project", json_output=True
    )
    assert code == 0
    assert _read_json(path) == {"permissions": {"additionalDirectories": ["/new"]}, "keep": 1}


def test_settings_set_project_read_roots_is_ignored_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🟡11（对齐 rust）：project 的 `readRoots` **不接收新值**、**不删旧键**、退出码 0、`--json` 带 `ignored`。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"readRoots": ["/old"], "theme": "light"})
    before = path.read_text(encoding="utf-8")
    capsys.readouterr()

    assert settings_cmd.settings_set(tmp_path, env, "readRoots", '["/new"]', scope="project", json_output=True) == 0

    assert path.read_text(encoding="utf-8") == before  # 新值不写、旧值不删
    payload = json.loads(capsys.readouterr().out)
    assert payload["ignored"] is True
    assert payload["key"] == "readRoots"
    assert payload["reason"]


def test_settings_set_project_read_roots_absent_stays_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🟡11 另一面：盘上没有该键时也不得凭空写入。"""
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = _project_settings(wd)
    _write_json(path, {"theme": "light"})
    before = path.read_text(encoding="utf-8")

    assert settings_cmd.settings_set(tmp_path, env, "readRoots", '["/new"]', scope="project", json_output=True) == 0
    assert path.read_text(encoding="utf-8") == before


def test_upsert_mcp_server_rejects_null_servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """🟡2：`servers: null` 不是「空配置」—— 形状守卫必须 fail-closed 而不是直接覆盖。

    注意 `load_mcp_config_file` 对 ``servers: null`` **不**报错（当缺失处理），所以这道守卫是唯一的拦截点。
    """
    env, wd = _env(tmp_path), tmp_path
    monkeypatch.chdir(wd)
    path = wd / ".tfrobot" / "mcp.json"
    _write_json(path, {"servers": None, "inputs": None, "keep": 1})
    before = path.read_text(encoding="utf-8")

    with pytest.raises(McpConfigCorruptError):
        upsert_mcp_server("figma", _stdio(), scope=McpWriteScope.PROJECT, env=env, cwd=wd)
    assert path.read_text(encoding="utf-8") == before


class _StubRegistry:
    """uninstall/disable 在触发预检前不需要真实 registry（先于任何 skill 注销）。"""

    def unregister(self, name: str) -> None:  # pragma: no cover - 预检失败时不可达
        raise AssertionError(f"unregister({name!r}) must not be reached")


def _seed_plugin(home: Path, pid: str, install_dir: Path) -> None:
    _write_json(
        home / "installed_plugins.json",
        {"version": 1, "plugins": {pid: [{"scope": "user", "installPath": str(install_dir)}]}},
    )


@pytest.mark.asyncio
async def test_uninstall_precheck_fails_closed_before_destructive_steps(tmp_path: Path) -> None:
    """🟡1：目标键畸形必须在**删树/删账本之前** fail-closed，否则重试因账本已空永远清不干净。"""
    env = _env(tmp_path)
    home = tmp_path / "skill-home"
    installed_tree = tmp_path / "installed-tree"
    installed_tree.mkdir()
    _seed_plugin(home, "a@mp", installed_tree)
    _write_json(_user_settings(env), {"installedPlugins": "oops"})  # 畸形 → 预检失败

    with pytest.raises(PluginInstallError):
        await uninstall_plugin("a@mp", _StubRegistry(), home, non_plugin_bundle_ids=set, env=env)

    assert installed_tree.exists(), "预检失败不得删安装树"
    assert "a@mp" in _read_json(home / "installed_plugins.json")["plugins"], "预检失败不得删账本"


@pytest.mark.asyncio
async def test_clear_enabled_user_layer_failure_is_best_effort(tmp_path: Path) -> None:
    """🔴B：user 层清理失败只 WARN，不得打断已完成的卸载（三层同姿态）。"""
    env = _env(tmp_path)
    path = _user_settings(env)
    _write_json(path, {"enabledPlugins": "not-a-map"})

    _clear_enabled_entries_visible_layers("a@mp", set(), env)  # 不抛

    assert path.read_text(encoding="utf-8") == '{"enabledPlugins": "not-a-map"}'


def test_plugin_uninstall_translates_plugin_install_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴A：`plugin_uninstall` 必须把 `PluginInstallError` 翻成退出码 1，而不是逃成 traceback。"""
    import asyncio

    from a2c_smcp.computer.cli.commands import plugin as plugin_cmd

    async def _boom(*_a: object, **_kw: object) -> bool:
        raise PluginInstallError("boom: settings corrupt")

    monkeypatch.setattr(plugin_cmd, "uninstall_plugin", _boom)
    capsys.readouterr()
    code = asyncio.run(
        plugin_cmd.plugin_uninstall(_StubRegistry(), tmp_path, _env(tmp_path), "a@mp", non_plugin_bundle_ids=set, json_output=True)  # type: ignore[arg-type]
    )
    assert code == 1
    assert "boom" in capsys.readouterr().out


def test_plugin_disable_translates_plugin_install_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴A：`plugin_disable` 同上。"""
    import asyncio

    from a2c_smcp.computer.cli.commands import plugin as plugin_cmd

    monkeypatch.setattr(
        plugin_cmd,
        "_installed_records",
        lambda *_a, **_kw: [{"scope": "user"}],
    )

    async def _boom(*_a: object, **_kw: object) -> None:
        raise PluginInstallError("boom: settings corrupt")

    monkeypatch.setattr(plugin_cmd, "disable_plugin", _boom)
    capsys.readouterr()
    code = asyncio.run(
        plugin_cmd.plugin_disable(  # type: ignore[arg-type]
            _StubRegistry(), tmp_path, _env(tmp_path), "a@mp", non_plugin_bundle_ids=set, json_output=True
        )
    )
    assert code == 1
    assert "boom" in capsys.readouterr().out


def test_marketplace_remove_fails_per_victim_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴A：`marketplace_remove` 按 victim 捕获——一个失败不得让其余 victim 一个都不处理，且须可感（退出码 1）。"""
    import asyncio

    env = _env(tmp_path)
    home = tmp_path / "skill-home"
    _write_json(
        home / "known_marketplaces.json",
        {"version": 1, "marketplaces": {"mp": {"source": {"type": "git", "url": "u"}, "installLocation": ""}}},
    )
    _write_json(
        home / "installed_plugins.json",
        {
            "version": 1,
            "plugins": {
                "a@mp": [{"scope": "user", "installPath": "/tmp/a"}],
                "b@mp": [{"scope": "user", "installPath": "/tmp/b"}],
            },
        },
    )
    seen: list[str] = []

    async def _uninstall(pid: str, *_a: object, **_kw: object) -> bool:
        seen.append(pid)
        if pid == "a@mp":
            raise PluginInstallError("boom: settings corrupt")
        return True

    monkeypatch.setattr(marketplace_cmd, "uninstall_plugin", _uninstall)
    monkeypatch.setattr(marketplace_cmd, "prune_marketplaces", lambda *_a, **_kw: 0)
    capsys.readouterr()

    code = asyncio.run(
        marketplace_cmd.marketplace_remove(
            _StubRegistry(), home, env, "mp", json_output=True  # type: ignore[arg-type]
        )
    )
    assert seen == ["a@mp", "b@mp"], "单个 victim 失败不得中断其余"
    assert code == 1, "部分失败必须可感（退出码 1）"
