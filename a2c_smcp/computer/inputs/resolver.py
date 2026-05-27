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
        env(`A2C_INPUT_<ID>`) → keyring（仅 password）→ 明文 value store（仅非密钥），并在**交互 prompt
        得值后**按类持久化（password→keyring、非密钥→明文 state；env 命中/command/headless 不落盘）。
        密钥在 headless（无 env+无 keyring+无 TTY）下**硬错误**，绝不落明文。
  English: Input resolvers. v0.2.1 #65 prepends an env→keyring→plaintext resolution chain and persists
           interactively-prompted values by type (secrets to OS keyring, non-secrets to plaintext state);
           headless secrets hard-error rather than ever writing plaintext.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping
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
from a2c_smcp.utils.logger import get_logger

logger = get_logger("computer")


class InputNotFoundError(KeyError):
    pass


class SecretResolutionError(RuntimeError):
    """
    中文: ``password:true`` input 在 headless（无 ``A2C_INPUT_<ID>`` env、无 keyring、无 TTY）下无法解析。
    English: A ``password:true`` input cannot be resolved headless (no env, no keyring, no TTY).

    设计要求**硬错误**而非降级落明文（§9.3，杜绝把密钥明文落盘的反模式）。
    """


def env_var_name(input_id: str) -> str:
    """
    把 input id 映射为环境变量名 ``A2C_INPUT_<ID_UPPER>``（非字母数字 → ``_``）/ Map id to env var name。

    含前缀 plugin id（``<plugin>@<mp>/<id>``）一并归一，如 ``frontend@team/figma_token`` →
    ``A2C_INPUT_FRONTEND_TEAM_FIGMA_TOKEN``。
    """
    return "A2C_INPUT_" + re.sub(r"[^A-Z0-9]", "_", input_id.upper())


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
        # 1. 定位定义：裸 id 未命中且给了 plugin 上下文 → 回退查带前缀池条目（§9.3 D2）
        #    Locate the definition; with plugin context, bare id falls back to the prefixed pool entry.
        cfg = self._inputs.get(input_id)
        resolved_id = input_id
        if cfg is None and plugin and marketplace:
            prefixed = prefix_input_id(plugin, marketplace, input_id)
            prefixed_cfg = self._inputs.get(prefixed)
            if prefixed_cfg is not None:
                cfg, resolved_id = prefixed_cfg, prefixed
        if cfg is None:
            raise InputNotFoundError(input_id)

        # 2. 进程内 cache（按解析后的池 id 缓存，避免不同 plugin 的同裸 id 串味）
        if resolved_id in self._cache:
            return self._cache[resolved_id]

        sess = session or self.session
        is_password = isinstance(cfg, MCPServerPromptStringInput) and bool(cfg.password)
        is_plain_persistable = isinstance(cfg, (MCPServerPromptStringInput, MCPServerPickStringInput)) and not is_password

        # 解析链步骤 2：环境变量 A2C_INPUT_<ID>（编排层注入，命中不落盘）
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

        # 解析链步骤 5：交互 prompt——password 在 headless（无 env + 无 TTY）下硬错误，绝不落明文。
        # keyring 已在步骤 3 穷尽并 miss，无 TTY 时无论 keyring 是否可用都拿不到密钥 → 统一硬错误
        # （而非落到 prompt 在 headless 抛不透明 OSError / 静默返回 ""）。
        has_tty = self._has_tty(sess)
        if is_password and not has_tty:
            raise SecretResolutionError(
                f"secret '{resolved_id}' 无法解析；请设置环境变量 {env_var_name(resolved_id)} 或在 TTY 重试 / "
                f"cannot resolve secret; set {env_var_name(resolved_id)} or retry in a TTY",
            )

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
