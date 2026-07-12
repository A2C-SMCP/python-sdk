# -*- coding: utf-8 -*-
# filename: bundle_id.py
# @Time    : 2026/07/11
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
BundleID 生成 / 校验 / connection-identity TLV 摘要（单一权威）/ Deterministic BundleID derivation (single source of truth).

协议依据 / Protocol: a2c-smcp-protocol docs/specification/data-structures.md §「MCP Tool 命名与路由（BundleID 模型）」
                     （refs A2C-SMCP/a2c-smcp-protocol#15；规范 commit 95b8553，0.3.0-dev）。

存在意义 / Why this module：
    `bundle_id` 缺省生成 **MUST** 逐字节确定、各 SDK（Python / Rust）产出同一结果——是跨 SDK 硬门槛。
    本模块以实现内置、与宿主无关的算法（Unicode 码点迭代 + 显式 ASCII 类 + SHA-256 摘要）为
    Computer 注册边界（`_arender_and_validate_server`）提供**唯一**权威，杜绝跨 SDK / 跨决策点漂移。
    SHA-256 与 rust 对齐，禁语言内建 hash()（进程级随机化）、禁 base32/64（大小写/padding 变体）。

✅ P0 一致性夹具 / Conformance vectors (LOCKED)：
    TLV 字节帧已由协议仓一致性向量「定死」并**逐字节对拍全绿**（`a2c-smcp-protocol` develop `57c2f9f`，
    16 条，`tests/unit_tests/utils/test_bundle_id_conformance.py`）。rust-sdk#117 回执确认 3 处契约：
      [1] ✅ 类型判别符（"stdio"/"streamable"/"sse"）与普通字符串字段同构，走 u32 长度前缀（非裸拼接）。
      [3] ✅ 顶层帧为判别符起的扁平拼接，无外层长度 / 计数包裹。
      [2] ⚠️ **raw（协议 #17 已定，`57c2f9f`）**：connection-identity 取 **raw / 未注入**配置——
          `${input:*}` / `${env:*}` / secret 占位**按字面**参与摘要，**MUST NOT** 先渲染。本模块对传入
          config 的字段值**原样摘要**（不感知渲染）；「取 raw」由调用方（Computer 注册边界
          `_arender_and_validate_server`）在 `ConfigRender.arender` **之前** derive-on-raw 保证。
"""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from a2c_smcp.computer.mcp_clients.model import MCPServerConfig

# 显式 ASCII 字符类 [A-Za-z0-9_-]（规范 Step 1.1：MUST 用显式类，MUST NOT 用 \w——各语言 Unicode \w 集合不一致）。
# Explicit ASCII class; any non-ASCII code point falls outside and is replaced.
_ALLOWED_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
)
# 折叠连续下划线（含原文 `__`）为单个；**不**折 `-`（规范 Step 1.2）。
_CONSECUTIVE_UNDERSCORE = re.compile(r"_+")
# 合法显式 bundle_id 的字符集断言（[A-Za-z0-9_-]+，`.` 等一律非法）。
_VALID_BUNDLE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

# fallback 前缀（规范 Step 3）：`bundle_` + 16 个小写 hex（= SHA-256 摘要前 8 字节）。
_FALLBACK_PREFIX = "bundle_"


def normalize_name(name: str) -> str:
    """规范化 `name` 为候选 bundle_id（规范 Step 1）；结果可能为空（→ 触发 fallback）。

    Normalize a server ``name`` into a candidate bundle_id (spec Step 1); may return "" (→ fallback).

    步骤（按 **Unicode 码点**迭代，`for c in name`，MUST NOT 按 UTF-8 字节 / grapheme）：
      1. 非 `[A-Za-z0-9_-]` 码点（任何非 ASCII 一律命中）→ `_`；
      2. 折叠连续 `_`（含原文 `__`）为单个，**不**折 `-`；
      3. 裁首尾 `[_-]`；
      4. **不**做大小写折叠（`MyServer` ≠ `myserver`）。
    """
    # Step 1.1 — 逐码点替换（Python `for c in str` 即按码点迭代）
    replaced = "".join(c if c in _ALLOWED_CHARS else "_" for c in name)
    # Step 1.2 — 折叠连续 `_`（含 `__`），不折 `-`
    folded = _CONSECUTIVE_UNDERSCORE.sub("_", replaced)
    # Step 1.3 — 裁首尾 `[_-]`；Step 1.4 — 不做大小写折叠（无操作）
    return folded.strip("_-")


def is_valid_bundle_id(bundle_id: str) -> bool:
    """显式 bundle_id 是否合法：非空、无连续 `__`、字符集 `[A-Za-z0-9_-]`（含 `.` 判非法）。"""
    return bool(bundle_id) and "__" not in bundle_id and _VALID_BUNDLE_ID.match(bundle_id) is not None


def validate_explicit_bundle_id(bundle_id: str) -> str:
    """校验**显式**传入的 bundle_id，合法则原样返回，否则抛 ``ValueError``。

    Validate an explicitly-provided bundle_id; return it unchanged or raise ``ValueError``.

    仅校验显式值——**省略** bundle_id 不算错误（走缺省生成，见 :func:`generate_bundle_id`）。
    """
    if not bundle_id:
        raise ValueError("bundle_id MUST NOT be empty (omit the field entirely to trigger default generation)")
    if "__" in bundle_id:
        raise ValueError(
            f"bundle_id MUST NOT contain consecutive underscores '__' (reserved separator between "
            f"bundle_id and tool name): {bundle_id!r}",
        )
    if _VALID_BUNDLE_ID.match(bundle_id) is None:
        raise ValueError(f"bundle_id charset MUST be [A-Za-z0-9_-] (e.g. '.' is invalid): {bundle_id!r}")
    return bundle_id


# ————————————————————————————————————————————————————————————————————————————
# connection-identity TLV 字节帧（规范 §connection-identity）/ TLV byte frame
#
# 为避免 JSON 跨语言序列化漂移，用长度前缀（TLV）字节帧、MUST NOT 用 JSON。所有多字节整数为 **u32 大端**。
# All multi-byte integers are u32 big-endian; strings are UTF-8. See ASSUMPTION[1..3] in the module docstring.
# ————————————————————————————————————————————————————————————————————————————


def _str_field(value: str) -> bytes:
    """字符串字段 = u32 大端字节长度 ‖ UTF-8 字节。"""
    raw = value.encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def _list_field(items: Sequence[str]) -> bytes:
    """列表 = u32 大端元素数 ‖ 元素*（**保序**——参数顺序有语义）。空列表 → 计数 0。"""
    out = struct.pack(">I", len(items))
    for item in items:
        out += _str_field(item)
    return out


def _map_field(mapping: Mapping[str, str] | None) -> bytes:
    """映射 = u32 大端条目数 ‖ (key ‖ value)*，**按 key 码点序升序**。空 / None → 计数 0。

    Python ``sorted`` 按码点排序；规范注明「UTF-8 字节序 = 码点序」，二者对全部合法 Unicode 等价。
    """
    items = sorted((mapping or {}).items(), key=lambda kv: kv[0])
    out = struct.pack(">I", len(items))
    for key, value in items:
        out += _str_field(key) + _str_field(value)
    return out


def connection_identity_tlv(config: MCPServerConfig) -> bytes:
    """构造该 Server 的 connection-identity TLV 字节串（fallback 摘要输入，规范 §connection-identity）。

    仅纳入**连接建立字段**；**排除** disabled/tool_meta/forbidden_tools/vrl/env_file/cwd/encoding/timeout 系列。
      - stdio           → 判别符 ‖ command ‖ args(列表,保序) ‖ env(映射,按 key 排序)
      - streamable/sse  → 判别符 ‖ url ‖ headers(映射,按 key 排序)

    判别符取协议 §9.1 小写 type（Http 变体在本 SDK 即记 ``streamable``，与规范一致）。
    字节帧（rust-sdk#117 回执确认 + 协议向量 57c2f9f 锁定）：判别符走 :func:`_str_field`（u32 长度前缀）；
    无外层包裹、判别符起扁平拼接。**raw（#17）**：本函数对 ``config`` 字段值原样摘要——调用方须传**未渲染**
    config（占位字面），故 ``${input:*}`` 参与摘要、bundle_id 跨渲染阶段稳定（见模块 docstring）。
    """
    # 逐分支访问 config.server_parameters（直接比对 config.type 以触发判别联合窄化；先提取 params 会丢失窄化）。
    if config.type == "stdio":
        stdio_params = config.server_parameters
        # ASSUMPTION[1] discriminator as length-prefixed string field
        return (
            _str_field("stdio")
            + _str_field(stdio_params.command)
            + _list_field(list(stdio_params.args or []))
            + _map_field(stdio_params.env)
        )
    if config.type in ("streamable", "sse"):
        http_params = config.server_parameters
        # config.type 已窄化为 "streamable" | "sse"，即协议 §9.1 判别符（Http 变体在本 SDK 即 "streamable"）。
        return _str_field(config.type) + _str_field(str(http_params.url)) + _map_field(http_params.headers)
    # 未知 type：正常流程不可达（MCPServerConfig 为封闭联合）；防御性硬失败暴露 SDK bug。
    raise ValueError(f"Unknown MCP server type for bundle_id fallback digest: {config.type!r}")  # pragma: no cover


def generate_bundle_id(config: MCPServerConfig) -> str:
    """从 `name`（+ 连接身份）**确定性缺省生成** bundle_id（规范 Step 1→3），忽略任何显式 bundle_id。

    Deterministically derive a bundle_id from ``name`` (+ connection identity); ignores any explicit value.
    这是一致性测试向量对拍的入口：``(name, connection config) → 期望 bundle_id``。
    """
    normalized = normalize_name(config.name)
    if normalized:  # Step 2 — 非空即取
        return normalized
    # Step 3 — fallback：bundle_ + lowercase_hex(SHA-256(TLV)[:8])（前 8 字节 = 16 hex）
    digest8 = hashlib.sha256(connection_identity_tlv(config)).digest()[:8]
    return _FALLBACK_PREFIX + digest8.hex()  # .hex() 已为小写


def resolve_bundle_id(config: MCPServerConfig) -> str:
    """解析该 Server 的最终 bundle_id：显式值优先（经校验），否则缺省生成。

    Resolve the final bundle_id: explicit (validated) wins, else deterministic default generation.
    在 Computer 注册边界（derive-on-load）调用；**MUST NOT** 回写配置源（如 mcp.json）。
    """
    explicit = getattr(config, "bundle_id", None)
    if explicit is not None:
        # 显式值：Pydantic field_validator 已校验；此处防御性再校验，保证注册边界单一权威。
        return validate_explicit_bundle_id(explicit)
    return generate_bundle_id(config)
