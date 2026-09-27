# -*- coding: utf-8 -*-
# filename: mock_sync_smcp_server.py
# @Time    : 2025/9/30 22:50
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文：同步 SMCP 服务器 Mock 实现，用于同步客户端集成测试。
English: Synchronous SMCP server Mock implementation for sync client integration tests.

v0.5.0 审查 S5：本替身**继承正式** :class:`SyncSMCPNamespace`（对照 async 侧的 ``MockComputerServerNamespace``），
房间管理（``server:join_office`` / ``server:leave_office`` / ``server:list_room`` / ``server:update_*``）全部走正式
实现——``office:`` 房名前缀、通知里的**名字**（而非 sid）、4101/4105/4106/403 的拒绝形态都与线上一致。此前的裸
``Namespace`` 替身自写这些 handler（无 leave、无前缀、通知填 sid、无任何校验），sync Agent 的集成测试因此测不到
v0.5.0 的拒绝形态。

唯一保留的替身行为：``client:*`` 事件直接返回**固定应答**（这些用例里没有真实 Computer 进程可供转发）。
The only stand-in behaviour left is the canned ``client:*`` replies (no real Computer to relay to).
"""

from typing import Any

from mcp.types import CallToolResult, TextContent
from socketio import Server

from a2c_smcp.server.sync_namespace import SyncSMCPNamespace
from a2c_smcp.smcp import (
    GetDeskTopReq,
    GetDeskTopRet,
    GetToolsReq,
    GetToolsRet,
    SMCPTool,
    ToolCallReq,
)
from a2c_smcp.testing import PermissiveSyncAuthenticationProvider
from a2c_smcp.utils.logger import logger


class MockSyncSMCPNamespace(SyncSMCPNamespace):
    """
    中文：同步 SMCP 命名空间 Mock：正式实现 + 放行认证 + ``client:*`` 固定应答。
    English: Production sync namespace with permissive auth and canned ``client:*`` replies.
    """

    def __init__(self) -> None:
        super().__init__(auth_provider=PermissiveSyncAuthenticationProvider())

    def on_client_tool_call(self, sid: str, data: ToolCallReq | None = None, *_extra: Any) -> dict:
        """处理工具调用请求（固定应答，不经 relay）"""
        logger.info(f"Agent {sid} 调用工具 {(data or {}).get('tool_name')}")

        # 返回模拟的工具调用结果
        result = CallToolResult(
            isError=False,
            content=[TextContent(type="text", text="mock tool result")],
        )
        return result.model_dump(mode="json")

    def on_client_get_tools(self, sid: str, data: GetToolsReq | None = None, *_extra: Any) -> GetToolsRet:
        """处理获取工具列表请求（固定应答，不经 relay）"""
        logger.info(f"Agent {sid} 拉取工具列表")

        # 返回模拟的工具列表
        tools = [
            SMCPTool(
                name="echo",
                bundle_id="mocksrv",  # #152 D1：name ≠ bundle_id 分叉（同 server 两工具共享 bundle_id）
                description="echo text",
                params_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                return_schema=None,
            ),
            SMCPTool(
                name="test_tool",
                bundle_id="mocksrv",
                description="test tool",
                params_schema={},
                return_schema=None,
            ),
        ]

        return GetToolsRet(tools=tools, req_id=(data or {})["req_id"])

    def on_client_get_desktop(self, sid: str, data: GetDeskTopReq | None = None, *_extra: Any) -> GetDeskTopRet:
        """处理获取桌面请求（固定应答，不经 relay）。"""
        logger.info(f"Agent {sid} 拉取桌面数据 size={(data or {}).get('desktop_size')}")
        desktops = ["window://mock\n\nhello world"]
        return GetDeskTopRet(desktops=desktops, req_id=(data or {})["req_id"])


def create_sync_smcp_socketio() -> Server:
    """
    创建同步 SMCP Socket.IO 服务器
    Create synchronous SMCP Socket.IO server

    ``async_handlers=True``：与生产装配（``create_local_sync_server``）一致——每个事件一个线程，线程间竞态
    （断连收尾 vs 入房、并发同名 join）在集成层才有机会暴露；串行分发会把它们全部遮住。
    Matches production dispatch (one thread per event) so cross-thread races are not masked.

    Returns:
        Server: Socket.IO 服务器实例
    """
    sio = Server(
        cors_allowed_origins="*",
        ping_timeout=60,
        ping_interval=25,
        async_handlers=True,
        always_connect=True,
    )

    # 注册 SMCP 命名空间
    sio.register_namespace(MockSyncSMCPNamespace())

    return sio
