# -*- coding: utf-8 -*-
# filename: dpe_uri.py
# @Time    : 2026/04/28 14:00
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
DPE URI 解析器 / DPEURI parser (v0.2 协议)

协议说明 / Protocol (v0.2 §4.2):
- scheme 固定为 dpe，URI 形如 dpe://host/doc-ref —— 纯文档标识符
  Scheme is fixed to 'dpe'; URI is a pure document identifier: dpe://host/doc-ref
- host 为 MCP 的唯一标识（建议反向域名风格 SHOULD），跨 MCP Server MUST 唯一
  host is the MCP unique id (reverse-DNS style recommended SHOULD); MUST be unique across MCP Servers
- doc-ref 为 host 之后第一个 '/' 起到 URI 末尾的整段路径，整体作为文档唯一标识；
  **不再有 sub-path（pages/N、elements/ID）概念** —— Page / Element 在 DPE JSON 内部表达
  doc-ref is the full path from the first '/' after host to URI end, used as the whole document identifier;
  **no more sub-path concepts** — Page / Element live inside the DPE JSON body
- 拒绝 `dpe://host` 形式（无 doc-ref）；解析失败对应错误码 4012
  Reject `dpe://host` form (no doc-ref); parse failure maps to error code 4012

URI query / fragment 处理分层（协议 §F4 Postel's law）：
URI query / fragment handling layers (Postel's law):
- Computer 解析层（本类）：解析时若含 query / fragment → WARN 日志后丢弃，不抛异常
  Computer parser layer (this class): on query/fragment → WARN + drop, no exception
- Agent SDK 构造层（build）：不接受 query / fragment 参数，纯标识符
  Agent SDK builder layer (build): does not accept query/fragment params

Pydantic v2 兼容：支持字符串 <-> DPEURI 的校验与序列化
Pydantic v2 compatibility: supports string <-> DPEURI validation and serialization
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cached_property
from typing import Any, Self, cast
from urllib.parse import quote, unquote

from pydantic import AnyUrl, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from yarl import URL as YURL

from a2c_smcp.utils.logger import get_logger

logger = get_logger("utils.dpe_uri")

# 反向域名风格 lint 模式（SHOULD）：至少 3 段小写字母数字 + 连字符，点分隔
# Reverse-DNS lint pattern (SHOULD): at least 3 lowercase alphanumeric+hyphen segments, dot-separated
# 例如 / e.g.: com.example.docs ✅ / docs ⚠️ / DOCS.EXAMPLE.COM ⚠️ / docs_server ⚠️
_REVERSE_DOMAIN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*){2,}$")


class DPEURI:
    """
    DPEURI 用于解析与构建 dpe:// 协议的 URI（v0.2 纯文档标识符形态）。
    DPEURI parses and builds dpe:// URIs (v0.2 pure-document-identifier form).
    """

    def __init__(self, uri: str) -> None:
        """
        使用 yarl.URL 解析并缓存 URL 组件。
        Parse and cache URL components via yarl.URL.

        v0.2 协议层容错：含 query / fragment 的 URI 仍可解析成功，但 query / fragment
        部分会被丢弃并记录 WARN（Computer 解析层 §F4 Postel's law）。
        v0.2 tolerance: URIs with query/fragment still parse, but query/fragment are
        dropped with a WARN log (Computer parser layer §F4 Postel's law).

        Raises:
            ValueError: scheme 非 dpe / scheme is not 'dpe'
            ValueError: doc-ref 为空（对应错误码 4012）/ empty doc-ref (maps to 4012)
            IndexError: host 缺失 / missing host
        """
        self._url = YURL(uri)
        if self._url.scheme != "dpe":
            raise ValueError(f"Invalid URI scheme: {self._url.scheme}, uri={uri}")
        if not self._url.host:
            raise IndexError(f"Missing host (MCP id) in URI: {uri}")

        # doc-ref 必须非空（拒绝 dpe://host 与 dpe://host/ 两种形式）
        # doc-ref MUST be non-empty (reject both `dpe://host` and `dpe://host/`)
        raw_path = self._url.raw_path.lstrip("/")
        if not raw_path:
            raise ValueError(f"Missing doc-ref in DPE URI (maps to 4012): {uri}")

        if self._url.query_string:
            logger.warning(
                "DPEURI v0.2 不再支持 query 元数据，已丢弃 query 部分。"
                "DPEURI v0.2 no longer supports query metadata; query has been dropped. "
                f"uri={uri}",
            )
        if self._url.fragment:
            logger.warning(
                "DPEURI v0.2 不支持 fragment，已丢弃 fragment 部分。"
                "DPEURI v0.2 does not support fragments; fragment has been dropped. "
                f"uri={uri}",
            )

        # host 反向域名风格 SHOULD lint（不阻塞解析，仅 WARN）
        # Reverse-DNS host style SHOULD lint (no block, WARN only)
        host = str(self._url.host)
        if not _REVERSE_DOMAIN_RE.match(host):
            logger.warning(
                "DPEURI host 不符合反向域名规范 (SHOULD)：建议 <tld>.<organization>.<service>[.<sub>]* 风格。"
                "DPEURI host does not match reverse-DNS lint (SHOULD). "
                f"host={host}, uri={uri}",
            )

    # -------------------------
    # 基础属性 / Basic properties
    # -------------------------
    @cached_property
    def host(self) -> str:
        """
        获取 MCP 唯一标识（host）
        Get MCP unique identifier (host)
        """
        return str(self._url.host)

    @cached_property
    def doc_ref(self) -> str:
        """
        文档唯一标识：host 之后整段 path，已逐段解码。
        Document identifier: full path after host, segment-wise decoded.

        例如 / e.g.:
            dpe://h/reports/2026/annual -> "reports/2026/annual"
            dpe://h/a%2Fb              -> "a/b"  (单段，URL 编码 / single segment)
        """
        return "/".join(self.doc_ref_parts)

    @cached_property
    def doc_ref_parts(self) -> list[str]:
        """
        doc-ref 分段列表：基于原始编码 path 分段后逐段解码，
        保证如 "c%2Fd" -> "c/d" 保持为单段。
        doc-ref segments: split raw-encoded path then decode each segment,
        keeping "c%2Fd" as one "c/d" part.
        """
        raw = self._url.raw_path.lstrip("/")
        if not raw:
            return []
        return [unquote(seg) for seg in raw.split("/")]

    # -------------------------
    # 构造函数 / Builder
    # -------------------------
    @classmethod
    def build(
        cls,
        *,
        host: str,
        doc_ref: str | Iterable[str],
    ) -> Self:
        """
        通过 host 与 doc-ref 构建 DPEURI（v0.2 纯文档标识符）。
        Build a v0.2 pure-document-identifier DPEURI from host and doc-ref.

        - `doc_ref: str` —— 视为整体路径，自动按 '/' 分段并对每段做 URL 编码
        - `doc_ref: Iterable[str]` —— 视为已分段路径，每段独立 URL 编码
          （如此 "c/d" 段保持为单段 "c%2Fd"）

        所有 query / fragment 在构造层不被接受 —— Agent SDK 构造层 MUST 严格（§F4）。

        Args:
            host: MCP 唯一标识，必须非空
            doc_ref: 文档标识，str 自动分段；Iterable[str] 视为已分段

        Returns:
            DPEURI 实例

        Raises:
            ValueError: host 为空 / doc_ref 为空
        """
        if not host:
            raise ValueError("host (MCP id) is required")

        if isinstance(doc_ref, str):
            segs_raw: list[str] = [s for s in doc_ref.split("/") if s != ""]
        else:
            segs_raw = list(doc_ref)

        if not segs_raw:
            raise ValueError("doc_ref is required (empty doc_ref maps to error code 4012)")

        segs = [quote(p, safe="") for p in segs_raw]
        return cls(f"dpe://{host}/" + "/".join(segs))

    # -------------------------
    # Pydantic v2 支持 / Pydantic v2 support
    # -------------------------
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> Any:
        """
        定义 Pydantic 校验与序列化行为：
        1. 校验：允许字符串或 DPEURI 实例 -> 返回 DPEURI 实例
        2. 序列化：DPEURI 实例 -> 字符串
        Define Pydantic validation and serialization:
        1. Validation: allow str or DPEURI -> return DPEURI
        2. Serialization: DPEURI -> str
        """
        from pydantic_core import core_schema

        def validate(value: str | DPEURI) -> DPEURI:
            if isinstance(value, cls):
                return value
            try:
                return cls(cast(str, value))
            except Exception as e:
                raise ValueError(f"Invalid DPEURI: {value}") from e

        def serialize(value: DPEURI) -> str:
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


def is_dpe_uri(value: str | AnyUrl | object) -> bool:
    """
    判断给定字符串是否为合法的 DPEURI（dpe:// 协议）
    Check whether the given string is a valid DPEURI (dpe:// scheme)

    规则 / Rules (v0.2):
    - 必须为 dpe scheme
    - 必须包含 host（MCP 唯一标识）
    - 必须包含非空 doc-ref（拒绝 dpe://host 形式）
    - URI 含 query / fragment 不会判定为非法（仅在解析时记录 WARN 并丢弃）
    """
    try:
        s = str(value)
    except Exception:
        return False
    try:
        _ = DPEURI(s)
        return True
    except Exception:
        return False
