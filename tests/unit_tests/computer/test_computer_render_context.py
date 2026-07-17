# -*- coding: utf-8 -*-
# filename: test_computer_render_context.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 渲染上下文透传 + active_workdir 单元测试（v0.2.1 #69 Group A/C）/ render context + active_workdir。

测试意图 / Test intentions:
- ``active_workdir``：空 registered_workdirs → None；非空 → 首个（绑定任务单根）；
- ``_render_variables``：``workspaceFolder`` 走 active_workdir，无则 cwd；
- 渲染上下文（Group A）：bundled server 的裸 ``${input:id}`` 经 plugin/marketplace 上下文解析到带前缀池条目
  （§9.3 D2）；env 命中（headless 安全，password 在 env 命中先于 TTY 守卫）；无上下文 → InputNotFoundError。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import MCPServerPromptStringInput


# ---------------------------------------------------------------------------
# #116 概念瘦身：Computer 无 workdir 概念 / #116: Computer carries no workdir concept
# ---------------------------------------------------------------------------
def test_computer_rejects_workdir_concepts(tmp_path: Path) -> None:
    """#116: `registered_workdirs` 构造参数与 `active_workdir` 属性均已移除。"""
    with pytest.raises(TypeError):
        Computer(name="t", registered_workdirs=[tmp_path])
    assert not hasattr(Computer(name="t"), "active_workdir")


def test_render_variables_no_workspace_folder() -> None:
    """#116: 渲染变量仅剩 userHome / pathSeparator（${workspaceFolder} 停产）。"""
    variables = Computer(name="t")._render_variables()
    assert set(variables) == {"userHome", "pathSeparator"}
    assert variables["pathSeparator"] == os.sep


_CFG = {"name": "s", "type": "stdio", "server_parameters": {"command": "node", "args": ["${input:token}"]}}


@pytest.mark.asyncio
async def test_render_context_resolves_prefixed_input(monkeypatch: pytest.MonkeyPatch) -> None:
    # 前缀化 id 入池 + bare ${input:token} 经 plugin/marketplace 上下文回退到 audit@acme/token（§9.3 D2）。
    # env 命中（A2C_SMCP_audit_acme_token）→ headless 安全（password 在 env 命中先于无 TTY 守卫）。
    monkeypatch.setenv("A2C_SMCP_audit_acme_token", "secret-val")
    comp = Computer(name="t")
    comp.add_or_update_input(MCPServerPromptStringInput(id="audit@acme/token", description="d", password=True, type="promptString"))

    _raw, validated = await comp._arender_and_validate_server(dict(_CFG), plugin="audit", marketplace="acme")  # #149：取渲染后
    assert "secret-val" in json.dumps(validated.model_dump(mode="json"))  # 渲染命中
    # #149：同源 raw 元须保留占位符字面（未渲染），证明 raw≠rendered、raw 绝不含已解析 secret。
    raw_dumped = json.dumps(_raw.model_dump(mode="json"))
    assert "${input:token}" in raw_dumped and "secret-val" not in raw_dumped


@pytest.mark.asyncio
async def test_render_without_context_leaves_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2C_SMCP_audit_acme_token", "secret-val")
    comp = Computer(name="t")
    comp.add_or_update_input(MCPServerPromptStringInput(id="audit@acme/token", description="d", password=True, type="promptString"))
    # 无 plugin/marketplace 上下文 → 裸 token 不在池、无前缀回退 → render 容错保留占位符（不解析、不泄漏 env 值）
    _raw, validated = await comp._arender_and_validate_server(dict(_CFG))  # #149：取渲染后
    dumped = json.dumps(validated.model_dump(mode="json"))
    assert "${input:token}" in dumped and "secret-val" not in dumped
