# -*- coding: utf-8 -*-
# filename: diagnostics.py
# @Time    : 2026/11/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
诊断文本的安全出口（凭据 / pydantic ``input_value`` 脱敏）/ Safe diagnostic text for logs and structured errors.

两个共享原语（#289 审查 🔴、#263 同族）：

- :func:`pydantic_error_text` / :func:`exception_text`：``ValidationError.__str__`` 内嵌 ``input_value=…``——
  当被校验的是**已渲染** config（``${input:secret}`` 已解析）时，原样透出即把 secret 写进 ``--json`` / 日志
  （协议 §6）。统一改为只报「字段 + 错误类型 + 条数」；非 pydantic 异常走散文脱敏。**多个落点共用**（manifest /
  installer / Computer 渲染·boot·治理 / REPL 展示），安全逻辑两份实现必然漂移。
- :func:`loggable_params`：MCP client 的 ``params`` 是**渲染后**的 ``server_parameters``（``env`` / ``headers``
  值、``args`` / ``command`` 里都可能含已解析 secret），连接失败日志不得原样回显。除 ``cwd`` / 超时等无 secret
  语义的字段外，一律 fail-closed 打码（保留字段名与 ``args`` 条数，便于排障定位）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from a2c_smcp.utils.git_url import escape_unsafe_chars, redact_git_urls_in_text, sanitize_text_for_diagnostics
from a2c_smcp.utils.logger import truncate

# 连接参数中**值**一律打码的字段（键名保留，便于排障定位）。
SECRET_PARAM_KEYS = frozenset({"env", "headers"})
# 任意字符串都可能是渲染后 secret 的字段（``${input:}`` 可出现在 command / args 任一元素）。
SECRET_PARAM_STRING_KEYS = frozenset({"command"})
SECRET_PARAM_SEQUENCE_KEYS = frozenset({"args"})
REDACTED_VALUE = "***"


def pydantic_error_text(
    exc: ValidationError,
    *,
    sanitize_segment: Callable[[str], str] | None = None,
) -> str:
    """
    pydantic 校验失败的安全诊断文本（**不含** ``input_value`` / ``url``）/ Safe text for a ValidationError.

    与 #263 在 ``parse_bundled_server`` 的修法同源：``errors(include_input=False, include_url=False)`` 后只
    拼「loc:type」。``loc`` 段过 :func:`~a2c_smcp.utils.git_url.escape_unsafe_chars`（控制字符 / 孤立代理项）。

    ``sanitize_segment``（#304 item 2）：``loc`` 段可能来自**手编 JSON 的 key**（如 ``extra_forbidden`` 的
    额外键），可含 userinfo 形态凭据——调用方可注入**段漏斗**（如
    :func:`~a2c_smcp.computer.settings.recovery.sanitize_marketplace_name`）逐段 fail-closed；缺省不注入
    （保持既有调用方行为）。散文漏斗按空白分词，无法回收「被空白劈开」的凭据，故须在段级处理。
    """
    errors = exc.errors(include_input=False, include_url=False)

    def _segment(part: object) -> str:
        text = str(part)
        return text if sanitize_segment is None else sanitize_segment(text)

    fields = ", ".join(
        f"{escape_unsafe_chars('.'.join(_segment(part) for part in err.get('loc', ())) or '<root>')}:{err.get('type', 'invalid')}"
        for err in errors
    )
    return f"{len(errors)} validation error(s) in [{fields}]"


def exception_text(exc: BaseException) -> str:
    """
    任意异常的安全诊断文本（日志 / 结构化错误共用出口）/ Safe diagnostic text for any exception.

    - ``ValidationError`` → :func:`pydantic_error_text`（不触 ``input_value``）；
    - ``SkillSourceError`` → 其消息已在构造期脱敏，按原样取（**不得**二次散文脱敏，会把紧凑 JSON 标点吞成
      乱码——见 :func:`~a2c_smcp.computer.skills.sources.safe_exception_text`）；
    - 其余 → ``redact_git_urls_in_text`` + ``sanitize_text_for_diagnostics``。

    **自身绝不抛**（错误转换在 ``except`` 内运行；二次抛出会顶替原异常）。
    """
    try:
        if isinstance(exc, ValidationError):
            return pydantic_error_text(exc)
        # 延迟导入：``computer.skills.sources`` 依赖本模块的同级 utils，模块级导入会引入 utils→computer 的
        # 层次倒挂（且 sources 的 ``safe_exception_text`` 需要 ``SkillSourceError`` 的构造期脱敏语义）。
        from a2c_smcp.computer.skills.sources import SkillSourceError, safe_exception_text

        if isinstance(exc, SkillSourceError):
            return safe_exception_text(exc)
        return sanitize_text_for_diagnostics(redact_git_urls_in_text(str(exc)))
    except Exception:  # noqa: BLE001 - 错误转换自身不得可抛
        return f"<unprintable {type(exc).__name__}>"


def _redact_url(url: str) -> str:
    """
    MCP server URL 的安全展示：仅保留 ``scheme://host[:port]``，path / query / fragment / userinfo 一律遮蔽。

    **不复用** :func:`~a2c_smcp.utils.git_url.diagnostic_url_for_display`——那是 marketplace 的 source 展示契约
    （保留 path、只遮凭据样段）；而 MCP URL 的 path 可由 ``${input:token}`` 渲染（``"/v1/<secret>/sse"``），
    属「任意字符串都可能是 secret」面（#289 终审 🔴2）→ fail-closed 把整段 path 遮掉。
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return REDACTED_VALUE
    # scheme 也可由 ``${input:}`` 渲染（``"${input:tok}://host"``）→ allow-list，未知 scheme 整条遮蔽。
    if parts.scheme.lower() not in {"http", "https", "sse", "ws", "wss"} or not host:
        return REDACTED_VALUE
    shown_host = f"[{host}]" if ":" in host else host  # IPv6 字面量补回方括号
    suffix = "/<redacted-path>" if parts.path and parts.path != "/" else ""
    port_part = f":{port}" if port else ""
    return f"{parts.scheme}://{shown_host}{port_part}{suffix}"


def _redact_param_value(key: str, value: Any) -> Any:
    """按字段名递归打码（fail-closed）：secret 值字段全遮，command 整体遮，args 逐项遮（保留条数）。"""
    if key in SECRET_PARAM_KEYS:
        if isinstance(value, Mapping):
            return {str(k): REDACTED_VALUE for k in value}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            # httpx 风格的 headers 序列 ``[(name, value), …]``：保留 header 名，值打码。
            return [
                f"{item[0]}: {REDACTED_VALUE}"
                if isinstance(item, (tuple, list)) and len(item) == 2
                else REDACTED_VALUE
                for item in value
            ]
        return REDACTED_VALUE if value is not None else None
    if key in SECRET_PARAM_STRING_KEYS:
        return REDACTED_VALUE if value is not None else None
    if key in SECRET_PARAM_SEQUENCE_KEYS:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [REDACTED_VALUE for _ in value]
        return REDACTED_VALUE if value is not None else None
    if key == "url" and isinstance(value, str):
        return _redact_url(value)
    if isinstance(value, Mapping):
        return {str(k): _redact_param_value(str(k), v) for k, v in value.items()}
    if value is None or isinstance(value, bool | int | float):
        return value  # 无可承载字符串的标量
    # 未知字符串 / Path / 自定义对象：无法证明不是渲染后 secret → fail-closed（与模块承诺一致）。
    return REDACTED_VALUE


def loggable_params(params: Any, *, max_len: int = 500) -> str:
    """
    连接参数的安全日志形态 / Log-safe rendering of connection params.

    ``params``（mcp SDK 的 ``StdioServerParameters`` / ``SseServerParameters`` / ``StreamableHttpParameters``，
    或同形映射）是 **materializer 渲染后**的配置：``${input:secret}`` 可解析进 ``env`` / ``headers`` / ``args`` /
    ``command`` 任一处，原样 ``str(params)`` 会泄进 WARNING 日志。本函数输出 JSON：上述字段值一律打码、URL 去
    userinfo/query，再 :func:`~a2c_smcp.utils.logger.truncate`。**自身绝不抛**（日志出口）。
    """
    try:
        data: Any = params
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="python")
        if isinstance(data, Mapping):
            data = {str(k): _redact_param_value(str(k), v) for k, v in data.items()}
            return truncate(json.dumps(data, ensure_ascii=False, default=str), max_len)
        return truncate(str(data), max_len)
    except Exception:  # noqa: BLE001 - 日志安全出口不得抛（否则顶替原异常）
        return f"<unprintable {type(params).__name__}>"
