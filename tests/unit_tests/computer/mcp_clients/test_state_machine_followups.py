# -*- coding: utf-8 -*-
# filename: test_state_machine_followups.py
# @Author  : JQQ
# @Software: PyCharm
"""#252：MCP client 状态机 follow-up 的回归用例（manager 侧）。

四项 follow-up 来自 #247（PR #251）的实现与独立审查：

1. ``_commit_active_client`` 提交前必须校验 ``state == connected``——``aconnect()`` 可以在
   keep-alive 已死、收敛任务尚未执行时**正常返回**，旧实现会把 error client 登记为 active，
   此后 :meth:`MCPServerManager.astart_client` 的幂等早退还会「谎报已启动」。
2. 被弃置的 client（challenge / redirect-stop / flow-abort / 超时 / connect 异常）必须显式回收
   会话与传输上下文——旧实现只回收内层竞速任务，client 悬空时其 keep-alive 任务仍持有
   ``ClientSession`` 与 httpx 传输。
3. （client 侧，见 ``test_base_client.py``）关闭信号按代隔离。
4. （client 侧）``async_session`` 惰性连接收窄。

另含本单同批修正的两条**死分支**：``_bounded_connect`` 的 30s 超时（``asyncio.wait`` 超时返回空
``done`` 而不抛 ``TimeoutError``）与「connect_task 被取消」都曾落到 ``return None``——把「没连上」
上报成「连接成功」并提交。

/ Regression cases for the #252 state-machine follow-ups (manager side).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from a2c_smcp.computer.mcp_clients import manager as manager_mod
from a2c_smcp.computer.mcp_clients.auth_error import UpstreamRedirectStoppedError
from a2c_smcp.computer.mcp_clients.base_client import STATES
from a2c_smcp.computer.mcp_clients.http_client import AuthSignal
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import MCPServerConnectionState, StreamableHttpServerConfig
from a2c_smcp.computer.mcp_clients.oauth_types import OAuthError, OAuthErrorCode
from tests.unit_tests.computer.mcp_clients.test_tool_list_cache import _cfg


class _FakeHttpClient:
    """``_bounded_connect`` 的最小假 client。

    三种「非成功」形态由构造参数决定：challenge 信号（事件预置 ⇒ 与 connect 同帧完成）、
    redirect-stop、connect 永不返回（测超时）/ connect 自我取消。
    """

    def __init__(
        self,
        *,
        challenge: AuthSignal | None = None,
        signal_less_challenge: bool = False,
        redirect_stop: int | None = None,
        hang: bool = False,
        cancel: bool = False,
        session_alive: bool = True,
        keepalive_alive: bool = True,
        list_gate: asyncio.Event | None = None,
    ) -> None:
        self._challenge = challenge
        self._redirect_stop = redirect_stop
        self._hang = hang
        self._cancel = cancel
        self._challenge_event = asyncio.Event()
        self._redirect_event = asyncio.Event()
        if challenge is not None or signal_less_challenge:
            self._challenge_event.set()
        if redirect_stop is not None:
            self._redirect_event.set()
        self.state = STATES.connected
        # #252：真实可用性判据的三要素（state / 会话 / keep-alive 存活）——假 client 也照此建模，
        # 否则「connected + 无会话」这一真实形态无处构造。
        self._async_session: Any = object() if session_alive else None
        self._keepalive_alive = keepalive_alive
        self._list_gate = list_gate
        self.list_entered = asyncio.Event()
        self.adiscard: Any = _AsyncRecorder()
        self.adisconnect: Any = _AsyncRecorder()

    @property
    def is_session_usable(self) -> bool:
        return self.state == STATES.connected and self._async_session is not None and self._keepalive_alive

    async def aconnect(self) -> None:
        if self._cancel:
            raise asyncio.CancelledError
        if self._hang:
            await asyncio.Event().wait()

    async def list_tools(self) -> list[Any]:
        """提交成功后 :meth:`MCPServerManager._arefresh_tool_mapping` 会真读一次。"""
        self.list_entered.set()
        if self._list_gate is not None:
            await self._list_gate.wait()
        return []

    def connect_challenge_event(self) -> asyncio.Event:
        return self._challenge_event

    def take_connect_challenge(self) -> AuthSignal | None:
        return self._challenge

    def connect_redirect_event(self) -> asyncio.Event:
        return self._redirect_event

    def take_connect_redirect_stop(self) -> int | None:
        return self._redirect_stop


class _AsyncRecorder:
    """记录调用次数的异步替身（不依赖 unittest.mock 的协程语义）。"""

    def __init__(self) -> None:
        self.await_count = 0

    async def __call__(self) -> None:
        self.await_count += 1


class _StubCoordinator:
    """``_aoauth_connect`` 所需的最小 coordinator 面。"""

    def __init__(self) -> None:
        self._aborted = asyncio.Event()
        self.failed_launches: list[OAuthError] = []

    def flow_aborted_event(self) -> asyncio.Event:
        return self._aborted

    def fail_launch(self, error: OAuthError) -> None:
        self.failed_launches.append(error)


def _manager() -> MCPServerManager:
    return MCPServerManager(auto_connect=False)


# ────────────────────── ① 提交前状态校验 ──────────────────────


@pytest.mark.asyncio
async def test_commit_rejects_non_connected_client() -> None:
    """①：``error`` 状态的 client 不得登记为 active（回滚后提交成功 ⇒ 本用例红）。"""
    manager = _manager()
    config = _cfg("alpha")
    manager._servers_config["alpha"] = config
    dead = _FakeHttpClient()
    dead.state = STATES.error

    with pytest.raises(ConnectionError):
        await manager._commit_active_client("alpha", dead, config=config)  # type: ignore[arg-type]

    assert "alpha" not in manager._active_clients, "坏 client 不得登记为 active"
    assert manager._connection_states["alpha"] == MCPServerConnectionState.ERROR
    assert dead.adisconnect.await_count == 1, "被拒的 client 必须退役（不得泄漏）"


@pytest.mark.asyncio
async def test_commit_accepts_connected_client() -> None:
    """① 正对照：健康 client 照常提交（守卫不得误伤）。"""
    manager = _manager()
    config = _cfg("alpha")
    manager._servers_config["alpha"] = config
    good = _FakeHttpClient()

    await manager._commit_active_client("alpha", good, config=config)  # type: ignore[arg-type]

    assert manager._active_clients["alpha"] is good
    assert manager._connection_states["alpha"] == MCPServerConnectionState.CONNECTED
    assert good.adisconnect.await_count == 0


@pytest.mark.asyncio
async def test_commit_rejection_does_not_overwrite_healthy_status() -> None:
    """①：已有健康活跃 client 时，坏 client 的提交被拒**不得**把状态改成 ERROR。"""
    manager = _manager()
    config = _cfg("alpha")
    manager._servers_config["alpha"] = config
    good = _FakeHttpClient()
    await manager._commit_active_client("alpha", good, config=config)  # type: ignore[arg-type]

    dead = _FakeHttpClient()
    dead.state = STATES.error
    with pytest.raises(ConnectionError):
        await manager._commit_active_client("alpha", dead, config=config)  # type: ignore[arg-type]

    assert manager._active_clients["alpha"] is good
    assert manager._connection_states["alpha"] == MCPServerConnectionState.CONNECTED, "健康 client 的状态面不得被坏提交覆盖"


# ────────────────────── `_bounded_connect` 死分支 ──────────────────────


@pytest.mark.asyncio
async def test_bounded_connect_timeout_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时必须上抛而非被当成「连接成功」（``asyncio.wait`` 超时返回空 ``done`` 不抛异常）。"""
    monkeypatch.setattr(manager_mod, "_CONNECT_TIMEOUT", 0.05)
    manager = _manager()
    hanging = _FakeHttpClient(hang=True)

    with pytest.raises(TimeoutError):
        await manager._bounded_connect(hanging)  # type: ignore[arg-type]

    assert hanging.adiscard.await_count == 1, "超时弃置的 client 必须回收"


@pytest.mark.asyncio
async def test_bounded_connect_treats_cancelled_connect_as_failure() -> None:
    """connect 任务被取消不是「连接成功」——旧实现落到 ``return None``。"""
    manager = _manager()
    cancelled = _FakeHttpClient(cancel=True)

    with pytest.raises(ConnectionError):
        await manager._bounded_connect(cancelled)  # type: ignore[arg-type]

    assert cancelled.adiscard.await_count == 1


# ────────────────────── ② 弃置 client 的会话回收 ──────────────────────


@pytest.mark.asyncio
async def test_bounded_connect_discards_client_on_challenge() -> None:
    """②：challenge 信号 ⇒ client 未被提交，必须显式回收其会话/传输上下文。"""
    manager = _manager()
    challenged = _FakeHttpClient(challenge=AuthSignal(401, None))

    signal = await manager._bounded_connect(challenged)  # type: ignore[arg-type]

    assert signal is not None and signal.status_code == 401
    assert challenged.adiscard.await_count == 1, "弃置 client 必须显式回收（回滚后为 0 ⇒ 红）"
    assert challenged.adisconnect.await_count == 0, "弃置路径不得经状态机 adisconnect"


@pytest.mark.asyncio
async def test_bounded_connect_keeps_client_on_success() -> None:
    """② 正对照：连接成功时 client 交给调用方提交，**不得**被回收。"""
    manager = _manager()
    healthy = _FakeHttpClient()

    assert await manager._bounded_connect(healthy) is None  # type: ignore[arg-type]

    assert healthy.adiscard.await_count == 0
    assert healthy.adisconnect.await_count == 0


@pytest.mark.asyncio
async def test_bounded_connect_discards_client_on_redirect_stop() -> None:
    """②：跨 origin redirect 被安全守卫 stop 时同样弃置 ⇒ 同样必须回收。"""
    manager = _manager()
    stopped = _FakeHttpClient(redirect_stop=302)

    with pytest.raises(UpstreamRedirectStoppedError):
        await manager._bounded_connect(stopped)  # type: ignore[arg-type]

    assert stopped.adiscard.await_count == 1


@pytest.mark.asyncio
async def test_aoauth_connect_discards_client_on_redirect_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """②：detached 交互式 OAuth 路径的非成功退出同样回收弃置 client。"""
    manager = _manager()
    bundle_id = "oauth_srv"
    manager._servers_config[bundle_id] = StreamableHttpServerConfig(
        name=bundle_id,
        server_parameters={"url": "https://mcp.example.com/x"},
    )
    coordinator = _StubCoordinator()
    manager._oauth_coordinators[bundle_id] = coordinator  # type: ignore[assignment]
    stopped = _FakeHttpClient(redirect_stop=302)
    monkeypatch.setattr(manager_mod, "client_factory", lambda *a, **kw: stopped)

    await manager._aoauth_connect(bundle_id, coordinator, manager._oauth_clear_epochs.get(bundle_id, 0))  # type: ignore[arg-type]

    assert stopped.adiscard.await_count == 1, "detached 路径弃置 client 必须回收（回滚后为 0 ⇒ 红）"
    assert manager._connection_states[bundle_id] == MCPServerConnectionState.ERROR


@pytest.mark.asyncio
async def test_aoauth_connect_commit_rejection_does_not_leak_task_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """①：交互式 OAuth 提交被状态守卫拒绝时，detached 任务**不得**带未回收异常收场。"""
    manager = _manager()
    bundle_id = "oauth_srv"
    manager._servers_config[bundle_id] = StreamableHttpServerConfig(
        name=bundle_id,
        server_parameters={"url": "https://mcp.example.com/x"},
    )
    coordinator = _StubCoordinator()
    manager._oauth_coordinators[bundle_id] = coordinator  # type: ignore[assignment]
    dying = _FakeHttpClient()
    dying.state = STATES.error
    monkeypatch.setattr(manager_mod, "client_factory", lambda *a, **kw: dying)

    await manager._aoauth_connect(bundle_id, coordinator, manager._oauth_clear_epochs.get(bundle_id, 0))  # type: ignore[arg-type]

    assert bundle_id not in manager._active_clients
    assert manager._connection_states[bundle_id] == MCPServerConnectionState.ERROR
    assert coordinator.failed_launches, "提交失败必须收敛 launch 等待者（不得留下悬挂的 launch）"
    assert dying.adiscard.await_count == 1


# ────────────────────── 🔴 审查补强：真实半断开形态 / 外层取消 / 状态面 ──────────────────────


@pytest.mark.asyncio
async def test_commit_rejects_half_broken_client() -> None:
    """🔴 真实形态：`state == connected` + 无会话 + keep-alive 已死 ⇒ 提交必须被拒。

    中文: 这正是 #252 要拦的窗口——传输在 ``initialize()`` 返回后立即死亡时，keep-alive 的 finally
    已清空会话，而收敛任务要等在途的 ``aconnect`` 结束才能把状态翻成 ``error``，故 aconnect 正常
    返回那一刻 state **仍是 connected**。只判 state 的守卫会放行（审查指出的假绿源）。
    """
    manager = _manager()
    config = _cfg("alpha")
    manager._servers_config["alpha"] = config
    half_broken = _FakeHttpClient(session_alive=False, keepalive_alive=False)
    assert half_broken.state == STATES.connected, "前置：状态仍是 connected（假绿源的关键）"

    with pytest.raises(ConnectionError):
        await manager._commit_active_client("alpha", half_broken, config=config)  # type: ignore[arg-type]

    assert "alpha" not in manager._active_clients
    assert manager._connection_states["alpha"] == MCPServerConnectionState.ERROR
    assert half_broken.adisconnect.await_count == 1


@pytest.mark.asyncio
async def test_commit_rejection_keeps_healthy_active_client_status() -> None:
    """🔴：已有健康活跃 client 时，半断开 client 的提交被拒**不得**把状态面改成 ERROR。"""
    manager = _manager()
    config = _cfg("alpha")
    manager._servers_config["alpha"] = config
    healthy = _FakeHttpClient()
    await manager._commit_active_client("alpha", healthy, config=config)  # type: ignore[arg-type]

    half_broken = _FakeHttpClient(session_alive=False, keepalive_alive=False)
    with pytest.raises(ConnectionError):
        await manager._commit_active_client("alpha", half_broken, config=config)  # type: ignore[arg-type]

    assert manager._active_clients["alpha"] is healthy
    assert manager._connection_states["alpha"] == MCPServerConnectionState.CONNECTED


@pytest.mark.asyncio
async def test_bounded_connect_challenge_without_payload_is_a_failure() -> None:
    """challenge 竞速先到但取不到信号载荷 ⇒ 必须失败（否则返回 None 会让调用方提交已弃置的 client）。"""
    manager = _manager()
    payload_less = _FakeHttpClient(signal_less_challenge=True)

    with pytest.raises(ConnectionError):
        await manager._bounded_connect(payload_less)  # type: ignore[arg-type]

    assert payload_less.adiscard.await_count == 1


@pytest.mark.asyncio
async def test_bounded_connect_discards_client_when_outer_cancelled() -> None:
    """外层取消：client 必须被回收，且 ``CancelledError`` 原样传播（不得包成 TimeoutError）。"""
    manager = _manager()
    hanging = _FakeHttpClient(hang=True)
    task = asyncio.create_task(manager._bounded_connect(hanging))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)  # 让 connect 任务真正起来
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert hanging.adiscard.await_count == 1, "取消形态下弃置 client 也必须回收（cleanup 路径）"


@pytest.mark.asyncio
async def test_astart_client_marks_error_when_bounded_connect_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_bounded_connect` 现在会真实上抛超时 ⇒ 调用方必须把连接态收敛为 ERROR（不得停在 CONNECTING）。"""
    monkeypatch.setattr(manager_mod, "_CONNECT_TIMEOUT", 0.05)
    manager = _manager()
    bundle_id = "hung_srv"
    manager._servers_config[bundle_id] = StreamableHttpServerConfig(
        name=bundle_id,
        server_parameters={"url": "https://mcp.example.com/hung"},
    )
    monkeypatch.setattr(manager_mod, "client_factory", lambda *a, **kw: _FakeHttpClient(hang=True))

    with pytest.raises(TimeoutError):
        await manager.astart_client(bundle_id)

    assert manager._connection_states[bundle_id] == MCPServerConnectionState.ERROR
    assert bundle_id not in manager._active_clients


@pytest.mark.asyncio
async def test_aoauth_connect_does_not_discard_committed_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：成功提交的 client 归 manager 所有，绝不 `adiscard`（🔴 归属 bug 的回归锁）。"""
    manager = _manager()
    bundle_id = "oauth_srv"
    manager._servers_config[bundle_id] = StreamableHttpServerConfig(
        name=bundle_id,
        server_parameters={"url": "https://mcp.example.com/x"},
    )
    coordinator = _StubCoordinator()
    manager._oauth_coordinators[bundle_id] = coordinator  # type: ignore[assignment]
    healthy = _FakeHttpClient()
    monkeypatch.setattr(manager_mod, "client_factory", lambda *a, **kw: healthy)

    await manager._aoauth_connect(bundle_id, coordinator, manager._oauth_clear_epochs.get(bundle_id, 0))  # type: ignore[arg-type]

    assert manager._active_clients[bundle_id] is healthy
    assert healthy.adiscard.await_count == 0, "已提交的 client 不得被弃置回收"


@pytest.mark.asyncio
async def test_aoauth_connect_cancelled_connect_converges(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect 任务被取消不得静默 return——必须收敛 launch 等待者与连接态（旧实现静默返回）。"""
    manager = _manager()
    bundle_id = "oauth_srv"
    manager._servers_config[bundle_id] = StreamableHttpServerConfig(
        name=bundle_id,
        server_parameters={"url": "https://mcp.example.com/x"},
    )
    coordinator = _StubCoordinator()
    manager._oauth_coordinators[bundle_id] = coordinator  # type: ignore[assignment]
    cancelled = _FakeHttpClient(cancel=True)
    monkeypatch.setattr(manager_mod, "client_factory", lambda *a, **kw: cancelled)

    await manager._aoauth_connect(bundle_id, coordinator, manager._oauth_clear_epochs.get(bundle_id, 0))  # type: ignore[arg-type]

    assert coordinator.failed_launches, "取消形态也必须解 launch 等待者（不得悬挂）"
    assert manager._connection_states[bundle_id] == MCPServerConnectionState.ERROR
    assert cancelled.adiscard.await_count == 1


@pytest.mark.asyncio
async def test_aoauth_connect_commit_rejection_keeps_healthy_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴：detached 提交被拒时不得覆盖**健康活跃 client** 的连接状态。"""
    manager = _manager()
    bundle_id = "oauth_srv"
    config = StreamableHttpServerConfig(
        name=bundle_id,
        server_parameters={"url": "https://mcp.example.com/x"},
    )
    manager._servers_config[bundle_id] = config
    healthy = _FakeHttpClient()
    await manager._commit_active_client(bundle_id, healthy, config=config)  # type: ignore[arg-type]

    coordinator = _StubCoordinator()
    manager._oauth_coordinators[bundle_id] = coordinator  # type: ignore[assignment]
    dying = _FakeHttpClient(session_alive=False, keepalive_alive=False)
    monkeypatch.setattr(manager_mod, "client_factory", lambda *a, **kw: dying)

    await manager._aoauth_connect(bundle_id, coordinator, manager._oauth_clear_epochs.get(bundle_id, 0))  # type: ignore[arg-type]

    assert manager._active_clients[bundle_id] is healthy
    assert manager._connection_states[bundle_id] == MCPServerConnectionState.CONNECTED, "健康 client 的状态面不得被坏提交覆盖"
    assert dying.adiscard.await_count == 1


@pytest.mark.asyncio
async def test_ensure_oauth_coordinator_wraps_bounded_connect_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """`OAuthFlow.launch` 是宿主的 typed facade 入口（#179）：bootstrap 的裸超时/连接失败必须转 OAuthError。

    安全断言（`oauth_types.OAuthError` 约定）：``message`` 必须是**静态文案**，绝不携带 provider
    响应 / token / authorization URL / code / state；原异常只能经 ``__cause__`` 保留。
    """
    manager = _manager()
    sensitive = "https://as.example/token?code=SECRET-CODE&state=SECRET-STATE&client_secret=SECRET-KEY"

    async def _boom(bundle_id: str) -> None:
        raise TimeoutError(sensitive)

    monkeypatch.setattr(manager, "_astart_client_auto", _boom)

    with pytest.raises(OAuthError) as exc:
        await manager._ensure_oauth_coordinator("oauth_srv")

    assert exc.value.code == OAuthErrorCode.Protocol
    assert "SECRET" not in exc.value.message, "OAuthError.message 必须为静态文案（不得携带 provider 侧敏感文本）"
    assert "as.example" not in exc.value.message, "不得把 authorization URL 带进 message"
    assert isinstance(exc.value.__cause__, TimeoutError), "原始异常必须保留为 __cause__（诊断不丢）"
    assert "SECRET" in str(exc.value.__cause__), "前置：敏感文本确实只走 __cause__"


@pytest.mark.asyncio
async def test_aoauth_connect_cancel_during_commit_refresh_keeps_owned_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴：提交**已在锁内登记**、随后在 ``_arefresh_tool_mapping`` 期间被取消 ⇒ 不得回收 manager-owned client。

    中文: ``committed`` 只在 ``_commit_active_client`` 整体返回后才置位，而登记发生在它内部、刷新之前。
    若 finally 只信 flag，就会把已登记为 active 的 client 拆掉，重新制造「connected + 无会话」。
    """
    manager = _manager()
    bundle_id = "oauth_srv"
    manager._servers_config[bundle_id] = StreamableHttpServerConfig(
        name=bundle_id,
        server_parameters={"url": "https://mcp.example.com/x"},
    )
    coordinator = _StubCoordinator()
    manager._oauth_coordinators[bundle_id] = coordinator  # type: ignore[assignment]
    gate = asyncio.Event()
    owned = _FakeHttpClient(list_gate=gate)
    monkeypatch.setattr(manager_mod, "client_factory", lambda *a, **kw: owned)

    task = asyncio.create_task(
        manager._aoauth_connect(bundle_id, coordinator, manager._oauth_clear_epochs.get(bundle_id, 0))  # type: ignore[arg-type]
    )
    await asyncio.wait_for(owned.list_entered.wait(), timeout=2.0)
    assert manager._active_clients.get(bundle_id) is owned, "前置：client 已在锁内登记"

    task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)

    assert manager._active_clients.get(bundle_id) is owned, "已登记的 client 不得被 finally 摘除"
    assert owned.adiscard.await_count == 0, "归属 manager 的 client 绝不能被 adiscard"


@pytest.mark.asyncio
async def test_commit_restores_swallowed_cancellation_instead_of_connection_error() -> None:
    """#252 × #211：连接被取消打断时，提交守卫必须**还原取消**，不得伪装成 ConnectionError。

    中文: transitions 的 ``process_context`` 按设计吞掉 ``CancelledError`` ⇒ ``aconnect()`` 照常返回，
    留下的正是「connected + 无会话」。若守卫抛 ``ConnectionError``，``astart_*`` / ``boot_up`` 的
    ``@restores_cancellation`` 与宿主 ``wait_for`` 都会把「取消」看成「连接失败」。
    """
    manager = _manager()
    config = _cfg("alpha")
    manager._servers_config["alpha"] = config
    half_broken = _FakeHttpClient(session_alive=False, keepalive_alive=False)

    async def _run() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)  # 取消在此投递，由本层吞掉（模拟 transitions 的吞点）
        except asyncio.CancelledError:
            pass
        await manager._commit_active_client("alpha", half_broken, config=config)  # type: ignore[arg-type]

    task = asyncio.create_task(_run())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "alpha" not in manager._active_clients, "半断开 client 仍不得登记"
    assert half_broken.adisconnect.await_count == 1
