# -*- coding: utf-8 -*-
# filename: test_tool_list_cache.py
# @Author  : JQQ
# @Software: PyCharm
"""#222 per-bundle 工具列表缓存 + 状态锁内 RPC 移出——契约用例。

**背景**：``_arefresh_tool_mapping`` 曾对**全部**活跃 client 各发一次 ``list_tools()`` 且**持状态锁**执行
（批量启动 N 个 ⇒ Σk = N(N+1)/2 次 RPC：6 个 = 21 次、20 个 = 210 次），尾巴被串行化成 O(N²) 个 RPC 时长。

**本文件钉死的两组契约**：

1. **缓存契约**（P1）：提交点只真读「新加入 / 缓存失效」的 bundle；公开 ``arefresh_tools()`` **恒全量重读**；
   失败必重读（路由可恢复）；配置变更**无需 RPC** 即被投影吸收；撤回点同步剪除缓存。
2. **锁窗口契约**（P2）：``list_tools`` RPC 期间**不得**持有状态锁；锁外窗口内落地的配置更新不得被陈旧快照覆盖。

/ Contracts for the per-bundle tool-list cache (#222): incremental structural commits, always-force public
refresh, failure-driven re-read, config absorbed without RPC, no resurrection from cache, and no RPC while the
state lock is held.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import StdioServerParameters
from mcp.types import CallToolResult, ReadResourceResult, Resource, Tool

from a2c_smcp.computer.mcp_clients.base_client import STATES
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import MCPClientProtocol, MCPServerConfig, StdioServerConfig, ToolMeta

# ────────────────────── 受控假件 / Controlled doubles ──────────────────────


class ToolListProbe:
    """每次 ``list_tools()`` 都真发（可计次）的假 client；支持挂起 / 抛错 / 换工具集。

    A fake client whose ``list_tools`` always really runs (countable), with hooks for failure and pause scenarios.
    """

    def __init__(self, config: MCPServerConfig, tools: list[Tool], message_handler: Any = None) -> None:
        self.config = config
        self.name = config.name
        self.state: STATES = STATES.connected
        self.message_handler = message_handler
        self.tools: list[Tool] = tools
        #: 置位后 ``list_tools`` 会挂在门上（用于把 RPC 窗口钉住）
        self.gate: asyncio.Event | None = None
        #: 置位后 ``list_tools`` 抛错
        self.boom: Exception | None = None
        #: 每次 list_tools 进入时登记「此刻状态锁是否被持有」（#222 P2 探针）
        self.lock_states: list[bool] = []
        #: 每次 list_tools 进入即置位（等待「RPC 已在途」用）
        self.entered = asyncio.Event()
        self.list_tools = AsyncMock(side_effect=self._list_tools)
        self.aconnect = AsyncMock()
        self.adisconnect = AsyncMock()
        self.call_tool = AsyncMock(return_value=cast(CallToolResult, MagicMock(spec=CallToolResult)))
        self._challenge_event: asyncio.Event | None = None
        self._redirect_event: asyncio.Event | None = None

    @property
    def calls(self) -> int:
        return int(self.list_tools.await_count)

    async def _list_tools(self) -> list[Tool]:
        self.lock_states.append(_LOCK_PROBE["locked"])
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.boom is not None:
            raise self.boom
        return self.tools

    def connect_challenge_event(self) -> asyncio.Event:
        if self._challenge_event is None:
            self._challenge_event = asyncio.Event()
        return self._challenge_event

    def connect_redirect_event(self) -> asyncio.Event:
        if self._redirect_event is None:
            self._redirect_event = asyncio.Event()
        return self._redirect_event

    def take_connect_redirect_stop(self) -> None:
        return None

    # 协议面其余方法（本用例不使用，仅为满足 MCPClientProtocol）
    async def list_windows(self) -> list[Resource]:
        return []

    async def list_resources_page(self, cursor: str | None = None) -> tuple[list[Resource], str | None]:
        return [], None

    async def get_window_detail(self, resource: Resource | str) -> ReadResourceResult:
        raise NotImplementedError


#: 状态锁持有探针（由 manager fixture 装上读写钩子）
_LOCK_PROBE: dict[str, bool] = {"locked": False}


class Harness:
    """假 client 工厂：按 name 建档探针，便于逐 client 计数。"""

    def __init__(self) -> None:
        self.probes: dict[str, ToolListProbe] = {}

    def factory(self, config: MCPServerConfig, message_handler: Any = None, **_kw: Any) -> ToolListProbe:
        probe = ToolListProbe(config, [_tool(_default_tool_name(config.name))], message_handler)
        self.probes[config.name] = probe
        return probe


def _tool(name: str) -> Tool:
    return Tool(name=name, inputSchema={"type": "object"})


def _default_tool_name(server_name: str) -> str:
    """Harness 给每个 server 的默认单工具名（exposed 名 = ``{bundle_id}__{本名}``）。"""
    return f"{server_name}_tool"


def _exposed(server_name: str, tool_name: str | None = None) -> str:
    return f"{server_name}__{tool_name or _default_tool_name(server_name)}"


def _cfg(
    name: str,
    *,
    forbidden_tools: list[str] | None = None,
    tool_meta: dict[str, ToolMeta] | None = None,
) -> MCPServerConfig:
    return StdioServerConfig(
        name=name,
        disabled=False,
        forbidden_tools=forbidden_tools or [],
        tool_meta=tool_meta or {},
        default_tool_meta=None,
        server_parameters=MagicMock(spec=StdioServerParameters),
    )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness()
    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", h.factory)
    return h


@pytest.fixture
async def manager(harness: Harness) -> MCPServerManager:
    del harness  # 仅用于挂上 client_factory
    # auto_connect=True：``aadd_or_aupdate_server`` 的启动分支需要它（`_adecide_add_or_update` 的 auto 闸）
    manager = MCPServerManager(auto_connect=True)
    await manager.enable_auto_reconnect()
    # 状态锁持有探针（#222 P2）：假 client 在 list_tools 内读取
    _LOCK_PROBE["locked"] = False
    raw_acquire, raw_release = manager._lock.acquire, manager._lock.release

    async def tracked_acquire() -> bool:
        got = await raw_acquire()
        _LOCK_PROBE["locked"] = True
        return got

    def tracked_release() -> None:
        _LOCK_PROBE["locked"] = False
        raw_release()

    manager._lock.acquire = tracked_acquire  # type: ignore[method-assign]
    manager._lock.release = tracked_release  # type: ignore[method-assign]
    return manager


def _probe(manager: MCPServerManager, name: str) -> ToolListProbe:
    """按 bundle_id（stdio 夹具下 == 配置名）取活跃 client 探针。"""
    return cast(ToolListProbe, manager._active_clients[name])


def _counts(manager: MCPServerManager, names: list[str]) -> dict[str, int]:
    return {name: _probe(manager, name).calls for name in names}


async def _start_all(manager: MCPServerManager, names: list[str]) -> list[str]:
    """登记 + 逐个启动（每个走各自的提交点），返回 bundle_id 列表（顺序同 names）。"""
    await manager.ainitialize([_cfg(n) for n in names])
    ids = manager.enabled_bundle_ids()
    for bundle_id in ids:
        await manager.astart_clients_batch([bundle_id])
    return ids


# ────────────────────── P1：缓存契约 / Cache contracts ──────────────────────


@pytest.mark.asyncio
async def test_each_commit_relists_only_its_own_bundle(manager: MCPServerManager) -> None:
    """**复现（#222 核心）**：逐个启动 3 个 server ⇒ ``list_tools`` 各恰 1 次、总计 3 次。

    旧行为：每次提交对**全部**活跃 client 全量重读 ⇒ 1+2+3 = 6 次。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2", "cache_srv_3"])

    counts = _counts(manager, ids)
    assert counts == dict.fromkeys(ids, 1), f"每个 bundle 只应在自身提交点被真读一次，实际：{counts}"
    assert sum(counts.values()) == 3, f"3 个 server 的总 RPC 次数应为 3（旧行为 6），实际：{counts}"


@pytest.mark.asyncio
async def test_new_server_does_not_relist_existing(manager: MCPServerManager) -> None:
    """新增 server（结构变更）⇒ 既有 server **零重读**，新 server 恰真读一次（提交点即时可见）。"""
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    before = _counts(manager, ids)

    await manager.astart_clients_batch([ids[0]])  # 幂等早退：不刷新
    assert _counts(manager, ids) == before, "重复启动必须幂等（零重读）"

    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))

    assert _counts(manager, ids) == before, f"新增 server 不得重读既有 server（旧行为各 +1）：{before}"
    assert _probe(manager, "cache_srv_3").calls == 1, "新 server 自身须恰被真读一次"
    assert _exposed("cache_srv_3") in manager._exposed_tools, "新 server 的工具须立即可见（提交点契约）"


@pytest.mark.asyncio
async def test_arefresh_tools_is_always_force(manager: MCPServerManager) -> None:
    """公开入口 ``arefresh_tools()`` **恒全量**：缓存全热时仍对每个活跃 bundle 各真读一次。

    #127/#197 全链条（通知路径 + ``client:get_tools`` 服务前刷新）依赖它——它必须真读上游。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    before = _counts(manager, ids)

    assert await manager.arefresh_tools() is False, "投影未变须报 False"
    assert _counts(manager, ids) == {name: before[name] + 1 for name in ids}, "强制刷新须每 bundle 重读一次"


@pytest.mark.asyncio
async def test_generation_change_invalidates_cache(manager: MCPServerManager) -> None:
    """复用判据含**世代**：同一 client 对象被 remove→reinsert（ABA）后缓存必须失效。

    世代与对象身份是同一族证据（#185 的 ABA 检测）；本用例打的是「身份不变、世代前进」半条。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    probe = _probe(manager, ids[0])
    before = probe.calls

    manager._active_client_generations[ids[0]] += 1  # 模拟 ABA：同一对象重新入册
    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))  # 结构变更（不主动重读既有 bundle）

    assert probe.calls == before + 1, "世代已变 ⇒ 缓存必须失效并重读上游"


@pytest.mark.asyncio
async def test_client_replacement_invalidates_cache(manager: MCPServerManager, harness: Harness) -> None:
    """复用判据含**对象身份**：活跃 client 被换成新对象 ⇒ 缓存必须失效。"""
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    old = _probe(manager, ids[0])

    replacement = harness.factory(_cfg(ids[0]))
    manager._active_clients[ids[0]] = cast(MCPClientProtocol, replacement)  # 就地换人（世代不动：打身份半条）
    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))

    assert old.calls == 1, "退役对象不得再被使用"
    assert replacement.calls == 1, "换新对象 ⇒ 缓存必须失效并重读上游"


@pytest.mark.asyncio
async def test_failure_invalidates_cache_and_next_refresh_recovers_routes(manager: MCPServerManager) -> None:
    """失败必须**删缓存**：下一次刷新（哪怕只是别的 bundle 的结构变更）要重读它并恢复路由。"""
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    failing = _probe(manager, ids[0])
    failing.boom = RuntimeError("list_tools boom")

    await manager.arefresh_tools()
    assert _exposed(ids[0]) not in manager._exposed_tools, "失败 bundle 的路由须被摘掉"
    assert ids[0] not in manager._tool_list_cache, "失败必须删缓存（否则永远不再重读）"
    before = failing.calls

    failing.boom = None  # 上游恢复（工具集不变）
    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))  # 别的 bundle 的结构变更

    assert failing.calls == before + 1, "失败过的 bundle 必须在下一次刷新被重读"
    assert _exposed(ids[0]) in manager._exposed_tools, "重读成功后路由须恢复"


@pytest.mark.asyncio
async def test_failure_invalidates_cache_even_if_round_is_cancelled(manager: MCPServerManager, harness: Harness) -> None:
    """失败必须**当场**删缓存，不能只依赖「提交点重绑定」：本轮若被取消（无提交），陈旧观测会残留，

    而后续刷新会命中它 ⇒ 把一个仍在失败的 server 的路由按**旧观测**复活（失败被静默抹平）。

    构造：强制刷新（恒全量，两个 bundle 都会真读）——srv1 抛错（失败）；srv2 的 RPC 钉在窗口内 ⇒
    取消在途轮（本轮不提交）；随后跑一次**结构变更**刷新，断言 srv1 被**重读**（而非命中残留条目）。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    failing, gated = _probe(manager, ids[0]), _probe(manager, ids[1])
    failing.boom = RuntimeError("list_tools boom")

    gate = asyncio.Event()
    gated.gate = gate
    gated.entered.clear()
    refresh = asyncio.create_task(manager.arefresh_tools())
    await asyncio.wait_for(gated.entered.wait(), timeout=5.0)  # srv1 已失败、srv2 的 RPC 在途

    refresh.cancel()
    gate.set()
    with contextlib.suppress(asyncio.CancelledError):
        await refresh

    assert ids[0] not in manager._tool_list_cache, "失败必须当场删缓存（本轮未提交时也不得残留）"

    gated.gate = None
    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))  # 结构变更刷新

    # 命中残留条目时只会有「启动 1 + 强制轮 1」= 2 次；≥3 即证明后续结构变更轮真读了上游
    assert failing.calls >= 3, f"失败后的结构变更轮必须重读上游，实际：{failing.calls}"
    assert _exposed(ids[0]) not in manager._exposed_tools, "失败未恢复前，路由不得按陈旧观测复活"


@pytest.mark.asyncio
async def test_second_refresh_waits_for_inflight_round(manager: MCPServerManager) -> None:
    """刷新互斥（``tool_refresh_lock``）：第二个刷新必须**等在途轮结束**，不得并发发起 RPC。

    无此互斥时，两个刷新会互相把对方的快照判成陈旧、并在各自重试里去重读对方**尚未提交**的 bundle
    ⇒ 批量启动的 RPC 次数从 N 膨胀回 O(N²)（rust 侧同名 ``tool_route_refresh_lock`` 即为此设）。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    probe = _probe(manager, ids[0])
    before = probe.calls  # 启动提交点已真读一次

    gate = asyncio.Event()
    probe.gate = gate
    probe.entered.clear()
    first = asyncio.create_task(manager.arefresh_tools())
    await asyncio.wait_for(probe.entered.wait(), timeout=5.0)

    second = asyncio.create_task(manager.arefresh_tools())
    await asyncio.sleep(0)  # 让第二个刷新跑到它的挂起点（要么等在锁上，要么并发发起 RPC）
    assert probe.calls == before + 1, "在途轮未结束时，第二个刷新不得发起 RPC（互斥失效即 RPC 风暴）"

    probe.gate = None
    gate.set()
    await asyncio.wait_for(first, timeout=5.0)
    await asyncio.wait_for(second, timeout=5.0)

    assert probe.calls == before + 2, "在途轮结束后，第二个刷新须真读一次（恒全量语义）"


@pytest.mark.asyncio
async def test_withdrawn_bundle_is_not_written_back_by_a_retried_round(manager: MCPServerManager) -> None:
    """在途轮**被撤回打断后重试**时，提交不得把已离场 bundle 的观测写回缓存（隔离审查 🔴）。

    ``resolved`` 跨重试累积；若提交处直接 `dict(resolved)`，则在「撤回 → 校验失配 → 快照变小 → 重试 → 提交」
    这条链上会把**已撤回** bundle 的条目复活 ⇒ 否证「撤回/清空是硬边界」。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    victim, gated = _probe(manager, ids[0]), _probe(manager, ids[1])
    victim_before = victim.calls

    gate = asyncio.Event()
    gated.gate = gate
    gated.entered.clear()
    refresh = asyncio.create_task(manager.arefresh_tools())
    await asyncio.wait_for(gated.entered.wait(), timeout=5.0)  # victim 已读完入 resolved、gated 在途

    # 复刻 clear_oauth 零 await 快速段：退役 client + bump 世代 + 确定性撤回
    manager._active_clients.pop(ids[0])
    manager._active_client_generations[ids[0]] = manager._active_client_generations.get(ids[0], 0) + 1
    assert manager._withdraw_bundle_tool_routes(ids[0]) is True

    gated.gate = None
    gate.set()
    await asyncio.wait_for(refresh, timeout=5.0)  # 本轮校验失配 ⇒ 重试 ⇒ 提交

    assert ids[0] not in manager._tool_list_cache, "已离场 bundle 的观测不得被重试轮的提交写回"
    assert ids[1] in manager._tool_list_cache, "仍在册者应留在缓存"
    assert not [k for k in manager._exposed_tools if k.startswith(f"{ids[0]}__")], "撤回的路由不得复活"
    assert victim.calls == victim_before + 1, "本轮只应真读一次（重试轮快照已不含它）"


@pytest.mark.asyncio
async def test_clear_all_during_inflight_round_keeps_cache_empty(manager: MCPServerManager) -> None:
    """``_clear_all`` 打断在途轮后，重试的提交**不得**把观测写回（此时世代表已空、世代守卫失效）。"""
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    gated = _probe(manager, ids[1])

    gate = asyncio.Event()
    gated.gate = gate
    gated.entered.clear()
    refresh = asyncio.create_task(manager.arefresh_tools())
    await asyncio.wait_for(gated.entered.wait(), timeout=5.0)

    manager._clear_all()  # 缓存清空 + 世代归零 + 活跃集清空

    gated.gate = None
    gate.set()
    await asyncio.wait_for(refresh, timeout=5.0)

    assert manager._tool_list_cache == {}, "清空后重试提交不得把观测写回（世代已归零 ⇒ 只剩身份兜底）"
    assert manager._active_clients == {}


@pytest.mark.asyncio
async def test_dead_session_is_re_read_and_routes_withdrawn(manager: MCPServerManager) -> None:
    """会话已死（``state != connected``）但**仍在册**的 client 不得命中缓存：须真读 ⇒ 失败 ⇒ 摘路由。

    旧行为：结构变更轮会对它真发 ``list_tools``，命中 ``base_client`` 的状态门抛 ``ConnectionError`` ⇒
    计入 ``failed`` ⇒ 路由被摘（fail-closed）。缓存若在此处命中，就把这条 fail-closed 静默吞掉。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    dying = _probe(manager, ids[0])
    before = dying.calls

    dying.state = STATES.error  # 会话死亡（keep-alive 失败置 error），client 仍在 _active_clients
    dying.boom = ConnectionError("Not connected to server")  # 复刻 base_client.list_tools 的状态门

    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))  # 结构变更刷新（该路径有提交点 + 收尾两处刷新）

    # 每个刷新点各真读一次（失败即删缓存 ⇒ 每个点都重读，与「失败不得被旧观测抹平」同源）
    assert dying.calls >= before + 1, f"会话不可用 ⇒ 不得命中缓存（须真读并经失败摘路由），实际：{dying.calls - before}"
    assert _exposed(ids[0]) not in manager._exposed_tools, "失败即摘路由：不得因缓存而失效"


@pytest.mark.asyncio
async def test_update_path_restart_reads_new_client_once(manager: MCPServerManager) -> None:
    """更新（restart）路径有**两个**刷新点（新 client 的提交点 + 入口收尾），合计只应真读新 client 一次。"""
    ids = await _start_all(manager, ["cache_srv_1"])

    await manager.aadd_or_aupdate_server(_cfg(ids[0], tool_meta={_default_tool_name(ids[0]): ToolMeta(alias="upd")}))

    fresh = _probe(manager, ids[0])  # restart ⇒ 新 client 对象
    assert fresh.calls == 1, f"restart 后两条刷新点合计只应真读一次，实际：{fresh.calls}"
    assert _exposed(ids[0], "upd") in manager._exposed_tools, "新声明须被投影吸收"


@pytest.mark.asyncio
async def test_clear_all_drops_cache(manager: MCPServerManager) -> None:
    """``_clear_all`` 必须连带清空缓存：它同时把**世代表**清空（世代归零）⇒ 若旧条目留下，

    ``(client, generation)`` 判据在换代后会再次命中，等于把 ABA 守卫的回绕窗口敞开。

    直调私有原语（不经 ``ainitialize``）：后者收尾的结构变更刷新会用「空快照重绑定」把缓存顺手清掉，
    从而**掩蔽**本方法自身那一行——测的必须是「清空是硬边界」这条不变量本身。
    """
    ids = await _start_all(manager, ["cache_srv_1"])
    assert ids[0] in manager._tool_list_cache

    manager._clear_all()  # 与世代表同拍归零

    assert manager._tool_list_cache == {}, "清空必须连带清缓存（世代已归零，旧条目不得留）"
    assert manager._active_clients == {}


@pytest.mark.asyncio
async def test_projection_is_recomputed_from_cached_tools_plus_current_config(manager: MCPServerManager) -> None:
    """缓存**只存上游观测（原始 Tool）**，投影每轮由「缓存工具 × 当前配置」重算 —— 只跳 RPC、不跳语义。

    若缓存改为存**配置派生物**（routes / 指纹），则配置变更会被缓存冻住；本用例正是拒那条捷径：换配置 +
    一次结构变更 ⇒ 该 bundle **零重读**，但 alias / forbidden 仍必须被投影吸收。

    注：活跃 bundle 经 ``aadd_or_aupdate_server`` 更新会走 restart（换 client），故这里直接替换配置对象
    （复刻更新路径写入 ``_servers_config`` 的落地形态），投影的纯函数性不依赖更新走哪条路。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    victim = ids[0]
    probe = _probe(manager, victim)
    before = probe.calls
    assert _exposed(victim) in manager._exposed_tools

    manager._servers_config[victim] = _cfg(victim, tool_meta={_default_tool_name(victim): ToolMeta(alias="renamed")})
    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))  # 结构变更（不主动重读既有 bundle）

    assert probe.calls == before, f"配置变更不得触发上游重读（缓存只跳 RPC）：{before} → {probe.calls}"
    assert _exposed(victim, "renamed") in manager._exposed_tools, "alias 必须被投影吸收"
    assert _exposed(victim) not in manager._exposed_tools

    manager._servers_config[victim] = _cfg(victim, forbidden_tools=[_default_tool_name(victim)])
    await manager.aadd_or_aupdate_server(_cfg("cache_srv_4"))

    assert probe.calls == before, "forbidden 同样不得触发上游重读"
    assert not [k for k in manager._exposed_tools if k.startswith(f"{victim}__")], "forbidden 必须被投影吸收"


@pytest.mark.asyncio
async def test_withdraw_prunes_cache(manager: MCPServerManager) -> None:
    """撤回点（clear 快速段形态）必须**同步剪除缓存条目**。

    不变量：缓存不得残留已撤回 bundle 的工具（撤回是「确定性撤回」，与 ``_tool_projection`` 同步裁剪同姿态）。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    victim = ids[0]

    manager._active_clients.pop(victim)  # 复刻 clear_oauth 零 await 快速段：退役 client
    assert manager._withdraw_bundle_tool_routes(victim) is True

    assert victim not in manager._tool_list_cache, "撤回必须同步剪除缓存（否则陈旧工具可能被复用）"
    assert manager._tool_projection.keys() == manager._exposed_tools.keys(), "两表须同步（投影不得留残影）"

    # 撤回不得复活：后续结构变更 + 强制刷新都不得让该 bundle 的路由回来
    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))
    assert not [k for k in manager._exposed_tools if k.startswith(f"{victim}__")], "撤回路由不得复活"
    assert await manager.arefresh_tools() is False, "撤回后刷新不得抖出虚假 revision"


@pytest.mark.asyncio
async def test_silent_upstream_change_needs_force_refresh(manager: MCPServerManager) -> None:
    """**显式钉住「已接受的语义收窄」**（非漏洞）：上游**静默**改工具（无通知、同 client 同世代）时，
    结构变更（不主动重读既有 bundle）不再顺带发现它；检出点是「该 bundle 自身重读」或「一次强制刷新」。

    Agent/CLI 可见面无回归：所有可见路径都先经 ``arefresh_tools()`` 强制刷新（``aget_available_tools``）。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    probe = _probe(manager, ids[0])
    probe.list_tools = AsyncMock(return_value=[_tool("brand_new_tool")])  # 静默换工具集

    await manager.aadd_or_aupdate_server(_cfg("cache_srv_3"))  # 结构变更：不重读既有 bundle
    assert _exposed(ids[0], "brand_new_tool") not in manager._exposed_tools, "（收窄）结构变更不再顺带发现静默变更"

    assert await manager.arefresh_tools() is True, "强制刷新须发现变更并报 True"
    assert _exposed(ids[0], "brand_new_tool") in manager._exposed_tools, "强制刷新后必须归位"


# ────────────────────── P2：锁窗口契约 / Lock-window contracts ──────────────────────


@pytest.mark.asyncio
async def test_refresh_does_not_hold_state_lock_across_rpc(manager: MCPServerManager) -> None:
    """``list_tools`` RPC 期间**不得**持有状态锁（对齐 rust「快照 → 放锁 → RPC → 校验重试」）。

    旧行为：RPC 在 ``async with self._lock`` 内发出（#208 明示的承重例外）⇒ 全部提交尾巴串行。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    for name in ids:
        probe = _probe(manager, name)
        assert probe.lock_states == [False], f"{name}: RPC 期间状态锁必须已释放，实际：{probe.lock_states}"
        assert probe.calls == 1


@pytest.mark.asyncio
async def test_config_update_landing_in_rpc_window_is_not_clobbered(manager: MCPServerManager) -> None:
    """锁外窗口内落地的配置更新**不得**被陈旧快照覆盖（提交校验须含配置身份）。

    RPC 窗口现在无锁 ⇒ 配置写入可落在窗口内（生产形态：更新路径取到已释放的状态锁并替换配置对象）；
    缺了配置身份校验，本轮会把旧配置算出的投影提交回去，alias 变更被静默吞掉。
    """
    ids = await _start_all(manager, ["cache_srv_1", "cache_srv_2"])
    victim = ids[0]
    probe = _probe(manager, victim)

    gate = asyncio.Event()
    probe.gate = gate
    probe.entered.clear()
    refresh = asyncio.create_task(manager.arefresh_tools())
    await asyncio.wait_for(probe.entered.wait(), timeout=5.0)  # 确认 RPC 已在途（此刻状态锁已释放）

    manager._servers_config[victim] = _cfg(victim, tool_meta={_default_tool_name(victim): ToolMeta(alias="mid_window")})
    probe.gate = None
    gate.set()
    await asyncio.wait_for(refresh, timeout=5.0)

    assert _exposed(victim, "mid_window") in manager._exposed_tools, "窗口内落地的配置更新必须生效"
    assert _exposed(victim) not in manager._exposed_tools
