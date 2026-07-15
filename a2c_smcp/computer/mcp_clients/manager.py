# filename: manager.py
# @Time    : 2025/8/17 16:53
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
import asyncio
import json
from collections.abc import AsyncGenerator, Iterable
from typing import Any

from mcp.client.session import MessageHandlerFnT
from mcp.types import CallToolResult, ReadResourceResult, Resource, Tool
from vrl_python import VRLRuntime

from a2c_smcp.computer.mcp_clients.base_client import MCPServerNotFoundError
from a2c_smcp.computer.mcp_clients.model import A2C_TOOL_META, A2C_VRL_TRANSFORMED, MCPClientProtocol, MCPServerConfig, ToolMeta
from a2c_smcp.computer.mcp_clients.utils import client_factory
from a2c_smcp.types import BUNDLE_ID, EXPOSED_TOOL_NAME, TOOL_NAME
from a2c_smcp.utils.bundle_id import resolve_bundle_id
from a2c_smcp.utils.logger import get_logger, truncate

logger = get_logger("computer")

# SKILL 资源枚举翻页安全上界：防御恒非空 cursor（server bug / 恶意）导致的无限循环挂死物化。
# Pagination safety bound for SKILL enumeration: guard against a never-terminating cursor hanging staging.
_MAX_SKILL_LIST_PAGES = 1000


class ToolNameDuplicatedError(Exception):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)


class MCPServerManager:
    """
    MCP Server管理器

    所有以下划线开头的私有方法是非协程安全的。如果外部调用，需要使用普通方法。

    # 动态取消（响应 server 端 notify:tool_call_cancel）已在 Computer.aexecute_tool 层实现（#96）：
    #   Computer 以 req_id 为键将 acall_tool 包装为可取消的在途任务（见 Computer._acall_tool_cancellable /
    #   Computer.acancel_tool）。此处 acall_tool 维持原有 asyncio.wait_for 超时语义即可，无需在 Manager 持有
    #   req_id 级注册表（acall_tool 以 server/tool 为键、被 alias 路径复用，不感知 req_id）。
    """

    def __init__(
        self,
        auto_connect: bool = False,
        auto_reconnect: bool = True,
        message_handler: MessageHandlerFnT | None = None,
    ) -> None:
        # 存储所有服务器配置，以 bundle_id 为唯一身份键（协议 #15/#18：身份=bundle_id，name 降纯 display 不做键）
        # Server configs keyed by bundle_id (unique identity; name is pure display, never a key — protocol #15/#18).
        self._servers_config: dict[BUNDLE_ID, MCPServerConfig] = {}
        # 活动客户端 {bundle_id: client}
        self._active_clients: dict[BUNDLE_ID, MCPClientProtocol] = {}
        # ExposedToolMapping：exposed_tool_name -> (bundle_id, 原始工具名)。list_tools 与 tool_call **共用同一份**表
        # （协议 §ExposedToolMapping）。exposed = {bundle_id}__{alias ?? 原始名}，bundle_id 无 `__` 保证单射→查表不 split。
        # 被 forbidden 的工具**不进本表**（不可见不可调用）；跨 bundle_id 天然唯一，无需跨 server 对账。
        self._exposed_tools: dict[EXPOSED_TOOL_NAME, tuple[BUNDLE_ID, TOOL_NAME]] = {}
        # 自动重连标志
        self._auto_reconnect: bool = auto_reconnect
        # 自动连接标志
        self._auto_connect: bool = auto_connect
        # 自定义消息处理器，透传到各具体Client
        self._message_handler: MessageHandlerFnT | None = message_handler
        # 内部锁防止并发修改
        self._lock = asyncio.Lock()

    def get_server_config(self, bundle_id: BUNDLE_ID) -> MCPServerConfig:
        """通过 bundle_id 获取服务配置 / Get server config by bundle_id。"""
        return self._servers_config[bundle_id]

    def server_configs(self) -> tuple[MCPServerConfig, ...]:
        """全部服务配置的不可变快照（运行期活跃配置集，含动态挂载/重挂项）/ snapshot of all active server configs。"""
        return tuple(self._servers_config.values())

    def get_tool_meta(self, bundle_id: BUNDLE_ID, tool_name: TOOL_NAME) -> ToolMeta | None:
        """
        中文: 获取指定服务器（bundle_id）下某工具合并后的元数据（优先具体 tool_meta，缺失字段回落 default_tool_meta）。
        English: Get merged ToolMeta for a tool under the given server (bundle_id).

        Args:
            bundle_id (BUNDLE_ID): 服务器唯一身份 / server bundle_id
            tool_name (TOOL_NAME): 工具**原始名称**（非 exposed）/ original tool name

        Returns:
            ToolMeta | None: 合并后的工具元数据；若两侧均为空返回 None / merged ToolMeta or None if both absent.
        """
        config = self.get_server_config(bundle_id)
        return self._merged_tool_meta(config, tool_name)

    async def enable_auto_connect(self) -> None:
        """启用自动连接"""
        async with self._lock:
            self._auto_connect = True

    async def disable_auto_connect(self) -> None:
        """禁用自动连接"""
        async with self._lock:
            self._auto_connect = False

    async def enable_auto_reconnect(self) -> None:
        """启用自动重连"""
        async with self._lock:
            self._auto_reconnect = True

    async def disable_auto_reconnect(self) -> None:
        """禁用自动重连"""
        async with self._lock:
            self._auto_reconnect = False

    async def ainitialize(self, servers: Iterable[MCPServerConfig]) -> None:
        """
        初始化管理器并添加服务器配置

        Args:
            servers (list[MCPServerConfig]): MCP服务器配置
        """
        async with self._lock:
            # 清理旧设置与配置
            # 1. 停止所有活动客户端
            await self._astop_all()
            # 2. 清空所有状态存储
            self._clear_all()
            # 3. 添加新配置（no-double-open，加载期 first-wins）：按配置顺序 per-bundle_id 保留**首个**，
            #    其余作 Computer 本地诊断（WARN，非协议错误码）。同 bundle_id = 同软件，任一时刻只一个。
            #    (protocol §no-double-open, boot=first-wins). Runtime add/update is update-in-place (see _add_or_update).
            seen_bundle_ids: set[BUNDLE_ID] = set()
            for server in servers:
                bundle_id = resolve_bundle_id(server)
                if bundle_id in seen_bundle_ids:
                    logger.warning(
                        f"no-double-open: 重复 bundle_id '{bundle_id}'（name={server.name!r}）——保留配置顺序首个、"
                        f"跳过此项（Computer 本地诊断，非协议错误码）；如需多实例请显式指定不同 bundle_id。",
                    )
                    continue
                seen_bundle_ids.add(bundle_id)
                await self._add_or_update_server_config(server)
            await self._arefresh_tool_mapping()

    async def _add_or_update_server_config(self, config: MCPServerConfig) -> None:
        """
        添加/更新服务器配置（不启动客户端）

        如果已存在，检查是否已经建立客户端连接，如果是，检查是否需要自动重连
        如果不存在，直接添加配置

        Args:
            config (MCPServerConfig): MCP服务器配置
        """
        bundle_id = resolve_bundle_id(config)
        if bundle_id in self._servers_config:
            # 运行期同 bundle_id = **原地更新**（intentional replace；name 可变、bundle_id 稳定），不算 no-double-open 冲突
            # Runtime same bundle_id = update-in-place (protocol §no-double-open runtime branch).
            if bundle_id in self._active_clients:
                if self._auto_reconnect:
                    self._servers_config[bundle_id] = config
                    await self._arestart_server(bundle_id)
                else:
                    raise RuntimeError(
                        f"Server bundle_id={bundle_id!r} (name={config.name!r}) is active. Stop it before updating config",
                    )
            else:
                # 配置存在但客户端未激活，更新配置并根据 auto_connect 决定是否启动
                # Config exists but client is not active, update config and start if auto_connect is enabled
                self._servers_config[bundle_id] = config
                if self._auto_connect:
                    await self._astart_client(bundle_id)
        else:
            self._servers_config[bundle_id] = config
            if self._auto_connect:
                await self._astart_client(bundle_id)

    async def aadd_or_aupdate_server(self, config: MCPServerConfig) -> None:
        """添加或更新服务器配置。运行期同 ``bundle_id`` = **原地更新**（不算 no-double-open 冲突）。"""
        async with self._lock:
            await self._add_or_update_server_config(config)
            await self._arefresh_tool_mapping()

    async def aremove_server(self, bundle_id: BUNDLE_ID) -> None:
        """按 bundle_id 移除服务器配置 / Remove a server config by bundle_id。"""
        async with self._lock:
            if bundle_id in self._active_clients:
                await self._astop_client(bundle_id)
            del self._servers_config[bundle_id]
            await self._arefresh_tool_mapping()

    async def _arestart_server(self, bundle_id: BUNDLE_ID) -> None:
        """重启服务器客户端（按 bundle_id）。"""
        # 明确使用当前管理器中的最新配置
        config = self._servers_config.get(bundle_id)
        if not config:
            # 防御性分支：正常流程不会触发 / Defensive branch, not triggered in normal flow
            raise ValueError(f"Server bundle_id={bundle_id!r} not found in config")  # pragma: no cover

        # 确保使用最新配置重启
        if bundle_id in self._active_clients:
            await self._astop_client(bundle_id)

        # 只有启用的配置才能重启
        if not config.disabled:
            await self._astart_client(bundle_id)

    async def astart_all(self) -> None:
        """启动所有启用的服务器"""
        async with self._lock:
            logger.debug(f"Manager Start all async task: {asyncio.current_task()}")
            for bundle_id in self._servers_config:
                if not self._servers_config[bundle_id].disabled:
                    await self._astart_client(bundle_id)

    async def astart_client(self, bundle_id: BUNDLE_ID) -> None:
        """启动单个服务器客户端（按 bundle_id）。"""
        async with self._lock:
            await self._astart_client(bundle_id)

    async def _astart_client(self, bundle_id: BUNDLE_ID) -> None:
        """启动单个服务器客户端（按 bundle_id）。"""
        config = self._servers_config.get(bundle_id)
        if not config:
            # 防御性分支：正常流程不会触发 / Defensive branch, not triggered in normal flow
            raise ValueError(f"Unknown server bundle_id={bundle_id!r}")  # pragma: no cover

        if config.disabled:
            raise RuntimeError(f"Cannot start disabled server bundle_id={bundle_id!r} (name={config.name!r})")

        if bundle_id in self._active_clients:
            return  # 已经启动

        # 根据配置类型创建客户端
        client = client_factory(config, message_handler=self._message_handler)
        await client.aconnect()
        self._active_clients[bundle_id] = client
        # ExposedToolMapping 刷新不再抛跨 server 重名（bundle_id 前缀天然唯一），无需回滚
        await self._arefresh_tool_mapping()

    async def astop_client(self, bundle_id: BUNDLE_ID) -> None:
        """停止单个服务器客户端（按 bundle_id）。"""
        async with self._lock:
            await self._astop_client(bundle_id)

    async def _astop_client(self, bundle_id: BUNDLE_ID) -> None:
        """停止单个服务器客户端（按 bundle_id）。"""
        client = self._active_clients.pop(bundle_id, None)
        if client:
            await client.adisconnect()
            await self._arefresh_tool_mapping()

    async def _astop_all(self) -> None:
        """停止所有客户端"""
        for name in list(self._active_clients.keys()):
            await self._astop_client(name)

    async def astop_all(self) -> None:
        """停止所有客户端"""
        async with self._lock:
            logger.debug(f"Manager Stop all async task: {asyncio.current_task()}")
            await self._astop_all()

    def _clear_all(self) -> None:
        """清空所有连接与映射 / Clear all state。"""
        self._servers_config.clear()
        self._active_clients.clear()
        self._exposed_tools.clear()

    async def aclose(self) -> None:
        """关闭所有连接（别名）"""
        await self.astop_all()

        # 2. 清空所有状态存储
        self._clear_all()

    async def _arefresh_tool_mapping(self) -> None:
        """重建 ExposedToolMapping：``_exposed_tools[exposed_tool_name] = (bundle_id, 原始工具名)``。

        Rebuild the shared ExposedToolMapping used by both ``available_tools`` and ``tool_call`` routing.

        ``exposed_tool_name = {bundle_id}__{alias ?? 原始名}``（协议 §exposed_tool_name）。跨 bundle_id 因前缀
        天然唯一——**无需**跨 server 重名对账（旧 ``ToolNameDuplicatedError`` 场景消失）。forbidden 工具**不进表**
        （不可见不可调用）。同一 bundle_id 内两工具经 ``alias`` 撞出相同 exposed → 保留首个 + Computer 本地诊断
        （WARN，非协议错误码）。
        """
        self._exposed_tools.clear()
        for bundle_id, client in self._active_clients.items():
            config = self._servers_config[bundle_id]
            try:
                tools = await client.list_tools()
            except Exception as e:
                logger.error(f"Error listing tools for bundle_id={bundle_id!r} (name={config.name!r}): {e}", exc_info=True)
                continue
            for t in tools or []:
                original_tool_name = t.name
                # 合并后的工具元数据（具体 tool_meta 优先，回落 default_tool_meta）
                tool_meta = self._merged_tool_meta(config, original_tool_name)
                # alias 仅替换 exposed 的**工具名部分**（协议新语义，仍带 {bundle_id}__ 前缀）；无 alias 回退原始名
                tool_part = tool_meta.alias if tool_meta and tool_meta.alias else original_tool_name
                # forbidden：按**原始名**或 **alias 后工具名**匹配（用户可用任一禁用）；命中即不暴露、不路由
                if original_tool_name in (config.forbidden_tools or []) or tool_part in (config.forbidden_tools or []):
                    continue
                exposed = f"{bundle_id}__{tool_part}"
                if exposed in self._exposed_tools:
                    # 同一 bundle_id 内 alias 撞名（跨 bundle_id 不可能撞）→ 保留首个 + 诊断，指导修正 alias
                    logger.warning(
                        f"exposed_tool_name 冲突（同 bundle_id={bundle_id!r} 内 alias 撞名）：'{exposed}'——保留首个、"
                        f"跳过原始工具 '{original_tool_name}'；请修正 tool_meta.alias（Computer 本地诊断，非协议错误码）。",
                    )
                    continue
                self._exposed_tools[exposed] = (bundle_id, original_tool_name)

    async def arefresh_tools(self) -> None:
        """公开的工具映射刷新入口：锁内重建 ExposedToolMapping（``_exposed_tools``）（#127）。

        Public tool-mapping refresh entry: rebuild the ExposedToolMapping under the lock (#127).

        用途 / Use: MCP Server 运行期 ``tools/list_changed`` 后，boot 期构建的 ``_exposed_tools`` 已陈旧——
        **新增**工具不在映射中，``available_tools()`` 迭代映射键时永远漏掉它（``client:get_tools`` 看不到新工具）。
        本方法在 **安全上下文**（如 socketio ``on_get_tools`` 服务路径）被调用以刷新映射。

        约束 / Constraint: **禁止**在 MCP ``ClientSession`` 的 ``message_handler`` 内联 ``await`` 本方法——
        其内部 ``list_tools()`` 会向同一会话发起请求，而接收循环正阻塞于 message_handler → **会话级重入死锁**
        （#127 探针实证 ``TimeoutError``）。变化侧仅应触发轻量 socketio emit，刷新交由服务侧安全上下文完成。
        MUST NOT be awaited inline inside an MCP ``message_handler`` (session-reentrant deadlock, see #127).
        """
        async with self._lock:
            await self._arefresh_tool_mapping()

    async def avalidate_tool_call(self, tool_name: EXPOSED_TOOL_NAME, parameters: dict) -> tuple[BUNDLE_ID, TOOL_NAME]:
        """校验 ``exposed_tool_name`` 并经 ExposedToolMapping 解析到 ``(bundle_id, 原始工具名)``。

        Validate an ``exposed_tool_name`` and route it via ExposedToolMapping to ``(bundle_id, original_tool_name)``.

        Args:
            tool_name (EXPOSED_TOOL_NAME): Agent 传入的 exposed_tool_name（``{bundle_id}__{alias??原始名}``）。
            parameters (dict): 工具调用参数（当前版本不做 Schema 校验）。

        Returns:
            tuple[BUNDLE_ID, TOOL_NAME]: 归属 bundle_id 与**原始**工具名。

        Raises:
            ValueError: exposed_tool_name 未命中 ExposedToolMapping（上层映射协议 ``4001``）。
        """
        # 标记当前parameters尚未被使用 / parameters not schema-checked in this version
        logger.debug(f"{truncate(parameters)}未被检查。当前版本不支持Schema校验。")
        # 整键查表（禁 split 反解身份；bundle_id 无 `__` 保证单射，原始名内含 `__` 无害）
        route = self._exposed_tools.get(tool_name)
        if route is None:
            raise ValueError(f"Tool '{tool_name}' not found in ExposedToolMapping")
        return route

    async def acall_tool(
        self,
        bundle_id: BUNDLE_ID,
        tool_name: TOOL_NAME,
        parameters: dict,
        timeout: float | None = None,
    ) -> CallToolResult:
        """
        触发MCP工具的调用。注意此方法 tool_name 必须是工具**原始名称**，若以 exposed_tool_name 调用请用 aexecute_tool。

        Args:
            bundle_id (BUNDLE_ID): 目标 MCP Server 唯一身份 / target server bundle_id
            tool_name (str): 工具**原始名称** / original tool name
            parameters (dict): 工具调用参数
            timeout (float | None): 超时时间

        Returns:
            CallToolResult: MCP 标准返回格式
        """
        # 获取MCP服务客户端连接
        client = self._active_clients.get(bundle_id)
        if not client:
            raise RuntimeError(f"Server bundle_id={bundle_id!r} for tool '{tool_name}' is not active")

        # 获取合并后的工具元数据
        config = self._servers_config[bundle_id]
        tool_meta = self._merged_tool_meta(config, tool_name)

        # 执行工具调用
        try:
            if timeout:
                result = await asyncio.wait_for(client.call_tool(tool_name, parameters), timeout)
            else:
                result = await client.call_tool(tool_name, parameters)

            # 如果有自定义元数据，则利用MCP协议返回Result中的meta元数据携带能力透传。
            if tool_meta:
                if result.meta:
                    result.meta.setdefault(A2C_TOOL_META, {}).update(tool_meta)
                else:
                    result.meta = {A2C_TOOL_META: tool_meta}

            # 中文: 如果配置了VRL脚本,尝试对返回值进行转换
            # English: If VRL script is configured, try to transform the return value
            if config.vrl:
                try:
                    # 中文: 将CallToolResult序列化为字典，并注入tool_name和parameters作为VRL的Event输入
                    # English: Serialize CallToolResult to dict and inject tool_name and parameters as VRL Event input
                    event = result.model_dump(mode="json")
                    # 中文: 注入工具调用的上下文信息
                    # English: Inject tool call context information
                    event["tool_name"] = tool_name
                    event["parameters"] = parameters

                    # 中文: 执行VRL转换（使用系统本地时区）
                    # English: Execute VRL transformation (use system local timezone)
                    # 获取系统时区名称，例如 "Asia/Shanghai" 或 "America/New_York"
                    # Get system timezone name, e.g., "Asia/Shanghai" or "America/New_York"
                    # VRL需要IANA时区名称，尝试从tzlocal获取；若失败则使用UTC
                    # VRL requires IANA timezone name; try to get from tzlocal, fallback to UTC
                    try:
                        import tzlocal

                        timezone_name = str(tzlocal.get_localzone())
                    except Exception:
                        # 如果无法获取本地时区，回退到UTC / Fallback to UTC if local timezone unavailable
                        timezone_name = "UTC"

                    vrl_result = VRLRuntime.run(config.vrl, event, timezone=timezone_name)
                    transformed_event = vrl_result.processed_event

                    # 中文: 将转换后的结果压缩为JSON字符串存入Meta（因为Meta要求简单数据结构）
                    # English: Compress transformed result to JSON string for Meta (Meta requires simple data structure)
                    if result.meta is None:
                        result.meta = {}
                    result.meta[A2C_VRL_TRANSFORMED] = json.dumps(transformed_event, ensure_ascii=False)

                    logger.debug(f"VRL转换成功 / VRL transformation succeeded for tool '{tool_name}'")
                except Exception as e:
                    # 中文: VRL转换失败不影响正常返回，仅记录警告日志
                    # English: VRL transformation failure doesn't affect normal return, just log warning
                    logger.warning(
                        f"VRL转换失败 / VRL transformation failed for tool '{tool_name}': {e}. "
                        f"原始结果将正常返回 / Original result will be returned normally.",
                    )

            return result
        except TimeoutError:
            raise TimeoutError(f"Tool '{tool_name}' execution timed out") from None
        except Exception as e:
            raise RuntimeError(f"Tool execution failed: {e}") from e

    async def aexecute_tool(self, tool_name: EXPOSED_TOOL_NAME, parameters: dict, timeout: float | None = None) -> CallToolResult:
        """执行指定工具。入参 ``tool_name`` 为 **exposed_tool_name**，经 ExposedToolMapping 解析后调用原始工具。"""
        bundle_id, original_tool_name = await self.avalidate_tool_call(tool_name, parameters)
        return await self.acall_tool(bundle_id, original_tool_name, parameters, timeout)

    async def list_resources(self, bundle_id: BUNDLE_ID, cursor: str | None = None) -> tuple[list[Resource], str | None]:
        """
        中文: 单页透传指定 MCP Server（bundle_id）的 `resources/list`，供 v0.2 `client:get_resources` 使用。
        英文: Single-page transparent forward of a server's `resources/list`, for v0.2 `client:get_resources`.

        不做 scheme / 元数据过滤、不做跨 Server 聚合；翻页由调用方通过 cursor 控制。
        No scheme/metadata filtering, no cross-server aggregation; pagination is caller-driven via cursor.

        Args:
            bundle_id (BUNDLE_ID): 目标 MCP Server 的 bundle_id（wire `mcp_server`，协议 #18）/ Target server bundle_id.
            cursor (str | None): MCP 标准翻页游标；首次传 None / MCP pagination cursor; None for first page.

        Returns:
            tuple[list[Resource], str | None]: (本页资源, 下一页游标——None 表示末页)。

        Raises:
            MCPServerNotFoundError: bundle_id 未注册（→ 上层映射 4014，payload ``mcp_server``=bundle_id）。
            MCPCapabilityNotSupportedError: 目标 Server 未声明 `resources` 能力（→ 上层映射 4015）。
        """
        client = self._active_clients.get(bundle_id)
        if client is None:
            raise MCPServerNotFoundError(f"MCP Server bundle_id={bundle_id!r} is not registered")
        return await client.list_resources_page(cursor)

    def get_server_status(self) -> list[tuple[BUNDLE_ID, bool, str]]:
        """获取服务器状态列表 [(bundle_id, 是否活跃, 状态), ...]（身份=bundle_id）。"""
        return [
            (
                bundle_id,
                bundle_id in self._active_clients,
                "pending" if bundle_id not in self._active_clients else self._active_clients[bundle_id].state,
            )
            for bundle_id in self._servers_config
        ]

    async def available_tools(self) -> AsyncGenerator[Tool, Any]:
        """获取暴露给 Agent 的工具及其元数据；``Tool.name`` = **exposed_tool_name**（``{bundle_id}__{alias??原始名}``）。

        Yield tools exposed to the Agent; each ``Tool.name`` is the exposed_tool_name (协议 §exposed_tool_name / #106)。
        Agent 即以 exposed_tool_name 寻址，``aexecute_tool`` 经 ExposedToolMapping 解析回原始名调用上游。
        """
        async with self._lock:
            servers_cached_tools: dict[BUNDLE_ID, list[Tool]] = {}
            for exposed_name, (bundle_id, original_tool_name) in self._exposed_tools.items():
                if bundle_id not in self._active_clients:
                    continue
                if bundle_id not in servers_cached_tools:
                    servers_cached_tools[bundle_id] = await self._active_clients[bundle_id].list_tools()
                tools = servers_cached_tools[bundle_id]
                config = self._servers_config[bundle_id]

                tool = next((t for t in tools if t.name == original_tool_name), None)
                if tool:
                    a2c_meta = self._merged_tool_meta(config, original_tool_name)
                    # 产出名字改写后的**副本**（改 name 为 exposed_name），避免原地 mutate 缓存对象。
                    # Yield a renamed copy (name=exposed_name) to avoid mutating the cached tool object.
                    update: dict[str, Any] = {"name": exposed_name}
                    if a2c_meta:
                        merged_meta = dict(tool.meta) if tool.meta else {}
                        merged_meta[A2C_TOOL_META] = a2c_meta
                        update["meta"] = merged_meta
                    yield tool.model_copy(update=update)

    async def list_windows(self, window_uri: str | None = None) -> list[tuple[BUNDLE_ID, Resource]]:
        """
        列出所有活动MCP服务器的窗口资源，并附带其归属 server 的 **bundle_id**（desktop 按 bundle_id 分组，协议 #18）。
        List window resources from all active MCP servers with owning server **bundle_id**.

        Args:
            window_uri (str | None): 若提供，则仅返回URI完全匹配的窗口；否则返回所有窗口。

        Returns:
            list[tuple[BUNDLE_ID, Resource]]: [(bundle_id, resource), ...]（``window://host`` 的 host 不受影响）。
        """
        results: list[tuple[BUNDLE_ID, Resource]] = []
        # 不加锁读取活跃客户端快照，避免长时间持锁阻塞 I/O
        active_snapshot = list(self._active_clients.items())
        for bundle_id, client in active_snapshot:
            try:
                resources = await client.list_windows()
            except Exception as e:
                logger.error(f"Error listing windows for bundle_id={bundle_id!r}: {e}", exc_info=True)
                continue

            for res in resources:
                if window_uri is not None and str(res.uri) != window_uri:
                    continue
                results.append((bundle_id, res))
        return results

    async def list_skill_resources(self, bundle_id: BUNDLE_ID | None = None) -> list[tuple[BUNDLE_ID, Resource]]:
        """
        中文: 枚举活跃 MCP Server 的 ``skill://`` 资源（附归属 **bundle_id**），**完整消费 cursor 翻页直至末尾**。
        英文: Enumerate ``skill://`` resources from active MCP servers (with owning **bundle_id**), exhausting pages.

        与 :meth:`list_resources`（单页、Agent 控制翻页）不同：SKILL 物化由 Computer 主导，须拿到**全量**
        ``skill://`` 集合，故在此完整消费翻页（协议 skill.md §12）。未声明 ``resources`` 能力或枚举出错的
        server **跳过**（记 ERROR、不中断其余），对齐「SKILL 通道不使用 4015」（error-handling.md / skill.md §1.5）。
        Servers lacking ``resources`` capability or erroring are skipped (logged ERROR, others continue).

        注意 / Note: 归属键为 **bundle_id**——既用于 ``read_resource`` 路由，也**直接**充当 SKILL name 的
        ``<server>`` 段与磁盘分组键（skill.md §1.3，协议 #142 supersede 了 #18 的「``<server>`` 段与 BundleID
        正交」结论）。staging 不再回查 display ``name``。
        The bundle_id is both the routing key and the SKILL ``<server>`` segment verbatim.

        Args:
            bundle_id (BUNDLE_ID | None): 若提供仅枚举该 server（ResourceListChanged 单 server 重枚举）；否则全部活跃 server。

        Returns:
            list[tuple[BUNDLE_ID, Resource]]: [(bundle_id, skill_resource), ...]
        """
        results: list[tuple[BUNDLE_ID, Resource]] = []
        active_snapshot = list(self._active_clients.items())
        for bid, client in active_snapshot:
            if bundle_id is not None and bid != bundle_id:
                continue
            try:
                cursor: str | None = None
                pages = 0
                while True:
                    page, cursor = await client.list_resources_page(cursor)
                    results.extend((bid, res) for res in page if str(res.uri).startswith("skill://"))
                    pages += 1
                    if not cursor:
                        break
                    if pages >= _MAX_SKILL_LIST_PAGES:
                        logger.error(
                            f"list_skill_resources: bundle_id={bid!r} exceeded {_MAX_SKILL_LIST_PAGES} pages "
                            f"(non-terminating cursor?); aborting enumeration for this server",
                        )
                        break
            except Exception as e:
                # 未声明 resources 能力 / 连接异常 / 翻页失败 → 跳过该 server，不阻断其余
                logger.error(f"Error listing skill resources for bundle_id={bid!r}: {e}", exc_info=True)
                continue
        return results

    async def read_resource(self, bundle_id: BUNDLE_ID, uri: str) -> ReadResourceResult:
        """
        中文: 读取指定 MCP Server（bundle_id）的单个资源内容（通用 ``resources/read`` 入口，供 SKILL ``resources``
              模式逐子资源物化与子文件渐进式披露、及 ``client:get_resources`` 复用）。
        英文: Read a single resource's contents from a server by bundle_id (generic ``resources/read`` entry).

        复用既有 ``client.get_window_detail``——其实现为通用 ``read_resource``（命名沿用历史，非仅 window）。

        Raises:
            MCPServerNotFoundError: bundle_id 未注册（→ 上层映射 4014）/ bundle_id not registered.
        """
        client = self._active_clients.get(bundle_id)
        if client is None:
            raise MCPServerNotFoundError(f"MCP Server bundle_id={bundle_id!r} is not registered")
        return await client.get_window_detail(uri)

    async def get_windows_details(self, window_uri: str | None = None) -> list[tuple[BUNDLE_ID, Resource, ReadResourceResult]]:
        """
        中文: 读取所有活动 MCP 服务器的窗口资源详情（附归属 bundle_id）。Resource 仅为标识，需 read_resource 取内容。
        英文: Read detailed contents for window resources from all active MCP servers (with owning bundle_id).

        Args:
            window_uri (str | None): 若提供，则仅读取该 URI 完全匹配的窗口；否则读取所有窗口。

        Returns:
            list[tuple[BUNDLE_ID, Resource, ReadResourceResult]]: 列表项为 (bundle_id, resource, contents)。
        """
        details: list[tuple[BUNDLE_ID, Resource, ReadResourceResult]] = []
        active_snapshot = list(self._active_clients.items())
        for bundle_id, client in active_snapshot:
            try:
                resources = await client.list_windows()
            except Exception as e:
                logger.error(f"Error listing windows for bundle_id={bundle_id!r}: {e}", exc_info=True)
                continue

            for res in resources:
                if window_uri is not None and str(res.uri) != window_uri:
                    continue
                content = await client.get_window_detail(res)
                details.append((bundle_id, res, content))
        return details

    @staticmethod
    def _merged_tool_meta(config: MCPServerConfig, tool_name: TOOL_NAME) -> ToolMeta | None:
        """
        浅层合并工具元数据：优先使用具体 tool_meta，若字段缺失则回落到 default_tool_meta。
        Shallow merge ToolMeta: prefer per-tool meta; fallback to default for missing root-level fields.
        """
        specific = (config.tool_meta or {}).get(tool_name)
        default = config.default_tool_meta
        if specific is None and default is None:
            return None
        if specific is None:
            return default
        if default is None:
            return specific
        # 仅根级字段浅合并；specific优先
        merged: dict = {}
        # Pydantic v2: model_dump 可排除 None，以避免用 None 覆盖
        merged.update(default.model_dump(exclude_none=True))
        merged.update(specific.model_dump(exclude_none=True))
        return ToolMeta(**merged)
