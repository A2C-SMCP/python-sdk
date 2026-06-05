# -*- coding: utf-8 -*-
# filename: test_computer_skills.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer SKILL 子系统接线单元测试（v0.2.1 #66）
Unit tests for the Computer SKILL subsystem wiring.

设计依据 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §5.1 / §5.3。
协议依据 / Protocol: a2c-smcp-protocol skill.md §6 / §8 / §9。

覆盖 / Coverage:
- 持有 SkillRegistry + 真 SkillBlobResolver 接线（占位已替换）
- 委托 get_skills / get_skill_ref / read_skill_resource（§9.2 包根仅取自 ref.path）
- _acollect_skill_refs（manager 未就绪 → 空集；有 skill:// → URI 集）
- _restage_mcp_skills 全量重物化 + 孤儿对账（消失→孤儿、回归→恢复）
- _on_manager_change skill:// 分支：集合变化→emit_update_skills；未变→跳过；ResourceUpdated(skill://)→emit
- window:// 与 skill:// 并行（互不影响既有窗口行为）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import Resource

from a2c_smcp.computer.blob.resolver import SkillBlobResolver
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.skills.debouncer import SkillEventDebouncer
from a2c_smcp.computer.skills.sandbox import SkillSandboxError
from a2c_smcp.smcp import A2CSkillRef

_SKILL_MD = "---\nname: demo\ndescription: d\nlicense: MIT\n---\n# Demo\nbody\n"


# ── 测试替身 / doubles ──────────────────────────────────────────────────────
class _DummyClient:
    """伪 Socket.IO 客户端：计数三类 emit（含 v0.2.1 emit_update_skills）。"""

    def __init__(self) -> None:
        self.update_tool_called = 0
        self.refresh_called = 0
        self.update_skills_called = 0

    async def emit_update_tool_list(self) -> None:
        self.update_tool_called += 1

    async def emit_refresh_desktop(self) -> None:
        self.refresh_called += 1

    async def emit_update_skills(self) -> None:
        self.update_skills_called += 1


class _FakeManager:
    """提供 list_skill_resources 的最小 manager（mounted 模式无需 read_resource）。"""

    def __init__(self, pairs: list[tuple[str, Resource]]) -> None:
        self._pairs = pairs

    def set_pairs(self, pairs: list[tuple[str, Resource]]) -> None:
        self._pairs = pairs

    async def list_skill_resources(self, server_name: str | None = None) -> list[tuple[str, Resource]]:
        return [(s, r) for s, r in self._pairs if server_name is None or s == server_name]


def _mounted_skill_resource(uri: str, src_dir: Path) -> Resource:
    """构造一个 mounted 模式 skill:// 根资源（指向本地 src_dir）。"""
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    return Resource(uri=uri, name=uri.rsplit("/", 1)[-1], _meta={"source": "mounted", "mount_dir": str(src_dir)})


def _computer(tmp_path: Path) -> Computer:
    return Computer(name="comp-test", blob_cache_root=tmp_path / "blobspool", auto_connect=False, auto_reconnect=False)


# ===========================================================================
# 接线 / wiring
# ===========================================================================
def test_holds_registry_and_real_skill_resolver(tmp_path: Path) -> None:
    comp = _computer(tmp_path)
    assert comp.skill_registry is not None
    # 占位已被真 resolver 替换（#66）
    assert isinstance(comp.blob_resolvers["skill"], SkillBlobResolver)


def test_get_skills_excludes_orphans(tmp_path: Path) -> None:
    comp = _computer(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    comp.skill_registry.register({"name": "mcp:srv:a", "source": "mcp:srv", "path": str(pkg)})
    comp.skill_registry.register({"name": "mcp:srv:b", "source": "mcp:srv", "path": str(pkg)})
    comp.skill_registry.mark_orphan("mcp:srv:b")
    names = [s["name"] for s in comp.get_skills()]
    assert names == ["mcp:srv:a"]


def test_get_skill_ref_none_for_absent_and_orphan(tmp_path: Path) -> None:
    comp = _computer(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    comp.skill_registry.register({"name": "mcp:srv:a", "source": "mcp:srv", "path": str(pkg)})
    assert comp.get_skill_ref("mcp:srv:a") is not None
    assert comp.get_skill_ref("mcp:srv:absent") is None
    comp.skill_registry.mark_orphan("mcp:srv:a")
    assert comp.get_skill_ref("mcp:srv:a") is None


def test_read_skill_resource_uses_ref_path_not_name(tmp_path: Path) -> None:
    comp = _computer(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    ref: A2CSkillRef = {"name": "mcp:srv:demo", "source": "mcp:srv", "path": str(pkg)}
    view = comp.read_skill_resource(ref, None)
    assert view.rel_path == "SKILL.md"
    assert view.abs_path == (pkg / "SKILL.md").resolve()


def test_read_skill_resource_traversal_raises(tmp_path: Path) -> None:
    comp = _computer(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    ref: A2CSkillRef = {"name": "mcp:srv:demo", "source": "mcp:srv", "path": str(pkg)}
    with pytest.raises(SkillSandboxError) as exc:
        comp.read_skill_resource(ref, "../escape")
    assert exc.value.reason == "traversal"


# ===========================================================================
# _acollect_skill_refs
# ===========================================================================
@pytest.mark.asyncio
async def test_acollect_skill_refs_empty_when_no_manager(tmp_path: Path) -> None:
    comp = _computer(tmp_path)
    assert await comp._acollect_skill_refs() == set()


@pytest.mark.asyncio
async def test_acollect_skill_refs_returns_uri_set(tmp_path: Path) -> None:
    comp = _computer(tmp_path)
    res = _mounted_skill_resource("skill://srv/demo", tmp_path / "src")
    comp.mcp_manager = _FakeManager([("srv", res)])  # type: ignore[assignment]
    assert await comp._acollect_skill_refs() == {"skill://srv/demo"}


# ===========================================================================
# _restage_mcp_skills + 孤儿对账
# ===========================================================================
@pytest.mark.asyncio
async def test_restage_registers_then_reconciles_orphans(tmp_path: Path) -> None:
    comp = _computer(tmp_path)
    comp._skill_home = tmp_path / "home"
    comp._skill_home.mkdir()
    res = _mounted_skill_resource("skill://srv/demo", tmp_path / "src")
    mgr = _FakeManager([("srv", res)])
    comp.mcp_manager = mgr  # type: ignore[assignment]

    # 首轮：物化 + 注册
    registered = await comp._restage_mcp_skills()
    assert registered == ["mcp:srv:demo"]
    assert comp.get_skill_ref("mcp:srv:demo") is not None

    # skill 从源消失 → 全量重物化 → 孤儿对账标孤儿（从 get_skills 排除）
    mgr.set_pairs([])
    await comp._restage_mcp_skills()
    assert comp.skill_registry.is_orphan("mcp:srv:demo") is True
    assert [s["name"] for s in comp.get_skills()] == []

    # skill 回归 → 重物化 → register 命中孤儿条目自动恢复
    mgr.set_pairs([("srv", res)])
    await comp._restage_mcp_skills()
    assert comp.skill_registry.is_orphan("mcp:srv:demo") is False
    assert comp.get_skill_ref("mcp:srv:demo") is not None


@pytest.mark.asyncio
async def test_reconcile_does_not_touch_non_mcp_sources(tmp_path: Path) -> None:
    """孤儿对账仅处理 mcp: 源；user/marketplace 源不被误标孤儿（属 #60/#61）。"""
    comp = _computer(tmp_path)
    comp._skill_home = tmp_path / "home"
    comp._skill_home.mkdir()
    pkg = tmp_path / "userpkg"
    pkg.mkdir()
    comp.skill_registry.register({"name": "my-helper", "source": "user", "path": str(pkg)})
    comp.mcp_manager = _FakeManager([])  # type: ignore[assignment]
    await comp._restage_mcp_skills()  # 全量重物化（mcp 源为空）
    # user 源不应被孤儿对账影响
    assert comp.skill_registry.is_orphan("my-helper") is False
    assert comp.get_skill_ref("my-helper") is not None


# ===========================================================================
# _on_manager_change skill:// 分支
# ===========================================================================
@pytest.mark.asyncio
async def test_resource_list_changed_skill_set_changed_emits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.types import ResourceListChangedNotification

    comp = _computer(tmp_path)
    client = _DummyClient()
    comp.socketio_client = client  # type: ignore[assignment]

    restaged: dict[str, int] = {"n": 0}

    async def fake_restage(server_name: str | None = None) -> list[str]:
        restaged["n"] += 1
        return ["mcp:srv:demo"]

    async def fake_collect_skills() -> set[str]:
        return {"skill://srv/demo"}

    async def fake_collect_windows() -> set[str]:
        return set()

    monkeypatch.setattr(comp, "_restage_mcp_skills", fake_restage)
    monkeypatch.setattr(comp, "_acollect_skill_refs", fake_collect_skills)
    monkeypatch.setattr(comp, "_acollect_window_uris", fake_collect_windows)
    comp._skills_cache = set()  # 原为空 → 变化

    await comp._on_manager_change(SimpleNamespace(root=ResourceListChangedNotification()))  # type: ignore[arg-type]
    assert restaged["n"] == 1
    assert comp._skills_cache == {"skill://srv/demo"}  # 缓存在处理器内同步更新
    await comp._skill_debouncer.aflush()  # 结算去抖窗口 → emit（#67：emit 经去抖器，不再裸调）
    assert client.update_skills_called == 1


@pytest.mark.asyncio
async def test_resource_list_changed_skill_set_unchanged_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.types import ResourceListChangedNotification

    comp = _computer(tmp_path)
    client = _DummyClient()
    comp.socketio_client = client  # type: ignore[assignment]

    restaged: dict[str, int] = {"n": 0}

    async def fake_restage(server_name: str | None = None) -> list[str]:
        restaged["n"] += 1
        return []

    async def fake_collect_skills() -> set[str]:
        return {"skill://srv/demo"}

    async def fake_collect_windows() -> set[str]:
        return set()

    monkeypatch.setattr(comp, "_restage_mcp_skills", fake_restage)
    monkeypatch.setattr(comp, "_acollect_skill_refs", fake_collect_skills)
    monkeypatch.setattr(comp, "_acollect_window_uris", fake_collect_windows)
    comp._skills_cache = {"skill://srv/demo"}  # 与采集一致 → 跳过

    await comp._on_manager_change(SimpleNamespace(root=ResourceListChangedNotification()))  # type: ignore[arg-type]
    assert restaged["n"] == 0
    assert client.update_skills_called == 0


@pytest.mark.asyncio
async def test_resource_updated_skill_uri_emits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.types import ResourceUpdatedNotification, ResourceUpdatedNotificationParams

    comp = _computer(tmp_path)
    client = _DummyClient()
    comp.socketio_client = client  # type: ignore[assignment]

    restaged: dict[str, int] = {"n": 0}

    async def fake_restage(server_name: str | None = None) -> list[str]:
        restaged["n"] += 1
        return ["mcp:srv:demo"]

    monkeypatch.setattr(comp, "_restage_mcp_skills", fake_restage)

    note = ResourceUpdatedNotification(params=ResourceUpdatedNotificationParams(uri="skill://srv/demo"))
    await comp._on_manager_change(SimpleNamespace(root=note))  # type: ignore[arg-type]
    assert restaged["n"] == 1
    await comp._skill_debouncer.aflush()  # 结算去抖窗口 → emit
    assert client.update_skills_called == 1
    assert client.refresh_called == 0  # 非 window，不刷新桌面


@pytest.mark.asyncio
async def test_resource_updated_non_skill_non_window_no_emit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.types import ResourceUpdatedNotification, ResourceUpdatedNotificationParams

    comp = _computer(tmp_path)
    client = _DummyClient()
    comp.socketio_client = client  # type: ignore[assignment]

    called: dict[str, int] = {"n": 0}

    async def fake_restage(server_name: str | None = None) -> list[str]:
        called["n"] += 1
        return []

    monkeypatch.setattr(comp, "_restage_mcp_skills", fake_restage)

    note = ResourceUpdatedNotification(params=ResourceUpdatedNotificationParams(uri="file://x"))
    await comp._on_manager_change(SimpleNamespace(root=note))  # type: ignore[arg-type]
    assert called["n"] == 0
    assert client.update_skills_called == 0
    assert client.refresh_called == 0


# ===========================================================================
# boot_up SKILL 子系统启动三步接线（解析 Home → 重物化 → 填充缓存）
# ===========================================================================
@pytest.mark.asyncio
async def test_boot_up_wires_skill_subsystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """boot_up 三步接线集成缝隙守护：注入 skill_home，断言 Home 解析落盘 + Registry 被填充 + 缓存 seed。

    三个子步骤各有直测；本测试守护它们在 boot_up 中被**按序接线**且结果落到实例状态。
    """
    from a2c_smcp.computer.mcp_clients.manager import MCPServerManager

    skill_home = tmp_path / "skillhome"
    comp = Computer(name="comp-boot", skill_home=skill_home, auto_connect=False, auto_reconnect=False)

    # manager.ainitialize 置空，避免真实 MCP 连接 / no-op ainitialize to avoid real MCP I/O
    async def _noop_ainit(self: MCPServerManager, servers: object) -> None:
        return None

    monkeypatch.setattr(MCPServerManager, "ainitialize", _noop_ainit)

    # 模拟 staging 成功（其内部已有直测）：注册一个 ref，验证 boot 确实调用了重物化并落库
    async def fake_restage(server_name: str | None = None) -> list[str]:
        pkg = tmp_path / "pkg"
        pkg.mkdir(exist_ok=True)
        comp.skill_registry.register({"name": "mcp:srv:boot", "source": "mcp:srv", "path": str(pkg)})
        return ["mcp:srv:boot"]

    async def fake_collect() -> set[str]:
        return {"skill://srv/boot"}

    monkeypatch.setattr(comp, "_restage_mcp_skills", fake_restage)
    monkeypatch.setattr(comp, "_acollect_skill_refs", fake_collect)

    await comp.boot_up()

    # 1) SKILL Home 解析并落盘 / Home resolved & created
    assert comp._skill_home == skill_home.resolve()
    assert comp._skill_home.is_dir()
    # 2) Registry 被重物化填充 / Registry populated by restage
    assert comp.get_skill_ref("mcp:srv:boot") is not None
    # 3) skill:// 缓存被 seed / cache seeded
    assert comp._skills_cache == {"skill://srv/boot"}

    await comp.shutdown()


# ===========================================================================
# v0.2.1 user 源 DropIn 接入 + 文件 watcher 生命周期（S14，#67）
# ===========================================================================
_USER_SKILL_MD = "---\nname: helper\ndescription: a user skill\n---\n# Helper\n"


@pytest.mark.asyncio
async def test_invalidate_user_skills_registers_then_orphans_on_delete(tmp_path: Path) -> None:
    """去抖器 invalidate：就地重扫 user 源注册；磁盘删除后重扫 → 标孤儿（从 get_skills 排除，保留以便恢复）。"""
    import shutil

    skill_home = tmp_path / "sh"
    user_skill = skill_home / "user" / "helper"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(_USER_SKILL_MD, encoding="utf-8")

    comp = Computer(name="c", skill_home=skill_home, auto_connect=False, auto_reconnect=False)
    comp._skill_home = comp._resolve_skill_home()  # 解析并建 home（不走完整 boot_up）

    await comp._invalidate_user_skills()
    ref = comp.get_skill_ref("helper")
    assert ref is not None
    assert ref["source"] == "user"
    assert Path(ref["path"]) == user_skill.resolve()  # 就地发现：path 指向原目录，未复制

    # 磁盘删除 → 重扫对账 → 标孤儿
    shutil.rmtree(user_skill)
    await comp._invalidate_user_skills()
    assert comp.get_skill_ref("helper") is None  # 孤儿不在 active
    assert "helper" in comp.skill_registry  # 仍在册以便恢复


@pytest.mark.asyncio
async def test_boot_up_starts_user_watcher_and_shutdown_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """user/ 存在 → boot_up 启动文件 watcher；mark_skill_internal_write 透传；shutdown 停止并清空。"""
    from a2c_smcp.computer.mcp_clients.manager import MCPServerManager

    monkeypatch.setenv("A2C_SKILL_WATCH_POLLING", "1")  # 用 PollingObserver，启停确定性
    skill_home = tmp_path / "sh"
    (skill_home / "user").mkdir(parents=True)  # 发现根存在 → watcher 启动

    comp = Computer(name="c", skill_home=skill_home, auto_connect=False, auto_reconnect=False)

    async def _noop_ainit(self: MCPServerManager, servers: object) -> None:
        return None

    async def fake_restage(server_name: str | None = None) -> list[str]:
        return []

    async def fake_collect() -> set[str]:
        return set()

    monkeypatch.setattr(MCPServerManager, "ainitialize", _noop_ainit)
    monkeypatch.setattr(comp, "_restage_mcp_skills", fake_restage)
    monkeypatch.setattr(comp, "_acollect_skill_refs", fake_collect)

    await comp.boot_up()
    assert comp._skill_watcher is not None
    assert comp._skill_watcher.is_running is True
    # 内部写打标透传（不报错；watcher 已启动）
    comp.mark_skill_internal_write(skill_home / "user" / "helper" / "SKILL.md")

    await comp.shutdown()
    assert comp._skill_watcher is None  # 停机后清空


@pytest.mark.asyncio
async def test_boot_up_no_user_watcher_when_no_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无任何发现根（user/ 不存在、无登记 workdir）→ watcher 对象建但不启动线程。"""
    from a2c_smcp.computer.mcp_clients.manager import MCPServerManager

    skill_home = tmp_path / "sh"  # 仅 boot 时建 <sh>，不建 <sh>/user

    comp = Computer(name="c", skill_home=skill_home, auto_connect=False, auto_reconnect=False)

    async def _noop_ainit(self: MCPServerManager, servers: object) -> None:
        return None

    async def fake_restage(server_name: str | None = None) -> list[str]:
        return []

    async def fake_collect() -> set[str]:
        return set()

    monkeypatch.setattr(MCPServerManager, "ainitialize", _noop_ainit)
    monkeypatch.setattr(comp, "_restage_mcp_skills", fake_restage)
    monkeypatch.setattr(comp, "_acollect_skill_refs", fake_collect)

    await comp.boot_up()
    assert comp._skill_watcher is not None
    assert comp._skill_watcher.is_running is False  # 无可监控根 → 未启动
    await comp.shutdown()


@pytest.mark.asyncio
async def test_emit_update_skills_now_no_client_is_noop(tmp_path: Path) -> None:
    """无 Socket.IO 客户端时去抖结算末端 no-op（不抛）/ debouncer sink no-op without client。"""
    comp = _computer(tmp_path)  # 未设置 socketio_client
    await comp._emit_update_skills_now()  # 不应抛


@pytest.mark.asyncio
async def test_start_skill_watcher_restart_stops_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复 _start_skill_watcher → 先停旧 watcher、装配新对象（覆盖重启分支）。"""
    monkeypatch.setenv("A2C_SKILL_WATCH_POLLING", "1")
    skill_home = tmp_path / "sh"
    (skill_home / "user").mkdir(parents=True)

    comp = Computer(name="c", skill_home=skill_home, auto_connect=False, auto_reconnect=False)
    comp._skill_home = comp._resolve_skill_home()

    comp._start_skill_watcher()
    first = comp._skill_watcher
    assert first is not None
    assert first.is_running is True

    comp._start_skill_watcher()  # 重启：旧的应被停，新的接管
    try:
        assert comp._skill_watcher is not first
        assert first.is_running is False
        assert comp._skill_watcher is not None
        assert comp._skill_watcher.is_running is True
    finally:
        if comp._skill_watcher is not None:
            comp._skill_watcher.stop()


@pytest.mark.asyncio
async def test_end_to_end_real_watch_event_triggers_emit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端接线：Computer 启动后真实文件事件 → _marshal → 去抖器 → invalidate(重扫注册) → emit。

    覆盖生产 marshal 闭包（watchdog 线程 → loop.call_soon_threadsafe → mark_dirty）这一拼接处。
    用 PollingObserver + 缩短去抖窗口；轮询/去抖均异步到达，故带超时轮询断言。
    """
    from a2c_smcp.computer.mcp_clients.manager import MCPServerManager

    monkeypatch.setenv("A2C_SKILL_WATCH_POLLING", "1")
    skill_home = tmp_path / "sh"
    (skill_home / "user").mkdir(parents=True)

    comp = Computer(name="c", skill_home=skill_home, auto_connect=False, auto_reconnect=False)
    client = _DummyClient()
    comp.socketio_client = client  # type: ignore[assignment]
    # 缩短去抖窗口加速测试（boot 时 _start_skill_watcher 闭包捕获此 debouncer）
    comp._skill_debouncer = SkillEventDebouncer(
        comp._emit_update_skills_now,
        invalidate=comp._invalidate_user_skills,
        window_ms=20,
    )

    async def _noop_ainit(self: MCPServerManager, servers: object) -> None:
        return None

    async def fake_restage(server_name: str | None = None) -> list[str]:
        return []

    async def fake_collect() -> set[str]:
        return set()

    monkeypatch.setattr(MCPServerManager, "ainitialize", _noop_ainit)
    monkeypatch.setattr(comp, "_restage_mcp_skills", fake_restage)
    monkeypatch.setattr(comp, "_acollect_skill_refs", fake_collect)

    await comp.boot_up()
    assert comp._skill_watcher is not None and comp._skill_watcher.is_running is True

    # 启动后落一个 user skill → 触发真实 watchdog 事件链
    skill_dir = skill_home / "user" / "helper"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_USER_SKILL_MD, encoding="utf-8")

    # 轮询（~1s）+ 去抖（20ms）均异步：超时内轮询等待 emit 到达
    for _ in range(60):
        if client.update_skills_called >= 1:
            break
        await asyncio.sleep(0.2)

    try:
        assert client.update_skills_called >= 1, "真实文件事件未经 _marshal→去抖→emit 链路触发上报"
        assert comp.get_skill_ref("helper") is not None  # invalidate 重扫已注册该 user skill
    finally:
        await comp.shutdown()
