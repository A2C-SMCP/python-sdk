# -*- coding: utf-8 -*-
# filename: test_store.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
物化层 store 单元测试（v0.2.1）
Unit tests for the materialized-file store (v0.2.1).

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §6.1 / §6.2 / §6.3。

测试意图 / Test intentions:
- 原子写：中途失败（os.replace 抛错）不留半截 JSON、目标文件保持原值、临时文件被清理
- 写保护头：保存文件首行为 JSONC ``// Maintained...``；读时容错剥离前导 ``//`` 再解析
- 文件锁：拿不到锁退避重试后抛 SettingsLockError（fail-fast，不无锁写）
- 损坏恢复：corrupt / 非对象根 → 先备份 ``.corrupt-<ts>.bak`` 再降级空配置；IO 错误不备份
- 手编痕迹：version 不符 / 未知顶层键 → WARN + in-memory（丢未知键、不阻断）
- 物化文件强制带 version；缺失文件 → 空配置带 version
- 路径解析：home 显式注入 / ``A2C_SKILL_HOME`` env 覆盖
- round-trip：save → load 内容一致（含 installed_plugins 多字段记录）
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

import a2c_smcp.computer.settings.store as store_mod
from a2c_smcp.computer.settings.store import (
    INSTALLED_PLUGINS_FILENAME,
    KNOWN_MARKETPLACES_FILENAME,
    MATERIALIZED_VERSION,
    WRITE_PROTECTION_HEADER,
    SettingsLockError,
    atomic_write_json,
    atomic_write_text,
    empty_installed_plugins,
    empty_known_marketplaces,
    file_lock,
    installed_plugins_path,
    known_marketplaces_path,
    load_installed_plugins,
    load_known_marketplaces,
    read_jsonc_with_recovery,
    save_installed_plugins,
    save_known_marketplaces,
    update_installed_plugins,
    update_known_marketplaces,
)


# ---------------------------------------------------------------------------
# 原子写 / atomic write
# ---------------------------------------------------------------------------
def test_atomic_write_text_creates_parent_and_writes(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "f.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_json_header_and_body(tmp_path: Path) -> None:
    target = tmp_path / "f.json"
    atomic_write_json(target, {"a": 1, "中文": "值"}, header=WRITE_PROTECTION_HEADER)
    text = target.read_text(encoding="utf-8")
    assert text.splitlines()[0] == WRITE_PROTECTION_HEADER
    # ensure_ascii=False 保留非 ASCII。
    assert "中文" in text
    # 去掉首行注释后是合法 JSON。
    body = "\n".join(text.splitlines()[1:])
    assert json.loads(body) == {"a": 1, "中文": "值"}


def test_atomic_write_json_without_header(tmp_path: Path) -> None:
    # header=None 分支：纯 JSON、无前导注释行。
    target = tmp_path / "f.json"
    atomic_write_json(target, {"a": 1})
    text = target.read_text(encoding="utf-8")
    assert not text.lstrip().startswith("//")
    assert json.loads(text) == {"a": 1}


# ---------------------------------------------------------------------------
# 文件锁 / file lock
# ---------------------------------------------------------------------------
def test_file_lock_acquire_release_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "f.json"
    with file_lock(target):
        atomic_write_text(target, "{}")
    # 释放后可再次获取。
    with file_lock(target):
        pass
    assert target.read_text(encoding="utf-8") == "{}"


@pytest.mark.skipif(sys.platform == "win32", reason="msvcrt lock semantics differ; POSIX flock 在此验证")
def test_file_lock_contention_raises(tmp_path: Path) -> None:
    target = tmp_path / "f.json"
    # 同进程不同 fd 的 flock 互相独立——外层持锁时内层（新 fd）拿不到 → 退避后抛 SettingsLockError。
    with file_lock(target):
        with pytest.raises(SettingsLockError, match="could not acquire lock"):
            with file_lock(target, retries=1, backoff_base=0.001, backoff_max=0.001):
                pass


# ---------------------------------------------------------------------------
# JSONC 读取 + 损坏恢复 / JSONC read + corruption recovery
# ---------------------------------------------------------------------------
def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_jsonc_with_recovery(tmp_path / "nope.json") is None


def test_read_strips_leading_jsonc_header(tmp_path: Path) -> None:
    p = tmp_path / "f.json"
    p.write_text(f"{WRITE_PROTECTION_HEADER}\n\n{{\"version\": 1}}\n", encoding="utf-8")
    assert read_jsonc_with_recovery(p) == {"version": 1}


def test_read_corrupt_backs_up_then_degrades(tmp_path: Path) -> None:
    p = tmp_path / KNOWN_MARKETPLACES_FILENAME
    p.write_text("{not valid json", encoding="utf-8")
    assert read_jsonc_with_recovery(p) is None
    # 原文件被移走，备份 .corrupt-<ts>.bak 留存（不静默永久丢数据）。
    assert not p.exists()
    backups = list(tmp_path.glob(f"{KNOWN_MARKETPLACES_FILENAME}.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"


def test_read_non_object_root_backs_up(tmp_path: Path) -> None:
    p = tmp_path / "f.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_jsonc_with_recovery(p) is None
    assert len(list(tmp_path.glob("f.json.corrupt-*.bak"))) == 1


def test_read_io_error_no_backup(tmp_path: Path) -> None:
    # 路径是目录 → exists() 真但 read_text 抛 OSError（IsADirectoryError）→ 返回 None、不备份。
    p = tmp_path / "adir.json"
    p.mkdir()
    assert read_jsonc_with_recovery(p) is None
    assert list(tmp_path.glob("*.corrupt-*.bak")) == []
    assert p.is_dir()  # 未被动


def test_backup_corrupt_replace_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # _backup_corrupt 的 os.replace 失败分支：备份失败仍返回 None（不阻断降级），无 .bak、损坏文件原地保留。
    p = tmp_path / KNOWN_MARKETPLACES_FILENAME
    p.write_text("{broken", encoding="utf-8")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("cannot rename to backup")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    store_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR)
    try:
        assert read_jsonc_with_recovery(p) is None
    finally:
        store_mod.logger.removeHandler(caplog.handler)
    assert list(tmp_path.glob("*.corrupt-*.bak")) == []  # 备份失败、无 .bak
    assert p.read_text(encoding="utf-8") == "{broken"  # 损坏文件原地保留（未被移走）
    assert any("Failed to back up corrupt file" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 手编痕迹 WARN / hand-edit warnings
# ---------------------------------------------------------------------------
def _attach_caplog(caplog: pytest.LogCaptureFixture) -> None:
    # 项目 logger 关闭 propagate，直接挂 caplog handler（同 test_scope.py / test_registry.py 范式）。
    store_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING)


def test_version_mismatch_warns_and_keeps_inmemory(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = known_marketplaces_path(home=tmp_path)
    p.write_text(json.dumps({"version": 99, "marketplaces": {"mp": {}}}), encoding="utf-8")
    _attach_caplog(caplog)
    try:
        loaded = load_known_marketplaces(home=tmp_path)
    finally:
        store_mod.logger.removeHandler(caplog.handler)
    assert loaded["version"] == MATERIALIZED_VERSION  # 规整回当前版本
    assert loaded["marketplaces"] == {"mp": {}}  # 已知字段保留
    assert any("unexpected version" in r.getMessage() for r in caplog.records)


def test_unknown_top_level_key_warns_and_dropped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = known_marketplaces_path(home=tmp_path)
    p.write_text(json.dumps({"version": 1, "marketplaces": {}, "handEditedJunk": 1}), encoding="utf-8")
    _attach_caplog(caplog)
    try:
        loaded = load_known_marketplaces(home=tmp_path)
    finally:
        store_mod.logger.removeHandler(caplog.handler)
    assert "handEditedJunk" not in loaded  # 未知顶层键被丢
    assert any("unexpected top-level keys" in r.getMessage() for r in caplog.records)


def test_container_not_object_treated_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = installed_plugins_path(home=tmp_path)
    p.write_text(json.dumps({"version": 1, "plugins": ["not", "a", "dict"]}), encoding="utf-8")
    _attach_caplog(caplog)
    try:
        loaded = load_installed_plugins(home=tmp_path)
    finally:
        store_mod.logger.removeHandler(caplog.handler)
    assert loaded["plugins"] == {}
    assert any("not an object" in r.getMessage() for r in caplog.records)


def test_coerce_known_marketplaces_non_dict(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # marketplaces 非 dict（与 plugins 侧对称）→ 视作空 + WARN。
    p = known_marketplaces_path(home=tmp_path)
    p.write_text(json.dumps({"version": 1, "marketplaces": ["not", "a", "dict"]}), encoding="utf-8")
    _attach_caplog(caplog)
    try:
        loaded = load_known_marketplaces(home=tmp_path)
    finally:
        store_mod.logger.removeHandler(caplog.handler)
    assert loaded["marketplaces"] == {}
    assert any("not an object" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# version 强制 + 空配置 / forced version & empty config
# ---------------------------------------------------------------------------
def test_empty_helpers_carry_version() -> None:
    assert empty_known_marketplaces() == {"version": MATERIALIZED_VERSION, "marketplaces": {}}
    assert empty_installed_plugins() == {"version": MATERIALIZED_VERSION, "plugins": {}}


def test_missing_file_loads_empty_with_version(tmp_path: Path) -> None:
    assert load_known_marketplaces(home=tmp_path) == empty_known_marketplaces()
    assert load_installed_plugins(home=tmp_path) == empty_installed_plugins()


def test_save_backfills_version_and_header(tmp_path: Path) -> None:
    # 不带 version 保存 → 落盘补齐 version + 写保护头。
    path = save_known_marketplaces({"marketplaces": {}}, home=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == WRITE_PROTECTION_HEADER
    body = json.loads("\n".join(text.splitlines()[1:]))
    assert body["version"] == MATERIALIZED_VERSION


# ---------------------------------------------------------------------------
# 路径解析 / path resolution
# ---------------------------------------------------------------------------
def test_paths_with_explicit_home(tmp_path: Path) -> None:
    assert known_marketplaces_path(home=tmp_path) == tmp_path / KNOWN_MARKETPLACES_FILENAME
    assert installed_plugins_path(home=tmp_path) == tmp_path / INSTALLED_PLUGINS_FILENAME


def test_paths_with_env_skill_home(tmp_path: Path) -> None:
    home = tmp_path / "skillhome"
    env = {"A2C_SKILL_HOME": str(home)}
    assert known_marketplaces_path(env=env) == home.resolve() / KNOWN_MARKETPLACES_FILENAME
    assert installed_plugins_path(env=env) == home.resolve() / INSTALLED_PLUGINS_FILENAME


# ---------------------------------------------------------------------------
# round-trip / save → load 一致
# ---------------------------------------------------------------------------
def test_known_marketplaces_roundtrip(tmp_path: Path) -> None:
    data = {
        "version": MATERIALIZED_VERSION,
        "marketplaces": {
            "my-team-skills": {
                "source": {"type": "git", "url": "git@github.com:team/skills.git"},
                "installLocation": "/abs/path/my-team-skills",
                "lastUpdated": "2026-05-21T10:30:00Z",
                "commitSha": "abc1234",
                "autoUpdate": True,
            }
        },
    }
    save_known_marketplaces(data, home=tmp_path)
    assert load_known_marketplaces(home=tmp_path) == data


def test_installed_plugins_roundtrip(tmp_path: Path) -> None:
    data = {
        "version": MATERIALIZED_VERSION,
        "plugins": {
            "frontend-design@my-team-skills": [
                {
                    "scope": "user",
                    "installPath": "/abs/path/frontend-design",
                    "version": "1.2.0",
                    "commitSha": "abc1234",
                    "installedAt": "2026-05-10T00:00:00Z",
                    "lastUpdated": "2026-05-10T00:00:00Z",
                    "mcpServers": ["figma-mcp"],
                }
            ]
        },
    }
    save_installed_plugins(data, home=tmp_path)
    assert load_installed_plugins(home=tmp_path) == data


def test_corrupt_then_load_recovers_empty_and_backs_up(tmp_path: Path) -> None:
    # 损坏文件 → load 降级空配置 + 留 .bak；随后 save 重建全新文件可正常 round-trip。
    p = known_marketplaces_path(home=tmp_path)
    p.write_text("GARBAGE@@@", encoding="utf-8")
    assert load_known_marketplaces(home=tmp_path) == empty_known_marketplaces()
    assert len(list(tmp_path.glob(f"{KNOWN_MARKETPLACES_FILENAME}.corrupt-*.bak"))) == 1
    save_known_marketplaces(empty_known_marketplaces(), home=tmp_path)
    assert load_known_marketplaces(home=tmp_path) == empty_known_marketplaces()


# ---------------------------------------------------------------------------
# 持锁 RMW / locked read-modify-write（update_*）
# ---------------------------------------------------------------------------
def test_update_known_marketplaces_inplace_mutator(tmp_path: Path) -> None:
    # mutator 就地改并返回 None → 落盘 + 返回最终结构（单把锁内 load→mutate→save）。
    def add_mp(data: dict) -> None:
        data["marketplaces"]["mp-a"] = {"source": {"type": "git", "url": "git@h:p"}, "installLocation": "/x"}
        return None

    result = update_known_marketplaces(add_mp, home=tmp_path)
    assert "mp-a" in result["marketplaces"]
    assert result["version"] == MATERIALIZED_VERSION
    assert load_known_marketplaces(home=tmp_path) == result  # 已落盘


def test_update_installed_plugins_returns_new_structure(tmp_path: Path) -> None:
    # mutator 返回全新结构（缺 version）→ 采用之并回填 version。
    def replace(_data: dict) -> dict:
        return {"plugins": {"p@mp": [{"scope": "user", "installPath": "/i"}]}}

    result = update_installed_plugins(replace, home=tmp_path)
    assert result["version"] == MATERIALIZED_VERSION
    assert result["plugins"]["p@mp"][0]["scope"] == "user"
    assert load_installed_plugins(home=tmp_path) == result


def test_update_is_atomic_rmw_over_existing(tmp_path: Path) -> None:
    # 已有内容时 RMW 读到当前值再改，不丢已有条目（reconciler #62 的核心语义）。
    save_known_marketplaces(
        {"marketplaces": {"old": {"source": {"type": "git", "url": "git@h:p"}, "installLocation": "/o"}}},
        home=tmp_path,
    )

    def add_new(data: dict) -> None:
        data["marketplaces"]["new"] = {"source": {"type": "git", "url": "git@h:q"}, "installLocation": "/n"}
        return None

    result = update_known_marketplaces(add_new, home=tmp_path)
    assert set(result["marketplaces"]) == {"old", "new"}
