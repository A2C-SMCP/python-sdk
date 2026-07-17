# -*- coding: utf-8 -*-
# filename: test_mcp_flag_config.py
# @Time    : 2026/07/17
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
flag 层 mcp.json（``--mcp-config``）+ embed 层（构造入参）的**真实构造路径**契约测试（#154 / #164）。
Contract tests for the flag-layer mcp.json (``--mcp-config``) and the embed layer, on the REAL construction path.

协议 / Protocol: ``computer-management/runtime-contract.md`` §2.5-3（唯一优先序：
``plugin < user < project < local < embed < flag < policy``）、§2.5-5（运行期权威配置集 MUST 携带 origin）；
``conformance-tests.md`` §2.0（夹具 name/bundle_id 分叉）+ **F7**（涉及 Computer 状态的契约测试 MUST 至少
一条走**真实构造路径**，不得全部依赖桩）。

**本文件即 F7 的落点**：走**真** :class:`Computer`（CLI 同款空集构造 ``inputs=set(), mcp_servers=set()``）+
**真** :func:`run_mcp_approval`，**不用** ``_FakeComp``。Epic #147 记载：``_FakeComputer`` 桩曾把「``Computer.mcp_servers``
有内容」这个生产中恒假的前提固化为真，**逃过 F7 之前的所有条款**——故此处刻意不桩 Computer。

测试意图 / Test intentions:
- flag 文件的 **``inputs`` 段**被消费（``--inputs`` 删除后的替代通路）+ server 的 ``${input:}`` 得以渲染；
- flag origin ⇒ trusted ⇒ 门控判 ENABLED ⇒ 挂载（净效果同旧 ``--config``，但**现在带 origin**）；
- flag server **可被 policy 拒绝名单拦下**（新行为：旧 ``--config`` 完全绕开门控直挂）；
- embed server（宿主构造入参）⇒ trusted ⇒ ENABLED；被 policy 拒绝时 ⇒ **确保停摘**（否则拒绝名单形同虚设）。

隔离：``monkeypatch.chdir(tmp)`` 锚 project/local（#116）+ ``XDG_CONFIG_HOME`` → tmp 锚 user。
**chdir 是必须的**：#137 flip 后 durable 路径落 ``cwd/.tfrobot/``，不 chdir 即污染真实仓库。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from a2c_smcp.computer.cli.commands.plugin import run_mcp_approval
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.mcp_config import MANAGED_MCP_FILENAME
from a2c_smcp.utils.bundle_id import resolve_bundle_id

# 夹具身份对：display name 含 `.` → normalize_name 折成 `_` ⇒ name ≠ bundle_id（conformance §2.0）。
# ⚠️ **勿用 `-`**：`normalize_name` **不折叠** `-`，`e2e-direct` 的 bundle_id 恰等于自身 ⇒ 两概念不分叉 ⇒
# 「误用 name 当身份」的实现能蒙混过关（本仓已因此出过四次假绿）。
SRV_NAME, SRV_BID = "e2e.direct", "e2e_direct"


@pytest.fixture(autouse=True)
def _no_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """policy 读 OS 源不确定 → 缺省固定为空（个别用例自行覆盖）/ pin policy empty for determinism。"""
    import a2c_smcp.computer.settings.policy as policy_mod

    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_: {})


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)  # #137：durable 落 cwd/.tfrobot/ ⇒ 不 chdir 即写进真实仓库
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return env


def _server_body(command_placeholder: str) -> dict[str, Any]:
    """一条 stdio server；``args[0]`` 走 ``${input:}`` 占位符，用以验证 inputs 段真的被消费并参与渲染。"""
    return {
        "type": "stdio",
        "disabled": True,  # 免真拉进程：本测试验证配置态/门控/渲染，不验证 spawn
        "forbidden_tools": [],
        "tool_meta": {},
        "server_parameters": {
            "command": "python",
            "args": [command_placeholder],
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
    }


def _write_flag_file(tmp_path: Path, *, servers: dict[str, Any], inputs: list[dict[str, Any]]) -> Path:
    p = tmp_path / "flag-mcp.json"
    p.write_text(json.dumps({"servers": servers, "inputs": inputs}), encoding="utf-8")
    return p


def _real_computer(tmp_path: Path, **kw: Any) -> Computer:
    """**CLI 同款真实构造**（空集 inputs/mcp_servers）——F7 要求，勿换成桩。"""
    return Computer(
        name="flag-config-test",
        inputs=set(),
        mcp_servers=set(),
        auto_connect=False,
        auto_reconnect=False,
        skill_home=tmp_path / "home",
        **kw,
    )


@pytest.mark.asyncio
async def test_mcp_flag_config_inputs_segment_consumed_and_server_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    **F7 主用例**：``--mcp-config`` 文件的 ``inputs`` 段入池，且 server 的 ``${input:}`` 据此渲染后挂载。

    这是 ``--inputs`` 被删除后的**替代通路**——旧 ``--config`` 的 schema（裸 server 对象）**没有 inputs 字段**，
    故当年必须另立 ``--inputs``；现 flag 层就是 mcp.json，``{servers, inputs}` 一体，通路归一。

    **变异验证**：把 ``plugin.py`` 的 ``comp.resolve_mcp_declarations(env=env)`` 换回
    ``resolve_mcp_config(env=env, flag_config_path=None)`` → resolved.servers 空 → 早 return → ``SCRIPT``
    不入池、server 不挂 → 本例转红（这是 `test_run_impl_hands_mcp_flag_path_to_computer` 的搭档守卫：
    那条只钉「交接」，本条钉「真的被消费」）。
    """
    env = _isolate(tmp_path, monkeypatch)
    script = "tests/integration_tests/computer/mcp_servers/direct_execution.py"
    flag = _write_flag_file(
        tmp_path,
        servers={SRV_NAME: _server_body("${input:SCRIPT}")},
        inputs=[{"id": "SCRIPT", "type": "promptString", "description": "server script", "default": script}],
    )

    comp = _real_computer(tmp_path, mcp_flag_config=flag)
    async with comp:
        await run_mcp_approval(comp, None, approve_all=False, settings_flag_path=None)

        # ① inputs 段真的入池（旧 --config schema 根本没有 inputs 字段 ⇒ 此断言在旧通路上无从谈起）
        assert "SCRIPT" in {i.id for i in comp.inputs}, "--mcp-config 的 inputs 段未被消费"

        # ② server 已挂 且 ${input:SCRIPT} 已渲染（证明 inputs 段确实参与了渲染，而不只是入池）
        assert comp.mcp_manager is not None
        active = {resolve_bundle_id(c): c for c in comp.mcp_manager.server_configs()}
        assert SRV_BID in active, "flag 层声明的 server 未挂载"
        assert active[SRV_BID].server_parameters.args == [script], "${input:SCRIPT} 未渲染"

        # ③ 该 server 的 origin 可观测为 flag（§2.5-5：权威集 MUST 携带 origin）
        declared = comp.resolve_mcp_declarations(env=env)
        assert declared.servers[SRV_NAME].origin.value == "flag"
        assert declared.servers[SRV_NAME].trusted_origin is True  # flag ∈ _TRUSTED_ORIGINS ⇒ 免批准框


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True], ids=["control_mounts", "denied_blocked"])
async def test_flag_server_can_be_denied_by_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: bool,
) -> None:
    """
    **新行为守卫**（用户已拍板接受）：flag server 现在**过审批门** ⇒ 可被 policy 拒绝名单拦下、**不挂载**。

    旧 ``--config`` 在 ``_run_impl`` 里 ``json.loads`` → ``amount_server`` 直挂，**完全绕开门控** ⇒ 企业
    policy 对它形同虚设。协议 §2.5-3 明定 ``policy > flag``，此为其落地。

    ⚠️ **必须带 ``denied=False`` 的正对照**：只断言「被拒时不在活跃集」是**弱断言**——任何让 flag 层压根没被
    解析的回归（如 ``plugin.py`` 退回 ``flag_config_path=None``）都会让它**因错误的理由变绿**（server 从未被
    考虑过 ⇒ 自然不在活跃集）。正对照钉住「不拒时它确实会挂」，两者合起来才真正测到「是**拒绝名单**拦下了它」。
    实测：无正对照时本例在该变异下保持绿（本次自查抓到的假绿）。
    """
    _isolate(tmp_path, monkeypatch)
    import a2c_smcp.computer.settings.policy as policy_mod

    policy = {"deniedMcpServers": [SRV_NAME]} if denied else {}
    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_: policy)
    flag = _write_flag_file(tmp_path, servers={SRV_NAME: _server_body("x.py")}, inputs=[])

    comp = _real_computer(tmp_path, mcp_flag_config=flag)
    async with comp:
        await run_mcp_approval(comp, None, approve_all=False, settings_flag_path=None)
        assert comp.mcp_manager is not None
        active = {resolve_bundle_id(c) for c in comp.mcp_manager.server_configs()}
        if denied:
            assert SRV_BID not in active, "policy 拒绝名单未能拦下 flag server（协议 §2.5-3：policy > flag）"
        else:
            assert SRV_BID in active, "正对照：未被拒绝时 flag server 必须挂上（否则上一分支是假绿）"


@pytest.mark.asyncio
async def test_embed_server_is_trusted_and_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    **embed 层**（宿主构造入参 ``Computer(mcp_servers=...)``）：origin=embed ⇒ trusted ⇒ ENABLED（档④）。

    宿主构造入参是**代码级显式意图**，与 flag 同属「调用方显式受信层」（Discussion #32 裁决 / §2.5-3）。
    ``boot_up`` 已无门挂起它 ⇒ 门控循环**不得重复挂**（会 restart 客户端 / ``auto_reconnect=False`` 时抛）。
    """
    env = _isolate(tmp_path, monkeypatch)
    embed_cfg = TypeAdapter(MCPServerConfig).validate_python({"name": SRV_NAME, **_server_body("x.py")})

    comp = _real_computer(tmp_path)
    comp._mcp_servers = {embed_cfg}  # 模拟宿主构造入参（构造后注入，避免与 _real_computer 的空集签名打架）
    async with comp:
        declared = comp.resolve_mcp_declarations(env=env)
        assert declared.servers[SRV_NAME].origin.value == "embed"
        assert declared.servers[SRV_NAME].trusted_origin is True

        await run_mcp_approval(comp, None, approve_all=False, settings_flag_path=None)
        assert comp.mcp_manager is not None
        active = {resolve_bundle_id(c) for c in comp.mcp_manager.server_configs()}
        assert SRV_BID in active, "embed server 应保持活跃（boot_up 已挂，门控判 ENABLED 不得摘）"


@pytest.mark.asyncio
async def test_embed_server_denied_by_policy_is_unmounted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    **档①②③ 对 embed 适用**（协议 §2.5 裁决：「用户/管理员保留最终关停权」）。

    ``boot_up`` **已无门挂起** embed server ⇒ 门控判 DISABLED 时若只打印不摘，policy 拒绝名单对 embed 就是
    **装饰品**。故循环契约是「DISABLED ⇒ **确保停摘**」而非「跳过」。

    **变异验证**：把 ``run_mcp_approval`` 的 DISABLED 分支改回只 ``console.print`` 不 ``_ensure_unmounted``
    → 本例转红。
    """
    _isolate(tmp_path, monkeypatch)
    import a2c_smcp.computer.settings.policy as policy_mod

    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_: {"deniedMcpServers": [SRV_NAME]})
    embed_cfg = TypeAdapter(MCPServerConfig).validate_python({"name": SRV_NAME, **_server_body("x.py")})

    comp = _real_computer(tmp_path)
    comp._mcp_servers = {embed_cfg}
    async with comp:
        assert comp.mcp_manager is not None
        pre = {resolve_bundle_id(c) for c in comp.mcp_manager.server_configs()}
        assert SRV_BID in pre, "前置：boot_up 无门挂起 embed server（正是本守卫存在的理由）"

        await run_mcp_approval(comp, None, approve_all=False, settings_flag_path=None)

        post = {resolve_bundle_id(c) for c in comp.mcp_manager.server_configs()}
        assert SRV_BID not in post, "policy 拒绝的 embed server 未被停摘 ⇒ 拒绝名单形同虚设"


@pytest.mark.asyncio
async def test_flag_beats_local_on_real_construction_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    **层序在真实构造路径上的端到端体现**：同名 server 上 flag 覆盖 local（§2.5-3 flag 次高）。

    Group A 的单元守卫钉的是 ``resolve_mcp_config`` 纯函数；本例钉「经 Computer 注入后，挂起来的确实是 flag 那份」。
    """
    _isolate(tmp_path, monkeypatch)
    (tmp_path / ".tfrobot").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".tfrobot" / "mcp.local.json").write_text(
        json.dumps({"servers": {SRV_NAME: _server_body("local.py")}, "inputs": []}), encoding="utf-8",
    )
    flag = _write_flag_file(tmp_path, servers={SRV_NAME: _server_body("flag.py")}, inputs=[])

    comp = _real_computer(tmp_path, mcp_flag_config=flag)
    async with comp:
        await run_mcp_approval(comp, None, approve_all=False, settings_flag_path=None)
        assert comp.mcp_manager is not None
        active = {resolve_bundle_id(c): c for c in comp.mcp_manager.server_configs()}
        assert active[SRV_BID].server_parameters.args == ["flag.py"], "flag 未覆盖 local（改前 local 胜）"


def test_managed_mcp_filename_is_stable() -> None:
    """``MANAGED_MCP_FILENAME`` 被本模块的 policy 用例间接依赖；此处仅做导入面存在性守卫。"""
    assert MANAGED_MCP_FILENAME.endswith(".json")
