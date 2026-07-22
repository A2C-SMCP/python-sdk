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
from tests.protocol_versions import COMPATIBLE_PEER, INCOMPATIBLE_PEER

# 注：``SERVER`` / ``TestCheckA2CVersion`` 用固定 0.2.0 输入验证 check_a2c_version 的判定**逻辑**，
# 不耦合 SDK ``PROTOCOL_VERSION``，无需随协议升级改动。
# 而下方 ASGI/WSGI 中间件用例走 **默认 server_version(=PROTOCOL_VERSION)**，故其兼容/不兼容 client
# 版本从 ``COMPATIBLE_PEER`` / ``INCOMPATIBLE_PEER`` 派生（不硬编码具体协议版本值）。
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


async def _drive_asgi(
    app: A2CProtocolVersionASGIMiddleware,
    scope: dict,
    recv_events: list[dict] | None = None,
) -> dict:
    """驱动 ASGI 可调用；``recv_events`` 为 receive() 依次返回的事件队列（WS 路径用）。"""
    sent: list[dict] = []
    queue = list(recv_events or [{"type": "http.request"}])

    async def receive() -> dict:
        return queue.pop(0) if queue else {"type": "websocket.disconnect"}

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
            {"type": "http", "path": "/socket.io/", "query_string": f"EIO=4&transport=polling&a2c_version={INCOMPATIBLE_PEER}".encode()},
        )
        start = out["messages"][0]
        body = json.loads(out["messages"][1]["body"])
        assert start["status"] == 400
        assert (b"x-a2c-error-code", b"4008") in start["headers"]
        assert body["code"] == 4008 and body["client_version"] == INCOMPATIBLE_PEER
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
            mw, {"type": "http", "path": "/socket.io/", "query_string": f"a2c_version={COMPATIBLE_PEER}".encode()}
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
    async def test_ws_scope_incompatible_rejected(self, downstream_marker) -> None:
        # versioning.md §测试建议 row 655 反例回归：WS-only 不兼容 MUST 被拒，**不得**透传下游
        # （此前 buggy 实现 `scope!=http → passthrough` 会让此连接被错误放行）
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        out = await _drive_asgi(
            mw,
            {
                "type": "websocket",
                "path": "/socket.io/",
                "query_string": f"a2c_version={INCOMPATIBLE_PEER}".encode(),
                "extensions": {"websocket.http.response": {}},
            },
            recv_events=[{"type": "websocket.connect"}],
        )
        assert called["hit"] is False, "WS-only 不兼容握手绝不能进入下游 socketio handler"
        assert out["messages"], "中间件 MUST 主动拒绝，而非静默放行"

    @pytest.mark.asyncio
    async def test_ws_denial_response_byte_identical_4008(self, downstream_marker) -> None:
        # §5 R3 上行：栈支持 WebSocket Denial Response → 与 polling 路径字节一致的 4008 + header
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        ws_out = await _drive_asgi(
            mw,
            {
                "type": "websocket",
                "path": "/socket.io/",
                "query_string": f"a2c_version={INCOMPATIBLE_PEER}".encode(),
                "extensions": {"websocket.http.response": {}},
            },
            recv_events=[{"type": "websocket.connect"}],
        )
        http_out = await _drive_asgi(
            mw, {"type": "http", "path": "/socket.io/", "query_string": f"a2c_version={INCOMPATIBLE_PEER}".encode()}
        )
        ws_start, ws_body = ws_out["messages"][0], ws_out["messages"][1]
        http_start, http_body = http_out["messages"][0], http_out["messages"][1]
        assert ws_start["type"] == "websocket.http.response.start"
        assert ws_body["type"] == "websocket.http.response.body"
        assert ws_start["status"] == http_start["status"] == 400
        # 字节一致：body 与 polling 路径完全相同（4008 单一事实源）
        assert ws_body["body"] == http_body["body"]
        assert json.loads(ws_body["body"])["code"] == 4008
        assert (b"x-a2c-error-code", b"4008") in ws_start["headers"]
        assert ws_start["headers"] == http_start["headers"]
        # Group A：显式 Content-Length（确定性定界，修 🟡6 单一事实源）
        cl = dict(http_start["headers"]).get(b"content-length")
        assert cl == str(len(http_body["body"])).encode()
        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_ws_no_denial_response_closes_4900(self, downstream_marker) -> None:
        # §5 R3 下行：栈不支持 denial-response → websocket.close code 4900（非 4008）
        from a2c_smcp.smcp import WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE

        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        out = await _drive_asgi(
            mw,
            {"type": "websocket", "path": "/socket.io/", "query_string": f"a2c_version={INCOMPATIBLE_PEER}".encode()},
            recv_events=[{"type": "websocket.connect"}],
        )
        close = out["messages"][-1]
        assert close["type"] == "websocket.close"
        assert close["code"] == WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE == 4900
        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_reject_websocket_non_connect_first_event_early_return(self, downstream_marker) -> None:
        # ③ 覆盖 _reject_websocket 首事件 ≠ websocket.connect 的提前返回分支：
        # 既不下发拒绝响应也不触下游（ASGI 规范要求 denial-response 在 connect 后发出）。
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        out = await _drive_asgi(
            mw,
            {
                "type": "websocket",
                "path": "/socket.io/",
                "query_string": f"a2c_version={INCOMPATIBLE_PEER}".encode(),
                "extensions": {"websocket.http.response": {}},
            },
            recv_events=[{"type": "websocket.disconnect"}],  # 首事件非 connect
        )
        assert out["messages"] == [], "首事件非 websocket.connect → 早返回，不得 send 任何消息"
        assert called["hit"] is False, "早返回也绝不透传下游"

    @pytest.mark.asyncio
    async def test_ws_scope_compatible_passes_through(self, downstream_marker) -> None:
        # §测试建议 row 652：WS-only 兼容 → 校验后放行（透传下游）
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        await _drive_asgi(
            mw,
            {"type": "websocket", "path": "/socket.io/", "query_string": f"a2c_version={COMPATIBLE_PEER}".encode()},
            recv_events=[{"type": "websocket.connect"}],
        )
        assert called["hit"] is True

    @pytest.mark.asyncio
    async def test_ws_scope_non_socketio_untouched(self, downstream_marker) -> None:
        # 路径作用域同样适用 websocket scope：非 socketio 前缀透传
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        await _drive_asgi(
            mw,
            {"type": "websocket", "path": "/health", "query_string": b""},
            recv_events=[{"type": "websocket.connect"}],
        )
        assert called["hit"] is True

    @pytest.mark.asyncio
    async def test_lifespan_scope_passthrough(self, downstream_marker) -> None:
        # 非 http/websocket scope（lifespan 等）仍透传
        app, called = downstream_marker
        mw = A2CProtocolVersionASGIMiddleware(app, socketio_path="/socket.io")
        await _drive_asgi(mw, {"type": "lifespan", "path": "", "query_string": b""})
        assert called["hit"] is True


def test_ws_close_code_namespace_isolation() -> None:
    # §4900：4900 是 WS close code，与 ErrorCode.PROTOCOL_VERSION_MISMATCH(4008) 不同命名空间不同值
    from a2c_smcp.smcp import WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE, ErrorCode

    assert WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE == 4900
    assert WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE != int(ErrorCode.PROTOCOL_VERSION_MISMATCH)
    assert WS_VERSION_HANDSHAKE_REJECTED_CLOSE_CODE not in {int(c) for c in ErrorCode}


def test_build_rejection_single_source_of_truth() -> None:
    # Group A / 🟡6：_build_rejection 是 ASGI-http / ASGI-ws-denial / WSGI 三路唯一事实源，
    # 必含 Content-Length（显式定界），4008 必含 X-A2C-Error-Code。
    from a2c_smcp.server.middleware import _build_rejection, _encode_headers

    body = {
        "code": 4008,
        "message": "Protocol version mismatch",
        "server_version": "0.2.0",
        "client_version": "0.3.0",
        "min_supported": "0.2.0",
        "max_supported": "0.2.999",
    }
    raw, headers = _build_rejection(body)
    hd = dict(headers)
    assert hd["content-type"] == "application/json"
    assert hd["content-length"] == str(len(raw))  # 显式定界，不依赖连接关闭
    assert hd["x-a2c-error-code"] == "4008"
    # WSGI 原生用 str 头；ASGI 经 _encode_headers 转 bytes —— 仅此一处差异，body 字节一致
    assert _encode_headers(headers) == [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]

    # 非 4008（缺失/非法 400）不带 x-a2c-error-code，但仍带 content-length
    raw2, headers2 = _build_rejection({"code": 400, "message": "Missing a2c_version query parameter"})
    hd2 = dict(headers2)
    assert "x-a2c-error-code" not in hd2
    assert hd2["content-length"] == str(len(raw2))


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
        body = b"".join(mw({"PATH_INFO": "/socket.io/", "QUERY_STRING": f"a2c_version={INCOMPATIBLE_PEER}"}, sr))
        assert cap["status"].startswith("400")
        assert ("x-a2c-error-code", "4008") in cap["headers"]
        assert json.loads(body)["code"] == 4008

    def test_compatible_passes_through(self) -> None:
        def app(environ, start_response):
            start_response("200 OK", [])
            return [b"DOWNSTREAM"]

        mw = A2CProtocolVersionWSGIMiddleware(app, socketio_path="/socket.io")
        sr, _ = self._start_response_collector()
        body = b"".join(mw({"PATH_INFO": "/socket.io/", "QUERY_STRING": f"a2c_version={COMPATIBLE_PEER}"}, sr))
        assert body == b"DOWNSTREAM"

    def test_path_scope_non_socketio_untouched(self) -> None:
        def app(environ, start_response):
            start_response("200 OK", [])
            return [b"DOWNSTREAM"]

        mw = A2CProtocolVersionWSGIMiddleware(app, socketio_path="/socket.io")
        sr, _ = self._start_response_collector()
        body = b"".join(mw({"PATH_INFO": "/health", "QUERY_STRING": ""}, sr))
        assert body == b"DOWNSTREAM"

    def test_missing_version_no_4008_header(self) -> None:
        # 镜像 ASGI test_missing_version_no_4008_header：缺失 a2c_version → 400，
        # 但 400≠4008，rejection builder 不得附带 x-a2c-error-code 诊断 header。
        def app(environ, start_response):  # pragma: no cover - 不应被调用
            start_response("200 OK", [])
            return [b"DOWNSTREAM"]

        mw = A2CProtocolVersionWSGIMiddleware(app, socketio_path="/socket.io")
        sr, cap = self._start_response_collector()
        body = b"".join(mw({"PATH_INFO": "/socket.io/", "QUERY_STRING": ""}, sr))
        assert cap["status"].startswith("400")
        assert not any(k == "x-a2c-error-code" for k, _ in cap["headers"])
        assert json.loads(body) == {"code": 400, "message": "Missing a2c_version query parameter"}

    def test_prefix_pollution_not_matched(self) -> None:
        # 镜像 ASGI test_prefix_pollution_not_matched：/socket.iofoo 不得被误判为
        # socketio 前缀（PATH_INFO 精确前缀匹配），即使缺 a2c_version 也透传下游。
        def app(environ, start_response):
            start_response("200 OK", [])
            return [b"DOWNSTREAM"]

        mw = A2CProtocolVersionWSGIMiddleware(app, socketio_path="/socket.io")
        sr, _ = self._start_response_collector()
        body = b"".join(mw({"PATH_INFO": "/socket.iofoo", "QUERY_STRING": ""}, sr))
        assert body == b"DOWNSTREAM"

    def test_ws_upgrade_incompatible_still_rejected(self) -> None:
        # WSGI 实测（勿假设）：WS-only over WSGI 起始即普通 HTTP GET（带 Upgrade 头）；
        # 中间件对 Upgrade 无关、仅看 PATH_INFO+QUERY_STRING → 仍走同一 400/4008，天然覆盖。
        def app(environ, start_response):  # pragma: no cover - 不应被调用
            start_response("200 OK", [])
            return [b"DOWNSTREAM"]

        mw = A2CProtocolVersionWSGIMiddleware(app, socketio_path="/socket.io")
        sr, cap = self._start_response_collector()
        environ = {
            "PATH_INFO": "/socket.io/",
            "QUERY_STRING": f"EIO=4&transport=websocket&a2c_version={INCOMPATIBLE_PEER}",
            "HTTP_UPGRADE": "websocket",
            "HTTP_CONNECTION": "Upgrade",
        }
        body = b"".join(mw(environ, sr))
        assert cap["status"].startswith("400")
        assert ("x-a2c-error-code", "4008") in cap["headers"]
        assert json.loads(body)["code"] == 4008
