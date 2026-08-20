# -*- coding: utf-8 -*-
# filename: test_computer_exceptions.py
"""
#173 结构化错误 + #192 / §5.13 语义迁移：
- boot / mount **不解析 input**（渲染推迟到实际启动）——Missing / ResolverFailed 从实际 start/restart surface；
- boot auto_connect 的 materialize 失败 → 回滚已启动 client 后上抛（retry-safe 保持）；
- durable add：形状非法仍早失败不落盘；input 缺失**不再阻断落盘**（raw 声明落盘，start 时 surface）；
- 未定义占位符字面保留（VS Code parity）行为不变。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters
from pydantic import ValidationError

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.inputs.resolver import (
    InputKind,
    InputResolutionError,
    InputResolver,
    MissingInputError,
    ResolverFailedError,
)
from a2c_smcp.computer.inputs.value_store import ValueStore
from a2c_smcp.computer.mcp_clients.model import MCPServerPromptStringInput, StdioServerConfig
from a2c_smcp.computer.settings.mcp_config import McpWriteScope, mcp_write_path
from a2c_smcp.utils.bundle_id import resolve_bundle_id


class _NoSecret:
    """空 secret store（keyring miss）确定性替身 / deterministic empty secret store."""

    available = False

    def get(self, input_id: str) -> str | None:  # noqa: ARG002
        return None

    def set(self, input_id: str, value: str) -> bool:  # noqa: ARG002
        return False


class _StubManager:
    """避免真实 spawn 的 MCPServerManager 替身 / stub manager (no real process spawn)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.received_servers: list[Any] = []

    async def ainitialize(self, servers: Any) -> None:
        self.received_servers = list(servers)

    async def aclose(self) -> None:
        pass


class _FakeClient:
    """最小 fake client（boot auto_connect 重试路径需真实 manager spawn）。"""

    def __init__(self, config: Any = None, message_handler: Any = None) -> None:
        self.state = "stopped"

    async def aconnect(self) -> None:
        self.state = "connected"

    async def adisconnect(self) -> None:
        self.state = "disconnected"

    def list_tools(self):  # noqa: ANN201
        import asyncio

        async def _empty():
            return []

        return asyncio.sleep(0, result=[])


def _patch_client_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "a2c_smcp.computer.mcp_clients.manager.client_factory",
        lambda config, message_handler=None: _FakeClient(config, message_handler),
    )


def _stdio_cfg(name: str, arg: str) -> StdioServerConfig:
    """单 server，env 中引用 ``${input:<id>}``（materialize 时经 resolver 解析）。"""
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command="/bin/echo", env={"X": arg}),
    )


def _comp(inputs: set, mcp_servers: set, *, resolver: InputResolver, auto_connect: bool = False) -> Computer:
    return Computer(
        name="c",
        inputs=inputs,
        mcp_servers=mcp_servers,
        auto_connect=auto_connect,
        auto_reconnect=False,
        input_resolver=resolver,
    )


@pytest.mark.asyncio
async def test_arender_and_validate_server_missing_input_keeps_original() -> None:
    """
    当出现未定义的 ${input:NOT_DEFINED} 时，渲染器按设计返回原值字符串而非抛错，
    因此应成功返回校验后的模型，且 env 中仍保留占位符字符串。
    覆盖 computer.py 中对未定义输入的 warning 日志路径。
    """
    comp = Computer(name="test", inputs=[], mcp_servers=set(), auto_connect=False, auto_reconnect=False)

    params = StdioServerParameters(
        command="echo",
        args=["hello"],
        env={"FOO": "${input:NOT_DEFINED}"},
        cwd=None,
        encoding="utf-8",
        encoding_error_handler="strict",
    )
    cfg = StdioServerConfig(name="bad", server_parameters=params)

    # #149：返回 (raw, rendered)；本用例断言渲染后结果，取第二元。
    _raw, validated = await comp._arender_and_validate_server(cfg)  # type: ignore[attr-defined]
    assert validated.server_parameters.env is not None
    assert validated.server_parameters.env.get("FOO") == "${input:NOT_DEFINED}"


# ── #192 / §5.13：boot/mount 不解析 input——Missing/ResolverFailed 从实际 start surface ──


@pytest.mark.asyncio
async def test_boot_up_propagates_missing_value_input(tmp_path: Path) -> None:
    """已定义 value input、headless 无 env/store/default → boot 不抛（只登记 raw），实际 start 上抛
    MissingInputError(VALUE)（非吞错保底）。"""
    inputs = [MCPServerPromptStringInput(id="b173_val", description="d")]  # default=None
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _stdio_cfg("s", "${input:b173_val}")
    comp = _comp({inputs[0]}, {cfg}, resolver=resolver)
    await comp.boot_up()  # 不抛：boot 不解析 input
    assert comp.mcp_manager is not None
    with pytest.raises(MissingInputError) as ei:
        await comp.mcp_manager.astart_client(resolve_bundle_id(cfg))
    err = ei.value
    assert err.kind is InputKind.VALUE
    assert err.id == "b173_val"
    assert err.env_hint == "A2C_SMCP_b173_val"
    assert err.error_code == 400


@pytest.mark.asyncio
async def test_boot_up_propagates_missing_secret_input_no_plaintext(tmp_path: Path) -> None:
    """password:true secret 缺失 → start 上抛 Missing(SECRET)；Missing 无值字段 ⇒ 错误天然不含明文。"""
    inputs = [MCPServerPromptStringInput(id="b173_sec", description="d", password=True)]
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}), secret_store=_NoSecret())
    cfg = _stdio_cfg("s", "${input:b173_sec}")
    comp = _comp({inputs[0]}, {cfg}, resolver=resolver)
    await comp.boot_up()
    assert comp.mcp_manager is not None
    with pytest.raises(MissingInputError) as ei:
        await comp.mcp_manager.astart_client(resolve_bundle_id(cfg))
    err = ei.value
    assert err.kind is InputKind.SECRET
    assert err.id == "b173_sec"
    # 错误文案不得含 "password" 字样 / 不含任何疑似明文
    assert "password" not in str(err).lower()


@pytest.mark.asyncio
async def test_boot_up_propagates_resolver_hard_failure(tmp_path: Path) -> None:
    """client resolver 硬失败 → start 上抛 ResolverFailedError（区别于 Missing 的未提供）。"""

    class _Failing(InputResolver):
        async def aresolve_by_id(  # type: ignore[override]
            self,
            input_id: str,
            *,
            session: Any = None,
            plugin: str | None = None,
            marketplace: str | None = None,
        ) -> Any:
            raise ResolverFailedError(id=input_id, reason="boom")

    inputs = [MCPServerPromptStringInput(id="b173_fail", description="d")]
    resolver = _Failing(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _stdio_cfg("s", "${input:b173_fail}")
    comp = _comp({inputs[0]}, {cfg}, resolver=resolver)
    await comp.boot_up()
    assert comp.mcp_manager is not None
    with pytest.raises(ResolverFailedError) as ei:
        await comp.mcp_manager.astart_client(resolve_bundle_id(cfg))
    assert ei.value.id == "b173_fail"
    assert "boom" in ei.value.reason
    assert isinstance(ei.value, InputResolutionError)


@pytest.mark.asyncio
async def test_boot_up_retry_succeeds_after_value_provided(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """boot 失败后生命周期可安全重试——补值（env）后同实例重试成功（无残留 manager/task/transport 阻塞）。
    注：python Computer 无 rust 的 lifecycle 状态机，故以「重试成功 + 回滚后无残留」证无残留（对齐 rust retry 测试意图）。"""
    var = "A2C_SMCP_b173_retry"
    monkeypatch.delenv(var, raising=False)  # 确保首轮缺失
    _patch_client_factory(monkeypatch)

    inputs = [MCPServerPromptStringInput(id="b173_retry", description="d")]
    # env=os.environ（live）便于中途 setenv；value_store 走 tmp_path 隔离。
    resolver = InputResolver(inputs, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _stdio_cfg("s", "${input:b173_retry}")
    comp = _comp({inputs[0]}, {cfg}, resolver=resolver, auto_connect=True)

    # 首轮：值缺失 → auto-connect 实际启动 materialize 抛 Missing → boot 失败并回滚
    with pytest.raises(MissingInputError):
        await comp.boot_up()
    assert comp.mcp_manager is not None
    assert comp.mcp_manager._active_clients == {}  # type: ignore[attr-defined]  # 回滚：无残留活跃 client

    # 补值（env 回退路径）→ 同一 Computer 重试成功（证明无残留状态阻塞重试）
    monkeypatch.setenv(var, "provided")
    await comp.boot_up()  # 不抛即成功
    assert len(comp.mcp_manager._active_clients) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_boot_up_tolerates_undefined_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """未定义占位符（不在 inputs 池）≠ 已定义但解析失败。boot 登记 raw 声明、占位符字面保留（VS Code parity）。"""
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", _StubManager)
    # 无 inputs 定义 → b173_undef 为未定义占位符
    comp = Computer(
        name="c",
        inputs=set(),
        mcp_servers={_stdio_cfg("s", "${input:b173_undef}")},
        auto_connect=False,
        auto_reconnect=False,
    )
    await comp.boot_up()  # 不抛即成功（raw 声明登记）
    received = comp.mcp_manager.received_servers  # type: ignore[attr-defined]
    assert len(received) == 1
    assert received[0].server_parameters.env.get("X") == "${input:b173_undef}"  # 占位符字面保留


@pytest.mark.asyncio
async def test_boot_up_tolerates_invalid_shape_keeps_original(monkeypatch: pytest.MonkeyPatch) -> None:
    """#192：boot 只做形状校验——形状非法**跳过挂载、不连坐上抛**（对齐旧「非 InputResolution 渲染错误容错」
    精神；非法声明无法派生 bundle_id 身份，解析类错误推迟到实际启动 surface）。"""
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", _StubManager)
    comp = Computer(name="test", inputs=[], mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    # 有 name（bundle_id 可派生）但缺必填 server_parameters → 形状非法
    bad = {"type": "stdio", "name": "keep"}
    comp._mcp_servers = [bad]  # type: ignore[attr-defined]  # list：boot 仅迭代（dict 不可入 set）

    # 形状校验失败 → 跳过挂载，boot 不抛
    await comp.boot_up()
    assert comp.mcp_manager is not None
    received = comp.mcp_manager.received_servers  # type: ignore[attr-defined]
    assert received == []  # 非法声明被跳过（不挂载、不连坐）


@pytest.mark.asyncio
async def test_amount_server_then_start_propagates_input_resolution_error(tmp_path: Path) -> None:
    """#192：transient mount 只登记 raw（不解析）——Missing 从实际 start surface（对齐 rust ``mount_server``
    只记录声明、start 时 surface）。"""
    inputs = [MCPServerPromptStringInput(id="b173_mount", description="d")]
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    comp = _comp({inputs[0]}, set(), resolver=resolver)
    cfg = _stdio_cfg("s", "${input:b173_mount}")
    await comp.amount_server(cfg)  # 不抛：mount 只登记 raw
    assert comp.mcp_manager is not None
    with pytest.raises(MissingInputError) as ei:
        await comp.mcp_manager.astart_client(resolve_bundle_id(cfg))
    assert ei.value.kind is InputKind.VALUE


@pytest.mark.asyncio
async def test_durable_add_persists_raw_and_start_surfaces_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#192：durable 声明不再被 input 缺失阻断——raw 落盘 + mount（对齐 rust：落盘 raw + mount raw），
    Missing 从实际 start surface。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)  # 隔离落盘面（project/local 锚 cwd、user 锚 XDG，#116/#134）
    inputs = [MCPServerPromptStringInput(id="b173_add", description="d")]
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    comp = _comp({inputs[0]}, set(), resolver=resolver)
    cfg = _stdio_cfg("s", "${input:b173_add}")
    await comp.aadd_or_aupdate_server_in_scope(cfg, McpWriteScope.LOCAL)  # 不抛：raw 声明落盘 + mount
    assert mcp_write_path(McpWriteScope.LOCAL, env=os.environ).exists()
    assert comp.mcp_manager is not None
    with pytest.raises(MissingInputError) as ei:
        await comp.mcp_manager.astart_client(resolve_bundle_id(cfg))
    assert ei.value.kind is InputKind.VALUE


@pytest.mark.asyncio
async def test_durable_add_invalid_shape_fails_before_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """形状非法仍**早失败不落盘**（绝不留下盘上声明而运行期未挂的半态）——早失败口径收窄为形状校验
    （#192：input 解析已推迟到实际启动，不再参与落盘前校验）。"""
    monkeypatch.chdir(tmp_path)
    comp = _comp(set(), set(), resolver=InputResolver([], env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)})))
    bad = {"type": "stdio", "name": "bad"}  # 缺必填 server_parameters → 形状非法
    with pytest.raises(ValidationError):
        await comp.aadd_or_aupdate_server_in_scope(bad, McpWriteScope.LOCAL)
    assert not (tmp_path / "mcp.local.json").exists()
