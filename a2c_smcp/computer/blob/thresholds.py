# -*- coding: utf-8 -*-
# filename: thresholds.py
# @Author  : JQQ
# @Software: PyCharm
"""
SKILL / blob 通道阈值（SDK 可配，协议要求「保证单条 ack 不超 Server buffer」）。
SKILL / blob channel thresholds (SDK-configurable; protocol mandates "single ack ≤ Server buffer").

设计依据 / Design source: ``docs/design-0.2.1-skill-computer-management.md`` §4.4.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 默认值 / Defaults (design §4.4)
DEFAULT_INLINE_BUDGET: int = 32 * 1024  # 32 KiB — 文本资源 ≤ 此 → body；否则铸句柄
DEFAULT_TOO_LARGE_CAP: int = 100 * 1024 * 1024  # 100 MiB — 超此 → 4017 too_large，零字节传输
DEFAULT_CHUNK_MAX_BYTES: int = 256 * 1024  # 256 KiB — 单块原始字节上限（clamp 后远低于 1 MiB buffer）

# 环境变量键 / Env override keys
ENV_INLINE_BUDGET: str = "A2C_SKILL_INLINE_BUDGET"
ENV_TOO_LARGE_CAP: str = "A2C_SKILL_MAX_SIZE"
ENV_CHUNK_MAX_BYTES: str = "A2C_BLOB_CHUNK_BYTES"


@dataclass(frozen=True)
class BlobThresholds:
    """SKILL / blob 阈值集合（不可变，便于注入与共享）/ Immutable threshold bundle for injection.

    Attributes:
        inline_budget: 文本资源内联预算（字节）。文本 MIME 且 ``total_size`` ≤ 此值 → 直接 body；
            否则铸 blob_handle 走 ``client:get_blob``。二进制 MIME 一律铸句柄（与此无关）。
            Inline text budget; binary MIME always handle-routed regardless.
        too_large_cap: 资源绝对上限（字节）。``total_size`` 超此 → ``4017 too_large``，
            **不铸句柄、零字节传输**（DoS 防御）。Hard cap; over → ``4017 too_large``, no handle.
        chunk_max_bytes: 单块原始字节上限。Computer 对客户建议值 ``clamp``，恒保证
            base64 (+33%) + envelope ≤ Server ``maxHttpBufferSize``（默认 1 MiB）。
            Per-chunk raw-byte ceiling; clamped to keep base64+envelope ≤ Server buffer.
    """

    inline_budget: int = DEFAULT_INLINE_BUDGET
    too_large_cap: int = DEFAULT_TOO_LARGE_CAP
    chunk_max_bytes: int = DEFAULT_CHUNK_MAX_BYTES

    def clamp_chunk(self, requested: int | None) -> int:
        """clamp 客户建议的单块大小到 ``[1, chunk_max_bytes]``；缺省取 ``chunk_max_bytes``。
        Clamp the client-requested chunk size into ``[1, chunk_max_bytes]``; default to max.
        """
        if requested is None or requested <= 0:
            return self.chunk_max_bytes
        return min(requested, self.chunk_max_bytes)


def default_thresholds() -> BlobThresholds:
    """构造一份**当前进程**默认阈值，按环境变量覆盖；非整数 / 非正环境变量 → 回退默认值。

    Build a default threshold bundle honoring env overrides; non-int / non-positive env values
    silently fall back to the hard-coded default.
    """
    return BlobThresholds(
        inline_budget=_read_positive_int_env(ENV_INLINE_BUDGET, DEFAULT_INLINE_BUDGET),
        too_large_cap=_read_positive_int_env(ENV_TOO_LARGE_CAP, DEFAULT_TOO_LARGE_CAP),
        chunk_max_bytes=_read_positive_int_env(ENV_CHUNK_MAX_BYTES, DEFAULT_CHUNK_MAX_BYTES),
    )


def _read_positive_int_env(key: str, fallback: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback
