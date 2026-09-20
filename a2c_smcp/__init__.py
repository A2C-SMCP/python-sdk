# -*- coding: utf-8 -*-
# filename: __init__.py.py
# @Time    : 2025/9/28 15:33
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
__version__: str = "0.4.0"

# A2C-SMCP 协议版本号 / A2C-SMCP protocol version.
# 与 SDK 包版本（__version__）独立：MAJOR.MINOR 锁定协议 MAJOR.MINOR，PATCH 自由。
# Independent from SDK package version: MAJOR.MINOR locks to protocol MAJOR.MINOR; PATCH is free.
#
# 0.4.0 → 0.5.0（#214）：三个房间事件的失败 ack 由 `(bool, str | None)` 元组改为 flat
# `ErrorPayload`——**ack 载荷字节序列与 arity 均变化**，按 versioning.md:140 属破坏性变更，MUST 走
# MINOR；v0.x 下 MINOR **严格匹配**，抬位后握手层才会正确拒绝 0.4.x 对端。
# 0.4.0 → 0.5.0 (#214): the room-event failure acks changed shape — a breaking wire change that MUST
# bump MINOR; under v0.x MINOR is matched strictly, so this is what makes the handshake reject 0.4.x.
PROTOCOL_VERSION: str = "0.5.0"
