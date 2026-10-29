"""
中文: 可选 `[cli]` extra 依赖的惰性导入助手（#226）。
    与 #187 为 `a2c_smcp.testing` 定下的契约一致：未安装 extra 时 **import 不报错**，
    仅真正调用需要该依赖的功能时抛携带安装提示的 ``ImportError``。
English: Lazy-import helper for the optional ``[cli]`` extra (#226). Mirrors the #187 contract used by
    ``a2c_smcp.testing``: importing never requires the extra; only calling the feature that needs it
    raises an actionable ``ImportError`` carrying the install command.

约束 / Constraint:
    本模块只允许导入标准库——它位于 `computer` 包导入链的底层，反向 a2c_smcp.* 导入会形成环。
    This module may import stdlib only: it sits at the bottom of the `computer` import chain, so an
    ``a2c_smcp.*`` import here would create a cycle.
"""

from __future__ import annotations

import importlib
from types import ModuleType

CLI_EXTRA_HINT = (
    '交互式终端输入 / 渲染需要可选依赖（rich / prompt-toolkit），请安装: pip install "a2c-smcp[cli]"'
    " / Interactive terminal input/rendering needs the optional deps (rich / prompt-toolkit): "
    'pip install "a2c-smcp[cli]"'
)


def require_cli_dependency(module: str) -> ModuleType:
    """中文: 惰性导入 `[cli]` extra 的某个模块；缺失时抛可行动 ImportError（含安装命令）。

    English: Lazily import a module from the ``[cli]`` extra; raise an actionable ImportError
    (including the install command) when it is missing.

    Args:
        module: 目标模块名（如 ``"rich.console"`` / ``"prompt_toolkit"``）。

    Returns:
        ModuleType: 已导入的模块对象。

    Raises:
        ImportError: 模块缺失（``ModuleNotFoundError`` 为其子类），消息含 ``pip install "a2c-smcp[cli]"``。
    """
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        # 只捕获 ModuleNotFoundError：若模块本身在、但内部依赖装坏，应原样暴露真因，
        # 不伪装成「没装 extra」。
        # Only ModuleNotFoundError: a broken install must surface its real cause, not be rewritten
        # into "the extra is missing".
        raise ImportError(f"{CLI_EXTRA_HINT} (missing: {module})") from exc
