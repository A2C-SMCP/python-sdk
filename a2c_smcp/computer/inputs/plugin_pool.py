# -*- coding: utf-8 -*-
# filename: plugin_pool.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
plugin ``mcp-servers/inputs.json`` 入池消歧 / Plugin inputs pool disambiguation（v0.2.1 #65，§9.3 D2）。

plugin 的 inputs 是 plugin-scoped（协议 v1 不跨 plugin 共享）；合进 Computer 全局 inputs 池时，为避免
两个 plugin 同 ``id`` 撞（如都用 ``api_token``），**池内存前缀 id** ``<plugin>@<marketplace>/<id>``
（决策 #65 方案 a：自动前缀消歧，对用户透明、零打断）。plugin 内 ``${input:<id>}`` 引用用**裸 id**，由
:class:`resolver.InputResolver` 在该 plugin 上下文里回退到带前缀池条目（见 resolver ``plugin``/``marketplace``
关键字）。user/CLI 直接定义的 inputs（非 plugin 来源）无前缀，裸 id 入池（沿用现状）。

本模块只负责**读取 + 校验 + 前缀改写**；实时挂载（enumerate installed plugins → 填池 → spawn）属 #69。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from a2c_smcp.computer.mcp_clients.model import MCPServerInput
from a2c_smcp.utils.logger import get_logger

logger = get_logger("computer")

_MCP_INPUT_ADAPTER: TypeAdapter[MCPServerInput] = TypeAdapter(MCPServerInput)


def prefix_input_id(plugin: str, marketplace: str, input_id: str) -> str:
    """
    构造带前缀的池内 id ``<plugin>@<marketplace>/<id>`` / Build the prefixed pool id。

    与 ``installer`` 的 plugin_id ``<plugin>@<marketplace>`` 命名空间一致（§9.3 D2）。
    """
    return f"{plugin}@{marketplace}/{input_id}"


def _read_input_defs(inputs_json: Path) -> list[Any]:
    """读 inputs.json → 原始 input 定义列表；兼容 ``{"inputs": [...]}`` 与裸 ``[...]``；畸形 → ``[]``。"""
    try:
        raw = inputs_json.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:  # pragma: no cover - 读权限/IO 异常
        logger.warning(f"读取 plugin inputs.json 失败 / Failed to read plugin inputs.json: {inputs_json}: {e}")
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"plugin inputs.json JSON 畸形，跳过 / Malformed plugin inputs.json, skip: {inputs_json}")
        return []
    if isinstance(data, Mapping):
        defs = data.get("inputs", [])
    else:
        defs = data
    return list(defs) if isinstance(defs, list) else []


def load_plugin_inputs(inputs_json: Path, plugin: str, marketplace: str) -> list[MCPServerInput]:
    """
    读取并前缀化 plugin 的 inputs 定义 / Load and prefix a plugin's input definitions。

    逐项校验为 :class:`MCPServerInput`（复用 ``mcp_config`` 的 field-tolerant 范式：畸形条目记 WARN 跳过、
    **不抛**），再把 ``id`` 改写为 :func:`prefix_input_id`（frozen 模型经 ``model_copy(update=)`` 重建）。
    文件缺失 / 畸形 → ``[]``。
    """
    result: list[MCPServerInput] = []
    for idef in _read_input_defs(inputs_json):
        try:
            inp = _MCP_INPUT_ADAPTER.validate_python(idef)
        except ValidationError as exc:
            raw_id = idef.get("id") if isinstance(idef, Mapping) else "<unknown>"
            logger.warning(f"plugin input 定义畸形，跳过 / Invalid plugin input def, skip [{raw_id}]: {exc}")
            continue
        prefixed = prefix_input_id(plugin, marketplace, inp.id)
        result.append(inp.model_copy(update={"id": prefixed}))
    return result
