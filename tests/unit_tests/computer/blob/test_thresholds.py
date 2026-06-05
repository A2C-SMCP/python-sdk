# -*- coding: utf-8 -*-
# filename: test_thresholds.py
# @Author  : JQQ
# @Software: PyCharm

"""
SKILL / blob 阈值默认值 + 环境变量覆盖 / Thresholds defaults + env overrides.

设计依据 / Design: docs/design-0.2.1-skill-computer-management.md §4.4.
"""

from __future__ import annotations

import pytest

from a2c_smcp.computer.blob.thresholds import (
    DEFAULT_CHUNK_MAX_BYTES,
    DEFAULT_INLINE_BUDGET,
    DEFAULT_TOO_LARGE_CAP,
    ENV_CHUNK_MAX_BYTES,
    ENV_INLINE_BUDGET,
    ENV_TOO_LARGE_CAP,
    BlobThresholds,
    default_thresholds,
)


class TestDefaults:
    def test_default_values(self) -> None:
        """硬编码默认值对齐设计 §4.4 / Hard-coded defaults match design §4.4."""
        assert DEFAULT_INLINE_BUDGET == 32 * 1024
        assert DEFAULT_TOO_LARGE_CAP == 100 * 1024 * 1024
        assert DEFAULT_CHUNK_MAX_BYTES == 256 * 1024

    def test_default_thresholds_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无环境变量 → 硬编码默认 / No env → hard-coded defaults."""
        monkeypatch.delenv(ENV_INLINE_BUDGET, raising=False)
        monkeypatch.delenv(ENV_TOO_LARGE_CAP, raising=False)
        monkeypatch.delenv(ENV_CHUNK_MAX_BYTES, raising=False)
        t = default_thresholds()
        assert t.inline_budget == DEFAULT_INLINE_BUDGET
        assert t.too_large_cap == DEFAULT_TOO_LARGE_CAP
        assert t.chunk_max_bytes == DEFAULT_CHUNK_MAX_BYTES


class TestEnvOverrides:
    def test_inline_budget_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_INLINE_BUDGET, "1024")
        t = default_thresholds()
        assert t.inline_budget == 1024

    def test_too_large_cap_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_TOO_LARGE_CAP, "104857600")
        t = default_thresholds()
        assert t.too_large_cap == 104857600

    def test_chunk_max_bytes_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CHUNK_MAX_BYTES, "65536")
        t = default_thresholds()
        assert t.chunk_max_bytes == 65536

    def test_invalid_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非整数 / 非正环境变量 → 静默回退默认值 / Non-int / non-positive → silent fallback."""
        for bad in ("not_a_number", "-1", "0", ""):
            monkeypatch.setenv(ENV_INLINE_BUDGET, bad)
            t = default_thresholds()
            assert t.inline_budget == DEFAULT_INLINE_BUDGET, f"failed for env value {bad!r}"


class TestClampChunk:
    """单块大小 clamp 规则 / Chunk-size clamp rules."""

    def test_clamp_caps_to_max(self) -> None:
        t = BlobThresholds(chunk_max_bytes=1024)
        assert t.clamp_chunk(99999) == 1024
        assert t.clamp_chunk(1025) == 1024

    def test_clamp_passes_through_below_max(self) -> None:
        t = BlobThresholds(chunk_max_bytes=1024)
        assert t.clamp_chunk(512) == 512

    def test_clamp_defaults_when_none_or_nonpositive(self) -> None:
        """缺省值 / 0 / 负数 → 取 chunk_max_bytes / None / 0 / negative → chunk_max_bytes."""
        t = BlobThresholds(chunk_max_bytes=2048)
        assert t.clamp_chunk(None) == 2048
        assert t.clamp_chunk(0) == 2048
        assert t.clamp_chunk(-1) == 2048


class TestImmutability:
    def test_frozen_dataclass(self) -> None:
        """frozen=True 防止意外修改 / frozen=True prevents accidental mutation."""
        t = BlobThresholds()
        with pytest.raises((AttributeError, Exception)):
            t.inline_budget = 999  # type: ignore[misc]
