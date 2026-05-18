# -*- coding: utf-8 -*-
# filename: test_middleware.py
# @Author  : JQQ
# @Software: PyCharm
"""
协议版本握手中间件单元测试 / Unit tests for the handshake middleware

不起真实服务器，直接驱动 ASGI / WSGI 可调用：验证校验判定、4008 header、路径作用域。
No real server: drive the ASGI / WSGI callables directly to verify the verdict, the 4008
header, and path scoping.
"""

import json

import pytest

from a2c_smcp.server.middleware import (
    A2CProtocolVersionASGIMiddleware,
    A2CProtocolVersionWSGIMiddleware,
    check_a2c_version,
)
from a2c_smcp.version import ProtocolVersion

SERVER = ProtocolVersion.parse("0.2.0")


class TestCheckA2CVersion:
    def test_missing(self) -> None:
        assert check_a2c_version("", SERVER) == (400, {"code": 400, "message": "Missing a2c_version query parameter"})

    def test_invalid(self) -> None:
        status, body = check_a2c_version("a2c_version=abc", SERVER)  # type: ignore[misc]
        assert status == 400 and body["code"] == 400 and "Invalid a2c_version" in body["message"]

    def test_incompatible_minor(self) -> None:
        status, body = check_a2c_version("a2c_version=0.3.0", SERVER)  # type: ignore[misc]
        assert status == 400
        assert body == {
            "code": 4008,
            "message": "Protocol version mismatch",
            "server_version": "0.2.0",
            "client_version": "0.3.0",
            "min_supported": "0.2.0",
            "max_supported": "0.2.999",
        }

    def test_compatible_patch_free(self) -> None:
        assert check_a2c_version("a2c_version=0.2.9", SERVER) is None


async def _drive_asgi(app: A2CProtocolVersionASGIMiddleware, scope: dict) -> dict:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await app(scope, receive, send)
    return {"messages": sent}


class TestASGIMiddleware:
    @pytest.fixture
    def downstream_marker(self):
        called: dict = {"hit": False}

        async def app(scope, receive, send) -> None:
            called["hit"] = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"DOWNSTREAM"})

        return app, called

    @pytest.mark.asyncio
    async def test_rejects_incompatible_with_4008_header(self, downstream_marker) -> None:
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        out = await _drive_asgi(
            mw,
            {"type": "http", "path": "/socket.io/", "query_string": b"EIO=4&transport=polling&a2c_version=0.3.0"},
        )
        start = out["messages"][0]
        body = json.loads(out["messages"][1]["body"])
        assert start["status"] == 400
        assert (b"x-a2c-error-code", b"4008") in start["headers"]
        assert body["code"] == 4008 and body["client_version"] == "0.3.0"
        assert called["hit"] is False  # 不得进入下游 socketio handler

    @pytest.mark.asyncio
    async def test_missing_version_no_4008_header(self, downstream_marker) -> None:
        app, _ = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        out = await _drive_asgi(mw, {"type": "http", "path": "/socket.io/", "query_string": b""})
        start = out["messages"][0]
        assert start["status"] == 400
        assert not any(k == b"x-a2c-error-code" for k, _ in start["headers"])  # 400≠4008，无诊断 header

    @pytest.mark.asyncio
    async def test_compatible_passes_through(self, downstream_marker) -> None:
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        out = await _drive_asgi(
            mw, {"type": "http", "path": "/socket.io/", "query_string": b"a2c_version=0.2.5"}
        )
        assert called["hit"] is True
        assert out["messages"][1]["body"] == b"DOWNSTREAM"

    @pytest.mark.asyncio
    async def test_path_scope_non_socketio_untouched(self, downstream_marker) -> None:
        # 协议 §4.6：非 socketio_path 路由不受影响，即使缺 a2c_version 也透传
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        out = await _drive_asgi(mw, {"type": "http", "path": "/health", "query_string": b""})
        assert called["hit"] is True
        assert out["messages"][1]["body"] == b"DOWNSTREAM"

    @pytest.mark.asyncio
    async def test_prefix_pollution_not_matched(self, downstream_marker) -> None:
        # /socket.iofoo 不得被误判为 socketio 前缀
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        await _drive_asgi(mw, {"type": "http", "path": "/socket.iofoo", "query_string": b""})
        assert called["hit"] is True

    @pytest.mark.asyncio
    async def test_non_http_scope_passthrough(self, downstream_marker) -> None:
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")

        async def receive() -> dict:
            return {}

        async def send(msg: dict) -> None:  # pragma: no cover
            pass

        await mw({"type": "websocket", "path": "/socket.io/", "query_string": b""}, receive, send)
        assert called["hit"] is True


class TestWSGIMiddleware:
    def _start_response_collector(self):
        captured: dict = {}

        def start_response(status: str, headers: list) -> None:
            captured["status"] = status
            captured["headers"] = headers

        return start_response, captured

    def test_rejects_incompatible(self) -> None:
        def app(environ, start_response):  # pragma: no cover - 不应被调用
            start_response("200 OK", [])
            return [b"DOWNSTREAM"]

        mw = A2CProtocolVersionWSGIMiddleware(app, socketio_path="/socket.io")
        sr, cap = self._start_response_collector()
        body = b"".join(mw({"PATH_INFO": "/socket.io/", "QUERY_STRING": "a2c_version=0.3.0"}, sr))
        assert cap["status"].startswith("400")
        assert ("x-a2c-error-code", "4008") in cap["headers"]
        assert json.loads(body)["code"] == 4008

    def test_compatible_passes_through(self) -> None:
        def app(environ, start_response):
            start_response("200 OK", [])
            return [b"DOWNSTREAM"]

        mw = A2CProtocolVersionWSGIMiddleware(app, socketio_path="/socket.io")
        sr, _ = self._start_response_collector()
        body = b"".join(mw({"PATH_INFO": "/socket.io/", "QUERY_STRING": "a2c_version=0.2.0"}, sr))
        assert body == b"DOWNSTREAM"

    def test_path_scope_non_socketio_untouched(self) -> None:
        def app(environ, start_response):
            start_response("200 OK", [])
            return [b"DOWNSTREAM"]

        mw = A2CProtocolVersionWSGIMiddleware(app, socketio_path="/socket.io")
        sr, _ = self._start_response_collector()
        body = b"".join(mw({"PATH_INFO": "/health", "QUERY_STRING": ""}, sr))
        assert body == b"DOWNSTREAM"
