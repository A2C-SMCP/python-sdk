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
    Scope note: the effect matrix is **Agent-scoped on purpose** — Computer's no-code branch deliberately
    keeps its last confirmed office (``computer/socketio/client.py``), so the two must NOT be unified.

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

**Agent 两侧**（显式 ``join_office`` 与自动回房）共用本值——一次死连接不会把 office 操作互斥占满
socketio 默认的 60s。代价：慢网络下显式 join 的超时阈值由 60s（默认）收紧到 10s（#217 裁决 4，
已披露）。**Computer 显式 join 未纳入**（仍走 socketio 默认超时），故本常量改名后语义更准确：
它约束的是「Agent 侧入房」，而非仅「回房」。/ Both Agent join paths share this bound; the Computer's
explicit join is deliberately out of scope and still uses the socketio default.
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

    =======================  ==========================  ==============================
    失败形态                  判据                         效应
    =======================  ==========================  ==============================
    服务端**明确拒绝**         ``code is not None``        ``desired := confirmed``
    **未获裁决**（形状/码）     ``ok=False, code is None``  ``desired = confirmed = None``
    **传输层失败**（无裁决）     ``verdict is None``         同上（双清空）
    =======================  ==========================  ==============================

    三条设计要点 / Three deliberate choices：

    1. **明确拒绝回退到 `confirmed` 而非清空**：服务端校验失败**不改变既有成员关系**（校验先于副作用）
       ⇒ 清空会让客户端宣称「不在任何房」，与实情相反；回退接的是**已确认**房号而非入口预写值
       （#213：预写可能属于一次被丢弃的在途 join）。
    2. **未获裁决 / 传输层失败双清空**：请求已发出 ⇒ 成员关系可能已变，旧「已确认」不再可信
       （与 Computer 自身**传输层**分支同口径）。**有意的分叉**：Computer 的「无码」分支**保留**
       ``_confirmed_office_id``，两侧不合并。
    3. **生效条件不在本函数内**：调用方须先通过 ``generation`` 守卫（被更新的操作抢占 ⇒ 整条效应
       不施加）；本函数只回答「若施加，落到哪」。/**成功**不在本表内——成功的落账只受**会话纪元**
       约束（无视操作抢占，#213），按表逐行实现会吞掉「被抢占 join 的成功落账」。

    Args:
        verdict (JoinOfficeVerdict | None): 服务端裁决；``None`` 表示传输层失败（无裁决）。
        confirmed: 失败前的「已确认」成员关系 ``(office_id, name)``。

    Returns:
        OfficeMembership: 应落到 ``desired`` / ``confirmed`` 上的新值。

    **已知边界（待协议/SDK 回裁，勿在此单静默改判据）**：判据键取 ``code is not None``（#217 裁决 2 /
    C5，与 Computer 同判据），其前提是「服务端校验**先于**副作用 ⇒ 拒绝不改变既有成员关系」。``500``
    可发生在成员关系**已提交之后**（服务端 ``enter_room`` 阶段 2 的入房广播抛错 ⇒ 按提交点收敛把该
    sid 摘成全无房、handler 再回 500）——此时按本表回退到 ``confirmed`` 会让客户端宣称仍在旧房。
    目前**有意维持**该判据以保持两端一致，边界记为 follow-up。/ Known boundary: 500 can post-date
    the membership commit, which breaks the row's premise; kept aligned with Computer on purpose.
    """
    if verdict is not None and verdict.code is not None:
        return OfficeMembership(desired=confirmed, confirmed=confirmed)
    return OfficeMembership(desired=None, confirmed=None)


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
