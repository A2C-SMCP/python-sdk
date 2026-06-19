# -*- coding: utf-8 -*-
"""
中文：Server 侧握手配置测试（对齐 Rust ``smcp-server-core/src/auth.rs`` #86 默认值变更）。
English: Handshake config tests for the Server-side auth providers.

#112(AS-38)：连接面鉴权改走 Socket.IO CONNECT ``auth`` dict（字段默认 ``token``），HTTP header 不再参与鉴权。

覆盖场景 / Covered scenarios:
  1. ``DEFAULT_AUTH_FIELD_NAME`` 导出为 ``token``
  2. async / sync ``DefaultAuthenticationProvider`` 默认 ``api_key_name`` 为 ``token``
  3. 默认 provider 能从 ``auth`` dict 的 ``token`` 字段完成鉴权
  4. 自定义 ``api_key_name`` 生效；旧 HTTP header 路径被忽略
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from a2c_smcp.server import (
    DEFAULT_AUTH_FIELD_NAME,
    DefaultAuthenticationProvider,
    DefaultSyncAuthenticationProvider,
)


def test_server_default_auth_field_name_is_token() -> None:
    """Server 模块导出的默认鉴权字段名为 ``token``"""
    assert DEFAULT_AUTH_FIELD_NAME == "token"


def test_async_default_auth_provider_default_api_key_name() -> None:
    """Async 默认 provider 的 ``api_key_name`` 默认值为 ``token``"""
    provider = DefaultAuthenticationProvider(admin_secret="adm")
    assert provider.api_key_name == "token"


def test_sync_default_auth_provider_default_api_key_name() -> None:
    """Sync 默认 provider 的 ``api_key_name`` 默认值为 ``token``"""
    provider = DefaultSyncAuthenticationProvider(admin_secret="adm")
    assert provider.api_key_name == "token"


@pytest.mark.asyncio
async def test_async_default_auth_provider_authenticates_auth_dict_token() -> None:
    """默认 provider 能从 ``auth`` dict 的 ``token`` 字段完成管理员鉴权"""
    provider = DefaultAuthenticationProvider(admin_secret="adm")
    sio = AsyncMock()

    assert await provider.authenticate(sio, {}, {"token": "adm"}, []) is True
    assert await provider.authenticate(sio, {}, {"token": "wrong"}, []) is False
    # 旧 HTTP header 路径携带正确密钥 → 必须被忽略
    assert await provider.authenticate(sio, {}, None, [(b"access_token", b"adm")]) is False


def test_sync_default_auth_provider_authenticates_auth_dict_token() -> None:
    """Sync 默认 provider 能从 ``auth`` dict 的 ``token`` 字段完成鉴权"""
    provider = DefaultSyncAuthenticationProvider(admin_secret="adm")
    sio = MagicMock()

    assert provider.authenticate(sio, {}, {"token": "adm"}, []) is True
    assert provider.authenticate(sio, {}, {"token": "wrong"}, []) is False
    assert provider.authenticate(sio, {}, None, [(b"access_token", b"adm")]) is False


@pytest.mark.asyncio
async def test_async_default_auth_provider_custom_api_key_name() -> None:
    """自定义 ``api_key_name`` 生效，默认 ``token`` 字段不应再通过"""
    provider = DefaultAuthenticationProvider(admin_secret="adm", api_key_name="x-tf-token")

    sio = AsyncMock()
    assert provider.api_key_name == "x-tf-token"

    # 自定义字段可通过 / Custom field passes
    assert await provider.authenticate(sio, {}, {"x-tf-token": "adm"}, []) is True
    # 默认 token 字段不再能通过 / Default token field no longer authenticates
    assert await provider.authenticate(sio, {}, {"token": "adm"}, []) is False
