# -*- coding: utf-8 -*-
# filename: server.py
"""
中文: 测试支持——下游 e2e/联调装配真实 SMCP 信令服务的公共配方（#187）。

    装配细节与 SDK 自身 e2e 套件同源（原 tests/e2e/conftest.py），涵盖全部易错点：
    permissive auth、``async_handlers=True`` + ``always_connect=True``、
    ``start_service_task=False``、协议版本握手中间件包裹（缺一层则不兼容客户端被静默放行，
    F-05 假绿，见 #93）。SDK 自己的测试也消费本模块（dogfood），故契约演进时下游随升级
    自动对齐，不再从私有测试「考古」复制。

    依赖契约：werkzeug（WSGI 同步 runner）与 uvicorn（ASGI 异步 runner）均**惰性导入**——
    未安装 ``a2c-smcp[server]`` extra 时 import 本模块不报错，仅调用起服务函数 / 构造
    runner 时抛携带安装提示的 ImportError。

English: Testing support — the public recipe downstream e2e/integration suites use to assemble a
    real SMCP signalling server (#187). Same source as the SDK's own e2e fixtures, covering every
    gotcha: permissive auth, ``async_handlers=True`` + ``always_connect=True``,
    ``start_service_task=False`` and the protocol version-handshake middleware (omit it and
    incompatible clients are silently let through — F-05 false green, #93). The SDK's own tests
    consume this module (dogfooding), so downstream tracks contract evolution on upgrade instead
    of archaeology. werkzeug (WSGI sync runner) and uvicorn (ASGI async runner) are imported
    lazily: importing this module never requires the ``a2c-smcp[server]`` extra — only calling
    the runners does, and then with an actionable ImportError.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import multiprocessing
import socket
import time
from collections.abc import Iterator
from multiprocessing.synchronize import Event
from typing import Any

import socketio
from socketio import Namespace, Server, WSGIApp

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.server import (
    A2CProtocolVersionASGIMiddleware,
    A2CProtocolVersionWSGIMiddleware,
    SMCPNamespace,
    SyncSMCPNamespace,
)
from a2c_smcp.server.auth import AuthenticationProvider
from a2c_smcp.server.sync_auth import SyncAuthenticationProvider

_SERVER_EXTRA_HINT = (
    '起本地信令服务需要可选依赖（werkzeug / uvicorn），请安装: pip install "a2c-smcp[server]"'
    ' / Running a local signalling server needs the optional deps (werkzeug / uvicorn): '
    'pip install "a2c-smcp[server]"'
)


# ============================================================================
# 中文: 放行认证与本地命名空间 / English: Permissive auth providers and local namespaces
# ============================================================================


class PermissiveSyncAuthenticationProvider(SyncAuthenticationProvider):
    """中文: 测试用放行认证提供者（同步）/ English: Permissive auth provider for testing (sync)"""

    def authenticate(self, sio: Server, environ: dict, auth: dict | None, headers: list) -> bool:
        return True


class PermissiveAuthenticationProvider(AuthenticationProvider):
    """中文: 测试用放行认证提供者（异步）/ English: Permissive auth provider for testing (async)"""

    async def authenticate(self, sio: socketio.AsyncServer, environ: dict, auth: dict | None, headers: list) -> bool:
        return True


class LocalSyncSMCPNamespace(SyncSMCPNamespace):
    """中文: 同步命名空间，继承自正式实现，仅替换认证 / English: Sync namespace with permissive auth"""

    def __init__(self) -> None:
        super().__init__(auth_provider=PermissiveSyncAuthenticationProvider())


class LocalSMCPNamespace(SMCPNamespace):
    """中文: 异步命名空间，继承自正式实现，仅替换认证 / English: Async namespace with permissive auth"""

    def __init__(self) -> None:
        super().__init__(auth_provider=PermissiveAuthenticationProvider())


# ============================================================================
# 中文: 服务端装配工厂 / English: Server assembly factories
# ============================================================================


def create_local_sync_server() -> tuple[Server, Namespace, A2CProtocolVersionWSGIMiddleware]:
    """中文: 创建同步 Socket.IO Server 并注册本地命名空间 / English: Create sync Socket.IO Server with local namespace

    中文: 返回的 app 包裹 ``A2CProtocolVersionWSGIMiddleware``，与生产部署等价——在 HTTP 传输层
        校验 ``a2c_version`` query，不兼容客户端被拒（HTTP 400 + 4008）。否则下游 F-05（版本
        不兼容拒绝）会被静默跳过（#93）。SDK 客户端经 build_handshake_url 自动携带兼容版本，
        故正常连接不受影响。
    English: The returned app wraps ``A2CProtocolVersionWSGIMiddleware`` (production-equivalent),
        validating ``a2c_version`` at the HTTP transport layer; incompatible clients get HTTP 400 +
        4008. Without it the protocol version gate is skipped and F-05 silently passes (#93).
    """
    sio = Server(
        cors_allowed_origins="*",
        ping_timeout=5,  # 中文: 测试环境使用较短超时 / English: Use shorter timeout for testing
        ping_interval=3,  # 中文: 测试环境使用较短间隔 / English: Use shorter interval for testing
        async_handlers=True,  # 如果想使用 call 方法，则必定需要将此参数设置为True / Required for call method
        always_connect=True,
    )
    ns = LocalSyncSMCPNamespace()
    sio.register_namespace(ns)
    wsgi_app = WSGIApp(sio, socketio_path="/socket.io")
    app = A2CProtocolVersionWSGIMiddleware(wsgi_app, socketio_path="/socket.io", server_version=PROTOCOL_VERSION)
    return sio, ns, app


def create_local_async_server() -> tuple[socketio.AsyncServer, socketio.AsyncNamespace, A2CProtocolVersionASGIMiddleware]:
    """
    中文: 创建本地异步 SMCP 服务器，用于测试。返回的 app 包裹 ``A2CProtocolVersionASGIMiddleware``，
        与生产等价——HTTP/WebSocket 传输层校验 ``a2c_version``，不兼容客户端被拒（#93）。
    English: Create local async SMCP server for testing. The returned app wraps
        ``A2CProtocolVersionASGIMiddleware`` (production-equivalent), validating ``a2c_version`` at
        the transport layer so incompatible clients are rejected (#93).
    """
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        ping_timeout=5,  # 中文: 测试环境使用较短超时 / English: Use shorter timeout for testing
        ping_interval=3,  # 中文: 测试环境使用较短间隔 / English: Use shorter interval for testing
        logger=False,
        engineio_logger=False,
    )
    # 避免关闭时后台任务异常 / avoid background task issues on shutdown
    sio.eio.start_service_task = False
    ns = LocalSMCPNamespace()
    sio.register_namespace(ns)
    asgi_app = socketio.ASGIApp(sio, socketio_path="/socket.io")
    app = A2CProtocolVersionASGIMiddleware(asgi_app, socketio_path="/socket.io", server_version=PROTOCOL_VERSION)
    return sio, ns, app


# ============================================================================
# 中文: 端口与 runner / English: Port helpers and runners
# ============================================================================


def pick_free_port() -> int:
    """中文: 选取随机可用端口 / English: Pick a free ephemeral port on 127.0.0.1."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _run_server_process(port: int, ready_event: Event) -> None:
    """
    中文: 在独立进程中运行同步服务器 / English: Run the sync server in a separate process
    """
    try:
        from werkzeug.serving import make_server  # 惰性导入：仅 runner 需要 [server] extra / lazy: only the runner needs it

        sio, _ns, wsgi_app = create_local_sync_server()
        # 禁用监控任务避免关闭时出错 / Disable monitoring task to avoid shutdown errors
        sio.eio.start_service_task = False

        server = make_server("127.0.0.1", port, wsgi_app, threaded=True)

        # 通知主进程服务器已准备好 / Notify main process that server is ready
        ready_event.set()

        # 运行服务器 / Run server
        server.serve_forever()
    except Exception as e:
        print(f"服务器进程错误 / Server process error: {e}")
        ready_event.set()  # 即使出错也要设置事件，避免主进程无限等待 / Set event even on error


@contextlib.contextmanager
def run_http_server() -> Iterator[tuple[str, int]]:
    """
    中文: 启动一个基于多进程的同步 Socket.IO Server（真实 HTTP 服务），返回 (host, port)。
        进入上下文前在父进程校验 werkzeug 可用性——未装 ``a2c-smcp[server]`` extra 时
        抛携带安装提示的 ImportError（惰性导入契约）。
    English: Start a multiprocess sync Socket.IO server over real HTTP, return (host, port).
        werkzeug availability is validated in the parent process before entering — without the
        ``a2c-smcp[server]`` extra an actionable ImportError is raised (lazy-import contract).
    """
    try:
        importlib.import_module("werkzeug.serving")
    except ImportError as e:
        raise ImportError(_SERVER_EXTRA_HINT) from e

    # 选取随机可用端口 / pick a free port
    port = pick_free_port()

    # 创建进程间通信事件 / Create inter-process communication event
    ready_event = multiprocessing.Event()

    # 启动服务器进程 / Start server process
    server_process = multiprocessing.Process(
        target=_run_server_process,
        args=(port, ready_event),
        daemon=True,
    )
    server_process.start()

    # 等待服务器准备好 / Wait for server to be ready
    if not ready_event.wait(timeout=10):
        server_process.terminate()
        server_process.join(timeout=2)
        raise RuntimeError("服务器进程启动超时 / Server process startup timeout")

    # 额外等待确保端口完全可用 / Extra wait to ensure port is fully available
    time.sleep(0.3)

    try:
        yield "127.0.0.1", port
    finally:
        # 终止服务器进程 / Terminate server process
        if server_process.is_alive():
            server_process.terminate()
            server_process.join(timeout=3)

        # 如果进程仍然存活，强制杀死 / Force kill if still alive
        if server_process.is_alive():
            server_process.kill()
            server_process.join(timeout=1)


class UvicornTestServer:
    """
    中文: 进程内异步 uvicorn 测试服务器（ASGI runner）。uvicorn 惰性导入——未装
        ``a2c-smcp[server]`` extra 时构造即抛携带安装提示的 ImportError。
    English: In-process async uvicorn test server (ASGI runner). uvicorn is imported lazily —
        without the ``a2c-smcp[server]`` extra, construction raises an actionable ImportError.

    Usage:
        @pytest.fixture
        async def start_stop_server():
            server = UvicornTestServer(app, port=port)
            await server.up()
            yield
            await server.down()
    """

    def __init__(self, app: Any = None, host: str = "127.0.0.1", port: int = 8000) -> None:
        """Create a Uvicorn test server.

        Args:
            app: the ASGI application (typically the middleware-wrapped ASGIApp). Defaults to None.
            host: the host interface. Defaults to "127.0.0.1".
            port: the port. Defaults to 8000.
        """
        try:
            import uvicorn
        except ImportError as e:
            raise ImportError(_SERVER_EXTRA_HINT) from e

        startup_done = asyncio.Event()
        self._startup_done = startup_done

        # 中文: 内层子类仅用于在 startup 完成时点亮就绪事件（等价原 mock_uv_server 的覆写手法）；
        #       事件经闭包捕获——它属于外层实例而非内层 server
        # English: inner subclass signals readiness from startup (same trick as the old mock_uv_server);
        #       the event is captured by closure since it belongs to the outer instance
        class _StartupSignalingServer(uvicorn.Server):
            async def startup(self, sockets: list[Any] | None = None) -> None:
                await super().startup(sockets=sockets)
                # self.config.setup_event_loop()  # 从0.36版本开始，不再需要这个方法 / not needed since 0.36
                startup_done.set()

        self._server = _StartupSignalingServer(config=uvicorn.Config(app, host=host, port=port))

    async def up(self) -> None:
        """Start up server asynchronously"""
        self._serve_task = asyncio.create_task(self._server.serve())
        await self._startup_done.wait()

    async def down(self, force: bool = False) -> None:
        """Shut down server asynchronously

        Args:
            force (bool): 中文: 是否强制快速关闭，跳过优雅关闭等待 / English: Force fast shutdown without graceful wait
        """
        self._server.should_exit = True
        if force:
            # 中文: 强制退出，不等待连接清理 / English: Force exit without waiting for connection cleanup
            self._server.force_exit = True
            # 中文: 取消服务任务以立即停止 / English: Cancel serve task to stop immediately
            if hasattr(self, "_serve_task") and not self._serve_task.done():
                self._serve_task.cancel()
                try:
                    # 中文: 减少超时时间到 0.2 秒以加快测试速度 / English: Reduce timeout to 0.2s to speed up tests
                    await asyncio.wait_for(self._serve_task, timeout=0.2)
                except (TimeoutError, asyncio.CancelledError):
                    pass
        else:
            await self._serve_task
