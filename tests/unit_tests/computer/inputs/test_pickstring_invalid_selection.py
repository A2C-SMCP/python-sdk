# -*- coding: utf-8 -*-
# filename: test_pickstring_invalid_selection.py
"""
Issue #192（runtime-contract §5.12）PickString 取值与失效语义向量（conformance §5 七景 ①②③ +
headless 首项回退 + command 不缓存）。
"""
from __future__ import annotations

from typing import Any

import pytest
from prompt_toolkit import PromptSession

from a2c_smcp.computer.inputs.resolver import (
    InputResolutionError,
    InputResolver,
    InvalidSelectionError,
)
from a2c_smcp.computer.inputs.value_store import ValueStore
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerCommandInput,
    MCPServerPickStringInput,
    PickStringOption,
)


class _NoopStore:
    """get 恒 miss、记录 set 的 value store 替身（隔离真实 state 文件 + 断言不反向持久化）。"""

    def __init__(self) -> None:
        self.sets: list[tuple[str, str]] = []

    def get(self, input_id: str) -> str | None:  # noqa: ARG002
        return None

    def set(self, input_id: str, value: str) -> bool:
        self.sets.append((input_id, value))
        return True


def _pick(default: str | None = None, options: tuple[tuple[str, str], ...] = (("中国", "cn"), ("欧洲", "eu"))) -> MCPServerPickStringInput:
    return MCPServerPickStringInput(
        id="region",
        description="pick one",
        options=[PickStringOption(label=label, value=value) for label, value in options],
        default=default,
    )


@pytest.mark.asyncio
async def test_stored_valid_value_accepted() -> None:
    """① 已存值匹配任一 option.value → 渲染成功，注入值为 value（而非 label）。"""
    r = InputResolver([_pick()], env={}, value_store=_NoopStore())
    r.set_cached_value("region", "cn")
    assert await r.aresolve_by_id("region") == "cn"


@pytest.mark.asyncio
async def test_stored_invalid_value_raises_invalid_selection_no_fallback() -> None:
    """② 已存值不匹配任一 option.value → 结构化 InvalidSelection（带 id），MUST NOT 回退 default / 首项。"""
    r = InputResolver([_pick(default="cn")], env={}, value_store=_NoopStore())
    r.set_cached_value("region", "mars")
    with pytest.raises(InvalidSelectionError) as ei:
        await r.aresolve_by_id("region")
    err = ei.value
    assert err.id == "region"
    assert err.value == "mars"
    assert isinstance(err, InputResolutionError)
    assert err.error_code == 400


@pytest.mark.asyncio
async def test_env_invalid_value_raises_invalid_selection() -> None:
    """env 候选同样逐一校验——陈旧环境值不得静默注入。"""
    r = InputResolver([_pick(default="cn")], env={"A2C_SMCP_region": "mars"}, value_store=_NoopStore())
    with pytest.raises(InvalidSelectionError):
        await r.aresolve_by_id("region")


@pytest.mark.asyncio
async def test_value_store_invalid_value_raises_invalid_selection() -> None:
    """value store 候选同样逐一校验。"""

    class _MemStore:
        def __init__(self, data: dict[str, str]) -> None:
            self._data = data

        def get(self, input_id: str) -> str | None:
            return self._data.get(input_id)

        def set(self, input_id: str, value: str) -> bool:  # noqa: ARG002
            return True

    r = InputResolver([_pick(default="cn")], env={}, value_store=_MemStore({"region": "mars"}))  # type: ignore[arg-type]
    with pytest.raises(InvalidSelectionError):
        await r.aresolve_by_id("region")


@pytest.mark.asyncio
async def test_no_user_value_uses_default() -> None:
    """③ 无用户值 → 先尝试 default（headless 直接回退 default，不触发 prompt 机制）。"""
    r = InputResolver([_pick(default="eu")], env={}, value_store=_NoopStore())
    assert await r.aresolve_by_id("region") == "eu"


@pytest.mark.asyncio
async def test_no_user_value_no_default_uses_first_option_value_not_persisted() -> None:
    """③ 无用户值且无 default → 首项 value（MAY 首项，对齐 rust SilentSession），且**不反向持久化**
    （不写 value store、不写解析缓存——下次解析仍从解析链开始）。"""
    store = _NoopStore()
    r = InputResolver([_pick()], env={}, value_store=store)
    v1 = await r.aresolve_by_id("region")
    assert v1 == "cn"  # 首项 value
    assert store.sets == []  # 未落盘
    assert r.get_cached_value("region") is None  # 未进缓存
    # 下次解析仍从解析链开始：注入值后按用户值解析
    r.set_cached_value("region", "eu")
    assert await r.aresolve_by_id("region") == "eu"


@pytest.mark.asyncio
async def test_headless_pickstring_no_default_first_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """headless（无 TTY、无 session）pickString 无 default → 首项 value（不再 Missing(VALUE)，
    对齐 rust SilentSession；promptString 的 Missing 语义不受影响）。"""
    r = InputResolver([_pick()], env={}, value_store=_NoopStore())
    # 守卫：ainput_pick 不得被调用（headless 直接回退，不经 prompt 机制）
    called: dict[str, bool] = {}

    async def boom(*args: Any, **kwargs: Any) -> Any:
        called["hit"] = True
        raise AssertionError("headless pickString 不应触发交互")

    import a2c_smcp.computer.inputs.resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "ainput_pick", boom)
    assert await r.aresolve_by_id("region") == "cn"
    assert not called


@pytest.mark.asyncio
async def test_interactive_pick_injects_value_not_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """① 交互选择：表格按 label 展示，注入 value（重复 label 场景按 index 返回条目本身）。"""
    r = InputResolver([_pick(options=(("a", "x"), ("a", "y")))], env={}, value_store=_NoopStore())

    async def fake_pick(
        message: str,
        opts: Any,
        *,
        default_index: int | None = None,
        multi: bool = False,
        session: PromptSession | None = None,
    ) -> Any:
        assert all(o.label == "a" for o in opts)  # 展示 label
        return opts[1]  # 选第二项（重复 label 下必须按条目返回，不可按 label 反查）

    import a2c_smcp.computer.inputs.resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "ainput_pick", fake_pick)
    monkeypatch.setattr(resolver_mod.InputResolver, "_has_tty", staticmethod(lambda session: True))
    assert await r.aresolve_by_id("region") == "y"  # 注入 value


def _patch_real_cli_io_eof(monkeypatch: pytest.MonkeyPatch, *, default_index: int | None) -> None:
    """用**真实 ainput_pick 行为镜像**打桩：PromptSession 抛 KeyboardInterrupt（= EOF）→ 真实 ainput_pick
    返回 ""（#192 起纯选择原语，default 回退语义在 resolver 层）。夹具与真实 cli_io 同构，避免「fake 返回 ""
    但真实 EOF-with-default 返回条目」的致盲（审查 🔴2 复核实证缺口）。"""

    class _EofSession:
        async def prompt_async(self, *args: Any, **kwargs: Any) -> str:
            raise KeyboardInterrupt()

    import a2c_smcp.computer.inputs.cli_io as cli_io_mod
    import a2c_smcp.computer.inputs.resolver as resolver_mod

    monkeypatch.setattr(cli_io_mod, "PromptSession", lambda: _EofSession())
    monkeypatch.setattr(cli_io_mod.console_util.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(resolver_mod.InputResolver, "_has_tty", staticmethod(lambda session: True))


@pytest.mark.asyncio
async def test_interactive_pick_empty_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """③ 交互 EOF/空输入 → default（其 index 按 value 匹配，非按 label），且**不反向持久化**
    （回退值不落盘、不写缓存——下次解析仍从解析链开始，审查 🔴2 回归守卫）。"""
    store = _NoopStore()
    r = InputResolver([_pick(default="eu")], env={}, value_store=store)
    _patch_real_cli_io_eof(monkeypatch, default_index=1)

    assert await r.aresolve_by_id("region") == "eu"
    assert store.sets == []  # 回退值绝不落盘
    assert r.get_cached_value("region") is None  # 回退值绝不写缓存


@pytest.mark.asyncio
async def test_interactive_pick_empty_no_default_falls_back_to_first_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """③ 交互 EOF/空输入且无 default → 首项 value（§5.12 MAY 首项），且**不反向持久化**（🔴2 回归守卫）。"""
    store = _NoopStore()
    r = InputResolver([_pick()], env={}, value_store=store)
    _patch_real_cli_io_eof(monkeypatch, default_index=None)

    assert await r.aresolve_by_id("region") == "cn"  # 首项 value
    assert store.sets == []  # 回退值绝不落盘
    assert r.get_cached_value("region") is None  # 回退值绝不写缓存


@pytest.mark.asyncio
async def test_command_not_cached_reexecutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Command 仅在实际启动（解析）时执行——不进缓存，每次解析重新执行（对齐 rust：Command 直通 session）。"""
    calls: dict[str, int] = {"n": 0}
    r = InputResolver([MCPServerCommandInput(id="cmd", description="d", command="echo hi")], env={})

    async def fake_run(command: str, *, shell: bool = True, parse: str = "raw") -> str:
        calls["n"] += 1
        return f"out{calls['n']}"

    import a2c_smcp.computer.inputs.resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "arun_command", fake_run)
    assert await r.aresolve_by_id("cmd") == "out1"
    assert await r.aresolve_by_id("cmd") == "out2"
    assert calls["n"] == 2
