# -*- coding: utf-8 -*-
# filename: value_store.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
非密钥 input 值的明文持久化 / Plaintext persistence for non-secret input values（v0.2.1 #65，§9.3）。

对标 VS Code：promptString/pickString 这类**非密钥**输入首次解析后落明文 state，跨重启复用、不再
prompt。密钥（``password:true``）**绝不**进此文件——走 :mod:`secret_store` 的 OS keyring（§9.3 安全
不变量）。文件位于 ``$XDG_STATE_HOME/a2c/input-values.json``（XDG-first，回退 ``~/.local/state/a2c``），
``0600`` 权限、原子写、gitignored。

键空间：**全局** ``<id>``（与模型「Input id 全局唯一、跨类型不重复」一致；无 workspace 维度）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from a2c_smcp.utils.atomic_io import atomic_write_text
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import resolve_xdg_first

logger = get_logger("computer")

XDG_STATE_HOME_ENV = "XDG_STATE_HOME"
_A2C_STATE_SUBDIR = "a2c"
VALUE_STORE_FILENAME = "input-values.json"


def resolve_value_store_path(env: Mapping[str, str] | None = None) -> Path:
    """
    解析明文 value store 路径 / Resolve the plaintext value-store path。

    优先 ``$XDG_STATE_HOME/a2c/input-values.json``（XDG_STATE_HOME 须为绝对路径，否则按 XDG 规范忽略），
    回退 ``~/.local/state/a2c/input-values.json``。复用 :func:`resolve_xdg_first`（与 ``settings/scope.py``
    的 user config dir 同范式，区别仅在 env 变量与子目录）。
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    base = resolve_xdg_first(
        environ,
        XDG_STATE_HOME_ENV,
        _A2C_STATE_SUBDIR,
        fallback=Path.home() / ".local" / "state" / _A2C_STATE_SUBDIR,
    )
    return base / VALUE_STORE_FILENAME


class ValueStore:
    """
    中文: 非密钥 input 值的明文存储（``{id: value}`` 扁平 map），原子写 + ``0600``。
    English: Plaintext store for non-secret input values (flat ``{id: value}`` map), atomic + ``0600``.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._path = resolve_value_store_path(env)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        """
        读取全量 map；文件不存在或畸形 JSON → ``{}``（field-tolerant，不抛）。
        Load the whole map; missing file or malformed JSON → ``{}`` (tolerant, never raises).
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:  # pragma: no cover - 防御性：读权限/IO 异常
            logger.warning(f"读取 value store 失败，按空处理 / Failed to read value store, treat as empty: {e}")
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"value store JSON 畸形，按空处理 / Malformed value store JSON, treat as empty: {self._path}")
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, input_id: str) -> Any | None:
        """按 id 取值；不存在返回 ``None`` / Get value by id; ``None`` if absent。"""
        return self.load().get(input_id)

    def set(self, input_id: str, value: Any) -> None:
        """写入/覆盖 id 的值（原子写 + ``0600``）/ Set value for id (atomic + ``0600``)。"""
        data = self.load()
        data[input_id] = value
        self._atomic_write(data)

    def delete(self, input_id: str) -> bool:
        """删除 id 的值，返回是否删除发生 / Delete value for id; returns whether deletion happened。"""
        data = self.load()
        if input_id not in data:
            return False
        data.pop(input_id, None)
        self._atomic_write(data)
        return True

    def _atomic_write(self, data: Mapping[str, Any]) -> None:
        # 原子写（复用共享原语）后立即 chmod 0600——value store 虽仅存非密钥，仍按最小可见性硬化。
        # Atomic write (shared primitive) then chmod 0600 — defense-in-depth even for non-secret values.
        atomic_write_text(self._path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        try:
            os.chmod(self._path, 0o600)
        except OSError as e:  # pragma: no cover - 例如 Windows / 特殊文件系统不支持 chmod
            logger.debug(f"chmod 0600 失败（非致命）/ chmod 0600 failed (non-fatal): {e}")
