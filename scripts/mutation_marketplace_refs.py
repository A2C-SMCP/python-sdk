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
import signal
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
PLUGIN_CLI = "a2c_smcp/computer/cli/commands/plugin.py"
CLI_MAIN = "a2c_smcp/computer/cli/main.py"

T_ID = "tests/unit_tests/computer/skills/test_marketplace_identity.py"
T_CLI = "tests/unit_tests/computer/cli/test_marketplace.py"
T_PLUGIN = "tests/unit_tests/computer/cli/test_plugin.py"
T_SETTINGS = "tests/unit_tests/computer/cli/test_settings.py"
T_STAGING = "tests/unit_tests/computer/skills/test_staging.py"


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
        "M9 marketplace REPL 全分支环境偷用 ambient（不跟 axes.env）",
        ((MP_CLI, "    env = axes.env\n", "    env = os.environ\n"),),
        (T_CLI + "::test_repl_add_uses_instance_env_axis",),
    ),
    Mutation(
        "M18 marketplace REPL 只认 comp.env（axes-only factory 混源）",
        ((MP_CLI, "    env = axes.env\n", '    env = getattr(comp, "env", None)\n'),),
        (T_CLI + "::test_repl_axes_only_factory_list_reads_instance_env",),
    ),
    Mutation(
        "M18b marketplace REPL add 只认 comp.env（axes-only factory 混源）",
        ((MP_CLI, "    env = axes.env\n", '    env = getattr(comp, "env", None)\n'),),
        (T_CLI + "::test_repl_axes_only_factory_add_reads_instance_env",),
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
        ((SETTINGS_CLI, "    env = axes.env\n", "    env = os.environ\n"),),
        (T_CLI + "::test_repl_settings_set_flows_into_marketplace_guard",),
    ),
    Mutation(
        "M14 显式 policy 注入被忽略（空 {} 不再覆盖 ambient）",
        (
            (
                IDENT,
                "        policy_settings=policy_settings if policy_settings is not None else resolve_policy_settings(env=env),",
                "        policy_settings=resolve_policy_settings(env=env),",
            ),
        ),
        (T_ID + "::test_intent_reference_pids_none_resolves_ambient_policy_and_explicit_overrides",),
    ),
    Mutation(
        "M15 plugin REPL 偷用 ambient os.environ（install 写错 user settings）",
        ((PLUGIN_CLI, "    env = axes.env\n", "    env = os.environ\n"),),
        (T_PLUGIN + "::test_repl_install_user_scope_uses_instance_env",),
    ),
    Mutation(
        "M16 plugin REPL project_path 退回进程 cwd",
        ((PLUGIN_CLI, "    project_path = str(axes.cwd or os.getcwd())", "    project_path = os.getcwd()"),),
        (T_PLUGIN + "::test_repl_install_project_scope_uses_instance_cwd",),
    ),
    Mutation(
        "M17 stage 提交点不转发轴（stage -> commit）",
        (
            (
                STAGING,
                """                    strict=False,
                    allow_replace=True,
                    cwd=cwd,
                    flag_settings_path=flag_settings_path,
                    policy_settings=policy_settings,
""",
                """                    strict=False,
                    allow_replace=True,
""",
            ),
        ),
        (T_CLI + "::test_stage_unregistered_name_commit_uses_flag_layer_guard",),
    ),
    Mutation(
        "M19 settings REPL 不传实例 cwd",
        ((SETTINGS_CLI, "    cwd = axes.cwd\n", "    cwd = None\n"),),
        (T_SETTINGS + "::test_repl_settings_show_project_and_local_scopes_use_instance_cwd",),
    ),
    Mutation(
        "M20 settings REPL 不传实例 flag 层",
        ((SETTINGS_CLI, "    flag_path = axes.flag_settings_path\n", "    flag_path = None\n"),),
        (T_SETTINGS + "::test_repl_settings_show_flag_scope_reads_instance_flag",),
    ),
    Mutation(
        "M21 settings REPL 丢弃实例 policy",
        ((SETTINGS_CLI, "    policy_settings = axes.policy_settings\n", "    policy_settings = None\n"),),
        (T_SETTINGS + "::test_repl_settings_policy_scope_and_merged_use_instance_policy",),
    ),
    Mutation(
        "M22 settings 读路径忽略 cwd 参数",
        (
            (
                SETTINGS_CLI,
                """    if scope in ("project", "local"):
        resolved_cwd = cwd if cwd is not None else Path(os.getcwd())
        path = (""",
                """    if scope in ("project", "local"):
        resolved_cwd = Path(os.getcwd())
        path = (""",
            ),
        ),
        (T_SETTINGS + "::test_repl_settings_show_project_and_local_scopes_use_instance_cwd",),
    ),
    Mutation(
        "M23 不剥除 ambient GIT_* 仓库定位变量",
        ((STAGING, "            base.pop(var, None)", "            pass"),),
        (T_STAGING + "::test_git_env_strips_ambient_repo_locators",),
    ),
    Mutation(
        "M24 不强制 ambient GIT_SSH_COMMAND 的 BatchMode",
        ((STAGING, '    if "-oBatchMode=yes" not in ssh_cmd:', "    if False:"),),
        (T_STAGING + "::test_git_env_forces_batch_mode_on_ambient_ssh_command",),
    ),
)


def _digest(path: Path) -> str:
    """文件字节摘要（还原校验用；避免「同文本不同编码」漏检）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 当前已应用的变异备份（SIGTERM / SIGINT 时据此还原，避免把 mutation 留在工作区）。
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
