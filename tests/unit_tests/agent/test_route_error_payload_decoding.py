# -*- coding: utf-8 -*-
"""
* 文件名: test_route_error_payload_decoding
* 描述: #216 —— Agent 对 ``get_tools`` / ``get_config`` / ``get_desktop`` 的路由层 flat ErrorPayload 解码。

        Server 对 ``client:*`` 的拒绝（``400`` / ``403`` / ``404`` / ``4103``）一律以 flat ErrorPayload 经 ack
        返回（error-handling.md §错误响应格式）。Agent 侧此前只有另外六个 get_* 解码它：
          - ``get_config`` 把错误载荷**静默**当成「零个 server」的成功（``{"servers": {}}``）——假成功；
          - ``get_tools`` / ``get_desktop`` 报误导性的「req_id 不匹配」。
        现三者统一抛 :class:`SMCPProtocolError`（带协议码），与其余 get_* 同一口径（async + sync）。

#216: the Agent decodes routing-layer flat ErrorPayloads on these three methods instead of reporting
fake success (get_config) or a misleading req_id mismatch (get_tools / get_desktop).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import ErrorCode, build_computer_not_found_error, build_room_rejection_error

METHODS = ["get_tools_from_computer", "get_config_from_computer", "get_desktop_from_computer"]
PAYLOADS = [
    pytest.param(build_room_rejection_error(ErrorCode.NOT_IN_ROOM), 4103, id="4103"),
    pytest.param(build_computer_not_found_error("pc"), 404, id="404"),
    pytest.param({"code": 403, "message": "Only agents may issue client:* calls"}, 403, id="403"),
    pytest.param({"code": 400, "message": "Invalid request payload"}, 400, id="400"),
]


def _provider() -> DefaultAgentAuthProvider:
    return DefaultAgentAuthProvider(agent_id="a", office_id="o")


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(("payload", "code"), PAYLOADS)
async def test_async_route_error_raises_protocol_error(method: str, payload: dict[str, Any], code: int) -> None:
    client = AsyncSMCPAgentClient(auth_provider=_provider())
    with patch.object(client, "call", new=AsyncMock(return_value=dict(payload))):
        with pytest.raises(SMCPProtocolError) as ei:
            await getattr(client, method)("pc")
    assert ei.value.code == code


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(("payload", "code"), PAYLOADS)
def test_sync_route_error_raises_protocol_error(method: str, payload: dict[str, Any], code: int) -> None:
    client = SMCPAgentClient(auth_provider=_provider())
    with patch.object(client, "call", new=MagicMock(return_value=dict(payload))):
        with pytest.raises(SMCPProtocolError) as ei:
            getattr(client, method)("pc")
    assert ei.value.code == code


async def test_async_get_config_success_still_returns_servers() -> None:
    """正对照：成功形态不受影响（否则「一律抛」也能让上面的用例转绿）。"""
    client = AsyncSMCPAgentClient(auth_provider=_provider())
    with patch.object(client, "call", new=AsyncMock(return_value={"servers": {"s": {"name": "s"}}})):
        ret = await client.get_config_from_computer("pc")
    assert ret == {"servers": {"s": {"name": "s"}}}


def test_sync_get_config_success_still_returns_servers() -> None:
    client = SMCPAgentClient(auth_provider=_provider())
    with patch.object(client, "call", new=MagicMock(return_value={"servers": {"s": {"name": "s"}}})):
        ret = client.get_config_from_computer("pc")
    assert ret == {"servers": {"s": {"name": "s"}}}


_SUCCESS = [
    pytest.param("get_tools_from_computer", "tools", [], id="get_tools"),
    pytest.param("get_desktop_from_computer", "desktops", [], id="get_desktop"),
]


def _echo_req_id(key: str, value: Any) -> Any:
    return lambda event, req, **kw: {key: value, "req_id": req["req_id"]}


@pytest.mark.parametrize(("method", "key", "value"), _SUCCESS)
async def test_async_success_shape_still_returned(method: str, key: str, value: Any) -> None:
    """正对照：get_tools / get_desktop 的成功形态不受解码影响。"""
    client = AsyncSMCPAgentClient(auth_provider=_provider())
    echo = _echo_req_id(key, value)
    with patch.object(client, "call", new=AsyncMock(side_effect=lambda *a, **kw: echo(*a, **kw))):
        ret = await getattr(client, method)("pc")
    assert ret[key] == value


@pytest.mark.parametrize(("method", "key", "value"), _SUCCESS)
def test_sync_success_shape_still_returned(method: str, key: str, value: Any) -> None:
    client = SMCPAgentClient(auth_provider=_provider())
    with patch.object(client, "call", new=MagicMock(side_effect=_echo_req_id(key, value))):
        ret = getattr(client, method)("pc")
    assert ret[key] == value
