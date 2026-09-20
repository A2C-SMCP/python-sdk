# -*- coding: utf-8 -*-
"""
* 文件名: room_acks
* 描述: #214 —— 房间事件 ack 的**测试侧**判定助手（单一权威）。

        集成 / e2e 用例里大量站点只是「装配」——把客户端放进房间以便测别的东西——它们的成功判据
        随 v0.5.0 从 `(True, None)` 元组变为**空 ack**（`None`）。把这条判据收敛到一处，避免 20+
        个文件各自手写、各自漂移。

        **刻意不做**成「生产同款解析」：`assert_empty_ack` 只判「成功 = `None`」这一条线上契约，
        不调用 `parse_join_ack` 做裁决（那会让测试与生产用同一份判定，回归互相掩盖）。断言型站点
        （真正在验证拒绝码的用例）应**直接**断言 payload 字段，不要用本模块。

#214 test-side helpers for the room-event ack contract. Deliberately minimal: this only asserts the
"success = empty ack" wire contract; cases that verify rejection *codes* assert payloads directly.
"""

from __future__ import annotations

from typing import Any


def assert_empty_ack(ack: Any, *, action: str = "server:join_office") -> None:
    """断言成功 ack 是协议规定的**空 ack**（`None`）。

    协议依据 / Protocol: error-handling.md:177-179——``server:join_office`` /
    ``server:leave_office`` 成功回**空 ack**（v0.5.0 前为 `(True, None)` 元组，已废除）。
    """
    if ack is None:
        return
    raise AssertionError(
        f"{action} 成功应回空 ack（None），实得 {ack!r}。"
        f"若收到 flat ErrorPayload，说明本端把失败当成了成功——请检查该站点是否该用拒绝断言。",
    )


def assert_rejected_ack(ack: Any, code: int, *, action: str = "server:join_office") -> dict[str, Any]:
    """断言 ack 是带指定协议码的 flat `ErrorPayload`，并返回该 payload 供进一步断言。"""
    assert isinstance(ack, dict), f"{action} 被拒时应回 flat ErrorPayload，实得 {ack!r}"
    assert ack.get("code") == code, f"{action} 期望 code={code}，实得 {ack!r}"
    return ack
