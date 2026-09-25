# -*- coding: utf-8 -*-
# filename: test_namespace_sync.py
# @Time    : 2025/09/30 23:42
# @Author  : A2C-SMCP
"""
中文：针对 `a2c_smcp/server/sync_namespace.py` 的同步命名空间集成测试。
English: Integration tests for SyncSMCPNamespace in `a2c_smcp/server/sync_namespace.py`.

说明：
- 仅在本测试包使用的 `_local_sync_server.py` 启动同步 Socket.IO 服务器。
- 使用 werkzeug 在独立进程中运行 WSGI 服务器，彻底解决 GIL 阻塞问题。
"""

import json
import multiprocessing
import socket
import threading
import time
from collections.abc import Generator
from multiprocessing import synchronize
from typing import Any

import pytest
from socketio import Client, Namespace, SimpleClient
from socketio.exceptions import TimeoutError as SioTimeoutError
from werkzeug.serving import make_server

from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import (
    ENTER_OFFICE_NOTIFICATION,
    GET_CONFIG_EVENT,
    GET_RESOURCES_EVENT,
    GET_TOOLS_EVENT,
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_EVENT,
    LEAVE_OFFICE_NOTIFICATION,
    LIST_ROOM_EVENT,
    SMCP_NAMESPACE,
    TOOL_CALL_EVENT,
    UPDATE_CONFIG_EVENT,
    ErrorCode,
    build_computer_not_found_error,
    build_non_agent_client_call_error,
    build_room_rejection_error,
)
from a2c_smcp.testing import create_local_sync_server
from tests.room_acks import assert_empty_ack, assert_rejected_ack


def _url(port: int) -> str:
    """中文: 携带 a2c_version 的裸客户端连接 URL（装配已含版本握手中间件，#187）。"""
    return f"http://localhost:{port}?a2c_version={PROTOCOL_VERSION}"


@pytest.fixture
def sync_server_port() -> int:
    """
    中文：查找可用端口。
    English: Find an available TCP port.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server_process(port: int, ready_event: synchronize.Event) -> None:
    """在独立进程中运行服务器"""
    try:
        sio, ns, wsgi_app = create_local_sync_server()
        # 禁用监控任务避免关闭时出错
        sio.eio.start_service_task = False

        server = make_server("localhost", port, wsgi_app, threaded=True)

        # 通知主进程服务器已准备好
        ready_event.set()

        # 运行服务器
        server.serve_forever()
    except Exception as e:
        print(f"服务器进程错误: {e}")
        ready_event.set()  # 即使出错也要设置事件，避免主进程无限等待


@pytest.fixture
def startup_and_shutdown_local_sync_server(sync_server_port: int) -> Generator[None, Any, None]:
    # 创建进程间通信事件
    ready_event = multiprocessing.Event()

    # 启动服务器进程
    server_process = multiprocessing.Process(
        target=_run_server_process,
        args=(sync_server_port, ready_event),
        daemon=True,
    )
    server_process.start()

    # 等待服务器准备好
    if not ready_event.wait(timeout=5):
        server_process.terminate()
        server_process.join(timeout=2)
        pytest.fail("服务器进程启动超时")

    try:
        yield
    finally:
        # 终止服务器进程
        if server_process.is_alive():
            server_process.terminate()
            server_process.join(timeout=3)

        # 如果进程仍然存活，强制杀死
        if server_process.is_alive():
            server_process.kill()
            server_process.join(timeout=1)


def _join_office(client: Client | SimpleClient, role: str, office_id: str, name: str) -> None:
    ack = (
        client.call(
            JOIN_OFFICE_EVENT,
            {"role": role, "office_id": office_id, "name": name},
            namespace=SMCP_NAMESPACE,
        )
        if isinstance(client, Client)
        else client.call(JOIN_OFFICE_EVENT, {"role": role, "office_id": office_id, "name": name})
    )
    if ack is not None:
        print(f"加入房间失败: role={role}, office_id={office_id}, name={name}, ack={ack!r}")
    assert_empty_ack(ack)


def test_enter_and_broadcast_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    agent = Client()
    computer = Client()

    enter_events: list[dict] = []

    @agent.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    def _on_enter(data: dict):  # noqa: ANN001
        enter_events.append(data)

    agent.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    office_id = "office-sync-s1"
    _join_office(agent, role="agent", office_id=office_id, name="robot-S1")

    computer.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    _join_office(computer, role="computer", office_id=office_id, name="comp-S1")

    time.sleep(0.2)
    assert enter_events, "Agent 应收到 ENTER_OFFICE_NOTIFICATION"

    agent.disconnect()
    computer.disconnect()


def test_leave_and_broadcast_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    agent = Client()
    computer = Client()

    leave_events: list[dict] = []

    @agent.on(LEAVE_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    def _on_leave(data: dict):  # noqa: ANN001
        leave_events.append(data)

    agent.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    office_id = "office-sync-s2"
    _join_office(agent, role="agent", office_id=office_id, name="robot-S2")

    computer.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    _join_office(computer, role="computer", office_id=office_id, name="comp-S2")

    ack = computer.call(LEAVE_OFFICE_EVENT, {"office_id": office_id}, namespace=SMCP_NAMESPACE)
    assert_empty_ack(ack)

    time.sleep(0.2)
    assert leave_events, "Agent 应收到 LEAVE_OFFICE_NOTIFICATION"

    agent.disconnect()
    computer.disconnect()


def _run_computer_client_process(port: int, computer_name_queue: multiprocessing.Queue, error_queue: multiprocessing.Queue) -> None:
    """在独立进程中运行Computer客户端"""
    computer = Client()

    @computer.on(GET_TOOLS_EVENT, namespace=SMCP_NAMESPACE)
    def _on_get_tools(data: dict):  # noqa: ANN001
        return {
            "tools": [
                {
                    "name": "echo",
                    "bundle_id": "echosrv",  # #152 D1：required，name ≠ bundle_id 分叉
                    "description": "echo text",
                    "params_schema": {"type": "object"},
                    "return_schema": None,
                },
            ],
            "req_id": data["req_id"],
        }

    try:
        computer.connect(_url(port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
        office_id = "office-sync-s3"
        computer_name = "comp-S3"
        _join_office(computer, role="computer", office_id=office_id, name=computer_name)

        # 将computer_sid发送给主进程
        computer_name_queue.put(computer_name)

        # 等待并处理GET_TOOLS_EVENT
        computer.wait()
    except Exception as e:
        error_queue.put(f"Computer客户端错误: {str(e)}")
    finally:
        computer.disconnect()


def _run_agent_client_process(
    port: int,
    computer_name: str,
    result_queue: multiprocessing.Queue,
    error_queue: multiprocessing.Queue,
) -> None:
    """在独立进程中运行Agent客户端"""
    try:
        agent = Client()
        agent_id = "robot-S3"
        agent.connect(_url(port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
        office_id = "office-sync-s3"
        _join_office(agent, role="agent", office_id=office_id, name=agent_id)

        # 确保连接稳定后再进行调用
        time.sleep(0.2)

        # 执行GET_TOOLS调用
        res = agent.call(
            GET_TOOLS_EVENT,
            {"computer": computer_name, "agent": agent_id, "req_id": "req-sync-1"},
            namespace=SMCP_NAMESPACE,
            timeout=15,
        )

        # 将结果发送给主进程
        result_queue.put(res)

        agent.disconnect()
    except Exception as e:
        error_queue.put(f"Agent客户端错误: {str(e)}")


# @pytest.mark.skip
def test_get_tools_success_sync(startup_and_shutdown_local_sync_server: Namespace, sync_server_port: int) -> None:
    """测试同步环境下获取工具列表，使用多进程避免GIL阻塞"""

    # 创建进程间通信队列
    computer_name_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    error_queue = multiprocessing.Queue()

    # 1. 启动Computer客户端进程并获取computer_sid
    computer_process = multiprocessing.Process(
        target=_run_computer_client_process,
        args=(sync_server_port, computer_name_queue, error_queue),
        daemon=True,
    )
    computer_process.start()

    try:
        # 等待获取computer_sid
        try:
            computer_name = computer_name_queue.get(timeout=5)
        except Exception:
            # 检查是否有错误
            if not error_queue.empty():
                error_msg = error_queue.get()
                pytest.fail(f"Computer客户端启动失败: {error_msg}")
            else:
                pytest.fail("获取Computer SID超时")

        print(f"获取到Computer NAME: {computer_name}")

        # 2. 启动Agent客户端进程执行工具列表获取
        agent_process = multiprocessing.Process(
            target=_run_agent_client_process,
            args=(sync_server_port, computer_name, result_queue, error_queue),
            daemon=True,
        )
        agent_process.start()

        try:
            # 等待Agent执行结果
            try:
                result = result_queue.get(timeout=20)
                # 验证结果
                assert isinstance(result, dict), f"期望返回dict，实际返回: {type(result)}"
                assert result.get("tools") and result["tools"][0]["name"] == "echo"
            except Exception:
                # 检查是否有错误
                if not error_queue.empty():
                    error_msg = error_queue.get()
                    pytest.fail(f"Agent客户端执行失败: {error_msg}")
                else:
                    pytest.fail("Agent执行超时")
        finally:
            # 清理Agent进程
            if agent_process.is_alive():
                agent_process.terminate()
                agent_process.join(timeout=2)
    finally:
        # 清理Computer进程
        if computer_process.is_alive():
            computer_process.terminate()
            computer_process.join(timeout=2)


def test_update_config_broadcast_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    agent = Client()
    computer = Client()

    received = {"count": 0}

    @agent.on("notify:update_config", namespace=SMCP_NAMESPACE)
    def _on_update(data: dict):  # noqa: ANN001
        received["count"] += 1

    agent.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    office_id = "office-sync-s4"
    _join_office(agent, role="agent", office_id=office_id, name="robot-S4")

    computer.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    _join_office(computer, role="computer", office_id=office_id, name="comp-S4")

    computer.call(UPDATE_CONFIG_EVENT, {"computer": computer.get_sid(namespace=SMCP_NAMESPACE)}, namespace=SMCP_NAMESPACE)

    time.sleep(0.2)
    assert received["count"] >= 1

    agent.disconnect()
    computer.disconnect()


def test_tool_call_forward_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    """测试同步环境下工具调用转发，使用多线程避免阻塞"""
    agent = Client()
    computer = Client()

    received = {"count": 0, "data": None}
    call_result: dict = {"error": None}
    # 用于同步的事件
    computer_ready = threading.Event()
    call_completed = threading.Event()

    @computer.on(TOOL_CALL_EVENT, namespace=SMCP_NAMESPACE)
    def _on_tool_call(data: dict):  # noqa: ANN001
        received["count"] += 1
        received["data"] = data
        # 返回响应给 Agent
        return {"ok": True, "echo": data}

    def run_computer_client():
        """在独立线程中运行Computer客户端"""
        try:
            computer.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
            office_id = "office-sync-s5"
            _join_office(computer, role="computer", office_id=office_id, name="comp-S5")
            computer_ready.set()  # 通知Computer客户端已准备好

            # 等待调用完成
            call_completed.wait(timeout=20)
        except Exception as e:
            call_result["error"] = f"Computer客户端错误: {str(e)}"
            computer_ready.set()
        finally:
            try:
                computer.disconnect()
            except Exception:
                pass

    # 先连接Agent客户端
    agent.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    office_id = "office-sync-s5"
    _join_office(agent, role="agent", office_id=office_id, name="robot-S5")

    # 启动Computer客户端线程
    computer_thread = threading.Thread(target=run_computer_client, daemon=True)
    computer_thread.start()

    try:
        # 等待Computer客户端准备好
        if not computer_ready.wait(timeout=10):
            pytest.fail("Computer客户端连接超时")

        if call_result["error"]:
            pytest.fail(call_result["error"])

        # 确保Computer客户端完全连接后再进行调用
        time.sleep(0.2)

        # 执行Agent工具调用
        res = agent.call(
            TOOL_CALL_EVENT,
            {
                "agent": "robot-S5",
                "computer": "comp-S5",
                "tool_name": "echo",
                "params": {"text": "hi"},
                "req_id": "req-sync-2",
                "timeout": 5,
            },
            namespace=SMCP_NAMESPACE,
            timeout=15,
        )

        # 同步命名空间现在使用 call 方法，等待 Computer 响应
        assert isinstance(res, dict), f"期望返回 dict，实际返回: {type(res)}"
        assert res.get("ok") is True, f"期望 ok=True，实际返回: {res}"
        assert res.get("echo") is not None, f"期望有 echo 字段，实际返回: {res}"

        # 验证 Computer 收到了工具调用
        assert received["count"] == 1, f"Computer应该收到1次工具调用事件，实际收到{received['count']}次"
        assert received["data"] is not None
        assert received["data"]["tool_name"] == "echo"
        assert received["data"]["params"]["text"] == "hi"

    finally:
        call_completed.set()
        computer_thread.join(timeout=5)
        agent.disconnect()


def test_tool_call_target_disconnect_midflight_returns_404_sync(
    startup_and_shutdown_local_sync_server,
    sync_server_port: int,
) -> None:
    """#100 Phase 1 sync 端到端复现：Computer 在 tool 执行中途断连 → Server 即时回 flat ErrorPayload(404)，
    Agent 远早于 ``timeout`` 返回。镜像 async 的 ``test_tool_call_target_disconnect_midflight_returns_404``，
    验证 sync 守卫（daemon 子线程跑 ``self.call`` + 真实 ``on_disconnect`` 触发信号）在真实 werkzeug 多进程 server 上生效。
    Sync mirror: target Computer disconnects mid tool_call → flat ErrorPayload(404) fast, no full-timeout hang.
    """
    agent = Client()
    computer = Client()

    tool_started = threading.Event()
    tool_block = threading.Event()  # 测试期间不 set（仅 finally 释放）→ 模拟慢工具在途 / blocks: slow tool in-flight
    box: dict = {}

    @computer.on(TOOL_CALL_EVENT, namespace=SMCP_NAMESPACE)
    def _on_tool_call(data: dict):  # noqa: ANN001
        tool_started.set()
        tool_block.wait(timeout=20)  # 有界阻塞，保证遗弃的 handler 线程退出 / bounded so the orphaned handler exits
        return {"ok": True}

    office_id = "office-sync-disc"
    computer.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    _join_office(computer, role="computer", office_id=office_id, name="comp-SD")
    agent.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    _join_office(agent, role="agent", office_id=office_id, name="robot-SD")

    def run_agent_call() -> None:
        try:
            box["res"] = agent.call(
                TOOL_CALL_EVENT,
                {"agent": "robot-SD", "computer": "comp-SD", "tool_name": "slow", "params": {}, "req_id": "req-sd", "timeout": 30},
                namespace=SMCP_NAMESPACE,
                timeout=40,
            )
        except BaseException as e:  # noqa: BLE001 捕获供主线程断言 / captured for main-thread assertion
            box["error"] = e

    caller = threading.Thread(target=run_agent_call, daemon=True)
    caller.start()
    try:
        assert tool_started.wait(timeout=10), "Computer 未在期望时间内进入 tool_call 处理"
        # 在途断连 Computer（Client.disconnect 走 eio abort=True，不 join 阻塞中的读循环）/ disconnect mid-flight
        computer.disconnect()
        # 关键：远早于 payload timeout=30s 返回（若挂死，join 在 20s 后判定线程仍存活 → 断言失败）/ fail-fast, not full timeout
        caller.join(timeout=20)
        assert not caller.is_alive(), "agent.call 应在 Computer 断连后快速返回，而非挂到满 timeout"
        assert "error" not in box, f"agent.call 不应抛异常（应收到 404 dict）：{box.get('error')!r}"
        res = box["res"]
        assert isinstance(res, dict), f"期望 dict，实际 {type(res)}"
        assert res.get("code") == int(ErrorCode.NOT_FOUND), f"期望 flat ErrorPayload(404)，实际 {res}"
        assert res.get("details", {}).get("computer_name") == "comp-SD"
    finally:
        tool_block.set()  # 释放遗弃的 handler 线程 / release the orphaned handler thread
        for client in (agent, computer):
            try:
                client.disconnect()
            except Exception:
                pass


def test_computer_duplicate_name_rejected(startup_and_shutdown_local_sync_server, sync_server_port: int):
    """
    中文：测试Computer重名检查：当房间内已存在同名Computer时，第二个Computer加入应失败
    English: Test Computer duplicate name check: second Computer with same name should fail to join
    """
    computer1 = SimpleClient()
    computer2 = SimpleClient()

    try:
        # 连接第一个 Computer
        # Connect first Computer
        computer1.connect(_url(sync_server_port), namespace=SMCP_NAMESPACE)
        office_id = "office-sync-dup-test"
        computer_name = "duplicate-comp-sync"

        # 第一个 Computer 成功加入
        # First Computer joins successfully
        ack = computer1.call(
            JOIN_OFFICE_EVENT,
            {"role": "computer", "office_id": office_id, "name": computer_name},
        )
        assert_empty_ack(ack, action="第一个Computer应该加入成功 / First Computer should join successfully, error")

        # 连接第二个 Computer（同名）
        # Connect second Computer (same name)
        computer2.connect(_url(sync_server_port), namespace=SMCP_NAMESPACE)

        # 第二个 Computer 尝试加入同一房间，应该失败
        # Second Computer tries to join same room, should fail
        ack2 = computer2.call(
            JOIN_OFFICE_EVENT,
            {"role": "computer", "office_id": office_id, "name": computer_name},
        )

        # 验证失败
        # Verify failure
        assert_rejected_ack(ack2, 4105, action="server:join_office")
        assert ack2["message"] == "Name already taken in room", ack2

    finally:
        computer1.disconnect()
        computer2.disconnect()


def test_computer_different_name_allowed(startup_and_shutdown_local_sync_server, sync_server_port: int):
    """
    中文：测试不同名Computer可以加入：房间内已有Computer，但名字不同，应该成功
    English: Test different name Computer can join: room has Computer but different name, should succeed
    """
    computer1 = SimpleClient()
    computer2 = SimpleClient()

    try:
        # 连接第一个 Computer
        # Connect first Computer
        computer1.connect(_url(sync_server_port), namespace=SMCP_NAMESPACE)
        office_id = "office-sync-diff-name-test"

        # 第一个 Computer 加入
        # First Computer joins
        ack = computer1.call(
            JOIN_OFFICE_EVENT,
            {"role": "computer", "office_id": office_id, "name": "comp-sync-1"},
        )
        assert_empty_ack(ack, action="第一个Computer应该加入成功 / First Computer should join successfully, error")

        # 连接第二个 Computer（不同名）
        # Connect second Computer (different name)
        computer2.connect(_url(sync_server_port), namespace=SMCP_NAMESPACE)

        # 第二个 Computer 加入同一房间，应该成功
        # Second Computer joins same room, should succeed
        ack2 = computer2.call(
            JOIN_OFFICE_EVENT,
            {"role": "computer", "office_id": office_id, "name": "comp-sync-2"},
        )

        # 验证成功
        # Verify success
        assert_empty_ack(ack2, action="不同名Computer应该加入成功 / Different name Computer should succeed, error")

    finally:
        computer1.disconnect()
        computer2.disconnect()


def test_computer_switch_room_with_same_name_allowed(startup_and_shutdown_local_sync_server, sync_server_port: int):
    """
    中文：测试Computer切换房间：同一个Computer从一个房间切换到另一个房间应该成功
    English: Test Computer switching rooms: same Computer switching from one room to another should succeed
    """
    computer = SimpleClient()

    try:
        # 连接 Computer
        # Connect Computer
        computer.connect(_url(sync_server_port), namespace=SMCP_NAMESPACE)
        computer_name = "switching-comp-sync"

        # 加入第一个房间
        # Join first room
        ack = computer.call(
            JOIN_OFFICE_EVENT,
            {"role": "computer", "office_id": "office-sync-room-1", "name": computer_name},
        )
        assert_empty_ack(ack, action="加入第一个房间应该成功 / Joining first room should succeed, error")

        # 切换到第二个房间（同名Computer）
        # Switch to second room (same name Computer)
        ack2 = computer.call(
            JOIN_OFFICE_EVENT,
            {"role": "computer", "office_id": "office-sync-room-2", "name": computer_name},
        )

        # 验证成功
        # Verify success
        assert_empty_ack(ack2, action="Computer切换房间应该成功 / Computer switching rooms should succeed, error")

    finally:
        computer.disconnect()


def test_list_room_session_info_contains_a2c_version_sync(
    startup_and_shutdown_local_sync_server,
    sync_server_port: int,
) -> None:
    """
    中文：同步镜像 test_version_handshake_server::test_list_room_session_info_contains_a2c_version。
    真实 SMCPAgentClient 连接时由 SDK 自动拼 ``a2c_version`` query → SyncSMCPNamespace.on_connect
    经 sync_base._extract_a2c_version 记录到 session → server:list_room 的 SessionInfo 带出。
    English: Sync mirror of the async handshake list_room test. The real sync SDK client
    auto-appends ``a2c_version``; SyncSMCPNamespace records it at connect and emits it back
    in the ``server:list_room`` SessionInfo.
    """
    office_id = "office-sync-handshake-ver"
    auth = DefaultAgentAuthProvider(agent_id="robot-ver-sync", office_id=office_id)
    agent = SMCPAgentClient(auth_provider=auth)
    agent.connect_to_server(
        _url(sync_server_port),
        namespace=SMCP_NAMESPACE,
        socketio_path="/socket.io",
    )
    try:
        agent.join_office(office_id=office_id, agent_name="robot-ver-sync", namespace=SMCP_NAMESPACE)
        result = agent.call(
            LIST_ROOM_EVENT,
            {"agent": "robot-ver-sync", "req_id": "lr-sync-1", "office_id": office_id},
            namespace=SMCP_NAMESPACE,
        )
        agents = [s for s in result["sessions"] if s["role"] == "agent"]
        assert agents, "房间内应有 agent 会话 / room must contain the agent session"
        assert all(s.get("a2c_version") == PROTOCOL_VERSION for s in agents)
    finally:
        agent.disconnect()


# ======================================================================
# GitHub #31：office/role 隔离负路径（同步镜像，黑盒）
# GitHub #31: office/role isolation negative paths (sync mirror, black-box)
#
# 同步服务器运行于独立进程，只能黑盒验证：处理器显式 raise SMCPNamespaceError
# → 不回 ACK → 客户端 .call() 超时（socketio TimeoutError）。跨房间用例额外
# 断言 Computer 的事件处理器从未被调用，证明在 Server 路由前即被拒绝（非误判超时）。
# The sync server runs in a separate process, so this is black-box: the handler
# raises SMCPNamespaceError → no ACK → the client .call() times out. The
# cross-room case additionally asserts the Computer handler was never invoked,
# proving rejection happened at the Server before routing (not a spurious timeout).
# ======================================================================


def test_get_tools_cross_office_rejected_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    """跨房间 client:get_tools（同步）：目标只在他房 → 立即 flat 404（#215 房内解析，与「不存在」相同），不路由到 Computer。"""
    agent = Client()
    computer = Client()
    computer_invoked = threading.Event()

    @computer.on(GET_TOOLS_EVENT, namespace=SMCP_NAMESPACE)
    def _on_get_tools(data: dict) -> dict:  # pragma: no cover - 不应被调用 / must not be called
        computer_invoked.set()
        return {"tools": [], "req_id": data.get("req_id", "")}

    agent.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    computer.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    try:
        _join_office(agent, role="agent", office_id="office-sync-neg-A", name="robot-sneg-1")
        _join_office(computer, role="computer", office_id="office-sync-neg-B", name="comp-sneg-1")

        ret = agent.call(
            GET_TOOLS_EVENT,
            {"computer": "comp-sneg-1", "agent": "robot-sneg-1", "req_id": "sneg-r1"},
            namespace=SMCP_NAMESPACE,
            timeout=3,
        )
        assert ret == build_computer_not_found_error("comp-sneg-1"), ret
        assert not computer_invoked.is_set(), "跨房间请求不得路由到 Computer / cross-room request must not reach Computer"
    finally:
        agent.disconnect()
        computer.disconnect()


def test_tool_call_wrong_role_rejected_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    """错角色 client:tool_call（同步）：Computer 发起工具调用 → 经 ack 立即回 flat 403（#216：不再超时无 ACK）。"""
    computer = Client()
    computer.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    try:
        _join_office(computer, role="computer", office_id="office-sync-neg-C", name="comp-sneg-2")

        ack = computer.call(
            TOOL_CALL_EVENT,
            {
                "agent": "comp-sneg-2",
                "computer": "comp-sneg-2",
                "req_id": "sneg-r2",
                "tool_name": "t",
                "params": {},
                "timeout": 5,
            },
            namespace=SMCP_NAMESPACE,
            timeout=3,
        )
        assert ack == build_non_agent_client_call_error(), ack
    finally:
        computer.disconnect()


# ---------------------------------------------------------------------------
# 中文：#94 client:get_config 同步中继回归 / English: #94 sync client:get_config relay regression
# ---------------------------------------------------------------------------
def _run_computer_config_client_process(port: int, computer_name_queue: multiprocessing.Queue, error_queue: multiprocessing.Queue) -> None:
    """独立进程运行 Computer 客户端，注册 GET_CONFIG_EVENT handler。"""
    computer = Client()

    @computer.on(GET_CONFIG_EVENT, namespace=SMCP_NAMESPACE)
    def _on_get_config(data: dict):  # noqa: ANN001
        # 占位符原样的最小合法 MCPServerStdioConfig（解析后密钥不外传）
        return {
            "servers": {
                "echo": {
                    "name": "echo",
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
                },
            },
            "inputs": [],
        }

    try:
        computer.connect(_url(port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
        office_id = "office-sync-cfg"
        computer_name = "comp-Scfg"
        _join_office(computer, role="computer", office_id=office_id, name=computer_name)
        computer_name_queue.put(computer_name)
        computer.wait()
    except Exception as e:
        error_queue.put(f"Computer客户端错误: {str(e)}")
    finally:
        computer.disconnect()


def _run_agent_config_client_process(
    port: int,
    computer_name: str,
    result_queue: multiprocessing.Queue,
    error_queue: multiprocessing.Queue,
) -> None:
    """独立进程运行 Agent 客户端，发起 GET_CONFIG_EVENT 调用。"""
    try:
        agent = Client()
        agent_id = "robot-Scfg"
        agent.connect(_url(port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
        _join_office(agent, role="agent", office_id="office-sync-cfg", name=agent_id)
        time.sleep(0.2)
        res = agent.call(
            GET_CONFIG_EVENT,
            {"computer": computer_name, "agent": agent_id, "req_id": "req-sync-cfg-1"},
            namespace=SMCP_NAMESPACE,
            timeout=15,
        )
        result_queue.put(res)
        agent.disconnect()
    except Exception as e:
        error_queue.put(f"Agent客户端错误: {str(e)}")


def test_get_config_success_sync(startup_and_shutdown_local_sync_server: Namespace, sync_server_port: int) -> None:
    """同步环境下 client:get_config 中继成功（#94），多进程避免 GIL 阻塞。修复前返回 None → 失败。"""
    computer_name_queue: multiprocessing.Queue = multiprocessing.Queue()
    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    error_queue: multiprocessing.Queue = multiprocessing.Queue()

    computer_process = multiprocessing.Process(
        target=_run_computer_config_client_process,
        args=(sync_server_port, computer_name_queue, error_queue),
        daemon=True,
    )
    computer_process.start()

    try:
        try:
            computer_name = computer_name_queue.get(timeout=5)
        except Exception:
            if not error_queue.empty():
                pytest.fail(f"Computer客户端启动失败: {error_queue.get()}")
            pytest.fail("获取Computer NAME超时")

        agent_process = multiprocessing.Process(
            target=_run_agent_config_client_process,
            args=(sync_server_port, computer_name, result_queue, error_queue),
            daemon=True,
        )
        agent_process.start()

        try:
            try:
                result = result_queue.get(timeout=20)
                assert isinstance(result, dict), f"期望返回 GetComputerConfigRet dict，实际：{type(result)}"
                assert result.get("servers") and result["servers"]["echo"]["type"] == "stdio"
            except AssertionError:
                raise
            except Exception:
                if not error_queue.empty():
                    pytest.fail(f"Agent客户端执行失败: {error_queue.get()}")
                pytest.fail("Agent执行超时")
        finally:
            if agent_process.is_alive():
                agent_process.terminate()
                agent_process.join(timeout=2)
    finally:
        if computer_process.is_alive():
            computer_process.terminate()
            computer_process.join(timeout=2)


def test_rejected_same_name_join_is_isolated_sync(
    startup_and_shutdown_local_sync_server,
    sync_server_port: int,
) -> None:
    """#213 sync：房内同名被拒（4105）的客户端不留在目标房（服务端权威 ``list_room`` + notify 隔离）。

    #215 起跨 office 同名合法，故冲突改在目标房内构造；``twin`` 正对照钉住「他房同名放行」。

    同步服务端在**独立进程**中运行，测试侧读不到它的真实成员关系 ⇒ 用服务端权威的
    ``server:list_room`` 与 wire 上的 ``notify:*`` 断言终态。「**从未**入房」这一顺序保证由单元
    用例（断言 socketio 层 ``enter_room`` 未被调用）与 async 集成用例（钩住 ``server.enter_room``
    记录）覆盖——只看终态会把「已入房 → 失败收敛」的实现误判为通过（变异验证证实）。
    """
    holder = Client()
    subject = Client()
    control = Client()
    joiner = Client()
    twin = Client()

    on_control: list[dict] = []
    on_subject: list[dict] = []

    @control.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    def _on_enter_control(data: dict) -> None:  # noqa: ANN001
        on_control.append(data)

    @subject.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    def _on_enter_subject(data: dict) -> None:  # noqa: ANN001
        on_subject.append(data)

    office_a, office_b = "office-213-sync-a", "office-213-sync-b"
    for client in (holder, subject, control, joiner, twin):
        client.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")

    _join_office(holder, role="computer", office_id=office_b, name="c213s")
    # #215 正对照：他房同名合法（_join_office 内断言空 ack）
    _join_office(twin, role="computer", office_id=office_a, name="c213s")
    _join_office(control, role="computer", office_id=office_b, name="c213s-control")

    ack = subject.call(
        JOIN_OFFICE_EVENT,
        {"role": "computer", "office_id": office_b, "name": "c213s"},
        namespace=SMCP_NAMESPACE,
    )
    assert_rejected_ack(ack, 4105, action="server:join_office")  # 目标房内同 role 同名

    # 服务端权威视图：被拒者不得出现在目标房成员列表里
    listed = control.call(
        LIST_ROOM_EVENT,
        {"agent": control.get_sid(namespace=SMCP_NAMESPACE), "req_id": "req-213", "office_id": office_b},
        namespace=SMCP_NAMESPACE,
    )
    subject_sid = subject.get_sid(namespace=SMCP_NAMESPACE)
    listed_sids = [s["sid"] for s in listed["sessions"]]
    names = sorted(s["name"] for s in listed["sessions"])
    assert subject_sid not in listed_sids, f"被拒客户端不得出现在目标房成员列表：{listed['sessions']}"
    assert names == ["c213s", "c213s-control"], f"目标房成员应为持有者 + 对照：{names}"

    # 验收口径：被拒客户端收不到该房任何 notify:*；正对照：合法成员收得到
    _join_office(joiner, role="computer", office_id=office_b, name="c213s-joiner")
    time.sleep(0.3)
    assert on_control, "正对照：目标房合法成员应收到 notify:enter_office"
    assert on_subject == [], "被拒客户端不得收到目标房任何 notify:*"

    for client in (holder, subject, control, joiner, twin):
        client.disconnect()


def test_rename_on_live_session_rejected_keeps_old_room_sync(
    startup_and_shutdown_local_sync_server,
    sync_server_port: int,
) -> None:
    """#221 sync：同一连接内改名（并换房）⇒ `403`，且**校验先于副作用** ⇒ 仍留在旧房。

    协议 events.md:610 / faq.md:162：改身份须换新连接。这是「**已在旧房** + 换房被拒」路径的集成
    守护（#213 原口径不变：修复前该路径先退旧房再报错，客户端落成「无房」却仍以为在旧房）。
    """
    peer = Client()
    mover = Client()
    latecomer = Client()

    on_peer_leave: list[dict] = []
    on_mover_enter: list[dict] = []

    @peer.on(LEAVE_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    def _on_peer_leave(data: dict) -> None:  # noqa: ANN001
        on_peer_leave.append(data)

    @mover.on(ENTER_OFFICE_NOTIFICATION, namespace=SMCP_NAMESPACE)
    def _on_mover_enter(data: dict) -> None:  # noqa: ANN001
        on_mover_enter.append(data)

    office_a, office_b = "office-221-mv-sync-a", "office-221-mv-sync-b"
    for client in (peer, mover, latecomer):
        client.connect(_url(sync_server_port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")

    _join_office(peer, role="computer", office_id=office_a, name="peer-221s")
    _join_office(mover, role="computer", office_id=office_a, name="mover-221s")

    ack = mover.call(
        JOIN_OFFICE_EVENT,
        {"role": "computer", "office_id": office_b, "name": "renamed-221s"},
        namespace=SMCP_NAMESPACE,
    )
    assert_rejected_ack(ack, 403, action="server:join_office")
    assert ack["message"] == "Role or name mismatch with existing session", ack
    time.sleep(0.2)
    assert on_peer_leave == [], "换房被拒不得让旧房对端看到 notify:leave_office"

    # 权威视图：mover 仍在 office-A，且身份未落地（仍列旧名）
    listed = peer.call(
        LIST_ROOM_EVENT,
        {"agent": peer.get_sid(namespace=SMCP_NAMESPACE), "req_id": "req-221-mv", "office_id": office_a},
        namespace=SMCP_NAMESPACE,
    )
    names = sorted(s["name"] for s in listed["sessions"])
    assert names == ["mover-221s", "peer-221s"], f"换房被拒后旧房成员应原封不动（含旧名）：{names}"

    # 仍是活成员：新成员入房时它照常收到旧房的 notify:enter_office（正对照）
    _join_office(latecomer, role="computer", office_id=office_a, name="late-221s")
    time.sleep(0.3)
    assert on_mover_enter, "旧房仍应把新成员入房广播给 mover"

    for client in (peer, mover, latecomer):
        client.disconnect()


# ---------------------------------------------------------------------------
# #216 取值域与边界校验加固（同步）—— 真实 socketio 链路验收 / wire-level acceptance (sync)
# ---------------------------------------------------------------------------

_OFFICE_LESS_ROUTES_SYNC = [
    (GET_TOOLS_EVENT, {"computer": "pc", "agent": "ag", "req_id": "r"}),
    (GET_RESOURCES_EVENT, {"computer": "pc", "agent": "ag", "req_id": "r", "mcp_server": "s"}),
    (GET_CONFIG_EVENT, {"computer": "pc", "agent": "ag", "req_id": "r"}),
    (TOOL_CALL_EVENT, {"computer": "pc", "agent": "ag", "req_id": "r", "tool_name": "t", "params": {}, "timeout": 5}),
]


def _connect_sync(client: Client, port: int) -> str:
    client.connect(_url(port), namespaces=[SMCP_NAMESPACE], socketio_path="/socket.io")
    return client.get_sid(namespace=SMCP_NAMESPACE)


def test_office_less_client_calls_return_4103_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    """#216 §四（同步）：未入房发起 client:* ⇒ call() 立即**返回** flat 4103（不再 SioTimeoutError）。"""
    agent = Client()
    _connect_sync(agent, sync_server_port)
    try:
        for event, payload in _OFFICE_LESS_ROUTES_SYNC:
            ack = agent.call(event, payload, namespace=SMCP_NAMESPACE, timeout=3)
            assert ack == build_room_rejection_error(ErrorCode.NOT_IN_ROOM), (event, ack)
    finally:
        agent.disconnect()


def test_cross_office_get_config_returns_404_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    """#216 §四 跨房（同步）：get_config 目标只在他房 ⇒ call() 返回 flat 404。"""
    agent, computer = Client(), Client()
    _connect_sync(agent, sync_server_port)
    _connect_sync(computer, sync_server_port)
    try:
        _join_office(agent, role="agent", office_id="office-s216-A", name="robot-s216")
        _join_office(computer, role="computer", office_id="office-s216-B", name="comp-s216")
        ack = agent.call(
            GET_CONFIG_EVENT, {"computer": "comp-s216", "agent": "robot-s216", "req_id": "r"}, namespace=SMCP_NAMESPACE, timeout=3
        )
        assert ack == build_computer_not_found_error("comp-s216"), ack
    finally:
        agent.disconnect()
        computer.disconnect()


def test_malformed_client_calls_return_400_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    """#216 §一（同步）：client:* 载荷畸形（无载荷 / 缺字段 / 多参）⇒ call() 立即返回 flat 400。"""
    agent = Client()
    _connect_sync(agent, sync_server_port)
    try:
        _join_office(agent, role="agent", office_id="office-s216-bad", name="robot-s216-bad")
        for data in (None, {"agent": "ag", "req_id": "r"}, ({"computer": "pc", "agent": "ag", "req_id": "r"}, "extra")):
            ack = agent.call(GET_TOOLS_EVENT, data, namespace=SMCP_NAMESPACE, timeout=3)
            assert ack == {"code": 400, "message": "Invalid request payload"}, (data, ack)
    finally:
        agent.disconnect()


def test_office_id_equal_to_peer_sid_cannot_reach_private_room_sync(
    startup_and_shutdown_local_sync_server, sync_server_port: int
) -> None:
    """#216 §三 回归（同步）：以 ``office_id = <对端 SID>`` 入房并广播，对端收不到；同房第三方正对照收得到。"""
    victim, attacker, observer = Client(), Client(), Client()
    victim_got: list[dict] = []
    observer_got: list[dict] = []

    @victim.on("notify:update_config", namespace=SMCP_NAMESPACE)
    def _victim(data: dict) -> None:
        victim_got.append(data)

    @observer.on("notify:update_config", namespace=SMCP_NAMESPACE)
    def _observer(data: dict) -> None:
        observer_got.append(data)

    victim_sid = _connect_sync(victim, sync_server_port)
    _connect_sync(observer, sync_server_port)
    _connect_sync(attacker, sync_server_port)
    try:
        _join_office(observer, role="agent", office_id=victim_sid, name="observer-s216")
        _join_office(attacker, role="computer", office_id=victim_sid, name="attacker-s216")

        attacker.emit(UPDATE_CONFIG_EVENT, {"computer": "attacker-s216"}, namespace=SMCP_NAMESPACE)
        deadline = time.monotonic() + 3
        while not observer_got and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)

        assert observer_got == [{"computer": "attacker-s216"}], observer_got
        assert victim_got == [], f"office_id=对端SID 的广播泄漏进了对端私有房: {victim_got!r}"
    finally:
        victim.disconnect()
        attacker.disconnect()
        observer.disconnect()


def test_name_conflict_ack_never_leaks_peer_sid_sync(startup_and_shutdown_local_sync_server, sync_server_port: int) -> None:
    """#216 §二 验收（同步）：同名 join 被拒（4105）的 ack 不含对端真实 sid、不含 namespace。"""
    holder, challenger = Client(), Client()
    holder_sid = _connect_sync(holder, sync_server_port)
    _connect_sync(challenger, sync_server_port)
    try:
        _join_office(holder, role="computer", office_id="office-s216-dup", name="dup-s216")
        ack = challenger.call(
            JOIN_OFFICE_EVENT, {"role": "computer", "office_id": "office-s216-dup", "name": "dup-s216"}, namespace=SMCP_NAMESPACE
        )
        assert_rejected_ack(ack, 4105)
        blob = json.dumps(ack, ensure_ascii=False)
        assert holder_sid not in blob, blob
        assert SMCP_NAMESPACE not in blob, blob
    finally:
        holder.disconnect()
        challenger.disconnect()
