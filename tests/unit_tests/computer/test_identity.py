# -*- coding: utf-8 -*-
# filename: test_identity.py
# @Time    : 2026/10/28
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 身份持久化（``a2c_smcp/computer/identity.py``）单测 —— protocol#64。

覆盖验收中的「持久化」组：首次生成 / 重启复用 / 重置后变更 / 同机并发首启同值 / 损坏 fail-fast，
以及位置与权限契约（``$XDG_CONFIG_HOME/a2c/computer-id``，目录 0700 / 文件 0600）。
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from a2c_smcp.computer.identity import (
    IDENTITY_DIR_MODE,
    IDENTITY_FILE_MODE,
    ComputerIdentityError,
    identity_store_path,
    load_or_create_computer_id,
    reset_computer_id,
)
from a2c_smcp.utils.computer_id import is_canonical_computer_id


def _identity_file(xdg_home: Path) -> Path:
    return xdg_home / "a2c" / "computer-id"


class TestPathResolution:
    """位置契约：XDG_CONFIG_HOME（仅绝对路径）否则 ~/.config。"""

    def test_absolute_xdg_config_home_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        # 与 mcp.json / settings.json 共用 resolve_user_config_dir ⇒ 结果已 expanduser().resolve()
        assert identity_store_path() == (tmp_path / "a2c" / "computer-id").resolve()

    def test_relative_xdg_config_home_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # XDG 规范：相对路径的 XDG_CONFIG_HOME 视为未设置 / relative values are ignored per XDG
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert identity_store_path() == (tmp_path / ".config" / "a2c" / "computer-id").resolve()

    def test_missing_xdg_falls_back_to_dot_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert identity_store_path() == (tmp_path / ".config" / "a2c" / "computer-id").resolve()

    def test_whitespace_padded_xdg_is_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """fix-review 阻塞项：XDG 带空白时必须与配置子系统解析到同一目录（不得分叉）。"""
        monkeypatch.setenv("XDG_CONFIG_HOME", f"  {tmp_path}  ")
        assert identity_store_path() == (tmp_path / "a2c" / "computer-id").resolve()

    def test_whitespace_only_xdg_falls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "   ")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert identity_store_path() == (tmp_path / ".config" / "a2c" / "computer-id").resolve()


class TestPersistence:
    """首次生成 / 重启复用 / 重置。"""

    def test_first_call_creates_canonical_uuid_with_newline(self, tmp_path: Path) -> None:
        path = _identity_file(tmp_path)
        assert not path.exists(), "物化前不得创建文件"

        value = load_or_create_computer_id(path)

        assert is_canonical_computer_id(value), value
        raw = path.read_text(encoding="utf-8")
        assert raw == f"{value}\n", f"UTF-8 单行 + 结尾换行: {raw!r}"

    def test_permissions_are_owner_only(self, tmp_path: Path) -> None:
        path = _identity_file(tmp_path)
        load_or_create_computer_id(path)

        assert stat.S_IMODE(os.stat(path.parent).st_mode) == IDENTITY_DIR_MODE
        assert stat.S_IMODE(os.stat(path).st_mode) == IDENTITY_FILE_MODE

    def test_existing_directory_permissions_are_not_changed(self, tmp_path: Path) -> None:
        """fix-review 🟡2：既存配置目录（mcp.json / settings.json 的家）权限不得被收紧到 0700。"""
        directory = tmp_path / "a2c"
        directory.mkdir()
        os.chmod(directory, 0o750)

        load_or_create_computer_id(directory / "computer-id")

        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o750, "既存目录权限必须原样保留"
        assert stat.S_IMODE(os.stat(directory / "computer-id").st_mode) == IDENTITY_FILE_MODE

    def test_reuse_across_calls_and_process_restart(self, tmp_path: Path) -> None:
        path = _identity_file(tmp_path)
        first = load_or_create_computer_id(path)
        # 模拟重启：同一文件再读一次 / restart: read the same file again
        assert load_or_create_computer_id(path) == first
        assert load_or_create_computer_id(path) == first

    def test_reset_replaces_value_and_persists(self, tmp_path: Path) -> None:
        path = _identity_file(tmp_path)
        old = load_or_create_computer_id(path)
        new = reset_computer_id(path)

        assert new != old
        assert is_canonical_computer_id(new)
        assert path.read_text(encoding="utf-8") == f"{new}\n"
        assert load_or_create_computer_id(path) == new

    def test_reset_creates_file_when_absent(self, tmp_path: Path) -> None:
        path = _identity_file(tmp_path)
        value = reset_computer_id(path)
        assert is_canonical_computer_id(value)
        assert path.read_text(encoding="utf-8") == f"{value}\n"


class TestCorruptionAndConcurrency:
    """损坏 fail-fast；并发首启同值；空 / 半写重试。"""

    def test_corrupt_complete_line_fails_fast(self, tmp_path: Path) -> None:
        path = _identity_file(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-a-uuid\n", encoding="utf-8")

        with pytest.raises(ComputerIdentityError, match="非法"):
            load_or_create_computer_id(path)
        # 绝不自动覆写：损坏内容原样保留 / never overwrite a corrupt identity silently
        assert path.read_text(encoding="utf-8") == "not-a-uuid\n"

    def test_trailing_newline_in_value_is_corrupt(self, tmp_path: Path) -> None:
        """fix-review 阻塞项：``<uuid>\\n\\n`` 不得被当成合法（整串 + 恰 36 字符）。"""
        path = _identity_file(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("abcdef01-2345-4678-8abc-def012345678\n\n", encoding="utf-8")

        with pytest.raises(ComputerIdentityError, match="非法"):
            load_or_create_computer_id(path)

    def test_nil_and_uppercase_are_corrupt(self, tmp_path: Path) -> None:
        path = _identity_file(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for bad in (
            "00000000-0000-0000-0000-000000000000\n",
            "ABCDEF01-2345-4678-8ABC-DEF012345678\n",
        ):
            path.write_text(bad, encoding="utf-8")
            with pytest.raises(ComputerIdentityError):
                load_or_create_computer_id(path)

    def test_empty_file_times_out(self, tmp_path: Path) -> None:
        path = _identity_file(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

        with pytest.raises(ComputerIdentityError, match="空或半写"):
            load_or_create_computer_id(path, retry_budget=0.05)

    def test_half_written_file_is_retried_then_adopted(self, tmp_path: Path) -> None:
        """并发创建者已建文件但尚未写完 ⇒ 重试后读到完整值（≤1s 契约）。"""
        path = _identity_file(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        target_value = "abcdef01-2345-4678-8abc-def012345678"

        def _complete() -> None:
            time.sleep(0.1)
            path.write_text(f"{target_value}\n", encoding="utf-8")

        writer = threading.Thread(target=_complete)
        writer.start()
        try:
            assert load_or_create_computer_id(path, retry_budget=1.0) == target_value
        finally:
            writer.join()

    def test_concurrent_first_start_shares_one_value(self, tmp_path: Path) -> None:
        """同机并发首启（O_CREAT|O_EXCL）⇒ 所有调用者得到同一值。"""
        path = _identity_file(tmp_path)
        barrier = threading.Barrier(8)
        results: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _worker() -> None:
            try:
                barrier.wait(timeout=5)
                value = load_or_create_computer_id(path)
                with lock:
                    results.append(value)
            except BaseException as e:  # noqa: BLE001 - 收集后统一断言
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors, errors
        assert len(results) == 8
        assert len(set(results)) == 1, f"并发首启必须共用同一值: {results!r}"
        assert path.read_text(encoding="utf-8") == f"{results[0]}\n"

    def test_unwritable_directory_fails_fast(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        with pytest.raises(ComputerIdentityError):
            load_or_create_computer_id(blocker / "a2c" / "computer-id")
