# -*- coding: utf-8 -*-
# filename: test_http_auth_error.py
# @Time    : 2026/07/18
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
真实 streamable-http 传输可达性判据（#133 / protocol Discussion #34）——**当前 SKIP，故意保留不删**。

Real streamable-http transport reachability judge (#133 / protocol Discussion #34) — currently SKIPPED on purpose.

⚠️ **为何 SKIP（勿轻易解除）**：本测试起真实 MCP server（握手放行、仅 ``tools/call`` 返 401/403）并经真实
``HttpMCPClient`` + ``manager.acall_tool`` 驱动。**当前会挂起**——mcp Python SDK 在 ``streamable_http.py``
``post_writer``（:412-420）把 ``tools/call`` 的 401/403 抛进传输任务组、拆连接关 ``read_stream``，``call_tool``
收不到 ``HTTPStatusError`` 而**挂起至超时**（反应式在 ``acall_tool`` 分类因此生产不可达）。协议 Discussion #34
裁定：检测点 SDK 自治、python 须**下沉传输层捕获** 401/403；且协议侧将经 ``/add-feature`` 增「授权失败 MUST NOT
挂起至超时」硬条款 + 4 景真实传输 conformance 向量（403 / 401 无 header / 401 带 ``WWW-Authenticate`` / 200+SSE
内 401）。

**解除 SKIP 的前置**：传输层捕获修复落地（call_tool 不再挂起、401/403 产 4006/4007）。届时本骨架转正为回归判据，
并补齐上述 4 景。协议侧会把对应判据写进 conformance 向量。链接：
https://github.com/A2C-SMCP/a2c-smcp-protocol/discussions/34
"""

from __future__ import annotations

import json
import multiprocessing
import socket
import time
from collections.abc import Generator
from datetime import timedelta

import pytest
import uvicorn
from mcp.client.session_group import StreamableHttpParameters
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Mount

from a2c_smcp.computer.mcp_clients.auth_error import META_ERROR_CODE_KEY, META_MCP_SERVER_KEY
from a2c_smcp.computer.mcp_clients.http_client import HttpMCPClient
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import StreamableHttpServerConfig

_AUTH_TOOL_NAME = "needs_auth"

# 前置未满足（传输层捕获未实现，当前会挂起）；解除前须先落修复，见模块 docstring + Discussion #34。
_SKIP_REASON = (
    "reactive auth detection unreachable for streamable-http: mcp SDK swallows 401/403 in post_writer -> "
    "call_tool hangs. Unskip ONLY after the transport-layer-capture fix lands. See protocol Discussion #34."
)

pytestmark = pytest.mark.skip(reason=_SKIP_REASON)


class _AuthTestServer(Server):
    """最小 MCP server：仅暴露一个 ``needs_auth`` 工具（正常不会被调到——tools/call 在 HTTP 层被门拦下）。"""

    def __init__(self) -> None:
        super().__init__("auth-error-test-server")

        @self.list_tools()
        async def _list_tools() -> list[Tool]:
            return [Tool(name=_AUTH_TOOL_NAME, description="needs auth", inputSchema={"type": "object", "properties": {}})]

        @self.call_tool()
        async def _call_tool(name: str, args: dict) -> list[TextContent]:  # pragma: no cover - 被 gate 拦截前不可达
            return [TextContent(type="text", text=f"called {name}")]


class _AuthGateASGI:
    """ASGI 中间件：对 JSON-RPC ``tools/call`` 的 POST 返回指定 HTTP 状态（401/403），模拟上游授权失败；

    其它请求（initialize / notifications / SSE GET / lifespan）原样放行——故 MCP 握手可完成、仅工具调用被拒。
    """

    def __init__(self, app: Starlette, *, deny_status: int) -> None:
        self.app = app
        self.deny_status = deny_status

    async def __call__(self, scope: dict, receive, send) -> None:  # noqa: ANN001 - ASGI 原生签名
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        buffered: list[dict] = []
        body = b""
        while True:
            message = await receive()
            buffered.append(message)
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        method = None
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                method = payload.get("method")
        except Exception:  # noqa: BLE001 - 非 JSON body 直接放行
            method = None
        if method == "tools/call":
            await send({"type": "http.response.start", "status": self.deny_status, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
            return
        index = 0

        async def _replay() -> dict:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, _replay, send)


def _build_gated_app(deny_status: int) -> _AuthGateASGI:
    server = _AuthTestServer()
    security_settings = TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )
    session_manager = StreamableHTTPSessionManager(app=server, json_response=False, security_settings=security_settings)
    inner = Starlette(routes=[Mount("/mcp", app=session_manager.handle_request)], lifespan=lambda app: session_manager.run())  # type: ignore[arg-type,misc]
    return _AuthGateASGI(inner, deny_status=deny_status)


def run_auth_gated_server(port: int, deny_status: int) -> None:
    """多进程目标：起 uvicorn 承载「握手放行、tools/call 拒 deny_status」的 MCP server。"""
    app = _build_gated_app(deny_status)
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning", access_log=False, timeout_keep_alive=5)
    try:
        uvicorn.Server(config).run()
    except Exception:  # pragma: no cover
        import traceback

        traceback.print_exc()


def _pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(params=[(401, 4006), (403, 4007)], ids=["401->4006", "403->4007"])
def auth_gated_server(request: pytest.FixtureRequest) -> Generator[tuple[int, int], None, None]:
    """起一个真实授权门 MCP server（daemon 子进程），yield (port, 期望 error_code)。"""
    deny_status, expected_code = request.param
    port = _pick_free_port()
    proc = multiprocessing.Process(target=run_auth_gated_server, args=(port, deny_status), daemon=True)
    proc.start()
    for _ in range(50):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", port))
            break
        except ConnectionRefusedError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("auth-gated MCP server failed to start")
    try:
        yield port, expected_code
    finally:
        proc.kill()
        proc.join(timeout=3)


@pytest.mark.anyio
async def test_upstream_auth_error_surfaces_via_real_transport(auth_gated_server: tuple[int, int]) -> None:
    """真实 streamable-http：上游对 tools/call 返回 401/403 → manager.acall_tool 应产 4006/4007，meta.mcp_server=bundle_id。

    修复（传输层捕获）落地后解除 SKIP：这条应快速通过而非挂起。协议 conformance 将补齐 4 景（见模块 docstring / #34）。
    """
    port, expected_code = auth_gated_server
    params = StreamableHttpParameters(url=f"http://127.0.0.1:{port}/mcp", timeout=timedelta(seconds=15))
    client = HttpMCPClient(params)
    await client.aconnect()
    try:
        manager = MCPServerManager()
        bundle_id = "gh-bundle"
        manager._servers_config[bundle_id] = StreamableHttpServerConfig(name="github", bundle_id=bundle_id, server_parameters=params)
        manager._active_clients[bundle_id] = client
        result = await manager.acall_tool(bundle_id, _AUTH_TOOL_NAME, {}, timeout=12)
        assert result.isError is True
        assert result.meta is not None
        assert result.meta[META_ERROR_CODE_KEY] == expected_code
        assert result.meta[META_MCP_SERVER_KEY] == bundle_id
    finally:
        await client.adisconnect()
