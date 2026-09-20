"""
* 文件名: types
* 作者: JQQ
* 创建日期: 2025/9/30
* 最后修改日期: 2025/10/8
* 版权: 2023 JQQ. All rights reserved.
* 依赖: None
* 描述: Agent端类型定义 / Agent-side type definitions
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, runtime_checkable

from mcp.types import CallToolResult
from typing_extensions import TypedDict

from a2c_smcp.smcp import (
    A2CSkillRef,
    EnterOfficeNotification,
    LeaveOfficeNotification,
    SMCPTool,
    UpdateMCPConfigNotification,
    UpdateToolListNotification,
)

# 为避免运行时循环依赖，仅在类型检查时导入具体Client类型
# To avoid runtime circular imports, import concrete Client types only during type checking
if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from a2c_smcp.agent.client import AsyncSMCPAgentClient
    from a2c_smcp.agent.sync_client import SMCPAgentClient

# 类型别名定义 / Type aliases
AgentID: TypeAlias = str
ComputerID: TypeAlias = str
RequestID: TypeAlias = str


class AgentConfig(TypedDict):
    """
    Agent配置信息
    Agent configuration information
    """

    agent: str  # Agent唯一标识 / Agent unique identifier
    office_id: str  # 办公室ID / Office ID


class ToolCallContext(TypedDict):
    """
    工具调用上下文信息
    Tool call context information
    """

    computer: str  # 目标计算机ID / Target computer ID
    tool_name: str  # 工具名称 / Tool name
    params: dict  # 调用参数 / Call parameters
    timeout: int  # 超时时间 / Timeout duration


class AgentEventHandler(Protocol):
    """
    Agent事件处理器协议，定义Agent需要处理的事件回调
    Agent event handler protocol, defines event callbacks that Agent needs to handle
    """

    def on_computer_enter_office(self, data: EnterOfficeNotification, client: SMCPAgentClient) -> None:
        """
        Computer加入办公室时的处理逻辑
        Handling logic when Computer joins office

        Args:
            data: 加入办公室的通知数据 / Office join notification data
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    def on_computer_leave_office(self, data: LeaveOfficeNotification, client: SMCPAgentClient) -> None:
        """
        Computer离开办公室时的处理逻辑
        Handling logic when Computer leaves office

        Args:
            data: 离开办公室的通知数据 / Office leave notification data
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    def on_computer_update_config(self, data: UpdateMCPConfigNotification, client: SMCPAgentClient) -> None:
        """
        Computer更新配置时的处理逻辑
        Handling logic when Computer updates configuration

        Args:
            data: 配置更新通知数据 / Configuration update notification data
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    def on_computer_update_tool_list(self, data: UpdateToolListNotification, client: SMCPAgentClient) -> None:
        """
        Computer工具列表更新（MCP 运行期 tools/list_changed）时的**预清**处理逻辑（#127）
        Pre-clean hook when a Computer's tool list changed at runtime (MCP tools/list_changed)

        触发时机 / Trigger:
            - notify:update_tool_list 到达后、SDK 自动回拉 client:get_tools **之前**
            - On ``notify:update_tool_list``, BEFORE the SDK auto-refetches ``client:get_tools``

        用途 / Purpose:
            - SDK 自动回拉最终仅回调 ``on_tools_received``（消费方通常只 add、不 remove）。本回调语义
              对齐 ``on_computer_update_config``：给消费方一个**预清**该 Computer 旧工具的时机，使工具
              **移除 / 同名换 schema** 不残留旧定义。
            - The auto-refetch only fires ``on_tools_received`` (consumers usually only add). This hook lets
              a consumer pre-clean the Computer's stale tools so removal / same-name schema change is clean.

        向后兼容 / Backward compatibility:
            - 旧 EventHandler 未实现此方法时，SDK 通过 hasattr 守卫静默跳过（仍会回拉，仅退化为只增不删）
            - Legacy handlers missing this method are silently skipped via hasattr guard (add-only fallback)

        Args:
            data: 工具列表更新通知数据 / Tool list update notification data
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    def on_tools_received(self, computer: str, tools: list[SMCPTool], client: SMCPAgentClient) -> None:
        """
        接收到工具列表时的处理逻辑
        Handling logic when tools list is received

        Args:
            computer: 计算机ID / Computer ID
            tools: 工具列表 / Tools list
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    def on_skills_received(self, computer: str, skills: list[A2CSkillRef], client: SMCPAgentClient) -> None:
        """
        接收到 SKILL 清单时的处理逻辑（v0.2.1+）
        Handling logic when SKILL inventory is received (v0.2.1+)

        触发时机 / Trigger:
            - notify:update_skills 自动重拉 client:get_skills 成功后
            - After auto-refresh of ``client:get_skills`` triggered by ``notify:update_skills``

        向后兼容 / Backward compatibility:
            - 旧 EventHandler 未实现此方法时，SDK 通过 hasattr 守卫静默跳过
            - Legacy handlers missing this method are silently skipped via hasattr guard

        Args:
            computer: 计算机ID / Computer ID
            skills: SKILL 引用列表（轻量元数据，无 SKILL.md body） / Skill refs (lightweight, no body)
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...


class AsyncAgentEventHandler(Protocol):
    """
    异步Agent事件处理器协议
    Async Agent event handler protocol
    """

    async def on_computer_enter_office(self, data: EnterOfficeNotification, client: AsyncSMCPAgentClient) -> None:
        """
        Computer加入办公室时的异步处理逻辑
        Async handling logic when Computer joins office

        Args:
            data: 加入办公室的通知数据 / Office join notification data
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    async def on_computer_leave_office(self, data: LeaveOfficeNotification, client: AsyncSMCPAgentClient) -> None:
        """
        Computer离开办公室时的异步处理逻辑
        Async handling logic when Computer leaves office

        Args:
            data: 离开办公室的通知数据 / Office leave notification data
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    async def on_computer_update_config(self, data: UpdateMCPConfigNotification, client: AsyncSMCPAgentClient) -> None:
        """
        Computer更新配置时的异步处理逻辑
        Async handling logic when Computer updates configuration

        Args:
            data: 配置更新通知数据 / Configuration update notification data
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    async def on_computer_update_tool_list(self, data: UpdateToolListNotification, client: AsyncSMCPAgentClient) -> None:
        """
        Computer工具列表更新（MCP 运行期 tools/list_changed）时的**预清**异步处理逻辑（#127）
        Async pre-clean hook when a Computer's tool list changed at runtime (MCP tools/list_changed)

        触发时机 / Trigger:
            - notify:update_tool_list 到达后、SDK 自动回拉 client:get_tools **之前**
            - On ``notify:update_tool_list``, BEFORE the SDK auto-refetches ``client:get_tools``

        用途 / Purpose:
            - SDK 自动回拉最终仅回调 ``on_tools_received``（消费方通常只 add、不 remove）。本回调语义
              对齐 ``on_computer_update_config``：给消费方一个**预清**该 Computer 旧工具的时机，使工具
              **移除 / 同名换 schema** 不残留旧定义。
            - The auto-refetch only fires ``on_tools_received`` (consumers usually only add). This hook lets
              a consumer pre-clean the Computer's stale tools so removal / same-name schema change is clean.

        向后兼容 / Backward compatibility:
            - 旧 EventHandler 未实现此方法时，SDK 通过 hasattr 守卫静默跳过（仍会回拉，仅退化为只增不删）
            - Legacy handlers missing this method are silently skipped via hasattr guard (add-only fallback)

        Args:
            data: 工具列表更新通知数据 / Tool list update notification data
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    async def on_tools_received(self, computer: str, tools: list[SMCPTool], client: AsyncSMCPAgentClient) -> None:
        """
        接收到工具列表时的异步处理逻辑
        Async handling logic when tools list is received

        Args:
            computer: 计算机ID / Computer ID
            tools: 工具列表 / Tools list
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...

    async def on_skills_received(self, computer: str, skills: list[A2CSkillRef], client: AsyncSMCPAgentClient) -> None:
        """
        接收到 SKILL 清单时的异步处理逻辑（v0.2.1+）
        Async handling logic when SKILL inventory is received (v0.2.1+)

        触发时机 / Trigger:
            - notify:update_skills 自动重拉 client:get_skills 成功后
            - After auto-refresh of ``client:get_skills`` triggered by ``notify:update_skills``

        向后兼容 / Backward compatibility:
            - 旧 EventHandler 未实现此方法时，SDK 通过 hasattr 守卫静默跳过
            - Legacy handlers missing this method are silently skipped via hasattr guard

        Args:
            computer: 计算机ID / Computer ID
            skills: SKILL 引用列表（轻量元数据，无 SKILL.md body） / Skill refs (lightweight, no body)
            client: 调用发生时的Socket.IO Client / Socket.IO Client where make the call
        """
        ...


# 工具调用回调函数类型 / Tool call callback function types
ToolCallCallback = Callable[[str, str, dict, int], CallToolResult]
AsyncToolCallCallback = Callable[[str, str, dict, int], CallToolResult]

# Agent ID获取函数类型 / Agent ID getter function types
AgentIDGetter = Callable[[], str]
AsyncAgentIDGetter = Callable[[], str]


# ────────────────────── #209 在途工具调用取消 / In-flight tool-call cancellation ──────────────────────

# 取消信号的轮询间隔（秒）。协议硬约束：实现一律**轮询**，不得假定存在可 await 的 ``wait``。
# Cancel-signal poll interval (seconds). The protocol forbids assuming an awaitable ``wait`` exists.
DEFAULT_CANCEL_POLL_INTERVAL: float = 0.05


@runtime_checkable
class CancelSignal(Protocol):
    """宿主侧取消信号的**最小**契约：只要求 ``is_set()``（#209）。

    协议 `events.md §server:tool_call_cancel`（protocol#58 / PR#60）冻结了「Agent 侧主动取消」的
    行为语义，但**不规定** SDK 侧接口形状。本协议刻意取最小面：

    - **只依赖 ``is_set()``** —— 实现**不得**假定存在可 await 的 ``wait``。宿主形态各异：内核
      ``CancellationToken`` 只有 ``is_cancelled`` property；``asyncio.Event.wait()`` 在
      Celery threads 池的 ``loop=None`` 下会 ``RuntimeError``；取消信号还可能从**另一线程**打入。
      故 SDK 一律**轮询** ``is_set()``；信号对象存在其他成员时也**不会**被访问。
    - **粘滞性由宿主负责** —— 「取消早于调用开始」是否生效取决于宿主信号本身是否粘滞（``is_set()``
      置位后保持为真）。SDK 不做额外记忆。
    - **跨线程**：``threading.Event`` 或任意仅含 ``is_set()`` 的对象可直接跨线程置位。若宿主用的是
      ``asyncio.Event``，``set()`` **非线程安全** —— 非事件循环线程须经
      ``loop.call_soon_threadsafe(event.set)``。

    满足该形状的 ``threading.Event`` / ``asyncio.Event`` / 自定义 token 均可直接传入
    :meth:`AsyncSMCPAgentClient.emit_tool_call` / :meth:`SMCPAgentClient.emit_tool_call` 的 ``cancel``。

    / The minimal host-side cancel-signal contract: ``is_set()`` and nothing else.
    """

    def is_set(self) -> bool:
        """信号是否已置位 / whether the cancel signal is set."""
        ...


class ToolCallOutcome(StrEnum):
    """``client:tool_call`` 结果的三态（+ 成功）分类（#209）。

    据**结果级** A2C 标记在 ``isError`` 之上进一步分流，供调用方区分「取消 / 超时 / 普通失败」：

    - :attr:`CANCELLED`：被 ``notify:tool_call_cancel`` 中断（结果级 ``meta.a2c_cancelled=true``）；
    - :attr:`TIMED_OUT`：超时（结果级 ``meta.a2c_timeout=true``）—— Agent 自身超时路径**本地合成**
      亦归此态；
    - :attr:`FAILED`：其它工具级失败（``isError=true`` 但无取消/超时标记）；
    - :attr:`COMPLETED`：成功。

    取值与 rust-sdk 的 ``ToolCallOutcome`` 逐字对齐（``completed`` / ``timed_out`` / ``cancelled`` /
    ``failed``，见 rust-sdk#218）；枚举取值即字符串，宿主日志与轨迹字段按此落库。

    / Tri-state (+success) classification of a ``client:tool_call`` result.
    """

    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"


def _meta_containers(result: CallToolResult | Mapping[str, Any]) -> tuple[Any, ...]:
    """收集结果级 meta 的所有候选容器（协议：Consumer **SHOULD** 宽松读取）。

    - ``Mapping``（wire dict）：``meta`` / ``_meta`` 两种线上 key —— 不同 SDK 序列化路径可能不同。
    - ``CallToolResult`` 形态：真实 ``.meta`` 字段；再兜底 ``model_extra`` —— MCP ``Result.meta``
      的字段 alias 是 ``_meta``，宿主若以构造器写法 ``CallToolResult(meta={...})`` 造结果，标记会落
      ``extra`` 而非字段（本仓 ``base.py`` 的 ``handle_tool_call_timeout`` 对此陷阱有注记），
      不兜底则这类结果被**静默误判**为普通失败。

    / Collect every candidate result-level meta container, leniently (protocol SHOULD).
    """
    if isinstance(result, Mapping):
        return (result.get("meta"), result.get("_meta"))
    extra = getattr(result, "model_extra", None)
    return (
        getattr(result, "meta", None),
        extra.get("meta") if isinstance(extra, Mapping) else None,
        extra.get("_meta") if isinstance(extra, Mapping) else None,
    )


def _result_meta_flag(result: CallToolResult | Mapping[str, Any], key: str) -> bool:
    """读结果级 meta 的布尔标记，**仅** ``is True`` 计真（与 rust ``as_bool()`` 同判）。

    / Read a result-level meta bool flag; strictly ``is True`` (mirrors rust's ``as_bool()``).
    """
    return any(isinstance(c, Mapping) and c.get(key) is True for c in _meta_containers(result))


def _result_is_error(result: CallToolResult | Mapping[str, Any]) -> bool:
    """读 ``isError``；缺席 / 非布尔真值均视为 False（与 rust ``unwrap_or(false)`` 同判）。"""
    value = result.get("isError") if isinstance(result, Mapping) else getattr(result, "isError", None)
    return value is True


def classify_tool_call_outcome(result: CallToolResult | Mapping[str, Any]) -> ToolCallOutcome:
    """把 ``client:tool_call`` 的结果分类为三态（+ 成功）（#209）。

    判定优先级 **取消 > 超时 > 失败 > 成功** —— 取消/超时同样 ``isError=True``，是对 ``isError`` 的
    **语义细化**，故**先判标记再判 ``isError``**；顺序写反会把取消态误归普通失败。

    纯函数、无 I/O；调用方对 ``emit_tool_call`` 的返回值自行调一次即可（``emit_tool_call`` 的返回
    类型恒为 ``CallToolResult``，不因此改变）。

    Args:
        result: ``emit_tool_call`` 的返回值，或 wire 形态的结果映射 / the call result, or a wire mapping.

    Returns:
        ToolCallOutcome: 三态（+ 成功）分类 / the tri-state (+success) classification.

    / Classify a ``client:tool_call`` result: cancelled > timed_out > failed > completed.
    """
    if _result_meta_flag(result, "a2c_cancelled"):
        return ToolCallOutcome.CANCELLED
    if _result_meta_flag(result, "a2c_timeout"):
        return ToolCallOutcome.TIMED_OUT
    if _result_is_error(result):
        return ToolCallOutcome.FAILED
    return ToolCallOutcome.COMPLETED
