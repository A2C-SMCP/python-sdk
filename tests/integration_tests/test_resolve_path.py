# -*- coding: utf-8 -*-
# filename: test_resolve_path.py
# @Author  : JQQ
# @Software: PyCharm
"""
``client:resolve_path`` 端到端集成测试（v0.5.0 #236 / protocol#65）.
End-to-end integration tests for ``client:resolve_path`` + ``kind=path`` pulls.

链路 / Stack：真实 ``Computer``（真实 settings 解析 + 真实 path core）→ ``SMCPComputerClient``
→ 真实 ``SMCPNamespace``（经 mock 测试服务器）→ 真实 ``AsyncSMCPAgentClient``。

协议依据 / Protocol: ``blob-transfer.md`` §生产者三：按路径读取；``error-handling.md`` §4020 / §4018；
``security.md`` §读取通道安全。

覆盖 / Coverage（Issue #236 验收清单的**事件级**部分；纯 path-core 行为归流 1 的
``test_path_resolver.py``）：
  - ``readRoots`` 未配置 → ``4020 forbidden``
  - 授权内 resolve → 元数据（fs 维度的 sha256 / size / mime / canonical_path）→ 分块 ``get_blob``
    重组后字节与 ``sha256`` 一致
  - 越界（存在 / 不存在）→ ``forbidden``（**不泄露存在性**）
  - 授权内不存在 → ``not_found``；目录 → ``not_a_file``
  - resolve 后删除 → ``4018 gone``；改写（尺寸变化）→ ``4018 gone``
  - 撤权（settings 改写 + ``refresh_read_roots``）→ ``4018 forbidden``
  - ``project`` scope 提供 ``readRoots`` → 整字段不生效（仍 ``forbidden``）
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from socketio import ASGIApp

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, SMCP_NAMESPACE
from a2c_smcp.testing import UvicornTestServer
from a2c_smcp.utils.path_norm import normalize_path
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio

COMPUTER = "comp-resolve-1"
AGENT = "robot-resolve-1"
OFFICE = "office-resolve-1"


@pytest.fixture
def server_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def socketio_server(server_port: int) -> AsyncGenerator[None, None]:
    sio = create_computer_test_socketio()
    sio.eio.start_service_task = False
    server = UvicornTestServer(ASGIApp(sio, socketio_path="/socket.io"), port=server_port)
    await server.up()
    try:
        yield
    finally:
        await server.down(force=True)


def _write_user_settings(cfg_home: Path, settings: dict[str, Any]) -> None:
    """写 user scope settings（``$XDG_CONFIG_HOME/a2c/settings.json``）."""
    target = cfg_home / "a2c" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings), encoding="utf-8")


@asynccontextmanager
async def _stack(
    tmp_path: Path,
    port: int,
    *,
    read_roots: Sequence[str] | None,
    project_settings: dict[str, Any] | None = None,
) -> AsyncGenerator[tuple[Computer, AsyncSMCPAgentClient], None]:
    """真实 Computer + 真实 Server 路由 + 真实 Agent 的端到端栈."""
    cfg_home = tmp_path / "cfg"
    if read_roots is not None:
        _write_user_settings(cfg_home, {"readRoots": list(read_roots)})

    # project/local scope 锚定独立 cwd（避免污染仓库工作区）
    project_root = tmp_path / "cwd"
    (project_root / ".tfrobot").mkdir(parents=True, exist_ok=True)
    if project_settings is not None:
        (project_root / ".tfrobot" / "settings.json").write_text(json.dumps(project_settings), encoding="utf-8")

    env = {**os.environ, "XDG_CONFIG_HOME": str(cfg_home)}
    computer = Computer(
        name=COMPUTER,
        mcp_servers=set(),
        auto_connect=False,
        env=env,
        project_root=project_root,
        blob_cache_root=tmp_path / "blobcache",
    )
    comp_client = SMCPComputerClient(computer=computer)
    url = f"http://localhost:{port}"
    await comp_client.connect(
        url,
        socketio_path="/socket.io",
        headers={"mock_header": "mock_value"},
        auth={"mock_header": "mock_value"},
        namespaces=[SMCP_NAMESPACE],
    )
    await comp_client.join_office(OFFICE)

    agent = AsyncSMCPAgentClient(auth_provider=DefaultAgentAuthProvider(agent_id=AGENT, office_id=OFFICE))
    await agent.connect_to_server(url, namespace=SMCP_NAMESPACE, socketio_path="/socket.io")
    await agent.emit(
        JOIN_OFFICE_EVENT,
        {"role": "agent", "office_id": OFFICE, "name": AGENT},
        namespace=SMCP_NAMESPACE,
    )
    try:
        yield computer, agent
    finally:
        await agent.disconnect()
        await comp_client.disconnect()


def _assert_protocol_error(exc: pytest.ExceptionInfo[SMCPProtocolError], code: int, reason: str) -> None:
    assert exc.value.code == code, f"期望 {code}，实际 {exc.value.code}（{exc.value.error_message}）"
    assert exc.value.reason == reason


@pytest.mark.asyncio
async def test_resolve_then_chunked_pull_roundtrip(tmp_path: Path, socketio_server: None, server_port: int) -> None:
    """授权内：resolve 元数据自洽 → 分块 get_blob 重组字节与 sha256 一致."""
    root = tmp_path / "root"
    root.mkdir()
    target = root / "a.txt"
    payload = b"hello world from resolve_path"
    target.write_bytes(payload)

    async with _stack(tmp_path, server_port, read_roots=[str(root)]) as (computer, agent):
        ret = await agent.resolve_path(COMPUTER, str(target))

        assert ret["canonical_path"] == normalize_path(str(target))
        assert ret["total_size"] == len(payload)
        assert ret["sha256"] == hashlib.sha256(payload).hexdigest()
        assert ret["mime_type"].startswith("text/")
        assert ret["mtime"] > 0
        assert isinstance(ret["blob_handle"], str) and ret["blob_handle"]

        chunks: list[bytes] = []
        offset = 0
        while True:
            got = await agent.get_blob(COMPUTER, ret["blob_handle"], chunk_offset=offset, max_chunk_bytes=4)
            assert got["sha256"] == ret["sha256"]  # 与 resolve 期一致
            chunks.append(base64.b64decode(got["blob"]))
            if got["eof"]:
                break
            offset += len(chunks[-1])

        assert b"".join(chunks) == payload

        # canonical_path 可直接回用作 path（协议 MUST）/ canonical_path round-trips as path
        again = await agent.resolve_path(COMPUTER, ret["canonical_path"])
        assert again["canonical_path"] == ret["canonical_path"]
        assert again["sha256"] == ret["sha256"]


@pytest.mark.asyncio
async def test_empty_read_roots_is_forbidden(tmp_path: Path, socketio_server: None, server_port: int) -> None:
    """``readRoots`` 未配置（默认空）= 不可读 → ``4020 forbidden``（fail-closed）."""
    target = tmp_path / "anything.txt"
    target.write_bytes(b"x")

    async with _stack(tmp_path, server_port, read_roots=None) as (_computer, agent):
        with pytest.raises(SMCPProtocolError) as exc:
            await agent.resolve_path(COMPUTER, str(target))
        _assert_protocol_error(exc, 4020, "forbidden")


@pytest.mark.asyncio
async def test_outside_read_roots_is_forbidden_without_leaking_existence(
    tmp_path: Path, socketio_server: None, server_port: int
) -> None:
    """越界：存在与不存在**同一** ``forbidden``（越界判定先于存在性探测，不泄露存在性）."""
    root = tmp_path / "root"
    root.mkdir()
    existing = tmp_path / "outside-exists.txt"
    existing.write_bytes(b"secret")
    missing = tmp_path / "outside-missing.txt"

    async with _stack(tmp_path, server_port, read_roots=[str(root)]) as (_computer, agent):
        for candidate in (existing, missing):
            with pytest.raises(SMCPProtocolError) as exc:
                await agent.resolve_path(COMPUTER, str(candidate))
            _assert_protocol_error(exc, 4020, "forbidden")


@pytest.mark.asyncio
async def test_missing_inside_root_is_not_found(tmp_path: Path, socketio_server: None, server_port: int) -> None:
    """授权内不存在 → ``not_found``（与越界可区分）."""
    root = tmp_path / "root"
    root.mkdir()

    async with _stack(tmp_path, server_port, read_roots=[str(root)]) as (_computer, agent):
        with pytest.raises(SMCPProtocolError) as exc:
            await agent.resolve_path(COMPUTER, str(root / "nope.txt"))
        _assert_protocol_error(exc, 4020, "not_found")


@pytest.mark.asyncio
async def test_directory_is_not_a_file(tmp_path: Path, socketio_server: None, server_port: int) -> None:
    """授权内目录 → ``not_a_file``."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)

    async with _stack(tmp_path, server_port, read_roots=[str(root)]) as (_computer, agent):
        with pytest.raises(SMCPProtocolError) as exc:
            await agent.resolve_path(COMPUTER, str(root / "sub"))
        _assert_protocol_error(exc, 4020, "not_a_file")


@pytest.mark.asyncio
async def test_delete_after_resolve_makes_pull_gone(tmp_path: Path, socketio_server: None, server_port: int) -> None:
    """resolve 后删除 → 拉取期 ``4018 gone``（``4018`` 枚举不变）."""
    root = tmp_path / "root"
    root.mkdir()
    target = root / "gone.txt"
    target.write_bytes(b"bye")

    async with _stack(tmp_path, server_port, read_roots=[str(root)]) as (_computer, agent):
        ret = await agent.resolve_path(COMPUTER, str(target))
        target.unlink()

        with pytest.raises(SMCPProtocolError) as exc:
            await agent.get_blob(COMPUTER, ret["blob_handle"])
        _assert_protocol_error(exc, 4018, "gone")


@pytest.mark.asyncio
async def test_rewrite_after_resolve_makes_pull_gone(tmp_path: Path, socketio_server: None, server_port: int) -> None:
    """resolve 后改写（尺寸变化 → stat 键不符）→ ``4018 gone``."""
    root = tmp_path / "root"
    root.mkdir()
    target = root / "rewritten.txt"
    target.write_bytes(b"original")

    async with _stack(tmp_path, server_port, read_roots=[str(root)]) as (_computer, agent):
        ret = await agent.resolve_path(COMPUTER, str(target))
        target.write_bytes(b"rewritten-content-is-longer")

        with pytest.raises(SMCPProtocolError) as exc:
            await agent.get_blob(COMPUTER, ret["blob_handle"])
        _assert_protocol_error(exc, 4018, "gone")


@pytest.mark.asyncio
async def test_revoke_after_resolve_makes_pull_forbidden(
    tmp_path: Path, socketio_server: None, server_port: int
) -> None:
    """撤权（``readRoots`` 改写 + 宿主使缓存失效）→ 拉取期 ``4018 forbidden``.

    缓存语义：``read_roots`` 已配置时进程内缓存（与 ``landingRoot`` 同款，运行期改值需重启）；
    宿主支持热更新时 MUST 调 ``Computer.refresh_read_roots()``——本用例即模拟该路径。
    """
    root = tmp_path / "root"
    (root / "other").mkdir(parents=True)
    target = root / "revoked.txt"
    target.write_bytes(b"revocable")

    async with _stack(tmp_path, server_port, read_roots=[str(root)]) as (computer, agent):
        ret = await agent.resolve_path(COMPUTER, str(target))

        # 撤权：settings 改为不含该文件的根 + 使缓存失效
        _write_user_settings(tmp_path / "cfg", {"readRoots": [str(root / "other")]})
        computer.refresh_read_roots()

        with pytest.raises(SMCPProtocolError) as exc:
            await agent.get_blob(COMPUTER, ret["blob_handle"])
        _assert_protocol_error(exc, 4018, "forbidden")

        # 新 resolve 同样被拒（授权已收回）
        with pytest.raises(SMCPProtocolError) as exc2:
            await agent.resolve_path(COMPUTER, str(target))
        _assert_protocol_error(exc2, 4020, "forbidden")


@pytest.mark.asyncio
async def test_external_settings_rewrite_revokes_without_refresh(
    tmp_path: Path, socketio_server: None, server_port: int
) -> None:
    """**外部进程**改写 settings（不调用 ``refresh_read_roots``）→ 下一次 ``get_blob`` 即 ``4018 forbidden``.

    协议 MUST：拉取期按**当前** ``readRoots`` 复核，撤权不得依赖宿主显式通知——
    ``a2c-computer settings set`` 由另一个进程写文件时，运行中的 Computer 不会被回调。
    """
    root = tmp_path / "root"
    (root / "other").mkdir(parents=True)
    target = root / "external-revoked.txt"
    target.write_bytes(b"revocable")

    async with _stack(tmp_path, server_port, read_roots=[str(root)]) as (_computer, agent):
        ret = await agent.resolve_path(COMPUTER, str(target))

        # 外部改写配置文件；不触碰任何 Computer 实例状态
        _write_user_settings(tmp_path / "cfg", {"readRoots": [str(root / "other")]})

        with pytest.raises(SMCPProtocolError) as exc:
            await agent.get_blob(COMPUTER, ret["blob_handle"])
        _assert_protocol_error(exc, 4018, "forbidden")


@pytest.mark.asyncio
async def test_project_scope_read_roots_is_ignored(tmp_path: Path, socketio_server: None, server_port: int) -> None:
    """``project`` scope（入 git）提供 ``readRoots`` → **整字段不生效**，仍不可读."""
    root = tmp_path / "root"
    root.mkdir()
    target = root / "a.txt"
    target.write_bytes(b"x")

    async with _stack(
        tmp_path, server_port, read_roots=None, project_settings={"readRoots": [str(root)]}
    ) as (_computer, agent):
        with pytest.raises(SMCPProtocolError) as exc:
            await agent.resolve_path(COMPUTER, str(target))
        _assert_protocol_error(exc, 4020, "forbidden")
