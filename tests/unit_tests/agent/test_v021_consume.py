# -*- coding: utf-8 -*-
# filename: test_v021_consume.py
# @Author  : JQQ
# @Software: PyCharm

"""
v0.2.1 Agent 消费侧单元测试 / Agent consume-side unit tests for v0.2.1.

覆盖 / Coverage:
  - SMCPProtocolError 暴露 details.reason / 4017/4018 字段
  - async + sync get_skills / get_skill (body 分支 + handle 分支自动 drain) / get_blob
  - notify:update_skills → 自动重拉 get_skills
  - tool_call binary post-process：_meta.a2c_blob_handle → drain_blob 还原 data/blob
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError, raise_for_error_payload
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import ErrorCode

# ── SMCPProtocolError ───────────────────────────────────────────────


class TestSMCPProtocolErrorV021:
    """v0.2.1 扩展：暴露 details.reason 与 4016/4017/4018 各 code-specific 字段."""

    def test_4017_exposes_details_reason_and_rel_path(self) -> None:
        payload = {
            "code": int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE),
            "message": "Skill resource not accessible",
            "details": {"reason": "traversal", "rel_path": "../etc"},
        }
        with pytest.raises(SMCPProtocolError) as ei:
            raise_for_error_payload(payload)
        assert ei.value.code == 4017
        assert ei.value.reason == "traversal"
        assert ei.value.details["rel_path"] == "../etc"

    def test_4018_exposes_details_reason(self) -> None:
        payload = {
            "code": int(ErrorCode.BLOB_NOT_ACCESSIBLE),
            "message": "Blob not accessible",
            "details": {"reason": "gone"},
        }
        with pytest.raises(SMCPProtocolError) as ei:
            raise_for_error_payload(payload)
        assert ei.value.code == 4018
        assert ei.value.reason == "gone"

    def test_4014_passthrough_keeps_mcp_server_name_top_level(self) -> None:
        """v0.2.1 兼容性：4014 仍保留顶层 mcp_server_name（v0.2 已有）.
        Backward-compat: 4014 keeps top-level mcp_server_name from v0.2."""
        payload = {"code": 4014, "message": "x", "mcp_server_name": "absent"}
        with pytest.raises(SMCPProtocolError) as ei:
            raise_for_error_payload(payload)
        assert ei.value.mcp_server_name == "absent"
        # details 容器为空（4014 无 details）/ details container empty for 4014
        assert ei.value.details == {}
        assert ei.value.reason is None


# ── async client get_skills / get_skill / get_blob ─────────────────


@pytest.fixture
def async_client() -> AsyncSMCPAgentClient:
    provider = DefaultAgentAuthProvider(agent_id="a", office_id="o")
    return AsyncSMCPAgentClient(auth_provider=provider)


class TestAsyncGetSkills:
    @pytest.mark.asyncio
    async def test_returns_typed_ret(self, async_client: AsyncSMCPAgentClient) -> None:
        with patch.object(async_client, "call", new=AsyncMock()) as mock_call:
            mock_call.return_value = {
                "skills": [{"name": "user:x:y", "source": "user", "path": "/s/x/y", "description": "demo skill"}],
                "req_id": "should-be-overridden",
            }
            # 用 monkeypatch + 真实 req_id 比对：覆写 mock 返回 req_id 与生成的一致
            # Patch req_id matching via inspecting call args
            real_call = async_client.call

            async def capture(event: str, req: dict, **kwargs: Any) -> dict:  # type: ignore[no-untyped-def]
                return {**mock_call.return_value, "req_id": req["req_id"]}

            mock_call.side_effect = capture
            ret = await async_client.get_skills("comp-1")
            assert ret["skills"] == [{"name": "user:x:y", "source": "user", "path": "/s/x/y", "description": "demo skill"}]
            _ = real_call

    @pytest.mark.asyncio
    async def test_4014_raises_protocol_error(self, async_client: AsyncSMCPAgentClient) -> None:
        with patch.object(async_client, "call", new=AsyncMock()) as mock_call:
            mock_call.return_value = {"code": 4014, "message": "x"}
            with pytest.raises(SMCPProtocolError) as ei:
                await async_client.get_skills("comp-1")
            assert ei.value.code == 4014


class TestAsyncGetSkill:
    @pytest.mark.asyncio
    async def test_body_branch_returns_inline(self, async_client: AsyncSMCPAgentClient) -> None:
        """文本可内联 → body 直接返回 / Inline text body returned as-is."""
        with patch.object(async_client, "call", new=AsyncMock()) as mock_call:
            async def fake(event: str, req: dict, **kwargs: Any) -> dict:  # type: ignore[no-untyped-def]
                return {
                    "name": "user:x:y",
                    "rel_path": "SKILL.md",
                    "mime_type": "text/markdown",
                    "total_size": 5,
                    "sha256": hashlib.sha256(b"# hi").hexdigest(),
                    "body": "# hi",
                    "req_id": req["req_id"],
                }

            mock_call.side_effect = fake
            ret = await async_client.get_skill("comp-1", "user:x:y")
            assert ret["body"] == "# hi"
            assert "blob_handle" not in ret

    @pytest.mark.asyncio
    async def test_blob_handle_branch_auto_drains_for_text_mime(self, async_client: AsyncSMCPAgentClient) -> None:
        """text/* MIME 的 blob_handle 自动 drain 回填 body / Auto-drain handle for text MIME → body."""
        payload = b"# large markdown body that overflows inline budget"
        sha = hashlib.sha256(payload).hexdigest()
        with (
            patch.object(async_client, "call", new=AsyncMock()) as mock_call,
            patch("a2c_smcp.agent.client.drain_blob", new=AsyncMock(return_value=(payload, "text/markdown"))) as mock_drain,
        ):
            async def fake(event: str, req: dict, **kwargs: Any) -> dict:  # type: ignore[no-untyped-def]
                return {
                    "name": "user:x:y",
                    "rel_path": "SKILL.md",
                    "mime_type": "text/markdown",
                    "total_size": len(payload),
                    "sha256": sha,
                    "blob_handle": "opaque-handle",
                    "req_id": req["req_id"],
                }

            mock_call.side_effect = fake
            ret = await async_client.get_skill("comp-1", "user:x:y")
            mock_drain.assert_awaited_once()
            assert ret["body"] == payload.decode("utf-8")
            assert "blob_handle" not in ret

    @pytest.mark.asyncio
    async def test_blob_handle_branch_keeps_handle_for_binary_mime(self, async_client: AsyncSMCPAgentClient) -> None:
        """二进制 MIME 的 blob_handle 保留供调用方自取字节 / Binary MIME keeps handle for caller-side fetch."""
        with patch.object(async_client, "call", new=AsyncMock()) as mock_call:
            async def fake(event: str, req: dict, **kwargs: Any) -> dict:  # type: ignore[no-untyped-def]
                return {
                    "name": "user:x:y",
                    "rel_path": "diagram.png",
                    "mime_type": "image/png",
                    "total_size": 100000,
                    "sha256": "b" * 64,
                    "blob_handle": "opaque-img",
                    "req_id": req["req_id"],
                }

            mock_call.side_effect = fake
            ret = await async_client.get_skill("comp-1", "user:x:y", rel_path="diagram.png")
            assert ret["blob_handle"] == "opaque-img"
            assert "body" not in ret

    @pytest.mark.asyncio
    async def test_4017_traversal_raises_protocol_error(self, async_client: AsyncSMCPAgentClient) -> None:
        with patch.object(async_client, "call", new=AsyncMock()) as mock_call:
            mock_call.return_value = {
                "code": 4017,
                "message": "Skill resource not accessible",
                "details": {"reason": "traversal", "rel_path": "../etc"},
            }
            with pytest.raises(SMCPProtocolError) as ei:
                await async_client.get_skill("comp-1", "user:x:y", rel_path="../etc")
            assert ei.value.code == 4017
            assert ei.value.reason == "traversal"


class TestAsyncGetBlobLowLevel:
    @pytest.mark.asyncio
    async def test_single_chunk_returns_typed_ret(self, async_client: AsyncSMCPAgentClient) -> None:
        with patch.object(async_client, "call", new=AsyncMock()) as mock_call:
            async def fake(event: str, req: dict, **kwargs: Any) -> dict:  # type: ignore[no-untyped-def]
                return {
                    "blob_handle": req["blob_handle"],
                    "mime_type": "image/png",
                    "total_size": 3,
                    "sha256": "c" * 64,
                    "chunk_offset": 0,
                    "eof": True,
                    "blob": "AAAA",
                    "req_id": req["req_id"],
                }

            mock_call.side_effect = fake
            ret = await async_client.get_blob("comp-1", "h")
            assert ret["eof"] is True
            assert ret["blob_handle"] == "h"

    @pytest.mark.asyncio
    async def test_4018_raises_protocol_error(self, async_client: AsyncSMCPAgentClient) -> None:
        with patch.object(async_client, "call", new=AsyncMock()) as mock_call:
            mock_call.return_value = {
                "code": 4018,
                "message": "Blob not accessible",
                "details": {"reason": "gone"},
            }
            with pytest.raises(SMCPProtocolError) as ei:
                await async_client.get_blob("comp-1", "h")
            assert ei.value.reason == "gone"


# ── notify:update_skills auto-refresh ──────────────────────────────


class TestSkillsUpdatedAutoRefresh:
    @pytest.mark.asyncio
    async def test_async_handler_calls_get_skills(self, async_client: AsyncSMCPAgentClient) -> None:
        """notify:update_skills → 自动调用 get_skills (auto-refresh pattern)."""
        with patch.object(async_client, "get_skills", new=AsyncMock(return_value={"skills": [], "req_id": "r"})) as mock_refresh:
            await async_client._on_skills_updated({"computer": "comp-1"})
            mock_refresh.assert_awaited_once_with("comp-1")

    @pytest.mark.asyncio
    async def test_async_handler_skips_missing_computer(self, async_client: AsyncSMCPAgentClient) -> None:
        """缺 computer 字段 → 警告并跳过 (不抛) / Missing ``computer`` → warn + skip (no raise)."""
        with patch.object(async_client, "get_skills", new=AsyncMock()) as mock_refresh:
            await async_client._on_skills_updated({})
            mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_async_dispatches_on_skills_received(self, async_client: AsyncSMCPAgentClient) -> None:
        """成功重拉后回调 on_skills_received，参数透传 / Hook fires with passthrough args (v0.2.1+)."""
        skills = [{"name": "user:x:y", "source": "user", "path": "/s/x/y", "description": "demo skill"}]
        hook = AsyncMock()
        async_client.event_handler = hook  # type: ignore[assignment]
        with patch.object(async_client, "get_skills", new=AsyncMock(return_value={"skills": skills, "req_id": "r"})):
            await async_client._on_skills_updated({"computer": "comp-1"})
            hook.on_skills_received.assert_awaited_once_with("comp-1", skills, async_client)

    @pytest.mark.asyncio
    async def test_async_skips_hook_when_event_handler_is_none(self, async_client: AsyncSMCPAgentClient) -> None:
        """event_handler is None：不抛 / silent skip when event_handler absent."""
        async_client.event_handler = None  # type: ignore[assignment]
        with patch.object(async_client, "get_skills", new=AsyncMock(return_value={"skills": [], "req_id": "r"})):
            await async_client._on_skills_updated({"computer": "comp-1"})  # must not raise

    @pytest.mark.asyncio
    async def test_async_skips_hook_when_handler_missing_method(self, async_client: AsyncSMCPAgentClient) -> None:
        """向后兼容：旧 handler 未实现 on_skills_received → hasattr 守卫静默跳过.
        Backward-compat: legacy handler missing the method → hasattr guard skips silently."""
        class _LegacyEH:
            async def on_tools_received(self, *a: Any, **k: Any) -> None: ...

        async_client.event_handler = _LegacyEH()  # type: ignore[assignment]
        with patch.object(async_client, "get_skills", new=AsyncMock(return_value={"skills": [], "req_id": "r"})):
            await async_client._on_skills_updated({"computer": "comp-1"})  # must not raise

    @pytest.mark.asyncio
    async def test_async_hook_exception_does_not_propagate(self, async_client: AsyncSMCPAgentClient) -> None:
        """hook 抛错被独立 try 捕获，不向上传播 / Hook errors isolated, not propagated."""
        hook = AsyncMock()
        hook.on_skills_received.side_effect = RuntimeError("hook boom")
        async_client.event_handler = hook  # type: ignore[assignment]
        with patch.object(async_client, "get_skills", new=AsyncMock(return_value={"skills": [], "req_id": "r"})):
            await async_client._on_skills_updated({"computer": "comp-1"})  # must not raise
            hook.on_skills_received.assert_awaited_once()


# ── tool_call binary post-process ───────────────────────────────────


class TestToolCallBinarySidebandRestore:
    """协议关键破坏性点：``_meta.a2c_blob_handle`` 自动 drain 还原；不实现 = 大二进制结果静默变空."""

    @pytest.mark.asyncio
    async def test_image_content_with_handle_drained_back_to_data(self, async_client: AsyncSMCPAgentClient) -> None:
        payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096  # fake PNG bytes
        sha = hashlib.sha256(payload).hexdigest()
        with patch("a2c_smcp.agent.client.drain_blob", new=AsyncMock(return_value=(payload, "image/png"))) as mock_drain:
            raw = {
                "content": [
                    {
                        "type": "image",
                        "data": "",  # cleared by Computer mint
                        "mimeType": "image/png",
                        "_meta": {
                            "a2c_blob_handle": "opaque",
                            "a2c_total_size": len(payload),
                            "a2c_sha256": sha,
                        },
                    },
                ],
                "isError": False,
            }
            out = await async_client._resolve_tool_call_binary_sideband(raw, "comp-1")
            mock_drain.assert_awaited_once()
            item = out["content"][0]
            # data 已被 drain 还原；_meta.a2c_* 字段已清除
            # data restored; _meta.a2c_* keys stripped
            assert base64.b64decode(item["data"]) == payload
            assert "_meta" not in item or "a2c_blob_handle" not in item.get("_meta", {})

    @pytest.mark.asyncio
    async def test_text_content_without_meta_untouched(self, async_client: AsyncSMCPAgentClient) -> None:
        """无 _meta.a2c_blob_handle 的 content item 原样保留 / Items without handle stay intact."""
        with patch("a2c_smcp.agent.client.drain_blob", new=AsyncMock()) as mock_drain:
            raw = {
                "content": [{"type": "text", "text": "no sideband here"}],
                "isError": False,
            }
            out = await async_client._resolve_tool_call_binary_sideband(raw, "comp-1")
            mock_drain.assert_not_awaited()
            assert out["content"][0]["text"] == "no sideband here"

    @pytest.mark.asyncio
    async def test_drain_failure_keeps_handle_with_warning(self, async_client: AsyncSMCPAgentClient) -> None:
        """drain 失败保留原 _meta.a2c_blob_handle（不吞噬整轮 tool_call）.
        On drain failure keep handle + warn (don't swallow entire tool_call)."""
        with patch("a2c_smcp.agent.client.drain_blob", new=AsyncMock(side_effect=RuntimeError("network fail"))):
            raw = {
                "content": [
                    {
                        "type": "image",
                        "data": "",
                        "mimeType": "image/png",
                        "_meta": {"a2c_blob_handle": "opaque"},
                    },
                ],
                "isError": False,
            }
            out = await async_client._resolve_tool_call_binary_sideband(raw, "comp-1")
            assert out["content"][0]["_meta"]["a2c_blob_handle"] == "opaque"

    @pytest.mark.asyncio
    async def test_embedded_resource_blob_restored(self, async_client: AsyncSMCPAgentClient) -> None:
        """EmbeddedResource.BlobResourceContents 路径：还原写入 resource.blob.
        EmbeddedResource.BlobResourceContents path: restored into ``resource.blob``."""
        payload = b"binary blob content" * 1024
        sha = hashlib.sha256(payload).hexdigest()
        with patch("a2c_smcp.agent.client.drain_blob", new=AsyncMock(return_value=(payload, "application/octet-stream"))):
            raw = {
                "content": [
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "embedded://x",
                            "blob": "",
                            "mimeType": "application/octet-stream",
                        },
                        "_meta": {
                            "a2c_blob_handle": "opaque-emb",
                            "a2c_total_size": len(payload),
                            "a2c_sha256": sha,
                        },
                    },
                ],
                "isError": False,
            }
            out = await async_client._resolve_tool_call_binary_sideband(raw, "comp-1")
            assert base64.b64decode(out["content"][0]["resource"]["blob"]) == payload


# ── sync mirror smoke ───────────────────────────────────────────────


class TestSyncMirror:
    """sync_client 镜像 smoke：方法存在 + skills/skill/blob 各有 happy path."""

    @pytest.fixture
    def sync_client(self) -> SMCPAgentClient:
        provider = DefaultAgentAuthProvider(agent_id="a", office_id="o")
        return SMCPAgentClient(auth_provider=provider)

    def test_sync_get_skills_relays(self, sync_client: SMCPAgentClient) -> None:
        with patch.object(sync_client, "call", new=MagicMock()) as mock_call:
            def fake(event: str, req: dict, **kwargs: Any) -> dict:  # type: ignore[no-untyped-def]
                return {"skills": [], "req_id": req["req_id"]}

            mock_call.side_effect = fake
            ret = sync_client.get_skills("comp-1")
            assert ret["skills"] == []

    def test_sync_get_skill_body_branch(self, sync_client: SMCPAgentClient) -> None:
        with patch.object(sync_client, "call", new=MagicMock()) as mock_call:
            def fake(event: str, req: dict, **kwargs: Any) -> dict:  # type: ignore[no-untyped-def]
                return {
                    "name": "user:x:y",
                    "rel_path": "SKILL.md",
                    "mime_type": "text/markdown",
                    "total_size": 4,
                    "sha256": hashlib.sha256(b"sync").hexdigest(),
                    "body": "sync",
                    "req_id": req["req_id"],
                }

            mock_call.side_effect = fake
            ret = sync_client.get_skill("comp-1", "user:x:y")
            assert ret["body"] == "sync"

    def test_sync_tool_call_binary_sideband_restore(self, sync_client: SMCPAgentClient) -> None:
        payload = b"sync binary payload" * 512
        sha = hashlib.sha256(payload).hexdigest()
        with patch("a2c_smcp.agent.sync_client.drain_blob_sync", new=MagicMock(return_value=(payload, "application/octet-stream"))):
            raw = {
                "content": [
                    {
                        "type": "image",
                        "data": "",
                        "mimeType": "image/png",
                        "_meta": {"a2c_blob_handle": "h", "a2c_total_size": len(payload), "a2c_sha256": sha},
                    },
                ],
                "isError": False,
            }
            out = sync_client._resolve_tool_call_binary_sideband(raw, "comp-1")
            assert base64.b64decode(out["content"][0]["data"]) == payload

    def test_sync_on_skills_updated_calls_get_skills(self, sync_client: SMCPAgentClient) -> None:
        with patch.object(sync_client, "get_skills", new=MagicMock(return_value={"skills": [], "req_id": "r"})) as mock_refresh:
            sync_client._on_skills_updated({"computer": "comp-1"})
            mock_refresh.assert_called_once_with("comp-1")

    def test_sync_dispatches_on_skills_received(self, sync_client: SMCPAgentClient) -> None:
        """sync 镜像：成功重拉后回调 on_skills_received（v0.2.1+）.
        Sync mirror: hook fires with passthrough args (v0.2.1+)."""
        skills = [{"name": "user:x:y", "source": "user", "path": "/s/x/y", "description": "demo skill"}]
        hook = MagicMock()
        sync_client.event_handler = hook  # type: ignore[assignment]
        with patch.object(sync_client, "get_skills", new=MagicMock(return_value={"skills": skills, "req_id": "r"})):
            sync_client._on_skills_updated({"computer": "comp-1"})
            hook.on_skills_received.assert_called_once_with("comp-1", skills, sync_client)

    def test_sync_skips_hook_when_event_handler_is_none(self, sync_client: SMCPAgentClient) -> None:
        """sync 镜像：event_handler is None → 静默跳过."""
        sync_client.event_handler = None  # type: ignore[assignment]
        with patch.object(sync_client, "get_skills", new=MagicMock(return_value={"skills": [], "req_id": "r"})):
            sync_client._on_skills_updated({"computer": "comp-1"})  # must not raise

    def test_sync_skips_hook_when_handler_missing_method(self, sync_client: SMCPAgentClient) -> None:
        """sync 镜像：向后兼容旧 handler 缺方法时 hasattr 守卫静默跳过."""
        class _LegacyEH:
            def on_tools_received(self, *a: Any, **k: Any) -> None: ...

        sync_client.event_handler = _LegacyEH()  # type: ignore[assignment]
        with patch.object(sync_client, "get_skills", new=MagicMock(return_value={"skills": [], "req_id": "r"})):
            sync_client._on_skills_updated({"computer": "comp-1"})  # must not raise

    def test_sync_hook_exception_does_not_propagate(self, sync_client: SMCPAgentClient) -> None:
        """sync 镜像：hook 抛错独立隔离不向上传播."""
        hook = MagicMock()
        hook.on_skills_received.side_effect = RuntimeError("hook boom")
        sync_client.event_handler = hook  # type: ignore[assignment]
        with patch.object(sync_client, "get_skills", new=MagicMock(return_value={"skills": [], "req_id": "r"})):
            sync_client._on_skills_updated({"computer": "comp-1"})  # must not raise
            hook.on_skills_received.assert_called_once()
