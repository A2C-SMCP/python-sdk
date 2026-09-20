# -*- coding: utf-8 -*-
"""
* 文件名: test_office
* 描述: #214 —— `server:join_office` 的 ACK 判定（`parse_join_ack`）判定矩阵。

        协议依据 / Protocol: error-handling.md:177-205
          - 成功 = **空 ack**（`None`）；
          - 失败 = flat `ErrorPayload`（顶层 `code`），且 `code` 须**机器可判**（#212 据此分流
            瞬态冲突的有界退避重试）。

        本文件是该单一权威的**直接**单测（此前只有经客户端状态机的间接覆盖）。

#214 verdict-matrix tests for the single authoritative join-ack parser.
"""

from __future__ import annotations

from typing import Any

import pytest

from a2c_smcp.utils.office import NO_RESPONSE_MESSAGE, parse_join_ack


class TestParseJoinAckVerdicts:
    """判定矩阵 / verdict matrix。"""

    def test_empty_ack_is_success(self) -> None:
        """空 ack（`None`）⇒ **成功**，且不带码。"""
        verdict = parse_join_ack(None)

        assert verdict.ok is True
        assert verdict.code is None
        assert verdict.message == ""

    def test_error_payload_is_explicit_rejection_with_code(self) -> None:
        """flat `ErrorPayload` ⇒ **显式拒绝**，并带出协议码供下游分流。"""
        verdict = parse_join_ack({"code": 4101, "message": "Room already has an agent"})

        assert verdict.ok is False
        assert verdict.code == 4101
        assert verdict.message == "Room already has an agent"

    def test_error_payload_with_details_still_parses(self) -> None:
        """`details` 的存在不影响判定（协议 details 为诊断容器）。"""
        verdict = parse_join_ack(
            {"code": 4105, "message": "Name already taken in room", "details": {"office_id": "o", "role": "computer"}}
        )

        assert verdict.ok is False
        assert verdict.code == 4105

    def test_error_payload_without_message_is_still_code_carried(self) -> None:
        """缺 `message` 时仍以码为准（宁可拒绝，不可误判成功）。"""
        verdict = parse_join_ack({"code": 4106})

        assert verdict.ok is False
        assert verdict.code == 4106

    @pytest.mark.parametrize("bad_code", [None, "not-a-number", {"nested": 1}])
    def test_unparsable_code_still_counts_as_rejection(self, bad_code: Any) -> None:
        """`code` 存在但**不可解析为整数** ⇒ 仍是拒绝（`code=None` 表示"有裁决但无码可分流"）。

        不能因为码解析失败就退回「无法判定」——那会把一次**明确的**服务端裁决降级成"没收到裁决"，
        进而触发清空房号这类与实情相反的动作。宁严勿宽。
        """
        verdict = parse_join_ack({"code": bad_code, "message": "weird"})

        assert verdict.ok is False
        assert verdict.code is None
        assert verdict.message == "weird"

    def test_unknown_protocol_code_is_still_rejection(self) -> None:
        """未知码（未来协议新增）⇒ **仍算拒绝**：宁严勿宽，绝不静默假装成功。"""
        verdict = parse_join_ack({"code": 4199, "message": "future code"})

        assert verdict.ok is False
        assert verdict.code == 4199

    @pytest.mark.parametrize(
        "inconclusive",
        [
            {},  # 空 dict：不是 ErrorPayload 也不是成功
            "",
            0,
            [],
            (),
            [True, None],  # 已废除的元组形态：不得被当成成功
            [False, "old shape"],
            {"message": "no code"},  # 无码 dict：形状不认识
            "ok",
        ],
    )
    def test_inconclusive_shapes_are_not_success(self, inconclusive: Any) -> None:
        """无法判定的形状 ⇒ 既不判成功也不给码（调用方按「未获裁决」保守处置）。"""
        verdict = parse_join_ack(inconclusive)

        assert verdict.ok is False, f"{inconclusive!r} 不得被判为成功"
        assert verdict.code is None, f"{inconclusive!r} 不携带协议码"
        assert verdict.message == NO_RESPONSE_MESSAGE

    def test_legacy_tuple_success_is_deliberately_not_accepted(self) -> None:
        """回归守卫：`(bool, str|None)` 元组形态已废除，**刻意**不再兼容（避免掩盖未迁移的调用方）。"""
        assert parse_join_ack((True, None)).ok is False
        assert parse_join_ack([True, None]).ok is False
