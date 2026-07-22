# -*- coding: utf-8 -*-
# filename: test_http_auth_error.py
# @Time    : 2026/07/20
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
HTTP 传输层授权错误捕获的稳定组件测试（#133）——进程内、不起子进程、CI 稳定。

Stable in-process component tests for HTTP transport-layer auth-error capture (#133).

协议依据 / Protocol: a2c-smcp-protocol ``error-handling.md`` §4006/4007 + ``conformance-tests.md`` §4.8。mcp Python
SDK 把 ``tools/call`` 的 401/403 抛进传输任务组、拆连接致 ``session.call_tool`` **挂起**（非抛异常），故本修复经
传输层 ``httpx`` client 在 mcp 吞掉前截获 401/403（``_AuthWatchingClient``）、再由 ``HttpMCPClient.call_tool`` 的
竞速兜底抛 ``UpstreamAuthError`` → ``manager.acall_tool`` 分类 → 4006/4007。

为何用组件测试而非真实 MCP server 子进程：起真实 server（multiprocessing daemon）在本仓 pytest 全量会话下与
其它子进程用例同跑时偶发挂起（与 e2e 同族的进程回收问题），不稳。本文件直接覆盖修复的**两个关键机制**，进程内、
可复现：① ``_AuthWatchingClient.stream()`` 在 401/403 时把 ``(rpc_id, status, WWW-Authenticate)`` 经 observer 回投
（``httpx.MockTransport`` 注入，不起 server）；② ``HttpMCPClient.call_tool`` 竞速——信号到达即取消挂起的
``super().call_tool()`` 并抛 ``UpstreamAuthError``。分类→构造（``UpstreamAuthError`` → 4006/4007）由
``test_auth_error.py`` / ``test_manager_auth_error.py`` 覆盖。

> 真实传输端到端（含 mcp SSE 握手）由手工 / 专项 invocation 验证（见 ``http_client.py`` _AuthWatchingClient 的
> 限界注释 + 协议 #35 第四景 follow-up）；本文件是 CI 稳定的机制回归守卫。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from a2c_smcp.computer.mcp_clients.auth_error import UpstreamAuthError
from a2c_smcp.computer.mcp_clients.base_client import STATES
from a2c_smcp.computer.mcp_clients.http_client import HttpMCPClient, _AuthSignalObserver, _AuthWatchingClient


# ── ① _AuthWatchingClient 在 401/403 时捕获 (rpc_id, status, WWW-Authenticate) ──
def _make_watching_client(
    observer: _AuthSignalObserver, *, status: int, www_authenticate: str | None
) -> _AuthWatchingClient:
    """构造一个 ``_AuthWatchingClient``，其 transport 对任意 POST 返回指定状态 + 头（不起真实 server）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"www-authenticate": www_authenticate} if www_authenticate is not None else {}
        return httpx.Response(status, headers=headers, request=request)

    return _AuthWatchingClient(
        observer=observer, transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0)
    )


def test_watching_client_captures_401_with_rpc_id_and_www_authenticate() -> None:
    """401 + WWW-Authenticate → observer 收到 (rpc_id=POST 体 JSON-RPC id, status=401, header 原值)。"""
    observer = _AuthSignalObserver()
    observer.register(7)  # 模拟在途 call_tool 登记的 request_id
    client = _make_watching_client(observer, status=401, www_authenticate='Bearer realm="mcp"')
    payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}

    async def drive() -> None:
        async with client.stream("POST", "http://127.0.0.1:1/mcp", json=payload):
            pass  # mcp 此后会调 raise_for_status（这里仅验证进入上下文前的捕获）

    asyncio.run(drive())
    signal = observer.take(7)
    assert signal is not None and signal.status_code == 401
    assert signal.www_authenticate_header == 'Bearer realm="mcp"'


def test_watching_client_captures_403_and_no_header() -> None:
    """403 无 WWW-Authenticate → 捕获 status=403、header=None。"""
    observer = _AuthSignalObserver()
    observer.register(3)
    client = _make_watching_client(observer, status=403, www_authenticate=None)
    payload = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}

    async def drive() -> None:
        async with client.stream("POST", "http://127.0.0.1:1/mcp", json=payload):
            pass

    asyncio.run(drive())
    signal = observer.take(3)
    assert signal is not None and signal.status_code == 403
    assert signal.www_authenticate_header is None


def test_watching_client_does_not_capture_non_auth_status() -> None:
    """非 401/403（如 500）→ 不捕获（不误判授权；5xx 归 4003 通用失败）。"""
    observer = _AuthSignalObserver()
    observer.register(1)
    client = _make_watching_client(observer, status=500, www_authenticate=None)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}

    async def drive() -> None:
        async with client.stream("POST", "http://127.0.0.1:1/mcp", json=payload):
            pass

    asyncio.run(drive())
    assert observer.take(1) is None, "500 不得被捕获为授权失败"


def test_watching_client_drops_signal_without_registered_observer() -> None:
    """迟到的/无主的信号（无在途监听者）→ 丢弃，不残留（调用已正常返回则不追溯失败）。"""
    observer = _AuthSignalObserver()
    # 不 register 任何 id → capture 应静默丢弃
    client = _make_watching_client(observer, status=401, www_authenticate=None)
    payload = {"jsonrpc": "2.0", "id": 999, "method": "tools/call", "params": {}}

    async def drive() -> None:
        async with client.stream("POST", "http://127.0.0.1:1/mcp", json=payload):
            pass

    asyncio.run(drive())
    assert observer.take(999) is None


# ── ② HttpMCPClient.call_tool 竞速：信号到达即取消挂起的 super().call_tool 并抛 UpstreamAuthError ──
@pytest.mark.asyncio
async def test_call_tool_raises_upstream_auth_when_signal_fires_while_call_hung() -> None:
    """super().call_tool 挂起（mcp 拆连接致 call_tool 永不返回）+ 信号到达 → 抛 UpstreamAuthError，不挂死。

    反致盲：request_id=42（独立值），断言 UpstreamAuthError.status_code == 403（若误用 401 默认即红）。
    """
    from mcp.client.session_group import StreamableHttpParameters

    client = HttpMCPClient(StreamableHttpParameters(url="http://127.0.0.1:1/mcp"))
    # 直接置入 connected 态 + 假 session（暴露 _request_id），绕开握手。
    object.__setattr__(client, "state", STATES.connected)
    fake_session = MagicMock()
    fake_session._request_id = 42
    client._async_session = fake_session  # type: ignore[attr-defined]

    # super().call_tool（基类）挂起——模拟 mcp 拆连接后 call_tool 永不返回。
    hang_event = asyncio.Event()

    async def hanging_super_call_tool(self, tool_name: str, params: dict) -> Any:  # noqa: ARG001
        await hang_event.wait()  # 永不 set → 挂起

    import contextlib

    import a2c_smcp.computer.mcp_clients.http_client as httpc_mod

    original = httpc_mod.BaseMCPClient.call_tool
    httpc_mod.BaseMCPClient.call_tool = hanging_super_call_tool  # type: ignore[assignment]
    try:
        # 在 call_tool 竞速开始后、信号到达前，经 observer 触发 403 捕获（call_tool 内部已 register(42)）。
        async def fire_signal_after_delay() -> None:
            await asyncio.sleep(0.1)
            client._auth_observer.capture_auth_signal(42, 403, None)

        fire_task = asyncio.ensure_future(fire_signal_after_delay())
        with pytest.raises(UpstreamAuthError) as exc_info:
            await asyncio.wait_for(client.call_tool("needs_auth", {}), timeout=10.0)
        assert exc_info.value.status_code == 403
        fire_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await fire_task
    finally:
        httpc_mod.BaseMCPClient.call_tool = original  # type: ignore[assignment]


# ── 回归守卫（隔离审查建议）：并发、正常路径、非授权异常 ──────────────────────────
def _make_connected_client(start_request_id: int) -> HttpMCPClient:
    """构造一个 connected 态 + 假 session（``_request_id`` 起始值可调）的 HttpMCPClient，绕开握手。"""
    from mcp.client.session_group import StreamableHttpParameters

    client = HttpMCPClient(StreamableHttpParameters(url="http://127.0.0.1:1/mcp"))
    object.__setattr__(client, "state", STATES.connected)
    fake_session = MagicMock()
    fake_session._request_id = start_request_id
    client._async_session = fake_session  # type: ignore[attr-defined]
    return client


@pytest.mark.asyncio
async def test_concurrent_call_tools_each_surfaces_own_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """同 client 两个并发 call_tool —— Lock 串行化后各读独立 ``_request_id``、各自信号到达即抛 UpstreamAuthError。

    反 🔴 回归：修复前并发两路读同一 ``_request_id``，第二路 register 冲突退化为未保护直通（挂起）。mock super
    忠实模拟 send_request「读 N → 用 N → 自增 N+1」并在用 N 后为该 id 触发 403 信号，再挂起。
    """
    client = _make_connected_client(start_request_id=100)

    async def super_reads_uses_increments_then_fires_and_hangs(self, tool_name, params):  # noqa: ARG001
        sess = self._async_session
        used_id = sess._request_id  # call_tool 已为此 id 注册 observer
        await asyncio.sleep(0)  # 让出循环（模拟 send_request 调度）
        sess._request_id = used_id + 1  # send_request 自增（供下一个并发 call_tool 读到独立 id）
        self._auth_observer.capture_auth_signal(used_id, 403, None)  # 上游对该 id 返 403
        await asyncio.Event().wait()  # mcp 拆连接 → call_tool 挂起

    monkeypatch.setattr(
        "a2c_smcp.computer.mcp_clients.http_client.BaseMCPClient.call_tool",
        super_reads_uses_increments_then_fires_and_hangs,
    )

    results = await asyncio.wait_for(
        asyncio.gather(client.call_tool("t1", {}), client.call_tool("t2", {}), return_exceptions=True),
        timeout=10.0,
    )
    # 两路均抛 UpstreamAuthError（无一路退化直通挂死、无 deadlock）。
    assert all(isinstance(r, UpstreamAuthError) and r.status_code == 403 for r in results), results


@pytest.mark.asyncio
async def test_call_tool_normal_success_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """合法成功调用（无授权信号）→ 原样返回基类结果，override 不破坏正常路径。"""
    from mcp.types import CallToolResult, TextContent

    client = _make_connected_client(start_request_id=1)
    ok = CallToolResult(content=[TextContent(type="text", text="ok")])

    async def returning_super(self, tool_name, params):  # noqa: ARG001
        return ok

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.http_client.BaseMCPClient.call_tool", returning_super)
    result = await asyncio.wait_for(client.call_tool("t", {}), timeout=5.0)
    assert result is ok


@pytest.mark.asyncio
async def test_call_tool_non_auth_exception_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """非授权异常（工具坏了 / 网络抖动，无 401/403 信号）→ 原样重抛，绝不误产 UpstreamAuthError（防 false-positive）。"""
    client = _make_connected_client(start_request_id=1)

    async def raising_super(self, tool_name, params):  # noqa: ARG001
        raise RuntimeError("tool blew up")

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.http_client.BaseMCPClient.call_tool", raising_super)
    with pytest.raises(RuntimeError, match="tool blew up"):
        await asyncio.wait_for(client.call_tool("t", {}), timeout=5.0)
