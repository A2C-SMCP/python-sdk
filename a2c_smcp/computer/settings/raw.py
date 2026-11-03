# -*- coding: utf-8 -*-
# filename: raw.py
# @Time    : 2026/11/03
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
人编意图层（settings.json / mcp.json）的 **raw 保真读原语**（#246）
Raw-fidelity read primitives for the human-edited intent layer.

**为什么存在 / Why**：写路径过去用 :func:`~a2c_smcp.computer.settings.scope.load_settings_file` 读取，而它返回
:func:`~a2c_smcp.computer.settings.schema.validate_settings` 的**校验过滤视图**；随后整文件回写会把
``readRoots`` / ``landingRoot`` / ``enabledMcpjsonServers`` / ``enableAllProjectMcpServers`` / 非法
``permissions`` 条目 / policy-only 越权字段一并无提示地抹掉。本模块只做「原样读回 + 形状断言」，
让写路径能**只改目标键**，与设计文档 ``docs/design-0.2.1-cli-marketplace-ux.md`` §5.6 对齐
（「``settings set`` 是单字段写、不整体重写文件」；「invalid settings simply not used but remain in the file」）。
Reads the raw JSON object so writers can touch only their own key; the validation view stays a
**read-side** concern and is never written back.

**fail-closed 判据 / Fail-closed rule**：任何「会把用户数据换成别的东西」的输入一律拒绝——corrupt JSON /
根非对象 / IO 不可读 / 目标键类型不符（**含显式 ``null``**）都抛 :class:`SettingsFileCorruptError`，
**原文件字节不变**。「键缺失」与「空文件」才当空值（空文件无数据可丢，拒绝写只会把用户卡在可平凡恢复的状态）。
Anything that would replace user data with something else is refused; only a missing key or an
empty file counts as "no value". The file is never modified by this module.

**不复用** :func:`~a2c_smcp.computer.settings.store.read_jsonc_with_recovery`：那条路径面向**物化文件**
（``.corrupt-<ts>.bak`` 备份 + 降级返回 ``None``），与人编文件「fail-closed、原文件保留、不备份不重命名」相反。

**不在** :mod:`a2c_smcp.computer.settings` 的 ``__init__`` re-export：遵循该文件既有约定——会被
``installer`` / ``mcp_config`` 导入的模块一律直接从子模块导入（既避免循环也避免加重 ``import ...settings``）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "SettingsFileCorruptError",
    "list_field_or_empty",
    "mapping_field_or_empty",
    "read_raw_object",
]


class SettingsFileCorruptError(RuntimeError):
    """
    人编配置文件无法安全改写 / The human-edited config file cannot be rewritten safely.

    抛出即意味着**本次写入被拒绝且原文件逐字节未动**——调用方须把它翻译成本层的用户错误（CLI 退出码 /
    领域异常），**不得**降级为「当作空配置继续写」，否则等价于静默清空用户文件。
    Raised before any write; the file is left untouched. Callers must surface it as a user error,
    never degrade to "treat as empty and continue".

    :ivar path: 目标文件路径 / the target file.
    :ivar reason: 面向用户的可操作原因 / user-facing, actionable reason.
    """

    def __init__(self, path: Path | str, reason: str) -> None:
        self.path = Path(path)
        self.reason = reason
        super().__init__(f"{self.path}: {reason}; file left unchanged")


def read_raw_object(path: Path) -> dict[str, Any] | None:
    """
    读原始 JSON 对象，**不校验、不过滤**（raw 保真）/ Read the raw JSON object, unvalidated and unfiltered。

    - 文件缺失 → ``None``（调用方按 ``{}`` 起手）/ missing → ``None``.
    - 空文件 / 纯空白 → ``{}``（无数据可丢）/ empty or whitespace-only → ``{}``.
    - IO 不可读 / JSON 解析失败 / 根非对象 → :class:`SettingsFileCorruptError`.

    :param path: 目标文件 / the target file.
    :raises SettingsFileCorruptError: 无法安全改写 / the file cannot be rewritten safely.
    """
    p = Path(path)
    try:
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsFileCorruptError(p, f"unreadable ({exc}); fix the file permissions or remove it") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsFileCorruptError(
            p, f"invalid JSON ({exc}); repair or delete the file, then retry — it was NOT overwritten"
        ) from exc
    if not isinstance(data, dict):
        raise SettingsFileCorruptError(
            p,
            f"root must be a JSON object, got {type(data).__name__}; repair or delete the file, then retry — "
            "it was NOT overwritten",
        )
    return data


def _malformed(path: Path, key: str, expected: str, value: Any) -> SettingsFileCorruptError:
    """构造「目标键形状不符」的可操作错误 / Build the actionable "malformed target key" error。"""
    return SettingsFileCorruptError(path, f"{key!r} must be {expected}, got {type(value).__name__}")


def list_field_or_empty(obj: Mapping[str, Any], key: str, *, path: Path) -> list[Any]:
    """
    取顶层**数组**字段的副本；键缺失 → ``[]``；存在但非数组（含 ``null``）→ 抛错 / Top-level array field。

    **不**过滤数组元素——保留非预期元素（如数组里的非字符串项）是 raw 保真的一部分，读面校验自会忽略它们。
    Elements are never filtered: preserving unexpected entries is part of raw fidelity; the read side
    already ignores them.

    :raises SettingsFileCorruptError: 键存在但形状不符 / present but of the wrong shape.
    """
    value = obj.get(key)
    if key not in obj:
        return []
    if not isinstance(value, list):
        raise _malformed(path, key, "an array", value)
    return list(value)


def mapping_field_or_empty(obj: Mapping[str, Any], key: str, *, path: Path) -> dict[str, Any]:
    """
    取顶层**对象**字段的浅拷贝；键缺失 → ``{}``；存在但非对象（含 ``null``）→ 抛错 / Top-level object field。

    :raises SettingsFileCorruptError: 键存在但形状不符 / present but of the wrong shape.
    """
    value = obj.get(key)
    if key not in obj:
        return {}
    if not isinstance(value, Mapping):
        raise _malformed(path, key, "an object", value)
    return dict(value)
