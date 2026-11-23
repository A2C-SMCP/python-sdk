#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
#296 marketplace re-add 引用守卫（flag / policy 层）的**可复现变异矩阵**（dev-only，不进 CI）。

背景：验收要求「去 flag / 去 policy / 不并集 / 跳过 references」等变异必须让定向用例转红。本脚本把每个
不变量逐个注入为 mutation，运行对应定向用例，断言必须转红（KILLED）。全部 mutation 在 ``try/finally``
内应用与还原，任何中断都会把源文件恢复原样；结束前逐字节校验还原成功。

运行（不要用 ``uv run``，避免在 worktree 内重建环境；本脚本会临时改写源码，勿与编辑/并发 run 同时进行）::

    python scripts/mutation_marketplace_refs.py            # 应用变异并跑定向用例
    python scripts/mutation_marketplace_refs.py --check    # 只读：校验各 old 片段仍唯一定位

备份目录按 PID 隔离（``mut296_<pid>_*``），结束逐文件 sha256 校验还原。

退出码：全部 KILLED → 0；任一 SURVIVED / SETUP-FAIL → 1。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

IDENT = "a2c_smcp/computer/skills/marketplace_identity.py"
STAGING = "a2c_smcp/computer/skills/staging.py"
SCOPE = "a2c_smcp/computer/settings/scope.py"
MP_CLI = "a2c_smcp/computer/cli/commands/marketplace.py"
SETTINGS_CLI = "a2c_smcp/computer/cli/commands/settings.py"
CLI_MAIN = "a2c_smcp/computer/cli/main.py"

T_ID = "tests/unit_tests/computer/skills/test_marketplace_identity.py"
T_CLI = "tests/unit_tests/computer/cli/test_marketplace.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    edits: tuple[tuple[str, str, str], ...]  # (file, old, new)
    tests: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M1 去 flag 层（intent_reference_pids 不传 flag_settings_path）",
        ((IDENT, "        flag_settings_path=flag_settings_path,\n", ""),),
        (
            T_ID + "::test_intent_reference_pids_includes_flag_layer",
            T_CLI + "::test_no_clone_rejected_by_flag_layer_reference",
        ),
    ),
    Mutation(
        "M2 去 policy 层（intent_reference_pids 强制空 policy）",
        (
            (
                IDENT,
                "        policy_settings=policy_settings if policy_settings is not None else resolve_policy_settings(env=env),",
                "        policy_settings={},",
            ),
        ),
        (
            T_ID + "::test_intent_reference_pids_policy_layer_true_and_false_both_count",
            T_CLI + "::test_no_clone_rejected_by_injected_policy_reference",
        ),
    ),
    Mutation(
        "M3 层不并集（merge_read 映射键高覆盖低）",
        (
            (
                SCOPE,
                """            if key in merged:
                merged[key] = merge_read(merged[key], hv, on_conflict=on_conflict, _path=child_path)""",
                """            if key in merged:
                merged[key] = hv""",
            ),
        ),
        (T_ID + "::test_intent_reference_pids_union_dedup_flag_and_policy",),
    ),
    Mutation(
        "M4 installed ∪ enabled 只取一侧（丢 enabledPlugins）",
        (
            (
                IDENT,
                "        pids.update(key for key in enabled if isinstance(key, str))",
                "        pass  # MUTATION: enabledPlugins ignored",
            ),
        ),
        (T_ID + "::test_intent_reference_pids_unions_installed_and_enabled",),
    ),
    Mutation(
        "M5 跳过 references 判定",
        ((STAGING, "        if references:\n            shown_refs", "        if False:\n            shown_refs"),),
        (T_ID + "::test_dangling_references_rejected_for_new_registration",),
    ),
    Mutation(
        "M6 提交点不传轴（commit -> registration_violation）",
        (
            (
                STAGING,
                """            observed_manifest_name,
            env=env,
            cwd=cwd,
            flag_settings_path=flag_settings_path,
            policy_settings=policy_settings,
        )""",
                """            observed_manifest_name,
            env=env,
        )""",
            ),
        ),
        (T_CLI + "::test_add_rejected_by_flag_layer_reference",),
    ),
    Mutation(
        "M7 --no-clone 不传轴（_register_intent_only -> registration_violation）",
        (
            (
                MP_CLI,
                """            name,
            env=env,
            cwd=cwd,
            flag_settings_path=flag_settings_path,
            policy_settings=policy_settings,
        )""",
                """            name,
            env=env,
        )""",
            ),
        ),
        (T_CLI + "::test_no_clone_rejected_by_flag_layer_reference",),
    ),
    Mutation(
        "M8 非交互 CLI 不读根级 --settings",
        ((CLI_MAIN, "            flag_settings_path=st.flag_path,\n", ""),),
        (T_CLI + "::test_cli_root_settings_flag_rejects_no_clone_readd",),
    ),
    Mutation(
        "M9 marketplace REPL 偷用 ambient os.environ（不跟 comp.env）",
        ((MP_CLI, '    env = getattr(comp, "env", None)\n', "    env = os.environ\n"),),
        (T_CLI + "::test_repl_add_uses_instance_env_axis",),
    ),
    Mutation(
        "M10 REPL 不传 cwd 轴",
        ((MP_CLI, "cwd=axes.cwd, flag_settings_path", "flag_settings_path"),),
        (T_CLI + "::test_repl_add_uses_instance_cwd_axis",),
    ),
    Mutation(
        "M11 REPL 不传 flag 轴",
        ((MP_CLI, "flag_settings_path=axes.flag_settings_path, policy_settings", "policy_settings"),),
        (T_CLI + "::test_repl_add_uses_instance_flag_axis",),
    ),
    Mutation(
        "M12 policy_settings=None 不解析 ambient policy（库层绕过）",
        (
            (
                IDENT,
                "        policy_settings=policy_settings if policy_settings is not None else resolve_policy_settings(env=env),",
                "        policy_settings=policy_settings,",
            ),
        ),
        (T_ID + "::test_intent_reference_pids_none_resolves_ambient_policy_and_explicit_overrides",),
    ),
    Mutation(
        "M13 settings REPL 与 marketplace 分派器 env 不同源（跨面逃逸）",
        ((SETTINGS_CLI, '    env = getattr(comp, "env", None)\n', "    env = os.environ\n"),),
        (T_CLI + "::test_repl_settings_set_flows_into_marketplace_guard",),
    ),
)


def _digest(path: Path) -> str:
    """文件字节摘要（还原校验用；避免「同文本不同编码」漏检）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    if "--check" in sys.argv[1:]:
        return check()

    failures: list[str] = []
    # 每次运行独立备份目录（per-PID）：并发 run / 并发 worktree 不互相覆盖；退出时整体清理。
    backup_dir = Path(tempfile.mkdtemp(prefix=f"mut296_{os.getpid()}_"))
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
                    proc = subprocess.run(
                        [PY, "-m", "pytest", *mutation.tests, "-q", "-p", "no:cacheprovider"],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                    )
                    status = "KILLED" if proc.returncode != 0 else "SURVIVED"
                    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
                    print(f"{mutation.name}: {status} | {tail}")
                    if status != "KILLED":
                        failures.append(mutation.name)
            finally:
                for rel, (digest, tmp) in backups.items():
                    shutil.copy2(tmp, ROOT / rel)
                    assert _digest(ROOT / rel) == digest, f"restore failed: {rel}"
    finally:
        shutil.rmtree(backup_dir, ignore_errors=True)
    print(f"\nSUMMARY: {len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} KILLED")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
