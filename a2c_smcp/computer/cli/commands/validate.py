# -*- coding: utf-8 -*-
# filename: validate.py
# @Time    : 2026/08/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``plugin validate`` / ``marketplace validate`` handler（#193 双入口 alias）/ Validate command handler.

设计依据 / Design: python-sdk #193（用户裁决：双入口 alias + 全量收集 + 远程 source 跳过 + 契约面含
SKILL 深检）。校验逻辑全在 :mod:`a2c_smcp.computer.skills.validator`（纯逻辑、零副作用）；本模块只做
渲染（rich 人类可读 / ``--json`` 机器可读）与退出码赋予。

退出码 / Exit codes（与 plugin.py / marketplace.py 的 0/1 语义对齐；本命令无网络路径，不用 2）：
- ``0``：无 error（warnings 不影响——CI 语义：通过）；
- ``1``：存在任一 error（含路径不可识别 / 不存在）。

JSON 输出结构（自动化测试钉死，CI 消费契约面）/ JSON output contract::

    {"valid": bool, "mode": "marketplace"|"plugin"|null, "path": str,
     "checked_files": [str], "errors": [{"code","file","location","message"}],
     "warnings": [{"code","file","location","message"}]}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.markup import escape

from a2c_smcp.computer.cli.utils import console
from a2c_smcp.computer.skills.validator import (
    ValidationIssue,
    ValidationResult,
    validate_path,
)
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# 退出码语义（#193 验收：成功 0 / 任一错误非 0）/ Exit codes.
EXIT_OK = 0
EXIT_INVALID = 1

_MODE_LABEL: dict[str, str] = {"marketplace": "marketplace", "plugin": "plugin"}


def _issue_dict(issue: ValidationIssue) -> dict[str, Any]:
    """issue → JSON dict（四键契约面）/ Issue to its four-key JSON contract shape."""
    return {"code": issue.code, "file": issue.file, "location": issue.location, "message": issue.message}


def _render_human(result: ValidationResult) -> None:
    """rich 人类可读输出：逐条 ``file:location — message`` + 汇总 / Human-readable rendering with summary."""
    mode = _MODE_LABEL.get(result.mode or "", "unknown")
    console.print(f"[bold]Validating {mode}[/bold] at {result.root}")
    for issue in result.issues:
        where = f"{issue.file}:{issue.location}" if issue.location != "." else issue.file
        marker, color = ("✗", "red") if issue.severity == "error" else ("⚠", "yellow")
        # where / message 经 ``escape``：pydantic 诊断含 ``[type=missing, ...]`` 等方括号段，裸拼会被 rich
        # 当样式标签吞掉；``\\[code]`` 同理转义代码段。/ Escape rich markup in literal text.
        console.print(f"[{color}]{marker} {escape(where)}[/{color}] \\[{issue.code}] {escape(issue.message)}")
    n_err, n_warn = len(result.errors), len(result.warnings)
    if result.valid:
        console.print(f"[green]✓ valid — {len(result.checked)} file(s) checked, {n_warn} warning(s)[/green]")
    else:
        console.print(f"[red]✗ invalid — {n_err} error(s), {n_warn} warning(s)[/red]")


def _render_json(result: ValidationResult) -> dict[str, Any]:
    """JSON 机器可读输出（结构即本模块 docstring 契约面）/ Machine-readable payload."""
    return {
        "valid": result.valid,
        "mode": result.mode,
        "path": str(result.root),
        "checked_files": list(result.checked),
        "errors": [_issue_dict(i) for i in result.errors],
        "warnings": [_issue_dict(i) for i in result.warnings],
    }


def plugin_validate(path: Path, *, json_output: bool = False) -> int:
    """校验本地 marketplace 根或 plugin 目录（零副作用）/ Validate a local marketplace root or plugin dir.

    ``plugin validate <path>`` 与 ``marketplace validate <path>`` 双入口共用本 handler（#193 用户裁决：
    同一实现，两族各挂一个名字）。
    """
    result = validate_path(Path(path))
    if json_output:
        console.print_json(data=_render_json(result))
    else:
        _render_human(result)
    return EXIT_OK if result.valid else EXIT_INVALID
