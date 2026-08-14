# -*- coding: utf-8 -*-
# filename: _skill_harness.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：v0.2.x SKILL / blob e2e 共享 harness（与 ``test_v02_full_flow.py`` 同口径，真进程链路）。

  集中 ``test_v02_full_flow.py`` 已验证的真进程装配：真实 ``SMCPNamespace``（携带
  ``client:get_skills/get_skill/get_blob`` 路由 + ``notify:update_skills`` 广播 + ``_relay_client_call``
  飞行断连守卫）+ 真实协议版本握手 ASGI 中间件 + 真实 ``UvicornTestServer``，供 SKILL/blob 各 e2e 复用。

English: Shared real-process harness for the v0.2.x SKILL / blob e2e suite (same shape as
  ``test_v02_full_flow.py``). Centralizes the proven assembly: real ``SMCPNamespace`` (which already
  routes ``client:get_skills/get_skill/get_blob``, broadcasts ``notify:update_skills``, and carries the
  ``_relay_client_call`` in-flight disconnect guard) wrapped by the real protocol-version ASGI middleware
  on a real ``UvicornTestServer``.

注 / Note：本文件**不含**测试，仅为助手，故无 ``pytest.mark.e2e``。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path

from socketio import ASGIApp

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.agent import AsyncSMCPAgentClient, DefaultAgentAuthProvider
from a2c_smcp.agent.types import AsyncAgentEventHandler
from a2c_smcp.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.server.middleware import A2CProtocolVersionASGIMiddleware
from a2c_smcp.smcp import JOIN_OFFICE_EVENT, SMCP_NAMESPACE
from a2c_smcp.testing import UvicornTestServer
from tests.integration_tests.mock_socketio_server import create_computer_test_socketio

SIO_PATH = "/socket.io"


def free_tcp_port() -> int:
    """中文：取一个空闲 TCP 端口 / Pick a free TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_mw_app(server_version: str = PROTOCOL_VERSION) -> ASGIApp:
    """中文：真实 SMCPNamespace（放行认证）+ 真实 A2C 协议版本握手 ASGI 中间件。
    English: real SMCPNamespace (permissive auth) wrapped by the real protocol-version ASGI middleware.
    """
    sio = create_computer_test_socketio()
    sio.eio.start_service_task = False
    return A2CProtocolVersionASGIMiddleware(
        ASGIApp(sio, socketio_path=SIO_PATH),
        socketio_path=SIO_PATH,
        server_version=server_version,
    )


@contextlib.asynccontextmanager
async def running_server(server_version: str = PROTOCOL_VERSION) -> AsyncIterator[str]:
    """中文：启动一个真实 Uvicorn ASGI 进程内服务，yield base URL，退出时强制关闭。
    English: Bring up a real in-process Uvicorn ASGI server; yield the base URL; force-down on exit.
    """
    port = free_tcp_port()
    server = UvicornTestServer(make_mw_app(server_version), port=port)
    await server.up()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await server.down(force=True)


async def connect_agent_and_computer(
    base: str,
    office_id: str,
    computer: Computer,
    *,
    agent_id: str,
    event_handler: AsyncAgentEventHandler | None = None,
) -> tuple[AsyncSMCPAgentClient, SMCPComputerClient]:
    """中文：在同一 office 内拉起 Agent + Computer 并完成 join（照搬 full_flow 连接序）。
    English: Connect an Agent + Computer into the same office and join (mirrors full_flow's sequence).

    返回前 ``sleep(0.5)`` 等 join 在房间内可见。Computer 经真实 ``boot_up`` 解析 SKILL Home + 发现 user 源。
    """
    auth = DefaultAgentAuthProvider(agent_id=agent_id, office_id=office_id)
    agent = AsyncSMCPAgentClient(auth_provider=auth, event_handler=event_handler)
    await agent.connect_to_server(base, namespace=SMCP_NAMESPACE, socketio_path=SIO_PATH)
    await agent.emit(
        JOIN_OFFICE_EVENT,
        {"role": "agent", "office_id": office_id, "name": agent_id},
        namespace=SMCP_NAMESPACE,
    )

    comp_client = SMCPComputerClient(computer=computer)
    await computer.boot_up()
    await comp_client.connect(base, socketio_path=SIO_PATH, namespaces=[SMCP_NAMESPACE])
    await comp_client.join_office(office_id)

    await asyncio.sleep(0.5)  # 等待 join 在房间内可见 / wait for room visibility
    return agent, comp_client


async def teardown(agent: AsyncSMCPAgentClient, computer: Computer) -> None:
    """中文：清理 Agent 连接 + 回收 MCP 子进程。
    English: Disconnect the Agent and reap MCP subprocesses.

    跳过 ``comp_client.disconnect()``（其 30s 超时拖慢用例，与 full_flow 同口径）。
    """
    with contextlib.suppress(Exception):
        await agent.disconnect()
    await asyncio.sleep(0.2)
    with contextlib.suppress(Exception):
        await computer.shutdown()


def write_user_skill(
    skill_home: Path,
    name: str,
    *,
    description: str,
    body: str = "",
    files: dict[str, bytes | str] | None = None,
) -> Path:
    """中文：在 ``<skill_home>/user/<name>/`` 落盘一个 user 源 DropIn SKILL 包，返回包根目录。
    English: Drop a user-source SKILL package at ``<skill_home>/user/<name>/``; return its dir.

    ``<name>`` 须为严格 kebab（user 源单段裸名）；frontmatter 强制 ``name`` + ``description``
    （``description`` 是 user 源唯一必填项，见 ``skills/staging.py``）。``files`` 写入包内附属资源
    （bytes → 二进制，str → UTF-8 文本）。
    """
    skill_dir = skill_home / "user" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = skill_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return skill_dir
