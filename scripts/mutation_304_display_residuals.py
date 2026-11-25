#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
#304 items 1/2 展示面脱敏（enabledPlugins 键 / mcp.json 摘要行）的**可复现变异矩阵**（dev-only，不进 CI）。

背景：验收要求「去 enabledPlugins 漏斗 / 拆分式错修 / 摘要回退 str(error) / 只脱敏 field 不脱敏 reason /
明细不脱敏 source_path / 名字漏斗去 scrub / 删 DEBUG 明细 / debug 改 console / pydantic 安全文本回退」
各轴都必须让定向用例转红。本脚本把每个不变量逐个注入为 mutation，运行对应定向用例，断言必须转红
（KILLED）。全部 mutation 在 ``try/finally`` 内应用与还原，任何中断都会把源文件恢复原样；结束前逐字节
校验还原成功。

运行（不要用 ``uv run``，避免在 worktree 内重建环境；本脚本会临时改写源码，勿与编辑/并发 run 同时进行）::

    python scripts/mutation_304_display_residuals.py            # 应用变异并跑定向用例
    python scripts/mutation_304_display_residuals.py --check    # 只读：校验各 old 片段仍唯一定位

进入变异循环前先跑 :func:`baseline`（每条变异的目标用例子集必须各自全绿，否则「失败」证明不了是变异造成的）；
子进程带硬超时（挂起不是 KILLED，且 ``finally`` 必须能还原）。备份目录按 PID 隔离（``mut304d_<pid>_*``），
结束逐文件 sha256 校验还原。

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
_PYTEST_TIMEOUT_S = 300  # 子进程硬超时：挂起时不得让父进程抱着**变异态**源码一起挂死

CLI_INIT = "a2c_smcp/computer/cli/commands/__init__.py"
PLUGIN = "a2c_smcp/computer/cli/commands/plugin.py"
RECOVERY = "a2c_smcp/computer/settings/recovery.py"
MCP_CONFIG = "a2c_smcp/computer/settings/mcp_config.py"

T_INIT = "tests/unit_tests/computer/cli/test_commands_init.py"
T_APPROVAL = "tests/unit_tests/computer/cli/test_mcp_approval.py"
T_SETTINGS = "tests/unit_tests/computer/cli/test_settings.py"

_ENABLED_PLUGINS_STEP = "        field = sanitize_prefixed_name_field(field, prefixes=(FIELD_ENABLED_PLUGINS,))\n"
_MCP_LINE_REASON = (
    '    return f"⚠ mcp.json[{error.scope.value}]: {field} — {sanitize_marketplace_text(error.reason)}"'
)
_DEBUG_LINE = '        logger.debug("mcp.json declaration error detail: %s", mcp_declaration_error_detail(e))\n'


@dataclass(frozen=True)
class Mutation:
    name: str
    edits: tuple[tuple[str, str, str], ...]  # (file, old, new)
    tests: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M1 去掉 enabledPlugins 漏斗（field 原样带凭据）",
        ((CLI_INIT, _ENABLED_PLUGINS_STEP, ""),),
        (
            T_INIT + "::test_format_settings_errors_redacts_enabled_plugins_credentials",
            T_SETTINGS + "::test_settings_commands_redact_enabled_plugins_credentials",
        ),
    ),
    Mutation(
        "M2 拆分式错修（只脱敏 @ 之后半段，plugin 半段泄漏）",
        (
            (
                CLI_INIT,
                _ENABLED_PLUGINS_STEP,
                "        if field.startswith(f\"{FIELD_ENABLED_PLUGINS}.\"):\n"
                "            _head, _, _mp = field[len(FIELD_ENABLED_PLUGINS) + 1:].partition(\"@\")\n"
                "            if _mp:\n"
                "                field = f\"{FIELD_ENABLED_PLUGINS}.{_head}@{sanitize_marketplace_name(_mp)}\"\n",
            ),
        ),
        (T_INIT + "::test_format_settings_errors_redacts_enabled_plugins_credentials",),
    ),
    Mutation(
        "M3 摘要回退 str(error)（整条 dataclass repr 上屏）",
        ((PLUGIN, _MCP_LINE_REASON, "    return str(error)"),),
        (
            T_APPROVAL + "::test_mcp_declaration_error_line_redacts_server_name_credentials",
            T_APPROVAL + "::test_mcp_json_error_line_redacts_server_name_credentials",
        ),
    ),
    Mutation(
        "M4 只脱敏 field、不脱敏 reason（name-mismatch 文案泄漏）",
        (
            (PLUGIN, _MCP_LINE_REASON, '    return f"⚠ mcp.json[{error.scope.value}]: {field} — {error.reason}"'),
            (
                PLUGIN,
                'f"scope={error.scope.value} field={field} reason={sanitize_marketplace_text(error.reason)} "',
                'f"scope={error.scope.value} field={field} reason={error.reason} "',
            ),
        ),
        (T_APPROVAL + "::test_mcp_declaration_error_line_redacts_name_embedded_in_reason",),
    ),
    Mutation(
        "M5 明细不脱敏 source_path",
        (
            (
                PLUGIN,
                '    source = "<unknown>" if error.source_path is None else sanitize_source_path(error.source_path)',
                '    source = "<unknown>" if error.source_path is None else error.source_path',
            ),
        ),
        (T_APPROVAL + "::test_mcp_declaration_error_detail_sanitizes_all_four_segments",),
    ),
    Mutation(
        "M6 名字漏斗去 scrub（Cf 族透传）",
        (
            (
                RECOVERY,
                '            return f"{head}{sanitize_marketplace_name(rest)}"',
                '            return f"{head}{redact_ledger_key(rest)}"',
            ),
        ),
        (
            T_INIT + "::test_format_settings_errors_scrubs_bidi_in_enabled_plugin_key",
            T_APPROVAL + "::test_mcp_declaration_error_detail_scrubs_bidi_and_marks_missing_source",
        ),
    ),
    Mutation(
        "M7 删除 DEBUG 明细",
        ((PLUGIN, _DEBUG_LINE, ""),),
        (T_APPROVAL + "::test_mcp_json_error_detail_is_logged_at_debug",),
    ),
    Mutation(
        "M8 DEBUG 明细改打 console（INFO 级顶到终端）",
        ((PLUGIN, _DEBUG_LINE, "        console.print(mcp_declaration_error_detail(e))\n"),),
        (T_APPROVAL + "::test_mcp_json_error_detail_stays_off_console_at_info_level",),
    ),
    Mutation(
        "M9 pydantic 安全文本回退 str(exc)（input_value 回归）",
        (
            (
                MCP_CONFIG,
                'f"invalid MCP server config: {pydantic_error_text(exc, sanitize_segment=_sanitize_declaration_name)}"',
                'f"invalid MCP server config: {exc}"',
            ),
        ),
        (T_APPROVAL + "::test_mcp_json_error_never_echoes_credential_payload",),
    ),
    Mutation(
        "M10 name-mismatch reason 回退原始 name 值（构造处不脱敏）",
        (
            (
                MCP_CONFIG,
                """        reason = (
            f"server 'name' field {shown!r} != map key "
            f"{_sanitize_declaration_name(name)!r} (the map key is the canonical identity)"
        )""",
                '''        reason = f"server 'name' field {body['name']!r} != map key {name!r} (the map key is the canonical identity)"''',
            ),
        ),
        (T_APPROVAL + "::test_mcp_json_error_reason_redacts_whitespace_split_credentials",),
    ),
    Mutation(
        "M11 pydantic loc 不过名字漏斗（手编 extra key 被空白劈开后泄漏）",
        (
            (
                MCP_CONFIG,
                "f\"invalid MCP server config: {pydantic_error_text(exc, sanitize_segment=_sanitize_declaration_name)}\"",
                'f"invalid MCP server config: {pydantic_error_text(exc)}"',
            ),
            (
                MCP_CONFIG,
                "f\"invalid input definition: {pydantic_error_text(exc, sanitize_segment=_sanitize_declaration_name)}\"",
                'f"invalid input definition: {pydantic_error_text(exc)}"',
            ),
        ),
        (
            T_APPROVAL + "::test_mcp_json_error_loc_redacts_whitespace_split_credentials",
            T_APPROVAL + "::test_mcp_json_error_input_loc_redacts_extra_key_credentials",
        ),
    ),
    Mutation(
        "M12 声明字段前缀去掉 inputs（inputs.<id> 泄漏）",
        (
            (
                MCP_CONFIG,
                "MCP_DECLARATION_FIELD_PREFIXES: tuple[str, str] = (MCP_SERVERS_FIELD, MCP_INPUTS_FIELD)",
                "MCP_DECLARATION_FIELD_PREFIXES: tuple[str, ...] = (MCP_SERVERS_FIELD,)",
            ),
        ),
        (T_APPROVAL + "::test_mcp_json_error_line_redacts_input_id_credentials",),
    ),
    Mutation(
        "M13 <file> reason 回退内嵌路径的 OSError 文本",
        (
            (
                MCP_CONFIG,
                "        detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc",
                "        detail = exc",
            ),
        ),
        (T_APPROVAL + "::test_mcp_json_error_file_reason_omits_path",),
    ),
    Mutation(
        "M14 DEBUG 明细改 logger.info（默认 INFO 级顶到终端）",
        (
            (
                PLUGIN,
                _DEBUG_LINE,
                '        logger.info("mcp.json declaration error detail: %s", mcp_declaration_error_detail(e))\n',
            ),
        ),
        (T_APPROVAL + "::test_mcp_json_error_detail_stays_off_console_at_info_level",),
    ),
    Mutation(
        "M15 非字符串 name 回退 str()（嵌套裸 token 上屏）",
        (
            (
                MCP_CONFIG,
                '        shown = _sanitize_declaration_name(raw_name) if isinstance(raw_name, str) else "<non-string>"',
                "        shown = _sanitize_declaration_name(raw_name) if isinstance(raw_name, str) else str(raw_name)",
            ),
        ),
        (T_APPROVAL + "::test_mcp_json_error_non_string_name_value_not_echoed",),
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
    """变异前**按每条变异的用例子集**各跑一遍：**必须全绿**，否则任何「失败」都证明不了是变异造成的。

    与 #211 的 M3 假杀死同型教训：目标用例若因无关原因（import/env 漂移、夹具坏）本就为红，``_classify``
    会把它误判成 KILLED。**必须按子集而非并集**：并集里绿、单跑为红（顺序/状态依赖）的用例仍会让该条变异
    的独立运行被误判。任一子集不为绿即终止，不进入变异循环。
    """
    for mutation in MUTATIONS:
        try:
            proc = subprocess.run(
                [PY, "-m", "pytest", *mutation.tests, "-q", "-p", "no:cacheprovider"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=_PYTEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            print(f"BASELINE: ERROR(timeout>{_PYTEST_TIMEOUT_S}s) — {mutation.name}")
            return 1
        if proc.returncode != 0:
            tail = proc.stdout.strip().splitlines()[-1:] or [""]
            print(f"BASELINE: FAIL（{mutation.name} 的目标用例变异前即为红）| {tail[0]}")
            return 1
    print(f"BASELINE: OK（{len(MUTATIONS)} 条变异的目标用例子集各自全绿）")
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
    backup_dir = Path(tempfile.mkdtemp(prefix=f"mut304d_{os.getpid()}_"))
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
                        # 挂起不是 KILLED：记 ERROR 并继续下一条（源码由 finally 还原）。
                        print(f"{mutation.name}: ERROR(timeout>{_PYTEST_TIMEOUT_S}s)")
                        failures.append(mutation.name)
                        continue
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
