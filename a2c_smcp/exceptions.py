# -*- coding: utf-8 -*-
# filename: exceptions.py
# @Author  : JQQ
# @Software: PyCharm

"""
A2C-SMCP 顶层共享异常 / A2C-SMCP top-level shared exceptions

协议版本握手相关异常。Agent 与 Computer 客户端共用，故置于顶层包——与协议参考实现
``from a2c_smcp.exceptions import ProtocolVersionError`` 路径一致，且避免 Computer 客户端
反向依赖 ``a2c_smcp.agent`` 子包。
Protocol version handshake exception. Shared by both the Agent and Computer clients, hence
placed at the top-level package — matching the protocol reference impl import path and
avoiding a reverse dependency from the Computer client onto the ``a2c_smcp.agent`` subpackage.

协议依据 / Protocol: a2c-smcp-protocol docs/specification/versioning.md (§连接握手流程)
                      docs/specification/error-handling.md (§协议版本不匹配 4008)
"""

from __future__ import annotations


class ProtocolVersionError(Exception):
    """
    协议版本不兼容（4008 Protocol Version Mismatch）。
    Protocol version incompatibility (4008 Protocol Version Mismatch).

    客户端在 Socket.IO 连接 URL query 中声明的 ``a2c_version`` 与 Server 不兼容时，Server
    在 HTTP 握手层返回 ``400`` + flat ErrorPayload(code=4008)。SDK 解析后**主动断开连接**
    再抛出本异常，交由用户业务代码处置（升级 SDK / 切换 Server 实例 / 报告运维）。
    When the ``a2c_version`` a client declares in the Socket.IO connect URL query is
    incompatible with the Server, the Server returns a ``400`` + flat ErrorPayload(code=4008)
    at the HTTP handshake layer. The SDK parses it, **proactively disconnects**, then raises
    this exception for user code to handle (upgrade SDK / switch Server / report to ops).

    body 字段缺失（非完整 JSON）时对应属性为 ``None``——仍抛出本异常，保证 SDK 绝不静默重试。
    Missing body fields (incomplete JSON) leave the corresponding attribute ``None`` — the
    exception is still raised so the SDK never silently retries.
    """

    def __init__(
        self,
        *,
        client_version: str | None = None,
        server_version: str | None = None,
        min_supported: str | None = None,
        max_supported: str | None = None,
        message: str = "Protocol version mismatch",
    ) -> None:
        self.client_version = client_version
        self.server_version = server_version
        self.min_supported = min_supported
        self.max_supported = max_supported
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        # 含 client / server 版本对比，便于用户一眼定位不兼容根因
        # Includes client / server version comparison for at-a-glance diagnosis
        return (
            f"{self.message} "
            f"(client_version={self.client_version}, server_version={self.server_version}, "
            f"min_supported={self.min_supported}, max_supported={self.max_supported})"
        )
