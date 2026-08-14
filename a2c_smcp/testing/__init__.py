# -*- coding: utf-8 -*-
"""
中文: a2c_smcp.testing —— 下游 e2e/联调测试支持（#187）。

    提供装配真实 SMCP 信令服务的公共配方（permissive auth、sync/async 装配、
    版本握手中间件包裹、WSGI/ASGI runner）。SDK 自身测试同源消费（dogfood）。
    依赖 ``a2c-smcp[server]`` extra（werkzeug / uvicorn）惰性导入——未装时
    import 本包不报错，仅调用起服务函数时抛可行动 ImportError。

English: a2c_smcp.testing — downstream e2e/integration test support (#187). The public recipe
    for assembling a real SMCP signalling server (permissive auth, sync/async assembly,
    version-handshake middleware wrap, WSGI/ASGI runners), dogfooded by the SDK's own tests.
    The ``a2c-smcp[server]`` extra deps (werkzeug / uvicorn) are imported lazily: importing this
    package never requires them — only calling the runners does, with an actionable ImportError.
"""

from .server import (
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

__all__ = [
    # 放行认证 / Permissive auth
    "PermissiveSyncAuthenticationProvider",
    "PermissiveAuthenticationProvider",
    # 本地命名空间 / Local namespaces
    "LocalSyncSMCPNamespace",
    "LocalSMCPNamespace",
    # 装配工厂 / Assembly factories
    "create_local_sync_server",
    "create_local_async_server",
    # 端口与 runner / Port helpers and runners
    "pick_free_port",
    "run_http_server",
    "UvicornTestServer",
]
