# -*- coding: utf-8 -*-
# filename: computer_id.py
# @Time    : 2026/10/28
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 稳定身份 ``computer_id`` 的**形态判定**（协议#64 单一权威）。
Canonical-form validation for the stable Computer identity ``computer_id`` (protocol#64).

协议依据 / Protocol: a2c-smcp-protocol data-structures.md §EnterOfficeReq：

- 形态 = 小写、带连字符的规范 UUID 文本（RFC 9562，``8-4-4-4-12``，36 字符）；
- nil / max UUID 虽满足形态，但 **MUST NOT** 被接受；
- **版本位不参与校验**：接收方 MUST 视为不透明字符串（v4 是生成侧约束，非接收侧约束）。

Protocol: canonical lowercase hyphenated UUID text; nil/max rejected; the version nibble is
deliberately NOT validated — the receiver treats the value as opaque.
"""

from __future__ import annotations

import re
from typing import Final

#: 规范 UUID 文本形态（小写、带连字符、8-4-4-4-12）。/ Canonical lowercase hyphenated UUID text.
#: **不带** ``^…$`` 锚点、由 :meth:`re.Pattern.fullmatch` 匹配：Python 的 ``$`` 会匹配结尾换行，
#: 带 ``$`` 的 ``match`` 会把 ``<uuid>\\n``（37 字符）误判为合法。
#: No ``^…$`` anchors; use ``fullmatch`` — Python's ``$`` also matches before a trailing newline.
CANONICAL_UUID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)

#: nil UUID：形态合法但协议明令拒绝。/ Nil UUID: canonical but explicitly rejected by the protocol.
NIL_UUID: Final[str] = "00000000-0000-0000-0000-000000000000"
#: max UUID：同上。/ Max UUID: same.
MAX_UUID: Final[str] = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def is_canonical_computer_id(value: object) -> bool:
    """
    是否为协议接受的 ``computer_id`` 文本。

    - 类型必须是 ``str``（``None`` / 数字 / 字节一律非法）；
    - 必须匹配规范 UUID 形态；
    - 不得为 nil / max UUID；
    - 版本位**不**校验（任何版本的规范 UUID 都接受，接收方视为不透明）。

    Whether ``value`` is a protocol-acceptable ``computer_id`` text: a ``str``, canonical UUID,
    neither nil nor max; the version nibble is intentionally not checked (opaque to receivers).
    """
    if not isinstance(value, str):
        return False
    # 协议澄清（protocol#64 / PR #74）：整串匹配且**长度恰为 36**——前导/尾随空白或换行一律 400。
    # 正则之外显式限长，避免任何锚点语义差异（如 Python ``$`` 匹配结尾换行）漏进来。
    # Whole-string match AND exactly 36 chars; never rely on anchor semantics alone.
    if len(value) != 36:
        return False
    if CANONICAL_UUID_PATTERN.fullmatch(value) is None:
        return False
    return value not in (NIL_UUID, MAX_UUID)
