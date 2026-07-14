# -*- coding: utf-8 -*-
# filename: test_computer_dual_path_crud.py
# @Time    : 2026/07/14
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
#137 ② Computer 双路径 MCP-server CRUD 单元测试（父 #135：对齐 rust-sdk 双路径）。

覆盖 flip 后的 **durable**（落盘 + 重启存活）与新增 **transient**（纯运行期、不落盘）两路径的语义边界：

- ``aadd_or_aupdate_server``：默认落 **Local**（``mcp.local.json``）、重投影可读、重启存活、**raw 未渲染**（D1）。
- ``aadd_or_aupdate_server_in_scope(Project)``：落 git 共享层（``mcp.json``）。
- ``amount_server``：**不落盘**（纯运行期投影）。
- ``aremove_server``：删**所有可写 scope** 声明 + 运行期停摘；**bundled 身份拒删**；**改已有恒落 origin**。

隔离：``monkeypatch.chdir(tmp)`` 锚 project/local（#116）、``XDG_CONFIG_HOME`` → tmp 锚 user；``auto_connect=False``
免拉起真实进程（``_amount_rendered`` 仅入册配置、不 spawn）。

Dual-path MCP-server CRUD unit tests for #137 ② (aligns rust-sdk dual-path). All disk paths isolated to tmp via
chdir + XDG; ``auto_connect=False`` avoids spawning real processes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inputs.resolver import InputResolver
from a2c_smcp.computer.settings.mcp_config import (
    McpWriteScope,
    McpWriteTargetError,
    mcp_write_path,
    resolve_mcp_config,
)


class _MapResolver(InputResolver):
    """按 id 查表的最小 InputResolver stub（``${input:id}`` → 映射值）/ minimal id→value resolver stub。"""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping

    def clear_cache(self, key: str | None = None) -> None:  # pragma: no cover - 本测试不触发
        pass

    async def aresolve_by_id(
        self, input_id: str, *, session: Any = None, plugin: str | None = None, marketplace: str | None = None,
    ) -> Any:
        return self.mapping[input_id]


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离落盘面：project/local 锚 cwd（#116），user 锚 XDG → tmp（不碰真实用户配置）。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)


def _stdio_dict(name: str, command: str) -> dict[str, Any]:
    return {
        "type": "stdio",
        "name": name,
        "disabled": True,  # disabled 免 boot/auto_connect 拉起进程（本测试只验证配置态 + 落盘）
        "server_parameters": {
            "command": command,
            "args": [],
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
        "forbidden_tools": [],
        "tool_meta": {},
    }


def _write_mcp_file(path: Path, servers: dict[str, Any]) -> None:
    """直写一个 scope 的 ``mcp.json``（绕开 upsert 的 existing→origin 重定向，用于多 scope 播种）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": servers, "inputs": []}), encoding="utf-8")


def _read_servers(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("servers", {})


def _comp(tmp_path: Path, resolver: InputResolver | None = None) -> Computer:
    return Computer(
        name="dual-path-test",
        auto_connect=False,
        auto_reconnect=False,
        skill_home=tmp_path / "home",
        input_resolver=resolver,
    )


# ── durable: aadd_or_aupdate_server 默认落 Local + 重投影 + 重启存活 + D1 raw ─────────────────────────
@pytest.mark.asyncio
async def test_aadd_or_aupdate_server_persists_local_reprojects_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path, resolver=_MapResolver({"cmd": "/bin/echo"}))

    # command 带占位符 ${input:cmd}：证明 D1（盘上 raw 未渲染、内存投影已渲染）。
    await comp.aadd_or_aupdate_server(_stdio_dict("echo", "${input:cmd}"))

    # 1) 落 **Local**（mcp.local.json），非 project 层。
    local_path = mcp_write_path(McpWriteScope.LOCAL, env=os.environ)
    project_path = mcp_write_path(McpWriteScope.PROJECT, env=os.environ)
    assert "echo" in _read_servers(local_path)
    assert "echo" not in _read_servers(project_path), "默认 durable 落 Local，不碰 git 共享 project 层"

    # 2) D1：盘上 raw **未渲染**（占位字面保留，绝不写渲染后 secret）。
    disk = resolve_mcp_config(env=os.environ).servers["echo"]
    assert disk.config.server_parameters.command == "${input:cmd}", "盘上必须是未渲染 raw（D1）"
    assert disk.origin.value == "local"

    # 3) 重投影：manager 运行期活跃集含该 server，且为**已渲染**结果。
    assert comp.mcp_manager is not None
    runtime = {c.name: c for c in comp.mcp_manager.server_configs()}
    assert "echo" in runtime
    assert runtime["echo"].server_parameters.command == "/bin/echo", "内存投影用渲染后结果"

    # 4) 重启存活：新 Computer（同 cwd/XDG）经 resolve_mcp_config 仍读得该声明。
    assert "echo" in resolve_mcp_config(env=os.environ).servers


# ── durable: _in_scope(Project) 落 git 共享层 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_aadd_or_aupdate_server_in_scope_project_persists_git_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)

    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("shared", "/bin/true"), McpWriteScope.PROJECT)

    project_path = mcp_write_path(McpWriteScope.PROJECT, env=os.environ)
    local_path = mcp_write_path(McpWriteScope.LOCAL, env=os.environ)
    assert "shared" in _read_servers(project_path), "显式 Project → 落 git 共享 mcp.json"
    assert "shared" not in _read_servers(local_path)
    assert resolve_mcp_config(env=os.environ).servers["shared"].origin.value == "project"


# ── transient: amount_server 不落盘 ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_amount_server_does_not_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path, resolver=_MapResolver({"cmd": "/bin/echo"}))

    await comp.amount_server(_stdio_dict("ephemeral", "${input:cmd}"))

    # 无任一 scope 落盘（纯运行期投影）。
    for scope in (McpWriteScope.LOCAL, McpWriteScope.PROJECT, McpWriteScope.USER):
        assert not mcp_write_path(scope, env=os.environ).exists(), f"transient 不应写 {scope} mcp.json"
    assert resolve_mcp_config(env=os.environ).servers == {}

    # 但运行期活跃集里在（capability 面已投影）。
    assert comp.mcp_manager is not None
    assert any(c.name == "ephemeral" for c in comp.mcp_manager.server_configs())


# ── durable: aremove_server 删所有可写 scope + 运行期停摘 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_aremove_server_deletes_all_writable_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    # 三个可写 scope 各播种同名 "multi" 声明（直写，绕 upsert 的 origin 重定向）。
    for scope in (McpWriteScope.USER, McpWriteScope.PROJECT, McpWriteScope.LOCAL):
        _write_mcp_file(mcp_write_path(scope, env=os.environ), {"multi": _stdio_dict("multi", "/bin/x")})
    assert set(resolve_mcp_config(env=os.environ).servers) == {"multi"}

    comp = _comp(tmp_path)
    # bundle_id("multi") == normalize("multi") == "multi"
    await comp.aremove_server("multi")

    for scope in (McpWriteScope.USER, McpWriteScope.PROJECT, McpWriteScope.LOCAL):
        assert "multi" not in _read_servers(mcp_write_path(scope, env=os.environ)), f"{scope} 声明必须删净"
    assert resolve_mcp_config(env=os.environ).servers == {}


@pytest.mark.asyncio
async def test_aremove_server_absent_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    # 无声明、无运行期投影 → 落盘 no-op、停摘 no-op，不抛。
    await comp.aremove_server("ghost")
    for scope in (McpWriteScope.LOCAL, McpWriteScope.PROJECT, McpWriteScope.USER):
        assert not mcp_write_path(scope, env=os.environ).exists()


# ── durable: aremove_server bundled 身份拒删 ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_aremove_server_rejects_bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    # 令 "figma" 命中 ledger 派生 bundled 集（隔离 Computer 的拒删分支，不需真装 plugin）。
    monkeypatch.setattr("a2c_smcp.computer.computer.bundled_mcp_server_names", lambda **_: {"figma"})

    comp = _comp(tmp_path)
    # 运行期投影一条 bundled server（transient；bundle_id("figma") == "figma"）。
    await comp.amount_server(_stdio_dict("figma", "/bin/figma"))

    with pytest.raises(McpWriteTargetError):
        await comp.aremove_server("figma")

    # 拒删后运行期投影仍在（未被误停摘）。
    assert comp.mcp_manager is not None
    assert any(c.name == "figma" for c in comp.mcp_manager.server_configs())


# ── durable: 改已有 server 恒落其 origin scope（新 scope 只作用于新声明）─────────────────────────────
@pytest.mark.asyncio
async def test_aadd_or_aupdate_server_existing_lands_origin_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)

    # 先在 Project 声明；再以**默认 Local** 更新同名 → 必须落回 Project（origin），不漂移到 Local。
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("origin-svc", "/bin/v1"), McpWriteScope.PROJECT)
    await comp.aadd_or_aupdate_server(_stdio_dict("origin-svc", "/bin/v2"))  # 默认 Local

    project_servers = _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ))
    local_servers = _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ))
    assert "origin-svc" in project_servers, "改已有恒落 origin（Project），不漂移"
    assert "origin-svc" not in local_servers, "默认 Local 入参对已有声明无效"
    assert project_servers["origin-svc"]["server_parameters"]["command"] == "/bin/v2", "内容已更新（整体替换）"


# ── transient: aunmount_server(name) 只摘目标 + 守护 no-op ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_aunmount_server_by_name_unmounts_only_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.amount_server(_stdio_dict("keep", "/bin/keep"))
    await comp.amount_server(_stdio_dict("drop", "/bin/drop"))
    assert comp.mcp_manager is not None
    assert {c.name for c in comp.mcp_manager.server_configs()} == {"keep", "drop"}

    await comp.aunmount_server("drop")
    assert {c.name for c in comp.mcp_manager.server_configs()} == {"keep"}, "按 name 只摘目标，不误伤同侪"
    # 纯运行期：不落任一 scope。
    for scope in (McpWriteScope.LOCAL, McpWriteScope.PROJECT, McpWriteScope.USER):
        assert not mcp_write_path(scope, env=os.environ).exists()
    # 不存在的 name → no-op（不抛、不误摘）。
    await comp.aunmount_server("ghost")
    assert {c.name for c in comp.mcp_manager.server_configs()} == {"keep"}


@pytest.mark.asyncio
async def test_aunmount_server_manager_none_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    # manager 未建（无任何挂载）→ no-op，不抛、不建 manager。
    await comp.aunmount_server("anything")
    await comp.aunmount_server_by_id("anything")
    assert comp.mcp_manager is None


# ── durable: aremove_server 无盘上声明但仍停摘运行期投影 ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_aremove_server_unmounts_runtime_projection_without_disk_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    # transient 投影一条（不落盘、无声明）；bundle_id("proj-only") == "proj-only"（连字符合法、不折叠）。
    await comp.amount_server(_stdio_dict("proj-only", "/bin/x"))
    assert comp.mcp_manager is not None
    assert any(c.name == "proj-only" for c in comp.mcp_manager.server_configs())

    # durable remove：盘上无匹配声明 → 落盘 no-op，但仍停摘运行期投影（文档承诺的关键分支）。
    await comp.aremove_server("proj-only")
    assert not any(c.name == "proj-only" for c in comp.mcp_manager.server_configs()), "无盘声明也须清运行期投影"
    for scope in (McpWriteScope.LOCAL, McpWriteScope.PROJECT, McpWriteScope.USER):
        assert not mcp_write_path(scope, env=os.environ).exists(), "无声明匹配 → 不产生任何落盘写"
