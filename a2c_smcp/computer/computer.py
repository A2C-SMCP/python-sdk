# 文件名: main.py
# @Time    : 2025/8/17 16:52
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm

"""
计算机管理模块 Computer
==============================

该模块定义了 Computer 类，用于管理 MCP 服务器的生命周期、工具获取等功能。

This module defines the Computer class, which manages the lifecycle of MCP servers and provides tool retrieval functions.

类和方法均采用 Google 风格注释（中英文双语）。
All classes and methods use Google style docstrings (bilingual: Chinese and English).

依赖 Dependencies:
    - mcp
    - pydantic
    - a2c_smcp_cc.mcp_clients.manager
    - a2c_smcp_cc.mcp_clients.model
    - a2c_smcp_cc.socketio.smcp
    - a2c_smcp_cc.socketio.types
    - a2c_smcp_cc.utils.logger
"""

import asyncio
import contextlib
import json
import os
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from mcp import Tool, types
from mcp.shared.session import RequestResponder
from mcp.types import (
    CallToolResult,
    Resource,
    ResourceListChangedNotification,
    ResourceUpdatedNotification,
    TextContent,
    ToolListChangedNotification,
)
from prompt_toolkit import PromptSession
from pydantic import BaseModel, TypeAdapter

from a2c_smcp.computer.base import BaseComputer
from a2c_smcp.computer.blob import (
    BlobResolver,
    BlobThresholds,
    BlobTooLargeError,
    SkillBlobResolver,
    ToolspoolBlobResolver,
    ToolspoolBlobStore,
    default_thresholds,
)
from a2c_smcp.computer.desktop.organize import organize_desktop
from a2c_smcp.computer.inputs.render import ConfigRender, load_env_file
from a2c_smcp.computer.inputs.resolver import InputNotFoundError, InputResolver
from a2c_smcp.computer.inventory import McpOwnership, McpPluginOwnership, McpServerWithMetadata, McpUserOwnership
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import MCPServerConfig, MCPServerInput
from a2c_smcp.computer.settings.installer import migrate_legacy_installs
from a2c_smcp.computer.settings.mcp_config import (
    McpWriteScope,
    McpWriteTargetError,
    ResolvedMcpConfig,
    is_writable_origin,
    remove_mcp_server,
    resolve_mcp_config,
    upsert_mcp_server,
)
from a2c_smcp.computer.settings.recovery import (
    BundledServerRecord,
    GovernanceRecoveryReport,
    RegisterBundledServer,
    collect_enabled_bundled_servers,
    recover_marketplace_skills,
)
from a2c_smcp.computer.skills import (
    SOURCE_USER,
    SkillEventDebouncer,
    SkillFileWatcher,
    SkillRegistry,
    SkillResourceView,
    ensure_skill_home,
    resolve_skill_view,
    stage_mcp_skills,
    stage_user_skills,
    user_dropin_root,
)
from a2c_smcp.computer.types import ToolCallRecord
from a2c_smcp.smcp import A2CSkillRef, Desktop, SMCPTool
from a2c_smcp.types import AttributeValue
from a2c_smcp.utils.bundle_id import resolve_bundle_id
from a2c_smcp.utils.env import env_truthy
from a2c_smcp.utils.env_segment import raise_on_env_name_collisions
from a2c_smcp.utils.logger import get_logger, truncate
from a2c_smcp.utils.window_uri import is_window_uri

logger = get_logger("computer")

# v0.2.1 SKILL 资源 URI scheme 前缀（与 manager.list_skill_resources 过滤一致）
# v0.2.1 SKILL resource URI scheme prefix (consistent with manager.list_skill_resources filter)
_SKILL_URI_PREFIX = "skill://"

if TYPE_CHECKING:
    # 仅用于类型检查，避免运行时引入依赖/循环引用
    from a2c_smcp.computer.socketio.client import SMCPComputerClient


class Computer(BaseComputer[PromptSession]):
    def __init__(
        self,
        name: str,
        inputs: set[MCPServerInput] | None = None,
        mcp_servers: set[MCPServerConfig] | None = None,
        auto_connect: bool = True,
        auto_reconnect: bool = True,
        confirm_callback: Callable[[str, str, str, dict], bool] | None = None,
        input_resolver: InputResolver | None = None,
        blob_cache_root: Path | None = None,
        blob_resolvers: dict[str, BlobResolver] | None = None,
        blob_thresholds: BlobThresholds | None = None,
        skill_home: Path | None = None,
        mcp_flag_config: Path | None = None,
    ) -> None:
        """
        初始化 Computer 实例
        Initialize Computer instance

        MCP Server 使用 set 管理配置项。注意基类重写了 __hash__ 并以 name 取哈希，但那**仅是哈希桶**、非身份判定：
        相等性走 Pydantic 全字段 __eq__，同名不同 config 只是碰撞、在 set 中各自共存。Server 唯一身份是
        bundle_id（协议 #18），去重（no-double-open）由 Manager 按 bundle_id 负责。

        The MCP Server configuration is in the form of a dictionary to help users reduce the possibility of duplicate
        configuration, and it is recommended to use the name of the MCP Server as the key to avoid duplicate
        configuration. Of course, if there is a special design that requires the name to be repeated, you can
        customize the dictionary value to avoid this limitation

        Args:
            inputs (set[MCPServerInput] | None): MCP服务器输入项配置集合（以 id 唯一、基于 set 去重）。MCP server input config set.
            mcp_servers (set[MCPServerConfig] | None): MCP服务器配置集合。MCP server config set.
            auto_connect (bool): 是否自动连接。Whether to auto connect.
            auto_reconnect (bool): 是否自动重连。Whether to auto reconnect.
            confirm_callback (Callable[[str, str, str, dict], bool] | None): 工具调用二次确认回调
            blob_cache_root (Path | None): v0.2.1 通用二进制传输缓存根。默认 ``~/.a2c``；
                ``.blobspool/`` 子目录将挂在其下。v0.2.1 milestone 内 #39 接管 SKILL Home 后会
                统一为 SKILL Home 同级。Generic blob-transfer cache root; ``.blobspool/`` lives
                here. To be replaced by SKILL Home sibling once #39 lands.
            blob_resolvers (dict[str, BlobResolver] | None): kind → resolver 映射覆盖；缺省装配
                ``toolspool`` (完整) 与 ``skill`` (#39 占位)。Kind → resolver override; defaults
                wire ``toolspool`` (complete) and ``skill`` (#39 placeholder).
            blob_thresholds (BlobThresholds | None): SKILL / blob 阈值（inline / too_large / chunk_max）；
                缺省经环境变量覆盖。Threshold bundle; defaults honor env overrides.
            skill_home (Path | None): v0.2.1 SKILL Home 根覆盖（测试 / 部署注入）。缺省走
                ``A2C_SKILL_HOME`` → ``$XDG_DATA_HOME/a2c/skills`` → ``~/.a2c/skills`` 解析链。
                mcp 源 ``skill://`` 物化落盘于 ``<home>/mcp/<server>/<skill>/``。
                SKILL Home root override (test/deploy injection); defaults to the env resolution chain.
            mcp_flag_config (Path | None): flag 层 ``mcp.json`` 路径（CLI ``--mcp-config <file>``，含
                ``servers``/``inputs``；优先级**次高、仅低于 policy**，§2.5-3）。与 ``mcp_servers``（embed 层）
                同属**当次 boot 的声明式输入** —— §2.5-5 要求 origin 集「可从当次 boot 的声明式输入重建」，
                Computer 即该 boot 对象，故二者同住于此，经 :meth:`resolve_mcp_declarations` 一并投影。
                The flag-layer mcp.json path (CLI ``--mcp-config``); a boot-declarative input alongside
                ``mcp_servers`` (the embed layer). See :meth:`resolve_mcp_declarations`.
        """
        self.name = name
        self.mcp_manager: MCPServerManager | None = None
        self._inputs: set[MCPServerInput] = set(inputs or set())
        # #155：构造期即拒坍缩池。**无条件**检（与是否注入 input_resolver 无关）——注入 resolver 时
        # 下面的 `input_resolver or InputResolver(...)` 会短路掉 resolver 自检，池将带着坍缩存活到
        # 后续任一 CRUD 才炸，且届时报的是与调用无关的既有 id。input_resolver 是**公开构造参数**，
        # 嵌入式宿主是一等消费者，不能只靠「测试才用」兜底。
        raise_on_env_name_collisions({i.id for i in self._inputs})
        # **embed scope 的声明面**（协议 §2.5-3 / §2.5-5，Discussion #32 裁决）：宿主构造入参 = 代码级显式意图。
        # The **embed-scope declaration surface**: the embedded host's constructor args = explicit code-level intent.
        #
        # ⚠️ 勿被历史注释误导：本字段**仅在此处赋值一次、全仓无第二处写入**，CLI 下恒空（`cli/main.py` 传
        # `mcp_servers=set()`，所有 server 走运行期挂载）。#149/#153 据此定性它为「构造期死快照」——那**依然成立**，
        # 指的是「MUST NOT 把它当**运行期活跃集**读」（它对运行期挂载不可见，读它会把「依赖已满足」误判为「未满足」；
        # 运行期权威见 `active_server_configs()` / `mcp_manager.server_configs()`，§2.5-4）。
        # 但 §2.5-5 **新增**要求：把它当 **embed 声明层**参与 resolve（见 `resolve_mcp_declarations`）。二者不矛盾——
        # 它在 CLI 下恒空不是缺陷，而是「CLI 不是嵌入式宿主」的正确表现。
        self._mcp_servers: set[MCPServerConfig] = set(mcp_servers or set())
        # flag 层 mcp.json 路径（`--mcp-config`）：与 `_mcp_servers` 同为当次 boot 的声明式输入（§2.5-5）。
        self._mcp_flag_config: Path | None = mcp_flag_config
        # #149：运行期活跃集的**声明面 raw 投影**缓存（bundle_id → raw-with-bundle 未渲染 config）。get_config wire
        # 从此取 body（占位符字面保留、绝不外泄已解析 secret）；SET 仍以 manager 运行期权威为准（见 active_server_configs）。
        # #149: raw (un-rendered) projection of the runtime-active set, keyed by bundle_id — the body source for the
        # client:get_config wire (placeholders kept literal, resolved secrets never leave the Computer). SET = manager authority.
        self._active_raw: dict[str, MCPServerConfig] = {}
        self._auto_connect = auto_connect
        self._auto_reconnect = auto_reconnect
        self._confirm_callback = confirm_callback
        # 中文: 按需解析器与渲染器（惰性解析 inputs，保持配置不可变）
        # English: Lazy input resolver and renderer (on-demand inputs, keep config immutable)
        self._input_resolver = input_resolver or InputResolver(self._inputs)
        self._config_render = ConfigRender()
        # 通过 weakref 存储 Socket.IO 客户端，避免与客户端互相强引用导致循环与内存泄露
        # Hold Socket.IO client via weakref to avoid strong reference cycle
        self._socketio_client_ref: weakref.ReferenceType[SMCPComputerClient] | None = None

        # 中文: 工具调用历史（仅保存最近10条），使用 asyncio.Lock 确保跨协程安全
        # English: Tool call history (keep last 10). Protected by asyncio.Lock for cross-coroutine safety
        self._tool_call_history: deque = deque(maxlen=10)
        self._tool_call_history_lock = asyncio.Lock()
        # 中文: 在途工具调用注册表（req_id → 承载 acall_tool 的可取消任务），用于响应 notify:tool_call_cancel（#96）。
        #   _cancelled_req_ids 标记「经 acancel_tool 显式取消」的 req_id，与外层协程自身被取消（连接断开/teardown）区分。
        #   Computer 仅单事件循环，容器读写均在 loop 内协程中发生，无需额外锁。
        # English: In-flight tool-call registry (req_id → cancellable task wrapping acall_tool) for notify:tool_call_cancel (#96).
        #   _cancelled_req_ids marks req_ids explicitly cancelled via acancel_tool, to distinguish from an outer-coroutine
        #   cancellation (connection drop/teardown). Single asyncio loop → no extra lock needed.
        self._inflight_tool_tasks: dict[str, asyncio.Task] = {}
        self._cancelled_req_ids: set[str] = set()
        # 窗口缓存（仅记录满足 WindowURI 的资源 URI，避免无关资源导致刷新）
        # Windows cache (only URIs that conform to WindowURI to avoid irrelevant refresh triggers)
        self._windows_cache: set[str] = set()

        # v0.2.1 SKILL 子系统 / v0.2.1 SKILL subsystem（设计 §5.1，#66）
        # 持有 name → A2CSkillRef 物化索引；SKILL Home 于 boot_up 解析落盘；skill:// 缓存仿窗口缓存。
        # Hold the materialized Registry; SKILL Home resolved at boot_up; skill:// cache mirrors windows cache.
        self._skill_registry: SkillRegistry = SkillRegistry()
        self._skill_home_override: Path | None = skill_home
        self._skill_home: Path | None = None
        self._skills_cache: set[str] = set()

        # v0.2.1 事件触发链（S14，#67，设计 §8）：多源 SKILL 变更 → 去抖器标脏 → 缓存失效 + 单次 emit。
        # user 源 DropIn 文件 watcher 递归监控 <home>/user/（#116 起仅 home 单根；workdir 维度已下沉 MCP 服务）。
        # The debouncer coalesces multi-source SKILL changes into a single emit; the file watcher feeds user-source changes.
        self._skill_debouncer: SkillEventDebouncer = SkillEventDebouncer(
            self._emit_update_skills_now,
            invalidate=self._invalidate_user_skills,
        )
        self._skill_watcher: SkillFileWatcher | None = None
        # 原生 Observer 不支持的 FS（网络挂载 / overlayfs）可经此环境变量切 PollingObserver 兜底。
        self._skill_watch_polling: bool = env_truthy("A2C_SKILL_WATCH_POLLING")

        # v0.2.1 通用二进制传输基础设施 / v0.2.1 generic blob-transfer infrastructure
        # 协议依据 / Protocol: blob-transfer.md；设计 / Design: §4.3 / §4.4
        self._blob_cache_root: Path = (blob_cache_root or Path.home() / ".a2c").expanduser().resolve()
        self._blob_cache_root.mkdir(parents=True, exist_ok=True)
        self._toolspool_store: ToolspoolBlobStore = ToolspoolBlobStore(self._blob_cache_root)
        # 默认 resolver 装配：toolspool 完整、skill 经 Registry + 沙箱重跑回源（#66 接管占位）
        # Default resolver wiring: toolspool complete; skill re-runs Registry + sandbox (replaces #39 placeholder)
        default_resolvers: dict[str, BlobResolver] = {
            "toolspool": ToolspoolBlobResolver(self._toolspool_store),
            "skill": SkillBlobResolver(self._skill_registry),
        }
        if blob_resolvers is not None:
            default_resolvers.update(blob_resolvers)
        self._blob_resolvers: dict[str, BlobResolver] = default_resolvers
        self._blob_thresholds: BlobThresholds = blob_thresholds or default_thresholds()

    # 工具调用历史类型已抽取到 a2c_smcp/computer/types.py 的 ToolCallRecord 供多处复用
    # The tool call record type is extracted to ToolCallRecord for reuse across modules

    @property
    def socketio_client(self) -> Optional["SMCPComputerClient"]:
        """
        获取当前绑定的 Socket.IO AsyncClient，如果已被销毁则返回 None。
        Get the currently bound Socket.IO AsyncClient; returns None if GC'ed.
        """
        return self._socketio_client_ref() if self._socketio_client_ref is not None else None

    # ------------------------
    # Socket.IO 客户端引用（weakref）/ Weak reference to Socket.IO client
    # ------------------------
    @socketio_client.setter
    def socketio_client(self, client: Optional["SMCPComputerClient"]) -> None:
        """
        设置（或清空）当前绑定的 Socket.IO AsyncClient 引用（以 weakref 方式存储）。
        Set (or clear) the bound Socket.IO AsyncClient reference (stored as weakref).

        Args:
            client (AsyncClient | None): 要绑定的客户端，None 表示清空。
        """
        self._socketio_client_ref = weakref.ref(client) if client is not None else None

    # ------------------------
    # 通用二进制传输 / Generic blob transfer (v0.2.1)
    # ------------------------
    @property
    def blob_resolvers(self) -> dict[str, BlobResolver]:
        """``kind → BlobResolver`` 派发表（``client:get_blob`` handler 使用）.

        Kind-to-resolver dispatch table consumed by the ``client:get_blob`` handler.
        """
        return self._blob_resolvers

    @property
    def blob_thresholds(self) -> BlobThresholds:
        """SKILL / blob 阈值（inline budget / too_large cap / chunk max）.

        Thresholds for inline budget, too-large cap, and chunk max bytes.
        """
        return self._blob_thresholds

    @property
    def toolspool_store(self) -> ToolspoolBlobStore:
        """``.blobspool`` 内容寻址暂存（``tool_call`` 二进制旁路铸造时写入）.

        Content-addressed ``.blobspool`` store; written when minting tool_call binary sideband.
        """
        return self._toolspool_store

    def mint_toolspool_handle(self, payload: bytes, mime: str) -> str:
        """铸造 ``kind=toolspool`` 不透明句柄并写盘 / Mint an opaque ``kind=toolspool`` handle.

        ``tool_call`` 返回的超内联预算二进制 content item 通过此入口写入 ``.blobspool``，
        返回的 handle 走 ``_meta.a2c_blob_handle`` 旁路交付 Agent（#40 接入）.
        Tool_call binary items that exceed the inline budget go through this entry, are written
        into ``.blobspool``, and the returned handle ships via ``_meta.a2c_blob_handle`` (#40).

        防御纵深 / Defense in depth (协议 ``blob-transfer.md`` §3 + 设计 §4.4):
            协议要求 too_large 在**铸造期**决断（不铸句柄、零字节传输；DoS 防御）。本入口即铸造期，
            ``len(payload) > thresholds.too_large_cap`` → 抛 :class:`BlobTooLargeError`，**不写盘**。
            上游 (#40) 应在其上再做内联预算 / 文本 vs 二进制路由判定；此处是兜底防御层。
            Protocol mandates too_large decided at minting time (no handle, zero bytes — DoS guard).
            This entry is that minting point; ``len(payload) > too_large_cap`` raises BlobTooLargeError
            **without writing to disk**. Upstream (#40) handles inline-budget routing; this is the
            fallback defense layer.

        Args:
            payload: 解码后的原始字节内容（**不是** base64）.
            mime: 内容 MIME（如 ``image/png``）.

        Returns:
            不透明 ``blob_handle`` 字符串.

        Raises:
            BlobTooLargeError: ``len(payload) > thresholds.too_large_cap`` —— 拒绝铸造，不写盘。
        """
        # 局部导入避免顶层循环：handle 编码 / Local import to avoid top-level cycles
        from a2c_smcp.computer.blob import encode_toolspool_handle

        size = len(payload)
        cap = self._blob_thresholds.too_large_cap
        if size > cap:
            raise BlobTooLargeError(size=size, cap=cap)
        cid = self._toolspool_store.put(payload, mime)
        return encode_toolspool_handle(cid=cid, mime=mime)

    async def boot_up(self, *, session: PromptSession | None = None) -> None:
        """
        启动计算机，初始化 MCP 服务器管理器。
        Boot up the computer and initialize the MCP server manager.

        1. 将 self._mcp_servers 逐个进行 model_dump 拿到dict配置，然后配合 self._inputs 进行ConfigRender。因为在具体配置中可能存在动态变量
            的引用。需要在此消解
        2. 通过当前类的 _resolve_prompt_string _resolve_pick_string _resolve_command 等方法对 MCPServerInput 做解析拿到最终结果进行替换
        3. 对于 self._mcp_servers 配置的符合变量提取模式但没有提供对应 input 定义时，不做任何处理，使用原值传递。
        """
        self.mcp_manager = MCPServerManager(
            auto_connect=self._auto_connect,
            auto_reconnect=self._auto_reconnect,
            message_handler=self._on_manager_change,
        )
        # 中文: 对每个 Server 配置执行：渲染(占位符解析链 + 预定义变量) + envFile 合并 + 校验生成不可变对象。
        #       统一复用 _arender_and_validate_server（DRY，#65）；失败时按稳妥策略保留原配置继续。
        # English: Render (resolution chain + predefined vars) + envFile merge + validate, reusing
        #          _arender_and_validate_server (DRY). On failure, keep the original config and continue.
        # #149：boot 重建运行期活跃集 → 先重置 raw 投影缓存，逐条登记（setdefault=first-wins，与 ainitialize
        # no-double-open 同序：同 bundle_id 保配置顺序首个）。
        self._active_raw.clear()
        validated_servers: list[MCPServerConfig] = []
        for server_cfg in self._mcp_servers:
            try:
                raw_cfg, validated = await self._arender_and_validate_server(server_cfg, session=session)
            except Exception as e:
                logger.error(f"配置渲染或校验失败: {getattr(server_cfg, 'name', 'unknown')} - {e}", exc_info=True)
                # 按稳妥策略: 保留原配置继续（raw == rendered == 原始未渲染 config）
                raw_cfg = validated = server_cfg
            validated_servers.append(validated)
            self._active_raw.setdefault(resolve_bundle_id(validated), raw_cfg)

        await self.mcp_manager.ainitialize(validated_servers)

        # v0.2.1 SKILL 子系统启动初始化（设计 §5.1，#66）：解析 SKILL Home → 治理启动恢复（#117）→
        # 物化 mcp 源 skill:// → 填充 Registry → 初始化 skill:// 缓存。失败隔离：记 ERROR、**不**阻断
        # Computer 启动（skill.md §1.5：SKILL 通道对部分失败健壮）。
        # SKILL subsystem boot init: resolve Home → governance recovery (#117) → materialize mcp-source
        # skills → seed cache. Failure-isolated: log ERROR, never block Computer boot.
        try:
            self._skill_home = self._resolve_skill_home()
        except Exception as e:  # pragma: no cover — 防御性兜底
            logger.error(f"SKILL Home 解析失败（不阻断 Computer 启动）/ SKILL Home resolve failed (non-blocking): {e}", exc_info=True)

        # #117/#123 治理启动恢复（协议 v0.3.0 §4.8.1，rust reconcile_governance 同位）：先跑 v0.2.x → v0.3.0
        # 一次性迁移（账本存量 → installedPlugins + enabledPlugins=true，保住升级前活跃态；标记键幂等），
        # 再从安装意图恢复活跃（installed ∧ enabled）plugin 的 bundled SKILL。**skills-only**——bundled MCP
        # server 不在 boot 拉进程（#93 client owns MCP config），client 经 reconcile_governance(hooks)
        # 显式重挂（设计 Y）。One-time legacy migration, then intent-driven skills-only recovery.
        if self._skill_home is not None:
            try:
                migrated = migrate_legacy_installs(self._skill_home)
                if migrated:
                    logger.info("v0.3.0 governance migration: %d legacy install(s) migrated to installedPlugins", len(migrated))
            except Exception as e:  # pragma: no cover — 失败隔离：迁移失败不阻断启动（下次 boot 重试）
                logger.warning(f"v0.3.0 治理迁移失败（不阻断启动）/ governance migration failed (non-blocking): {e}")
            try:
                recovery_report = await self.reconcile_governance()
                if recovery_report.restored_skills or recovery_report.failed_marketplaces:
                    logger.info(
                        "governance recovery: %d skills restored, %d plugins, %d marketplaces degraded",
                        len(recovery_report.restored_skills),
                        len(recovery_report.restored_plugins),
                        len(recovery_report.failed_marketplaces),
                    )
            except Exception as e:  # pragma: no cover — 防御性兜底（recovery 内部已各自降级）
                logger.error(f"治理启动恢复失败（不阻断启动）/ governance recovery failed (non-blocking): {e}", exc_info=True)

        try:
            await self._restage_mcp_skills()
            self._skills_cache = await self._acollect_skill_refs()
        except Exception as e:  # pragma: no cover — 防御性兜底，正常路径已在内部各自降级
            logger.error(f"SKILL 子系统启动初始化失败（不阻断 Computer 启动）/ SKILL boot init failed (non-blocking): {e}", exc_info=True)

        # v0.2.1 user 源 DropIn 就地发现 + 文件 watcher（S14，#67，设计 §8.3）：启动时全量扫 user 源 →
        # 注册；启动递归 watcher 监控 user/ + 各登记 workdir/.tfrobot/skills/，SKILL.md 变更经去抖器触发 emit。
        # 失败隔离：记 ERROR、**不**阻断 Computer 启动。
        # User-source in-place discovery + file watcher: initial full scan + start recursive watcher (failure-isolated).
        try:
            if self._skill_home is not None:
                await self._invalidate_user_skills()  # 初次全量发现 user 源 / initial full user-source discovery
                self._start_skill_watcher()
        except Exception as e:  # pragma: no cover — 防御性兜底
            logger.error(f"user 源 DropIn 发现 / watcher 启动失败（不阻断启动）/ user DropIn boot failed: {e}", exc_info=True)

    def _resolve_skill_home(self) -> Path:
        """解析并创建 SKILL Home（0o700 防御性写）/ Resolve & create SKILL Home。

        显式注入 ``skill_home`` 时经 ``A2C_SKILL_HOME`` 覆盖；否则走默认 env 解析链
        （``A2C_SKILL_HOME`` → ``$XDG_DATA_HOME/a2c/skills`` → ``~/.a2c/skills``）。
        """
        if self._skill_home_override is not None:
            return ensure_skill_home({"A2C_SKILL_HOME": str(self._skill_home_override)})
        return ensure_skill_home()

    async def _on_manager_change(
        self,
        message: RequestResponder[types.ServerRequest, types.ClientResult] | types.ServerNotification | Exception,
    ) -> None:
        """
        当 MCPServerManager 检测到变化时的回调。

        目前仅处理工具列表变化：若存在 Socket.IO 连接，则向服务端发送 UPDATE_TOOL_LIST_EVENT。
        其它变化类型暂未实现，打印 Warning 日志。
        """
        if isinstance(getattr(message, "root", None), ToolListChangedNotification):
            client = self.socketio_client
            if client is None:
                logger.debug("Socket.IO 客户端不存在或已释放，忽略更新上报")
                return
            try:
                # 直接通过事件常量发送（工具列表更新）
                # Directly emit tool list update event
                await client.emit_update_tool_list()
            except Exception as e:  # pragma: no cover
                logger.error(f"上报工具变更失败: {e}", exc_info=True)
        elif isinstance(getattr(message, "root", None), ResourceListChangedNotification):
            # 资源列表变化：window:// 与 skill:// **并行独立**处理（互不阻断，设计 §5.1）。
            # Resource list changed: window:// and skill:// handled in parallel & independently.
            client = self.socketio_client
            if client is None:
                logger.debug("Socket.IO 客户端不存在或已释放，忽略资源列表变化上报")
                return
            await self._on_resource_list_changed_windows(client)
            await self._on_resource_list_changed_skills()
        elif isinstance(getattr(message, "root", None), ResourceUpdatedNotification):
            # 资源内容更新按 scheme 分流：window:// → 桌面刷新；skill:// → 重物化并上报 SKILL 更新。
            # Resource content updated, dispatched by scheme: window:// → desktop; skill:// → restage skills.
            client = self.socketio_client
            if client is None:
                logger.debug("Socket.IO 客户端不存在或已释放，忽略资源更新上报")
                return
            uri = getattr(getattr(getattr(message, "root", None), "params", None), "uri", None)
            uri_str = str(uri) if uri is not None else ""
            if uri is not None and is_window_uri(uri_str):
                try:
                    await client.emit_refresh_desktop()
                except Exception as e:  # pragma: no cover
                    logger.error(f"上报桌面刷新失败: {e}")
            elif uri_str.startswith(_SKILL_URI_PREFIX):
                await self._on_skill_resource_updated()
            else:
                logger.debug("收到资源更新但非 window/skill scheme，放行 / Non-window/skill resource updated, ignore")
        else:
            logger.warning(f"收到未处理的变化类型: {truncate(message)}，当前版本仅处理工具列表变化")

    async def _on_resource_list_changed_windows(self, client: "SMCPComputerClient") -> None:
        """window:// 集合变化 → 比对缓存 → 触发桌面刷新（行为同 v0.2 既有逻辑，原样抽取）。

        window:// set changed → compare cache → trigger desktop refresh (behavior-neutral extraction).
        """
        try:
            new_windows = await self._acollect_window_uris()
        except Exception as e:  # pragma: no cover
            logger.error(f"收集窗口资源失败，跳过刷新: {e}", exc_info=True)
            return

        if new_windows != self._windows_cache:
            added = sorted(new_windows - self._windows_cache)
            removed = sorted(self._windows_cache - new_windows)
            logger.info(
                "WindowURI 列表发生变化，将触发桌面刷新 | Window list changed, refreshing desktop. "
                f"added={len(added)}, removed={len(removed)}",
            )
            if added:
                logger.debug(f"新增窗口: {added}")
            if removed:
                logger.debug(f"移除窗口: {removed}")
            self._windows_cache = new_windows
            try:
                await client.emit_refresh_desktop()
            except Exception as e:  # pragma: no cover
                logger.error(f"上报桌面刷新失败: {e}", exc_info=True)
        else:
            # 打印关键信息帮助开发者判断策略是否需要更新
            logger.info(
                "收到 ResourceListChangedNotification 但 WindowURI 未变化，跳过刷新 / "
                "Resource list changed but WindowURI set unchanged, skip refresh",
            )

    async def _on_resource_list_changed_skills(self) -> None:
        """skill:// 集合变化 → 重物化 mcp 源 + 孤儿对账 → 去抖器标脏（仿窗口缓存对比）。

        集合未变化则仅 DEBUG 跳过（设计 §5.1：``_acollect_skill_refs`` 集合相同跳过）。变化时重物化 mcp 源
        并 :meth:`SkillEventDebouncer.mark_dirty`——emit 经去抖器与其它源在 300ms 窗口内合并为一次（设计 §8.1）。
        skill:// set changed → restage mcp source + orphan reconcile → debouncer.mark_dirty; unchanged → DEBUG skip.
        """
        try:
            new_skills = await self._acollect_skill_refs()
        except Exception as e:  # pragma: no cover
            logger.error(f"收集 skill:// 资源失败，跳过 SKILL 刷新: {e}", exc_info=True)
            return

        if new_skills == self._skills_cache:
            logger.debug("收到 ResourceListChanged 但 skill:// 集合未变化，跳过 SKILL 重物化 / skill:// set unchanged, skip")
            return

        added = len(new_skills - self._skills_cache)
        removed = len(self._skills_cache - new_skills)
        logger.info(f"skill:// 列表发生变化，重物化并标脏 SKILL 更新 | skill:// list changed. added={added}, removed={removed}")
        try:
            await self._restage_mcp_skills()
            self._skills_cache = new_skills
            self._skill_debouncer.mark_dirty()
        except Exception as e:  # pragma: no cover
            logger.error(f"SKILL 重物化/标脏失败: {e}", exc_info=True)

    async def _on_skill_resource_updated(self) -> None:
        """skill:// 资源内容更新 → 重物化 + 去抖器标脏（不比较集合，降低延迟，仿 window ResourceUpdated）。

        单 SKILL 增量物化的 server 归属无法仅从 URI 推断，故走全量重物化（正确优先；增量为后续优化）。
        skill:// content updated → restage + debouncer.mark_dirty (no set compare, lower latency).
        """
        try:
            await self._restage_mcp_skills()
            self._skill_debouncer.mark_dirty()
        except Exception as e:  # pragma: no cover
            logger.error(f"SKILL 内容更新重物化/标脏失败: {e}", exc_info=True)

    async def _acollect_window_uris(self) -> set[str]:
        """
        收集当前所有 MCP Server 的 WindowURI 集合（去重）。
        Collect deduplicated set of WindowURIs across all active MCP servers.

        Returns:
            set[str]: WindowURI 集合
        """
        if not self.mcp_manager:
            return set()
        pairs = await self.mcp_manager.list_windows()
        # 仅保留符合 WindowURI 协议的资源
        return {str(res.uri) for _srv, res in pairs if is_window_uri(str(res.uri))}

    async def _acollect_skill_refs(self) -> set[str]:
        """
        收集当前所有 MCP Server 的 ``skill://`` 资源 URI 集合（去重）/ Collect deduplicated skill:// URI set。

        用于 ``ResourceListChanged`` 缓存对比（集合相同跳过重物化，仿 :meth:`_acollect_window_uris`）。
        ``manager.list_skill_resources`` 已按 ``skill://`` scheme 过滤并完整消费 cursor 翻页。

        Returns:
            set[str]: skill:// 资源 URI 集合（含子资源——任何变化都触发重物化，缓存对比更敏感）。
        """
        if not self.mcp_manager:
            return set()
        pairs = await self.mcp_manager.list_skill_resources()
        return {str(res.uri) for _srv, res in pairs}

    async def _restage_mcp_skills(self, bundle_id: str | None = None) -> list[str]:
        """
        物化 mcp 源 ``skill://`` → 注册进 :class:`SkillRegistry` / Materialize & register mcp-source skills。

        全量重物化（``bundle_id is None``）后做孤儿对账：本轮未出现的 mcp 源 SKILL → 标孤儿
        （从 ``get_skills`` 排除，保留以便 source 回归时恢复）。SKILL Home 未就绪 / 无 manager → 空列表。
        Full restage reconciles orphans: mcp-source skills absent this run are marked orphaned.

        Args:
            bundle_id: 若提供仅重物化该 server（单 server 重枚举）；否则全部活跃 server + 孤儿对账。

        Returns:
            list[str]: 本轮成功注册（或刷新）的 SKILL name 列表。
        """
        if not self.mcp_manager or self._skill_home is None:
            return []
        registered = await stage_mcp_skills(self.mcp_manager, self._skill_registry, self._skill_home, bundle_id=bundle_id)
        if bundle_id is None:
            self._reconcile_orphans(set(registered), lambda s: s.startswith("mcp:"))
        return registered

    def _reconcile_orphans(self, present_names: set[str], source_pred: Callable[[str], bool]) -> None:
        """
        全量重物化 / 重扫后的孤儿对账（按源谓词限定）/ Orphan reconciliation after a full restage/rescan。

        当前活跃、``source_pred(source)`` 命中、但本轮 ``present_names`` 未出现的 SKILL →
        :meth:`SkillRegistry.mark_orphan`（消失即从 ``get_skills`` 排除；恢复由 staging 的 ``register_or_update``
        命中孤儿条目自动完成）。``source_pred`` 把对账**限定在单一源**——mcp（``startswith("mcp:")``）/ user
        （``== SOURCE_USER``）/ 后续 marketplace（#61）各自传入谓词，互不误标。
        ``source_pred`` confines reconciliation to a single source so the others are never mis-orphaned.
        """
        for ref in self._skill_registry.active_refs():
            source = ref.get("source", "") or ""
            name = ref.get("name", "") or ""
            if name and name not in present_names and source_pred(source):
                self._skill_registry.mark_orphan(name)

    # ── v0.2.1 事件触发链：去抖结算 + user 源 watcher（S14，#67）────────────────
    async def _emit_update_skills_now(self) -> None:
        """
        去抖器结算末端：向信令服务器推送 ``server:update_skills`` / Debouncer settlement sink。

        无 Socket.IO 客户端 / 未入房间 → no-op（emit 的 office_id 守卫在 client 侧）。该协程是
        :class:`SkillEventDebouncer` 的 ``on_emit``，**不**应被事件处理器裸调（一律经去抖器 :meth:`mark_dirty`）。
        """
        client = self.socketio_client
        if client is None:
            logger.debug("Socket.IO 客户端不存在或已释放，跳过 SKILL 更新上报 / no client, skip update_skills")
            return
        await client.emit_update_skills()

    async def _invalidate_user_skills(self) -> None:
        """
        缓存失效（文件源重扫）：就地重扫 user 源 DropIn 并对账孤儿 / Invalidate by rescanning user-source DropIn。

        设计 §8.1「缓存失效」对**文件源**的落实——watcher/CLI 标脏后、emit 前重扫 ``<home>/user/``
        （``stage_user_skills`` 幂等 ``register_or_update``，#116 起仅 home 单根），并把本轮
        未发现的 user 源 SKILL 标孤儿（磁盘删除即从 ``get_skills`` 排除）。mcp 源由其 ``ResourceListChanged``
        处理器即时重物化，**不**在此重复。SKILL Home 未就绪 → no-op。

        **同步执行、不卸载线程池**：:class:`SkillRegistry` 按设计为单事件循环线程访问、无锁；``stage_user_skills``
        会写 Registry，若经 ``asyncio.to_thread`` 在 worker 线程改 ``_entries``，将与循环线程的 ``active_refs``
        迭代（``client:get_skills``）/ mcp 重物化产生数据竞争（``dict changed size during iteration`` / 撕裂写）。
        user DropIn 扫描仅遍历少量目录、读少量 ``SKILL.md``（亚毫秒~低毫秒），同步执行不构成有意义阻塞，
        并守住 Registry 单线程不变量。Run synchronously on the loop thread to preserve the Registry's
        single-thread (lock-free) invariant; the user-DropIn scan is tiny and does not meaningfully block.
        """
        if self._skill_home is None:
            return
        try:
            discovered = stage_user_skills(self._skill_registry, self._skill_home)
            self._reconcile_orphans(set(discovered), lambda s: s == SOURCE_USER)
        except Exception as e:  # pragma: no cover — 防御性兜底（staging 内部已各自降级）
            logger.error(f"user 源重扫 / 对账失败 / user-source rescan failed: {e}", exc_info=True)

    def _start_skill_watcher(self) -> None:
        """
        启动 user 源 DropIn 文件 watcher / Start the user-source DropIn file watcher。

        监控根 = ``<home>/user/``（递归、过滤 ``SKILL.md``，**不**监 marketplace clone 树；#116 起仅
        home 单根）。watchdog 回调在独立线程触发 → 经 ``loop.call_soon_threadsafe`` marshal 回事件
        循环线程调去抖器 :meth:`mark_dirty`。已有 watcher → 先停。SKILL Home 未就绪 → no-op。
        """
        if self._skill_home is None:
            return
        if self._skill_watcher is not None:
            self._skill_watcher.stop()
        loop = asyncio.get_running_loop()
        debouncer = self._skill_debouncer

        def _marshal() -> None:
            try:
                loop.call_soon_threadsafe(debouncer.mark_dirty)
            except RuntimeError:  # pragma: no cover — 事件循环已关闭（停机竞态）/ loop closed during shutdown
                pass

        watcher = SkillFileWatcher(_marshal, use_polling=self._skill_watch_polling)
        watcher.watch([user_dropin_root(self._skill_home)])
        self._skill_watcher = watcher

    def mark_skill_internal_write(self, path: str | Path) -> None:
        """
        给 SKILL 文件 watcher 打内部写标记 / Mark an internal write for the SKILL file watcher。

        供 CLI / SDK 在**写入被监控的 user 源 DropIn 路径**后调用，避免自写触发 watcher 重载循环（对标 CC
        ``markInternalWrite``）。watcher 未启动 → no-op。
        """
        if self._skill_watcher is not None:
            self._skill_watcher.mark_internal_write(path)

    def _render_variables(self) -> dict[str, str]:
        """
        预定义渲染变量（对标 VS Code，§9.1）/ Predefined render variables (VS Code parity)。

        ``userHome``=用户主目录；``pathSeparator``=``os.sep``。``${env:VAR}`` 不在此（由 render 直接读
        进程环境）。#116：``${workspaceFolder}`` 已随 workdir 概念瘦身停产（按未知占位符原样保留）。
        """
        return {
            "userHome": os.path.expanduser("~"),
            "pathSeparator": os.sep,
        }

    def _apply_env_file(self, rendered: dict[str, Any]) -> dict[str, Any]:
        """
        合并 ``envFile`` 的 ``KEY=VALUE`` 进 stdio server 的 ``env``（显式 env 胜，§9.1）/ Merge envFile into env。

        仅对 stdio（``server_parameters.env`` 存在）生效；sse/http 无 env 字段，原样返回。envFile **路径**
        已在 :meth:`ConfigRender.arender` 渲染（``${input:}`` 等已展开）。**显式 ``env`` 同名项覆盖
        envFile**（显式胜）。本方法不改 ``envFile`` 字段本身（spawn 仅消费 ``server_parameters.env``）。
        """
        env_file = rendered.get("envFile") or rendered.get("env_file")
        if not env_file or not isinstance(env_file, str):
            return rendered
        params = rendered.get("server_parameters")
        if not isinstance(params, dict) or ("env" not in params and "command" not in params):
            # 非 stdio（sse/http 无 env/command）→ envFile 不适用，记 WARN（已填但被忽略）+ 原样返回。
            # non-stdio (sse/http) → envFile not applicable; WARN that it is ignored, then passthrough.
            name = rendered.get("name", "unknown")
            srv_type = rendered.get("type", "unknown")
            logger.warning(f"envFile 在非 stdio（{srv_type}）server 上不适用，已忽略: {name} / envFile ignored on non-stdio server")
            return rendered
        file_env = load_env_file(Path(env_file))
        if not file_env:
            return rendered
        explicit_env = params.get("env") or {}
        # 显式 env 同名项胜：先铺 envFile，再覆盖以显式值 / explicit env wins over envFile
        params["env"] = {**file_env, **explicit_env}
        rendered["server_parameters"] = params
        return rendered

    async def _arender_and_validate_server(
        self,
        server: MCPServerConfig | dict[str, Any],
        *,
        session: PromptSession | None = None,
        plugin: str | None = None,
        marketplace: str | None = None,
    ) -> tuple[MCPServerConfig, MCPServerConfig]:
        """
        动态渲染并校验单个 MCP 服务器配置，支持原始字典或模型实例。
        Render and validate a single MCP server config dynamically, supporting raw dict or model instance.

        规则 Rules:
          - 使用 ConfigRender 对包含 ${input:...} 的占位符进行惰性渲染，解析时依赖当前 InputResolver。
          - 渲染后使用 Pydantic 校验并生成不可变模型对象（确保最终为类型安全且不可变）。

        Args:
          - server (MCPServerConfig | dict[str, Any]): 待处理的配置，可以是 Pydantic 模型或原始字典。| The config to process, can be
                a Pydantic model or a raw dict.
          - session (PromptSession | None, optional): 若渲染过程中需要交互式输入解析，使用的 Prompt 会话；可为空表示静默解析。 |
                Prompt session used for interactive resolving during rendering; can be None for silent resolving.

        Returns:
          - tuple[MCPServerConfig, MCPServerConfig]:
            中文: ``(raw_with_bundle, rendered_with_bundle)`` 二元组（#149）。两者 ``bundle_id`` 均物化为同一 derive-on-raw
                值；``raw`` = **未渲染** config（占位符字面保留，供 get_config wire 保真、绝不外泄已解析 secret），
                ``rendered`` = 渲染校验后的不可变 config（供 manager 物化 / spawn）。
            English: ``(raw_with_bundle, rendered_with_bundle)`` (#149). Both carry the same materialized ``bundle_id``;
                ``raw`` is the un-rendered config (placeholders kept, feeds the get_config wire — resolved secrets never
                leave the Computer), ``rendered`` is the validated immutable config (feeds manager materialization / spawn).

        Raises:
          - InputNotFoundError:
            中文: 当渲染中引用了未定义的 input 占位符时抛出（异常会被向上抛，由调用者决定回退策略）。
            English: Raised when an undefined input placeholder is referenced during rendering (propagated to caller).
          - Exception:
            中文: 其他渲染/校验阶段发生的异常，将被记录日志并继续向上抛出。
            English: Other exceptions during render/validation are logged and re-raised.

        Notes:
          - 中文: 若传入的是字典，将使用 TypeAdapter(MCPServerConfig) 进行模型解析；若为模型实例，则按其具体类型进行校验。
          - English: If input is a dict, TypeAdapter(MCPServerConfig) is used; if it's a model instance, its concrete type validates.
        """

        # 中文: 根据 input_id 解析输入值，未定义时抛出 InputNotFoundError
        # English: Resolve input value by input_id; raise InputNotFoundError if not defined
        async def _resolve_input_by_id(input_id: str) -> Any:
            try:
                # plugin/marketplace 上下文（#69 Group A）：bundled server 的裸 ${input:id} 经此回退到
                # 带前缀池条目 <plugin>@<marketplace>/<id>（§9.3 D2）。非 plugin 来源传 None=现状。
                # plugin/marketplace context lets a bundled server's bare ${input:id} fall back to the prefixed pool entry.
                return await self._input_resolver.aresolve_by_id(input_id, session=session, plugin=plugin, marketplace=marketplace)
            except InputNotFoundError:
                logger.warning(f"未定义的输入占位符: {input_id} / Undefined input placeholder: {input_id}")
                # 透传异常到上层，由上层决定是否回退或继续
                raise

        variables = self._render_variables()
        try:
            if isinstance(server, dict):
                raw = server
                # BundleID 取 raw（protocol#17）：注入前配置即身份来源。构 raw model 求 bundle_id
                # （占位 ${input:*} 按字面参与摘要）。注：raw 若在**非字符串**字段（如 timeout）含占位会在此
                # 提前校验失败——属边角（§9.4 占位面向字符串字段），正常配置不触及。
                raw_model: MCPServerConfig = TypeAdapter(MCPServerConfig).validate_python(raw)
                rendered = await self._config_render.arender(raw, _resolve_input_by_id, variables=variables)
                rendered = self._apply_env_file(rendered)
                # 使用 TypeAdapter 将 union 类型解析为具体模型
                validated: MCPServerConfig = TypeAdapter(MCPServerConfig).validate_python(rendered)
            else:
                raw = server.model_dump(mode="json")
                raw_model = server  # 模型入参即未渲染 raw（self._mcp_servers / 调用方原样）
                rendered = await self._config_render.arender(raw, _resolve_input_by_id, variables=variables)
                rendered = self._apply_env_file(rendered)
                validated = type(server).model_validate(rendered)
            # 物化 bundle_id：在 RAW 配置上 derive-on-load（protocol#15/#17），注入渲染后 config——
            # 使 config.bundle_id 解析后恒有值、作 no-double-open 去重键跨渲染阶段稳定。model_copy 不重跑
            # validator（resolved 已合法）。生成不在 Pydantic（config frozen）。
            resolved_bundle_id = resolve_bundle_id(raw_model)
            # #149：并列返回 (raw-with-bundle, rendered-with-bundle)。raw 供 get_config wire 保真（占位符字面保留），
            # rendered 供 manager 物化 / spawn。二者 bundle_id 均物化为同一 derive-on-raw 值（no-double-open 跨渲染稳定）。
            # DRY：复用同一次 raw_model / validated，不二次校验。
            return (
                raw_model.model_copy(update={"bundle_id": resolved_bundle_id}),
                validated.model_copy(update={"bundle_id": resolved_bundle_id}),
            )
        except Exception as e:
            name = (server.get("name") if isinstance(server, dict) else getattr(server, "name", "unknown")) or "unknown"
            logger.error(f"动态渲染/校验MCP配置失败: {name} - {e}", exc_info=True)
            raise e

    # ════════════════════════════════════════════════════════════════════════════
    # 双路径 MCP-server CRUD（#137 ②，父 #135：对齐 rust-sdk ``crates/smcp-computer/src/computer.rs``）
    # Dual-path MCP-server CRUD (aligns rust-sdk).
    #
    # 一句话分流规则（与 rust 完全同构）：**「用户此刻在声明一个 server」→ durable（落盘+重启存活）；
    # 「把别处已是真相的东西投影进 runtime」→ transient（纯运行期、不落盘）。**
    #   - durable  ：:meth:`aadd_or_aupdate_server` / :meth:`aadd_or_aupdate_server_in_scope` / :meth:`aremove_server`
    #                （REPL ``server add``/``rm``、外部 API 用户显式增删）。对齐 rust ``add_or_update_server*`` / ``remove_server``。
    #   - transient：:meth:`amount_server` / :meth:`aunmount_server_by_id`
    #                （boot 读已声明 mcp.json 挂载、``--config @file`` 加载、plugin/治理 D2 挂载/重挂）。对齐 rust
    #                ``mount_server`` / ``unmount_server_by_id``。
    #
    # 寻址（#143 / R4，协议 sdk-api-guidance §5.1）：本类**公开 API 一律收 bundle_id，无 name 启发式**。历史的
    #   ``aunmount_server(name)`` 便捷入口已**删除**（它是库层最后一个 name 入口；零生产调用方——plugin
    #   disable/uninstall/marketplace 级联走 ``aunmount_server_by_id``）。name→bundle_id 解析只存在于人机面
    #   :mod:`a2c_smcp.computer.cli.resolve`，那里未命中/多命中可交互报错；库层做启发式回退会让每个外部集成方
    #   各继承一次不可靠推断（name 空间与 id 空间在缺省派生下大面积重叠）。
    #
    # §12 R2 revision 分账（对齐 rust）：durable 落盘属 **config** 轴变化、transient 属纯 **capability** 轴。python 侧
    #   **capability** 上报由 manager 物化经 ``_on_manager_change`` → ``emit_update_tool_list`` **自动**触发（两路径皆有）；
    #   **config** 上报（``emit_update_config``/``notify:update_config``）由 **调用方（CLI/外部宿主）** 驱动——现状即
    #   REPL durable 路径 emit、boot/plugin transient 路径不 emit，恰好满足分账，故 SDK 层**不在** CRUD 方法内 emit
    #   config（保持既有「client owns Socket.IO 上报」分层，#93；rust 内部 bump 属实现细节，概念对齐即可，#135）。
    # ════════════════════════════════════════════════════════════════════════════

    async def _amount_rendered(self, raw_cfg: MCPServerConfig, validated: MCPServerConfig) -> None:
        """物化**已渲染校验**的 config 进 manager（内存投影 + capability 上报）/ Mount an already-rendered config。

        对齐 rust ``mount_rendered``：durable 与 transient 两路径的**共享核**——durable 落盘后复用同一次 render 结果
        经此物化，**避免重复触发** input/secret resolver 副作用（不二次渲染）。manager 惰性初始化于此单点。

        #149：``raw_cfg`` = 同一 server 的**未渲染 raw**（bundle_id 已物化），随物化登记进 ``_active_raw`` 供 get_config
        wire 取 body（占位符字面保留）。运行期同 bundle_id = 覆盖（与 manager ``_add_or_update`` 原地更新语义一致）。
        #149: ``raw_cfg`` is the un-rendered raw of the same server; register it into ``_active_raw`` for the wire body.
        """
        if self.mcp_manager is None:
            self.mcp_manager = MCPServerManager(
                auto_connect=self._auto_connect,
                auto_reconnect=self._auto_reconnect,
                message_handler=self._on_manager_change,
            )
        # 先物化进 manager，**成功后**再登记 raw（事务性一致：manager add 失败——如 server 活跃 ∧ 非 auto_reconnect
        # 抛 RuntimeError——则不留 attempted≠running 的 map 漂移；raw 恒未渲染故无安全影响，此为展示一致性加固）。
        # Register raw only after a successful manager add, so a failed update leaves no attempted≠running drift.
        await self.mcp_manager.aadd_or_aupdate_server(validated)
        self._active_raw[resolve_bundle_id(validated)] = raw_cfg

    @staticmethod
    def _raw_body_for_disk(server: MCPServerConfig | dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """从入参取**未渲染 raw** server 定义体（map key = ``name``，body 不含 name）/ Extract the un-rendered raw body。

        **D1 铁律**：durable 落盘取此 raw（占位 ``${input:}`` / ``${env:}`` 字面保留），**绝不写渲染后 secret**
        （值不离 Computer，§9.1）。dict 入参原样、model 入参 ``model_dump(mode="json")``；剥离 ``name``（身份由
        ``servers.<name>`` map key 承载，与 :func:`resolve_mcp_config` 读取契约一致，杜绝 name/key 冗余漂移）。
        """
        raw = dict(server) if isinstance(server, dict) else server.model_dump(mode="json")
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("server config must carry a non-empty 'name' for durable persistence")
        body = {k: v for k, v in raw.items() if k != "name"}
        return name, body

    # ── transient 路径（纯运行期，不落盘）/ transient path (runtime-only, no disk write) ──────────────

    async def amount_server(
        self,
        server: MCPServerConfig | dict[str, Any],
        *,
        session: PromptSession | None = None,
        plugin: str | None = None,
        marketplace: str | None = None,
    ) -> None:
        """**运行期挂载**一个 MCP server（渲染校验 → 物化，**不落盘**）/ Transient mount (no disk write)。

        对齐 rust ``mount_server``。语义 = 本 SDK 历史 ``aadd_or_aupdate_server`` 的**原状**（现已 flip 为 durable，
        故抽此新名承接纯运行期投影）。用于「把别处已是真相的东西投影进 runtime」：boot 读已声明 mcp.json 各 scope
        （含 ``--mcp-config`` flag 层）挂载、plugin/治理 bundled 重挂——这些**不应回写** mcp.json（否则双源 /
        每 boot 重写 / scope 漂移，见 #138）。只 bump **capability**（manager 物化自动上报），不碰持久 config。

        Args:
            server: 待挂载配置，可为模型或原始字典。
            session: Computer 管理 Session（交互式 input 解析用）。
            plugin / marketplace: plugin 实时挂载 D2 上下文（#69 Group A）；非 None 时 bundled server 的裸
                ``${input:id}`` 解析到带前缀池条目 ``<plugin>@<marketplace>/<id>``（§9.3 D2）。普通来源传 None。
        """
        raw_cfg, validated = await self._arender_and_validate_server(server, session=session, plugin=plugin, marketplace=marketplace)
        await self._amount_rendered(raw_cfg, validated)

    async def aunmount_server_by_id(self, bundle_id: str) -> None:
        """按 **bundle_id** 纯运行期**停摘**一个 server（不删声明、不落盘）/ Transient unmount by bundle_id。

        对齐 rust ``unmount_server_by_id``。manager 未建 / 无匹配 → no-op（避免 ``manager.aremove_server`` 的
        ``KeyError``）。durable :meth:`aremove_server` 停摘段复用本方法。
        """
        if self.mcp_manager is None:
            return
        if any(resolve_bundle_id(cfg) == bundle_id for cfg in self.mcp_manager.server_configs()):
            await self.mcp_manager.aremove_server(bundle_id)
            self._active_raw.pop(bundle_id, None)  # #149 hygiene（读时以 manager 集为准，非正确性关键）

    # ── durable 路径（落盘 raw + 重启存活）/ durable path (persist raw + survive restart) ─────────────

    async def aadd_or_aupdate_server(
        self,
        server: MCPServerConfig | dict[str, Any],
        *,
        session: PromptSession | None = None,
        plugin: str | None = None,
        marketplace: str | None = None,
    ) -> None:
        """**持久声明**一个 MCP server（默认落 **Local**）并物化 / Durably declare a server (defaults to Local)。

        对齐 rust ``add_or_update_server``（:1978 ``add_or_update_server_in_scope(server, Local)``）。**破坏性变更**
        （#135/#137）：历史此方法为纯运行期，现 flip 为持久——落盘 ``mcp.local.json``（不入 git、boot 读取、重启
        存活）+ 运行期物化。用于「用户此刻在声明一个 server」（REPL ``server add``、外部 API 用户新增）。纯运行期
        投影请改用 :meth:`amount_server`。

        Args 同 :meth:`amount_server`（保留 ``plugin`` / ``marketplace`` D2 上下文 kwargs）。
        """
        await self.aadd_or_aupdate_server_in_scope(
            server, McpWriteScope.LOCAL, session=session, plugin=plugin, marketplace=marketplace,
        )

    async def aadd_or_aupdate_server_in_scope(
        self,
        server: MCPServerConfig | dict[str, Any],
        scope: McpWriteScope,
        *,
        session: PromptSession | None = None,
        plugin: str | None = None,
        marketplace: str | None = None,
    ) -> None:
        """**持久声明**一个 MCP server 到**显式 scope** 并物化 / Durably declare a server into an explicit scope。

        对齐 rust ``add_or_update_server_in_scope``（:1992）。``scope`` ∈ ``Local``（``mcp.local.json`` 不入 git）/
        ``Project``（``mcp.json`` 入 git、团队共享）/ ``User``（``$XDG_CONFIG_HOME/a2c`` 全局）。**改已有 server 恒落其
        origin scope**（① :func:`upsert_mcp_server` 已保证，``scope`` 仅对新声明生效，杜绝跨 scope 漂移）。

        流程（对齐 rust）：**先 render 校验**（早失败、不落盘）→ 取**未渲染 raw**（D1）→ 经 ① 落盘 raw → **复用**同一
        次 render 结果 :meth:`_amount_rendered` 物化（不二次渲染）。落盘属 config 轴、物化属 capability 轴（分账见类内
        双路径块注释）。

        :raises McpConfigCorruptError: 目标 ``mcp.json`` 结构损坏（① 拒绝覆盖以免销毁既有内容）。
        """
        # 1. 先渲染校验（早失败：损坏配置在落盘前抛出，绝不留下盘上声明而运行期未挂的半态）。
        raw_cfg, validated = await self._arender_and_validate_server(server, session=session, plugin=plugin, marketplace=marketplace)
        # 2. 取未渲染 raw 落盘（D1：占位字面保留、绝不写 secret）。
        name, raw_body = self._raw_body_for_disk(server)
        upsert_mcp_server(name, raw_body, scope=scope, env=os.environ)
        # 3. 复用 render 结果物化（capability 自动上报；config 上报由调用方驱动，见分账注释）。
        await self._amount_rendered(raw_cfg, validated)

    def resolve_mcp_declarations(self, *, env: Mapping[str, str] | None = None) -> ResolvedMcpConfig:
        """当次 boot 的**非-plugin 声明面**（协议 §2.5-5 权威集的非-plugin 部分）/ This boot's non-plugin declaration surface。

        = :func:`resolve_mcp_config` 全量层 + 本 Computer 的两条 **boot 声明式输入**：
        ``--mcp-config``（flag 层，:attr:`_mcp_flag_config`）与宿主构造入参（embed 层，:attr:`_mcp_servers`）。
        产出条目**均携带正确 origin**，origin 恒 ∈ ``{user, project, local, embed, flag, policy}``（结构性非-plugin，
        见 :data:`~a2c_smcp.computer.settings.schema.SCOPE_ORDER`）。

        §2.5-5 要求该集合「MUST 可从当次 boot 的声明式输入重建、MUST NOT 落盘为快照」——本方法即**每次重算**、
        零持久态，天然满足。

        **与 `active_server_configs()` 的分工**（勿混）：本方法是**声明面**（谁被声明了、以何 origin）；
        ``active_server_configs()`` / ``mcp_manager.server_configs()`` 是**运行期活跃集**（谁真的挂起来了）。
        依赖预检与 wire 投影读后者（§2.5-4）；回收判据的「X 非用户声明」读前者（§4.9.1-2）。
        """
        return resolve_mcp_config(
            env=env,
            flag_config_path=self._mcp_flag_config,
            embed_servers=self._mcp_servers,
        )

    async def aremove_server(self, bundle_id: str, *, session: PromptSession | None = None) -> None:
        """**持久删除**一个 MCP server 声明（bundle_id → 声明名 → 删所有可写 scope）+ 运行期停摘 / Durably remove。

        对齐 rust ``remove_server``（:2038）。**破坏性变更**（#135/#137）：历史仅运行期停摘（→ 复活 footgun：人写在
        ``mcp.json`` 的 server 重启复活），现 flip 为持久删除。

        **守卫按 origin 判定，不按账本名集**（#148 / 指南 §5，取代历史 name-keyed bundled 拒删——那会误伤与某已装
        plugin bundled server 同名的用户 server）：

        1. **声明优先**：经 :meth:`resolve_mcp_declarations` 取**快照**（含 flag/embed 层），收集 ``bundle_id``
           匹配的声明名（:func:`resolve_bundle_id` 逐条比对）。
        2. **有声明 ∧ origin 可写（user/project/local）⇒ 放行**：经 :func:`remove_mcp_server` 删**所有可写 scope**
           + :meth:`aunmount_server_by_id` 停摘。删的是用户自己那条声明——即便它与某 plugin bundled server 同
           bundle_id，用户覆盖权优先（runtime-contract §2.5「用户主权」），plugin 基线保留。

           *为何 origin 可写 ⇒ 删干净*：只读 scope（embed/flag/policy）在 §2.5-3 序中**全部高于**三个可写 scope
           ⇒ origin 若是可写 scope，该名在只读层**必然无声明**（否则 origin 就会是那个更高的只读 scope）⇒ 删尽可写
           scope 即删尽该名的全部声明。
        3. **有声明 ∧ origin 只读（embed/flag/policy）⇒ 拒删**（抛 :class:`McpWriteTargetError`）：胜出的那条声明
           SDK **删不掉**（policy 企业托管 / flag 命令行文件 / embed 宿主构造入参根本不在盘上）。若仍走档 2 会
           **静默假成功**——``remove_mcp_server`` 无声可删、``aunmount`` 停摘、返回成功，而下次 boot 该声明原样复活
           （#143 正在根治的同类假成功）。故显式报错并指出真正的关停途径。
        4. **无声明 ∧ 运行期仍有该 bundle_id 活跃投影 ⇒ 拒删**（抛 :class:`McpWriteTargetError`）：它是**纯运行期
           投影**——「挂载却不落声明」的生产路径为 plugin/治理重挂（#137③）与临时 :meth:`amount_server`，均不入
           :func:`resolve_mcp_config`。durable rm 只操作声明面，对无声明的投影拒删——若属 plugin 应经
           ``plugin uninstall`` **整体**停用（单独打掉某 bundled server 产生 §2.4 禁止的半态），若属纯 transient
           应经 :meth:`aunmount_server_by_id` 停摘。**架构限制**：``origin == plugin`` 不进 resolve（结构性缺席，见
           :data:`~a2c_smcp.computer.settings.schema.SCOPE_ORDER`）故仍不可精确区分，保守拒删 = 指南 §5
           「``origin == plugin`` ∧ 无用户侧声明」的**可观测等价**（宁可拒删导向显式停用，也不越权停摘）。
        5. **无声明 ∧ 未活跃 ⇒ no-op**（无声明可删、无投影可停；``aunmount_server_by_id`` 幂等 no-op）。

        Args:
            bundle_id (str): MCP Server 唯一身份 bundle_id（协议 #18）。**入参即身份，无 name 启发式**（R4）——
                历史此处曾注「REPL ``server rm <name>`` 在缺省身份下（bundle_id = normalize(name)）以 name 寻址
                即可」，该前提**只在 name 本身已属 ``[A-Za-z0-9_-]`` 时成立**，正是 #143 静默假成功的病根，已作废。
                人机面的 ``<name|bundle_id>`` 解析见 :mod:`a2c_smcp.computer.cli.resolve`。

        Raises:
            McpWriteTargetError: 档 3（声明只存在于只读 scope）或档 4（纯运行期投影）。
        """
        env = os.environ
        # 1. 声明优先：快照解析声明名（未渲染 config，derive-on-raw 与注册边界同源）。含 flag/embed 层（§2.5-5）。
        snapshot = self.resolve_mcp_declarations(env=env)
        matched = {name: srv.origin for name, srv in snapshot.servers.items() if resolve_bundle_id(srv.config) == bundle_id}
        if matched:
            # 3. 胜出声明落只读 scope ⇒ 删不掉，拒删而非静默假成功（见 docstring 档 3）。
            readonly = {name: origin for name, origin in matched.items() if not is_writable_origin(origin)}
            if readonly:
                detail = ", ".join(f"{name!r} (origin={origin.value})" for name, origin in sorted(readonly.items()))
                raise McpWriteTargetError(
                    f"bundle_id={bundle_id!r} is declared in a read-only scope and cannot be durably removed: {detail}. "
                    "A 'policy' declaration is enterprise-managed (edit managed-mcp.json); a 'flag' declaration comes "
                    "from this run's '--mcp-config <file>' (drop it from that file or from the command line); an "
                    "'embed' declaration comes from the embedding host's Computer(mcp_servers=...) constructor "
                    "argument. To only stop it for this run, unmount the projection instead.",
                )
            # 2. origin 可写 ⇒ 放行：删所有可写 scope + 停摘（用户覆盖权，即便与某 bundled 同 bundle_id 亦可删）。
            for name in matched:
                remove_mcp_server(name, env=env)
            await self.aunmount_server_by_id(bundle_id)
            return
        # 4. 无声明 ∧ 运行期仍活跃 ⇒ plugin/治理投影，拒删、导向 plugin uninstall（origin==plugin 的可观测等价）。
        if self.mcp_manager is not None and any(
            resolve_bundle_id(cfg) == bundle_id for cfg in self.mcp_manager.server_configs()
        ):
            raise McpWriteTargetError(
                f"bundle_id={bundle_id!r} has no mcp.json declaration but is active at runtime; "
                "it is a runtime-only projection (a plugin/governance mount, or an ad-hoc amount_server), "
                "not a durably-declared server—if it is plugin-provided use 'plugin uninstall' to disable the "
                "whole plugin; otherwise unmount the transient projection directly instead of removing it via mcp.json",
            )
        # 4. 无声明且未活跃 ⇒ no-op（aunmount_server_by_id 幂等：manager None 或不含该 id 时安全 no-op）。
        await self.aunmount_server_by_id(bundle_id)

    def update_inputs(self, inputs: set[MCPServerInput], *, session: PromptSession | None = None) -> None:
        """
        更新 inputs 定义，并清空解析缓存。
        Update inputs definition and clear resolver cache.

        注意：更新 inputs 只会影响后续的渲染，不会自动对已激活的配置进行重新渲染/重启。
        如需应用到已存在的服务，可结合 aadd_or_aupdate_server 重新提交配置达到热更新效果。

        :raises EnvNameCollisionError: 新池内两个 id 撞同一 env 名（#155 F4）；此时**本实例状态完全不变**。
        """
        # 拷贝而非持有入参引用：池已带「无坍缩」不变量（#155），别名会让调用方事后 `s.add(...)` 把坍缩
        # 塞进池且完全绕过校验。与 `__init__` / `add_or_update_input` 的拷贝语义对齐。
        candidate = set(inputs or set())
        # 复用传入或已有的会话，以便后续解析共享同一 Session
        # Reuse provided or existing session so subsequent resolving shares the same session
        sess = session or getattr(self._input_resolver, "session", None)
        # 先构造（坍缩在此抛，#155）、成功后再一并赋值——否则异常会留下「池已换 + resolver 是旧的」的裂开状态。
        # Construct first (collision raises here), assign only on success: keeps state intact on rejection.
        resolver = InputResolver(candidate, session=sess)
        self._inputs = candidate
        self._input_resolver = resolver
        # 清理缓存，确保后续渲染使用最新 inputs
        self._input_resolver.clear_cache()

    def add_or_update_input(self, input_cfg: MCPServerInput, *, session: PromptSession | None = None) -> None:
        """
        按 id 动态新增或更新单个 input。
        Add or update a single input by id dynamically.

        规则 Rules:
          - 以 input.id 为唯一键，存在则替换，不存在则追加
          - 重新构建 InputResolver 并清空对应缓存，确保后续渲染拿到最新值

        :raises EnvNameCollisionError: 新 id 与池内既有 id 撞同一 env 名（#155 F4）；此时**本实例状态完全不变**。
        """
        if not input_cfg or not getattr(input_cfg, "id", None):
            logger.warning("无效的 input 配置，忽略 / Invalid input config, skip")
            return

        # 在**副本**上算新池：由于 __hash__ 与 __eq__ 基于 id，先丢弃再添加可实现“更新”。
        # 用副本是为了坍缩被拒时不污染 self._inputs（#155）。
        candidate = set(self._inputs)
        candidate.discard(input_cfg)
        candidate.add(input_cfg)

        # 重新初始化解析器以应用最新定义，并清理该 id 的缓存
        sess = session or getattr(self._input_resolver, "session", None)
        # 先构造（坍缩在此抛）、成功后再一并赋值，保证拒绝路径上状态不变。
        resolver = InputResolver(candidate, session=sess)
        self._inputs = candidate
        self._input_resolver = resolver
        self._input_resolver.clear_cache(input_cfg.id)

    def remove_input(self, input_id: str, *, session: PromptSession | None = None) -> bool:
        """
        按 id 移除单个 input，返回是否删除成功。
        Remove a single input by id. Returns whether deletion happened.
        """
        if not input_id:
            return False

        # 注：此处**刻意**不做 add/update 那套「先构造再赋值」——纯删除只会减少 env 名，不可能新增坍缩
        # （#155）。这是推理结论而非疏漏，勿「顺手对齐」写出无意义代码。
        removed = False
        target = None
        for existed in self._inputs:
            if existed.id == input_id:
                target = existed
                break
        if target is not None:
            self._inputs.discard(target)
            removed = True

        # 重新初始化解析器，并清理该 id 的缓存（如果有）
        sess = session or getattr(self._input_resolver, "session", None)
        self._input_resolver = InputResolver(self._inputs, session=sess)
        self._input_resolver.clear_cache(input_id)
        return removed

    def get_input(self, input_id: str, *, session: PromptSession | None = None) -> MCPServerInput | None:
        """
        获取指定 id 的 input 定义（只读）。
        Get input definition by id (read-only).
        """
        if not input_id:
            return None
        for existed in self._inputs:
            if existed.id == input_id:
                return existed
        return None

    def list_inputs(self, *, session: PromptSession | None = None) -> tuple[MCPServerInput, ...]:
        """
        列出当前全部 inputs（不可变）。
        List all current inputs (immutable).
        """
        return tuple(self._inputs)

    # ------------------------
    # 当前 inputs 值（缓存）增删改查 / CRUD for current input values (cache)
    # ------------------------
    def get_input_value(self, input_id: str, *, session: PromptSession | None = None) -> Any | None:
        """
        中文: 获取指定 id 的当前已解析值（来自缓存）。若尚未解析，则返回 None。
        English: Get current resolved value for given id from cache. Returns None if not resolved yet.
        """
        return self._input_resolver.get_cached_value(input_id)

    def set_input_value(self, input_id: str, value: Any, *, session: PromptSession | None = None) -> bool:
        """
        中文: 设置指定 id 的当前值（写入缓存）。仅当该 id 在 inputs 定义中存在时生效，返回是否成功。
        English: Set current value for given id (write to cache). Only works if id exists in inputs; returns success.
        """
        return self._input_resolver.set_cached_value(input_id, value)

    def remove_input_value(self, input_id: str, *, session: PromptSession | None = None) -> bool:
        """
        中文: 删除指定 id 的当前缓存值，返回是否删除发生。
        English: Delete current cached value for given id. Returns whether deletion happened.
        """
        return self._input_resolver.delete_cached_value(input_id)

    def list_input_values(self, *, session: PromptSession | None = None) -> dict[str, Any]:
        """
        中文: 列出所有已解析的 inputs 当前值（缓存快照）。若无则返回空字典。
        English: List all resolved input values (cache snapshot). Returns empty dict if none.
        """
        return self._input_resolver.list_cached_values()

    def clear_input_values(self, input_id: str | None = None, *, session: PromptSession | None = None) -> None:
        """
        中文: 清空所有或指定 id 的输入值缓存。
        English: Clear all cached values or the specified id.
        """
        self._input_resolver.clear_cache(input_id)

    async def shutdown(self, *, session: PromptSession | None = None) -> None:
        """
        关闭计算机，关闭 MCP 服务器管理器。
        Shutdown the computer and close the MCP server manager.

        v0.2.1（#67）：先停 user 源文件 watcher（不再产生新事件），再关去抖器（丢弃挂起 emit），最后关
        MCP 管理器。Stop the file watcher, close the debouncer (drop pending emit), then the MCP manager.
        """
        if self._skill_watcher is not None:
            self._skill_watcher.stop()
            self._skill_watcher = None
        await self._skill_debouncer.aclose()
        if self.mcp_manager:
            await self.mcp_manager.aclose()
        self.mcp_manager = None
        self._active_raw.clear()  # #149：运行期活跃集销毁 → 清空 raw 投影缓存

    async def __aenter__(self) -> "Computer":
        """
        异步上下文进入方法。
        Async context enter method.

        Returns:
            Computer: 当前实例。Current instance.
        """
        await self.boot_up()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object | None) -> None:
        """
        异步上下文退出方法。
        Async context exit method.

        Args:
            exc_type (type[BaseException] | None): 异常类型。Exception type.
            exc_val (BaseException | None): 异常值。Exception value.
            exc_tb (object | None): 异常追踪。Exception traceback.
        """
        await self.shutdown()

    @property
    def mcp_servers(self) -> tuple[MCPServerConfig, ...]:
        """
        获取 MCP 服务器配置（不可变）。
        Get MCP server config (immutable).

        Returns:
            tuple[MCPServerConfig, ...]: 配置字典。Config dict.
        """
        return tuple(self._mcp_servers)

    def active_server_configs(self) -> tuple[MCPServerConfig, ...]:
        """运行期活跃 MCP server 配置集的**声明面 raw 投影**（``client:get_config`` wire 权威，#149 / F2 / PROTO-2）。

        Runtime-active MCP server configs projected to their **raw (un-rendered) declared form** — the authority for
        the ``client:get_config`` wire projection (#149 / F2 / PROTO-2).

        - **SET** 取 :meth:`MCPServerManager.server_configs`（运行期权威——含 boot 物化 / :meth:`amount_server` 动态挂载 /
          plugin 治理重挂项；**非**构造期死快照 :attr:`mcp_servers`——后者 CLI 空集构造下恒空，正是 #149 P0 病根）。
        - 每条 **body** 取 :attr:`_active_raw` 里的**未渲染 raw**（``${input:}`` / ``${env:}`` 字面保留、**绝不外泄已解析
          secret**，§9.1 值不离 Computer）。manager 存的是渲染后 config（供 spawn），故此处按 bundle_id join 回 raw。
        - manager 未建（pre-boot）→ 空元组（无运行期活跃集）。

        不变式：每个运行期活跃 bundle_id 必有 raw 记录（boot 与 :meth:`_amount_rendered` 两挂载漏斗均登记）。若缺失
        （某挂载路径漏登记的 Bug）→ **fail-closed**：从 wire **省略**该 server（大声 WARN 可诊断），**绝不**回退渲染后
        config——那会把已解析 secret 送上「MUST 为 raw」的 wire（§9.1）。宁可 Agent 少看到一个 server，也不泄漏一把
        secret。正常路径两漏斗均登记 raw，不触发。

        区别于 :attr:`mcp_servers`（构造期快照）与 :meth:`list_mcp_servers_with_metadata`（含 pre-boot 声明回退的诊断
        视图）：本方法是**纯运行期活跃集 + raw body** 的 wire 投影，无构造期回退。
        """
        if self.mcp_manager is None:
            return ()
        out: list[MCPServerConfig] = []
        for cfg in self.mcp_manager.server_configs():
            bundle_id = resolve_bundle_id(cfg)
            raw = self._active_raw.get(bundle_id)
            if raw is None:
                # fail-closed（安全纵深）：缺 raw 记录 = 某挂载漏斗漏登记的 Bug。**省略**该 server、绝不回退 ``cfg``
                # （渲染后 config，含已解析 secret）——避免在「MUST 为 raw」的 wire 上泄漏 secret。大声 WARN 供诊断。
                # Fail-closed: omit the server rather than leak the rendered config's resolved secrets onto a raw-only wire.
                logger.warning(
                    f"active_server_configs: 运行期活跃 bundle_id={bundle_id!r} 无 raw 记录，**从 get_config 省略**该 server"
                    f"（某挂载路径未登记 raw，属 Bug；绝不回退渲染值以免 secret 外泄）/ omitting server (no raw record)",
                )
                continue
            out.append(raw)
        return tuple(out)

    @property
    def inputs(self) -> tuple[MCPServerInput, ...]:
        """
        获取 MCP 服务配置中的动态字段定义（不可变视图）。内部以 set 管理，返回 tuple 快照。
        Get Inputs in MCP server config (immutable view). Internally managed as a set, returns a tuple snapshot.

        Returns:
            tuple[MCPServerInput, ...]: 动态字段定义。Inputs
        """
        return tuple(self._inputs)

    async def aget_available_tools(self) -> list[SMCPTool]:
        """
        获取可用工具列表。
        Get available tools list.

        Returns:
            list[SMCPTool]: 工具列表。Tool list.
        """
        if not self.mcp_manager:
            raise RuntimeError("当前MCP Manger为空")
        # #127：服务 client:get_tools 前先刷新工具映射。MCP 运行期 tools/list_changed 时，manager 的 boot 期
        # _tool_mapping 已陈旧——available_tools() 迭代该映射键，**新增**工具不在其中永远漏掉。本方法运行于
        # socketio on_get_tools 安全上下文（非 MCP 接收循环），可安全 await 刷新（内联刷新会话级死锁见 #127）。
        # #127: refresh the tool mapping before serving get_tools; runtime tool additions are otherwise missed.
        await self.mcp_manager.arefresh_tools()
        # 从 Manager 获取全部工具，携带各自归属 bundle_id（#152 D1）。
        tools = [(bundle_id, t) async for bundle_id, t in self.mcp_manager.available_tools()]

        def is_attr(v: Any) -> bool:
            """
            判断值是否为简单属性。
            Check if value is a simple attribute.

            Args:
                v (Any): 待检测值。Value to check.

            Returns:
                bool: 是否为简单属性。Whether it is simple attribute.
            """
            try:
                TypeAdapter(AttributeValue).validate_python(v)
                return True
            except Exception as e:
                logger.debug(f"非简单属性:{truncate(v)}", exc_info=e)
                return False

        def convert_tool(bundle_id: str, t: Tool) -> SMCPTool:
            """
            将 MCP 工具定义转换为 SMCP 工具定义。
            Convert MCP tool definition to SMCP tool definition.

            Args:
                bundle_id (str): 该工具所属 MCP Server 的**解析后** bundle_id（#152 D1，工具归属）。
                t (Tool): MCP 工具。MCP tool.

            Returns:
                SMCPTool: SMCP 工具。SMCP tool.
            """
            meta = {}
            if t.meta:
                for k, v in t.meta.items():
                    if not is_attr(v):
                        try:
                            # 无论是 BaseModel 还是其他复杂类型，都序列化为 JSON 字符串
                            # Serialize both BaseModel and other complex types to JSON string
                            meta[k] = json.dumps(v.model_dump(mode="json")) if isinstance(v, BaseModel) else json.dumps(v)
                        except Exception as e:
                            logger.error(f"无法序列化工具元数据{k}:{v}", exc_info=e)
                            meta[k] = str(v)
                    else:
                        meta[k] = v
            if t.annotations:
                meta["MCP_TOOL_ANNOTATION"] = json.dumps(t.annotations.model_dump(mode="json"))
            return SMCPTool(
                name=t.name,
                bundle_id=bundle_id,  # #152 D1：显式归属（解析后值，来自 available_tools 产出，非 config.bundle_id）
                description=t.description or "None",
                params_schema=t.inputSchema,
                return_schema=t.outputSchema,
                meta=meta,
            )

        mcp_tools = [convert_tool(bundle_id, t) for bundle_id, t in tools]
        return mcp_tools

    async def aexecute_tool(self, req_id: str, tool_name: str, parameters: dict, timeout: float | None = None) -> CallToolResult:
        """
        调用工具。主要通过Manager实现对MCP Server的调用，但是在此进行比如auto_apply的判断，如果有需要用户二次确认的设计，在此实现。二次确认
            的方法由初始化时注入

        Args:
            req_id (str): 请求ID
            tool_name (str): 工具名称
            parameters (str): 工具调用参数
            timeout (float | None): 超时时间限制

        Returns:
            CallToolResult: MCP协议的标准返回
        """
        if not self.mcp_manager:
            raise RuntimeError("当前MCP Manager为空")
        # 中文: 统一记录输出，保证任何返回路径都能记录历史
        # English: Unify return to ensure we always record call history
        bundle_id, tool_name = await self.mcp_manager.avalidate_tool_call(tool_name, parameters)

        ts = datetime.now(UTC).isoformat()
        success: bool = False
        error_msg: str | None = None

        try:
            # 中文: 通过 Manager 获取合并后的 ToolMeta（specific 优先，缺失字段回落 default_tool_meta）
            # English: Use Manager to get merged ToolMeta (specific overrides; fallback to default_tool_meta)
            merged_meta = self.mcp_manager.get_tool_meta(bundle_id, tool_name)

            # 中文: 仅当合并结果的 auto_apply 显式为 True 时直接执行；否则进入二次确认流程
            # English: Only execute directly if merged auto_apply is explicitly True; otherwise require confirmation
            if merged_meta is not None and merged_meta.auto_apply is True:
                result = await self._acall_tool_cancellable(req_id, bundle_id, tool_name, parameters, timeout)
            else:
                # 除非明确允许 auto_apply 否则均需要调用二次确认回调进行确认
                # Unless auto_apply is explicitly allowed, require confirm callback
                if self._confirm_callback:
                    try:
                        apply = self._confirm_callback(req_id, bundle_id, tool_name, parameters)
                    except TimeoutError:
                        # 二次确认「等待」超时（工具从未执行）：刻意**不**打 ``a2c_timeout`` 标记——协议
                        # error-handling.md「Computer 端超时」语义界定为工具**执行**超时，此处属确认等待超时，
                        # 对 Agent 等价于「普通失败」桶，故无标记。区别于下方 ``except TimeoutError`` 的执行超时分支。
                        # Confirm-callback *wait* timeout (tool never executed): intentionally NOT marked with
                        # ``a2c_timeout`` — the protocol scopes "Computer-side timeout" to tool *execution* timeout,
                        # while this is an approval-wait timeout that maps to the ordinary-failure bucket for the Agent.
                        result = CallToolResult(
                            content=[TextContent(text="当前工具需要用户二次确认是否可以调用，当前确认超时。", type="text")],
                            isError=True,
                        )
                    except Exception as e:
                        logger.error(f"工具确认回调，调用失败:{e}", exc_info=True)
                        error_msg = str(e)
                        result = CallToolResult(
                            content=[TextContent(text=f"在工具调用二次确认时发生异常，异常信息：{e}", type="text")],
                            isError=True,
                        )
                    else:
                        if apply:
                            result = await self._acall_tool_cancellable(req_id, bundle_id, tool_name, parameters, timeout)
                        else:
                            result = CallToolResult(content=[TextContent(text="工具调用二次确认被拒绝，请稍后再试", type="text")])
                else:
                    result = CallToolResult(
                        content=[
                            TextContent(
                                text="当前工具需要调用前进行二次确认，但客户端目前没有实现二次确认回调方法。请联系用户反馈此问题",
                                type="text",
                            ),
                        ],
                        isError=True,
                    )

            success = not result.isError
        except TimeoutError as e:
            # 中文: 工具执行超时（``Manager.acall_tool`` 的 ``asyncio.wait_for`` 抛 TimeoutError，经
            #   ``_acall_tool_cancellable`` 透传至此）。按协议（32eea98 / protocol#5）写入结果级
            #   ``meta.a2c_timeout``，使 Agent 能把「超时」与「普通失败 / 取消」区分开（专门分支，避免把任意异常误标超时）。
            # English: Tool execution timeout (``asyncio.wait_for`` in ``Manager.acall_tool`` raises TimeoutError,
            #   relayed here via ``_acall_tool_cancellable``). Per protocol (32eea98 / protocol#5) write result-level
            #   ``meta.a2c_timeout`` so the Agent can tell timeout apart from ordinary failure / cancellation.
            #   A dedicated branch (not the generic fallback) avoids mislabeling arbitrary errors as timeouts.
            error_msg = str(e)
            result = CallToolResult(
                content=[TextContent(text=f"工具调用超时 / Tool call timeout: {e}", type="text")],
                isError=True,
            )
            result.meta = {"a2c_timeout": True}  # 真实 meta 字段，出线 key=meta / real field, wire key ``meta``
            success = False
        except Exception as e:  # pragma: no cover
            # 中文: 兜底异常，转换为错误结果并记录
            # English: Fallback for unexpected exception; convert to error result and record
            error_msg = str(e)
            result = CallToolResult(
                content=[TextContent(text=f"调用异常: {e}", type="text")],
                isError=True,
            )
            success = False
        finally:
            # 显式取消（acancel_tool 标记本 req_id，由 _acall_tool_cancellable 吞 CancelledError 返回取消态）：
            # 历史标记为 cancelled，使「被取消」可与其它失败区分；并在此清理标记。
            # Explicit cancel: mark history as cancelled (distinguishable from other failures) and clear the marker.
            if req_id in self._cancelled_req_ids:
                self._cancelled_req_ids.discard(req_id)
                success = False
                error_msg = error_msg or "cancelled"
            await self._append_tool_history(
                {
                    "timestamp": ts,
                    "req_id": req_id,
                    "server": bundle_id,
                    "tool": tool_name,
                    "parameters": parameters,
                    "timeout": timeout,
                    "success": success,
                    "error": error_msg,
                },
            )

        return result

    async def _acall_tool_cancellable(
        self,
        req_id: str,
        bundle_id: str,
        tool_name: str,
        parameters: dict,
        timeout: float | None,
    ) -> CallToolResult:
        """中文: 将 ``Manager.acall_tool`` 包装为「可被 :meth:`acancel_tool` 中断」的在途任务（#96）。

        - 在 ``_inflight_tool_tasks[req_id]`` 登记承载任务；
        - 若经 :meth:`acancel_tool` 显式取消（``req_id`` 入 ``_cancelled_req_ids`` 且**外层协程自身未被取消**）：
          吞掉 ``CancelledError`` 并返回取消态 ``CallToolResult(isError=True)``（即成为原 ``client:tool_call`` 的 ack）；
        - 若**外层协程自身**被取消（连接断开 / teardown）：确保承载任务退场后向上重抛，绝不伪装成结果；
        - 任何路径 ``finally`` 都清理注册表与取消标记。

        判别器：``asyncio.current_task().cancelling()``（外层是否被取消）AND ``req_id in _cancelled_req_ids``
        （是否本 req_id 被显式取消）双重确认才吞，杜绝把真实外层取消误判为取消态结果。

        取消语义边界：``inner.cancel()`` 取消承载任务，会经 ``base_client.call_tool`` best-effort 向远端补发 MCP
        ``notifications/cancelled``（见 :meth:`~a2c_smcp.computer.mcp_clients.base_client.BaseMCPClient._emit_mcp_cancelled`，
        #96 最后一公里）。但远端是否真正停止执行**仍不保证**——MCP 取消为**协作式**，server 可忽略该通知并跑完。即 Agent
        能迅速拿到取消态响应，远端通常被中断，但不作硬保证。

        English: Wrap ``Manager.acall_tool`` as a cancellable in-flight task so ``notify:tool_call_cancel`` can
        interrupt it (#96). Explicit cancel → swallow & return a cancelled result; outer-coroutine cancel →
        re-raise after teardown; always clean up the registry. The discriminator combines
        ``current_task().cancelling()`` with the ``_cancelled_req_ids`` marker. Cancelling the task makes
        ``base_client.call_tool`` best-effort emit MCP ``notifications/cancelled`` to the remote
        (see ``BaseMCPClient._emit_mcp_cancelled``); whether the REMOTE server actually stops is still NOT
        guaranteed (MCP cancellation is cooperative — the server may ignore it).
        """
        if self.mcp_manager is None:
            raise RuntimeError("当前MCP Manager为空")
        inner: asyncio.Task[CallToolResult] = asyncio.ensure_future(
            self.mcp_manager.acall_tool(bundle_id, tool_name, parameters, timeout),
        )
        self._inflight_tool_tasks[req_id] = inner
        try:
            return await inner
        except asyncio.CancelledError:
            current = asyncio.current_task()
            outer_cancelled = bool(current is not None and current.cancelling())
            if not outer_cancelled and req_id in self._cancelled_req_ids:
                # 显式工具取消：吞掉异常并返回取消态结果 / explicit tool cancel: swallow & return cancelled result
                # 协议已标准化取消 ack（a2c-smcp-protocol 32eea98 / protocol#5）：在结果级 ``meta`` 写入
                # ``a2c_cancelled``（MUST）+ ``a2c_cancel_reason``（SHOULD，本路径仅经 ``acancel_tool`` ↔
                # ``notify:tool_call_cancel`` 触达，语义恒为 agent 请求），使 Agent 能区分「取消 / 超时 / 普通失败」。
                # Protocol-standardized cancel ack (a2c-smcp-protocol 32eea98 / protocol#5): write result-level
                # ``meta`` markers ``a2c_cancelled`` (MUST) + ``a2c_cancel_reason`` (SHOULD; this path is only
                # reached via ``acancel_tool`` ↔ ``notify:tool_call_cancel``, so the reason is always agent-requested)
                # so the Agent can distinguish cancellation from timeout / ordinary failure.
                # 经 ``.meta`` 属性赋值写入真实 meta 字段（出线默认 dump→key=meta），与 Manager 透传 A2C_TOOL_META 同构。
                # Set via the ``.meta`` attribute (real field; default dump → wire key ``meta``), mirroring how the
                # Manager carries ``A2C_TOOL_META``. NB: passing ``meta=`` to the ctor would land in ``extra`` (alias
                # is ``_meta``) and leave ``result.meta`` None.
                cancelled_result = CallToolResult(
                    content=[TextContent(text="工具调用已被取消 / Tool call cancelled", type="text")],
                    isError=True,
                )
                cancelled_result.meta = {"a2c_cancelled": True, "a2c_cancel_reason": "agent_requested"}
                return cancelled_result
            # 外层协程自身被取消：确保承载任务退场后向上传播 / outer cancellation: ensure teardown then re-raise
            if not inner.done():
                inner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await inner
            raise
        finally:
            # 仅清理在途任务注册表；``_cancelled_req_ids`` 标记留给 :meth:`aexecute_tool` 的 finally 读取后清理，
            # 以便历史能区分「被取消」与其它失败（见 aexecute_tool）。
            # Only clear the in-flight registry here; the ``_cancelled_req_ids`` marker is read & cleared by
            # aexecute_tool's finally so history can distinguish "cancelled" from other failures.
            self._inflight_tool_tasks.pop(req_id, None)

    async def acancel_tool(self, req_id: str) -> bool:
        """中文: 取消一个在途工具调用（响应 ``notify:tool_call_cancel``，#96）。

        Args:
            req_id (str): 被取消的请求 ID（``AgentCallData.req_id``）。

        Returns:
            bool: ``True`` 表示已对一个在途任务请求取消；``False`` 表示该 ``req_id`` 未知或已完成（幂等 no-op）。

        English: Cancel an in-flight tool call (handles ``notify:tool_call_cancel``). Returns ``False`` as an
        idempotent no-op when ``req_id`` is unknown or already finished.

        说明：取消承载任务后会向远端 best-effort 补发 MCP ``notifications/cancelled``，但远端是否真正停止**仍不保证**
        （MCP 取消为协作式，server 可忽略；详见 :meth:`_acall_tool_cancellable`）。
        Note: cancelling the task best-effort emits MCP ``notifications/cancelled`` to the remote, but whether the
        remote actually stops is still NOT guaranteed (cooperative cancellation; see :meth:`_acall_tool_cancellable`).
        """
        task = self._inflight_tool_tasks.get(req_id)
        if task is None or task.done():
            return False
        self._cancelled_req_ids.add(req_id)
        task.cancel()
        return True

    async def get_resources(self, mcp_server: str, cursor: str | None = None) -> tuple[list[Resource], str | None]:
        """
        中文: 单页透传指定 MCP Server 的 `resources/list`，供 v0.2 `client:get_resources` 使用。
        英文: Single-page transparent forward of a server's `resources/list`, for v0.2 `client:get_resources`.

        Computer 仅作透传层：不做 scheme / 元数据过滤、不做跨 Server 聚合；翻页由调用方通过 cursor 控制。
        Computer is a transparent layer: no scheme/metadata filtering, no cross-server aggregation;
        pagination is caller-driven via cursor.

        Args:
            mcp_server (str): 目标 MCP Server 的 bundle_id（= get_config servers 字典 key，协议 #18）/ Target server bundle_id.
            cursor (str | None): MCP 标准翻页游标；首次传 None / MCP pagination cursor; None for first page.

        Returns:
            tuple[list[Resource], str | None]: (本页资源, 下一页游标——None 表示末页) /
                (resources on this page, next cursor — None when last page).

        Raises:
            RuntimeError: MCP Manager 尚未初始化 / MCP Manager not initialized.
            MCPServerNotFoundError: mcp_server 未注册（→ 处理器映射 4014）/ not registered (→ handler maps 4014).
            MCPCapabilityNotSupportedError: 未声明 `resources` 能力（→ 处理器映射 4015）/
                `resources` capability not declared (→ handler maps 4015).
        """
        if not self.mcp_manager:
            raise RuntimeError("当前MCP Manager为空 / MCP Manager is not initialized")
        return await self.mcp_manager.list_resources(mcp_server, cursor)

    # ------------------------
    # SKILL 通道委托 / SKILL channel delegation (v0.2.1, #66)
    # 协议依据 / Protocol: skill.md §6 / §9；设计 / Design: §5.1
    # ------------------------
    @property
    def skill_registry(self) -> SkillRegistry:
        """name → A2CSkillRef 物化索引（``client:get_skills`` / ``get_skill`` / ``get_blob`` 经此解析）。

        The materialized name → A2CSkillRef index consumed by the SKILL channel handlers.
        """
        return self._skill_registry

    @property
    def skill_home(self) -> Path:
        """SKILL Home 绝对根（``boot_up`` 解析；未启动时按 env 解析链兜底并缓存）/ SKILL Home root。

        CLI marketplace / skill 命令经此取物化根（``known_marketplaces.json`` / ``installed_plugins.json`` /
        ``marketplace/<name>/`` clone 树所在）。
        """
        if self._skill_home is None:
            self._skill_home = self._resolve_skill_home()
        return self._skill_home

    def mark_skills_dirty(self) -> None:
        """标记 SKILL 集合变更 → 去抖器在窗口内合并触发单次 ``emit_update_skills``（CLI marketplace 变更后调）。

        Mark the SKILL set dirty so the debouncer coalesces a single emit (called after CLI marketplace mutations).
        """
        self._skill_debouncer.mark_dirty()

    def _resolve_declared_settings(self) -> dict[str, Any]:
        """治理恢复的 declared 合并视图（user/project/local/policy；无 flag——Computer 不持 ``--settings`` 知识）。

        已知限制（与 rust 同构文档化）：flag scope 的 ``enabledPlugins`` 不在此视图；CLI 接线时经
        ``reconcile_governance(declared=...)`` 显式传 flag-aware 视图。跨重启可靠 disable 请写 user scope。
        The declared view for governance recovery (no flag scope at boot); pass flag-aware ``declared`` explicitly.
        """
        from a2c_smcp.computer.settings.policy import resolve_policy_settings
        from a2c_smcp.computer.settings.scope import resolve_settings

        return resolve_settings(policy_settings=resolve_policy_settings()).settings

    async def reconcile_governance(
        self,
        *,
        existing_bundle_ids: Callable[[], set[str]] | None = None,
        register_server: RegisterBundledServer | None = None,
        inject_inputs: Callable[[BundledServerRecord], Awaitable[None]] | None = None,
        declared: Mapping[str, Any] | None = None,
    ) -> GovernanceRecoveryReport:
        """
        治理启动恢复公共入口（#117/#123，协议 v0.3.0 §4.8；两 SDK 同构契约"设计 Y"）/ Governance recovery entry。

        阶段一（恒执行）：从安装意图恢复**活跃**（installed ∧ ``enabledPlugins[id] is True``，缺省翻转）plugin
        的 bundled SKILL 进当前 Registry，并重物化账本缺失的 installed pid（intent 驱动、additive-only、幂等、
        失败降级不抛——见 :mod:`~a2c_smcp.computer.settings.recovery`；``installed_disabled`` 恢复为惰性）。
        阶段二（仅当给 ``register_server``）：client 显式重挂 bundled MCP server——逐 plugin 根先
        ``inject_inputs(record)``（携归属上下文做 plugin inputs 前缀化注入，bundled server 的
        ``${input:}`` D2 渲染前置；每 plugin 根仅一次），再逐 server 经 ``register_server(config, record)``
        携归属上下文注册；**同 ``bundle_id`` 已存在 = 依赖已满足 → skip 不覆盖**（协议 §2.5-1「复用既有实例」，
        additive-only，用户配置胜）；单 server 失败 → WARN 不阻断其余。

        boot 默认（``boot_up`` 内无 hooks 调用）= skills-only：bundled MCP server 不在 boot 拉进程
        （#93 client owns MCP config），仅经 :func:`collect_enabled_bundled_servers` 可查询；重挂永远是
        client 的显式意志（CLI 为参考 client 实现，外部 GUI/client 调用同一入口）。

        :param existing_bundle_ids: 现存 server 的 **bundle_id** 集工厂（依赖已满足判定）。数据源 MUST 是
            **运行期权威配置集**（``{resolve_bundle_id(cfg) for cfg in comp.mcp_manager.server_configs()}``，
            如 :func:`~a2c_smcp.computer.cli.commands.build_mcp_callbacks`），**MUST NOT** 读构造期快照
            ``Computer.mcp_servers``——它在 CLI 下恒空（协议 §2.5-4），会把「依赖已满足」误判为「未满足」。
            ⚠️ ``None`` = **不判**，同 bundle_id 的 server 会经 ``register_server`` 覆盖既有配置——client
            重挂时**应当传入**，仅在明确无既有面的受控场景省略。
        :param register_server: 重挂回调 ``(config, record) -> Awaitable``；``None`` = skills-only。
        :param inject_inputs: plugin inputs 注入回调（入参含归属的 record）；每 plugin 根仅调一次。
        :param declared: 合并声明视图覆盖（``installedPlugins`` + ``enabledPlugins`` 两键权威；
            ``None`` = 现算 user/project/local/policy）。
        :return: :class:`GovernanceRecoveryReport`（``remounted_servers`` 仅阶段二填充）。
        """
        home = self.skill_home
        if declared is None:
            declared = self._resolve_declared_settings()

        report = await recover_marketplace_skills(self._skill_registry, home, declared)
        if report.restored_skills:
            self.mark_skills_dirty()

        if register_server is None:
            return report

        existing = set(existing_bundle_ids()) if existing_bundle_ids is not None else set()
        injected_roots: set[Path] = set()
        for record in collect_enabled_bundled_servers(home, declared):
            # 身份 = bundle_id：按 display name 判会误伤「同名异 id」——协议 §5.6 明定其为**合法共存**，
            # MUST NOT 视为冲突；同 bundle_id 才是「依赖已满足」（§2.5-1），此时复用既有实例、不覆盖。
            bundle_id = resolve_bundle_id(record.config)
            if bundle_id in existing:
                logger.info(
                    "governance recovery: dependency satisfied for bundle_id %r (plugin %s); reusing the existing "
                    "server instead of remounting (existing wins)",
                    bundle_id,
                    record.plugin_id,
                )
                continue
            try:
                if inject_inputs is not None and record.install_path not in injected_roots:
                    await inject_inputs(record)
                    injected_roots.add(record.install_path)
                await register_server(record.config, record)
            except Exception as e:  # 单 server 失败隔离 / per-server failure isolation
                logger.warning(
                    "governance recovery: failed to remount bundled server %r (plugin %s): %s",
                    bundle_id,
                    record.plugin_id,
                    e,
                )
                continue
            existing.add(bundle_id)
            report.remounted_servers.append(bundle_id)
        return report

    def list_mcp_servers_with_metadata(self) -> list[McpServerWithMetadata]:
        """列出 MCP 服务器 + 归属 / 生命周期元数据（活跃 inventory，#121；#144 迁 bundle_id 主键，对齐 rust-sdk #97）。

        面向 client（如 ``tfrobot-client``）Skill / MCP tab：一次拿到「当前 Computer 有哪些 MCP server（**身份 =
        ``bundle_id``**，client 据此关联回 ``client:get_config.servers``（bundle_id 为 key）与工具
        ``{bundle_id}__{tool}``）+ 每条归谁（user vs plugin，含 marketplace / plugin / pluginId）+ 能否从普通
        MCP tab 编辑 / 启停」，**无需**读 SDK ledger、**无需**解析 plugin manifest、**无需**持内存 ownership map。
        协议依据 a2c-smcp-protocol v0.3.0 §4.8（归属 = boot 纯函数、每次可复现；enabled bundled server 进程未拉起
        也须可查询；「已启用」= installed ∧ ``enabledPlugins[id] is True``，``installed_disabled`` 不进本投影，§2.4）
        + Discussion #23 F1/F2。元数据类型见 :mod:`~a2c_smcp.computer.inventory`，**SDK-facing、不进** Agent-facing
        ``client:*`` wire。

        合并两个来源（**按 bundle_id 去重**，运行期条目优先）：

        1. 运行期活跃配置集——manager 已建时取 ``MCPServerManager.server_configs()`` 快照（构造期声明经 boot 物化项
           + ``aadd_or_aupdate_server`` 动态挂载项 + client 经 ``reconcile_governance(hooks)`` 重挂的 plugin bundled
           项，§2.5-4 运行期权威）；manager 未建（pre-boot）回退构造期 embed 声明集 ``self._mcp_servers``。
        2. ledger 派生的**已启用但尚未物化**的 plugin bundled server（boot 默认 ``register_server=None`` 后即此态）——
           补入 inventory，满足 §4.8「进程未拉起也可观测」（client 据此物化或引导 Marketplace）。

        **归属 F1 纯推导**（``managed_by``，Discussion #23 F1）：``bundle_id ∈ 非-plugin 声明面 ⇒ user，否则若被 plugin
        声明 ⇒ plugin``。「非-plugin 声明面」= :meth:`resolve_mcp_declarations`（携 ``origin``，结构性 ∈
        ``{user/project/local/embed/flag/policy}``）的 bundle_id 集。故用户自己声明的 server **永远 user 主权**（可编辑），
        **即便它与某 plugin 依赖同 ``bundle_id``**（§2.5 用户主权）——这正是 #144 纠正的旧「同名误标 plugin 只读」缺陷；
        纯运行期 transient 投影（无声明、非 bundled）落 user（无 plugin 元数据可构造 ``McpPluginOwnership``，与人机面
        :func:`~a2c_smcp.computer.cli.resolve.collect_candidates` 的 ``runtime`` 归属可观测等价）。

        结果按 ``bundle_id`` 排序（唯一全序、稳定可测；``name`` 可碰撞非全序）。**不**含运行期「进程是否已启动」状态——
        那由 ``MCPServerManager.get_server_status`` 单独提供。异 ``bundle_id`` 的同名 server 合法共存（§5.6），各自独立成条。

        .. note::
           **flag-scope 结构性差异（非 #144 缺陷）**：本方法读核心层 flag-less :meth:`_resolve_declared_settings`
           判「谁是 enabled plugin」（Computer 结构上不持 ``--settings`` flag 知识，与 rust 同构文档化），故经
           ``--settings`` flag 启用的 plugin 不在本视图；人机面 :func:`~a2c_smcp.computer.cli.resolve.collect_candidates`
           读 flag-aware 视图。此差异是核心 / CLI 边界的既有限制。
        """
        home = self.skill_home
        # F1「∃ origin != plugin 的声明」判据的 bundle_id 集 = 非-plugin 声明面（携 origin，结构性 ∈ 非-plugin：
        # durable + flag --mcp-config + embed）。与 `aremove_server` / `cli.resolve.collect_candidates` 同接缝。
        non_plugin_bundle_ids = {
            resolve_bundle_id(srv.config) for srv in self.resolve_mcp_declarations(env=os.environ).servers.values()
        }
        # ledger 派生的已启用 bundled server（归属纯函数，与 reconcile_governance 同解析视图），**按 bundle_id 为键**（#144）。
        declared = self._resolve_declared_settings()
        bundled: dict[str, BundledServerRecord] = {
            resolve_bundle_id(record.config): record for record in collect_enabled_bundled_servers(home, declared)
        }

        def ownership_for(bundle_id: str) -> McpOwnership:
            # F1 纯推导：有非-plugin 声明 ⇒ user（用户主权，即便与某 plugin 依赖同 bundle_id，§2.5）；否则若被 plugin
            # 声明 ⇒ plugin；纯运行期 transient 孤儿（无 plugin 元数据可构造 McpPluginOwnership）⇒ user（同 collect_candidates
            # 的 runtime 归属可观测等价）。
            record = bundled.get(bundle_id)
            if record is not None and bundle_id not in non_plugin_bundle_ids:
                return McpPluginOwnership(marketplace=record.marketplace, plugin=record.plugin, plugin_id=record.plugin_id)
            return McpUserOwnership()

        out: list[McpServerWithMetadata] = []
        materialized: set[str] = set()

        # 来源一：运行期活跃配置集。manager 已建 = 权威（含动态挂载/重挂项；`_mcp_servers` 仅构造期声明快照，
        # 此后不回写）；未建（pre-boot）回退构造集。**按 bundle_id 去重**（no-double-open ⇒ 运行期集本就唯一）。
        active_configs = self.mcp_manager.server_configs() if self.mcp_manager is not None else tuple(self._mcp_servers)
        for cfg in active_configs:
            bundle_id = resolve_bundle_id(cfg)
            materialized.add(bundle_id)
            out.append(
                McpServerWithMetadata.assemble(cfg.name, bundle_id=bundle_id, disabled=cfg.disabled, managed_by=ownership_for(bundle_id))
            )

        # 来源二：已启用但尚未物化的 bundled server（bundle_id 不在运行期集 → 补入；§4.8 可观测）。
        for bundle_id, record in bundled.items():
            if bundle_id not in materialized:
                out.append(
                    McpServerWithMetadata.assemble(
                        record.config.name, bundle_id=bundle_id, disabled=record.config.disabled, managed_by=ownership_for(bundle_id)
                    )
                )

        out.sort(key=lambda entry: entry.bundle_id)
        return out

    def get_skills(self) -> list[A2CSkillRef]:
        """当前已安装且可用 SKILL（排除孤儿；不排序、不去重）—— ``client:get_skills`` 数据源。

        Active installed SKILLs (orphans excluded; unsorted, undeduped) — source for client:get_skills.
        """
        return self._skill_registry.active_refs()

    def get_skill_ref(self, name: str) -> A2CSkillRef | None:
        """O(1) 活跃精确解析 ``name`` → :class:`A2CSkillRef`（孤儿 / 未注册 → ``None``）。

        这是 name→包根的唯一解析入口（§9.2 name 寻址防越权）。handler 据 ``None`` 回 ``4014``。
        Single name→package-root resolution entry (§9.2); handler maps ``None`` to ``4014``.
        """
        return self._skill_registry.resolve(name)

    def read_skill_resource(self, ref: A2CSkillRef, rel_path: str | None) -> SkillResourceView:
        """沙箱解析 SKILL 包内资源 → 消费字节视图（铸造期带 ``too_large`` 守卫）。

        §9.2：包根**仅**取自 ``ref["path"]``（由 Registry 经 name 解析），:func:`resolve_skill_view`
        只接受 ``root: Path``，绝不从 name / rel_path 推导 FS 路径。``total_size`` / ``sha256`` 基于
        消费字节（SKILL.md→frontmatter 剥离后 body；其它→原始字节），与 ``get_blob`` 解析期一致。

        Args:
            ref: 来自 :meth:`get_skill_ref` 的活跃 A2CSkillRef（``path`` 必为可读绝对包根）。
            rel_path: 包根内 POSIX 相对路径；``None`` / 空 → 入口 ``SKILL.md``。

        Returns:
            SkillResourceView: 消费字节视图（mime / total_size / sha256 / is_text / 切片）。

        Raises:
            SkillSandboxError: 沙箱拒绝（traversal/forbidden/not_found）或 too_large（→ handler 映射 4017）。
        """
        root = Path(ref["path"])
        return resolve_skill_view(root, rel_path, too_large_cap=self._blob_thresholds.too_large_cap)

    async def get_desktop(self, size: int | None = None, window_uri: str | None = None) -> list[Desktop]:
        """
        获取当前Computer的桌面布局信息。桌面内容由各MCP工具的特定Resources组成。
        Get the desktop layout/content by aggregating MCP window resources.

        Args:
            size (int | None): 可选，限制返回的桌面窗口组合长度；不填则返回全部。
                               Optional max number of windows to include; None for all.
            window_uri (str | None): 可选，若指定则优先获取该 URI 对应的窗口。
                                     Optional specific WindowURI to fetch; otherwise organize all.

        Returns:
            list[Desktop]: 桌面组合列表。List of Desktop strings.
        """
        if not self.mcp_manager:
            logger.warning("MCP 管理器尚未初始化，返回空桌面 / MCP manager not initialized, return empty desktop")
            return []

        # 1) 从 Manager 拉取窗口资源“及其详情”（含归属 server 的 bundle_id——desktop 按 bundle_id 分组，协议 #18）
        #    Fetch window resources WITH their details (and the owning server's bundle_id)
        windows = await self.mcp_manager.get_windows_details(window_uri)

        # 2) 读取近期工具调用历史，供组织策略使用
        #    Read recent tool call history for organizing policy
        history = await self.aget_tool_call_history()

        # 3) 调用抽象组织函数（考虑资源详情进行组织，例如过滤无内容的窗口、按优先级等）
        #    Delegate to organizing policy (consider contents, e.g., filter empty, keep priority ordering)
        desktops = await organize_desktop(windows=windows, size=size, history=history)
        return desktops

    # ------------------------
    # 工具调用历史接口 / Tool call history APIs
    # ------------------------
    async def _append_tool_history(self, record: ToolCallRecord) -> None:
        """
        中文: 追加一条工具调用历史（保持最多10条）。协程安全。
        English: Append one tool call record (keep last 10). Coroutine-safe.
        """
        async with self._tool_call_history_lock:
            self._tool_call_history.append(record)

    async def aget_tool_call_history(self) -> tuple[ToolCallRecord, ...]:
        """
        中文: 获取只读的工具调用历史（按时间先后，最多10条）。
        English: Get read-only tool call history (chronological, up to 10).
        """
        async with self._tool_call_history_lock:
            return tuple(self._tool_call_history)
