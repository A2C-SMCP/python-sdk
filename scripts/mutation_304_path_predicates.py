#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
#304 item 3 settings 路径判据（readRoots 单一判据 / landingRoot trim）的**可复现变异矩阵**（dev-only，不进 CI）。

背景：验收要求「加回 `_is_absolute_path` / 去 trim / 谓词合并 / 去 normalize / 委托写死平台 /
运行期忽略平台」各轴都必须让定向用例转红。本脚本把每个不变量逐个注入为 mutation，运行对应定向用例，
断言必须转红（KILLED）。全部 mutation 在 ``try/finally`` 内应用与还原，任何中断都会把源文件恢复原样；
结束前逐字节校验还原成功。

运行（不要用 ``uv run``，避免在 worktree 内重建环境；本脚本会临时改写源码，勿与编辑/并发 run 同时进行）::

    python scripts/mutation_304_path_predicates.py            # 应用变异并跑定向用例
    python scripts/mutation_304_path_predicates.py --check    # 只读：校验各 old 片段仍唯一定位

备份目录按 PID 隔离（``mut304_<pid>_*``），结束逐文件 sha256 校验还原。

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

SCHEMA = "a2c_smcp/computer/settings/schema.py"
RESOLVER = "a2c_smcp/computer/blob/path_resolver.py"

T_READ_ROOTS = "tests/unit_tests/computer/settings/test_read_roots.py"
T_SCHEMA = "tests/unit_tests/computer/settings/test_schema.py"

_NORMALIZE_CALL = "        try:\n            normalize_path(item, platform)\n"
_LANDING_ROOT_PREDICATE = (
    "    trimmed = value.strip()\n"
    '    if not trimmed or not _is_absolute_path(trimmed):\n'
    '        return _DROP, [_err(scope, key, "must be an absolute path")]\n'
    "    return trimmed, []"
)


@dataclass(frozen=True)
class Mutation:
    name: str
    edits: tuple[tuple[str, str, str], ...]  # (file, old, new)
    tests: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M1 加回前置 `_is_absolute_path`（两套口径分叉，verbatim 被误拒）",
        (
            (
                SCHEMA,
                _NORMALIZE_CALL,
                "        if not _is_absolute_path(item):\n"
                '            errors.append(_err(scope, field, "must be an absolute path"))\n'
                "            continue\n" + _NORMALIZE_CALL,
            ),
        ),
        (
            T_READ_ROOTS + "::test_read_roots_accepts_windows_verbatim_prefix",
            T_READ_ROOTS + "::test_read_roots_config_time_and_runtime_share_one_predicate",
            T_READ_ROOTS + "::test_read_roots_production_entry_uses_host_platform",
        ),
    ),
    Mutation(
        "M2 去 landingRoot trim（带首尾空白的配置重新失效）",
        ((SCHEMA, "    trimmed = value.strip()\n", "    trimmed = value\n"),),
        (T_SCHEMA + "::TestLandingRoot::test_surrounding_whitespace_trimmed_and_activated",),
    ),
    Mutation(
        "M3 谓词合并（landingRoot 改用 normalize_path）",
        (
            (
                SCHEMA,
                _LANDING_ROOT_PREDICATE,
                "    trimmed = value.strip()\n"
                "    if not trimmed:\n"
                '        return _DROP, [_err(scope, key, "must be an absolute path")]\n'
                "    try:\n"
                "        normalize_path(trimmed, native_platform())\n"
                "    except InvalidPathError:\n"
                '        return _DROP, [_err(scope, key, "must be an absolute path")]\n'
                "    return trimmed, []",
            ),
        ),
        (T_SCHEMA + "::TestLandingRoot::test_syntactic_predicate_is_deliberately_not_merged",),
    ),
    Mutation(
        "M4 删掉 readRoots 的 normalize_path 判据（不再拒绝非法形态）",
        (
            (
                SCHEMA,
                _NORMALIZE_CALL
                + "        except InvalidPathError as exc:\n"
                '            errors.append(_err(scope, field, f"invalid path: {exc}"))\n'
                "            continue\n",
                "",
            ),
        ),
        (
            T_READ_ROOTS + "::test_read_roots_rejects_drive_relative_on_windows",
            T_READ_ROOTS + "::test_read_roots_config_time_and_runtime_share_one_predicate",
        ),
    ),
    Mutation(
        "M5 生产入口不取宿主平台（取反平台，任意宿主均可判）",
        (
            (
                SCHEMA,
                "    return _validate_read_roots_for(key, value, scope, native_platform())",
                '    return _validate_read_roots_for(key, value, scope, "windows" if native_platform() == "posix" else "posix")',
            ),
        ),
        (T_READ_ROOTS + "::test_read_roots_production_entry_uses_host_platform",),
    ),
    Mutation(
        "M6 运行期 build_read_roots 忽略 platform 注入（配置期/运行期脱钩）",
        (
            (
                RESOLVER,
                "    故 Windows 形态的一致性对拍需要显式平台。\n"
                '    """\n'
                "    pf: PathPlatform = native_platform() if platform is None else platform\n"
                "    roots: list[ReadRoot] = []",
                "    故 Windows 形态的一致性对拍需要显式平台。\n"
                '    """\n'
                "    pf: PathPlatform = native_platform()\n"
                "    roots: list[ReadRoot] = []",
            ),
        ),
        (T_READ_ROOTS + "::test_read_roots_config_time_and_runtime_share_one_predicate",),
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

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    failures: list[str] = []
    # 每次运行独立备份目录（per-PID）：并发 run / 并发 worktree 不互相覆盖；退出时整体清理。
    backup_dir = Path(tempfile.mkdtemp(prefix=f"mut304_{os.getpid()}_"))
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
                    proc = subprocess.run(
                        [PY, "-m", "pytest", *mutation.tests, "-q", "-p", "no:cacheprovider"],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                    )
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
