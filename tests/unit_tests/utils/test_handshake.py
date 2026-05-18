# -*- coding: utf-8 -*-
# filename: test_handshake.py
# @Author  : JQQ
# @Software: PyCharm
"""
连接握手共享工具单元测试 / Unit tests for shared handshake helpers

- build_handshake_url：保留既有 query、无 query、去重防漂移、保留 path / fragment
- extract_4008_payload：engineio 链式 args[1]（权威）、json.loads 回退、非 4008/非 JSON → None
"""

from urllib.parse import parse_qs, urlparse

import pytest

from a2c_smcp.utils.handshake import build_handshake_url, extract_4008_payload


class TestBuildHandshakeUrl:
    def test_preserves_existing_query(self) -> None:
        out = build_handshake_url("wss://h/p?role=agent&x=1", "0.2.0")
        q = parse_qs(urlparse(out).query)
        assert q["role"] == ["agent"] and q["x"] == ["1"] and q["a2c_version"] == ["0.2.0"]
        assert urlparse(out).path == "/p"

    def test_no_query(self) -> None:
        out = build_handshake_url("wss://h", "0.2.0")
        assert parse_qs(urlparse(out).query)["a2c_version"] == ["0.2.0"]

    def test_dedup_caller_supplied_version_is_overridden(self) -> None:
        # 防漂移：调用方误传 a2c_version 必须被 SDK 常量覆盖（协议 MUST）
        out = build_handshake_url("wss://h?a2c_version=9.9.9&k=v", "0.2.0")
        q = parse_qs(urlparse(out).query)
        assert q["a2c_version"] == ["0.2.0"] and q["k"] == ["v"]

    def test_fragment_preserved(self) -> None:
        out = build_handshake_url("wss://h/p?a=b#frag", "0.2.0")
        assert urlparse(out).fragment == "frag"


def _chained(body: object) -> Exception:
    """构造 socketio→engineio 链式异常：engineio ConnectionError(msg, body)。"""
    cause = ConnectionError("Unexpected status code 400 in server response", body)
    exc = ConnectionError("Unexpected status code 400 in server response")
    exc.__cause__ = cause
    return exc


class TestExtract4008Payload:
    def test_authoritative_engineio_chained_arg(self) -> None:
        body = {"code": 4008, "server_version": "0.2.0", "client_version": "0.3.0"}
        assert extract_4008_payload(_chained(body)) == body

    def test_chained_non_4008_returns_none(self) -> None:
        assert extract_4008_payload(_chained({"code": 400, "message": "Missing a2c_version query parameter"})) is None

    def test_json_loads_fallback(self) -> None:
        # 协议参考实现路径：str(exc) 即纯 JSON
        exc = ConnectionError('{"code": 4008, "server_version": "0.2.0"}')
        assert extract_4008_payload(exc) == {"code": 4008, "server_version": "0.2.0"}

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionError("Unexpected status code 400 in server response"),  # 非 JSON
            ConnectionError("Connection refused by the server"),
            _chained(None),  # engineio body 非 JSON → arg=None
        ],
    )
    def test_non_json_returns_none(self, exc: Exception) -> None:
        assert extract_4008_payload(exc) is None
