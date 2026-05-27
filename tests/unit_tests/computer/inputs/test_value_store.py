# -*- coding: utf-8 -*-
# filename: test_value_store.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``ValueStore`` 单元测试（v0.2.1 #65）/ Non-secret value store unit tests（§9.3）。

测试意图 / Test intentions:
- 路径解析：``XDG_STATE_HOME`` 绝对 → XDG-first；未设/相对 → ``~/.local/state/a2c`` 回退；
- get/set/delete/load 往返；畸形 JSON / 非 dict 顶层 → ``{}``；落盘 ``0600``。
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from a2c_smcp.computer.inputs.value_store import VALUE_STORE_FILENAME, ValueStore, resolve_value_store_path


def test_path_xdg_first(tmp_path: Path) -> None:
    p = resolve_value_store_path({"XDG_STATE_HOME": str(tmp_path)})
    assert p == (tmp_path / "a2c" / VALUE_STORE_FILENAME).resolve()


def test_path_fallback_when_unset_or_relative(tmp_path: Path) -> None:
    fallback = (Path.home() / ".local" / "state" / "a2c" / VALUE_STORE_FILENAME).expanduser().resolve()
    assert resolve_value_store_path({}) == fallback
    # 相对值按 XDG 规范视为未设置 → 回退 / relative value treated as unset per XDG spec
    assert resolve_value_store_path({"XDG_STATE_HOME": "relative/dir"}) == fallback


def test_set_get_delete_roundtrip(tmp_path: Path) -> None:
    store = ValueStore({"XDG_STATE_HOME": str(tmp_path)})
    assert store.get("k") is None
    store.set("k", "v")
    assert store.get("k") == "v"
    store.set("k2", "v2")
    assert store.load() == {"k": "v", "k2": "v2"}
    assert store.delete("k") is True
    assert store.get("k") is None
    assert store.delete("missing") is False


def test_malformed_json_treated_as_empty(tmp_path: Path) -> None:
    store = ValueStore({"XDG_STATE_HOME": str(tmp_path)})
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.load() == {}
    # 非 dict 顶层（如 list）也按空处理 / non-dict top-level → empty
    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == {}


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX 权限位 / POSIX mode bits")
def test_written_file_is_0600(tmp_path: Path) -> None:
    store = ValueStore({"XDG_STATE_HOME": str(tmp_path)})
    store.set("k", "v")
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600
