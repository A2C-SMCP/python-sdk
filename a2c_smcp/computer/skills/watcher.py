# -*- coding: utf-8 -*-
# filename: watcher.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 文件 watcher：监控 user 源 DropIn 发现根，SKILL.md 变更 → 去抖 emit（v0.2.1）
SKILL file watcher: watch user-source DropIn roots, SKILL.md change → debounced emit (v0.2.1)

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §8.3。

监控范围 / Scope（§8.3）：
- 监控根（**递归**）= ``$A2C_SKILL_HOME/user/`` + **全部已登记** ``<workdir>/.tfrobot/skills/``（能力发现层、
  全局并集、不随 active workdir 切换）；过滤器为 ``**/SKILL.md``。**绝不监** marketplace clone 树
  （``<home>/marketplace/<mp>/...``）——clone 树是物化产物，变更只经 CLI 操作发生（操作自调去抖标脏），
  监控它会引发 ``git pull`` 雪崩并破坏「意图层 / 物化层」单向同步边界（§8.3 三条理由）。
- **监控范围 ≠ 发现单元**：watcher 监根递归子树并过滤 ``SKILL.md``；SKILL 的「发现单元」是
  ``<root>/<skill>/SKILL.md``（根下一级），深度过滤由 :func:`~a2c_smcp.computer.skills.staging.stage_user_skills`
  在重扫时负责，watcher 只管「有 SKILL.md 变更 → 标脏」。

触发规则 / Trigger rule：
- ``SKILL.md`` 文件 created/modified/deleted/moved → 触发（经 :meth:`mark_internal_write` 打标的自写除外）；
- **目录** deleted/moved → 触发（``rm -rf <skill>/`` / ``mv`` 在部分平台仅报目录事件、不逐文件，避免漏删）；
- 其余（目录 created/modified、非 SKILL.md 附属文件）→ 忽略。
偶发过触发由下游 300ms 去抖 + 重扫集合对比吸收（宁可多扫一次，不可漏掉删除）。

线程模型 / Threading：watchdog 观察者回调在**独立线程**触发。本 watcher 的 ``on_change`` 回调由调用方注入，
**必须**自行做线程安全 marshal（通常 ``loop.call_soon_threadsafe(debouncer.mark_dirty)``），见
:meth:`~a2c_smcp.computer.computer.Computer._start_skill_watcher`。

实现 / Impl：默认原生 :class:`~watchdog.observers.Observer`（inotify/FSEvents/ReadDirectoryChangesW）；
``use_polling=True`` 切 :class:`~watchdog.observers.polling.PollingObserver`，给不支持原生事件的 FS
（某些网络挂载 / 容器 overlayfs）兜底。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver

from a2c_smcp.computer.skills.sandbox import DEFAULT_SKILL_FILE
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# 内部写打标默认存活窗口（秒）/ Default TTL for internal-write marks (seconds)。对齐 CC ~2s。
DEFAULT_INTERNAL_WRITE_TTL_S = 2.0

# PollingObserver 下的 TTL 下限（秒）/ TTL floor under PollingObserver。
# 轮询模式自写事件最迟于下个轮询周期（watchdog 默认 ~1s）才上报，若 TTL < 轮询周期，自写事件可能在打标过期后
# 才到达 → 逃过抑制 → 触发「写回 → watcher → 重载」自触发。故 polling 时把 TTL 抬到 ≥ 轮询周期 + 余量。
# A self-write surfaces only at the next poll (~1s default); TTL must exceed the poll period to suppress it.
POLLING_INTERNAL_WRITE_TTL_FLOOR_S = 5.0


class _SkillMdEventHandler(FileSystemEventHandler):
    """
    watchdog 事件过滤器 / watchdog event filter。

    仅对 ``SKILL.md`` 文件事件（排除内部写）与目录删/移事件回调 ``on_change``；其余忽略（见模块「触发规则」）。
    注：覆盖方法参数加宽为 :class:`FileSystemEvent`（基类签名为各具体事件类型，加宽参数对 LSP 安全）。
    """

    def __init__(self, on_change: Callable[[], None], is_internal: Callable[[str], bool]) -> None:
        self._on_change = on_change
        self._is_internal = is_internal

    def _fire_if_skill_md(self, raw_path: str | bytes) -> None:
        """路径 basename 为 SKILL.md 且非内部写 → 触发 ``on_change``。"""
        path = os.fsdecode(raw_path)
        if os.path.basename(path) != DEFAULT_SKILL_FILE:
            return
        if self._is_internal(path):
            logger.debug("SKILL watcher 忽略内部写自触发 / ignore internal-write self-trigger: %s", path)
            return
        self._on_change()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._fire_if_skill_md(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._fire_if_skill_md(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            # rm -rf <skill>/ 在部分平台仅报目录删除（不逐文件）→ 直接标脏，交重扫对账孤儿。
            self._on_change()
        else:
            self._fire_if_skill_md(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self._on_change()
        else:
            self._fire_if_skill_md(event.src_path)
            # dest 落点为 SKILL.md（如 other → SKILL.md 改名）也应触发；dest_path 在基类即存在（非移动事件为空串）。
            self._fire_if_skill_md(event.dest_path)


class SkillFileWatcher:
    """
    user 源 DropIn 文件 watcher（watchdog 集成）/ User-source DropIn file watcher。

    :param on_change: 检测到相关变更时调用的**线程安全** marshaller（通常
        ``lambda: loop.call_soon_threadsafe(debouncer.mark_dirty)``）；在 watchdog 观察者线程内被调用。
    :param use_polling: ``True`` 用 :class:`PollingObserver`（不支持原生事件的 FS 兜底），默认原生 Observer。
    :param internal_write_ttl_s: :meth:`mark_internal_write` 打标的存活窗口（秒）。
    """

    def __init__(
        self,
        on_change: Callable[[], None],
        *,
        use_polling: bool = False,
        internal_write_ttl_s: float = DEFAULT_INTERNAL_WRITE_TTL_S,
    ) -> None:
        self._on_change = on_change
        self._use_polling = use_polling
        # polling 模式抬高 TTL 到 ≥ 轮询周期，避免迟到的自写事件逃过抑制（见 POLLING_INTERNAL_WRITE_TTL_FLOOR_S）。
        self._internal_ttl = max(internal_write_ttl_s, POLLING_INTERNAL_WRITE_TTL_FLOOR_S) if use_polling else internal_write_ttl_s
        self._handler = _SkillMdEventHandler(on_change, self._is_internal_write)
        self._observer: BaseObserver | None = None
        self._watched: list[Path] = []
        self._internal_writes: dict[str, float] = {}
        self._lock = threading.Lock()

    # ── 内部写打标（避免回写自触发）/ internal-write marking ─────────────────
    def mark_internal_write(self, path: str | Path) -> None:
        """
        登记一次 SDK 自写路径 / Mark a path the SDK itself just wrote。

        其 ``internal_write_ttl_s`` 窗口内对该路径的 SKILL.md 事件将被忽略，避免「写回 → watcher →
        重载 → 写回」自触发循环（对标 CC ``settings.ts`` ``markInternalWrite``）。线程安全。
        """
        key = self._normalize(path)
        with self._lock:
            self._internal_writes[key] = time.monotonic() + self._internal_ttl

    def _is_internal_write(self, path: str) -> bool:
        """路径是否处于未过期的内部写打标窗口内（顺带清理过期项）/ Whether path is within an unexpired mark。"""
        key = self._normalize(path)
        now = time.monotonic()
        with self._lock:
            expired = [k for k, exp in self._internal_writes.items() if exp <= now]
            for k in expired:
                del self._internal_writes[k]
            exp = self._internal_writes.get(key)
            return exp is not None and exp > now

    @staticmethod
    def _normalize(path: str | Path) -> str:
        """归一为 realpath 字符串（与 watchdog 上报路径对齐）；解析失败回退原值。"""
        try:
            return str(Path(path).resolve())
        except OSError:  # pragma: no cover - 防御性（路径解析极少抛）
            return str(path)

    # ── 生命周期 / lifecycle ─────────────────────────────────────────────────
    def watch(self, roots: Iterable[Path]) -> None:
        """
        对全部**存在**的发现根注册递归监控并启动 Observer / Schedule recursive watches on existing roots。

        缺失根跳过 + DEBUG（容错：user/ 或某 workdir 尚未创建很正常）；重复根去重；**无可监控根 → 不启动线程**
        （避免空 Observer 线程）。重复调用前请先 :meth:`stop`。
        """
        existing: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            rp = root.resolve()
            key = str(rp)
            if key in seen:
                continue
            seen.add(key)
            if not rp.is_dir():
                logger.debug("SKILL watcher 跳过不存在的发现根 / skip missing root: %s", rp)
                continue
            existing.append(rp)

        if not existing:
            logger.debug("SKILL watcher 无可监控发现根，Observer 不启动 / no watchable roots, observer not started")
            return

        observer: BaseObserver = PollingObserver() if self._use_polling else Observer()
        for rp in existing:
            observer.schedule(self._handler, str(rp), recursive=True)
            logger.debug("SKILL watcher 注册递归监控 / watching (recursive): %s", rp)
        observer.daemon = True
        observer.start()
        self._observer = observer
        self._watched = existing
        logger.info("SKILL 文件 watcher 已启动，监控 %d 个发现根 / started, watching %d root(s)", len(existing), len(existing))

    def stop(self, *, timeout: float = 2.0) -> None:
        """停止并 join Observer 线程（幂等；未启动 → no-op）/ Stop and join the observer thread (idempotent)。"""
        observer = self._observer
        self._observer = None
        self._watched = []
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=timeout)
        except Exception as e:  # pragma: no cover - 防御性（停机竞态）
            logger.error("SKILL watcher 停止异常 / stop failed: %s", e, exc_info=True)

    @property
    def is_running(self) -> bool:
        """Observer 是否在运行 / Whether the observer thread is running。"""
        return self._observer is not None

    @property
    def watched_roots(self) -> tuple[Path, ...]:
        """当前实际监控的发现根（已存在并 schedule 的）/ Currently scheduled (existing) roots。"""
        return tuple(self._watched)
