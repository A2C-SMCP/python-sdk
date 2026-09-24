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
from unittest.mock import MagicMock

import pytest

from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.utils import office as office_mod
from a2c_smcp.utils.office import (
    JOIN_VALIDATION_REJECTION_CODES,
    NO_RESPONSE_MESSAGE,
    OFFICE_JOIN_TIMEOUT,
    OFFICE_REJOIN_RETRY_BASE_DELAY,
    OFFICE_REJOIN_RETRY_BUDGET,
    OFFICE_REJOIN_RETRY_MAX_DELAY,
    TRANSIENT_JOIN_CONFLICT_CODES,
    JoinOfficeVerdict,
    build_join_failure_payload,
    is_validation_rejection,
    join_failure_message,
    log_join_rejection,
    parse_join_ack,
    rejoin_retry_delay,
    resolve_join_failure,
)


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


class TestOfficeJoinTimeoutConstant:
    """常量硬切 / hard rename（#218 裁决 4、C9）。"""

    def test_bounded_wait_constant_is_shared_by_agent_join_paths(self) -> None:
        """Agent 两侧显式 join 与自动回房共用同一有界等待（10s）。"""
        assert OFFICE_JOIN_TIMEOUT == 10

    def test_legacy_rejoin_alias_is_gone(self) -> None:
        """旧名 `OFFICE_REJOIN_TIMEOUT` **不得**留别名：两套名字并存必然漂移。"""
        import a2c_smcp.utils.office as office_mod

        assert not hasattr(office_mod, "OFFICE_REJOIN_TIMEOUT")


class TestResolveJoinFailureMatrix:
    """失败效应矩阵（单一权威）/ the single-authoritative failure-effect matrix.

    协议未覆盖「失败后 SDK 本地状态如何处置」⇒ 本矩阵是 SDK 自治裁量（#217 裁决 2）。
    """

    def test_explicit_rejection_falls_back_to_confirmed(self) -> None:
        """明确拒绝（顶层带码）⇒ `desired := confirmed`——房号不被清空。

        这是 `_confirmed_office` 存在的理由：房 A 已在房中换房 B 被拒时，若一律清空，
        重连后将**静默掉出**仍有效的房 A。
        """
        effect = resolve_join_failure(
            JoinOfficeVerdict(ok=False, code=4106, message="Agent already in another room"),
            confirmed=("officeA", "alice"),
        )

        assert effect.desired == ("officeA", "alice")
        assert effect.confirmed == ("officeA", "alice")

    def test_explicit_rejection_without_confirmed_clears(self) -> None:
        """从未成功入房过（`confirmed is None`）⇒ 被拒即清空（首次撞冲突 = 永久冲突）。"""
        effect = resolve_join_failure(
            JoinOfficeVerdict(ok=False, code=4101, message="Room already has an agent"),
            confirmed=None,
        )

        assert effect.desired is None
        assert effect.confirmed is None

    def test_indeterminate_verdict_clears_both(self) -> None:
        """「未获裁决」（形状不认识 / 码不可解析）⇒ **双清空**：请求已发出，旧确认不再可信。"""
        effect = resolve_join_failure(
            JoinOfficeVerdict(ok=False, code=None, message=NO_RESPONSE_MESSAGE),
            confirmed=("officeA", "alice"),
        )

        assert effect.desired is None
        assert effect.confirmed is None

    def test_transport_failure_clears_both(self) -> None:
        """传输层失败无裁决（`verdict=None`）⇒ 与「未获裁决」同效应：不臆断服务端状态。"""
        effect = resolve_join_failure(None, confirmed=("officeB", "bob"))

        assert effect.desired is None
        assert effect.confirmed is None


class TestJoinFailureMessage:
    """文案权威（唯一）/ the single text authority for join rejections."""

    def test_generic_code_uses_verdict_message_verbatim(self) -> None:
        """一般码：逐字用 `verdict.message`（同态口径的「同一文案」即指此处）。"""
        verdict = JoinOfficeVerdict(ok=False, code=4101, message="Room already has an agent")

        assert join_failure_message(verdict) == "Room already has an agent"

    def test_indeterminate_keeps_server_message(self) -> None:
        """未获裁决也不得硬写常量：`dict` 带码但码不可解析时仍要保住服务端文案。"""
        verdict = JoinOfficeVerdict(ok=False, code=None, message=NO_RESPONSE_MESSAGE)

        assert join_failure_message(verdict) == NO_RESPONSE_MESSAGE

    def test_403_appends_session_identity_hint(self) -> None:
        """`403`（改名被拒）⇒ 追加会话身份提示——调用方从「以为自己改错了」自救的唯一信息。"""
        verdict = JoinOfficeVerdict(ok=False, code=403, message="Role or name mismatch with existing session")

        message = join_failure_message(verdict, confirmed_name="alice")

        assert message.startswith("Role or name mismatch with existing session")
        assert "alice" in message
        assert "重新建立连接" in message

    def test_403_without_confirmed_name_stays_verbatim(self) -> None:
        """无已确认身份可归因时省略提示（不臆造名字）。"""
        verdict = JoinOfficeVerdict(ok=False, code=403, message="Role or name mismatch with existing session")

        assert join_failure_message(verdict, confirmed_name=None) == "Role or name mismatch with existing session"

    def test_non_403_never_appends_hint(self) -> None:
        """身份提示**只**对 403 追加（其余码追加会破坏双路径同文案）。"""
        verdict = JoinOfficeVerdict(ok=False, code=4105, message="Name already taken in room")

        assert join_failure_message(verdict, confirmed_name="alice") == "Name already taken in room"


class TestBuildJoinFailurePayload:
    """异常载荷构造 / exception payload construction（#217 C2 / C3）。"""

    def test_explicit_rejection_carries_code(self) -> None:
        """明确拒绝 ⇒ 载荷带协议码，供调用方机器分流。"""
        payload = build_join_failure_payload(JoinOfficeVerdict(ok=False, code=4101, message="boom"))

        assert payload.get("code") == 4101
        assert payload.get("message") == "boom"

    def test_unknown_code_survives_construction(self) -> None:
        """未知码（未来协议新增）**仍**带码抛出——绝不因白名单而静默放过。"""
        payload = build_join_failure_payload(JoinOfficeVerdict(ok=False, code=4199, message="future"))

        assert SMCPProtocolError(payload).code == 4199

    def test_indeterminate_payload_omits_code_key(self) -> None:
        """未获裁决 ⇒ **省略** `code` 键（写 `{"code": None}` 会 `int(None)` 抛 TypeError）。"""
        payload = build_join_failure_payload(JoinOfficeVerdict(ok=False, code=None, message=NO_RESPONSE_MESSAGE))

        assert "code" not in payload
        assert SMCPProtocolError(payload).code == -1

    def test_unparsable_code_is_indeterminate_but_still_raises(self) -> None:
        """码不可解析（`code=None`）⇒ 与未获裁决同形：`.code == -1` 且**不** TypeError。"""
        verdict = parse_join_ack({"code": "not-a-number", "message": "weird"})

        payload = build_join_failure_payload(verdict)
        error = SMCPProtocolError(payload)

        assert error.code == -1
        assert error.error_message == "weird"


class TestLogJoinRejection:
    """共享日志产出者（同态口径 ②）/ the shared rejection-log producer."""

    def test_log_carries_code_and_message_without_path_specifics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """文案含 code + message，且**不含** SID / namespace（两路径唯一差异只能是返回渠道）。"""
        fake_logger = MagicMock()
        monkeypatch.setattr(office_mod, "logger", fake_logger)
        verdict = JoinOfficeVerdict(ok=False, code=4101, message="Room already has an agent")

        log_join_rejection("officeA", verdict)

        fake_logger.error.assert_called_once()
        text = fake_logger.error.call_args[0][0]
        assert "officeA" in text
        assert "4101" in text
        assert "Room already has an agent" in text
        assert "sid" not in text.lower()
        assert "namespace" not in text.lower()

    def test_log_appends_403_hint_via_message_authority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """403 提示与异常文案同源（同一文案权威），不得各写各的。"""
        fake_logger = MagicMock()
        monkeypatch.setattr(office_mod, "logger", fake_logger)
        verdict = JoinOfficeVerdict(ok=False, code=403, message="Role or name mismatch with existing session")

        log_join_rejection("officeA", verdict, confirmed_name="alice")

        text = fake_logger.error.call_args[0][0]
        assert "alice" in text
        assert "重新建立连接" in text


# ── #212 重连回房的有界退避（瞬态冲突白名单 + 曲线 + 校验类判据）──────────────────────
#
# 协议依据：error-handling.md:486 重试表（4101/4105 条件可选）、room-model.md:216-218（回收窗口
# 可达数十秒 ⇒ 任何有限短窗都覆盖不了）、room-model.md:157（校验必须先于副作用）。曲线与预算大小
# 归 SDK 自治，故本节的期望值是 **SDK 决策**（用户拍板 45s / 1.2.4.5.5…），不是协议条款。


class TestRejoinRetryConstants:
    """退避预算与曲线常量 / backoff budget & curve (SDK 自治裁量)."""

    def test_budget_covers_default_reclaim_window(self) -> None:
        """默认预算须覆盖 socket.io 默认最长回收窗口 = ``ping_interval(25) + ping_timeout(20)``。

        写死 45.0 而非只断言 ``>= 45``：这是**产品决策**（用户拍板），改小它等于让重试徒劳
        （room-model.md:216-218 已论证「任何有限短窗都覆盖不了回收窗口」）。
        """
        assert OFFICE_REJOIN_RETRY_BUDGET == 45.0

    def test_curve_is_bounded_and_within_budget(self) -> None:
        """曲线 1 → 2 → 4 →（封顶）5 秒；封顶值必须远小于预算，否则预算只够一两次尝试。"""
        assert OFFICE_REJOIN_RETRY_BASE_DELAY == 1.0
        assert OFFICE_REJOIN_RETRY_MAX_DELAY == 5.0
        assert OFFICE_REJOIN_RETRY_MAX_DELAY < OFFICE_REJOIN_RETRY_BUDGET


class TestTransientJoinConflictCodes:
    """瞬态冲突白名单 = 协议重试表里「条件可选」的那两码（error-handling.md:496）。"""

    def test_only_room_full_and_name_conflict(self) -> None:
        assert frozenset({4101, 4105}) == TRANSIENT_JOIN_CONFLICT_CODES

    def test_already_in_room_is_not_transient(self) -> None:
        """4106 不属瞬态冲突：重连产生的是**新会话**（无 ``office_id``），不可能「已在其它房」。

        它只由客户端状态错误产生，须先显式退房（error-handling.md:498）——重试不改变结果。
        """
        assert 4106 not in TRANSIENT_JOIN_CONFLICT_CODES


class TestIsValidationRejection:
    """「校验先于副作用」判据 / the pre-commit (validation) rejection predicate.

    #212 裁决 4：判据由「顶层有没有码」收敛为「码是否属**校验类**」——只有校验期返回的码才保证
    「既有成员关系未被改变」，据此回退到「已确认房」才成立。``500`` 是 handler catch-all，可晚于
    成员关系提交（入房广播抛错 ⇒ 服务端按提交点收敛为无房），此时回退会让客户端宣称仍在旧房。
    """

    @pytest.mark.parametrize("code", [400, 403, 4101, 4105, 4106])
    def test_join_validation_codes_are_recognised(self, code: int) -> None:
        assert is_validation_rejection(JoinOfficeVerdict(ok=False, code=code, message="x")) is True

    @pytest.mark.parametrize("code", [500, 4102, 4103, 4104, 4199])
    def test_post_commit_reserved_and_unknown_codes_are_excluded(self, code: int) -> None:
        """``500`` 可晚于提交；``4102`` 是预留码、``4103``/``4104`` 只由 ``list_room`` 产出（join 产不出）；
        未知码 fail-safe（与 ``parse_join_ack`` 的「宁严勿宽」同向）。"""
        assert is_validation_rejection(JoinOfficeVerdict(ok=False, code=code, message="x")) is False

    def test_indeterminate_and_transport_are_not_validation_rejections(self) -> None:
        assert is_validation_rejection(JoinOfficeVerdict(ok=False, code=None, message=NO_RESPONSE_MESSAGE)) is False
        assert is_validation_rejection(None) is False

    def test_whitelist_matches_join_reachable_validation_codes(self) -> None:
        """白名单是**维护耦合点**：它必须恰好是「join 的校验类码」集合。

        服务端 ``server:join_office`` 可达码 = ``{400, 403, 4101, 4105, 4106, 500}``（``4102``-``4104``
        只由别的事件产出）。未来 MINOR 新增校验类码时，本断言不会自动报警，但会落到「非校验 ⇒ 双清空」
        这一 fail-safe 方向（牺牲恢复能力、不撒谎）——改动这里请同时更新本断言与 docstring。
        """
        assert frozenset({400, 403, 4101, 4105, 4106}) == JOIN_VALIDATION_REJECTION_CODES
        assert TRANSIENT_JOIN_CONFLICT_CODES <= JOIN_VALIDATION_REJECTION_CODES


class TestRejoinRetryDelay:
    """退避曲线与预算夹取（纯函数，三条回放路径共用）/ shared backoff policy for all replay paths."""

    @staticmethod
    def _room_full() -> JoinOfficeVerdict:
        return JoinOfficeVerdict(ok=False, code=4101, message="Room already has an agent")

    def test_curve_doubles_then_caps(self) -> None:
        assert [rejoin_retry_delay(self._room_full(), attempt=i, remaining=999.0) for i in range(5)] == [
            1.0,
            2.0,
            4.0,
            5.0,
            5.0,
        ]

    def test_name_conflict_is_retryable_too(self) -> None:
        """4101 与 4105 是同一类瞬态冲突的两种形态（重连撞旧会话：Agent 独占 / 同名）。"""
        verdict = JoinOfficeVerdict(ok=False, code=4105, message="Name already taken in room")
        assert rejoin_retry_delay(verdict, attempt=0, remaining=999.0) == 1.0

    @pytest.mark.parametrize("code", [4106, 500, 4102, 4103, 4104, 4199])
    def test_non_transient_codes_never_retry(self, code: int) -> None:
        assert rejoin_retry_delay(JoinOfficeVerdict(ok=False, code=code, message="x"), attempt=0, remaining=999.0) is None

    def test_indeterminate_and_transport_never_retry(self) -> None:
        """形状不认识 / 码不可解析（无码）与传输层失败（无裁决）都不重试：协议只对 4101/4105 放行。"""
        assert (
            rejoin_retry_delay(
                JoinOfficeVerdict(ok=False, code=None, message=NO_RESPONSE_MESSAGE), attempt=0, remaining=999.0
            )
            is None
        )
        assert rejoin_retry_delay(None, attempt=0, remaining=999.0) is None

    @pytest.mark.parametrize("remaining", [0.0, -0.5])
    def test_exhausted_budget_never_retries(self, remaining: float) -> None:
        """预算耗尽（含非正预算的退化配置）⇒ 放弃并落失败效应。"""
        assert rejoin_retry_delay(self._room_full(), attempt=0, remaining=remaining) is None

    def test_delay_is_clamped_to_remaining_budget(self) -> None:
        """末段退避不得越过预算：否则「预算 = 墙钟上界」不成立，测试也无法用小预算提速。"""
        assert rejoin_retry_delay(self._room_full(), attempt=3, remaining=0.25) == 0.25

    def test_huge_attempt_does_not_overflow(self) -> None:
        """指数必须封顶：``2.0 ** 1024`` 在 Python 里**抛 OverflowError**（不是返回 inf）。

        预算可被配置成任意大（部署方为覆盖超长回收窗口而调大）⇒ 尝试次数随之变多。若不封顶，异常会从
        纯函数逃出：async 侧成为 detached task 的未取回异常、sync 侧让回房线程带栈退出（恰在预算耗尽前
        最需要它的时候）。封顶后该值早已被 ``OFFICE_REJOIN_RETRY_MAX_DELAY`` 压低，无行为差异。
        """
        assert rejoin_retry_delay(self._room_full(), attempt=5000, remaining=1e9) == 5.0


class TestResolveJoinFailurePostCommitCodes:
    """判据收敛后的效应矩阵（#212 裁决 4）/ effect matrix for non-validation rejections.

    与 ``TestResolveJoinFailureMatrix`` 分开成节，是为了让「这是**新增**的语义」在测试结构上可见：
    原矩阵只按「有没有码」分两行，新判据把 ``500`` / 预留码 / 未知码从「回退」搬到「双清空」。
    """

    def test_internal_error_clears_both(self) -> None:
        """500 可发生在成员关系**已提交之后** ⇒ 旧「已确认房」不再可信，双清空。"""
        effect = resolve_join_failure(
            JoinOfficeVerdict(ok=False, code=500, message="Internal server error"),
            confirmed=("officeA", "alice"),
        )

        assert effect.desired is None
        assert effect.confirmed is None

    def test_unknown_code_clears_both(self) -> None:
        """未知码 fail-safe：宁可丢掉恢复能力，也不宣称一个可能已不存在的成员关系。"""
        effect = resolve_join_failure(
            JoinOfficeVerdict(ok=False, code=4199, message="future code"),
            confirmed=("officeA", "alice"),
        )

        assert effect.desired is None
        assert effect.confirmed is None

    @pytest.mark.parametrize("code", [4102, 4103, 4104])
    def test_reserved_and_foreign_room_codes_clear_both(self, code: int) -> None:
        """这三个码不由 ``join`` 产出（4102 预留、4103/4104 属 list_room）；万一收到即 fail-safe 双清空。"""
        effect = resolve_join_failure(JoinOfficeVerdict(ok=False, code=code, message="x"), confirmed=("officeA", "alice"))

        assert effect.desired is None
        assert effect.confirmed is None

    def test_bad_request_still_falls_back(self) -> None:
        """400（载荷畸形）属**请求校验**，先于任何副作用 ⇒ 既有成员关系不变 ⇒ 仍回退。"""
        effect = resolve_join_failure(
            JoinOfficeVerdict(ok=False, code=400, message="Bad request"),
            confirmed=("officeA", "alice"),
        )

        assert effect.desired == ("officeA", "alice")
        assert effect.confirmed == ("officeA", "alice")
