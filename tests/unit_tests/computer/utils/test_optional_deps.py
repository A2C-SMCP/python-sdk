"""#226：`require_cli_dependency` 的异常粒度。

中文: 只有「缺的正是请求的模块或其父包」时才套 `[cli]` extra 处方；传递依赖缺失说明装坏了，
必须原样抛出真因，不能被伪装成「没装 extra」。
English: Only a missing requested module (or parent package) gets the ``[cli]`` extra hint;
a missing transitive dependency means a broken install and must surface its real cause.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from a2c_smcp.computer.utils import optional_deps


def _fake_import(exc: BaseException) -> SimpleNamespace:
    def _import(name: str) -> None:
        raise exc

    return SimpleNamespace(import_module=_import)


def _patch(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    # 只替换本模块对 importlib 的引用，不动全局 importlib。
    monkeypatch.setattr(optional_deps, "importlib", _fake_import(exc))


def test_missing_requested_module_gets_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ModuleNotFoundError("No module named 'prompt_toolkit'", name="prompt_toolkit"))

    with pytest.raises(ImportError) as excinfo:
        optional_deps.require_cli_dependency("prompt_toolkit")

    assert "a2c-smcp[cli]" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)


def test_missing_parent_package_gets_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ModuleNotFoundError("No module named 'rich'", name="rich"))

    with pytest.raises(ImportError) as excinfo:
        optional_deps.require_cli_dependency("rich.table")

    assert "a2c-smcp[cli]" in str(excinfo.value)


def test_missing_transitive_dependency_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ModuleNotFoundError("No module named 'wcwidth'", name="wcwidth"))

    with pytest.raises(ModuleNotFoundError) as excinfo:
        optional_deps.require_cli_dependency("prompt_toolkit")

    assert excinfo.value.name == "wcwidth"
    assert "a2c-smcp[cli]" not in str(excinfo.value), "传递依赖缺失不得伪装成没装 extra"


def test_missing_module_without_name_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ModuleNotFoundError("No module named ..."))

    with pytest.raises(ModuleNotFoundError) as excinfo:
        optional_deps.require_cli_dependency("prompt_toolkit")

    assert "a2c-smcp[cli]" not in str(excinfo.value)


def test_positive_control_returns_real_module() -> None:
    if importlib.util.find_spec("typer") is None:
        pytest.skip("本环境未安装 cli extra")

    module = optional_deps.require_cli_dependency("typer")

    assert module.__name__ == "typer"
