#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
#268 / #301 marketplace 迁移的**可复现变异矩阵**（dev-only，不进 CI）/ Reproducible mutation matrix.

背景：PR #298 多轮审查要求「变异数字可复现」；#301 终审要求脚本**自证**：
- **基线绿跑**：全部目标用例先去重并集跑一次，rc 必须 0，否则 `BASELINE-FAIL` 直接退出；
- **严格 KILLED**：仅 `rc == 1`（有用例失败）**且**输出列出全部目标用例 → KILLED；`rc == 0` → SURVIVED；
  其余（收集错误 / 解释器缺失 / 超时）→ ERROR——`returncode != 0` 本身不足以证明变异被杀死；
- **语法校验**：每条 mutation 应用后先 `ast.parse`，避免「改坏了」被误读成「变异未被杀死」；
- **超时**：每条 mutation 的 pytest 有硬超时（`TIMEOUT` 秒），`TimeoutExpired` → ERROR；
- **还原自证**：备份按 PID 隔离，结束（含 SIGINT/SIGTERM）以 sha256 **逐字节**校验还原；还原失败
  **raise**（不变量不用 `assert`，`python -O` 会剥离）。
- **锚点演进校验**：每次替换前对**当前**文本再断言 `count == 1`（`--check` 只校验原始文件；若前一条
  edit 注入了后一条的锚点，只有这一步能发现）。

运行（不要用会重建环境的裸 ``uv run``；``uv run --no-sync`` 或项目 venv 的 python。
本脚本会临时改写源码，勿与编辑 / 并发 run 同时进行）::

    python scripts/mutation_marketplace_migration.py            # 基线 + 应用变异并跑定向用例
    python scripts/mutation_marketplace_migration.py --check    # 只读：校验各 old 片段仍唯一定位

退出码：基线绿且全部 KILLED → 0；任一 SURVIVED / SETUP-FAIL / ERROR / 基线红 → 1；
pytest / 解释器不可用 → 2。
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

MOD = "a2c_smcp/computer/settings/marketplace_migration.py"
SECRET = "a2c_smcp/computer/inputs/secret_store.py"
CLI = "a2c_smcp/computer/cli/commands/marketplace.py"

T = "tests/unit_tests/computer/settings/test_marketplace_migration.py"
S = "tests/unit_tests/computer/inputs/test_secret_store.py"
IT = "tests/integration_tests/computer/cli/test_marketplace_migrate.py"

TIMEOUT = 600  # 秒：单次 pytest 硬超时（挂起 = ERROR，不是 KILLED）


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
            (
                MOD,
                """        _rekey_values(item, env, write_new=True)
        _rekey_secrets(item, secrets, write_new=True)""",
                """        _rekey_values(item, env, write_new=True)
        _rekey_secrets(item, secrets, write_new=False)""",
            ),
            (
                MOD,
                """        _rekey_values(item, env, write_new=False)
        _rekey_secrets(item, secrets, write_new=False)""",
                """        _rekey_values(item, env, write_new=False)
        _rekey_secrets(item, secrets, write_new=True)""",
            ),
        ),
        (T + "::test_new_secret_is_written_before_old_is_deleted",),
    ),
    Mutation(
        "P4 只删重建成功的旧账本",
        (
            (
                MOD,
                """        if find_plugin_entry(item.manifest, plugin) is None:
            _note_kept_ledger_record(item, plugin)
            continue""",
                """        if find_plugin_entry(item.manifest, plugin) is None:
            item.ledger_rebuild.append((pid, "user", None))
            _note_kept_ledger_record(item, plugin)
            continue""",
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
        (
            (
                MOD,
                "        state.known = store.load_known_marketplaces_strict(home, env)",
                "        state.known = store.load_known_marketplaces(home, env)",
            ),
        ),
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
        (
            T + "::test_old_value_changed_before_p3_aborts",
            T + "::test_p3_null_old_value_missing_aborts",
        ),
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
        "item5 planned None 即 abort（无 source 正路误伤）",
        (
            (
                MOD,
                '    if not _source_unchanged(item.old_source, old_record.get("source")):',
                '    if item.old_source is None or not _source_unchanged(item.old_source, old_record.get("source")):',
            ),
        ),
        (T + "::test_missing_source_key_plans_and_applies",),
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
        # 只打「后端读失败」这一半：keyring 不可用由 `keyring_available()` 前置守卫钉住，
        # 不在本变异（替换 try/except 体）的可达面内（#301 item 3 严格判据暴露）。
        (S + "::test_get_strict_backend_error_raises",),
    ),
    Mutation(
        "scope 归一（忠实形态：非 str / 空串不归一为 user）",
        (
            (
                MOD,
                '    return value if isinstance(value, str) and value else "user"',
                '    return record.get("scope", "user")',
            ),
        ),
        (T + "::test_invalid_scope_is_normalized_to_user", T + "::test_empty_scope_is_normalized_to_user"),
    ),
    Mutation(
        "4b P0.5 projectPath 守卫删整段 if（预检不再 blocked）",
        (
            (
                MOD,
                """            if _record_needs_project_path(record):
                item.action = ACTION_BLOCKED
                item.reason = REASON_PROJECT_PATH_MISSING
                item.warnings.append(
                    f"plugin {parts[0]!r} has a {_norm_ledger_scope(record)} ledger record without a valid projectPath; "
                    "fix or remove that record before migrating"
                )
                return""",
                "            pass",
            ),
        ),
        (T + "::test_project_record_without_project_path_blocks_in_dry_run",),
    ),
    Mutation(
        "4d P0.5 不跳过「plugin 已出清单」组（回退宽判据）",
        (
            (
                MOD,
                """        if find_plugin_entry(item.manifest, parts[0]) is None:
            kept_plugins.append(parts[0])
            continue
""",
                "",
            ),
        ),
        (
            T + "::test_project_record_without_project_path_for_removed_plugin_keeps_ledger",
            T + "::test_project_record_without_project_path_for_baseline_derived_keeps_ledger",
        ),
    ),
    Mutation(
        "4e 谓词去掉 installPath 守卫（缺路径记录被误 blocked）",
        (
            (
                MOD,
                """    if _ledger_install_path(record) is None:
        return False
    return _norm_ledger_scope(record) in {"project", "local"} and not _norm_project_path(record)""",
                """    return _norm_ledger_scope(record) in {"project", "local"} and not _norm_project_path(record)""",
            ),
        ),
        # 调用方已前置 `_ledger_install_path(...) is None`，该守卫的承重目标是**谓词自足性**直调用例
        # （账本场景用例在变异下仍绿，故不入列表）。
        (T + "::test_record_needs_project_path_predicate_contract",),
    ),
    Mutation(
        "4f 删 _rebuild_ledger 的 projectPath raise（竞态守护失效）",
        (
            (
                MOD,
                """            # P0.5 已用同一判据前置（`_record_needs_project_path`）；这里只有**竞态**才可达
            # （预检之后账本被并发写入这样的记录）——保留为纵深防御（#301 item 9 裁决）。
            if _record_needs_project_path(record):
                raise _ItemFailure(
                    REASON_LEDGER_REBUILD_FAILED,
                    f"plugin {plugin!r} has a {scope} ledger record without a valid projectPath",
                )
""",
                "",
            ),
        ),
        (T + "::test_rebuild_ledger_race_project_path_raise",),
    ),
    Mutation(
        "4g P0.5 不预告静态 keep（缺 installPath / 记录非对象 / records 非数组）",
        (
            (
                MOD,
                """        if not isinstance(records, list):
            kept_notes.append(f"plugin {parts[0]!r} ledger records are not an array; keeping them untouched")
            continue
        if find_plugin_entry(item.manifest, parts[0]) is None:""",
                """        if not isinstance(records, list):
            continue
        if find_plugin_entry(item.manifest, parts[0]) is None:""",
            ),
            (
                MOD,
                """            if not isinstance(record, Mapping):
                kept_notes.append(None)
                continue
            if _ledger_install_path(record) is None:
                kept_notes.append(f"plugin {parts[0]!r} ledger record has no installPath; keeping it untouched")
                continue""",
                """            if not isinstance(record, Mapping):
                continue
            if _ledger_install_path(record) is None:
                continue""",
            ),
        ),
        (
            T + "::test_non_list_ledger_records_mark_incomplete",
            T + "::test_non_mapping_ledger_record_is_predicted_in_dry_run",
            T + "::test_project_record_without_project_path_and_install_path_keeps_ledger",
        ),
    ),
    Mutation(
        "4h carve-out 扩大：新清单内根缺失不再阻断（D14 失效）",
        (
            (
                MOD,
                """        if not root.is_dir():
            return REASON_PLUGIN_ROOT_UNREADABLE if plugin in ledger_plugins else None""",
                """        if not root.is_dir():
            return None""",
            ),
        ),
        (T + "::test_in_manifest_plugin_missing_root_still_blocks",),
    ),
    Mutation(
        "4i P0.5 不预告 source 非映射的 keep（dry-run 退化）",
        (
            (
                MOD,
                """    if (
        has_ledger_records
        and not isinstance(item.old_source, Mapping)
        and not isinstance(state.known.get("marketplaces", {}).get(item.new or ""), Mapping)
    ):
        _note_ledger_record_kept(
            item,
            "registration source is not an object; plugin ledger records cannot be re-materialized",
        )
""",
                "",
            ),
        ),
        (T + "::test_bare_string_source_ledger_keep_predicted_in_dry_run",),
    ),
    Mutation(
        "4j 枚举集回退 kebab 校验（strip 口径与 carve-out 分叉）",
        (
            (
                MOD,
                """        if isinstance(name, str) and name.strip():
            manifest_plugins.add(name.strip())""",
                """        if isinstance(name, str) and is_valid_marketplace_name(name):
            manifest_plugins.add(name)""",
            ),
        ),
        (T + "::test_whitespace_manifest_entry_is_enumerated_as_in_manifest",),
    ),
    Mutation(
        "4a rebuild 传 projectPath（project/local 记录保留 projectPath）",
        ((MOD, "                    project_path=project_path,\n", ""),),
        (T + "::test_project_record_rebuild_preserves_project_path",),
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
        # merge 用例的期望值恰为默认 0（不搬），删掉赋值后仍通过；只留 externalTrees == 1 的用例
        # （#301 item 3 严格判据暴露）。
        (T + "::test_external_tree_is_renamed",),
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

BASELINE_TESTS: tuple[str, ...] = tuple(sorted({test for mutation in MUTATIONS for test in mutation.tests}))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_ACTIVE_BACKUPS: dict[str, tuple[str, Path]] = {}


def _restore(rel: str, digest: str, tmp: Path) -> None:
    shutil.copy2(tmp, ROOT / rel)
    actual = _digest(ROOT / rel)
    if actual != digest:  # 不变量用 raise（assert 会被 python -O 剥离）
        raise RuntimeError(f"restore failed: {rel} ({actual} != {digest})")


def _restore_active() -> None:
    for rel, (digest, tmp) in list(_ACTIVE_BACKUPS.items()):
        _restore(rel, digest, tmp)
        _ACTIVE_BACKUPS.pop(rel, None)


def _sig_handler(signum: int, _frame: object) -> None:
    _restore_active()
    raise SystemExit(128 + signum)


_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")


def _failed_nodes(out: str) -> set[str]:
    """pytest short summary 中失败的 node id（`FAILED` / `ERROR` 行；参数化后缀原样保留）。"""
    nodes: set[str] = set()
    for line in out.splitlines():
        match = _FAILED_LINE.match(line)
        if match:
            nodes.add(match.group(1))
    return nodes


def _target_failed(node: str, failed: set[str]) -> bool:
    """目标 node id 是否真的失败（允许 `[param]` 后缀；**禁止裸子串匹配**——traceback 里的用例名不算）。"""
    return any(entry == node or entry.startswith(node + "[") for entry in failed)


def _classify(proc: subprocess.CompletedProcess[str], tests: tuple[str, ...]) -> tuple[str, str]:
    """KILLED 判据：**仅** pytest 返回 1（有用例失败）且 short summary 的 ``FAILED``/``ERROR`` 行
    确实列出全部目标用例。

    ``returncode != 0`` 不足以证明变异被杀死（解释器缺失 / 收集错误都会非 0）；裸子串匹配也不够
    （目标名可能只出现在 traceback / 别的用例输出里）。#309 🟡7 裁决：只认 short summary 的失败行。
    """
    out = proc.stdout + proc.stderr
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if proc.returncode == 0:
        return "SURVIVED", tail
    if proc.returncode == 1:
        failed = _failed_nodes(out)
        if all(_target_failed(test, failed) for test in tests):
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
    print(f"CHECK: {'OK' if ok else 'FAILED'} ({len(MUTATIONS)} mutations, {len(BASELINE_TESTS)} target tests)")
    return 0 if ok else 1


def _baseline() -> bool:
    """基线绿跑：全部目标用例先去重并集跑一次，rc 必须 0（否则变异结果无意义）。"""
    try:
        proc = subprocess.run(
            [PY, "-m", "pytest", *BASELINE_TESTS, "-q", "-p", "no:cacheprovider"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"BASELINE-FAIL: timeout (>{TIMEOUT}s)")
        return False
    if proc.returncode != 0:
        print(f"BASELINE-FAIL: rc={proc.returncode}")
        for line in (proc.stdout or "").strip().splitlines()[-20:]:
            print(f"  {line}")
        return False
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    print(f"BASELINE: OK ({len(BASELINE_TESTS)} target tests) | {tail}")
    return True


def main() -> int:
    # 每次都先做锚点唯一性校验：源码漂移时立即失败，避免半途 SETUP-FAIL / 误改无关片段。
    if check() != 0:
        return 1
    if "--check" in sys.argv[1:]:
        return 0
    try:
        import pytest  # noqa: F401
    except ImportError:
        print("pytest 不可用：请用项目 venv 运行（不要裸 python），否则变异会被误判。")
        return 2
    if not _baseline():
        return 1

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    failures: list[str] = []
    # 每次运行独立备份目录（per-PID）：并发 run / 并发 worktree 不互相覆盖；退出时整体清理。
    backup_dir = Path(tempfile.mkdtemp(prefix=f"mut301_{os.getpid()}_"))
    try:
        for mutation in MUTATIONS:
            backups: dict[str, tuple[str, Path]] = {}
            status = "SETUP-FAIL"
            try:
                for rel, old, new in mutation.edits:
                    path = ROOT / rel
                    if rel not in backups:
                        tmp = backup_dir / f"{len(backups)}_{path.name}.bak"
                        shutil.copy2(path, tmp)
                        backups[rel] = (_digest(path), tmp)
                    text = path.read_text(encoding="utf-8")
                    count = text.count(old)
                    if count != 1:  # 演进中的文本也须唯一（前一条 edit 可能注入后一条的锚点）
                        print(f"{mutation.name}: SETUP-FAIL ({rel}: occurrence={count})")
                        failures.append(mutation.name)
                        break
                    path.write_text(text.replace(old, new, 1), encoding="utf-8")
                else:
                    broken = [rel for rel in backups if not _syntax_ok(rel)]
                    if broken:
                        status, tail = f"ERROR(SYNTAX: {', '.join(broken)})", ""
                    else:
                        # 备份登记为「活跃」：子进程期间收到 SIGTERM/SIGINT 也能还原。
                        _ACTIVE_BACKUPS.update(backups)
                        try:
                            proc = subprocess.run(
                                [PY, "-m", "pytest", *mutation.tests, "-q", "-p", "no:cacheprovider"],
                                cwd=ROOT,
                                capture_output=True,
                                text=True,
                                timeout=TIMEOUT,
                            )
                        except subprocess.TimeoutExpired:
                            status, tail = f"ERROR(timeout>{TIMEOUT}s)", ""
                        else:
                            status, tail = _classify(proc, mutation.tests)
                    print(f"{mutation.name}: {status} | {tail}")
                    if status != "KILLED":
                        failures.append(mutation.name)
            finally:
                for rel, (digest, tmp) in backups.items():
                    _restore(rel, digest, tmp)
                    _ACTIVE_BACKUPS.pop(rel, None)
    finally:
        _restore_active()
        shutil.rmtree(backup_dir, ignore_errors=True)
    print(f"\nSUMMARY: {len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} KILLED")
    return 0 if not failures else 1


def _syntax_ok(rel: str) -> bool:
    try:
        ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    except SyntaxError as exc:
        print(f"  SYNTAX-ERROR {rel}: {exc}")
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
