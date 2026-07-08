# -*- coding: utf-8 -*-
# filename: test_v02_update_tool_list_e2e.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：#127 —— MCP 运行期工具变化 → Agent 工具缓存刷新的真进程全链路 e2e。

  在真实 Uvicorn ASGI 进程 + 真实 SMCPNamespace（广播 notify:update_tool_list）+ 真实握手中间件 +
  真实 stdio MCP 子进程（``mutable_tools_stdio_server.py``）上，端到端复核完整刷新链：

    MCP tools/list_changed → Computer emit server:update_tool_list → Server 广播 notify:update_tool_list
    → Agent 自动回拉 client:get_tools → 预清回调 on_computer_update_tool_list + on_tools_received

  用运行期可变工具集驱动**新增 / 同名换 schema / 移除**三类变化，且**不伴随任何 config 变更**（纯
  tools/list_changed，专门规避靠 notify:update_config 蹭刷新的假通过）。消费方 handler 采用**加法式**
  工具注册表（on_tools_received 只合并、不删），故：
    - remove 断言唯有**预清回调**生效才成立（加法式若无预清则旧工具残留）——直接验证 #127 关键约束；
    - fire-and-forget 无 ack，断言一律用带超时的轮询等待（wait_for）。

English: Real-process full-chain e2e for #127 (runtime MCP tool change → Agent tool-cache refresh).
  An additive consumer registry makes the *remove* assertion pass only when the pre-clean callback fires.

对标样板 / Mirrors: ``test_v02_skill_blob_e2e.py::test_update_skills_live_refetch_loop``（SKILL 刷新链样板）。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer import Computer
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig, ToolMeta
from tests.e2e._skill_harness import connect_agent_and_computer, running_server, teardown

pytestmark = pytest.mark.e2e

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MUTABLE_SRV = _PROJECT_ROOT / "tests" / "integration_tests" / "computer" / "mcp_servers" / "mutable_tools_stdio_server.py"


def _mutable_cfg(name: str = "mutable-srv") -> StdioServerConfig:
    """构造可变工具集 stdio MCP Server 配置（auto_apply 跳过二次确认，供 e2e 无人值守调用）。"""
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(_MUTABLE_SRV)]),
        default_tool_meta=ToolMeta(auto_apply=True),
    )


class _AdditiveToolRegistry:
    """中文：加法式工具注册表消费方（对标真实 Agent 消费方语义：on_tools_received 只增；删由预清回调完成）。

    - ``on_computer_update_tool_list``：**预清**——清空该 computer 的旧工具（#127 新回调）。
    - ``on_tools_received``：**合并**——按工具名写入（不删）。若无预清，则移除/换 schema 会残留旧定义。
    其余回调实现为 no-op，避免连接握手期 AttributeError。
    """

    def __init__(self) -> None:
        # {computer: {tool_name: tool_dict}}
        self.registry: dict[str, dict[str, dict]] = {}
        self.preclean_calls = 0

    async def on_computer_enter_office(self, data, client) -> None:  # noqa: ANN001
        return None

    async def on_computer_leave_office(self, data, client) -> None:  # noqa: ANN001
        return None

    async def on_computer_update_config(self, data, client) -> None:  # noqa: ANN001
        return None

    async def on_computer_update_tool_list(self, data, client) -> None:  # noqa: ANN001
        # 预清：清空该 computer 的旧工具集（保留 key 便于观测）
        self.preclean_calls += 1
        self.registry[data["computer"]] = {}

    async def on_tools_received(self, computer, tools, client) -> None:  # noqa: ANN001
        bucket = self.registry.setdefault(computer, {})
        for t in tools:
            bucket[t["name"]] = t

    async def on_skills_received(self, computer, skills, client) -> None:  # noqa: ANN001
        return None

    # —— 观测辅助 / observation helpers ——
    def names(self, computer: str) -> set[str]:
        return set(self.registry.get(computer, {}).keys())

    def schema_props(self, computer: str, tool: str) -> set[str]:
        t = self.registry.get(computer, {}).get(tool)
        return set((t or {}).get("params_schema", {}).get("properties", {}).keys())


class _LegacyAdditiveRegistry:
    """中文：**旧**消费方——未实现 ``on_computer_update_tool_list`` 预清回调（模拟未升级的下游）。

    仅加法式 ``on_tools_received``（只增不删）。用于真链路验证 SDK 的向后兼容契约：
      - SDK 的 ``hasattr`` 守卫使旧消费方**仍能**因 notify:update_tool_list 自动回拉看到新增工具；
      - 但缺预清 → **移除**后旧工具在其本地视图残留（反证预清回调 ``on_computer_update_tool_list`` 的必要性）。
    """

    def __init__(self) -> None:
        self.registry: dict[str, dict[str, dict]] = {}

    async def on_computer_enter_office(self, data, client) -> None:  # noqa: ANN001
        return None

    async def on_computer_leave_office(self, data, client) -> None:  # noqa: ANN001
        return None

    async def on_computer_update_config(self, data, client) -> None:  # noqa: ANN001
        return None

    # 故意**不**实现 on_computer_update_tool_list（模拟旧 handler）/ intentionally NO pre-clean hook

    async def on_tools_received(self, computer, tools, client) -> None:  # noqa: ANN001
        bucket = self.registry.setdefault(computer, {})
        for t in tools:
            bucket[t["name"]] = t

    async def on_skills_received(self, computer, skills, client) -> None:  # noqa: ANN001
        return None

    def names(self, computer: str) -> set[str]:
        return set(self.registry.get(computer, {}).keys())

    def schema_props(self, computer: str, tool: str) -> set[str]:
        t = self.registry.get(computer, {}).get(tool)
        return set((t or {}).get("params_schema", {}).get("properties", {}).keys())


async def _wait_until(pred: Callable[[], bool], *, timeout: float = 20.0, interval: float = 0.1) -> None:
    """轮询等待 pred() 为真（fire-and-forget 无 ack；宽超时防 CI 抖动）。超时抛 TimeoutError。"""

    async def _loop() -> None:
        while not pred():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_loop(), timeout=timeout)


@pytest.mark.asyncio
async def test_update_tool_list_live_refetch_add_change_remove(tmp_path) -> None:  # noqa: ANN001
    """运行期 新增→同名换 schema→移除 均经 notify:update_tool_list 回拉后反映到 Agent 本地工具视图。"""
    office_id = "e2e-update-tool-list"
    handler = _AdditiveToolRegistry()
    computer = Computer(name="comp-tl", mcp_servers={_mutable_cfg()})

    async with running_server() as base:
        agent, _comp_client = await connect_agent_and_computer(
            base,
            office_id,
            computer,
            agent_id="robot-tl",
            event_handler=handler,
        )
        try:
            # —— 基线：enter_office 自动回拉 → 仅控制工具 set_phase ——
            await _wait_until(lambda: "set_phase" in handler.names("comp-tl"))
            assert "dynamic_tool" not in handler.names("comp-tl")

            # —— 新增（不伴随 config 变更）：set_phase(1) → dynamic_tool(schemaA: alpha) ——
            await agent.emit_tool_call("comp-tl", "set_phase", {"phase": 1}, timeout=10)
            await _wait_until(lambda: "dynamic_tool" in handler.names("comp-tl"))
            assert handler.schema_props("comp-tl", "dynamic_tool") == {"alpha"}

            # —— 同名换 schema：set_phase(2) → dynamic_tool(schemaB: beta，旧 alpha 须清） ——
            await agent.emit_tool_call("comp-tl", "set_phase", {"phase": 2}, timeout=10)
            await _wait_until(lambda: handler.schema_props("comp-tl", "dynamic_tool") == {"beta"})
            assert "alpha" not in handler.schema_props("comp-tl", "dynamic_tool")

            # —— 移除：set_phase(0) → dynamic_tool 消失（唯预清回调生效才成立，加法式否则残留） ——
            # 注意等**正向终态** names=={set_phase}：预清会先清空注册表，若只等「dynamic_tool 不在」会命中
            # 「清空后、on_tools_received 重加 set_phase 前」的空窗而误判。
            await agent.emit_tool_call("comp-tl", "set_phase", {"phase": 0}, timeout=10)
            await _wait_until(lambda: handler.names("comp-tl") == {"set_phase"})
            assert "dynamic_tool" not in handler.names("comp-tl")

            # 预清回调确实被触发过（新增/换 schema/移除 至少三次）/ pre-clean hook actually fired
            assert handler.preclean_calls >= 3
        finally:
            await teardown(agent, computer)


@pytest.mark.asyncio
async def test_legacy_consumer_backward_compat_and_preclean_necessity(tmp_path) -> None:  # noqa: ANN001
    """真链路验证向后兼容契约：旧消费方（无预清回调）仍能因通知自动回拉看到**新增**工具；
    但**移除**后旧工具在其本地视图残留——反证预清回调 on_computer_update_tool_list 的必要性。

    非空断言设计：先用 add→同名换 schema（alpha→beta，**正向可观测**）证明整条 notify→回拉链对旧消费方
    **持续**生效（非一次性），再验证 remove 后 dynamic_tool 仍带最新 beta schema 残留（同一机制的 remove
    也已触发、但旧消费方无从清理）。与含预清的 ``test_update_tool_list_live_refetch...``（remove 后
    dynamic_tool 干净消失）形成直接对照。
    """
    office_id = "e2e-update-tool-list-legacy"
    legacy = _LegacyAdditiveRegistry()
    computer = Computer(name="comp-tl-legacy", mcp_servers={_mutable_cfg()})

    async with running_server() as base:
        agent, _comp_client = await connect_agent_and_computer(
            base,
            office_id,
            computer,
            agent_id="robot-tl-legacy",
            event_handler=legacy,
        )
        try:
            # 基线：enter_office 自动回拉 → 仅 set_phase / baseline
            await _wait_until(lambda: "set_phase" in legacy.names("comp-tl-legacy"))

            # 新增：旧消费方**仍**因 notify:update_tool_list 自动回拉看到 dynamic_tool（hasattr 守卫不破链路）
            await agent.emit_tool_call("comp-tl-legacy", "set_phase", {"phase": 1}, timeout=10)
            await _wait_until(lambda: legacy.schema_props("comp-tl-legacy", "dynamic_tool") == {"alpha"})

            # 同名换 schema：alpha→beta 正向可观测——证明 notify→回拉链对旧消费方**持续**生效（非一次性）
            await agent.emit_tool_call("comp-tl-legacy", "set_phase", {"phase": 2}, timeout=10)
            await _wait_until(lambda: legacy.schema_props("comp-tl-legacy", "dynamic_tool") == {"beta"})

            # 移除：set_phase(0) → 同一链路已被证明生效，但旧消费方缺预清 → dynamic_tool 残留（仍带 beta schema）
            await agent.emit_tool_call("comp-tl-legacy", "set_phase", {"phase": 0}, timeout=10)
            await asyncio.sleep(3.0)  # 让 notify→回拉→on_tools_received 链完成（无 ack，确定性宽等）
            assert legacy.schema_props("comp-tl-legacy", "dynamic_tool") == {"beta"}, (
                "旧消费方缺预清 → 移除的工具应残留（正是 on_computer_update_tool_list 预清回调要解决的问题）"
            )
        finally:
            await teardown(agent, computer)
