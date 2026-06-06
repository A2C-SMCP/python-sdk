# -*- coding: utf-8 -*-
# filename: test_smcp.py
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
"""
Issue #21 验收测试：协议常量 + 数据类型 + 错误码枚举（v0.2.0）。
Acceptance tests for issue #21: protocol constants + data types + error code enum (v0.2.0).

协议来源 / Protocol source:
  https://github.com/A2C-SMCP/a2c-smcp-protocol v0.2.0
  - docs/specification/error-handling.md §4.5
  - docs/specification/data-structures.md
"""
from __future__ import annotations

import base64
import hashlib
from enum import IntEnum

import a2c_smcp
from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.smcp import (
    GET_BLOB_EVENT,
    GET_RESOURCES_EVENT,
    GET_SKILL_EVENT,
    GET_SKILLS_EVENT,
    UPDATE_SKILLS_EVENT,
    UPDATE_SKILLS_NOTIFICATION,
    A2CSkillRef,
    BlobHandle,
    ErrorCode,
    GetBlobReq,
    GetBlobRet,
    GetSkillReq,
    GetSkillRet,
    GetSkillsReq,
    GetSkillsRet,
    build_computer_not_found_error,
    is_protocol_error_payload,
)


class TestProtocolVersion:
    """协议版本常量导出 / Protocol version constant export."""

    def test_protocol_version_importable_from_package(self) -> None:
        """from a2c_smcp import PROTOCOL_VERSION 可用 / importable."""
        assert PROTOCOL_VERSION == "0.2.0"

    def test_protocol_version_attribute_on_module(self) -> None:
        """模块级属性可访问 / accessible as module attribute."""
        assert a2c_smcp.PROTOCOL_VERSION == "0.2.0"

    def test_protocol_version_independent_from_package_version(self) -> None:
        """协议版本与包版本独立 / protocol version is independent from package version."""
        assert a2c_smcp.__version__ != PROTOCOL_VERSION


class TestErrorCode:
    """ErrorCode 枚举与协议指南 §4.5 一致 / matches protocol guide §4.5."""

    def test_error_code_is_int_enum(self) -> None:
        assert issubclass(ErrorCode, IntEnum)

    def test_mcp_upstream_authorization_codes(self) -> None:
        """4006 / 4007 MCP 上游授权 / upstream authorization codes."""
        assert ErrorCode.TOOL_AUTHORIZATION_REQUIRED == 4006
        assert ErrorCode.TOOL_AUTHORIZATION_FAILED == 4007

    def test_protocol_version_mismatch_code(self) -> None:
        """4008 协议版本握手 / version handshake."""
        assert ErrorCode.PROTOCOL_VERSION_MISMATCH == 4008

    def test_mcp_routing_codes(self) -> None:
        """4014 / 4015 MCP Server 路由 / MCP Server routing codes."""
        assert ErrorCode.MCP_SERVER_NOT_FOUND == 4014
        assert ErrorCode.MCP_CAPABILITY_NOT_SUPPORTED == 4015

    def test_not_found_code(self) -> None:
        """404 通用资源不存在；client:* 路由层用于 Computer 名未命中（error-handling.md §20，#92）.
        404 generic not-found; used by the client:* router for an absent Computer name."""
        assert ErrorCode.NOT_FOUND == 404

    def test_build_computer_not_found_error_shape(self) -> None:
        """builder 产出 flat ErrorPayload(404)，且被 is_protocol_error_payload 识别（Agent 据此抛 SMCPProtocolError 而非超时，#92）.
        Builder yields a flat ErrorPayload(404) recognized by the predicate (so the Agent raises instead of timing out)."""
        err = build_computer_not_found_error("ghost-computer")
        assert err["code"] == int(ErrorCode.NOT_FOUND)
        assert err["details"]["computer_name"] == "ghost-computer"
        assert "ghost-computer" in err["message"]
        assert is_protocol_error_payload(err) is True

    def test_int_serialization(self) -> None:
        """IntEnum 可以直接当 int 使用 / IntEnum acts as int for serialization."""
        assert int(ErrorCode.PROTOCOL_VERSION_MISMATCH) == 4008
        assert ErrorCode.MCP_SERVER_NOT_FOUND + 1 == 4015


class TestEventConstants:
    """v0.2 新增事件常量字符串 / v0.2 added event constant strings."""

    def test_get_resources_event_string(self) -> None:
        assert GET_RESOURCES_EVENT == "client:get_resources"


class TestEventConstantsV021:
    """v0.2.1 新增事件常量字符串 / v0.2.1 added event constant strings.

    协议依据 / Protocol: a2c-smcp-protocol docs/specification/events.md
      §client:get_skills / §client:get_skill / §client:get_blob /
      §server:update_skills / §notify:update_skills
    """

    def test_get_skills_event_string(self) -> None:
        assert GET_SKILLS_EVENT == "client:get_skills"

    def test_get_skill_event_string(self) -> None:
        assert GET_SKILL_EVENT == "client:get_skill"

    def test_get_blob_event_string(self) -> None:
        assert GET_BLOB_EVENT == "client:get_blob"

    def test_update_skills_event_string(self) -> None:
        assert UPDATE_SKILLS_EVENT == "server:update_skills"

    def test_update_skills_notification_string(self) -> None:
        assert UPDATE_SKILLS_NOTIFICATION == "notify:update_skills"


class TestErrorCodeV021:
    """ErrorCode v0.2.1 加性扩展（4016/4017/4018）+ is_protocol_error_payload 一致性。
    v0.2.1 additive ErrorCode extension and predicate consistency."""

    def test_skill_name_invalid_code(self) -> None:
        assert ErrorCode.SKILL_NAME_INVALID == 4016

    def test_skill_resource_not_accessible_code(self) -> None:
        assert ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE == 4017

    def test_blob_not_accessible_code(self) -> None:
        assert ErrorCode.BLOB_NOT_ACCESSIBLE == 4018

    def test_is_protocol_error_payload_recognizes_new_codes(self) -> None:
        """新错误码自动并入 _ERROR_CODE_VALUES 集合 → is_protocol_error_payload 直接识别（无需 server/agent 二次维护）。
        New codes automatically join _ERROR_CODE_VALUES → recognized by predicate without extra wiring."""
        # 4016 — details.name
        assert is_protocol_error_payload({"code": 4016, "message": "x", "details": {"name": "bad!"}})
        # 4017 — details.reason / details.rel_path / details.total_size
        assert is_protocol_error_payload(
            {"code": 4017, "message": "x", "details": {"reason": "traversal", "rel_path": "../etc"}},
        )
        # 4018 — details.reason
        assert is_protocol_error_payload({"code": 4018, "message": "x", "details": {"reason": "gone"}})

    def test_4014_reuse_for_skill_name_absent_still_recognized(self) -> None:
        """SKILL name 合法但 Registry 未命中 → 4014 复用语义；predicate 仍识别为协议错误。
        SKILL name legal-but-absent reuses 4014; predicate still recognizes it as a protocol error."""
        assert is_protocol_error_payload({"code": 4014, "message": "skill not found"})


class TestTypedDictsV021:
    """v0.2.1 TypedDict 镜像层可构造性烟雾测试（字段对齐 data-structures.md）。
    Smoke tests confirming v0.2.1 TypedDict mirror layer is constructable per data-structures.md."""

    def test_a2c_skill_ref_required_field_set(self) -> None:
        """v0.2.2：A2CSkillRef **4 必选** = name/source/path/description（去 total=False → total=True + NotRequired[]）。
        ``__required_keys__`` 正式编码必选集（PEP 655），类型检查器可在编译期发现漏填（#50）。
        v0.2.2: 4 required fields encoded in ``__required_keys__`` so the type checker catches omissions."""
        assert A2CSkillRef.__required_keys__ == frozenset({"name", "source", "path", "description"})
        assert A2CSkillRef.__optional_keys__ == frozenset(
            {"uri", "license", "compatibility", "allowed_tools", "version", "skill_metadata"},
        )
        # 含全部 4 必选的最小 ref 合法 / minimal ref carrying all 4 required is valid
        ref: A2CSkillRef = {
            "name": "mcp:tfrobot-tools:code-review",
            "source": "mcp:tfrobot-tools",
            "path": "/tmp/skills/mcp/tfrobot-tools/code-review",
            "description": "Code review skill",
        }
        assert ref["name"] == "mcp:tfrobot-tools:code-review"
        assert ref["source"].startswith("mcp:")
        assert ref["path"].startswith("/")
        assert ref["description"] == "Code review skill"

    def test_a2c_skill_ref_full_with_frontmatter_fields(self) -> None:
        """完整 ref 含 SKILL.md frontmatter 派生字段 / Full ref includes frontmatter-derived fields."""
        ref: A2CSkillRef = {
            "name": "user:dev-tools:reviewer",
            "source": "user",
            "path": "/home/u/.a2c/skills/user/dev-tools/reviewer",
            "description": "Code reviewer skill",
            "license": "MIT",
            "compatibility": ">=0.2.1",
            "allowed_tools": ["read", "grep"],
            "version": "1.0.0",
            "skill_metadata": {"author": "alice"},
        }
        assert ref["allowed_tools"] == ["read", "grep"]
        assert ref["skill_metadata"]["author"] == "alice"

    def test_a2c_skill_ref_uri_only_for_mcp_source(self) -> None:
        """uri 仅 MCP 来源；其他来源不应携带（结构允许，语义由 Computer 端 staging 决定）。
        ``uri`` is MCP-only by semantics; structure permits, semantics enforced by Computer staging."""
        ref: A2CSkillRef = {
            "name": "mcp:tfrobot-tools:code-review",
            "source": "mcp:tfrobot-tools",
            "path": "/tmp/skills/mcp/tfrobot-tools/code-review",
            "description": "Code review skill",  # 必选 / required (v0.2.2)
            "uri": "skill://tfrobot-tools/code-review",
        }
        assert ref.get("uri", "").startswith("skill://")

    def test_get_skills_req_required_fields(self) -> None:
        """GetSkillsReq total=True：agent / req_id / computer 都是必选。
        total=True: agent / req_id / computer all required."""
        req: GetSkillsReq = {"agent": "ag1", "req_id": "r1", "computer": "c1"}
        assert req["computer"] == "c1"

    def test_get_skills_ret_skills_list(self) -> None:
        """GetSkillsRet 携带 skills 列表与 req_id；空清单（无任何 SKILL）亦合法。
        Holds skills list and req_id; empty list (no SKILLs) is valid."""
        ret: GetSkillsRet = {"skills": [], "req_id": "r1"}
        assert ret["skills"] == []

    def test_get_skill_req_default_rel_path_omitted(self) -> None:
        """rel_path 可省，缺省语义在 Computer 端解析为 SKILL.md。
        rel_path may be omitted; defaults to SKILL.md (server-side resolution)."""
        req: GetSkillReq = {"agent": "ag1", "req_id": "r1", "computer": "c1", "name": "user:x:y"}
        assert "rel_path" not in req

    def test_get_skill_req_with_rel_path(self) -> None:
        req: GetSkillReq = {
            "agent": "ag1",
            "req_id": "r1",
            "computer": "c1",
            "name": "user:x:y",
            "rel_path": "docs/intro.md",
        }
        assert req["rel_path"] == "docs/intro.md"

    def test_get_skill_ret_body_branch(self) -> None:
        """body 与 blob_handle 恰一存在 —— body 分支（文本且可内联）。
        Exactly one of body / blob_handle — body branch (inlineable text)."""
        ret: GetSkillRet = {
            "name": "user:x:y",
            "rel_path": "SKILL.md",
            "mime_type": "text/markdown",
            "total_size": 42,
            "sha256": "a" * 64,
            "body": "# hello",
            "req_id": "r1",
        }
        assert "blob_handle" not in ret
        assert ret["body"].startswith("#")

    def test_get_skill_ret_blob_handle_branch(self) -> None:
        """body 与 blob_handle 恰一存在 —— blob_handle 分支（二进制或过大文本）。
        blob_handle branch (binary or oversize text)."""
        ret: GetSkillRet = {
            "name": "user:x:y",
            "rel_path": "diagram.png",
            "mime_type": "image/png",
            "total_size": 100_000,
            "sha256": "b" * 64,
            "blob_handle": "opaque-handle-xyz",
            "req_id": "r1",
        }
        assert "body" not in ret
        assert ret["blob_handle"] == "opaque-handle-xyz"

    def test_get_blob_req_minimal(self) -> None:
        """chunk_offset / max_chunk_bytes 可省；缺省语义在 Computer 解析（offset=0, clamp）。
        chunk_offset / max_chunk_bytes optional; defaults resolved by Computer."""
        req: GetBlobReq = {
            "agent": "ag1",
            "req_id": "r1",
            "computer": "c1",
            "blob_handle": "opaque",
        }
        assert "chunk_offset" not in req
        assert "max_chunk_bytes" not in req

    def test_get_blob_req_with_offset(self) -> None:
        req: GetBlobReq = {
            "agent": "ag1",
            "req_id": "r1",
            "computer": "c1",
            "blob_handle": "opaque",
            "chunk_offset": 65536,
            "max_chunk_bytes": 262144,
        }
        assert req["chunk_offset"] == 65536

    def test_get_blob_ret_chunk_with_eof(self) -> None:
        """eof=True 时本块结束；spec 不变式 / spec invariant (blob-transfer.md §3):
            eof ⟺ chunk_offset + len(decode(blob)) == total_size
        且 / and: 重组 sha256 == 响应 sha256（Agent SHOULD 校验 / Agent SHOULD verify）.

        基于真实 round-trip 字节构造（base64 编码、长度等式、sha256 一致性同时成立）。
        Built from a real round-trip: base64 encoding, length equation, and sha256 consistency
        all hold simultaneously."""
        raw_bytes = b"hello, blob"
        ret: GetBlobRet = {
            "blob_handle": "opaque",
            "mime_type": "application/octet-stream",
            "total_size": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "chunk_offset": 0,
            "eof": True,
            "blob": base64.b64encode(raw_bytes).decode("ascii"),
            "req_id": "r1",
        }
        # eof 不变式 / eof invariant: chunk_offset + 本块解码字节数 == total_size
        decoded = base64.b64decode(ret["blob"])
        assert ret["eof"] is True
        assert ret["chunk_offset"] + len(decoded) == ret["total_size"]
        # 完整性校验 / Integrity check: 重组 sha256 == 响应 sha256
        assert hashlib.sha256(decoded).hexdigest() == ret["sha256"]

    def test_blob_handle_is_str_alias(self) -> None:
        """BlobHandle: TypeAlias = str —— 在结构上等同于 str。
        BlobHandle: TypeAlias = str — structurally equivalent to str."""
        h: BlobHandle = "any-opaque-string"
        assert isinstance(h, str)


class TestIsProtocolErrorPayload:
    """flat ErrorPayload 共享谓词 / shared flat-ErrorPayload predicate (server + agent)."""

    def test_true_for_known_protocol_error_codes(self) -> None:
        """顶层 code 属协议错误码 → True / top-level protocol code → True."""
        assert is_protocol_error_payload({"code": 4014, "message": "x", "mcp_server_name": "s"})
        assert is_protocol_error_payload({"code": 4015, "message": "x", "capability": "resources"})
        assert is_protocol_error_payload({"code": 4008, "message": "x"})
        # IntEnum 实例与 int 等值，亦应判定为 True / IntEnum value equals int
        assert is_protocol_error_payload({"code": ErrorCode.MCP_SERVER_NOT_FOUND})

    def test_false_for_success_payloads(self) -> None:
        """GetResourcesRet 类成功响应无 code → False / success payload without code."""
        assert not is_protocol_error_payload({"resources": [], "req_id": "r1"})
        assert not is_protocol_error_payload({"resources": [{"uri": "window://h/p"}], "next_cursor": "c2"})

    def test_false_for_non_protocol_or_missing_code(self) -> None:
        """非协议错误码 / 缺 code / code=None → False / non-protocol or missing code."""
        assert not is_protocol_error_payload({"code": 200})
        assert not is_protocol_error_payload({"code": 9999})
        assert not is_protocol_error_payload({"code": None})
        assert not is_protocol_error_payload({"message": "no code field"})

    def test_false_for_non_dict(self) -> None:
        """非 dict（无嵌套 envelope 不解包）→ False / non-dict inputs."""
        assert not is_protocol_error_payload(None)
        assert not is_protocol_error_payload("4014")
        assert not is_protocol_error_payload([{"code": 4014}])
        assert not is_protocol_error_payload(4014)
