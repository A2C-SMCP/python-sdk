"""#248：`require_extra` 的异常粒度（契约自 #226 的 `require_cli_dependency` 上收）。

中文: 只有「缺的正是请求的模块或其父包」时才套 extra 处方；传递依赖缺失说明装坏了，必须原样
抛出真因，不能被伪装成「没装 extra」。本文件同时钉住 **`[cli]` / `[server]` 两类 extra 行为一致**
——那正是 #248 要消除的分歧（服务端曾宽捕 `ImportError`，把坏安装也说成「没装 extra」）。
English: Only a missing requested module (or its parent package) gets the extra hint; a missing
transitive dependency means a broken install and must surface its real cause. The parametrised
cases pin that both extras now behave identically — exactly the divergence #248 removed (the
server side used to catch ``ImportError`` broadly and blame a missing extra for a broken install).
"""

from __future__ import annotations

from types import ModuleType

import pytest

from a2c_smcp.utils import optional_deps
from tests.optional_deps_fakes import RecordingImportlib

# 两类 extra 代表：模块名与真实提示文案的**形状**（文案本身由调用方持有，这里只验helper 拼接）。
EXTRAS = [
    pytest.param("prompt_toolkit", 'pip install "a2c-smcp[cli]"', id="cli"),
    pytest.param("uvicorn", 'pip install "a2c-smcp[server]"', id="server"),
]


@pytest.mark.parametrize(("module", "hint"), EXTRAS)
def test_missing_requested_module_gets_install_hint(
    monkeypatch: pytest.MonkeyPatch, module: str, hint: str
) -> None:
    exc = ModuleNotFoundError(f"No module named '{module}'", name=module)
    fake = RecordingImportlib(exc=exc)
    monkeypatch.setattr(optional_deps, "importlib", fake)

    with pytest.raises(ImportError) as excinfo:
        optional_deps.require_extra(module, hint)

    assert fake.calls == [module], "必须走注入的 importlib，否则断言的是真实环境而非被测逻辑"
    assert hint in str(excinfo.value)
    assert f"(missing: {module})" in str(excinfo.value)
    assert excinfo.value.__cause__ is exc


@pytest.mark.parametrize(
    ("module", "missing", "hint"),
    [
        pytest.param("rich.table", "rich", 'pip install "a2c-smcp[cli]"', id="cli"),
        pytest.param("uvicorn.middleware", "uvicorn", 'pip install "a2c-smcp[server]"', id="server"),
    ],
)
def test_missing_parent_package_gets_install_hint(
    monkeypatch: pytest.MonkeyPatch, module: str, missing: str, hint: str
) -> None:
    """中文: 父包缺失与目标模块缺失等价——都是「没装 extra」，须给同一处方。

    文案必须钉住 ``(missing: {module})``（**请求的**模块），不是 ``(missing: {missing})``——
    后者在父包路径上会把 ``rich.table`` 漂移成 ``rich``，让用户照着一个装不上的名字去查。
    """
    exc = ModuleNotFoundError(f"No module named '{missing}'", name=missing)
    fake = RecordingImportlib(exc=exc)
    monkeypatch.setattr(optional_deps, "importlib", fake)

    with pytest.raises(ImportError) as excinfo:
        optional_deps.require_extra(module, hint)

    assert fake.calls == [module]
    assert hint in str(excinfo.value)
    assert f"(missing: {module})" in str(excinfo.value), "文案须报请求的模块，不是缺失的父包"
    assert excinfo.value.__cause__ is exc


@pytest.mark.parametrize(
    ("module", "transitive", "hint"),
    [
        pytest.param("prompt_toolkit", "wcwidth", 'pip install "a2c-smcp[cli]"', id="cli"),
        pytest.param("uvicorn", "click", 'pip install "a2c-smcp[server]"', id="server"),
    ],
)
def test_missing_transitive_dependency_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch, module: str, transitive: str, hint: str
) -> None:
    """中文: 传递依赖缺失 = 装坏了，原样抛让真因可见，绝不说成「没装 extra」。"""
    exc = ModuleNotFoundError(f"No module named '{transitive}'", name=transitive)
    fake = RecordingImportlib(exc=exc)
    monkeypatch.setattr(optional_deps, "importlib", fake)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        optional_deps.require_extra(module, hint)

    assert fake.calls == [module]
    assert excinfo.value is exc
    assert excinfo.value.name == transitive
    assert hint not in str(excinfo.value), "传递依赖缺失不得伪装成没装 extra"


def test_missing_module_without_name_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """中文: 缺 `name` 时无法断定缺的就是目标模块，宁可原样抛也不误报 extra。"""
    exc = ModuleNotFoundError("No module named ...")
    fake = RecordingImportlib(exc=exc)
    monkeypatch.setattr(optional_deps, "importlib", fake)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        optional_deps.require_extra("prompt_toolkit", 'pip install "a2c-smcp[cli]"')

    assert excinfo.value is exc
    assert "a2c-smcp[cli]" not in str(excinfo.value)


def test_plain_import_error_inside_target_module_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """中文: 目标模块**找得到**、但它自己在导入期抛普通 ``ImportError``（如 Windows 上
        ``werkzeug.serving`` 的 ``DLL load failed``）时，同样原样抛。

    这正是服务端宽捕 `ImportError` 误诊的另一类：把「模块坏了」说成「没装 extra」。
    只捕 ``ModuleNotFoundError`` 才能同时放过传递依赖缺失与这类内部失败。
    """
    exc = ImportError("cannot import name 'make_server' from 'werkzeug.serving'")
    fake = RecordingImportlib(exc=exc)
    monkeypatch.setattr(optional_deps, "importlib", fake)

    with pytest.raises(ImportError) as excinfo:
        optional_deps.require_extra("werkzeug.serving", 'pip install "a2c-smcp[server]"')

    assert fake.calls == ["werkzeug.serving"]
    assert excinfo.value is exc, "普通 ImportError 必须原样冒泡，不换壳"
    assert "a2c-smcp[server]" not in str(excinfo.value)


def test_returns_module_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel: ModuleType = ModuleType("prompt_toolkit")
    fake = RecordingImportlib(result=sentinel)
    monkeypatch.setattr(optional_deps, "importlib", fake)

    assert optional_deps.require_extra("prompt_toolkit", 'pip install "a2c-smcp[cli]"') is sentinel
    assert fake.calls == ["prompt_toolkit"]
