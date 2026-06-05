# -*- coding: utf-8 -*-
# filename: test_model_skill.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``GetSkillRet`` 服务侧 XOR 校验模型单元测试（v0.2.1 #66）
Unit tests for the server-side ``GetSkillRet`` XOR validation model.

协议依据 / Protocol: a2c-smcp-protocol data-structures.md §GetSkillRet；skill.md §9
                      （``body`` 与 ``blob_handle`` **恰一存在**）。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from a2c_smcp.computer.mcp_clients.model import GetSkillRet

_BASE: dict[str, Any] = {
    "name": "mcp:srv:demo",
    "rel_path": "SKILL.md",
    "mime_type": "text/markdown",
    "total_size": 12,
    "sha256": "a" * 64,
    "req_id": "r-1",
}


def test_body_only_valid() -> None:
    m = GetSkillRet.model_validate({**_BASE, "body": "hello body\n"})
    assert m.body == "hello body\n"
    assert m.blob_handle is None
    dumped = m.model_dump(exclude_none=True)
    assert "body" in dumped and "blob_handle" not in dumped


def test_blob_handle_only_valid() -> None:
    m = GetSkillRet.model_validate({**_BASE, "blob_handle": "opaque-handle"})
    assert m.blob_handle == "opaque-handle"
    assert m.body is None
    dumped = m.model_dump(exclude_none=True)
    assert "blob_handle" in dumped and "body" not in dumped


def test_both_present_rejected() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        GetSkillRet.model_validate({**_BASE, "body": "x", "blob_handle": "y"})


def test_neither_present_rejected() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        GetSkillRet.model_validate(dict(_BASE))


def test_extra_field_forbidden() -> None:
    """extra='forbid'：服务侧自构造多余字段即 SDK bug，硬失败暴露。"""
    with pytest.raises(ValidationError):
        GetSkillRet.model_validate({**_BASE, "body": "x", "unexpected_field": 1})


def test_empty_string_body_counts_as_present() -> None:
    """空字符串 body（如剥离 frontmatter 后空正文）视为存在（None 才是缺席）。"""
    m = GetSkillRet.model_validate({**_BASE, "body": ""})
    assert m.body == ""
    assert m.blob_handle is None
