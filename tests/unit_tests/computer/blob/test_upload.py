# -*- coding: utf-8 -*-
# filename: test_upload.py
# @Author  : JQQ
# @Software: PyCharm

"""
``client:put_blob`` 上传会话管理单测 / Unit tests for the upload-session store.

协议依据 / Protocol: a2c-smcp-protocol blob-transfer.md §3（事件 + 有界会话）/ §7（landing 沙箱）；
error-handling.md §4019（reason 表）。覆盖面 = 8 个 reason 全路径 + 有界会话 MUST（闲置超时 /
并发上限 / 孤儿 GC）+ 安全名消毒 + Computer property 接线。
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import a2c_smcp.computer.blob.upload as upload_mod
from a2c_smcp.computer.blob.thresholds import BlobThresholds
from a2c_smcp.computer.blob.upload import BlobUploadStore, sanitize_name_hint
from a2c_smcp.computer.computer import Computer
from a2c_smcp.smcp import ErrorCode

REQ_ID = "req-it"


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _first_req(payload: bytes, *, sha: str | None = None, total: int | None = None,
               name_hint: str | None = None, eof: bool = False, offset: int = 0) -> dict[str, Any]:
    req: dict[str, Any] = {
        "agent": "agent-it", "req_id": REQ_ID, "computer": "c1",
        "chunk_offset": offset, "eof": eof,
        "total_size": total if total is not None else len(payload),
        "sha256": sha if sha is not None else _sha(payload),
        "blob": _b64(payload),
    }
    if name_hint is not None:
        req["name_hint"] = name_hint
    return req


def _next_req(upload_id: str, payload: bytes, *, offset: int, eof: bool = False,
              extra: dict[str, Any] | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {
        "agent": "agent-it", "req_id": REQ_ID, "computer": "c1",
        "upload_id": upload_id, "chunk_offset": offset, "eof": eof,
        "blob": _b64(payload),
    }
    if extra:
        req.update(extra)
    return req


def _reason(ret: dict[str, Any]) -> tuple[int, str]:
    """断言辅助：从 flat ErrorPayload 提取 (code, details.reason)。"""
    return int(ret["code"]), str((ret.get("details") or {}).get("reason"))


@pytest.fixture()
def store(tmp_path: Path) -> BlobUploadStore:
    return BlobUploadStore(tmp_path / "landing", BlobThresholds())


class TestHappyPath:
    def test_first_chunk_ack_returns_upload_id(self, store: BlobUploadStore) -> None:
        payload = b"x" * 10
        ret = store.handle_chunk(_first_req(payload[:4]))
        assert "code" not in ret
        assert ret["upload_id"]
        assert ret["chunk_offset"] == 0
        assert ret["req_id"] == REQ_ID
        assert "landing_path" not in ret  # 非末块 ack 不携带落点

    def test_multi_chunk_roundtrip_bytes_on_disk(self, store: BlobUploadStore, tmp_path: Path) -> None:
        payload = bytes(range(256)) * 40  # 10240 B；chunk=256 → 40 块
        chunk = 256
        ret: dict[str, Any] = store.handle_chunk(_first_req(payload[:chunk], total=len(payload), sha=_sha(payload)))
        upload_id = ret["upload_id"]
        for off in range(chunk, len(payload) - chunk, chunk):
            ret = store.handle_chunk(_next_req(upload_id, payload[off : off + chunk], offset=off))
            assert ret["upload_id"] == upload_id
            assert ret["chunk_offset"] == off
        ret = store.handle_chunk(
            _next_req(upload_id, payload[-chunk:], offset=len(payload) - chunk, eof=True)
        )
        assert ret["landing_path"].startswith(str(tmp_path / "landing"))
        assert ret["total_size"] == len(payload)
        assert ret["sha256"] == _sha(payload)
        assert Path(ret["landing_path"]).read_bytes() == payload  # 逐字节一致
        # 定稿后 .part 已消失（原子 rename 走的是 .part）
        assert not (tmp_path / "landing" / ".a2c-upload" / f"{upload_id}.part").exists()

    def test_single_chunk_eof_degenerate(self, store: BlobUploadStore) -> None:
        """首块即 eof（单块上传）为合法退化，一次调用定稿。"""
        payload = b"single-chunk-payload"
        ret = store.handle_chunk(_first_req(payload, eof=True))
        assert Path(ret["landing_path"]).read_bytes() == payload

    def test_name_hint_sanitized_in_landing_path(self, store: BlobUploadStore) -> None:
        payload = b"data"
        ret = store.handle_chunk(_first_req(payload, name_hint="../../etc/passwd", eof=True))
        name = Path(ret["landing_path"]).name
        assert "/" not in name and ".." not in name
        assert name.startswith(ret["upload_id"])  # upload_id 派生前缀

    def test_no_name_hint_yields_pure_upload_id(self, store: BlobUploadStore) -> None:
        ret = store.handle_chunk(_first_req(b"data", eof=True))
        assert Path(ret["landing_path"]).name == ret["upload_id"]


class TestForbidden:
    def test_landing_root_unset_fail_closed(self, tmp_path: Path) -> None:
        store = BlobUploadStore(None, BlobThresholds())
        code, reason = _reason(store.handle_chunk(_first_req(b"data")))
        assert code == int(ErrorCode.BLOB_WRITE_FAILED)
        assert reason == "forbidden"

    def test_landing_root_not_writable(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        store = BlobUploadStore(blocker / "landing", BlobThresholds())
        code, reason = _reason(store.handle_chunk(_first_req(b"data")))
        assert reason == "forbidden"


class TestInvalidDeclaration:
    @pytest.mark.parametrize(
        ("mutate", "label"),
        [
            pytest.param(lambda r: r.pop("total_size"), "missing_total_size", id="missing-total"),
            pytest.param(lambda r: r.update(total_size=0), "total_below_1", id="total-0"),
            pytest.param(lambda r: r.update(total_size=True), "total_is_bool", id="total-bool"),
            pytest.param(lambda r: r.pop("sha256"), "missing_sha", id="missing-sha"),
            pytest.param(lambda r: r.update(sha256="zz"), "sha_not_hex", id="sha-not-hex"),
        ],
    )
    def test_bad_first_chunk_declaration(self, store: BlobUploadStore, mutate: Any, label: str) -> None:
        req = _first_req(b"data", eof=True)
        mutate(req)
        code, reason = _reason(store.handle_chunk(req))
        assert code == int(ErrorCode.BLOB_WRITE_FAILED)
        assert reason == "invalid_declaration"

    def test_declaration_fields_resent_on_subsequent_chunk(self, store: BlobUploadStore) -> None:
        ret = store.handle_chunk(_first_req(b"a" * 8))
        upload_id = ret["upload_id"]
        code, reason = _reason(
            store.handle_chunk(_next_req(upload_id, b"b" * 4, offset=8, extra={"sha256": _sha(b"b" * 4)}))
        )
        assert reason == "invalid_declaration"

    def test_invalid_base64_blob(self, store: BlobUploadStore) -> None:
        req = _first_req(b"data", eof=True)
        req["blob"] = "!!!not-base64!!!"
        code, reason = _reason(store.handle_chunk(req))
        assert reason == "invalid_declaration"

    def test_blob_not_a_string(self, store: BlobUploadStore) -> None:
        req = _first_req(b"data", eof=True)
        req["blob"] = 12345
        code, reason = _reason(store.handle_chunk(req))
        assert reason == "invalid_declaration"


class TestTooLarge:
    def test_declared_over_cap_rejected_zero_bytes(self, tmp_path: Path) -> None:
        store = BlobUploadStore(tmp_path / "landing", BlobThresholds(upload_max_bytes=16))
        code, reason = _reason(store.handle_chunk(_first_req(b"a" * 17, total=17)))
        assert reason == "too_large"
        # 零字节落盘：landing root 甚至未被创建
        assert not (tmp_path / "landing" / ".a2c-upload").exists()


class TestBusy:
    def test_concurrent_cap_rejects_new_session(self, tmp_path: Path) -> None:
        store = BlobUploadStore(tmp_path / "landing", BlobThresholds(upload_max_concurrent=1))
        payload = b"a" * 8
        first = store.handle_chunk(_first_req(payload[:4], total=len(payload), sha=_sha(payload)))  # 会话 1 在途（非 eof）
        assert "code" not in first
        code, reason = _reason(store.handle_chunk(_first_req(b"b" * 4)))
        assert reason == "busy"
        # 会话结束后可再开（无泄漏）
        done = store.handle_chunk(_next_req(first["upload_id"], payload[4:], offset=4, eof=True))
        assert "code" not in done
        again = store.handle_chunk(_first_req(b"c" * 4))
        assert "code" not in again


class TestInvalidUpload:
    def test_unknown_upload_id(self, store: BlobUploadStore) -> None:
        code, reason = _reason(store.handle_chunk(_next_req("deadbeef" * 4, b"x", offset=0)))
        assert reason == "invalid_upload"

    def test_empty_string_upload_id_is_subsequent_chunk(self, store: BlobUploadStore) -> None:
        """显式空串 upload_id ≠ 缺省（首块）：走后续块路径 → invalid_upload（路径分叉正对照）。"""
        code, reason = _reason(store.handle_chunk(_next_req("", b"x", offset=0)))
        assert reason == "invalid_upload"

    def test_idle_timeout_expires_session(
        self, store: BlobUploadStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # thresholds 固定 1s；monkeypatch monotonic 使下一块到达时会话已闲置超时
        clock = {"now": 100.0}
        monkeypatch.setattr(upload_mod.time, "monotonic", lambda: clock["now"])
        payload = b"a" * 8
        ret = store.handle_chunk(_first_req(payload[:4]))
        upload_id = ret["upload_id"]
        clock["now"] += 3600.0
        code, reason = _reason(store.handle_chunk(_next_req(upload_id, payload[4:], offset=4, eof=True)))
        assert reason == "invalid_upload"
        # 过期会话的 .part 已被清理
        assert not (store.landing_root / ".a2c-upload" / f"{upload_id}.part").exists()


class TestRange:
    def test_out_of_order_chunk(self, store: BlobUploadStore) -> None:
        payload = b"a" * 12
        ret = store.handle_chunk(_first_req(payload[:4]))
        upload_id = ret["upload_id"]
        code, reason = _reason(store.handle_chunk(_next_req(upload_id, payload[8:], offset=8)))
        assert reason == "range"  # 期望 offset == 已收 4，收到 8

    def test_first_chunk_nonzero_offset(self, store: BlobUploadStore) -> None:
        code, reason = _reason(store.handle_chunk(_first_req(b"data", offset=4)))
        assert reason == "range"  # 首块已收字节恒 0

    def test_final_chunk_does_not_complete_total(self, store: BlobUploadStore) -> None:
        payload = b"a" * 12
        ret = store.handle_chunk(_first_req(payload[:4]))
        upload_id = ret["upload_id"]
        code, reason = _reason(
            store.handle_chunk(_next_req(upload_id, b"b" * 4, offset=4, eof=True))  # 只到 8 ≠ 声明 12
        )
        assert reason == "range"

    def test_non_eof_chunk_overrunning_declared_total(self, store: BlobUploadStore) -> None:
        """🔴1 回归：非 eof 块超声明 total_size → range + 会话作废（DoS 上限不可旁路）。

        声明 8B 后持续发非 eof 块（每块 in-order 合法）——写后不得超声明值，否则首块
        ``too_large`` 决断被击穿（``.part`` 无界增长）。
        """
        payload = b"a" * 8
        ret = store.handle_chunk(_first_req(payload[:4], total=8, sha=_sha(payload)))
        upload_id = ret["upload_id"]
        code, reason = _reason(store.handle_chunk(_next_req(upload_id, b"b" * 100, offset=4)))
        assert reason == "range"
        # 会话已作废：后续同 upload_id 块 → invalid_upload；.part 已删
        code2, reason2 = _reason(store.handle_chunk(_next_req(upload_id, payload[4:], offset=4, eof=True)))
        assert reason2 == "invalid_upload"
        assert not (store.landing_root / ".a2c-upload" / f"{upload_id}.part").exists()


class TestIntegrity:
    def test_sha_mismatch_discards_part(self, store: BlobUploadStore) -> None:
        payload = b"a" * 8
        wrong_sha = _sha(b"different-bytes")
        ret = store.handle_chunk(_first_req(payload[:4], total=len(payload), sha=wrong_sha))
        upload_id = ret["upload_id"]
        code, reason = _reason(store.handle_chunk(_next_req(upload_id, payload[4:], offset=4, eof=True)))
        assert reason == "integrity"
        final_ret = store.handle_chunk(_next_req(upload_id, payload[4:], offset=4, eof=True))
        assert "landing_path" not in final_ret  # 错误负载（integrity 丢弃）构造上无落点键
        # .part 与产物均不存在（丢弃，不落盘）
        assert not (store.landing_root / ".a2c-upload" / f"{upload_id}.part").exists()
        assert not list(store.landing_root.glob(f"{upload_id}*"))


class TestOrphanGc:
    def test_aged_orphan_part_collected_on_next_upload(self, store: BlobUploadStore) -> None:
        """超龄孤儿（崩溃遗留，mtime 停在闲置阈值外）→ 首块触发回收。"""
        import os
        import time as time_mod

        part_dir = store.landing_root / ".a2c-upload"
        part_dir.mkdir(parents=True)
        orphan = part_dir / "ffffffffffffffffffffffffffffffff.part"
        orphan.write_bytes(b"crash-leftover")
        aged = time_mod.time() - (BlobThresholds().upload_idle_timeout_seconds + 60)
        os.utime(orphan, (aged, aged))  # backdate 超过闲置阈值
        store.handle_chunk(_first_req(b"fresh-upload", eof=True))
        assert not orphan.exists()

    def test_fresh_orphan_part_spared_by_age_grace(self, store: BlobUploadStore) -> None:
        """未超龄的表外 .part 不收：可能是共享 landing root 的兄弟进程在途会话（mtime 宽限）。"""
        part_dir = store.landing_root / ".a2c-upload"
        part_dir.mkdir(parents=True)
        fresh = part_dir / "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee.part"
        fresh.write_bytes(b"sibling-process-in-flight")
        store.handle_chunk(_first_req(b"fresh-upload", eof=True))
        assert fresh.exists()  # 新鲜 → 宽限保留


class TestSanitizeNameHint:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, ""),
            ("", ""),
            ("../..", ""),
            ("report v1.pdf", "report_v1.pdf"),
            ("a/b/c.txt", "a_b_c.txt"),
            ("..start", "start"),
            ("ok-name_1.bin", "ok-name_1.bin"),
            ("x" * 200, "x" * 64),  # 长度夹取
        ],
    )
    def test_table(self, raw: str | None, expected: str) -> None:
        assert sanitize_name_hint(raw) == expected


class TestComputerWiring:
    """Computer.landing_root / blob_upload_store 的 lazy 接线（settings resolve 桩）。"""

    @staticmethod
    def _computer_with_settings(monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any], tmp_path: Path) -> Computer:
        comp = Computer(name="comp-put-blob", blob_cache_root=tmp_path)
        stub = SimpleNamespace(settings=settings)
        monkeypatch.setattr(comp, "_resolve_declared_settings", lambda: stub)
        return comp

    def test_landing_root_unset_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        comp = self._computer_with_settings(monkeypatch, {}, tmp_path)
        assert comp.landing_root is None

    def test_landing_root_resolved_from_settings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        comp = self._computer_with_settings(monkeypatch, {"landingRoot": str(tmp_path / "land")}, tmp_path)
        assert comp.landing_root == tmp_path / "land"

    def test_blob_upload_store_is_singleton(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        comp = self._computer_with_settings(monkeypatch, {"landingRoot": str(tmp_path / "land")}, tmp_path)
        assert comp.blob_upload_store is comp.blob_upload_store

    def test_unconfigured_store_not_cached_hot_enable(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """🟡1 回归：未配置时 store 不缓存——先 forbidden、补配置后立即可用（热启用语义对齐）。"""
        comp = self._computer_with_settings(monkeypatch, {}, tmp_path)
        store_a = comp.blob_upload_store
        code, reason = _reason(store_a.handle_chunk(_first_req(b"data")))
        assert reason == "forbidden"
        # 事后落盘配置（settings 桩切换）→ 不重启即可用
        stub = SimpleNamespace(settings={"landingRoot": str(tmp_path / "land2")})
        monkeypatch.setattr(comp, "_resolve_declared_settings", lambda: stub)
        store_b = comp.blob_upload_store
        assert store_b is not store_a  # None-root 未缓存，重建绑定新 root
        ret = store_b.handle_chunk(_first_req(b"data", eof=True))
        assert "code" not in ret
        assert comp.blob_upload_store is store_b  # 已配置后恢复单例

    def test_unconfigured_store_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        comp = self._computer_with_settings(monkeypatch, {}, tmp_path)
        code, reason = _reason(comp.blob_upload_store.handle_chunk(_first_req(b"data")))
        assert reason == "forbidden"
