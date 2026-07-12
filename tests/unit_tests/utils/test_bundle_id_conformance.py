# -*- coding: utf-8 -*-
# filename: test_bundle_id_conformance.py
# @Time    : 2026/07/11
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
BundleID 缺省生成**跨 SDK 一致性对拍**（P0 硬门槛）/ Cross-SDK conformance for default bundle_id derivation.

夹具来源 / Fixture source（唯一权威）：
    a2c-smcp-protocol ``docs/specification/fixtures/bundle_id_conformance_vectors.json``
    @ develop ``57c2f9f``（PR #16 首版 14 条 + #17 raw 决策补 2 条 ``${input:*}`` 占位向量 = 16 条）。
    本文件旁的 ``fixtures/bundle_id_conformance_vectors.json`` 为该规范文件的**逐字节 vendored 副本**——
    协议仓更新向量时须重新同步（``git show origin/develop:docs/specification/fixtures/... > fixtures/...``）。

含义 / Meaning：任一 SDK 的 ``resolve_bundle_id(name, config)`` **MUST** 对每条向量产出 ``expected_bundle_id``
方为合规。python 与 rust 逐字节一致是协议 §「MCP Tool 命名与路由（BundleID 模型）」的硬门槛（a2c-smcp-protocol#15）。

**raw 语义（#17 已定）**：connection-identity 取 **raw / 未注入**配置——``${input:*}`` 占位**按字面**参与摘要
（末 2 条向量钉住此点）。故本文件用字面占位值构造 config；真实注册链的 raw-derive 时机由 Computer
``_arender_and_validate_server`` 在 ``ConfigRender.arender`` **之前**保证（见 Phase 3）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters

from a2c_smcp.computer.mcp_clients.model import (
    MCPServerConfig,
    SseServerConfig,
    StdioServerConfig,
    StreamableHttpServerConfig,
)
from a2c_smcp.utils.bundle_id import resolve_bundle_id

_FIXTURE = Path(__file__).parent / "fixtures" / "bundle_id_conformance_vectors.json"


def _load() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _build_config(name: str, cfg: dict[str, Any]) -> MCPServerConfig:
    """由夹具的**扁平连接身份**形式构造 MCPServerConfig（含可选显式 bundle_id）。"""
    bundle_id = cfg.get("bundle_id")
    ctype = cfg["type"]
    if ctype == "stdio":
        return StdioServerConfig(
            name=name,
            bundle_id=bundle_id,
            server_parameters=StdioServerParameters(command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env")),
        )
    if ctype == "streamable":
        return StreamableHttpServerConfig(
            name=name,
            bundle_id=bundle_id,
            server_parameters=StreamableHttpParameters(url=cfg["url"], headers=cfg.get("headers")),
        )
    if ctype == "sse":
        return SseServerConfig(
            name=name,
            bundle_id=bundle_id,
            server_parameters=SseServerParameters(url=cfg["url"], headers=cfg.get("headers")),
        )
    raise ValueError(f"unknown fixture config type: {ctype!r}")


_VECTORS: list[dict[str, Any]] = _load()["vectors"]


def test_fixture_integrity() -> None:
    """守卫 vendored 副本未被截断/损坏（14 条向量 + algorithm 段）。"""
    data = _load()
    assert "algorithm" in data
    # #17 raw 决策：algorithm 段须声明 input_state=raw；向量数 16（14 首版 + 2 条 ${input:*} 占位）。
    assert "raw" in data["algorithm"].get("input_state", "")
    assert len(data["vectors"]) == 16
    for v in _VECTORS:
        assert {"name", "config", "expected_bundle_id"} <= v.keys()


@pytest.mark.parametrize("vec", _VECTORS, ids=[v["expected_bundle_id"] for v in _VECTORS])
def test_bundle_id_conformance(vec: dict[str, Any]) -> None:
    """逐字节对拍：resolve_bundle_id(name, config) == expected_bundle_id（与 rust-sdk 一致）。"""
    cfg = _build_config(vec["name"], vec["config"])
    got = resolve_bundle_id(cfg)
    assert got == vec["expected_bundle_id"], f"{vec['desc']}: got={got} expected={vec['expected_bundle_id']}"
