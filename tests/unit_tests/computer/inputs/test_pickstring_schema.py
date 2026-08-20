# -*- coding: utf-8 -*-
# filename: test_pickstring_schema.py
"""
Issue #192（镜像 protocol#48 / rust-sdk#187）：PickString options 结构化 {label,value} schema 校验向量
（conformance-tests §5 七景 ④⑤⑥⑦ + 必填性 + wire TypedDict 镜像）。
"""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from a2c_smcp.computer.mcp_clients.model import MCPServerPickStringInput
from a2c_smcp.smcp import MCPServerInput as WireMCPServerInput


def _structured(options: list[dict[str, str]], *, default: str | None = None) -> dict:
    body: dict = {"id": "region", "description": "pick one", "type": "pickString", "options": options}
    if default is not None:
        body["default"] = default
    return body


class TestPickStringSchema:
    """模型层（model.py）schema 校验。"""

    def test_valid_structured_options(self) -> None:
        cfg = MCPServerPickStringInput.model_validate(
            _structured([{"label": "中国", "value": "cn"}, {"label": "欧洲", "value": "eu"}], default="cn")
        )
        assert [o.label for o in cfg.options] == ["中国", "欧洲"]
        assert [o.value for o in cfg.options] == ["cn", "eu"]
        assert cfg.default == "cn"

    def test_default_null_treated_as_no_default(self) -> None:
        """⑥（邻接条款）：default 显式 null → 不拒绝，视为无默认。"""
        cfg = MCPServerPickStringInput.model_validate(_structured([{"label": "a", "value": "x"}], default=None))
        assert cfg.default is None

    def test_duplicate_labels_and_values_allowed(self) -> None:
        """⑥：label / value 重复 → 校验通过（不要求唯一）。"""
        cfg = MCPServerPickStringInput.model_validate(
            _structured([{"label": "a", "value": "x"}, {"label": "a", "value": "x"}])
        )
        assert len(cfg.options) == 2

    @pytest.mark.parametrize(
        "bad",
        [
            _structured([]),  # ⑤ 空 options
            _structured([{"label": "", "value": "x"}]),  # ⑤ 空 label
            _structured([{"label": "a", "value": ""}]),  # ⑤ 空 value
            _structured([{"label": "a", "value": "x"}], default="nope"),  # ④ default 失配
        ],
        ids=["empty-options", "empty-label", "empty-value", "default-mismatch"],
    )
    def test_invalid_structures_rejected(self, bad: dict) -> None:
        with pytest.raises(ValidationError):
            MCPServerPickStringInput.model_validate(bad)

    def test_missing_options_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPServerPickStringInput.model_validate({"id": "r", "description": "d", "type": "pickString"})

    def test_legacy_string_array_rejected_with_pointer(self) -> None:
        """⑦：旧 ``options: list[str]`` MUST 以 validation 拒绝（报错指路新结构，无 alias）。"""
        with pytest.raises(ValidationError, match="label"):
            MCPServerPickStringInput.model_validate(
                {"id": "r", "description": "d", "type": "pickString", "options": ["a", "b"]}
            )


class TestWireTypedDictMirror:
    """wire 层（smcp.py TypedDict）镜像校验——REPL ``inputs load`` 先经此适配器。"""

    def test_wire_adapter_accepts_structured(self) -> None:
        TypeAdapter(WireMCPServerInput).validate_python(_structured([{"label": "a", "value": "x"}]))

    def test_wire_adapter_rejects_legacy_string_array_with_pointer(self) -> None:
        with pytest.raises(ValidationError, match="label"):
            TypeAdapter(WireMCPServerInput).validate_python(
                {"id": "r", "description": "d", "type": "pickString", "options": ["a", "b"]}
            )
