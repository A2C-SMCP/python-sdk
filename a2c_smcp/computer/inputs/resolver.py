"""
文件名: resolver.py
作者: JQQ
创建日期: 2025/9/18
最后修改日期: 2026/5/27
版权: 2023 JQQ. All rights reserved.
依赖: prompt_toolkit, rich
描述:
  中文: inputs 解析器定义与实现。按需根据 id 解析三类输入：promptString、pickString、command。
        v0.2.1 #65（§9.3，对标 VS Code SecretStorage）：在交互 prompt 前前插解析链
        env(`A2C_SMCP_<ENV_SEGMENT(id)>`) → keyring（仅 password）→ 明文 value store（仅非密钥），并在
        **交互 prompt 得值后**按类持久化（password→keyring、非密钥→明文 state；env 命中/command/headless
        不落盘）。#173（对齐 rust-sdk#144 D1）：headless 下已定义但 resolver/env/default 均无法提供的
        input/secret → 结构化 ``MissingInputError``（value 无 default / secret 一律），**非仅日志**、绝不落明文。
        env 名派生的单一权威在 `a2c_smcp/utils/env_segment.py`（#155 / PROTO-5，0.3.0 起 `A2C_INPUT_`
        前缀硬切废止）。
  English: Input resolvers. v0.2.1 #65 prepends an env→keyring→plaintext resolution chain and persists
           interactively-prompted values by type (secrets to OS keyring, non-secrets to plaintext state);
           headless secrets hard-error rather than ever writing plaintext.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from prompt_toolkit import PromptSession

from a2c_smcp.computer.inputs.base import BaseInputResolver
from a2c_smcp.computer.inputs.cli_io import ainput_pick, ainput_prompt, arun_command
from a2c_smcp.computer.inputs.plugin_pool import prefix_input_id
from a2c_smcp.computer.inputs.secret_store import SecretStore
from a2c_smcp.computer.inputs.value_store import ValueStore
from a2c_smcp.computer.mcp_clients.model import (
    MCPServerCommandInput,
    MCPServerInput,
    MCPServerPickStringInput,
    MCPServerPromptStringInput,
)
from a2c_smcp.utils.env_segment import env_var_name
from a2c_smcp.utils.logger import get_logger

logger = get_logger("computer")


class InputNotFoundError(KeyError):
    pass


class InputKind(Enum):
    """
    中文: input 种类（供 client 区分补录 UI / 结构化错误分流，对齐 rust-sdk#144 ``InputKind``）。
    English: Input kind for client re-prompt UI triage & structured-error routing (aligns rust-sdk#144).
    """

    VALUE = "value"  # 非密钥（promptString / pickString / command）
    SECRET = "secret"  # 密钥（password:true）

    def __str__(self) -> str:
        return self.value


class InputResolutionError(Exception):
    """
    中文: D1 结构化 input/secret 解析错误（对齐 rust-sdk#144 ``InputResolutionError``，``error_code=400``）。
          ``boot_up`` 对其上抛（非仅日志），由 client 据此驱动补录 UI 并在保存后重试。子类：
          :class:`MissingInputError`（已定义但无 resolver/env/default）、:class:`ResolverFailedError`（client resolver 硬失败）。
    English: D1 structured input/secret resolution error (aligns rust-sdk#144, ``error_code=400``).
          ``boot_up`` propagates it (not just logged); clients drive a re-prompt UI and retry after saving.
    """

    # 诊断分类码（对齐 rust ``ComputerError::InputResolution → error_code()=400``）。**非 wire 字段**——python 尚无
    # ``ComputerError → ErrorPayload`` 映射，目前仅 boot_up 日志 + 结构化诊断消费；与 ``auth_error.ErrorCode``
    # （wire meta key，4006/4007）同名不同义。待 python ComputerError analog 落地时一并接线 surface。
    error_code: int = 400  # BAD_REQUEST（client 须补录 input/secret）

    def __init__(self, *, id: str, message: str) -> None:
        self.id = id
        super().__init__(message)


class MissingInputError(InputResolutionError):
    """
    中文: 已定义但 resolver/env/default 均无法提供的 input/secret → 结构化 Missing（**非仅日志**、非空串）。
          无值字段 ⇒ 错误天然不含明文（对齐 rust ``Missing`` 结构体）。env_hint 自动派生自 ``env_var_name(id)``。
    English: A defined input/secret that no resolver/env/default could provide → structured Missing
             (not just logged, not an empty string). Carries no value field ⇒ never leaks plaintext.
    """

    def __init__(self, *, id: str, kind: InputKind) -> None:
        env_hint = env_var_name(id)
        self.kind = kind
        self.env_hint = env_hint
        super().__init__(
            id=id,
            message=(f"required {kind.value} input '{id}' is unresolved; supply it via a runtime resolver or set env {env_hint}"),
        )


class ResolverFailedError(InputResolutionError):
    """
    中文: client resolver 侧硬失败（区别于「未提供」的 :class:`MissingInputError`）。
    English: Client-resolver hard failure (distinct from the "not provided" :class:`MissingInputError`).
    """

    def __init__(self, *, id: str, reason: str) -> None:
        self.reason = reason
        super().__init__(id=id, message=f"resolver failed for input '{id}': {reason}")


class InputResolver(BaseInputResolver[PromptSession]):
    """
    中文: 输入解析器，支持基于 id 的惰性解析、解析链取值与按类持久化。
    English: Input resolver with lazy per-id resolution, a resolution chain, and per-type persistence.
    """

    def __init__(
        self,
        inputs: Iterable[MCPServerInput],
        session: PromptSession | None = None,
        *,
        env: Mapping[str, str] | None = None,
        value_store: ValueStore | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        """
        中文: CLI 特化的输入解析器，支持可选的 PromptSession 注入 + 解析链存储注入（便于测试）。
        English: CLI-specialized resolver with optional PromptSession + resolution-chain store injection (testable).
        """
        super().__init__(inputs, session=session)
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._value_store: ValueStore = value_store if value_store is not None else ValueStore(env=self._env)
        self._secret_store: SecretStore = secret_store if secret_store is not None else SecretStore()

    async def aresolve_by_id(
        self,
        input_id: str,
        *,
        session: PromptSession | None = None,
        plugin: str | None = None,
        marketplace: str | None = None,
    ) -> Any:
        # 1. 定位定义（§5.11 plugin input 解析序，v0.3.1）：
        #    显式完整 scoped 引用（id 含 @）= 精确命中、不回退全局；
        #    绑定 plugin 的 server 裸引用 = scoped 先 → 全局回退（仅同 kind 分支，scoped 已定位则绝不再试全局）；
        #    未绑定 plugin 的 server（用户自定义）= 仅全局（行为不变）。
        #    §5.11 def-location: explicit scoped (@)=direct; plugin-bound bare=scoped-first→global fallback; unbound=global only.
        # NOTE: Heuristic assumes bare input IDs never contain "@" (true in A2C ecosystem).
        # IDs like "api_token" are never explicit scoped refs; users write
        # ${input:<P>@<M>/<id>} for cross-plugin refs, which always contain "@".
        is_explicit_scoped = "@" in input_id
        scoped_id: str | None = None
        if plugin and marketplace and not is_explicit_scoped:
            scoped_id = prefix_input_id(plugin, marketplace, input_id)

        cfg = None
        resolved_id = input_id

        if is_explicit_scoped:
            cfg = self._inputs.get(input_id)
            if cfg is not None:
                resolved_id = input_id
        elif scoped_id is not None:
            # Scoped first（§5.11 规则 ①）
            scoped_cfg = self._inputs.get(scoped_id)
            if scoped_cfg is not None:
                cfg, resolved_id = scoped_cfg, scoped_id
            else:
                # Scoped 未定义 → 全局回退（§5.11 规则 ②）
                global_cfg = self._inputs.get(input_id)
                if global_cfg is not None:
                    cfg, resolved_id = global_cfg, input_id
        else:
            cfg = self._inputs.get(input_id)
            if cfg is not None:
                resolved_id = input_id

        if cfg is None:
            # §5.11 规则 ③：皆不可命中 → 错误 id = scoped id（plugin 上下文），裸 id 否则
            raise InputNotFoundError(scoped_id if scoped_id is not None else input_id)

        # 2. 进程内 cache（按解析后的池 id 缓存，避免不同 plugin 的同裸 id 串味）
        if resolved_id in self._cache:
            return self._cache[resolved_id]

        sess = session or self.session
        is_password = isinstance(cfg, MCPServerPromptStringInput) and bool(cfg.password)
        is_plain_persistable = isinstance(cfg, (MCPServerPromptStringInput, MCPServerPickStringInput)) and not is_password

        # 解析链步骤 2：环境变量 A2C_SMCP_<ENV_SEGMENT(id)>（编排层注入，命中不落盘）。
        # 只传裸 id：live 路径不带 server/tool 段，与 rust 逐字节一致（#155 决策 1）。
        env_val = self._env.get(env_var_name(resolved_id))
        if env_val is not None:
            self._cache[resolved_id] = env_val
            return env_val

        # 解析链步骤 3：OS keyring（仅 password:true）
        if is_password:
            secret = self._secret_store.get(resolved_id)
            if secret is not None:
                self._cache[resolved_id] = secret
                return secret

        # 解析链步骤 4：明文 value store（仅非密钥 promptString/pickString）
        if is_plain_persistable:
            stored = self._value_store.get(resolved_id)
            if stored is not None:
                self._cache[resolved_id] = stored
                return stored

        # 解析链步骤 5：解析。headless（无 TTY）下无交互可能 → 结构化 Missing（#173，对齐 rust-sdk#144 D1），
        # 非仅日志、非空串，供 client 驱动补录 UI 并重试：
        #   - secret（password:true）一律 Missing(SECRET)——绝不落明文、绝不静默回退（keyring 已在步骤 3 穷尽 miss，
        #     无 TTY 时无论 keyring 是否可用都拿不到密钥，统一结构化硬错误而非落到 prompt 抛不透明 OSError / 返回 ""）。
        #   - value（promptString/pickString）无 default → Missing(VALUE)；有 default → 落到下方 prompt 经
        #     EOFError 回退返回 default（rust：default 存在 ⇒ 不 Missing）。
        #   - command 在 headless 仍可执行（真相=命令输出），不在此报 Missing。
        has_tty = self._has_tty(sess)
        if not has_tty and is_password:
            raise MissingInputError(id=resolved_id, kind=InputKind.SECRET)
        if not has_tty and isinstance(cfg, (MCPServerPromptStringInput, MCPServerPickStringInput)) and cfg.default is None:
            raise MissingInputError(id=resolved_id, kind=InputKind.VALUE)

        if isinstance(cfg, MCPServerPromptStringInput):
            value = await self._aresolve_prompt(cfg, session=sess)
        elif isinstance(cfg, MCPServerPickStringInput):
            value = await self._aresolve_pick(cfg, session=sess)
        elif isinstance(cfg, MCPServerCommandInput):
            value = await self._aresolve_command(cfg)
        else:  # pragma: no cover
            logger.warning(f"未知输入类型: {type(cfg)} / Unknown input type")
            value = None

        # 解析后持久化（仅**交互 prompt 得值**时落盘，§9.3「prompt 得」；headless/默认回退不持久化）：
        #   password → keyring（keyring 不可用 → 仅会话缓存、绝不落明文）；非密钥 → 明文 value store。
        if has_tty and value is not None:
            if is_password:
                if not self._secret_store.set(resolved_id, str(value)):
                    logger.debug(f"keyring 不可用，密钥 {resolved_id} 仅会话缓存、不落明文 / keyring unavailable, secret cached only")
            elif is_plain_persistable:
                self._value_store.set(resolved_id, value)

        self._cache[resolved_id] = value
        return value

    @staticmethod
    def _has_tty(session: PromptSession | None) -> bool:
        """是否处于可交互环境（注入了 session，或 stdin 是 TTY）/ Whether interactive (session injected or stdin is a TTY)。"""
        if session is not None:
            return True
        try:
            return sys.stdin.isatty()
        except Exception:  # pragma: no cover - 防御性：stdin 被替换为非标准对象
            return False

    async def _aresolve_prompt(self, cfg: MCPServerPromptStringInput, *, session: PromptSession | None = None) -> str:
        msg = cfg.description or f"请输入 {cfg.id} / Please input {cfg.id}"
        pwd = bool(cfg.password)
        return await ainput_prompt(msg, password=pwd, default=cfg.default, session=session)

    async def _aresolve_pick(self, cfg: MCPServerPickStringInput, *, session: PromptSession | None = None) -> str:
        msg = cfg.description or f"请选择 {cfg.id} / Please pick {cfg.id}"
        options = cfg.options or []
        default_index = None
        if cfg.default is not None and cfg.default in options:
            default_index = options.index(cfg.default)
        picked = await ainput_pick(msg, options, default_index=default_index, multi=False, session=session)
        return str(picked) or (cfg.default or "")

    async def _aresolve_command(self, cfg: MCPServerCommandInput) -> Any:
        # 约定: command 为完整可执行字符串，由 shell 执行。args 如存在，暂不拼接，后续可扩展。
        return await arun_command(cfg.command, shell=True, parse="raw")
