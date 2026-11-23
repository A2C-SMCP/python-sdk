#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
#268 marketplace 迁移的**可复现变异矩阵**（dev-only，不进 CI）/ Reproducible mutation matrix.

背景：PR #298 多轮审查都要求「变异数字可复现」。本脚本把关键不变量逐个注入为 mutation，
运行对应的定向用例，断言必须转红（KILLED）。全部 mutation 在 ``try/finally`` 内应用与还原，
任何中断都会把源文件恢复原样；结束前逐字节校验还原成功。

运行（不要用 ``uv run``，避免在 worktree 内重建环境）::

    python scripts/mutation_marketplace_migration.py

退出码：全部 KILLED → 0；任一 SURVIVED / SETUP-FAIL → 1。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

MOD = "a2c_smcp/computer/settings/marketplace_migration.py"
STORE = "a2c_smcp/computer/settings/store.py"
SECRET = "a2c_smcp/computer/inputs/secret_store.py"
CLI = "a2c_smcp/computer/cli/commands/marketplace.py"
HELP = "a2c_smcp/computer/cli/help.py"

T = "tests/unit_tests/computer/settings/test_marketplace_migration.py"
S = "tests/unit_tests/computer/inputs/test_secret_store.py"
IT = "tests/integration_tests/computer/cli/test_marketplace_migrate.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    edits: tuple[tuple[str, str, str], ...]  # (file, old, new)
    tests: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "先加后减（installed/enabled）",
        (
            (
                MOD,
                """        for pid in item.installed_add:
            _write_installed_plugin(pid, True, env)
        for pid, value in item.enabled_set.items():
            _write_enabled_plugin(pid, value, "user", None, env)""",
                """        for pid in item.installed_remove:
            _write_installed_plugin(pid, False, env)
        for pid in item.enabled_remove:
            _write_enabled_plugin(pid, None, "user", None, env)""",
            ),
            (
                MOD,
                """        for pid in item.installed_remove:
            _write_installed_plugin(pid, False, env)
        for pid in item.enabled_remove:
            _write_enabled_plugin(pid, None, "user", None, env)
        _remove_user_references(item, env)""",
                """        _remove_user_references(item, env)""",
            ),
        ),
        (T + "::test_additive_writes_happen_before_removals",),
    ),
    Mutation(
        "先写新 secret 再删旧",
        (
            (MOD, "_rekey_secrets(item, secrets, write_new=True)", "_rekey_secrets(item, secrets, write_new=SWAP)"),
            (MOD, "_rekey_secrets(item, secrets, write_new=False)", "_rekey_secrets(item, secrets, write_new=True)"),
            (MOD, "_rekey_secrets(item, secrets, write_new=SWAP)", "_rekey_secrets(item, secrets, write_new=False)"),
        ),
        (T + "::test_new_secret_is_written_before_old_is_deleted",),
    ),
    Mutation(
        "P4 只删重建成功的旧账本",
        (
            (
                MOD,
                """            item.incomplete = True
            item.reason = item.reason or REASON_LEDGER_REBUILD_FAILED
            continue
        for record in records:""",
                """            item.ledger_rebuild.append((pid, "user", None))
            item.incomplete = True
            item.reason = item.reason or REASON_LEDGER_REBUILD_FAILED
            continue
        for record in records:""",
            ),
        ),
        (T + "::test_plugin_missing_from_new_manifest_keeps_old_ledger",),
    ),
    Mutation(
        "C4 排除源 pid",
        ((MOD, "    candidates = scan_pids - source_pids", "    candidates = scan_pids"),),
        (T + "::test_c4_source_pid_excluded_but_third_party_clash_aborts",),
    ),
    Mutation(
        "§7.4 认领谓词允许 D.name == old",
        ((MOD, '        if not (child.name == old or child.name not in known_names):', "        if child.name in known_names:"),),
        (T + "::test_claim_window_renames_old_directory",),
    ),
    Mutation(
        "strict 只读 loader",
        ((MOD, "store.load_known_marketplaces_strict(home, env)", "store.load_known_marketplaces(home, env)"),),
        (T + "::test_strict_loader_corrupt_never_renames",),
    ),
    Mutation(
        "keyring 不可用按项 abort",
        (
            (
                MOD,
                "            item.action = ACTION_BLOCKED\n            item.reason = REASON_KEYRING_UNAVAILABLE",
                "            item.action = ACTION_RENAME\n            item.reason = None",
            ),
        ),
        (T + "::test_keyring_unavailable_aborts_item_with_zero_writes",),
    ),
    Mutation(
        "新 known[new].baseline = new",
        ((MOD, '        record[MANIFEST_NAME_BASELINE_FIELD] = new_name\n', ""),),
        (T + "::test_new_known_record_resets_baseline",),
    ),
    Mutation(
        "冲突双守卫（handler + execute）",
        (
            (
                CLI,
                "    if apply and not plan.corrupt and not plan.errors and not blocking_conflicts(plan):",
                "    if apply and not plan.corrupt and not plan.errors:",
            ),
            (
                MOD,
                "    if plan.corrupt or plan.errors or blocking_conflicts(plan):\n        return plan",
                "    if plan.corrupt or plan.errors:\n        return plan",
            ),
        ),
        (IT + "::test_typer_migrate_conflict_exits_1_with_zero_writes",),
    ),
    Mutation(
        "默认 dry-run（apply 才写）",
        (
            (
                CLI,
                "    if apply and not plan.corrupt and not plan.errors and not blocking_conflicts(plan):",
                "    if not plan.corrupt and not plan.errors and not blocking_conflicts(plan):",
            ),
        ),
        (IT + "::test_typer_migrate_default_dry_run_then_apply",),
    ),
    Mutation(
        "apply 写入前重新检测",
        (
            (
                MOD,
                """    fresh = build_plan(
        home, env, names=selected, update_declarations=plan.update_declarations, cwd=plan.cwd
    )
    plan.items = fresh.items
    plan.conflicts = fresh.conflicts
    plan.corrupt = fresh.corrupt
    plan.errors = fresh.errors
    plan.applied = False
    if plan.corrupt or plan.errors or blocking_conflicts(plan):
        return plan
""",
                "    plan.applied = False\n",
            ),
        ),
        (T + "::test_plan_is_rebuilt_before_apply_and_concurrent_value_aborts",),
    ),
    Mutation(
        "旧 value P1 复检（含 null 键缺失）",
        (
            (
                MOD,
                """            if old_key not in data or data[old_key] != value:
                raise _ItemFailure(
                    REASON_VALUE_TARGET_CONFLICT,
                    f"source input value {old_key!r} changed during the migration; aborting before writing the new key",
                )
""",
                "",
            ),
        ),
        (T + "::test_old_value_changed_before_p1_aborts", T + "::test_null_old_value_missing_is_detected"),
    ),
    Mutation(
        "旧 value P3 复检",
        (
            (
                MOD,
                """            if old_key not in data or data[old_key] != value:
                raise _ItemFailure(
                    REASON_VALUE_TARGET_CONFLICT,
                    f"source input value {old_key!r} changed during the migration; keeping the old key",
                )
""",
                "",
            ),
        ),
        (T + "::test_old_value_changed_before_p3_aborts",),
    ),
    Mutation(
        "目标 value P1 异值不复写",
        (
            (
                MOD,
                """            if new_key in data:
                if data[new_key] != value:
                    raise _ItemFailure(
                        REASON_VALUE_TARGET_CONFLICT,
                        f"target input value {new_key!r} changed during the migration; refusing to overwrite",
                    )
                continue""",
                "            if new_key in data:\n                continue",
            ),
        ),
        (T + "::test_write_time_value_recheck_direct",),
    ),
    Mutation(
        "目标 value P3 删前验证",
        (
            (
                MOD,
                """            if new_key not in data or data[new_key] != value:
                raise _ItemFailure(
                    REASON_VALUE_TARGET_CONFLICT,
                    f"target input value {new_key!r} is missing or changed; keeping the old key",
                )""",
                "            pass",
            ),
        ),
        (T + "::test_rekey_values_p3_verifies_target_before_deleting_old", T + "::test_p3_target_value_exists_but_differs_aborts"),
    ),
    Mutation(
        "旧 secret P1/P3 复检",
        (
            (
                MOD,
                """            if _strict_get(pair.old_key) != pair.value:
                raise _ItemFailure(
                    REASON_SECRET_TARGET_CONFLICT,
                    f"source secret {pair.old_key!r} changed during the migration; aborting before writing the new key",
                )
""",
                "",
            ),
            (
                MOD,
                """            if _strict_get(pair.old_key) != pair.value:
                raise _ItemFailure(
                    REASON_SECRET_TARGET_CONFLICT,
                    f"source secret {pair.old_key!r} changed during the migration; keeping the old key",
                )
""",
                "",
            ),
        ),
        (T + "::test_old_secret_changed_before_p1_and_p3_aborts",),
    ),
    Mutation(
        "目标 secret P1 异值不复写",
        (
            (
                MOD,
                """            if existing is not None:
                if existing != pair.value:
                    raise _ItemFailure(
                        REASON_SECRET_TARGET_CONFLICT,
                        f"target secret key {pair.new_key!r} changed during the migration; refusing to overwrite",
                    )
                continue""",
                "            if existing is not None:\n                continue",
            ),
        ),
        (T + "::test_write_time_secret_recheck_aborts",),
    ),
    Mutation(
        "目标 secret P3 删前验证",
        (
            (
                MOD,
                """            if _strict_get(pair.new_key) != pair.value:
                raise _ItemFailure(
                    REASON_SECRET_TARGET_CONFLICT,
                    f"target secret key {pair.new_key!r} is missing or changed; keeping the old key",
                )
""",
                "",
            ),
        ),
        (T + "::test_p3_secret_target_missing_aborts_and_keeps_old", T + "::test_p3_target_secret_exists_but_differs_aborts"),
    ),
    Mutation(
        "旧注册 source 与计划快照比较",
        (
            (
                MOD,
                '    if not _source_unchanged(item.old_source, old_record.get("source")):',
                '    if item.old_source is not None and not same_source(item.old_source, old_record.get("source")):',
            ),
        ),
        (T + "::test_unrecognizable_source_unchanged_still_migrates", T + "::test_source_added_after_plan_aborts"),
    ),
    Mutation(
        "merge 保留目标记录元数据",
        (
            (
                MOD,
                """        if existing.get(MANIFEST_NAME_BASELINE_FIELD) == new_name:
            return  # 目标记录已就绪：不重写、不制造 mtime 变化""",
                "",
            ),
            (
                MOD,
                """        def _mut_merge(data: store.KnownMarketplacesFile) -> None:
            record = data["marketplaces"].get(new_name)
            if not isinstance(record, Mapping):
                raise _ItemFailure(REASON_NAME_TAKEN, "target registration disappeared during the migration")
            updated = dict(record)
            updated[MANIFEST_NAME_BASELINE_FIELD] = new_name
            data["marketplaces"][new_name] = updated  # type: ignore[assignment]""",
                """        def _mut_merge(data: store.KnownMarketplacesFile) -> None:
            record = dict(old_record)
            for key, value in existing.items():
                record.setdefault(key, value)
            record["installLocation"] = str(marketplace_skill_dir(home, new_name).resolve())
            record[MANIFEST_NAME_BASELINE_FIELD] = new_name
            data["marketplaces"][new_name] = record  # type: ignore[assignment]""",
            ),
        ),
        (T + "::test_merge_keeps_target_record_metadata", T + "::test_merge_write_known_new_is_noop_when_ready"),
    ),
    Mutation(
        "keyring 读失败 ≠ 键不存在",
        (
            (
                SECRET,
                """        try:
            secret: str | None = kr.get_password(self._service, input_id)
            return secret
        except Exception as e:
            raise SecretStoreError(f"keyring read failed: {e}") from e""",
                "        return self.get(input_id)",
            ),
        ),
        (S + "::test_get_strict_backend_error_raises", S + "::test_get_strict_unavailable_raises"),
    ),
    Mutation(
        "scope 归一（非 str/空 → user）",
        (
            (
                MOD,
                """    value = record.get("scope")
    return value if isinstance(value, str) and value else "user\"""",
                """    return str(record.get("scope") or "user")""",
            ),
        ),
        (T + "::test_invalid_scope_is_normalized_to_user", T + "::test_empty_scope_is_normalized_to_user"),
    ),
    Mutation(
        "project/local 缺 projectPath 预检 blocked",
        (
            (
                MOD,
                """                if scope in {"project", "local"} and not _norm_project_path(record):
                    item.action = ACTION_BLOCKED
                    item.reason = REASON_PROJECT_PATH_MISSING""",
                """                if scope in {"project", "local"} and not _norm_project_path(record):
                    pass
                    item.reason = REASON_PROJECT_PATH_MISSING""",
            ),
        ),
        (T + "::test_project_record_without_project_path_blocks_in_dry_run",),
    ),
    Mutation(
        "baseline 次选先直搬注册旧目录",
        (
            (
                MOD,
                """    if item.baseline_derived:
        # 清单不可读 → 旧目录不构成 §7.4 候选；**先**无条件搬注册的旧目录，绝不按清单名搬游离目录。
        if not old_dir.is_dir() or old_dir.is_symlink():
            raise _ItemFailure(REASON_CATALOG_LOST, "registered clone directory is missing or is a symlink")
        old_dir.rename(new_dir)
        return

""",
                "",
            ),
        ),
        (T + "::test_baseline_derived_never_claims_stray_directory",),
    ),
    Mutation(
        "baseline 崩溃续跑证据",
        (
            (
                MOD,
                """        except MarketplaceIdentityError:
            # 清单不可读时的次选证据：记录自身的 baseline 锚定在该名字上（baseline_derived 崩溃窗口）。
            if record.get(MANIFEST_NAME_BASELINE_FIELD) == name:
                matches.append(name)
            continue""",
                """        except MarketplaceIdentityError:
            continue""",
            ),
        ),
        (T + "::test_baseline_derived_crash_window_resumes",),
    ),
    Mutation(
        "baseline 负向门（旧记录 baseline 必须匹配）",
        (
            (
                MOD,
                """            baseline_evidence = isinstance(record.get(MANIFEST_NAME_BASELINE_FIELD), str) and (
                record.get(MANIFEST_NAME_BASELINE_FIELD) == renamed[0]
            )""",
                """            baseline_evidence = True""",
            ),
        ),
        (T + "::test_baseline_evidence_requires_old_record_to_match",),
    ),
    Mutation(
        "externalTrees 单点计算",
        (
            (
                MOD,
                """            if item.merge:
                item.actions["catalog"] = "noop"
            item.actions["externalTrees"] = _external_tree_count(home, item)""",
                """            if item.merge:
                item.actions["catalog"] = "noop\"""",
            ),
        ),
        (T + "::test_external_tree_is_renamed", T + "::test_merge_keeps_target_record_metadata"),
    ),
    Mutation(
        "info 清单名路径段守卫",
        (
            (
                CLI,
                """    if not is_safe_registration_path_segment(name):
        return None, False
""",
                "",
            ),
        ),
        (T + "::test_info_does_not_read_outside_skill_home",),
    ),
    Mutation(
        "P4 无变化不落盘",
        ((MOD, "    if not has_match:\n        return\n", "    pass\n"),),
        (T + "::test_drop_old_ledger_does_not_create_missing_file",),
    ),
    Mutation(
        "跳过已物化账本记录",
        (
            (
                MOD,
                """            if already and ledger_entry_fully_materialized(already):
                item.ledger_rebuild.append((pid, scope, project_path))
                continue
""",
                "",
            ),
        ),
        (T + "::test_rebuild_skips_already_materialized_records",),
    ),
    Mutation(
        "human/JSON actions 单一权威",
        (
            (
                CLI,
                "    return item.visible_actions()",
                "    return item.actions",
            ),
            (
                MOD,
                '            "actions": self.visible_actions(),',
                '            "actions": dict(self.actions),',
            ),
        ),
        (T + "::test_human_table_suppresses_actions_for_non_executed_items", T + "::test_render_migration_rows_assertion"),
    ),
    Mutation(
        "plan.errors 脱敏",
        (
            (
                MOD,
                '                + ", ".join(sanitize_text_for_diagnostics(repr(name)) for name in unknown)',
                '                + ", ".join(repr(name) for name in unknown)',
            ),
            (
                MOD,
                '        "errors": [sanitize_text_for_diagnostics(message) for message in plan.errors],',
                '        "errors": list(plan.errors),',
            ),
        ),
        (T + "::test_unknown_name_errors_are_redacted",),
    ),
    Mutation(
        "不可哈希 type 守卫",
        (
            (
                MOD,
                '            if raw_type is None or (isinstance(raw_type, str) and raw_type in {"pickString", "command", "promptString"}):',
                '            if raw_type in {"pickString", "command", "promptString", None}:',
            ),
        ),
        (T + "::test_unhashable_input_type_does_not_crash",),
    ),
)


def main() -> int:
    failures: list[str] = []
    for mutation in MUTATIONS:
        backups: dict[str, tuple[str, str]] = {}
        try:
            for rel, old, new in mutation.edits:
                path = ROOT / rel
                if rel not in backups:
                    tmp = f"/tmp/mut268_{path.name}.bak"
                    shutil.copy2(path, tmp)
                    backups[rel] = (path.read_text(encoding="utf-8"), tmp)
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
            for rel, (original, tmp) in backups.items():
                shutil.copy2(tmp, ROOT / rel)
                assert (ROOT / rel).read_text(encoding="utf-8") == original, f"restore failed: {rel}"
    print(f"\nSUMMARY: {len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} KILLED")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
