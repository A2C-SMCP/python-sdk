# -*- coding: utf-8 -*-
# filename: window_uri.py
# @Time    : 2025/10/01 19:43
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
窗口协议 WindowURI 封装（v0.2 协议）
WindowURI encapsulation for MCP window:// resources (v0.2 protocol)

协议说明 / Protocol (v0.2):
- scheme 固定为 window，URI 形如 window://host/path1/path2/.../pathN —— 纯标识符
  Scheme is fixed to 'window'; URI is a pure identifier: window://host/path1/.../pathN
- host 为 MCP 的唯一标识（建议反向域名风格），跨 MCP Server MUST 唯一
  host is the MCP unique id (reverse-DNS style recommended); MUST be unique across MCP Servers
- path 可以有 0..N 个段，由 MCP 决定其窗口拓扑
  path can have 0..N segments; topology decided by the MCP
- v0.2 起 priority / fullscreen 等元数据不再放在 URI query，而下沉到 MCP Resource
  的 annotations / _meta（详见协议指南 v0.2-uri-metadata-refactor.md §4.1）
  Since v0.2, priority / fullscreen metadata are no longer carried in URI query —
  they live on Resource.annotations / Resource._meta (see protocol guide §4.1)

URI query 处理分层（协议 §F4 Postel's law）：
URI query handling layers (Postel's law):
- Computer 解析层（本类）：解析时若含 query → WARN 日志后丢弃，不抛异常
  Computer parser layer (this class): on query → WARN + drop, no exception
- Agent SDK 构造层（build_uri）：不接受 priority / fullscreen 参数
  Agent SDK builder layer (build_uri): does not accept priority / fullscreen params

Pydantic v2 兼容：支持字符串 <-> WindowURI 的校验与序列化
Pydantic v2 compatibility: supports string <-> WindowURI validation and serialization
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cached_property
from typing import Any, Self, cast

from pydantic import AnyUrl, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from yarl import URL as YURL

from a2c_smcp.utils.logger import get_logger

logger = get_logger("utils.window_uri")


class WindowURI:
    """
    WindowURI 用于解析与构建 window:// 协议的 URI（v0.2 纯标识符形态）。
    WindowURI parses and builds window:// URIs (v0.2 pure-identifier form).
    """

    def __init__(self, uri: str) -> None:
        """
        使用 yarl.URL 解析并缓存 URL 组件，避免第三方在 Py3.12 的兼容性问题。
        Parse and cache URL components via yarl.URL to avoid compat issues on Py3.12.

        v0.2 协议层容错：含 query 的 URI 仍可解析成功，但 query 部分会被丢弃并记录 WARN。
        v0.2 tolerance: URIs with query still parse, but the query is dropped with a WARN log.
        """
        self._url = YURL(uri)
        if self._url.scheme != "window":
            raise ValueError(f"Invalid URI scheme: {self._url.scheme}, uri={uri}")
        if not self._url.host:
            raise IndexError(f"Missing host (MCP id) in URI: {uri}")
        if self._url.query_string:
            logger.warning(
                "WindowURI v0.2 不再支持 query 元数据，已丢弃 query 部分。"
                "WindowURI v0.2 no longer supports query metadata; query has been dropped. "
                f"uri={uri}",
            )

    # -------------------------
    # 基础属性 / Basic properties
    # -------------------------
    @cached_property
    def mcp_id(self) -> str:
        """
        获取 MCP 唯一标识（host）
        Get MCP unique identifier (host)
        """
        return str(self._url.host)

    @cached_property
    def windows(self) -> list[str]:
        """
        获取 window 路径段列表，允许为空列表
        Get list of window path segments, can be empty
        """
        return list(self.parts)

    @cached_property
    def parts(self) -> list[str]:
        """
        路径段列表：基于原始编码的 path 分割后逐段解码，保证如 "c%2Fd" -> "c/d" 保持为单段
        Path segments: split raw-encoded path then decode each segment, keeping "c%2Fd" as one "c/d" part
        """
        from urllib.parse import unquote

        raw = self._url.raw_path.lstrip("/")
        if not raw:
            return []
        return [unquote(seg) for seg in raw.split("/")]

    # -------------------------
    # 已废弃属性（v0.2 起 query 不再承载元数据）/ Deprecated attrs
    # -------------------------
    # TODO(#11): 移除 priority / fullscreen 属性，调用方改读 Resource.annotations.priority / _meta.fullscreen
    @cached_property
    def priority(self) -> int | None:
        """
        v0.2 起 priority 不再来自 URI query，统一返回 None。
        Since v0.2, priority is no longer carried in the URI query; always returns None.

        元数据真实来源：MCP Resource.annotations.priority（float [0,1]）。调用方应改读
        Resource.annotations，本属性仅为旧调用点向后兼容（由 sub-issue #4 移除）。
        Real source: MCP Resource.annotations.priority (float [0,1]). Callers should
        switch to Resource.annotations; this attribute exists only for legacy back-compat
        (to be removed by sub-issue #4).
        """
        return None

    @cached_property
    def fullscreen(self) -> bool | None:
        """
        v0.2 起 fullscreen 不再来自 URI query，统一返回 None。
        Since v0.2, fullscreen is no longer carried in the URI query; always returns None.

        元数据真实来源：MCP Resource._meta.fullscreen（bool）。
        Real source: MCP Resource._meta.fullscreen (bool).
        """
        return None

    # -------------------------
    # 构造函数 / Builder
    # -------------------------
    @classmethod
    def build_uri(
        cls,
        *,
        host: str,
        windows: Iterable[str] | None = None,
    ) -> Self:
        """
        通过 host 与 windows（0..N 段路径）构建 WindowURI（v0.2 纯标识符）。
        Build a v0.2 pure-identifier WindowURI from host and windows (0..N path segments).

        所有路径段进行 URL 编码（如 "c/d" -> "c%2Fd"，保持为单段）。
        All path segments are URL-encoded (e.g., "c/d" -> "c%2Fd", kept as a single segment).

        v0.2 起不再接受 priority / fullscreen 参数 —— 元数据由 MCP Server 在
        Resource.annotations / _meta 中声明，详见 v0.2 协议指南 §4.1。
        Since v0.2, priority / fullscreen are not accepted here — metadata is declared by the
        MCP Server on Resource.annotations / _meta (see protocol guide §4.1).
        """
        from urllib.parse import quote

        if not host:
            raise ValueError("host (MCP id) is required")

        # 对每个段进行编码，确保如 "c/d" -> "c%2Fd" 保持为单段
        segs = [quote(p, safe="") for p in (list(windows) if windows else [])]
        path = "/" + "/".join(segs) if segs else ""
        return cls(f"window://{host}{path}")

    # -------------------------
    # Pydantic v2 支持 / Pydantic v2 support
    # -------------------------
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> Any:
        """
        定义 Pydantic 的校验与序列化行为：
        1. 校验：允许字符串或 WindowURI 实例 -> 返回 WindowURI 实例
        2. 序列化：WindowURI 实例 -> 字符串
        Define Pydantic validation and serialization behavior:
        1. Validation: allow str or WindowURI -> return WindowURI
        2. Serialization: WindowURI -> str
        """
        from pydantic_core import core_schema

        def validate(value: str | WindowURI) -> WindowURI:
            if isinstance(value, cls):
                return value
            try:
                return cls(cast(str, value))
            except Exception as e:
                raise ValueError(f"Invalid WindowURI: {value}") from e

        def serialize(value: WindowURI) -> str:
            return str(value)

        return core_schema.no_info_plain_validator_function(
            function=validate,
            serialization=core_schema.plain_serializer_function_ser_schema(function=serialize),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {"type": "string", "format": "uri"}

    # -------------------------
    # 基础访问 / Basic delegates
    # -------------------------
    def __str__(self) -> str:  # noqa: D401
        """返回标准字符串形式 / Return canonical string form"""
        return str(self._url)


def is_window_uri(value: str | AnyUrl | object) -> bool:
    """
    判断给定字符串是否为合法的 WindowURI（window:// 协议）
    Check whether the given string is a valid WindowURI (window:// scheme)

    规则 / Rules (v0.2):
    - 必须为 window scheme
    - 必须包含 host（MCP 唯一标识）
    - URI 含 query 不会判定为非法（仅在解析时记录 WARN 并丢弃）
    """
    try:
        s = str(value)
    except Exception:
        return False
    try:
        _ = WindowURI(s)
        return True
    except Exception:
        return False
