"""
* 文件名: _request_builders
* 作者: JQQ
* 创建日期: 2026/5/19
* 最后修改日期: 2026/5/19
* 版权: 2023 JQQ. All rights reserved.
* 依赖: 无第三方依赖 / no third-party deps
* 描述: Agent 请求构造纯函数 + 事件合法性校验，由同步/异步 Base 客户端共享，
        消除 base.py 中 BaseAgentClient / BaseAgentSyncClient 的逐字重复。
        Pure request-builder functions + emit-event validation shared by the
        sync/async Agent base clients, de-duplicating the verbatim copies in
        base.py (BaseAgentClient / BaseAgentSyncClient).

注意 / Note:
    这些函数纯构造、无 I/O、无 async 差异；`get_agent_config()` 的调用仍由
    Base 客户端持有（保持既有语义），构造结果作为 `agent_config` 参数传入。
    `uuid.uuid4().hex` 的调用语义保持不变（每个请求一次）。
    These functions are pure constructors (no I/O, no async difference);
    `get_agent_config()` is still invoked by the Base client (preserving the
    existing semantics) and the result is passed in as `agent_config`.
    The `uuid.uuid4().hex` semantics are unchanged (one call per request).
"""

import uuid

from a2c_smcp.agent.types import AgentConfig
from a2c_smcp.smcp import GetDeskTopReq, GetResourcesReq, GetToolsReq, ToolCallReq


def build_tool_call_request(
    agent_config: AgentConfig,
    computer: str,
    tool_name: str,
    params: dict,
    timeout: int,
) -> ToolCallReq:
    """
    创建工具调用请求对象
    Create tool call request object

    Args:
        agent_config (AgentConfig): Agent 配置（来自 auth_provider.get_agent_config()）/
            Agent configuration (from auth_provider.get_agent_config())
        computer (str): 目标计算机ID / Target computer ID
        tool_name (str): 工具名称 / Tool name
        params (dict): 调用参数 / Call parameters
        timeout (int): 超时时间 / Timeout duration

    Returns:
        ToolCallReq: 工具调用请求 / Tool call request
    """
    return ToolCallReq(
        computer=computer,
        tool_name=tool_name,
        params=params,
        agent=agent_config["agent"],
        req_id=uuid.uuid4().hex,
        timeout=timeout,
    )


def build_get_tools_request(agent_config: AgentConfig, computer: str) -> GetToolsReq:
    """
    创建获取工具请求对象
    Create get tools request object

    Args:
        agent_config (AgentConfig): Agent 配置 / Agent configuration
        computer (str): 目标计算机ID / Target computer ID

    Returns:
        GetToolsReq: 获取工具请求 / Get tools request
    """
    return GetToolsReq(
        computer=computer,
        agent=agent_config["agent"],
        req_id=uuid.uuid4().hex,
    )


def build_get_resources_request(
    agent_config: AgentConfig,
    computer: str,
    mcp_server: str,
    cursor: str | None = None,
) -> GetResourcesReq:
    """
    创建获取资源请求对象（透明转发 MCP resources/list）
    Create get-resources request object (transparent forward of MCP resources/list)

    Args:
        agent_config (AgentConfig): Agent 配置 / Agent configuration
        computer (str): 目标计算机ID / Target computer ID
        mcp_server (str): 目标 MCP Server 名称 / Target MCP Server name
        cursor (str | None): MCP 标准翻页游标；首次传 None / MCP pagination cursor; None for first page

    Returns:
        GetResourcesReq: 获取资源请求 / Get resources request
    """
    req: GetResourcesReq = {
        "computer": computer,
        "mcp_server": mcp_server,
        "agent": agent_config["agent"],
        "req_id": uuid.uuid4().hex,
    }
    if cursor is not None:
        req["cursor"] = cursor
    return req


def build_get_desktop_request(
    agent_config: AgentConfig,
    computer: str,
    *,
    size: int | None = None,
    window: str | None = None,
) -> GetDeskTopReq:
    """
    创建获取桌面请求对象
    Create get desktop request object

    Args:
        agent_config (AgentConfig): Agent 配置 / Agent configuration
        computer (str): 目标计算机ID / Target computer ID
        size (int | None): 桌面窗口数量上限 / Max number of windows
        window (str | None): 指定窗口URI / Specific window URI

    Returns:
        GetDeskTopReq: 获取桌面请求 / Get desktop request
    """
    req: GetDeskTopReq = {
        "computer": computer,
        "agent": agent_config["agent"],
        "req_id": uuid.uuid4().hex,
    }
    if size is not None:
        req["desktop_size"] = size
    if window is not None:
        req["window"] = window
    return req


def validate_emit_event(event: str) -> None:
    """
    验证发送事件的合法性
    Validate the legality of emitted events

    Args:
        event (str): 事件名称 / Event name

    Raises:
        ValueError: 当事件不合法时 / When event is invalid
    """
    if event.startswith("notify:"):
        raise ValueError("AgentClient不允许使用notify:*事件 / AgentClient is not allowed to use notify:* events")
    if event.startswith("agent:"):
        raise ValueError("AgentClient不允许发起agent:*事件 / AgentClient is not allowed to initiate agent:* events")
