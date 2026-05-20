# -*- coding: utf-8 -*-
# filename: test_handle.py
# @Author  : JQQ
# @Software: PyCharm

"""
``blob_handle`` 编解码单元测试 / Unit tests for ``blob_handle`` codec.

协议依据 / Protocol: a2c-smcp-protocol blob-transfer.md §1 (opacity) / §5.4 (handle untrusted).
设计依据 / Design: docs/design-0.2.1-skill-computer-management.md §4.3.
"""

from __future__ import annotations

import base64

import msgpack
import pytest

from a2c_smcp.computer.blob.handle import (
    HANDLE_KIND_SKILL,
    HANDLE_KIND_TOOLSPOOL,
    HANDLE_VERSION,
    BlobHandleInvalidError,
    decode_blob_handle,
    encode_skill_handle,
    encode_toolspool_handle,
)


class TestEncodeRoundTrip:
    """编码 → 解码完整 round-trip / Encode → decode full round-trip."""

    def test_skill_handle_round_trip(self) -> None:
        handle = encode_skill_handle(name="mcp:tfrobot-tools:code-review", rel_path="docs/intro.md")
        kind, payload = decode_blob_handle(handle)
        assert kind == HANDLE_KIND_SKILL
        assert payload == {"name": "mcp:tfrobot-tools:code-review", "rel_path": "docs/intro.md"}

    def test_toolspool_handle_round_trip(self) -> None:
        cid = "a" * 64
        handle = encode_toolspool_handle(cid=cid, mime="image/png")
        kind, payload = decode_blob_handle(handle)
        assert kind == HANDLE_KIND_TOOLSPOOL
        assert payload == {"cid": cid, "mime": "image/png"}

    def test_base64url_no_padding(self) -> None:
        """base64url 编码无 ``=`` padding，URL 安全 / no padding, URL-safe."""
        handle = encode_skill_handle(name="user:x:y", rel_path="SKILL.md")
        assert "=" not in handle
        # base64url charset：A-Z a-z 0-9 - _
        assert all(c.isalnum() or c in "-_" for c in handle)


class TestDecodeRejectsInvalidHandles:
    """协议 §5.4「不信任句柄内容」反例测试 / Adversarial tests for "do not trust handle contents"."""

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(BlobHandleInvalidError, match="empty"):
            decode_blob_handle("")

    def test_non_base64url_rejected(self) -> None:
        with pytest.raises(BlobHandleInvalidError):
            decode_blob_handle("not!!!valid@@@base64")

    def test_random_bytes_in_base64url_rejected(self) -> None:
        """合法 base64url 但非 msgpack 内容 → 解码失败 / Valid base64url, non-msgpack content."""
        garbage = base64.urlsafe_b64encode(b"\xff\xfe\xfd not msgpack").rstrip(b"=").decode()
        with pytest.raises(BlobHandleInvalidError, match="msgpack"):
            decode_blob_handle(garbage)

    def test_msgpack_non_dict_rejected(self) -> None:
        """msgpack 内容为 list/int/str → 句柄结构错 / msgpack non-dict payloads."""
        for non_dict in ([1, 2, 3], 42, "hello"):
            packed = msgpack.packb(non_dict, use_bin_type=True)
            assert packed is not None
            handle = base64.urlsafe_b64encode(packed).rstrip(b"=").decode()
            with pytest.raises(BlobHandleInvalidError, match="mapping"):
                decode_blob_handle(handle)

    def test_unknown_version_rejected(self) -> None:
        """``v`` 未来版本号 → 拒绝（不前向兼容；解析方 MUST 容忍未知值兜底）.
        Unknown future version rejected; parsers must default to fail-safe."""
        packed = msgpack.packb({"v": 9999, "kind": "skill", "name": "x", "rel_path": "y"}, use_bin_type=True)
        assert packed is not None
        handle = base64.urlsafe_b64encode(packed).rstrip(b"=").decode()
        with pytest.raises(BlobHandleInvalidError, match="version"):
            decode_blob_handle(handle)

    def test_unknown_kind_rejected(self) -> None:
        packed = msgpack.packb({"v": HANDLE_VERSION, "kind": "unknown_kind", "x": 1}, use_bin_type=True)
        assert packed is not None
        handle = base64.urlsafe_b64encode(packed).rstrip(b"=").decode()
        with pytest.raises(BlobHandleInvalidError, match="kind"):
            decode_blob_handle(handle)

    def test_skill_handle_missing_fields_rejected(self) -> None:
        # 缺 name / Missing name
        packed = msgpack.packb({"v": HANDLE_VERSION, "kind": "skill", "rel_path": "x"}, use_bin_type=True)
        assert packed is not None
        handle = base64.urlsafe_b64encode(packed).rstrip(b"=").decode()
        with pytest.raises(BlobHandleInvalidError, match="name"):
            decode_blob_handle(handle)

    def test_toolspool_handle_missing_fields_rejected(self) -> None:
        # 缺 cid / Missing cid
        packed = msgpack.packb({"v": HANDLE_VERSION, "kind": "toolspool", "mime": "image/png"}, use_bin_type=True)
        assert packed is not None
        handle = base64.urlsafe_b64encode(packed).rstrip(b"=").decode()
        with pytest.raises(BlobHandleInvalidError, match="cid"):
            decode_blob_handle(handle)

    def test_tampered_handle_truncation_rejected(self) -> None:
        """截断已合法的句柄 → 解码失败（防伪造测试）/ Truncated valid handle → rejected."""
        handle = encode_skill_handle(name="user:x:y", rel_path="SKILL.md")
        tampered = handle[:-5]  # 截尾 / chop tail
        with pytest.raises(BlobHandleInvalidError):
            decode_blob_handle(tampered)

    def test_non_string_handle_rejected(self) -> None:
        for non_str in (None, 42, [], {}):
            with pytest.raises(BlobHandleInvalidError, match="str"):
                decode_blob_handle(non_str)  # type: ignore[arg-type]
