# -*- coding: utf-8 -*-
# filename: errors.py
# @Author  : JQQ
# @Software: PyCharm

"""
Agent 端协议错误 / Agent-side protocol errors

A2C-SMCP 协议级错误经 Socket.IO ack 第一参以 **flat ErrorPayload** 回传（无嵌套 envelope）。
A2C-SMCP protocol errors are returned as a **flat ErrorPayload** in the Socket.IO ack first arg
(no nested envelope). See a2c-smcp-protocol/docs/specification/error-handling.md.
"""

from __future__ import annotations

from typing import Any, cast

from a2c_smcp.smcp import ErrorPayload, is_protocol_error_payload


class SMCPProtocolError(Exception):
    """
    A2C-SMCP 协议级错误（flat ErrorPayload）。
    A2C-SMCP protocol-level error (flat ErrorPayload).

    当 Agent SDK 在 Socket.IO ack 中识别到 flat ErrorPayload（顶层含 ``code``）时抛出，
    覆盖范围 / Covers:
      - ``client:get_resources``：``4014`` / ``4015``（顶层平铺 ``mcp_server``(bundle_id) / ``capability``）
      - ``client:get_skill[s]``：``4014`` 复用（SKILL ``name`` 合法但未命中）/ ``4016`` Invalid Name /
        ``4017`` Skill Resource Not Accessible（v0.2.1 ``details.reason``）
      - ``client:get_blob``：``4018 Blob Not Accessible``（v0.2.1 ``details.reason``）
      - ``server:join_office``（#218）：入房被拒 —— ``400``（载荷畸形）/ ``403``（同连接改名，文案追加
        会话身份提示）/ ``4101``（房内已有 Agent）/ ``4105``（同名）/ ``4106``（已在它房）。**未知码同样
        抛出**（宁严勿宽：未来协议新增码时绝不静默放过）。

    **``.code == -1`` 是契约（#218 写成）**：当服务端拒绝但**码不可解析**或 ack 形状不认识（「未获裁决」）
    时，载荷**省略 ``code`` 键**（写 ``{"code": None}`` 会让 ``int(None)`` 抛 ``TypeError``）⇒
    ``.code`` 取默认值 ``-1``，``.message`` 仍带服务端文案。调用方按 ``e.code`` 分流时须把 ``-1``
    当作「未获裁决」处理。/ ``-1`` means "rejected but no parseable code"; the key is omitted on purpose.

    ``details`` 是诊断容器，Agent MUST NOT 透传给最终用户（防泄露）。
    ``details`` is a diagnostic container; the Agent MUST NOT propagate it to end users.

    **入房路径刻意不走** :func:`raise_for_error_payload`：该 helper 以 ``code ∈ ErrorCode`` 白名单为
    闸，会把未知码与码不可解析的拒绝静默放过 —— 正是 #218 要消灭的「入房被拒零感知」（见
    ``utils/office.py`` 的 ``build_join_failure_payload``）。
    """

    def __init__(self, payload: ErrorPayload) -> None:
        self.payload: ErrorPayload = payload
        self.code: int = int(payload.get("code", -1))
        self.error_message: str = str(payload.get("message", ""))
        # 4014 / 4015 顶层 code-specific 字段（``mcp_server`` = bundle_id，协议 #18）/ Top-level fields (4014 / 4015)
        self.mcp_server: str | None = payload.get("mcp_server")
        self.capability: str | None = payload.get("capability")
        # 4016 / 4017 / 4018 details 容器（v0.2.1 起 code-specific 字段下沉到 details）
        # details container for 4016 / 4017 / 4018 (since v0.2.1 code-specific fields live under details)
        self.details: dict[str, Any] = dict(payload.get("details") or {})
        # 4017 / 4018 共用 ``details.reason`` 开放枚举（解析方 MUST 容忍未知值兜底）
        # ``details.reason`` open enum shared by 4017 / 4018; parsers MUST default to fail-safe on unknown
        self.reason: str | None = self.details.get("reason")
        super().__init__(f"[{self.code}] {self.error_message}")


def raise_for_error_payload(response: Any) -> None:
    """
    若响应是 flat ErrorPayload（顶层 ``code`` 属协议错误码），抛出 :class:`SMCPProtocolError`。
    Raise :class:`SMCPProtocolError` if the response is a flat ErrorPayload
    (top-level ``code`` is a protocol error code).

    协议依据 / Protocol: error-handling.md —— 无嵌套 envelope，禁止二次 unwrap。
    No nested envelope, no re-unwrap.

    Args:
        response (Any): Socket.IO ack 返回值 / Socket.IO ack return value.
    """
    if is_protocol_error_payload(response):
        raise SMCPProtocolError(cast(ErrorPayload, response))
