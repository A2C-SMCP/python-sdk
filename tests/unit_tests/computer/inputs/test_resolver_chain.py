# -*- coding: utf-8 -*-
# filename: test_resolver_chain.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
解析链单元测试（v0.2.1 #65，§9.3）/ Resolution-chain unit tests。

链序：cache → env(`A2C_SMCP_<ENV_SEGMENT(id)>`) → keyring(password) → 明文 value store(非密钥) → prompt → default。
持久化：仅**交互 prompt 得值**（有 TTY/session）按类落盘；env 命中/headless/command 不落盘。
#173（对齐 rust-sdk#144 D1）：headless 下已定义但 resolver/env/default 均无法提供的 input/secret →
结构化 ``MissingInputError``（value 无 default / secret 一律），**非仅日志**、绝不落明文；client 据此补录重试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import a2c_smcp.computer.inputs.resolver as resolver_mod
from a2c_smcp.computer.inputs.plugin_pool import prefix_input_id
from a2c_smcp.computer.inputs.resolver import (
    InputKind,
    InputNotFoundError,
    InputResolutionError,
    InputResolver,
    MissingInputError,
    ResolverFailedError,
)
from a2c_smcp.computer.inputs.value_store import ValueStore
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerCommandInput,
    MCPServerPickStringInput,
    MCPServerPromptStringInput,
    PickStringOption,
)
from a2c_smcp.utils.env_segment import EnvNameCollisionError, detect_env_name_collisions, env_var_name

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
    """#155 F4 硬切：前缀 A2C_SMCP_、id 段不再 upper()、'@' '/' '.' 均走 ENV_SEGMENT。"""
    assert env_var_name("figma_token") == "A2C_SMCP_figma_token"
    assert env_var_name("frontend@my-team/api.key") == "A2C_SMCP_frontend_my_team_api_key"


def test_resolver_rejects_env_name_collision() -> None:
    """#155 F4 坍缩 fail-fast：两个 id 映射到同一完整 env 名 → 注册期硬错误（此前是静默串味、后写的赢）。"""
    inputs = [
        MCPServerPromptStringInput(id="figma-token", description="d"),
        MCPServerPromptStringInput(id="figma_token", description="d"),
    ]
    with pytest.raises(EnvNameCollisionError) as ei:
        InputResolver(inputs)
    msg = str(ei.value)
    # 提示 MUST 自解释：撞上的完整 env 名 + 两个肇事 id 都要出现，否则用户无从下手
    assert "A2C_SMCP_figma_token" in msg
    assert "figma-token" in msg and "figma_token" in msg


def test_resolver_allows_ids_whose_full_names_differ() -> None:
    """#155 F4 检测面 = **完整 env 名**：完整名不同即 MUST NOT 报错——防 fail-fast 收得过紧。

    正对照：`plugin-a@mp/token` 与 `plugin_a@mp/secret` 的 `plugin-a`/`plugin_a` 部分同映射为
    `plugin_a`，但 token/secret 让**完整名**分叉 ⇒ 必须放行。
    """
    a = prefix_input_id("plugin-a", "mp", "token")
    b = prefix_input_id("plugin_a", "mp", "secret")
    # 判据直断：检测器对这两个 id 返回空冲突集（而非靠「构造不抛」这种装饰性断言）
    assert detect_env_name_collisions([a, b]) == {}
    assert env_var_name(a) != env_var_name(b)
    InputResolver([MCPServerPromptStringInput(id=a, description="d"), MCPServerPromptStringInput(id=b, description="d")])


@pytest.mark.asyncio
async def test_env_hit_not_persisted(tmp_path: Path) -> None:
    inputs = [MCPServerPromptStringInput(id="tok", description="d")]
    vs = _vstore(tmp_path)
    r = InputResolver(inputs, env={"A2C_SMCP_tok": "from-env"}, value_store=vs, secret_store=_Secret())
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
    inputs = [
        MCPServerPickStringInput(
            id="region",
            description="d",
            options=[PickStringOption(label="us", value="us"), PickStringOption(label="eu", value="eu")],
            default="us",
        )
    ]
    vs = _vstore(tmp_path)

    async def fake_pick(*_a, **_k):
        return "eu"

    monkeypatch.setattr(resolver_mod, "ainput_pick", fake_pick)
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret())
    assert await r.aresolve_by_id("region", session=_SESSION) == "eu"
    assert vs.get("region") == "eu"


@pytest.mark.asyncio
async def test_headless_password_raises_missing_secret(tmp_path: Path) -> None:
    """#173（对齐 rust-sdk#144）：password headless（无 env+无 keyring+无 TTY）→ 结构化 Missing(SECRET)，
    绝不落明文。Missing 无值字段 ⇒ 错误天然不含明文。"""
    inputs = [MCPServerPromptStringInput(id="sec", description="d", password=True)]
    vs = _vstore(tmp_path)
    # session=None + 无 env + keyring 不可用 → headless Missing(SECRET)（密钥不落明文）
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret(available=False))
    with pytest.raises(MissingInputError) as ei:
        await r.aresolve_by_id("sec", session=None)
    err = ei.value
    assert err.kind is InputKind.SECRET
    assert err.id == "sec"
    assert err.env_hint == "A2C_SMCP_sec"
    assert err.error_code == 400
    # 错误文案不得含任何疑似明文 / 不得用 "password" 字样（rust parity）
    assert "password" not in str(err).lower()
    assert vs.get("sec") is None  # 绝不落明文 / never written to plaintext


@pytest.mark.asyncio
async def test_headless_password_missing_when_keyring_available_but_unseeded(tmp_path: Path) -> None:
    """keyring 可用但该密钥未播种 + 无 TTY → 仍 Missing(SECRET)（#65 fix-review #1 中间态 + #173 结构化）。"""
    inputs = [MCPServerPromptStringInput(id="sec", description="d", password=True)]
    vs = _vstore(tmp_path)
    # available=True 但 seed 为空（步骤 3 keyring miss）+ session=None + 无 env → Missing(SECRET)，不走 prompt
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret(available=True))
    with pytest.raises(MissingInputError) as ei:
        await r.aresolve_by_id("sec", session=None)
    assert ei.value.kind is InputKind.SECRET
    assert vs.get("sec") is None  # 绝不落明文 / never written to plaintext


@pytest.mark.asyncio
async def test_headless_password_missing_even_with_default(tmp_path: Path) -> None:
    """#173 守卫：password:true 即便带 default、headless 仍 Missing(SECRET)——secret 绝不降级用 default / 落明文。"""
    inputs = [MCPServerPromptStringInput(id="sec", description="d", password=True, default="should-not-use")]
    vs = _vstore(tmp_path)
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret(available=False))
    with pytest.raises(MissingInputError) as ei:
        await r.aresolve_by_id("sec", session=None)
    assert ei.value.kind is InputKind.SECRET
    assert vs.get("sec") is None  # 绝不落明文 / never written to plaintext


@pytest.mark.asyncio
async def test_headless_value_no_default_raises_missing(tmp_path: Path) -> None:
    """#173（对齐 rust-sdk#144）：已定义 value input、headless、无 env/store/default → Missing(VALUE)，
    而非静默回退空串。"""
    inputs = [MCPServerPromptStringInput(id="val", description="d")]  # default=None, password=None
    vs = _vstore(tmp_path)
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret(available=False))
    with pytest.raises(MissingInputError) as ei:
        await r.aresolve_by_id("val", session=None)
    err = ei.value
    assert err.kind is InputKind.VALUE
    assert err.id == "val"
    assert err.env_hint == "A2C_SMCP_val"
    assert err.error_code == 400


@pytest.mark.asyncio
async def test_headless_value_with_default_uses_default(tmp_path: Path) -> None:
    """#173 守卫：value input headless 但**有 default** → 用 default，不报 Missing（rust：default 存在 ⇒ 不 Missing）。"""
    inputs = [MCPServerPromptStringInput(id="val", description="d", default="fallback")]
    vs = _vstore(tmp_path)
    r = InputResolver(inputs, env={}, value_store=vs, secret_store=_Secret(available=False))
    # headless + default → 返回 default，不抛
    assert await r.aresolve_by_id("val", session=None) == "fallback"


@pytest.mark.asyncio
async def test_env_fallback_avoids_missing(tmp_path: Path) -> None:
    """#173 守卫：env 命中（headless 安全）→ 解析成功，不报 Missing（补录闭环的 env 注入路径）。"""
    inputs = [MCPServerPromptStringInput(id="val", description="d")]  # 无 default
    vs = _vstore(tmp_path)
    r = InputResolver(inputs, env={"A2C_SMCP_val": "from-env"}, value_store=vs, secret_store=_Secret(available=False))
    assert await r.aresolve_by_id("val", session=None) == "from-env"


@pytest.mark.asyncio
async def test_resolver_failed_error_carries_reason(tmp_path: Path) -> None:
    """#173（对齐 rust-sdk#144）：ResolverFailed 结构化错误字段（id/reason/error_code=400）。"""

    class _Failing(InputResolver):
        async def aresolve_by_id(
            self, input_id: str, *, session: Any = None, plugin: str | None = None, marketplace: str | None = None
        ) -> Any:
            raise ResolverFailedError(id=input_id, reason="boom")

    inputs = [MCPServerPromptStringInput(id="fail", description="d")]
    r = _Failing(inputs, env={}, value_store=_vstore(tmp_path), secret_store=_Secret(available=False))
    with pytest.raises(ResolverFailedError) as ei:
        await r.aresolve_by_id("fail", session=_SESSION)
    err = ei.value
    assert err.id == "fail"
    assert "boom" in err.reason
    assert err.error_code == 400
    # ResolverFailed 也是 InputResolutionError（base 类捕获）
    assert isinstance(err, InputResolutionError)


def test_missing_error_constructor_fields() -> None:
    """#173：MissingInputError 构造子自动派生 env_hint（A2C_SMCP_<ENV_SEGMENT(id)>），字段齐全。"""
    err = MissingInputError(id="figma_token", kind=InputKind.SECRET)
    assert err.id == "figma_token"
    assert err.kind is InputKind.SECRET
    assert err.env_hint == "A2C_SMCP_figma_token"
    assert err.error_code == 400
    # Display 面向 client 补录：含 id + 种类 + env 补录名，不含 "password" 字样
    msg = str(err)
    assert "figma_token" in msg
    assert "secret" in msg
    assert "A2C_SMCP_figma_token" in msg
    assert "password" not in msg.lower()


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


# ---- §5.11 plugin input 解析序 / scoped-first resolution order ------------------


@pytest.mark.asyncio
async def test_plugin_scoped_wins_when_both_defined(tmp_path: Path) -> None:
    """§5.11 ①：scoped 与 global 同存同 kind → scoped 胜（scoped-first，旧行为 global 胜）。"""
    scoped = prefix_input_id("frontend", "my-team", "api_token")
    inputs = [
        MCPServerPromptStringInput(id=scoped, description="scoped"),
        MCPServerPromptStringInput(id="api_token", description="global"),
    ]
    vs = _vstore(tmp_path)
    r = InputResolver(
        inputs,
        env={env_var_name(scoped): "scoped-v", env_var_name("api_token"): "global-v"},
        value_store=vs,
        secret_store=_Secret(),
    )
    assert await r.aresolve_by_id("api_token", plugin="frontend", marketplace="my-team", session=_SESSION) == "scoped-v"


@pytest.mark.asyncio
async def test_plugin_prefix_fallback(tmp_path: Path) -> None:
    """§5.11 ②：裸 id 在 plugin 上下文先查 scoped，scoped 命中 → 直接取 scoped 池条目（D2）。"""
    pid = prefix_input_id("frontend", "my-team", "api_token")
    inputs = [MCPServerPromptStringInput(id=pid, description="d")]
    vs = _vstore(tmp_path)
    # env 用前缀归一名命中 → 验证 resolved_id 用的是带前缀 id
    r = InputResolver(inputs, env={env_var_name(pid): "v"}, value_store=vs, secret_store=_Secret())
    assert await r.aresolve_by_id("api_token", plugin="frontend", marketplace="my-team", session=_SESSION) == "v"


@pytest.mark.asyncio
async def test_scoped_missing_global_exists_fallback(tmp_path: Path) -> None:
    """§5.11 ②：scoped 不存在、global 存在（同 kind）→ 回退 global。"""
    inputs = [MCPServerPromptStringInput(id="api_token", description="global")]
    vs = _vstore(tmp_path)
    r = InputResolver(
        inputs,
        env={env_var_name("api_token"): "global-v"},
        value_store=vs,
        secret_store=_Secret(),
    )
    assert await r.aresolve_by_id("api_token", plugin="frontend", marketplace="my-team", session=_SESSION) == "global-v"


@pytest.mark.asyncio
async def test_plugin_bound_both_miss_error_carries_scoped_id() -> None:
    """§5.11 ③：plugin 上下文下裸引用皆不可命中 → InputNotFoundError.id 为完整 scoped id。"""
    r = InputResolver([], env={}, value_store=_vstore(Path("/tmp")), secret_store=_Secret())
    with pytest.raises(InputNotFoundError) as ei:
        await r.aresolve_by_id("api_token", plugin="frontend", marketplace="my-team", session=_SESSION)
    assert ei.value.args[0] == "frontend@my-team/api_token"  # scoped id，非裸 "api_token"


@pytest.mark.asyncio
async def test_explicit_scoped_reference_no_global_fallback(tmp_path: Path) -> None:
    """§5.11 补充：显式完整引用 ${input:<P>@<M>/<id>} 直接命中 scoped、不回退 global。"""
    scoped = prefix_input_id("frontend", "my-team", "api_token")
    inputs = [
        MCPServerPromptStringInput(id=scoped, description="scoped"),
        MCPServerPromptStringInput(id="api_token", description="global"),
    ]
    vs = _vstore(tmp_path)
    r = InputResolver(
        inputs,
        env={env_var_name(scoped): "scoped-v", env_var_name("api_token"): "global-v"},
        value_store=vs,
        secret_store=_Secret(),
    )
    # 显式 scoped 引用：id 含 @ → 直接查 scoped 池条目，不回退 global
    assert await r.aresolve_by_id(scoped, plugin="frontend", marketplace="my-team", session=_SESSION) == "scoped-v"


@pytest.mark.asyncio
async def test_unbound_server_bare_ref_global_only(tmp_path: Path) -> None:
    """§5.11 补充：未绑定 plugin 的 server 裸引用仅解析 global（行为不变）。"""
    inputs = [MCPServerPromptStringInput(id="api_token", description="global")]
    vs = _vstore(tmp_path)
    r = InputResolver(
        inputs,
        env={env_var_name("api_token"): "global-v"},
        value_store=vs,
        secret_store=_Secret(),
    )
    # 无 plugin/marketplace → 仅 global 查
    assert await r.aresolve_by_id("api_token", session=_SESSION) == "global-v"


@pytest.mark.asyncio
async def test_scoped_defined_but_unresolvable_no_global_fallback(tmp_path: Path) -> None:
    """§5.11 跨 kind 守卫：scoped 已定位但取值失败 → MissingInputError(scoped_id)，不回退 global。

    scoped-first 结构上保证：scoped 命中池后绝不因取值失败而尝试 global（跨 kind 约束自然满足）。"""
    scoped = prefix_input_id("frontend", "my-team", "secret")
    inputs = [
        MCPServerPromptStringInput(id=scoped, description="scoped-secret", password=True),
        MCPServerPromptStringInput(id="secret", description="global-value"),  # 不同 kind（无 password）
    ]
    vs = _vstore(tmp_path)
    r = InputResolver(
        inputs,
        env={},  # 无 env → scoped secret 无法解析
        value_store=vs,
        secret_store=_Secret(available=False),  # keyring 不可用
    )
    # headless：scoped secret 无法解析 → MissingInputError(SECRET, scoped_id)，绝不回退 global
    with pytest.raises(MissingInputError) as ei:
        await r.aresolve_by_id("secret", plugin="frontend", marketplace="my-team", session=None)
    err = ei.value
    assert err.kind is InputKind.SECRET
    assert err.id == scoped  # scoped id，非裸 "secret"
    assert vs.get("secret") is None  # 绝不落明文


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
