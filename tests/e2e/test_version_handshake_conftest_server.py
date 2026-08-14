# -*- coding: utf-8 -*-
# filename: test_version_handshake_conftest_server.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文: E2E conftest 测试 Server 协议版本握手回归测试（#93）。

    UAT full-protocol 场景 F-05 经 ``tests/e2e/conftest.py::_run_server_process`` →
    ``create_local_sync_server()`` 启动 Server。若该工厂返回裸 ``WSGIApp`` 而未包裹
    ``A2CProtocolVersionWSGIMiddleware``，则协议版本握手闸门在 HTTP 传输层被完全跳过，
    ``a2c_version=99.0.0`` 的不兼容客户端不会被拒（HTTP 200），F-05 失效。

    本测试用进程内 WSGI/ASGI test client 直接断言两个工厂返回的 app 已套上版本握手中间件
    （确定性，零多进程 flakiness）。断言风格镜像
    ``tests/integration_tests/test_version_handshake_server.py``。

English: Regression test (#93) ensuring the E2E conftest test servers wrap the protocol
    version handshake middleware. UAT F-05 launches the Server via
    ``create_local_sync_server()``; without the middleware the ``a2c_version`` gate is
    skipped at the transport layer and an incompatible client connects (HTTP 200). We assert
    in-process (werkzeug WSGI client / httpx ASGI transport) that both factories' apps reject
    incompatible versions and pass compatible ones through.

协议依据 / Protocol: a2c-smcp-protocol docs/specification/versioning.md (§连接握手 / §错误码 4008)
"""

from __future__ import annotations

import json

import httpx
import pytest
from werkzeug.test import Client

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.testing import create_local_async_server, create_local_sync_server
from tests.protocol_versions import max_supported_of, min_supported_of

# 中文: 曾缺失 e2e marker 导致本文件 4 个用例从未被收集（-m e2e 全部 deselect，#93 回归网失活）。
#       现补回：进程内断言、零多进程 flakiness，e2e 套件开销可忽略。
# English: this file once lacked the e2e marker, so its 4 tests were never collected (#93 guard was
#       dead). Re-marked: in-process assertions, zero multiprocess flakiness, negligible e2e cost.
pytestmark = pytest.mark.e2e

_INCOMPATIBLE = "99.0.0"  # 与任意 0.x/1.x 均不兼容的定值 MAJOR 差异（不耦合具体 PROTOCOL_VERSION）


def _poll_path(version: str) -> str:
    """polling 握手 URL（query 含 a2c_version）/ polling handshake URL carrying a2c_version."""
    return f"/socket.io/?EIO=4&transport=polling&a2c_version={version}"


def _assert_4008_body(body: dict) -> None:
    """断言 4008 flat ErrorPayload 四字段 / assert the four 4008 flat-ErrorPayload fields."""
    assert body["code"] == 4008
    assert body["message"] == "Protocol version mismatch"
    assert body["server_version"] == PROTOCOL_VERSION
    assert body["client_version"] == _INCOMPATIBLE
    assert body["min_supported"] == min_supported_of(PROTOCOL_VERSION)
    assert body["max_supported"] == max_supported_of(PROTOCOL_VERSION)


# ============================================================================
# 中文: 同步 WSGI 工厂 / English: sync WSGI factory
# ============================================================================


def test_sync_conftest_server_rejects_incompatible() -> None:
    """中文: create_local_sync_server() 返回的 app 必须以 400 + 4008 拒绝不兼容版本。"""
    _sio, _ns, app = create_local_sync_server()
    client = Client(app)
    resp = client.get(_poll_path(_INCOMPATIBLE))

    assert resp.status_code == 400
    assert resp.headers.get("X-A2C-Error-Code") == "4008"
    _assert_4008_body(json.loads(resp.get_data(as_text=True)))


def test_sync_conftest_server_accepts_compatible() -> None:
    """中文: 兼容版本必须被透传给 socketio（非 400、无错误头）。"""
    _sio, _ns, app = create_local_sync_server()
    client = Client(app)
    resp = client.get(_poll_path(PROTOCOL_VERSION))

    assert resp.status_code != 400
    assert "X-A2C-Error-Code" not in resp.headers


# ============================================================================
# 中文: 异步 ASGI 工厂 / English: async ASGI factory
# ============================================================================


@pytest.mark.asyncio
async def test_async_conftest_server_rejects_incompatible() -> None:
    """中文: create_local_async_server() 返回的 app 必须以 400 + 4008 拒绝不兼容版本。"""
    _sio, _ns, app = create_local_async_server()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get(_poll_path(_INCOMPATIBLE))

    assert resp.status_code == 400
    assert resp.headers.get("X-A2C-Error-Code") == "4008"
    _assert_4008_body(resp.json())


@pytest.mark.asyncio
async def test_async_conftest_server_accepts_compatible() -> None:
    """中文: 兼容版本必须被透传给 socketio（非 400、无错误头）。"""
    _sio, _ns, app = create_local_async_server()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get(_poll_path(PROTOCOL_VERSION))

    assert resp.status_code != 400
    assert "X-A2C-Error-Code" not in resp.headers
