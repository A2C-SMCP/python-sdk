# -*- coding: utf-8 -*-
# filename: test_handshake_4008_normalization.py
# @Author  : JQQ
# @Software: PyCharm
"""
握手 4008 归一化 **确定性契约** 单测 / Deterministic 4008-normalization contract.

背景 / Why this exists（架构决策记录）：
    集成测试用进程内 UvicornTestServer + 真实 aiohttp，在 coverage 插桩（sys.settrace
    慢 10-50×）下，服务端可能在客户端读完 400 body **之前**关闭连接 → aiohttp 抛裸
    ``RuntimeError("Connection closed.")``（aiohttp 已知问题簇 aio-libs/aiohttp#4581/#3904）。
    这是**传输层固有现实**：body 在到达前丢失时，任何客户端机制都无法还原 4008 原因
    （对任何 HTTP-400 拒绝都成立，含 Socket.IO 自身 Origin/CORS 400）。因此
    "4008 → ProtocolVersionError 映射" 这一**确定性契约**必须在确定性层级验证（喂入
    受控异常），而非靠进程内服务器竞态的网络往返断言精确异常类型（那是用非确定性
    载体测确定性契约 = 脆弱假绿）。集成测试只断言确定性保证（连接被拒、未建立）。

契约（versioning.md §连接握手 / §4 / §5）：
    - body 携 4008（无论经 socketio ConnectionError 还是裸 RuntimeError 的 __cause__）
      → 归一为 ProtocolVersionError，字段正确，并在抛前主动 disconnect（§4 死循环防御）
    - body 丢失（裸 RuntimeError 无 4008） → **原样重抛**，绝不误判为版本错误，不 disconnect
    - body 是非 4008 协议错误（如 400 缺参） → 原样重抛
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from engineio.exceptions import ConnectionError as EngineConnError
from socketio import AsyncClient
from socketio.exceptions import ConnectionError as SioConnError

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.exceptions import ProtocolVersionError
from a2c_smcp.smcp import SMCP_NAMESPACE

_BODY_4008: dict[str, Any] = {
    "code": 4008,
    "message": "Protocol version mismatch",
    "server_version": "0.3.0",
    "client_version": "0.2.0",
    "min_supported": "0.3.0",
    "max_supported": "0.3.999",
}
_BODY_400 = {"code": 400, "message": "Missing a2c_version query parameter"}


def _engine_cause(body: dict | None) -> EngineConnError:
    """engineio 在 400 时的真实形态：ConnectionError(msg, parsed_body)。"""
    return EngineConnError("Unexpected status code 400 in server response", body)


def _sio_4008(body: dict) -> SioConnError:
    """正常/快路径：socketio 重抛 ConnectionError，__cause__ 挂 engineio(含 body)。"""
    e = SioConnError("Unexpected status code 400 in server response")
    e.__cause__ = _engine_cause(body)
    return e


def _runtime_with_4008(body: dict) -> RuntimeError:
    """纵深防御路径：传输层裸 RuntimeError，但 __cause__ 仍挂着 4008（拓宽 catch 可救回）。"""
    e = RuntimeError("Connection closed.")
    e.__cause__ = _engine_cause(body)
    return e


def _runtime_no_body() -> RuntimeError:
    """body 真丢：裸 RuntimeError 无任何 4008 线索（coverage 竞态的真实形态）。"""
    return RuntimeError("Connection closed.")


def _sio_non_4008() -> SioConnError:
    """非版本错误：400 缺参，不得误判为 ProtocolVersionError。"""
    e = SioConnError("Unexpected status code 400 in server response")
    e.__cause__ = _engine_cause(_BODY_400)
    return e


# (raise_exc_factory, expect) —— expect: "pve" 归一为 ProtocolVersionError；否则原异常类型
_SCENARIOS = [
    pytest.param(lambda: _sio_4008(_BODY_4008), ProtocolVersionError, id="sio-4008-fast-path"),
    pytest.param(lambda: _runtime_with_4008(_BODY_4008), ProtocolVersionError, id="runtime-4008-defense-in-depth"),
    pytest.param(_runtime_no_body, RuntimeError, id="runtime-no-body-not-misclassified"),
    pytest.param(_sio_non_4008, SioConnError, id="non-4008-preserved"),
]


def _assert_pve_fields(exc: ProtocolVersionError) -> None:
    assert exc.client_version == "0.2.0"
    assert exc.server_version == "0.3.0"
    assert exc.min_supported == "0.3.0"
    assert exc.max_supported == "0.3.999"


# ---------------------------------------------------------------------------
# Async Agent
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(("make_exc", "expected"), _SCENARIOS)
async def test_async_agent_normalizes_4008(make_exc: Callable[[], BaseException], expected: type) -> None:
    agent = AsyncSMCPAgentClient(auth_provider=DefaultAgentAuthProvider(agent_id="r", office_id="o"))
    disconnect = AsyncMock()
    agent.connect = AsyncMock(side_effect=make_exc())  # type: ignore[method-assign]
    agent.disconnect = disconnect  # type: ignore[method-assign]
    with pytest.raises(expected) as ei:
        await agent.connect_to_server("http://h", namespace=SMCP_NAMESPACE)
    if expected is ProtocolVersionError:
        _assert_pve_fields(ei.value)
        disconnect.assert_awaited_once()  # §4：抛前主动断开
    else:
        disconnect.assert_not_awaited()  # 非 4008：不强制断开，保持原异常语义


# ---------------------------------------------------------------------------
# Sync Agent
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(("make_exc", "expected"), _SCENARIOS)
async def test_sync_agent_normalizes_4008(make_exc: Callable[[], BaseException], expected: type) -> None:
    agent = SMCPAgentClient(auth_provider=DefaultAgentAuthProvider(agent_id="r", office_id="o"))
    disconnect = MagicMock()
    agent.connect = MagicMock(side_effect=make_exc())  # type: ignore[method-assign]
    agent.disconnect = disconnect  # type: ignore[method-assign]

    def _run() -> None:
        agent.connect_to_server("http://h", namespace=SMCP_NAMESPACE)

    with pytest.raises(expected) as ei:
        await asyncio.to_thread(_run)
    if expected is ProtocolVersionError:
        _assert_pve_fields(ei.value)
        disconnect.assert_called_once()
    else:
        disconnect.assert_not_called()


# ---------------------------------------------------------------------------
# Computer client（connect 覆盖，调 super().connect）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(("make_exc", "expected"), _SCENARIOS)
async def test_computer_client_normalizes_4008(make_exc: Callable[[], BaseException], expected: type) -> None:
    client = SMCPComputerClient(computer=Computer(name="c", mcp_servers=set()))
    disconnect = AsyncMock()
    client.disconnect = disconnect  # type: ignore[method-assign]
    # 覆盖 super().connect（AsyncClient.connect）—— Computer.connect 内部 await super().connect
    with patch.object(AsyncClient, "connect", new=AsyncMock(side_effect=make_exc())):
        with pytest.raises(expected) as ei:
            await client.connect("http://h", namespaces=[SMCP_NAMESPACE])
    if expected is ProtocolVersionError:
        _assert_pve_fields(ei.value)
        disconnect.assert_awaited_once()
    else:
        disconnect.assert_not_awaited()


def test_clients_use_shared_broadened_catch_constant() -> None:
    """三处 connect 站点必须共用 HANDSHAKE_CONNECT_ERRORS（拓宽 catch 单一事实源，防漂移）。"""
    import a2c_smcp.agent.client as ac
    import a2c_smcp.agent.sync_client as sc
    import a2c_smcp.computer.socketio.client as cc
    from a2c_smcp.utils.handshake import HANDSHAKE_CONNECT_ERRORS

    assert RuntimeError in HANDSHAKE_CONNECT_ERRORS  # 裸 RuntimeError 必须可兜底
    assert SioConnError in HANDSHAKE_CONNECT_ERRORS
    for mod in (ac, sc, cc):
        assert mod.HANDSHAKE_CONNECT_ERRORS is HANDSHAKE_CONNECT_ERRORS
