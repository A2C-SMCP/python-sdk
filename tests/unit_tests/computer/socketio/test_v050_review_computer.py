# -*- coding: utf-8 -*-
"""
v0.5.0 整 milestone 跨节点审查 — Computer socketio 侧回归锁。

- Y4：``server:leave_office`` 有 ack（协议 events.md:173）⇒ 等裁决；被拒 ⇒ ``RuntimeError``（与显式 join 被拒
  同一异常族），本地意图 / 已确认房号照样清空（#223：退房是最后一句陈述）。
- Y8：显式 ``join_office`` 的 ACK 等待有界（``OFFICE_JOIN_TIMEOUT``）——断线时 socketio 丢弃 ack 回调，默认
  60s 会让 office 操作锁被占满，把重连回房与退房一并卡住。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, LEAVE_OFFICE_EVENT, SMCP_NAMESPACE
from a2c_smcp.utils.office import OFFICE_JOIN_TIMEOUT


def _make_client() -> SMCPComputerClient:
    computer = MagicMock()
    computer.name = "review-computer"
    client = SMCPComputerClient(computer=computer)
    client.namespaces[SMCP_NAMESPACE] = "fake-sid"
    return client


def _recording_call(client: SMCPComputerClient, ack: Any) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(event: str, data: Any = None, *args: Any, **kwargs: Any) -> Any:
        calls.append((event, kwargs))
        return ack

    client.call = fake_call  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_leave_waits_for_ack_and_raises_on_rejection() -> None:
    client = _make_client()
    client.office_id = client._confirmed_office_id = "officeA"
    calls = _recording_call(client, {"code": 500, "message": "Internal error"})

    with pytest.raises(RuntimeError, match="Internal error"):
        await client.leave_office("officeA")

    assert [event for event, _ in calls] == [LEAVE_OFFICE_EVENT], "leave 必须走 call（等 ack）"
    assert calls[0][1].get("timeout") == OFFICE_JOIN_TIMEOUT
    assert client.office_id is None and client._confirmed_office_id is None, "被拒也不得回滚本地清账"


@pytest.mark.asyncio
async def test_leave_empty_ack_is_success() -> None:
    client = _make_client()
    client.office_id = client._confirmed_office_id = "officeA"
    _recording_call(client, None)
    await client.leave_office("officeA")
    assert client.office_id is None


@pytest.mark.asyncio
async def test_explicit_join_ack_wait_is_bounded() -> None:
    client = _make_client()
    calls = _recording_call(client, None)
    await client.join_office("officeA")
    assert [event for event, _ in calls] == [JOIN_OFFICE_EVENT]
    assert calls[0][1].get("timeout") == OFFICE_JOIN_TIMEOUT, "显式 join 不得沿用 socketio 默认 60s 超时"
