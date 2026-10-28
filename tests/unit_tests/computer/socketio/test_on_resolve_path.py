# -*- coding: utf-8 -*-
# filename: test_on_resolve_path.py
# @Author  : JQQ
# @Software: PyCharm
"""
``client:resolve_path`` handler 与 ``client:get_blob`` path 分支的**接线层**单测（v0.5.0 #236）.
Wiring-level unit tests for ``client:resolve_path`` and the ``kind=path`` branch of ``get_blob``.

协议依据 / Protocol: events.md §client:resolve_path；error-handling.md §4020 / §4018；
blob-transfer.md §生产者三：按路径读取（拉取期复核）。

边界 / Scope：path core（``normalize_path`` / 九步管线 / 句柄编解码）属 #236 流 1，由
``tests/unit_tests/computer/blob/test_path_resolver.py`` 覆盖；本文件只验证**接线**：
事件载荷 → ``Computer`` 方法 → flat ``ErrorPayload`` 映射，以及 ``PathPullResult`` 的
``close()`` 在成功 / range 早退两条路径上都被调用（fd 泄漏守卫）。

覆盖 / Coverage:
  - ``readRoots`` 未配置 → ``4020 forbidden``，且**不**调用 resolve（fail-closed 先于一切）
  - 成功路径字段映射（canonical_path / blob_handle / mime / size / sha / mtime / req_id）
  - ``PathResolveError`` 四 reason → ``4020`` flat ``details.reason``
  - 句柄 kind=path → 拉取期复核 + 切片（单块 / 分块 / max_chunk 下界）
  - range 越界 → ``4018 range``，句柄仍被 close
  - ``BlobHandleForbiddenError`` / ``GoneError`` → ``4018 forbidden`` / ``gone``
  - office/role 隔离：``computer`` 名不匹配 → 抛 ``SMCPNamespaceError``（非 ErrorPayload）
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.blob import BlobHandleForbiddenError, BlobHandleGoneError
from a2c_smcp.computer.blob.path_resolver import PathResolveError, ReadRoot, ResolvedPath
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio import client as computer_sio
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.exceptions import SMCPNamespaceError


@pytest.fixture()
def computer(tmp_path: Path) -> Computer:
    """最小可用 Computer（blob 缓存挂 tmp_path，避免写用户目录）。"""
    return Computer(name="comp-test", blob_cache_root=tmp_path)


@pytest.fixture()
def client(computer: Computer) -> SMCPComputerClient:
    return SMCPComputerClient(computer=computer)


def _resolve_req(path: str = "/srv/docs/a.txt", *, computer_name: str = "comp-test") -> dict:
    return {"agent": "agent-1", "req_id": "r-1", "computer": computer_name, "path": path}


def _grant(computer: Computer, *roots: str) -> None:
    """直接注入编译后的读根（跳过 settings 解析，聚焦接线）。"""
    computer._read_roots = [ReadRoot(configured=r, real=r) for r in roots]


def _resolved(**overrides: Any) -> ResolvedPath:
    base: dict[str, Any] = {
        "canonical_path": "/srv/docs/a.txt",
        "blob_handle": "opaque-handle",
        "mime_type": "text/plain",
        "total_size": 11,
        "sha256": hashlib.sha256(b"hello world").hexdigest(),
        "mtime": 1_700_000_000_123,
    }
    base.update(overrides)
    return ResolvedPath(**base)


class _FakePull:
    """``PathPullResult`` 的接线用替身：记录 close 次数并模拟惰性切片。"""

    def __init__(self, data: bytes, *, mime: str = "text/plain") -> None:
        self._data = data
        self.canonical_path = "/srv/docs/a.txt"
        self.mime_type = mime
        self.total_size = len(data)
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.close_calls = 0

    def slice(self, offset: int, length: int) -> bytes:
        return self._data[offset : offset + length]

    def close(self) -> None:
        self.close_calls += 1


# ---------------------------------------------------------------------------
# client:resolve_path / handler wiring
# ---------------------------------------------------------------------------
class TestResolvePathHandler:
    """``on_resolve_path`` 接线 / handler wiring."""

    @pytest.mark.asyncio
    async def test_empty_read_roots_is_forbidden_without_touching_fs(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``readRoots`` 未配置 → ``4020 forbidden``；resolve 不得被调用（fail-closed 先于一切）."""
        called: list[str] = []
        monkeypatch.setattr(computer, "resolve_read_path", lambda path: called.append(path) or _resolved())

        ret = await client.on_resolve_path(_resolve_req())  # type: ignore[arg-type]

        assert ret == {"code": 4020, "message": "Path not accessible", "details": {"reason": "forbidden"}}
        assert called == [], "readRoots 为空时 MUST NOT 触碰文件系统 / must not touch the filesystem"

    @pytest.mark.asyncio
    async def test_success_maps_all_fields(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """成功路径：``ResolvedPath`` 六字段 + ``req_id`` 原样映射进 ``ResolvePathRet``."""
        _grant(computer, "/srv/docs")
        monkeypatch.setattr(computer, "resolve_read_path", lambda path: _resolved())

        ret = await client.on_resolve_path(_resolve_req())  # type: ignore[arg-type]

        assert ret == {
            "canonical_path": "/srv/docs/a.txt",
            "blob_handle": "opaque-handle",
            "mime_type": "text/plain",
            "total_size": 11,
            "sha256": hashlib.sha256(b"hello world").hexdigest(),
            "mtime": 1_700_000_000_123,
            "req_id": "r-1",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["invalid_path", "forbidden", "not_found", "not_a_file"])
    async def test_path_resolve_error_maps_to_4020(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch, reason: str
    ) -> None:
        """四种铸造期失败 → ``4020`` flat ErrorPayload，``reason`` 下沉 ``details``（开放枚举）."""
        _grant(computer, "/srv/docs")

        def _raise(path: str) -> ResolvedPath:
            raise PathResolveError(reason)  # type: ignore[arg-type]

        monkeypatch.setattr(computer, "resolve_read_path", _raise)

        ret = await client.on_resolve_path(_resolve_req())  # type: ignore[arg-type]

        assert ret["code"] == 4020  # type: ignore[index]
        assert ret["message"] == "Path not accessible"  # type: ignore[index]
        assert ret["details"] == {"reason": reason}  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_computer_name_mismatch_raises(
        self, computer: Computer, client: SMCPComputerClient
    ) -> None:
        """隔离不变量：``computer`` 名不匹配 → 抛 ``SMCPNamespaceError``（不伪装成 ErrorPayload）."""
        _grant(computer, "/srv/docs")
        with pytest.raises(SMCPNamespaceError):
            await client.on_resolve_path(_resolve_req(computer_name="other"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# client:get_blob / kind=path 拉取期复核
# ---------------------------------------------------------------------------
def _patch_path_handle(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """让 ``decode_blob_handle`` 返回 ``kind=path``（不依赖流 1 的编解码实现）。"""
    body = payload or {"canonical_path": "/srv/docs/a.txt", "total_size": 11, "mtime": 0, "sha256": "x", "mime": "text/plain"}
    monkeypatch.setattr(computer_sio, "decode_blob_handle", lambda handle: ("path", body))
    return body


def _blob_req(*, offset: int = 0, max_chunk: int | None = None) -> dict:
    req: dict = {"agent": "agent-1", "req_id": "r-2", "computer": "comp-test", "blob_handle": "opaque-handle"}
    if offset:
        req["chunk_offset"] = offset
    if max_chunk is not None:
        req["max_chunk_bytes"] = max_chunk
    return req


class TestPathGetBlobBranch:
    """``_process_get_blob`` 的 ``kind=path`` 特判 / path branch of the pull handler."""

    @pytest.mark.asyncio
    async def test_single_chunk_returns_handle_metadata_and_closes(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单块：元数据取句柄内值（不重算），``eof`` 由 len(chunk) 判定，句柄 close 恰好一次."""
        fake = _FakePull(b"hello world")
        _patch_path_handle(monkeypatch)
        monkeypatch.setattr(computer, "pull_path_blob", lambda payload: fake)

        ret = await client.on_get_blob(_blob_req())  # type: ignore[arg-type]

        assert ret["total_size"] == 11  # type: ignore[index]
        assert ret["sha256"] == fake.sha256  # type: ignore[index]
        assert ret["mime_type"] == "text/plain"  # type: ignore[index]
        assert ret["chunk_offset"] == 0  # type: ignore[index]
        assert ret["eof"] is True  # type: ignore[index]
        assert base64.b64decode(ret["blob"]) == b"hello world"  # type: ignore[index]
        assert fake.close_calls == 1

    @pytest.mark.asyncio
    async def test_chunked_offsets(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """分块：``max_chunk_bytes`` 生效、非末块 ``eof=False``，每块各自 close."""
        fake = _FakePull(b"0123456789")
        _patch_path_handle(monkeypatch)
        monkeypatch.setattr(computer, "pull_path_blob", lambda payload: fake)

        first = await client.on_get_blob(_blob_req(offset=0, max_chunk=4))  # type: ignore[arg-type]
        second = await client.on_get_blob(_blob_req(offset=4, max_chunk=4))  # type: ignore[arg-type]

        assert base64.b64decode(first["blob"]) == b"0123"  # type: ignore[index]
        assert first["eof"] is False  # type: ignore[index]
        assert base64.b64decode(second["blob"]) == b"4567"  # type: ignore[index]
        assert second["eof"] is False  # type: ignore[index]
        assert fake.close_calls == 2

    @pytest.mark.asyncio
    async def test_range_error_still_closes(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``chunk_offset`` 越界 → ``4018 range``；**早退路径也必须 close**（fd 泄漏守卫）."""
        fake = _FakePull(b"abc")
        _patch_path_handle(monkeypatch)
        monkeypatch.setattr(computer, "pull_path_blob", lambda payload: fake)

        ret = await client.on_get_blob(_blob_req(offset=99))  # type: ignore[arg-type]

        assert ret == {"code": 4018, "message": "Blob not accessible", "details": {"reason": "range"}}
        assert fake.close_calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "expected_reason"),
        [
            (BlobHandleForbiddenError("revoked"), "forbidden"),
            (BlobHandleGoneError("deleted"), "gone"),
        ],
    )
    async def test_pull_time_revalidation_failures(
        self,
        computer: Computer,
        client: SMCPComputerClient,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
        expected_reason: str,
    ) -> None:
        """拉取期复核失败 → ``4018``（撤权 forbidden / 删除 gone），``4018`` 枚举不变."""
        _patch_path_handle(monkeypatch)

        def _raise(payload: Any) -> Any:
            raise error

        monkeypatch.setattr(computer, "pull_path_blob", _raise)

        ret = await client.on_get_blob(_blob_req())  # type: ignore[arg-type]

        assert ret["code"] == 4018  # type: ignore[index]
        assert ret["details"] == {"reason": expected_reason}  # type: ignore[index]
