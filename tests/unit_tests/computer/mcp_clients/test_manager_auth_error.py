# -*- coding: utf-8 -*-
# filename: test_manager_auth_error.py
# @Time    : 2026/07/18
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
生产路径断言：Manager 工具调用因上游授权失败时 surface 4006/4007，``meta.mcp_server`` == **路由所用 bundle_id**（#133）。

Production-path assertions: the manager surfaces 4006/4007 on upstream auth failure with
``meta.mcp_server`` == the routing bundle_id (#133, protocol error-handling.md §4006/4007).

反致盲：夹具 ``bundle_id="gh-bundle"`` 与 display ``name="github"``、``tool_name="query"`` **三者互异**——
若代码误写 ``mcp_server=name`` 或 ``=tool_name``，断言即红（字面量夹具/同值夹具证不了生产路径正确，见 #150）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp import StdioServerParameters

from a2c_smcp.computer.mcp_clients.auth_error import META_ERROR_CODE_KEY, META_MCP_SERVER_KEY
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig
from a2c_smcp.smcp import ErrorCode

_BUNDLE_ID = "gh-bundle"
_NAME = "github"
_TOOL = "query"


def _http_status_error(status: int, *, detail: str = "boom") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.com/mcp")
    return httpx.HTTPStatusError(detail, request=req, response=httpx.Response(status, request=req, text=detail))


def _manager_with_failing_client(exc: BaseException) -> MCPServerManager:
    """装配一个 manager：bundle_id=gh-bundle 的活跃 client 的 call_tool 抛 ``exc``。"""
    manager = MCPServerManager()
    cfg = StdioServerConfig(name=_NAME, bundle_id=_BUNDLE_ID, server_parameters=StdioServerParameters(command="node"))
    manager._servers_config[_BUNDLE_ID] = cfg
    manager._exposed_tools[f"{_BUNDLE_ID}__{_TOOL}"] = (_BUNDLE_ID, _TOOL)
    client = MagicMock()
    client.call_tool = AsyncMock(side_effect=exc)
    manager._active_clients[_BUNDLE_ID] = client
    return manager


# ── acall_tool（已知 bundle_id 直调） ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_acall_tool_surfaces_4006_on_401_with_routing_bundle_id() -> None:
    """上游 401 → 返回 isError 结果，meta.error_code=4006，meta.mcp_server == 路由 bundle_id（非 name/tool_name）。"""
    manager = _manager_with_failing_client(_http_status_error(401))
    result = await manager.acall_tool(_BUNDLE_ID, _TOOL, {})
    assert result.isError is True
    assert result.meta[META_ERROR_CODE_KEY] == ErrorCode.TOOL_AUTHORIZATION_REQUIRED
    assert result.meta[META_MCP_SERVER_KEY] == _BUNDLE_ID
    assert result.meta[META_MCP_SERVER_KEY] not in (_NAME, _TOOL), "mcp_server 必须是 bundle_id，非 display name / tool_name"


@pytest.mark.asyncio
async def test_acall_tool_surfaces_4007_on_403() -> None:
    """上游 403 → meta.error_code=4007，mcp_server == bundle_id。"""
    manager = _manager_with_failing_client(_http_status_error(403))
    result = await manager.acall_tool(_BUNDLE_ID, _TOOL, {})
    assert result.isError is True
    assert result.meta[META_ERROR_CODE_KEY] == ErrorCode.TOOL_AUTHORIZATION_FAILED
    assert result.meta[META_MCP_SERVER_KEY] == _BUNDLE_ID


# ── aexecute_tool（exposed_tool_name 经 ExposedToolMapping 解析 bundle_id） ────
@pytest.mark.asyncio
async def test_aexecute_tool_routes_bundle_id_into_meta() -> None:
    """exposed_tool_name → 解析出 bundle_id → 授权失败 → meta.mcp_server == 解析后的 bundle_id（证明路由身份流入 meta）。"""
    manager = _manager_with_failing_client(_http_status_error(401))
    result = await manager.aexecute_tool(f"{_BUNDLE_ID}__{_TOOL}", {})
    assert result.isError is True
    assert result.meta[META_MCP_SERVER_KEY] == _BUNDLE_ID


# ── 安全边界：不外泄异常原文 ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_auth_error_result_does_not_leak_exception_text() -> None:
    """异常原文（可能含 token/URL）不得出现在授权错误结果任何字段（§auth_hint 安全边界）。"""
    secret = "Bearer-super-secret-token-xyz"
    manager = _manager_with_failing_client(_http_status_error(401, detail=secret))
    result = await manager.acall_tool(_BUNDLE_ID, _TOOL, {})
    import json

    assert secret not in json.dumps(result.model_dump(mode="json"), ensure_ascii=False)


# ── 非授权失败：无回归（仍抛 RuntimeError，不误判为授权） ─────────────────────
@pytest.mark.asyncio
async def test_non_auth_failure_still_raises_runtime_error() -> None:
    """普通工具异常不属授权语义 → acall_tool 仍抛 RuntimeError（保持既有行为，不误产 4006/4007）。"""
    manager = _manager_with_failing_client(ValueError("tool blew up"))
    with pytest.raises(RuntimeError, match="Tool execution failed"):
        await manager.acall_tool(_BUNDLE_ID, _TOOL, {})
