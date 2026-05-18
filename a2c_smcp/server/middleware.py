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
  - 仅处理 HTTP 传输（polling 握手）。默认 transports polling 优先，首个握手必为 HTTP，
    故 WS-only 边角不在本中间件职责内——与协议 Python 参考实现（Starlette
    ``BaseHTTPMiddleware``，亦仅 HTTP）的作用域一致
  - Pure ASGI / WSGI native protocol; does NOT depend on ``socketio.AsyncServer`` internals
  - Path scope MUST only match the socketio HTTP mount prefix (no collateral damage)
  - HTTP transport only (polling handshake); aligned with the protocol's Python reference
    impl (Starlette ``BaseHTTPMiddleware``, also HTTP-only)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/versioning.md (§连接握手流程 / §错误码)
                      docs/specification/error-handling.md (§协议版本不匹配 4008)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import parse_qs

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.smcp import ErrorCode
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


def _response_headers_ascii(body: dict[str, Any]) -> list[tuple[str, str]]:
    """4008 时附加冗余诊断 header ``X-A2C-Error-Code: 4008`` / add redundant diag header on 4008."""
    headers = [("content-type", "application/json")]
    if body.get("code") == _PROTOCOL_VERSION_MISMATCH:
        headers.append(("x-a2c-error-code", str(_PROTOCOL_VERSION_MISMATCH)))
    return headers


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
        # 仅校验命中 socketio 前缀的 HTTP 握手；其它 scope / 路由原样透传
        # Only validate HTTP handshakes on the socketio prefix; pass everything else through
        if scope.get("type") != "http" or not _path_in_scope(scope.get("path", ""), self._prefix):
            await self.app(scope, receive, send)
            return

        query_string = scope.get("query_string", b"")
        if isinstance(query_string, bytes):
            query_string = query_string.decode("latin-1")
        verdict = check_a2c_version(query_string, self._server)
        if verdict is None:
            await self.app(scope, receive, send)
            return

        status, body = verdict
        logger.warning(f"A2C handshake rejected: status={status} body={body}")
        raw = json.dumps(body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in _response_headers_ascii(body)],
            }
        )
        await send({"type": "http.response.body", "body": raw})


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
        raw = json.dumps(body).encode("utf-8")
        headers = _response_headers_ascii(body)
        headers.append(("content-length", str(len(raw))))
        start_response(f"{status} Bad Request", headers)
        return [raw]
