# -*- coding: utf-8 -*-
# filename: test_resource_refresh_off_receive_loop.py
# @Author  : JQQ
# @Software: PyCharm
"""
单元测试（#210）：资源变更刷新**不得**在 MCP 接收循环内联执行。

Unit tests for #210: resource-change refreshes must not run inline inside the MCP receive loop.

背景 / Background: ``Computer._on_manager_change`` 是 MCP ``ClientSession`` 的 ``message_handler``，由接收
循环**内联** await（``mcp/shared/session.py`` ``_receive_loop`` → ``await self._message_handler(req)``，无
``create_task``）。其中 await 任何对**同一会话**的请求 = 自阻塞：``list_windows`` / ``list_skill_resources``
的响应只能由正阻塞的接收循环读取 → 该 server 永久失能（后续通知收不到、工具调用一并挂起）。

``_on_manager_change`` is an MCP receive-loop inline callback; awaiting a same-session request there
deadlocks (#127 同源约束；#210 复现 window / skill 两条路径)。本修复把三条含真实 RPC 的刷新路径
（① window 集合、② skill 集合、③ skill 内容）统一调度到后台任务，并按**动作**分设脏位 —— ② 会先比对
``resources/list`` 的 URI 集合，而 ③（``resources/updated``）**不比对**；两者共用一位会让「集合不变、内容
变了」的内容级更新被比对挡掉（静默丢更新）。

The three RPC-bearing refresh paths are scheduled onto a background task with per-**action** dirty flags:
② compares the resource-set first while ③ must not, so they cannot share one flag.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import (
    ResourceListChangedNotification,
    ResourceUpdatedNotification,
    ResourceUpdatedNotificationParams,
)

from a2c_smcp.computer.computer import Computer


class _RecordingClient:
    """记录三类 emit 次数的伪 Socket.IO 客户端 / Fake Socket.IO client counting the three emits."""

    def __init__(self) -> None:
        self.desktop_refreshes = 0
        self.skills_emits = 0
        self.tool_emits = 0

    async def emit_refresh_desktop(self) -> None:
        self.desktop_refreshes += 1

    async def emit_update_skills(self) -> None:
        self.skills_emits += 1

    async def emit_update_tool_list(self) -> None:
        self.tool_emits += 1


def _res_list_changed() -> Any:
    """MCP ``notifications/resources/list_changed`` 替身 / A ResourceListChangedNotification stand-in."""
    return SimpleNamespace(root=ResourceListChangedNotification())


def _res_updated(uri: str) -> Any:
    """MCP ``notifications/resources/updated`` 替身 / A ResourceUpdatedNotification stand-in."""
    return SimpleNamespace(root=ResourceUpdatedNotification(params=ResourceUpdatedNotificationParams(uri=uri)))


async def _booted_computer(tmp_path: Path) -> Computer:
    """boot 后返回 Computer（``mcp_manager`` 就绪但无活跃 client —— auto_connect=False）。

    ``skill_home`` / ``blob_cache_root`` 一律落 ``tmp_path``：否则 ``ensure_skill_home()`` 会解析到开发者
    真实的 ``~/.a2c/skills`` 并在那里建目录、写 intent 账本（既有 #197 测试的卫生欠债，本文件不再复制）。
    """
    computer = Computer(
        name="test",
        blob_cache_root=tmp_path / "blobspool",
        skill_home=tmp_path / "home",
        auto_connect=False,
        auto_reconnect=False,
    )
    await computer.boot_up()
    assert computer.mcp_manager is not None
    return computer


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    """轮询等待 predicate 为真（避免对后台任务调度做固定 sleep 假设）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待条件超时 / timed out waiting for condition")
        await asyncio.sleep(0.005)


async def _settle_resources(computer: Computer) -> None:
    """等待资源刷新后台任务结算（含结算后又起的新一轮；未调度则直接返回）。"""
    while True:
        task = getattr(computer, "_resource_refresh_task", None)
        if task is None or task.done():
            return
        await task


async def _deliver(computer: Computer, message: Any, timeout: float = 1.0) -> None:
    """投递一条变更通知，并**要求 handler 在有界时间内返回**（#210 的红灯判据）。

    内联刷新（缺陷形态）会让 handler 停在刷新上 —— 本助手把它变成一次可读的超时失败，而不是挂死整个
    测试会话。修复后 handler 零 await，此超时永不触发。
    """
    await asyncio.wait_for(computer._on_manager_change(message), timeout=timeout)


class _ActionRecorder:
    """记录后台任务实际跑了哪些**动作**：① windows 集合 / ② skills 集合 / ③ skills 内容。

    形参收 ``*args``：修复前这三个方法由接收循环**内联**调用（``windows`` 还带 ``client`` 实参），
    收窄签名会让红灯期因 ``TypeError`` 而非因断言失败 —— 那是错误的原因红。
    """

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.gate: asyncio.Event | None = None
        self._gated_once = False

    async def windows(self, *args: Any) -> None:
        self.actions.append("windows")
        if self.gate is not None and not self._gated_once:
            self._gated_once = True
            await self.gate.wait()

    async def skills_list(self, *args: Any) -> None:
        self.actions.append("skills_list")

    async def skills_content(self, *args: Any) -> None:
        self.actions.append("skills_content")

    def install(self, computer: Computer) -> None:
        computer._on_resource_list_changed_windows = self.windows  # type: ignore[method-assign]
        computer._on_resource_list_changed_skills = self.skills_list  # type: ignore[method-assign]
        computer._on_skill_resource_updated = self.skills_content  # type: ignore[method-assign]


class TestRefreshLeavesReceiveLoop:
    """#210：刷新必须离开接收循环，且三条动作按各自的语义执行。"""

    @pytest.mark.asyncio
    async def test_resource_list_changed_does_not_collect_inline(self, tmp_path: Path) -> None:
        """核心红灯：``_on_manager_change`` 返回时刷新**尚未发生**（内联即自阻塞接收循环）。

        **必须绑定 Socket.IO 客户端**：缺陷形态下该分支在 ``client is None`` 时提前 return，不绑客户端
        就根本走不到 RPC —— ``actions == []`` 会**空过**（断言与「内联自阻塞」的主张不同源）。绑上后，
        缺陷形态会把 ``windows`` 内联跑掉，首条断言立刻报红。
        """
        computer = await _booted_computer(tmp_path)
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]
        rec = _ActionRecorder()
        rec.install(computer)

        await computer._on_manager_change(_res_list_changed())

        assert rec.actions == [], "handler 不得内联采集资源（否则接收循环自阻塞，#210）"

        await _settle_resources(computer)

        assert rec.actions == ["windows", "skills_list"], "后台任务须真的把两条路径都跑掉（防「整段删掉」）"

    @pytest.mark.asyncio
    async def test_resource_updated_skill_uri_uses_content_action(self, tmp_path: Path) -> None:
        """``resources/updated(skill://)`` 走**内容**动作（不比对），而非集合动作。"""
        computer = await _booted_computer(tmp_path)
        rec = _ActionRecorder()
        rec.install(computer)

        await computer._on_manager_change(_res_updated("skill://srv/demo"))

        assert rec.actions == [], "内容级更新同样不得内联（其下游重物化含真实 RPC）"

        await _settle_resources(computer)

        assert rec.actions == ["skills_content"], "③ 与 ② 语义不同，不得合并（集合比对会挡掉内容级更新）"

    @pytest.mark.asyncio
    async def test_resource_updated_skill_content_not_blocked_by_set_compare(self, tmp_path: Path) -> None:
        """③ 的真身断言：URI 集合**未变**时内容级更新仍须重物化（这正是 resource_updated 的意义）。"""
        computer = await _booted_computer(tmp_path)
        restaged: list[int] = []

        async def same_refs() -> set[str]:
            return {"skill://srv/demo"}

        async def fake_restage(server_name: str | None = None) -> list[str]:
            restaged.append(1)
            return ["mcp:srv:demo"]

        computer._acollect_skill_refs = same_refs  # type: ignore[method-assign]
        computer._restage_mcp_skills = fake_restage  # type: ignore[method-assign]
        computer._skills_cache = {"skill://srv/demo"}  # 与采集一致 → 集合路径会跳过

        await computer._on_manager_change(_res_updated("skill://srv/demo"))
        await _settle_resources(computer)

        assert restaged == [1], "集合未变不等于内容未变：内容级更新必须无条件重物化"

    @pytest.mark.asyncio
    async def test_resource_updated_window_uri_still_emits_inline(self, tmp_path: Path) -> None:
        """窗口内容更新只做 Socket.IO emit（无 RPC）→ 保持内联，且不调度刷新任务。"""
        computer = await _booted_computer(tmp_path)
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]

        await computer._on_manager_change(_res_updated("window://host/main"))

        assert client.desktop_refreshes == 1, "window:// 内容更新无本地状态可更，保持内联 emit"
        assert computer._resource_refresh_task is None, "无 RPC 的路径不得被拖进后台任务"


class TestLocalStateFirst:
    """#210：资源刷新改为「本地状态先行」——未加入 office 也推进本地状态，emit 由 client 守卫兜住。"""

    @pytest.mark.asyncio
    async def test_list_changed_advances_local_state_without_socketio_client(self, tmp_path: Path) -> None:
        """无 Socket.IO 客户端仍更新窗口缓存 / 重物化 SKILL（对齐 #197 tools 路径的既定姿态）。"""
        computer = await _booted_computer(tmp_path)
        restaged: list[int] = []

        async def fake_windows() -> set[str]:
            return {"window://host/main"}

        async def fake_refs() -> set[str]:
            return {"skill://srv/demo"}

        async def fake_restage(server_name: str | None = None) -> list[str]:
            restaged.append(1)
            return ["mcp:srv:demo"]

        computer._acollect_window_uris = fake_windows  # type: ignore[method-assign]
        computer._acollect_skill_refs = fake_refs  # type: ignore[method-assign]
        computer._restage_mcp_skills = fake_restage  # type: ignore[method-assign]
        assert computer.socketio_client is None

        await computer._on_manager_change(_res_list_changed())
        await _settle_resources(computer)

        assert computer._windows_cache == {"window://host/main"}, "未入房间不代表本地窗口缓存应停滞"
        assert computer._skills_cache == {"skill://srv/demo"}, "未入房间仍须物化 SKILL（否则入房后 get_skills 陈旧）"
        assert restaged == [1]
        await computer._skill_debouncer.aflush()  # 结算挂起的 SKILL 去抖窗口，避免遗留任务

    @pytest.mark.asyncio
    async def test_updated_skill_advances_registry_without_socketio_client(self, tmp_path: Path) -> None:
        """``resources/updated(skill://)`` 在无 client 时同样重物化。"""
        computer = await _booted_computer(tmp_path)
        restaged: list[int] = []

        async def fake_restage(server_name: str | None = None) -> list[str]:
            restaged.append(1)
            return []

        computer._restage_mcp_skills = fake_restage  # type: ignore[method-assign]
        assert computer.socketio_client is None

        await computer._on_manager_change(_res_updated("skill://srv/demo"))
        await _settle_resources(computer)

        assert restaged == [1]
        await computer._skill_debouncer.aflush()


class TestRefreshSemanticsPreserved:
    """效果面回归：缓存比对、emit 条件与异常隔离不得因「搬家」而漂移。"""

    @pytest.mark.asyncio
    async def test_unchanged_window_set_does_not_emit(self, tmp_path: Path) -> None:
        """阴性对照：集合未变 → 不 emit（杀「无条件 emit」）。"""
        computer = await _booted_computer(tmp_path)
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]

        async def same_windows() -> set[str]:
            return {"window://host/main"}

        async def empty_refs() -> set[str]:
            return set()

        computer._acollect_window_uris = same_windows  # type: ignore[method-assign]
        computer._acollect_skill_refs = empty_refs  # type: ignore[method-assign]
        computer._windows_cache = {"window://host/main"}

        await computer._on_manager_change(_res_list_changed())
        await _settle_resources(computer)

        assert client.desktop_refreshes == 0, "集合未变不得触发桌面刷新"

    @pytest.mark.asyncio
    async def test_changed_window_set_emits_and_commits_cache(self, tmp_path: Path) -> None:
        """正对照：集合变化 → emit 一次且缓存提交（与阴性对照配对，缺一则弱断言）。"""
        computer = await _booted_computer(tmp_path)
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]

        async def changed_windows() -> set[str]:
            return {"window://host/main", "window://host/side"}

        async def empty_refs() -> set[str]:
            return set()

        computer._acollect_window_uris = changed_windows  # type: ignore[method-assign]
        computer._acollect_skill_refs = empty_refs  # type: ignore[method-assign]

        await computer._on_manager_change(_res_list_changed())
        await _settle_resources(computer)

        assert client.desktop_refreshes == 1
        assert computer._windows_cache == {"window://host/main", "window://host/side"}

    @pytest.mark.asyncio
    async def test_round_abort_is_contained_and_later_notification_still_runs(self, tmp_path: Path) -> None:
        """轮中止分支（``except Exception``）：记 ERROR 后**结束整轮**、不冒泡，且后续通知仍能开新一轮。

        该分支是本轮新增代码里唯一的兜底路径：它会把**已消费的脏位**一并丢弃（轮首已清），故必须钉住
        「不挂死、不冒泡、下一轮照跑」——否则一次偶发异常就静默停掉该 Computer 的全部资源刷新。
        """
        computer = await _booted_computer(tmp_path)
        calls: list[int] = []

        async def boom(*args: Any) -> None:
            calls.append(1)
            raise RuntimeError("round boom")

        computer._on_resource_list_changed_windows = boom  # type: ignore[method-assign]

        await computer._on_manager_change(_res_list_changed())
        await _settle_resources(computer)  # 不得抛出

        assert calls == [1], "异常轮次须被兜住（且不重试成死循环）"
        assert computer._resource_refresh_task is not None and computer._resource_refresh_task.done()

        # 后续通知仍须能开新一轮（脏位被丢弃不等于任务永久停摆）
        await computer._on_manager_change(_res_list_changed())
        await _settle_resources(computer)

        assert calls == [1, 1], "轮中止后到达的通知必须仍被处理"

    async def test_skills_content_flag_wins_over_list_flag_in_a_round(self, tmp_path: Path) -> None:
        """③ 吃掉 ②（无在途任务时的合并规则）：同轮同时标脏时只跑无条件重物化，不重复跑集合比对路径。

        该合并规则此前只写在 :meth:`_schedule_resource_refresh` 的 docstring 里 —— 没有测试钉住它，
        后人「顺手改成两个都跑」不会被任何断言拦住（多一次全量重物化 RPC）。
        """
        computer = await _booted_computer(tmp_path)
        rec = _ActionRecorder()
        rec.install(computer)

        await computer._on_manager_change(_res_list_changed())  # 标 ① + ②
        await computer._on_manager_change(_res_updated("skill://srv/demo"))  # 标 ③（尚无在途轮次）
        await _settle_resources(computer)

        assert rec.actions == ["windows", "skills_content"], "③ 须吃掉 ②，不得两条技能动作都跑"

    async def test_collect_error_is_swallowed_and_does_not_emit(self, tmp_path: Path) -> None:
        """采集异常 → 记日志、不 emit、不向接收循环冒泡。"""
        computer = await _booted_computer(tmp_path)
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]

        async def boom() -> set[str]:
            raise RuntimeError("collect boom")

        async def empty_refs() -> set[str]:
            return set()

        computer._acollect_window_uris = boom  # type: ignore[method-assign]
        computer._acollect_skill_refs = empty_refs  # type: ignore[method-assign]

        await computer._on_manager_change(_res_list_changed())  # 不得抛出
        await _settle_resources(computer)

        assert client.desktop_refreshes == 0
        assert computer._windows_cache == set()


class TestCoalescingAndShutdown:
    """在途合并与停机结算（对齐 #197 的两条硬断言：句柄已 cancel 结算、停机窗口不复活）。"""

    @pytest.mark.asyncio
    async def test_notification_during_inflight_refresh_is_not_dropped(self, tmp_path: Path) -> None:
        """在途期间到达的通知不得被丢弃 → 结算后补跑一轮。"""
        computer = await _booted_computer(tmp_path)
        rec = _ActionRecorder()
        rec.gate = asyncio.Event()
        rec.install(computer)

        await _deliver(computer, _res_list_changed())
        await _wait_until(lambda: rec.actions == ["windows"])  # 第一轮已进入并被卡住
        await _deliver(computer, _res_updated("skill://srv/demo"))  # 在途 → 标脏 ③
        rec.gate.set()
        await _settle_resources(computer)

        assert rec.actions == ["windows", "skills_list", "skills_content"], "在途通知必须补跑，不得丢弃"

    @pytest.mark.asyncio
    async def test_shutdown_cancels_inflight_refresh_and_stops_rescheduling(self, tmp_path: Path) -> None:
        """停机：在途刷新被 **cancel 并结算**；``aclose()`` 窗口内到达的通知不得复活任务。

        五条断言分别钉死实现属性（仅断言终态会漏）：① 在途句柄 `done() and cancelled()`（杀「只 cancel 不
        await」与「整段摘除 cancel」）；② 停机后句柄置 None；③ 停机窗口内**投递通知**不得新建任务（只有
        「先摘 ``mcp_manager`` 引用、再 ``aclose``」能挡住）；④ 被取消的轮次**不得提交**窗口缓存（杀
        「提前提交 / finally 提交」）；⑤ 停机后再投递不复活。
        """
        computer = await _booted_computer(tmp_path)
        client = _RecordingClient()
        computer.socketio_client = client  # type: ignore[assignment]
        assert computer.mcp_manager is not None
        manager = computer.mcp_manager

        gate = asyncio.Event()
        entered: list[str] = []

        async def slow_windows() -> set[str]:
            entered.append("w")
            await gate.wait()  # 卡住在途轮次，制造停机窗口
            return {"window://host/main"}

        async def empty_refs() -> set[str]:
            return set()

        computer._acollect_window_uris = slow_windows  # type: ignore[method-assign]
        computer._acollect_skill_refs = empty_refs  # type: ignore[method-assign]

        window_saw_no_task: list[bool] = []

        async def aclose_spy() -> None:
            await _deliver(computer, _res_list_changed())
            window_saw_no_task.append(computer._resource_refresh_task is None)

        manager.aclose = aclose_spy  # type: ignore[method-assign]

        await _deliver(computer, _res_list_changed())
        inflight = computer._resource_refresh_task
        await _wait_until(lambda: inflight is not None and not inflight.done() and entered == ["w"])

        await computer.shutdown()

        assert inflight is not None and inflight.done() and inflight.cancelled(), "在途刷新须被 cancel 并结算"
        assert computer._resource_refresh_task is None
        assert window_saw_no_task == [True], "停机窗口内到达的通知不得新建任务（摘引用须先于 aclose）"
        assert computer._windows_cache == set(), "被取消的轮次不得提交窗口缓存"
        assert client.desktop_refreshes == 0, "被取消的轮次不得 emit"
        assert computer.mcp_manager is None

        await _deliver(computer, _res_list_changed())
        assert computer._resource_refresh_task is None, "停机后不得再调度资源刷新"
