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

from typing import ClassVar

from a2c_smcp.smcp import ErrorCode


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


class SMCPNamespaceError(Exception):
    """
    SMCP 命名空间路由/隔离校验失败（role / office(Room) / session 不匹配）。
    SMCP namespace routing/isolation check failure (role / office(Room) / session mismatch).

    适用范围 / Scope：Server 端 ``on_client_*`` / ``on_server_*`` 处理器，以及 Computer 端
    socketio handler 的隔离校验——跨房间访问、错误角色发起、computer/agent 标识不匹配等。
    Server-side ``on_client_*`` / ``on_server_*`` handlers and the Computer-side socketio
    handler isolation checks — cross-room access, wrong-role initiation, computer/agent
    identity mismatch, etc.

    取代原先的 ``assert``：``assert`` 在 ``python -O`` / ``PYTHONOPTIMIZE`` 下被字节码整体
    剥离，会令隔离校验被静默关闭（协议将房间隔离列为**不可违反约束**）。显式 ``raise``
    保证校验在任何运行模式下均生效；属 sid/session 路由层拒绝，**非** 4014/4015 业务错误，
    故沿用与同处理器既有路由失败一致的"抛异常"形态，不回 flat ErrorPayload。
    Replaces the former ``assert``: ``assert`` is stripped wholesale under ``python -O`` /
    ``PYTHONOPTIMIZE``, silently disabling the isolation check (the protocol lists room
    isolation as an **inviolable constraint**). An explicit ``raise`` guarantees the check
    holds in every run mode. This is a sid/session routing-layer rejection, **not** a
    4014/4015 business error, so it keeps the same "raise" shape as the pre-existing
    routing failures in the same handlers and does NOT return a flat ErrorPayload.

    放置于顶层包：Server 与 Computer 客户端共用，避免 Computer 反向依赖 ``a2c_smcp.server``。
    Placed at the top-level package: shared by both Server and Computer client, avoiding a
    reverse dependency from the Computer client onto ``a2c_smcp.server``.

    **范围收窄（#214 / v0.5.0）**：三个**房间管理事件**中唯一走本异常的 ``server:list_room``
    越权拒绝，自 v0.5.0 起改为回 flat ``ErrorPayload(4104)``（协议 §房间管理错误响应），不再走
    「抛异常 ⇒ 静默不 ack」形态——「挂到客户端自身超时」与「立即收到结构化错误」是两种客户端
    可感行为。本异常继续适用于 ``client:*`` 路由层与 Computer 端 socketio handler 的隔离校验
    （那里的静默不 ack 由协议 §飞行中断连 许可）。
    Scope narrowed by #214: the room-event cross-room rejection now returns a flat
    ``ErrorPayload(4104)``; this exception still covers ``client:*`` routing and the
    Computer-side handlers.

    协议依据 / Protocol: docs/specification/error-handling.md（§通用错误码 403 / §4104 Cross Room Access）
                          docs/specification/security.md（§房间隔离）
    """


class RoomRejection(ValueError):
    """
    房间管理事件的**业务拒绝**（协议 flat ``ErrorPayload`` 的结构化载体）。
    A room-management business rejection carrying the protocol error code.

    适用 / Scope：``server:join_office`` 的三个业务闸门——目标房已有 Agent（``4101``）、
    房内已有同 role 同名会话（``4105``）、Agent 已在其它房（``4106``）。由
    ``SMCPNamespace.enter_room`` / ``BaseNamespace._ensure_name_registerable`` 抛出，由
    ``on_server_join_office`` 捕获并**按 code 转换为 flat ErrorPayload**（#214）。

    **为什么继承 ``ValueError``**：这些闸门历来抛 ``ValueError``（``except ValueError`` /
    ``pytest.raises(ValueError)`` 是既有契约）。换型只增加**信息量**（带上协议码），不改变
    「校验失败」这一**类别**——故既有调用方无需改动。

    **消息构造上是安全的**：``str(e)`` 只含**与发起者自身相关**的上下文，**绝不**含对端会话
    标识（sid 等）。即使将来有人误把 ``str(e)`` 塞进 ack 也无法泄露；冗长诊断（含对端 sid）
    一律由抛出点的 ``logger.warning`` 承载。见 error-handling.md:149 / :328
    （``details`` **MUST NOT** 携带其它会话的内部标识）。

    Protocol: error-handling.md §连接与房间管理错误码 / §房间管理错误响应（protocol#61，v0.5.0）。
    """

    #: 协议错误码（:class:`~a2c_smcp.smcp.ErrorCode` 取值）；子类逐一钉死。
    code: ClassVar[int] = 0


class RoomFullError(RoomRejection):
    """目标房已有 Agent（``4101``）——一房一 Agent 规则。"""

    code: ClassVar[int] = ErrorCode.ROOM_FULL

    def __init__(self) -> None:
        super().__init__("Room already has an agent")


class NameConflictError(RoomRejection):
    """房内已有同 role 同名会话（``4105``）——``name`` 是 ``client:*`` 的路由地址，房内必须唯一。"""

    code: ClassVar[int] = ErrorCode.NAME_CONFLICT

    def __init__(self) -> None:
        super().__init__("Name already taken in room")


class AlreadyInRoomError(RoomRejection):
    """Agent 已在其它房又请求入新房（``4106``）——Agent 换房 **MUST** 是显式两步。"""

    code: ClassVar[int] = ErrorCode.ALREADY_IN_ROOM

    def __init__(self) -> None:
        super().__init__("Agent already in another room")
