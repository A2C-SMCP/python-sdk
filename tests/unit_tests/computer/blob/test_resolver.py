# -*- coding: utf-8 -*-
# filename: test_resolver.py
# @Author  : JQQ
# @Software: PyCharm

"""
``BlobResolver`` 协议 + 内置 resolver 单元测试 / Tests for ``BlobResolver`` Protocol + built-ins.

协议依据 / Protocol: blob-transfer.md §5.4「Computer 解析时 MUST 重跑铸造通道边界校验」.
设计依据 / Design: docs/design-0.2.1-skill-computer-management.md §4.3.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from a2c_smcp.computer.blob.handle import BlobHandleError, BlobHandleGoneError, BlobHandleInvalidError
from a2c_smcp.computer.blob.resolver import (
    BlobResolver,
    ResolvedBlob,
    SkillBlobResolverPending,
    ToolspoolBlobResolver,
)
from a2c_smcp.computer.blob.toolspool import ToolspoolBlobStore


class TestToolspoolBlobResolver:
    def test_resolve_round_trip(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"hello world", "text/plain")
        resolver = ToolspoolBlobResolver(store)
        resolved = resolver.resolve({"cid": cid, "mime": "text/plain"})
        assert isinstance(resolved, ResolvedBlob)
        assert resolved.payload == b"hello world"
        assert resolved.mime == "text/plain"
        assert resolved.total_size == 11
        assert resolved.sha256 == hashlib.sha256(b"hello world").hexdigest()

    def test_resolve_gone_when_cid_missing(self, tmp_path: Path) -> None:
        """cid 格式合法但物理文件不在 → ``4018 gone`` / Valid cid, file absent → gone."""
        store = ToolspoolBlobStore(tmp_path)
        resolver = ToolspoolBlobResolver(store)
        with pytest.raises(BlobHandleGoneError):
            resolver.resolve({"cid": "a" * 64, "mime": "image/png"})

    def test_resolve_invalid_handle_on_forged_cid(self, tmp_path: Path) -> None:
        """伪造句柄注入越界 cid → ``4018 invalid_handle`` / Forged cid → invalid_handle.

        反例测试：句柄不可被信任，即使 resolver 拿到也不应越权。
        Defense-in-depth: handle untrusted, resolver MUST whitelist cid."""
        store = ToolspoolBlobStore(tmp_path)
        resolver = ToolspoolBlobResolver(store)
        for forged_cid in ("..", "../../etc/passwd", "not-hex", ""):
            with pytest.raises(BlobHandleInvalidError):
                resolver.resolve({"cid": forged_cid, "mime": "image/png"})

    def test_resolve_uses_handle_mime_not_disk_mime(self, tmp_path: Path) -> None:
        """handle 内 mime 是权威；disk sidecar 仅诊断 / handle mime is authoritative, disk is diagnostic."""
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"binary", "application/octet-stream")
        resolver = ToolspoolBlobResolver(store)
        resolved = resolver.resolve({"cid": cid, "mime": "image/png"})  # 故意不同 / intentionally different
        assert resolved.mime == "image/png"

    def test_protocol_runtime_checkable(self, tmp_path: Path) -> None:
        """ToolspoolBlobResolver 实现 BlobResolver Protocol（runtime_checkable）.
        ToolspoolBlobResolver satisfies the runtime_checkable BlobResolver Protocol."""
        store = ToolspoolBlobStore(tmp_path)
        resolver = ToolspoolBlobResolver(store)
        assert isinstance(resolver, BlobResolver)


class TestSkillBlobResolverPending:
    """``kind=skill`` 占位实现：本 PR 边界，#39 接管后替换.
    Placeholder for ``kind=skill``; replaced by #39 wiring up Registry + sandbox."""

    def test_resolve_raises_forbidden(self) -> None:
        """未接入即视为「铸造通道授权撤销」→ forbidden（保守，符合协议「不信任句柄」边界）.
        Until #39 plugs in, treat as channel revoked → forbidden (conservative, protocol-aligned)."""
        resolver = SkillBlobResolverPending()
        with pytest.raises(BlobHandleError) as exc_info:
            resolver.resolve({"name": "user:x:y", "rel_path": "SKILL.md"})
        assert exc_info.value.reason == "forbidden"

    def test_protocol_runtime_checkable(self) -> None:
        resolver = SkillBlobResolverPending()
        assert isinstance(resolver, BlobResolver)
