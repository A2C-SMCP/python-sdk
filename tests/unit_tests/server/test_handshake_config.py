# -*- coding: utf-8 -*-
"""
中文：Server 侧握手配置测试（对齐 Rust ``smcp-server-core/src/auth.rs`` 默认值变更）。
English: Handshake config tests for the Server-side auth providers.

覆盖场景 / Covered scenarios:
  1. ``DEFAULT_AUTH_HEADER_NAME`` 导出为 ``access_token``
  2. async / sync ``DefaultAuthenticationProvider`` 默认 ``api_key_name`` 为 ``access_token``
  3. 默认 provider 能识别 ``access_token`` header 完成鉴权
  4. 自定义 ``api_key_name`` 生效
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from a2c_smcp.server import (
    DEFAULT_AUTH_HEADER_NAME,
    DefaultAuthenticationProvider,
    DefaultSyncAuthenticationProvider,
)


def test_server_default_auth_header_name_is_access_token() -> None:
    """Server 模块导出的默认 header 名为 ``access_token``"""
    assert DEFAULT_AUTH_HEADER_NAME == "access_token"


def test_async_default_auth_provider_default_api_key_name() -> None:
    """Async 默认 provider 的 ``api_key_name`` 默认值为 ``access_token``"""
    provider = DefaultAuthenticationProvider(admin_secret="adm")
    assert provider.api_key_name == "access_token"


def test_sync_default_auth_provider_default_api_key_name() -> None:
    """Sync 默认 provider 的 ``api_key_name`` 默认值为 ``access_token``"""
    provider = DefaultSyncAuthenticationProvider(admin_secret="adm")
    assert provider.api_key_name == "access_token"


@pytest.mark.asyncio
async def test_async_default_auth_provider_authenticates_access_token_header() -> None:
    """默认 provider 能识别 ``access_token`` header 并完成管理员鉴权"""
    provider = DefaultAuthenticationProvider(admin_secret="adm")
    sio = AsyncMock()

    headers_ok = [(b"access_token", b"adm")]
    assert await provider.authenticate(sio, {}, None, headers_ok) is True

    headers_bad = [(b"access_token", b"wrong")]
    assert await provider.authenticate(sio, {}, None, headers_bad) is False


def test_sync_default_auth_provider_authenticates_access_token_header() -> None:
    """Sync 默认 provider 能识别 ``access_token`` header"""
    provider = DefaultSyncAuthenticationProvider(admin_secret="adm")
    sio = MagicMock()

    assert provider.authenticate(sio, {}, None, [(b"access_token", b"adm")]) is True
    assert provider.authenticate(sio, {}, None, [(b"access_token", b"wrong")]) is False


@pytest.mark.asyncio
async def test_async_default_auth_provider_custom_api_key_name() -> None:
    """自定义 ``api_key_name`` 生效，默认 header 不应再通过"""
    provider = DefaultAuthenticationProvider(admin_secret="adm", api_key_name="x-tf-token")

    sio = AsyncMock()
    assert provider.api_key_name == "x-tf-token"

    # 自定义 header 可通过 / Custom header passes
    assert await provider.authenticate(sio, {}, None, [(b"x-tf-token", b"adm")]) is True
    # 旧默认 header 不再能通过 / Former default header no longer authenticates
    assert await provider.authenticate(sio, {}, None, [(b"access_token", b"adm")]) is False
