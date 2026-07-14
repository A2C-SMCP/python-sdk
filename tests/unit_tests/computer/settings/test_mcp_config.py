# -*- coding: utf-8 -*-
# filename: test_mcp_config.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``.tfrobot/mcp.json`` 定义层 + MCP 批准门控单元测试（v0.2.1 #64）
``.tfrobot/mcp.json`` definition layer + MCP approval-gate unit tests.

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §9.1 / §9.2 / §5.1 / §5.5。

测试意图 / Test intentions（纯文件系统、无 git / manager；env 重定向 XDG_CONFIG_HOME 隔离 user mcp.json）:
- load：缺失→空；损坏/根非对象→空+err；servers 非对象/inputs 非数组→该字段空+err。
- merge：scope 优先级 policy>local>project>user>flag；同名 server 高 scope 整体覆盖 + origin；无 active →
  仅 user+flag+policy；inputs 按 id 去重高胜。
- ext 剥离：envFile 入 ext + 配置校验通过；含真未知 key 的 server → drop + err（字段容错、不 abort）。
- gate：未知→pending；enabled/disabled（disabled 优先）；enableAll；plugin-bundled 免批准；user-origin 自动
  enabled；policy deny→disabled；policy allow 白名单。
- 写助手：approve/deny/approveAll 写 active-workdir local（dedup、无 version/header）；缺 active → 抛。
- bundled 接缝：bundled_mcp_server_names 跨多记录并集。
"""

import json
import os
from pathlib import Path

import pytest

from a2c_smcp.computer.mcp_clients.model import StdioServerConfig
from a2c_smcp.computer.settings.mcp_config import (
    MANAGED_MCP_FILENAME,
    McpApprovalStatus,
    McpConfigCorruptError,
    McpUpsertResult,
    McpWriteScope,
    McpWriteTargetError,
    ResolvedMcpConfig,
    approve_all_project_mcp,
    approve_mcp_server,
    bundled_mcp_server_names,
    deny_mcp_server,
    gate_mcp_servers,
    load_mcp_config_file,
    managed_mcp_config_path,
    mcp_server_status,
    mcp_write_path,
    remove_mcp_server,
    resolve_mcp_config,
    upsert_mcp_server,
)
from a2c_smcp.computer.settings.policy import LINUX_MANAGED_DIR, MACOS_MANAGED_DIR, WINDOWS_MANAGED_DIR
from a2c_smcp.computer.settings.schema import SettingsScope
from a2c_smcp.computer.settings.scope import workdir_local_settings_path
from a2c_smcp.computer.settings.store import save_installed_plugins


# ── 辅助 / helpers ───────────────────────────────────────────────────────────
def _env(tmp_path: Path) -> dict[str, str]:
    """重定向 XDG_CONFIG_HOME → tmp，隔离 user mcp.json / settings.json 写。"""
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _stdio(command: str = "node") -> dict:
    """无 name 的 stdio server 定义体（name 由 mcp.json map key 注入）。"""
    return {"type": "stdio", "server_parameters": {"command": command}}


def _user_mcp(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "a2c" / "mcp.json"


def _mcp_doc(servers: dict | None = None, inputs: list | None = None) -> dict:
    return {"servers": servers or {}, "inputs": inputs or []}


# ── load_mcp_config_file ──────────────────────────────────────────────────────
def test_load_missing_returns_empty(tmp_path: Path) -> None:
    data, errors = load_mcp_config_file(tmp_path / "nope.json", SettingsScope.USER)
    assert data == {"servers": {}, "inputs": []}
    assert errors == []


def test_load_corrupt_json_returns_empty_plus_error(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text("{bad json", encoding="utf-8")
    data, errors = load_mcp_config_file(p, SettingsScope.USER)
    assert data == {"servers": {}, "inputs": []}
    assert len(errors) == 1 and errors[0].field == "<file>"


def test_load_non_object_root_returns_empty_plus_error(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    _write_json(p, ["not", "an", "object"])
    data, errors = load_mcp_config_file(p, SettingsScope.USER)
    assert data == {"servers": {}, "inputs": []}
    assert len(errors) == 1 and errors[0].field == "<root>"


def test_load_wrong_typed_fields_emptied_with_errors(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    _write_json(p, {"servers": ["oops"], "inputs": {"oops": 1}})
    data, errors = load_mcp_config_file(p, SettingsScope.USER)
    assert data == {"servers": {}, "inputs": []}
    assert {e.field for e in errors} == {"servers", "inputs"}


def test_load_ok(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    _write_json(p, _mcp_doc(servers={"figma": _stdio()}, inputs=[{"id": "tok", "type": "promptString", "description": "d"}]))
    data, errors = load_mcp_config_file(p, SettingsScope.USER)
    assert list(data["servers"]) == ["figma"] and len(data["inputs"]) == 1
    assert errors == []


# ── resolve_mcp_config：merge / scope ─────────────────────────────────────────
def test_resolve_user_only_no_active(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio()}))
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent-managed.json")
    assert set(out.servers) == {"figma"}
    srv = out.servers["figma"]
    assert isinstance(srv.config, StdioServerConfig) and srv.config.name == "figma"
    assert srv.origin == SettingsScope.USER and srv.trusted_origin is True


def test_resolve_scope_priority_high_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同名 server：local(高) 整体覆盖 user(低)，origin 记最高 scope（project/local 锚 cwd，#116）。"""
    env = _env(tmp_path)
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio("user-cmd")}))
    _write_json(tmp_path / ".tfrobot" / "mcp.local.json", _mcp_doc(servers={"figma": _stdio("local-cmd")}))
    monkeypatch.chdir(tmp_path)
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    srv = out.servers["figma"]
    assert srv.origin == SettingsScope.LOCAL and srv.trusted_origin is False  # workspace-shared、受门控
    assert isinstance(srv.config, StdioServerConfig)
    assert srv.config.server_parameters.command == "local-cmd"  # 整体覆盖（高胜）


def test_resolve_origins_partition_trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """user/flag/policy → trusted；project/local（锚 cwd）→ 受门控。"""
    env = _env(tmp_path)
    flag = tmp_path / "flag-mcp.json"
    managed = tmp_path / "managed-mcp.json"
    _write_json(flag, _mcp_doc(servers={"flag-srv": _stdio()}))
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"user-srv": _stdio()}))
    _write_json(tmp_path / ".tfrobot" / "mcp.json", _mcp_doc(servers={"proj-srv": _stdio()}))
    _write_json(managed, _mcp_doc(servers={"policy-srv": _stdio()}))
    monkeypatch.chdir(tmp_path)
    out = resolve_mcp_config(env=env, flag_config_path=flag, managed_mcp_path=managed)
    trust = {n: s.trusted_origin for n, s in out.servers.items()}
    assert trust == {"flag-srv": True, "user-srv": True, "proj-srv": False, "policy-srv": True}


def test_resolve_inputs_dedup_by_id_high_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path)
    _write_json(_user_mcp(tmp_path), _mcp_doc(inputs=[{"id": "tok", "type": "promptString", "description": "user"}]))
    local_inputs = [
        {"id": "tok", "type": "promptString", "description": "local"},
        {"id": "other", "type": "promptString", "description": "x"},
    ]
    _write_json(tmp_path / ".tfrobot" / "mcp.local.json", _mcp_doc(inputs=local_inputs))
    monkeypatch.chdir(tmp_path)
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    by_id = {i.id: i for i in out.inputs}
    assert set(by_id) == {"tok", "other"}
    assert by_id["tok"].description == "local"  # 高 scope 胜


# ── ext 剥离 + 字段级容错 ──────────────────────────────────────────────────────
def test_resolve_envfile_stripped_to_ext(tmp_path: Path) -> None:
    """envFile 是 VS Code 扩展（非 MCPServerConfig 字段）→ 剥离入 ext，配置仍校验通过。"""
    env = _env(tmp_path)
    sdef = {**_stdio(), "envFile": "${workspaceFolder}/.env"}
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"figma": sdef}))
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    srv = out.servers["figma"]
    assert srv.ext == {"envFile": "${workspaceFolder}/.env"}
    assert out.errors == []


def test_resolve_placeholders_preserved_unrendered(tmp_path: Path) -> None:
    """${input:}/${env:} 占位符是字符串、校验无碍，且原样保留（不渲染 → 值不离 Computer）。"""
    env = _env(tmp_path)
    sdef = {"type": "stdio", "server_parameters": {"command": "node", "env": {"TOK": "${input:figma_token}"}}}
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"figma": sdef}))
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    assert isinstance(out.servers["figma"].config, StdioServerConfig)
    assert out.servers["figma"].config.server_parameters.env == {"TOK": "${input:figma_token}"}


def test_resolve_malformed_server_dropped_not_abort(tmp_path: Path) -> None:
    """单 server 含真未知 key（extra=forbid）→ drop + 记错；其余 server 存活（不 abort）。"""
    env = _env(tmp_path)
    servers = {"good": _stdio(), "bad": {**_stdio(), "bogus_field": 1}}
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers=servers))
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    assert set(out.servers) == {"good"}  # bad 被 drop
    assert any(e.field == "servers.bad" for e in out.errors)


def test_resolve_server_name_key_mismatch_dropped(tmp_path: Path) -> None:
    env = _env(tmp_path)
    sdef = {**_stdio(), "name": "different"}  # 内嵌 name 与 map key 冲突
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"figma": sdef}))
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    assert out.servers == {}
    assert any("!= map key" in e.reason for e in out.errors)


def test_resolve_malformed_input_dropped(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_json(_user_mcp(tmp_path), _mcp_doc(inputs=[{"id": "ok", "type": "promptString", "description": "d"}, {"id": "bad"}]))
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    assert {i.id for i in out.inputs} == {"ok"}  # 缺 description 的 bad 被 drop
    assert any(e.field == "inputs.bad" for e in out.errors)


# ── gate：mcp_server_status / gate_mcp_servers ────────────────────────────────
def test_status_pending_when_unknown_workspace(tmp_path: Path) -> None:
    assert mcp_server_status("x", settings={}, bundled=set(), trusted_origin=False) == McpApprovalStatus.PENDING


def test_status_enabled_via_enabled_list() -> None:
    s = {"enabledMcpjsonServers": ["x"]}
    assert mcp_server_status("x", settings=s, bundled=set(), trusted_origin=False) == McpApprovalStatus.ENABLED


def test_status_disabled_takes_priority_over_enabled() -> None:
    s = {"enabledMcpjsonServers": ["x"], "disabledMcpjsonServers": ["x"]}
    assert mcp_server_status("x", settings=s, bundled=set(), trusted_origin=False) == McpApprovalStatus.DISABLED


def test_status_enable_all() -> None:
    s = {"enableAllProjectMcpServers": True}
    assert mcp_server_status("x", settings=s, bundled=set(), trusted_origin=False) == McpApprovalStatus.ENABLED


def test_status_bundled_auto_enabled_even_if_not_listed() -> None:
    assert mcp_server_status("x", settings={}, bundled={"x"}, trusted_origin=False) == McpApprovalStatus.ENABLED


def test_status_disabled_overrides_bundled() -> None:
    """disabled（显式拒绝）优先于 bundled 免批准。"""
    s = {"disabledMcpjsonServers": ["x"]}
    assert mcp_server_status("x", settings=s, bundled={"x"}, trusted_origin=False) == McpApprovalStatus.DISABLED


def test_status_trusted_origin_auto_enabled() -> None:
    assert mcp_server_status("x", settings={}, bundled=set(), trusted_origin=True) == McpApprovalStatus.ENABLED


def test_status_policy_deny() -> None:
    s = {"deniedMcpServers": ["x"], "enabledMcpjsonServers": ["x"]}
    assert mcp_server_status("x", settings=s, bundled={"x"}, trusted_origin=True) == McpApprovalStatus.DISABLED


def test_status_policy_allow_whitelist() -> None:
    s = {"allowedMcpServers": ["other"]}  # 非空白名单且 x 不在内 → disabled
    assert mcp_server_status("x", settings=s, bundled=set(), trusted_origin=True) == McpApprovalStatus.DISABLED
    s2 = {"allowedMcpServers": ["x"]}
    assert mcp_server_status("x", settings=s2, bundled=set(), trusted_origin=False) == McpApprovalStatus.PENDING


def test_gate_mcp_servers_maps_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path)
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"user-srv": _stdio()}))
    _write_json(tmp_path / ".tfrobot" / "mcp.json", _mcp_doc(servers={"proj-srv": _stdio()}))
    monkeypatch.chdir(tmp_path)
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    statuses = gate_mcp_servers(out, settings={}, bundled=set())
    assert statuses == {"user-srv": McpApprovalStatus.ENABLED, "proj-srv": McpApprovalStatus.PENDING}


# ── bundled 接缝 ──────────────────────────────────────────────────────────────
def test_bundled_mcp_server_names_union(tmp_path: Path) -> None:
    save_installed_plugins(
        {
            "plugins": {
                "a@mp": [{"scope": "user", "installPath": "/x", "bundledMcpServers": ["figma", "blender"]}],
                "b@mp": [{"scope": "user", "installPath": "/y", "bundledMcpServers": ["blender", "godot"]}],
            }
        },
        home=tmp_path,
    )
    assert bundled_mcp_server_names(home=tmp_path) == {"figma", "blender", "godot"}


def test_bundled_mcp_server_names_empty(tmp_path: Path) -> None:
    assert bundled_mcp_server_names(home=tmp_path) == set()


# ── 批准写助手（local scope）─────────────────────────────────────────────────
def _read_local_settings(wd: Path) -> dict:
    return json.loads(workdir_local_settings_path(wd).read_text(encoding="utf-8"))


def test_approve_writes_enabled_to_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    approve_mcp_server("figma")
    approve_mcp_server("figma")  # dedup
    settings = _read_local_settings(tmp_path)
    assert settings["enabledMcpjsonServers"] == ["figma"]
    assert "version" not in settings  # 人编层无 version
    # 无写保护头：首行即 JSON
    assert workdir_local_settings_path(tmp_path).read_text(encoding="utf-8").lstrip().startswith("{")


def test_deny_writes_disabled_to_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    deny_mcp_server("sketchy")
    assert _read_local_settings(tmp_path)["disabledMcpjsonServers"] == ["sketchy"]


def test_approve_all_sets_bool_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    approve_all_project_mcp()
    assert _read_local_settings(tmp_path)["enableAllProjectMcpServers"] is True


def test_approve_then_resolve_settings_flips_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """approve roundtrip：写 local enabled → 再判定 status 由 pending→enabled（接缝验证）。"""
    monkeypatch.chdir(tmp_path)
    assert mcp_server_status("figma", settings={}, bundled=set(), trusted_origin=False) == McpApprovalStatus.PENDING
    approve_mcp_server("figma")
    enabled = _read_local_settings(tmp_path)["enabledMcpjsonServers"]
    after = mcp_server_status("figma", settings={"enabledMcpjsonServers": enabled}, bundled=set(), trusted_origin=False)
    assert after == McpApprovalStatus.ENABLED


def test_resolved_mcp_config_dataclass_defaults() -> None:
    """ResolvedMcpConfig inputs/errors 默认空（无 server 场景）。"""
    rc = ResolvedMcpConfig(servers={})
    assert rc.inputs == [] and rc.errors == []


# ── managed-mcp.json 平台派生 / 无 id input 覆盖盲区（fix-review #84）────────────
def test_managed_mcp_config_path_platform_branches() -> None:
    """平台派生三分支 + 文件名拼接（resolve_* 测试均注入 managed_mcp_path，此处直测兜底派生路径）。"""
    assert managed_mcp_config_path("darwin") == MACOS_MANAGED_DIR / MANAGED_MCP_FILENAME
    assert managed_mcp_config_path("win32") == WINDOWS_MANAGED_DIR / MANAGED_MCP_FILENAME
    assert managed_mcp_config_path("linux") == LINUX_MANAGED_DIR / MANAGED_MCP_FILENAME
    assert managed_mcp_config_path("freebsd") == LINUX_MANAGED_DIR / MANAGED_MCP_FILENAME  # 非 darwin/win32 → linux 兜底


def test_resolve_idless_and_non_object_inputs_dropped(tmp_path: Path) -> None:
    """无 id 对象 input + 非对象 input：各走 <noid-N> key + inputs.<unknown>，皆 drop、不崩、去重 key 不冲突。"""
    env = _env(tmp_path)
    inputs = [{"type": "promptString", "description": "no-id"}, "junk-string"]
    _write_json(_user_mcp(tmp_path), _mcp_doc(inputs=inputs))
    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    assert out.inputs == []  # 两者皆 drop、不崩
    unknown_errs = [e for e in out.errors if e.field == "inputs.<unknown>"]
    assert len(unknown_errs) == 2  # 各自独立报错 → <noid-N> key 不冲突（非互相覆盖）


# ---------------------------------------------------------------------------
# #116 概念瘦身：PROJECT/LOCAL 锚定进程 cwd / #116: PROJECT/LOCAL anchored at process cwd
# ---------------------------------------------------------------------------
def test_resolve_mcp_config_anchors_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#116: PROJECT/LOCAL 层无条件锚定 cwd，`resolve_mcp_config` 不再有 active_workdir 形参。"""
    env = _env(tmp_path)
    _write_json(tmp_path / ".tfrobot" / "mcp.json", _mcp_doc(servers={"proj-srv": _stdio()}))
    monkeypatch.chdir(tmp_path)

    out = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    assert "proj-srv" in out.servers
    assert out.servers["proj-srv"].origin == SettingsScope.PROJECT
    assert out.servers["proj-srv"].trusted_origin is False  # project 非可信来源，语义不变


# ---------------------------------------------------------------------------
# scope-aware 持久写层：upsert / remove 单个 server 定义（#136，对齐 rust write_target.rs / crud.rs）
# scope-aware durable write layer: upsert / remove one server definition (#136)
# ---------------------------------------------------------------------------
def _read_mcp(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _proj_mcp(wd: Path) -> Path:
    return wd / ".tfrobot" / "mcp.json"


def _local_mcp(wd: Path) -> Path:
    return wd / ".tfrobot" / "mcp.local.json"


def test_mcp_write_path_maps_three_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """三 scope → 落点：user=$XDG/a2c/mcp.json、project=<cwd>/.tfrobot/mcp.json、local=mcp.local.json。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert mcp_write_path(McpWriteScope.USER, env=env) == _user_mcp(tmp_path)
    assert mcp_write_path(McpWriteScope.PROJECT, env=env) == _proj_mcp(tmp_path)
    assert mcp_write_path(McpWriteScope.LOCAL, env=env) == _local_mcp(tmp_path)


@pytest.mark.parametrize(
    ("scope", "path_of"),
    [
        (McpWriteScope.USER, _user_mcp),
        (McpWriteScope.PROJECT, _proj_mcp),
        (McpWriteScope.LOCAL, _local_mcp),
    ],
)
def test_upsert_new_lands_requested_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: McpWriteScope,
    path_of: object,
) -> None:
    """新声明落**请求 scope**；raw body 原样落盘；``{servers, inputs}`` 规整形状；changed=True。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    body = _stdio("node")
    res = upsert_mcp_server("figma", body, scope=scope, env=env, home=tmp_path)
    assert res == McpUpsertResult(scope=scope, changed=True)
    doc = _read_mcp(path_of(tmp_path))  # type: ignore[operator]
    assert doc == {"servers": {"figma": body}, "inputs": []}


def test_upsert_writes_raw_unrendered_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D1 铁律：占位符 ``${input:}`` / ``${env:}`` 逐字保留、绝不渲染（值不离 Computer）。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    body = {
        "type": "stdio",
        "server_parameters": {"command": "node", "env": {"TOK": "${input:tok}", "HM": "${env:HOME}"}},
    }
    upsert_mcp_server("srv", body, scope=McpWriteScope.LOCAL, env=env, home=tmp_path)
    stored = _read_mcp(_local_mcp(tmp_path))["servers"]["srv"]
    assert stored["server_parameters"]["env"] == {"TOK": "${input:tok}", "HM": "${env:HOME}"}  # 未渲染


def test_upsert_existing_lands_origin_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """改已有 server 恒落其 **origin** scope；请求 scope 仅新声明生效 → 请求的 project scope 不被创建。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio("old")}))  # origin=user
    res = upsert_mcp_server("figma", _stdio("new"), scope=McpWriteScope.PROJECT, env=env, home=tmp_path)
    assert res.scope == McpWriteScope.USER and res.changed is True  # 落 origin(user)，非请求(project)
    assert _read_mcp(_user_mcp(tmp_path))["servers"]["figma"]["server_parameters"]["command"] == "new"
    assert not _proj_mcp(tmp_path).exists()  # 请求的 project scope 未被创建（无 scope 漂移）


def test_upsert_unchanged_content_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内容未变 → changed=False 且不落盘（对齐 rust 仅真变才 bump revision）。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    body = _stdio("node")
    assert upsert_mcp_server("figma", body, scope=McpWriteScope.LOCAL, env=env, home=tmp_path).changed is True
    path = _local_mcp(tmp_path)
    mtime = path.stat().st_mtime_ns
    second = upsert_mcp_server("figma", dict(body), scope=McpWriteScope.LOCAL, env=env, home=tmp_path)
    assert second == McpUpsertResult(scope=McpWriteScope.LOCAL, changed=False)
    assert path.stat().st_mtime_ns == mtime  # 未重写、无 churn


def test_upsert_bundled_name_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bundled server 名（plugin ledger 派生）→ 拒写 McpWriteTargetError，且不落盘。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_installed_plugins(
        {"plugins": {"a@mp": [{"scope": "user", "installPath": "/x", "bundledMcpServers": ["figma"]}]}},
        home=tmp_path,
    )
    with pytest.raises(McpWriteTargetError):
        upsert_mcp_server("figma", _stdio(), scope=McpWriteScope.LOCAL, env=env, home=tmp_path)
    assert not _local_mcp(tmp_path).exists()  # 拒写发生在落盘前


def test_remove_clears_all_writable_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """remove 跨 user/project/local 全清同名声明（真删干净）；同 scope 其他 server 保留；返回 True。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    _write_json(_user_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio("u"), "keep": _stdio()}))
    _write_json(_proj_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio("p")}))
    _write_json(_local_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio("l")}))
    assert remove_mcp_server("figma", env=env) is True
    assert "figma" not in _read_mcp(_user_mcp(tmp_path))["servers"]
    assert "keep" in _read_mcp(_user_mcp(tmp_path))["servers"]  # 同 scope 邻居保留
    assert _read_mcp(_proj_mcp(tmp_path))["servers"] == {}
    assert _read_mcp(_local_mcp(tmp_path))["servers"] == {}


def test_remove_absent_is_noop_no_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全无匹配 → no-op 返回 False；不创建目录/锁/文件。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert remove_mcp_server("ghost", env=env) is False
    assert not (tmp_path / ".tfrobot").exists()  # 未创建 .tfrobot 目录/锁
    assert not _user_mcp(tmp_path).exists()


def test_remove_skips_scope_without_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未声明该 name 的 scope 不被触碰（不 normalize）→ 其未知顶层 key 原样保留。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    up = _user_mcp(tmp_path)
    _write_json(up, {"servers": {"other": _stdio()}, "inputs": [], "$schema": "x"})  # 无 figma
    _write_json(_local_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio()}))
    assert remove_mcp_server("figma", env=env) is True
    assert _read_mcp(up).get("$schema") == "x"  # user 文件未被重写/规整
    assert _read_mcp(_local_mcp(tmp_path))["servers"] == {}


def test_upsert_over_corrupt_target_refuses_no_clobber(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 回归：目标 mcp.json 损坏 → 抛 McpConfigCorruptError，绝不覆盖（既有内容/字节完好保留）。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    corrupt = "{ this is not valid json"
    lp = _local_mcp(tmp_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(corrupt, encoding="utf-8")
    with pytest.raises(McpConfigCorruptError):
        upsert_mcp_server("figma", _stdio(), scope=McpWriteScope.LOCAL, env=env, home=tmp_path)
    assert lp.read_text(encoding="utf-8") == corrupt  # 逐字节未动（无覆盖、无备份丢失）


def test_upsert_over_malformed_field_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """结构性字段错（servers 非对象）→ 拒写（否则整体覆盖会销毁那批无法安全合并的 server）。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    lp = _local_mcp(tmp_path)
    _write_json(lp, {"servers": ["oops-not-a-map"], "inputs": []})
    with pytest.raises(McpConfigCorruptError):
        upsert_mcp_server("figma", _stdio(), scope=McpWriteScope.LOCAL, env=env, home=tmp_path)
    assert _read_mcp(lp)["servers"] == ["oops-not-a-map"]  # 原样保留


def test_upsert_existing_policy_origin_falls_back_to_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只读 policy origin：非可写 → 回落写请求 scope（rust parity：policy 等同新声明；写会被 policy 读遮蔽）。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    managed = tmp_path / "managed-mcp.json"
    _write_json(managed, _mcp_doc(servers={"figma": _stdio("policy-cmd")}))
    # origin 快照要看到 policy 定义：resolve_mcp_config 默认按平台推导 managed 路径，故重定向到我们的临时文件。
    monkeypatch.setattr(
        "a2c_smcp.computer.settings.mcp_config.managed_mcp_config_path",
        lambda platform=None: managed,
    )
    res = upsert_mcp_server("figma", _stdio("user-cmd"), scope=McpWriteScope.USER, env=env, home=tmp_path)
    assert res.scope == McpWriteScope.USER and res.changed is True  # 回落请求 scope（user）
    assert _read_mcp(_user_mcp(tmp_path))["servers"]["figma"]["server_parameters"]["command"] == "user-cmd"
    # shadow 语义：user 定义落盘了，但 policy 读优先级最高 → 有效配置仍是 policy-cmd（changed=True 却被遮蔽）。
    shadowed = resolve_mcp_config(env=env).servers["figma"]
    assert isinstance(shadowed.config, StdioServerConfig)
    assert shadowed.origin == SettingsScope.POLICY and shadowed.config.server_parameters.command == "policy-cmd"


def test_upsert_existing_same_scope_in_place_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已有 server 在同一 origin scope 内改内容 → 原地改写、changed=True（非跨 scope、非 noop）。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    _write_json(_local_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio("old"), "keep": _stdio()}))
    res = upsert_mcp_server("figma", _stdio("new"), scope=McpWriteScope.LOCAL, env=env, home=tmp_path)
    assert res == McpUpsertResult(scope=McpWriteScope.LOCAL, changed=True)
    doc = _read_mcp(_local_mcp(tmp_path))
    assert doc["servers"]["figma"]["server_parameters"]["command"] == "new"
    assert "keep" in doc["servers"]  # 同 scope 邻居保留（整体替换只动目标条目）


def test_remove_skips_corrupt_scope_but_clears_clean_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """remove 跨 scope best-effort：损坏 scope 跳过（原字节保留），干净 scope 照删，返回 True。"""
    env = _env(tmp_path)
    monkeypatch.chdir(tmp_path)
    corrupt = "{not json"
    up = _user_mcp(tmp_path)
    up.parent.mkdir(parents=True, exist_ok=True)
    up.write_text(corrupt, encoding="utf-8")  # user scope 损坏
    _write_json(_local_mcp(tmp_path), _mcp_doc(servers={"figma": _stdio()}))  # local scope 干净、含 figma
    assert remove_mcp_server("figma", env=env) is True  # 从干净 local 删掉 → True
    assert up.read_text(encoding="utf-8") == corrupt  # 损坏 user scope 逐字节未动
    assert _read_mcp(_local_mcp(tmp_path))["servers"] == {}


def test_approval_writes_anchor_cwd_no_failfast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#116: 批准写盘锚定 cwd（`<cwd>/.tfrobot/settings.local.json`），不再因缺 workdir 而 fail-fast。"""
    monkeypatch.chdir(tmp_path)
    approve_mcp_server("figma")
    deny_mcp_server("sketchy")
    approve_all_project_mcp()

    settings = _read_local_settings(tmp_path)
    assert settings["enabledMcpjsonServers"] == ["figma"]
    assert settings["disabledMcpjsonServers"] == ["sketchy"]
    assert settings["enableAllProjectMcpServers"] is True
