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

    ``details`` 是诊断容器，Agent MUST NOT 透传给最终用户（防泄露）。
    ``details`` is a diagnostic container; the Agent MUST NOT propagate it to end users.
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
