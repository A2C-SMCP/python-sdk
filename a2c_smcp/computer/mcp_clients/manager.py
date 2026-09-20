# filename: manager.py
# @Time    : 2025/8/17 16:53
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, NamedTuple, NoReturn, cast

from mcp.client.session import MessageHandlerFnT
from mcp.types import CallToolResult, ReadResourceResult, Resource, Tool
from vrl_python import VRLRuntime

from a2c_smcp.computer.mcp_clients.auth_error import (
    UpstreamRedirectStoppedError,
    build_auth_error_result,
    classify_auth_error,
)
from a2c_smcp.computer.mcp_clients.base_client import MCPServerNotFoundError
from a2c_smcp.computer.mcp_clients.http_client import AuthSignal, HttpMCPClient
from a2c_smcp.computer.mcp_clients.model import (
    A2C_TOOL_META,
    A2C_VRL_TRANSFORMED,
    MCPClientProtocol,
    MCPServerActivationState,
    MCPServerConfig,
    MCPServerConnectionState,
    MCPServerRuntimeStatus,
    StreamableHttpServerConfig,
    ToolMeta,
)
from a2c_smcp.computer.mcp_clients.oauth_coordinator import (
    OAuthCoordinator,
    parse_bearer_resource_metadata,
)
from a2c_smcp.computer.mcp_clients.oauth_credential_store import (
    InMemoryOAuthCredentialStore,
    OAuthCredentialStore,
)
from a2c_smcp.computer.mcp_clients.oauth_flow import OAuthFlow
from a2c_smcp.computer.mcp_clients.oauth_security import (
    same_origin,
    validate_secure_url,
)
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthError,
    OAuthErrorCode,
    OAuthFlowOutcome,
    OAuthOptions,
    OAuthProtocolError,
    OAuthStatus,
    default_oauth_options,
)
from a2c_smcp.computer.mcp_clients.start_gate import McpStartGate
from a2c_smcp.computer.mcp_clients.utils import client_factory
from a2c_smcp.types import BUNDLE_ID, EXPOSED_TOOL_NAME, SERVER_NAME, TOOL_NAME
from a2c_smcp.utils.bundle_id import resolve_bundle_id
from a2c_smcp.utils.cancellation import restores_cancellation
from a2c_smcp.utils.logger import get_logger, truncate

logger = get_logger("computer")


@dataclass(frozen=True)
class _ServerDeclaredToolMeta:
    """Server 声明层白名单输入（协议 §ToolMeta 三层合并规则：白名单**仅 tags** 一项）。

    MUST NOT 复用完整 ``ToolMeta`` 作输入模型——``ToolMeta`` 为 ``extra="allow"``，直接构造会把
    ``auto_apply`` 等白名单外字段静默收进 canonical（提权向量，Disc#56 本侧确认 P1、裁决不变量 2）。
    """

    tags: list[str] | None = None


def _parse_server_declared_tool_meta(raw: Any) -> tuple[_ServerDeclaredToolMeta | None, str | None]:
    """解析 ``Tool._meta["a2c_tool_meta"]`` Server 声明 → ``(合法提取物, None)`` 或 ``(None, 丢弃原因)``。

    畸形判据仅两条（协议 §ToolMeta 三层合并规则）：声明非对象 / ``tags`` 非 ``list[str]``；
    含白名单外字段（``auto_apply`` 等）**合法**——字段级过滤，值不参与合并、声明其余部分仍生效。

    Parse the server-declared ``a2c_tool_meta`` (native dict in ``Tool._meta``, NOT a JSON string):
    malformed = non-object or ``tags`` not ``list[str]``; extra whitelist-external fields are fine (field-level filter).
    """
    if not isinstance(raw, dict):
        return None, "声明非对象"
    raw_tags = raw.get("tags")
    if raw_tags is None:
        # 缺失或显式 null：合法声明但无 tags → 继承下一层。
        return _ServerDeclaredToolMeta(tags=None), None
    if isinstance(raw_tags, list) and all(isinstance(t, str) for t in raw_tags):
        return _ServerDeclaredToolMeta(tags=raw_tags), None
    return None, "tags 非 list[str]"


# SKILL 资源枚举翻页安全上界：防御恒非空 cursor（server bug / 恶意）导致的无限循环挂死物化。
# Pagination safety bound for SKILL enumeration: guard against a never-terminating cursor hanging staging.
_MAX_SKILL_LIST_PAGES = 1000

# #179 bounded connect：匿名/恢复路径的 connect 竞速上界（Rust CONNECT_TIMEOUT_SECS）。
# 交互式路径不套此界——生命周期由 pending flow TTL（10 分钟）+ 取消链约束。
_CONNECT_TIMEOUT = 30.0

# #208：启动事务的 raw 声明在渲染窗口内被改写时的**重试上限**。每次重试都要重跑一次 materialize
# （可能含交互提示），故给一个宽松但**有界**的上限；耗尽则放弃本次启动（见 `_astart_client`）。
_MAX_MATERIALIZE_RETRIES = 8

# automatic-only（Rust #180）：无显式 oauth 配置时也按 challenge 准入，参数从 metadata 派生。
# 私有变体类型不跨界——经 oauth_types 公开工厂获取默认选项（🟡7）。
_DEFAULT_OAUTH_OPTIONS = default_oauth_options()

# OAuthRequired 错误的稳定形态（宿主经 facade 驱动授权；auto 启动路径吞掉并保留 activation）。
# 与 coordinator redirect_handler 的 OAuthError.protocol(AuthorizationRequired) 同源——
# 单一权威经 classmethod 派生，防两文件文案漂移（#179 隔离审查 🔵9）。
_OAUTH_REQUIRED_MSG = OAuthError.protocol(OAuthProtocolError.AuthorizationRequired).message


def _raise_oauth_required() -> NoReturn:
    raise OAuthError.protocol(OAuthProtocolError.AuthorizationRequired)


def _is_oauth_required_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, OAuthError)
        and exc.code == OAuthErrorCode.Protocol
        and exc.message == _OAUTH_REQUIRED_MSG
    )


def _bump_active_client_generation(generations: dict[BUNDLE_ID, int], bundle_id: BUNDLE_ID) -> None:
    """per-bundle 世代 +1（ABA 检测计数器；Rust ``bump_active_client_generation`` 的 python 面）。

    #185：每次 `_active_clients` 插入/移除/替换都必须 bump——同一 client 对象被
    remove 再 reinsert 时，仅靠对象身份（``is``）无法检出 ABA 变化，generation 补齐。
    """
    generations[bundle_id] = generations.get(bundle_id, 0) + 1


def _bearer_challenge_parts(www_authenticate: str | None) -> tuple[str | None, str | None]:
    """拆解 WWW-Authenticate challenge（#185 精确分类）：返回 ``(scheme, resource_metadata_url)``。

    - 非 Bearer challenge → ``(None, None)``（Rust ``ChallengeAdmission::Unsupported`` 面）；
    - Bearer 但无 ``resource_metadata`` → ``(scheme, None)``（Rust ``BearerWithoutMetadata`` 面，
      即 ``HttpAuthenticationError::OAuthDiscoveryFailed`` 语义）。
    """
    if not www_authenticate:
        return None, None
    scheme, _, rest = www_authenticate.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None, None
    return scheme, parse_bearer_resource_metadata(rest)


def _has_static_authorization_header(config: MCPServerConfig) -> bool:
    """配置是否携带静态 Authorization header（#181 static-only 判据）。

    Rust #180 移除显式 OAuth 配置后，带静态 Authorization 的 HTTP server 走 static-only
    路径——其 401 是静态凭据失败，绝不触发 OAuth（两条路径天然分离，无需互斥检测）。
    """
    if not isinstance(config, StreamableHttpServerConfig):
        return False
    headers = config.server_parameters.headers
    if not headers:
        return False
    return any(name.lower() == "authorization" for name in headers)


def _is_secure_endpoint(config: MCPServerConfig) -> bool:
    """protected resource 端点是否 HTTPS 或 loopback HTTP（#181 安全不变量 1）。

    与 :func:`validate_secure_url` 同判据——准入失败分类时以 bool 面复用（不抛异常）。
    """
    if not isinstance(config, StreamableHttpServerConfig):
        return False  # pragma: no cover — 调用方已按 _oauth_spec 收窄
    try:
        validate_secure_url(str(config.server_parameters.url))
        return True
    except OAuthError:
        return False


def _tool_projection_fingerprint(tool: Tool) -> str:
    """工具定义的**全字段**规范化指纹（#197）：含 name / description / inputSchema / annotations / meta。

    Full-field canonical fingerprint of a tool definition (#197). 同名换 schema 也能被检出——拒绝
    name-only 捷径（对齐 Rust PR #199 的 projection 比对）。``exclude_none`` 保证「字段由 None 变为实际值」
    同样计入变化；``sort_keys`` 抹平 dict 键序抖动，避免同一投影因序列化顺序被判成变化。
    """
    return json.dumps(
        tool.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _projection_excluding_failed(
    projection: dict[EXPOSED_TOOL_NAME, str],
    failed: set[BUNDLE_ID],
) -> dict[EXPOSED_TOOL_NAME, str]:
    """剔除失败 bundle 的投影条目（#197）：临时故障不得被读成「工具被移除」。

    Drop projection entries owned by failed bundles so a transient ``list_tools`` failure is never
    mistaken for a real capability change — mirrors Rust's "a failure must not masquerade as a tool
    removal or publish a false capability revision".

    归属以 **exposed 名前缀**（``exposed = {bundle_id}__{tool}``，bundle_id 无连续 ``__`` 保证单射）判定，
    **不**经路由表反查：失败 bundle 的**携带条目**在提交后已不在路由表中（路由被摘掉，而投影条目为防
    「失败 → 恢复」抖动被有意保留），反查会把它们误判为他人所有 —— 于是「连续失败」与「失败 → 恢复」
    各产生一次虚假 revision。
    """
    if not failed:
        return projection
    prefixes = tuple(f"{bundle}__" for bundle in failed)
    return {key: value for key, value in projection.items() if not key.startswith(prefixes)}


class ToolNameDuplicatedError(Exception):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)


MaterializeFunc = Callable[[BUNDLE_ID, MCPServerConfig], Awaitable[MCPServerConfig]]
"""#192 / §5.13：raw 声明 → rendered config 物化回调（Computer 注入）。

``(bundle_id, raw) -> rendered``——restart-with-update 场景传入**新** raw（尚未登记进 Computer._active_raw），
故 raw 显式作参数而非按 bundle_id 回查；scope（§5.11 plugin 上下文）由回调方自行按 bundle_id 取挂载时登记值。"""


class StartOutcome(NamedTuple):
    """批量启动的逐项结果（#208）。``error is None`` = 该项成功。

    对齐 rust ``start_mcp_clients_batch`` 的 ``Vec<(BundleId, ComputerResult<()>)>``：结果是
    **输入序**的（非完成序），失败是**值**而非中断——单项失败不影响其他项。

    / Per-item outcome of a batch start, in input order; a failure is a value, never an abort.
    """

    bundle_id: BUNDLE_ID
    error: Exception | None


class MCPServerManager:
    """
    MCP Server管理器

    所有以下划线开头的私有方法是非协程安全的。如果外部调用，需要使用普通方法。

    # #208 锁层次（**单向，不可逆序**）/ Lock hierarchy (strictly one-way):

        start_gate permit  →  per-bundle 生命周期锁  →  状态锁 ``_lock``

    推论（改本文件时**必须**遵守）：
    1. **不得**在持有状态锁 ``_lock`` 时去取门许可或 per-bundle 锁——那会与「bundle 锁 → 状态锁」
       的正常路径构成环形等待（死锁）。
    2. 状态锁的语义已收窄为**纯状态临界区**：**不得跨越任何慢 I/O**（用户提示 / ``aconnect`` /
       ``adisconnect``）、**不得跨越**任何门/bundle 锁的获取。**两处明示例外**（皆为承重，勿擅自
       「修掉」）：(a) :meth:`_arefresh_tool_mapping` 的全量 ``list_tools``；(b)
       :meth:`_reject_commit` 的退役 ``adisconnect``（罕见拒绝路径，保持既有原子的
       「校验→处置→上抛」形状）。
    3. per-bundle 锁覆盖**完整启动事务**（materialize → spawn → commit），使「同 bundle 双开」
       与「启动中移除/停止」由**结构**排除，而非靠 epoch 补偿。
    4. 唯一例外：:meth:`_arefresh_tool_mapping` 在状态锁内做全量 ``list_tools``。其无界重试的
       收敛性**恰恰依赖**「持有状态锁期间没有别的协程能改 ``_active_clients``」——故它**绝不**
       得从无锁调用点被调用（见该方法的 docstring）。

    / The state lock is a state critical-section lock, not a lifecycle gate; the start gate and
    per-bundle locks sit outside it, acquired in that order and never in reverse.

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
        oauth_credential_store: OAuthCredentialStore | None = None,
        materializer: MaterializeFunc | None = None,
        mcp_start_concurrency: int | None = None,
    ) -> None:
        # 存储所有服务器配置，以 bundle_id 为唯一身份键（协议 #15/#18：身份=bundle_id，name 降纯 display 不做键）
        # #192 / §5.13：``_servers_config`` 为「当前配置」（挂载时=raw 声明、materialize 后=rendered 进程配置，
        # 单店语义保持既有消费点不变）；``_servers_config_raw`` 为 **raw 声明店**（materialize 的解析源——
        # 每次实际启动 MUST 从 raw 重解析，rendered 品绝不作为解析结果来源）。
        # standalone manager（materializer=None）调用方传入即已渲染，两店同值。
        # Server configs keyed by bundle_id (unique identity; name is pure display, never a key — protocol #15/#18).
        self._servers_config: dict[BUNDLE_ID, MCPServerConfig] = {}
        self._servers_config_raw: dict[BUNDLE_ID, MCPServerConfig] = {}
        # #192 / §5.13：raw → rendered 物化回调（由 Computer 注入，闭包读其 _active_raw 与 §5.11 scope）。
        self._materializer: MaterializeFunc | None = materializer
        # 活动客户端 {bundle_id: client}
        self._active_clients: dict[BUNDLE_ID, MCPClientProtocol] = {}
        # #184: 已接受的启动意图（不因 OAuth 未授权或连接失败而丢失）
        # Accepted activation intents; not lost on OAuth or connect failure.
        self._activation_intents: set[BUNDLE_ID] = set()
        # #184: 最近一次数据面连接状态（与 _activation_intents 正交）
        # Latest data-plane connection state; orthogonal to activation_intents.
        self._connection_states: dict[BUNDLE_ID, MCPServerConnectionState] = {}
        # ExposedToolMapping：exposed_tool_name -> (bundle_id, 原始工具名)。list_tools 与 tool_call **共用同一份**表
        # （协议 §ExposedToolMapping）。exposed = {bundle_id}__{alias ?? 原始名}，bundle_id 无 `__` 保证单射→查表不 split。
        # 被 forbidden 的工具**不进本表**（不可见不可调用）；跨 bundle_id 天然唯一，无需跨 server 对账。
        self._exposed_tools: dict[EXPOSED_TOOL_NAME, tuple[BUNDLE_ID, TOOL_NAME]] = {}
        # #197：暴露工具投影指纹（exposed_tool_name → 工具定义规范化 JSON，见 _tool_projection_fingerprint）。
        # 与 _exposed_tools（只有名字映射）不同，本表含 description / inputSchema / annotations / meta，
        # 故「同名换 schema」也能被检出——``Computer.capability_revision`` 的变化判据即基于本表比对。
        # Projection fingerprint (#197); the change predicate behind Computer.capability_revision.
        self._tool_projection: dict[EXPOSED_TOOL_NAME, str] = {}
        # #185：per-bundle 活跃 client 世代计数（ABA 检测）——每次 _active_clients 插入/移除/替换 +1。
        # available_tools 发布前与 _arefresh_tool_mapping 提交前按「client 身份 + generation」二次校验，
        # 同一 client 对象被 remove 再 reinsert（ABA）也能被检出（Rust active_client_generations 的 python 面）。
        self._active_client_generations: dict[BUNDLE_ID, int] = {}
        # #185：per-bundle clear epoch——clear_oauth 的零 await 快速段每次 +1。_astart_client 入口捕获、
        # _commit_active_client 提交前比对：clear 在 start 连接 RPC 在途时发生 → 提交被拒（凭据已撤销），
        # 弥补 python 单一全局锁无法复刻 Rust per-bundle lifecycle lock 的 start-vs-clear 串行化。
        self._oauth_clear_epochs: dict[BUNDLE_ID, int] = {}
        # 自动重连标志
        self._auto_reconnect: bool = auto_reconnect
        # 自动连接标志
        self._auto_connect: bool = auto_connect
        # 自定义消息处理器，透传到各具体Client
        self._message_handler: MessageHandlerFnT | None = message_handler
        # 内部锁防止并发修改（#208：语义收窄为**状态临界区**——不得跨越慢 I/O，见类 docstring 的锁层次）
        self._lock = asyncio.Lock()
        # #208：Computer 级启动并发门（单启 / 批量 / 治理恢复共享同一把）。未配置 = 不限流但仍计数，
        # 批量驱动据 ``max is None`` 走既有逐项串行语义（分叉在**驱动**、不在门，对齐 rust）。
        # 门装在 **manager** 而非 Computer：``Computer.boot_up`` 每次新建 manager，故 boot 回滚的
        # ``aclose()``（会关闸）随 manager 一起废弃，重试拿到全新门——不会把 Computer 永久关死。
        self._start_gate = McpStartGate(mcp_start_concurrency)
        # #208：per-bundle 生命周期锁——覆盖该 bundle 的**完整启动事务**（materialize → spawn → commit），
        # 并被 _astop_client / aremove_server / _arestart_server 同键获取。作用 = 结构性排除
        # 「同 bundle 双开（两个进程）」与「启动中移除/停止后被复活」，无需 epoch 补偿。
        # ⚠️ 本表**绝不**由 _clear_all 清理：中途换表会让「持锁者 A」与「新取锁者 B」并存，
        # 双开静默复开。换表只发生在 ainitialize（先 drain 再换，见该方法的「新一代」语义）。
        self._bundle_locks: dict[BUNDLE_ID, asyncio.Lock] = {}
        # #179 OAuth 接线：可注入凭据 store（构造参数或 builder；默认进程内）、per-bundle
        # coordinator 注册表（challenge 准入后单例）、per-bundle OAuthFlow handle 注册表、
        # 在途 connect 任务。
        self._oauth_credential_store: OAuthCredentialStore = (
            oauth_credential_store or InMemoryOAuthCredentialStore()
        )
        self._oauth_coordinators: dict[BUNDLE_ID, OAuthCoordinator] = {}
        self._oauth_flows: dict[BUNDLE_ID, OAuthFlow] = {}
        self._oauth_connect_tasks: dict[BUNDLE_ID, asyncio.Task[None]] = {}

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

        #208：**收集后再启动**（对齐 rust ``reconcile_governance`` 的 collect-then-batch）——
        登记循环只做纯登记（不解析 input、不启动），全部登记完成后才经统一启动器逐项启动，
        使「单个启动 / 批量启动 / 治理恢复」共享同一把 Computer 级并发门。

        **新一代语义**：``aclose()`` 对本 manager 是终态（门已关），而本方法对**同一 manager**
        重新开始一代——故先关闸收敛在途、停清旧态，再**安装全新门与全新 bundle 锁表**。
        （``test_astart_all_same_tool_coexist`` 钉死此语义：aclose → ainitialize → astart_all 仍能启动。）

        Args:
            servers (list[MCPServerConfig]): MCP服务器配置

        Raises:
            Exception: 逐项启动后的**首个**错误（boot 回滚依赖此上抛，对齐 rust「全部尝试 → 报首错」）。
        """
        # 1. 终态收敛：关闸（排队/新启动即刻失败）→ 等在途事务收敛（**不得持任何锁**）
        self._start_gate.close()
        await self._start_gate.drain()
        # 1b. detached OAuth connect 任务结算——与 :meth:`aclose` **对称**，不可省：
        #     这些任务**不占门计数**，`drain()` 收敛不到它们。宿主正在等交互式 OAuth flow 时调本方法，
        #     一个停在 `_commit_active_client` 状态锁上的 detached 任务会被 `_astop_all` 逐项释放锁的
        #     间隙唤醒并于其间提交（此刻 `_servers_config` 未清、epoch 未变、移除守卫亦通过）——
        #     它不在 `_astop_all` 的快照里 ⇒ **漏停**，而 `_clear_all` 只丢引用不 disconnect ⇒ 进程泄漏。
        await self._asettle_oauth_connect_tasks()
        # 2. 停止所有活动客户端 + 清空所有状态存储（两者之间不得有 await：清空不 disconnect，
        #    任何在停止与清空之间提交的 client 都会被静默丢弃 ⇒ 进程泄漏）
        #    ⚠️ 不得在 ``self._lock`` 内调用：``_astop_all`` **自取**状态锁（asyncio.Lock 不可重入 ⇒ 自死锁）
        await self._astop_all()
        self._clear_all()
        # 3. 新一代：换门 + 换 bundle 锁表（drain 已保证无在途持锁者，故换表安全）
        self._start_gate = McpStartGate(max=self._start_gate.max)
        self._bundle_locks = {}
        # 4. 纯登记（no-double-open，加载期 first-wins）：按配置顺序 per-bundle_id 保留**首个**，
        #    其余作 Computer 本地诊断（WARN，非协议错误码）。同 bundle_id = 同软件，任一时刻只一个。
        #    (protocol §no-double-open, boot=first-wins). Runtime add/update is update-in-place (see _add_or_update).
        seen_bundle_ids: set[BUNDLE_ID] = set()
        to_start: list[BUNDLE_ID] = []
        async with self._lock:
            for server in servers:
                bundle_id = resolve_bundle_id(server)
                if bundle_id in seen_bundle_ids:
                    logger.warning(
                        f"no-double-open: 重复 bundle_id '{bundle_id}'（name={server.name!r}）——保留配置顺序首个、"
                        f"跳过此项（Computer 本地诊断，非协议错误码）；如需多实例请显式指定不同 bundle_id。",
                    )
                    continue
                seen_bundle_ids.add(bundle_id)
                self._servers_config[bundle_id] = server
                self._servers_config_raw[bundle_id] = server
                if self._auto_connect:
                    to_start.append(bundle_id)
        # 5. 锁外统一启动（auto 语义：OAuthRequired 吞掉、其余上抛；失败不阻塞其余项）
        if to_start:
            await self._araise_first_error(await self._astart_clients_batch_auto(to_start))
        async with self._lock:
            await self._arefresh_tool_mapping()

    async def _add_or_update_server_config(self, config: MCPServerConfig, *, start: bool = True) -> None:
        """
        添加/更新服务器配置

        如果已存在，检查是否已经建立客户端连接，如果是，检查是否需要自动重连
        如果不存在，直接添加配置

        #208：**决策与动作同持门 + per-bundle 锁**（由 :meth:`astart_client` / :meth:`_arestart_server`
        内部转发到 ``_locked`` 变体，不在本层二次取锁）。这一点是承重的——若决策只取状态锁，
        决策与动作之间就会对在途启动事务敞开窗口，产生「更新被旧配置覆盖 / 进程仍是旧配置而两店是新配置 /
        restart 写回 clobber 并发更新」整族静默错误（隔离审查 🔴-A/🔴-B/🟡-C）。同持锁后，
        更新与同 bundle 的启动/重启/停止/移除**全序化**。

        Args:
            config (MCPServerConfig): MCP服务器配置
            start: ``False`` = 只登记不启动（治理恢复的 collect-then-batch；#208）。
                ⚠️ 对**已激活**且 ``auto_reconnect=True`` 的 bundle，该分支刻意「不预写新声明」
                （预写是 restart 成功后才做的），故 ``start=False`` 在此**登记不了**任何东西——
                已改为 fail-fast 而非静默 no-op。需要更新运行中的 server 请用缺省 ``start=True``。

        Raises:
            RuntimeError: 已激活 + ``auto_reconnect=False`` 时更新配置（须先 stop）；
                ``start=False`` 命中已激活 + ``auto_reconnect=True`` 时（避免「只挂载」被静默误读为已登记）。
            McpStartGateClosed: 门已关（shutdown / ainitialize 换代中）——本入口同样受 Computer 级门控。
        """
        bundle_id = resolve_bundle_id(config)
        action: str | None = None
        # 门 → bundle 锁（严格单向；两个动作在 `_locked` 变体里执行，不再取锁）
        async with await self._start_gate.acquire():
            async with self._bundle_lock(bundle_id):
                action = await self._adecide_add_or_update(config, bundle_id, start=start)
                if action == "restart":
                    await self._arestart_server_locked(bundle_id, config)
                elif action == "start":
                    await self._astart_client_auto_locked(bundle_id)

    async def _adecide_add_or_update(self, config: MCPServerConfig, bundle_id: BUNDLE_ID, *, start: bool) -> str | None:
        """决策相：判分支 + 登记声明，返回待执行动作（``"start"`` / ``"restart"`` / ``None``）。

        **须已持门许可 + 该 bundle 的生命周期锁**（由本类唯一调用方 ``_add_or_update_server_config`` 保证；
        决策必须与动作同锁，见该方法的说明）。
        """
        action: str | None = None
        async with self._lock:
            if bundle_id in self._servers_config:
                # 运行期同 bundle_id = **原地更新**（intentional replace；name 可变、bundle_id 稳定），不算 no-double-open 冲突
                # Runtime same bundle_id = update-in-place (protocol §no-double-open runtime branch).
                if bundle_id in self._active_clients:
                    if self._auto_reconnect:
                        # #192 / §5.13：**不预写**新 config——_arestart_server 先 materialize（失败旧配置/旧进程不动），
                        # 成功后才替换存储（运行中不热更新、失败不留下 raw/rendered 半态）。
                        if not start:
                            # #208：此分支**连新声明都不登记**（预写只在 restart 成功后发生），故
                            # `start=False` 落到这里 = 配置被静默丢弃，与「只登记不启动」的语义不符。
                            # fail-fast 让调用方看到真相（治理恢复路径由 existing_bundle_ids 前置挡住）。
                            raise RuntimeError(
                                f"Server bundle_id={bundle_id!r} (name={config.name!r}) is active; start=False "
                                "cannot register a new declaration for a running server — use start=True (restart) "
                                "or stop it first",
                            )
                        action = "restart"
                    else:
                        raise RuntimeError(
                            f"Server bundle_id={bundle_id!r} (name={config.name!r}) is active. Stop it before updating config",
                        )
                else:
                    # 配置存在但客户端未激活，更新配置并根据 auto_connect 决定是否启动
                    # Config exists but client is not active, update config and start if auto_connect is enabled
                    self._servers_config[bundle_id] = config
                    self._servers_config_raw[bundle_id] = config
                    # #179：transport 类型切换（streamable→stdio/sse）→ 退役陈旧 OAuth
                    # 运行时态（coordinator/flow/connect task），勿沿用旧 server_url/store。
                    if self._oauth_spec(config) is None and bundle_id in self._oauth_coordinators:
                        self._retire_oauth_bundle(bundle_id)
                    if self._auto_connect and start:
                        action = "start"
            else:
                self._servers_config[bundle_id] = config
                self._servers_config_raw[bundle_id] = config
                if self._auto_connect and start:
                    action = "start"
        return action

    async def _astart_client_auto(self, bundle_id: BUNDLE_ID) -> None:
        """auto 路径启动（配置挂载/重启触发）：OAuthRequired 吞掉并记录（activation 保留），
        其余错误照常上抛——单 server 显式 :meth:`astart_client` 仍向调用方传播 OAuthRequired。
        """
        async with await self._start_gate.acquire():
            async with self._bundle_lock(bundle_id):
                await self._astart_client_auto_locked(bundle_id)

    async def _astart_client_auto_locked(self, bundle_id: BUNDLE_ID) -> None:
        """auto 路径启动主体。**须已持门许可 + 该 bundle 的生命周期锁**（由调用方获取；#208）。

        ⚠️ 不可重入：持锁调用方（``_add_or_update_server_config``）须调本变体而非 :meth:`_astart_client_auto`。
        """
        try:
            await self._astart_client_locked(bundle_id)
        except OAuthError as exc:
            if not _is_oauth_required_error(exc):
                raise
            logger.info(
                f"Server bundle_id={bundle_id!r} requires OAuth authorization "
                f"(state=Started+AuthorizationRequired); host drives the flow via "
                f"create_oauth_flow/complete_oauth.",
            )

    async def aadd_or_aupdate_server(self, config: MCPServerConfig, *, start: bool = True) -> None:
        """添加或更新服务器配置。运行期同 ``bundle_id`` = **原地更新**（不算 no-double-open 冲突）。

        #208：启动动作在 :meth:`_add_or_update_server_config` 内部于**锁外**执行（门 + bundle 锁），
        故本入口不再包状态锁。

        Args:
            start: ``False`` = **只登记不启动**（#208 治理恢复的 collect-then-batch：调用方自行在
                挂载全部完成后经 :meth:`astart_clients_batch` 统一启动）。缺省 ``True`` = 既有语义。
        """
        await self._add_or_update_server_config(config, start=start)
        async with self._lock:
            await self._arefresh_tool_mapping()

    async def aremove_server(self, bundle_id: BUNDLE_ID) -> None:
        """按 bundle_id 移除服务器配置 / Remove a server config by bundle_id。

        #208：整个移除在**该 bundle 的生命周期锁**内进行 ⇒ 与同 bundle 的在途启动事务串行化，
        不存在「移除后启动事务提交回来把 client 复活」的窗口。
        """
        async with self._bundle_lock(bundle_id):
            if bundle_id in self._active_clients:
                await self._astop_client_locked(bundle_id)
            # #179：退役 OAuth 运行时态（coordinator/flow/connect task）——配置移除后
            # 再以同 bundle_id 挂回不同 transport 时不得沿用陈旧 coordinator。
            async with self._lock:
                self._retire_oauth_bundle(bundle_id)
                del self._servers_config[bundle_id]
                self._servers_config_raw.pop(bundle_id, None)
            async with self._lock:
                await self._arefresh_tool_mapping()

    async def _arestart_server(self, bundle_id: BUNDLE_ID, config: MCPServerConfig) -> None:
        """重启服务器客户端（按 bundle_id）。``config`` = 调用方提供的新 raw/rendered 声明（update-in-place 入参）。

        §5.13（#192，对齐 rust ``restart_client_by_id_materialized``）：**先 materialize**——失败即上抛，
        旧进程与旧配置**不动**（尽量保留仍在运行的旧进程）；成功后才 stop 旧 → 以新 rendered spawn
        （不二次 materialize，command 不会重复执行）。

        #208：与单启同门同锁（rust ``restart_mcp_client`` 同样过 gate → per-bundle lock），
        故 restart 不得游离于 Computer 级上限之外。
        """
        async with await self._start_gate.acquire():
            async with self._bundle_lock(bundle_id):
                await self._arestart_server_locked(bundle_id, config)

    async def _arestart_server_locked(self, bundle_id: BUNDLE_ID, config: MCPServerConfig) -> None:
        """重启主体。**须已持门许可 + 该 bundle 的生命周期锁**（由调用方获取；#208）。

        与 :meth:`_astart_client_locked` 同理由：让更新路径能在同一对锁下「先决策、再 restart」，
        避免 restart 的写回覆盖并发更新（隔离审查 🟡-C：C 的更新被 A 的 restart 写回静默丢弃）。

        ⚠️ 不可重入：持锁调用方不得再经 :meth:`_arestart_server`。
        """
        if config.disabled:
            # 停用配置：仅停止旧进程（对齐 rust disabled → stop，不 materialize），两店存新声明供状态面读取。
            if bundle_id in self._active_clients:
                await self._astop_client_locked(bundle_id)
            async with self._lock:
                self._servers_config[bundle_id] = config
                self._servers_config_raw[bundle_id] = config
            return

        rendered = await self._amaterialize(bundle_id, config)
        if bundle_id in self._active_clients:
            await self._astop_client_locked(bundle_id)
        async with self._lock:
            self._servers_config[bundle_id] = rendered
            self._servers_config_raw[bundle_id] = config  # 新 raw 声明（下一次实际启动的重解析源）
        # OAuthRequired 吞掉（restart 属配置变更触发的自动动作，与 auto 路径同语义）
        try:
            await self._aspawn_client(bundle_id, rendered, self._oauth_clear_epochs.get(bundle_id, 0))
        except OAuthError as exc:
            if not _is_oauth_required_error(exc):
                raise
            logger.info(
                f"Server bundle_id={bundle_id!r} requires OAuth authorization "
                f"(state=Started+AuthorizationRequired); host drives the flow via "
                f"create_oauth_flow/complete_oauth.",
            )

    async def _araise_first_error(self, outcomes: Iterable["StartOutcome"]) -> None:
        """把批量逐项结果收敛为「首错上抛」（对齐 rust ``start_all_mcp_clients`` 的聚合口径）。

        **全部项都已尝试**——本方法只决定向调用方报哪个错误，不决定谁被启动。

        / Converge per-item outcomes to the first error (all items were already attempted).
        """
        for outcome in outcomes:
            if outcome.error is not None:
                raise outcome.error

    async def _astart_one(self, bundle_id: BUNDLE_ID) -> None:
        """批量驱动的**逐项体**（刻意**不**挂 ``@restores_cancellation``）。

        子任务各自持有 ``_scope_depth() == 0``，若挂装饰器会在子任务内自行还原取消，把成功变成
        调用方无法归因的 ``CancelledError``。取消的还原只发生在最外层公开入口（见 cancellation 模块）。

        / The per-item body of the batch driver; deliberately undecorated (see cancellation module).
        """
        await self._astart_client_auto(bundle_id)

    async def _astart_clients_batch_auto(self, bundle_ids: Iterable[BUNDLE_ID]) -> list[StartOutcome]:
        """批量启动驱动（#208）：配置/未配置的**分叉在此**，不在门（对齐 rust）。

        - **未配置上限** ⇒ 逐项串行（与既有 ``astart_all`` 行为一致），仅「单项失败不阻塞后续项」。
        - **已配置** ⇒ 结构化并发（``create_task`` + ``asyncio.wait``），各项统一过门控；
          任意时刻总在途 ≤ 上限，重叠批次与单启交叉调用同样受约束。

        结果**保输入序**（非完成序）；失败是**值**（``StartOutcome.error``）而非中断——逐项失败绝不
        阻断其他项（协议批次接口部分失败语义，对齐 rust ``start_mcp_clients_batch``）。

        **刻意不用 ``asyncio.gather``**：gather 会把取消级联进子任务并弃置未 await 的子任务（子任务
        可能停在半开的 transport 上）。``asyncio.wait`` 不级联；被取消时先**收敛**（等子任务结算）
        再上抛，使最外层的 ``@restores_cancellation`` 拿到干净的传播链。

        / Batch driver: serial when unconfigured, structured-concurrent when configured; results in
        input order, failures as values, never cascading cancellation into children.
        """
        ids = list(bundle_ids)
        # 空集：**任何配置下**都必须返回空结果（不得落到下面的并发分支）。
        # ``asyncio.wait([])`` 在 Configured 分支会抛 ``ValueError: Set of Tasks/Futures is empty.``——
        # 而 ``astart_all()`` 在「未挂载任何 server / 全部 disabled」时正会走到这里（#208 前是零次循环
        # 的 no-op），故这是必须挡住的回归；rust ``join_all(&[])`` 同样返回空 Vec（双端口径一致）。
        if not ids:
            return []
        if self._start_gate.max is None:
            outcomes: list[StartOutcome] = []
            for bundle_id in ids:
                try:
                    await self._astart_one(bundle_id)
                except Exception as exc:  # noqa: BLE001 — 单项失败是**值**，不中断其余项
                    outcomes.append(StartOutcome(bundle_id, exc))
                else:
                    outcomes.append(StartOutcome(bundle_id, None))
            return outcomes

        tasks = [asyncio.create_task(self._astart_one(bid)) for bid in ids]
        try:
            await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
        except asyncio.CancelledError:
            # 取消**子任务**再结算（收敛而非弃置）：只 `wait` 不 `cancel` 会让入口被**无界等待**的子任务
            # 卡住（交互式 input 提示 / 用户挂着的 OAuth 可达分钟级），`wait_for` 到点不返回、CLI Ctrl-C
            # 在提示窗口内表现为「没反应」。先 cancel 再等它们各自收尾 —— 既保住「不弃置半开 transport」，
            # 又让收敛**有界**（对齐 rust：`join_all` 的 future 被 drop 即取消）。
            for task in tasks:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
            raise
        concurrent_outcomes: list[StartOutcome] = []
        for bundle_id, task in zip(ids, tasks, strict=True):
            if task.cancelled():
                # 子任务自身被取消：不是「该项失败」，须如实上抛（不得伪装成普通结果值）
                raise asyncio.CancelledError()
            failure = task.exception()
            if failure is not None and not isinstance(failure, Exception):
                # BaseException（KeyboardInterrupt / SystemExit…）：不是「该 server 启动失败」，
                # 如实上抛而非当成结果值（对齐「失败是值、中断不是值」的边界）
                raise failure
            concurrent_outcomes.append(StartOutcome(bundle_id, failure))
        return concurrent_outcomes

    @restores_cancellation
    async def astart_clients_batch(self, bundle_ids: Iterable[BUNDLE_ID]) -> list[StartOutcome]:
        """按输入序批量启动 MCP 客户端（#208，镜像 rust ``start_mcp_clients_batch``）。

        受 **Computer 级并发上限**约束（与单启、治理恢复共享同一把门，故重叠批次的总在途不超上限）；
        返回**每 bundle_id 的成败**，顺序与入参一致（稳定逐项结果序，非完成序）；单项失败**不阻塞**
        其他项。幂等由 :meth:`_astart_client` 在 materialize **之前**的活跃检查保证——重复启动不重解析
        Input、不重复 spawn（协议 §5.13）。

        **不做**「首错即停」，也**不上抛**单项失败——调用方按需从 :class:`StartOutcome` 取值。
        OAuthRequired 按 auto 语义吞并（与 ``astart_all`` 一致）；显式单启 :meth:`astart_client`
        仍向调用方传播该错误。

        Args:
            bundle_ids: 待启动的 bundle_id 序列（结果与之同序）。

        Returns:
            list[StartOutcome]: 逐项结果（输入序）；``error is None`` 表示该项成功。

        / Start the given bundle ids with the Computer-level ceiling; per-item results in input order.
        """
        logger.debug(f"Manager start batch async task: {asyncio.current_task()}")
        return await self._astart_clients_batch_auto(bundle_ids)

    # #211：CLI REPL 的 `start all` 直接 await 本入口（外层 `except Exception` 捕不到 CancelledError，
    # 吞掉取消会表现为「回车没反应」）；其子树经 client 状态机，故在此还原信号。
    @restores_cancellation
    async def astart_all(self) -> None:
        """启动所有启用的服务器

        #179：单个 server 的 OAuthRequired 不打断整体——逐 server 走 :meth:`_astart_client_auto`
        （单一吞并权威，🟡4）；其余错误照常上抛。单 server 显式启动（:meth:`astart_client`）
        仍向调用方传播。

        #208：改经统一批量驱动（受 Computer 级并发上限约束）。**语义变化**（对齐 rust
        ``start_all_mcp_clients``）：不再「首错即停」——**全部项都会尝试启动**，批次收敛后返回
        **输入序的第一个失败**（无失败则不抛）。
        """
        await self._araise_first_error(await self._astart_clients_batch_auto(self.enabled_bundle_ids()))

    # #211：同上（CLI `start <target>`）；私有 `_astart_client` 保持裸奔 —— 它位于逐 client 循环内，
    # 在此层补抛才会中断循环。
    @restores_cancellation
    async def astart_client(self, bundle_id: BUNDLE_ID) -> None:
        """启动单个服务器客户端（按 bundle_id）。

        #208：门与 per-bundle 锁在 :meth:`_astart_client` 内于最外层获取，故本入口不再包状态锁。
        """
        await self._astart_client(bundle_id)

    async def _amaterialize(self, bundle_id: BUNDLE_ID, raw: MCPServerConfig) -> MCPServerConfig:
        """§5.13（#192）：实际启动前把 raw 声明重解析为 rendered config。

        - standalone manager（无 materializer）→ 调用方传入即已渲染，原样返回（既有行为不变）。
        - 身份复核（对齐 rust ``replace_materialized_config``）：渲染不得改变 bundle_id 身份
          （raw/rendered 连接身份漂移 = 硬错误）。
        """
        if self._materializer is None:
            return raw
        rendered = await self._materializer(bundle_id, raw)
        resolved = resolve_bundle_id(rendered)
        if resolved != bundle_id:
            raise RuntimeError(
                f"materialized config identity changed from {bundle_id!r} to {resolved!r} — "
                f"raw/rendered bundle_id 漂移（渲染不得改变身份）"
            )
        return rendered

    async def _astart_client(self, bundle_id: BUNDLE_ID) -> None:
        """启动单个服务器客户端（按 bundle_id）。

        #179 OAuth bounded transaction（对齐 Rust #179，一次调用内完成）：
        1. 记录 activation intent（#184 语义：只显式 stop 才清除）
        2. 无 coordinator 的 Streamable HTTP server：**匿名 initialize 至多一次**，
           与 connect-phase challenge 信号竞速（30s 上界；aconnect 在 401 下会挂起，
           见 #133 实证）
        3. challenge（Bearer + resource_metadata + same-origin）→ 构造 coordinator =
           **凭据恢复 + 初始化**：restore 后 Authorized → 带 provider initialize（成功即
           连接）；Unauthorized + 宿主已注册 flow → 派发 detached connect 任务（交互式
           flow 的驱动者，**不持 manager 锁**——provider 在 callback 上阻塞可达分钟级）
        4. 真正缺用户授权（未注册 flow）→ 连接状态 AUTHORIZATION_REQUIRED + OAuthRequired
           错上抛（activation 保留）

        Raises:
            OAuthError: ``Protocol(authorizationRequired)`` —— 需要宿主经 facade 驱动授权；
                非 streamable 的 challenge / 非 challenge 失败按原异常传播。
        """
        # #208 锁层次（承重）：门 → per-bundle 生命周期锁 —— 事务全程持锁，commit 在锁内。
        # permit 覆盖**完整启动事务**（materialize → spawn → commit），故单启/批量/治理恢复共享同一上限；
        # bundle 锁使「同 bundle 双开」「启动中移除/停止」**以及「启动中配置更新」**三者由结构排除
        # ——后者是关键：更新路径同样先取门与 bundle 锁（见 :meth:`_add_or_update_server_config`），
        # 故它的「决策 + 登记 + 动作」与任何在途启动事务互斥，不存在交错窗口。
        # ⚠️ 门可能已关（shutdown/ainitialize）→ 排队者与后来者**即刻失败**，这正是验收标准 6。
        async with await self._start_gate.acquire():
            async with self._bundle_lock(bundle_id):
                await self._astart_client_locked(bundle_id)

    async def _astart_client_locked(self, bundle_id: BUNDLE_ID) -> None:
        """启动事务主体。**须已持门许可 + 该 bundle 的生命周期锁**（由调用方获取；#208）。

        拆出 ``_locked`` 变体是为了让**配置更新路径**（:meth:`_add_or_update_server_config`）能在同一
        对锁下「先决策、再执行动作」，从而与在途启动事务互斥——否则更新会落在启动的渲染/握手窗口里
        被静默丢弃或反向覆盖（隔离审查 🔴-A/🔴-B/🟡-C 同族）。

        ⚠️ 不可重入：持锁调用方不得再经 :meth:`_astart_client`（会二次取同一 ``asyncio.Lock`` ⇒ 自死锁）。
        """
        # ── phase A：状态临界区（纯内存读判，无 I/O await）─────────────
        for _attempt in range(_MAX_MATERIALIZE_RETRIES):
            async with self._lock:
                config = self._servers_config.get(bundle_id)
                if not config:
                    # 防御性分支：正常流程不会触发 / Defensive branch, not triggered in normal flow
                    raise ValueError(f"Unknown server bundle_id={bundle_id!r}")  # pragma: no cover

                if config.disabled:
                    raise RuntimeError(f"Cannot start disabled server bundle_id={bundle_id!r} (name={config.name!r})")

                # #184：先记录控制面启动意图——后续 OAuth challenge / transport error 只改 connection 状态，
                # 不清除 activation。只有显式 stop 才清除 / Record activation intent first; subsequent
                # OAuth or transport errors affect only connection state.
                self._activation_intents.add(bundle_id)
                # #185：捕获 clear epoch——后续各 commit 点比对，clear 在连接 RPC 在途时发生则提交被拒。
                clear_epoch = self._oauth_clear_epochs.get(bundle_id, 0)

                if bundle_id in self._active_clients:
                    self._connection_states[bundle_id] = MCPServerConnectionState.CONNECTED
                    return  # 已经启动（幂等：**早于** materialize ⇒ 不重解析 Input，协议 §5.13）

                # §5.13（#192，对齐 rust start_client_by_id_materialized）：从 stopped 实际启动 → 从 **raw 声明店**
                # materialize（rendered 品绝不作为解析结果来源；结构化错误上抛、不落 CONNECTING、不改任何状态），
                # 再以 rendered config spawn。standalone（无 raw 条目）回退当前配置（调用方传入即已渲染）。
                raw_in_store = self._servers_config_raw.get(bundle_id)
                raw = raw_in_store if raw_in_store is not None else config
            # ── phase B：锁外慢路径（用户提示 / 进程握手）——两侧锁仍持有 ──
            rendered = await self._amaterialize(bundle_id, raw)
            async with self._lock:
                if self._servers_config_raw.get(bundle_id) is raw_in_store:
                    self._servers_config[bundle_id] = rendered
                    break
            # 声明在渲染窗口内被换过（当下只可能来自**绕过本对锁**的路径，如 `_clear_all` 换代）——
            # 整轮重跑 phase A：**含 `disabled` 复校**。少校一步就会 spawn 一个「已被禁用」的声明
            # （隔离审查 🔴-A：禁用却运行，`start all` 面又看不到它）。
        else:  # pragma: no cover — 需宿主以高于一次 render 的频率持续改写声明
            logger.warning(
                f"bundle_id={bundle_id!r} 的声明在 {_MAX_MATERIALIZE_RETRIES} 次渲染窗口内持续变更，"
                f"放弃本次启动（排在 bundle 锁后的更新启动将接手最新声明）",
            )
            return
        await self._aspawn_client(bundle_id, rendered, clear_epoch)

    async def _aspawn_client(self, bundle_id: BUNDLE_ID, config: MCPServerConfig, clear_epoch: int) -> None:
        """以**已 materialize 的 rendered config** spawn（``_astart_client`` / ``_arestart_server`` 共用，不二次解析）。

        中文: 自「置 CONNECTING」起的既有 spawn 流程（OAuth bounded transaction 等）整体原样保留。
        English: Spawn with an already-materialized config; the existing flow from CONNECTING onward is unchanged.
        """
        # 设置连接状态为 connecting / Set connection state to connecting.
        self._connection_states[bundle_id] = MCPServerConnectionState.CONNECTING

        coordinator = self._oauth_coordinators.get(bundle_id)
        oauth_spec = self._oauth_spec(config)

        if oauth_spec is None:
            # ── 非 OAuth 通道（stdio / sse）：原 plain connect 路径 ──
            # 若存在陈旧 coordinator（运行期 transport 类型切换，如 streamable→stdio），
            # 先退役——否则下方 coordinator 分支会对 stdio client 调
            # connect_challenge_event()（不存在该面）直接 AttributeError。
            if coordinator is not None:
                self._retire_oauth_bundle(bundle_id)
            client = client_factory(config, message_handler=self._message_handler)
            try:
                await client.aconnect()
            except Exception:
                # 连接失败：保留 activation intent，仅更新 connection 状态（#184 语义）
                self._connection_states[bundle_id] = MCPServerConnectionState.ERROR
                raise
            await self._commit_active_client(bundle_id, client, clear_epoch)
            return

        if coordinator is not None and _has_static_authorization_header(config):
            # #181 static-only 配置切换窗口（二轮审查 🟡5）：bundle 先前经 challenge
            # 准入持有 coordinator，随后配置加入静态 Authorization header（仍 streamable
            # → 不退役）——退役陈旧 coordinator，让 anonymous-first 重走：静态凭据
            # 200 直连，或 401 → static-only 精确拒绝（绝不回退 OAuth）
            self._retire_oauth_bundle(bundle_id)
            coordinator = None

        if coordinator is None:
            # ── anonymous-first（每 client 生命周期至多一次） ──
            client = cast(
                HttpMCPClient, client_factory(config, message_handler=self._message_handler)
            )
            try:
                signal = await self._bounded_connect(client)
            except UpstreamRedirectStoppedError:
                # #181：跨 origin redirect 被安全守卫 stop（非授权错误）——ERROR + 上抛
                self._connection_states[bundle_id] = MCPServerConnectionState.ERROR
                raise
            if signal is None:
                await self._commit_active_client(bundle_id, client, clear_epoch)  # 匿名连通
                return
            # #181 static-only（Rust #180）：配置了静态 Authorization header 的 server
            # 永远走静态认证——401 即静态凭据被拒（4006 分类面），**绝不回退 OAuth**。
            # 两条认证路径天然分离，无需互斥检测逻辑。
            if _has_static_authorization_header(config):
                self._connection_states[bundle_id] = MCPServerConnectionState.ERROR
                raise OAuthError(
                    OAuthErrorCode.Protocol,
                    f"static Authorization header configured for bundle_id={bundle_id!r}; "
                    f"upstream rejected it with HTTP {signal.status_code} — "
                    "OAuth is never attempted for this server (static-only)",
                )
            coordinator = self._admit_oauth_coordinator(bundle_id, config, signal)
            if coordinator is None:
                # 非 Bearer challenge / cross-origin resource_metadata / 非安全端点 → 不准入。
                # 精确分类（#185，对齐 Rust HttpAuthenticationError）：challenge 非 Bearer →
                # UnsupportedChallenge 语义；Bearer 但 cross-origin 或非 HTTPS → 同语义。
                # 裸 Bearer（无 resource_metadata）**不再走本分支**——准入后 AS 发现交由
                # mcp ≥1.29 的 well-known 回退链（PRM path 插入/根 → 服务器 origin 的
                # RFC 8414 AS metadata）。均以 Protocol + 判别文案 surface（与
                # authorizationRequired 文案区分 → 不被 _astart_client_auto 吞掉）。
                scheme, _metadata_url = _bearer_challenge_parts(signal.www_authenticate_header)
                if not _is_secure_endpoint(config):
                    # #181：protected resource 端点非 HTTPS 且非 loopback——安全不变量 1
                    # 拒绝，消息与 cross-origin 区分（排障友好）
                    message = (
                        f"protected resource endpoint for bundle_id={bundle_id!r} must use "
                        "HTTPS (or loopback HTTP for development) — OAuth is never attempted "
                        "over plaintext HTTP"
                    )
                else:
                    message = (
                        f"Unsupported authentication challenge for bundle_id={bundle_id!r} "
                        f"(challenge scheme={scheme!r}, or resource_metadata cross-origin)"
                    )
                self._connection_states[bundle_id] = MCPServerConnectionState.ERROR
                raise OAuthError(OAuthErrorCode.Protocol, message)
            self._oauth_coordinators[bundle_id] = coordinator

        if coordinator is not None:
            status = await coordinator.restore_credentials()
            if status.state == "authorized":
                # 凭据恢复 → 带 provider 的 authenticated initialize（单次；401 → 凭据失效）
                client = cast(
                    HttpMCPClient,
                    client_factory(
                        config, message_handler=self._message_handler, oauth_coordinator=coordinator
                    ),
                )
                try:
                    signal = await self._bounded_connect(client, coordinator)
                except UpstreamRedirectStoppedError:
                    # #181：跨 origin redirect 被安全守卫 stop（非授权错误）——ERROR + 上抛
                    self._connection_states[bundle_id] = MCPServerConnectionState.ERROR
                    raise
                if signal is None:
                    await self._commit_active_client(bundle_id, client, clear_epoch)
                    return
                if signal.status_code == 401:
                    # 恢复的凭据已被服务端拒绝 → 清槽（generation bump + store 清空 +
                    # status→Unauthorized），否则每次重启/重 start 都 restore→401→
                    # OAuthRequired 死循环（Rust invalidate_credentials 语义）。
                    await coordinator.invalidate_credentials()
                else:
                    # 403 insufficient_scope 等：交给 observe 通道（#133 共享钩子）
                    await coordinator.observe_service_error(
                        signal.status_code, signal.www_authenticate_header
                    )
                self._connection_states[bundle_id] = MCPServerConnectionState.AUTHORIZATION_REQUIRED
                _raise_oauth_required()
            if coordinator.has_registered_request():
                # status() 在 Pending 下先行 expire 陈旧 flow（gen 不符 / PKCE 过 TTL），
                # 防「僵尸注册 → ghost flow 再派发」；fresh 时 no-op 返回 Pending。
                await coordinator.status()
                if coordinator.has_registered_request():
                    # 宿主已注册 flow → 交互式 connect 任务驱动 launch/complete（不阻塞本调用方）
                    self._ensure_oauth_connect_task(bundle_id, coordinator)
                    return

        # 真正缺用户授权：activation 保留，仅 connection 状态落 AUTHORIZATION_REQUIRED
        self._connection_states[bundle_id] = MCPServerConnectionState.AUTHORIZATION_REQUIRED
        _raise_oauth_required()

    async def _commit_active_client(self, bundle_id: BUNDLE_ID, client: MCPClientProtocol, clear_epoch: int | None = None) -> None:
        """登记活跃 client —— **本方法自取 ``_lock``**（#208）+ 刷新 ExposedToolMapping。

        #208：提交点由「调用方持锁」改为「自带锁」，使整个启动事务可以在**锁外**跑慢路径
        （materialize / aconnect）而不必把 130 行 OAuth 分支迷宫切成多相。锁内的部分**仍**含
        :meth:`_arefresh_tool_mapping`：其无界重试的收敛性依赖「持锁期间无他人改 ``_active_clients``」。

        #185：提交前做 clear epoch 校验——start 连接 RPC 在途时若 :meth:`clear_oauth` 已发生
        （凭据已撤销、clear 快速段已过），提交被拒并转 OAuthRequired。Rust 以 per-bundle
        lifecycle lock 串行化 start-vs-clear；python 单一全局锁由 clear 的**零 await 快速段**
        绕过（#185 关键不变量：clear 从不等待上游 MCP I/O），故以 epoch 补偿同一串行化语义。
        未传 ``clear_epoch`` 的调用方（无此竞态面）跳过校验。

        #208 新增守卫（既有缺陷根治）：``bundle_id not in _servers_config`` ⇒ 提交被拒。
        :meth:`_retire_oauth_bundle` 只 ``cancel()`` 而**不** bump epoch，故 detached 的
        :meth:`_aoauth_connect` 若已越过 coordinator 身份检查并停在锁上，会为**已移除**的 bundle
        写回 ``_active_clients``——「移除后复活」。该路径**不走** per-bundle 锁（交互式 flow 可阻塞
        分钟级，持锁会卡死 ``remove``/``restart``），故必须在此以状态守卫 fail-closed 兜住。
        """
        async with self._lock:
            if clear_epoch is not None and self._oauth_clear_epochs.get(bundle_id, 0) != clear_epoch:
                # 凭据已被并发 clear 撤销：不得登记 client。已建立的 transport 做 best-effort 退役
                # （隔离审查 🔴1：拒绝后 client 无处置即丢弃 → 连接泄漏 + keep-alive 任务悬挂）；
                # 断开失败仅 WARN、绝不吞掉 OAuthRequired 主异常。连接状态由 clear 快速段 commit
                # （AUTHORIZATION_REQUIRED / DISCONNECTED），此处不覆盖。auto 路径按 OAuthRequired
                # 吞掉、显式 start 向调用方传播。stdio 路径不可达（clear 对无 coordinator bundle 抛
                # NotConfigured → epoch 恒不失配），统一处置仅为防御。
                await self._reject_commit(bundle_id, client, "clear_oauth")
                _raise_oauth_required()
            if bundle_id not in self._servers_config:
                # 配置已被移除 / 已换代（aremove_server / _clear_all）——不得复活。
                await self._reject_commit(bundle_id, client, "bundle removed")
                _raise_oauth_required()
            self._active_clients[bundle_id] = client
            _bump_active_client_generation(self._active_client_generations, bundle_id)
            self._connection_states[bundle_id] = MCPServerConnectionState.CONNECTED
            # ExposedToolMapping 刷新不再抛跨 server 重名（bundle_id 前缀天然唯一），无需回滚
            await self._arefresh_tool_mapping()

    @staticmethod
    async def _reject_commit(bundle_id: BUNDLE_ID, client: MCPClientProtocol, reason: str) -> None:
        """提交被拒时的 best-effort 退役（**须持 ``_lock``**；断开失败仅 WARN，绝不吞主异常）。

        / Best-effort teardown of a rejected client; never swallows the primary error.
        """
        try:
            await client.adisconnect()
        except Exception:
            logger.warning(
                f"start for bundle_id={bundle_id!r} rejected by {reason}, and transport disconnect failed",
                exc_info=True,
            )

    async def _bounded_connect(
        self, client: HttpMCPClient, coordinator: OAuthCoordinator | None = None
    ) -> AuthSignal | None:
        """aconnect() 与 connect-phase challenge 信号（+ 可选 flow-aborted）竞速（30s 界）。

        ``coordinator is None`` = anonymous-first 尝试（信号通道可得 401/403）。
        ``coordinator`` 提供 = 带 provider 的 authenticated 尝试——关键差异（#179 实证）：
        auth-attached 的 401 由 httpx auth 机制在 ``stream()`` **内部**消费（provider
        全流程 inline，见上游 ``mcp/client/auth.py`` ``async_auth_flow``——无已知上游
        issue），connect-phase 信号通道**看不到**；死路径为 provider 流程死亡后 aconnect
        挂起（上游 ``mcp/client/streamable_http.py`` ``post_writer`` 异常吞没致请求侧
        不 resolve，#133 实证——无已知上游 issue）。故增竞速 ``flow_aborted``
        （redirect_handler 无注册请求时置位）：aborted 先到 ⇒ 恢复的凭据被 401
        challenge 拒绝（mcp auth 仅在 401 时跑全流程）⇒ 合成 401 信号。403
        （insufficient_scope 等）作为 final response 正常透过 → 信号通道可得。

        返回 challenge 信号（client 未登记、随 GC 弃置，**不得** ``adisconnect``
        未连上的 client——会递归 aconnect）；连通返回 None。

        #179 隔离审查 🔴3：try/finally 保证本协程以**任何方式**退出（含被外层取消）
        时内层任务全部回收——``asyncio.wait`` 不级联取消 pending 内层任务（asyncio
        语义，已最小复现证实），孤儿 aconnect 会以 #133 挂起模式永久泄漏 httpx 会话。

        Raises:
            TimeoutError: 30s 内既未连通也未收到 challenge。
            BaseException: 非 challenge 的 connect 失败原样上抛。
        """
        if coordinator is not None:
            # 本次 connect 为全新尝试：旧 flow 的 abort 标记与本次无关
            coordinator.flow_aborted_event().clear()
        connect_task = asyncio.create_task(client.aconnect())
        challenge_wait = asyncio.create_task(client.connect_challenge_event().wait())
        redirect_stop_wait = asyncio.create_task(client.connect_redirect_event().wait())
        aborted_wait = (
            asyncio.create_task(coordinator.flow_aborted_event().wait())
            if coordinator is not None
            else None
        )
        inner = [connect_task, challenge_wait, redirect_stop_wait] + (
            [aborted_wait] if aborted_wait is not None else []
        )
        try:
            try:
                done, _pending = await asyncio.wait(
                    set(inner),
                    timeout=_CONNECT_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except TimeoutError:
                raise TimeoutError(
                    f"connect neither succeeded nor received an auth challenge within {_CONNECT_TIMEOUT}s"
                ) from None

            if challenge_wait in done:
                signal = client.take_connect_challenge()
                return signal
            if redirect_stop_wait in done:
                # #181：安全守卫 stop 的跨 origin redirect——mcp 吞掉 3xx 异常致
                # aconnect 挂起（#133 同款）；立即解出为 typed error（非授权错误）
                status = client.take_connect_redirect_stop() or 0
                raise UpstreamRedirectStoppedError(status)
            if aborted_wait is not None and aborted_wait in done:
                # provider 全流程死亡 = 401 challenge 拒绝了恢复的凭据（无注册 flow 的
                # redirect_handler 抛 authorizationRequired）→ 合成 401 信号供调用方清槽
                return AuthSignal(401, None)
            # connect 先完成：成功 → None；失败 → 原异常上抛（未登记 client，随弃置）
            if connect_task in done and not connect_task.cancelled():
                exc = connect_task.exception()
                if exc is not None:
                    raise exc
            return None
        finally:
            for t in inner:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*inner, return_exceptions=True)

    def _oauth_spec(self, config: MCPServerConfig) -> tuple[str, OAuthOptions] | None:
        """Streamable HTTP → ``(server_url, effective options)``；非 streamable → None（无 OAuth 通道）。

        automatic-only（Rust #180）：无显式 ``oauth`` 配置也按 challenge 准入，
        options 落默认（AuthCode + DCR），scopes / resource 从 metadata 派生。
        """
        if not isinstance(config, StreamableHttpServerConfig):
            return None
        options = config.oauth if config.oauth is not None else _DEFAULT_OAUTH_OPTIONS
        return (str(config.server_parameters.url), options)

    def _admit_oauth_coordinator(
        self, bundle_id: BUNDLE_ID, config: MCPServerConfig, signal: AuthSignal
    ) -> OAuthCoordinator | None:
        """按 challenge 证据准入并构造 coordinator（automatic admission）。

        准入门槛（Rust auto-admission 的 python 面）：
        - Bearer challenge **携带** ``resource_metadata``：须与端点 same-origin（#185/#181）；
        - Bearer challenge **无** ``resource_metadata``（裸 challenge）：仍准入——AS 发现
          交由 mcp ≥1.29 的 well-known 回退链（PRM path 插入 / 根 → 服务器 origin 的
          RFC 8414 AS metadata）。回退链 URL 均由 mcp 从 server_url 派生（安全面自约束）；
          链上全部 404 时由 coordinator 的 discovery 校验 / fail_launch 收敛为 typed error。
        PRM / AS metadata 的深层校验由 mcp inline 流程 + #181 安全不变量承担。
        不准入返回 None（调用方落 ERROR + 报错）。

        #185：scheme 判据复用 :func:`_bearer_challenge_parts`（与调用方精确分类单一权威）——
        非 Bearer challenge 即便携带 resource_metadata 字样也不准入（#179 的 regex 搜索
        会把 ``Basic resource_metadata=...`` 误判为准入；两套判据分叉即分类死分支）。
        """
        scheme, metadata_url = _bearer_challenge_parts(signal.www_authenticate_header)
        if scheme is None:
            return None
        assert isinstance(config, StreamableHttpServerConfig)  # 调用方已按 _oauth_spec 收窄
        server_url = str(config.server_parameters.url)
        if metadata_url is not None and not same_origin(metadata_url, server_url):
            return None
        # #181：protected resource 端点 HTTPS-only（localhost/loopback http 豁免）——
        # 非 https 且非 loopback 的明文 server 不进 OAuth 通道（对齐 Rust validate_secure_url）
        try:
            validate_secure_url(server_url)
        except OAuthError:
            return None
        spec = self._oauth_spec(config)
        if spec is None:  # pragma: no cover — 防御分支
            return None
        _url, options = spec
        resource = options.resource or server_url
        return OAuthCoordinator(
            bundle_id=bundle_id,
            server_url=server_url,
            resource=resource,
            options=options,
            credential_store=self._oauth_credential_store,
        )

    def _ensure_oauth_connect_task(
        self, bundle_id: BUNDLE_ID, coordinator: OAuthCoordinator
    ) -> None:
        """确保至多一次带 provider 的 connect 尝试在途（交互式 flow 的 transport 驱动者）。

        detached task：provider 在 callback_handler 上可阻塞分钟级（等宿主 complete），
        **不得**持 manager 锁——complete_oauth 需要锁之外的 coordinator 锁即可推进。
        """
        existing = self._oauth_connect_tasks.get(bundle_id)
        if existing is not None and not existing.done():
            # 已在途（Rust get_or_try_init 单例语义）。但旧任务可能正随上一 flow 的
            # 终态失败收尾——若届时仍有 launch 等待者（终态后新 flow 已注册），
            # 续派发一次，防「旧任务刚好结束、新 launch 无 transport 驱动」挂起。
            def _rekick(_t: asyncio.Task[None]) -> None:
                if coordinator.launch_awaiting():
                    self._ensure_oauth_connect_task(bundle_id, coordinator)

            existing.add_done_callback(_rekick)
            return
        # #185：clear epoch 在 **dispatch 时**捕获（而非任务首轮调度时）——clear 的 cancel 链
        # 经 done callback 同步重派发（_rekick）后，本任务的首轮调度可能晚于 clear 快速段的
        # epoch bump；dispatch 时捕获才能让任务携带「意图创立时」的 epoch，供状态写点比对。
        clear_epoch = self._oauth_clear_epochs.get(bundle_id, 0)
        task = asyncio.create_task(self._aoauth_connect(bundle_id, coordinator, clear_epoch))
        self._oauth_connect_tasks[bundle_id] = task

        def _cleanup(t: asyncio.Task[None]) -> None:
            if self._oauth_connect_tasks.get(bundle_id) is t:
                self._oauth_connect_tasks.pop(bundle_id, None)

        task.add_done_callback(_cleanup)

    async def _aoauth_connect(
        self, bundle_id: BUNDLE_ID, coordinator: OAuthCoordinator, clear_epoch: int
    ) -> None:
        """交互式 OAuth connect（detached）：401 → mcp inline auth flow → 发布 URL → 等回调 → 交换 → 重试。

        生命周期由 pending flow TTL（10 分钟）+ 取消链约束，不套 30s 上界（用户授权可达分钟级）。
        成功 → 锁内 commit（epoch 守卫，见 :meth:`_commit_active_client`）；失败 → ``fail_launch``
        解 wait_launch 等待者 + 连接状态 ERROR——**两处状态写点均先比对 dispatch 时捕获的
        ``clear_epoch``**：clear 快速段已过（epoch 失配）则不写（隔离审查 🔴2：ghost 重派发
        任务不得覆盖 clear 的 committed 状态；真实时序 = clear 的 cancel 链触发 _rekick 重派发，
        任务首轮调度晚于 clear 快速段，其 aborted/fail 分支若照写 ERROR 会覆盖 AUTHORIZATION_REQUIRED）。
        """
        config = self._servers_config.get(bundle_id)
        if config is None:  # pragma: no cover — 防御分支
            return
        if self._oauth_coordinators.get(bundle_id) is not coordinator:
            # coordinator 已退役（transport 切换 / 配置移除）——ghost 任务守卫：
            # 勿以陈旧 coordinator 对旧端点建立连接 / 写连接状态（#179 隔离复核 🟡c）
            return
        client = cast(
            HttpMCPClient,
            client_factory(config, message_handler=self._message_handler, oauth_coordinator=coordinator),
        )
        connect_task = asyncio.create_task(client.aconnect())
        aborted_wait = asyncio.create_task(coordinator.flow_aborted_event().wait())
        redirect_stop_wait = asyncio.create_task(client.connect_redirect_event().wait())
        inner = [connect_task, aborted_wait, redirect_stop_wait]
        try:
            done, _pending = await asyncio.wait(
                set(inner),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if redirect_stop_wait in done:
                # #181 三轮审查 🔴：交互式路径的 redirect-stop 竞速——跨 origin redirect
                # 被安全守卫 stop 时（如 DCR 端点 3xx 跨 origin），mcp 的
                # _handle_registration_response 抛 OAuthRegistrationError → broad except
                # 吞掉 → aconnect 永不 resolve（#133 同款挂起）。以 typed error 收敛
                # launch 等待者（fail_launch 同步原子免锁）+ epoch 守卫写 ERROR（同
                # aborted 分支模式）。
                status = client.take_connect_redirect_stop() or 0
                async with self._lock:
                    if self._oauth_clear_epochs.get(bundle_id, 0) == clear_epoch:
                        coordinator.fail_launch(
                            OAuthError(
                                OAuthErrorCode.Protocol,
                                f"cross-origin redirect (HTTP {status}) stopped by "
                                "same-origin guard during interactive OAuth flow",
                            )
                        )
                        self._connection_states[bundle_id] = MCPServerConnectionState.ERROR
                return
            if aborted_wait in done:
                # flow 已终止/失败（cancel / expire / clear / complete-Terminated）：provider
                # 流程死亡后 mcp 请求侧挂起（#133 实证）——取消之，防 connect 任务泄漏、
                # 并让 done_callback 的 re-kick 能为终态后新 flow 续派发。
                # epoch 比对与状态写**同锁内同步段**（复核 R1）：比对在锁外时，锁获取
                # await 会给 clear 快速段（持久化 store 的 clear() 有让出点）留插入窗口，
                # 使陈旧比对结论覆盖 committed 状态。锁内重读 epoch 恒反映最新。
                async with self._lock:
                    if self._oauth_clear_epochs.get(bundle_id, 0) == clear_epoch:
                        self._connection_states[bundle_id] = MCPServerConnectionState.ERROR
                return
            if connect_task.cancelled():
                return  # pragma: no cover — 防御分支
            exc = connect_task.exception()
            if exc is not None:
                # 同 aborted 分支：比对 + fail_launch + 状态写同锁内同步段（复核 R1）。
                async with self._lock:
                    if self._oauth_clear_epochs.get(bundle_id, 0) == clear_epoch:
                        coordinator.fail_launch(
                            OAuthError(OAuthErrorCode.Protocol, _OAUTH_REQUIRED_MSG)
                        )
                        self._connection_states[bundle_id] = MCPServerConnectionState.ERROR
                return
            try:
                # #208：_commit_active_client **自取**状态锁（外层不得再包——asyncio.Lock 不可重入）
                await self._commit_active_client(bundle_id, client, clear_epoch)
            except OAuthError as commit_exc:
                if not _is_oauth_required_error(commit_exc):
                    # 非 OAuthRequired 的意外错误（当前 commit 链只抛 OAuthRequired，本分支
                    # 不可达属防御）：响亮上抛 + 异常日志——宁可未回收任务异常留痕，不静默吞
                    # 掉未来 commit 链引入的错误类别（detached 任务禁则的权衡依据）。
                    logger.exception(
                        f"oauth connect task for bundle_id={bundle_id!r} failed at commit "
                        "with unexpected OAuthError"
                    )
                    raise
                # #185：clear 在交互式 connect 在途时到达（cancel 链之外的窗口）→ epoch 守卫
                # 拒绝提交。本任务是 detached 的 fire-and-forget——异常不得成为未回收任务异常；
                # 使命已终结，静默收尾（不写状态：clear 快速段已 commit 正确状态）。
                logger.debug(
                    f"oauth connect task for bundle_id={bundle_id!r} rejected at commit: "
                    "credentials cleared concurrently (clear epoch mismatch)",
                )
        finally:
            # #179 隔离审查 🔴3：外层取消（clear_oauth / _clear_all cancel 本任务）不级联
            # 内层任务——finally 统一回收，防孤儿 aconnect 挂起泄漏 + 跨 flow 污染。
            for t in inner:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*inner, return_exceptions=True)

    async def _ensure_oauth_coordinator(self, bundle_id: BUNDLE_ID) -> OAuthCoordinator:
        """OAuthFlow.launch 入口：coordinator 存在即返回；否则跑一次 bounded connect 触发
        challenge 捕获与准入（OAuthRequired 属预期，吞掉后复查注册表）。

        Raises:
            OAuthError: ``NotConfigured`` —— 未准入（无 challenge 证据）。
        """
        coordinator = self._oauth_coordinators.get(bundle_id)
        if coordinator is not None:
            return coordinator
        try:
            # #208 锁序：**不得**在此包状态锁——``_astart_client`` 要取门与 bundle 锁，
            # 「持状态锁 → 取门/bundle 锁」与正常路径（门 → bundle → 状态锁）构成环形等待 ⇒ 死锁。
            # 改用 ``_astart_client_auto``（语义等价：同吞 OAuthRequired、其余上抛）。
            await self._astart_client_auto(bundle_id)
        except OAuthError as exc:
            if not _is_oauth_required_error(exc):
                raise
        coordinator = self._oauth_coordinators.get(bundle_id)
        if coordinator is None:
            raise OAuthError(
                OAuthErrorCode.NotConfigured,
                "OAuth has not been admitted for this server",
            )
        return coordinator

    def _discard_oauth_flow(self, bundle_id: BUNDLE_ID) -> None:
        """撤下注册的 OAuthFlow handle（handle.cancel pre-admission 路径）。"""
        self._oauth_flows.pop(bundle_id, None)

    def _retire_oauth_bundle(self, bundle_id: BUNDLE_ID) -> None:
        """撤下 bundle 的全部 OAuth 运行时态（coordinator / flow handle / 在途 connect 任务）。

        用于 transport 类型切换（streamable→stdio/sse）与 aremove_server——凭据 store
        不清（仅运行时态退役，clear_oauth 才清凭据槽）。
        """
        coordinator = self._oauth_coordinators.pop(bundle_id, None)
        self._oauth_flows.pop(bundle_id, None)
        # 解出旧 coordinator 的 launch 等待者（若有）→ launch_awaiting() 回落 False，
        # 挂在该任务上的 _rekick 不再复活 ghost connect（#179 隔离复核 🟡c）。
        if coordinator is not None:
            coordinator.fail_launch(
                OAuthError(OAuthErrorCode.AuthorizationCancelled, "Authorization was cancelled")
            )
        task = self._oauth_connect_tasks.pop(bundle_id, None)
        if task is not None:
            task.cancel()

    # #211：同上（CLI `stop <target>`）；私有 `_astop_client` 保持裸奔，理由见上。
    @restores_cancellation
    async def astop_client(self, bundle_id: BUNDLE_ID) -> None:
        """停止单个服务器客户端（按 bundle_id）。

        #208：整个停止在**该 bundle 的生命周期锁**内 ⇒ 与同 bundle 在途启动事务串行化——
        「启动尚未 commit 时 stop」不会出现「stop 返回后 client 才被提交」的复活窗口。
        """
        async with self._bundle_lock(bundle_id):
            await self._astop_client_locked(bundle_id)

    async def _astop_client(self, bundle_id: BUNDLE_ID) -> None:
        """经 per-bundle 锁的停止（公开/锁外调用点用）。/ Stop via the per-bundle lock."""
        async with self._bundle_lock(bundle_id):
            await self._astop_client_locked(bundle_id)

    async def _astop_client_locked(self, bundle_id: BUNDLE_ID) -> None:
        # ⚠️ 未命中 = **静默 no-op**，与同类三兄弟刻意不同（``aremove_server`` 抛 KeyError、``_astart_client``
        #    抛 ValueError）：停止是幂等语义，且与 rust ``stop_client`` 逐行同构（#143 决策 2 保留现状）。
        #    代价：**报错义务落在人机面** :mod:`a2c_smcp.computer.cli.resolve` —— REPL 先 ``resolve_target``
        #    解析并校验「已注册」，未命中不下传。**绕过 CLI 直调本方法的调用方拿不到任何未命中信号**
        #    （历史 P0「stop <未知 token> 却打印 ✅ 停止完成」即由此而来，见 #143）。新增调用方请自行判存。
        """停止单个服务器客户端（按 bundle_id）。**须持该 bundle 的生命周期锁**（#208）。

        #184：同时清除 control-plane activation intent 与 data-plane connection 状态。
        只有显式 stop 才清除 activation；OAuth 凭据变化只影响 connection。

        ⚠️ ``asyncio.Lock`` **不可重入** ⇒ 持锁调用方（``aremove_server`` / ``_arestart_server`` /
        ``_astop_client`` 自身）必须调用本 ``_locked`` 变体，不得调 :meth:`astop_client`。
        """
        async with self._lock:
            # 清除 control-plane activation intent
            self._activation_intents.discard(bundle_id)
            self._connection_states.pop(bundle_id, None)

            client = self._active_clients.pop(bundle_id, None)
            if client:
                # #185：每次 active-client 移除都必须 bump 世代（ABA 检测，见 _bump_active_client_generation）
                _bump_active_client_generation(self._active_client_generations, bundle_id)
        if client:
            # disconnect 在状态锁外（慢 I/O），随后再回锁内刷新映射
            await client.adisconnect()
            async with self._lock:
                await self._arefresh_tool_mapping()

    async def _astop_all(self) -> None:
        """停止所有客户端。

        #184：收集 ``active_clients`` 与 ``activation_intents`` 的**并集**，
        确保 OAuth-pending server（有 activation 但无活跃连接）也被停止。

        #208：快照在状态锁内取（短临界区），逐项停在各 bundle 锁内——**不跨项持锁**，
        故与在途启动的串行化只发生在同 bundle 上（跨 bundle 无谓阻塞）。
        """
        async with self._lock:
            all_ids = set(self._active_clients.keys()) | self._activation_intents
        for bid in all_ids:
            await self._astop_client(bid)

    # #211：同上（CLI `stop all`）；私有 `_astop_all` 保持裸奔，理由见上。
    @restores_cancellation
    async def astop_all(self) -> None:
        """停止所有客户端

        #208：本入口**不再**包状态锁（``_astop_all`` 逐项自取 bundle 锁 → 状态锁；外层持状态锁
        即锁序倒置）。停止的原子性由 per-bundle 锁逐项保证。
        """
        logger.debug(f"Manager Stop all async task: {asyncio.current_task()}")
        await self._astop_all()

    def _clear_all(self) -> None:
        """清空所有连接与映射 / Clear all state。

        ⚠️ **清空 ≠ 断开**：本方法只丢引用、**不** ``adisconnect``。故调用方必须先确保在途启动
        事务已收敛（:meth:`aclose` / :meth:`ainitialize` 的 ``close() → drain()`` 前导），否则
        「清空之后才提交」的 client 会被静默丢弃 ⇒ **进程泄漏**。

        ⚠️ **本方法不得清理 ``_bundle_locks``**：启动事务在锁外进行，中途换表会让「持锁者 A」与
        「新取锁者 B」并存，同 bundle 双开静默复开。换表只发生在 :meth:`ainitialize`（先 drain 再换）。

        / Clears state but never disconnects; never touches ``_bundle_locks``.
        """
        self._servers_config.clear()
        self._servers_config_raw.clear()
        self._active_clients.clear()
        self._activation_intents.clear()
        self._connection_states.clear()
        self._exposed_tools.clear()
        self._tool_projection.clear()  # #197：投影指纹随路由同清（两表同步不变量，勿留残影）
        self._active_client_generations.clear()
        self._oauth_clear_epochs.clear()
        # #179 OAuth 注册表随清（detached connect 任务取消弃置；coordinator/store 凭宿主注入）
        self._oauth_coordinators.clear()
        self._oauth_flows.clear()
        # 先快照再 cancel：任务的 done callback（_rekick）可能在任何挂起点重派发并改写本表，
        # 直接迭代活字典会 RuntimeError（与 _arefresh_tool_mapping 同族的「物化快照」纪律）。
        for task in list(self._oauth_connect_tasks.values()):
            task.cancel()
        self._oauth_connect_tasks.clear()

    async def _asettle_oauth_connect_tasks(self) -> None:
        """**取消并等待结算**全部 detached OAuth connect 任务（#208）。

        这些任务**不占门计数**（``_ensure_oauth_connect_task`` 立即返回，交互式 flow 可在事务之外
        持续运行），故 ``drain()`` 收敛不到它们。若不先结算，一个已越过 coordinator 检查、正停在
        状态锁上的任务会在 ``_astop_all`` 的快照之后提交 ⇒ 漏停 + 进程泄漏。
        ``_clear_all`` 的裸 ``cancel()`` 从「主要手段」降为**兜底**。

        / Cancel *and await* detached OAuth connect tasks so no late commit escapes the stop pass.
        """
        tasks = [t for t in self._oauth_connect_tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @restores_cancellation
    async def aclose(self) -> None:
        """关闭所有连接（别名）。

        #211：``@restores_cancellation`` —— 其下的 ``client.adisconnect()`` 会经过 client 状态机，
        而 ``AsyncMachine.process_context`` **按设计**吞掉回调内抛出的 ``CancelledError``；不还原的话，
        宿主 ``wait_for(manager.aclose(), t)`` 会「正常返回」、拿不到超时信号。装饰点必须是本层（最外层
        入口）而非更内层：在 ``_astop_all`` 循环中途补抛会让**剩余 client 不再被停止**、``_clear_all`` 被
        跳过 —— 见 :mod:`a2c_smcp.utils.cancellation` 的「只挂最外层」约束。

        #208 收敛顺序（**不可换**，写错 = 进程泄漏）：
        1. ``gate.close()``：不再接纳新启动，**排队者即刻失败**（在途不中断）；
        2. ``await gate.drain()``：等在途启动事务收敛（**不持任何锁**——在途事务可能正等状态锁）；
        3. 结算 detached OAuth connect 任务（不占门计数，drain 收敛不到）；
        4. ``_astop_all()``：此时快照完整（无「尚未提交的启动」）；
        5. ``_clear_all()``：与 4 之间**不得有 await**（``_clear_all`` 不 disconnect）。
        """
        # 1. 关闸：排队者与后来者即刻失败
        self._start_gate.close()
        # 2. 在途收敛（不得持任何锁）+ 3. detached OAuth connect 任务结算
        # ⚠️ 收敛前导被打断时**不得就此跳过拆除**：`drain()` 是 teardown 的**前置** await 且不是吞点，
        # 若宿主 `wait_for(shutdown(), t)` 恰在有在途启动事务时到点，取消会从 drain 直接上抛 ——
        # `_astop_all` / `_clear_all` 全被跳过 ⇒ **一个 client 都不会被停**（比「拆到一半」更彻底地失败）。
        # 故吞掉该取消、把拆除走完；信号无需手工补抛：`Task.cancelling()` 不因吞掉而自减，
        # 最外层的 ``@restores_cancellation`` 会在收尾处如实还原（#211 同一契约）。
        try:
            await self._start_gate.drain()
            await self._asettle_oauth_connect_tasks()
        except asyncio.CancelledError:
            logger.warning(
                "aclose 的收敛前导被取消：继续走完 stop/clear（取消信号由最外层入口还原）",
            )
        # 4. 停止所有客户端（此时快照完整）
        await self.astop_all()

        # 5. 清空所有状态存储（与上一步之间不得有 await）
        self._clear_all()

    def _withdraw_bundle_tool_routes(self, bundle_id: BUNDLE_ID) -> bool:
        """确定性撤回单个 server 的缓存工具投影（**无 await、不取锁、不接触任何 MCP**）。

        Fail-closed 原语（Rust ``withdraw_bundle_tool_routes`` 的 python 面）：即便上游 MCP
        server 已不可达，本地授权撤销也立即生效。与全量 :meth:`_arefresh_tool_mapping` 不同，
        本方法**只移除**指定 bundle 的路由**并同步裁剪其投影指纹**（#197，两表同步不变量）、
        不发起任何 ``tools/list`` RPC。

        返回值维持**纯路由口径**（Rust 为 ``routes_changed || disabled_changed || projection_changed``）：
        当前可达状态下投影变化 ⊆ 路由变化，故口径差异不漏 bump；若将来出现「投影多于路由」的可达状态
        （如 clear 后路由已空但投影仍携带），需一并纳入返回值。

        **rebind 而非 in-place del**：在途迭代器（如 :meth:`available_tools` 的发布校验段）
        读旧 dict 对象不受影响，避免「dictionary changed size during iteration」。

        Returns:
            bool: 实际是否有路由被移除（供 capability_changed 判定）。
        """
        before = len(self._exposed_tools)
        self._exposed_tools = {exposed: route for exposed, route in self._exposed_tools.items() if route[0] != bundle_id}
        # #197：投影指纹**同步裁剪** —— 否则紧随其后的 ``arefresh_tools()`` 会因残留条目报告一次变化，
        # 令 clear_oauth 的能力撤销被计成两次 revision（Rust 同名方法 routes / disabled / projection 三表齐撤）。
        # 按前缀裁剪（此处的 rebind 已丢失旧路由，无法反查归属）。
        _prefix = f"{bundle_id}__"
        self._tool_projection = {exposed: fp for exposed, fp in self._tool_projection.items() if not exposed.startswith(_prefix)}
        return len(self._exposed_tools) != before

    async def _arefresh_tool_mapping(self) -> bool:
        """重建 ExposedToolMapping（**须持 ``_lock``**）：快照 → 构建新表 → 提交前世代校验 → 失配整轮重试。

        Rebuild the shared ExposedToolMapping used by both ``available_tools`` and ``tool_call`` routing.

        #197：返回**暴露工具投影是否真实变化** —— ``Computer`` 据此推进 ``capability_revision``。判据是含
        description / inputSchema / annotations / meta 的**全字段**投影指纹（拒绝 name-only 捷径，对齐
        Rust PR #199 的 ``projection_changed``）；``list_tools`` 失败的 bundle 在**新旧两侧对称剔除**
        （对齐 Rust「失败不 commit、不视为工具被移除」），故临时故障不会被误报成一次能力变化。

        Returns whether the exposed tool projection actually changed (#197), feeding
        ``Computer.capability_revision``. Failures are excluded symmetrically so they never read as removals.

        ``exposed_tool_name = {bundle_id}__{alias ?? 原始名}``（协议 §exposed_tool_name）。跨 bundle_id 因前缀
        天然唯一——**无需**跨 server 重名对账（旧 ``ToolNameDuplicatedError`` 场景消失）。forbidden 工具**不进表**
        （不可见不可调用）。同一 bundle_id 内两工具经 ``alias`` 撞出相同 exposed → 保留首个 + Computer 本地诊断
        （WARN，非协议错误码）。

        #185：:meth:`clear_oauth` 的**零 await 快速段不取锁**，可在本方法 RPC 在途时并发撤回路由 /
        退役 client——若直接覆写活表，已撤回的路由会被陈旧快照**复活**。故：① 快照物化（list，
        锁内 RPC 在途时 clear 对 ``_active_clients`` 的 pop 是就地变异，直接迭代活字典会 RuntimeError）；
        ② 构建**新表**（不再原地 clear + 增量写，锁外读者只见旧表或新整表，与 Rust「原子换出」同构）；
        ③ 提交前按「活跃 client 集合 + client 身份 + per-bundle 世代」校验快照一致性，失配则整轮重试
        （Rust ``refresh_tool_routes`` 的 snapshot-validate-retry 同构）。重试**无界**（Rust 同为无界
        ``continue``）：仅高频世代抖动（start/stop/clear 风暴）下多轮，最终一致；勿加 sleep——
        抖动平息后一轮即收敛。
        """
        while True:
            snapshot: list[tuple[BUNDLE_ID, MCPClientProtocol, int]] = [
                (
                    bundle_id,
                    client,
                    self._active_client_generations.get(bundle_id, 0),
                )
                for bundle_id, client in self._active_clients.items()
            ]
            new_routes: dict[EXPOSED_TOOL_NAME, tuple[BUNDLE_ID, TOOL_NAME]] = {}
            # #197：本轮投影指纹 + list_tools 失败的 bundle（失败 bundle 不参与变化判定）
            new_projection: dict[EXPOSED_TOOL_NAME, str] = {}
            failed: set[BUNDLE_ID] = set()
            for bundle_id, client, _generation in snapshot:
                config = self._servers_config[bundle_id]
                # #151 R1'：default_tool_meta.alias 天生病态（alias 是 per-tool 改名）→ 已忽略（见 _merged_tool_meta），
                # 每次刷新各 server 各打一次响亮配置诊断（方案 d，与 no-double-open「不静默丢 + 配置诊断」同姿态；非协议错误码）。
                # 跨 SDK：rust 侧同款 R1' 待以**同方案 d**跟修（否则 python 各以原始名暴露 / rust 塌名 = 双端分叉，向量测不出；
                # #142 教训）——镜像 follow-up 追踪于 Epic #147。
                if config.default_tool_meta is not None and config.default_tool_meta.alias:
                    logger.warning(
                        f"default_tool_meta.alias={config.default_tool_meta.alias!r}（bundle_id={bundle_id!r}）已忽略："
                        f"alias 是 per-tool 改名，放 default 位会令该 server 所有工具塌成同名。"
                        f"如需改名请在具体 tool_meta.<工具名> 内单独配 alias（Computer 本地诊断，非协议错误码）。",
                    )
                try:
                    tools = await client.list_tools()
                except Exception as e:
                    logger.error(f"Error listing tools for bundle_id={bundle_id!r} (name={config.name!r}): {e}", exc_info=True)
                    failed.add(bundle_id)  # #197：本轮无法判定该 bundle → 不参与投影比对（见提交块对称剔除）
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
                    if exposed in new_routes:
                        # 同一 bundle_id 内 alias 撞名（跨 bundle_id 不可能撞）→ 保留首个 + 诊断，指导修正 alias
                        logger.warning(
                            f"exposed_tool_name 冲突（同 bundle_id={bundle_id!r} 内 alias 撞名）：'{exposed}'——保留首个、"
                            f"跳过原始工具 '{original_tool_name}'；请修正 tool_meta.alias（Computer 本地诊断，非协议错误码）。",
                        )
                        continue
                    new_routes[exposed] = (bundle_id, original_tool_name)
                    new_projection[exposed] = _tool_projection_fingerprint(t)
            # 提交前校验：活跃 client 集合 + 身份 + 世代是否与快照一致（clear 快速段 / stop / start
            # 均会造成失配）→ 失配则整轮重试，绝不以陈旧快照覆写更新的投影
            if len(self._active_clients) == len(snapshot) and all(
                self._active_clients.get(bundle_id) is client and self._active_client_generations.get(bundle_id, 0) == generation
                for bundle_id, client, generation in snapshot
            ):
                # #197：变化判定 —— 失败 bundle 在**旧侧**显式剔除（新侧天然不含：其 ``continue`` 跳过了
                # 路由与指纹写入）。两侧都含失败 bundle 的条目时，「本次失败」会被读成「工具被移除」。
                changed = new_projection != _projection_excluding_failed(self._tool_projection, failed)
                # 投影提交时**保留失败 bundle 的携带条目**：否则「失败 → 恢复」会被判成一次变化，产生虚假
                # revision。归属按 exposed 名前缀（不依赖路由表 —— 路由表本轮已被换成不含失败 bundle 的新表）。
                merged_projection = dict(new_projection)
                for _exposed, _fingerprint in self._tool_projection.items():
                    if _exposed not in merged_projection and _exposed.startswith(tuple(f"{bundle}__" for bundle in failed)):
                        merged_projection[_exposed] = _fingerprint
                self._exposed_tools = new_routes
                self._tool_projection = merged_projection
                return changed

    async def arefresh_tools(self) -> bool:
        """公开的工具映射刷新入口：锁内重建 ExposedToolMapping（``_exposed_tools``）（#127）。

        返回值 / Returns（#197）: 本次刷新的**暴露工具投影是否真实变化** —— ``Computer`` 据此推进
        ``capability_revision``（变化才推进，未变不产生虚假 revision）。既有调用方忽略返回值不受影响。
        Whether the exposed tool projection actually changed (#197); existing callers may ignore it.

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
            return await self._arefresh_tool_mapping()

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
            # 上游授权失败（4006/4007）→ 以 CallToolResult 携结果级 meta surface（协议 error-handling.md §4006/4007；
            # meta.mcp_server = 路由所用 bundle_id，供 Agent correlate + 区分「需授权」vs「工具坏了」，#133）。
            # 其它失败保持通用 RuntimeError（无授权正面信号绝不误判）。此处是全链唯一 bundle_id ∧ 原始异常同在处。
            # Upstream auth failure → surface as CallToolResult with result-level meta (mcp_server = routing bundle_id).
            error_code = classify_auth_error(e)
            if error_code is not None:
                return build_auth_error_result(bundle_id, error_code)
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

    # ── #184 启动/连接状态正交化 runtime status API ──────────────────────────

    # 将 per-client STATES 字符串映射到 MCPServerConnectionState
    _CLIENT_STATE_TO_CONNECTION: dict[str, MCPServerConnectionState] = {
        "initialized": MCPServerConnectionState.CONNECTING,
        "connected": MCPServerConnectionState.CONNECTED,
        "disconnected": MCPServerConnectionState.DISCONNECTED,
        "error": MCPServerConnectionState.ERROR,
    }

    def get_server_runtime_statuses(self) -> list[MCPServerRuntimeStatus]:
        """获取所有已注册 server 的正交运行时状态快照 / Orthogonal runtime status snapshot.

        **纯内存读取、无 I/O**。从 _servers_config / _activation_intents /
        _connection_states / _active_clients 四源合成，每个 server 返回其
        control-plane activation 与 data-plane connection 的当前状态。
        """
        results: list[MCPServerRuntimeStatus] = []
        for bid, config in self._servers_config.items():
            activation = (
                MCPServerActivationState.STARTED
                if bid in self._activation_intents
                else MCPServerActivationState.STOPPED
            )

            remembered = self._connection_states.get(bid)
            if activation == MCPServerActivationState.STOPPED:
                connection = MCPServerConnectionState.DISCONNECTED
            elif remembered is MCPServerConnectionState.AUTHORIZATION_REQUIRED:
                # 终端用户动作边界（如 clear_oauth）：优先取 committed 状态，
                # 而非并发采样的 live client state。
                connection = MCPServerConnectionState.AUTHORIZATION_REQUIRED
            elif bid in self._active_clients:
                live_state = self._active_clients[bid].state
                connection = self._CLIENT_STATE_TO_CONNECTION.get(
                    live_state, MCPServerConnectionState.DISCONNECTED
                )
            else:
                connection = remembered or MCPServerConnectionState.DISCONNECTED

            results.append(
                MCPServerRuntimeStatus(
                    bundle_id=bid,
                    name=config.name,
                    activation=activation,
                    connection=connection,
                )
            )
        return results

    def get_server_status(self) -> list[tuple[BUNDLE_ID, SERVER_NAME, bool, str]]:
        """获取服务器状态列表 [(bundle_id, display_name, 是否活跃, 状态), ...]。

        #184 backward compat：委托 :meth:`get_server_runtime_statuses`。
        ``is_active`` 现在投影 ``is_started()``（原为 ``bundle_id in _active_clients``）；
        ``state`` 投影 connection state snake_case 字符串（原为 ``"pending"`` 或 client.state）。
        """
        return [
            (s.bundle_id, s.name, s.is_started(), s.connection.value)
            for s in self.get_server_runtime_statuses()
        ]

    # ── #179 公共 OAuth facade（签名对齐 Rust Computer/Manager） ─────────────

    def with_oauth_credential_store(self, store: OAuthCredentialStore) -> "MCPServerManager":
        """注入凭据存储（builder，返回 self）。缺省 :class:`InMemoryOAuthCredentialStore`；
        跨进程恢复由宿主注入持久化 store（Epic Sub 4，Rust ``with_oauth_credential_store``）。

        注意：对**已准入**的 coordinator 不生效（coordinator 构造时已绑定 store）——
        请在首次 connect 之前注入（Computer 版同约定：须在 boot_up 前调用）。
        """
        self._oauth_credential_store = store
        return self

    def with_mcp_start_concurrency(self, max_concurrency: int) -> "MCPServerManager":
        """安装 Computer 级 **MCP 最大并发启动数**（构造期策略，``0`` 按 ``1`` 处理；builder，返回 self）。

        该上限约束**单个启动、批量启动与 Plugin 治理恢复**全路径——三者共享同一把门，故任意时刻
        总在途启动事务数 ≤ 本值。**未配置**（缺省）= 保持既有行为：批量仍逐项串行、不新增并发
        （门仍计数，故 ``drain`` 与关闸语义在串行路径上同样成立）。

        并发许可覆盖**完整启动事务**（Input 重解析 → manager start 全链），事务结束即释放；重叠批次
        与单启/恢复的交叉调用也维持上限。**Input 交互解析保持串行**（由 ``Computer`` 侧的解析锁保证）。

        🔴 **fail-closed**：任何 ``acquire`` 尝试之后调用即抛 ``RuntimeError`` —— 运行期换门会让
        已排队的等待者脱离新上限乃至悬挂，配置必须在启动前完成（对齐 rust ``set_max`` 的 panic）。

        属于**宿主运行时策略**（下游 tfrobot-client 产品默认 5），不落盘、不成为用户配置。

        / Host-side runtime policy for the Computer-level start concurrency ceiling. Unconfigured
        keeps the existing serial behaviour; fails closed if installed after any start attempt.
        """
        self._start_gate.configure(max_concurrency)
        return self

    def enabled_bundle_ids(self) -> list[BUNDLE_ID]:
        """已挂载且**未禁用**的 bundle_id（按挂载顺序）——批量启动的输入面（#208）。

        对齐 rust ``enabled_mcp_bundle_ids``：``start all`` 与治理恢复只迭代**已挂载** server，
        不遍历声明面。纯读、无锁（调用方拿到的是一份新 list）。

        / Mounted, non-disabled bundle ids in mount order — the batch-start input surface.
        """
        return [bid for bid, cfg in self._servers_config.items() if not cfg.disabled]

    def _bundle_lock(self, bundle_id: BUNDLE_ID) -> asyncio.Lock:
        """取该 bundle 的生命周期锁（惰性建档）。

        **须在最外层获取**（禁止在持状态锁时调用——见类 docstring 的锁层次）。
        同一 bundle 的启动事务、停止与移除共用此锁 ⇒ 「双开」与「启动中移除」由结构排除。

        / The per-bundle lifecycle lock; acquire it outermost (never while holding the state lock).
        """
        lock = self._bundle_locks.get(bundle_id)
        if lock is None:
            lock = self._bundle_locks[bundle_id] = asyncio.Lock()
        return lock

    async def oauth_status(self, bundle_id: BUNDLE_ID) -> OAuthStatus:
        """查询指定 server 的 OAuth 授权状态。

        Raises:
            OAuthError: ``NotConfigured`` —— bundle 未注册 / 未准入（从未见过 Bearer challenge）；
                ``UnsupportedTransport`` —— 非 Streamable HTTP（无 OAuth 通道）。
        """
        config = self._servers_config.get(bundle_id)
        if config is None:
            raise OAuthError(
                OAuthErrorCode.NotConfigured,
                "OAuth has not been admitted for this server",
            )
        if self._oauth_spec(config) is None:
            raise OAuthError(
                OAuthErrorCode.UnsupportedTransport,
                f"Server bundle_id={bundle_id!r} does not support OAuth",
            )
        coordinator = self._oauth_coordinators.get(bundle_id)
        if coordinator is None:
            raise OAuthError(
                OAuthErrorCode.NotConfigured,
                "OAuth has not been admitted for this server",
            )
        return await coordinator.status()

    def create_oauth_flow(self, bundle_id: BUNDLE_ID, request: OAuthBeginRequest) -> OAuthFlow:
        """注册交互式授权 flow（**同步、无 I/O**；对齐 Rust ``create_oauth_flow``）。

        相同请求幂等返回同一 handle；不同请求 → ``AuthorizationAlreadyPending``。
        准入推迟到 :meth:`OAuthFlow.launch`（跑 bounded connect）。
        """
        config = self._servers_config.get(bundle_id)
        if config is None:
            raise OAuthError(
                OAuthErrorCode.NotConfigured,
                "OAuth has not been admitted for this server",
            )
        if self._oauth_spec(config) is None:
            raise OAuthError(
                OAuthErrorCode.UnsupportedTransport,
                f"Server bundle_id={bundle_id!r} does not support OAuth",
            )
        existing = self._oauth_flows.get(bundle_id)
        coordinator = self._oauth_coordinators.get(bundle_id)
        # Rust ``!flow.is_terminal()`` 过滤：只有**非终态**的既有 flow 参与幂等/冲突
        # 判定。终态后（completed / cancelled / expired）新请求**替换**注册表槽——
        # 宿主 loopback retry 每次换 ephemeral port（新 redirect_uri）正是此模式。
        if existing is not None and (coordinator is None or coordinator.has_active_flow()):
            if existing._request == request:
                return existing  # 幂等：同一 flow 的 clone
            raise OAuthError(
                OAuthErrorCode.AuthorizationAlreadyPending,
                "A different authorization flow is already pending",
            )
        flow = OAuthFlow(manager=self, bundle_id=bundle_id, request=request)
        self._oauth_flows[bundle_id] = flow
        return flow

    async def complete_oauth(
        self, bundle_id: BUNDLE_ID, callback: OAuthCallback
    ) -> OAuthFlowOutcome:
        """提交宿主浏览器回调（经注册的 handle 委托 coordinator 完成交换）。

        Raises:
            OAuthError: ``StateMismatch`` —— 无注册 flow；其余按 coordinator 语义。
        """
        flow = self._oauth_flows.get(bundle_id)
        if flow is None:
            raise OAuthError(
                OAuthErrorCode.StateMismatch,
                "No pending authorization flow",
            )
        return await flow.complete(callback)

    async def cancel_oauth(
        self, bundle_id: BUNDLE_ID, cancellation: OAuthCancellation
    ) -> OAuthFlowOutcome:
        """宿主取消/AS 错误回调（经注册 handle 按 reason 分派；Rust ``cancel_compat``）。

        Raises:
            OAuthError: ``StateMismatch`` —— 无注册 flow。
        """
        flow = self._oauth_flows.get(bundle_id)
        if flow is None:
            raise OAuthError(
                OAuthErrorCode.StateMismatch,
                "No pending authorization flow",
            )
        return await flow._cancel_compat(cancellation)

    async def clear_oauth(self, bundle_id: BUNDLE_ID) -> bool:
        """清除该 server 的 OAuth 授权并报告 Agent 面能力是否被**实际撤回**（#185）。

        排干 pending flow（cancel + 撤销在途 connect 任务）→ 清凭据槽（DCR 注册 +
        token + issuer index）→ status 回落 Unauthorized → **零 await 快速段** commit：
        clear epoch +1 → 退役活跃 client → 确定性撤回本 bundle 路由 → 连接状态 commit
        （Started → AUTHORIZATION_REQUIRED，否则 DISCONNECTED；**不清 activation**，
        #184：只有显式 stop 才清）→ 传输断开（撤销已 commit 后才做，fallible 仅 WARN）。

        **关键不变量（#185，对齐 Rust ``clear_oauth_with_outcome``）**：快速段不取
        ``_lock``、不含任何 await → 在 asyncio 单线程事件循环内原子，**从不等待**任何
        锁内 inflight 上游 MCP I/O（tools/list / connect / disconnect）——即便上游 server
        永远不应答，本地授权撤销也立即生效。start-vs-clear 竞态由
        :meth:`_commit_active_client` 的 clear epoch 校验补偿（见该处注释）。

        🔴 **#208：本方法刻意也 Bypass ``_bundle_locks``**（勿「补上」）。per-bundle 生命周期锁是
        ``await`` 型，取它就意味着撤销授权可能要等一次 render/connect 结束——恰好摧毁上面那条
        不变量。与 start 的互斥**仍**由 clear epoch 保证：无论 start 事务走到哪一步，
        :meth:`_commit_active_client` 是唯一登记点且会比对 epoch 后拒绝。

        Returns:
            bool: ``True`` = 有活跃 client 被退役或路由被实际撤回（capability_changed，
                调用方据此 bump revision + 广播 update_tool_list）；``False`` = 无事发生
                （幂等：重复 clear 已清除的 bundle 不触发二次传播）。

        Raises:
            OAuthError: ``NotConfigured`` —— 未准入。
        """
        coordinator = self._oauth_coordinators.get(bundle_id)
        if coordinator is None:
            raise OAuthError(
                OAuthErrorCode.NotConfigured,
                "OAuth has not been admitted for this server",
            )
        # 撤销在途 connect 任务（交互式 flow 的 transport 驱动者）
        task = self._oauth_connect_tasks.pop(bundle_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._oauth_flows.pop(bundle_id, None)
        # 排干 pending flow + 清凭据 + status→Unauthorized（coordinator 自身锁；本地 store I/O）
        await coordinator.clear()
        # ── 能力撤销快速段（零 await → asyncio 原子，不取 _lock）────────────────────
        # ① clear epoch +1：start 提交守卫（Rust 以 per-bundle lifecycle lock 串行化 start-vs-clear；
        #    python 单一全局锁由本快速段绕过，故以 epoch 补偿同一语义，见 _commit_active_client）
        self._oauth_clear_epochs[bundle_id] = self._oauth_clear_epochs.get(bundle_id, 0) + 1
        # ② 退役活跃 client（pop 就地变异；与 _arefresh_tool_mapping 的快照化配合防迭代 RuntimeError）
        client = self._active_clients.pop(bundle_id, None)
        had_connected_client = client is not None
        if had_connected_client:
            _bump_active_client_generation(self._active_client_generations, bundle_id)
        # ③ 确定性撤回本 bundle 路由（纯本地、无 RPC、fail-closed）
        routes_withdrawn = self._withdraw_bundle_tool_routes(bundle_id)
        # ④ 连接状态 commit（get_server_runtime_statuses 的 committed 分支渲染）
        if bundle_id in self._activation_intents:
            self._connection_states[bundle_id] = MCPServerConnectionState.AUTHORIZATION_REQUIRED
        else:
            self._connection_states[bundle_id] = MCPServerConnectionState.DISCONNECTED
        # ── 传输断开（撤销已 commit 后才做；fallible 仅 WARN，Rust 同款）────────────
        if client is not None:
            try:
                await client.adisconnect()
            except Exception:
                logger.warning(
                    f"OAuth credentials cleared for bundle_id={bundle_id!r}, but transport disconnect failed",
                    exc_info=True,
                )
        return had_connected_client or routes_withdrawn

    async def available_tools(self) -> AsyncGenerator[tuple[BUNDLE_ID, Tool], Any]:
        """获取暴露给 Agent 的工具及其归属；产出 ``(bundle_id, Tool)``，``Tool.name`` = **exposed_tool_name**。

        Yield ``(bundle_id, Tool)`` exposed to the Agent; each ``Tool.name`` is the exposed_tool_name
        (协议 §exposed_tool_name / #106)。``bundle_id`` = 该工具所属 server 的**解析后**身份（#152 D1，供
        ``SMCPTool.bundle_id`` 填充，显式归属、禁前缀反推）。Agent 即以 exposed_tool_name 寻址，
        ``aexecute_tool`` 经 ExposedToolMapping 解析回原始名调用上游。与 ``list_windows`` / ``list_resources``
        返回 ``(bundle_id, X)`` 的既定约定一致。

        #185：快照 → **锁外** RPC（每 bundle 至多一次 tools/list）→ 发布前锁外二次校验
        （零 await 同步段 → asyncio 原子）——「路由值未变 + client 身份未变 + 世代未变」三项全真才产出，
        任一失配（:meth:`clear_oauth` 快速段已撤回该 bundle）→ 丢弃该候选。与 Rust
        ``list_available_tools_with_bundle_id`` 发布前重验证同构：clear 要么先于本拉取完成
        （候选被过滤），要么后于本拉取（capability 广播触发 Agent 重拉）——Agent **永不**在
        clear 完成后拿到含已撤回 bundle 的过期工具列表。
        """
        # ① 快照（锁内一次性取：路由 + client 身份 + 世代 + config；此后全部 RPC 在锁外——
        #    clear 快速段绝不因本拉取的 tools/list RPC 而阻塞）
        snapshots: list[tuple[EXPOSED_TOOL_NAME, BUNDLE_ID, TOOL_NAME, MCPClientProtocol, int, MCPServerConfig]] = []
        async with self._lock:
            for exposed_name, (bundle_id, original_tool_name) in self._exposed_tools.items():
                client = self._active_clients.get(bundle_id)
                if client is None:
                    continue
                snapshots.append((
                    exposed_name,
                    bundle_id,
                    original_tool_name,
                    client,
                    self._active_client_generations.get(bundle_id, 0),
                    self._servers_config[bundle_id],
                ))
        # ② 锁外 RPC：每 bundle_id 仅拉一次 tools/list（跨该 server 的多个 routed tool 复用，
        #    同 #91 per-server 缓存约定；异常照旧向调用方传播，与历史行为一致）
        servers_cached_tools: dict[BUNDLE_ID, list[Tool]] = {}
        for _exposed_name, bundle_id, _original_tool_name, client, _generation, _config in snapshots:
            if bundle_id not in servers_cached_tools:
                servers_cached_tools[bundle_id] = await client.list_tools()
        # ③ 组装候选（改名副本 + A2C meta；携发布校验所需的路由/身份/世代证据）
        candidates: list[tuple[EXPOSED_TOOL_NAME, BUNDLE_ID, TOOL_NAME, MCPClientProtocol, int, Tool]] = []
        # 畸形声明诊断：每 server 每次 tools/list 刷新至多一次（#151 R1' 防刷屏先例，协议 config-diagnostics 按 server 聚合）。
        warned_servers: set[BUNDLE_ID] = set()
        for exposed_name, bundle_id, original_tool_name, client, generation, config in snapshots:
            tools = servers_cached_tools.get(bundle_id)
            if tools is None:
                continue
            tool = next((t for t in tools if t.name == original_tool_name), None)
            if tool is None:
                continue
            # 无条件 reconcile（协议 §ToolMeta 三层合并规则，protocol#51 / PR#57 裁决）：对每个 Tool 消费并校验
            # Server 声明（即使 tool_meta 与 default_tool_meta 均不存在）；恒以 dict(tool.meta) 起底，按四分支
            # 覆写/pop：无声明→现状（配置非空才写）／合法→三层 canonical 恒覆写／非法→丢弃+诊断+配置 canonical
            # 或 pop。原生 _meta 其它 key 全程原样保留（shallow copy）。
            # 以 **key 存在性**判定有无声明——显式 null 声明（a2c_tool_meta: null）属「声明非对象」畸形判据，
            # 不得以值判空短路为「无声明」（否则 key 原样出线、违反 canonical-final）。
            config_meta = self._merged_tool_meta(config, original_tool_name)
            merged_meta = dict(tool.meta) if tool.meta else {}
            if A2C_TOOL_META not in merged_meta:
                # 无声明：维持现状（配置合并产物非空才写）。
                if config_meta:
                    merged_meta[A2C_TOOL_META] = config_meta
            else:
                declared, invalid_reason = _parse_server_declared_tool_meta(merged_meta[A2C_TOOL_META])
                if invalid_reason is not None:
                    # 畸形声明 → 丢弃 + 本地诊断（MUST NOT 令 tools/list 失败或工具消失）。
                    if bundle_id not in warned_servers:
                        warned_servers.add(bundle_id)
                        logger.warning(
                            f"畸形 ToolMeta Server 声明（bundle_id={bundle_id!r}, server={config.name!r}, "
                            f"tool={original_tool_name!r}）：{invalid_reason}——声明已丢弃（Computer 本地诊断，非协议错误码）。"
                        )
                    # 有配置终值 → 配置 canonical（维持现状）；无 → 删除该 key。
                    if config_meta:
                        merged_meta[A2C_TOOL_META] = config_meta
                    else:
                        merged_meta.pop(A2C_TOOL_META, None)
                else:
                    # 合法声明：三层合并 canonical 恒覆写（含「合法+无配置」→ tags-only canonical）。
                    merged_meta[A2C_TOOL_META] = self._merged_tool_meta(
                        config, original_tool_name, server_declared=declared
                    )
            # 产出名字改写后的**副本**（改 name 为 exposed_name），避免原地 mutate 缓存对象。
            # Yield a renamed copy (name=exposed_name) to avoid mutating the cached tool object.
            update: dict[str, Any] = {"name": exposed_name, "meta": merged_meta}
            candidates.append((
                exposed_name,
                bundle_id,
                original_tool_name,
                client,
                generation,
                tool.model_copy(update=update),
            ))
        # ④ 发布前锁外二次校验（零 await 同步段 → asyncio 原子）：三项全真才产出；
        #    携归属 bundle_id 产出（#152 D1）；bundle_id 即 _exposed_tools 键的解析后身份。
        for exposed_name, bundle_id, original_tool_name, client, generation, tool in candidates:
            route_is_current = self._exposed_tools.get(exposed_name) == (bundle_id, original_tool_name)
            client_is_current = self._active_clients.get(bundle_id) is client
            generation_is_current = self._active_client_generations.get(bundle_id, 0) == generation
            if route_is_current and client_is_current and generation_is_current:
                yield bundle_id, tool

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
    def _merged_tool_meta(
        config: MCPServerConfig,
        tool_name: TOOL_NAME,
        server_declared: _ServerDeclaredToolMeta | None = None,
    ) -> ToolMeta | None:
        """
        三层合并工具元数据（协议 v0.4.0 §ToolMeta 三层合并规则，protocol#51 / PR#57 裁决）：

            tool_meta[tool]（配置最具体） > default_tool_meta（配置默认） > Server 声明（最低层，白名单仅 tags）

        Server 声明层恒为最低层：配置 tags 缺失/null 时回落声明值（``[]`` 显式清除、不回落）；其余字段
        （auto_apply/alias/ret_object_mapper）仅来自配置。``server_declared=None`` 时保持纯 config 两层合并
        （#199 裁决③：acall_tool / get_tool_meta / _arefresh_tool_mapping 三个 config-only 消费点零行为差）。

        Three-layer merge: per-tool meta > default meta > server-declared tags (whitelist only `tags`).

        **例外（#151 R1'）**：``alias`` **绝不**从 ``default_tool_meta`` 继承——``alias`` 语义天生 per-tool（把某个工具
        改名），放 default 位会落到该 server 每一个未单独配 alias 的工具、令其 exposed 名全塌成同一个（first-wins 静默
        丢工具）。故 alias **仅**取自具体 ``tool_meta[tool_name]``；default 位的 alias 被忽略（诊断由
        :meth:`_arefresh_tool_mapping` 每次刷新各 server 打一次）。其余根级字段（tags/auto_apply/ret_object_mapper）照常回落。

        Exception (#151 R1'): ``alias`` is NEVER inherited from ``default_tool_meta`` — it is inherently per-tool; a
        default alias would collapse every un-aliased tool of the server onto one exposed name (first-wins tool loss).
        ``alias`` comes solely from the per-tool entry; other root fields still fall back to the default.
        """
        specific = (config.tool_meta or {}).get(tool_name)
        default = config.default_tool_meta
        if specific is None and default is None:
            # 无配置：合法声明 → tags-only canonical（可能全 null）；无声明 → None（维持现状）。
            if server_declared is None:
                return None
            return ToolMeta(tags=server_declared.tags)
        if specific is None:
            # 无 per-tool meta：继承 default 的非 alias 字段，alias 强制置空（#151 R1'）。
            config_meta = default.model_copy(update={"alias": None}) if default is not None else None
        elif default is None:
            config_meta = specific
        else:
            # 仅根级字段浅合并；specific优先
            merged: dict = {}
            # Pydantic v2: model_dump 可排除 None，以避免用 None 覆盖
            merged.update(default.model_dump(exclude_none=True))
            merged.update(specific.model_dump(exclude_none=True))
            # #151 R1'：alias 只认 per-tool（specific），绝不采纳 default 带入的 alias。
            merged["alias"] = specific.alias
            config_meta = ToolMeta(**merged)
        if config_meta is None:
            # 不可达（specific/default 双 None 已提前返回）——仅满足类型收窄。
            return None
        # 第三层回落：config 终值 tags 缺失/null（exclude_none 已剔除）→ 采纳 Server 声明 tags。
        if server_declared is not None and config_meta.tags is None:
            return config_meta.model_copy(update={"tags": server_declared.tags})
        return config_meta
