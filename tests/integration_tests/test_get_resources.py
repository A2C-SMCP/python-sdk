# -*- coding: utf-8 -*-
# filename: test_get_resources.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：``client:get_resources`` 端到端集成测试，覆盖协议指南 §6.3 测试矩阵。
English: End-to-end integration tests for ``client:get_resources``, covering protocol guide §6.3 matrix.

协议依据 / Protocol: a2c-smcp-protocol v0.2.x
  - events.md#client:get_resources（透明转发 MCP resources/list + cursor 翻页）
  - error-handling.md（flat ErrorPayload：4014 / 4015，无嵌套 envelope）
  - migrations/v0.2-uri-metadata-refactor.md §6.3 测试矩阵

Part 1（异步全链路真实栈）：真实 Computer + MCPServerManager + 真实 stdio MCP Server
  → SMCPComputerClient → 真实 SMCPNamespace → 真实 AsyncSMCPAgentClient。
Part 2（同步镜像）：真实 SyncSMCPNamespace（多进程）+ 真实 SMCPAgentClient.get_resources
  + Mock Computer 应答（验证同步路由与 flat ErrorPayload 透传 / SMCPProtocolError）。
"""

from __future__ import annotations

import multiprocessing
import socket
import sys
import time
from collections.abc import AsyncGenerator, Generator
from multiprocessing import synchronize
from pathlib import Path
from typing import Any, Literal

import pytest
from mcp import StdioServerParameters
from socketio import ASGIApp, AsyncClient, Client
from werkzeug.serving import make_server

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.smcp import GET_RESOURCES_EVENT, JOIN_OFFICE_EVENT, SMCP_NAMESPACE
from tests.integration_tests.computer.socketio.mock_uv_server import UvicornTestServer
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio
from tests.integration_tests.server._local_sync_server import create_local_sync_server

MCP_SERVERS_DIR = Path(__file__).resolve().parent / "computer" / "mcp_servers"
PAGED_SERVER = MCP_SERVERS_DIR / "resources_paged_mixed_stdio_server.py"
TOOLS_ONLY_SERVER = MCP_SERVERS_DIR / "tool_stdio_server.py"


# ======================================================================
# Part 1 — 异步全链路真实栈 / Async full real stack
# ======================================================================


@pytest.fixture
def basic_server_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def socketio_server(basic_server_port: int) -> AsyncGenerator[None, None]:
    """基于真实 SMCPNamespace 的测试服务器 / Test server backed by the real SMCPNamespace."""
    sio = create_computer_test_socketio()
    sio.eio.start_service_task = False
    asgi_app = ASGIApp(sio, socketio_path="/socket.io")
    server = UvicornTestServer(asgi_app, port=basic_server_port)
    await server.up()
    try:
        yield
    finally:
        await server.down(force=True)


async def _agent_join(agent: AsyncSMCPAgentClient, office_id: str, name: str) -> None:
    await agent.emit(
        JOIN_OFFICE_EVENT,
        {"role": "agent", "office_id": office_id, "name": name},
        namespace=SMCP_NAMESPACE,
    )


async def _make_booted_computer(name: str) -> Computer:
    """真实 Computer：paged-srv 具备 resources 能力且分页；tools-srv 仅 tools 能力（触发 4015）。"""
    paged_cfg = StdioServerConfig(
        name="paged-srv",
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(PAGED_SERVER)]),
    )
    tools_cfg = StdioServerConfig(
        name="tools-srv",
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(TOOLS_ONLY_SERVER)]),
    )
    computer = Computer(name=name, mcp_servers={paged_cfg, tools_cfg})
    await computer.boot_up()
    return computer


@pytest.mark.asyncio
async def test_get_resources_success_and_cursor_pagination(socketio_server: None, basic_server_port: int) -> None:
    """§6.3 ✅ 合法 MCP Server 返回 {resources, next_cursor}；✅ cursor 翻页正确处理；透明转发不过滤非 window。"""
    office_id = "office-res-1"
    computer = await _make_booted_computer("comp-res-1")
    comp_client = SMCPComputerClient(computer=computer)
    await comp_client.connect(
        f"http://localhost:{basic_server_port}",
        socketio_path="/socket.io",
        headers={"mock_header": "mock_value"},
        auth={"mock_header": "mock_value"},
        namespaces=[SMCP_NAMESPACE],
    )
    await comp_client.join_office(office_id)

    auth = DefaultAgentAuthProvider(agent_id="robot-res-1", office_id=office_id)
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    await agent.connect_to_server(
        f"http://localhost:{basic_server_port}",
        namespace=SMCP_NAMESPACE,
        socketio_path="/socket.io",
    )
    await _agent_join(agent, office_id, "robot-res-1")

    try:
        # 第 1 页：cursor=None → PAGE1 单个 window 资源，next_cursor="page2"
        page1 = await agent.get_resources(computer="comp-res-1", mcp_server="paged-srv")
        assert page1["next_cursor"] == "page2"
        assert len(page1["resources"]) == 1
        r0 = page1["resources"][0]
        assert r0["uri"].startswith("window://example.desktop.paged/p1")
        assert r0["name"] == "P1"
        # snake_case 字段规整 / camelCase→snake_case normalization
        assert r0["mime_type"] == "text/markdown"
        assert "mimeType" not in r0

        # 第 2 页：cursor="page2" → 含非 window(file://) + window，末页无 next_cursor（透明转发，不过滤）
        page2 = await agent.get_resources(computer="comp-res-1", mcp_server="paged-srv", cursor="page2")
        assert "next_cursor" not in page2
        uris = {r["uri"] for r in page2["resources"]}
        assert any(u.startswith("file://") for u in uris), "透明转发：非 window 资源不得被过滤"
        assert any(u.startswith("window://example.desktop.paged/p2") for u in uris)
        assert len(page2["resources"]) == 2
    finally:
        await agent.disconnect()
        await comp_client.disconnect()
        await computer.shutdown()


@pytest.mark.asyncio
async def test_get_resources_unregistered_server_raises_4014(socketio_server: None, basic_server_port: int) -> None:
    """§6.3 ❌ 引用未注册 server → 4014 MCP Server Not Found（flat ErrorPayload → SMCPProtocolError）。"""
    office_id = "office-res-2"
    computer = await _make_booted_computer("comp-res-2")
    comp_client = SMCPComputerClient(computer=computer)
    await comp_client.connect(
        f"http://localhost:{basic_server_port}",
        socketio_path="/socket.io",
        headers={"mock_header": "mock_value"},
        auth={"mock_header": "mock_value"},
        namespaces=[SMCP_NAMESPACE],
    )
    await comp_client.join_office(office_id)

    auth = DefaultAgentAuthProvider(agent_id="robot-res-2", office_id=office_id)
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    await agent.connect_to_server(
        f"http://localhost:{basic_server_port}",
        namespace=SMCP_NAMESPACE,
        socketio_path="/socket.io",
    )
    await _agent_join(agent, office_id, "robot-res-2")

    try:
        with pytest.raises(SMCPProtocolError) as ei:
            await agent.get_resources(computer="comp-res-2", mcp_server="does-not-exist")
        assert ei.value.code == 4014
        assert ei.value.mcp_server_name == "does-not-exist"
    finally:
        await agent.disconnect()
        await comp_client.disconnect()


@pytest.mark.asyncio
async def test_get_resources_capability_missing_raises_4015(socketio_server: None, basic_server_port: int) -> None:
    """§6.3 ❌ MCP Server 不支持 resources capability → 4015（flat ErrorPayload，含 capability 字段）。"""
    office_id = "office-res-3"
    computer = await _make_booted_computer("comp-res-3")
    comp_client = SMCPComputerClient(computer=computer)
    await comp_client.connect(
        f"http://localhost:{basic_server_port}",
        socketio_path="/socket.io",
        headers={"mock_header": "mock_value"},
        auth={"mock_header": "mock_value"},
        namespaces=[SMCP_NAMESPACE],
    )
    await comp_client.join_office(office_id)

    auth = DefaultAgentAuthProvider(agent_id="robot-res-3", office_id=office_id)
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    await agent.connect_to_server(
        f"http://localhost:{basic_server_port}",
        namespace=SMCP_NAMESPACE,
        socketio_path="/socket.io",
    )
    await _agent_join(agent, office_id, "robot-res-3")

    try:
        with pytest.raises(SMCPProtocolError) as ei:
            await agent.get_resources(computer="comp-res-3", mcp_server="tools-srv")
        assert ei.value.code == 4015
        assert ei.value.mcp_server_name == "tools-srv"
        assert ei.value.capability == "resources"
    finally:
        await agent.disconnect()
        await comp_client.disconnect()


# ======================================================================
# Part 2 — 同步镜像：真实 SyncSMCPNamespace（多进程）+ 真实 SMCPAgentClient
# ======================================================================

_SYNC_OFFICE = "office-sync-res"
_SYNC_COMPUTER = "comp-sync-res"


@pytest.fixture
def sync_server_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run_sync_server_process(port: int, ready_event: synchronize.Event) -> None:
    try:
        sio, _ns, wsgi_app = create_local_sync_server()
        sio.eio.start_service_task = False
        server = make_server("localhost", port, wsgi_app, threaded=True)
        ready_event.set()
        server.serve_forever()
    except Exception as e:  # pragma: no cover - 进程内异常兜底
        print(f"sync server process error: {e}")
        ready_event.set()


@pytest.fixture
def sync_smcp_server(sync_server_port: int) -> Generator[int, Any, None]:
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(target=_run_sync_server_process, args=(sync_server_port, ready), daemon=True)
    proc.start()
    if not ready.wait(timeout=5):
        proc.terminate()
        proc.join(timeout=2)
        pytest.fail("同步服务器进程启动超时 / sync server process startup timeout")
    try:
        yield sync_server_port
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


def _run_mock_computer_process(port: int, ready_q: multiprocessing.Queue, err_q: multiprocessing.Queue) -> None:
    """Mock Computer：paged-srv 返回分页 GetResourcesRet；no-res 返回 flat 4015 ErrorPayload。"""
    computer = Client()

    @computer.on(GET_RESOURCES_EVENT, namespace=SMCP_NAMESPACE)
    def _on_get_resources(data: dict) -> dict:  # noqa: ANN001
        mcp_server = data["mcp_server"]
        if mcp_server == "does-not-exist":
            # 镜像 async test_get_resources_unregistered_server_raises_4014：
            # 未注册 server → flat 4014 ErrorPayload（无 capability 字段）
            return {
                "code": 4014,
                "message": "MCP Server not found",
                "mcp_server_name": "does-not-exist",
            }
        if mcp_server == "no-res":
            return {
                "code": 4015,
                "message": "MCP Server does not support 'resources' capability",
                "mcp_server_name": "no-res",
                "capability": "resources",
            }
        if data.get("cursor") == "page2":
            return {
                "resources": [{"uri": "file://tmp/s.txt", "name": "S"}],
                "req_id": data["req_id"],
            }
        return {
            "resources": [{"uri": "window://h/p1", "name": "P1", "mime_type": "text/markdown"}],
            "next_cursor": "page2",
            "req_id": data["req_id"],
        }

    try:
        computer.connect(f"http://localhost:{port}", namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
        _sync_join(computer, role="computer", office_id=_SYNC_OFFICE, name=_SYNC_COMPUTER)
        ready_q.put(_SYNC_COMPUTER)
        computer.wait()
    except Exception as e:
        err_q.put(f"mock computer error: {e}")
    finally:
        computer.disconnect()


def _run_sync_agent_process(port: int, result_q: multiprocessing.Queue, err_q: multiprocessing.Queue) -> None:
    """真实 SMCPAgentClient.get_resources：验证同步翻页 + flat ErrorPayload → SMCPProtocolError。"""
    try:
        auth = DefaultAgentAuthProvider(agent_id="robot-sync-res", office_id=_SYNC_OFFICE)
        agent = SMCPAgentClient(auth_provider=auth)
        agent.connect_to_server(f"http://localhost:{port}", namespace=SMCP_NAMESPACE, socketio_path="/socket.io")
        agent.join_office(office_id=_SYNC_OFFICE, agent_name="robot-sync-res", namespace=SMCP_NAMESPACE)
        time.sleep(0.3)

        page1 = agent.get_resources(computer=_SYNC_COMPUTER, mcp_server="paged-srv")
        page2 = agent.get_resources(computer=_SYNC_COMPUTER, mcp_server="paged-srv", cursor="page2")

        err_code: int | None = None
        capability: str | None = None
        try:
            agent.get_resources(computer=_SYNC_COMPUTER, mcp_server="no-res")
        except SMCPProtocolError as e:
            err_code = e.code
            capability = e.capability

        # 镜像 async test_get_resources_unregistered_server_raises_4014：
        # 未注册 server → flat 4014 ErrorPayload 经 SyncSMCPNamespace 透传 → SMCPProtocolError
        err4014_code: int | None = None
        err4014_server: str | None = None
        try:
            agent.get_resources(computer=_SYNC_COMPUTER, mcp_server="does-not-exist")
        except SMCPProtocolError as e:
            err4014_code = e.code
            err4014_server = e.mcp_server_name

        result_q.put(
            {
                "page1_next": page1.get("next_cursor"),
                "page1_uri": page1["resources"][0]["uri"],
                "page2_has_next": "next_cursor" in page2,
                "page2_uri": page2["resources"][0]["uri"],
                "err_code": err_code,
                "capability": capability,
                "err4014_code": err4014_code,
                "err4014_server": err4014_server,
            },
        )
        agent.disconnect()
    except Exception as e:
        err_q.put(f"sync agent error: {e}")


def test_get_resources_sync_pagination_and_error_passthrough(sync_smcp_server: int) -> None:
    """同步镜像：SMCPAgentClient.get_resources + SyncSMCPNamespace 路由 + flat ErrorPayload 透传。"""
    port = sync_smcp_server
    ready_q: multiprocessing.Queue = multiprocessing.Queue()
    result_q: multiprocessing.Queue = multiprocessing.Queue()
    err_q: multiprocessing.Queue = multiprocessing.Queue()

    comp_proc = multiprocessing.Process(target=_run_mock_computer_process, args=(port, ready_q, err_q), daemon=True)
    comp_proc.start()
    try:
        try:
            ready_q.get(timeout=8)
        except Exception:
            if not err_q.empty():
                pytest.fail(f"Mock Computer 启动失败: {err_q.get()}")
            pytest.fail("Mock Computer 启动超时")

        agent_proc = multiprocessing.Process(target=_run_sync_agent_process, args=(port, result_q, err_q), daemon=True)
        agent_proc.start()
        try:
            try:
                res = result_q.get(timeout=20)
            except Exception:
                if not err_q.empty():
                    pytest.fail(f"同步 Agent 执行失败: {err_q.get()}")
                pytest.fail("同步 Agent 执行超时")

            assert res["page1_next"] == "page2"
            assert res["page1_uri"] == "window://h/p1"
            assert res["page2_has_next"] is False
            assert res["page2_uri"] == "file://tmp/s.txt"
            # flat ErrorPayload（4015）经 SyncSMCPNamespace 原样透传 → SMCPProtocolError
            assert res["err_code"] == 4015
            assert res["capability"] == "resources"
            # flat ErrorPayload（4014 未注册 server）同样透传 → SMCPProtocolError，含 mcp_server_name
            assert res["err4014_code"] == 4014
            assert res["err4014_server"] == "does-not-exist"
        finally:
            if agent_proc.is_alive():
                agent_proc.terminate()
                agent_proc.join(timeout=2)
    finally:
        if comp_proc.is_alive():
            comp_proc.terminate()
            comp_proc.join(timeout=2)
