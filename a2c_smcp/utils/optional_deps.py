# -*- coding: utf-8 -*-
# filename: optional_deps.py
"""
中文: 可选 extra 依赖的惰性导入助手（#248）。**本模块只提供机制**，不认识任何具体 extra。

    它把 ``importlib.import_module`` 的失败按「缺的是不是你要的那个模块」分成两类：

    1. **是**（缺失名 = 目标模块，或是其父包前缀）⇒ 抛携带安装提示的可行动 ``ImportError``，
       消息为 ``"{hint} (missing: {module})"``；
    2. **不是**（传递依赖缺失 / 装坏了）⇒ 原样抛出，让真因可见——把坏安装说成「没装 extra」
       只会把用户引向重装一遍 extra 的死路。

    提示文案由**调用方**持有（``[cli]`` 见 ``a2c_smcp.computer.utils.optional_deps``，
    ``[server]`` 见 ``a2c_smcp.testing.server``）：机制与政策分层，本模块因此可被任何 extra 复用。
English: Lazy-import helper for optional extras (#248) — **mechanism only**, it knows no extra.

    It splits ``importlib.import_module`` failures in two: a missing *requested* module (or one of
    its parent packages) becomes an actionable ``ImportError`` shaped as ``"{hint} (missing: {module})"``;
    anything else (a missing transitive dependency / a broken install) is re-raised untouched so the
    real cause stays visible — blaming a missing extra sends the user down a reinstall-the-extra
    dead end. Hint text is owned by the caller (``[cli]`` in
    ``a2c_smcp.computer.utils.optional_deps``, ``[server]`` in ``a2c_smcp.testing.server``).

约束 / Constraint:
    只允许导入标准库。``a2c_smcp.utils`` 的 ``__init__`` 是 **eager** 导入（它本就导入
    ``a2c_smcp.smcp``，见 ``utils/office.py``），故本模块及其所在包**不得**反向依赖
    ``a2c_smcp.computer``——``computer → utils`` 是既有方向，``utils → computer`` 才成环。
    Stdlib only: ``a2c_smcp.utils.__init__`` imports eagerly (it already pulls ``a2c_smcp.smcp``,
    see ``utils/office.py``), so this module (and its package) must never depend back on
    ``a2c_smcp.computer``: ``computer → utils`` is the established direction, ``utils → computer``
    would close the loop.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def require_extra(module: str, hint: str) -> ModuleType:
    """中文: 惰性导入某个 extra 提供的模块；**该模块本身**缺失时抛可行动 ImportError。

    English: Lazily import a module provided by an extra; raise an actionable ImportError when
    that module itself is missing.

    Args:
        module: 目标模块名（如 ``"rich.console"`` / ``"uvicorn"``）/ target module name.
        hint: 缺失时附带的安装提示（含可通过的安装命令）/ install hint embedded in the error.

    Returns:
        ModuleType: 已导入的模块对象 / the imported module object.

    Raises:
        ImportError: 目标模块或其父包缺失——消息为 ``"{hint} (missing: {module})"``。
        ModuleNotFoundError: 传递依赖缺失 / 坏安装——原样抛出，真因可见 / re-raised untouched.
    """
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        # 只有「缺的正是请求的模块或其父包」才套 extra 处方；若是传递依赖缺失
        # （如 prompt_toolkit 在、wcwidth 不在，或 uvicorn 在、click 不在），说明装坏了，
        # 原样抛让真因可见。
        # Only rewrite when the missing name is the requested module (or a parent package).
        # A missing transitive dependency means a broken install: surface the real cause untouched.
        missing = exc.name or ""
        if missing != module and not module.startswith(f"{missing}."):
            raise
        raise ImportError(f"{hint} (missing: {module})") from exc
