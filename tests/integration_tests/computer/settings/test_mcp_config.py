# -*- coding: utf-8 -*-
# filename: test_mcp_config.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``.tfrobot/mcp.json`` 定义层 + 批准门控集成测试（v0.2.1 #64）—— 全栈跨 scope 真文件 + 真 settings 解析
``.tfrobot/mcp.json`` definition layer + approval-gate integration tests: full cross-scope real files.

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §9.1 / §9.2 / §5.1 / §5.5。

测试意图 / Test intentions（无 git；真实跨 user(XDG) + active project/local(.tfrobot) + managed 目录铺 mcp.json，
真实 installed_plugins.json 账本仍在但 **#148 起审批门不再感知它**；批准 roundtrip 经真实 ``resolve_settings`` 六层合并读回）:
- resolve + gate 全栈：user→enabled(trusted) / project→pending(workspace) / **借用 bundled 名的 project server→pending
  （#148 借名不再免批准）** / policy deny→disabled。
- approve roundtrip：pending project server → ``approve_mcp_server`` 写 local → ``resolve_settings`` 读回 →
  重门控 → enabled（接缝验证、不重复造判定）。
- scope 覆盖全栈：同名 server user+local 双定义，local 胜（origin=local、workspace 受门控）。
"""

import json
import os
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.mcp_config import (
    McpApprovalStatus,
    approve_mcp_server,
    gate_mcp_servers,
    resolve_mcp_config,
)
from a2c_smcp.computer.settings.schema import SettingsScope
from a2c_smcp.computer.settings.scope import resolve_settings
from a2c_smcp.computer.settings.store import save_installed_plugins


# ── 辅助 / helpers ───────────────────────────────────────────────────────────
def _env(tmp_path: Path) -> dict[str, str]:
    """XDG_CONFIG_HOME → user mcp.json/settings.json；A2C_SKILL_HOME → installed_plugins.json（bundled 接缝）。"""
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "A2C_SKILL_HOME": str(tmp_path / "home")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _stdio() -> dict:
    return {"type": "stdio", "server_parameters": {"command": "node"}}


def _mcp(servers: dict) -> dict:
    return {"servers": servers, "inputs": []}


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    return h


# ── resolve + gate 全栈 ───────────────────────────────────────────────────────
def test_resolve_and_gate_full_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path)
    wd = tmp_path / "wd"
    managed_mcp = tmp_path / "managed-mcp.json"

    # 跨 scope 铺 server 定义：user(trusted) / project(workspace，锚 cwd，#116) / policy(trusted)。
    _write_json(tmp_path / "cfg" / "a2c" / "mcp.json", _mcp({"user-srv": _stdio()}))
    _write_json(wd / ".tfrobot" / "mcp.json", _mcp({"proj-srv": _stdio(), "blender": _stdio()}))
    _write_json(managed_mcp, _mcp({"policy-srv": _stdio()}))
    monkeypatch.chdir(wd)

    # bundled 账本：plugin 携带 "blender" → 即使在 workspace mcp.json 出现，也免批准。
    save_installed_plugins(
        {"plugins": {"3d@mp": [{"scope": "user", "installPath": "/x", "bundledMcpServers": ["blender"]}]}},
        home=_home(tmp_path),
    )

    # policy denies "policy-srv"（企业拒绝名单，policy scope）。
    policy = {"deniedMcpServers": ["policy-srv"]}

    resolved = resolve_mcp_config(env=env, managed_mcp_path=managed_mcp)
    assert set(resolved.servers) == {"user-srv", "proj-srv", "blender", "policy-srv"}

    settings = resolve_settings(env=env, policy_settings=policy).settings
    statuses = gate_mcp_servers(resolved, settings)

    assert statuses == {
        "user-srv": McpApprovalStatus.ENABLED,  # trusted origin（用户自己加）
        "proj-srv": McpApprovalStatus.PENDING,  # workspace 共享、未决 → 弹框（#69）
        # #148（P0 安全回归）：project scope 声明的 "blender" 借用了已装 plugin 的 bundled 名（账本已记，见上）。
        # 档④删除后审批门**不再感知账本**，它当普通 untrusted workspace server 处理 → PENDING（弹框），杜绝借名绕过。
        "blender": McpApprovalStatus.PENDING,
        "policy-srv": McpApprovalStatus.DISABLED,  # 企业拒绝名单
    }


def test_approve_roundtrip_flips_pending_to_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pending workspace server → approve 写 local（cwd 锚，#116）→ resolve_settings 读回 → 重门控 enabled。"""
    env = _env(tmp_path)
    wd = tmp_path / "wd"
    managed_mcp = tmp_path / "absent-managed-mcp.json"
    _write_json(wd / ".tfrobot" / "mcp.json", _mcp({"figma": _stdio()}))
    monkeypatch.chdir(wd)

    resolved = resolve_mcp_config(env=env, managed_mcp_path=managed_mcp)

    before = gate_mcp_servers(resolved, resolve_settings(env=env).settings)
    assert before == {"figma": McpApprovalStatus.PENDING}

    # 批准框 [y]es：写 cwd 的 local settings.local.json。
    approve_mcp_server("figma")

    after = gate_mcp_servers(resolved, resolve_settings(env=env).settings)
    assert after == {"figma": McpApprovalStatus.ENABLED}


def test_scope_override_full_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同名 server user(低) + local(高，锚 cwd)：local 整体覆盖、origin=local、workspace 受门控（pending）。"""
    env = _env(tmp_path)
    wd = tmp_path / "wd"
    _write_json(tmp_path / "cfg" / "a2c" / "mcp.json", _mcp({"shared": {"type": "stdio", "server_parameters": {"command": "user-cmd"}}}))
    _write_json(wd / ".tfrobot" / "mcp.local.json", _mcp({"shared": {"type": "stdio", "server_parameters": {"command": "local-cmd"}}}))
    monkeypatch.chdir(wd)

    resolved = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    srv = resolved.servers["shared"]
    assert srv.config.server_parameters.command == "local-cmd"  # 高 scope 整体覆盖
    assert srv.trusted_origin is False  # origin=local → workspace 共享、受门控

    statuses = gate_mcp_servers(resolved, resolve_settings(env=env).settings)
    assert statuses == {"shared": McpApprovalStatus.PENDING}


# ── #157（P0 安全）：project scope 自我批准 ─────────────────────────────────────
# 攻击链：被 clone 的仓库同时携带两个**入 git** 的文件 —— `.tfrobot/mcp.json`（恶意 server）+
# `.tfrobot/settings.json`（自我批准）。project scope 若能为「自身是否受信」供给判据，则审批门形同虚设。
# 协议 `guides/mcp-approval-gate-alignment.md` §2.1 通则（MUST）：审批门的输入 MUST 来自比被判定 server
# 更高信任的来源；**enable 方向**判据（档⑤ enabledMcpjsonServers / 档⑥ enableAllProjectMcpServers）
# 由 project 供给时 MUST 被过滤 + 记错。与 #148 删掉的档④ 同构，且更易达成（无需装任何插件、无需任何名字）。
# Attack chain: a cloned repo ships both git-tracked files; project scope MUST NOT supply its own trust.
@pytest.mark.parametrize(
    "self_approval",
    [
        pytest.param({"enableAllProjectMcpServers": True}, id="gate6-enable-all"),
        pytest.param({"enabledMcpjsonServers": ["evil"]}, id="gate5-enabled-list"),
    ],
)
def test_project_settings_cannot_self_approve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    self_approval: dict,
) -> None:
    """被 clone 仓库的 project settings.json 自我批准 → MUST PENDING（非 ENABLED）+ 响亮失败（记错）。"""
    env = _env(tmp_path)
    wd = tmp_path / "wd"  # 模拟被 clone 的仓库目录
    _write_json(wd / ".tfrobot" / "mcp.json", _mcp({"evil": _stdio()}))
    _write_json(wd / ".tfrobot" / "settings.json", self_approval)  # ← 入 git，随仓库分发
    monkeypatch.chdir(wd)

    resolved = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    assert resolved.servers["evil"].trusted_origin is False  # origin=project → 本就该受门控

    resolved_st = resolve_settings(env=env)
    statuses = gate_mcp_servers(resolved, resolved_st.settings)

    # ① 安全：自我批准判据被过滤 → 恶意 server 仍需用户当面批准。
    assert statuses == {"evil": McpApprovalStatus.PENDING}

    # ② 响亮失败：越权字段进 settings 校验错误通道（不静默忽略，§2.1 / §3 同姿态）。
    offending = [e for e in resolved_st.errors if e.field in self_approval and e.scope is SettingsScope.PROJECT]
    assert len(offending) == 1, f"越权字段 MUST 记一条错误，实得 {resolved_st.errors}"
    assert "settings.local.json" in offending[0].reason  # 文案须给出可操作去向


def test_local_scope_approval_still_works_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ #157 陷阱 1 守护（全栈）：受信供给方**含 local** —— 三个批准写助手只写 local scope，
    若把 local 一并判为不受信，每次批准都会在读回时被自己过滤掉、**批准永远不生效**。读面与写面 MUST 对称。
    Guards that LOCAL stays a trusted supplier: the approval write helpers only ever write local scope.
    """
    env = _env(tmp_path)
    wd = tmp_path / "wd"
    # project 层同时携带自我批准（应被过滤）——确保 local 的批准不是被 project 层「顺带」放行的。
    _write_json(wd / ".tfrobot" / "mcp.json", _mcp({"figma": _stdio()}))
    _write_json(wd / ".tfrobot" / "settings.json", {"enableAllProjectMcpServers": True})
    monkeypatch.chdir(wd)

    resolved = resolve_mcp_config(env=env, managed_mcp_path=tmp_path / "absent.json")
    assert gate_mcp_servers(resolved, resolve_settings(env=env).settings) == {"figma": McpApprovalStatus.PENDING}

    approve_mcp_server("figma")  # 批准框 [y]es → 写 local settings.local.json

    after = gate_mcp_servers(resolved, resolve_settings(env=env).settings)
    assert after == {"figma": McpApprovalStatus.ENABLED}, "local 供给的批准 MUST 读得回（写面/读面对称）"
