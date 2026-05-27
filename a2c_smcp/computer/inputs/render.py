"""
文件名: render.py
作者: JQQ
创建日期: 2025/9/18
最后修改日期: 2026/5/27
版权: 2023 JQQ. All rights reserved.
依赖: re, typing
描述:
  中文: MCP 配置按需渲染器。识别占位符并替换，支持递归容器与深度限制。
        v0.2.1 #65（§9.1，对标 VS Code）：除 ``${input:<id>}``（经回调走解析链）外，新增
        ``${env:<VAR>}``（进程环境变量，缺失→空串 + WARN）与预定义变量 ``${workspaceFolder}`` /
        ``${userHome}`` / ``${pathSeparator}``。未知占位符保持原样（向后兼容）。
  English: On-demand renderer for MCP configs. v0.2.1 adds ``${env:VAR}`` (process env, missing → empty
           + WARN) and predefined ``${workspaceFolder}`` / ``${userHome}`` / ``${pathSeparator}`` on top
           of ``${input:id}``. Unknown placeholders are left untouched (backward compatible).
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from a2c_smcp.utils.logger import get_logger

logger = get_logger("computer")


def load_env_file(path: Path) -> dict[str, str]:
    """
    加载 VS Code 风格 ``envFile`` 的 ``KEY=VALUE`` 行（§9.1）/ Load a VS Code-style ``envFile``。

    解析规则：跳过空行与 ``#`` 注释；容忍前缀 ``export ``；去除值两端成对引号；无 ``=`` 的行跳过 + WARN。
    文件缺失 / 读失败 → ``{}`` + WARN（容错，不抛）。变量展开**不**在此发生——envFile 值按字面取，
    占位符（如 ``${workspaceFolder}``）由调用方在渲染整份配置时（含 envFile **路径**）先行替换。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(f"envFile 不存在，跳过: {path} / envFile not found, skip")
        return {}
    except OSError as e:  # pragma: no cover - 读权限/IO 异常
        logger.warning(f"envFile 读取失败，跳过: {path}: {e} / failed to read envFile, skip")
        return {}

    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            logger.warning(f"envFile 行无 '='，跳过: {raw_line!r} / envFile line without '=', skip")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


# 中文: 匹配任意 ${...} 占位符；token 由 _aresolve_token 按前缀分派（input:/env:/预定义）
# English: Match any ${...} placeholder; the inner token is dispatched by prefix in _aresolve_token.
PLACEHOLDER_PATTERN = re.compile(r"\$\{([^}]+)}")

_PREDEFINED_VARS = ("workspaceFolder", "userHome", "pathSeparator")

ResolveInput = Callable[[str], Awaitable[Any]]


class ConfigRender:
    """
    中文: 配置渲染器，按需解析字符串中的占位符，并递归处理字典与列表。
    English: Config renderer that lazily resolves placeholders in strings and recursively processes dicts and lists.
    """

    def __init__(self, *, max_depth: int = 10) -> None:
        self._max_depth = max_depth

    async def arender(
        self,
        data: Any,
        resolve_input: ResolveInput,
        *,
        variables: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        _depth: int = 0,
    ) -> Any:
        """
        中文: 递归渲染任意结构的数据，字符串中按需替换占位符。
        English: Recursively render arbitrary structured data, replacing placeholders in strings on demand.

        :param variables: 预定义变量映射（workspaceFolder/userHome/pathSeparator）/ predefined variables.
        :param env: ``${env:VAR}`` 取值来源（默认 ``os.environ``，可注入便于测试）/ source for ``${env:VAR}``.
        """
        if _depth > self._max_depth:
            logger.error("渲染深度超过限制，停止递归 / Rendering depth exceeded, stop recursion")
            return data

        if isinstance(data, dict):
            return {k: await self.arender(v, resolve_input, variables=variables, env=env, _depth=_depth + 1) for k, v in data.items()}
        if isinstance(data, list):
            return [await self.arender(x, resolve_input, variables=variables, env=env, _depth=_depth + 1) for x in data]
        if isinstance(data, str):
            return await ConfigRender.arender_str(data, resolve_input, variables=variables, env=env)
        return data

    @staticmethod
    async def _aresolve_token(
        token: str,
        resolve_input: ResolveInput,
        variables: Mapping[str, str] | None,
        env: Mapping[str, str] | None,
    ) -> tuple[bool, Any]:
        """
        解析单个占位符 token / Resolve a single placeholder token。

        返回 ``(matched, value)``：``matched=False`` 表示未知占位符，调用方应**保持原样**不替换。
        Returns ``(matched, value)``; ``matched=False`` means unknown placeholder → caller leaves it as-is.
        """
        if token.startswith("input:"):
            input_id = token[len("input:") :]
            try:
                return True, await resolve_input(input_id)
            except KeyError:
                logger.warning(f"未找到输入项: {input_id} / Input id not found: {input_id}")
                return False, None
            except Exception as e:  # pragma: no cover
                logger.error(f"解析输入失败: {input_id}, 错误: {e}", exc_info=True)
                return False, None
        if token.startswith("env:"):
            var = token[len("env:") :]
            source: Mapping[str, str] = os.environ if env is None else env
            val = source.get(var)
            if val is None:
                logger.warning(f"环境变量未定义，替换为空串: {var} / Env var undefined, substitute empty: {var}")
                return True, ""  # VS Code parity：缺失 env → 空串
            return True, val
        if token in _PREDEFINED_VARS:
            val = (variables or {}).get(token)
            if val is None:
                logger.warning(f"预定义变量未提供，替换为空串: {token} / Predefined var missing, substitute empty: {token}")
                return True, ""
            return True, val
        return False, None  # 未知占位符 → 保持原样 / unknown → leave as-is

    @staticmethod
    async def arender_str(
        s: str,
        resolve_input: ResolveInput,
        *,
        variables: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Any:
        """
        中文: 对字符串中的所有占位符按需替换；若整串恰为单一占位符且解析为非字符串值，返回该非字符串值。
        English: Replace placeholders lazily; if the whole string is a single placeholder resolving to a
        non-string (e.g. ``${input:x}`` → dict), return that value.
        """
        matches = list(PLACEHOLDER_PATTERN.finditer(s))
        if not matches:
            return s

        # 整串恰为单一占位符 → 允许返回非字符串类型（例如 ${input:x} 解析为 dict/list）
        if len(matches) == 1 and matches[0].span() == (0, len(s)):
            matched, value = await ConfigRender._aresolve_token(matches[0].group(1), resolve_input, variables, env)
            return value if matched else s

        # 否则逐个替换为其字符串表现形式（未知占位符保持原样）
        result = s
        offset = 0
        for m in matches:
            start, end = m.span()
            matched, value = await ConfigRender._aresolve_token(m.group(1), resolve_input, variables, env)
            if not matched:
                continue
            repl = str(value)
            result = result[: start + offset] + repl + result[end + offset :]
            offset += len(repl) - (end - start)
        return result
