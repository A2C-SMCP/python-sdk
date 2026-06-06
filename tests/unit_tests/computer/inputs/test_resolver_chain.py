# -*- coding: utf-8 -*-
# filename: test_resolver_chain.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
解析链单元测试（v0.2.1 #65，§9.3）/ Resolution-chain unit tests。

链序：cache → env(`A2C_INPUT_<ID>`) → keyring(password) → 明文 value store(非密钥) → prompt → default。
持久化：仅**交互 prompt 得值**（有 TTY/session）按类落盘；env 命中/headless/command 不落盘。
密钥 headless（无 env+无 keyring+无 TTY）→ ``SecretResolutionError``，绝不落明文。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import a2c_smcp.computer.inputs.resolver as resolver_mod
from a2c_smcp.computer.inputs.plugin_pool import prefix_input_id
from a2c_smcp.computer.inputs.resolver import InputResolver, SecretResolutionError, env_var_name
from a2c_smcp.computer.inputs.value_store import ValueStore
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerCommandInput,
    MCPServerPickStringInput,
    MCPServerPromptStringInput,
)

_SESSION: Any = object()  # 非 None → _has_tty 视为可交互 / non-None → treated as interactive (TTY)


class _Secret:
    def __init__(self, *, available: bool = True, seed: dict[str, str] | None = None) -> None:
        self.available = available
        self._store: dict[str, str] = dict(seed or {})

    def get(self, k: str) -> str | None:
        return self._store.get(k) if self.available else None

    def set(self, k: str, v: str) -> bool:
        if not self.available:
            return False
        self._store[k] = v
        return True


def _vstore(tmp_path: Path) -> ValueStore:
    return ValueStore({"XDG_STATE_HOME": str(tmp_path)})


def test_env_var_name_normalization() -> None:
    assert env_var_name("figma_token") == "A2C_INPUT_FIGMA_TOKEN"
    assert env_var_name("frontend@my-team/api.key") == "A2C_INPUT_FRONTEND_MY_TEAM_API_KEY"


@pytest.mark.asyncio
async def test_env_hit_not_persisted(tmp_path: Path) -> None:
    inputs = [MCPServerPromptStringInput(id="tok", description="d")]
    vs = _vstore(tmp_path)
    r = InputResolver(inputs, env={"A2C_INPUT_TOK": "from-env"}, value_store=vs, secret_store=_Secret())
    assert await r.aresolve_by_id("tok", session=_SESSION) == "from-env"
    # env 命中不落盘（编排层拥有）/ env hit is never persisted
    assert vs.get("tok") is None


@pytest.mark.asyncio
async def test_keyring_hit_for_password(tmp_path: Path) -> None:
    inputs = [MCPServerPromptStringInput(id="sec", description="d", password=True)]
    sec = _Secret(seed={"sec": "from-keyring"})
    r = InputResolver(inputs, env={}, value_store=_vstore(tmp_path), secret_store=sec)
    assert await r.aresolve_by_id("sec", session=_SESSION) == "from-keyring"


@pytest.mark.asyncio
async def test_value_store_hit_for_non_password(tmp_path: Path) -> None:
    inputs = [MCPServerPromptStringInput(id="name", description="d")]
    vs = _vstore(tmp_path)
    vs.set("name", "stored-val")
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret())
    assert await r.aresolve_by_id("name", session=_SESSION) == "stored-val"


@pytest.mark.asyncio
async def test_prompt_persists_password_to_keyring(tmp_path: Path, monkeypatch) -> None:
    inputs = [MCPServerPromptStringInput(id="sec", description="d", password=True)]
    sec = _Secret(available=True)
    vs = _vstore(tmp_path)

    async def fake_prompt(*_a, **_k):
        return "typed-secret"

    monkeypatch.setattr(resolver_mod, "ainput_prompt", fake_prompt)
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=sec)
    assert await r.aresolve_by_id("sec", session=_SESSION) == "typed-secret"
    # 密钥进 keyring，绝不进明文 value store / secret → keyring, never plaintext
    assert sec.get("sec") == "typed-secret"
    assert vs.get("sec") is None


@pytest.mark.asyncio
async def test_prompt_persists_non_password_to_value_store(tmp_path: Path, monkeypatch) -> None:
    inputs = [MCPServerPickStringInput(id="region", description="d", options=["us", "eu"], default="us")]
    vs = _vstore(tmp_path)

    async def fake_pick(*_a, **_k):
        return "eu"

    monkeypatch.setattr(resolver_mod, "ainput_pick", fake_pick)
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret())
    assert await r.aresolve_by_id("region", session=_SESSION) == "eu"
    assert vs.get("region") == "eu"


@pytest.mark.asyncio
async def test_headless_password_hard_errors_without_persist(tmp_path: Path) -> None:
    inputs = [MCPServerPromptStringInput(id="sec", description="d", password=True)]
    vs = _vstore(tmp_path)
    # session=None + 无 env + keyring 不可用 → headless 硬错误（密钥不落明文）
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret(available=False))
    with pytest.raises(SecretResolutionError):
        await r.aresolve_by_id("sec", session=None)
    assert vs.get("sec") is None  # 绝不落明文 / never written to plaintext


@pytest.mark.asyncio
async def test_headless_password_hard_errors_when_keyring_available_but_unseeded(tmp_path: Path) -> None:
    """keyring 可用但该密钥未播种 + 无 TTY → 仍硬错误（#65 fix-review #1，中间态）/ available-but-unseeded headless hard-errors。"""
    inputs = [MCPServerPromptStringInput(id="sec", description="d", password=True)]
    vs = _vstore(tmp_path)
    # available=True 但 seed 为空（步骤 3 keyring miss）+ session=None + 无 env → 应硬错误，不走 prompt
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret(available=True))
    with pytest.raises(SecretResolutionError):
        await r.aresolve_by_id("sec", session=None)
    assert vs.get("sec") is None  # 绝不落明文 / never written to plaintext


@pytest.mark.asyncio
async def test_command_not_persisted(tmp_path: Path, monkeypatch) -> None:
    inputs = [MCPServerCommandInput(id="cmd", description="d", command="echo hi")]
    vs = _vstore(tmp_path)

    async def fake_run(*_a, **_k):
        return "hi"

    monkeypatch.setattr(resolver_mod, "arun_command", fake_run)
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret())
    assert await r.aresolve_by_id("cmd", session=_SESSION) == "hi"
    assert vs.get("cmd") is None  # command 真相是命令，不持久化值


@pytest.mark.asyncio
async def test_plugin_prefix_fallback(tmp_path: Path) -> None:
    """裸 id 在 plugin 上下文回退到带前缀池条目（D2）/ bare id resolves to prefixed pool entry in plugin context。"""
    pid = prefix_input_id("frontend", "my-team", "api_token")
    inputs = [MCPServerPromptStringInput(id=pid, description="d")]
    vs = _vstore(tmp_path)
    # env 用前缀归一名命中 → 验证 resolved_id 用的是带前缀 id
    r = InputResolver(inputs, env={env_var_name(pid): "v"}, value_store=vs, secret_store=_Secret())
    assert await r.aresolve_by_id("api_token", plugin="frontend", marketplace="my-team", session=_SESSION) == "v"


@pytest.mark.asyncio
async def test_two_plugins_same_bare_id_no_collision(tmp_path: Path) -> None:
    a = prefix_input_id("plugin-a", "mp", "token")
    b = prefix_input_id("plugin-b", "mp", "token")
    inputs = [
        MCPServerPromptStringInput(id=a, description="d"),
        MCPServerPromptStringInput(id=b, description="d"),
    ]
    r = InputResolver(
        inputs,
        env={env_var_name(a): "A", env_var_name(b): "B"},
        value_store=_vstore(tmp_path),
        secret_store=_Secret(),
    )
    assert await r.aresolve_by_id("token", plugin="plugin-a", marketplace="mp", session=_SESSION) == "A"
    assert await r.aresolve_by_id("token", plugin="plugin-b", marketplace="mp", session=_SESSION) == "B"
