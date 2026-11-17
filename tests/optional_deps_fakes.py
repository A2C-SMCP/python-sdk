# -*- coding: utf-8 -*-
"""
中文: 惰性导入测试替身（#248）。``require_extra`` 只从**模块级** ``importlib`` 取 ``import_module``，
    故把它整体替换即可精确控制「缺哪个模块」，并**记录调用**。
    **调用记录是防假绿的承重墙**：未装对应 extra 的环境里，真实导入同样抛 ``ModuleNotFoundError``，
    只断言异常类型无法区分「走了注入的 fake」与「走了真实导入」——独立实现的旧路径会在此现形。
English: Lazy-import test doubles (#248). ``require_extra`` resolves ``import_module`` off its
    module-global ``importlib``, so swapping that object pins exactly which module goes missing —
    and *records the calls*. The call log is the load-bearing guard against false green: an
    environment without the extra raises the same ``ModuleNotFoundError``, so asserting on the
    exception alone cannot tell the injected fake apart from a real import.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any


class RecordingImportlib:
    """中文: 记录被导入模块名的 ``importlib`` 替身，按构造时的 `exc` 抛错或按 `result` 返回。

    English: ``importlib`` stand-in that records requested module names and either raises the
    ``exc`` given at construction or returns ``result``.
    """

    def __init__(self, *, exc: BaseException | None = None, result: ModuleType | None = None) -> None:
        if (exc is None) == (result is None):
            raise ValueError("RecordingImportlib 需要 exc / result 恰一个 / exactly one of exc, result")
        self.calls: list[str] = []
        self._exc = exc
        self._result = result

    def import_module(self, name: str) -> Any:
        self.calls.append(name)
        if self._exc is not None:
            raise self._exc
        return self._result
