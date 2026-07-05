# -*- coding: utf-8 -*-
# filename: test_mcp_approval.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
启动期 MCP 批准框单元测试（v0.2.1 #69 Group B，§9.2）/ Boot-time MCP approval-box unit tests。

测试意图 / Test intentions（fake Computer 桩 + 内存 session；XDG 重定向隔离 user；policy monkeypatch 为空）:
- PENDING + TTY ``[y]`` → 写 local ``enabledMcpjsonServers`` + 挂载；**重启幂等**（二次 gate → ENABLED、不再弹）；
- PENDING + 非 TTY（session=None）→ skip+WARN、不挂、不写；``--approve-all-mcp`` → 挂载但**不落盘**；
- ENABLED（user origin trusted）→ 直挂；DISABLED（local disabled）→ 跳过；
- ``ext``（envFile）合回挂载 dict（否则 spawn 丢失）；``resolved.errors`` 呈现 WARN。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import a2c_smcp.computer.cli.commands.plugin as plugin_cmd
from a2c_smcp.computer.cli.commands.plugin import run_mcp_approval
from a2c_smcp.computer.settings.scope import user_settings_path, workdir_local_settings_path


@pytest.fixture(autouse=True)
def _no_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """policy 读取 OS 源不确定 → 测试中固定为空，保证确定性 / pin policy empty for determinism。

    fix-review #4 后 ``resolved_settings`` 移入 ``cli.commands.__init__``（函数内 lazy import policy），
    故 monkeypatch 须打到来源模块 ``settings.policy``（lazy import 会取到打过的属性）。
    """
    import a2c_smcp.computer.settings.policy as policy_mod

    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_: {})


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _project_mcp(wd: Path, servers: dict[str, Any]) -> None:
    _write_json(wd / ".tfrobot" / "mcp.json", {"servers": servers, "inputs": []})


def _user_mcp(env: dict[str, str], servers: dict[str, Any]) -> None:
    _write_json(Path(env["XDG_CONFIG_HOME"]) / "a2c" / "mcp.json", {"servers": servers, "inputs": []})


class _Session:
    """内存 PromptSession 桩：按序返回预置答案 / in-memory prompt session returning canned answers。"""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def prompt_async(self, prompt: str, **_: Any) -> str:
        self.prompts.append(prompt)
        return self._answers.pop(0)


class _FakeComp:
    def __init__(self, home: Path) -> None:
        self.skill_home = home
        self.injected: list[Any] = []
        self.mounted: list[dict[str, Any]] = []

    def add_or_update_input(self, inp: Any) -> None:
        self.injected.append(inp)

    async def aadd_or_aupdate_server(
        self, cfg: dict[str, Any], *, session: Any = None, plugin: Any = None, marketplace: Any = None,
    ) -> None:
        self.mounted.append(cfg)


def _mounted_names(comp: _FakeComp) -> set[str]:
    return {str(m.get("name")) for m in comp.mounted}


def _stdio() -> dict[str, Any]:
    return {"type": "stdio", "server_parameters": {"command": "node"}}


# ── PENDING + TTY [y] → 写 local + 挂载 + 重启幂等 ──────────────────────────────
@pytest.mark.asyncio
async def test_pending_approve_yes_writes_local_and_mounts_then_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"shared": _stdio()})  # project origin → 非 trusted → PENDING
    monkeypatch.chdir(wd)  # #116：project/local 层锚定进程 cwd

    comp = _FakeComp(home)
    sess = _Session(["y"])
    # run_mcp_approval 读 os.environ 派生 XDG → 用 monkeypatch env 注入
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, sess, approve_all=False, flag_config=None)
    assert "shared" in _mounted_names(comp)  # [y] → 挂载
    local = json.loads(workdir_local_settings_path(wd).read_text(encoding="utf-8"))
    assert local["enabledMcpjsonServers"] == ["shared"]  # 写 local scope（cwd 单根）

    # 重启幂等：二次启动 gate 读到 local enabled → ENABLED 直挂、不再弹（session 无答案也不报错）
    comp2 = _FakeComp(home)
    sess2 = _Session([])
    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp2, sess2, approve_all=False, flag_config=None)
    assert "shared" in _mounted_names(comp2)
    assert sess2.prompts == []  # 不再弹批准框


# ── PENDING + 非 TTY → skip+WARN（不挂不写）；--approve-all-mcp → 挂载不落盘 ──────
@pytest.mark.asyncio
async def test_pending_non_tty_skips_and_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"shared": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=False, flag_config=None)  # session=None = 非 TTY
    assert _mounted_names(comp) == set()  # 未挂
    assert not workdir_local_settings_path(wd).exists()  # 未写


@pytest.mark.asyncio
async def test_pending_non_tty_approve_all_mounts_without_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"shared": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, flag_config=None)
    assert "shared" in _mounted_names(comp)  # --approve-all-mcp → 挂载
    assert not workdir_local_settings_path(wd).exists()  # 仅本次、不落盘


# ── ENABLED（user origin trusted）直挂 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_user_origin_mounts_without_box(tmp_path: Path) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _user_mcp(env, {"mine": _stdio()})  # user origin → trusted_origin → ENABLED、免批准
    comp = _FakeComp(home)
    sess = _Session([])  # 不应被调用
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, sess, approve_all=False, flag_config=None)
    assert "mine" in _mounted_names(comp)
    assert sess.prompts == []


# ── ext（envFile）合回挂载 dict（#69 Group B 风险 2）─────────────────────────────
@pytest.mark.asyncio
async def test_envfile_ext_merged_into_mount_dict(tmp_path: Path) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    envfile = str(wd / ".env")
    _user_mcp(env, {"mine": {**_stdio(), "envFile": envfile}})  # user origin → 直挂
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, _Session([]), approve_all=False, flag_config=None)
    mine = next(m for m in comp.mounted if m.get("name") == "mine")
    assert mine.get("envFile") == envfile  # ext 合回，否则 spawn 丢 envFile


# ── resolved.errors 呈现 WARN ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_malformed_server_surfaces_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    # 畸形 server（缺 type）→ drop + error；valid 同存
    _project_mcp(wd, {"bad": {"server_parameters": {}}, "good": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, flag_config=None)
    out = capsys.readouterr().out
    assert "mcp.json" in out  # resolved.errors 呈现（被 drop 的 bad 不静默）
