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
  6. 寻址对拍（#150 R5②）：同 display 名 + 显式异 bundle_id 共存 → 运行期按 bundle_id 键各自保留（测寻址、非生成算法）。

#150 site3 改造：全套夹具 display name 与 bundle_id **取值分叉**（``"svc.disp"`` → bundle_id ``svc_disp``）；运行期存在性/
寻址断言经 :func:`_runtime_bundle_ids` 落在 **bundle_id** 维度，盘上 ``mcp.json`` 断言保 **name** 键（两识别空间分账，
后者为 wontfix 正交声明面）——缺省 name≡bundle_id 曾双重致盲，分叉后全套既有用例自动升级为身份泄漏守卫。

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
from a2c_smcp.utils.bundle_id import resolve_bundle_id


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


def _stdio_dict(name: str, command: str, *, disabled: bool = False, bundle_id: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {
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
    if bundle_id is not None:
        d["bundle_id"] = bundle_id  # #150 R5②：注入**显式** bundle_id，令 display name 与身份可控地分叉。
    return d


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
    await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)


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


def _runtime_bundle_ids(comp: Computer) -> set[str]:
    """运行期投影的**身份键集** = bundle_id（#150 site3/#4：断言维度改 bundle_id，不再只取 display name 丢弃身份维度）。

    Runtime projection's identity key set = bundle_id. server_configs() 内部按 bundle_id 键；``.name`` 仅 display，
    存在性/寻址断言 MUST 落在 bundle_id 维度，否则 name≡bundle_id 缺省路径会把身份错乱盖住（本文件夹具已全数分叉）。
    """
    return {resolve_bundle_id(c) for c in comp.mcp_manager.server_configs()} if comp.mcp_manager is not None else set()


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
    # #150 R5①：display name "svc.disp" → bundle_id "svc_disp"（分叉）；盘上 map key = name（wontfix 正交项）。
    await comp.aadd_or_aupdate_server(_stdio_dict("svc.disp", "${input:cmd}"))

    local = _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ))
    assert "svc.disp" in local, "durable 默认落 Local（mcp.local.json）；盘上按 name 键"
    assert local["svc.disp"]["server_parameters"]["command"] == "${input:cmd}", "盘上为 raw 未渲染（占位字面保留、非 /bin/echo）"
    assert "svc.disp" not in _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ)), "不碰 git 共享 project 层"
    # 内存投影为**渲染后**值——与盘上 raw 形成对照，坐实「盘上确为未渲染 raw」而非二者恰好相等的假阳性。
    runtime = {c.name: c for c in comp.mcp_manager.server_configs()}
    assert runtime["svc.disp"].server_parameters.command == "/bin/echo", "内存投影用渲染后结果（raw≠rendered，D1 断言可证伪）"


@pytest.mark.asyncio
async def test_alignment_durable_in_scope_project_persists_git_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """durable ``_in_scope(PROJECT)`` → ``.tfrobot/mcp.json``（入 git、团队共享层）。"""
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("shared.disp", "/bin/true"), McpWriteScope.PROJECT)

    assert "shared.disp" in _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ)), "显式 Project → git 层（盘上 name 键）"
    assert "shared.disp" not in _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ))


@pytest.mark.asyncio
async def test_alignment_durable_update_existing_lands_origin_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改已有 server 恒落其 origin scope（默认 Local 入参对已有声明无效，不迁移）。"""
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("svc.disp", "/bin/v1"), McpWriteScope.PROJECT)
    await comp.aadd_or_aupdate_server(_stdio_dict("svc.disp", "/bin/v2"))  # 默认 Local，但恒落 origin=Project

    project = _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ))
    assert "svc.disp" in project and project["svc.disp"]["server_parameters"]["command"] == "/bin/v2", "落 origin 且内容更新"
    assert "svc.disp" not in _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ)), "不漂移到 Local"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 2 — 重启存活（durable 声明经模拟 boot 后仍在运行期投影）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_durable_add_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """durable add → **全新 Computer**（模拟重启）经 ``run_mcp_approval`` 读盘挂载 → 该 server 仍在运行期投影。"""
    _isolate(tmp_path, monkeypatch)
    comp_a = _comp(tmp_path)
    await comp_a.aadd_or_aupdate_server(_stdio_dict("survivor.disp", "/bin/echo"))  # name "survivor.disp" → bundle_id "survivor_disp"
    assert "survivor_disp" in _runtime_bundle_ids(comp_a)

    # 「重启」= 丢弃 comp_a、全新实例读同一 cwd/XDG。
    comp_b = _comp(tmp_path)
    assert "survivor_disp" not in _runtime_bundle_ids(comp_b), "新实例构造期无运行期投影（未 boot）"
    await _boot_mount(comp_b)
    assert "survivor_disp" in _runtime_bundle_ids(comp_b), "durable 声明重启后仍挂载（对齐 rust 重启存活）"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 3 — remove 复活 footgun 守护（rust 8f4229a 复活守护自足 + Synthesized 拒删）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_remove_deletes_all_scopes_and_no_resurrection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人写 mcp.json 声明 → boot 挂载 → ``aremove_server`` 删所有可写 scope → **再 boot 不复活**（footgun 守护）。"""
    _isolate(tmp_path, monkeypatch)
    # 人在 project + local 两层都写了同名声明（复活 footgun 最险场景：删不干净就重启复活）。盘上 map key = name。
    _write_mcp_file(mcp_write_path(McpWriteScope.PROJECT, env=os.environ), {"ghost.disp": _stdio_dict("ghost.disp", "/bin/p")})
    _write_mcp_file(mcp_write_path(McpWriteScope.LOCAL, env=os.environ), {"ghost.disp": _stdio_dict("ghost.disp", "/bin/l")})

    comp_a = _comp(tmp_path)
    await _boot_mount(comp_a)
    assert "ghost_disp" in _runtime_bundle_ids(comp_a), "boot 读盘挂载人写声明（运行期身份键 = bundle_id）"

    # durable remove 以 **bundle_id** 寻址（name "ghost.disp" → bundle_id "ghost_disp"）→ 删所有可写 scope + 运行期停摘。
    await comp_a.aremove_server("ghost_disp")
    assert "ghost_disp" not in _runtime_bundle_ids(comp_a), "运行期投影已停摘"
    assert "ghost.disp" not in _read_servers(mcp_write_path(McpWriteScope.PROJECT, env=os.environ)), "project 声明删净（盘上 name 键）"
    assert "ghost.disp" not in _read_servers(mcp_write_path(McpWriteScope.LOCAL, env=os.environ)), "local 声明删净"

    # 关键：全新实例重启 → 盘上已无声明 → **不复活**。
    comp_b = _comp(tmp_path)
    await _boot_mount(comp_b)
    assert "ghost_disp" not in _runtime_bundle_ids(comp_b), "复活 footgun 守护：删后重启不复活"


@pytest.mark.asyncio
async def test_alignment_remove_rejects_undeclared_runtime_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无用户侧声明却运行期活跃的投影（plugin/治理）``aremove_server`` → 拒删（**origin 判据**，不按账本名集；
    对应 rust ``WriteTargetError::Synthesized``，#148/F3 取代历史 name-keyed bundled 拒删）。"""
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.amount_server(_stdio_dict("figma.disp", "/bin/figma"))  # 无盘声明的运行期投影（name "figma.disp" → bundle_id "figma_disp"）
    assert "figma.disp" not in resolve_mcp_config(env=os.environ).servers, "前置：盘上无任何 figma 声明（config 按 name 键）"

    with pytest.raises(McpWriteTargetError):
        await comp.aremove_server("figma_disp")  # 以 bundle_id 寻址
    assert "figma_disp" in _runtime_bundle_ids(comp), "拒删后运行期投影仍在（未误停摘）"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 4 — transient 不落盘（只运行期投影变、config 源字节不变）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_transient_mount_does_not_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``amount_server`` 前后所有 mcp 文件字节不变（config 源稳定），但运行期投影新增（capability 面变）。"""
    _isolate(tmp_path, monkeypatch)
    # 先播一个人写声明，证明 transient 连既有文件也不动。盘上 map key = name。
    _write_mcp_file(mcp_write_path(McpWriteScope.PROJECT, env=os.environ), {"declared.disp": _stdio_dict("declared.disp", "/bin/d")})
    comp = _comp(tmp_path)
    before = _snapshot_mcp_files()

    await comp.amount_server(_stdio_dict("ephemeral.disp", "/bin/e"))

    assert _snapshot_mcp_files() == before, "transient 挂载不改任一 mcp 文件（config 源不变）"
    assert "ephemeral_disp" in _runtime_bundle_ids(comp), "但运行期投影已含新 server（capability 面变；身份键 = bundle_id）"


@pytest.mark.asyncio
async def test_alignment_transient_unmount_does_not_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``aunmount_server_by_id(bundle_id)`` 只摘运行期投影，不动任一 mcp 文件。

    #143 / R4：库层停摘一律收 **bundle_id**（历史 name 便捷入口已删，rust 侧 ``unmount_server`` 收 id 后同构）。
    """
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.amount_server(_stdio_dict("tmp.srv.disp", "/bin/t"))  # name "tmp.srv.disp" → bundle_id "tmp_srv_disp"
    before = _snapshot_mcp_files()  # 均不存在（transient 未落盘）

    await comp.aunmount_server_by_id("tmp_srv_disp")  # 以 **bundle_id**（身份）寻址，非 display name

    assert _snapshot_mcp_files() == before, "transient 停摘不产生任何落盘"
    assert "tmp_srv_disp" not in _runtime_bundle_ids(comp), "运行期投影已摘除（身份键 = bundle_id）"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 5 — 投影调用方不回写（#138 ③ 守护：boot 挂载已声明 server 不改用户 mcp.json）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_boot_mount_does_not_write_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_mcp_approval`` 挂载**已声明** server（投影语义）→ 用户 mcp.json 层 **byte-diff 干净**（不回写）。"""
    _isolate(tmp_path, monkeypatch)
    _write_mcp_file(mcp_write_path(McpWriteScope.LOCAL, env=os.environ), {"declared.disp": _stdio_dict("declared.disp", "/bin/d")})
    before = _snapshot_mcp_files()

    comp = _comp(tmp_path)
    await _boot_mount(comp)

    assert _snapshot_mcp_files() == before, "boot 挂载已声明 server 不回写任一 mcp 文件（无双源/scope 漂移）"
    assert "declared_disp" in _runtime_bundle_ids(comp), "但确实挂载进运行期投影（身份键 = bundle_id）"


# ═══════════════════════════════════════════════════════════════════════════════
# 组 6 — 寻址对拍（#150 R5②：同 display 名 + 显式异 bundle_id 共存，测「寻址行为」非「生成算法」）
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_alignment_same_name_distinct_bundle_id_coexist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**同 display 名 + 显式异 bundle_id** 两 server 经 transient 挂载 → 运行期投影**同时含**两 bundle_id（各自共存）。

    English: two servers sharing a display name but carrying explicitly distinct bundle_id coexist in the runtime
    projection, each keyed by its own bundle_id.

    #150 R5② 针对根因：现有对拍只测**生成算法**（逐字节向量），缺陷全在**寻址行为**——本向量正是补上这一半。
    manager 以 bundle_id 为键：display 名碰撞不塌缩、异 bundle_id 不触发 no-double-open ⇒ 二者并存。
    用 transient ``amount_server`` 避开盘上 name-keyed mcp.json 的同名覆盖（那是正交的 wontfix 声明面形态）。
    对应 rust：``mount_server`` 两次异 bundle_id 后 ``server_configs()`` 含两条——SDK 方法名不强制对拍，wire/寻址语义一致即可。
    """
    _isolate(tmp_path, monkeypatch)
    comp = _comp(tmp_path)
    await comp.amount_server(_stdio_dict("dup.disp", "/bin/a", bundle_id="dup-a"))
    await comp.amount_server(_stdio_dict("dup.disp", "/bin/b", bundle_id="dup-b"))

    # 运行期身份键 = bundle_id：两条各按自身 bundle_id 共存（若寻址误按 name 则塌缩为 1，此断言即红）。
    assert {"dup-a", "dup-b"} <= _runtime_bundle_ids(comp)
    # 佐证：两条 display name 逐字相同——证明区分靠 bundle_id 而非 name。
    names = [c.name for c in comp.mcp_manager.server_configs()]
    assert names.count("dup.disp") == 2
