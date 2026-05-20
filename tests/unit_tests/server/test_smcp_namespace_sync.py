"""
* 文件名: test_smcp_namespace_sync
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: pytest, socketio
* 描述: 同步版 SMCP Namespace 测试用例 / Sync SMCP Namespace test cases
"""

from unittest.mock import MagicMock

import pytest

from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.server import (
    DefaultSyncAuthenticationProvider,
    SyncAuthenticationProvider,
    SyncSMCPNamespace,
)
from a2c_smcp.smcp import (
    GET_BLOB_EVENT,
    GET_SKILL_EVENT,
    GET_SKILLS_EVENT,
    SMCP_NAMESPACE,
    UPDATE_SKILLS_NOTIFICATION,
    EnterOfficeReq,
    ErrorCode,
    LeaveOfficeReq,
)


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

        success, error = smcp_namespace.on_server_join_office("test_sid", data)

        assert success is True
        assert error is None
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

        success, error = smcp_namespace.on_server_join_office("test_sid", data)

        assert success is False
        assert "Role mismatch" in error

    def test_leave_office(self, smcp_namespace):
        smcp_namespace.leave_room = MagicMock()

        data = LeaveOfficeReq(**{"office_id": "office_123"})

        success, error = smcp_namespace.on_server_leave_office("test_sid", data)

        assert success is True
        assert error is None
        smcp_namespace.leave_room.assert_called_once_with("test_sid", "office_123")


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
        ns.call.return_value = {"skills": [{"name": "user:x:y", "source": "user", "path": "/s/x/y"}], "req_id": "r1"}
        ret = ns.on_client_get_skills(agent_sid, {"agent": "agent-1", "req_id": "r1", "computer": comp_name})
        ns.call.assert_called_once()
        args, kwargs = ns.call.call_args
        assert args[0] == GET_SKILLS_EVENT
        assert kwargs["to"] == comp_sid
        assert kwargs["namespace"] == SMCP_NAMESPACE
        # 语义级断言：内容完整透传 / Semantic-level assertion: contents passed through
        assert ret["skills"] == [{"name": "user:x:y", "source": "user", "path": "/s/x/y"}]
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

    def test_get_blob_computer_not_found_raises_value_error(self, smcp_namespace, mock_server):
        smcp_namespace.server = mock_server
        smcp_namespace._name_to_sid_map = {}
        smcp_namespace.get_session = MagicMock(return_value={"role": "agent", "office_id": "room1"})
        with pytest.raises(ValueError, match="not found"):
            smcp_namespace.on_client_get_blob(
                "a-sid",
                {"agent": "agent-1", "req_id": "r", "computer": "absent", "blob_handle": "h"},
            )

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
