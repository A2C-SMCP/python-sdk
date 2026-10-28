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
  - **resolve 在线程池执行**（全量 sha256 不得阻塞 socket.io 事件循环）
  - ``PathResolveError`` 四 reason → ``4020`` flat ``details.reason``
  - 句柄 kind=path → 拉取期复核 + 切片（单块 / 分块 / max_chunk 下界）
  - range 越界 → ``4018 range``，句柄仍被 close
  - ``BlobHandleForbiddenError`` / ``GoneError`` → ``4018 forbidden`` / ``gone``
  - office/role 隔离：``computer`` 名不匹配 → 抛 ``SMCPNamespaceError``（非 ErrorPayload）
"""

from __future__ import annotations

import base64
import hashlib
import threading
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.blob import BlobHandleForbiddenError, BlobHandleGoneError
from a2c_smcp.computer.blob.path_resolver import PathResolveError, ResolvedPath
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.scope import ResolvedSettings
from a2c_smcp.computer.socketio import client as computer_sio
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.utils.path_norm import normalize_path


@pytest.fixture()
def computer(tmp_path: Path) -> Computer:
    """最小可用 Computer（blob 缓存挂 tmp_path，避免写用户目录）。"""
    return Computer(name="comp-test", blob_cache_root=tmp_path)


@pytest.fixture()
def client(computer: Computer) -> SMCPComputerClient:
    return SMCPComputerClient(computer=computer)


def _resolve_req(path: str = "/srv/docs/a.txt", *, computer_name: str = "comp-test") -> dict:
    return {"agent": "agent-1", "req_id": "r-1", "computer": computer_name, "path": path}


def _grant(computer: Computer, monkeypatch: pytest.MonkeyPatch, *roots: str) -> None:
    """注入 settings 视图（``readRoots`` = ``roots``），走真实的 ``read_roots`` 编译路径.

    不直接写 ``_read_roots`` 缓存：``read_roots`` 每次现读 settings（撤权即时生效），
    只覆写私有缓存会被判定为「raw 已变」并重新解析真实用户配置，导致测试依赖宿主环境。
    Patch the settings view instead of the private cache so the real compile path runs.
    """
    monkeypatch.setattr(
        computer,
        "_resolve_declared_settings",
        lambda: ResolvedSettings(settings={"readRoots": list(roots)}, errors=[], per_scope_layers={}),
    )


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
        """``readRoots`` 未配置 → ``4020 forbidden``；resolve 不得被调用（fail-closed 先于一切）.

        🟡7：显式注入空授权视图，**不**依赖宿主真实 ``~/.config/a2c/settings.json`` 为空。
        """
        _grant(computer, monkeypatch)  # 零读根
        called: list[str] = []
        monkeypatch.setattr(computer, "resolve_read_path", lambda path, **kw: called.append(path) or _resolved())

        ret = await client.on_resolve_path(_resolve_req())  # type: ignore[arg-type]

        assert ret == {"code": 4020, "message": "Path not accessible", "details": {"reason": "forbidden"}}
        assert called == [], "readRoots 为空时 MUST NOT 触碰文件系统 / must not touch the filesystem"

    @pytest.mark.asyncio
    async def test_success_maps_all_fields(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """成功路径：``ResolvedPath`` 六字段 + ``req_id`` 原样映射进 ``ResolvePathRet``."""
        _grant(computer, monkeypatch, "/srv/docs")
        monkeypatch.setattr(computer, "resolve_read_path", lambda path, **kw: _resolved())

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
    async def test_resolve_runs_off_the_event_loop(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``resolve`` 必须在线程池执行——全量 sha256 数 GB 文件会阻塞事件循环（心跳/工具调用超时）.

        断言方式：在 ``resolve_read_path`` 内记录当前线程 id，与事件循环线程比对。若有人把
        ``await asyncio.to_thread(...)`` 改回直接调用，本用例即失败（锁住该决定）。
        Asserts `resolve_read_path` runs on a worker thread, not the event-loop thread.
        """
        _grant(computer, monkeypatch, "/srv/docs")
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}

        def fake_resolve(path: str, **kw: Any) -> ResolvedPath:
            seen["thread"] = threading.get_ident()
            return _resolved()

        monkeypatch.setattr(computer, "resolve_read_path", fake_resolve)

        await client.on_resolve_path(_resolve_req())  # type: ignore[arg-type]

        assert "thread" in seen, "resolve_read_path 未被调用 / not called"
        assert seen["thread"] != loop_thread, (
            "resolve 必须在事件循环之外的线程执行（asyncio.to_thread），否则全量 sha256 会阻塞事件循环"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["invalid_path", "forbidden", "not_found", "not_a_file"])
    async def test_path_resolve_error_maps_to_4020(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch, reason: str
    ) -> None:
        """四种铸造期失败 → ``4020`` flat ErrorPayload，``reason`` 下沉 ``details``（开放枚举）."""
        _grant(computer, monkeypatch, "/srv/docs")

        def _raise(path: str, **kw: Any) -> ResolvedPath:
            raise PathResolveError(reason)  # type: ignore[arg-type]

        monkeypatch.setattr(computer, "resolve_read_path", _raise)

        ret = await client.on_resolve_path(_resolve_req())  # type: ignore[arg-type]

        assert ret["code"] == 4020  # type: ignore[index]
        assert ret["message"] == "Path not accessible"  # type: ignore[index]
        assert ret["details"] == {"reason": reason}  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_computer_name_mismatch_raises(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """隔离不变量：``computer`` 名不匹配 → 抛 ``SMCPNamespaceError``（不伪装成 ErrorPayload）."""
        _grant(computer, monkeypatch, "/srv/docs")
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
        _grant(computer, monkeypatch, "/srv/docs")
        _patch_path_handle(monkeypatch)
        monkeypatch.setattr(computer, "pull_path_blob", lambda payload, **kw: fake)

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
        _grant(computer, monkeypatch, "/srv/docs")
        _patch_path_handle(monkeypatch)
        monkeypatch.setattr(computer, "pull_path_blob", lambda payload, **kw: fake)

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
        _grant(computer, monkeypatch, "/srv/docs")
        _patch_path_handle(monkeypatch)
        monkeypatch.setattr(computer, "pull_path_blob", lambda payload, **kw: fake)

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
        _grant(computer, monkeypatch, "/srv/docs")
        _patch_path_handle(monkeypatch)

        def _raise(payload: Any, **kw: Any) -> Any:
            raise error

        monkeypatch.setattr(computer, "pull_path_blob", _raise)

        ret = await client.on_get_blob(_blob_req())  # type: ignore[arg-type]

        assert ret["code"] == 4018  # type: ignore[index]
        assert ret["details"] == {"reason": expected_reason}  # type: ignore[index]


# ---------------------------------------------------------------------------
# 授权视图与拒绝集来源 / authorization view & rejection set provenance
# ---------------------------------------------------------------------------
class TestReadAuthorizationSource:
    """``read_roots`` / ``protected_paths`` 的来源与新鲜度（隔离审查 🔴1/🔴2/🟡4）."""

    def test_read_roots_reread_on_every_call(self, computer: Computer, monkeypatch: pytest.MonkeyPatch) -> None:
        """🔴2：授权**每请求现读**——settings 变则 roots 立刻变（无缓存可陈旧）.

        同时锁住 realpath 新鲜度：缓存实现会让 root 符号链接换指向后旧目标继续可读。
        """
        settings: dict[str, Any] = {"readRoots": ["/srv/a"]}
        monkeypatch.setattr(
            computer,
            "_resolve_declared_settings",
            lambda: ResolvedSettings(settings=dict(settings), errors=[], per_scope_layers={}),
        )

        assert [r.configured for r in computer.read_roots] == ["/srv/a"]

        settings["readRoots"] = ["/srv/b"]

        assert [r.configured for r in computer.read_roots] == ["/srv/b"], "settings 变化必须立刻反映（不得缓存）"

    def test_protected_paths_includes_mcp_config_flag_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴1：``--mcp-config`` 文件（flag 层 mcp.json，可含 server 字面量 env/凭据）必须在拒绝集内."""
        mcp_cfg = tmp_path / "flag-mcp.json"
        mcp_cfg.write_text("{}", encoding="utf-8")
        computer = Computer(name="comp-test", blob_cache_root=tmp_path / "cache", mcp_flag_config=mcp_cfg)
        _grant(computer, monkeypatch, "/srv/docs")

        protected = computer.protected_paths()

        assert normalize_path(str(mcp_cfg)) in protected.paths

    def test_protected_paths_covers_flag_settings_and_tfrobot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """flag settings 与工作目录治理目录（``.tfrobot``）同样在拒绝集内（回归锁）."""
        flag_settings = tmp_path / "flag-settings.json"
        flag_settings.write_text("{}", encoding="utf-8")
        project_root = tmp_path / "cwd"
        project_root.mkdir()
        computer = Computer(
            name="comp-test",
            blob_cache_root=tmp_path / "cache",
            flag_settings_path=flag_settings,
            project_root=project_root,
        )
        _grant(computer, monkeypatch, "/srv/docs")

        protected = computer.protected_paths()

        assert normalize_path(str(flag_settings)) in protected.paths
        assert normalize_path(str(project_root / ".tfrobot")) in protected.paths

    @pytest.mark.asyncio
    async def test_unexpected_settings_error_returns_flat_500(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🟡4：``read_roots``/``protected_paths`` 抛未预期异常（如 OSError）→ flat ``500``（**必须 ack**）.

        异常逃出 async handler 会导致**不发 ack**、Agent 挂到自身超时；协议要求有 ack 通道即必须 ack。
        """

        def boom() -> Any:
            raise OSError("settings dir unreadable")

        monkeypatch.setattr(computer, "path_access_policy", boom)

        ret = await client.on_resolve_path(_resolve_req())  # type: ignore[arg-type]

        assert ret["code"] == 500  # type: ignore[index]
        assert ret["message"] == "Internal error"  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_path_pull_unexpected_error_returns_flat_500(
        self, computer: Computer, client: SMCPComputerClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🟡4（get_blob path 分支同款）：未预期异常 → flat ``500``，不挂死 Agent."""
        _patch_path_handle(monkeypatch)

        def boom() -> Any:
            raise OSError("settings dir unreadable")

        monkeypatch.setattr(computer, "path_access_policy", boom)

        ret = await client.on_get_blob(_blob_req())  # type: ignore[arg-type]

        assert ret["code"] == 500  # type: ignore[index]
