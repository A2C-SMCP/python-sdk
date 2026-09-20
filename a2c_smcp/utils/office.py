# -*- coding: utf-8 -*-
# filename: office.py
# @Time    : 2026/09/19
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Office 入房 ACK 判定（单一权威）/ Office join-ack verdict (single source of truth).

存在意义 / Why this module：
    ``server:join_office`` 的 ACK 判定同时被 **四处** 消费——Computer 显式 join、Computer 自动回房、
    Agent 自动回房（async + sync）——判定漂移会让同一份服务端裁决在不同路径上得出不同结论（例如一处
    把空响应当失败、另一处当成功）。此处收敛为唯一实现，与 :mod:`a2c_smcp.utils.mime` 同款"单一权威"
    约定。/ The join ack is consumed by the Computer's explicit join, both sides' automatic office
    replays (async + sync); a single implementation prevents verdict drift.

#203 语境 / #203 context：
    Socket.IO 房间成员关系属于**会话**，传输层断线自动重连后 namespace 换新 SID，客户端必须重放
    ``server:join_office``（镜像 rust-sdk#204）。回房用 :data:`OFFICE_REJOIN_TIMEOUT` 做**有界**等待：
    socketio 的 ``call()`` 在连接已断时不会快速失败，会吃满默认超时（60s）；无界等待会让 office 操作
    长时间互斥。/ The replay uses a bounded ack wait because ``call()`` does not fail fast on a dead
    connection and would otherwise burn the 60s default.

#214 语境 / #214 context：
    协议 v0.5.0 废除 ``(bool, str | None)`` 元组形态：**成功 = 空 ack**，**失败 = flat
    ``ErrorPayload``**（顶层 ``code``）。本模块随之改为返回 :class:`JoinOfficeVerdict`——把协议码
    显式带出，供 #212 的「瞬态冲突有界退避重试」按 ``code`` 分流，无需二次解析原始载荷。
    Protocol v0.5.0 replaced the `(bool, str|None)` tuple with an empty-ack / flat-`ErrorPayload`
    contract; the verdict now carries the protocol code for #212's bounded retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

OFFICE_REJOIN_TIMEOUT = 10
"""自动回房等待 ACK 的上界（秒）——镜像 rust-sdk 的 10s 有界等待。

Bounded ack wait for the automatic office replay, mirroring rust-sdk's 10s bound. 显式
``join_office`` 仍用 socketio 默认超时，不受此常量影响 / explicit joins keep the socketio default.
"""

NO_RESPONSE_MESSAGE = "服务器未返回结果 / No response from server"
"""**无法判定**（既非空 ack、也非可识别的 flat ErrorPayload）时的兜底文案。

Fallback text for an *indeterminate* ack shape. 注意它**不再**表示"空响应失败"——自 v0.5.0 起
``None``（空 ack）是**成功**；真正落进本文案的是形状不认识的响应（如已废除的元组形态）。
"""


@dataclass(frozen=True, slots=True)
class JoinOfficeVerdict:
    """
    ``server:join_office`` ack 的裁决 / Verdict of a ``server:join_office`` ack.

    三分歧 / Three-way split（协议 error-handling.md:177-205）:

    ==================  ========  ============  ==========================================
    服务端回应           ``ok``    ``code``      ``message``
    ==================  ========  ============  ==========================================
    空 ack（``None``）   ``True``  ``None``      ``""``
    flat ErrorPayload   ``False`` 协议码        ``payload["message"]``
    其它（无法判定）      ``False``  ``None``      :data:`NO_RESPONSE_MESSAGE`
    ==================  ========  ============  ==========================================

    ``code`` 是**唯一**可用于机器分流的字段：#212 只对 ``4101`` / ``4105``（且仅当本端刚经历
    传输层重连）做有界退避重试，其余码一律放弃并如实报错。
    ``code`` is the only machine-actionable field — #212 retries bounded/backoff on 4101/4105 only.
    """

    ok: bool
    code: int | None
    message: str


def parse_join_ack(result: Any) -> JoinOfficeVerdict:
    """
    解析 ``server:join_office`` 的 ACK → :class:`JoinOfficeVerdict`。

    Parse a ``server:join_office`` ack into a :class:`JoinOfficeVerdict`.

    判定规则 / Verdict rules（协议 error-handling.md:177-205）:
      - ``None``（空 ack——socketio 把**零参** ACK 折叠为 ``None``）→ **成功**；
      - ``dict`` 且顶层含 ``code`` → **显式拒绝**，带出协议码；
      - 其余一切形状 → **无法判定**（``ok=False`` 且无码）。

    **刻意不再兼容 ``(bool, str | None)`` 元组**：v0.x 下 MINOR 严格匹配（versioning.md），跨版本
    对端在握手层即被拒绝、物理上不可能互联 ⇒ 兼容分支没有真实读者，只会把「未迁移的调用方」
    静默判成成功（正是本次要修的缺陷形态）。保留它等于给旧形状留一条假绿通道。
    The legacy tuple shape is deliberately NOT accepted: MINOR-strict handshake makes mixed-version
    peers impossible, so a compatibility branch would only mask un-migrated callers.

    **未知码仍算拒绝**（宁严勿宽）：未来协议新增码时，旧 SDK 必须 fail-safe 到「被拒」，
    绝不静默假装成功。/ Unknown codes still count as rejection — fail safe, never fake success.

    Args:
        result (Any): ``call(JOIN_OFFICE_EVENT, ...)`` 的返回值 / the ack payload as received.

    Returns:
        JoinOfficeVerdict: 见 :class:`JoinOfficeVerdict` 的三分歧表。
    """
    if result is None:
        return JoinOfficeVerdict(ok=True, code=None, message="")
    if isinstance(result, dict) and "code" in result:
        try:
            code: int | None = int(result["code"])
        except (TypeError, ValueError):
            # 码不可解析（如字符串化失败）⇒ 仍判拒绝、只是无码可分流（宁严勿宽）
            code = None
        return JoinOfficeVerdict(ok=False, code=code, message=str(result.get("message", "")))
    return JoinOfficeVerdict(ok=False, code=None, message=NO_RESPONSE_MESSAGE)
