# -*- coding: utf-8 -*-
# filename: test_blob_put.py
# @Author  : JQQ
# @Software: PyCharm

"""
上行落盘例程单测（``pump_blob`` / ``pump_blob_sync``，v0.4.0 #196）/ Upload-pump unit tests.

协议依据 / Protocol: a2c_smcp-protocol blob-transfer.md §3（in-order ack-paced / 能力门控 /
防御性超时兜算）/ §6（末块 ack 回显比对 SHOULD）；error-handling.md §4019。

含跨模块对拍（pump 构造的 req 直喂 ``BlobUploadStore.handle_chunk``）——验证 Agent 发送面与
Computer 接收面的 wire 契约粘合（声明只在首块 / in-order / base64 / 末块取 landing_path）。
Includes a cross-module paring test (pump-built reqs fed straight into BlobUploadStore) —
the Agent-send ↔ Computer-receive wire contract glue.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest
import socketio.exceptions

from a2c_smcp.computer.blob.thresholds import BlobThresholds
from a2c_smcp.computer.blob.upload import BlobUploadStore
from a2c_smcp.utils.blob import (
    BlobUploadError,
    BlobUploadUnsupportedError,
    PutBlobResult,
    pump_blob,
    pump_blob_sync,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _RecordingCall:
    """记录每次调用的 fake async/sync call（可编程 ack 序列或异常）。"""

    def __init__(self, acks: list[Any] | None = None, raise_at: dict[int, Exception] | None = None) -> None:
        self.calls: list[tuple[str | None, int, bool, bytes, dict[str, Any] | None]] = []
        self._acks = acks
        self._raise_at = raise_at or {}

    def _invoke(self, upload_id: str | None, offset: int, eof: bool, chunk: bytes,
                declaration: dict[str, Any] | None) -> dict[str, Any]:
        self.calls.append((upload_id, offset, eof, chunk, declaration))
        exc = self._raise_at.get(len(self.calls) - 1)
        if exc is not None:
            raise exc
        if self._acks is not None:
            ack = self._acks[len(self.calls) - 1]
            if isinstance(ack, Exception):
                raise ack
            return ack
        return {"upload_id": "uid-fixed", "chunk_offset": offset, "req_id": "r"}

    async def __call__(self, upload_id: str | None, offset: int, eof: bool, chunk: bytes,
                       declaration: dict[str, Any] | None) -> dict[str, Any]:
        return self._invoke(upload_id, offset, eof, chunk, declaration)


class TestPumpAsync:
    def test_multi_chunk_call_sequence(self) -> None:
        """声明只在首块；in-order 推进；末块 ack 收口。"""
        data = b"x" * 10
        call = _RecordingCall(acks=[
            {"upload_id": "uid-1", "chunk_offset": 0, "req_id": "r"},
            {"upload_id": "uid-1", "chunk_offset": 4, "req_id": "r"},
            {"upload_id": "uid-1", "chunk_offset": 8, "req_id": "r",
             "landing_path": "/landing/uid-1_f.bin", "total_size": 10, "sha256": _sha(data)},
        ])
        result = asyncio.run(pump_blob(call, "c1", data, name_hint="f.bin", chunk_size=4))
        assert isinstance(result, PutBlobResult)
        assert result.landing_path == "/landing/uid-1_f.bin"
        assert result.total_size == 10
        assert result.sha256 == _sha(data)
        # 调用序列：首块（uid=None + 声明）→ 2 块（uid 回填、无声明）
        assert call.calls[0] == ("uid-None-sentinel" if False else None, 0, False, data[:4],
                                 {"total_size": 10, "sha256": _sha(data), "name_hint": "f.bin"})
        assert call.calls[1][:1] == ("uid-1",)
        assert call.calls[1][4] is None  # 后续块不携带声明
        assert call.calls[2] == ("uid-1", 8, True, data[8:], None)

    def test_single_chunk(self) -> None:
        data = b"tiny"
        call = _RecordingCall(acks=[
            {"upload_id": "u", "chunk_offset": 0, "req_id": "r",
             "landing_path": "/landing/u", "total_size": 4, "sha256": _sha(data)},
        ])
        result = asyncio.run(pump_blob(call, "c1", data))
        assert result.landing_path == "/landing/u"
        assert len(call.calls) == 1
        assert call.calls[0][2] is True  # eof 单块

    @pytest.mark.parametrize("bad", [-1, 0])
    def test_bad_chunk_size_rejected_at_entry(self, bad: int) -> None:
        """🟡3 回归：非正 chunk_size 入口拒绝——否则切片恒空 → offset 永不前进 → 死循环。"""
        call = _RecordingCall()
        with pytest.raises(BlobUploadError) as excinfo:
            asyncio.run(pump_blob(call, "c1", b"data", chunk_size=bad))
        assert excinfo.value.reason == "bad_chunk_size"
        assert call.calls == []
        with pytest.raises(BlobUploadError):
            pump_blob_sync(lambda *a: {}, "c1", b"data", chunk_size=bad)  # sync 同款收口

    def test_empty_payload_rejected(self) -> None:
        call = _RecordingCall()
        with pytest.raises(BlobUploadError) as excinfo:
            asyncio.run(pump_blob(call, "c1", b""))
        assert excinfo.value.reason == "empty_payload"
        assert call.calls == []  # 零调用


class TestTimeoutFallback:
    def test_first_chunk_timeout_maps_to_unsupported_with_context(self) -> None:
        data = b"payload-bytes"
        call = _RecordingCall(raise_at={0: socketio.exceptions.TimeoutError("ack timeout")})
        with pytest.raises(BlobUploadUnsupportedError) as excinfo:
            asyncio.run(pump_blob(call, "c1", data, name_hint="ctx.bin"))
        err = excinfo.value
        # 「字节留上下文」：载荷 + 声明完整随异常保留
        assert err.data == data
        assert err.total_size == len(data)
        assert err.sha256 == _sha(data)
        assert err.name_hint == "ctx.bin"
        assert err.reason == "upload_unsupported"

    def test_first_chunk_builtin_timeout_not_confused(self) -> None:
        """builtin TimeoutError 不是 socketio 异常——不得被误归一为 unsupported（原样上抛）。

        防御 ``except TimeoutError`` 意外吞掉非 socketio 超时（socketio.exceptions.TimeoutError
        并非 builtin 子类，两通道互不重叠）。
        """
        call = _RecordingCall(raise_at={0: TimeoutError("builtin")})
        with pytest.raises(TimeoutError) as excinfo:
            asyncio.run(pump_blob(call, "c1", b"data"))
        assert not isinstance(excinfo.value, BlobUploadUnsupportedError)

    def test_subsequent_chunk_timeout_reraises_verbatim(self) -> None:
        data = b"x" * 8
        call = _RecordingCall(acks=[
            {"upload_id": "u", "chunk_offset": 0, "req_id": "r"},
            socketio.exceptions.TimeoutError("mid-upload timeout"),
        ])
        with pytest.raises(socketio.exceptions.TimeoutError) as excinfo:
            asyncio.run(pump_blob(call, "c1", data, chunk_size=4))
        assert not isinstance(excinfo.value, BlobUploadUnsupportedError)


class TestErrorMapping:
    def test_busy_maps_to_reason(self) -> None:
        call = _RecordingCall(acks=[{"code": 4019, "message": "Blob write failed",
                                     "details": {"reason": "busy"}}])
        with pytest.raises(BlobUploadError) as excinfo:
            asyncio.run(pump_blob(call, "c1", b"data"))
        assert excinfo.value.reason == "busy"

    def test_other_protocol_code_passthrough(self) -> None:
        call = _RecordingCall(acks=[{"code": 4008, "message": "mismatch"}])
        with pytest.raises(BlobUploadError) as excinfo:
            asyncio.run(pump_blob(call, "c1", b"data"))
        assert excinfo.value.reason == "protocol_error_4008"

    def test_final_echo_mismatch(self) -> None:
        data = b"data-bytes!"
        call = _RecordingCall(acks=[
            {"upload_id": "u", "chunk_offset": 0, "req_id": "r",
             "landing_path": "/landing/u", "total_size": len(data), "sha256": "0" * 64},
        ])
        with pytest.raises(BlobUploadError) as excinfo:
            asyncio.run(pump_blob(call, "c1", data))
        assert excinfo.value.reason == "echo_mismatch"

    def test_final_ack_missing_landing_path(self) -> None:
        data = b"data"
        call = _RecordingCall(acks=[{"upload_id": "u", "chunk_offset": 0, "req_id": "r"}])
        with pytest.raises(BlobUploadError) as excinfo:
            asyncio.run(pump_blob(call, "c1", data))
        assert excinfo.value.reason == "incomplete_ack"


class TestPumpSync:
    def test_sync_mirror_matches_async(self) -> None:
        data = b"y" * 12
        store: dict[str, Any] = {}

        def call(upload_id: str | None, offset: int, eof: bool, chunk: bytes,
                 declaration: dict[str, Any] | None) -> dict[str, Any]:
            # 直接喂真实 store（见 TestCrossModule；此处仅验证 sync 形态可跑）
            return sync_feeder(store["impl"], upload_id, offset, eof, chunk, declaration)

        def sync_feeder(impl: BlobUploadStore, upload_id: str | None, offset: int, eof: bool,
                        chunk: bytes, declaration: dict[str, Any] | None) -> dict[str, Any]:
            import base64

            req: dict[str, Any] = {
                "agent": "a", "req_id": "r", "computer": "c1",
                "chunk_offset": offset, "eof": eof,
                "blob": base64.b64encode(chunk).decode("ascii"),
            }
            if upload_id is not None:
                req["upload_id"] = upload_id
            if declaration is not None:
                req["total_size"] = declaration["total_size"]
                req["sha256"] = declaration["sha256"]
                if declaration.get("name_hint") is not None:
                    req["name_hint"] = declaration["name_hint"]
            ret = impl.handle_chunk(req)
            return dict(ret)

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            impl = BlobUploadStore(Path(td), BlobThresholds())
            store["impl"] = impl
            result = pump_blob_sync(call, "c1", data, chunk_size=5)
            assert Path(result.landing_path).read_bytes() == data
            assert result.sha256 == _sha(data)

    def test_sync_first_chunk_timeout_unsupported(self) -> None:
        def call(*args: Any) -> dict[str, Any]:
            raise socketio.exceptions.TimeoutError("t")

        with pytest.raises(BlobUploadUnsupportedError):
            pump_blob_sync(call, "c1", b"data")


class TestCrossModuleParing:
    """Agent 发送面（pump）× Computer 接收面（store）wire 契约对拍。"""

    @pytest.mark.parametrize("size", [1, 5, 1000, 256 * 1024 + 7])
    def test_roundtrip_through_real_store(self, tmp_path: Path, size: int) -> None:
        """pump 构造的 req 直喂 BlobUploadStore：字节逐一致、sha/total 回显闭合。"""
        data = bytes((i * 31 + 7) % 256 for i in range(size))
        impl = BlobUploadStore(tmp_path, BlobThresholds())

        async def call(upload_id: str | None, offset: int, eof: bool, chunk: bytes,
                       declaration: dict[str, Any] | None) -> dict[str, Any]:
            import base64

            req: dict[str, Any] = {
                "agent": "a", "req_id": "r", "computer": "c1",
                "chunk_offset": offset, "eof": eof,
                "blob": base64.b64encode(chunk).decode("ascii"),
            }
            if upload_id is not None:
                req["upload_id"] = upload_id
            if declaration is not None:
                req["total_size"] = declaration["total_size"]
                req["sha256"] = declaration["sha256"]
                if declaration.get("name_hint") is not None:
                    req["name_hint"] = declaration["name_hint"]
            return dict(impl.handle_chunk(req))

        result = asyncio.run(pump_blob(call, "c1", data, name_hint="paring.bin"))
        assert Path(result.landing_path).read_bytes() == data  # 逐字节对拍
        assert result.total_size == size
        assert result.sha256 == _sha(data)
        assert Path(result.landing_path).name.endswith("_paring.bin")

    def test_unconfigured_store_surfaces_forbidden(self) -> None:
        impl = BlobUploadStore(None, BlobThresholds())

        async def call(upload_id: str | None, offset: int, eof: bool, chunk: bytes,
                       declaration: dict[str, Any] | None) -> dict[str, Any]:
            import base64

            req: dict[str, Any] = {
                "agent": "a", "req_id": "r", "computer": "c1",
                "chunk_offset": offset, "eof": eof, "blob": base64.b64encode(chunk).decode("ascii"),
                "total_size": declaration["total_size"], "sha256": declaration["sha256"],
            }
            return dict(impl.handle_chunk(req))

        with pytest.raises(BlobUploadError) as excinfo:
            asyncio.run(pump_blob(call, "c1", b"data"))
        assert excinfo.value.reason == "forbidden"  # fail-closed 传导到 Agent 侧
