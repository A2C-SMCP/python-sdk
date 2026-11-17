# -*- coding: utf-8 -*-
# filename: optional_deps.py
"""
中文: 可选 ``[cli]`` extra 依赖的惰性导入助手（#226），#248 起**委托**到共享实现。

    ``require_cli_dependency`` 与 ``CLI_EXTRA_HINT`` 是 v0.5.0 已发布的公开面
    （``computer/utils/console.py``、``computer/inputs/cli_io.py``、``clean_install_smoke.sh`` 都走它），
    故保留为 re-export shim：粒度与文案全部由 :func:`a2c_smcp.utils.optional_deps.require_extra`
    决定，真正实现在那里（``[cli]`` / ``[server]`` 共用一套）。**不要在本模块再长出第二份实现**——
    两份实现的粒度漂移正是 #248 要消除的问题。
English: Lazy-import helper for the optional ``[cli]`` extra (#226); since #248 it **delegates** to
    the shared implementation. ``require_cli_dependency`` and ``CLI_EXTRA_HINT`` are public surface
    published in v0.5.0 (``computer/utils/console.py``, ``computer/inputs/cli_io.py`` and
    ``clean_install_smoke.sh`` still use them), so they survive as a re-export shim; the granularity
    and the message come from :func:`a2c_smcp.utils.optional_deps.require_extra`, which both extras
    share. Never grow a second implementation here — diverging granularity is exactly what #248 fixed.

约束 / Constraint:
    #248 之前本模块只允许导入标准库（它位于 ``computer`` 导入链底层）。现在会经 ``a2c_smcp.utils``
    的 eager ``__init__`` 导入其同级模块——已核实这条链不反向加载 ``a2c_smcp.computer``，且
    ``mcp_clients/base_client.py`` 本就导入 ``a2c_smcp.utils``，故导入面未变宽、不成环。
    Before #248 this module was stdlib-only (it sits at the bottom of the ``computer`` import chain).
    It now pulls ``a2c_smcp.utils``' eager ``__init__`` siblings; that chain provably never imports
    ``a2c_smcp.computer`` back, and ``mcp_clients/base_client.py`` already imports ``a2c_smcp.utils``,
    so the import surface neither widens nor closes a cycle.
"""

from __future__ import annotations

from types import ModuleType

from a2c_smcp.utils.optional_deps import require_extra

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
    return require_extra(module, CLI_EXTRA_HINT)
