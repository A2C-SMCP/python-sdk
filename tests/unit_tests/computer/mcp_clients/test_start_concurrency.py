# -*- coding: utf-8 -*-
# filename: test_start_concurrency.py
# @Author  : JQQ
# @Software: PyCharm
"""#208 Computer 级 MCP 有限并发启动——并发矩阵（镜像 rust-sdk#214 的受控假件）。

**证明结构**（关键，勿退化成计时断言）：受控假 client 的 ``connect()`` **没有显式放行 token
就不可能完成**，每次进入时先在计数器上登记（``entered`` / ``active`` / ``max_active``）再阻塞。
于是 ``max_active`` 是**真实的并发下界**——若门失效（放进第 cap+1 个），``max_active`` 必然超过
上限；反之门有效时它不可能超过。与墙钟无关。

负向断言（「第 cap+1 个还没进来」）用**计数器 + 若干 ``sleep(0)`` tick**：门的快路径无挂起点，
tick 足以让任何**被错误放行**的任务跑到 ``on_enter`` 并抬高计数器。故该断言是充分敏感的，
而不是「睡一会儿看看」。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import StdioServerParameters
from mcp.types import Tool

from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import MCPServerConfig, StdioServerConfig
from a2c_smcp.computer.mcp_clients.oauth_types import OAuthError
from a2c_smcp.computer.mcp_clients.start_gate import McpStartGateClosed

# ────────────────────── 受控假件 / Controlled doubles ──────────────────────


class ConnectControl:
    """**受控慢握手**：每次 connect 阻塞至显式放行，并如实登记并发度。

    镜像 rust ``test_support::ConnectControl``。语义：``entered`` 记 connect 已进入；
    ``active`` / ``max_active`` 记**在途未完成**的 connect 活跃数。放行 = :meth:`release_pass`，
    每个 connect 恰好消费一个一次性 token（不回收复用）。
    """

    def __init__(self) -> None:
        self.entered = 0
        self.active = 0
        self.max_active = 0
        self._tokens: list[asyncio.Event] = []

    async def on_enter(self) -> None:
        self.entered += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        token = asyncio.Event()
        self._tokens.append(token)
        try:
            await token.wait()
        finally:
            self.active -= 1

    def release_pass(self, n: int) -> None:
        """放行**最早 n 个尚未放行**的 connect（确定性推进，不依赖调度顺序猜测）。"""
        released = 0
        for token in self._tokens:
            if not token.is_set():
                token.set()
                released += 1
                if released >= n:
                    return


async def _settle(times: int = 3) -> None:
    """让出事件循环若干次，使任何**已就绪**的任务跑到其下一个挂起点（含受控假件的 on_enter）。"""
    for _ in range(times):
        await asyncio.sleep(0)


async def _wait_entered(control: ConnectControl, n: int, *, timeout: float = 5.0) -> None:
    """等待 ``entered`` 达到 n（有界轮询；超时即失败，不留悬挂）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while control.entered < n:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"等待 connect 进入 {n} 个超时（实际 {control.entered}）")
        await asyncio.sleep(0.005)


async def _drain(task: asyncio.Task[Any], control: ConnectControl, *, timeout: float = 5.0) -> Any:
    """反复放行直至 ``task`` 完成（**上限断言之后**才用）。

    上限断言一旦通过，后续放行不可能污染它：``active`` 的递减发生在 connect 返回处，**严格早于**
    permit 归还，而新进入者必须先拿到 permit ⇒ ``active <= in_flight <= cap`` 恒成立。
    本助手只是把「分几批才放得完」交给循环处理（cap 之外的项要等前面的 permit 归还才进入）。

    / Keep releasing until the task completes; only used after the ceiling assertion has passed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not task.done():
        if loop.time() > deadline:
            task.cancel()
            raise AssertionError("等待批量启动收敛超时（疑似门/锁未推进）")
        control.release_pass(64)
        await asyncio.sleep(0.001)
    return await task


class ControlledMCPClient:
    """aconnect 走受控阻塞的假 client；其余面与 ``MockMCPClient`` 同款。"""

    def __init__(self, control: ConnectControl, config: MCPServerConfig, message_handler: Any = None) -> None:
        self._control = control
        self.name = config.name
        self.state = "connected"
        self.message_handler = message_handler
        self.tools = [Tool(name=f"{config.name}_tool", inputSchema={"type": "object"})]
        self.list_tools = AsyncMock(return_value=self.tools)
        self.adisconnect = AsyncMock()
        self.call_tool = AsyncMock()
        self._challenge_event: asyncio.Event | None = None

    async def aconnect(self) -> None:
        await self._control.on_enter()

    def connect_challenge_event(self) -> asyncio.Event:
        if self._challenge_event is None:
            self._challenge_event = asyncio.Event()
        return self._challenge_event

    def connect_redirect_event(self) -> asyncio.Event:
        return asyncio.Event()

    def take_connect_redirect_stop(self) -> None:
        return None


@dataclass
class Harness:
    control: ConnectControl
    created: list[ControlledMCPClient] = field(default_factory=list)
    #: 这些 name 的 client 在 ``aconnect`` 上直接抛错（失败隔离用例）
    fail_names: set[str] = field(default_factory=set)

    def factory(self, config: MCPServerConfig, message_handler: Any = None, **_kw: Any) -> ControlledMCPClient:
        client = ControlledMCPClient(self.control, config, message_handler)
        if config.name in self.fail_names:

            async def _boom() -> None:
                raise RuntimeError("Simulated connect failure")

            client.aconnect = _boom  # type: ignore[method-assign]
        self.created.append(client)
        return client


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness(ConnectControl())
    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", h.factory)
    return h


def _cfg(name: str, *, disabled: bool = False) -> MCPServerConfig:
    return StdioServerConfig(
        name=name,
        disabled=disabled,
        forbidden_tools=[],
        tool_meta={},
        default_tool_meta=None,
        server_parameters=MagicMock(spec=StdioServerParameters),
    )


def _cfgs(n: int) -> list[MCPServerConfig]:
    return [_cfg(f"s{i}") for i in range(n)]


async def _register(manager: MCPServerManager, configs: list[MCPServerConfig]) -> list[str]:
    """纯登记（auto_connect 关）→ 返回可启动的 bundle_id（顺序同 configs）。

    经 ``ainitialize`` 登记：它会把门换成新一代（继承既有上限），故并发策略须**先**安装。
    """
    await manager.ainitialize(configs)
    return manager.enabled_bundle_ids()


# ────────────────────── T1/T2：上限与串行 / Cap & serial ──────────────────────


@pytest.mark.asyncio
async def test_batch_caps_at_five_of_six_and_preserves_input_order(harness: Harness) -> None:
    """★验收 7：6 个 server、cap=5 → 前 5 个可并发、第 6 个必须等待；结果保**输入序**。"""
    manager = MCPServerManager()
    manager.with_mcp_start_concurrency(5)
    ids = await _register(manager, _cfgs(6))

    batch = asyncio.create_task(manager.astart_clients_batch(ids))
    await _wait_entered(harness.control, 5)
    await _settle()

    assert harness.control.entered == 5, "第 6 个必须等第 5 个释放许可后才可进入"
    assert harness.control.max_active == 5, "并发度必须恰好达上限（门有效性下界）"
    assert manager._start_gate.in_flight == 5
    assert not batch.done()

    outcomes = await _drain(batch, harness.control)

    assert [o.bundle_id for o in outcomes] == ids, "结果必须保输入序（非完成序）"
    assert all(o.error is None for o in outcomes)
    assert manager._start_gate.in_flight == 0
    assert len(manager._active_clients) == 6
    assert harness.control.max_active == 5, "全程不得超过上限"

    # H4 收敛界：6 次提交各触发一次全量刷新 ⇒ list_tools 总次数 ≤ Σ(提交时的活跃数) = 21。
    # 若 _arefresh_tool_mapping 的无界重试被并发提交拖成空转，此处会远超上限。
    total_list_tools = sum(int(c.list_tools.await_count) for c in harness.created)
    assert 6 <= total_list_tools <= 21, f"刷新轮数失控（疑似重试空转）：{total_list_tools}"


@pytest.mark.asyncio
async def test_unconfigured_batch_stays_serial(harness: Harness) -> None:
    """★验收 1：**不配置**上限 → 保持既有逐项串行（一次只有一个 connect 在途）。"""
    manager = MCPServerManager()
    assert manager._start_gate.max is None
    ids = await _register(manager, _cfgs(6))

    batch = asyncio.create_task(manager.astart_clients_batch(ids))
    for i in range(6):
        await _wait_entered(harness.control, i + 1)
        await _settle()
        assert harness.control.entered == i + 1, "未配置上限时必须逐项串行（第 i+2 个不得提前进入）"
        assert manager._start_gate.in_flight == 1
        harness.control.release_pass(1)

    outcomes = await asyncio.wait_for(batch, timeout=5)
    assert all(o.error is None for o in outcomes)
    assert harness.control.max_active == 1


# ────────────────────── T3/T4：重叠批次与幂等 / Overlap & idempotency ──────────────────────


@pytest.mark.asyncio
async def test_overlapping_batches_share_one_ceiling(harness: Harness) -> None:
    """★验收 4：两批**重叠**共享同一上限——若各批各设一整套限流，第 4 个会被放进来。"""
    manager = MCPServerManager()
    manager.with_mcp_start_concurrency(3)
    ids = await _register(manager, _cfgs(5))

    first = asyncio.create_task(manager.astart_clients_batch(ids))
    second = asyncio.create_task(manager.astart_clients_batch(ids))
    await _wait_entered(harness.control, 3)
    await _settle()

    assert harness.control.entered == 3, "重叠批次的在途总量不得超过上限"
    assert manager._start_gate.in_flight == 3

    both = asyncio.gather(first, second)
    first_outcomes, second_outcomes = await _drain(both, harness.control)

    assert [o.bundle_id for o in first_outcomes] == ids
    assert [o.bundle_id for o in second_outcomes] == ids
    assert all(o.error is None for o in first_outcomes + second_outcomes)
    assert harness.control.max_active <= 3
    # 同 bundle_id 双开由 per-bundle 生命周期锁结构性排除：每个 bundle 只连一次
    assert len(harness.created) == 5, f"同 bundle 不得双开：期望 5 个 client，实际 {len(harness.created)}"
    assert harness.control.entered == 5


@pytest.mark.asyncio
async def test_idempotent_start_does_not_re_materialize(harness: Harness) -> None:
    """★验收 4：重复启动幂等——**不重解析 Input**（幂等检查早于 materialize，协议 §5.13）。"""
    renders: list[str] = []

    async def _materializer(bundle_id: str, raw: MCPServerConfig) -> MCPServerConfig:
        renders.append(bundle_id)
        return raw

    manager = MCPServerManager(materializer=_materializer)
    manager.with_mcp_start_concurrency(2)
    ids = await _register(manager, _cfgs(2))

    first = await _drain(asyncio.create_task(manager.astart_clients_batch(ids)), harness.control)
    assert all(o.error is None for o in first)
    assert sorted(renders) == sorted(ids), "首次启动每个 bundle 恰好 materialize 一次"

    # 重复启动：批内重复 id + 再跑一批（幂等路径不连、不解析 → 无需放行）
    renders.clear()
    dup = await manager.astart_clients_batch([ids[0], ids[0]])
    again = await manager.astart_clients_batch(ids)
    assert all(o.error is None for o in dup + again)
    assert renders == [], "幂等启动不得重解析 Input（materializer 不应被调用）"
    assert len(harness.created) == 2


# ────────────────────── T6：shutdown / Close ──────────────────────


@pytest.mark.asyncio
async def test_aclose_rejects_queued_and_waits_for_inflight(harness: Harness) -> None:
    """★验收 6：在途收敛 + 排队者**即刻**失败 + 无进程泄漏（镜像 rust shutdown 用例）。"""
    manager = MCPServerManager()
    manager.with_mcp_start_concurrency(2)
    ids = await _register(manager, _cfgs(6))

    batch = asyncio.create_task(manager.astart_clients_batch(ids))
    await _wait_entered(harness.control, 2)
    await _settle()
    assert manager._start_gate.in_flight == 2

    closing = asyncio.create_task(manager.aclose())
    await _settle()
    assert not closing.done(), "在途未收敛时 aclose 不得返回"

    # 关闸后新启动**即刻**失败（不等许可释放）
    with pytest.raises(McpStartGateClosed):
        await manager.astart_client(ids[2])

    harness.control.release_pass(2)
    await asyncio.wait_for(closing, timeout=5)
    outcomes = await asyncio.wait_for(batch, timeout=5)

    assert [o.bundle_id for o in outcomes] == ids
    assert all(o.error is None for o in outcomes[:2]), "在途两项应正常完成"
    assert all(isinstance(o.error, McpStartGateClosed) for o in outcomes[2:]), "排队四项应门关闭失败"
    assert manager._start_gate.in_flight == 0
    # 泄漏检查：只应为那 2 个真正连上的 client 建过实例，且都被断开
    assert len(harness.created) == 2
    for client in harness.created:
        client.adisconnect.assert_awaited()
    assert manager._active_clients == {}


@pytest.mark.asyncio
async def test_aclose_waits_for_start_transaction_that_has_not_committed_any_state(harness: Harness) -> None:
    """``aclose`` 的 ``drain()`` 是**独立**承重的，不是靠 per-bundle 锁「顺带」等到。

    构造：先把该 bundle 的生命周期锁占住，再发起启动事务——它在 ``gate.acquire()`` 之后、
    **phase A 之前**停住（既无 activation intent、也无活跃 client，``_astop_all`` 的快照看不见它）。
    此时 ``aclose`` 若不等在途收敛，就会先 ``_clear_all`` 抹掉配置，等锁释放后该事务以
    「未知 bundle」失败——「在途事务被拆台」。``drain()`` 正是排除该窗口的那一件。

    此处直接取私有锁是为**确定性复现**该窗口（其余用例经真实 client 阻塞点进入）。
    """
    manager = MCPServerManager()
    ids = await _register(manager, _cfgs(1))
    bid = ids[0]

    async with manager._bundle_lock(bid):  # 占住事务内层锁 → 启动事务停在 phase A 之前
        starting = asyncio.create_task(manager.astart_client(bid))
        await _settle()
        assert manager._activation_intents == set(), "前提：事务尚未进入 phase A（快照看不见它）"
        assert not starting.done()

        closing = asyncio.create_task(manager.aclose())
        await _settle()
        assert not closing.done(), "在途事务未收敛时 aclose 不得返回"

    # 释放 bundle 锁后事务继续推进到真实 connect（受控件）——放行它完成，再等 aclose 收敛
    await _drain(asyncio.gather(starting, closing), harness.control)

    assert manager._active_clients == {}, "收敛后仍须被停止干净"
    assert manager._servers_config == {}


@pytest.mark.asyncio
async def test_empty_batch_returns_empty_under_both_configurations(harness: Harness) -> None:
    """隔离审查 🔴1：**空集**在「已配置上限」下不得炸——`asyncio.wait([])` 会抛 `ValueError`。

    ``astart_all()`` 在「未挂载任何 server / 全部 disabled」时正会走到这里（#208 前是零次循环的
    no-op），故这是必须挡住的回归；rust ``join_all(&[])`` 亦返回空 Vec（双端口径一致）。
    """
    configured = MCPServerManager()
    configured.with_mcp_start_concurrency(3)
    assert await configured.astart_clients_batch([]) == []
    await configured.astart_all()  # 未挂载任何 server ⇒ 同走空集路径，不得抛

    unconfigured = MCPServerManager()
    assert await unconfigured.astart_clients_batch([]) == []
    await unconfigured.astart_all()


@pytest.mark.asyncio
async def test_update_during_start_does_not_get_overwritten_by_stale_render(harness: Harness) -> None:
    """隔离审查 🔴2：启动事务在途时的配置更新**不得被旧 raw 的渲染结果覆盖**。

    竞态（#208 锁域收窄引入）：决策相只取状态锁、不取 bundle 锁 ⇒ 它的登记可落在本事务
    materialize 期间。若无护栏，phase B 会把**旧 raw** 的渲染写回 ``_servers_config`` ——
    更新被静默丢弃 + raw/rendered 分歧（活跃进程仍是旧配置）。

    本用例用受控 materializer 在渲染窗口内制造一次真实更新；终态要求：**新配置生效**（不是旧值）。
    """
    gate = asyncio.Event()
    seen: list[str] = []

    async def _materializer(bundle_id: str, raw: MCPServerConfig) -> MCPServerConfig:
        seen.append(bundle_id)
        if len(seen) == 1:
            await gate.wait()  # 第一次渲染时挂住，让更新落在窗口内
        return raw

    manager = MCPServerManager(materializer=_materializer)
    ids = await _register(manager, [_cfg("s0")])
    bid = ids[0]

    original_raw = manager._servers_config_raw[bid]
    starting = asyncio.create_task(manager.astart_client(bid))
    for _ in range(5):
        await _settle()
        if seen:
            break
    assert seen, "第一次渲染应已开始（受控 materializer 已挂住）"

    # 渲染窗口内更新同 bundle 的声明。**必须作为独立任务**：该更新的启动会排队等本事务持有的
    # bundle 锁，直接 await 即自阻塞（而本事务正等我们放行）。
    updating = asyncio.create_task(manager.aadd_or_aupdate_server(_cfg("s0")))
    for _ in range(10):
        await _settle()
        if manager._servers_config_raw.get(bid) is not original_raw:
            break
    assert manager._servers_config_raw.get(bid) is not original_raw, "更新声明应已登记进 raw 店"

    gate.set()
    await _drain(starting, harness.control)
    await asyncio.wait_for(updating, timeout=5)

    # 终态：渲染被**重跑**（seen 至少两次）且两店一致——不得留下「raw 新 / rendered 旧」
    assert len(seen) >= 2, "声明在渲染窗口内被换过 ⇒ 必须重读重渲染，而非写回旧值"
    assert bid in manager._servers_config
    assert bid in manager._active_clients


@pytest.mark.asyncio
async def test_ainitialize_settles_detached_oauth_tasks_like_aclose(harness: Harness) -> None:
    """隔离审查 🟡4：``ainitialize`` 换代的收敛前导须与 ``aclose`` **对称**——detached OAuth connect
    任务要在返回前被**取消并结算**，而非只靠 ``_clear_all`` 的裸 ``cancel()`` 兜底。

    为什么必须结算：这些任务不占门计数（``drain()`` 收敛不到），一个停在 ``_commit_active_client``
    状态锁上的任务会被 ``_astop_all`` 逐项释放锁的间隙唤醒并**在其间提交**（那一刻 ``_servers_config``
    未清、epoch 未变、移除守卫也通过）⇒ 不在 ``_astop_all`` 快照里 ⇒ 漏停；而 ``_clear_all`` 只丢引用不
    disconnect ⇒ 进程泄漏。

    此处直接钉**结构契约**（取消 + 结算发生在返回之前），用永不自然结束的假任务做到确定性判定。
    """
    manager = MCPServerManager()
    ids = await _register(manager, _cfgs(1))
    bid = ids[0]

    settled: list[bool] = []

    async def _detached() -> None:
        try:
            await asyncio.sleep(30)  # 永不自然结束；只有被取消才会结算
        finally:
            settled.append(True)

    task = asyncio.create_task(_detached())
    manager._oauth_connect_tasks[bid] = task
    await _settle(1)  # 让任务真正跑起来（未启动即被取消 ⇒ 协程体不执行，finally 不触发）
    assert not task.done()

    await manager.ainitialize([_cfg("other")])

    assert task.done(), "detached oauth 任务必须在 ainitialize 返回前被取消并结算（不得留到 _clear_all 兜底）"
    assert settled == [True], "任务须已收尾"
    assert manager._oauth_connect_tasks == {}


@pytest.mark.asyncio
async def test_start_false_on_running_server_fails_fast(harness: Harness) -> None:
    """隔离审查 🟡5：``start=False`` 落在「已激活」分支须 fail-fast，而非静默丢弃配置。

    该分支刻意「不预写新声明」（预写只在 restart 成功后发生），故 ``start=False`` 落到这里等于
    既没登记、也没启动、也不报错——与「只登记不启动」的 docstring 直接矛盾。
    """
    manager = MCPServerManager()
    ids = await _register(manager, _cfgs(1))
    bid = ids[0]

    await _drain(asyncio.create_task(manager.astart_clients_batch(ids)), harness.control)
    assert bid in manager._active_clients

    with pytest.raises(RuntimeError, match="start=False"):
        await manager.aadd_or_aupdate_server(_cfg("s0"), start=False)


@pytest.mark.asyncio
async def test_aclose_cancelled_during_drain_still_tears_down(harness: Harness) -> None:
    """隔离审查 🟡6：``drain()`` 窗口内到达的取消**不得**跳过整个拆除。

    ``drain()`` 是 teardown 的**前置** await 且不是吞点 —— 若不接住，取消会从这里直接上抛，
    ``_astop_all`` / ``_clear_all`` 全被跳过 ⇒ **一个 client 都不会被停**（比「拆到一半」更彻底地失败）。
    正确姿态与 #211 同源：吞掉该取消、把拆除走完，信号由最外层 ``@restores_cancellation`` 还原。
    """
    gate = asyncio.Event()
    entered: list[str] = []

    async def _materializer(bundle_id: str, raw: MCPServerConfig) -> MCPServerConfig:
        entered.append(bundle_id)
        await gate.wait()  # 停在渲染窗口 = 事务**持着 bundle 锁** ⇒ drain 必然等待
        return raw

    manager = MCPServerManager(materializer=_materializer)
    ids = await _register(manager, [_cfg("s0")])
    bid = ids[0]

    starting = asyncio.create_task(manager.astart_client(bid))
    for _ in range(5):
        await _settle()
        if entered:
            break
    assert entered, "启动事务应已停在渲染窗口"

    closing = asyncio.create_task(manager.aclose())
    await _settle()
    assert not closing.done(), "在途未收敛时 aclose 不得返回"

    closing.cancel()  # 取消落在 drain 窗口
    await _settle()

    gate.set()
    # 放行在途事务（closing 的收尾在等它释放 bundle 锁）
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _drain(starting, harness.control)

    # 两面都要钉：① 拆除走完（吞掉取消之后**继续**执行 stop/clear）；
    # ② 取消信号**未被吞没**——由最外层 @restores_cancellation 在收尾处还原（`Task.cancelling()`
    #    不因吞掉而自减）。只 suppress 全吞会让「静默成功」与「正确还原」无从区分。
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert manager._servers_config == {}, "取消不得跳过 _clear_all"
    assert manager._active_clients == {}, "取消不得跳过 astop_all（否则 client 全部存活）"
    assert harness.created, "在途事务应已建成 client"
    assert harness.created[0].adisconnect.await_count >= 1, "已连上的 client 必须被停（拆除走完）"


# ────────────────────── T7/T8：并发启动 / 同 bundle 双开与启动中移除 ──────────────────────


@pytest.mark.asyncio
async def test_concurrent_same_bundle_starts_spawn_one_process(harness: Harness) -> None:
    """H1：同一 bundle_id 的 N 次并发启动**只连一次**（per-bundle 生命周期锁，no-double-open）。"""
    manager = MCPServerManager()
    ids = await _register(manager, _cfgs(1))
    bid = ids[0]

    starts = [asyncio.create_task(manager.astart_client(bid)) for _ in range(8)]
    await _wait_entered(harness.control, 1)
    await _settle()
    assert harness.control.entered == 1, "同 bundle 并发启动不得出现第二个 connect"

    harness.control.release_pass(10)
    await asyncio.wait_for(asyncio.gather(*starts), timeout=5)

    assert len(harness.created) == 1, "同 bundle 不得双开（两个进程）"
    assert harness.control.entered == 1
    assert list(manager._active_clients) == [bid]


@pytest.mark.asyncio
async def test_remove_during_start_does_not_resurrect(harness: Harness) -> None:
    """H2：启动事务在途时移除该 bundle → 不得**复活**，且已建连接被断开（无泄漏）。"""
    manager = MCPServerManager()
    ids = await _register(manager, _cfgs(1))
    bid = ids[0]

    starting = asyncio.create_task(manager.astart_client(bid))
    await _wait_entered(harness.control, 1)

    removing = asyncio.create_task(manager.aremove_server(bid))
    await _settle()
    assert not removing.done(), "移除必须等该 bundle 的启动事务收尾（同一生命周期锁）"

    harness.control.release_pass(1)
    await asyncio.wait_for(starting, timeout=5)
    await asyncio.wait_for(removing, timeout=5)

    assert bid not in manager._active_clients, "移除后不得复活"
    assert bid not in manager._servers_config
    assert len(harness.created) == 1
    harness.created[0].adisconnect.assert_awaited()


@pytest.mark.asyncio
async def test_remove_during_start_via_stop_also_does_not_resurrect(harness: Harness) -> None:
    """H2 变体：在途启动 + ``astop_client`` → 停止同样等事务收尾，之后不得再有活跃 client。"""
    manager = MCPServerManager()
    ids = await _register(manager, _cfgs(1))
    bid = ids[0]

    starting = asyncio.create_task(manager.astart_client(bid))
    await _wait_entered(harness.control, 1)
    stopping = asyncio.create_task(manager.astop_client(bid))
    await _settle()
    assert not stopping.done()

    harness.control.release_pass(1)
    await asyncio.wait_for(starting, timeout=5)
    await asyncio.wait_for(stopping, timeout=5)

    assert bid not in manager._active_clients
    assert bid in manager._servers_config, "stop 不删声明（区别于 remove）"
    assert bid not in manager._activation_intents, "显式 stop 清除 activation intent"
    harness.created[0].adisconnect.assert_awaited()


# ────────────────────── 提交点守卫 / Commit guard ──────────────────────


@pytest.mark.asyncio
async def test_commit_rejects_client_for_removed_bundle(harness: Harness) -> None:
    """既有缺陷根治：detached OAuth 任务可为**已移除** bundle 提交 client ⇒ 必须 fail-closed。

    ``_retire_oauth_bundle`` 只 cancel 不 bump epoch，且交互式 flow 刻意**不走** per-bundle 锁
    （可阻塞分钟级），故该路径只能靠 ``_commit_active_client`` 的状态守卫兜住。
    """
    manager = MCPServerManager()
    ids = await _register(manager, _cfgs(1))
    bid = ids[0]
    await manager.aremove_server(bid)

    late_client = ControlledMCPClient(harness.control, _cfg(bid), None)
    with pytest.raises(OAuthError):
        await manager._commit_active_client(bid, late_client, clear_epoch=None)

    assert bid not in manager._active_clients, "已移除的 bundle 不得被复活"
    late_client.adisconnect.assert_awaited()  # 被拒的 client 必须 best-effort 退役（否则连接泄漏）


@pytest.mark.asyncio
async def test_batch_failure_is_value_and_does_not_block_others(harness: Harness) -> None:
    """★验收 3：批量逐项结果——单项失败**不阻塞**其他项，且失败是**值**而非中断。"""
    manager = MCPServerManager()
    manager.with_mcp_start_concurrency(3)
    ids = await _register(manager, _cfgs(3))
    # 中位项连接失败 —— 若驱动用「首错即停」，s2 绝不会被尝试
    harness.fail_names.add("s1")

    outcomes = await _drain(asyncio.create_task(manager.astart_clients_batch(ids)), harness.control)

    assert [o.bundle_id for o in outcomes] == ids, "结果保输入序"
    assert outcomes[0].error is None
    assert isinstance(outcomes[1].error, RuntimeError)
    assert outcomes[2].error is None, "单项失败不得阻塞其他项"
    assert set(manager._active_clients) == {"s0", "s2"}
    assert "s1" not in manager._active_clients
