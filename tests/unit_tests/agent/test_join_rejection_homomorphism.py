# -*- coding: utf-8 -*-
"""
#219 C1 守护：「首次入房被拒」与「重放被拒」产出**同一种可感结果**（async + sync 四条路径配对）。

同态口径（#217 裁决 6）= **同一份裁决**（``parse_join_ack``）+ **同一张效应表**（``resolve_join_failure``）
+ **同一个日志产出者**（``log_join_rejection``）——**不是**状态终态同态：两路径起点不同（重放前断连已清
``_confirmed_office``），且 4101 + 无已确认房时「查表」与「内联双清空」终态相同 ⇒ 终态断言失明。故效应表
用**哨兵返回值**钉死「写入的就是表的输出」，产出者用 spy 钉死「经共享函数」，文案另以真实产出者逐字比对。

spy 必须按**路径所在模块**分别打桩（显式 ``agent.base`` / async 重放 ``agent.client`` / sync 重放
``agent.sync_client``）：#218 为此刻意不抽公共 helper，只打一处会让另外两处的原地重写假绿。

``403`` / ``4106`` 物理上只有显式路径能产出（新 sid 无身份、无房）⇒ 不入同态矩阵。重放预算压到 ``0``
（退化为单次尝试，#212），否则每格白跑满默认 45s。

#219 guard: the explicit join and the replay must share one verdict, one effect table and one log producer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from a2c_smcp.agent import base as base_mod
from a2c_smcp.agent import client as client_mod
from a2c_smcp.agent import sync_client as sync_client_mod
from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import SMCP_NAMESPACE
from a2c_smcp.utils import office as office_mod
from a2c_smcp.utils.office import OfficeMembership, join_failure_message, parse_join_ack

_OFFICE = "officeA"
_NAME = "agent-1"
_DESIRED = (_OFFICE, _NAME)

#: 双路径均可产出的拒绝形态（``4101`` 为合规主向量；``4105`` 在 #215 后 = 同房同 role 同名）
VECTORS = [
    pytest.param({"code": 4101, "message": "Room already has an agent"}, id="4101"),
    pytest.param({"code": 4105, "message": "Name already taken in room"}, id="4105"),
    pytest.param({"code": "4101", "message": "Room already has an agent"}, id="stringified-code"),
    pytest.param({"code": 4101}, id="no-message"),
    pytest.param({"code": 500, "message": "Internal server error"}, id="500"),
    pytest.param({"code": 4199, "message": "future code"}, id="unknown-code"),
    pytest.param({"code": "not-a-number", "message": "weird"}, id="unparsable-code"),
    pytest.param({"message": "boom"}, id="no-code"),
    pytest.param("garbage", id="non-dict"),
]
#: 路径 → 其实现所在模块（spy 打桩目标）
PATHS = {
    "async-explicit": base_mod,
    "async-replay": client_mod,
    "sync-explicit": base_mod,
    "sync-replay": sync_client_mod,
}


class _TransportDown(Exception):
    """传输层失败替身（无裁决）/ stand-in for a transport failure."""


def _auth() -> DefaultAgentAuthProvider:
    return DefaultAgentAuthProvider(agent_id=_NAME, office_id=_OFFICE)


def _drive(path: str, ack: Any, *, budget: float = 0.0, attempts: list[int] | None = None) -> tuple[Any, Any]:
    """经 ``path`` 驱动一次被拒入房 ⇒ ``(显式路径的异常或 None, 客户端)``；起点 confirmed=None（生产可达）。

    async 路径用 ``asyncio.run`` 而非 ``@pytest.mark.asyncio``（**刻意**）：同一用例要在一个循环里交替驱动
    sync / async 四条路径做配对比对，而 sync 路径不能在运行中的事件循环里阻塞 —— 勿「顺手统一」拆散配对。
    """
    log = attempts if attempts is not None else []

    def respond() -> Any:
        log.append(1)
        if isinstance(ack, Exception):
            raise ack
        return ack

    async def acall(*_a: Any, **_kw: Any) -> Any:
        return respond()

    def scall(*_a: Any, **_kw: Any) -> Any:
        return respond()

    client: Any = AsyncSMCPAgentClient(auth_provider=_auth()) if path.startswith("async") else SMCPAgentClient(auth_provider=_auth())
    client.call = acall if path.startswith("async") else scall
    client.office_rejoin_retry_budget = budget
    if path.endswith("replay"):
        client.namespaces[SMCP_NAMESPACE] = "fake-sid"  # 回房守卫判据：namespace 在册
        client._desired_office = _DESIRED
        if path == "async-replay":
            asyncio.run(client._arejoin_office(_DESIRED, client._office_generation))
        else:
            client._rejoin_office(_DESIRED, client._office_generation)
        return None, client
    try:
        if path == "async-explicit":
            asyncio.run(client.join_office(_OFFICE, _NAME, namespace=SMCP_NAMESPACE))
        else:
            client.join_office(_OFFICE, _NAME, namespace=SMCP_NAMESPACE)
    except (SMCPProtocolError, _TransportDown) as e:
        return e, client
    raise AssertionError(f"{path}: 被拒入房必须可感（抛异常），实际静默返回")


def _spy(monkeypatch: pytest.MonkeyPatch, name: str, make: Callable[[str, list], Any]) -> dict[str, list]:
    """在三个模块命名空间各打一处 spy；记录按**模块**分桶（路径命中的必须是自己模块的绑定）。

    本守护**刻意**依赖生产代码的 ``from a2c_smcp.utils.office import ...`` 名字绑定：改成属性访问
    （``office.xxx(...)``）时须同步改 spy（方向安全：误红而非假绿）。
    """
    calls: dict[str, list] = {m.__name__: [] for m in set(PATHS.values())}
    for mod_name, bucket in calls.items():
        monkeypatch.setattr(f"{mod_name}.{name}", make(mod_name, bucket))
    return calls


_SENTINEL = OfficeMembership(desired=("S-D", "n"), confirmed=("S-C", "n"))


@pytest.mark.parametrize("ack", [*VECTORS, pytest.param(_TransportDown("down"), id="transport")])
def test_same_verdict_and_same_effect_table(monkeypatch: pytest.MonkeyPatch, ack: Any) -> None:
    """A：四条路径经**同一个裁决函数**、以相等的裁决查**同一张表**，并把表的输出原样写入状态（哨兵 ≠ 任何内联值）。

    裁决腿同样钉在**绑定层**（透传 spy）：只比对样本向量的输出时，一个只在样本外输入上分叉的本地解析器
    会漏过（隔离审查实证）。传输层失败无 ack ⇒ 不经裁决函数。
    """

    def make(_mod: str, bucket: list) -> Any:
        def fake(verdict: Any, *, confirmed: Any) -> OfficeMembership:
            bucket.append((verdict, confirmed))
            return _SENTINEL

        return fake

    def make_parse(_mod: str, bucket: list) -> Any:
        def fake(result: Any) -> Any:
            bucket.append(result)
            return office_mod.parse_join_ack(result)

        return fake

    seen = []
    for path, mod in PATHS.items():
        calls = _spy(monkeypatch, "resolve_join_failure", make)
        parses = _spy(monkeypatch, "parse_join_ack", make_parse)
        _exc, client = _drive(path, ack)
        parse_hits = {m: len(b) for m, b in parses.items() if b}
        expected_parse = {} if isinstance(ack, Exception) else {mod.__name__: 1}
        assert parse_hits == expected_parse, f"{path} 须恰一次经 {mod.__name__} 的共享裁决函数，实得 {parse_hits}"
        hits = {m: len(b) for m, b in calls.items() if b}
        assert hits == {mod.__name__: 1}, f"{path} 须恰一次经 {mod.__name__} 的共享效应表，实得 {hits}"
        seen.append(calls[mod.__name__][0])
        state = (client._desired_office, client._confirmed_office)
        assert state == (_SENTINEL.desired, _SENTINEL.confirmed), f"{path} 须写入表的输出，实得 {state}"
    expected_verdict = None if isinstance(ack, Exception) else parse_join_ack(ack)
    assert seen == [(expected_verdict, None)] * len(PATHS), f"裁决/查表入参须四路径逐一相等：{seen}"


@pytest.mark.parametrize("ack", [*VECTORS, pytest.param(_TransportDown("down"), id="transport")])
def test_same_log_producer(monkeypatch: pytest.MonkeyPatch, ack: Any) -> None:
    """B：拒绝日志四路径各恰一次经**本模块绑定的共享产出者**且入参相等；传输层失败四路径都不经它。"""

    def make(_mod: str, bucket: list) -> Any:
        def fake(office_id: str, verdict: Any, *, confirmed_name: str | None = None) -> None:
            bucket.append((office_id, verdict, confirmed_name))

        return fake

    seen = []
    for path, mod in PATHS.items():
        calls = _spy(monkeypatch, "log_join_rejection", make)
        _drive(path, ack)
        hits = {m: len(b) for m, b in calls.items() if b}
        if isinstance(ack, Exception):
            assert hits == {}, f"{path}: 传输层失败无裁决 ⇒ 不得经拒绝产出者，实得 {hits}"
            continue
        assert hits == {mod.__name__: 1}, f"{path} 须恰一次经 {mod.__name__} 的共享产出者，实得 {hits}"
        seen.append(calls[mod.__name__][0])
    if not isinstance(ack, Exception):
        assert seen == [(_OFFICE, parse_join_ack(ack), None)] * len(PATHS), f"产出者入参须四路径相等：{seen}"


class _Sink:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, msg: str, *_a: Any, **_kw: Any) -> None:
        self.errors.append(msg)

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_a, **_kw: None


@pytest.mark.parametrize("replay_budget", [pytest.param(0.0, id="single-attempt"), pytest.param(0.2, id="after-retries")])
@pytest.mark.parametrize("ack", VECTORS)
def test_perceivable_output_is_byte_identical(monkeypatch: pytest.MonkeyPatch, ack: Any, replay_budget: float) -> None:
    """C：真实产出者下，四路径的 ERROR 文本逐字相同；显式异常的 ``(code, message)`` 与文案同源。

    增量价值（相对 A/B 组）= **显式异常形态** + 未打桩的真实产出者文案，不是独立的第三道防线。

    ``after-retries``：重放在预算内真实退避重试后才耗尽（仅 4101/4105 会重试）——重试不得改动共享文案。
    """
    sink = _Sink()
    monkeypatch.setattr(office_mod, "logger", sink)
    verdict = parse_join_ack(ack)
    texts, excs = {}, {}
    for path in PATHS:
        attempts: list[int] = []
        exc, _client = _drive(path, ack, budget=replay_budget, attempts=attempts)
        if path.endswith("replay") and replay_budget and verdict.code in (4101, 4105):
            assert len(attempts) >= 2, f"{path}: 夹具须真实重试过，否则 after-retries 格退化为单次（实得 {attempts}）"
        assert len(sink.errors) == 1, f"{path} 须恰一条共享 ERROR，实得 {sink.errors}"
        texts[path] = sink.errors.pop()
        if exc is not None:
            excs[path] = (exc.code, exc.error_message)

    assert len(set(texts.values())) == 1, f"四路径 ERROR 文本须逐字相同：{texts}"
    expected = (verdict.code if verdict.code is not None else -1, join_failure_message(verdict))
    assert excs == {"async-explicit": expected, "sync-explicit": expected}, f"显式异常形态须一致：{excs}"
    assert expected[1] in texts["async-explicit"] and f"code={verdict.code} " in texts["async-explicit"]
