# -*- coding: utf-8 -*-
# filename: test_python_rust_alignment.py
# @Time    : 2026/07/15
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
#139 ④ python↔rust 双路径一致性对拍 + remove 复活 footgun 回归（父 #135 parity epic）。

对拍 rust ``crates/smcp-computer/tests/python_rust_alignment.rs`` + ``computer.rs`` `mod tests`（durable/transient
落盘对拍、复活守护）。**非逐字节比对 rust 输出，而是语义等价断言**——落盘位置 / raw 保留 / 重启存活 / 复活防御 /
transient 不落盘 / 投影不回写——逐条与 rust 对应用例**同结论**。协议归属由 a2c-smcp-protocol#19 裁 out-of-scope
（本地约定，无协议门控）。

对拍语义清单（与 rust 用例对应）:
  1. durable add 落盘对拍：``aadd_or_aupdate_server`` → ``mcp.local.json`` raw 未渲染；``_in_scope(PROJECT)`` → git 层；
     改已有落 origin。（rust ``add_or_update_server`` / ``add_or_update_server_in_scope``）
  2. 重启存活：durable 声明经模拟 boot（``run_mcp_approval``）后仍在运行期投影。（rust 重启存活断言）
  3. remove 复活守护：人写 mcp.json 声明 → ``aremove_server`` 删所有可写 scope → 再 boot **不复活**；无声明的运行期
     投影拒删（#148 origin 判据）。（rust ``8f4229a`` 复活守护测试自足 + ``Synthesized`` 拒删）
  4. transient 不落盘：``amount_server`` / ``aunmount_server`` 前后 mcp 文件字节不变、只运行期投影变。（rust ``mount_server``）
  5. 投影调用方不回写：boot 挂载已声明 server 后用户 mcp.json 层 diff 干净。（#138 ③ 守护）

隔离：``monkeypatch.chdir(tmp)`` 锚 project/local（#116）、``XDG_CONFIG_HOME`` → tmp 锚 user；``_no_policy`` 固定
policy 为空保确定性；``auto_connect=False`` 免拉起真实进程（``run_mcp_approval`` 经 transient ``amount_server`` 入册
配置、不 spawn）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.cli.commands.plugin import run_mcp_approval
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inputs.resolver import InputResolver
from a2c_smcp.computer.settings.mcp_config import (
    McpWriteScope,
    McpWriteTargetError,
    mcp_write_path,
    resolve_mcp_config,
)


class _MapResolver(InputResolver):
    """按 id 查表的最小 InputResolver stub（``${input:id}`` → 映射值）——令 raw≠rendered，使 D1 断言可证伪。"""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping

    def clear_cache(self, key: str | None = None) -> None:  # pragma: no cover - 本测试不触发
        pass

    async def aresolve_by_id(
        self, input_id: str, *, session: Any = None, plugin: str | None = None, marketplace: str | None = None,
    ) -> Any:
        return self.mapping[input_id]


@pytest.fixture(autouse=True)
def _no_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """policy 读 OS 源不确定 → 测试固定为空，保 ``run_mcp_approval`` 门控确定性（同 test_mcp_approval）。"""
    import a2c_smcp.computer.settings.policy as policy_mod

    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_: {})


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离落盘面：project/local 锚 cwd（#116），user 锚 XDG → tmp（不碰真实用户配置）。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)


def _stdio_dict(name: str, command: str, *, disabled: bool = False) -> dict[str, Any]:
    return {
        "type": "stdio",
        "name": name,
        "disabled": disabled,
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


def _comp(tmp_path: Path, *, resolver: InputResolver | None = None) -> Computer:
    """全新 Computer（每个实例 = 一次「进程启动」；auto_connect=False 免 spawn）。

    ``resolver`` 注入后 ``${input:id}`` 渲染为映射值——D1 对拍需借此令 raw（盘上占位）≠ rendered（内存投影），
    使「盘上是否为 raw」断言真正可证伪（否则未定义占位符渲染后原样保留、raw≡rendered、断言永真）。
    """
    return Computer(
        name="align", auto_connect=False, auto_reconnect=False, skill_home=tmp_path / "home", input_resolver=resolver,
    )


async def _boot_mount(comp: Computer) -> None:
    """模拟 boot 的 MCP 挂载段：``run_mcp_approval`` 读 ``resolve_mcp_config`` → 门控 → transient ``amount_server``。

    ``session=None``（非 TTY）+ ``approve_all=True`` → PENDING（工作区 local/project origin）**仅本次挂载、不落盘**，
    忠实复现「新进程启动读盘挂载已声明 server」。
    """
    await run_mcp_approval(comp, None, approve_all=True, flag_config=None)


def _write_mcp_file(path: Path, servers: dict[str, Any]) -> None:
    """直写一个 scope 的 ``mcp.json``（模拟人编声明 / 多 scope 播种，绕 upsert 的 origin 重定向）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": servers, "inputs": []}), encoding="utf-8")


def _read_servers(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("servers", {})


def _snapshot_mcp_files() -> dict[str, bytes | None]:
    """三个可写 scope 的 ``mcp.json`` 字节快照（None=不存在）——config 源不变性断言用。"""
    snap: dict[str, bytes | None] = {}
    for scope in (McpWriteScope.LOCAL, McpWriteScope.PROJECT, McpWriteScope.USER):
        p = mcp_write_path(scope, env=os.environ)
        snap[scope.value] = p.read_bytes() if p.exists() else None
    return snap


def _runtime_names(comp: Computer) -> set[str]:
    return {c.name for c in comp.mcp_manager.server_configs()} if comp.mcp_manager is not None else set()


# ═══════════════════════════════════════════════════════════════════════════════
# 组 1 — durable add 落盘对拍（rust add_or_update_server / add_or_update_server_in_scope）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_durable_add_persists_raw_to_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """durable ``aadd_or_aupdate_server`` → ``mcp.local.json`` 出现该 server，body 为 **raw 未渲染**（``${input:}`` 原样）。

    **判别性守护 D1**：注入 resolver 令 ``${input:cmd}`` → ``/bin/echo``，故 raw（盘上）与 rendered（内存）**不同**；
    断言盘上=占位字面 **且** 内存投影=渲染值——若实现改为「落渲染后 body」（secret 落盘泄漏回归）则前一条断言必然失败。
    """
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path, resolver=_MapResolver({"cmd": "/bin/echo"}))
    # command 带占位符：resolver 使 raw≠rendered，证明盘上写的是 raw 未渲染（D1：绝不写渲染后 secret）。
    await comp.aadd_or_aupdate_server(_stdio_dict("svc", "${input:cmd}"))

    local = _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ))
    assert "svc" in local, "durable 默认落 Local（mcp.local.json）"
    assert local["svc"]["server_parameters"]["command"] == "${input:cmd}", "盘上为 raw 未渲染（占位字面保留、非 /bin/echo）"
    assert "svc" not in _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ)), "不碰 git 共享 project 层"
    # 内存投影为**渲染后**值——与盘上 raw 形成对照，坐实「盘上确为未渲染 raw」而非二者恰好相等的假阳性。
    runtime = {c.name: c for c in comp.mcp_manager.server_configs()}
    assert runtime["svc"].server_parameters.command == "/bin/echo", "内存投影用渲染后结果（raw≠rendered，D1 断言可证伪）"


@pytest.mark.asyncio
async def test_alignment_durable_in_scope_project_persists_git_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """durable ``_in_scope(PROJECT)`` → ``.tfrobot/mcp.json``（入 git、团队共享层）。"""
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("shared", "/bin/true"), McpWriteScope.PROJECT)

    assert "shared" in _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ)), "显式 Project → git 层"
    assert "shared" not in _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ))


@pytest.mark.asyncio
async def test_alignment_durable_update_existing_lands_origin_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改已有 server 恒落其 origin scope（默认 Local 入参对已有声明无效，不迁移）。"""
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("svc", "/bin/v1"), McpWriteScope.PROJECT)
    await comp.aadd_or_aupdate_server(_stdio_dict("svc", "/bin/v2"))  # 默认 Local，但恒落 origin=Project

    project = _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ))
    assert "svc" in project and project["svc"]["server_parameters"]["command"] == "/bin/v2", "落 origin 且内容更新"
    assert "svc" not in _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ)), "不漂移到 Local"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 2 — 重启存活（durable 声明经模拟 boot 后仍在运行期投影）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_durable_add_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """durable add → **全新 Computer**（模拟重启）经 ``run_mcp_approval`` 读盘挂载 → 该 server 仍在运行期投影。"""
    _isolate(tmp_path, monkeypatch)
    comp_a = _comp(tmp_path)
    await comp_a.aadd_or_aupdate_server(_stdio_dict("survivor", "/bin/echo"))
    assert "survivor" in _runtime_names(comp_a)

    # 「重启」= 丢弃 comp_a、全新实例读同一 cwd/XDG。
    comp_b = _comp(tmp_path)
    assert "survivor" not in _runtime_names(comp_b), "新实例构造期无运行期投影（未 boot）"
    await _boot_mount(comp_b)
    assert "survivor" in _runtime_names(comp_b), "durable 声明重启后仍挂载（对齐 rust 重启存活）"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 3 — remove 复活 footgun 守护（rust 8f4229a 复活守护自足 + Synthesized 拒删）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_remove_deletes_all_scopes_and_no_resurrection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人写 mcp.json 声明 → boot 挂载 → ``aremove_server`` 删所有可写 scope → **再 boot 不复活**（footgun 守护）。"""
    _isolate(tmp_path, monkeypatch)
    # 人在 project + local 两层都写了同名声明（复活 footgun 最险场景：删不干净就重启复活）。
    _write_mcp_file(mcp_write_path(McpWriteScope.PROJECT, env=os.environ), {"ghost": _stdio_dict("ghost", "/bin/p")})
    _write_mcp_file(mcp_write_path(McpWriteScope.LOCAL, env=os.environ), {"ghost": _stdio_dict("ghost", "/bin/l")})

    comp_a = _comp(tmp_path)
    await _boot_mount(comp_a)
    assert "ghost" in _runtime_names(comp_a), "boot 读盘挂载人写声明"

    # durable remove（bundle_id("ghost") == "ghost"）→ 删所有可写 scope + 运行期停摘。
    await comp_a.aremove_server("ghost")
    assert "ghost" not in _runtime_names(comp_a), "运行期投影已停摘"
    assert "ghost" not in _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ)), "project 声明删净"
    assert "ghost" not in _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ)), "local 声明删净"

    # 关键：全新实例重启 → 盘上已无声明 → **不复活**。
    comp_b = _comp(tmp_path)
    await _boot_mount(comp_b)
    assert "ghost" not in _runtime_names(comp_b), "复活 footgun 守护：删后重启不复活"


@pytest.mark.asyncio
async def test_alignment_remove_rejects_undeclared_runtime_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无用户侧声明却运行期活跃的投影（plugin/治理）``aremove_server`` → 拒删（**origin 判据**，不按账本名集；
    对应 rust ``WriteTargetError::Synthesized``，#148/F3 取代历史 name-keyed bundled 拒删）。"""
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.amount_server(_stdio_dict("figma", "/bin/figma"))  # 无盘声明的运行期投影（bundle_id=="figma"）
    assert "figma" not in resolve_mcp_config(env=os.environ).servers, "前置：盘上无任何 figma 声明"

    with pytest.raises(McpWriteTargetError):
        await comp.aremove_server("figma")
    assert "figma" in _runtime_names(comp), "拒删后运行期投影仍在（未误停摘）"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 4 — transient 不落盘（只运行期投影变、config 源字节不变）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_transient_mount_does_not_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``amount_server`` 前后所有 mcp 文件字节不变（config 源稳定），但运行期投影新增（capability 面变）。"""
    _isolate(tmp_path, monkeypatch)
    # 先播一个人写声明，证明 transient 连既有文件也不动。
    _write_mcp_file(mcp_write_path(McpWriteScope.PROJECT, env=os.environ), {"declared": _stdio_dict("declared", "/bin/d")})
    comp = _comp(tmp_path)
    before = _snapshot_mcp_files()

    await comp.amount_server(_stdio_dict("ephemeral", "/bin/e"))

    assert _snapshot_mcp_files() == before, "transient 挂载不改任一 mcp 文件（config 源不变）"
    assert "ephemeral" in _runtime_names(comp), "但运行期投影已含新 server（capability 面变）"


@pytest.mark.asyncio
async def test_alignment_transient_unmount_does_not_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``aunmount_server(name)`` 只摘运行期投影，不动任一 mcp 文件。"""
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.amount_server(_stdio_dict("tmp-srv", "/bin/t"))
    before = _snapshot_mcp_files()  # 均不存在（transient 未落盘）

    await comp.aunmount_server("tmp-srv")

    assert _snapshot_mcp_files() == before, "transient 停摘不产生任何落盘"
    assert "tmp-srv" not in _runtime_names(comp), "运行期投影已摘除"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 5 — 投影调用方不回写（#138 ③ 守护：boot 挂载已声明 server 不改用户 mcp.json）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_boot_mount_does_not_write_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_mcp_approval`` 挂载**已声明** server（投影语义）→ 用户 mcp.json 层 **byte-diff 干净**（不回写）。"""
    _isolate(tmp_path, monkeypatch)
    _write_mcp_file(mcp_write_path(McpWriteScope.LOCAL, env=os.environ), {"declared": _stdio_dict("declared", "/bin/d")})
    before = _snapshot_mcp_files()

    comp = _comp(tmp_path)
    await _boot_mount(comp)

    assert _snapshot_mcp_files() == before, "boot 挂载已声明 server 不回写任一 mcp 文件（无双源/scope 漂移）"
    assert "declared" in _runtime_names(comp), "但确实挂载进运行期投影"
