# -*- coding: utf-8 -*-
# filename: progress.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Rich 进度反馈（git clone/pull spinner）+ 刷新汇总表 / Rich progress feedback + refresh summary（S15，#68）。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §10.4。

:func:`clone_spinner` 在 clone/pull await 期间显示 spinner（json / 非终端下自动退化为 no-op）；
:func:`refresh_summary_table` 渲染多 marketplace 刷新汇总。
.. note::
   byte-级进度百分比（``45% (2.3/5.1 MiB)``）需 staging 暴露 git ``--progress`` stderr 行解析 hook
   （属 staging 改动、出本 PR cli/ 范围）；v0.2.1 先 spinner + 汇总满足可视进度，%-解析记为后续增强。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from a2c_smcp.computer.cli.utils import console


@contextlib.contextmanager
def _spinner(description: str) -> Iterator[None]:
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console) as p:
        p.add_task(description, total=None)
        yield


def clone_spinner(description: str, *, enabled: bool = True) -> contextlib.AbstractContextManager[None]:
    """clone/pull 期 spinner；``enabled=False`` 或非终端（json / 管道）→ no-op / Spinner, no-op when disabled。"""
    if not enabled or not console.is_terminal:
        return contextlib.nullcontext()
    return _spinner(description)


def refresh_summary_table(rows: list[dict[str, Any]]) -> Table:
    """多 marketplace 刷新汇总表（updated / unchanged / missing）/ Build the refresh summary table。"""
    table = Table(title="Refresh summary", header_style="bold magenta")
    for col in ("Marketplace", "Status", "Skills"):
        table.add_column(col)
    marks = {"updated": "✓", "unchanged": "·", "missing": "✗"}
    for r in rows:
        status = str(r.get("status", "?"))
        table.add_row(str(r.get("name", "")), f"{marks.get(status, '?')} {status}", str(r.get("skills", "—")))
    return table
