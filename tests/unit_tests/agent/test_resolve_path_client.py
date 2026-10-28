# -*- coding: utf-8 -*-
# filename: test_resolve_path_client.py
# @Author  : JQQ
# @Software: PyCharm
"""
Agent 侧 ``resolve_path()`` 公共方法单测（sync + async，v0.5.0 #236 / protocol#65）.
Unit tests for the Agent-side ``resolve_path()`` public method (sync + async).

协议依据 / Protocol: events.md §client:resolve_path；error-handling.md §4020.

测试意图 / Test intentions:
- 发出的事件名 = ``client:resolve_path``，载荷含 ``computer`` / ``path`` / ``agent`` / ``req_id``
- 成功响应原样返回 ``ResolvePathRet``（含 ``req_id`` 回显校验）
- ``4020`` flat ErrorPayload → :class:`SMCPProtocolError`（``.code`` / ``.reason`` 可分流）
- ``req_id`` 不匹配 → ``ValueError``（防串包）

异步与同步实现必须同构（CLAUDE.md §同步/异步一致性）.
"""

from __future__ import annotations

from typing import Any

import pytest

from a2c_smcp.agent.auth import DefaultAgentAuthProvider
from a2c_smcp.agent.client import AsyncSMCPAgentClient
from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.agent.sync_client import SMCPAgentClient
from a2c_smcp.smcp import RESOLVE_PATH_EVENT


def _auth() -> DefaultAgentAuthProvider:
    return DefaultAgentAuthProvider(agent_id="agent-1", office_id="office-1")


def _ok_payload(req: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_path": "/srv/docs/a.txt",
        "blob_handle": "opaque-handle",
        "mime_type": "text/plain",
        "total_size": 11,
        "sha256": "a" * 64,
        "mtime": 1_700_000_000_123,
        "req_id": req["req_id"],
    }


def test_sync_resolve_path_emits_event_and_returns_ret() -> None:
    client = SMCPAgentClient(auth_provider=_auth())
    seen: dict[str, Any] = {}

    def fake_call(event: str, data: Any = None, namespace: str | None = None, **kwargs: Any) -> dict[str, Any]:
        seen["event"] = event
        seen["data"] = dict(data or {})
        return _ok_payload(seen["data"])

    client.call = fake_call  # type: ignore[method-assign]

    ret = client.resolve_path("comp-1", "/srv/docs/a.txt")

    assert seen["event"] == RESOLVE_PATH_EVENT
    assert seen["data"]["computer"] == "comp-1"
    assert seen["data"]["path"] == "/srv/docs/a.txt"
    assert seen["data"]["agent"] == "agent-1"
    assert seen["data"]["req_id"]
    assert ret["canonical_path"] == "/srv/docs/a.txt"
    assert ret["req_id"] == seen["data"]["req_id"]


def test_sync_resolve_path_raises_on_4020() -> None:
    client = SMCPAgentClient(auth_provider=_auth())
    client.call = lambda *a, **kw: {  # type: ignore[method-assign]
        "code": 4020,
        "message": "Path not accessible",
        "details": {"reason": "forbidden"},
    }

    with pytest.raises(SMCPProtocolError) as exc:
        client.resolve_path("comp-1", "/etc/shadow")

    assert exc.value.code == 4020
    assert exc.value.reason == "forbidden"


def test_sync_resolve_path_rejects_mismatched_req_id() -> None:
    client = SMCPAgentClient(auth_provider=_auth())
    client.call = lambda *a, **kw: {**_ok_payload({"req_id": "other"}), "req_id": "other"}  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="mismatched req_id"):
        client.resolve_path("comp-1", "/srv/docs/a.txt")


@pytest.mark.asyncio
async def test_async_resolve_path_emits_event_and_returns_ret() -> None:
    client = AsyncSMCPAgentClient(auth_provider=_auth())
    seen: dict[str, Any] = {}

    async def fake_call(event: str, data: Any = None, namespace: str | None = None, **kwargs: Any) -> dict[str, Any]:
        seen["event"] = event
        seen["data"] = dict(data or {})
        return _ok_payload(seen["data"])

    client.call = fake_call  # type: ignore[method-assign]

    ret = await client.resolve_path("comp-1", "/srv/docs/a.txt")

    assert seen["event"] == RESOLVE_PATH_EVENT
    assert seen["data"]["computer"] == "comp-1"
    assert ret["blob_handle"] == "opaque-handle"
    assert ret["req_id"] == seen["data"]["req_id"]


@pytest.mark.asyncio
async def test_async_resolve_path_raises_on_4020() -> None:
    client = AsyncSMCPAgentClient(auth_provider=_auth())

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"code": 4020, "message": "Path not accessible", "details": {"reason": "not_found"}}

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(SMCPProtocolError) as exc:
        await client.resolve_path("comp-1", "/srv/docs/missing.txt")

    assert exc.value.code == 4020
    assert exc.value.reason == "not_found"
