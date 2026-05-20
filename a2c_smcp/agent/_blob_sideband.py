# -*- coding: utf-8 -*-
# filename: _blob_sideband.py
# @Author  : JQQ
# @Software: PyCharm

"""
Agent 端 blob 旁路纯函数收敛（async + sync 共享）/ Pure-function helpers for Agent-side blob sideband.

抽取自 ``client.py`` / ``sync_client.py`` 中 ``_resolve_tool_call_binary_sideband`` 与 ``get_skill``
``blob_handle → text body`` 解码的逐字镜像逻辑——沿用 #30 范式（``_request_builders.py``），把无 I/O
差异的纯结构变换收敛到本模块，async/sync 各自仅保留 ``await drain_blob`` vs ``drain_blob_sync``
这一行差异的骨架。

Extracted from the verbatim async/sync mirrors in ``client.py`` / ``sync_client.py``:
``_resolve_tool_call_binary_sideband`` and the ``blob_handle → text body`` decode block in
``get_skill``. Follows the #30 ``_request_builders.py`` pattern — pure structural transforms
collected here so the async/sync sites differ only by the ``await drain_blob`` vs
``drain_blob_sync`` line.

协议依据 / Protocol:
  - blob-transfer.md §5（生产者通道接入契约）
  - data-structures.md §BlobHandle（``CallToolResult`` content item ``_meta.a2c_blob_handle`` 旁路）
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any


def extract_sideband_handles(raw: Any) -> Iterator[tuple[dict[str, Any], dict[str, Any], str]]:
    """枚举 ``CallToolResult.content`` 中携带 ``_meta.a2c_blob_handle`` 的 item / Enumerate sideband items.

    Yields:
        ``(item, meta, handle)`` 三元组：
          - ``item``：content 列表的单个 dict 条目（可变，调用方就地修改 data/blob/_meta）
          - ``meta``：``item["_meta"]``（已校验为 dict）
          - ``handle``：``a2c_blob_handle`` 字符串（已校验非空 str）

    非 dict raw / 非 list content / 非 dict item / 非 dict meta / 无 handle 的 item 一律跳过.
    Returns nothing for non-dict raw, non-list content, non-dict item, non-dict meta, or missing handle.
    """
    if not isinstance(raw, dict):
        return
    content = raw.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        meta = item.get("_meta")
        if not isinstance(meta, dict):
            continue
        handle = meta.get("a2c_blob_handle")
        if not isinstance(handle, str) or not handle:
            continue
        yield item, meta, handle


def inject_payload_into_content_item(item: dict[str, Any], meta: dict[str, Any], payload: bytes) -> None:
    """将 drain 后的字节回填到 content item 并清理 ``_meta.a2c_*`` 字段.

    Inject drained bytes back into the content item and strip ``_meta.a2c_*`` keys.

    回填规则 / Injection rules (data-structures.md §BlobHandle):
      - ImageContent / AudioContent (``type ∈ {"image","audio"}`` 或顶层 ``data`` 键存在) → 写 ``item["data"]``
      - EmbeddedResource.BlobResourceContents (``item["resource"]["blob"]`` 存在) → 写 ``item["resource"]["blob"]``
      - 兜底：未知 shape 时顶层写 ``data``，避免静默丢字节

    清理 / Cleanup: 移除所有 ``a2c_*`` 前缀的 ``_meta`` 键；若 ``_meta`` 清空则连键一并 pop——
    旁路完成后 Agent 不应感知 sideband 机制（透明性契约）.
    Strip all ``a2c_*``-prefixed keys from ``_meta``; pop ``_meta`` if emptied so the Agent sees a
    normal inline result (transparency invariant).
    """
    b64 = base64.b64encode(payload).decode("ascii")
    if "data" in item or item.get("type") in {"image", "audio"}:
        item["data"] = b64
    elif "resource" in item and isinstance(item["resource"], dict) and "blob" in item["resource"]:
        item["resource"]["blob"] = b64
    else:
        # 兜底：未知 content shape 时顶层写 data，避免静默丢字节
        # Fallback for unknown content shape: write top-level data to avoid silent byte loss
        item["data"] = b64
    # 清理 _meta.a2c_* 字段 / Strip a2c_* keys
    for k in [k for k in meta if k.startswith("a2c_")]:
        meta.pop(k, None)
    if not meta:
        item.pop("_meta", None)


def decode_text_body(payload: bytes) -> str | None:
    """文本 MIME 的 blob 字节解码回 ``body`` 字符串；解码失败返回 ``None`` 让调用方保留 ``blob_handle``.

    Decode bytes back to a text ``body``; return ``None`` on UTF-8 failure so the caller can keep
    the original ``blob_handle`` for binary-safe handling (conservative fallback).
    """
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
