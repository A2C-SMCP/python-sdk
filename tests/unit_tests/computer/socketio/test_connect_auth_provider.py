# -*- coding: utf-8 -*-
"""
#200 动态 auth provider（方案 C 原生透传）单元测试 / #200 dynamic auth provider unit tests (plan C).

锁定 ``SMCPComputerClient.connect`` 的 ``auth_provider`` 显式参数契约：
  - 与静态 ``auth`` 互斥 → ValueError（keyword 与 positional 两条路径）
  - 非 callable → TypeError 早失败
  - 路由到原生 auth 路径（零新增机制），静态 ``auth`` 路径零回归
  - 握手注入（a2c_version / polling-first）不受影响

Locks the ``auth_provider`` explicit-parameter contract on ``SMCPComputerClient.connect``:
  - mutually exclusive with static ``auth`` → ValueError (keyword and positional paths)
  - non-callable → TypeError, fails fast
  - routed into the native auth path (zero new machinery); static ``auth`` zero regression
  - handshake injection (a2c_version / polling-first) unaffected
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from socketio import AsyncClient

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.utils.handshake import A2C_VERSION_QUERY_KEY, DEFAULT_HANDSHAKE_TRANSPORTS


@pytest.fixture
def captured_super_connect():
    """Patch ``AsyncClient.connect`` 捕获 ``super().connect`` 收到的实参 / capture super().connect args."""
    holder: dict[str, Any] = {}

    # 签名用 *args 全量捕获：super().connect(handshake_url, *args, **kwargs) 的实参序列
    # 为 (self, handshake_url, *args)；f_args[1] = 注入后的握手 URL。
    # Captures everything positionally: the call is super().connect(handshake_url, *args, **kwargs),
    # so f_args[1] is the handshake-injected URL.
    async def fake_super_connect(*f_args: Any, **f_kwargs: Any) -> None:
        holder["url"] = f_args[1]
        holder["args"] = f_args[2:]
        holder["kwargs"] = f_kwargs

    original = AsyncClient.connect
    AsyncClient.connect = fake_super_connect
    try:
        yield holder
    finally:
        AsyncClient.connect = original


async def _provider() -> dict[str, str]:
    return {"token": "A"}


@pytest.mark.asyncio
async def test_auth_provider_routed_to_native_auth_path(captured_super_connect):
    """``auth_provider`` 必须路由到原生 ``auth`` kwarg（零新增机制），且握手注入不受影响。

    English: ``auth_provider`` must be routed into the native ``auth`` kwarg (zero new machinery),
    with handshake injection (a2c_version / polling-first) unaffected.
    """
    provider = _provider
    client = SMCPComputerClient(computer=MagicMock())

    await client.connect("http://localhost:8000", auth_provider=provider)

    # 原生 auth 路径：auth = provider 本身（由底层每次握手 await 重新求值）
    assert captured_super_connect["kwargs"]["auth"] is provider
    # 握手注入保留 / handshake injection preserved
    assert f"{A2C_VERSION_QUERY_KEY}={PROTOCOL_VERSION}" in captured_super_connect["url"]
    assert captured_super_connect["kwargs"]["transports"] == DEFAULT_HANDSHAKE_TRANSPORTS


@pytest.mark.asyncio
async def test_auth_and_auth_provider_mutually_exclusive(captured_super_connect):
    """静态 ``auth`` 与 ``auth_provider`` 同时传入 → ValueError 早失败，不触达底层。

    English: static ``auth`` + ``auth_provider`` together → ValueError fails fast, never reaches the wire.
    """
    client = SMCPComputerClient(computer=MagicMock())

    with pytest.raises(ValueError, match="互斥"):
        await client.connect("http://localhost:8000", auth={"token": "static"}, auth_provider=_provider)

    # 未触达底层 / never reached the underlying client
    assert not captured_super_connect


@pytest.mark.asyncio
async def test_positional_auth_and_auth_provider_mutually_exclusive(captured_super_connect):
    """按上游签名**位置**传入 auth（url 之后第 2 个位置参数）同样触发互斥 ValueError。

    English: auth passed **positionally** (2nd positional after url, per upstream signature)
    also triggers the mutual-exclusion ValueError.
    """
    client = SMCPComputerClient(computer=MagicMock())

    with pytest.raises(ValueError, match="互斥"):
        await client.connect(
            "http://localhost:8000",
            {"h": "v"},  # headers（位置参数 1）
            {"token": "static"},  # auth（位置参数 2）
            auth_provider=_provider,
        )

    assert not captured_super_connect


@pytest.mark.asyncio
async def test_auth_provider_non_callable_raises_typeerror(captured_super_connect):
    """非 callable 的 ``auth_provider`` → TypeError 早失败（运行时防御）。

    English: a non-callable ``auth_provider`` → TypeError fails fast (runtime defense).
    """
    client = SMCPComputerClient(computer=MagicMock())

    # 负向类型注入（非 callable）——运行时防御拦截；静态类型有意绕开注解
    # Negative-type injection (non-callable) — intercepted by the runtime defense;
    # deliberately bypasses the static annotation.
    not_callable: Any = {"token": "not-callable"}
    with pytest.raises(TypeError, match="callable"):
        await client.connect("http://localhost:8000", auth_provider=not_callable)

    assert not captured_super_connect


@pytest.mark.asyncio
async def test_sync_callable_auth_provider_accepted(captured_super_connect):
    """同步 callable 被接受并路由（原生路径兼容；文档推荐 async）。

    English: sync callables are accepted and routed (native-path compatibility; async recommended).
    """

    def sync_provider() -> dict[str, str]:
        return {"token": "A"}

    # 同步 callable 有意绕开注解（别名契约是 async；原生路径兼容 sync）
    # The sync callable deliberately bypasses the annotation (alias contract is async;
    # the native path accepts sync).
    provider_any: Any = sync_provider
    client = SMCPComputerClient(computer=MagicMock())

    await client.connect("http://localhost:8000", auth_provider=provider_any)

    assert captured_super_connect["kwargs"]["auth"] is sync_provider


@pytest.mark.asyncio
async def test_static_auth_zero_regression(captured_super_connect):
    """静态 ``auth`` 调用方式零回归：原样透传，不新增/改写任何字段（验收标准 6）。

    English: static ``auth`` zero regression — passed through untouched, no field added or rewritten (criterion 6).
    """
    client = SMCPComputerClient(computer=MagicMock())

    await client.connect("http://localhost:8000", auth={"token": "static"})

    assert captured_super_connect["kwargs"]["auth"] == {"token": "static"}


@pytest.mark.asyncio
async def test_no_auth_no_auth_provider_leaves_native_defaults(captured_super_connect):
    """两者皆缺省时，不注入 ``auth`` key（沿用上游 ``auth=None`` 默认语义）。

    English: when both are absent, no ``auth`` key is injected (upstream ``auth=None`` default preserved).
    """
    client = SMCPComputerClient(computer=MagicMock())

    await client.connect("http://localhost:8000")

    assert "auth" not in captured_super_connect["kwargs"]
