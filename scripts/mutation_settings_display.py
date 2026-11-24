#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
#295 settings 校验错误展示面（脱敏 + markup 安全）的**可复现变异矩阵**（dev-only，不进 CI）。

背景：验收要求「去脱敏 / 去 escape / 去 reason 脱敏」三轴都必须让定向用例转红。本脚本把每个不变量逐个
注入为 mutation，运行对应定向用例，断言必须转红（KILLED）。全部 mutation 在 ``try/finally`` 内应用与
还原，任何中断都会把源文件恢复原样；结束前逐字节校验还原成功。

运行（不要用 ``uv run``，避免在 worktree 内重建环境；本脚本会临时改写源码，勿与编辑/并发 run 同时进行）::

    python scripts/mutation_settings_display.py            # 应用变异并跑定向用例
    python scripts/mutation_settings_display.py --check    # 只读：校验各 old 片段仍唯一定位

备份目录按 PID 隔离（``mut295_<pid>_*``），结束逐文件 sha256 校验还原。

退出码：全部 KILLED → 0；任一 SURVIVED / SETUP-FAIL → 1。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

CLI_INIT = "a2c_smcp/computer/cli/commands/__init__.py"
SETTINGS_CLI = "a2c_smcp/computer/cli/commands/settings.py"
PLUGIN_CLI = "a2c_smcp/computer/cli/commands/plugin.py"
INTERACTIVE = "a2c_smcp/computer/cli/interactive_impl.py"

T_INIT = "tests/unit_tests/computer/cli/test_commands_init.py"
T_SETTINGS = "tests/unit_tests/computer/cli/test_settings.py"
T_APPROVAL = "tests/unit_tests/computer/cli/test_mcp_approval.py"
T_INTERACTIVE = "tests/unit_tests/computer/cli/test_interactive_more.py"
T_PLUGIN = "tests/unit_tests/computer/cli/test_plugin.py"

_FIELD_AND_REASON = (
    "        field, reason = (sanitized.field, sanitized.reason) if sanitized is not None else (e.field, e.reason)"
)
_PYTEST_TIMEOUT_S = 300  # 子进程硬超时：挂起时不得让父进程抱着**变异态**源码一起挂死


@dataclass(frozen=True)
class Mutation:
    name: str
    edits: tuple[tuple[str, str, str], ...]  # (file, old, new)
    tests: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M1 去展示漏斗脱敏（format_settings_errors 直用未脱敏 field/reason）",
        (
            (
                CLI_INIT,
                "        sanitized = sanitized_marketplace_error(e)\n" + _FIELD_AND_REASON + "\n",
                "        field, reason = e.field, e.reason\n",
            ),
        ),
        (
            T_INIT + "::test_format_settings_errors_sanitizes_marketplace_declaration_fields",
            T_INIT + "::test_format_settings_errors_redacts_marketplace_name_shapes",
            T_SETTINGS + "::test_settings_commands_redact_marketplace_credentials",
            T_APPROVAL + "::test_settings_error_line_redacts_marketplace_credentials",
        ),
    ),
    Mutation(
        "M2 去 escape（_emit_errors 直接把整行当 markup 解析）",
        (
            (
                SETTINGS_CLI,
                'console_err.print(f"[yellow]{escape(line)}[/yellow]")',
                'console_err.print(f"[yellow]{line}[/yellow]")',
            ),
        ),
        (T_SETTINGS + "::test_settings_commands_escape_markup_in_error_lines",),
    ),
    Mutation(
        "M3 只去 reason 脱敏（field 仍走段漏斗）",
        (
            (
                CLI_INIT,
                _FIELD_AND_REASON,
                "        field, reason = (sanitized.field, e.reason) if sanitized is not None else (e.field, e.reason)",
            ),
        ),
        (
            T_INIT + "::test_format_settings_errors_sanitizes_marketplace_reason_text",
            T_INIT + "::test_format_settings_errors_sanitizes_marketplace_declaration_fields",
        ),
    ),
    Mutation(
        "M4 去审批门 server 名 escape（展示值退回裸 repr）",
        (
            (
                PLUGIN_CLI,
                "    return escape(repr(name))",
                "    return repr(name)",
            ),
        ),
        (T_APPROVAL + "::test_approval_lines_escape_markup_in_server_names",),
    ),
    Mutation(
        "M5 去 settings.py 插值 escape（_err 与成功行）",
        (
            (
                SETTINGS_CLI,
                'console.print(f"[red]✗ {escape(msg)}[/red]")',
                'console.print(f"[red]✗ {msg}[/red]")',
            ),
            (
                SETTINGS_CLI,
                'console.print(f"[green]✓ set {escape(key)} in {escape(scope)} scope[/green]")',
                'console.print(f"[green]✓ set {key} in {scope} scope[/green]")',
            ),
        ),
        (
            T_SETTINGS + "::test_settings_get_markup_key_reports_without_crash",
            T_SETTINGS + "::test_settings_set_markup_key_writes_then_reports",
        ),
    ),
    Mutation(
        "M6 去审批门挂载失败行的异常 escape",
        (
            (
                PLUGIN_CLI,
                "✗ failed to mount MCP server {_server_name_for_display(name)}: {escape(str(exc))}[/red]",
                "✗ failed to mount MCP server {_server_name_for_display(name)}: {str(exc)}[/red]",
            ),
        ),
        (T_APPROVAL + "::test_approval_mount_failure_line_escapes_markup_exception",),
    ),
    Mutation(
        "M7 去审批门 input 冲突行的 id detail escape",
        (
            (
                PLUGIN_CLI,
                '        detail = f"{escape(\', \'.join(repr(i) for i in now))}（本次跳过 / skipped）"',
                '        detail = f"{\', \'.join(repr(i) for i in now)}（本次跳过 / skipped）"',
            ),
        ),
        (T_APPROVAL + "::test_input_collision_line_escapes_markup_ids",),
    ),
    Mutation(
        "M8 去 REPL 顶层降级兜底的 escape",
        (
            (
                INTERACTIVE,
                'console.print(f"[{style}]{escape(message)}: {escape(str(exc))}[/{style}]")',
                'console.print(f"[{style}]{message}: {str(exc)}[/{style}]")',
            ),
        ),
        (T_INTERACTIVE + "::test_print_degraded_escapes_markup",),
    ),
    Mutation(
        "M9 去未知子命令提示的 escape",
        (
            (
                CLI_INIT,
                'console_util.console.print(f"[yellow]unknown {escape(kind)} subcommand: {escape(sub)}[/yellow]")',
                'console_util.console.print(f"[yellow]unknown {kind} subcommand: {sub}[/yellow]")',
            ),
        ),
        (T_INIT + "::test_print_unknown_subcommand_escapes_markup",),
    ),
    Mutation(
        "M10 去审批门摘除失败行的异常 escape",
        (
            (
                PLUGIN_CLI,
                "✗ failed to unmount disabled MCP server {_server_name_for_display(name)}: {escape(str(exc))}[/red]",
                "✗ failed to unmount disabled MCP server {_server_name_for_display(name)}: {str(exc)}[/red]",
            ),
        ),
        (T_APPROVAL + "::test_approval_unmount_failure_line_escapes_markup_exception",),
    ),
    Mutation(
        "M11 去治理重挂成功行的 escape",
        (
            (
                PLUGIN_CLI,
                'f"[green]✓ restored bundled MCP server {escape(repr(cfg.name))} "',
                'f"[green]✓ restored bundled MCP server {cfg.name!r} "',
            ),
        ),
        (T_PLUGIN + "::test_run_governance_remount_register_line_escapes_markup",),
    ),
    Mutation(
        "M12 去 plugin 分派器接线（回退为未转义的直排）",
        (
            (
                PLUGIN_CLI,
                '        print_unknown_subcommand("plugin", sub)',
                '        console.print(f"[yellow]unknown plugin subcommand: {sub}[/yellow]")',
            ),
        ),
        (T_PLUGIN + "::test_repl_unknown_subcommand_escapes_markup",),
    ),
    Mutation(
        "M13 去交互循环审批兜底的接线（回退为未转义直排）",
        (
            (
                INTERACTIVE,
                '_print_degraded("⚠ MCP 批准门控初始化失败 / MCP approval init failed", e)',
                'console.print(f"[yellow]⚠ MCP 批准门控初始化失败 / MCP approval init failed: {e}[/yellow]")',
            ),
        ),
        (T_INTERACTIVE + "::test_interactive_degradations_route_to_safe_helper",),
    ),
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_ACTIVE_BACKUPS: dict[str, tuple[str, Path]] = {}


def _restore_active() -> None:
    for rel, (digest, tmp) in list(_ACTIVE_BACKUPS.items()):
        shutil.copy2(tmp, ROOT / rel)
        assert _digest(ROOT / rel) == digest, f"restore failed: {rel}"
        _ACTIVE_BACKUPS.pop(rel, None)


def _sig_handler(signum: int, _frame: object) -> None:
    _restore_active()
    raise SystemExit(128 + signum)


def _classify(proc: subprocess.CompletedProcess[str], tests: tuple[str, ...]) -> tuple[str, str]:
    """KILLED 判据：**仅** pytest 返回 1（有用例失败）且输出确实列出目标用例失败。

    ``returncode != 0`` 不足以证明变异被杀死（解释器缺失 / 收集错误 / 超时都会非 0）；
    目标用例名出现才说明是**该变异**打红了目标。
    """
    out = proc.stdout + proc.stderr
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if proc.returncode == 0:
        return "SURVIVED", tail
    if proc.returncode == 1:
        names = [t.rsplit("::", 1)[-1] for t in tests]
        if all(name in out for name in names):
            return "KILLED", tail
        return "ERROR(目标用例未失败)", tail
    return f"ERROR(rc={proc.returncode})", tail


def check() -> int:
    """只读校验：每条 mutation 的 ``old`` 片段仍能在当前源码中唯一定位。"""
    ok = True
    for mutation in MUTATIONS:
        for rel, old, _new in mutation.edits:
            text = (ROOT / rel).read_text(encoding="utf-8")
            count = text.count(old)
            if count != 1:
                print(f"{mutation.name}: CHECK-FAIL ({rel}: occurrence={count})")
                ok = False
    print("CHECK: OK" if ok else "CHECK: FAILED")
    return 0 if ok else 1


def baseline() -> int:
    """变异前先跑一遍全部目标用例：**必须全绿**，否则任何「失败」都证明不了是变异造成的。

    与 #211 的 M3 假杀死同型教训：目标用例若因无关原因（import/env 漂移、夹具坏）本就为红，``_classify``
    会把它误判成 KILLED。基线不为绿直接终止，不进入变异循环。
    """
    tests = sorted({t for mutation in MUTATIONS for t in mutation.tests})
    try:
        proc = subprocess.run(
            [PY, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=_PYTEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"BASELINE: ERROR(timeout>{_PYTEST_TIMEOUT_S}s)")
        return 1
    if proc.returncode != 0:
        tail = proc.stdout.strip().splitlines()[-1:] or [""]
        print(f"BASELINE: FAIL（目标用例变异前即为红，变异判据不可信）| {tail[0]}")
        return 1
    print(f"BASELINE: OK（{len(tests)} 个目标用例全绿）")
    return 0


def main() -> int:
    # 每次都先做锚点唯一性校验：源码漂移时立即失败，避免半途 SETUP-FAIL / 误改无关片段。
    if check() != 0:
        return 1
    if "--check" in sys.argv[1:]:
        return 0
    try:
        import pytest  # noqa: F401
    except ImportError:
        print("pytest 不可用：请用项目 venv 运行（不要 uv run / 不要裸 python），否则变异会被误判。")
        return 2
    if baseline() != 0:
        return 1

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    failures: list[str] = []
    # 每次运行独立备份目录（per-PID）：并发 run / 并发 worktree 不互相覆盖；退出时整体清理。
    backup_dir = Path(tempfile.mkdtemp(prefix=f"mut295_{os.getpid()}_"))
    try:
        for mutation in MUTATIONS:
            backups: dict[str, tuple[str, Path]] = {}
            try:
                for rel, old, new in mutation.edits:
                    path = ROOT / rel
                    if rel not in backups:
                        tmp = backup_dir / f"{len(backups)}_{path.name}.bak"
                        shutil.copy2(path, tmp)
                        backups[rel] = (_digest(path), tmp)
                    text = path.read_text(encoding="utf-8")
                    if old not in text:
                        print(f"{mutation.name}: SETUP-FAIL ({rel})")
                        failures.append(mutation.name)
                        break
                    path.write_text(text.replace(old, new, 1), encoding="utf-8")
                else:
                    # 备份登记为「活跃」：子进程期间收到 SIGTERM/SIGINT 也能还原。
                    _ACTIVE_BACKUPS.update(backups)
                    try:
                        proc = subprocess.run(
                            [PY, "-m", "pytest", *mutation.tests, "-q", "-p", "no:cacheprovider"],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=_PYTEST_TIMEOUT_S,
                        )
                    except subprocess.TimeoutExpired:
                        # 挂起不是 KILLED：目标用例没证明失败，且这是基础设施故障。
                        print(f"{mutation.name}: ERROR(timeout>{_PYTEST_TIMEOUT_S}s)")
                        failures.append(mutation.name)
                    else:
                        status, tail = _classify(proc, mutation.tests)
                        print(f"{mutation.name}: {status} | {tail}")
                        if status != "KILLED":
                            failures.append(mutation.name)
            finally:
                for rel, (digest, tmp) in backups.items():
                    shutil.copy2(tmp, ROOT / rel)
                    assert _digest(ROOT / rel) == digest, f"restore failed: {rel}"
                    _ACTIVE_BACKUPS.pop(rel, None)
    finally:
        _restore_active()
        shutil.rmtree(backup_dir, ignore_errors=True)
    print(f"\nSUMMARY: {len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} KILLED")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
