# -*- coding: utf-8 -*-
# filename: http_client.py
# @Time    : 2025/8/19 10:55
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.session import MessageHandlerFnT
from mcp.client.session_group import StreamableHttpParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult

from a2c_smcp.computer.mcp_clients.auth_error import UpstreamAuthError
from a2c_smcp.computer.mcp_clients.base_client import STATES, BaseMCPClient


@dataclass(frozen=True)
class _AuthSignal:
    """传输层观测到的一次上游授权失败信号 / one observed upstream auth-failure signal."""

    status_code: int
    www_authenticate_header: str | None


def _parse_jsonrpc_id(content: bytes | None) -> int | str | None:
    """从 POST 请求体解析 JSON-RPC ``id``（与 ``session._request_id`` 同源，用于关联到发起的 ``call_tool``）。"""
    if not content:
        return None
    try:
        msg = json.loads(content)
    except (ValueError, TypeError):
        return None
    if isinstance(msg, dict):
        return msg.get("id")
    return None


class _AuthWatchingClient(httpx.AsyncClient):
    """``httpx.AsyncClient`` 子类：在 ``stream()`` 响应为 401/403 时，把信号经 ``observer`` 回投给 HttpMCPClient。

    mcp Python SDK 在 ``streamable_http.py`` ``post_writer`` 里把 ``tools/call`` 的 401/403 抛进传输任务组、
    拆连接关 ``read_stream``，导致 ``session.call_tool`` **挂起**（不是抛异常）。故授权失败信号须在 mcp 吞掉它
    **之前**于传输层截获，再经 side-channel 让 ``HttpMCPClient.call_tool`` 自身兜底合成（协议 error-handling.md
    §可观测判据：已观测授权失败信号但 ``CallToolResult`` 不会经原路径返回时，Computer MUST 自身层面兜底）。

    截获点：``stream()`` 拿到响应、在 mcp 调 ``raise_for_status()`` 之前——状态码 + ``WWW-Authenticate`` 均可得，
    且请求体（``response.request.content``）携带 JSON-RPC ``id`` 供关联。

    **覆盖范围（协议 §4.8 四景）**：本机制覆盖「初始响应即 401/403」的前三景（授权失败的**主流形态**）。第四景
    （POST 200 + SSE 流内 401）不在初始响应上、mcp-python 以「流静默终止」呈现而非可截获的 401，需 SSE 流体
    拦截，留作 follow-up（见 ``test_http_auth_error.py`` 模块尾注 + 协议 #35）。
    """

    def __init__(
        self,
        *,
        observer: _AuthSignalObserver,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 复刻 mcp ``create_mcp_http_client`` 的默认（follow_redirects=True、timeout 兜底 30s）。
        # ``transport`` 仅用于测试注入（httpx.MockTransport）；生产路径不传（默认 None → 真实网络）。
        kwargs: dict[str, Any] = {"follow_redirects": True, "timeout": timeout or httpx.Timeout(30.0)}
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        if transport is not None:
            kwargs["transport"] = transport
        super().__init__(**kwargs)
        self._observer = observer

    @contextlib.asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: Any) -> AsyncIterator[httpx.Response]:  # type: ignore[override]
        async with super().stream(method, url, **kwargs) as response:
            if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
                rpc_id = _parse_jsonrpc_id(response.request.content)
                self._observer.capture_auth_signal(
                    rpc_id,
                    response.status_code,
                    response.headers.get("www-authenticate"),
                )
            yield response


class _AuthSignalObserver:
    """HttpMCPClient 持有的信号枢纽：watching client 写、``call_tool`` 读（race）。

    per-request（按 JSON-RPC ``id``）的 ``asyncio.Event`` 让 ``call_tool`` 能在信号到达瞬间醒来取消挂起的调用，
    无需任意超时阈值（合法慢调用不受影响）。同一 client 并发多个 ``call_tool`` 各持不同 ``id``，互不串扰。
    """

    def __init__(self) -> None:
        self._signals: dict[object, _AuthSignal] = {}
        self._events: dict[object, asyncio.Event] = {}

    def register(self, rpc_id: object) -> asyncio.Event | None:
        """登记一次在途调用，返回供其 race 的 Event；``rpc_id`` 为 None（无法关联）则返回 None（退化为直通）。"""
        if rpc_id is None or rpc_id in self._events:
            return None  # None 无法关联；或 id 复用（理论不达）——保守退化为不监听
        event = asyncio.Event()
        self._events[rpc_id] = event
        return event

    def capture_auth_signal(
        self, rpc_id: object, status_code: int, www_authenticate_header: str | None
    ) -> None:
        """传输层回调：仅在已有在途监听者时记录信号并唤醒（迟到的/无主的信号丢弃——调用已正常返回则不追溯失败）。"""
        if rpc_id is None or rpc_id not in self._events:
            return
        self._signals[rpc_id] = _AuthSignal(status_code, www_authenticate_header)
        self._events[rpc_id].set()

    def take(self, rpc_id: object) -> _AuthSignal | None:
        return self._signals.pop(rpc_id, None)

    def discard(self, rpc_id: object) -> None:
        self._events.pop(rpc_id, None)
        self._signals.pop(rpc_id, None)


class HttpMCPClient(BaseMCPClient[StreamableHttpParameters]):
    def __init__(
        self,
        params: StreamableHttpParameters,
        state_change_callback: Callable[[str, str], None | Awaitable[None]] | None = None,
        message_handler: MessageHandlerFnT | None = None,
    ) -> None:
        """
        初始化HTTP客户端，支持传入自定义 message_handler
        Initialize HTTP client with optional message_handler
        """
        assert isinstance(params, StreamableHttpParameters), "params must be an instance of StreamableHttpParameters"
        super().__init__(params, state_change_callback, message_handler)
        self._auth_observer = _AuthSignalObserver()

    async def _create_async_session(self) -> ClientSession:
        """
        创建异步会话

        Returns:
            ClientSession: 异步会话
        """
        # 目前忽略了 GetSessionIdCallback。只有在手动管理Session才有必要，在封装内全部使用自动管理。
        # 需要注意 self.params.model_dump() 的 mode 参数使用默认python，不可以使用json，因为当前Params中有 timedelta，如果使用json会序列化
        # 为str，导致连接报错。
        #
        # #133：注入 ``httpx_client_factory`` 为「授权失败信号观测 client」，使 mcp 用的 httpx client 在 401/403
        # 时把信号回投本 client（mcp 自身会吞掉该异常致 call_tool 挂起，故须经 side-channel 兜底）。
        def _httpx_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return _AuthWatchingClient(observer=self._auth_observer, headers=headers, timeout=timeout, auth=auth)

        aread_stream, awrite_stream, _ = await self._aexit_stack.enter_async_context(
            streamablehttp_client(**self.params.model_dump(mode="python"), httpx_client_factory=_httpx_factory),
        )
        # 如果提供了 message_handler，则一并传入 ClientSession
        # If message_handler is provided, pass it into ClientSession
        client_session = await self._aexit_stack.enter_async_context(
            ClientSession(aread_stream, awrite_stream, message_handler=self._message_handler),
        )
        return client_session

    async def call_tool(self, tool_name: str, params: dict) -> CallToolResult:
        """调用工具，并在 mcp 吞掉 401/403 致底层 ``call_tool`` 挂起时**自身兜底** surface 授权错误（#133）。

        与基类 ``call_tool`` 同样先捕获本次 MCP ``request_id``（= POST 体 JSON-RPC ``id``，关联键），随后把
        ``super().call_tool()`` 与「该 request 的授权信号 Event」**竞速**：

        - 信号先到（上游返 401/403、mcp 拆连接致 ``super().call_tool`` 挂起）→ 取消挂起的调用，抛
          :class:`UpstreamAuthError`（携状态码），交 :meth:`MCPServerManager.acall_tool` 的分类器 → 4006/4007。
        - 调用先返回/抛错（正常或非授权失败）→ 原样透传，行为与基类一致（合法慢调用不受任何超时影响）。

        无任意超时阈值——信号到达即响应、合法调用不设上限，满足协议 §可观测判据「MUST NOT 挂至超时」。
        """
        # 复刻基类状态守卫：未连接须先抛 ConnectionError（基类在 ``async_session`` 之前判），勿让 ``await
        # self.async_session`` 的惰性建连绕过它（否则「未连接调用」行为回归）。
        if self.state != STATES.connected:
            raise ConnectionError("Not connected to server")
        session = await self.async_session
        request_id = getattr(session, "_request_id", None)
        event = self._auth_observer.register(request_id)
        if event is None:
            # 无法关联（session 不暴露 _request_id）→ 退化为基类直通，不引入回归。
            return await super().call_tool(tool_name, params)

        call_task = asyncio.ensure_future(super().call_tool(tool_name, params))
        event_task = asyncio.ensure_future(event.wait())
        try:
            await asyncio.wait({call_task, event_task}, return_when=asyncio.FIRST_COMPLETED)
            if event.is_set():
                # 已观测授权失败信号：取消挂起的 super().call_tool（触发基类 #96 best-effort 通知远端），再兜底抛 UpstreamAuthError。
                # mcp 的 session.call_tool 在传输被拆后可能不响应取消（挂在已关流上）→ 用有限等待兜底，超时即弃
                # （传输已断、任务终将随 session 关闭回收），绝不拖住本调用（协议 §可观测判据「MUST NOT 挂至超时」）。
                call_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception, asyncio.TimeoutError):
                    await asyncio.wait_for(call_task, timeout=3.0)
                signal = self._auth_observer.take(request_id)
                if signal is not None:
                    raise UpstreamAuthError(signal.status_code, signal.www_authenticate_header)
            # 调用先完成（正常结果或非授权异常）→ 原样返回/重抛。
            return call_task.result()
        finally:
            event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task
            self._auth_observer.discard(request_id)
