# -*- coding: utf-8 -*-
# filename: test_handshake.py
# @Author  : JQQ
# @Software: PyCharm
"""
连接握手共享工具单元测试 / Unit tests for shared handshake helpers

- build_handshake_url：保留既有 query、无 query、去重防漂移、保留 path / fragment
- extract_4008_payload：engineio 链式 args[1]（权威）、json.loads 回退、非 4008/非 JSON → None
- enforce_polling_first：§1 polling-first MUST 护栏，WS-only 显式覆盖强制重注入
"""

import logging
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from a2c_smcp.exceptions import ProtocolVersionError
from a2c_smcp.utils.handshake import (
    DEFAULT_HANDSHAKE_TRANSPORTS,
    apply_polling_first_guard,
    build_handshake_url,
    build_protocol_version_error,
    enforce_polling_first,
    extract_4008_payload,
)


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


class TestEnforcePollingFirst:
    @pytest.mark.parametrize(
        "transports",
        [
            ["websocket"],  # WS-only
            ["websocket", "polling"],  # websocket 起始（首个握手仍是 WS）
            ("websocket",),  # tuple 形态
        ],
    )
    def test_ws_first_is_overridden(self, transports) -> None:
        effective, overridden = enforce_polling_first(transports)
        assert overridden is True
        assert effective == DEFAULT_HANDSHAKE_TRANSPORTS
        assert effective[0] == "polling"  # §1 polling-first MUST 落地

    @pytest.mark.parametrize(
        "transports",
        [
            ["polling", "websocket"],  # 默认，已合规
            ["polling"],  # polling-only
            None,  # python-socketio 默认（polling 优先）
            [],  # 空 → 不改动
        ],
    )
    def test_polling_first_or_none_untouched(self, transports) -> None:
        effective, overridden = enforce_polling_first(transports)
        assert overridden is False
        assert effective is transports  # 原样返回，不复制不改写


class TestApplyPollingFirstGuard:
    """🟡2 收敛：三处 connect 站点共用此接线（含 loud warning），单一事实源单测。"""

    def test_ws_only_override_warns_and_rewrites(self) -> None:
        logger = logging.getLogger("t.guard")
        logger.warning = MagicMock()  # type: ignore[method-assign]
        out = apply_polling_first_guard(["websocket"], logger)
        assert out == DEFAULT_HANDSHAKE_TRANSPORTS
        assert out[0] == "polling"
        logger.warning.assert_called_once()
        assert "polling-first MUST" in logger.warning.call_args.args[0]

    def test_polling_first_no_warn(self) -> None:
        logger = logging.getLogger("t.guard2")
        logger.warning = MagicMock()  # type: ignore[method-assign]
        out = apply_polling_first_guard(["polling", "websocket"], logger)
        assert out == ["polling", "websocket"]
        logger.warning.assert_not_called()

    def test_none_untouched_no_warn(self) -> None:
        logger = logging.getLogger("t.guard3")
        logger.warning = MagicMock()  # type: ignore[method-assign]
        assert apply_polling_first_guard(None, logger) is None
        logger.warning.assert_not_called()


class TestBuildProtocolVersionError:
    """🟡2 收敛：4008 flat ErrorPayload → ProtocolVersionError 字段映射单一事实源。"""

    def test_full_payload_mapped(self) -> None:
        e = build_protocol_version_error(
            {
                "code": 4008,
                "message": "Protocol version mismatch",
                "server_version": "0.3.0",
                "client_version": "0.2.0",
                "min_supported": "0.3.0",
                "max_supported": "0.3.999",
            }
        )
        assert isinstance(e, ProtocolVersionError)
        assert e.client_version == "0.2.0" and e.server_version == "0.3.0"
        assert e.min_supported == "0.3.0" and e.max_supported == "0.3.999"
        assert "0.2.0" in str(e) and "0.3.0" in str(e)

    def test_missing_fields_default_and_none(self) -> None:
        e = build_protocol_version_error({"code": 4008})
        assert e.client_version is None and e.server_version is None
        assert e.min_supported is None and e.max_supported is None
        assert e.message == "Protocol version mismatch"  # 默认 message
