"""
* 文件名: base
* 作者: JQQ
* 创建日期: 2025/9/30
* 最后修改日期: 2025/10/8
* 版权: 2023 JQQ. All rights reserved.
* 依赖: socketio, loguru
* 描述: Agent基础客户端抽象类（异步和同步版本）/ Agent base client abstract classes (async and sync versions)
"""

from abc import ABC, abstractmethod
from typing import Any, cast

from mcp.types import CallToolResult, TextContent

from a2c_smcp.agent import _request_builders as _rb
from a2c_smcp.agent.auth import AgentAuthProvider
from a2c_smcp.agent.types import AgentEventHandler, AsyncAgentEventHandler
from a2c_smcp.smcp import (
    JOIN_OFFICE_EVENT,
    LEAVE_OFFICE_EVENT,
    EnterOfficeNotification,
    EnterOfficeReq,
    GetBlobReq,
    GetDeskTopReq,
    GetDeskTopRet,
    GetResourcesReq,
    GetSkillReq,
    GetSkillsReq,
    GetToolsReq,
    GetToolsRet,
    LeaveOfficeNotification,
    LeaveOfficeReq,
    ToolCallReq,
    UpdateMCPConfigNotification,
)
from a2c_smcp.utils.logger import get_logger

logger = get_logger("agent")


class BaseAgentClient(ABC):
    """
    Agent异步基础客户端抽象类，提供通用的SMCP协议处理逻辑（异步版本）
    Agent async base client abstract class, provides common SMCP protocol handling logic (async version)
    """

    def __init__(
        self,
        auth_provider: AgentAuthProvider,
        event_handler: AsyncAgentEventHandler | None = None,
    ) -> None:
        """
        初始化异步基础Agent客户端
        Initialize async base Agent client

        Args:
            auth_provider (AgentAuthProvider): 认证提供者 / Authentication provider
            event_handler (AsyncAgentEventHandler | None): 异步事件处理器 / Async event handler
        """
        self.auth_provider = auth_provider
        self.event_handler = event_handler

    @abstractmethod
    async def emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        """
        异步发送事件的抽象方法
        Abstract method for async sending events
        """
        pass

    @abstractmethod
    async def call(self, event: str, data: Any = None, namespace: str | None = None, timeout: int = 60) -> Any:
        """
        异步调用事件并等待响应的抽象方法
        Abstract method for async calling events and waiting for response
        """
        pass

    def validate_emit_event(self, event: str) -> None:
        """
        验证发送事件的合法性
        Validate the legality of emitted events

        Args:
            event (str): 事件名称 / Event name

        Raises:
            ValueError: 当事件不合法时 / When event is invalid
        """
        _rb.validate_emit_event(event)

    def create_tool_call_request(self, computer: str, tool_name: str, params: dict, timeout: int) -> ToolCallReq:
        """
        创建工具调用请求对象
        Create tool call request object

        Args:
            computer (str): 目标计算机ID / Target computer ID
            tool_name (str): 工具名称 / Tool name
            params (dict): 调用参数 / Call parameters
            timeout (int): 超时时间 / Timeout duration

        Returns:
            ToolCallReq: 工具调用请求 / Tool call request
        """
        return _rb.build_tool_call_request(self.auth_provider.get_agent_config(), computer, tool_name, params, timeout)

    def create_get_tools_request(self, computer: str) -> GetToolsReq:
        """
        创建获取工具请求对象
        Create get tools request object

        Args:
            computer (str): 目标计算机ID / Target computer ID

        Returns:
            GetToolsReq: 获取工具请求 / Get tools request
        """
        return _rb.build_get_tools_request(self.auth_provider.get_agent_config(), computer)

    def create_get_resources_request(self, computer: str, mcp_server: str, cursor: str | None = None) -> GetResourcesReq:
        """
        创建获取资源请求对象（透明转发 MCP resources/list）
        Create get-resources request object (transparent forward of MCP resources/list)

        Args:
            computer (str): 目标计算机ID / Target computer ID
            mcp_server (str): 目标 MCP Server 名称 / Target MCP Server name
            cursor (str | None): MCP 标准翻页游标；首次传 None / MCP pagination cursor; None for first page

        Returns:
            GetResourcesReq: 获取资源请求 / Get resources request
        """
        return _rb.build_get_resources_request(self.auth_provider.get_agent_config(), computer, mcp_server, cursor)

    def create_get_desktop_request(self, computer: str, *, size: int | None = None, window: str | None = None) -> GetDeskTopReq:
        """
        创建获取桌面请求对象
        Create get desktop request object

        Args:
            computer (str): 目标计算机ID / Target computer ID
            size (int | None): 桌面窗口数量上限 / Max number of windows
            window (str | None): 指定窗口URI / Specific window URI

        Returns:
            GetDeskTopReq: 获取桌面请求 / Get desktop request
        """
        return _rb.build_get_desktop_request(self.auth_provider.get_agent_config(), computer, size=size, window=window)

    def create_get_skills_request(self, computer: str) -> GetSkillsReq:
        """创建获取 SKILL 清单请求对象 / Create get-skills request object (v0.2.1).

        协议依据 / Protocol: a2c-smcp-protocol events.md §client:get_skills.
        """
        return _rb.build_get_skills_request(self.auth_provider.get_agent_config(), computer)

    def create_get_skill_request(self, computer: str, name: str, rel_path: str | None = None) -> GetSkillReq:
        """创建获取 SKILL 包内单个资源请求对象 / Create get-skill request object (v0.2.1).

        协议依据 / Protocol: events.md §client:get_skill. ``rel_path`` 缺省取 ``SKILL.md`` 入口.
        """
        return _rb.build_get_skill_request(self.auth_provider.get_agent_config(), computer, name, rel_path)

    def create_get_blob_request(
        self,
        computer: str,
        blob_handle: str,
        chunk_offset: int | None = None,
        max_chunk_bytes: int | None = None,
    ) -> GetBlobReq:
        """创建通用二进制拉取单块请求对象 / Create get-blob chunk request object (v0.2.1).

        协议依据 / Protocol: events.md §client:get_blob；blob-transfer.md.
        """
        return _rb.build_get_blob_request(
            self.auth_provider.get_agent_config(),
            computer,
            blob_handle,
            chunk_offset,
            max_chunk_bytes,
        )

    def process_desktop_response(self, response: GetDeskTopRet, computer: str) -> None:
        """
        处理桌面响应（默认仅记录日志；留作后续扩展回调）。
        Process desktop response (log only by default; placeholder for future callbacks).
        """
        try:
            desktops = response.get("desktops", []) if isinstance(response, dict) else []
            logger.info(f"Received desktop from computer {computer}, windows={len(desktops)}")
        except Exception as e:
            logger.error(f"Error processing desktop response: {e}", exc_info=True)

    def handle_tool_call_timeout(self, req_id: str) -> CallToolResult:
        """
        处理工具调用超时情况
        Handle tool call timeout situation

        Args:
            req_id (str): 请求ID / Request ID

        Returns:
            CallToolResult: 超时错误结果 / Timeout error result
        """
        result = CallToolResult(
            content=[TextContent(text=f"工具调用超时 / Tool call timeout, req_id={req_id}", type="text")],
            isError=True,
        )
        # 协议结果级标记（32eea98 / protocol#5）：标识此结果为超时返回，使 Agent 能区分「超时 / 取消 / 普通失败」。
        # 经 ``.meta`` 写入真实 meta 字段（出线默认 dump→key=meta）；构造器传 ``meta=`` 会落 extra（字段 alias 为 ``_meta``）。
        # Protocol result-level marker (32eea98 / protocol#5): mark this result as a timeout return so the Agent can
        # distinguish timeout from cancellation / ordinary failure. Set via ``.meta`` (real field, wire key ``meta``);
        # passing ``meta=`` to the ctor would land in ``extra`` since the field alias is ``_meta``.
        result.meta = {"a2c_timeout": True}
        return result

    def validate_office_data(self, data: EnterOfficeNotification | LeaveOfficeNotification) -> str:
        """
        验证办公室数据并返回计算机ID
        Validate office data and return computer ID

        Args:
            data: 办公室通知数据 / Office notification data

        Returns:
            str: 计算机ID / Computer ID

        Raises:
            AssertionError: 当数据无效时 / When data is invalid
        """
        agent_config = self.auth_provider.get_agent_config()
        assert data["office_id"] == agent_config["office_id"], "无效的办公室ID / Invalid office ID"
        assert data.get("computer"), "无效的计算机ID / Invalid computer ID"
        return cast(str, data["computer"])

    async def handle_computer_enter_office(self, data: EnterOfficeNotification) -> None:
        """
        异步处理Computer加入办公室事件
        Async handle Computer enter office event

        Args:
            data: 加入办公室通知数据 / Enter office notification data
        """
        try:
            computer = self.validate_office_data(data)
            logger.info(f"Computer {computer} entered office {data['office_id']}")

            # 调用异步事件处理器（强制携带 client 引用）
            # Call async event handler (force passing client reference)
            if self.event_handler:
                await self.event_handler.on_computer_enter_office(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer enter office: {e}", exc_info=True)

    async def handle_computer_leave_office(self, data: LeaveOfficeNotification) -> None:
        """
        异步处理Computer离开办公室事件
        Async handle Computer leave office event

        Args:
            data: 离开办公室通知数据 / Leave office notification data
        """
        try:
            computer = self.validate_office_data(data)
            logger.info(f"Computer {computer} left office {data['office_id']}")

            # 调用异步事件处理器（强制携带 client 引用）
            # Call async event handler (force passing client reference)
            if self.event_handler:
                await self.event_handler.on_computer_leave_office(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer leave office: {e}", exc_info=True)

    async def handle_computer_update_config(self, data: UpdateMCPConfigNotification) -> None:
        """
        异步处理Computer更新配置事件
        Async handle Computer update config event

        Args:
            data: 配置更新通知数据 / Config update notification data
        """
        try:
            computer = data["computer"]
            logger.info(f"Computer {computer} updated config")

            # 调用异步事件处理器（强制携带 client 引用）
            # Call async event handler (force passing client reference)
            if self.event_handler:
                await self.event_handler.on_computer_update_config(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer update config: {e}", exc_info=True)

    async def process_tools_response(self, response: GetToolsRet, computer: str) -> None:
        """
        异步处理工具响应
        Async process tools response

        Args:
            response: 工具响应数据 / Tools response data
            computer: 计算机ID / Computer ID
        """
        try:
            if tools := response.get("tools"):
                logger.info(f"Received {len(tools)} tools from computer {computer}")

                # 调用异步事件处理器（强制携带 client 引用）
                # Call async event handler (force passing client reference)
                if self.event_handler:
                    await self.event_handler.on_tools_received(computer, tools, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error processing tools response: {e}", exc_info=True)

    async def join_office(self, office_id: str, agent_name: str, namespace: str | None = None) -> None:
        """
        加入一个Office（Socket.IO中的Room）
        Join an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID，在A2C-smcp协议中，OfficeID即为Socket.IO RoomID
                            / Room ID, in A2C-smcp protocol, OfficeID is the Socket.IO RoomID
            agent_name (str): Agent名称，提供给前端展示用
                            / Agent name, for frontend display
            namespace (str | None): 命名空间 / Namespace
        """
        await self.emit(
            JOIN_OFFICE_EVENT,
            EnterOfficeReq(office_id=office_id, role="agent", name=agent_name),
            namespace=namespace,
        )

    async def leave_office(self, office_id: str, namespace: str | None = None) -> None:
        """
        离开一个Office（Socket.IO中的Room）
        Leave an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID / Room ID
            namespace (str | None): 命名空间 / Namespace
        """
        await self.emit(LEAVE_OFFICE_EVENT, LeaveOfficeReq(office_id=office_id), namespace=namespace)

    @abstractmethod
    def register_event_handlers(self) -> None:
        """
        注册事件处理器，子类需要实现具体的注册逻辑
        Register event handlers, subclasses need to implement specific registration logic
        """
        # 这个方法需要在子类中实现具体的事件注册逻辑
        # This method needs to implement specific event registration logic in subclasses
        pass


class BaseAgentSyncClient(ABC):
    """
    Agent同步基础客户端抽象类，提供通用的SMCP协议处理逻辑（同步版本）
    Agent sync base client abstract class, provides common SMCP protocol handling logic (sync version)
    """

    def __init__(
        self,
        auth_provider: AgentAuthProvider,
        event_handler: AgentEventHandler | None = None,
    ) -> None:
        """
        初始化同步基础Agent客户端
        Initialize sync base Agent client

        Args:
            auth_provider (AgentAuthProvider): 认证提供者 / Authentication provider
            event_handler (AgentEventHandler | None): 同步事件处理器 / Sync event handler
        """
        self.auth_provider = auth_provider
        self.event_handler = event_handler

    @abstractmethod
    def emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
        """
        发送事件的抽象方法
        Abstract method for sending events
        """
        pass

    @abstractmethod
    def call(self, event: str, data: Any = None, namespace: str | None = None, timeout: int = 60) -> Any:
        """
        调用事件并等待响应的抽象方法
        Abstract method for calling events and waiting for response
        """
        pass

    def validate_emit_event(self, event: str) -> None:
        """
        验证发送事件的合法性
        Validate the legality of emitted events

        Args:
            event (str): 事件名称 / Event name

        Raises:
            ValueError: 当事件不合法时 / When event is invalid
        """
        _rb.validate_emit_event(event)

    def create_tool_call_request(self, computer: str, tool_name: str, params: dict, timeout: int) -> ToolCallReq:
        """
        创建工具调用请求对象
        Create tool call request object

        Args:
            computer (str): 目标计算机ID / Target computer ID
            tool_name (str): 工具名称 / Tool name
            params (dict): 调用参数 / Call parameters
            timeout (int): 超时时间 / Timeout duration

        Returns:
            ToolCallReq: 工具调用请求 / Tool call request
        """
        return _rb.build_tool_call_request(self.auth_provider.get_agent_config(), computer, tool_name, params, timeout)

    def create_get_tools_request(self, computer: str) -> GetToolsReq:
        """
        创建获取工具请求对象
        Create get tools request object

        Args:
            computer (str): 目标计算机ID / Target computer ID

        Returns:
            GetToolsReq: 获取工具请求 / Get tools request
        """
        return _rb.build_get_tools_request(self.auth_provider.get_agent_config(), computer)

    def create_get_resources_request(self, computer: str, mcp_server: str, cursor: str | None = None) -> GetResourcesReq:
        """
        创建获取资源请求对象（透明转发 MCP resources/list）
        Create get-resources request object (transparent forward of MCP resources/list)

        Args:
            computer (str): 目标计算机ID / Target computer ID
            mcp_server (str): 目标 MCP Server 名称 / Target MCP Server name
            cursor (str | None): MCP 标准翻页游标；首次传 None / MCP pagination cursor; None for first page

        Returns:
            GetResourcesReq: 获取资源请求 / Get resources request
        """
        return _rb.build_get_resources_request(self.auth_provider.get_agent_config(), computer, mcp_server, cursor)

    def create_get_desktop_request(self, computer: str, *, size: int | None = None, window: str | None = None) -> GetDeskTopReq:
        """
        创建获取桌面请求对象
        Create get desktop request object

        Args:
            computer (str): 目标计算机ID / Target computer ID
            size (int | None): 桌面窗口数量上限 / Max number of windows
            window (str | None): 指定窗口URI / Specific window URI

        Returns:
            GetDeskTopReq: 获取桌面请求 / Get desktop request
        """
        return _rb.build_get_desktop_request(self.auth_provider.get_agent_config(), computer, size=size, window=window)

    def create_get_skills_request(self, computer: str) -> GetSkillsReq:
        """创建获取 SKILL 清单请求对象 / Create get-skills request object (v0.2.1).

        协议依据 / Protocol: a2c-smcp-protocol events.md §client:get_skills.
        """
        return _rb.build_get_skills_request(self.auth_provider.get_agent_config(), computer)

    def create_get_skill_request(self, computer: str, name: str, rel_path: str | None = None) -> GetSkillReq:
        """创建获取 SKILL 包内单个资源请求对象 / Create get-skill request object (v0.2.1).

        协议依据 / Protocol: events.md §client:get_skill. ``rel_path`` 缺省取 ``SKILL.md`` 入口.
        """
        return _rb.build_get_skill_request(self.auth_provider.get_agent_config(), computer, name, rel_path)

    def create_get_blob_request(
        self,
        computer: str,
        blob_handle: str,
        chunk_offset: int | None = None,
        max_chunk_bytes: int | None = None,
    ) -> GetBlobReq:
        """创建通用二进制拉取单块请求对象 / Create get-blob chunk request object (v0.2.1).

        协议依据 / Protocol: events.md §client:get_blob；blob-transfer.md.
        """
        return _rb.build_get_blob_request(
            self.auth_provider.get_agent_config(),
            computer,
            blob_handle,
            chunk_offset,
            max_chunk_bytes,
        )

    def process_desktop_response(self, response: GetDeskTopRet, computer: str) -> None:
        """
        处理桌面响应（默认仅记录日志；留作后续扩展回调）。
        Process desktop response (log only by default; placeholder for future callbacks).
        """
        try:
            desktops = response.get("desktops", []) if isinstance(response, dict) else []
            logger.info(f"Received desktop from computer {computer}, windows={len(desktops)}")
        except Exception as e:
            logger.error(f"Error processing desktop response: {e}", exc_info=True)

    def handle_tool_call_timeout(self, req_id: str) -> CallToolResult:
        """
        处理工具调用超时情况
        Handle tool call timeout situation

        Args:
            req_id (str): 请求ID / Request ID

        Returns:
            CallToolResult: 超时错误结果 / Timeout error result
        """
        result = CallToolResult(
            content=[TextContent(text=f"工具调用超时 / Tool call timeout, req_id={req_id}", type="text")],
            isError=True,
        )
        # 协议结果级标记（32eea98 / protocol#5）：标识此结果为超时返回，使 Agent 能区分「超时 / 取消 / 普通失败」。
        # 经 ``.meta`` 写入真实 meta 字段（出线默认 dump→key=meta）；构造器传 ``meta=`` 会落 extra（字段 alias 为 ``_meta``）。
        # Protocol result-level marker (32eea98 / protocol#5): mark this result as a timeout return so the Agent can
        # distinguish timeout from cancellation / ordinary failure. Set via ``.meta`` (real field, wire key ``meta``);
        # passing ``meta=`` to the ctor would land in ``extra`` since the field alias is ``_meta``.
        result.meta = {"a2c_timeout": True}
        return result

    def validate_office_data(self, data: EnterOfficeNotification | LeaveOfficeNotification) -> str:
        """
        验证办公室数据并返回计算机ID
        Validate office data and return computer ID

        Args:
            data: 办公室通知数据 / Office notification data

        Returns:
            str: 计算机ID / Computer ID

        Raises:
            AssertionError: 当数据无效时 / When data is invalid
        """
        agent_config = self.auth_provider.get_agent_config()
        assert data["office_id"] == agent_config["office_id"], "无效的办公室ID / Invalid office ID"
        assert data.get("computer"), "无效的计算机ID / Invalid computer ID"
        return cast(str, data["computer"])

    def handle_computer_enter_office(self, data: EnterOfficeNotification) -> None:
        """
        处理Computer加入办公室事件
        Handle Computer enter office event

        Args:
            data: 加入办公室通知数据 / Enter office notification data
        """
        try:
            computer = self.validate_office_data(data)
            logger.info(f"Computer {computer} entered office {data['office_id']}")

            # 调用事件处理器（强制携带 client 引用）
            # Call event handler (force passing client reference)
            if self.event_handler:
                self.event_handler.on_computer_enter_office(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer enter office: {e}", exc_info=True)

    def handle_computer_leave_office(self, data: LeaveOfficeNotification) -> None:
        """
        处理Computer离开办公室事件
        Handle Computer leave office event

        Args:
            data: 离开办公室通知数据 / Leave office notification data
        """
        try:
            computer = self.validate_office_data(data)
            logger.info(f"Computer {computer} left office {data['office_id']}")

            # 调用事件处理器（强制携带 client 引用）
            # Call event handler (force passing client reference)
            if self.event_handler:
                self.event_handler.on_computer_leave_office(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer leave office: {e}", exc_info=True)

    def handle_computer_update_config(self, data: UpdateMCPConfigNotification) -> None:
        """
        处理Computer更新配置事件
        Handle Computer update config event

        Args:
            data: 配置更新通知数据 / Config update notification data
        """
        try:
            computer = data["computer"]
            logger.info(f"Computer {computer} updated config")

            # 调用事件处理器（强制携带 client 引用）
            # Call event handler (force passing client reference)
            if self.event_handler:
                self.event_handler.on_computer_update_config(data, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error handling computer update config: {e}", exc_info=True)

    def process_tools_response(self, response: GetToolsRet, computer: str) -> None:
        """
        处理工具响应
        Process tools response

        Args:
            response: 工具响应数据 / Tools response data
            computer: 计算机ID / Computer ID
        """
        try:
            if tools := response.get("tools"):
                logger.info(f"Received {len(tools)} tools from computer {computer}")

                # 调用事件处理器（强制携带 client 引用）
                # Call event handler (force passing client reference)
                if self.event_handler:
                    self.event_handler.on_tools_received(computer, tools, self)  # type: ignore[arg-type]

        except Exception as e:
            logger.error(f"Error processing tools response: {e}", exc_info=True)

    def join_office(self, office_id: str, agent_name: str, namespace: str | None = None) -> None:
        """
        加入一个Office（Socket.IO中的Room）
        Join an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID，在A2C-smcp协议中，OfficeID即为Socket.IO RoomID
                            / Room ID, in A2C-smcp protocol, OfficeID is the Socket.IO RoomID
            agent_name (str): Agent名称，提供给前端展示用
                            / Agent name, for frontend display
            namespace (str | None): 命名空间 / Namespace
        """
        self.emit(
            JOIN_OFFICE_EVENT,
            EnterOfficeReq(office_id=office_id, role="agent", name=agent_name),
            namespace=namespace,
        )

    def leave_office(self, office_id: str, namespace: str | None = None) -> None:
        """
        离开一个Office（Socket.IO中的Room）
        Leave an Office (Room in Socket.IO)

        Args:
            office_id (str): 房间ID / Room ID
            namespace (str | None): 命名空间 / Namespace
        """
        self.emit(LEAVE_OFFICE_EVENT, LeaveOfficeReq(office_id=office_id), namespace=namespace)

    @abstractmethod
    def register_event_handlers(self) -> None:
        """
        注册事件处理器，子类需要实现具体的注册逻辑
        Register event handlers, subclasses need to implement specific registration logic
        """
        # 这个方法需要在子类中实现具体的事件注册逻辑
        # This method needs to implement specific event registration logic in subclasses
        pass
