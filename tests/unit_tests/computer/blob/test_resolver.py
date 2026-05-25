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

from a2c_smcp.computer.blob.handle import (
    BlobHandleForbiddenError,
    BlobHandleGoneError,
    BlobHandleInvalidError,
)
from a2c_smcp.computer.blob.resolver import (
    BlobResolver,
    ResolvedBlob,
    SkillBlobResolver,
    ToolspoolBlobResolver,
)
from a2c_smcp.computer.blob.toolspool import ToolspoolBlobStore
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.resource import DEFAULT_SKILL_MIME
from a2c_smcp.smcp import A2CSkillRef

_SKILL_MD = "---\nname: demo\nversion: 1.2.3\n---\n# Demo\n\nbody line\n"
_SKILL_BODY = "# Demo\n\nbody line\n"  # frontmatter 剥离后（闭合 --- 行之后）/ body after closing fence
_NOTE_BYTES = b"plain note bytes\n"


class TestToolspoolBlobResolver:
    def test_resolve_round_trip(self, tmp_path: Path) -> None:
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"hello world", "text/plain")
        resolver = ToolspoolBlobResolver(store)
        resolved = resolver.resolve({"cid": cid, "mime": "text/plain"})
        assert isinstance(resolved, ResolvedBlob)
        # v0.2.1 #51: 走 lazy slice 接口断言（.payload shim 保留但 deprecated）
        # Assert via lazy slice (the .payload shim is retained but deprecated).
        assert resolved.slice(0, resolved.total_size) == b"hello world"
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


class TestSkillBlobResolver:
    """``kind=skill`` 解析器：name→Registry 解析包根 → rel_path 重跑沙箱 → 惰性切片（#66）.

    协议依据 / Protocol: blob-transfer.md §5.4「重跑铸造通道边界校验，不信任句柄内容」；
    skill.md §9 安全模型；§4.4「资源字节三处一致」。
    """

    @pytest.fixture
    def registry(self, tmp_path: Path) -> SkillRegistry:
        """构造一个含 SKILL.md / 子资源 / .skillenv 的包根，注册进 Registry。"""
        pkg = tmp_path / "home" / "mcp" / "srv" / "demo"
        (pkg / "references").mkdir(parents=True)
        (pkg / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
        (pkg / "references" / "note.txt").write_bytes(_NOTE_BYTES)
        (pkg / ".skillenv").write_text("SECRET=1\n", encoding="utf-8")
        reg = SkillRegistry()
        ref: A2CSkillRef = {"name": "mcp:srv:demo", "source": "mcp:srv", "path": str(pkg)}
        assert reg.register(ref) is True
        return reg

    def test_resolve_skill_md_strips_frontmatter(self, registry: SkillRegistry) -> None:
        """SKILL.md：消费字节 = frontmatter 剥离后 body；sha256/total_size 与铸造期一致。"""
        resolver = SkillBlobResolver(registry)
        resolved = resolver.resolve({"name": "mcp:srv:demo", "rel_path": "SKILL.md"})
        assert isinstance(resolved, ResolvedBlob)
        expected = _SKILL_BODY.encode("utf-8")
        assert resolved.slice(0, resolved.total_size) == expected
        assert resolved.total_size == len(expected)
        assert resolved.sha256 == hashlib.sha256(expected).hexdigest()
        assert resolved.mime == DEFAULT_SKILL_MIME

    def test_resolve_subresource_raw_bytes(self, registry: SkillRegistry) -> None:
        """子资源：原始字节（不剥 frontmatter）；sha256 基于原始字节。"""
        resolver = SkillBlobResolver(registry)
        resolved = resolver.resolve({"name": "mcp:srv:demo", "rel_path": "references/note.txt"})
        assert resolved.slice(0, resolved.total_size) == _NOTE_BYTES
        assert resolved.total_size == len(_NOTE_BYTES)
        assert resolved.sha256 == hashlib.sha256(_NOTE_BYTES).hexdigest()

    def test_resolve_gone_when_name_unregistered(self, registry: SkillRegistry) -> None:
        """name 未注册 → ``4018 gone``（铸造通道授权撤销）。"""
        resolver = SkillBlobResolver(registry)
        with pytest.raises(BlobHandleGoneError):
            resolver.resolve({"name": "mcp:srv:nope", "rel_path": "SKILL.md"})

    def test_resolve_gone_when_orphaned(self, registry: SkillRegistry) -> None:
        """name 已孤儿 → ``4018 gone``（source 消失，不可服务）。"""
        registry.mark_orphan("mcp:srv:demo")
        resolver = SkillBlobResolver(registry)
        with pytest.raises(BlobHandleGoneError):
            resolver.resolve({"name": "mcp:srv:demo", "rel_path": "SKILL.md"})

    def test_resolve_gone_when_file_missing(self, registry: SkillRegistry) -> None:
        """rel_path 不存在（源文件铸造后消失）→ not_found → ``4018 gone``。"""
        resolver = SkillBlobResolver(registry)
        with pytest.raises(BlobHandleGoneError):
            resolver.resolve({"name": "mcp:srv:demo", "rel_path": "references/missing.txt"})

    @pytest.mark.parametrize("rel_path", ["../outside.txt", "../../etc/passwd", "references/../../escape"])
    def test_resolve_forbidden_on_traversal(self, registry: SkillRegistry, rel_path: str) -> None:
        """句柄内 rel_path 穿越 → 重跑沙箱拒绝 → ``4018 forbidden``（不信任句柄）。"""
        resolver = SkillBlobResolver(registry)
        with pytest.raises(BlobHandleForbiddenError):
            resolver.resolve({"name": "mcp:srv:demo", "rel_path": rel_path})

    def test_resolve_forbidden_on_skillenv(self, registry: SkillRegistry) -> None:
        """``.skillenv`` 任何路径命中 → ``4018 forbidden``（即使句柄声称该路径）。"""
        resolver = SkillBlobResolver(registry)
        with pytest.raises(BlobHandleForbiddenError):
            resolver.resolve({"name": "mcp:srv:demo", "rel_path": ".skillenv"})

    def test_handle_path_is_re_sandboxed_not_trusted(self, registry: SkillRegistry, tmp_path: Path) -> None:
        """反例：伪造句柄注入绝对路径逃逸 → 重跑沙箱拒绝，绝不按句柄路径直读。"""
        resolver = SkillBlobResolver(registry)
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("TOPSECRET", encoding="utf-8")
        with pytest.raises(BlobHandleForbiddenError):
            resolver.resolve({"name": "mcp:srv:demo", "rel_path": str(secret)})

    def test_protocol_runtime_checkable(self) -> None:
        resolver = SkillBlobResolver(SkillRegistry())
        assert isinstance(resolver, BlobResolver)


class TestResolvedBlobLazySlice:
    """``ResolvedBlob.slice()`` 边界契约（v0.2.1 #51）/ Boundary contract for ``slice()``.

    覆盖：中段切片 / EOF probe / 越界 raise / length 自动截断 / 负数防御 / 兼容 shim.
    """

    @pytest.fixture
    def resolved(self, tmp_path: Path) -> ResolvedBlob:
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"hello world", "text/plain")  # 11 bytes
        return ToolspoolBlobResolver(store).resolve({"cid": cid, "mime": "text/plain"})

    def test_mid_stream_slice(self, resolved: ResolvedBlob) -> None:
        assert resolved.slice(2, 4) == b"llo "

    def test_end_aligned_slice_at_eof(self, resolved: ResolvedBlob) -> None:
        assert resolved.slice(5, 6) == b" world"

    def test_offset_at_total_size_returns_empty(self, resolved: ResolvedBlob) -> None:
        """EOF probe semantics: offset == total_size returns b"" (NOT a range error)."""
        assert resolved.slice(11, 100) == b""

    def test_offset_beyond_total_size_raises(self, resolved: ResolvedBlob) -> None:
        with pytest.raises(ValueError, match="> total_size"):
            resolved.slice(12, 1)

    def test_length_zero_returns_empty(self, resolved: ResolvedBlob) -> None:
        assert resolved.slice(3, 0) == b""

    def test_length_over_remaining_auto_truncates(self, resolved: ResolvedBlob) -> None:
        """length 超出剩余字节自动截断（不 raise）/ length over remaining auto-truncates."""
        assert resolved.slice(8, 999) == b"rld"

    def test_negative_offset_raises(self, resolved: ResolvedBlob) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            resolved.slice(-1, 5)

    def test_negative_length_raises(self, resolved: ResolvedBlob) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            resolved.slice(0, -1)

    def test_payload_shim_returns_full_bytes(self, resolved: ResolvedBlob) -> None:
        """向后兼容 shim：.payload 等价 slice(0, total_size).
        Back-compat shim: .payload equivalent to slice(0, total_size)."""
        assert resolved.payload == b"hello world"


class TestLazyMemoryFootprint:
    """惰性切片不变量（v0.2.1 #51）：resolve() 不打开 blob body 文件.
    Lazy invariant: ``resolve()`` reads metadata only; never opens the blob body file."""

    def test_resolve_does_not_open_blob_body(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """resolve() 仅读 stat + mime sidecar；不打开 blob body 文件。
        ``Path.read_text`` / ``Path.stat`` 走 ``os.open``，监控 ``builtins.open`` 不受干扰。
        """
        store = ToolspoolBlobStore(tmp_path)
        big_payload = b"x" * (4 * 1024 * 1024)  # 4 MiB
        cid = store.put(big_payload, "application/octet-stream")
        resolver = ToolspoolBlobResolver(store)

        real_open = open
        opened_blob_paths: list[str] = []

        def tracking_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            path_str = str(file)
            # 仅记录 blob body（以 cid 结尾、非 .mime sidecar）
            if path_str.endswith(cid) and not path_str.endswith(".mime"):
                opened_blob_paths.append(path_str)
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", tracking_open)

        # resolve() 只应读 stat + mime sidecar，不打开 blob body
        resolved = resolver.resolve({"cid": cid, "mime": "application/octet-stream"})
        assert opened_blob_paths == [], f"resolve() opened blob body unexpectedly: {opened_blob_paths}"

        # slice() 必须打开 blob body（独立 fd）
        chunk = resolved.slice(0, 1024)
        assert len(opened_blob_paths) == 1, "slice() should open exactly once"
        assert chunk == big_payload[:1024]

    def test_multiple_slices_independent_fds(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """多次 slice 各自独立 open；每次 read 完即关闭.
        Each slice() invocation opens an independent fd; closed via ``with``."""
        store = ToolspoolBlobStore(tmp_path)
        cid = store.put(b"abcdefghij" * 1024, "text/plain")  # 10 KiB
        resolver = ToolspoolBlobResolver(store)

        real_open = open
        open_count = 0

        def counting_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal open_count
            path_str = str(file)
            if path_str.endswith(cid) and not path_str.endswith(".mime"):
                open_count += 1
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", counting_open)
        resolved = resolver.resolve({"cid": cid, "mime": "text/plain"})
        resolved.slice(0, 1024)
        resolved.slice(1024, 1024)
        resolved.slice(8192, 2048)
        assert open_count == 3
