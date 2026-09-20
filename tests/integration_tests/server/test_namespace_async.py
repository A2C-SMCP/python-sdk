# -*- coding: utf-8 -*-
# filename: test_namespace_async.py
# @Time    : 2025/09/30 23:20
# @Author  : A2C-SMCP
# @Software: PyCharm
"""
中文：针对 `a2c_smcp/server/namespace.py` 的异步命名空间集成测试。
English: Integration tests for async SMCPNamespace in `a2c_smcp/server/namespace.py`.

说明：
- 复用全局 fixtures：`socketio_server`, `basic_server_port`。
- 服务器端命名空间来自 `tests/integration_tests/mock_socketio_server.py` 的 `MockComputerServerNamespace`，不做修改。
- 客户端使用 socketio.AsyncClient 直接与服务端交互，验证服务端行为。
"""

import asyncio
from typing import Literal

import pytest
from mcp.types import CallToolResult, TextContent
from socketio import AsyncClient

from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.smcp import (
    ENTER_OFFICE_NOTIFICATION,
    GET_CONFIG_EVENT,
    GET_TOOLS_EVENT,
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_EVENT,
    LEAVE_OFFICE_NOTIFICATION,
    LIST_ROOM_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_EVENT,
    EnterOfficeReq,
    ErrorCode,
    GetComputerConfigReq,
    GetToolsReq,
    UpdateMCPConfigNotification,
)
from tests.room_acks import assert_empty_ack, assert_rejected_ack


async def _join_office(client: AsyncClient, role: Literal["computer", "agent"], office_id: str, name: str) -> None:
    payload: EnterOfficeReq = {"role": role, "office_id": office_id, "name": name}
    # v0.5.0（#214）：成功 = 空 ack（None）；失败 = flat ErrorPayload
    assert_empty_ack(await client.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE))


@pytest.mark.asyncio
async def test_enter_and_broadcast(socketio_server, basic_server_port: int):
    """
    中文：Agent 先入场，Computer 后入场，服务端应广播 ENTER_OFFICE_NOTIFICATION 给同房间的 Agent。
    English: Agent first, Computer then; server should broadcast ENTER_OFFICE_NOTIFICATION to Agent in same room.
    """
    agent = AsyncClient()
    computer = AsyncClient()

    enter_events: list[dict] = []

    @agent.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    async def _on_enter(data: dict):
        enter_events.append(data)

    # 连接并让 Agent 入场
    await agent.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    office_id = "office-async-1"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-A")

    # 连接并让 Computer 入场
    await computer.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await _join_office(computer, role="computer", office_id=office_id, name="comp-A")

    # 等待广播
    await asyncio.sleep(0.2)

    assert enter_events, "Agent 应收到 ENTER_OFFICE_NOTIFICATION"

    await agent.disconnect()
    await computer.disconnect()


@pytest.mark.asyncio
async def test_leave_and_broadcast(socketio_server, basic_server_port: int):
    """
    中文：Computer 离开办公室，服务端应广播 LEAVE_OFFICE_NOTIFICATION 给房间内其他客户端。
    English: When Computer leaves, server should broadcast LEAVE_OFFICE_NOTIFICATION to others in the room.
    """
    agent = AsyncClient()
    computer = AsyncClient()

    leave_events: list[dict] = []

    @agent.on(LEAVE_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    async def _on_leave(data: dict):
        leave_events.append(data)

    await agent.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    office_id = "office-async-2"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-B")

    await computer.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await _join_office(computer, role="computer", office_id=office_id, name="comp-B")

    # 通过 server:leave_office 离开
    ack = await computer.call(
        LEAVE_OFFICE_EVENT,
        {"office_id": office_id},
        namespace=SMCP_NAMESPACE,
    )
    assert_empty_ack(ack)

    await asyncio.sleep(0.2)
    assert leave_events, "Agent 应收到 LEAVE_OFFICE_NOTIFICATION"

    await agent.disconnect()
    await computer.disconnect()


@pytest.mark.asyncio
async def test_tool_call_roundtrip(socketio_server, basic_server_port: int):
    """
    中文：Agent 发起 client:tool_call，服务端转发至目标 Computer，并将其 ACK 作为结果返回。
    English: Agent calls client:tool_call; server forwards to Computer and returns ACK result.
    """
    agent = AsyncClient()
    computer = AsyncClient()

    await agent.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    office_id = "office-async-3"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-C")

    await computer.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await _join_office(computer, role="computer", office_id=office_id, name="comp-C")

    @computer.on(TOOL_CALL_EVENT, namespace=SMCP_NAMESPACE)
    async def _on_tool_call(data: dict):
        return CallToolResult(
            isError=False,
            content=[TextContent(type="text", text="ok from computer")],
        ).model_dump(mode="json")

    # 使用 agent 直接调用服务端事件（测试服务端转发与聚合）
    res = await agent.call(
        TOOL_CALL_EVENT,
        {
            "agent": "robot-C",
            "computer": "comp-C",
            "tool_name": "echo",
            "params": {"text": "hi"},
            "req_id": "req-001",
            "timeout": 5,
        },
        namespace=SMCP_NAMESPACE,
    )

    assert isinstance(res, dict)
    assert res.get("isError") is False
    assert any(c.get("text") == "ok from computer" for c in res.get("content", []))

    await agent.disconnect()
    await computer.disconnect()


@pytest.mark.asyncio
async def test_tool_call_target_disconnect_midflight_returns_404(socketio_server, basic_server_port: int):
    """#100 Phase 1（端到端复现）：Computer 在 tool 执行中途断连 → 服务端即时回 flat ErrorPayload(404)，
    Agent 不再静默挂死到满 ``timeout``。验证在途断连守卫在真实 socketio 上生效。
    Target Computer disconnects mid tool_call → server returns flat ErrorPayload(404) fast, no full-timeout hang.
    """
    agent = AsyncClient()
    computer = AsyncClient()

    await agent.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    office_id = "office-async-disc"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-D")

    await computer.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await _join_office(computer, role="computer", office_id=office_id, name="comp-D")

    tool_started = asyncio.Event()
    tool_block = asyncio.Event()  # 测试期间永不 set：模拟慢工具在途 / never set: tool blocks in-flight

    @computer.on(TOOL_CALL_EVENT, namespace=SMCP_NAMESPACE)
    async def _on_tool_call(data: dict):
        tool_started.set()
        await tool_block.wait()
        return CallToolResult(isError=False, content=[TextContent(type="text", text="late")]).model_dump(mode="json")

    # 发起 tool_call（per-request timeout=30s）作为后台任务 / issue tool_call as a task with a 30s timeout
    call_task = asyncio.ensure_future(
        agent.call(
            TOOL_CALL_EVENT,
            {"agent": "robot-D", "computer": "comp-D", "tool_name": "slow", "params": {}, "req_id": "req-disc", "timeout": 30},
            namespace=SMCP_NAMESPACE,
        ),
    )
    try:
        # 确认工具已在 Computer 端在途（服务端已登记在途信号）/ tool is in-flight on the Computer
        await asyncio.wait_for(tool_started.wait(), timeout=5)
        # 在途断连 Computer / disconnect the Computer mid-flight
        await computer.disconnect()
        # 关键：远早于 30s timeout 拿到 404（若挂死，wait_for 会先超时报错）/ 404 well before the 30s timeout
        res = await asyncio.wait_for(call_task, timeout=15)
    finally:
        tool_block.set()
        if not call_task.done():
            call_task.cancel()

    assert isinstance(res, dict)
    assert res.get("code") == int(ErrorCode.NOT_FOUND)
    assert res.get("details", {}).get("computer_name") == "comp-D"

    await agent.disconnect()


@pytest.mark.asyncio
async def test_get_tools_success_same_office(socketio_server, basic_server_port: int):
    """
    中文：Agent 与 Computer 同房间，调用 client:get_tools，服务端通过 call 获取并返回工具列表。
    English: Agent and Computer in same room; client:get_tools returns tools list via server call.
    """
    agent = AsyncClient()
    computer = AsyncClient()

    await agent.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    office_id = "office-async-4"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-D")

    await computer.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await _join_office(computer, role="computer", office_id=office_id, name="comp-D")

    tools_ready = asyncio.Event()

    @computer.on(GET_TOOLS_EVENT, namespace=SMCP_NAMESPACE)
    async def _on_get_tools(data: GetToolsReq):
        tools_ready.set()
        return {
            "tools": [
                {
                    "name": "echo",
                    "bundle_id": "echosrv",  # #152 D1：required，name ≠ bundle_id 分叉
                    "description": "echo text",
                    "params_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                    "return_schema": None,
                },
            ],
            "req_id": data["req_id"],
        }

    res = await agent.call(
        GET_TOOLS_EVENT,
        {
            "computer": "comp-D",
            "agent": "robot-D",
            "req_id": "req-002",
        },
        namespace=SMCP_NAMESPACE,
    )

    await asyncio.wait_for(tools_ready.wait(), timeout=3)

    assert isinstance(res, dict)
    assert res.get("tools") and res["tools"][0]["name"] == "echo"

    await agent.disconnect()
    await computer.disconnect()


@pytest.mark.asyncio
async def test_update_config_broadcast(socketio_server, basic_server_port: int):
    """
    中文：Computer 触发 server:update_config，服务端向同房间广播 UPDATE_CONFIG_NOTIFICATION。
    English: Computer emits server:update_config; server broadcasts UPDATE_CONFIG_NOTIFICATION.
    """
    agent = AsyncClient()
    computer = AsyncClient()

    update_events: list[UpdateMCPConfigNotification] = []

    @agent.on("notify:update_config", namespace=SMCP_NAMESPACE)
    async def _on_update(data: UpdateMCPConfigNotification) -> None:
        update_events.append(data)

    await agent.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    office_id = "office-async-5"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-E")

    await computer.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await _join_office(computer, role="computer", office_id=office_id, name="comp-E")

    # 由 Computer 触发 server:update_config
    await computer.emit(
        UPDATE_CONFIG_EVENT,
        {"computer": computer.get_sid(SMCP_NAMESPACE)},
        namespace=SMCP_NAMESPACE,
    )

    await asyncio.sleep(0.2)
    assert update_events and update_events[0]["computer"] == computer.get_sid(SMCP_NAMESPACE)

    await agent.disconnect()
    await computer.disconnect()


@pytest.mark.asyncio
async def test_list_room_success(socketio_server, basic_server_port: int):
    """
    中文：测试 Agent 成功列出房间内所有会话信息
    English: Test Agent successfully lists all sessions in a room
    """
    from a2c_smcp.smcp import LIST_ROOM_EVENT, ListRoomReq

    agent = AsyncClient()
    computer1 = AsyncClient()
    computer2 = AsyncClient()

    # 连接所有客户端 / Connect all clients
    await agent.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await computer1.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await computer2.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )

    # 让所有客户端加入同一房间 / All clients join the same room
    office_id = "office-list-room-1"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-list")
    await _join_office(computer1, role="computer", office_id=office_id, name="comp-list-1")
    await _join_office(computer2, role="computer", office_id=office_id, name="comp-list-2")

    # 等待所有客户端加入 / Wait for all clients to join
    await asyncio.sleep(0.2)

    # Agent 调用 list_room 事件 / Agent calls list_room event
    list_req: ListRoomReq = {
        "agent": agent.sid,
        "req_id": "list_req_1",
        "office_id": office_id,
    }
    result = await agent.call(LIST_ROOM_EVENT, list_req, namespace=SMCP_NAMESPACE)

    # 验证结果 / Verify result
    assert result is not None
    assert result["req_id"] == "list_req_1"
    assert "sessions" in result
    assert len(result["sessions"]) == 3  # 1 agent + 2 computers

    # 验证会话信息 / Verify session info
    sessions = result["sessions"]
    roles = [s["role"] for s in sessions]
    assert roles.count("agent") == 1
    assert roles.count("computer") == 2
    assert all(s["office_id"] == office_id for s in sessions)

    # 断开连接 / Disconnect
    await agent.disconnect()
    await computer1.disconnect()
    await computer2.disconnect()


@pytest.mark.asyncio
async def test_list_room_empty_office(socketio_server, basic_server_port: int):
    """
    中文：测试 Agent 查询只有自己的房间
    English: Test Agent queries a room with only itself
    """
    from a2c_smcp.smcp import LIST_ROOM_EVENT, ListRoomReq

    agent = AsyncClient()

    await agent.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )

    # Agent 加入 office_empty
    # Agent joins office_empty
    office_id = "office_empty"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-alone")

    await asyncio.sleep(0.2)

    # Agent 查询自己所在的房间（只有自己）
    # Agent queries its own room (only itself)
    list_req: ListRoomReq = {
        "agent": agent.sid,
        "req_id": "list_req_empty",
        "office_id": office_id,
    }
    result = await agent.call(LIST_ROOM_EVENT, list_req, namespace=SMCP_NAMESPACE)

    # 验证结果：应该只有 1 个会话（Agent 自己）
    # Verify result: should only have 1 session (Agent itself)
    assert result is not None
    assert result["req_id"] == "list_req_empty"
    assert "sessions" in result
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["role"] == "agent"
    assert result["sessions"][0]["office_id"] == office_id

    await agent.disconnect()


@pytest.mark.asyncio
async def test_computer_duplicate_name_rejected(socketio_server, basic_server_port: int):
    """
    中文：测试Computer重名检查：当房间内已存在同名Computer时，第二个Computer加入应失败
    English: Test Computer duplicate name check: second Computer with same name should fail to join
    """
    computer1 = AsyncClient()
    computer2 = AsyncClient()

    # 连接第一个 Computer
    # Connect first Computer
    await computer1.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    office_id = "office-dup-test"
    computer_name = "duplicate-comp"

    # 第一个 Computer 成功加入
    # First Computer joins successfully
    await _join_office(computer1, role="computer", office_id=office_id, name=computer_name)

    # 连接第二个 Computer（同名）
    # Connect second Computer (same name)
    await computer2.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )

    # 第二个 Computer 尝试加入同一房间，应该失败
    # Second Computer tries to join same room, should fail
    payload: EnterOfficeReq = {"role": "computer", "office_id": office_id, "name": computer_name}
    ack = await computer2.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE)

    # 验证失败：v0.5.0 起以 flat ErrorPayload 承载，错误语义看**协议码**而非自由文本
    # Verify failure: since v0.5.0 the rejection is a flat ErrorPayload — read the code, not free text
    assert_rejected_ack(ack, 4105, action="server:join_office")
    assert ack["message"] == "Name already taken in room", ack

    await computer1.disconnect()
    await computer2.disconnect()


@pytest.mark.asyncio
async def test_computer_different_name_allowed(socketio_server, basic_server_port: int):
    """
    中文：测试不同名Computer可以加入：房间内已有Computer，但名字不同，应该成功
    English: Test different name Computer can join: room has Computer but different name, should succeed
    """
    computer1 = AsyncClient()
    computer2 = AsyncClient()

    # 连接第一个 Computer
    # Connect first Computer
    await computer1.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    office_id = "office-diff-name-test"

    # 第一个 Computer 加入
    # First Computer joins
    await _join_office(computer1, role="computer", office_id=office_id, name="comp-1")

    # 连接第二个 Computer（不同名）
    # Connect second Computer (different name)
    await computer2.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )

    # 第二个 Computer 加入同一房间，应该成功
    # Second Computer joins same room, should succeed
    payload: EnterOfficeReq = {"role": "computer", "office_id": office_id, "name": "comp-2"}
    ack = await computer2.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE)

    # 验证成功
    # Verify success
    assert_empty_ack(ack, action="不同名Computer应该加入成功 / Different name Computer should succeed, error")

    await computer1.disconnect()
    await computer2.disconnect()


@pytest.mark.asyncio
async def test_computer_switch_room_with_same_name_allowed(socketio_server, basic_server_port: int):
    """
    中文：测试Computer切换房间：同一个Computer从一个房间切换到另一个房间应该成功
    English: Test Computer switching rooms: same Computer switching from one room to another should succeed
    """
    computer = AsyncClient()

    # 连接 Computer
    # Connect Computer
    await computer.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )

    computer_name = "switching-comp"

    # 加入第一个房间
    # Join first room
    await _join_office(computer, role="computer", office_id="office-room-1", name=computer_name)

    # 切换到第二个房间（同名Computer）
    # Switch to second room (same name Computer)
    payload: EnterOfficeReq = {"role": "computer", "office_id": "office-room-2", "name": computer_name}
    ack = await computer.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE)

    # 验证成功
    # Verify success
    assert_empty_ack(ack, action="Computer切换房间应该成功 / Computer switching rooms should succeed, error")


# ======================================================================
# GitHub #31：office/role 隔离负路径（显式 raise，-O 下亦生效）
# GitHub #31: office/role isolation negative paths (explicit raise, holds under -O)
#
# 服务端与 UvicornTestServer 同进程，可直接以真实会话调用真实处理器，
# 精确断言抛出 SMCPNamespaceError（而非依赖 ACK 超时的弱断言）。
# Server runs in-process with UvicornTestServer, so the real handler can be
# invoked with real sessions and precisely asserted to raise SMCPNamespaceError
# (instead of a weak ACK-timeout assertion).
# ======================================================================


async def _connect_join(client: AsyncClient, port: int, role: Literal["computer", "agent"], office_id: str, name: str) -> str:
    """连接 + 加入 office，返回服务端可见的 sid。/ Connect + join office, return server-side sid."""
    await client.connect(
        f"http://localhost:{port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    await _join_office(client, role=role, office_id=office_id, name=name)
    return client.get_sid(namespace=SMCP_NAMESPACE)


@pytest.mark.asyncio
async def test_get_tools_cross_office_rejected(socketio_server, basic_server_port: int):
    """跨房间 client:get_tools：Agent(office_A) 取 Computer(office_B) 工具 → SMCPNamespaceError。"""
    agent = AsyncClient()
    computer = AsyncClient()
    agent_sid = await _connect_join(agent, basic_server_port, "agent", "office-neg-A", "robot-neg-1")
    await _connect_join(computer, basic_server_port, "computer", "office-neg-B", "comp-neg-1")

    # v0.2.2 #46 起，client:get_tools 收编进 _relay_client_call，跨房间错误文案统一为通用 "跨房间" 模板。
    with pytest.raises(SMCPNamespaceError, match="跨房间"):
        await socketio_server.on_client_get_tools(
            agent_sid,
            {"computer": "comp-neg-1", "agent": "robot-neg-1", "req_id": "neg-r1"},
        )

    await agent.disconnect()
    await computer.disconnect()


@pytest.mark.asyncio
async def test_get_resources_cross_office_rejected(socketio_server, basic_server_port: int):
    """跨房间 client:get_resources（PR #29 来源场景）→ SMCPNamespaceError，不路由到 Computer。"""
    agent = AsyncClient()
    computer = AsyncClient()
    agent_sid = await _connect_join(agent, basic_server_port, "agent", "office-neg-A2", "robot-neg-2")
    await _connect_join(computer, basic_server_port, "computer", "office-neg-B2", "comp-neg-2")

    # v0.2.1 #41 起，跨房间消息由 ``_relay_client_call`` 统一收敛，错误文案改为通用 "跨房间" 模板
    # Since v0.2.1 #41, cross-office checks are centralized in ``_relay_client_call``
    with pytest.raises(SMCPNamespaceError, match="跨房间"):
        await socketio_server.on_client_get_resources(
            agent_sid,
            {"computer": "comp-neg-2", "agent": "robot-neg-2", "mcp_server": "any", "req_id": "neg-r2"},
        )

    await agent.disconnect()
    await computer.disconnect()


@pytest.mark.asyncio
async def test_tool_call_wrong_role_rejected(socketio_server, basic_server_port: int):
    """错角色 client:tool_call：由 Computer 发起工具调用 → SMCPNamespaceError。"""
    computer = AsyncClient()
    comp_sid = await _connect_join(computer, basic_server_port, "computer", "office-neg-C", "comp-neg-3")

    with pytest.raises(SMCPNamespaceError, match="目前仅支持Agent调用工具"):
        await socketio_server.on_client_tool_call(
            comp_sid,
            {
                "agent": "comp-neg-3",
                "computer": "comp-neg-3",
                "req_id": "neg-r3",
                "tool_name": "t",
                "params": {},
                "timeout": 5,
            },
        )

    await computer.disconnect()


@pytest.mark.asyncio
async def test_list_room_cross_office_rejected(socketio_server, basic_server_port: int):
    """跨房间 server:list_room：Agent(office_A) 查询 office_B → flat ErrorPayload(4104)，不泄露他房会话。

    #214 前该路径 raise ⇒ 不回 ACK ⇒ 调用方挂到自身超时；协议明令不许「静默不 ack」。
    """
    agent = AsyncClient()
    agent_sid = await _connect_join(agent, basic_server_port, "agent", "office-neg-D", "robot-neg-4")

    ack = await socketio_server.on_server_list_room(
        agent_sid,
        {"agent": "robot-neg-4", "req_id": "neg-r4", "office_id": "office-neg-OTHER"},
    )
    assert_rejected_ack(ack, 4104, action="server:list_room")
    assert ack["details"] == {"office_id": "office-neg-OTHER"}
    assert "sessions" not in ack, "越权拒绝不得携带他房会话数据"

    await agent.disconnect()


# ---------------------------------------------------------------------------
# 中文：#94 client:get_config 中继回归 / English: #94 client:get_config relay regression
# ---------------------------------------------------------------------------
def _stub_stdio_server_config(name: str) -> dict:
    """最小合法 MCPServerStdioConfig（占位符原样，无解析后密钥）/ Minimal valid stdio config (placeholder form)."""
    return {
        "name": name,
        "type": "stdio",
        "disabled": False,
        "forbidden_tools": [],
        "tool_meta": {},
        "server_parameters": {
            "command": "python",
            "args": [],
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
    }


@pytest.mark.asyncio
async def test_get_config_success_same_office(socketio_server, basic_server_port: int):
    """
    中文：Agent 与 Computer 同房间，调用 client:get_config，服务端经 _relay_client_call 转发并返回配置（#94）。
    English: Same office; client:get_config is relayed to the Computer and returns its config (#94).

    修复前 Server 缺 on_client_get_config，call 返回 None；修复后返回含 servers 的配置。
    """
    agent = AsyncClient()
    computer = AsyncClient()

    await agent.connect(f"http://localhost:{basic_server_port}", namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    office_id = "office-async-cfg"
    await _join_office(agent, role="agent", office_id=office_id, name="robot-CFG")

    await computer.connect(f"http://localhost:{basic_server_port}", namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    await _join_office(computer, role="computer", office_id=office_id, name="comp-CFG")

    config_relayed = asyncio.Event()

    @computer.on(GET_CONFIG_EVENT, namespace=SMCP_NAMESPACE)
    async def _on_get_config(data: GetComputerConfigReq):
        config_relayed.set()
        return {"servers": {"echo": _stub_stdio_server_config("echo")}, "inputs": []}

    res = await agent.call(
        GET_CONFIG_EVENT,
        {"computer": "comp-CFG", "agent": "robot-CFG", "req_id": "req-cfg-1"},
        namespace=SMCP_NAMESPACE,
        timeout=10,
    )

    await asyncio.wait_for(config_relayed.wait(), timeout=3)

    assert isinstance(res, dict), f"期望返回 GetComputerConfigRet dict，实际：{type(res)}"
    assert res.get("servers") and res["servers"]["echo"]["type"] == "stdio"
    assert res["servers"]["echo"]["server_parameters"]["command"] == "python"

    await agent.disconnect()
    await computer.disconnect()


@pytest.mark.asyncio
async def test_rejected_cross_office_join_never_enters_room(socketio_server, basic_server_port: int, monkeypatch):
    """#213：跨 office 同名被拒的客户端**从未**进入目标房——入房记录 + 成员关系 + 广播隔离三重断言。

    修复前：``enter_room`` 先真入房、后 ``_register_name`` 抛错，回滚只覆盖会话 ⇒ 被拒客户端留在
    目标房里持续收 ``notify:*``，而会话说它不在任何房。

    断言「从未入房」必须钩住 **socketio 层**（``server.enter_room``）：只断言终态（成员关系为空 /
    收不到广播）会被「已入房→失败收敛」的实现同样满足，属**空转**——变异验证证实过
    （去掉前置闸门后仅凭收敛，终态断言仍然全绿）。
    ``server.enter_room`` 只承载 namespace 层的房加入（自身 sid 房间由 manager 直接建），故记录精确。
    """
    entered: list[tuple[str, str]] = []
    real_enter_room = socketio_server.server.enter_room

    async def _recording_enter_room(sid: str, room: str, namespace: str | None = None) -> None:
        entered.append((sid, room))
        await real_enter_room(sid, room, namespace=namespace)

    monkeypatch.setattr(socketio_server.server, "enter_room", _recording_enter_room)

    holder = AsyncClient()  # office-A 持有名字 "c213"
    subject = AsyncClient()  # 以同名进 office-B → 被拒
    control = AsyncClient()  # office-B 的合法成员（正对照）
    joiner = AsyncClient()  # 触发 office-B 的 notify:enter_office

    on_control: list[dict] = []
    on_subject: list[dict] = []

    @control.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    async def _on_enter_control(data: dict):  # noqa: ANN202
        on_control.append(data)

    @subject.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    async def _on_enter_subject(data: dict):  # noqa: ANN202
        on_subject.append(data)

    office_a, office_b = "office-213-a", "office-213-b"
    holder_sid = await _connect_join(holder, basic_server_port, "computer", office_a, "c213")
    control_sid = await _connect_join(control, basic_server_port, "computer", office_b, "c213-control")
    # 正对照：钩子确实记录了合法入房（证明下面「未入房」的断言不是钩子失灵）
    assert (control_sid, office_b) in entered, "入房钩子应记录合法成员的加入"

    await subject.connect(
        f"http://localhost:{basic_server_port}",
        namespaces=[SMCP_NAMESPACE],
        socketio_path="/socket.io",
    )
    # 目标房闸门放行（office-B 里没有同名），名字注册表（裸名全局）才是冲突点
    payload: EnterOfficeReq = {"role": "computer", "office_id": office_b, "name": "c213"}
    ack = await subject.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE)
    assert_rejected_ack(ack, 4105, action="server:join_office")  # 目标房同名（跨 office 同名由注册表闸门拦下）

    subject_sid = subject.get_sid(namespace=SMCP_NAMESPACE)
    assert subject_sid is not None

    # 验收口径 ①：**从未**进入目标房（顺序保证：校验先于副作用）
    assert (subject_sid, office_b) not in entered, "被拒的加入不得触碰目标房"
    # 验收口径 ②：终态一致——真实成员关系里不含目标房
    assert [r for r in socketio_server.rooms(subject_sid) if r != subject_sid] == [], "被拒客户端不得留在目标房"
    # 正对照：持有者的成员关系确实可被 rooms() 读出（证明上面的断言不是空转）
    assert [r for r in socketio_server.rooms(holder_sid) if r != holder_sid] == [office_a]

    # 验收口径 ③：被拒客户端收不到该房任何 notify:*；正对照：合法成员收得到
    await _connect_join(joiner, basic_server_port, "computer", office_b, "c213-joiner")
    await asyncio.sleep(0.3)
    assert on_control, "正对照：office-B 合法成员应收到 notify:enter_office"
    assert on_subject == [], "被拒客户端不得收到目标房任何 notify:*"

    for client in (holder, subject, control, joiner):
        await client.disconnect()


@pytest.mark.asyncio
async def test_rename_on_live_session_rejected_keeps_old_room(socketio_server, basic_server_port: int):
    """#221：同一连接内改名（并换房）⇒ `403`，且**校验先于副作用** ⇒ Computer 留在旧房。

    协议 events.md:610「同一 sid 声明了与既有会话不同的 role / name ⇒ 拒绝，403」；faq.md:162 对
    403 的补救是「**重连或换 sid**」——即改身份须新连接，不是在同一 sid 上改。CLI 的
    ``socket join <office> <name>`` 遇到改名时改走透明重连（新 sid，见 ``interactive_impl.py``），
    本用例守服务端那一半。

    与 #213 的关系：这是「**已在旧房** + 换房被拒」这一路径的集成守护——修复前该路径先退旧房再报错，
    客户端落成「无房」却仍以为在旧房。现在拒因是 403（身份声明冲突，先于任何房间副作用），
    「旧房原封不动」的验收口径不变。房内同名（4105）在换房路径上的「校验先于副作用」由单测
    ``test_computer_same_name_in_target_room_is_4105`` 覆盖（裸名注册表下该路径在线上不可构造）。
    """
    peer = AsyncClient()  # office-A 的对端（观察 leave 通知）
    mover = AsyncClient()  # office-A 成员，改名换房到 office-B → 被拒
    latecomer = AsyncClient()  # 之后加入 office-A，用于证明 mover 仍是活成员

    on_peer_leave: list[dict] = []
    on_mover_enter: list[dict] = []

    @peer.on(LEAVE_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    async def _on_peer_leave(data: dict):  # noqa: ANN202
        on_peer_leave.append(data)

    @mover.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    async def _on_mover_enter(data: dict):  # noqa: ANN202
        on_mover_enter.append(data)

    office_a, office_b = "office-221-mv-a", "office-221-mv-b"
    peer_sid = await _connect_join(peer, basic_server_port, "computer", office_a, "peer-221")
    mover_sid = await _connect_join(mover, basic_server_port, "computer", office_a, "mover-221")

    # 改名（并换房）：声明与会话不同的 name ⇒ 身份声明冲突 403
    payload: EnterOfficeReq = {"role": "computer", "office_id": office_b, "name": "renamed-221"}
    ack = await mover.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE)
    assert_rejected_ack(ack, 403, action="server:join_office")
    assert ack["message"] == "Role or name mismatch with existing session", ack
    assert "details" not in ack, f"403 无 code-specific details: {ack!r}"

    # 验收口径：校验先于副作用 ⇒ 旧房成员关系原封不动，对端也未收到 leave
    assert [r for r in socketio_server.rooms(mover_sid) if r != mover_sid] == [office_a], "被拒的换房必须留在旧房"
    assert on_peer_leave == [], "换房被拒不得让旧房对端看到 notify:leave_office"

    # 仍是活成员：新成员入房时它照常收到旧房的 notify:enter_office（正对照）
    await _connect_join(latecomer, basic_server_port, "computer", office_a, "late-221")
    await asyncio.sleep(0.3)
    assert on_mover_enter, "旧房仍应把新成员入房广播给 mover"

    # 身份未落地：旧房成员列表里它仍叫 mover-221
    listed = await peer.call(
        LIST_ROOM_EVENT,
        {"agent": peer_sid, "req_id": "req-221-mv", "office_id": office_a},
        namespace=SMCP_NAMESPACE,
    )
    names = sorted(s["name"] for s in listed["sessions"])
    assert names == ["late-221", "mover-221", "peer-221"], f"改名不得在旧房落地：{names}"

    for client in (peer, mover, latecomer):
        await client.disconnect()


@pytest.mark.asyncio
async def test_get_config_cross_office_rejected(socketio_server, basic_server_port: int):
    """跨房间 client:get_config：Agent(office_A) 取 Computer(office_B) 配置 → SMCPNamespaceError，不路由到 Computer（#94）。"""
    agent = AsyncClient()
    computer = AsyncClient()
    agent_sid = await _connect_join(agent, basic_server_port, "agent", "office-neg-cfgA", "robot-neg-cfg")
    await _connect_join(computer, basic_server_port, "computer", "office-neg-cfgB", "comp-neg-cfg")

    # 与 get_tools/get_resources 同级：跨房间由 _relay_client_call 统一收敛为通用 "跨房间" 错误。
    with pytest.raises(SMCPNamespaceError, match="跨房间"):
        await socketio_server.on_client_get_config(
            agent_sid,
            {"computer": "comp-neg-cfg", "agent": "robot-neg-cfg", "req_id": "neg-cfg-1"},
        )

    await agent.disconnect()
    await computer.disconnect()


# ======================================================================
# #214：ack 形态的**线上**回归（真 socketio 栈）
#
# 本节的用例全部经 `client.call(...)` 走真实 ACK 通道——直接调用 handler 的单元用例
# **看不见**两类失效：(a) 异常逃出 handler ⇒ socketio 根本不发 ACK ⇒ 调用方挂到自身超时；
# (b) 返回值形状不是协议约定（如回了元组/空 dict）。acceptance「不再出现挂到超时」只能在此层钉死。
# Wire-level regressions: these assertions can only be made through a real ack channel.
# ======================================================================


@pytest.mark.asyncio
async def test_join_office_ack_shapes_over_wire(socketio_server, basic_server_port: int):
    """join 的三种线上回应：成功回 `None`；被拒回 flat ErrorPayload；**参数绑定失败也必须回 ack**。"""
    client = AsyncClient()
    await client.connect(
        f"http://localhost:{basic_server_port}", namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io"
    )
    try:
        # 成功 → 空 ack（客户端 call() 折叠为 None）
        ack = await client.call(
            JOIN_OFFICE_EVENT,
            {"role": "agent", "office_id": "office-wire-214", "name": "wire-agent"},
            namespace=SMCP_NAMESPACE,
            timeout=5,
        )
        assert ack is None, f"成功必须是空 ack，实得 {ack!r}"

        # 角色不符（会话已声明 agent，本次声明 computer）→ 403 payload
        rejected = await client.call(
            JOIN_OFFICE_EVENT,
            {"role": "computer", "office_id": "office-wire-214", "name": "wire-agent"},
            namespace=SMCP_NAMESPACE,
            timeout=5,
        )
        assert isinstance(rejected, dict) and rejected["code"] == 403, rejected

        # 载荷缺失 / 畸形：**必须在 socketio 元组展开或参数绑定处就回 ack**，
        # 绝不能因为异常逃出 handler 而让调用方挂到自身超时（error-handling.md:102-106）。
        for label, payload in [("no-payload", None), ("wrong-type", "not-a-dict"), ("empty-dict", {})]:
            try:
                ack = (
                    await client.call(JOIN_OFFICE_EVENT, namespace=SMCP_NAMESPACE, timeout=5)
                    if payload is None
                    else await client.call(JOIN_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE, timeout=5)
                )
            except Exception as exc:  # 超时 ⇒ 静默不 ack ⇒ acceptance 不满足
                raise AssertionError(f"[{label}] 畸形载荷不得挂到客户端超时：{exc!r}") from exc
            assert isinstance(ack, dict) and ack["code"] == 400, f"[{label}] {ack!r}"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_list_room_cross_office_ack_over_wire(socketio_server, basic_server_port: int):
    """`server:list_room` 越权在线上的形态：立即回 4104，**不**挂到客户端超时、**不**返回空列表。

    旧实现是 raise SMCPNamespaceError ⇒ 不发 ACK ⇒ 调用方吃满 timeout；本用例是那条验收
    （「不再出现『挂到超时』或『返回空 sessions』」）的线上钉死。
    """
    agent = AsyncClient()
    await agent.connect(
        f"http://localhost:{basic_server_port}", namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io"
    )
    try:
        await _join_office(agent, role="agent", office_id="office-wire-214-a", name="wire-lister")

        try:
            ack = await agent.call(
                LIST_ROOM_EVENT,
                {"agent": "wire-lister", "req_id": "wire-r1", "office_id": "office-wire-214-b"},
                namespace=SMCP_NAMESPACE,
                timeout=5,
            )
        except Exception as exc:
            raise AssertionError(f"越权查询不得挂到客户端超时：{exc!r}") from exc

        assert isinstance(ack, dict), f"越权必须回 flat ErrorPayload，实得 {ack!r}"
        assert ack["code"] == 4104, ack
        assert "sessions" not in ack, "越权拒绝不得携带目标房成员信息"
    finally:
        await agent.disconnect()


@pytest.mark.asyncio
async def test_leave_office_ack_shapes_over_wire(socketio_server, basic_server_port: int):
    """leave 的线上形态：有房/无房都回空 ack；载荷畸形回 400（**不**静默不 ack）。

    协议 §server:leave_office：无房退房是**幂等成功**（不走 4103）；载荷 schema 失败 ⇒ 400。
    两者都必须在线上有回应——`call()` 一旦超时，就说明 handler 又走了「异常逃出 ⇒ 不发 ACK」。
    """
    client = AsyncClient()
    await client.connect(
        f"http://localhost:{basic_server_port}", namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io"
    )
    try:
        await _join_office(client, role="agent", office_id="office-wire-leave", name="wire-leaver")

        # 有房退房 → 空 ack
        assert_empty_ack(
            await client.call(
                LEAVE_OFFICE_EVENT, {"office_id": "office-wire-leave"}, namespace=SMCP_NAMESPACE, timeout=5
            ),
            action="server:leave_office",
        )

        # 无房再退 → 幂等成功（仍为空 ack，**不是** 4103）
        assert_empty_ack(
            await client.call(
                LEAVE_OFFICE_EVENT, {"office_id": "office-wire-leave"}, namespace=SMCP_NAMESPACE, timeout=5
            ),
            action="server:leave_office(幂等)",
        )

        # 载荷畸形 → 400（且有 ack）
        for label, payload in [("empty-dict", {}), ("null-room", {"office_id": None})]:
            ack = await client.call(LEAVE_OFFICE_EVENT, payload, namespace=SMCP_NAMESPACE, timeout=5)
            assert isinstance(ack, dict) and ack["code"] == 400, f"[{label}] {ack!r}"
    finally:
        await client.disconnect()
