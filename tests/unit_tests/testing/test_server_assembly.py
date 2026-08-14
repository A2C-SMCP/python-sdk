# -*- coding: utf-8 -*-
# filename: test_server_assembly.py
"""
中文: `a2c_smcp.testing` 服务端装配模块的单元测试（#187）。

    覆盖两类契约：
    1. 装配正确性——工厂返回的 app 必须包裹协议版本握手中间件（生产等价，防 F-05 假绿，镜像
       ``tests/e2e/test_version_handshake_conftest_server.py`` 的断言手法，但跑在 ``poe test``
       下、不依赖 e2e marker，永久生效）；
    2. 惰性导入契约——未安装 ``a2c-smcp[server]`` extra 时，``import a2c_smcp.testing`` 不报错，
       仅调用起服务函数/构造 runner 时抛携带安装提示的 ImportError。

English: Unit tests for the `a2c_smcp.testing` server-assembly module (#187). Two contracts:
    the factories must wrap the protocol version-handshake middleware (production-equivalent,
    mirroring the F-05 false-green guard), and the lazy-import contract — importing the module
    must never require werkzeug/uvicorn; only calling the runners does, with an actionable hint.
"""

from __future__ import annotations

import json
import socket
import sys

import httpx
import pytest
from werkzeug.test import Client

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.server.middleware import A2CProtocolVersionASGIMiddleware, A2CProtocolVersionWSGIMiddleware
from a2c_smcp.testing import (
    LocalSMCPNamespace,
    LocalSyncSMCPNamespace,
    PermissiveAuthenticationProvider,
    PermissiveSyncAuthenticationProvider,
    UvicornTestServer,
    create_local_async_server,
    create_local_sync_server,
    pick_free_port,
    run_http_server,
)
from tests.protocol_versions import max_supported_of, min_supported_of

_INCOMPATIBLE = "99.0.0"  # 与任意 0.x/1.x 均不兼容的定值 MAJOR 差异（不耦合具体 PROTOCOL_VERSION）


def _poll_path(version: str) -> str:
    """polling 握手 URL（query 含 a2c_version）/ polling handshake URL carrying a2c_version."""
    return f"/socket.io/?EIO=4&transport=polling&a2c_version={version}"


def _assert_4008_body(body: dict) -> None:
    """断言 4008 flat ErrorPayload 六字段 / assert the six 4008 flat-ErrorPayload fields."""
    assert body["code"] == 4008
    assert body["message"] == "Protocol version mismatch"
    assert body["server_version"] == PROTOCOL_VERSION
    assert body["client_version"] == _INCOMPATIBLE
    assert body["min_supported"] == min_supported_of(PROTOCOL_VERSION)
    assert body["max_supported"] == max_supported_of(PROTOCOL_VERSION)


# ============================================================================
# 中文: 装配正确性（生产等价：permissive auth + 版本握手中间件包裹）
# English: Assembly correctness (production-equivalent: permissive auth + version middleware)
# ============================================================================


def test_sync_factory_returns_wrapped_production_equivalent_app() -> None:
    """中文: sync 工厂返回 (Server, 放行命名空间, 握手中间件包裹的 WSGI app)。"""
    _sio, ns, app = create_local_sync_server()

    assert isinstance(ns, LocalSyncSMCPNamespace)
    assert isinstance(ns.auth_provider, PermissiveSyncAuthenticationProvider)
    assert isinstance(app, A2CProtocolVersionWSGIMiddleware)


def test_async_factory_returns_wrapped_production_equivalent_app() -> None:
    """中文: async 工厂返回 (AsyncServer, 放行命名空间, 握手中间件包裹的 ASGI app)。"""
    _sio, ns, app = create_local_async_server()

    assert isinstance(ns, LocalSMCPNamespace)
    assert isinstance(ns.auth_provider, PermissiveAuthenticationProvider)
    assert isinstance(app, A2CProtocolVersionASGIMiddleware)


def test_sync_factory_rejects_incompatible_version() -> None:
    """中文: sync 工厂返回的 app 必须以 400 + 4008 拒绝不兼容版本（防 F-05 假绿）。"""
    _sio, _ns, app = create_local_sync_server()
    client = Client(app)
    resp = client.get(_poll_path(_INCOMPATIBLE))

    assert resp.status_code == 400
    assert resp.headers.get("X-A2C-Error-Code") == "4008"
    _assert_4008_body(json.loads(resp.get_data(as_text=True)))


def test_sync_factory_passes_compatible_version() -> None:
    """中文: 兼容版本必须被透传给 socketio（非 400、无错误头）。"""
    _sio, _ns, app = create_local_sync_server()
    client = Client(app)
    resp = client.get(_poll_path(PROTOCOL_VERSION))

    assert resp.status_code != 400
    assert "X-A2C-Error-Code" not in resp.headers


@pytest.mark.asyncio
async def test_async_factory_rejects_incompatible_version() -> None:
    """中文: async 工厂返回的 app 必须以 400 + 4008 拒绝不兼容版本（防 F-05 假绿）。"""
    _sio, _ns, app = create_local_async_server()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get(_poll_path(_INCOMPATIBLE))

    assert resp.status_code == 400
    assert resp.headers.get("X-A2C-Error-Code") == "4008"
    _assert_4008_body(resp.json())


@pytest.mark.asyncio
async def test_async_factory_passes_compatible_version() -> None:
    """中文: 兼容版本必须被透传给 socketio（非 400、无错误头）。"""
    _sio, _ns, app = create_local_async_server()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get(_poll_path(PROTOCOL_VERSION))

    assert resp.status_code != 400
    assert "X-A2C-Error-Code" not in resp.headers


# ============================================================================
# 中文: 惰性导入契约（不装 [server] extra 也能 import 模块，用 runner 时才报可行动错误）
# English: Lazy-import contract (module imports without the [server] extra; runners raise actionable)
# ============================================================================


def test_module_import_succeeds_without_server_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """中文: 未装 [server] extra 时 import a2c_smcp.testing 不得报错（惰性契约的另一半——
        顶层不得引入 werkzeug/uvicorn；调用 runner 时的报错由另两测试守护）。"""
    import importlib

    import a2c_smcp.testing as testing_pkg

    monkeypatch.setitem(sys.modules, "werkzeug", None)
    monkeypatch.setitem(sys.modules, "werkzeug.serving", None)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    importlib.reload(testing_pkg)  # 若顶层引 werkzeug/uvicorn 则此处抛 ImportError


def test_run_http_server_missing_werkzeug_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """中文: 未装 werkzeug 时 run_http_server 抛携安装提示的 ImportError（模块 import 本身不触发）。"""
    monkeypatch.setitem(sys.modules, "werkzeug.serving", None)

    with pytest.raises(ImportError, match=r"a2c-smcp\[server\]"):
        with run_http_server():
            pass  # pragma: no cover - 进入 with 前即抛


def test_uvicorn_test_server_missing_uvicorn_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """中文: 未装 uvicorn 时构造 UvicornTestServer 抛携安装提示的 ImportError。"""
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    with pytest.raises(ImportError, match=r"a2c-smcp\[server\]"):
        UvicornTestServer(app=None)


# ============================================================================
# 中文: 工具函数与 runner 行为
# English: Utility functions and runner behaviour
# ============================================================================


def test_pick_free_port_returns_bindable_port() -> None:
    """中文: pick_free_port 返回可绑定的本地端口。"""
    port = pick_free_port()
    assert 0 < port < 65536
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))  # 未被占用则绑定成功（TOCTOU 容忍：仅作工具自检）


def test_run_http_server_roundtrip() -> None:
    """中文: run_http_server 正常路径真实起停一轮：端口可 TCP 连接，退出后进程被回收（端口可重绑）。"""
    with run_http_server() as (host, port):
        with socket.create_connection((host, port), timeout=5):
            pass  # 端口已监听即通过 / listening port proves up
    # 退出 with 后多进程 runner 已被 finally 回收：端口可重新绑定即证 / port rebindable proves reaped
    with socket.socket() as s:
        s.bind((host, port))


@pytest.mark.asyncio
async def test_uvicorn_test_server_up_down_roundtrip() -> None:
    """中文: UvicornTestServer 真实起停一轮：up 后端口可 TCP 连接，down(force) 正常收尾。"""
    _sio, _ns, app = create_local_async_server()
    port = pick_free_port()
    server = UvicornTestServer(app, port=port)
    await server.up()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass  # 端口已监听即通过 / listening port proves up
    finally:
        await server.down(force=True)
