# -*- coding: utf-8 -*-
# filename: version.py
# @Author  : JQQ
# @Software: PyCharm

"""
协议版本语义与兼容性判定 / Protocol version semantics & compatibility

严格实现 a2c-smcp-protocol docs/specification/versioning.md §兼容性判定规则 的参考算法：
  - v0.x（MAJOR=0，不稳定阶段）：MAJOR 与 MINOR **必须严格匹配**，PATCH 自由
  - v1.0+（稳定阶段）：MAJOR 严格匹配，Server MINOR **≥** Client MINOR，PATCH 自由
Strict implementation of the reference algorithm in versioning.md §compatibility rules.

供 Server 端 HTTP 握手中间件复用（client 侧只声明版本、解析 4008，不做兼容判定——
判定权属 Server）。Reused by the Server-side HTTP handshake middleware (the client only
declares its version and parses 4008; compatibility verdict belongs to the Server).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProtocolVersion:
    """语义化版本三元组 / Semantic version triple (MAJOR.MINOR.PATCH)."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, s: str) -> ProtocolVersion:
        """
        解析 ``MAJOR.MINOR.PATCH``；段数不为 3 或非整数均抛 ``ValueError``。
        Parse ``MAJOR.MINOR.PATCH``; raise ``ValueError`` when not exactly 3 integer parts.
        """
        parts = s.split(".")
        if len(parts) != 3:
            raise ValueError(f"Invalid version: {s}")
        try:
            return cls(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError as e:
            raise ValueError(f"Invalid version: {s}") from e

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def is_compatible(client: ProtocolVersion, server: ProtocolVersion) -> bool:
    """
    判定 Client 是否能连接到 Server（versioning.md §判定函数参考实现）。
    Whether the Client may connect to the Server (reference impl in versioning.md).
    """
    if client.major != server.major:
        return False
    if client.major == 0:
        # v0.x 严格匹配 MINOR / strict MINOR match in v0.x
        return client.minor == server.minor
    # v1.0+ 向后兼容（较新的 Server 接纳较旧的 Client）/ backward compatible
    return client.minor <= server.minor
