# -*- coding: utf-8 -*-
# filename: test_rematerialize_on_start.py
"""
Issue #192（runtime-contract §5.13）Input 重解析时机向量（conformance §5 六景）——Computer + 真实 manager +
fake client（不 spawn 真进程）。镜像 rust PR#190 同构测试。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inputs.resolver import InputResolver, MissingInputError
from a2c_smcp.computer.inputs.value_store import ValueStore
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerPickStringInput,
    MCPServerPromptStringInput,
    PickStringOption,
    StdioServerConfig,
)
from a2c_smcp.utils.bundle_id import resolve_bundle_id


class _RecordingClient:
    """记录 spawn 配置的 fake MCP client（无真进程）。"""

    def __init__(self, config: StdioServerConfig, message_handler: Any = None) -> None:
        self.config = config
        self.message_handler = message_handler
        self.state = "stopped"
        self.disconnect_called = False
        self.list_tools = AsyncMock(return_value=[])

    async def aconnect(self) -> None:
        self.state = "connected"

    async def adisconnect(self) -> None:
        self.disconnect_called = True
        self.state = "disconnected"


_SPAWNED: list[_RecordingClient] = []


@pytest.fixture(autouse=True)
def _patch_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    _SPAWNED.clear()

    def factory(config: StdioServerConfig, message_handler: Any = None) -> _RecordingClient:
        client = _RecordingClient(config, message_handler)
        _SPAWNED.append(client)
        return client

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)


def _pick_region() -> MCPServerPickStringInput:
    return MCPServerPickStringInput(
        id="region",
        description="pick one",
        options=[PickStringOption(label="中国", value="cn"), PickStringOption(label="欧洲", value="eu")],
        default=None,
    )


def _cfg_with_env(env: dict[str, str], name: str = "pick-server") -> StdioServerConfig:
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command="/bin/echo", env=env),
    )


def _region_cfg(env_key: str = "R") -> StdioServerConfig:
    return _cfg_with_env({env_key: "${input:region}"})


def _make_comp(
    inputs: set,
    mcp_servers: set,
    *,
    auto_connect: bool = False,
    auto_reconnect: bool = False,
    resolver: InputResolver | None = None,
) -> Computer:
    return Computer(
        name="c",
        inputs=inputs,
        mcp_servers=mcp_servers,
        auto_connect=auto_connect,
        auto_reconnect=auto_reconnect,
        input_resolver=resolver,
    )


class _CountingResolver(InputResolver):
    """计数 aresolve_by_id 调用次数的 resolver（观察「单次渲染只解析一次 / 幂等 start 不解析」）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def aresolve_by_id(  # type: ignore[override]
        self,
        input_id: str,
        *,
        session: Any = None,
        plugin: str | None = None,
        marketplace: str | None = None,
    ) -> Any:
        self.calls += 1
        return await super().aresolve_by_id(input_id, session=session, plugin=plugin, marketplace=marketplace)


@pytest.mark.asyncio
async def test_stopped_start_uses_latest_value_and_running_not_hot_updated(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """①+③：stopped → 实际 start → resolver 最新值生效；运行中修改 resolver 返回值 → 运行实例不热更新。"""
    monkeypatch.setenv("A2C_SMCP_region", "cn")
    inputs = [_pick_region()]
    resolver = InputResolver(inputs, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _region_cfg()
    bid = resolve_bundle_id(cfg)
    comp = _make_comp({_pick_region()}, {cfg}, resolver=resolver)

    await comp.boot_up()  # auto_connect=False：boot 不解析、不启动
    assert comp.mcp_manager is not None
    assert _SPAWNED == []

    await comp.mcp_manager.astart_client(bid)
    assert _SPAWNED[-1].config.server_parameters.env == {"R": "cn"}

    # 改选（client 注入新值，cache 通道）→ 运行中实例不热更新
    comp.set_input_value("region", "eu")
    assert _SPAWNED[-1].config.server_parameters.env == {"R": "cn"}  # 运行实例保持

    # 停止 → 再启动 → 最新值生效
    await comp.mcp_manager.astop_client(bid)
    await comp.mcp_manager.astart_client(bid)
    assert _SPAWNED[-1].config.server_parameters.env == {"R": "eu"}


@pytest.mark.asyncio
async def test_restart_uses_latest_value(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """②：实际 restart（update-in-place 触发）→ resolver 最新值生效（改选后重启不再静默沿用旧渲染品）。"""
    monkeypatch.setenv("A2C_SMCP_region", "cn")
    resolver = InputResolver([_pick_region()], value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _region_cfg()
    bid = resolve_bundle_id(cfg)
    comp = _make_comp({_pick_region()}, {cfg}, auto_reconnect=True, resolver=resolver)

    await comp.boot_up()
    await comp.mcp_manager.astart_client(bid)
    old_client = _SPAWNED[-1]
    assert old_client.config.server_parameters.env == {"R": "cn"}

    comp.set_input_value("region", "eu")
    # 同 bundle_id 挂载（update-in-place）→ restart：先 materialize 再 stop 旧 → 新 spawn 用最新值
    await comp.amount_server(_region_cfg())

    assert old_client.disconnect_called
    assert _SPAWNED[-1].config.server_parameters.env == {"R": "eu"}


@pytest.mark.asyncio
async def test_idempotent_start_does_not_reresolve(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """④：已运行 server 幂等 start MAY no-op——不重复解析（materialize 不再次调用 resolver）。"""
    monkeypatch.setenv("A2C_SMCP_region", "cn")
    resolver = _CountingResolver([_pick_region()], value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _region_cfg()
    bid = resolve_bundle_id(cfg)
    comp = _make_comp({_pick_region()}, {cfg}, resolver=resolver)

    await comp.boot_up()
    await comp.mcp_manager.astart_client(bid)
    assert resolver.calls == 1
    await comp.mcp_manager.astart_client(bid)  # 幂等
    assert resolver.calls == 1  # 不重复解析
    assert len(_SPAWNED) == 1


@pytest.mark.asyncio
async def test_single_render_resolves_same_id_once(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """⑤：单次 server 渲染中相同 id 只解析一次（per-render map）。"""
    monkeypatch.setenv("A2C_SMCP_region", "cn")
    resolver = _CountingResolver([_pick_region()], value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _cfg_with_env({"A": "${input:region}", "B": "${input:region}"})
    bid = resolve_bundle_id(cfg)
    comp = _make_comp({_pick_region()}, {cfg}, resolver=resolver)

    await comp.boot_up()
    await comp.mcp_manager.astart_client(bid)
    assert resolver.calls == 1
    assert _SPAWNED[-1].config.server_parameters.env == {"A": "cn", "B": "cn"}


@pytest.mark.asyncio
async def test_restart_materialize_failure_preserves_running_process(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⑥：实际启动重解析失败 → 结构化错误，且尽量保留仍在运行的旧进程（restart 场景：先 materialize 后 stop）。"""
    monkeypatch.setenv("A2C_SMCP_region", "cn")
    inputs = {_pick_region(), MCPServerPromptStringInput(id="must_fill", description="d")}
    resolver = InputResolver(inputs, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _region_cfg()
    bid = resolve_bundle_id(cfg)
    comp = _make_comp(inputs, {cfg}, auto_reconnect=True, resolver=resolver)

    await comp.boot_up()
    await comp.mcp_manager.astart_client(bid)
    old_client = _SPAWNED[-1]
    assert old_client.state == "connected"

    # 新 raw 引用缺失 input（defined but headless-unresolvable）→ restart materialize 失败
    bad_cfg = _cfg_with_env({"R": "${input:must_fill}"})
    with pytest.raises(MissingInputError):
        await comp.amount_server(bad_cfg)

    # 旧进程保留：未 disconnect、仍在活跃表、配置未被替换
    assert old_client.disconnect_called is False
    assert old_client.state == "connected"
    assert comp.mcp_manager._active_clients == {bid: old_client}  # type: ignore[attr-defined]
    # 旧 raw 声明条目保留（§5.13 失败零变更）：wire 投影仍可 join 回旧 raw（不得 fail-closed 省略仍在运行的 server）
    assert comp._active_raw[bid].config.name == "pick-server"  # type: ignore[attr-defined]
    assert {resolve_bundle_id(c) for c in comp.active_server_configs()} == {bid}


@pytest.mark.asyncio
async def test_boot_does_not_resolve_inputs_before_actual_start(tmp_path: pytest.TempPathFactory) -> None:
    """§5.13：boot 不解析 input 值——缺失 input 的结构化错误从下一次实际 start/restart surface。"""
    inputs = {MCPServerPromptStringInput(id="must_fill", description="d")}
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _cfg_with_env({"R": "${input:must_fill}"}, name="bad-server")
    bid = resolve_bundle_id(cfg)
    comp = _make_comp(inputs, {cfg}, resolver=resolver)

    await comp.boot_up()  # 不抛（旧行为：boot 渲染即抛 Missing）
    assert _SPAWNED == []
    with pytest.raises(MissingInputError):
        await comp.mcp_manager.astart_client(bid)


@pytest.mark.asyncio
async def test_boot_auto_connect_rollback_on_resolution_failure(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#173 retry-safe 保持：auto_connect boot 中途 materialize 失败 → 回滚已启动 client（无残留）→ 补值重试成功。"""
    monkeypatch.delenv("A2C_SMCP_must_fill", raising=False)
    inputs = {MCPServerPromptStringInput(id="must_fill", description="d")}
    resolver = InputResolver(inputs, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    good = _cfg_with_env({"X": "plain"}, name="good-server")
    bad = _cfg_with_env({"R": "${input:must_fill}"}, name="bad-server")
    comp = _make_comp(inputs, {good, bad}, auto_connect=True, resolver=resolver)

    with pytest.raises(MissingInputError):
        await comp.boot_up()

    # 回滚：无论启动顺序，失败后无残留活跃 client / 配置
    assert comp.mcp_manager is not None
    assert comp.mcp_manager._active_clients == {}  # type: ignore[attr-defined]
    assert comp.mcp_manager._servers_config == {}  # type: ignore[attr-defined]

    # 补值 → 同实例重试成功（两 server 均启动）
    monkeypatch.setenv("A2C_SMCP_must_fill", "x")
    await comp.boot_up()
    assert len(comp.mcp_manager._active_clients) == 2  # type: ignore[attr-defined]
