"""
* 文件名: test_smcp_namespace_sync
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: pytest, socketio
* 描述: 同步版 SMCP Namespace 测试用例 / Sync SMCP Namespace test cases
"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest
from socketio.exceptions import TimeoutError as SioTimeoutError

from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.server import (
    DefaultSyncAuthenticationProvider,
    SyncAuthenticationProvider,
    SyncSMCPNamespace,
)
from a2c_smcp.server.sync_base import SyncBaseNamespace
from a2c_smcp.smcp import (
    CANCEL_TOOL_CALL_NOTIFICATION,
    ENTER_OFFICE_NOTIFICATION,
    GET_BLOB_EVENT,
    GET_SKILL_EVENT,
    GET_SKILLS_EVENT,
    GET_TOOLS_EVENT,
    LEAVE_OFFICE_NOTIFICATION,
    SMCP_NAMESPACE,
    UPDATE_CONFIG_NOTIFICATION,
    UPDATE_DESKTOP_NOTIFICATION,
    UPDATE_SKILLS_NOTIFICATION,
    UPDATE_TOOL_LIST_NOTIFICATION,
    EnterOfficeReq,
    ErrorCode,
    LeaveOfficeReq,
    is_protocol_error_payload,
)
from tests.room_acks import assert_empty_ack, assert_rejected_ack


class MockSyncAuthProvider(SyncAuthenticationProvider):
    """Mock同步认证提供者 / Mock sync authentication provider"""

    def authenticate(self, sio, environ: dict, auth: dict | None, headers: list) -> bool:  # noqa: D401
        for header in headers:
            if isinstance(header, (list, tuple)) and len(header) >= 2:
                header_name = header[0].decode("utf-8").lower() if isinstance(header[0], bytes) else str(header[0]).lower()
                header_value = header[1].decode("utf-8") if isinstance(header[1], bytes) else str(header[1])
                if header_name == "access_token" and header_value == "valid_key":
                    return True
        return False


@pytest.fixture
def mock_auth_provider():
    return MockSyncAuthProvider()


@pytest.fixture
def smcp_namespace(mock_auth_provider):
    return SyncSMCPNamespace(mock_auth_provider)


@pytest.fixture
def mock_server():
    server = MagicMock()
    server.app = MagicMock()
    server.app.state = MagicMock()
    server.app.state.agent_id = "test_agent"
    server.manager = MagicMock()
    # get_participants 返回空列表
    server.manager.get_participants.return_value = []
    return server


class TestSyncSMCPNamespace:
    def test_namespace_initialization(self, smcp_namespace):
        assert smcp_namespace.namespace == SMCP_NAMESPACE
        assert isinstance(smcp_namespace.auth_provider, MockSyncAuthProvider)

    def test_successful_connection(self, smcp_namespace, mock_server):
        smcp_namespace.server = mock_server

        environ = {
            "asgi.scope": {
                "headers": [
                    (b"access_token", b"valid_key"),
                ],
            },
        }

        result = smcp_namespace.on_connect("test_sid", environ, None)
        assert result is True

    def test_failed_authentication(self, smcp_namespace, mock_server):
        smcp_namespace.server = mock_server

        environ = {
            "asgi.scope": {
                "headers": [
                    (b"access_token", b"invalid_key"),
                ],
            },
        }

        with pytest.raises(ConnectionRefusedError):
            smcp_namespace.on_connect("test_sid", environ, None)

    def test_join_office_success(self, smcp_namespace):
        # mock 会话相关方法
        session = {}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace.enter_room = MagicMock()

        data = EnterOfficeReq(**{
            "role": "computer",
            "name": "test_computer",
            "office_id": "office_123",
        })

        ack = smcp_namespace.on_server_join_office("test_sid", data)

        assert_empty_ack(ack)
        assert session["role"] == "computer"
        assert session["name"] == "test_computer"

    def test_join_office_role_mismatch(self, smcp_namespace):
        session = {"role": "agent"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()

        data = EnterOfficeReq(**{
            "role": "computer",
            "name": "test_computer",
            "office_id": "office_123",
        })

        ack = smcp_namespace.on_server_join_office("test_sid", data)

        # 身份声明冲突 ⇒ 403，**非**房间语义
        assert_rejected_ack(ack, 403)

    def test_leave_office(self, smcp_namespace):
        # 房间号取自会话（权威），故会话必须带 office_id / the room comes from the session
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "office_id": "office_123"})
        smcp_namespace.leave_room = MagicMock()

        data = LeaveOfficeReq(**{"office_id": "office_123"})

        ack = smcp_namespace.on_server_leave_office("test_sid", data)

        assert_empty_ack(ack, action="server:leave_office")
        smcp_namespace.leave_room.assert_called_once_with("test_sid", "office_123")


class TestEnterRoomTransactionalCommitSync:
    """#213 sync mirror：``enter_room`` 的「校验先于副作用」与「失败即收敛」不变量。

    Sync mirror of async ``TestEnterRoomTransactionalCommit``. 协议口径（room-model.md
    §Computer 加入规则 注记 3）：任何会改变既有成员关系的动作（入房 / 退房 / 广播
    ``notify:leave_office``）都必须排在所有可能失败的校验之后；任何一步抛错后，socketio 的真实
    成员关系必须与会话状态一致（被拒客户端**不得**留在房里收 ``notify:*``）。
    """

    def test_cross_office_name_conflict_touches_nothing(self, smcp_namespace, mock_server):
        """跨 office 同名被拒：旧房未动、目标房未进、无广播、会话仍是旧房。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(return_value=["c-sid", "roomA"])
        session = {"role": "computer", "name": "dup", "office_id": "roomA", "sid": "c-sid"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace.emit = MagicMock()
        smcp_namespace.leave_room = MagicMock()
        smcp_namespace._name_to_sid_map = {"dup": "other-sid"}

        with pytest.raises(ValueError, match="Name already taken in room"):
            smcp_namespace.enter_room("c-sid", "roomB")

        smcp_namespace.leave_room.assert_not_called()  # 原实现先退旧房 / old impl left the old room first
        smcp_namespace.emit.assert_not_called()
        mock_server.enter_room.assert_not_called()  # 目标房从未进入 / never joined the target room
        assert session["office_id"] == "roomA"
        assert smcp_namespace._name_to_sid_map == {"dup": "other-sid"}

    def test_move_with_self_owned_name_succeeds(self, smcp_namespace, mock_server):
        """换房且注册表里的 name 属于**本 sid** ⇒ 必须放行（``existing_sid == sid`` 豁免）。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(return_value=["c-sid", "roomA", "roomB"])
        session = {"role": "computer", "name": "dup", "office_id": "roomA", "sid": "c-sid"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace.emit = MagicMock()
        smcp_namespace._name_to_sid_map = {"dup": "c-sid"}

        smcp_namespace.enter_room("c-sid", "roomB")

        mock_server.leave_room.assert_called_once_with("c-sid", "roomA", namespace=SMCP_NAMESPACE)
        assert session["office_id"] == "roomB"
        assert smcp_namespace._name_to_sid_map == {"dup": "c-sid"}
        assert [c.args[0] for c in smcp_namespace.emit.call_args_list] == [
            LEAVE_OFFICE_NOTIFICATION,
            ENTER_OFFICE_NOTIFICATION,
        ]

    def test_phase2_failure_converges_membership_and_session(self, smcp_namespace, mock_server):
        """入房之后失败（末步广播抛错）⇒ 摘除房间 + 清会话 + 回收 name。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(return_value=["solo", "roomB"])
        session = {"role": "computer", "name": "solo", "sid": "solo"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace.emit = MagicMock(side_effect=RuntimeError("broadcast boom"))

        with pytest.raises(RuntimeError, match="broadcast boom"):
            smcp_namespace.enter_room("solo", "roomB")

        mock_server.leave_room.assert_called_once_with("solo", "roomB", namespace=SMCP_NAMESPACE)
        assert "office_id" not in session
        assert "solo" not in smcp_namespace._name_to_sid_map

    def test_phase2_success_is_untouched_by_convergence(self, smcp_namespace, mock_server):
        """**正对照**：同装置成功路径 ⇒ 成员关系 / 会话 / 注册 / 广播各就位。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(return_value=["solo", "roomB"])
        session = {"role": "computer", "name": "solo", "sid": "solo"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace.emit = MagicMock()

        smcp_namespace.enter_room("solo", "roomB")

        assert session["office_id"] == "roomB"
        assert smcp_namespace._name_to_sid_map["solo"] == "solo"
        smcp_namespace.emit.assert_called_once()
        mock_server.leave_room.assert_not_called()  # 成功路径不得出现摘除 / no eviction on success

    def test_leave_room_not_committed_keeps_old_room(self, smcp_namespace, mock_server):
        """换房时旧房离开**未提交**（广播前抛错）⇒ 一切原样，收敛不得触发。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(return_value=["c-sid", "roomA"])
        session = {"role": "computer", "name": "n", "office_id": "roomA", "sid": "c-sid"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace.emit = MagicMock(side_effect=RuntimeError("leave broadcast boom"))
        smcp_namespace._name_to_sid_map = {"n": "c-sid"}

        with pytest.raises(RuntimeError, match="leave broadcast boom"):
            smcp_namespace.enter_room("c-sid", "roomB")

        assert session["office_id"] == "roomA"
        mock_server.leave_room.assert_not_called()
        assert smcp_namespace._name_to_sid_map == {"n": "c-sid"}

    def test_leave_room_committed_converges_to_no_room(self, smcp_namespace, mock_server):
        """换房时旧房离开**已提交**后失败（入房通知抛错）⇒ 收敛为「无房」。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(return_value=["c-sid", "roomB"])  # 已退旧房、已入新房
        session = {"role": "computer", "name": "n", "office_id": "roomA", "sid": "c-sid"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace._name_to_sid_map = {"n": "c-sid"}

        def _emit_boom(event: str, *args: object, **kwargs: object) -> None:
            if event == ENTER_OFFICE_NOTIFICATION:
                raise RuntimeError("enter broadcast boom")

        smcp_namespace.emit = MagicMock(side_effect=_emit_boom)

        with pytest.raises(RuntimeError, match="enter broadcast boom"):
            smcp_namespace.enter_room("c-sid", "roomB")

        left = [c.args[:2] for c in mock_server.leave_room.call_args_list]
        assert ("c-sid", "roomA") in left and ("c-sid", "roomB") in left
        assert "office_id" not in session
        assert "n" not in smcp_namespace._name_to_sid_map

    def test_join_office_rollback_removes_fields_absent_from_backup(self, smcp_namespace, mock_server):
        """backup 里**没有** role/name（全新连接首连即被拒）⇒ 回滚须删除字段，而非留下 ``None``。"""
        smcp_namespace.server = mock_server
        store: dict[str, dict] = {"c-sid": {"sid": "c-sid"}}
        smcp_namespace.get_session = MagicMock(side_effect=lambda sid, *a, **k: store[sid])
        smcp_namespace.save_session = MagicMock(side_effect=lambda sid, sess, *a, **k: store.__setitem__(sid, sess))
        smcp_namespace.enter_room = MagicMock(side_effect=RuntimeError("boom"))

        ack = smcp_namespace.on_server_join_office(
            "c-sid",
            EnterOfficeReq(**{"role": "computer", "name": "n", "office_id": "roomB"}),
        )

        assert_rejected_ack(ack, 500)
        assert "boom" not in json.dumps(ack)
        final = store["c-sid"]
        assert "role" not in final and "name" not in final, "backup 缺失的字段须删除而非赋 None"

    def test_convergence_failure_does_not_mask_original_error(self, smcp_namespace, mock_server):
        """收敛自身抛错（读真实成员关系失败）⇒ 原始异常必须原样上抛，收敛错误只记日志。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(side_effect=RuntimeError("rooms boom"))
        session = {"role": "computer", "name": "solo", "sid": "solo"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace.emit = MagicMock(side_effect=RuntimeError("broadcast boom"))

        with pytest.raises(RuntimeError, match="broadcast boom"):
            smcp_namespace.enter_room("solo", "roomB")

    def test_convergence_never_releases_another_sids_name(self, smcp_namespace, mock_server):
        """收敛回收 name 带**归属守卫**：并发下被他人抢走的 name 绝不能被本次失败注销。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(return_value=["solo", "roomB"])
        session = {"role": "computer", "name": "solo", "sid": "solo"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()

        def _emit_hijack_then_boom(event: str, *args: object, **kwargs: object) -> None:
            if event == ENTER_OFFICE_NOTIFICATION:
                smcp_namespace._name_to_sid_map["solo"] = "intruder"  # 并发抢占
                raise RuntimeError("broadcast boom")

        smcp_namespace.emit = MagicMock(side_effect=_emit_hijack_then_boom)

        with pytest.raises(RuntimeError, match="broadcast boom"):
            smcp_namespace.enter_room("solo", "roomB")

        assert smcp_namespace._name_to_sid_map == {"solo": "intruder"}, "不得替抢占者注销 name 映射"

    def test_agent_name_conflict_never_enters_room(self, smcp_namespace, mock_server):
        """闸门**角色无关**：Agent 撞跨 office 同名同样不得进入房间。"""
        smcp_namespace.server = mock_server
        mock_server.rooms = MagicMock(return_value=["a-sid"])
        mock_server.manager.get_participants.return_value = []
        session = {"role": "agent", "name": "dup", "sid": "a-sid"}
        smcp_namespace.get_session = MagicMock(return_value=session)
        smcp_namespace.save_session = MagicMock()
        smcp_namespace.emit = MagicMock()
        smcp_namespace._name_to_sid_map = {"dup": "other-sid"}

        with pytest.raises(ValueError, match="Name already taken in room"):
            smcp_namespace.enter_room("a-sid", "roomB")

        mock_server.enter_room.assert_not_called()
        smcp_namespace.emit.assert_not_called()
        assert "office_id" not in session

    def test_join_office_rejection_does_not_restore_stale_office(self, smcp_namespace, mock_server):
        """处理器回滚只回 ``role`` / ``name``：``enter_room`` 收敛掉的房号不得被 backup 复活。"""
        smcp_namespace.server = mock_server
        store: dict[str, dict] = {"c-sid": {"role": "computer", "name": "old-name", "office_id": "roomA", "sid": "c-sid"}}
        smcp_namespace.get_session = MagicMock(side_effect=lambda sid, *a, **k: store[sid])
        smcp_namespace.save_session = MagicMock(side_effect=lambda sid, sess, *a, **k: store.__setitem__(sid, sess))

        def _enter_boom(*args: object, **kwargs: object) -> None:
            store["c-sid"].pop("office_id", None)  # 模拟 enter_room 的失败收敛
            raise RuntimeError("converged then boom")

        smcp_namespace.enter_room = MagicMock(side_effect=_enter_boom)

        ack = smcp_namespace.on_server_join_office(
            "c-sid",
            EnterOfficeReq(**{"role": "computer", "name": "new-name", "office_id": "roomB"}),
        )

        assert_rejected_ack(ack, 500)
        assert "boom" not in json.dumps(ack)
        final = store["c-sid"]
        assert "office_id" not in final, "收敛结果不得被 backup 里的旧房号复活"
        assert final["role"] == "computer" and final["name"] == "old-name", "role/name 须回滚"


class TestV021ClientRoutesAndUpdateSkillsSync:
    """v0.2.1 #41 sync mirror：3 ``client:*`` 路由 + ``server:update_skills`` 广播.

    Sync mirror of async ``TestV021ClientRoutesAndUpdateSkills``.
    """

    @pytest.fixture
    def routed_ns(self, smcp_namespace, mock_server):
        smcp_namespace.server = mock_server
        agent_sid = "a-sid"
        comp_name = "c1"
        comp_sid = "c-sid"
        sess_agent = {"role": "agent", "office_id": "room1", "name": "agent-1"}
        sess_comp = {"role": "computer", "office_id": "room1", "name": comp_name}
        smcp_namespace.get_session = MagicMock(side_effect=lambda sid: sess_comp if sid == comp_sid else sess_agent)
        smcp_namespace._name_to_sid_map = {comp_name: comp_sid, "agent-1": agent_sid}
        smcp_namespace.call = MagicMock()
        return smcp_namespace, agent_sid, comp_name, comp_sid

    def test_on_client_get_skills_relays(self, routed_ns):
        ns, agent_sid, comp_name, comp_sid = routed_ns
        ns.call.return_value = {
            "skills": [{"name": "user:x:y", "source": "user", "path": "/s/x/y", "description": "demo skill"}],
            "req_id": "r1",
        }
        ret = ns.on_client_get_skills(agent_sid, {"agent": "agent-1", "req_id": "r1", "computer": comp_name})
        ns.call.assert_called_once()
        args, kwargs = ns.call.call_args
        assert args[0] == GET_SKILLS_EVENT
        assert kwargs["to"] == comp_sid
        assert kwargs["namespace"] == SMCP_NAMESPACE
        # 语义级断言：内容完整透传 / Semantic-level assertion: contents passed through
        assert ret["skills"] == [{"name": "user:x:y", "source": "user", "path": "/s/x/y", "description": "demo skill"}]
        assert ret["req_id"] == "r1"

    def test_on_client_get_skill_relays(self, routed_ns):
        ns, agent_sid, comp_name, _comp_sid = routed_ns
        ns.call.return_value = {
            "name": "user:x:y",
            "rel_path": "SKILL.md",
            "mime_type": "text/markdown",
            "total_size": 5,
            "sha256": "a" * 64,
            "body": "# hi",
            "req_id": "r2",
        }
        ret = ns.on_client_get_skill(
            agent_sid,
            {"agent": "agent-1", "req_id": "r2", "computer": comp_name, "name": "user:x:y"},
        )
        assert ns.call.call_args[0][0] == GET_SKILL_EVENT
        assert ret["body"] == "# hi"

    def test_on_client_get_blob_relays(self, routed_ns):
        ns, agent_sid, comp_name, _comp_sid = routed_ns
        ns.call.return_value = {
            "blob_handle": "h",
            "mime_type": "image/png",
            "total_size": 3,
            "sha256": "b" * 64,
            "chunk_offset": 0,
            "eof": True,
            "blob": "AAAA",
            "req_id": "r3",
        }
        ret = ns.on_client_get_blob(
            agent_sid,
            {"agent": "agent-1", "req_id": "r3", "computer": comp_name, "blob_handle": "h"},
        )
        assert ns.call.call_args[0][0] == GET_BLOB_EVENT
        assert ret["eof"] is True

    def test_get_skill_4017_passthrough(self, routed_ns):
        """``4017 traversal`` flat ErrorPayload 跨 sync 路由透传 / 4017 passes through sync routing.

        显式镜像 async 同名用例，避免依赖 ``_relay_client_call`` 抽象等价性的间接证据。
        Explicit sync mirror of async case; avoids relying solely on shared-helper equivalence."""
        ns, agent_sid, comp_name, _comp_sid = routed_ns
        ns.call.return_value = {
            "code": int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE),
            "message": "Skill resource not accessible",
            "details": {"reason": "traversal", "rel_path": "../etc"},
        }
        ret = ns.on_client_get_skill(
            agent_sid,
            {"agent": "agent-1", "req_id": "r-err", "computer": comp_name, "name": "user:x:y"},
        )
        assert ret["code"] == int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE)
        assert ret["details"]["reason"] == "traversal"
        assert ret["details"]["rel_path"] == "../etc"

    def test_get_skill_4014_name_absent_passthrough(self, routed_ns):
        """SKILL ``name`` 格式合法但 Registry 未命中复用 4014（sync mirror of async case）."""
        ns, agent_sid, comp_name, _comp_sid = routed_ns
        ns.call.return_value = {"code": int(ErrorCode.MCP_SERVER_NOT_FOUND), "message": "skill not found"}
        ret = ns.on_client_get_skill(
            agent_sid,
            {"agent": "agent-1", "req_id": "r-err", "computer": comp_name, "name": "user:no-such:skill"},
        )
        assert ret["code"] == int(ErrorCode.MCP_SERVER_NOT_FOUND)

    def test_get_blob_4018_passthrough(self, routed_ns):
        """``4018 gone`` flat ErrorPayload 跨 sync 路由透传 / 4018 gone passes through sync routing."""
        ns, agent_sid, comp_name, _comp_sid = routed_ns
        ns.call.return_value = {
            "code": int(ErrorCode.BLOB_NOT_ACCESSIBLE),
            "message": "Blob not accessible",
            "details": {"reason": "gone"},
        }
        ret = ns.on_client_get_blob(
            agent_sid,
            {"agent": "agent-1", "req_id": "r-err", "computer": comp_name, "blob_handle": "h"},
        )
        assert ret["code"] == int(ErrorCode.BLOB_NOT_ACCESSIBLE)
        assert ret["details"]["reason"] == "gone"

    def test_get_blob_computer_not_found_returns_error_payload(self, smcp_namespace, mock_server):
        """``computer`` 名未注册 → flat ErrorPayload(404)，不抛未捕获异常（#92；sync mirror of async case）.
        Unregistered ``computer`` → flat ErrorPayload(404), no uncaught raise (#92)."""
        smcp_namespace.server = mock_server
        smcp_namespace._name_to_sid_map = {}
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "office_id": "room1"})
        ret = smcp_namespace.on_client_get_blob(
            "a-sid",
            {"agent": "agent-1", "req_id": "r", "computer": "absent", "blob_handle": "h"},
        )
        assert ret["code"] == int(ErrorCode.NOT_FOUND)
        assert is_protocol_error_payload(ret) is True
        assert ret["details"]["computer_name"] == "absent"

    def test_on_client_tool_call_computer_not_found_returns_error_payload(self, smcp_namespace, mock_server):
        """sync：``client:tool_call`` 目标 Computer 名未注册 → flat ErrorPayload(404)，不抛未捕获 ValueError（#99；sync mirror）.
        Sync mirror: unregistered target Computer → flat ErrorPayload(404), no uncaught ValueError (#99)."""
        smcp_namespace.server = mock_server
        smcp_namespace._name_to_sid_map = {}
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "office_id": "room1"})
        smcp_namespace.call = MagicMock()
        ret = smcp_namespace.on_client_tool_call(
            "a-sid",
            {"agent": "agent-1", "req_id": "r", "computer": "absent", "tool_name": "t", "params": {}, "timeout": 5},
        )
        assert ret["code"] == int(ErrorCode.NOT_FOUND)
        assert is_protocol_error_payload(ret) is True
        assert ret["details"]["computer_name"] == "absent"
        smcp_namespace.call.assert_not_called()  # 未找到时不应转发 / no relay on not-found

    def test_get_blob_cross_office_raises_smcp_namespace_error(self, smcp_namespace, mock_server):
        """跨房间 → 显式 raise SMCPNamespaceError（对齐 #31 `-O` 加固）.
        Cross-office → explicit raise SMCPNamespaceError (per #31)."""
        smcp_namespace.server = mock_server
        comp_sid = "c-sid"
        smcp_namespace._name_to_sid_map = {"c1": comp_sid, "agent-1": "a-sid"}
        sess_comp = {"role": "computer", "office_id": "room1", "name": "c1"}
        sess_agent = {"role": "agent", "office_id": "room2", "name": "agent-1"}
        smcp_namespace.get_session = MagicMock(
            side_effect=lambda sid: sess_comp if sid == comp_sid else sess_agent,
        )
        with pytest.raises(SMCPNamespaceError, match="跨房间"):
            smcp_namespace.on_client_get_blob(
                "a-sid",
                {"agent": "agent-1", "req_id": "r", "computer": "c1", "blob_handle": "h"},
            )

    def test_on_server_update_skills_broadcasts(self, smcp_namespace, mock_server):
        smcp_namespace.server = mock_server
        comp_sid = "c-sid"
        comp_name = "c1"
        sess_comp = {"role": "computer", "office_id": "room1", "name": comp_name}
        smcp_namespace.get_session = MagicMock(return_value=sess_comp)
        smcp_namespace.emit = MagicMock()
        smcp_namespace.on_server_update_skills(comp_sid, {"computer": comp_name})
        smcp_namespace.emit.assert_called_once()
        args, kwargs = smcp_namespace.emit.call_args
        assert args[0] == UPDATE_SKILLS_NOTIFICATION
        assert args[1] == {"computer": comp_name}
        assert kwargs["room"] == "room1"
        assert kwargs["skip_sid"] == comp_sid

    def test_on_server_update_skills_rejects_non_computer(self, smcp_namespace, mock_server):
        smcp_namespace.server = mock_server
        sess_agent = {"role": "agent", "office_id": "room1", "name": "agent-1"}
        smcp_namespace.get_session = MagicMock(return_value=sess_agent)
        smcp_namespace.emit = MagicMock()
        with pytest.raises(SMCPNamespaceError, match="Computer"):
            smcp_namespace.on_server_update_skills("a-sid", {"computer": "c1"})
        smcp_namespace.emit.assert_not_called()

    # ── v0.2.2 #46（sync 镜像）：旧路由收编 _relay_client_call + 飞行断连防御 ──

    def test_get_tools_relays_via_unified_helper(self, routed_ns):
        """sync：``client:get_tools`` 收编后经 ``_relay_client_call`` 转发到正确事件 + Computer SID."""
        ns, agent_sid, comp_name, comp_sid = routed_ns
        ns.call.return_value = {"tools": [], "req_id": "r1"}
        ret = ns.on_client_get_tools(agent_sid, {"computer": comp_name})
        ns.call.assert_called_once()
        assert ns.call.call_args[0][0] == GET_TOOLS_EVENT
        assert ns.call.call_args.kwargs["to"] == comp_sid
        assert ret["tools"] == []

    def test_get_tools_flat_error_passthrough(self, routed_ns):
        """sync：v0.2.2 旧路由非豁免——``client:get_tools`` 的 flat ErrorPayload 原样透传，不强转 GetToolsRet."""
        ns, agent_sid, comp_name, _comp_sid = routed_ns
        err = {"code": int(ErrorCode.MCP_SERVER_NOT_FOUND), "message": "computer mcp not ready"}
        ns.call.return_value = err
        ret = ns.on_client_get_tools(agent_sid, {"computer": comp_name})
        assert ret["code"] == int(ErrorCode.MCP_SERVER_NOT_FOUND)

    def test_get_desktop_flat_error_passthrough(self, routed_ns):
        """sync：``client:get_desktop`` 同样透传 flat ErrorPayload."""
        ns, agent_sid, comp_name, _comp_sid = routed_ns
        err = {"code": int(ErrorCode.MCP_SERVER_NOT_FOUND), "message": "no desktop"}
        ns.call.return_value = err
        ret = ns.on_client_get_desktop(agent_sid, {"computer": comp_name})
        assert ret["code"] == int(ErrorCode.MCP_SERVER_NOT_FOUND)

    def test_relay_agent_session_none_raises_namespace_error(self, smcp_namespace, mock_server):
        """sync：发起者飞行中断连 ``get_session(sid)`` → None → **显式 raise SMCPNamespaceError**（替代 AttributeError，#46/#31）."""
        smcp_namespace.server = mock_server
        comp_sid = "c-sid"
        comp_name = "c1"
        smcp_namespace._name_to_sid_map = {comp_name: comp_sid}
        sess_comp = {"role": "computer", "office_id": "room1", "name": comp_name}
        smcp_namespace.get_session = MagicMock(side_effect=lambda sid: sess_comp if sid == comp_sid else None)
        smcp_namespace.call = MagicMock()
        with pytest.raises(SMCPNamespaceError, match="session gone"):
            smcp_namespace.on_client_get_tools("gone-agent-sid", {"computer": comp_name})
        smcp_namespace.call.assert_not_called()


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """轮询线程间条件。Poll a cross-thread condition until true or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class TestInflightTargetDisconnectGuardSync:
    """#100 Phase 1 sync mirror：目标 Computer 在途断连 → 即时回 flat ErrorPayload(404)，不静默挂死到满超时.

    同步版 ``self.call`` 阻塞当前 worker 线程、其等待原语在 ``call()`` 内部不可达，故守卫把 ``self.call`` 丢到
    daemon 子线程跑、主线程等「完成 OR 由 ``on_disconnect`` 触发的断连信号」共享事件；断连先到 → 404（子线程
    成为有界僵尸，至自身 timeout 退出）。三结局严格区分：断连→404 / 真超时→抛 TimeoutError / 正常→透传。
    Sync mirror of the async guard. Explicit mirror per the sync/async parity convention.
    """

    @pytest.fixture
    def guard_ns(self, smcp_namespace, mock_server):
        smcp_namespace.server = mock_server
        agent_sid = "a-sid"
        comp_name = "c1"
        comp_sid = "c-sid"
        sess_agent = {"role": "agent", "office_id": "room1", "name": "agent-1"}
        sess_comp = {"role": "computer", "office_id": "room1", "name": comp_name}
        smcp_namespace.get_session = MagicMock(side_effect=lambda sid: sess_comp if sid == comp_sid else sess_agent)
        smcp_namespace._name_to_sid_map = {comp_name: comp_sid, "agent-1": agent_sid}
        smcp_namespace.call = MagicMock()
        return smcp_namespace, agent_sid, comp_name, comp_sid

    @staticmethod
    def _tool_call_req(comp_name: str, timeout: int = 30) -> dict:
        return {"agent": "agent-1", "req_id": "r", "computer": comp_name, "tool_name": "t", "params": {}, "timeout": timeout}

    def test_target_disconnect_midflight_returns_404(self, guard_ns):
        """在途 tool_call + 目标断连 → flat ErrorPayload(404)，registry 清理（核心复现，sync）."""
        ns, agent_sid, comp_name, comp_sid = guard_ns
        started = threading.Event()
        gate = threading.Event()  # 测试期间永不 set → 模拟死目标 / never set: simulates dead target

        def blocking_call(*_a, **_k):
            started.set()
            gate.wait()
            return {"late": True}

        ns.call = MagicMock(side_effect=blocking_call)
        box: dict = {}

        def run():
            box["ret"] = ns.on_client_tool_call(agent_sid, self._tool_call_req(comp_name))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            assert started.wait(timeout=2)  # relay 已下发 self.call（子线程已启动）/ relay dispatched self.call
            assert _wait_until(lambda: comp_sid in ns._inflight_disconnect_signals)
            ns._fire_inflight_disconnect_signals(comp_sid)  # 模拟断连 / simulate disconnect
            worker.join(timeout=2)
            assert not worker.is_alive()
            assert box["ret"]["code"] == int(ErrorCode.NOT_FOUND)
            assert is_protocol_error_payload(box["ret"]) is True
            assert box["ret"]["details"]["computer_name"] == comp_name
            assert comp_sid not in ns._inflight_disconnect_signals
        finally:
            gate.set()  # 释放僵尸子线程 / release the orphaned runner thread
            worker.join(timeout=2)

    def test_genuine_timeout_propagates_not_404(self, guard_ns):
        """目标在线但慢（真超时）→ 原样抛 TimeoutError，**绝不**转 404（sync）."""
        ns, agent_sid, comp_name, comp_sid = guard_ns
        ns.call = MagicMock(side_effect=SioTimeoutError())
        with pytest.raises(SioTimeoutError):
            ns.on_client_tool_call(agent_sid, self._tool_call_req(comp_name, timeout=5))
        assert comp_sid not in ns._inflight_disconnect_signals

    def test_normal_completion_passes_through(self, guard_ns):
        """正常返回 → 透传、registry 清空（sync）."""
        ns, agent_sid, comp_name, comp_sid = guard_ns
        ns.call = MagicMock(return_value={"ok": True})
        ret = ns.on_client_tool_call(agent_sid, self._tool_call_req(comp_name, timeout=5))
        assert ret == {"ok": True}
        assert comp_sid not in ns._inflight_disconnect_signals

    def test_two_concurrent_calls_both_404_on_disconnect(self, guard_ns):
        """同 Computer 两并发在途 → 断连后都 404（value 为 set；sync）."""
        ns, agent_sid, comp_name, comp_sid = guard_ns
        gate = threading.Event()

        def blocking_call(*_a, **_k):
            gate.wait()
            return {"late": True}

        ns.call = MagicMock(side_effect=blocking_call)
        boxes: list[dict] = [{}, {}]

        def run(i: int):
            boxes[i]["ret"] = ns.on_client_tool_call(agent_sid, self._tool_call_req(comp_name))

        workers = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(2)]
        for w in workers:
            w.start()
        try:
            assert _wait_until(lambda: len(ns._inflight_disconnect_signals.get(comp_sid, ())) == 2)
            ns._fire_inflight_disconnect_signals(comp_sid)
            for w in workers:
                w.join(timeout=2)
            assert all(not w.is_alive() for w in workers)
            assert boxes[0]["ret"]["code"] == int(ErrorCode.NOT_FOUND)
            assert boxes[1]["ret"]["code"] == int(ErrorCode.NOT_FOUND)
            assert comp_sid not in ns._inflight_disconnect_signals
        finally:
            gate.set()
            for w in workers:
                w.join(timeout=2)

    def test_cross_office_raises_with_no_dangling_signal(self, smcp_namespace, mock_server):
        """跨房间仍先 raise SMCPNamespaceError，且未登记任何信号（登记在隔离校验之后；sync）."""
        smcp_namespace.server = mock_server
        comp_sid = "c-sid"
        smcp_namespace._name_to_sid_map = {"c1": comp_sid, "agent-1": "a-sid"}
        sess_comp = {"role": "computer", "office_id": "room1", "name": "c1"}
        sess_agent = {"role": "agent", "office_id": "room2", "name": "agent-1"}
        smcp_namespace.get_session = MagicMock(side_effect=lambda sid: sess_comp if sid == comp_sid else sess_agent)
        smcp_namespace.call = MagicMock()
        with pytest.raises(SMCPNamespaceError, match="跨房间"):
            smcp_namespace.on_client_tool_call(
                "a-sid",
                {"agent": "agent-1", "req_id": "r", "computer": "c1", "tool_name": "t", "params": {}, "timeout": 5},
            )
        assert comp_sid not in smcp_namespace._inflight_disconnect_signals
        smcp_namespace.call.assert_not_called()

    def test_toctou_disconnect_before_registration_returns_404(self, guard_ns):
        """TOCTOU：解析后、登记前目标已断（``get_sid_by_name`` 复查转 None）→ 短路 404，不触发 ``self.call``（sync）."""
        ns, agent_sid, comp_name, comp_sid = guard_ns
        seq = [comp_sid, None]
        ns.get_sid_by_name = MagicMock(side_effect=lambda _name: seq.pop(0) if seq else None)
        ns.call = MagicMock(return_value={"ok": True})
        ret = ns.on_client_tool_call(agent_sid, self._tool_call_req(comp_name, timeout=5))
        assert ret["code"] == int(ErrorCode.NOT_FOUND)
        assert ret["details"]["computer_name"] == comp_name
        ns.call.assert_not_called()
        assert comp_sid not in ns._inflight_disconnect_signals

    def test_on_disconnect_calls_super_then_fires_signals(self, guard_ns, monkeypatch):
        """``on_disconnect`` 覆写：**先 ``super()``、再 fire 信号**（关闭 TOCTOU，#100 fix-review 🟡2；sync）.
        Asserts super runs before fire (the TOCTOU-closing order; sync mirror)."""
        ns, _agent_sid, _comp_name, comp_sid = guard_ns
        ev = ns._register_inflight_signal(comp_sid)
        seen_set_at_super: list[bool] = []

        def _super(_sid):
            seen_set_at_super.append(ev.is_set())  # super 运行时信号尚未 fire / not yet fired when super runs

        monkeypatch.setattr(SyncBaseNamespace, "on_disconnect", MagicMock(side_effect=_super))
        ns.on_disconnect(comp_sid)
        assert seen_set_at_super == [False]  # super 先于 fire / super ran before fire
        assert ev.is_set()  # fire 在 super 之后 / fire ran after super


#: 两类「向发起者所在房间广播」的 handler——都应「未入房即拒、绝不降级为全命名空间广播」。
#: Both families of "broadcast to the initiator's office" handlers must reject office-less
#: sessions outright instead of degrading to a namespace-wide broadcast.
UPDATE_HANDLERS = [
    ("on_server_update_config", UPDATE_CONFIG_NOTIFICATION),
    ("on_server_update_tool_list", UPDATE_TOOL_LIST_NOTIFICATION),
    ("on_server_update_desktop", UPDATE_DESKTOP_NOTIFICATION),
    ("on_server_update_skills", UPDATE_SKILLS_NOTIFICATION),
]


class TestServerBroadcastOfficeIsolationSync:
    """#212 附带（sync 镜像）：未入房不得把「房间广播」降级为「全命名空间广播」。

    ``emit(..., room=None)`` 的 socketio 语义是「广播给整个命名空间」——一旦发起者尚未
    入房（回房窗口内等），用 ``session.get("office_id")`` 取到的 ``None`` 就会把通知泄漏
    给所有 office 的成员。隔离不变量只允许显式 raise（#31 口径）。
    Cross-office broadcast isolation (sync mirror): ``room=None`` means namespace-wide in
    socketio, so an office-less initiator must be rejected explicitly.
    """

    @pytest.mark.parametrize(("handler", "expected_event"), UPDATE_HANDLERS)
    def test_update_handlers_require_office_membership(self, smcp_namespace, mock_server, handler, expected_event):
        """未入房的 Computer 上报 ``server:update_*`` → 显式 raise 且**不得产生任何广播**。"""
        smcp_namespace.server = mock_server
        smcp_namespace.get_session = MagicMock(return_value={"role": "computer", "name": "c1"})
        smcp_namespace.emit = MagicMock()
        with pytest.raises(SMCPNamespaceError, match="未加入任何房间"):
            getattr(smcp_namespace, handler)("c-sid", {"computer": "c1"})
        smcp_namespace.emit.assert_not_called()

    @pytest.mark.parametrize(("handler", "expected_event"), UPDATE_HANDLERS)
    def test_update_handlers_broadcast_only_to_own_office(self, smcp_namespace, mock_server, handler, expected_event):
        """正对照：已入房的上报仍只投递到自己的 office，且投递的是本 handler 对应的事件。

        English: positive control — an in-office reporter still reaches exactly its own room,
        emitting the event that belongs to *this* handler.
        """
        smcp_namespace.server = mock_server
        smcp_namespace.get_session = MagicMock(return_value={"role": "computer", "office_id": "room1", "name": "c1"})
        smcp_namespace.emit = MagicMock()
        getattr(smcp_namespace, handler)("c-sid", {"computer": "c1"})
        smcp_namespace.emit.assert_called_once()
        args, kwargs = smcp_namespace.emit.call_args
        assert args[0] == expected_event
        assert kwargs["room"] == "room1"
        assert kwargs["skip_sid"] == "c-sid"

    def test_leave_office_convergence_is_exhaustive_and_payload_independent(self, smcp_namespace):
        """收敛必须**遍历全部**非 sid 房，且载荷不参与目标选择（载荷只当过滤器会漏收敛）。

        载荷房刻意取一个**不在**成员关系里的值：任何「载荷当过滤器」或「只退第一个额外房」的
        欠收敛写法都会在此暴露（这两类写法在其余场景下全绿）。
        """
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "name": "a1"})
        smcp_namespace.rooms = MagicMock(return_value=["a-sid", "roomA", "roomB"])
        smcp_namespace.leave_room = MagicMock()
        ack = smcp_namespace.on_server_leave_office("a-sid", {"office_id": "roomC"})
        assert_empty_ack(ack, action="server:leave_office")
        assert {c.args for c in smcp_namespace.leave_room.call_args_list} == {("a-sid", "roomA"), ("a-sid", "roomB")}

    def test_leave_office_convergence_error_is_reported(self, smcp_namespace):
        """收敛分支自身的失败出口：``leave_room`` 抛错 → flat ErrorPayload(500)（笼统文案，原文只进日志）。"""
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "name": "a1"})
        smcp_namespace.rooms = MagicMock(return_value=["a-sid", "roomA"])
        smcp_namespace.leave_room = MagicMock(side_effect=RuntimeError("boom"))
        ack = smcp_namespace.on_server_leave_office("a-sid", {"office_id": "roomB"})

        assert_rejected_ack(ack, 500, action="server:leave_office")
        assert "boom" not in json.dumps(ack)

    def test_tool_call_cancel_requires_office_membership(self, smcp_namespace, mock_server):
        """未入房的 Agent 发 ``server:tool_call_cancel`` → 显式 raise 且**不得产生任何广播**。

        Agent 侧该 emit **没有** office 守卫：工具调用 ack 超时分支会直接发取消，此时若回房
        尚未落地，服务端 session 无 ``office_id`` ⇒ 载荷会泄漏给全部 office。rust 同事件已在
        缺省时直接拒绝（``handler.rs`` "session not in office"），本用例对齐该行为。
        """
        smcp_namespace.server = mock_server
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "name": "a1"})
        smcp_namespace.emit = MagicMock()
        with pytest.raises(SMCPNamespaceError, match="未加入任何房间"):
            smcp_namespace.on_server_tool_call_cancel("a-sid", {"agent": "a1", "req_id": "r1"})
        smcp_namespace.emit.assert_not_called()

    def test_tool_call_cancel_broadcasts_only_to_own_office(self, smcp_namespace, mock_server):
        """正对照：已入房的 Agent 发取消 → 只投递到自己的 office。"""
        smcp_namespace.server = mock_server
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "office_id": "room1", "name": "a1"})
        smcp_namespace.emit = MagicMock()
        smcp_namespace.on_server_tool_call_cancel("a-sid", {"agent": "a1", "req_id": "r1"})
        smcp_namespace.emit.assert_called_once()
        args, kwargs = smcp_namespace.emit.call_args
        assert args[0] == CANCEL_TOOL_CALL_NOTIFICATION
        assert kwargs["room"] == "room1"
        assert kwargs["skip_sid"] == "a-sid"

    # ── leave_office：广播目标只取服务端权威会话，绝不用客户端载荷 ──────────────

    def test_leave_office_ignores_client_supplied_room(self, smcp_namespace):
        """载荷声称的房间号不得作为广播目标：A 房成员不能向 B 房注入 ``notify:leave_office``。

        此前 ``leave_room(sid, data["office_id"])`` 直接采信客户端载荷，同一条事件既向
        任意房间广播、又把自身会话清理掉 ⇒ 跨房注入 + 会话与真实房间状态漂移。
        """
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "office_id": "roomA", "name": "a1"})
        smcp_namespace.leave_room = MagicMock()
        ack = smcp_namespace.on_server_leave_office("a-sid", {"office_id": "roomB"})
        assert_empty_ack(ack, action="server:leave_office")
        smcp_namespace.leave_room.assert_called_once_with("a-sid", "roomA")

    def test_leave_office_without_office_is_idempotent_noop(self, smcp_namespace):
        """未入房时退房是**幂等空操作**：既不得 raise，也绝不得回退到载荷里的房间号。

        载荷携带 ``office_id=None``：v0.5.0（#214）起它在 **schema 闸门**即被拒（400），
        故「回退到载荷里的 None 房间」⇒ ``room=None`` ⇒ 全命名空间广播泄漏这条注入路径，
        现在连业务代码都到不了。本用例同时钉住「拒绝时不得产生任何副作用」。
        """
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "name": "a1"})
        # 真实未入房的 socket 只在自己的 sid 房间里 / an office-less socket sits only in its own sid room
        smcp_namespace.rooms = MagicMock(return_value=["a-sid"])
        smcp_namespace.leave_room = MagicMock()
        ack = smcp_namespace.on_server_leave_office("a-sid", {"office_id": None})
        assert_rejected_ack(ack, 400, action="server:leave_office")
        smcp_namespace.leave_room.assert_not_called()

    def test_leave_office_without_office_ignores_truthy_payload_room(self, smcp_namespace):
        """会话无房时，载荷里的**真房间号**同样不得成为广播目标（兜底写法会复活跨房注入）。

        仅用 ``office_id=None`` 作载荷不足以钉死：一个 ``session.get("office_id") or
        data["office_id"]`` 式的「载荷兜底」实现能通过全部其它用例，却把跨房注入原样带回。
        A truthy payload room must not be used as a fallback target when the session has none.
        """
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "name": "a1"})
        smcp_namespace.rooms = MagicMock(return_value=["a-sid"])
        smcp_namespace.leave_room = MagicMock()
        ack = smcp_namespace.on_server_leave_office("a-sid", {"office_id": "roomB"})
        assert_empty_ack(ack, action="server:leave_office")
        smcp_namespace.leave_room.assert_not_called()

    def test_leave_office_without_session_office_converges_actual_rooms(self, smcp_namespace):
        """会话与真实成员关系**漂移**时（会话无 office_id 但 socket 仍在房），退房须收敛真实成员关系。

        漂移态来自旧版实现（采信载荷 ⇒ 清空会话却把人留在原房）。房间号只取会话之后，若
        无房分支直接早退，这类 socket 将永远留在房里——旧码反倒能把它逐出，属修复引入的行为收缩。
        收敛依据是服务端权威的 socketio 成员关系（非载荷），客户端无法借此向未加入的房间广播。
        """
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "name": "a1"})
        smcp_namespace.rooms = MagicMock(return_value=["a-sid", "roomA"])  # 含自身 sid 房间
        smcp_namespace.leave_room = MagicMock()
        ack = smcp_namespace.on_server_leave_office("a-sid", {"office_id": "roomA"})
        assert_empty_ack(ack, action="server:leave_office")
        smcp_namespace.leave_room.assert_called_once_with("a-sid", "roomA")
