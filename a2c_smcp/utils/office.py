# -*- coding: utf-8 -*-
# filename: office.py
# @Time    : 2026/09/19
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Office 入房 ACK 判定与失败效应（单一权威）/ Office join-ack verdict & failure effects (single source of truth).

存在意义 / Why this module：
    ``server:join_office`` 的 ACK 判定同时被 **四处** 消费——Computer 显式 join、Computer 自动回房、
    Agent 自动回房（async + sync）、以及 #218 起的 **Agent 显式 join**——判定或文案漂移会让同一份
    服务端裁决在不同路径上得出不同结论（例如一处把空响应当失败、另一处当成功）。此处收敛为唯一实现，
    与 :mod:`a2c_smcp.utils.mime` 同款"单一权威"约定。/ The join ack is consumed by four call sites
    (Computer explicit join + replay, Agent replay + explicit join); a single implementation prevents
    verdict/wording drift.

#203 语境 / #203 context：
    Socket.IO 房间成员关系属于**会话**，传输层断线自动重连后 namespace 换新 SID，客户端必须重放
    ``server:join_office``（镜像 rust-sdk#204）。回房用 :data:`OFFICE_JOIN_TIMEOUT` 做**有界**等待：
    socketio 的 ``call()`` 在连接已断时不会快速失败，会吃满默认超时（60s）；无界等待会让 office 操作
    长时间互斥。/ The replay uses a bounded ack wait because ``call()`` does not fail fast on a dead
    connection and would otherwise burn the 60s default.

#214 语境 / #214 context：
    协议 v0.5.0 废除 ``(bool, str | None)`` 元组形态：**成功 = 空 ack**，**失败 = flat
    ``ErrorPayload``**（顶层 ``code``）。本模块随之改为返回 :class:`JoinOfficeVerdict`——把协议码
    显式带出，供 #212 的「瞬态冲突有界退避重试」按 ``code`` 分流，无需二次解析原始载荷。
    Protocol v0.5.0 replaced the `(bool, str|None)` tuple with an empty-ack / flat-`ErrorPayload`
    contract; the verdict now carries the protocol code for #212's bounded retry.

#218 语境 / #218 context：
    Agent 显式 ``join_office`` 由「无 ack 的 emit」改为**等 ACK**后，**失败效应**（本地意图 `desired`
    与「已确认成员关系」`confirmed` 的去留）同样需要单一权威——显式路径与自动回房路径必须给出
    **同一份裁决 + 同一张效应表 + 同一个日志产出者**（#217 裁决 6 的同态口径；差异只允许是显式路径
    多一条「抛 ``SMCPProtocolError``」的同步返回渠道）。协议**未覆盖**失败后 SDK 本地状态如何处置，
    故 :func:`resolve_join_failure` 属 SDK 自治裁量（#217 裁决 2）。
    Scope note: the effect matrix is **Agent-scoped on purpose** — Computer's non-validation branch
    (no code / ``500`` / unknown) deliberately keeps its last confirmed office
    (``computer/socketio/client.py``), so the two must NOT be unified. 判据（:func:`is_validation_rejection`）
    两侧**共用**，只有「非校验类 ⇒ 落到哪」这一半刻意分叉。

#212 语境 / #212 context：
    **静默断线**（拔网线 / 代理被 kill / NAT 超时）下服务端要等自身心跳超时才回收旧会话，客户端却在
    秒级内重连并重放 ``server:join_office`` ⇒ 必然撞上 ``4101``/``4105``（协议 error-handling.md
    §建议的重试策略）。故**回放路径**对这两码做**有界退避重试**（:func:`rejoin_retry_delay` +
    :data:`OFFICE_REJOIN_RETRY_BUDGET`）；预算耗尽才落失败效应。**只作用于回放路径**——显式
    ``join_office`` 不重试（首次入房撞上冲突即永久冲突）。同批把效应表判据由「顶层有没有码」收敛为
    :func:`is_validation_rejection`：只有**校验先于副作用**的码才能回退到「已确认房」。
    Bounded backoff for the transient 4101/4105 conflicts, on the replay paths only; the effect matrix
    now keys on pre-commit (validation) rejections rather than on mere code presence.

**不得**用 :func:`a2c_smcp.smcp.build_room_rejection_error` 构造这里的异常载荷：那是**服务端**载荷
构造器（未知码即 ``ValueError``、``4102`` 构造上拒绝），与「未知码仍算拒绝、码不可解析也要抛」相反。
Do NOT reuse the server-side rejection builder here: it rejects unknown codes by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from a2c_smcp.smcp import ErrorCode, ErrorPayload
from a2c_smcp.utils.logger import get_logger

logger = get_logger("utils.office")

OFFICE_JOIN_TIMEOUT = 10
"""入房等 ACK 的有界上界（秒）/ Bounded ack wait for ``server:join_office``, in seconds.

**Agent 两侧**（显式 ``join_office`` 与自动回房）与 **Computer 自动回房**共用本值——一次死连接不会把
office 操作互斥占满 socketio 默认的 60s。代价：慢网络下显式 join 的超时阈值由 60s（默认）收紧到 10s
（#217 裁决 4，已披露）。**Computer 显式 join 未纳入**（仍走 socketio 默认超时）。
#212 的退避重试**逐次**套用本值，故恢复耗时的上界 = :data:`OFFICE_REJOIN_RETRY_BUDGET` + 本值。
All replay paths share this bound; the Computer's explicit join is deliberately out of scope and still
uses the socketio default.
"""

OFFICE_REJOIN_RETRY_BUDGET: float = 45.0
"""重连回房的**退避预算**（秒，墙钟上界）——#212 / Wall-clock backoff budget for a rejoin replay (#212).

默认取满 socket.io 的**最长默认会话回收窗口** ``ping_interval(25) + ping_timeout(20) = 45s``：静默断线后
服务端要等自身心跳超时才回收旧会话，而客户端在秒级内就重连重放 ⇒ 这段时间的重放必然撞上
``4101``/``4105``。协议（`room-model.md` §静默断线与会话回收）对回收窗口只给出 **SHOULD 级部署约束**
且明确「任何有限短窗都覆盖不了回收窗口」，故默认值按常见默认取满；部署方调大了 ping 参数时，由使用方
覆盖客户端的公开属性 ``office_rejoin_retry_budget``（Agent 两侧与 Computer 各一个实例属性，默认取本值）。

预算内**每次尝试各取一次 office 操作锁**（退避期间必须放锁，否则一次恢复会把 office 操作互斥占满）；
上界是「预算 + 一次 :data:`OFFICE_JOIN_TIMEOUT` 有界 ACK 等待」——末次尝试在预算内发起，其 ACK 仍可能
等到自己的超时。非正值 ⇒ 退化回单次尝试（测试与嵌入方可用它精确关掉重试）。
Covers socket.io's default reclaim window; per-attempt locking keeps the office op-lock free during backoff.
"""

OFFICE_REJOIN_RETRY_BASE_DELAY: float = 1.0
"""退避基数（秒）：第 ``n`` 次尝试被拒后等 ``min(base * 2**n, max, 剩余预算)``。"""

OFFICE_REJOIN_RETRY_MAX_DELAY: float = 5.0
"""退避封顶（秒），与 socketio 自身 ``reconnection_delay_max`` 默认值同量级。

回收是**阶跃式**的（服务端心跳超时那一刻旧会话才消失），封顶越大，「已回收」被检出得越晚——即用户看到
的恢复延迟上限。另：末段退避会被 ``min(..., 剩余预算)`` 夹取，故曲线末段可能短于本值。
"""

TRANSIENT_JOIN_CONFLICT_CODES: frozenset[int] = frozenset({ErrorCode.ROOM_FULL, ErrorCode.NAME_CONFLICT})
"""**瞬态冲突**白名单（``4101`` / ``4105``）——唯一可重试的两码 / the only retryable join rejections.

协议 `error-handling.md` §建议的重试策略 把二者列为**同一类瞬态冲突的两种形态**（目标房已有 Agent /
房内同 role 同名）：静默断线后服务端尚未回收旧会话时，重放必然撞上它们；`4106` **不属**此类（重连产生的
是新会话，无 ``office_id``，不可能「已在其它房」），不可重试。
"""

JOIN_VALIDATION_REJECTION_CODES: frozenset[int] = frozenset(
    {
        ErrorCode.BAD_REQUEST,
        ErrorCode.FORBIDDEN,
        ErrorCode.ROOM_FULL,
        ErrorCode.NAME_CONFLICT,
        ErrorCode.ALREADY_IN_ROOM,
    }
)
"""**校验类**拒绝白名单——保证「既有成员关系未被改变」的码 / pre-commit (validation) rejections.

`room-model.md` 的加入伪代码明写「**校验必须先于副作用**」，故加入校验能返回的码（``4101`` / ``4105`` /
``4106``）与请求校验码（``400`` / ``403``）都**不改变既有成员关系** ⇒ 客户端可以回退到「已确认房」
（见 :func:`resolve_join_failure`）。

**刻意排除**：``500``（handler catch-all，可发生在成员关系**已提交之后**——入房广播抛错时服务端按提交点
收敛为「无房」，此时回退会让客户端宣称仍在旧房）、``4102``（预留码）/ ``4103`` / ``4104``（只由
``server:list_room`` 产出，join 物理上产不出）、以及**未知码**（fail-safe：宁丢恢复能力，不撒谎）。
本集合是**维护耦合点**：它必须恰好等于「``server:join_office`` 的校验类可达码」，见
``tests/unit_tests/utils/test_office.py::TestIsValidationRejection``。
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

    ``code`` 是**唯一**可用于机器分流的字段：重连回房只对
    :data:`TRANSIENT_JOIN_CONFLICT_CODES`（``4101`` / ``4105``）做有界退避重试（:func:`rejoin_retry_delay`），
    其余码一律放弃并如实报错；失败效应则按 :func:`is_validation_rejection` 二分。
    ``code`` is the only machine-actionable field — only 4101/4105 are retried (see
    :func:`rejoin_retry_delay`), and :func:`is_validation_rejection` splits the failure effects.
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


def is_validation_rejection(verdict: JoinOfficeVerdict | None) -> bool:
    """
    该裁决是否为**校验类拒绝**（副作用之前发生 ⇒ 既有成员关系未被改变）。

    Is this verdict a *pre-commit* (validation) rejection, i.e. one that provably did not change the
    server-side membership?（#212 裁决 4）

    `room-model.md` 的加入伪代码明写「校验必须先于副作用」，故白名单
    :data:`JOIN_VALIDATION_REJECTION_CODES` 内的码可以安全地回退到「已确认房」；其余（``500`` / 预留码 /
    未知码 / 无码 / 无裁决）一律按「不臆断」处理（见 :func:`resolve_join_failure`）。
    The whitelist covers codes that the join validation can return *before* any membership side effect;
    everything else (500, reserved codes, unknown codes, no verdict) must not be assumed harmless.

    Args:
        verdict (JoinOfficeVerdict | None): 服务端裁决；``None`` 表示传输层失败（无裁决）。

    Returns:
        bool: ``True`` 仅当裁决带码且该码属校验类。
    """
    return verdict is not None and verdict.code in JOIN_VALIDATION_REJECTION_CODES


@dataclass(frozen=True, slots=True)
class OfficeMembership:
    """
    一次失败效应施加后的成员关系状态 / Membership state after a failure effect is applied.

    ``desired`` = 调用方声明的意图（决定「下次重连回哪个房」）；``confirmed`` = **服务端已确认**的
    成员关系（只在成功裁决时写入）。两者分离是必需的：#213 判例——入口预写的意图在并发下可能属于
    一次「裁决被丢弃」的在途 join，绝不代表真实成员关系。/ Intent vs server-confirmed membership;
    a rejection falls back to the *confirmed* one, never to the pre-written intent.
    """

    desired: tuple[str, str] | None
    confirmed: tuple[str, str] | None


def resolve_join_failure(
    verdict: JoinOfficeVerdict | None,
    *,
    confirmed: tuple[str, str] | None,
) -> OfficeMembership:
    """
    失败效应表（单一权威，Agent 作用域）/ The failure-effect matrix (single authority, Agent scope).

    ==============================  ==================================  ==============================
    失败形态                         判据                                 效应
    ==============================  ==================================  ==============================
    服务端**校验类拒绝**              :func:`is_validation_rejection`     ``desired := confirmed``
    **提交后失败 / 未获裁决**          ``code`` 非空但不属校验类，或 ``code is None``  ``desired = confirmed = None``
    **传输层失败**（无裁决）           ``verdict is None``                  同上（双清空）
    ==============================  ==================================  ==============================

    三条设计要点 / Three deliberate choices：

    1. **校验类拒绝回退到 `confirmed` 而非清空**：服务端校验失败**不改变既有成员关系**（校验先于副作用）
       ⇒ 清空会让客户端宣称「不在任何房」，与实情相反；回退接的是**已确认**房号而非入口预写值
       （#213：预写可能属于一次被丢弃的在途 join）。
    2. **其余一律双清空**：请求已发出 ⇒ 成员关系可能已变，旧「已确认」不再可信。**有意的分叉**：Computer
       的同类分支**保留** ``_confirmed_office_id``，两侧不合并。
    3. **生效条件不在本函数内**：调用方须先通过 ``generation`` 守卫（被更新的操作抢占 ⇒ 整条效应
       不施加）；本函数只回答「若施加，落到哪」。/**成功**不在本表内——成功的落账只受**会话纪元**
       约束（无视操作抢占，#213），按表逐行实现会吞掉「被抢占 join 的成功落账」。

    Args:
        verdict (JoinOfficeVerdict | None): 服务端裁决；``None`` 表示传输层失败（无裁决）。
        confirmed: 失败前的「已确认」成员关系 ``(office_id, name)``。

    Returns:
        OfficeMembership: 应落到 ``desired`` / ``confirmed`` 上的新值。

    **判据来源（#212 裁决 4，已裁决）**：判据键由「顶层有没有码」收敛为
    :func:`is_validation_rejection`——原判据的前提是「服务端校验**先于**副作用 ⇒ 拒绝不改变既有成员
    关系」，而 ``500`` **可发生在成员关系已提交之后**（服务端 ``enter_room`` 阶段 2 的入房广播抛错 ⇒
    按提交点收敛把该 sid 摘成全无房、handler 再回 500）⇒ 旧判据会让客户端宣称仍在旧房。取舍：``500``
    若发生在**提交之前**，改成双清空会多丢一次恢复机会（旧房号不再回退）——协议未覆盖，按「不臆断、
    不撒谎」取双清空。/ Judgment key converged to the pre-commit whitelist: a 500 can post-date the
    membership commit, so falling back on it would claim a membership that may no longer exist.
    """
    if is_validation_rejection(verdict):
        return OfficeMembership(desired=confirmed, confirmed=confirmed)
    return OfficeMembership(desired=None, confirmed=None)


def rejoin_retry_delay(verdict: JoinOfficeVerdict | None, *, attempt: int, remaining: float) -> float | None:
    """
    回放路径的**退避策略**（纯函数，三条回放路径共用）/ The shared backoff policy for the replay paths.

    Return the seconds to wait before the next replay attempt, or ``None`` to stop retrying and apply the
    failure effect.

    **只对** :data:`TRANSIENT_JOIN_CONFLICT_CODES`（``4101`` / ``4105``）返回非 ``None``：协议
    `error-handling.md` §建议的重试策略 只对这两码放行「有界退避重试」，且**仅限传输层重连后的恢复
    路径**——本函数由回放路径调用，故调用点本身就是该前提（显式 ``join_office`` 不调用它）。
    曲线 ``base * 2**attempt``、封顶 :data:`OFFICE_REJOIN_RETRY_MAX_DELAY`、并以 ``remaining``（墙钟
    剩余预算）夹取，使「预算 = 恢复耗时的上界」成立且极小预算可确定性测试。

    Args:
        verdict (JoinOfficeVerdict | None): 本次尝试的裁决；``None``（传输层失败）不重试。
        attempt (int): 本次失败是**第几次**尝试的裁决（0 起算）⇒ ``attempt=0`` 即「第一次被拒」。
        remaining (float): 距预算终点的剩余秒数（``<= 0`` ⇒ 耗尽，不重试）。

    Returns:
        float | None: 下次重试前的等待秒数（``> 0``）；``None`` ⇒ 不重试。

    English: pure backoff policy shared by all four replay paths; `attempt` is 0-based, `remaining` is the
    remaining wall-clock budget (clamped so a retry never overshoots it).
    """
    if verdict is None or verdict.code not in TRANSIENT_JOIN_CONFLICT_CODES:
        return None
    if remaining <= 0:
        return None
    # ``2.0 ** min(attempt, 30)``：
    # - 浮点底数而非 ``2 ** attempt``：后者在 mypy 下是 ``Any``（``int.__pow__`` 可能返回 float，如
    #   ``2 ** -1``），会让返回值静默退化成 Any；
    # - **指数封顶**：``2.0 ** 1024`` 在 Python 里**抛 OverflowError**（不是返回 inf）。预算可被配置成
    #   任意大（部署方为覆盖超长回收窗口调大时），若指数不封顶，异常会从纯函数逃出 —— async 侧成为
    #   detached task 的未取回异常、sync 侧让回房线程带栈退出（恰在预算耗尽前最需要它的时候）。封顶后
    #   该值早已被 :data:`OFFICE_REJOIN_RETRY_MAX_DELAY` 压低，无行为差异。
    capped_attempt = min(attempt, 30)
    delay: float = min(OFFICE_REJOIN_RETRY_BASE_DELAY * (2.0**capped_attempt), OFFICE_REJOIN_RETRY_MAX_DELAY)
    return min(delay, remaining)


def join_failure_message(verdict: JoinOfficeVerdict, *, confirmed_name: str | None = None) -> str:
    """
    入房失败文案权威（异常载荷与日志共用）/ The single text authority for join failures.

    一般码**逐字**用 ``verdict.message``（#217 同态口径的「同一文案」即指此处）；**唯一豁免**是
    ``403``（同连接改名被拒）：追加一句 SDK 提示，把调用方从「以为自己改错了」里救出来。豁免成立的
    理由是 ``403`` 物理上**只有显式路径能产出**（新 sid 无身份）⇒ 不存在重放侧的对偶文案，追加不会
    破坏同态口径；而该提示又不能放进 ``details``（``SMCPProtocolError`` 自陈 details MUST NOT 透传）。

    **未获裁决不硬写常量**：形状不认识时 ``verdict.message`` 本就是 :data:`NO_RESPONSE_MESSAGE`；
    硬写会让「``dict`` 含 ``code`` 但不可解析」这一子案例丢掉服务端 message，与重放侧日志文案分叉。

    Args:
        verdict: 服务端裁决。
        confirmed_name (str | None): 已确认的会话身份名（``403`` 提示用）；缺省则省略提示。

    Returns:
        str: 面向调用方/日志的单行文案。
    """
    if verdict.code == ErrorCode.FORBIDDEN and confirmed_name:
        return (
            f"{verdict.message} 本连接会话身份已固化为 {confirmed_name}；改名须重新建立连接"
        )
    return verdict.message


def build_join_failure_payload(
    verdict: JoinOfficeVerdict,
    *,
    confirmed_name: str | None = None,
) -> ErrorPayload:
    """
    构造抛 ``SMCPProtocolError`` 用的 flat ``ErrorPayload`` / Build the flat ErrorPayload to raise.

    归一化为 ``{"code", "message"}``（有意取舍：本条路径的调用方处置只需 ``code``，故丢掉 ``details``
    以免诊断容器被透传）。**未获裁决必须省略 ``code`` 键**——写 ``{"code": None}`` 会让
    ``int(payload.get("code", -1))`` 抛 ``TypeError``；省略才得 ``.code == -1``（SDK 既有代码行为，
    自 #218 起写成**契约**）。

    **不得**改走 :func:`a2c_smcp.agent.errors.raise_for_error_payload`：它以 ``code ∈ ErrorCode``
    白名单为闸，会把未知码与码不可解析的拒绝**静默放过**——而 ``parse_join_ack`` 刻意判其为拒绝，
    放过即等于「首次入房被拒零感知」，正是本单要消灭的形态（#217 C2）。

    Args:
        verdict: 服务端裁决。
        confirmed_name (str | None): 已确认的会话身份名（``403`` 提示用）。

    Returns:
        ErrorPayload: 可直接交给 ``SMCPProtocolError`` 的扁平载荷。
    """
    payload: ErrorPayload = {"message": join_failure_message(verdict, confirmed_name=confirmed_name)}
    if verdict.code is not None:
        payload["code"] = verdict.code
    return payload


def log_join_rejection(office_id: str, verdict: JoinOfficeVerdict, *, confirmed_name: str | None = None) -> None:
    """
    入房被拒的 ERROR 日志产出者（两路径共用）/ The shared ERROR-log producer for join rejections.

    #217 同态口径 ②：显式路径与自动回房路径必须**同一个产出者、同一份文案**——日志是重放路径
    既有的可感通道（无人等 ACK，没有调用方可返回）。文案含 ``code`` + ``message``，**不含** SID /
    namespace（对端会话标识不得外泄）。/ One producer, one wording, no peer session identifiers.

    **产出条件在调用方**（有意的不对称，供 #219 断言）：显式路径的裁决一定会走到本函数（调用方在等，
    文案另有异常通道）；重放路径仅在结果**未被抢占**时记录（陈旧结果整条丢弃，包括日志）。

    Args:
        office_id (str): 目标房号 / target office id.
        verdict: 服务端裁决。
        confirmed_name (str | None): 已确认的会话身份名（``403`` 提示用）。
    """
    logger.error(
        f"加入 Office 被拒绝: {office_id} - code={verdict.code} "
        f"{join_failure_message(verdict, confirmed_name=confirmed_name)}",
    )
