# -*- coding: utf-8 -*-
"""
测试 a2c_smcp/server/auth.py

#112(AS-38)：连接面鉴权改走 Socket.IO CONNECT ``auth`` dict（字段默认 ``token``），HTTP header 不再参与鉴权。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from a2c_smcp.server.auth import DEFAULT_AUTH_FIELD_NAME, DefaultAuthenticationProvider

# get_agent_id 方法已被移除，不再需要测试
# get_agent_id method has been removed, no longer need to test


def test_default_auth_field_name_is_token():
    """#112(AS-38)：默认鉴权字段名为 auth dict 内的 ``token`` / default auth field is ``token``."""
    assert DEFAULT_AUTH_FIELD_NAME == "token"


@pytest.mark.asyncio
async def test_authenticate_admin_ok_and_missing_or_wrong_key():
    prov = DefaultAuthenticationProvider("admin_secret")
    sio = AsyncMock()
    environ = {}
    headers: list = []  # #112(AS-38)：headers 不再参与鉴权

    # 正确 token（在 auth dict 内）
    ok = await prov.authenticate(sio, environ, {"token": "admin_secret"}, headers)
    assert ok is True

    # 缺失 auth dict（None）
    ok2 = await prov.authenticate(sio, environ, None, headers)
    assert ok2 is False

    # auth dict 无 token 字段
    ok2b = await prov.authenticate(sio, environ, {}, headers)
    assert ok2b is False

    # 错误 token
    ok3 = await prov.authenticate(sio, environ, {"token": "wrong"}, headers)
    assert ok3 is False


@pytest.mark.asyncio
async def test_authenticate_ignores_http_header():
    """#112(AS-38)：凭据放在 HTTP header（旧路径）必须被忽略 / credential in header must be ignored."""
    prov = DefaultAuthenticationProvider("admin_secret")
    sio = AsyncMock()
    # 旧 header 路径携带正确密钥，但 auth dict 为空 → 仍拒绝
    headers = [(b"access_token", b"admin_secret")]
    assert await prov.authenticate(sio, {}, None, headers) is False


@pytest.mark.asyncio
async def test_authenticate_custom_field_name():
    """自定义 ``api_key_name`` 字段名必须真实生效 / custom field name must take effect."""
    prov = DefaultAuthenticationProvider("admin_secret", api_key_name="x-legacy-key")
    sio = AsyncMock()
    assert await prov.authenticate(sio, {}, {"x-legacy-key": "admin_secret"}, []) is True
    # 默认 token 字段在自定义字段名下无效
    assert await prov.authenticate(sio, {}, {"token": "admin_secret"}, []) is False


# has_admin_permission 方法已被移除，管理员权限检查已集成到 authenticate 方法中
# has_admin_permission method has been removed, admin permission check is now integrated into authenticate method


@pytest.mark.asyncio
async def test_admin_permission_integrated_in_authenticate():
    """测试管理员权限检查已集成到认证方法中 / Test admin permission check integrated in authenticate method"""
    sio = AsyncMock()
    environ = {}

    # 无管理员密钥配置的提供者
    # Provider without admin secret configured
    prov1 = DefaultAuthenticationProvider(None)
    assert await prov1.authenticate(sio, environ, {"token": "any_key"}, []) is False

    # 有管理员密钥配置的提供者
    # Provider with admin secret configured
    prov2 = DefaultAuthenticationProvider("admin_secret")
    assert await prov2.authenticate(sio, environ, {"token": "admin_secret"}, []) is True
