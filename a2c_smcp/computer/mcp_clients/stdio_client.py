# -*- coding: utf-8 -*-
# filename: stdio_client.py
# @Time    : 2025/8/19 10:55
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
import asyncio
from collections.abc import Awaitable, Callable

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.session import MessageHandlerFnT

from a2c_smcp.computer.mcp_clients._proc import list_own_child_pids
from a2c_smcp.computer.mcp_clients.base_client import BaseMCPClient

# 进程启动串行锁：在「快照前→进入 stdio_client（spawn）→快照后」期间串行化，确保并发启动多个
# stdio client 时，新出现的子进程被准确归属到本 client（用于「兑底强杀」的 PID 捕获）。
# Spawn lock: serialize the snapshot→spawn→snapshot window so concurrent stdio startups can't
# mis-attribute children across clients when capturing PIDs for the force-kill fallback.
_SPAWN_LOCK = asyncio.Lock()


class StdioMCPClient(BaseMCPClient[StdioServerParameters]):
    def __init__(
        self,
        params: StdioServerParameters,
        state_change_callback: Callable[[str, str], None | Awaitable[None]] | None = None,
        message_handler: MessageHandlerFnT | None = None,
    ) -> None:
        """
        初始化STDIO客户端，支持传入自定义 message_handler
        Initialize STDIO client with optional message_handler
        """
        assert isinstance(params, StdioServerParameters), "params must be an instance of StdioServerParameters"
        super().__init__(params, state_change_callback, message_handler)

    async def _create_async_session(self) -> ClientSession:
        """
        创建异步会话

        Returns:
            ClientSession: 异步会话
        """
        # 在串行窗口内「快照→spawn→快照」捕获本 client 启动的子进程 PID，供基类「兑底强杀」使用。
        # mcp 的 stdio_client 在 enter 期间 spawn 子进程；差集即本 client 的子进程。
        # Capture child PIDs spawned by this client (snapshot→spawn→snapshot under the lock) for the
        # base-class force-kill fallback. mcp's stdio_client spawns the child during context entry.
        async with _SPAWN_LOCK:
            _children_before = list_own_child_pids()
            stdout, stdin = await self._aexit_stack.enter_async_context(stdio_client(self.params))
            self._child_pids = list_own_child_pids() - _children_before
        # 如果提供了 message_handler，则一并传入 ClientSession
        # If message_handler is provided, pass it into ClientSession
        client_session = await self._aexit_stack.enter_async_context(
            ClientSession(stdout, stdin, message_handler=self._message_handler),
        )
        return client_session
