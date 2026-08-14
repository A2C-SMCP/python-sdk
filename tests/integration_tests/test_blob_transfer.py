# -*- coding: utf-8 -*-
# filename: test_blob_transfer.py
# @Author  : JQQ
# @Software: PyCharm

"""
通用二进制传输集成测试 / Integration tests for generic binary transfer.

协议依据 / Protocol:
  - a2c-smcp-protocol blob-transfer.md §3（并行幂等）/ §5（生产者契约）/ §5.4（句柄不信任）
  - error-handling.md §4018（flat ErrorPayload）

测试分层 / Test layering:
  - Part 1 (in-process)：真实 Computer + 真实 drain_blob，跨 socketio 抽象层的 call adapter，
    覆盖跨抽象边界的串行/并行/大 payload/真实 base64+sha256 round-trip。
  - Part 2 (real wire async)：真 Socket.IO transport + production Server 路由 (#41 已落地：
    ``MockComputerServerNamespace`` 自带 ``on_client_get_blob``) → 跨真实 wire 端到端。
  - Part 3 (real wire sync mirror)：多进程 + SyncSMCPNamespace + drain_blob_sync 并行
    （ThreadPoolExecutor）+ flat ErrorPayload 透传。

#41 历史背景 / History note:
  PR #44 (#38) 落地时 Server 还没有 ``on_client_get_blob`` 路由，测试曾通过 subclass 注入
  test-only forwarder 验证 wire 完整性；#41 (PR ↓) 后 ``SMCPNamespace`` / ``SyncSMCPNamespace``
  已携带 production 路由，本测试改用通用 mock 命名空间。
  Before #41, this file injected a test-only forwarder; #41 ships production routing, so this
  file now uses the standard mock namespaces.
"""

from __future__ import annotations

import base64
import hashlib
import multiprocessing
import socket
import time
from collections.abc import AsyncGenerator, Generator, Mapping
from multiprocessing import synchronize
from typing import Any, Literal

import pytest
from socketio import ASGIApp, AsyncClient, AsyncServer, Client
from werkzeug.serving import make_server

from a2c_smcp.computer.blob import BlobThresholds, BlobTooLargeError
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import (
    GET_BLOB_EVENT,
    JOIN_OFFICE_EVENT,
    SMCP_NAMESPACE,
    ErrorCode,
    GetBlobReq,
    GetBlobRet,
)
from a2c_smcp.testing import UvicornTestServer
from a2c_smcp.utils.blob import BlobTransferError, drain_blob, drain_blob_sync
from tests.integration_tests.mock_socketio_server import (
    MockComputerServerNamespace,
    MockComputerServerSyncNamespace,
)

# ======================================================================
# Part 1 — In-process integration (no Socket.IO)
# ======================================================================
#
# 用 in-memory call adapter 直接驱动 Computer.on_get_blob，验证 drain_blob 与 handler
# 在跨抽象边界（dict 序列化 / base64 编码 / sha256 自证）的完整性，不引入 Socket.IO 噪音。
# Uses an in-memory call adapter driving Computer.on_get_blob directly to verify drain_blob
# and the handler across the abstraction boundary (dict serialization, base64, sha256) without
# Socket.IO noise.


@pytest.fixture()
def computer_with_client(tmp_path) -> tuple[Computer, SMCPComputerClient]:
    """构造一个最小可用的 Computer + 绑定 client（不连接 Server）。"""
    comp = Computer(name="comp-blob-1", blob_cache_root=tmp_path)
    cli = SMCPComputerClient(computer=comp)
    return comp, cli


def _make_in_proc_call(client: SMCPComputerClient) -> Any:
    """构造 in-process 异步 call adapter，直连 client.on_get_blob。

    Build an in-process async call adapter that directly drives ``client.on_get_blob``.
    """

    async def call(computer: str, blob_handle: str, chunk_offset: int, max_chunk_bytes: int) -> Mapping[str, Any]:
        req: GetBlobReq = {
            "agent": "agent-it",
            "req_id": "r-it",
            "computer": computer,
            "blob_handle": blob_handle,
            "chunk_offset": chunk_offset,
            "max_chunk_bytes": max_chunk_bytes,
        }
        # Computer.on_get_blob 直接返回 dict（GetBlobRet 或 ErrorPayload）
        return await client.on_get_blob(req)

    return call


class TestInProcSerialDrain:
    """串行 drain_blob round-trip：mint → on_get_blob → drain_blob → 字节一致 + sha256 自证。"""

    @pytest.mark.asyncio
    async def test_single_chunk_blob(self, computer_with_client: tuple[Computer, SMCPComputerClient]) -> None:
        comp, cli = computer_with_client
        payload = b"hello, in-process blob"
        handle = comp.mint_toolspool_handle(payload=payload, mime="text/plain")
        call = _make_in_proc_call(cli)
        data, mime = await drain_blob(call, comp.name, handle)
        assert data == payload
        assert mime == "text/plain"

    @pytest.mark.asyncio
    async def test_multi_chunk_blob(self, computer_with_client: tuple[Computer, SMCPComputerClient]) -> None:
        comp, cli = computer_with_client
        # 80 KiB payload → 默认 chunk 256 KiB 单块；用 chunk_size=4096 强制多块拉取
        # 80 KiB payload → default chunk is 256 KiB; force chunked pull via chunk_size=4096
        payload = bytes((i * 31 + 7) % 256 for i in range(80 * 1024))
        handle = comp.mint_toolspool_handle(payload=payload, mime="application/octet-stream")
        call = _make_in_proc_call(cli)
        data, mime = await drain_blob(call, comp.name, handle, chunk_size=4096)
        assert hashlib.sha256(data).hexdigest() == hashlib.sha256(payload).hexdigest()
        assert data == payload
        assert mime == "application/octet-stream"


class TestInProcParallelDrain:
    """并行 drain_blob round-trip：concurrency=N 与串行结果完全一致（协议 §3 并行红利）."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("concurrency", [2, 4, 8])
    async def test_parallel_matches_serial(
        self,
        computer_with_client: tuple[Computer, SMCPComputerClient],
        concurrency: int,
    ) -> None:
        comp, cli = computer_with_client
        # 300 KiB random-ish payload — 强制多块（chunk=8 KiB → 37+ 块）
        payload = bytes((i * 17 + i // 257) % 256 for i in range(300 * 1024))
        expected_sha = hashlib.sha256(payload).hexdigest()
        handle = comp.mint_toolspool_handle(payload=payload, mime="image/png")
        call = _make_in_proc_call(cli)

        serial, _ = await drain_blob(call, comp.name, handle, chunk_size=8192, concurrency=1)
        parallel, _ = await drain_blob(call, comp.name, handle, chunk_size=8192, concurrency=concurrency)
        assert serial == payload
        assert parallel == payload
        assert hashlib.sha256(parallel).hexdigest() == expected_sha

    @pytest.mark.asyncio
    async def test_parallel_large_blob_round_trip(
        self,
        computer_with_client: tuple[Computer, SMCPComputerClient],
    ) -> None:
        """大 payload (2 MiB) 并行拉取 + 完整 sha256 自证 / Large 2 MiB blob parallel + sha256."""
        comp, cli = computer_with_client
        payload = (b"\x00\x01\x02\x03\x04\x05\x06\x07" * 32 * 1024)  # 2 MiB
        handle = comp.mint_toolspool_handle(payload=payload, mime="application/octet-stream")
        call = _make_in_proc_call(cli)
        data, _ = await drain_blob(call, comp.name, handle, chunk_size=64 * 1024, concurrency=4)
        assert data == payload
        assert hashlib.sha256(data).hexdigest() == hashlib.sha256(payload).hexdigest()


class TestInProcErrorReasons:
    """4018 各 reason 通过 drain_blob 时正确 raise BlobTransferError。"""

    @pytest.mark.asyncio
    async def test_invalid_handle_raises_through_drain(
        self,
        computer_with_client: tuple[Computer, SMCPComputerClient],
    ) -> None:
        _comp, cli = computer_with_client
        call = _make_in_proc_call(cli)
        with pytest.raises(BlobTransferError) as ei:
            await drain_blob(call, "comp-blob-1", "not!a!valid!handle")
        assert ei.value.reason == "invalid_handle"

    @pytest.mark.asyncio
    async def test_gone_reason_raises_through_drain(
        self,
        computer_with_client: tuple[Computer, SMCPComputerClient],
    ) -> None:
        from a2c_smcp.computer.blob import encode_toolspool_handle

        _comp, cli = computer_with_client
        # 合法 cid 形态但从未铸造 / Well-formed cid never minted
        handle = encode_toolspool_handle(cid="a" * 64, mime="image/png")
        call = _make_in_proc_call(cli)
        with pytest.raises(BlobTransferError) as ei:
            await drain_blob(call, "comp-blob-1", handle)
        assert ei.value.reason == "gone"


class TestInProcCrossInstanceCacheReuse:
    """跨 Computer 实例 cid 复用（"无状态可重解析" 承诺；同 cache_root 即可解析）.

    Cross-instance cid reuse (the "stateless reparseable" promise; same cache_root resolves).
    """

    @pytest.mark.asyncio
    async def test_second_computer_resolves_handle_minted_by_first(self, tmp_path) -> None:
        # 第一个 Computer 实例铸造句柄 / First Computer instance mints the handle
        comp1 = Computer(name="comp-mint", blob_cache_root=tmp_path)
        payload = b"persistent across computer instances"
        handle = comp1.mint_toolspool_handle(payload=payload, mime="application/octet-stream")
        # 第二个 Computer 实例（模拟重启 / 多 Computer 共享 cache_root）解析同一句柄
        # Second Computer instance (simulating restart / shared cache_root) resolves the same handle
        comp2 = Computer(name="comp-resolve", blob_cache_root=tmp_path)
        cli2 = SMCPComputerClient(computer=comp2)
        call = _make_in_proc_call(cli2)
        # 注意：comp 名匹配本实例 / Note: ``computer`` arg must match the resolving instance
        data, _ = await drain_blob(call, "comp-resolve", handle)
        assert data == payload


class TestInProcThresholdsAndDefense:
    """mint 入口 too_large 防御 + chunk clamp 行为的跨抽象集成验证."""

    @pytest.mark.asyncio
    async def test_mint_refuses_oversize_payload(self, tmp_path) -> None:
        comp = Computer(
            name="comp-cap",
            blob_cache_root=tmp_path,
            blob_thresholds=BlobThresholds(too_large_cap=64),
        )
        before = list(comp.toolspool_store.iter_cids())
        with pytest.raises(BlobTooLargeError):
            comp.mint_toolspool_handle(payload=b"x" * 256, mime="image/png")
        # 不变量：拒绝铸造时不应写盘 / Invariant: refused mint must not write
        assert list(comp.toolspool_store.iter_cids()) == before

    @pytest.mark.asyncio
    async def test_chunk_size_clamped_to_thresholds_via_drain(self, tmp_path) -> None:
        """drain_blob 客户建议远超 chunk_max_bytes → Computer clamp → 实际单块字节 ≤ clamp.
        Client suggests > chunk_max_bytes → Computer clamps → actual chunk bytes ≤ clamp."""
        comp = Computer(
            name="comp-clamp",
            blob_cache_root=tmp_path,
            blob_thresholds=BlobThresholds(chunk_max_bytes=1024),
        )
        cli = SMCPComputerClient(computer=comp)
        payload = b"X" * 4096
        handle = comp.mint_toolspool_handle(payload=payload, mime="application/octet-stream")
        call = _make_in_proc_call(cli)
        # 客户建议 99999 字节单块；Computer clamp 到 1024 → 实际拉 4 块
        # Client suggests 99999 bytes; Computer clamps to 1024 → 4 chunks
        data, _ = await drain_blob(call, comp.name, handle, chunk_size=99999, concurrency=1)
        assert data == payload


class TestInProcLazyInvariant:
    """v0.2.1 #51 lazy slice 端到端不变量：每 chunk 一次独立 ``read_chunk`` + 累计字节 ≈ ``total_size``.

    End-to-end lazy invariant (v0.2.1 #51): one ``read_chunk`` per chunk request; total bytes
    read across all chunks ≈ ``total_size`` (NOT ``N × total_size``).

    防 production ``on_get_blob`` / ``_process_get_blob`` 误回退到 ``resolved.payload[...]``：
    - 单测仍过（``.payload`` shim 工作 → ``slice(0, total_size)``）
    - 集成字节正确性测试仍过（base64 解码后 bytes 一致）
    - 但每次 chunk 请求都触发**全文件读**，内存峰值悄悄回归到 v0.2.1 #51 之前

    PR #52 fix-review 🟡1：reviewer 原方案 ``builtins.open count == 25`` 无法捕获此回退
    （``.payload`` shim 仍走 ``read_chunk`` → 仍 25 次 open）；本测试改监控 ``read_chunk``
    **累计字节数** 精确区分两种实现.
    """

    @pytest.mark.asyncio
    async def test_lazy_invariant_read_chunk_bytes_equal_total_size(
        self,
        monkeypatch: pytest.MonkeyPatch,
        computer_with_client: tuple[Computer, SMCPComputerClient],
    ) -> None:
        """drain N chunks → ``read_chunk`` 累计字节 ≈ ``total_size``，**非** ``N × total_size``.

        Drain N chunks → cumulative bytes read via ``read_chunk`` ≈ ``total_size``,
        NOT ``N × total_size`` (which would indicate ``.payload`` shim regression).
        """
        from a2c_smcp.computer.blob.toolspool import ToolspoolBlobStore

        comp, cli = computer_with_client
        total_size = 100 * 1024  # 100 KiB
        chunk_size = 4096
        expected_chunks = total_size // chunk_size  # 25

        payload = bytes((i * 31 + 7) % 256 for i in range(total_size))
        handle = comp.mint_toolspool_handle(payload=payload, mime="application/octet-stream")

        # 监控 read_chunk 累计字节 + 调用次数
        # Monitor read_chunk cumulative bytes + call count
        real_read_chunk = ToolspoolBlobStore.read_chunk
        cumulative_bytes = 0
        call_count = 0

        def tracking_read_chunk(self_store, cid, offset, length):  # type: ignore[no-untyped-def]
            nonlocal cumulative_bytes, call_count
            data = real_read_chunk(self_store, cid, offset, length)
            cumulative_bytes += len(data)
            call_count += 1
            return data

        monkeypatch.setattr(ToolspoolBlobStore, "read_chunk", tracking_read_chunk)

        call = _make_in_proc_call(cli)
        data, _ = await drain_blob(call, comp.name, handle, chunk_size=chunk_size)

        # 字节正确性（基线 sanity，独立于 lazy 验证）
        # Byte correctness (baseline sanity, independent of lazy verification)
        assert data == payload

        # 关键不变量：每 chunk 一次 read_chunk，累计字节恰好 == total_size
        # Key invariant: one read_chunk per chunk request; cumulative bytes == total_size
        assert call_count == expected_chunks, (
            f"expected {expected_chunks} read_chunk calls (one per chunk), got {call_count}"
        )
        assert cumulative_bytes == total_size, (
            f"LAZY INVARIANT VIOLATED: cumulative read_chunk bytes = {cumulative_bytes} "
            f"(expected ≈ {total_size}). If ≈ {expected_chunks * total_size}, this indicates "
            f"regression to .payload shim causing full-file read per chunk request."
        )


class TestInProcSkillBlobRoundTrip:
    """SKILL 生产者通道：``on_get_skill`` 铸句柄 → ``drain_blob`` 解析回源 → 字节/sha256 一致（#66）.

    验证设计 §4.4「资源字节三处一致」的端到端契约：``client:get_skill`` 铸造期上报的
    ``total_size`` / ``sha256``（消费字节基准）**必须**与 ``client:get_blob`` 重跑沙箱后回源、经
    ``drain_blob`` 重组的字节完全一致。SKILL.md 走 frontmatter 剥离后 body；子资源走原始字节。
    Verifies the get_skill mint and the get_blob resolve agree on consumed bytes end-to-end.
    """

    @staticmethod
    def _register(comp: Computer, pkg) -> None:
        assert comp.skill_registry.register({"name": "mcp:srv:demo", "source": "mcp:srv", "path": str(pkg)}) is True

    @staticmethod
    async def _get_skill(cli: SMCPComputerClient, name: str, rel_path: str | None = None) -> Mapping[str, Any]:
        from a2c_smcp.smcp import GetSkillReq

        req: GetSkillReq = {"agent": "agent-it", "req_id": "r-skill", "computer": cli.computer.name, "name": name}
        if rel_path is not None:
            req["rel_path"] = rel_path
        return await cli.on_get_skill(req)

    @pytest.mark.asyncio
    async def test_skill_md_over_budget_handle_drain_consistency(
        self,
        computer_with_client: tuple[Computer, SMCPComputerClient],
        tmp_path,
    ) -> None:
        comp, cli = computer_with_client
        pkg = tmp_path / "home" / "mcp" / "srv" / "demo"
        pkg.mkdir(parents=True)
        # body > 32 KiB inline 预算 → 铸句柄；frontmatter 剥离后 body == big_body
        big_body = "# Title\n\n" + ("lorem ipsum dolor sit amet 测试 中文\n" * 3000)
        (pkg / "SKILL.md").write_text(f"---\nname: demo\nversion: 1.0.0\n---\n{big_body}", encoding="utf-8")
        self._register(comp, pkg)

        ret = await self._get_skill(cli, "mcp:srv:demo")
        assert "blob_handle" in ret  # 超内联预算 → 铸句柄（非 body）
        assert "body" not in ret
        assert ret["mime_type"] == "text/markdown"

        expected = big_body.encode("utf-8")
        # get_skill 铸造期 sha256/total_size 基于剥离后 body
        assert ret["sha256"] == hashlib.sha256(expected).hexdigest()
        assert ret["total_size"] == len(expected)

        # get_blob 重跑沙箱回源（chunk_size 强制多块）→ 重组字节与 sha256 一致
        call = _make_in_proc_call(cli)
        data, mime = await drain_blob(call, comp.name, ret["blob_handle"], chunk_size=4096)
        assert data == expected
        assert mime == "text/markdown"
        assert hashlib.sha256(data).hexdigest() == ret["sha256"]

    @pytest.mark.asyncio
    async def test_binary_subresource_handle_drain_consistency(
        self,
        computer_with_client: tuple[Computer, SMCPComputerClient],
        tmp_path,
    ) -> None:
        comp, cli = computer_with_client
        pkg = tmp_path / "home" / "mcp" / "srv" / "demo"
        (pkg / "assets").mkdir(parents=True)
        (pkg / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")
        blob = bytes((i * 7 + 3) % 256 for i in range(50 * 1024))  # 50 KiB 二进制
        (pkg / "assets" / "img.png").write_bytes(blob)
        self._register(comp, pkg)

        ret = await self._get_skill(cli, "mcp:srv:demo", "assets/img.png")
        assert ret["mime_type"] == "image/png"
        assert "blob_handle" in ret  # 二进制 MIME 一律铸句柄
        assert ret["sha256"] == hashlib.sha256(blob).hexdigest()

        call = _make_in_proc_call(cli)
        data, mime = await drain_blob(call, comp.name, ret["blob_handle"], chunk_size=4096)
        assert data == blob  # 子资源 = 原始字节（不剥 frontmatter）
        assert mime == "image/png"


# ======================================================================
# Part 2 — Real Socket.IO transport (async)
# ======================================================================
#
# #41 落地后 SMCPNamespace 已携带 ``on_client_get_blob`` production 路由；本节直接复用
# 通用 ``MockComputerServerNamespace``（包装 ``SMCPNamespace`` 并附带操作录制），无需 test-only forwarder.
# After #41, ``SMCPNamespace`` carries production routing; this section reuses the standard
# ``MockComputerServerNamespace`` (which wraps ``SMCPNamespace`` with op recording).


def _create_blob_test_socketio() -> AsyncServer:
    sio = AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        ping_timeout=10,
        ping_interval=10,
        async_handlers=True,
    )
    sio.register_namespace(MockComputerServerNamespace())
    return sio


@pytest.fixture()
def real_blob_server_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
async def real_blob_server(real_blob_server_port: int) -> AsyncGenerator[None, None]:
    sio = _create_blob_test_socketio()
    sio.eio.start_service_task = False
    asgi_app = ASGIApp(sio, socketio_path="/socket.io")
    server = UvicornTestServer(asgi_app, port=real_blob_server_port)
    await server.up()
    try:
        yield
    finally:
        await server.down(force=True)


def _make_wire_call(agent_client: AsyncClient) -> Any:
    """将真实 socketio.AsyncClient.call 适配为 drain_blob 的 call 签名.
    Adapt real socketio.AsyncClient.call to drain_blob's call signature."""

    async def call(computer: str, blob_handle: str, chunk_offset: int, max_chunk_bytes: int) -> Mapping[str, Any]:
        req = {
            "agent": "agent-wire",
            "req_id": f"r-{chunk_offset}",
            "computer": computer,
            "blob_handle": blob_handle,
            "chunk_offset": chunk_offset,
            "max_chunk_bytes": max_chunk_bytes,
        }
        ack = await agent_client.call(GET_BLOB_EVENT, req, namespace=SMCP_NAMESPACE)
        assert isinstance(ack, dict)
        return ack

    return call


async def _agent_join(agent: AsyncClient, office_id: str, name: str) -> None:
    ok, err = await agent.call(
        JOIN_OFFICE_EVENT,
        {"role": "agent", "office_id": office_id, "name": name},
        namespace=SMCP_NAMESPACE,
    )
    assert ok and err is None


class TestRealWireAsync:
    """真 Socket.IO transport：base64 + ack + multi-chunk 跨 wire 完整性."""

    @pytest.mark.asyncio
    async def test_serial_full_round_trip_over_wire(
        self,
        real_blob_server: None,
        real_blob_server_port: int,
        tmp_path,
    ) -> None:
        office_id = "office-blob-wire-1"
        comp = Computer(name="comp-wire-1", blob_cache_root=tmp_path)
        comp_client = SMCPComputerClient(computer=comp)
        await comp_client.connect(
            f"http://localhost:{real_blob_server_port}",
            socketio_path="/socket.io",
            auth={"mock": "ok"},
            namespaces=[SMCP_NAMESPACE],
        )
        await comp_client.join_office(office_id)

        agent = AsyncClient()
        await agent.connect(
            f"http://localhost:{real_blob_server_port}",
            socketio_path="/socket.io",
            auth={"mock": "ok"},
            namespaces=[SMCP_NAMESPACE],
        )
        await _agent_join(agent, office_id, "agent-wire-1")
        try:
            payload = b"hello over the wire" * 1000  # ~19 KiB
            expected_sha = hashlib.sha256(payload).hexdigest()
            handle = comp.mint_toolspool_handle(payload=payload, mime="application/octet-stream")
            call = _make_wire_call(agent)
            data, mime = await drain_blob(call, comp.name, handle, chunk_size=4096, concurrency=1)
            assert data == payload
            assert hashlib.sha256(data).hexdigest() == expected_sha
            assert mime == "application/octet-stream"
        finally:
            await agent.disconnect()
            await comp_client.disconnect()

    @pytest.mark.asyncio
    async def test_parallel_full_round_trip_over_wire(
        self,
        real_blob_server: None,
        real_blob_server_port: int,
        tmp_path,
    ) -> None:
        office_id = "office-blob-wire-2"
        comp = Computer(name="comp-wire-2", blob_cache_root=tmp_path)
        comp_client = SMCPComputerClient(computer=comp)
        await comp_client.connect(
            f"http://localhost:{real_blob_server_port}",
            socketio_path="/socket.io",
            auth={"mock": "ok"},
            namespaces=[SMCP_NAMESPACE],
        )
        await comp_client.join_office(office_id)

        agent = AsyncClient()
        await agent.connect(
            f"http://localhost:{real_blob_server_port}",
            socketio_path="/socket.io",
            auth={"mock": "ok"},
            namespaces=[SMCP_NAMESPACE],
        )
        await _agent_join(agent, office_id, "agent-wire-2")
        try:
            payload = bytes((i * 13 + 5) % 256 for i in range(120 * 1024))  # 120 KiB
            expected_sha = hashlib.sha256(payload).hexdigest()
            handle = comp.mint_toolspool_handle(payload=payload, mime="image/png")
            call = _make_wire_call(agent)
            # 真 wire concurrency=4，跨 Socket.IO ack
            data, _ = await drain_blob(call, comp.name, handle, chunk_size=8192, concurrency=4)
            assert data == payload
            assert hashlib.sha256(data).hexdigest() == expected_sha
        finally:
            await agent.disconnect()
            await comp_client.disconnect()

    @pytest.mark.asyncio
    async def test_gone_over_wire_returns_4018(
        self,
        real_blob_server: None,
        real_blob_server_port: int,
        tmp_path,
    ) -> None:
        """跨 wire 真实返回 flat ErrorPayload 4018，drain_blob 转 BlobTransferError(gone)."""
        from a2c_smcp.computer.blob import encode_toolspool_handle

        office_id = "office-blob-wire-3"
        comp = Computer(name="comp-wire-3", blob_cache_root=tmp_path)
        comp_client = SMCPComputerClient(computer=comp)
        await comp_client.connect(
            f"http://localhost:{real_blob_server_port}",
            socketio_path="/socket.io",
            auth={"mock": "ok"},
            namespaces=[SMCP_NAMESPACE],
        )
        await comp_client.join_office(office_id)

        agent = AsyncClient()
        await agent.connect(
            f"http://localhost:{real_blob_server_port}",
            socketio_path="/socket.io",
            auth={"mock": "ok"},
            namespaces=[SMCP_NAMESPACE],
        )
        await _agent_join(agent, office_id, "agent-wire-3")
        try:
            # 合法 cid 形态但从未铸造 / Well-formed cid never minted → expect 4018 gone
            handle = encode_toolspool_handle(cid="b" * 64, mime="image/png")
            call = _make_wire_call(agent)
            with pytest.raises(BlobTransferError) as ei:
                await drain_blob(call, comp.name, handle)
            assert ei.value.reason == "gone"
        finally:
            await agent.disconnect()
            await comp_client.disconnect()


# ======================================================================
# Part 3 — Sync mirror (multi-process + SyncSMCPNamespace + drain_blob_sync)
# ======================================================================


_SYNC_OFFICE = "office-blob-sync"
_SYNC_COMPUTER = "comp-blob-sync"


def _create_sync_blob_socketio_app():
    """创建同步 Socket.IO + WSGI app（#41 production 路由已携带 ``on_client_get_blob``）.

    Build a sync Socket.IO + WSGI app using the production ``SyncSMCPNamespace``-backed mock.
    """
    import socketio

    sio = socketio.Server(
        async_mode="threading",
        cors_allowed_origins="*",
    )
    sio.register_namespace(MockComputerServerSyncNamespace())
    return sio, socketio.WSGIApp(sio, socketio_path="/socket.io")


@pytest.fixture()
def sync_blob_server_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run_sync_blob_server_process(port: int, ready_event: synchronize.Event) -> None:
    try:
        _sio, wsgi_app = _create_sync_blob_socketio_app()
        server = make_server("localhost", port, wsgi_app, threaded=True)
        ready_event.set()
        server.serve_forever()
    except Exception as e:  # pragma: no cover
        print(f"sync blob server process error: {e}")
        ready_event.set()


@pytest.fixture()
def sync_blob_server(sync_blob_server_port: int) -> Generator[int, Any, None]:
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_run_sync_blob_server_process,
        args=(sync_blob_server_port, ready),
        daemon=True,
    )
    proc.start()
    if not ready.wait(timeout=5):
        proc.terminate()
        proc.join(timeout=2)
        pytest.fail("同步 blob 服务器启动超时 / sync blob server startup timeout")
    try:
        yield sync_blob_server_port
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=3)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=1)


def _sync_join(client: Client, role: Literal["computer", "agent"], office_id: str, name: str) -> None:
    ok, err = client.call(
        JOIN_OFFICE_EVENT,
        {"role": role, "office_id": office_id, "name": name},
        namespace=SMCP_NAMESPACE,
    )
    assert ok and err is None


def _run_sync_computer_process(
    port: int,
    cache_dir: str,
    ready_q: multiprocessing.Queue,
    handle_q: multiprocessing.Queue,
    err_q: multiprocessing.Queue,
) -> None:
    """同步 mock Computer：复用 production ``_process_get_blob`` 核心逻辑.

    Sync mock Computer: reuses production ``_process_get_blob`` core; handler body is a
    pass-through wrapper, eliminating duplicate sync/async sync mirror maintenance.

    抽取自 PR #52 fix-review 🟡2：让 sync wire 测试与 async ``SMCPComputerClient.on_get_blob``
    走同一份纯函数实现（production handler body 本是同步代码），不再手写 decode + dispatch
    + slice 的镜像逻辑。
    Per PR #52 fix-review 🟡2: sync wire mock and async ``SMCPComputerClient.on_get_blob``
    now share a single pure-function implementation; the duplicated decode/dispatch/slice
    mirror logic is eliminated.
    """
    from pathlib import Path

    from a2c_smcp.computer.computer import Computer
    from a2c_smcp.computer.socketio.client import _process_get_blob

    # 真 Computer 实例（自动管理 toolspool_store + blob_resolvers + blob_thresholds）
    # Real Computer instance (auto-managed toolspool_store + blob_resolvers + blob_thresholds).
    computer_inst = Computer(name=_SYNC_COMPUTER, blob_cache_root=Path(cache_dir))

    # 在进程内铸造句柄 / Mint handle within this process
    payload = b"sync mirror round trip" * 100  # ~2 KiB
    handle = computer_inst.mint_toolspool_handle(
        payload=payload, mime="application/octet-stream",
    )
    expected_sha = hashlib.sha256(payload).hexdigest()
    handle_q.put({"handle": handle, "expected_sha": expected_sha, "size": len(payload)})

    computer = Client()

    @computer.on(GET_BLOB_EVENT, namespace=SMCP_NAMESPACE)
    def _on_get_blob(data: dict) -> dict:
        # 复用 production 同步核心；不再手写 decode + dispatch + slice 逻辑
        # Reuse production sync core; no more duplicated decode/dispatch/slice logic.
        ret = _process_get_blob(data, computer_inst)
        return dict(ret)

    try:
        computer.connect(
            f"http://localhost:{port}",
            namespaces=[SMCP_NAMESPACE],
            socketio_path="/socket.io",
        )
        _sync_join(computer, role="computer", office_id=_SYNC_OFFICE, name=_SYNC_COMPUTER)
        ready_q.put(_SYNC_COMPUTER)
        computer.wait()
    except Exception as e:
        err_q.put(f"sync computer error: {e}")
    finally:
        computer.disconnect()


def _run_sync_agent_process(
    port: int,
    handle_info: dict,
    result_q: multiprocessing.Queue,
    err_q: multiprocessing.Queue,
) -> None:
    """同步 Agent：用 drain_blob_sync 跨真 wire 拉取，验证串行 + 并行 + sha256."""
    try:
        agent = Client()
        agent.connect(
            f"http://localhost:{port}",
            namespaces=[SMCP_NAMESPACE],
            socketio_path="/socket.io",
        )
        _sync_join(agent, role="agent", office_id=_SYNC_OFFICE, name="agent-sync-blob")
        time.sleep(0.3)

        def call(computer: str, blob_handle: str, chunk_offset: int, max_chunk_bytes: int) -> Mapping[str, Any]:
            ack = agent.call(
                GET_BLOB_EVENT,
                {
                    "agent": "agent-sync-blob",
                    "req_id": f"r-{chunk_offset}",
                    "computer": computer,
                    "blob_handle": blob_handle,
                    "chunk_offset": chunk_offset,
                    "max_chunk_bytes": max_chunk_bytes,
                },
                namespace=SMCP_NAMESPACE,
            )
            assert isinstance(ack, dict)
            return ack

        # 串行 / Serial
        serial_data, serial_mime = drain_blob_sync(call, _SYNC_COMPUTER, handle_info["handle"], chunk_size=512)
        # 并行 / Parallel (ThreadPoolExecutor)
        parallel_data, _ = drain_blob_sync(
            call,
            _SYNC_COMPUTER,
            handle_info["handle"],
            chunk_size=512,
            concurrency=4,
        )
        result_q.put({
            "serial_sha": hashlib.sha256(serial_data).hexdigest(),
            "parallel_sha": hashlib.sha256(parallel_data).hexdigest(),
            "serial_size": len(serial_data),
            "parallel_size": len(parallel_data),
            "serial_mime": serial_mime,
            "expected_sha": handle_info["expected_sha"],
            "expected_size": handle_info["size"],
        })
        agent.disconnect()
    except Exception as e:
        err_q.put(f"sync agent error: {e}")


def test_blob_transfer_sync_wire_round_trip(sync_blob_server: int, tmp_path) -> None:
    """同步全栈：mint(进程 A) → SyncSMCPNamespace 路由 → drain_blob_sync 串行+并行(进程 B) → sha256 一致.
    Sync full-stack: mint(proc A) → SyncSMCPNamespace relay → drain_blob_sync serial+parallel(proc B)."""
    port = sync_blob_server
    cache_dir = str(tmp_path)
    ready_q: multiprocessing.Queue = multiprocessing.Queue()
    handle_q: multiprocessing.Queue = multiprocessing.Queue()
    result_q: multiprocessing.Queue = multiprocessing.Queue()
    err_q: multiprocessing.Queue = multiprocessing.Queue()

    comp_proc = multiprocessing.Process(
        target=_run_sync_computer_process,
        args=(port, cache_dir, ready_q, handle_q, err_q),
        daemon=True,
    )
    comp_proc.start()
    try:
        try:
            ready_q.get(timeout=8)
        except Exception:
            if not err_q.empty():
                pytest.fail(f"Mock Computer 启动失败: {err_q.get()}")
            pytest.fail("Mock Computer 启动超时")

        handle_info = handle_q.get(timeout=3)

        agent_proc = multiprocessing.Process(
            target=_run_sync_agent_process,
            args=(port, handle_info, result_q, err_q),
            daemon=True,
        )
        agent_proc.start()
        try:
            try:
                res = result_q.get(timeout=20)
            except Exception:
                if not err_q.empty():
                    pytest.fail(f"同步 Agent 执行失败: {err_q.get()}")
                pytest.fail("同步 Agent 执行超时")

            # 串行与并行结果必须一致 + 与铸造时 sha256 完全相同
            # Serial and parallel results must match each other and the minted sha256
            assert res["serial_sha"] == res["expected_sha"]
            assert res["parallel_sha"] == res["expected_sha"]
            assert res["serial_size"] == res["expected_size"]
            assert res["parallel_size"] == res["expected_size"]
            assert res["serial_mime"] == "application/octet-stream"
        finally:
            if agent_proc.is_alive():
                agent_proc.terminate()
                agent_proc.join(timeout=2)
    finally:
        if comp_proc.is_alive():
            comp_proc.terminate()
            comp_proc.join(timeout=2)
