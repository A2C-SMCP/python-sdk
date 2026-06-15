# -*- coding: utf-8 -*-
# filename: test_base_client_windows.py
# @Time    : 2025/10/02 17:05
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文: 集成测试 BaseMCPClient.list_windows，针对新提供的 Resources 服务器（无订阅/有订阅）。
英文: Integration tests for BaseMCPClient.list_windows with new Resources servers (no subscribe/with subscribe).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters
from mcp.types import ResourceUpdatedNotification

from a2c_smcp.computer.mcp_clients.stdio_client import StdioMCPClient


async def _wait_for(pred: Callable[[], bool], timeout: float = 5.0, interval: float = 0.05) -> None:
    """中文: 轮询等待断言成立或超时（用于等待异步到达的 MCP 通知）。
    English: poll until ``pred()`` is true or timeout (await asynchronously-delivered MCP notifications).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_list_windows_without_subscribe_returns_resources() -> None:
    """
    中文: 针对仅 Resources 且不支持订阅的服务器，list_windows 仍应正常返回 window:// 资源。
    英文: For Resources-only server without subscribe, list_windows should still return window:// resources.
    """
    server_py = Path(__file__).resolve().parents[2] / "computer" / "mcp_servers" / "resources_stdio_server.py"
    assert server_py.exists(), f"server script not found: {server_py}"

    params = StdioServerParameters(command=sys.executable, args=[str(server_py)])
    client = StdioMCPClient(params)

    await client.aconnect()
    await client._create_session_success_event.wait()

    # capabilities 检查 / check capabilities
    assert client.initialize_result is not None
    assert client.initialize_result.capabilities.resources is not None
    assert client.initialize_result.capabilities.resources.subscribe is False

    windows = await client.list_windows()
    assert isinstance(windows, list)
    assert len(windows) >= 1, "Resources capability without subscribe should still list window:// resources."
    uris = [str(r.uri) for r in windows]
    assert all(u.startswith("window://") for u in uris)

    await client.adisconnect()
    await client._async_session_closed_event.wait()


@pytest.mark.asyncio
async def test_list_windows_subscribe_is_idempotent_no_storm() -> None:
    """
    中文: #110 反馈环根因回归 —— ``list_windows()`` 重复调用必须「幂等订阅」：每个 window:// 资源
          在会话内**至多 subscribe 一次**。否则每次重复 subscribe 都会触发 subscribe-srv 回发一条
          ``ResourceUpdatedNotification``，与 Agent 侧「桌面更新自动回拉 ``GET_DESKTOP`` → ``GET_DESKTOP``
          经 ``get_windows_details`` 又调用 ``list_windows()``」串成自放大反馈环，在慢速 CI（2 核）上把
          事件循环压垮 → 整套 e2e 偶发挂死（~40%，signal-timeout 打不断）。

    英文: Regression for the #110 feedback-loop root cause. Repeated ``list_windows()`` MUST subscribe each
          window:// resource at most once per session; otherwise every re-subscribe makes subscribe-srv
          re-emit a ``ResourceUpdatedNotification``, which combined with the Agent's auto ``GET_DESKTOP``
          refresh (whose ``get_windows_details`` calls ``list_windows()`` again) forms a self-amplifying loop
          that wedges the event loop on slow CI (~40% e2e hang; signal-timeout cannot interrupt it).
    """
    server_py = Path(__file__).resolve().parents[2] / "computer" / "mcp_servers" / "resources_subscribe_stdio_server.py"
    assert server_py.exists(), f"server script not found: {server_py}"

    # subscribe-srv 每次收到 resources/subscribe 都会回发一次 ResourceUpdatedNotification，
    # 故收到的通知条数 == 实际 subscribe 次数，可直接据此断言幂等性。
    # subscribe-srv re-emits one ResourceUpdatedNotification per resources/subscribe, so the count of
    # received notifications equals the number of subscribe calls — a direct probe for idempotency.
    updates: list[str] = []

    async def _count_resource_updated(message: Any) -> None:
        root = getattr(message, "root", None)
        if isinstance(root, ResourceUpdatedNotification):
            updates.append(str(root.params.uri))

    params = StdioServerParameters(command=sys.executable, args=[str(server_py)])
    client = StdioMCPClient(params, message_handler=_count_resource_updated)

    await client.aconnect()
    await client._create_session_success_event.wait()
    assert client.initialize_result is not None
    assert client.initialize_result.capabilities.resources is not None
    assert client.initialize_result.capabilities.resources.subscribe is True

    try:
        # 首轮：两个 window:// 资源各订阅一次 → 期望恰好收到 2 条 resource_updated
        # First round: two window:// resources each subscribed once → expect exactly 2 resource_updated
        windows = await client.list_windows()
        n_windows = len(windows)
        assert n_windows == 2, f"subscribe-srv 暴露两个 window:// 资源，实际 {n_windows}"
        await _wait_for(lambda: len(updates) >= n_windows, timeout=5.0)
        first_round = len(updates)
        assert first_round == n_windows, f"首轮应每个窗口恰一次 resource_updated，实际 {first_round}"

        # 二次 list_windows：幂等订阅下不得重新 subscribe → 不得产生任何新增 resource_updated。
        # 给「重复订阅风暴」留出充足到达时间（本地 stdio 亚秒级），幂等下计数应保持不变。
        # Second list_windows: idempotent subscribe must not re-subscribe → no new resource_updated.
        await client.list_windows()
        await asyncio.sleep(1.0)
        assert len(updates) == first_round, (
            "list_windows() 重复调用重新订阅，触发 resource_updated 风暴（#110 反馈环根因）："
            f"第二轮新增 {len(updates) - first_round} 条 / repeated list_windows re-subscribed and stormed"
        )
    finally:
        await client.adisconnect()
        await client._async_session_closed_event.wait()


@pytest.mark.asyncio
async def test_list_windows_subscribe_resets_on_reconnect() -> None:
    """
    中文: #110 幂等性以**会话为界** —— 断开重连后 ``list_windows()`` 必须重新订阅。已订阅集合在会话拆除时
          清空，故重连建立新会话后会对同一 window:// 资源再次 subscribe（每个窗口再产生一条 resource_updated）。
          反证：若清空缺失，重连后将永久抑制订阅 → 真实内容更新通知丢失（破坏 DoD #3 / v0.2 通知语义）。

    英文: #110 idempotency is **per-session** — after disconnect+reconnect, ``list_windows()`` MUST re-subscribe.
          The subscribed-URI set is cleared on session teardown, so a reconnected (fresh) session re-subscribes
          each window:// resource. Without the clear, reconnect would permanently suppress subscriptions.
    """
    server_py = Path(__file__).resolve().parents[2] / "computer" / "mcp_servers" / "resources_subscribe_stdio_server.py"
    assert server_py.exists(), f"server script not found: {server_py}"

    updates: list[str] = []

    async def _count_resource_updated(message: Any) -> None:
        root = getattr(message, "root", None)
        if isinstance(root, ResourceUpdatedNotification):
            updates.append(str(root.params.uri))

    params = StdioServerParameters(command=sys.executable, args=[str(server_py)])
    client = StdioMCPClient(params, message_handler=_count_resource_updated)

    try:
        # 首个会话：订阅 N 个窗口 → N 条 resource_updated / first session: subscribe N windows → N updates
        await client.aconnect()
        await client._create_session_success_event.wait()
        windows = await client.list_windows()
        n_windows = len(windows)
        assert n_windows == 2, f"subscribe-srv 暴露两个 window:// 资源，实际 {n_windows}"
        await _wait_for(lambda: len(updates) >= n_windows, timeout=5.0)
        after_first = len(updates)
        assert after_first == n_windows

        # 断开 → 重置 → 重连（建立全新会话）/ disconnect → reset → reconnect (fresh session)
        await client.adisconnect()
        await client.ainitialize()
        await client.aconnect()
        await client._create_session_success_event.wait()

        # 重连后会话级订阅集合已清空 → list_windows 重新订阅 → 再产生 N 条 resource_updated
        # Post-reconnect the per-session set is cleared → list_windows re-subscribes → N more updates
        await client.list_windows()
        await _wait_for(lambda: len(updates) >= after_first + n_windows, timeout=5.0)
        assert len(updates) == after_first + n_windows, (
            f"重连后应重新订阅（每窗口再一条 resource_updated），实际新增 {len(updates) - after_first} "
            f"/ reconnect must re-subscribe"
        )
    finally:
        await client.adisconnect()
        await client._async_session_closed_event.wait()


@pytest.mark.skip(
    reason=(
        "待 sub-issue #11 (organize_desktop 改读 annotations / _meta) 重写："
        "v0.2 起 priority 不再来自 URI query，base_client.list_windows() 的 priority 排序"
        "需迁移到读取 Resource.annotations.priority；mock server 也需相应升级。"
    ),
)
@pytest.mark.asyncio
async def test_list_windows_with_subscribe_returns_sorted_and_subscribed() -> None:
    """
    中文: 针对支持订阅的服务器，list_windows 应返回 window:// 资源，并按 ``annotations.priority`` 降序排序。
    英文: For server with subscribe enabled, list_windows should return window:// resources sorted by
          ``annotations.priority`` desc (v0.2 — priority no longer comes from URI query).
    """
    server_py = Path(__file__).resolve().parents[2] / "computer" / "mcp_servers" / "resources_subscribe_stdio_server.py"
    assert server_py.exists(), f"server script not found: {server_py}"

    params = StdioServerParameters(command=sys.executable, args=[str(server_py)])
    client = StdioMCPClient(params)

    await client.aconnect()
    await client._create_session_success_event.wait()

    # capabilities 检查 / check capabilities
    assert client.initialize_result is not None
    assert client.initialize_result.capabilities.resources is not None
    assert client.initialize_result.capabilities.resources.subscribe is True

    windows = await client.list_windows()
    assert isinstance(windows, list)
    assert len(windows) >= 1

    # v0.2: URI 为纯标识符，不再附 ?priority= / ?fullscreen= query
    # v0.2: URIs are pure identifiers (no query metadata)
    uris = [str(r.uri) for r in windows]
    assert all(u.startswith("window://") for u in uris)
    assert all("?" not in u for u in uris), "v0.2 windows MUST NOT carry URI query"

    # 订阅版 mock server 通过 annotations 声明：dashboard(priority=0.9, fullscreen=true) 与 main(priority=0.6)
    # base_client 按 annotations.priority 降序排序，因此 dashboard 在前
    # subscribe-enabled mock declares dashboard(priority=0.9, fullscreen=true) and main(priority=0.6) via annotations;
    # base_client sorts by annotations.priority desc, so dashboard comes first
    assert any("/dashboard" in u for u in uris), "dashboard window expected"
    assert any("/main" in u for u in uris), "main window expected"

    if len(uris) >= 2:
        assert "/dashboard" in uris[0]
        assert "/main" in uris[1]

    # 进一步校验元数据下沉到 annotations / _meta（v0.2 §4.1）
    # Verify metadata sinking into annotations / _meta (v0.2 §4.1)
    by_uri = {str(r.uri): r for r in windows}
    dash = next(r for u, r in by_uri.items() if "/dashboard" in u)
    main = next(r for u, r in by_uri.items() if "/main" in u)
    assert dash.annotations is not None and dash.annotations.priority == 0.9
    assert main.annotations is not None and main.annotations.priority == 0.6
    assert dash.meta == {"fullscreen": True}

    await client.adisconnect()
    await client._async_session_closed_event.wait()
