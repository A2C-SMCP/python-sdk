# -*- coding: utf-8 -*-
# filename: handshake.py
# @Author  : JQQ
# @Software: PyCharm

"""
连接握手共享工具 / Shared connection-handshake helpers

供 Agent（async / sync）与 Computer 客户端复用，避免握手 URL 拼接与 4008 识别逻辑三处重复。
Reused by the Agent (async / sync) and Computer clients to avoid triplicating the handshake
URL build and 4008 detection logic.

协议依据 / Protocol: a2c-smcp-protocol docs/specification/versioning.md (§连接握手流程)
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 协议规定的版本握手 query key / Protocol-defined version handshake query key
A2C_VERSION_QUERY_KEY = "a2c_version"

# MUST 默认 transports：首个握手走 HTTP polling，确保 4008 的 HTTP 400 body 可被读取
# （versioning.md §1：polling-first 自 v0.2.1 由 SHOULD 收紧为 MUST）
# Default transports (MUST): first handshake on HTTP polling so the 4008 HTTP 400 body is
# readable (versioning.md §1: polling-first tightened from SHOULD to MUST since v0.2.1)
DEFAULT_HANDSHAKE_TRANSPORTS: list[str] = ["polling", "websocket"]


def enforce_polling_first(transports: Any) -> tuple[Any, bool]:
    """
    落实 versioning.md §1 polling-first **MUST**：SDK 不静默放行调用方显式 WS-only。
    Enforce versioning.md §1 polling-first **MUST**: the SDK never silently allows a
    caller-forced WebSocket-only handshake.

    判定：``transports`` 是非空序列且首元素不是 ``"polling"``（含 ``["websocket"]`` /
    ``["websocket","polling"]``）→ 视为违反 §1，强制重注入
    :data:`DEFAULT_HANDSHAKE_TRANSPORTS`（仍保留 websocket 供握手后升级），返回
    ``(corrected, True)``；否则原样返回 ``(transports, False)``。

    ``None`` / 空 / 已 polling-first → 不改动（python-socketio ``None`` 默认即 polling 优先）。
    Returns ``(effective_transports, was_overridden)``.
    """
    if isinstance(transports, (list, tuple)) and len(transports) > 0 and transports[0] != "polling":
        return list(DEFAULT_HANDSHAKE_TRANSPORTS), True
    return transports, False


def build_handshake_url(url: str, protocol_version: str) -> str:
    """
    在连接 URL 上追加 ``a2c_version`` query 参数，**保留**调用方既有 query。
    Append the ``a2c_version`` query parameter to the connection URL, **preserving** any
    caller-supplied query.

    协议 MUST：握手版本以 SDK 的 ``PROTOCOL_VERSION`` 常量为准；调用方若误传同名 query
    会被覆盖，杜绝版本漂移。Protocol MUST: the handshake version is the SDK's
    ``PROTOCOL_VERSION`` constant; a caller-supplied duplicate key is overridden to
    prevent version drift.
    """
    parts = urlparse(url)
    # 去重既有的 a2c_version（防漂移），保留其余所有 query 原样
    # Drop any pre-existing a2c_version (anti-drift); keep every other query pair intact
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != A2C_VERSION_QUERY_KEY]
    kept.append((A2C_VERSION_QUERY_KEY, protocol_version))
    return urlunparse(parts._replace(query=urlencode(kept)))


def extract_4008_payload(exc: BaseException) -> dict[str, Any] | None:
    """
    从 socketio ``ConnectionError`` 中提取 4008 flat ErrorPayload；非 4008 返回 ``None``。
    Extract the 4008 flat ErrorPayload from a socketio ``ConnectionError``; return ``None``
    when the error is not a protocol version mismatch.

    实现说明 / Implementation note:
        python-socketio 5.x + python-engineio 4.x 在 HTTP polling 收到 ``400`` 时抛出
        ``engineio.exceptions.ConnectionError(msg, parsed_json_body)``，socketio 再以
        ``raise socketio.exceptions.ConnectionError(args[0]) from <engineio_exc>`` 链式重抛。
        因此**权威**的 body 载体是 ``exc.__cause__.args[1]``（已解析的 dict）；协议
        versioning.md 的 ``json.loads(str(exc))`` 参考片段为**非规范**示例，仅作防御性回退。
        Hence the authoritative body carrier is ``exc.__cause__.args[1]`` (parsed dict);
        versioning.md's ``json.loads(str(exc))`` snippet is a non-normative example kept
        only as a defensive fallback.

        body 非 JSON / 无 4008 标识时返回 ``None``，调用方据此**保持原异常**（不可把任意
        连接失败误判为版本错误）。When the body is not JSON / not a 4008, returns ``None``
        so the caller preserves the original exception (never mislabel an arbitrary
        connection failure as a version error).
    """
    # 1) 权威路径：engineio 链式异常第二参 = 解析后的 body dict
    #    Authoritative path: the chained engineio exception's 2nd arg is the parsed body dict
    cause = exc.__cause__
    if cause is not None:
        args: tuple[Any, ...] = getattr(cause, "args", ())
        if len(args) > 1 and isinstance(args[1], dict) and args[1].get("code") == 4008:
            return args[1]
    # 2) 防御性回退：协议参考实现路径 json.loads(str(exc))
    #    Defensive fallback: the protocol reference-impl path json.loads(str(exc))
    try:
        body = json.loads(str(exc))
    except (ValueError, TypeError):
        return None
    if isinstance(body, dict) and body.get("code") == 4008:
        return body
    return None
