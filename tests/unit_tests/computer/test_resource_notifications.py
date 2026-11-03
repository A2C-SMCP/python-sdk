"""#227: bounded resource notification work, event preservation, and lifecycle cleanup."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp import types

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.socketio.client import SMCPComputerClient


def _computer(root: Path) -> Computer:
    computer = Computer(
        name="notifications",
        auto_connect=False,
        skill_home=root / "skills",
        blob_cache_root=root / "blobs",
        project_root=root,
        env={"XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data")},
    )
    computer.mcp_manager = MCPServerManager(auto_connect=False)
    return computer


async def _notify(computer: Computer) -> None:
    await computer._on_manager_change(types.ServerNotification(types.ResourceListChangedNotification()))


@pytest.mark.asyncio
async def test_bursts_coalesce_but_notifications_during_refresh_are_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    computer = _computer(tmp_path)
    client = SMCPComputerClient(computer=computer)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def collect() -> set[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return {f"window://phase{calls}"}

    monkeypatch.setattr(computer, "_acollect_window_uris", collect)
    monkeypatch.setattr(computer, "_acollect_skill_refs", AsyncMock(return_value=set()))
    try:
        async with asyncio.timeout(2):
            for _ in range(100):
                await _notify(computer)
            task = computer._resource_refresh_task
            await entered.wait()
            for _ in range(100):
                await _notify(computer)
            assert computer._resource_refresh_task is task
            release.set()
            await task
        assert calls == 2
        assert computer._windows_cache == {"window://phase2"}
        assert computer.socketio_client is client
    finally:
        await computer.shutdown()


@pytest.mark.asyncio
async def test_failed_window_query_does_not_block_skills_or_later_notifications(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    computer = _computer(tmp_path)
    client = SMCPComputerClient(computer=computer)
    windows = AsyncMock(side_effect=[RuntimeError("query failed"), {"window://recovered"}])
    skills = AsyncMock(return_value={"skill://srv/demo"})
    restage = AsyncMock(return_value=[])
    monkeypatch.setattr(computer, "_acollect_window_uris", windows)
    monkeypatch.setattr(computer, "_acollect_skill_refs", skills)
    monkeypatch.setattr(computer, "_restage_mcp_skills", restage)
    try:
        await _notify(computer)
        await computer._resource_refresh_task
        restage.assert_awaited_once()
        await _notify(computer)
        await computer._resource_refresh_task
        assert computer._windows_cache == {"window://recovered"}
        assert computer.socketio_client is client
    finally:
        await computer.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_refresh_before_closing_manager_and_rejects_late_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computer = _computer(tmp_path)
    client = SMCPComputerClient(computer=computer)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def collect() -> set[str]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return set()

    async def close_manager() -> None:
        assert cancelled.is_set()
        await _notify(computer)  # A transport can deliver its final notification while closing.
        assert computer._resource_refresh_task is None

    monkeypatch.setattr(computer, "_acollect_window_uris", collect)
    manager = MCPServerManager(auto_connect=False)
    monkeypatch.setattr(manager, "aclose", close_manager)
    computer.mcp_manager = manager
    async with asyncio.timeout(2):
        await _notify(computer)
        await entered.wait()
        await computer.shutdown()
        await computer.shutdown()
    assert computer._resource_refresh_task is None
    assert computer.socketio_client is client


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_boot_failure_cleans_notification_work_and_retry_reopens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    computer = _computer(tmp_path)
    client = SMCPComputerClient(computer=computer)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def collect() -> set[str]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return set()

    async def fail_initialize(self: MCPServerManager, configs: list) -> None:
        await _notify(computer)
        await entered.wait()
        raise asyncio.CancelledError() if cancel else RuntimeError("boot failed")

    async with asyncio.timeout(3):
        with monkeypatch.context() as patch:
            patch.setattr(computer, "_acollect_window_uris", collect)
            patch.setattr(MCPServerManager, "ainitialize", fail_initialize)
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await computer.boot_up()
        assert cancelled.is_set()
        assert computer._resource_refresh_task is None
        await computer.boot_up()
        try:
            await _notify(computer)
            assert computer._resource_refresh_task is not None
            await computer._resource_refresh_task
            assert computer.socketio_client is client
        finally:
            await computer.shutdown()


@pytest.mark.asyncio
async def test_shutdown_then_boot_restarts_resource_and_skill_notifications(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    computer = _computer(tmp_path)
    client = SMCPComputerClient(computer=computer)
    emit = AsyncMock()
    monkeypatch.setattr(client, "emit_update_skills", emit)
    for _ in range(2):
        await computer.boot_up()
        try:
            await computer._on_manager_change(
                types.ServerNotification(
                    types.ResourceUpdatedNotification(
                        params=types.ResourceUpdatedNotificationParams(uri="skill://srv/demo"),
                    )
                )
            )
            await computer._resource_refresh_task
            await computer._skill_debouncer.aflush()
        finally:
            await computer.shutdown()
    assert emit.await_count == 2


@pytest.mark.asyncio
async def test_unexpected_worker_failure_preserves_a_later_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    computer = _computer(tmp_path)
    client = SMCPComputerClient(computer=computer)
    calls = 0

    async def handle_windows() -> None:
        nonlocal calls
        assert computer.socketio_client is client
        calls += 1
        if calls == 1:
            await _notify(computer)
            raise RuntimeError("unexpected handler failure")

    monkeypatch.setattr(computer, "_on_resource_list_changed_windows", handle_windows)
    skills = AsyncMock()
    monkeypatch.setattr(computer, "_on_resource_list_changed_skills", skills)
    try:
        await _notify(computer)
        await computer._resource_refresh_task
        if computer._resource_refresh_task is not None:
            await computer._resource_refresh_task
        assert calls == 2
        skills.assert_awaited_once()
    finally:
        await computer.shutdown()


@pytest.mark.asyncio
async def test_lazy_mount_after_shutdown_reopens_notifications(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    computer = _computer(tmp_path)
    client = SMCPComputerClient(computer=computer)
    emit = AsyncMock()
    monkeypatch.setattr(client, "emit_update_skills", emit)
    await computer.shutdown()
    await computer.amount_server({
        "type": "stdio",
        "name": "lazy",
        "disabled": True,
        "server_parameters": {"command": "unused"},
    })
    try:
        await computer._on_manager_change(
            types.ServerNotification(
                types.ResourceUpdatedNotification(
                    params=types.ResourceUpdatedNotificationParams(uri="skill://srv/demo"),
                )
            )
        )
        await computer._resource_refresh_task
        await computer._skill_debouncer.aflush()
        emit.assert_awaited_once()
    finally:
        await computer.shutdown()
