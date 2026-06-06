# -*- coding: utf-8 -*-
# filename: middleware.py
# @Author  : JQQ
# @Software: PyCharm

"""
协议版本握手中间件 / Protocol version handshake middleware

在 Socket.IO 业务 handler **之前**于 HTTP 传输层校验 URL query 中的 ``a2c_version``，
不兼容时直接返回 ``HTTP 400``，请求根本进不了 Socket.IO ``connect`` handler——即使 handler
有 bug 也无法绕过校验（协议唯一规范的时机硬约束）。
Validates the ``a2c_version`` URL query at the HTTP transport layer **before** any Socket.IO
business handler. Incompatible requests get ``HTTP 400`` and never reach the Socket.IO
``connect`` handler — bypass is impossible even if the handler is buggy (the protocol's only
normative timing constraint).

设计要点 / Design notes:
  - 纯 ASGI / WSGI 原生协议实现，**不依赖** ``socketio.AsyncServer`` 内部 hook
  - 路径作用域 **MUST** 仅命中 socketio HTTP 挂载前缀，避免误伤同一应用上的其它路由
  - ASGI 下 **MUST** 同时校验 ``http``（polling 握手）与 ``websocket``（直连 WS 握手）两类
    scope——只校验 ``http`` 会让 ``transports=["websocket"]`` 客户端绕过版本闸门、击穿
    传递性保证（versioning.md §5）。WS-only 不兼容拒绝形态（§5 R3，按运行栈能力二选一）：
      * 栈支持 ASGI WebSocket Denial Response（uvicorn≥0.21 / hypercorn / daphne）→ 回与
        polling 路径**字节一致**的 HTTP 400 + 4008 flat body + ``X-A2C-Error-Code: 4008``
      * 不支持 → 以 WebSocket close code ``4900`` 关闭握手（**非** 4008，不同命名空间）
  - WSGI 不拆分 http / websocket scope（WS 握手起始即普通 HTTP GET，``environ`` 携
    ``QUERY_STRING`` 与 Upgrade 头），现有逻辑对 Upgrade 无关、仅看 PATH_INFO+QUERY_STRING，
    故 WS-only over WSGI 天然走同一 400/4008 路径——该缺口本质 **ASGI 特有**
  - Pure ASGI / WSGI native protocol; does NOT depend on ``socketio.AsyncServer`` internals
  - Path scope MUST only match the socketio HTTP mount prefix (no collateral damage)
  - ASGI MUST validate BOTH ``http`` and ``websocket`` scopes (versioning.md §5);
    WSGI inherently covers WS-only since it does not split scopes

客户端已知限制 / Client-side known limitation (versioning.md §5 R4):
  收到 WS close ``4900`` 后「改 polling 重连取回权威 4008」受限于 python-socketio 暴露 ws
  close code 的能力；当前 SDK 在客户端侧以 §1 polling-first 护栏（见 utils/handshake.py
  ``enforce_polling_first``）从源头规避 ``4900``，R4 自动重连取回 4008 暂不实现。

协议依据 / Protocol: a2c-smcp-protocol docs/specification/versioning.md (§连接握手流程 / §5 / §错误码)
                      docs/specification/error-handling.md (§协议版本不匹配 4008)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import parse_qs

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.smcp import WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE, ErrorCode
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.version import ProtocolVersion, is_compatible

logger = get_logger("server")

# python-socketio ``ASGIApp`` / ``WSGIApp`` 默认 HTTP 挂载路径 / default HTTP mount path
DEFAULT_SOCKETIO_PATH = "/socket.io"

_PROTOCOL_VERSION_MISMATCH = int(ErrorCode.PROTOCOL_VERSION_MISMATCH)  # 4008


def _normalize_prefix(socketio_path: str) -> str:
    """归一化 socketio HTTP 路径前缀为 ``/xxx`` 形式 / Normalize the prefix to ``/xxx``."""
    return "/" + socketio_path.strip("/")


def _path_in_scope(path: str, prefix: str) -> bool:
    """
    仅当 ``path`` 命中 socketio HTTP 挂载前缀时才校验，其它路由透传。
    Only validate when ``path`` hits the socketio HTTP mount prefix; pass through otherwise.

    用 ``== prefix`` 或 ``startswith(prefix + "/")`` 精确匹配，避免 ``/socket.iofoo``
    这类前缀污染。Exact match via ``== prefix`` or ``startswith(prefix + "/")`` to avoid
    prefix pollution like ``/socket.iofoo``.
    """
    return path == prefix or path.startswith(prefix + "/")


def _mismatch_body(client: ProtocolVersion, server: ProtocolVersion) -> dict[str, Any]:
    """
    构造 4008 flat ErrorPayload（顶层四字段对齐 error-handling.md §标准字段总表）。
    Build the 4008 flat ErrorPayload (four top-level fields per error-handling.md).

    v0.x 严格匹配 MINOR，故 Server 支持区间即同 MAJOR.MINOR 的整个 PATCH 段。
    v0.x strictly matches MINOR, so the supported range is the whole PATCH band of the
    same MAJOR.MINOR.
    """
    return {
        "code": _PROTOCOL_VERSION_MISMATCH,
        "message": "Protocol version mismatch",
        "server_version": str(server),
        "client_version": str(client),
        "min_supported": f"{server.major}.{server.minor}.0",
        "max_supported": f"{server.major}.{server.minor}.999",
    }


def check_a2c_version(query_string: str, server: ProtocolVersion) -> tuple[int, dict[str, Any]] | None:
    """
    校验 query 中的 ``a2c_version``。兼容返回 ``None``（透传）；否则返回 ``(status, body)``。
    Validate ``a2c_version`` in the query. Compatible → ``None`` (pass through); otherwise
    ``(status, body)``.

    - 缺失 → ``400 {"code": 400, "message": "Missing a2c_version query parameter"}``
    - 非法 SemVer → ``400 {"code": 400, "message": "Invalid a2c_version: ..."}``
    - 不兼容 → ``400`` + 4008 flat ErrorPayload（调用方据 ``code==4008`` 决定是否补
      ``X-A2C-Error-Code`` header）
    """
    raw_values = parse_qs(query_string).get("a2c_version")
    raw = raw_values[0] if raw_values else None
    if not raw:
        return 400, {"code": 400, "message": "Missing a2c_version query parameter"}
    try:
        client = ProtocolVersion.parse(raw)
    except ValueError as e:
        return 400, {"code": 400, "message": f"Invalid a2c_version: {e}"}
    if not is_compatible(client, server):
        return 400, _mismatch_body(client, server)
    return None


def _coerce_server_version(server_version: str | ProtocolVersion) -> ProtocolVersion:
    return server_version if isinstance(server_version, ProtocolVersion) else ProtocolVersion.parse(server_version)


def _build_rejection(body: dict[str, Any]) -> tuple[bytes, list[tuple[str, str]]]:
    """
    构造拒绝响应的 ``(raw_body_bytes, str_headers)``——**ASGI http / ASGI websocket
    denial-response / WSGI 三路唯一事实源**，保证 4008 响应字节一致。
    Single source of truth for ASGI-http / ASGI-ws-denial-response / WSGI rejection
    responses, guaranteeing a byte-identical 4008 response across all three.

    **含 ``Content-Length``**：HTTP/1.1 响应若无 Content-Length 且非分块，则 body 仅靠
    连接关闭定界——高负载/插桩下客户端读 body 与服务端关连接竞态时，aiohttp 会抛裸
    ``RuntimeError("Connection closed.")`` 而非携 4008 的 ``ConnectionError``，使
    versioning.md 传递性保证（被拒方 MUST 得知版本不匹配）在压力下失效。显式定界后
    客户端精确读 N 字节、干净 EOF，4008 body 确定性可达。
    Includes ``Content-Length`` so the response is explicitly framed; otherwise the
    HTTP/1.1 body is delimited by connection close and a read/close race surfaces a
    bare ``RuntimeError`` instead of the 4008-bearing ``ConnectionError``.

    头以 ``str`` 返回（WSGI 原生即 str）；ASGI 侧经 :func:`_encode_headers` 转 bytes。
    4008 附冗余诊断头 ``X-A2C-Error-Code``。
    """
    raw = json.dumps(body).encode("utf-8")
    headers: list[tuple[str, str]] = [
        ("content-type", "application/json"),
        ("content-length", str(len(raw))),
    ]
    if body.get("code") == _PROTOCOL_VERSION_MISMATCH:
        headers.append(("x-a2c-error-code", str(_PROTOCOL_VERSION_MISMATCH)))
    return raw, headers


def _encode_headers(headers: list[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    """ASGI 头需 bytes；WSGI 直接用 str —— 这是共享 :func:`_build_rejection` 之后两栈唯一差异点。"""
    return [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]


class A2CProtocolVersionASGIMiddleware:
    """
    ASGI 协议版本握手中间件。包裹 ``socketio.ASGIApp``：
    ASGI protocol version handshake middleware. Wrap it around ``socketio.ASGIApp``::

        app = A2CProtocolVersionASGIMiddleware(socketio.ASGIApp(sio, socketio_path="/socket.io"))

    Args:
        app: 下游 ASGI 应用 / downstream ASGI app
        socketio_path: socketio HTTP 挂载路径，**MUST** 与 ``ASGIApp(socketio_path=...)``
            一致 / must match ``ASGIApp(socketio_path=...)``
        server_version: Server 实现的协议版本，默认 SDK ``PROTOCOL_VERSION`` 常量
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[Any]],
        *,
        socketio_path: str = DEFAULT_SOCKETIO_PATH,
        server_version: str | ProtocolVersion = PROTOCOL_VERSION,
    ) -> None:
        self.app = app
        self._prefix = _normalize_prefix(socketio_path)
        self._server = _coerce_server_version(server_version)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        # 协议 §5 MUST：http（polling 握手）与 websocket（直连 WS 握手）两类 scope 均须校验；
        # 其它 scope（lifespan 等）与非 socketio 前缀路由原样透传
        # Protocol §5 MUST: validate BOTH http and websocket scopes; pass everything else through
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket") or not _path_in_scope(scope.get("path", ""), self._prefix):
            await self.app(scope, receive, send)
            return

        query_string = scope.get("query_string", b"")
        if isinstance(query_string, bytes):
            query_string = query_string.decode("latin-1")
        verdict = check_a2c_version(query_string, self._server)
        if verdict is None:
            # 兼容（含 WS-only 兼容）→ 校验后放行 / compatible (incl. WS-only) → pass through
            await self.app(scope, receive, send)
            return

        status, body = verdict
        logger.warning(f"A2C handshake rejected: scope={scope_type} status={status} body={body}")
        if scope_type == "http":
            await self._reject_http(send, status, body)
        else:
            await self._reject_websocket(scope, receive, send, status, body)

    @staticmethod
    async def _reject_http(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
        body: dict[str, Any],
    ) -> None:
        """polling 握手拒绝：HTTP 400 + flat ErrorPayload（含 Content-Length，确定性定界）。"""
        raw, headers = _build_rejection(body)
        await send({"type": "http.response.start", "status": status, "headers": _encode_headers(headers)})
        await send({"type": "http.response.body", "body": raw})

    @staticmethod
    async def _reject_websocket(
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
        body: dict[str, Any],
    ) -> None:
        """
        直连 WS 握手拒绝（versioning.md §5 R3，按运行栈能力二选一）：
        WS-only handshake rejection (versioning.md §5 R3, one of two by stack capability):

        - 支持 ASGI WebSocket Denial Response（uvicorn≥0.21 / hypercorn / daphne）→ 在 WS
          打开**之前**回与 polling 路径**字节一致**的 HTTP 400 + 4008 body + 同一 header
        - 不支持 → 以 WebSocket close code ``4900`` 关闭（独立命名空间，非 4008）

        ASGI 规范要求 denial-response 在收到 ``websocket.connect`` **之后**发出，故先消费
        一次 receive；非 connect 事件（如 disconnect）直接返回。
        """
        event = await receive()
        if event.get("type") != "websocket.connect":
            return
        if "websocket.http.response" in (scope.get("extensions") or {}):
            raw, headers = _build_rejection(body)
            await send({"type": "websocket.http.response.start", "status": status, "headers": _encode_headers(headers)})
            await send({"type": "websocket.http.response.body", "body": raw})
        else:
            await send({"type": "websocket.close", "code": WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE})


class A2CProtocolVersionWSGIMiddleware:
    """
    WSGI 协议版本握手中间件（同步 Server 镜像）。包裹 ``socketio.WSGIApp``：
    WSGI protocol version handshake middleware (sync-Server mirror). Wrap ``socketio.WSGIApp``::

        app = A2CProtocolVersionWSGIMiddleware(socketio.WSGIApp(sio, socketio_path="/socket.io"))

    参数语义与 :class:`A2CProtocolVersionASGIMiddleware` 一致。
    Parameter semantics identical to :class:`A2CProtocolVersionASGIMiddleware`.
    """

    def __init__(
        self,
        app: Callable[..., Iterable[bytes]],
        *,
        socketio_path: str = DEFAULT_SOCKETIO_PATH,
        server_version: str | ProtocolVersion = PROTOCOL_VERSION,
    ) -> None:
        self.app = app
        self._prefix = _normalize_prefix(socketio_path)
        self._server = _coerce_server_version(server_version)

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Callable[[str, list[tuple[str, str]]], Any],
    ) -> Iterable[bytes]:
        if not _path_in_scope(environ.get("PATH_INFO", ""), self._prefix):
            return self.app(environ, start_response)

        verdict = check_a2c_version(environ.get("QUERY_STRING", ""), self._server)
        if verdict is None:
            return self.app(environ, start_response)

        status, body = verdict
        logger.warning(f"A2C handshake rejected: status={status} body={body}")
        # 与 ASGI 两路共用 _build_rejection（含 Content-Length）；WSGI 原生用 str 头
        raw, headers = _build_rejection(body)
        start_response(f"{status} Bad Request", headers)
        return [raw]
