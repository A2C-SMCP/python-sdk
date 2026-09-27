# -*- coding: utf-8 -*-
# filename: test_review_v050_manager.py
# @Author  : JQQ
# @Software: PyCharm
"""v0.5.0 跨节点审查（MCP 管理层）回归用例：🔴1 / 🔴2 / Y1 / Y2 / Y9。

这批缺陷都出在多个 Issue 叠加后的交叉路径上，单节点用例看不到：

- 🔴1（#197 × #222）：``client:get_tools`` 服务前的强制刷新提交了新投影，却丢掉了「投影已变」的返回值。随后的
  ``tools/list_changed`` 后台刷新比对的是**已提交**投影 ⇒ 得 False ⇒ ``capability_revision`` 永久少推进一次。
- Y1：``available_tools`` 曾对每个 bundle 再发一次 ``list_tools``——路由取自刷新那次读、schema 取自第二次读，
  两次读之间上游一变即分叉；第二次读失败还会拖垮整个 ``get_tools``。
- 🔴2（#208 回归）：公开 stop / remove 不在 ``drain()`` 视野内——client 出册后锁外 ``adisconnect`` 期间
  ``aclose`` 早返（子进程未停）；``_clear_all`` 之后恢复执行的 remove 撞 ``KeyError``。
- Y2：``_commit_active_client`` 在已有另一个活跃 client 时无条件覆盖 ⇒ 先到者的 transport 泄漏。
- Y9：刷新一轮失败时丢掉了本轮工作 / 本轮在途期间到达的通知。

/ Regression cases for the v0.5.0 cross-node review of the MCP manager layer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import ResourceListChangedNotification, ToolListChangedNotification

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.start_gate import McpStartGateClosed
from tests.unit_tests.computer.mcp_clients.test_tool_list_cache import (  # noqa: F401 - fixtures 须以名字导入
    Harness,
    ToolListProbe,
    _cfg,
    _exposed,
    _probe,
    _start_all,
    _tool,
    harness,
    manager,
)


async def _computer_over(tmp_path: Path, manager: MCPServerManager) -> Computer:
    """boot 一个 Computer 后把其 manager 换成夹具 manager（受控假 client，零真实子进程）。"""
    computer = Computer(
        name="test",
        blob_cache_root=tmp_path / "blobspool",
        skill_home=tmp_path / "home",
        auto_connect=False,
        auto_reconnect=False,
    )
    await computer.boot_up()
    computer.mcp_manager = manager
    return computer


async def _settle_tool_refresh(computer: Computer) -> None:
    while True:
        task = getattr(computer, "_tool_refresh_task", None)
        if task is None or task.done():
            return
        await task


async def _settle_resources(computer: Computer) -> None:
    while True:
        task = getattr(computer, "_resource_refresh_task", None)
        if task is None or task.done():
            return
        await task


def _tool_list_changed() -> Any:
    return SimpleNamespace(root=ToolListChangedNotification())


def _res_list_changed() -> Any:
    return SimpleNamespace(root=ResourceListChangedNotification())


# ────────────────────── 🔴1 + Y1：get_tools 服务已提交投影 ──────────────────────


@pytest.mark.asyncio
async def test_get_tools_forced_refresh_advances_revision_exactly_once(
    tmp_path: Path, manager: MCPServerManager
) -> None:
    """🔴1：get_tools 强刷提交了新投影 ⇒ revision 在此推进；紧随其后的 list_changed 不得漏记、也不得重复记。"""
    [bundle_id] = await _start_all(manager, ["alpha"])
    computer = await _computer_over(tmp_path, manager)
    assert computer.capability_revision == 0

    # 上游**静默**加了个工具（尚未发 tools/list_changed）——Agent 恰在此时拉取
    _probe(manager, bundle_id).tools = [_tool("alpha_tool"), _tool("beta")]
    names = {t["name"] for t in await computer.aget_available_tools()}
    assert _exposed("alpha", "beta") in names
    assert computer.capability_revision == 1, "强刷提交了新投影，这次变化必须在此计入能力轴"

    # 迟到的 list_changed：投影相对**已提交**者未变 ⇒ 不再推进（不重复计数）
    await computer._on_manager_change(_tool_list_changed())
    await _settle_tool_refresh(computer)
    assert computer.capability_revision == 1


@pytest.mark.asyncio
async def test_get_tools_issues_single_list_tools_per_bundle(tmp_path: Path, manager: MCPServerManager) -> None:
    """Y1：每次 get_tools 每个 bundle 恰一次 tools/list（强刷那次）；产出与已提交路由表逐项一致。"""
    ids = await _start_all(manager, ["alpha", "beta"])
    computer = await _computer_over(tmp_path, manager)
    before = {bid: _probe(manager, bid).calls for bid in ids}

    tools = await computer.aget_available_tools()

    assert {bid: _probe(manager, bid).calls - before[bid] for bid in ids} == {bid: 1 for bid in ids}
    assert {t["name"] for t in tools} == set(manager._exposed_tools)


@pytest.mark.asyncio
async def test_get_tools_schema_comes_from_committed_observation(tmp_path: Path, manager: MCPServerManager) -> None:
    """Y1：上游在「刷新读」之后再变，Agent 看到的仍是**刚提交的**那次观测（不与投影分叉）。"""
    [bundle_id] = await _start_all(manager, ["alpha"])
    computer = await _computer_over(tmp_path, manager)
    probe = _probe(manager, bundle_id)
    first = [_tool("alpha_tool"), _tool("committed")]
    later = [_tool("alpha_tool"), _tool("drifted")]
    reads = iter([first, later, later])

    async def drifting() -> list[Any]:
        return next(reads)

    probe.list_tools.side_effect = drifting

    names = {t["name"] for t in await computer.aget_available_tools()}

    assert names == {_exposed("alpha"), _exposed("alpha", "committed")}
    assert names == set(manager._exposed_tools)


@pytest.mark.asyncio
async def test_get_tools_survives_one_failing_bundle(tmp_path: Path, manager: MCPServerManager) -> None:
    """Y1：单个 bundle 的 tools/list 失败只让它缺席，不得让整个 get_tools 失败。

    两种时序都要钉住：① 强刷那次就失败（已被刷新对称剔除）；② 强刷成功、**之后**才坏——旧实现的第二次读
    会在此抛出并拖垮整个 get_tools，新实现不再发第二次读，产出即刚提交的观测。
    """
    alpha, beta = await _start_all(manager, ["alpha", "beta"])
    computer = await _computer_over(tmp_path, manager)
    _probe(manager, beta).boom = RuntimeError("upstream down")

    names = {t["name"] for t in await computer.aget_available_tools()}

    assert names == {_exposed("alpha")}

    # ② beta 恢复；alpha 在强刷成功之后才坏
    _probe(manager, beta).boom = None
    alpha_probe = _probe(manager, alpha)
    reads = iter([alpha_probe.tools])

    async def ok_then_down() -> list[Any]:
        try:
            return next(reads)
        except StopIteration:
            raise RuntimeError("upstream down after refresh") from None

    alpha_probe.list_tools.side_effect = ok_then_down

    names = {t["name"] for t in await computer.aget_available_tools()}

    assert names == {_exposed("alpha"), _exposed("beta")}


# ────────────────────── 🔴2：stop / remove 入 drain ──────────────────────


def _gate_disconnect(probe: ToolListProbe) -> tuple[asyncio.Event, asyncio.Event, list[str]]:
    """把探针的 adisconnect 钉在门上：返回 (已进入, 放行, 事件序)。"""
    entered, release, order = asyncio.Event(), asyncio.Event(), []

    async def slow_disconnect() -> None:
        entered.set()
        await release.wait()
        order.append("disconnected")

    probe.adisconnect.side_effect = slow_disconnect
    return entered, release, order


@pytest.mark.asyncio
async def test_aclose_waits_for_inflight_stop(manager: MCPServerManager) -> None:
    """🔴2：client 已出册、adisconnect 在途时 aclose 不得先返回（否则子进程未停就宣告关闭）。"""
    [bundle_id] = await _start_all(manager, ["alpha"])
    entered, release, order = _gate_disconnect(_probe(manager, bundle_id))

    stop = asyncio.create_task(manager.astop_client(bundle_id))
    await entered.wait()
    assert bundle_id not in manager._active_clients  # 已出册：aclose 的快照看不到它

    async def close() -> None:
        await manager.aclose()
        order.append("aclose returned")

    closing = asyncio.create_task(close())
    await asyncio.sleep(0.05)
    assert not closing.done(), "aclose 必须等在途停止事务收敛"

    release.set()
    await asyncio.gather(stop, closing)
    assert order == ["disconnected", "aclose returned"]


@pytest.mark.asyncio
async def test_remove_racing_aclose_does_not_raise_keyerror(manager: MCPServerManager) -> None:
    """🔴2：remove 的 adisconnect 在途时 aclose 并发——remove 恢复后不得撞已清空的配置表。"""
    [bundle_id] = await _start_all(manager, ["alpha"])
    entered, release, _order = _gate_disconnect(_probe(manager, bundle_id))

    remove = asyncio.create_task(manager.aremove_server(bundle_id))
    await entered.wait()
    closing = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0.05)
    release.set()

    results = await asyncio.gather(remove, closing, return_exceptions=True)
    assert results == [None, None]
    assert manager._servers_config == {}


@pytest.mark.asyncio
async def test_public_stop_and_remove_after_close_raise_gate_closed(manager: MCPServerManager) -> None:
    """🔴2：关闸后的公开停止 / 移除明确失败（拆除已覆盖它们），而不是在已清空的状态上静默乱跑。"""
    [bundle_id] = await _start_all(manager, ["alpha"])
    await manager.aclose()

    with pytest.raises(McpStartGateClosed):
        await manager.astop_client(bundle_id)
    with pytest.raises(McpStartGateClosed):
        await manager.aremove_server(bundle_id)
    with pytest.raises(McpStartGateClosed):
        await manager.astop_all()


@pytest.mark.asyncio
async def test_ainitialize_after_close_still_stops_and_restarts(manager: MCPServerManager) -> None:
    """🔴2 边界：aclose / ainitialize 在关闸**之后**仍须能停（走私有 `_astop_all`，不被票据拒绝）。"""
    await _start_all(manager, ["alpha"])
    await manager.ainitialize([_cfg("beta")])
    assert set(manager._active_clients) == {"beta"}


# ────────────────────── Y2：提交不覆盖已有活跃 client ──────────────────────


@pytest.mark.asyncio
async def test_commit_does_not_overwrite_existing_active_client(manager: MCPServerManager, harness: Harness) -> None:
    """Y2：先到者胜；后到者被退役（不泄漏），对调用方即「已启动」——不抛。"""
    [bundle_id] = await _start_all(manager, ["alpha"])
    winner = _probe(manager, bundle_id)
    generation = manager._active_client_generations[bundle_id]
    latecomer = ToolListProbe(_cfg("alpha"), [_tool("alpha_tool")])

    await manager._commit_active_client(bundle_id, latecomer)  # type: ignore[arg-type]

    assert manager._active_clients[bundle_id] is winner
    assert manager._active_client_generations[bundle_id] == generation, "被拒的提交不得推进世代"
    latecomer.adisconnect.assert_awaited_once()
    winner.adisconnect.assert_not_awaited()


# ────────────────────── Y9：失败轮不丢工作 / 不丢通知 ──────────────────────


@pytest.mark.asyncio
async def test_resource_round_failure_restores_its_work(tmp_path: Path, manager: MCPServerManager) -> None:
    """Y9：失败轮的工作还回脏位——无新通知时结束（不自旋），下一次任何通知都会连带重做它。"""
    computer = await _computer_over(tmp_path, manager)
    calls: list[int] = []

    async def boom() -> None:
        calls.append(1)
        raise RuntimeError("round boom")

    computer._on_resource_list_changed_windows = boom  # type: ignore[method-assign]

    await computer._on_manager_change(_res_list_changed())
    await _settle_resources(computer)

    assert calls == [1], "持续失败 + 无新通知时不得自旋"
    assert computer._windows_list_dirty is True, "失败轮的工作须还回脏位"


@pytest.mark.asyncio
async def test_resource_notification_during_failed_round_is_retried(tmp_path: Path, manager: MCPServerManager) -> None:
    """Y9：失败轮在途期间到达的通知不得随失败丢掉——须补跑一轮。"""
    computer = await _computer_over(tmp_path, manager)
    calls: list[int] = []

    async def fail_once_after_new_notification() -> None:
        calls.append(1)
        if len(calls) == 1:
            computer._schedule_resource_refresh(windows_list=True)  # 在途期间到达的新通知
            raise RuntimeError("first round boom")

    computer._on_resource_list_changed_windows = fail_once_after_new_notification  # type: ignore[method-assign]

    await computer._on_manager_change(_res_list_changed())
    await _settle_resources(computer)

    assert calls == [1, 1]
    assert computer._windows_list_dirty is False


@pytest.mark.asyncio
async def test_tool_notification_during_failed_round_is_retried(tmp_path: Path, manager: MCPServerManager) -> None:
    """Y9：工具投影刷新失败轮在途期间到达的通知须补跑（并在变化时推进 revision）。"""
    computer = await _computer_over(tmp_path, manager)
    calls: list[int] = []

    async def fail_once_after_new_notification() -> bool:
        calls.append(1)
        if len(calls) == 1:
            computer._schedule_tool_projection_refresh()  # 在途期间到达的新通知 → 仅标脏
            raise RuntimeError("first round boom")
        return True

    manager.arefresh_tools = fail_once_after_new_notification  # type: ignore[method-assign]

    await computer._on_manager_change(_tool_list_changed())
    await _settle_tool_refresh(computer)

    assert calls == [1, 1]
    assert computer.capability_revision == 1


@pytest.mark.asyncio
async def test_tool_round_failure_without_new_notification_does_not_spin(tmp_path: Path, manager: MCPServerManager) -> None:
    """Y9 阴性对照：失败 + 无新通知 ⇒ 结束本轮，不推进、不重试成死循环。"""
    computer = await _computer_over(tmp_path, manager)
    calls: list[int] = []

    async def always_fail() -> bool:
        calls.append(1)
        raise RuntimeError("boom")

    manager.arefresh_tools = always_fail  # type: ignore[method-assign]

    await computer._on_manager_change(_tool_list_changed())
    await _settle_tool_refresh(computer)

    assert calls == [1]
    assert computer.capability_revision == 0
