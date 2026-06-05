# -*- coding: utf-8 -*-
# filename: test_watcher.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SkillFileWatcher 单元测试（v0.2.1，#67）/ Unit tests for SkillFileWatcher。

设计依据 / Design: docs/design-0.2.1-cli-marketplace-ux.md §8.3。

测试意图 / Test intentions:
- 事件过滤（确定性、免真实 FS 计时）：SKILL.md 文件增删改移 → 触发；非 SKILL.md / 目录增改 → 忽略；
  目录删/移 → 触发（rm -rf / mv 整个 skill 目录，部分平台仅报目录事件）；改名落点为 SKILL.md → 触发。
- markInternalWrite：打标窗口内对该路径的 SKILL.md 事件被忽略（避免自写回触发重载循环）。
- watch/stop 生命周期与簿记：缺失根不启动；重复根去重；仅监控传入的根（marketplace clone 树不监）；stop 幂等。
- PollingObserver 真实事件冒烟：创建 SKILL.md → 回调被触发（验证 watchdog↔handler 接线确实送达）。
"""

from __future__ import annotations

import threading
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from a2c_smcp.computer.skills.watcher import SkillFileWatcher, _SkillMdEventHandler


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> None:
        self.n += 1


def _handler(counter: _Counter, *, internal=lambda _p: False) -> _SkillMdEventHandler:  # noqa: ANN001
    return _SkillMdEventHandler(counter, internal)


# ── 事件过滤（直注合成事件，确定性）/ event filtering ────────────────────────
def test_skill_md_file_events_fire() -> None:
    c = _Counter()
    h = _handler(c)
    h.on_created(FileCreatedEvent("/abs/skills/demo/SKILL.md"))
    h.on_modified(FileModifiedEvent("/abs/skills/demo/SKILL.md"))
    h.on_deleted(FileDeletedEvent("/abs/skills/demo/SKILL.md"))
    h.on_moved(FileMovedEvent("/abs/skills/demo/SKILL.md", "/abs/skills/demo/SKILL.md.bak"))
    assert c.n == 4


def test_non_skill_md_file_ignored() -> None:
    c = _Counter()
    h = _handler(c)
    h.on_created(FileCreatedEvent("/abs/skills/demo/reference.md"))
    h.on_modified(FileModifiedEvent("/abs/skills/demo/scripts/run.py"))
    h.on_deleted(FileDeletedEvent("/abs/skills/demo/notes.txt"))
    assert c.n == 0


def test_dir_created_modified_ignored() -> None:
    c = _Counter()
    h = _handler(c)
    h.on_created(DirCreatedEvent("/abs/skills/demo"))
    h.on_modified(DirModifiedEvent("/abs/skills/demo"))
    assert c.n == 0


def test_dir_deleted_and_moved_fire() -> None:
    # rm -rf <skill>/ 或 mv <skill>/ 在部分平台仅报目录事件 → 必须触发以免漏删。
    c = _Counter()
    h = _handler(c)
    h.on_deleted(DirDeletedEvent("/abs/skills/demo"))
    h.on_moved(DirMovedEvent("/abs/skills/demo", "/abs/skills/demo2"))
    assert c.n == 2


def test_moved_into_skill_md_fires() -> None:
    # other.md → SKILL.md 改名：dest 落点为 SKILL.md，也应触发。
    c = _Counter()
    h = _handler(c)
    h.on_moved(FileMovedEvent("/abs/skills/demo/draft.md", "/abs/skills/demo/SKILL.md"))
    assert c.n == 1


# ── markInternalWrite 抑制自触发 / internal-write suppression ────────────────
def test_mark_internal_write_suppresses_then_other_path_fires(tmp_path: Path) -> None:
    c = _Counter()
    w = SkillFileWatcher(c)
    target = tmp_path / "demo" / "SKILL.md"
    other = tmp_path / "other" / "SKILL.md"

    w.mark_internal_write(target)
    # 打标路径的 SKILL.md 事件被忽略 / marked path suppressed
    w._handler.on_modified(FileModifiedEvent(str(target)))
    assert c.n == 0
    # 未打标路径正常触发 / unmarked path fires
    w._handler.on_modified(FileModifiedEvent(str(other)))
    assert c.n == 1


def test_internal_write_expires(tmp_path: Path) -> None:
    c = _Counter()
    w = SkillFileWatcher(c, internal_write_ttl_s=0.0)  # 立即过期
    target = tmp_path / "demo" / "SKILL.md"
    w.mark_internal_write(target)
    w._handler.on_modified(FileModifiedEvent(str(target)))  # TTL=0 → 不抑制
    assert c.n == 1


# ── watch / stop 生命周期与簿记 / lifecycle & bookkeeping ────────────────────
def test_watch_missing_roots_not_started(tmp_path: Path) -> None:
    w = SkillFileWatcher(_Counter())
    w.watch([tmp_path / "does-not-exist"])
    assert w.is_running is False
    assert w.watched_roots == ()


def test_watch_dedup_existing_root(tmp_path: Path) -> None:
    root = tmp_path / "user"
    root.mkdir()
    w = SkillFileWatcher(_Counter())
    try:
        w.watch([root, root])  # 同根传两次 → 去重，无重复 schedule / 假 WARN
        assert w.is_running is True
        assert w.watched_roots == (root.resolve(),)
    finally:
        w.stop()
    assert w.is_running is False


def test_watch_only_given_roots_marketplace_excluded(tmp_path: Path) -> None:
    # clone 树不监：只监控显式传入的发现根，marketplace 目录即便存在也不在监控集。
    user_root = tmp_path / "user"
    marketplace = tmp_path / "marketplace"
    user_root.mkdir()
    marketplace.mkdir()
    w = SkillFileWatcher(_Counter())
    try:
        w.watch([user_root])
        assert w.watched_roots == (user_root.resolve(),)
        assert marketplace.resolve() not in w.watched_roots
    finally:
        w.stop()


def test_stop_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "user"
    root.mkdir()
    w = SkillFileWatcher(_Counter())
    w.watch([root])
    w.stop()
    w.stop()  # 重复停止不报错
    assert w.is_running is False


# ── PollingObserver 真实事件冒烟（验证接线送达）/ real-event smoke ───────────
def test_polling_observer_detects_skill_md_create(tmp_path: Path) -> None:
    root = tmp_path / "user"
    root.mkdir()
    fired = threading.Event()
    w = SkillFileWatcher(fired.set, use_polling=True)
    try:
        w.watch([root])
        assert w.is_running is True
        skill_dir = root / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        # PollingObserver 轮询（默认 ~1s）→ 留足窗口；非阻塞超时断言
        assert fired.wait(timeout=8.0), "watcher 未在超时内检测到 SKILL.md 创建"
    finally:
        w.stop()
