# -*- coding: utf-8 -*-
"""上游授权错误（4006/4007）真实传输一致性对拍 / real-transport conformance for upstream auth errors.

协议依据（source of truth）：
- ``a2c-smcp-protocol/docs/specification/computer-management/conformance-tests.md`` §4.8：
  双端 MUST 用**真实 MCP server**（initialize 放行、仅 ``tools/call`` 返指定状态）覆盖，禁止以合成
  错误对象充数；挂起判定用 time-box。
- ``a2c-smcp-protocol/docs/specification/fixtures/auth_error_conformance_vectors.json`` 前三景：
  1. desc「403 Forbidden → 4007（已授权但 scope/权限不足）」upstream={403, www_authenticate: null}
  2. desc「401 无 WWW-Authenticate → 4006（凭证缺失/未提供）」upstream={401, www_authenticate: null}
  3. desc「401 带 WWW-Authenticate → 4006（OAuth 合规标准应答；库可能吸收为 AuthRequired 语义变体丢状态码）」
     upstream={401, www_authenticate: 'Bearer realm="mcp"'}
  第 4 景 desc「手写 SSE 客户端：tools/call 返 401 → 4006（rust-sdk sse_client.rs 路径）」python N/A：
  python-sdk 无手写 SSE 客户端路径（streamable_http 由 mcp-python 提供），故不写用例、也不留 skip 占位。

为什么必须真实传输：mcp-python 在 ``post_writer`` 把 ``tools/call`` 的 401/403 抛进传输任务组后拆连接，
``session.call_tool`` 侧会**挂起**；只有真实 transport 才复现该行为，从而验证 SDK 的传输层捕获 + 竞速兜底
（#133）在 wire-level 生效。

起服方式：同事件循环进程内 ``UvicornTestServer``（真 TCP、真 streamable-http 全栈），刻意不用
multiprocessing 子进程——历史 ``cdb1340`` 的子进程方案在全量 pytest 会话下偶发挂起，``c90d710`` 已删除。

反致盲设计：
- ``bundle_id`` / ``name`` / ``tool_name`` 三者互异：``meta.mcp_server`` 必须等于路由 ``bundle_id``，
  若实现误用 display name 会立即变红；
- ASGI 门统计到达的 JSON-RPC method：``tools/call`` 必须恰为 1、``initialize`` 至少为 1——请求没到门
  或握手没穿透时直接红，不靠结果字段「碰巧」为假绿。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import Counter
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from mcp.client.session_group import StreamableHttpParameters
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Mount
from transitions import MachineError

from a2c_smcp.computer.mcp_clients.auth_error import META_ERROR_CODE_KEY, META_MCP_SERVER_KEY
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import StreamableHttpServerConfig
from a2c_smcp.smcp import ErrorCode
from a2c_smcp.testing import UvicornTestServer
from a2c_smcp.utils.bundle_id import resolve_bundle_id

# 反致盲：三者互异，证明 meta.mcp_server 取的是 bundle_id 而非 display name / 工具名。
_TOOL_NAME = "needs_auth"
_BUNDLE_ID = "auth_error_bundle"
_DISPLAY_NAME = "auth-error-display"

# 有界等待（integration job 不带 --timeout，任何无界 await 都可能挂死整套 CI）。
_SERVER_UP_TIMEOUT = 10.0
# manager 内部 bounded connect 上界 30s；测试先于它失败，缩短红灯时间。
_START_TIMEOUT = 15.0
# 向量 expected.hangs=false 的判据：这条 await 必须在 time-box 内返回；生产侧取消后硬等待 ≤3s。
_CALL_TIMEOUT = 10.0
# 优雅停服让 uvicorn 跑完 shutdown(sockets) 与 lifespan shutdown，join session manager task group。
_SERVER_DOWN_TIMEOUT = 15.0
_SERVER_FORCE_DOWN_TIMEOUT = 5.0
# base_client 内部 _TEARDOWN_TIMEOUT=10s，最坏两段共 20s；外层取 20s 不掩盖内部超时上界。
_CLOSE_TIMEOUT = 20.0


def _jsonrpc_method(body: bytes) -> str | None:
    """从 POST body 解析 JSON-RPC ``method``（非 JSON / 非 dict / 无 method 均返回 None）。"""
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("method"), str):
        return payload["method"]
    return None


class _AuthTestServer(Server):
    """最小 MCP server：仅暴露 ``needs_auth`` 工具；``tools/call`` 在 HTTP 层被门拦下，正常不可达。"""

    def __init__(self) -> None:
        super().__init__("auth-error-test-server")

        @self.list_tools()
        async def _list_tools() -> list[Tool]:
            return [Tool(name=_TOOL_NAME, description="needs auth", inputSchema={"type": "object", "properties": {}})]

        @self.call_tool()
        async def _call_tool(name: str, args: dict) -> list[TextContent]:  # pragma: no cover - 被 gate 拦截前不可达
            return [TextContent(type="text", text=f"called {name}")]


class _AuthGateASGI:
    """ASGI 门：仅对 JSON-RPC ``tools/call`` 的 POST 返回指定状态，其余请求（lifespan / GET SSE / 其它方法）原样透传。

    这样 MCP 握手（initialize / notifications / tools/list）可正常完成，只有工具调用被拒——精确复刻协议
    ``fixture_setup.real_server_contract``。
    """

    def __init__(self, app: Any, *, deny_status: int, www_authenticate: str | None) -> None:
        self.app = app
        self.deny_status = deny_status
        self.www_authenticate = www_authenticate
        self.method_hits: Counter[str] = Counter()
        # 实际发出的拒绝响应头（小写名 → 值）：用于断言「401 带 header」确有其 header，
        # 避免景 3 与景 2 在断言面不可区分（仅靠 scenario 配置自证）。
        self.denied_headers: dict[str, str] = {}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:  # noqa: ANN001 - ASGI 原生签名
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        # 缓冲全部 http.request 消息后逐条原样重放：拼接 body 伪装成单条消息会破坏 more_body 语义。
        buffered: list[dict[str, Any]] = []
        body = b""
        while True:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        method = _jsonrpc_method(body)
        if method is not None:
            self.method_hits[method] += 1

        if method == "tools/call":
            headers = [(b"content-type", b"application/json")]
            # 景 1/2 向量 upstream.www_authenticate = null ⇒ 完全不发该头（不是发空值头）。
            if self.www_authenticate is not None:
                headers.append((b"www-authenticate", self.www_authenticate.encode("ascii")))
            self.denied_headers = {name.decode("ascii").lower(): value.decode("ascii") for name, value in headers}
            await send({"type": "http.response.start", "status": self.deny_status, "headers": headers})
            await send({"type": "http.response.body", "body": b'{"error":"upstream authorization failed"}'})
            return

        index = 0

        async def _replay() -> dict[str, Any]:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, _replay, send)


def _build_gated_app(deny_status: int, www_authenticate: str | None) -> _AuthGateASGI:
    """构建「握手放行、仅 tools/call 拒 deny_status」的真实 MCP ASGI app。

    每次调用都新建 ``StreamableHTTPSessionManager``——其 ``run()`` 每实例只能执行一次。
    """
    server = _AuthTestServer()
    security_settings = TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        security_settings=security_settings,
    )
    inner = Starlette(
        routes=[Mount("/mcp", app=session_manager.handle_request)],
        lifespan=lambda app: session_manager.run(),  # type: ignore[arg-type,return-value]
    )
    return _AuthGateASGI(inner, deny_status=deny_status, www_authenticate=www_authenticate)


@dataclass(frozen=True)
class _Scenario:
    """一景的 upstream 行为与向量期望（desc 见模块 docstring）。"""

    deny_status: int
    www_authenticate: str | None
    expected_error_code: ErrorCode


_SCENARIOS = [
    pytest.param(
        _Scenario(403, None, ErrorCode.TOOL_AUTHORIZATION_FAILED),
        id="403-no-www-authenticate-4007",
    ),
    pytest.param(
        _Scenario(401, None, ErrorCode.TOOL_AUTHORIZATION_REQUIRED),
        id="401-no-www-authenticate-4006",
    ),
    pytest.param(
        _Scenario(401, 'Bearer realm="mcp"', ErrorCode.TOOL_AUTHORIZATION_REQUIRED),
        id="401-www-authenticate-4006",
    ),
]


@dataclass(frozen=True)
class _GatedServer:
    """参数化夹具交出：URL、门（计数可读）、本景期望。"""

    url: str
    gate: _AuthGateASGI
    scenario: _Scenario


@pytest.fixture(params=_SCENARIOS)
async def gated_auth_server(request: pytest.FixtureRequest, basic_server_port: int) -> AsyncGenerator[_GatedServer, None]:
    """每个参数景各起一个进程内真实 uvicorn（ephemeral port），用例结束前有界停服。"""
    scenario: _Scenario = request.param
    app = _build_gated_app(scenario.deny_status, scenario.www_authenticate)
    server = UvicornTestServer(app, port=basic_server_port)
    try:
        await asyncio.wait_for(server.up(), timeout=_SERVER_UP_TIMEOUT)
    except BaseException:
        # up() 失败也要回收 serve task，避免遗留后台任务。
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.down(force=True), timeout=_SERVER_FORCE_DOWN_TIMEOUT)
        raise
    try:
        # 带尾斜杠：避免 Starlette ``Mount("/mcp")`` 对 ``/mcp`` 发 307 → ``/mcp/``（会让同一
        # JSON-RPC 请求在门里被统计两次，也让用例多一跳）。
        yield _GatedServer(
            url=f"http://127.0.0.1:{basic_server_port}/mcp/",
            gate=app,
            scenario=scenario,
        )
    finally:
        try:
            # 先优雅：让 uvicorn 跑完 shutdown(sockets) 与 lifespan shutdown，join session manager task group。
            await asyncio.wait_for(server.down(force=False), timeout=_SERVER_DOWN_TIMEOUT)
        except BaseException:
            # BaseException：用例被取消（CI 中断）时也要尝试强制停服，避免残留绑定端口。
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.down(force=True), timeout=_SERVER_FORCE_DOWN_TIMEOUT)


async def _bounded_teardown(manager: MCPServerManager, client: Any) -> None:
    """有界拆除 manager，并回收 keep-alive 任务异常（防止 "Task exception was never retrieved" 噪声）。

    只容忍已知的 ``MachineError``，其真实因果链（实测）：401/403 令 mcp 传输任务组失败 → anyio **取消宿主
    keep-alive 任务** → ``base_client._keep_alive_task`` 的 ``except asyncio.CancelledError`` 吞掉取消 →
    其 ``finally`` 中 ``_aexit_stack.aclose()`` 抛 ``ExceptionGroup`` 并置 ``_async_session=None`` /
    ``_async_session_closed_event``——状态机**从未转移**，仍停在 ``connected``。随后 ``adisconnect`` 的条件
    回调经 ``async_session`` 惰性属性走 ``aconnect``，抛
    ``MachineError("Can't trigger event aconnect from state connected!")``，``aclose`` 的 ``_clear_all()``
    因此被跳过。

    ⚠️ 这是**任何传输层失败**都可能触发的生产既有缺陷（非 #231 范围；需另建 follow-up Issue），本用例只记录
    不修生产代码。其它清理异常（尤其 time-box 超时）与措辞不符的 MachineError 照旧上抛。
    """
    try:
        await asyncio.wait_for(manager.aclose(), timeout=_CLOSE_TIMEOUT)
    except MachineError as exc:
        message = str(exc)
        if "aconnect" not in message or "state connected" not in message:
            raise AssertionError(f"unexpected MachineError during teardown: {exc}") from exc
    finally:
        keep_alive = getattr(client, "_session_keep_alive_task", None) if client is not None else None
        if keep_alive is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(keep_alive), timeout=1.0)
    # 容忍态边界：上述已知缺陷只允许跳过 _clear_all，client 必须已被 aclose 出册。
    if manager._active_clients:
        raise AssertionError(f"manager.aclose() left active clients: {list(manager._active_clients)}")


@pytest.mark.asyncio
async def test_upstream_auth_error_over_real_streamable_http(gated_auth_server: _GatedServer) -> None:
    """三景共用：真实传输下 4006/4007 surfacing + 同用例 hangs=false 断言。"""
    scenario = gated_auth_server.scenario
    config = StreamableHttpServerConfig(
        name=_DISPLAY_NAME,
        bundle_id=_BUNDLE_ID,
        server_parameters=StreamableHttpParameters(
            url=gated_auth_server.url,
            timeout=timedelta(seconds=20),
        ),
        oauth=None,
    )
    assert resolve_bundle_id(config) == _BUNDLE_ID

    manager = MCPServerManager(auto_connect=False)
    primary_error: BaseException | None = None
    try:
        await manager.ainitialize([config])
        # initialize + tools/list 必须穿透门到达真实 MCP server。
        await asyncio.wait_for(manager.astart_client(_BUNDLE_ID), timeout=_START_TIMEOUT)
        # 走线上入口 aexecute_tool（经 ExposedToolMapping 把暴露名解析回 bundle_id + 原始名），
        # 避免直接传 bundle_id 令 meta.mcp_server 断言自证。
        # 向量 expected.hangs=false：这条 await 必须在 time-box 内返回；挂起即 TimeoutError 判红。
        result = await asyncio.wait_for(
            manager.aexecute_tool(f"{_BUNDLE_ID}__{_TOOL_NAME}", {}),
            timeout=_CALL_TIMEOUT,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        # aclose 会先把 client 出册，故在调用前取引用供 keep-alive 任务回收。
        client = manager._active_clients.get(_BUNDLE_ID)
        try:
            await _bounded_teardown(manager, client)
        except BaseException:
            # 主断言已失败时，清理异常不得顶替原始失败（否则对端只看到 teardown 错误）。
            if primary_error is None:
                raise

    # 反致盲：请求确实到达门，且握手确实穿透（不是「请求没发出」或「initialize 被误伤」）。
    assert gated_auth_server.gate.method_hits["tools/call"] == 1
    assert gated_auth_server.gate.method_hits["initialize"] >= 1
    # 「401 无 header / 带 header」两景必须可区分：断言拒绝响应**实际发出**的 header（删掉发送分支即红）。
    assert gated_auth_server.gate.denied_headers.get("www-authenticate") == scenario.www_authenticate

    assert result.isError is True
    assert result.meta is not None
    assert result.meta[META_ERROR_CODE_KEY] == int(scenario.expected_error_code)
    assert result.meta[META_MCP_SERVER_KEY] == _BUNDLE_ID
    assert result.meta[META_MCP_SERVER_KEY] != config.name


# 尾注 / Tail note：协议向量第 4 景「手写 SSE 客户端：tools/call 返 401」python N/A——python-sdk 无手写 SSE
# 客户端路径（streamable_http 由 mcp-python 提供）；另 POST 200 + SSE 流内 401 不在初始响应上，mcp-python 以
# 「流静默终止」呈现而非可截获的 401（见 ``http_client.py`` ``_AuthWatchingClient`` 覆盖范围注释 + 协议 #35）。
