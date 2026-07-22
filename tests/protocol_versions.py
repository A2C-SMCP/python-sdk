# -*- coding: utf-8 -*-
# filename: protocol_versions.py
# @Author  : JQQ
"""
中文：从 SDK ``PROTOCOL_VERSION`` **派生**「兼容 / 不兼容对端版本」及 4008 拒绝载荷的
``min_supported`` / ``max_supported``，供所有版本握手相关测试复用。

  设计目的：**杜绝测试耦合 ``PROTOCOL_VERSION`` 的具体值**。协议版本会经常升级，测试若硬编码
  "0.2.0" / "0.3.0" 之类字面量，每次 bump 都要改一大堆——这是测试设计不合理。改为从当前
  ``PROTOCOL_VERSION`` 推导，协议升级时这些测试**零改动**。

English: Derive compatible / incompatible peer versions (and the 4008 payload's
``min_supported`` / ``max_supported``) from the SDK ``PROTOCOL_VERSION`` so version-handshake
tests never couple to a specific value; bumping the protocol version requires no test edits.

兼容规则依据 / Compat rule: a2c-smcp-protocol versioning.md（v0.x：MAJOR.MINOR 必须一致、PATCH 自由）。
"""

from __future__ import annotations

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.version import ProtocolVersion

_SELF = ProtocolVersion.parse(PROTOCOL_VERSION)

# 与 server(=PROTOCOL_VERSION) **兼容**：同 MAJOR.MINOR、仅抬 PATCH（v0.x PATCH 自由；v1.x 亦兼容）。
# Compatible with a server at PROTOCOL_VERSION: same MAJOR.MINOR, bumped PATCH.
COMPATIBLE_PEER: str = f"{_SELF.major}.{_SELF.minor}.999"

# 与 server **不兼容**：抬 MINOR（v0.x MINOR 不一致即不兼容；对端更高 MINOR 在 v1.x 亦不兼容）。
# Incompatible with a server at PROTOCOL_VERSION: bumped MINOR.
INCOMPATIBLE_PEER: str = f"{_SELF.major}.{_SELF.minor + 1}.0"


def min_supported_of(version: str) -> str:
    """给定某 server 版本，返回其 4008 载荷的 ``min_supported``（``{major}.{minor}.0``）。

    Return the ``min_supported`` a server at ``version`` reports in its 4008 payload.
    与 ``a2c_smcp/server/middleware.py`` 的构造保持一致 / mirrors the middleware builder.
    """
    v = ProtocolVersion.parse(version)
    return f"{v.major}.{v.minor}.0"


def max_supported_of(version: str) -> str:
    """给定某 server 版本，返回其 4008 载荷的 ``max_supported``（``{major}.{minor}.999``）。

    Return the ``max_supported`` a server at ``version`` reports in its 4008 payload.
    """
    v = ProtocolVersion.parse(version)
    return f"{v.major}.{v.minor}.999"
