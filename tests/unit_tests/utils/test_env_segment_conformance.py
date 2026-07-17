# -*- coding: utf-8 -*-
# filename: test_env_segment_conformance.py
# @Time    : 2026/07/17
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
input env 命名（ENV_SEGMENT）**跨 SDK 一致性对拍**（P0 硬门槛）/ Cross-SDK conformance for input env var naming.

夹具来源 / Fixture source（唯一权威）：
    a2c-smcp-protocol ``docs/specification/fixtures/env_segment_conformance_vectors.json``
    —— 规范正文见 ``docs/guides/computer-mcp-config-guide.md`` §「环境变量命名规则（双端统一规范）」
    @ develop ``9cde57c``（PROTO-5 / Discussion #23 F4-F5）。
    本文件旁的 ``fixtures/env_segment_conformance_vectors.json`` 为该规范文件的**逐字节 vendored 副本**——
    协议仓更新向量时须重新同步（``git show origin/develop:docs/specification/fixtures/... > fixtures/...``）。

含义 / Meaning：任一 SDK 的 ``env_var_name(input_id, bundle_id, tool_name)`` **MUST** 对每条向量产出
``expected_env_var_name`` 方为合规。python 与 rust 逐字节一致是 F4 的硬门槛（rust 镜像 rust-sdk#140）。

**server/tool 段的现状（#155 决策 1）**：双端 live 解析路径**均只传裸 id**（rust 的 ``InputContext``
调用点全在 ``#[cfg(test)]``，本轮明文保持预防性）。故带 bundle_id / tool_name 的向量覆盖的是
``env_var_name`` 的**函数能力**与双端形态一致性，非当前生产调用形态——这正是「接线时不会双端分叉」的锚。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.utils.env_segment import env_var_name

_FIXTURE = Path(__file__).parent / "fixtures" / "env_segment_conformance_vectors.json"


def _load() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


_VECTORS: list[dict[str, Any]] = _load()["vectors"]


def test_fixture_integrity() -> None:
    """守卫 vendored 副本未被截断/损坏（16 条向量 + algorithm 段的规范性声明）。"""
    data = _load()
    assert "algorithm" in data
    algo = data["algorithm"]
    # F4 三条不可协商的规范点须在 algorithm 段明文声明（副本被裁剪时此处先红）
    assert "A2C_SMCP_" in algo.get("prefix", "")
    assert "no_folding" in algo
    assert "collision" in algo
    assert len(data["vectors"]) == 16
    for v in _VECTORS:
        assert {"desc", "id", "input_id", "bundle_id", "tool_name", "expected_env_var_name"} <= v.keys()


@pytest.mark.parametrize("vec", _VECTORS, ids=[v["id"] for v in _VECTORS])
def test_env_var_name_conformance(vec: dict[str, Any]) -> None:
    """逐字节对拍：env_var_name(input_id, bundle_id, tool_name) == expected（与 rust-sdk 一致）。"""
    got = env_var_name(vec["input_id"], bundle_id=vec["bundle_id"], tool_name=vec["tool_name"])
    assert got == vec["expected_env_var_name"], f"{vec['desc']}: got={got} expected={vec['expected_env_var_name']}"


def test_case_preserved_pair_does_not_collapse() -> None:
    """对照对显式断言：MyServer / myserver 两个合法共存 bundle_id **不**坍缩（#155 验收 ③）。

    向量表里两者是独立条目，逐条断言无法证明「彼此不同」——此处正面对拍二者取值分叉。
    """
    upper = env_var_name("token", bundle_id="MyServer")
    lower = env_var_name("token", bundle_id="myserver")
    assert upper != lower, f"MyServer/myserver 坍缩到同一 env 名 {upper}——保留大小写失效"


def test_known_collision_pair_maps_to_same_name() -> None:
    """对照对显式断言：'-' 与 '_' 确实坍缩（ENV_SEGMENT 非单射）⇒ fail-fast 存在的理由（#155 验收 ④）。

    这条是**正对照**：若哪天 ENV_SEGMENT 变得单射，坍缩 fail-fast 就成了永假分支，此处会先红提醒。
    """
    assert env_var_name("a-b") == env_var_name("a_b") == "A2C_SMCP_a_b"


def test_env_segment_is_not_bundle_id_normalize_name() -> None:
    """ENV_SEGMENT **不**折叠连续 '_'、**不**裁首尾——与 bundle_id.normalize_name 的行为差异钉死。

    normalize_name('a--b') == 'a-b'（不折 '-'）、normalize_name('_lead_') == 'lead'（裁首尾）；
    误把 normalize_name 当 ENV_SEGMENT 复用会让 a_b/a__b 坍缩且首尾信息丢失 ⇒ 此处先红。
    """
    from a2c_smcp.utils.bundle_id import normalize_name

    assert env_var_name("a--b") == "A2C_SMCP_a__b"
    assert env_var_name("_lead_trail_") == "A2C_SMCP__lead_trail_"
    # 反向：normalize_name 若被误用，产出会与上面两条不同
    assert normalize_name("a--b") == "a--b"
    assert normalize_name("_lead_trail_") == "lead_trail"
