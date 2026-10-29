"""#226：纯 core 安装（无 `[cli]` extra）下的导入契约。

中文 / English:
    `import a2c_smcp.computer` 不得在导入期要求 rich / prompt_toolkit（#187 惰性契约）；
    交互式路径（console / ainput_prompt）在缺失时须抛可行动 ImportError，文案含安装命令。
    全部用例在**子进程**中运行：本进程早已 import 过这些模块，`sys.modules` 阻断不可靠。

    Importing `a2c_smcp.computer` must not require rich / prompt_toolkit (the #187 lazy contract);
    interactive paths (console / ainput_prompt) must raise an actionable ImportError containing the
    install command when they are missing. Every case runs in a **subprocess** because this process
    has already imported those modules, making in-process `sys.modules` blocking unreliable.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_MARKER = "CORE_IMPORT_CONTRACT_OK"

# 子进程公共前置：来源断言（防导入到已安装的 wheel）+ marker。
# Shared subprocess preamble: source-tree assertion (so an installed wheel cannot mask a broken
# source tree) plus a marker for the parent to detect a clean finish.
_PREAMBLE = """
import importlib
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo_root))

_MARKER = "CORE_IMPORT_CONTRACT_OK"


def _assert_source():
    import a2c_smcp

    path = Path(a2c_smcp.__file__ or "").resolve()
    assert path.is_relative_to(repo_root), f"imported {path}, expected source tree {repo_root}"
"""

_BLOCK_CLI_DEPS = """
# 模拟纯 core 安装：把 rich / prompt_toolkit 全族置 None（import 时抛 ModuleNotFoundError）。
# Simulate a bare-core install: poison rich / prompt_toolkit (and submodules) in sys.modules.
for _name in (
    "rich",
    "rich.console",
    "rich.table",
    "prompt_toolkit",
    "prompt_toolkit.completion",
    "prompt_toolkit.document",
    "prompt_toolkit.patch_stdout",
):
    sys.modules[_name] = None


def _assert_blocked():
    # 夹具自检：阻断必须真的生效，否则后面「import 成功」是假绿。
    # Fixture self-check: the blocker must actually block, else a later "import succeeded" is false green.
    for _name in ("rich", "prompt_toolkit"):
        try:
            importlib.import_module(_name)
        except ModuleNotFoundError:
            continue
        raise SystemExit(f"fixture self-check failed: {_name} importable despite blocker")
"""


def _run(body: str, *, block_cli_deps: bool = False) -> subprocess.CompletedProcess[str]:
    """中文: 拼接「零缩进前置 + 可选的阻断块 + dedent(正文)」后执行。

    不要在拼接**之后**整体 dedent：前缀从第 0 列开始，整体 dedent 不会生效，而正文会被并进
    前缀最后一个函数体，子进程 0 退出却什么都没跑（假绿）。正文自身 dedent 后必须从第 0 列开始。
    """
    script = _PREAMBLE + (_BLOCK_CLI_DEPS if block_cli_deps else "") + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"subprocess failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert _MARKER in result.stdout, f"marker missing\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_core_import_does_not_load_cli_deps() -> None:
    """中文: `import a2c_smcp.computer` 不得把 rich / prompt_toolkit 拉进 sys.modules（惰性），
    且 console 正向可用（有 cli extra 时行为不变）。"""
    if importlib.util.find_spec("rich") is None or importlib.util.find_spec("prompt_toolkit") is None:
        import pytest

        pytest.skip("本环境未安装 cli extra，无法验证正向对照")

    result = _run(
        """
        import a2c_smcp.computer

        _assert_source()
        assert "rich" not in sys.modules, "rich was imported eagerly by a2c_smcp.computer"
        assert "prompt_toolkit" not in sys.modules, "prompt_toolkit was imported eagerly by a2c_smcp.computer"

        from a2c_smcp.computer.utils import console as console_util

        assert console_util.console is not None, "console must stay usable when the extra is installed"
        assert console_util.console is console_util.console, "console identity must be stable"
        print(_MARKER)
        """
    )
    _assert_ok(result)


def test_core_import_succeeds_without_cli_extra() -> None:
    """中文: 纯 core（rich / prompt_toolkit 不可导入）下 `import a2c_smcp.computer` 必须成功，
    且 `set_no_color` 不得触发依赖导入。"""
    result = _run(
        """
        _assert_blocked()

        import a2c_smcp.computer

        _assert_source()
        assert "rich" not in sys.modules
        assert "prompt_toolkit" not in sys.modules

        from a2c_smcp.computer.utils import console as console_util

        console_util.set_no_color(True)  # 必须不触发 rich 导入
        print(_MARKER)
        """,
        block_cli_deps=True,
    )
    _assert_ok(result)


def test_interactive_paths_raise_actionable_import_error_without_cli_extra() -> None:
    """中文: 纯 core 下真正需要 rich / prompt_toolkit 的路径必须抛可行动 ImportError
    （文案含 `pip install "a2c-smcp[cli]"`，cause 为 ModuleNotFoundError），不得静默。"""
    result = _run(
        """
        import asyncio

        _assert_blocked()

        import a2c_smcp.computer
        from a2c_smcp.computer.inputs.cli_io import ainput_prompt
        from a2c_smcp.computer.utils import console as console_util

        _assert_source()

        for label, call in (
            ("console", lambda: console_util.console),
            ("console_err", lambda: console_util.console_err),
            ("ainput_prompt", lambda: asyncio.run(ainput_prompt("value: "))),
        ):
            try:
                call()
            except ImportError as exc:
                text = str(exc)
                assert "a2c-smcp[cli]" in text, f"{label}: hint missing from {text!r}"
                assert isinstance(exc.__cause__, ModuleNotFoundError), f"{label}: cause is {exc.__cause__!r}"
            else:
                raise SystemExit(f"{label}: expected an actionable ImportError")
        print(_MARKER)
        """,
        block_cli_deps=True,
    )
    _assert_ok(result)
