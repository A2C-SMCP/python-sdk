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
from typing import TYPE_CHECKING, Any

import httpx
from mcp import ClientSession
from mcp.client.session import MessageHandlerFnT
from mcp.client.session_group import StreamableHttpParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult

from a2c_smcp.computer.mcp_clients.auth_error import (
    UpstreamAuthError,
    UpstreamRedirectStoppedError,
)
from a2c_smcp.computer.mcp_clients.base_client import STATES, BaseMCPClient
from a2c_smcp.computer.mcp_clients.oauth_security import (
    CROSS_ORIGIN_REDIRECT_STOP_MARKER,
    OAuthGuardTransport,
)
from a2c_smcp.computer.mcp_clients.oauth_types import (
    SYNTH_INSUFFICIENT_SCOPE_CHALLENGE,
    OAuthError,
    is_stepup_insufficient_scope_error,
)

if TYPE_CHECKING:
    from a2c_smcp.computer.mcp_clients.oauth_coordinator import OAuthCoordinator


@dataclass(frozen=True)
class AuthSignal:
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


def _parse_jsonrpc_id_from_stream_kwargs(kwargs: dict[str, Any]) -> int | str | None:
    """从 ``stream()`` 的调用参数解析 JSON-RPC ``id``（异常路径无 response 对象可用）。

    mcp 1.15 以 ``content=<bytes>`` 传 POST 体；mcp ≥1.29 以 ``json=<dict>`` 传
    （``streamable_http._handle_post_request``）——两形态兼容，任一成功即返回。
    """
    from_content = _parse_jsonrpc_id(kwargs.get("content"))
    if from_content is not None:
        return from_content
    payload = kwargs.get("json")
    if isinstance(payload, dict):
        return payload.get("id")
    return None


class _AuthWatchingClient(httpx.AsyncClient):
    """``httpx.AsyncClient`` 子类：在 ``stream()`` 响应为 401/403 时，把信号经 ``observer`` 回投给 HttpMCPClient。

    mcp Python SDK（上游 ``mcp/client/streamable_http.py`` ``post_writer`` / ``_handle_post_request``）在
    401/403 时把异常抛进传输任务组、拆连接关 ``read_stream``，导致请求侧（``session.call_tool`` /
    ``on_enter_connected``）**挂起**而非抛异常（上游吞没行为，#133 实证；无已知上游 issue）。故授权
    失败信号须在 mcp 吞掉它**之前**于传输层截获，再经 side-channel 让 ``HttpMCPClient.call_tool``
    自身兜底合成（协议 error-handling.md §可观测判据：已观测授权失败信号但 ``CallToolResult`` 不会经
    原路径返回时，Computer MUST 自身层面兜底）。

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
        # timeout 兜底 30s（mcp ``create_mcp_http_client`` 同款；OAUTH_HTTP_TIMEOUT 对齐面）。
        # #181：``follow_redirects=False`` —— redirect 处理由 ``OAuthGuardTransport`` 手工
        # follow（resource-origin 仅同源、跨 origin stop；httpx 内置 redirect 会把自定义
        # header 跟随到跨 origin，GHSA-9g45-5xwm-f3wc 面）。``transport`` 由调用方注入
        # guard（生产/测试同构；测试传 ``httpx.MockTransport`` 内层即可）。
        kwargs: dict[str, Any] = {"follow_redirects": False, "timeout": timeout or httpx.Timeout(30.0)}
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
        try:
            async with super().stream(method, url, **kwargs) as response:
                if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
                    rpc_id = _parse_jsonrpc_id(response.request.content)
                    www_auth = response.headers.get("www-authenticate")
                    self._observer.capture_auth_signal(
                        rpc_id,
                        response.status_code,
                        www_auth,
                    )
                    # #179：connect-phase 通道（aconnect 的 401/403 challenge 无 rpc_id 关联；
                    # call_tool 路径同时写两条通道互不干扰——connect 槽由 manager 消费一次）。
                    self._observer.capture_connect_signal(
                        response.status_code,
                        www_auth,
                    )
                elif response.extensions.get(CROSS_ORIGIN_REDIRECT_STOP_MARKER) is not None:
                    # #181：安全守卫 stop 的跨 origin redirect（非授权错误，不走 auth 通道）——
                    # mcp 吞掉 3xx 异常致请求侧挂起（#133 同款），经同机制双通道合成 typed error
                    rpc_id = _parse_jsonrpc_id(response.request.content)
                    self._observer.capture_redirect_stopped_signal(rpc_id, response.status_code)
                    self._observer.capture_connect_redirect_stopped(response.status_code)
                yield response
        except OAuthError as exc:
            # mcp ≥1.29 的 403 insufficient_scope inline step-up 在 auth 层吞掉 403 响应并
            # 自动跑授权；coordinator 以 InsufficientScope sentinel 挡回（绕宿主契约 + 无
            # metadata 无从校验）——此处把 auth 层异常重新合成为**等效 403 信号**，走 SDK
            # 既有 4007 分类面（call_tool 竞速取消兜底，与 403 响应截获路径同构）。request
            # 关联键从 kwargs 的 POST 体解析（异常路径拿不到 response.request）。
            if is_stepup_insufficient_scope_error(exc):
                rpc_id = _parse_jsonrpc_id_from_stream_kwargs(kwargs)
                self._observer.capture_auth_signal(rpc_id, httpx.codes.FORBIDDEN, SYNTH_INSUFFICIENT_SCOPE_CHALLENGE)
                self._observer.capture_connect_signal(httpx.codes.FORBIDDEN, SYNTH_INSUFFICIENT_SCOPE_CHALLENGE)
            raise


class _AuthSignalObserver:
    """HttpMCPClient 持有的信号枢纽：watching client 写、``call_tool`` 读（race）。

    per-request（按 JSON-RPC ``id``）的 ``asyncio.Event`` 让 ``call_tool`` 能在信号到达瞬间醒来取消挂起的调用，
    无需任意超时阈值（合法慢调用不受影响）。同一 client 并发多个 ``call_tool`` 各持不同 ``id``，互不串扰。
    """

    def __init__(self) -> None:
        self._signals: dict[object, AuthSignal] = {}
        self._events: dict[object, asyncio.Event] = {}
        # #181：跨 origin redirect stop 信号（非授权错误，独立槽防与 auth 信号混类）
        self._redirect_stops: dict[object, int] = {}
        # #179 connect-phase 槽：aconnect 期间的 401/403 challenge（无 rpc_id 关联）。
        # 与 call_tool 的 per-request 通道正交——stream() 两个通道都写。
        self._connect_signal: AuthSignal | None = None
        self._connect_event: asyncio.Event = asyncio.Event()
        # #181 connect-phase 的 redirect stop 槽（幂等；take 后重置）
        self._connect_redirect_stop: int | None = None
        self._connect_redirect_event: asyncio.Event = asyncio.Event()

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
        self._signals[rpc_id] = AuthSignal(status_code, www_authenticate_header)
        self._events[rpc_id].set()

    def capture_connect_signal(
        self, status_code: int, www_authenticate_header: str | None
    ) -> None:
        """记录首个 connect-phase 授权信号（幂等；``take_connect_signal`` 消费后重置）。"""
        if self._connect_signal is None:
            self._connect_signal = AuthSignal(status_code, www_authenticate_header)
            self._connect_event.set()

    def connect_event(self) -> asyncio.Event:
        """connect-phase challenge 的唤醒事件（manager bounded connect 竞速用）。"""
        return self._connect_event

    def take_connect_signal(self) -> AuthSignal | None:
        """取走并重置 connect-phase 信号（manager 准入判定消费一次）。"""
        signal = self._connect_signal
        self._connect_signal = None
        self._connect_event = asyncio.Event()
        return signal

    def capture_redirect_stopped_signal(self, rpc_id: object, status_code: int) -> None:
        """跨 origin redirect stop：与 auth 信号同通道唤醒（#181 二轮审查 🔴）。"""
        if rpc_id is None or rpc_id not in self._events:
            return
        self._redirect_stops[rpc_id] = status_code
        self._events[rpc_id].set()

    def notify_redirect_stopped(self, status_code: int, request: httpx.Request) -> None:
        """transport 层回调（#181 三轮审查 🔴）：auth 管道内请求不经 ``stream()``
        override——唯一可靠截获点在 transport；双通道写入（per-rpc + connect）。"""
        rpc_id = _parse_jsonrpc_id(request.content)
        self.capture_redirect_stopped_signal(rpc_id, status_code)
        self.capture_connect_redirect_stopped(status_code)

    def take_redirect_stop(self, rpc_id: object) -> int | None:
        return self._redirect_stops.pop(rpc_id, None)

    def capture_connect_redirect_stopped(self, status_code: int) -> None:
        """记录首个 connect-phase redirect stop（幂等；take 后重置）。"""
        if self._connect_redirect_stop is None:
            self._connect_redirect_stop = status_code
            self._connect_redirect_event.set()

    def connect_redirect_event(self) -> asyncio.Event:
        return self._connect_redirect_event

    def take_connect_redirect_stop(self) -> int | None:
        status = self._connect_redirect_stop
        self._connect_redirect_stop = None
        self._connect_redirect_event = asyncio.Event()
        return status

    def take(self, rpc_id: object) -> AuthSignal | None:
        return self._signals.pop(rpc_id, None)

    def discard(self, rpc_id: object) -> None:
        self._events.pop(rpc_id, None)
        self._signals.pop(rpc_id, None)
        self._redirect_stops.pop(rpc_id, None)


class HttpMCPClient(BaseMCPClient[StreamableHttpParameters]):
    def __init__(
        self,
        params: StreamableHttpParameters,
        state_change_callback: Callable[[str, str], None | Awaitable[None]] | None = None,
        message_handler: MessageHandlerFnT | None = None,
        oauth_coordinator: OAuthCoordinator | None = None,
        httpx_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        初始化HTTP客户端，支持传入自定义 message_handler

        Args:
            params: Streamable HTTP 连接参数
            state_change_callback: 状态变更回调
            message_handler: 自定义消息处理器
            oauth_coordinator: 可选 OAuth 协调器（#178），注入后 HTTP 客户端在连接时
                              携带 OAuthClientProvider 处理 Bearer challenge。
            httpx_transport: 仅测试/诊断注入的 httpx transport（``httpx.MockTransport``
                             假 AS 组件测试，沿用 #133 接缝）；生产路径不传（None → 真实网络）。
        """
        assert isinstance(params, StreamableHttpParameters), "params must be an instance of StreamableHttpParameters"
        super().__init__(params, state_change_callback, message_handler)
        self._auth_observer = _AuthSignalObserver()
        self._oauth = oauth_coordinator
        self._httpx_transport = httpx_transport
        # 串行化 call_tool：``_request_id`` 在 ``await async_session`` 之后同步读、但实际自增发生在
        # ``super().call_tool()`` 任务里（ensure_future 调度后才跑 send_request）→ 并发 call_tool 会读到同一 id，
        # 致 observer 注册冲突 + 信号错配（隔离审查 🔴）。单 MCP server 工具调用并发非性能瓶颈，串行化最简且正确。
        self._call_tool_lock = asyncio.Lock()

    # ── #179 connect-phase challenge 通道（manager bounded connect 用） ──────

    def connect_challenge_event(self) -> asyncio.Event:
        """connect-phase 401/403 challenge 的唤醒事件（与 :meth:`aconnect` 竞速）。"""
        return self._auth_observer.connect_event()

    def take_connect_challenge(self) -> AuthSignal | None:
        """取走并重置 connect-phase challenge 信号（manager 准入判定消费一次）。"""
        return self._auth_observer.take_connect_signal()

    def connect_redirect_event(self) -> asyncio.Event:
        """connect-phase 跨 origin redirect stop 的唤醒事件（#181，bounded connect 竞速用）。"""
        return self._auth_observer.connect_redirect_event()

    def take_connect_redirect_stop(self) -> int | None:
        """取走并重置 connect-phase redirect stop 信号。"""
        return self._auth_observer.take_connect_redirect_stop()

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
            # OAuth (#178)：若 coordinator 需要注入 OAuthClientProvider，以它为 auth。
            # 注意：mcp streamablehttp_client 已透传 auth 参数给 httpx_client_factory，
            # 此处确保 coordinator 提供的 provider 覆盖 mcp 层 auth（后者通常为 None）。
            effective_auth = auth
            if self._oauth is not None and self._oauth.needs_oauth_provider():
                effective_auth = self._oauth.build_oauth_provider()
            # #181：安全传输守卫（same-origin redirect + config header 注入面 + OAuth 面
            # 响应体上限）。生产/测试同构：测试注入的 MockTransport 作内层、守卫行为
            # 与生产一致（防假绿）。
            base_transport: httpx.AsyncBaseTransport = self._httpx_transport or httpx.AsyncHTTPTransport()
            guard = OAuthGuardTransport(
                base_transport,
                protected_resource_url=str(self.params.url),
                config_header_names=frozenset(self.params.headers or {}),
                # #181 三轮审查 🔴：auth 管道内请求不经 stream() override——transport
                # 层回调写入 observer 双通道（per-rpc + connect）
                on_redirect_stop=self._auth_observer.notify_redirect_stopped,
            )
            return _AuthWatchingClient(
                observer=self._auth_observer,
                headers=headers,
                timeout=timeout,
                auth=effective_auth,
                transport=guard,
            )

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
        # 串行化：见 ``_call_tool_lock`` 注释（并发 call_tool 的 _request_id 读竞态，隔离审查 🔴）。
        async with self._call_tool_lock:
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
                    # #181：redirect stop 与 auth 信号共用唤醒事件——先判别 redirect 槽
                    # （非授权错误，typed error 走通用失败路径而非 4006/4007）
                    redirect_status = self._auth_observer.take_redirect_stop(request_id)
                    if redirect_status is not None:
                        raise UpstreamRedirectStoppedError(redirect_status)
                    # event.is_set() 保证 signal 已写入（capture 先写信号再 set event）；take 取出。
                    # 防御：理论不达的 None（如 event 被外部误 set）→ 不落到 call_task.result()（call_task 已
                    # cancel，会冒 CancelledError 给上层不被 except Exception 捕获），而是按 §降级语义兜底 4006。
                    signal = self._auth_observer.take(request_id)
                    status = signal.status_code if signal is not None else httpx.codes.UNAUTHORIZED
                    raise UpstreamAuthError(status, signal.www_authenticate_header if signal is not None else None)
                # 调用先完成（正常结果或非授权异常）→ 原样返回/重抛。
                return call_task.result()
            finally:
                event_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await event_task
                self._auth_observer.discard(request_id)
