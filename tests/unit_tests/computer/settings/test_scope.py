# -*- coding: utf-8 -*-
# filename: test_scope.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
五级 scope 路径解析 + 读/写 merge + active-workdir / 能力层解析单元测试（v0.2.1）
Unit tests for scope path resolution + read/write merge + active-workdir / capability resolution.

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §5.0 / §5.1 / §5.4。

测试意图 / Test intentions:
- 读 merge 矩阵：标量高覆盖 / 数组拼接去重（低在前）/ 对象深合并；
  **嵌套 extraKnownMarketplaces[].source 深合并不被整体替换**（关键用例，§5.4 例 2）
- 写 merge：数组整体替换（不拼接）/ DELETE 删 key / 嵌套对象不毁兄弟键
- 路径解析：user config dir XDG-first + 回退；workdir project/local 路径
- active-workdir 解析：无 active → project/local 空；非 active 目录不贡献标量；
  能力层（enabledPlugins/extraKnownMarketplaces）跨全部登记目录全局并集
"""

import json
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.scope import (
    DELETE,
    apply_write,
    merge_layers,
    merge_read,
    resolve_settings,
    resolve_user_config_dir,
    user_settings_path,
    workdir_local_settings_path,
    workdir_project_settings_path,
)


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


# ---------------------------------------------------------------------------
# 读 merge 矩阵 / read-merge matrix
# ---------------------------------------------------------------------------
def test_read_scalar_high_wins() -> None:
    assert merge_read({"strictKnownMarketplaces": False}, {"strictKnownMarketplaces": True}) == {
        "strictKnownMarketplaces": True
    }


def test_read_array_concat_dedup_low_first() -> None:
    low = {"trustedMarketplaces": ["a", "b"]}
    high = {"trustedMarketplaces": ["b", "c"]}
    assert merge_read(low, high) == {"trustedMarketplaces": ["a", "b", "c"]}


def test_read_object_leaf_override_keeps_siblings() -> None:
    # 例 1：local false 覆盖 project true；project 独有 bar 保留。
    low = {"enabledPlugins": {"foo@mp": True, "bar@mp": True}}
    high = {"enabledPlugins": {"foo@mp": False}}
    assert merge_read(low, high) == {"enabledPlugins": {"foo@mp": False, "bar@mp": True}}


def test_read_nested_object_deep_merge_source_preserved() -> None:
    # 例 2（关键）：高 scope 只给 autoUpdate，嵌套 source 不被整体替换。
    low = {"extraKnownMarketplaces": {"mp": {"source": {"type": "git", "url": "u"}, "autoUpdate": False}}}
    high = {"extraKnownMarketplaces": {"mp": {"autoUpdate": True}}}
    merged = merge_read(low, high)
    assert merged == {"extraKnownMarketplaces": {"mp": {"source": {"type": "git", "url": "u"}, "autoUpdate": True}}}


def test_read_key_only_in_low_preserved() -> None:
    assert merge_read({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_merge_layers_fold_low_to_high() -> None:
    layers = [
        {"strictKnownMarketplaces": False, "trustedMarketplaces": ["a"]},  # capability/low
        {"strictKnownMarketplaces": True, "trustedMarketplaces": ["b"]},  # user
        {"trustedMarketplaces": ["c"]},  # high
    ]
    merged = merge_layers(layers)
    assert merged["strictKnownMarketplaces"] is True
    assert merged["trustedMarketplaces"] == ["a", "b", "c"]


def test_merge_read_conflict_hook_fires_on_scalar_only() -> None:
    seen: list[tuple[str, object, object]] = []
    merge_read(
        {"x": 1, "arr": ["a"], "obj": {"k": 1}},
        {"x": 2, "arr": ["b"], "obj": {"k": 2}},
        on_conflict=lambda p, lo, hi: seen.append((p, lo, hi)),
    )
    # 标量 x 与嵌套叶子 obj.k 冲突回调；数组拼接不回调。
    paths = {s[0] for s in seen}
    assert paths == {"x", "obj.k"}


# ---------------------------------------------------------------------------
# 写 merge / write-merge
# ---------------------------------------------------------------------------
def test_write_array_replaced_not_concatenated() -> None:
    existing = {"trustedMarketplaces": ["a", "b"]}
    assert apply_write(existing, {"trustedMarketplaces": ["c"]}) == {"trustedMarketplaces": ["c"]}


def test_write_delete_sentinel_removes_key() -> None:
    existing = {"strictKnownMarketplaces": True, "trustedMarketplaces": ["a"]}
    assert apply_write(existing, {"strictKnownMarketplaces": DELETE}) == {"trustedMarketplaces": ["a"]}


def test_write_delete_missing_key_is_noop() -> None:
    assert apply_write({"a": 1}, {"missing": DELETE}) == {"a": 1}


def test_write_nested_object_keeps_siblings() -> None:
    existing = {"enabledPlugins": {"foo@mp": True, "bar@mp": True}}
    # 写 foo@mp=False，只动该叶子，bar 保留。
    assert apply_write(existing, {"enabledPlugins": {"foo@mp": False}}) == {
        "enabledPlugins": {"foo@mp": False, "bar@mp": True}
    }


def test_write_does_not_mutate_inputs() -> None:
    existing = {"trustedMarketplaces": ["a"]}
    apply_write(existing, {"trustedMarketplaces": ["c"]})
    assert existing == {"trustedMarketplaces": ["a"]}  # 原对象不变


# ---------------------------------------------------------------------------
# 路径解析 / path resolution
# ---------------------------------------------------------------------------
def test_user_config_dir_xdg_first(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg-config"
    assert resolve_user_config_dir({"XDG_CONFIG_HOME": str(xdg)}) == (xdg / "a2c").resolve()


def test_user_config_dir_fallback_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # 相对 XDG_CONFIG_HOME 按规范忽略 → 回退 ~/.config/a2c
    assert resolve_user_config_dir({"XDG_CONFIG_HOME": "relative"}) == (tmp_path / ".config" / "a2c").resolve()


def test_user_settings_path(tmp_path: Path) -> None:
    xdg = tmp_path / "c"
    assert user_settings_path({"XDG_CONFIG_HOME": str(xdg)}) == (xdg / "a2c" / "settings.json").resolve()


def test_workdir_paths(tmp_path: Path) -> None:
    assert workdir_project_settings_path(tmp_path) == tmp_path / ".tfrobot" / "settings.json"
    assert workdir_local_settings_path(tmp_path) == tmp_path / ".tfrobot" / "settings.local.json"


# ---------------------------------------------------------------------------
# cwd 锚定解析（#116）/ cwd-anchored resolution
# ---------------------------------------------------------------------------
def _empty_user_env(tmp_path: Path) -> dict:
    # 指向无 settings.json 的隔离 config dir，确保 user 层为空。
    return {"XDG_CONFIG_HOME": str(tmp_path / "empty-config")}


def test_policy_highest_priority_and_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_json(workdir_project_settings_path(tmp_path), {"strictKnownMarketplaces": False})
    monkeypatch.chdir(tmp_path)
    resolved = resolve_settings(
        env=_empty_user_env(tmp_path),
        policy_settings={"strictKnownMarketplaces": True, "allowedMcpServers": ["srv"]},
    )
    # policy 最高优先级覆盖 project。
    assert resolved.settings["strictKnownMarketplaces"] is True
    # policy-only 字段在 policy scope 合法保留。
    assert resolved.settings["allowedMcpServers"] == ["srv"]


def test_corrupt_settings_file_yields_error_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = workdir_project_settings_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    resolved = resolve_settings(env=_empty_user_env(tmp_path))
    assert resolved.settings == {}  # 损坏文件回退空、不崩
    assert any(e.field == "<file>" for e in resolved.errors)


def test_invalid_scalar_field_reported_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 单层单文件的字段级错误只报一次（_dedup_errors 防御性保留）。
    _write_json(workdir_project_settings_path(tmp_path), {"strictKnownMarketplaces": "bad"})
    monkeypatch.chdir(tmp_path)
    resolved = resolve_settings(env=_empty_user_env(tmp_path))
    scalar_errors = [e for e in resolved.errors if e.field == "strictKnownMarketplaces"]
    assert len(scalar_errors) == 1


def test_flag_layer_overrides_user_and_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # flag 层（--settings <file>）优先级高于 project，仅低于 policy。project 锚 cwd（#116）。
    _write_json(workdir_project_settings_path(tmp_path), {"strictKnownMarketplaces": False, "trustedMarketplaces": ["proj"]})
    flag_file = tmp_path / "flag-settings.json"
    flag_file.write_text(json.dumps({"strictKnownMarketplaces": True, "trustedMarketplaces": ["flag"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    resolved = resolve_settings(
        env=_empty_user_env(tmp_path),
        flag_settings_path=flag_file,
    )
    assert resolved.settings["strictKnownMarketplaces"] is True  # flag 标量覆盖 project
    # 数组拼接去重（低 project 在前 + 高 flag 在后）。
    assert resolved.settings["trustedMarketplaces"] == ["proj", "flag"]


# ---------------------------------------------------------------------------
# #116 概念瘦身：project/local 锚定进程 cwd / #116: project/local anchored at process cwd
# ---------------------------------------------------------------------------
def test_resolve_settings_anchors_project_local_at_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#116: project/local 层无条件锚定 cwd，`resolve_settings` 不再有 workdir 形参。"""
    _write_json(workdir_project_settings_path(tmp_path), {"strictKnownMarketplaces": True, "enabledPlugins": {"p@mp": True}})
    _write_json(workdir_local_settings_path(tmp_path), {"strictKnownMarketplaces": False, "trustedMarketplaces": ["m"]})
    monkeypatch.chdir(tmp_path)

    resolved = resolve_settings(env=_empty_user_env(tmp_path))
    assert resolved.settings["strictKnownMarketplaces"] is False  # local 覆盖 project
    assert resolved.settings["trustedMarketplaces"] == ["m"]
    assert resolved.settings["enabledPlugins"] == {"p@mp": True}  # 能力字段随 project 层进（无需独立能力层）


def test_extra_known_marketplaces_deep_merge_at_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#116: extraKnownMarketplaces（dict）在 cwd project/local 层间深合并（键并集，同名叶子高层胜）。"""
    _write_json(
        workdir_project_settings_path(tmp_path),
        {"extraKnownMarketplaces": {"mp1": {"source": {"type": "git", "url": "git@h:a.git"}}}},
    )
    _write_json(
        workdir_local_settings_path(tmp_path),
        {"extraKnownMarketplaces": {"mp2": {"source": {"type": "git", "url": "git@h:b.git"}}}},
    )
    monkeypatch.chdir(tmp_path)

    resolved = resolve_settings(env=_empty_user_env(tmp_path))
    assert set(resolved.settings["extraKnownMarketplaces"]) == {"mp1", "mp2"}  # 深合并键并集
    assert resolved.settings["extraKnownMarketplaces"]["mp1"]["source"]["url"] == "git@h:a.git"
