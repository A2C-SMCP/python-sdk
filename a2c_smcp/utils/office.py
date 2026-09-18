# -*- coding: utf-8 -*-
# filename: office.py
# @Time    : 2026/09/19
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Office 入房 ACK 判定（单一权威）/ Office join-ack verdict (single source of truth).

存在意义 / Why this module：
    ``server:join_office`` 的 ACK 判定同时被 **三处** 消费——Computer 显式 join、Computer 自动回房、
    Agent 自动回房（async + sync）——判定漂移会让同一份服务端裁决在不同路径上得出不同结论（例如一处
    把空响应当失败、另一处当成功）。此处收敛为唯一实现，与 :mod:`a2c_smcp.utils.mime` 同款"单一权威"
    约定。/ The join ack is consumed by the Computer's explicit join, both sides' automatic office
    replays and the Agent's async/sync replay; a single implementation prevents verdict drift.

#203 语境 / #203 context：
    Socket.IO 房间成员关系属于**会话**，传输层断线自动重连后 namespace 换新 SID，客户端必须重放
    ``server:join_office``（镜像 rust-sdk#204）。回房用 :data:`OFFICE_REJOIN_TIMEOUT` 做**有界**等待：
    socketio 的 ``call()`` 在连接已断时不会快速失败，会吃满默认超时（60s）；无界等待会让 office 操作
    长时间互斥。/ The replay uses a bounded ack wait because ``call()`` does not fail fast on a dead
    connection and would otherwise burn the 60s default.
"""

from __future__ import annotations

from typing import Any

OFFICE_REJOIN_TIMEOUT = 10
"""自动回房等待 ACK 的上界（秒）——镜像 rust-sdk 的 10s 有界等待。

Bounded ack wait for the automatic office replay, mirroring rust-sdk's 10s bound. 显式
``join_office`` 仍用 socketio 默认超时，不受此常量影响 / explicit joins keep the socketio default.
"""

NO_RESPONSE_MESSAGE = "服务器未返回结果 / No response from server"
"""空响应（``None`` / 空列表）的失败文案 / failure text for an empty ack."""


def parse_join_ack(result: Any) -> tuple[bool, str]:
    """
    解析 ``server:join_office`` 的 ACK → ``(是否成功, 失败原因)``。

    Parse a ``server:join_office`` ack into ``(ok, error_message)`` — the message is empty on success.

    判定规则（与历史实现逐条等价）/ Verdict rules (equivalent to the historical inline checks):
      - ``[ok, msg]`` / ``(ok, msg)``（长度 ≥ 2）→ ``bool(ok)``，失败时取 ``msg`` 文案；
      - 空响应（``None`` / ``[]`` / ``""`` 等 falsy）→ **失败**：服务端未给出裁决；
      - 其余（如单项 ``[True]``）→ 成功（服务端显式回 true，无错误文案）。

    Args:
        result (Any): ``call(JOIN_OFFICE_EVENT, ...)`` 的返回值 / the ack payload as received.

    Returns:
        tuple[bool, str]: 是否成功，以及失败原因（成功时为空串）。
    """
    if isinstance(result, (list, tuple)) and len(result) >= 2:
        if result[0]:
            return True, ""
        return False, str(result[1])
    if not result:
        return False, NO_RESPONSE_MESSAGE
    return True, ""
