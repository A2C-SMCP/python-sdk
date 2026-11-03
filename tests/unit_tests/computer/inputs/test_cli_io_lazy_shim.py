"""#226：cli 安装面下 `PromptSession` shim 的正向委托。

中文: 现有 11 处 monkeypatch 全绕过 shim 本体——若 shim 被掏空（如 `return None`），
这些测试仍会绿。本文件钉住「有 extra 时 shim 真的委托到 prompt_toolkit」，并让 `ainput_pick`
用假会话真跑一次，覆盖 `rich.table` 路径。
English: The existing monkeypatches bypass the shim body, so a hollowed shim would stay green.
These tests pin that the shim really delegates to prompt_toolkit when the extra is installed,
and run `ainput_pick` once with a fake session to cover the `rich.table` path.
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest

from a2c_smcp.computer.inputs import cli_io
from a2c_smcp.computer.utils.optional_deps import require_cli_dependency

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("prompt_toolkit") is None or importlib.util.find_spec("rich") is None,
    reason="需要 cli extra（prompt_toolkit / rich）",
)


class _FakeSession:
    """最小会话替身：只实现 `prompt_async`。"""

    def __init__(self, value: str) -> None:
        self._value = value

    async def prompt_async(self, message: str, **kwargs: object) -> str:
        return self._value


def test_prompt_session_shim_delegates_to_prompt_toolkit() -> None:
    import prompt_toolkit

    assert cli_io.PromptSession is not prompt_toolkit.PromptSession, "应保留惰性 shim 而非直接别名"
    assert isinstance(cli_io.PromptSession(), prompt_toolkit.PromptSession)
    assert require_cli_dependency("prompt_toolkit").PromptSession is prompt_toolkit.PromptSession


def test_ainput_pick_renders_rich_table_with_injected_session() -> None:
    result = asyncio.run(cli_io.ainput_pick("pick", ["a", "b"], session=_FakeSession("1")))

    assert result == "b"


def test_ainput_prompt_uses_injected_session() -> None:
    result = asyncio.run(cli_io.ainput_prompt("value", session=_FakeSession("x")))

    assert result == "x"
