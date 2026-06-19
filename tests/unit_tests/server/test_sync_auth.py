# -*- coding: utf-8 -*-
"""
测试 a2c_smcp/server/sync_auth.py

#112(AS-38)：连接面鉴权改走 Socket.IO CONNECT ``auth`` dict（字段默认 ``token``），HTTP header 不再参与鉴权。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from a2c_smcp.server.sync_auth import DEFAULT_AUTH_FIELD_NAME, DefaultSyncAuthenticationProvider

# get_agent_id 方法已被移除，不再需要测试
# get_agent_id method has been removed, no longer need to test


def test_sync_default_auth_field_name_is_token():
    assert DEFAULT_AUTH_FIELD_NAME == "token"


def test_sync_authenticate_variants():
    prov = DefaultSyncAuthenticationProvider("adm")
    sio = MagicMock()
    environ = {}

    # 正确 token（在 auth dict 内）
    assert prov.authenticate(sio, environ, {"token": "adm"}, []) is True

    # 缺失 / 无 token / 错误 token
    assert prov.authenticate(sio, environ, None, []) is False
    assert prov.authenticate(sio, environ, {}, []) is False
    assert prov.authenticate(sio, environ, {"token": "wrong"}, []) is False

    # 旧 HTTP header 路径携带正确密钥 → 必须被忽略
    assert prov.authenticate(sio, environ, None, [(b"access_token", b"adm")]) is False


def test_sync_custom_field_name():
    prov = DefaultSyncAuthenticationProvider("adm", api_key_name="x-legacy-key")
    sio = MagicMock()
    assert prov.authenticate(sio, {}, {"x-legacy-key": "adm"}, []) is True
    assert prov.authenticate(sio, {}, {"token": "adm"}, []) is False


# has_admin_permission 方法已被移除，管理员权限检查已集成到 authenticate 方法中
# has_admin_permission method has been removed, admin permission check is now integrated into authenticate method


def test_sync_admin_permission_integrated_in_authenticate():
    """测试管理员权限检查已集成到认证方法中 / Test admin permission check integrated in authenticate method"""
    sio = MagicMock()
    environ = {}

    # 无管理员密钥配置的提供者
    # Provider without admin secret configured
    prov1 = DefaultSyncAuthenticationProvider(None)
    assert prov1.authenticate(sio, environ, {"token": "any_key"}, []) is False

    # 有管理员密钥配置的提供者
    # Provider with admin secret configured
    prov2 = DefaultSyncAuthenticationProvider("adm")
    assert prov2.authenticate(sio, environ, {"token": "adm"}, []) is True
