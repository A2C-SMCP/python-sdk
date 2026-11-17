"""#248：`a2c_smcp.computer.utils.optional_deps` 是**委托 shim**，不是第二份实现。

中文: 旧路径是 v0.5.0 已发布的公开面（`console.py` / `cli_io.py` / `clean_install_smoke.sh` 都走它），
    故保留为 re-export shim。本文件要钉的正是「它真的委托到 `a2c_smcp.utils.optional_deps`」——
    把 fake 装在**新模块**上：若旧路径另有一份独立实现（自带 `importlib`），调用根本走不到这个 fake，
    `fake.calls` 会是空列表。这是「测试 patch 旧模块 `importlib` 造成永不生效的假绿」的反面锁。
English: The legacy path stays public (published in v0.5.0 and still used by ``console.py``,
    ``cli_io.py`` and ``clean_install_smoke.sh``), so it survives as a delegating shim. These
    tests pin that it *really* delegates: the fake is installed on the **new** module, so a
    legacy module carrying its own copy of the logic would never reach it and ``fake.calls``
    would stay empty. That is the inverse of the false green called out in #248.
"""

from __future__ import annotations

import importlib.util

import pytest

from a2c_smcp.computer.utils.optional_deps import CLI_EXTRA_HINT, require_cli_dependency
from a2c_smcp.utils import optional_deps
from tests.optional_deps_fakes import RecordingImportlib


def test_old_path_delegates_to_shared_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = ModuleNotFoundError("No module named 'prompt_toolkit'", name="prompt_toolkit")
    fake = RecordingImportlib(exc=exc)
    monkeypatch.setattr(optional_deps, "importlib", fake)

    with pytest.raises(ImportError) as excinfo:
        require_cli_dependency("prompt_toolkit")

    assert fake.calls == ["prompt_toolkit"], "旧路径必须真委托到共享 helper（独立实现则走不到该 fake）"
    assert "a2c-smcp[cli]" in str(excinfo.value)
    assert CLI_EXTRA_HINT in str(excinfo.value)
    assert excinfo.value.__cause__ is exc


def test_old_path_keeps_cli_hint_for_transitive_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """中文: 旧路径的粒度与文案保持 #226 原样——传递依赖缺失仍原样抛。"""
    exc = ModuleNotFoundError("No module named 'wcwidth'", name="wcwidth")
    fake = RecordingImportlib(exc=exc)
    monkeypatch.setattr(optional_deps, "importlib", fake)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        require_cli_dependency("prompt_toolkit")

    assert fake.calls == ["prompt_toolkit"]
    assert excinfo.value is exc
    assert "a2c-smcp[cli]" not in str(excinfo.value)


def test_old_path_returns_real_module() -> None:
    if importlib.util.find_spec("typer") is None:
        pytest.skip("本环境未安装 cli extra")

    module = require_cli_dependency("typer")

    assert module.__name__ == "typer"
