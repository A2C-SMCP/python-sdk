# -*- coding: utf-8 -*-
# filename: test_plugin_inputs_prefix.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
plugin inputs 入池消歧单元测试（v0.2.1 #65，§9.3 D2）/ Plugin inputs prefix-disambiguation unit tests。

测试意图 / Test intentions:
- ``prefix_input_id`` 形态 ``<plugin>@<marketplace>/<id>``；
- ``load_plugin_inputs`` 把 inputs.json 逐项校验为 MCPServerInput 并前缀化 id；兼容 ``{"inputs":[...]}`` 与裸 ``[...]``；
- 畸形条目跳过（不抛）；文件缺失 → ``[]``；两 plugin 同裸 id 前缀后不撞。
"""

from __future__ import annotations

import json
from pathlib import Path

from a2c_smcp.computer.inputs.plugin_pool import load_plugin_inputs, prefix_input_id


def test_prefix_input_id() -> None:
    assert prefix_input_id("frontend", "my-team", "figma_token") == "frontend@my-team/figma_token"


def _write(p: Path, obj: object) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_load_wraps_and_prefixes(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "inputs.json",
        {"inputs": [{"id": "api_token", "type": "promptString", "description": "token", "password": True}]},
    )
    out = load_plugin_inputs(f, "frontend", "my-team")
    assert len(out) == 1
    assert out[0].id == "frontend@my-team/api_token"
    assert out[0].password is True  # 其余字段保留 / other fields preserved


def test_load_bare_list(tmp_path: Path) -> None:
    f = _write(tmp_path / "inputs.json", [{"id": "x", "type": "promptString", "description": "d"}])
    out = load_plugin_inputs(f, "p", "mp")
    assert [i.id for i in out] == ["p@mp/x"]


def test_load_skips_malformed_entries(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "inputs.json",
        {"inputs": [{"id": "ok", "type": "promptString", "description": "d"}, {"bogus": True}]},
    )
    out = load_plugin_inputs(f, "p", "mp")
    assert [i.id for i in out] == ["p@mp/ok"]  # 畸形条目被跳过 / malformed entry skipped


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_plugin_inputs(tmp_path / "nope.json", "p", "mp") == []


def test_load_malformed_json_returns_empty(tmp_path: Path) -> None:
    f = tmp_path / "inputs.json"
    f.write_text("{ broken", encoding="utf-8")
    assert load_plugin_inputs(f, "p", "mp") == []


def test_two_plugins_same_id_no_collision(tmp_path: Path) -> None:
    a = load_plugin_inputs(_write(tmp_path / "a.json", [{"id": "token", "type": "promptString", "description": "d"}]), "a", "mp")
    b = load_plugin_inputs(_write(tmp_path / "b.json", [{"id": "token", "type": "promptString", "description": "d"}]), "b", "mp")
    assert a[0].id == "a@mp/token"
    assert b[0].id == "b@mp/token"
    assert a[0].id != b[0].id
