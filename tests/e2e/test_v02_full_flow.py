# -*- coding: utf-8 -*-
# filename: test_v02_full_flow.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：A2C-SMCP v0.2 真实进程全链路 e2e（Tracking #8 / sub-issue #20，§6.6 集成测试要求）。

  在一个真实 Uvicorn ASGI 进程 + 真实 A2C 协议版本握手中间件 + 真实 stdio MCP 子进程上，
  端到端覆盖 v0.2 协议的四项保留能力（DPE 移除后）：

    1. 版本握手协商：兼容 server（middleware.server_version == SDK PROTOCOL_VERSION）
       → 三端连接建立，且 Server 从 URL query 写入 ``a2c_version`` 经 ``server:list_room`` 带出；
       不兼容 server（0.3.0）→ 连接被拒、绝不建立（含 §4 死循环防御的确定性保证）。
    2. window:// 资源聚合：``client:get_desktop`` 仅聚合 window://，非 window 资源不进桌面。
    3. get_resources 翻页：``client:get_resources`` 透明转发 MCP resources/list + cursor 翻页，
       非 window 资源不被过滤（透明转发语义，与桌面聚合语义对照）。
    4. 工具调用链路：``client:tool_call`` 经 Server 路由到 Computer 的真实 MCP Server 并回传结果。

English: Real-process full-chain e2e for A2C-SMCP v0.2 (Tracking #8 / sub-issue #20, §6.6).
  One real Uvicorn ASGI process + real protocol-version handshake middleware + real stdio MCP
  subprocesses, covering the four retained v0.2 capabilities (post-DPE-removal): version
  handshake negotiation, window:// desktop aggregation, get_resources pagination with
  transparent passthrough, and the tool-call chain.

协议依据 / Protocol: a2c-smcp-protocol v0.2.x
  - versioning.md（URL query a2c_version + HTTP 400 + 4008 + §4 死循环防御）
  - events.md#client:get_resources（透明转发 MCP resources/list + cursor 翻页）
  - migrations/v0.2-uri-metadata-refactor.md §6.6 集成测试要求

注 / Note: 4008 → ProtocolVersionError 精确归一的确定性契约由
``tests/unit_tests/test_handshake_4008_normalization.py`` 验证；不兼容连接「绝不建立」
的确定性保证由 ``tests/integration_tests/test_version_handshake_client.py`` 验证。
本 e2e 仅在真实进程级别复核两类保证，不重复单测的精确归一断言。
"""

from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from socketio import ASGIApp

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.agent import AsyncSMCPAgentClient, DefaultAgentAuthProvider
from a2c_smcp.computer import Computer
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig, ToolMeta
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.exceptions import ProtocolVersionError
from a2c_smcp.server.middleware import A2CProtocolVersionASGIMiddleware
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, SMCP_NAMESPACE
from a2c_smcp.utils.handshake import HANDSHAKE_CONNECT_ERRORS
from tests.integration_tests.computer.socketio.mock_uv_server import UvicornTestServer
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio
from tests.protocol_versions import INCOMPATIBLE_PEER

pytestmark = pytest.mark.e2e

_SIO_PATH = "/socket.io"
# 从 PROTOCOL_VERSION 派生的不兼容 server 版本（MINOR 不匹配）——不耦合具体协议版本值
_INCOMPATIBLE_SERVER = INCOMPATIBLE_PEER
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MCP_SERVERS_DIR = _PROJECT_ROOT / "tests" / "integration_tests" / "computer" / "mcp_servers"
# subscribe-srv：暴露 window:// 资源（annotations priority / _meta fullscreen）+ 工具 mark_a
_SUBSCRIBE_SRV = _MCP_SERVERS_DIR / "resources_subscribe_stdio_server.py"
# paged-srv：分页混合资源（page1=window+nextCursor；page2=非window file:// + window）
_PAGED_SRV = _MCP_SERVERS_DIR / "resources_paged_mixed_stdio_server.py"
# 确定性拒绝形态集合：归一成功 → ProtocolVersionError；body 竞态丢失 → 原始连接异常
# （二者都满足"连接被拒、绝不建立"的确定性保证；精确类型契约见单测）
_REJECTION_TYPES: tuple[type[BaseException], ...] = (ProtocolVersionError, *HANDSHAKE_CONNECT_ERRORS)


def _stdio_cfg(name: str, script: Path) -> StdioServerConfig:
    """
    中文：构造真实 stdio MCP Server 配置；default_tool_meta.auto_apply=True 跳过二次确认回调。
    English: build a real stdio MCP Server config; default_tool_meta.auto_apply=True skips the
    secondary-confirmation callback so the e2e tool-call chain runs unattended.
    """
    return StdioServerConfig(
        name=name,
        server_parameters=StdioServerParameters(command=sys.executable, args=[str(script)]),
        default_tool_meta=ToolMeta(auto_apply=True),
    )


def _make_mw_app(server_version: str) -> ASGIApp:
    """
    中文：真实 SMCPNamespace（放行认证）+ 真实 A2C 协议版本握手 ASGI 中间件。
    English: real SMCPNamespace (permissive auth) wrapped by the real protocol-version ASGI middleware.
    """
    sio = create_computer_test_socketio()
    sio.eio.start_service_task = False
    return A2CProtocolVersionASGIMiddleware(
        ASGIApp(sio, socketio_path=_SIO_PATH),
        socketio_path=_SIO_PATH,
        server_version=server_version,
    )


@pytest.fixture
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def compat_server(free_port: int) -> AsyncGenerator[int, None]:
    """中文：兼容握手 server（server_version == SDK PROTOCOL_VERSION）。"""
    server = UvicornTestServer(_make_mw_app(PROTOCOL_VERSION), port=free_port)
    await server.up()
    try:
        yield free_port
    finally:
        await server.down(force=True)


@pytest.fixture
async def incompat_server(free_port: int) -> AsyncGenerator[int, None]:
    """中文：不兼容握手 server（server_version=0.3.0，与 SDK 0.2.x MINOR 不匹配）。"""
    server = UvicornTestServer(_make_mw_app(_INCOMPATIBLE_SERVER), port=free_port)
    await server.up()
    try:
        yield free_port
    finally:
        await server.down(force=True)


@pytest.mark.asyncio
async def test_v02_full_flow_compatible(compat_server: int) -> None:
    """
    中文：兼容握手下的 v0.2 全链路 —— 版本握手协商 + window:// 聚合 + get_resources 翻页 + 工具调用。
    English: full v0.2 chain under a compatible handshake.
    """
    base = f"http://127.0.0.1:{compat_server}"
    office_id = "e2e-v02-flow"
    computer = Computer(
        name="comp-v02",
        mcp_servers={_stdio_cfg("subscribe-srv", _SUBSCRIBE_SRV), _stdio_cfg("paged-srv", _PAGED_SRV)},
    )
    comp_client = SMCPComputerClient(computer=computer)
    auth = DefaultAgentAuthProvider(agent_id="robot-v02", office_id=office_id)
    agent = AsyncSMCPAgentClient(auth_provider=auth)

    try:
        # —— 版本握手协商（兼容路径）：Agent + Computer 经真实 ASGI 协议中间件建立连接 ——
        await agent.connect_to_server(base, namespace=SMCP_NAMESPACE, socketio_path=_SIO_PATH)
        await agent.emit(
            JOIN_OFFICE_EVENT,
            {"role": "agent", "office_id": office_id, "name": "robot-v02"},
            namespace=SMCP_NAMESPACE,
        )
        await computer.boot_up()
        await comp_client.connect(base, socketio_path=_SIO_PATH, namespaces=[SMCP_NAMESPACE])
        await comp_client.join_office(office_id)
        assert agent.connected is True
        assert comp_client.connected is True
        await asyncio.sleep(0.5)  # 等待 join 在房间内可见 / wait for room visibility

        # 协商结果落地：Server 在 HTTP 握手阶段从 URL query 写入 a2c_version，经 server:list_room 带出
        computers = await agent.get_computers_in_office(office_id)
        assert computers, "应能在房间内发现 Computer 会话 / Computer session must be discoverable"
        assert all(c.get("a2c_version") == PROTOCOL_VERSION for c in computers), (
            f"协商版本须等于 SDK PROTOCOL_VERSION={PROTOCOL_VERSION}，实际={[c.get('a2c_version') for c in computers]}"
        )

        # —— window:// 资源聚合：桌面仅聚合 window://，非 window 资源不进桌面 ——
        desktop = await agent.get_desktop_from_computer("comp-v02", timeout=15)
        desktops = desktop["desktops"]
        assert desktops, "桌面应至少聚合一个 window:// 资源 / desktop must aggregate at least one window://"
        joined = "\n".join(desktops)
        assert "window://" in joined
        assert "example.desktop.subscribe.a" in joined, "subscribe-srv 的 window:// 应被聚合"
        assert "file://" not in joined, "桌面仅聚合 window://，非 window 资源不得出现 / desktop is window-only"

        # —— get_resources 翻页 + 透明转发（非 window 不过滤，与桌面聚合语义对照）——
        page1 = await agent.get_resources(computer="comp-v02", mcp_server="paged-srv")
        assert page1["next_cursor"] == "page2"
        assert len(page1["resources"]) == 1
        r0 = page1["resources"][0]
        assert r0["uri"].startswith("window://example.desktop.paged/p1")
        # camelCase→snake_case 规整 / mimeType normalized to mime_type
        assert r0["mime_type"] == "text/markdown"
        assert "mimeType" not in r0

        page2 = await agent.get_resources(computer="comp-v02", mcp_server="paged-srv", cursor="page2")
        assert "next_cursor" not in page2
        uris = {r["uri"] for r in page2["resources"]}
        assert any(u.startswith("file://") for u in uris), "透明转发：非 window 资源不得被过滤"
        assert any(u.startswith("window://example.desktop.paged/p2") for u in uris)

        # —— 工具调用链路：Agent → Server → Computer 真实 MCP Server → 结果回传 ——
        result = await agent.emit_tool_call(computer="comp-v02", tool_name="subscribe-srv__mark_a", params={}, timeout=15)
        assert result.isError is False, f"工具调用失败 / tool call failed: {result}"
        assert len(result.content) >= 1
        assert "ok:mark_a" in result.content[0].text
    finally:
        await agent.disconnect()
        await asyncio.sleep(0.2)
        # 显式 shutdown 清理 MCP 子进程；跳过 comp_client.disconnect()（其 30s 超时拖慢用例）
        # Explicit shutdown to reap MCP subprocesses; skip comp_client.disconnect() (30s timeout)
        await computer.shutdown()


# subscribe-srv 暴露的两个 window:// 资源（见 resources_subscribe_stdio_server.py）：
#   - /main      ：annotations.priority=0.6，非 fullscreen
#   - /dashboard ：annotations.priority=0.9，_meta.fullscreen=True
# subscribe-srv exposes two window:// resources: /main (priority 0.6) and /dashboard (priority 0.9, fullscreen).
_SUBSCRIBE_MAIN_URI = "window://example.desktop.subscribe.a/main"
_SUBSCRIBE_DASHBOARD_URI = "window://example.desktop.subscribe.a/dashboard"


@pytest.mark.asyncio
async def test_v02_get_desktop_window_filter(compat_server: int) -> None:
    """
    中文：``client:get_desktop`` 的 window 定向过滤全链路 e2e —— Agent 传入指定 WindowURI 时，
          全链路（Agent → Server → Computer → Manager 精确匹配 → organize）仅返回该窗口内容。

      契约依据 / Contract:
        - ``GetDeskTopReq.window``（smcp.py）：指定则尝试只获取该 Window。
        - ``manager.get_windows_details(window_uri)``：对 ``Resource.uri`` 做「完全相等」过滤（非前缀/模糊）。
        - ``organize_desktop``：过滤已在 Manager 层完成，故 fullscreen 短路规则（§6.2-4）不会再隐藏被定向
          选中的非全屏窗口。

      三组对照断言 / Three contrasting assertions:
        1) 不指定 window：fullscreen 的 /dashboard 短路 subscribe-srv，桌面含 /dashboard、不含 /main（基线）。
        2) 指定 window=/main：桌面仅含 /main，且不含 /dashboard（定向命中 + 绕过 fullscreen 短路）。
        3) 指定不存在的 window：完全相等过滤 → 空桌面（验证 exact-match、非模糊匹配语义）。

    English: e2e for the window-targeted filter of ``client:get_desktop``. When the Agent passes a specific
      WindowURI, the full chain returns only that window's content; the baseline (no filter) returns the
      fullscreen /dashboard (which short-circuits /main), and an unmatched URI yields an empty desktop
      (exact-match, not prefix/fuzzy).
    """
    base = f"http://127.0.0.1:{compat_server}"
    office_id = "e2e-v02-desktop-filter"
    # 仅挂载 subscribe-srv（含 /main 与 fullscreen /dashboard 两个 window:// 资源），保持真实进程足迹最小。
    # Mount only subscribe-srv to keep the real-process footprint minimal.
    computer = Computer(
        name="comp-v02-df",
        mcp_servers={_stdio_cfg("subscribe-srv", _SUBSCRIBE_SRV)},
    )
    comp_client = SMCPComputerClient(computer=computer)
    auth = DefaultAgentAuthProvider(agent_id="robot-v02-df", office_id=office_id)
    agent = AsyncSMCPAgentClient(auth_provider=auth)

    try:
        # —— 三端经真实 ASGI 协议中间件建立连接 / establish the three-party connection ——
        await agent.connect_to_server(base, namespace=SMCP_NAMESPACE, socketio_path=_SIO_PATH)
        await agent.emit(
            JOIN_OFFICE_EVENT,
            {"role": "agent", "office_id": office_id, "name": "robot-v02-df"},
            namespace=SMCP_NAMESPACE,
        )
        await computer.boot_up()
        await comp_client.connect(base, socketio_path=_SIO_PATH, namespaces=[SMCP_NAMESPACE])
        await comp_client.join_office(office_id)
        assert agent.connected is True
        assert comp_client.connected is True
        await asyncio.sleep(0.5)  # 等待 join 在房间内可见 / wait for room visibility

        # (1) 基线：不指定 window —— fullscreen 的 /dashboard 短路 subscribe-srv，/main 被隐藏
        baseline = (await agent.get_desktop_from_computer("comp-v02-df", timeout=15))["desktops"]
        baseline_joined = "\n".join(baseline)
        assert _SUBSCRIBE_DASHBOARD_URI in baseline_joined, f"基线桌面应含 fullscreen /dashboard：{baseline}"
        assert _SUBSCRIBE_MAIN_URI not in baseline_joined, f"基线下 fullscreen 短路，/main 不应出现：{baseline}"

        # (2) 指定 window=/main —— 全链路定向命中：桌面仅含 /main，且绕过 fullscreen 短路、不含 /dashboard
        filtered = (await agent.get_desktop_from_computer("comp-v02-df", window=_SUBSCRIBE_MAIN_URI, timeout=15))["desktops"]
        assert len(filtered) == 1, f"定向过滤后桌面应只有一个窗口：{filtered}"
        only = filtered[0]
        assert only.startswith(_SUBSCRIBE_MAIN_URI), f"定向命中的窗口应为 /main：{only!r}"
        assert _SUBSCRIBE_DASHBOARD_URI not in only, f"定向 /main 时不应混入 /dashboard：{only!r}"

        # (3) 指定不存在的 window —— 完全相等过滤 → 空桌面（非前缀/模糊匹配语义）
        missing = (
            await agent.get_desktop_from_computer(
                "comp-v02-df",
                window="window://example.desktop.subscribe.a/does-not-exist",
                timeout=15,
            )
        )["desktops"]
        assert missing == [], f"不匹配的 WindowURI 应返回空桌面（exact-match 语义）：{missing}"
    finally:
        await agent.disconnect()
        await asyncio.sleep(0.2)
        # 与全链路用例一致：显式 shutdown 清理 MCP 子进程；跳过 comp_client.disconnect()（其 30s 超时拖慢用例）
        # Same as the full-flow case: explicit shutdown reaps MCP subprocesses; skip comp_client.disconnect().
        await computer.shutdown()


@pytest.mark.asyncio
async def test_v02_full_flow_incompatible_handshake_rejected(incompat_server: int) -> None:
    """
    中文：不兼容握手下，Agent 与 Computer 连接均被拒、绝不建立（真实进程级复核 §4 死循环防御保证）。
    English: under an incompatible handshake, both Agent and Computer connections are rejected and never established.
    """
    base = f"http://127.0.0.1:{incompat_server}"

    auth = DefaultAgentAuthProvider(agent_id="robot-v02-bad", office_id="e2e-v02-bad")
    agent = AsyncSMCPAgentClient(auth_provider=auth)
    with pytest.raises(_REJECTION_TYPES):
        await agent.connect_to_server(base, namespace=SMCP_NAMESPACE, socketio_path=_SIO_PATH)
    assert agent.connected is False  # 确定性保证：连接绝不建立（含 §4 死循环防御）

    comp_client = SMCPComputerClient(computer=Computer(name="comp-v02-bad", mcp_servers=set()))
    with pytest.raises(_REJECTION_TYPES):
        await comp_client.connect(base, socketio_path=_SIO_PATH, namespaces=[SMCP_NAMESPACE])
    assert comp_client.connected is False
