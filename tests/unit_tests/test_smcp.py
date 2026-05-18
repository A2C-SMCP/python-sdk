# -*- coding: utf-8 -*-
# filename: test_smcp.py
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
"""
Issue #21 验收测试：协议常量 + 数据类型 + 错误码枚举（v0.2.0）。
Acceptance tests for issue #21: protocol constants + data types + error code enum (v0.2.0).

协议来源 / Protocol source:
  https://github.com/A2C-SMCP/a2c-smcp-protocol v0.2.0
  - docs/specification/error-handling.md §4.5
  - docs/specification/data-structures.md
"""
from __future__ import annotations

from enum import IntEnum

import a2c_smcp
from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.smcp import (
    GET_RESOURCES_EVENT,
    ErrorCode,
)


class TestProtocolVersion:
    """协议版本常量导出 / Protocol version constant export."""

    def test_protocol_version_importable_from_package(self) -> None:
        """from a2c_smcp import PROTOCOL_VERSION 可用 / importable."""
        assert PROTOCOL_VERSION == "0.2.0"

    def test_protocol_version_attribute_on_module(self) -> None:
        """模块级属性可访问 / accessible as module attribute."""
        assert a2c_smcp.PROTOCOL_VERSION == "0.2.0"

    def test_protocol_version_independent_from_package_version(self) -> None:
        """协议版本与包版本独立 / protocol version is independent from package version."""
        assert a2c_smcp.__version__ != PROTOCOL_VERSION


class TestErrorCode:
    """ErrorCode 枚举与协议指南 §4.5 一致 / matches protocol guide §4.5."""

    def test_error_code_is_int_enum(self) -> None:
        assert issubclass(ErrorCode, IntEnum)

    def test_mcp_upstream_authorization_codes(self) -> None:
        """4006 / 4007 MCP 上游授权 / upstream authorization codes."""
        assert ErrorCode.TOOL_AUTHORIZATION_REQUIRED == 4006
        assert ErrorCode.TOOL_AUTHORIZATION_FAILED == 4007

    def test_protocol_version_mismatch_code(self) -> None:
        """4008 协议版本握手 / version handshake."""
        assert ErrorCode.PROTOCOL_VERSION_MISMATCH == 4008

    def test_mcp_routing_codes(self) -> None:
        """4014 / 4015 MCP Server 路由 / MCP Server routing codes."""
        assert ErrorCode.MCP_SERVER_NOT_FOUND == 4014
        assert ErrorCode.MCP_CAPABILITY_NOT_SUPPORTED == 4015

    def test_int_serialization(self) -> None:
        """IntEnum 可以直接当 int 使用 / IntEnum acts as int for serialization."""
        assert int(ErrorCode.PROTOCOL_VERSION_MISMATCH) == 4008
        assert ErrorCode.MCP_SERVER_NOT_FOUND + 1 == 4015


class TestEventConstants:
    """v0.2 新增事件常量字符串 / v0.2 added event constant strings."""

    def test_get_resources_event_string(self) -> None:
        assert GET_RESOURCES_EVENT == "client:get_resources"
