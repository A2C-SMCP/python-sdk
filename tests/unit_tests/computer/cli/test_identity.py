# -*- coding: utf-8 -*-
# filename: test_identity.py
# @Time    : 2026/10/28
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``identity reset`` 两种入口的单测 —— protocol#64。

- 独立 CLI：只做原子文件替换 + 重启提示（不计入「已连接拒绝」）；
- REPL：复用 SDK API（已连接 / 有入房意图时拒绝）。

Two entry points: the standalone CLI (file swap + restart hint only) and the REPL (delegates to the
SDK API so a live connection is refused in-process).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from a2c_smcp.computer.cli.commands import identity as identity_cmd
from a2c_smcp.computer.cli.main import app
from a2c_smcp.computer.identity import identity_store_path, load_or_create_computer_id


class _StubClient:
    def __init__(self, *, result: str | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def reset_identity(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _stub_computer(client: Any) -> Any:
    """最小 Computer 替身：REPL dispatch 只读 ``socketio_client``。"""
    return SimpleNamespace(socketio_client=client)


class TestStandaloneCli:
    """独立 CLI：原子替换 + 重启提示 + 失败非零退出。"""

    def test_reset_replaces_file_and_hints_restart(self, capsys: pytest.CaptureFixture[str]) -> None:
        old = load_or_create_computer_id()
        code = identity_cmd.reset_identity_cli()

        assert code == 0
        new = load_or_create_computer_id()
        assert new != old, "独立 CLI 必须换新值"
        out = capsys.readouterr().out
        assert "重启" in out, f"必须提示需重启运行中的 Computer: {out!r}"

    def test_unwritable_store_exits_non_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")

        code = identity_cmd.reset_identity_cli(env={"XDG_CONFIG_HOME": str(blocker)})

        assert code == 1
        captured = capsys.readouterr()
        assert "重置失败" in captured.out + captured.err

    def test_typer_entry_point_resets_identity(self) -> None:
        old = load_or_create_computer_id()
        result = CliRunner().invoke(app, ["identity", "reset"])

        assert result.exit_code == 0, result.output
        assert load_or_create_computer_id() != old
        assert identity_store_path().exists()


class TestRepl:
    """REPL：复用 SDK API，已连接拒绝可感；未连接时走文件重置。"""

    @pytest.mark.asyncio
    async def test_refusal_from_sdk_is_printed_not_raised(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = _StubClient(error=RuntimeError("已连接（或存在入房意图）时不能重置 Computer 身份：请先断开连接"))
        comp = _stub_computer(client)

        await identity_cmd.repl_dispatch(comp, ["identity", "reset"])

        assert client.calls == 1, "REPL 必须复用 SDK API（而不是直接改文件）"
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "重置失败" in out and "请先断开" in out

    @pytest.mark.asyncio
    async def test_success_prints_new_id_and_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = _StubClient(result="abcdef01-2345-4678-8abc-def012345678")

        await identity_cmd.repl_dispatch(_stub_computer(client), ["identity", "reset"])

        out = capsys.readouterr().out
        assert "abcdef01-2345-4678-8abc-def012345678" in out
        assert "重启" in out

    @pytest.mark.asyncio
    async def test_without_client_resets_file_directly(self) -> None:
        old = load_or_create_computer_id()

        await identity_cmd.repl_dispatch(_stub_computer(None), ["identity", "reset"])

        assert load_or_create_computer_id() != old

    @pytest.mark.asyncio
    async def test_usage_for_unknown_subcommand(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = _StubClient(result="unused")
        await identity_cmd.repl_dispatch(_stub_computer(client), ["identity", "show"])

        assert client.calls == 0
        assert "usage" in capsys.readouterr().out.lower()
