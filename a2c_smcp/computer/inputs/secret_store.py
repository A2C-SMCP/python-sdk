# -*- coding: utf-8 -*-
# filename: secret_store.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
密钥（``password:true`` input）的 OS keyring 封装 / OS keyring wrapper for secret inputs（v0.2.1 #65，§9.3）。

对标 VS Code SecretStorage：``keyring`` 库自动选 macOS Keychain / Windows Credential Manager /
Linux Secret Service（libsecret）。``keyring`` 是**可选依赖**（``pip install a2c-smcp[keyring]``）——
故在此**惰性 import**：缺失不破坏模块导入，仅令 :func:`keyring_available` 返回 ``False``，由
:mod:`resolver` 走 headless 降级（env）或硬错误（**绝不写明文**，§9.3）。

键空间：service=``a2c-computer``、key=**全局 ``<id>``**（与模型「Input id 全局唯一」一致）。
"""

from __future__ import annotations

from typing import Any

from a2c_smcp.utils.logger import get_logger

logger = get_logger("computer")

SERVICE_NAME = "a2c-computer"

# 可用性探测结果缓存（None=未探测）/ Cached availability probe (None = not probed yet).
_available: bool | None = None


def _load_keyring() -> Any | None:
    """惰性 import keyring；缺失返回 ``None`` / Lazily import keyring; ``None`` if not installed。"""
    try:
        # 惰性 import 是本模块设计核心（可选依赖缺失不破坏导入）/ lazy import is intentional (optional dep)
        import keyring  # noqa: PLC0415

        return keyring
    except ImportError:
        return None


def keyring_available(*, force_recheck: bool = False) -> bool:
    """
    探测 OS keyring 是否可用 / Probe whether an OS keyring backend is usable。

    判定：``keyring`` 已安装 **且** 当前 backend 非 ``fail``/``null``（容器/CI/无 Secret Service 环境下
    ``keyring`` 会回退到 ``fail.Keyring``，此时不可用）。结果缓存（``force_recheck`` 可重探，便于测试）。
    """
    global _available
    if _available is not None and not force_recheck:
        return _available

    kr = _load_keyring()
    if kr is None:
        _available = False
        return False
    try:
        backend = kr.get_keyring()
        # fail.Keyring / chainer 为空 → 不可用。以模块路径判定，避免硬依赖具体类。
        mod = type(backend).__module__
        _available = "fail" not in mod and backend is not None
    except Exception as e:  # pragma: no cover - 后端探测异常按不可用处理
        logger.debug(f"keyring 后端探测异常，按不可用处理 / keyring backend probe failed, treat unavailable: {e}")
        _available = False
    return _available


class SecretStore:
    """
    中文: ``password:true`` input 值的 OS keyring 存取；keyring 不可用时为安全降级（get→None / set→False）。
    English: OS keyring store for ``password:true`` input values; degrades safely when keyring unavailable.
    """

    def __init__(self, *, service: str = SERVICE_NAME) -> None:
        self._service = service

    @property
    def available(self) -> bool:
        return keyring_available()

    def get(self, input_id: str) -> str | None:
        """从 keyring 取密钥；不可用或未存返回 ``None`` / Get secret; ``None`` if unavailable or absent。"""
        if not keyring_available():
            return None
        kr = _load_keyring()
        if kr is None:  # pragma: no cover - available 已保证 kr 非空
            return None
        try:
            secret: str | None = kr.get_password(self._service, input_id)
            return secret
        except Exception as e:  # pragma: no cover - keyring 运行期异常
            logger.warning(f"keyring 读取失败 / keyring get failed for {input_id}: {e}")
            return None

    def set(self, input_id: str, value: str) -> bool:
        """写密钥进 keyring，返回是否成功；不可用 → ``False``（**绝不**降级落明文）/ Store secret; ``False`` if unavailable。"""
        if not keyring_available():
            return False
        kr = _load_keyring()
        if kr is None:  # pragma: no cover
            return False
        try:
            kr.set_password(self._service, input_id, value)
            return True
        except Exception as e:  # pragma: no cover - keyring 运行期异常
            logger.warning(f"keyring 写入失败 / keyring set failed for {input_id}: {e}")
            return False

    def delete(self, input_id: str) -> bool:
        """从 keyring 删除密钥，返回是否删除发生 / Delete secret; returns whether deletion happened。"""
        if not keyring_available():
            return False
        kr = _load_keyring()
        if kr is None:  # pragma: no cover
            return False
        try:
            kr.delete_password(self._service, input_id)
            return True
        except Exception as e:  # keyring 在未存时抛 PasswordDeleteError，按未删除处理
            logger.debug(f"keyring 删除未发生 / keyring delete no-op for {input_id}: {e}")
            return False
