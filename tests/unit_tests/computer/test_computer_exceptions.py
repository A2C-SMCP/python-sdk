# -*- coding: utf-8 -*-
# filename: test_computer_exceptions.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters

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
from a2c_smcp.computer.settings.mcp_config import McpWriteScope


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


def _stdio_cfg(name: str, arg: str) -> StdioServerConfig:
    """单 server，env 中引用 ``${input:<id>}``（boot_up 读 self._mcp_servers 渲染）。"""
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command="/bin/echo", env={"X": arg}),
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


# ── #173（对齐 rust-sdk#144）：boot_up 须把 D1 结构化 InputResolution 上抛（非仅日志），对齐 mount_server ──


@pytest.mark.asyncio
async def test_boot_up_propagates_missing_value_input(tmp_path: Path) -> None:
    """已定义 value input、headless 无 env/store/default → boot_up 上抛 MissingInputError(VALUE)（非吞错保底）。"""
    inputs = [MCPServerPromptStringInput(id="b173_val", description="d")]  # default=None
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _stdio_cfg("s", "${input:b173_val}")
    comp = Computer(
        name="c",
        inputs=set(inputs),
        mcp_servers={cfg},
        auto_connect=False,
        auto_reconnect=False,
        input_resolver=resolver,
    )
    with pytest.raises(MissingInputError) as ei:
        await comp.boot_up()
    err = ei.value
    assert err.kind is InputKind.VALUE
    assert err.id == "b173_val"
    assert err.env_hint == "A2C_SMCP_b173_val"
    assert err.error_code == 400


@pytest.mark.asyncio
async def test_boot_up_propagates_missing_secret_input_no_plaintext(tmp_path: Path) -> None:
    """password:true secret 缺失 → Missing(SECRET)；Missing 无值字段 ⇒ 错误天然不含明文。"""
    inputs = [MCPServerPromptStringInput(id="b173_sec", description="d", password=True)]
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}), secret_store=_NoSecret())
    cfg = _stdio_cfg("s", "${input:b173_sec}")
    comp = Computer(
        name="c",
        inputs=set(inputs),
        mcp_servers={cfg},
        auto_connect=False,
        auto_reconnect=False,
        input_resolver=resolver,
    )
    with pytest.raises(MissingInputError) as ei:
        await comp.boot_up()
    err = ei.value
    assert err.kind is InputKind.SECRET
    assert err.id == "b173_sec"
    # 错误文案不得含 "password" 字样 / 不含任何疑似明文
    assert "password" not in str(err).lower()


@pytest.mark.asyncio
async def test_boot_up_propagates_resolver_hard_failure(tmp_path: Path) -> None:
    """client resolver 硬失败 → boot_up 上抛 ResolverFailedError（区别于 Missing 的未提供）。"""

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
    comp = Computer(
        name="c",
        inputs=set(inputs),
        mcp_servers={cfg},
        auto_connect=False,
        auto_reconnect=False,
        input_resolver=resolver,
    )
    with pytest.raises(ResolverFailedError) as ei:
        await comp.boot_up()
    assert ei.value.id == "b173_fail"
    assert "boom" in ei.value.reason
    assert isinstance(ei.value, InputResolutionError)


@pytest.mark.asyncio
async def test_boot_up_retry_succeeds_after_value_provided(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """boot 失败后生命周期可安全重试——补值（env）后同实例重试成功（无残留 manager/task/transport 阻塞）。
    注：python Computer 无 rust 的 lifecycle 状态机，故以「重试成功」证无残留（对齐 rust retry 测试意图）。"""
    var = "A2C_SMCP_b173_retry"
    monkeypatch.delenv(var, raising=False)  # 确保首轮缺失
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", _StubManager)

    inputs = [MCPServerPromptStringInput(id="b173_retry", description="d")]
    # env=os.environ（live）便于中途 setenv；value_store 走 tmp_path 隔离。
    resolver = InputResolver(inputs, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    cfg = _stdio_cfg("s", "${input:b173_retry}")
    comp = Computer(
        name="c",
        inputs=set(inputs),
        mcp_servers={cfg},
        auto_connect=False,
        auto_reconnect=False,
        input_resolver=resolver,
    )

    # 首轮：值缺失 → Missing → boot 失败
    with pytest.raises(MissingInputError):
        await comp.boot_up()

    # 补值（env 回退路径）→ 同一 Computer 重试成功（证明无残留状态阻塞重试）
    monkeypatch.setenv(var, "provided")
    await comp.boot_up()  # 不抛即成功


@pytest.mark.asyncio
async def test_boot_up_tolerates_undefined_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未定义占位符（不在 inputs 池）≠ 已定义但解析失败。前者保留字面、不上抛（VS Code parity），
    仅后者（InputResolution）上抛。本测试守护「不连坐误伤」。"""
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", _StubManager)
    # 无 inputs 定义 → b173_undef 为未定义占位符
    comp = Computer(
        name="c",
        inputs=set(),
        mcp_servers={_stdio_cfg("s", "${input:b173_undef}")},
        auto_connect=False,
        auto_reconnect=False,
    )
    await comp.boot_up()  # 不抛即成功（字面保留）


@pytest.mark.asyncio
async def test_boot_up_tolerates_non_input_render_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """#173 守卫：非 InputResolution 的渲染错误（如 renderer 自身抛 RuntimeError）仍按稳妥策略保留原配置
    继续（对齐 rust「其余渲染错误维持容错」），不连坐上抛——只有 InputResolution 才上抛。"""
    monkeypatch.setattr("a2c_smcp.computer.computer.MCPServerManager", _StubManager)
    comp = Computer(name="test", inputs=[], mcp_servers=set(), auto_connect=False, auto_reconnect=False)
    cfg = StdioServerConfig(name="keep", server_parameters=StdioServerParameters(command="/bin/echo"))
    comp._mcp_servers = {cfg}  # type: ignore[attr-defined]

    async def boom(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise RuntimeError("render failed")

    comp._config_render.arender = boom  # type: ignore[assignment,method-assign]

    # 非 InputResolution 错误 → 保留原配置继续，boot 不抛
    await comp.boot_up()
    assert comp.mcp_manager is not None
    # 稳妥策略核心承诺：ainitialize 收到的是**原始未渲染 cfg**（保留而非丢弃），对齐被取代的旧测试
    # `test_boot_up_render_error_path` 的 `isinstance(servers[0], MCPServerConfig)` 守卫强度。
    received = comp.mcp_manager.received_servers  # type: ignore[attr-defined]
    assert len(received) == 1
    assert received[0].name == "keep"


@pytest.mark.asyncio
async def test_amount_server_propagates_input_resolution_error(tmp_path: Path) -> None:
    """#173：transient mount 路径同样上抛 InputResolution（render 放行 ⇒ 免费对齐 rust ``mount_server``）。
    钉契约防回归——避免未来 render.py 改动无意重新吞回 InputResolution 时，此「免费对齐」静默蒸发。"""
    inputs = [MCPServerPromptStringInput(id="b173_mount", description="d")]
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    comp = Computer(
        name="c",
        inputs=set(inputs),
        mcp_servers=set(),
        auto_connect=False,
        auto_reconnect=False,
        input_resolver=resolver,
    )
    with pytest.raises(MissingInputError) as ei:
        await comp.amount_server(_stdio_cfg("s", "${input:b173_mount}"))
    assert ei.value.kind is InputKind.VALUE


@pytest.mark.asyncio
async def test_aadd_or_aupdate_server_propagates_input_resolution_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#173：durable 声明路径同样上抛 InputResolution（render 先于落盘 ⇒ 早失败不残留半态盘声明，对齐 rust）。
    钉契约防回归。"""
    monkeypatch.chdir(tmp_path)  # 隔离 mcp.local.json（render 先失败，本不应触盘，此为 hygiene）
    inputs = [MCPServerPromptStringInput(id="b173_add", description="d")]
    resolver = InputResolver(inputs, env={}, value_store=ValueStore({"XDG_STATE_HOME": str(tmp_path)}))
    comp = Computer(
        name="c",
        inputs=set(inputs),
        mcp_servers=set(),
        auto_connect=False,
        auto_reconnect=False,
        input_resolver=resolver,
    )
    with pytest.raises(MissingInputError) as ei:
        await comp.aadd_or_aupdate_server_in_scope(_stdio_cfg("s", "${input:b173_add}"), McpWriteScope.LOCAL)
    assert ei.value.kind is InputKind.VALUE
    # render 先失败 ⇒ 未落盘半态声明（D1 早失败承诺：绝不留下盘上声明而运行期未挂的半态）
    assert not (tmp_path / "mcp.local.json").exists()
