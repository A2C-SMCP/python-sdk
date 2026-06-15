# -*- coding: utf-8 -*-
# filename: test_mime.py
# @Time    : 2026/06/12
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
单一权威 MIME 工具单元测试 / Unit tests for the single-source MIME helper (#105)。

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §6.4。

测试意图 / Test intentions:
- ``guess_mime`` 确定性：基线表全集固定输出、未登记回退 octet-stream、大小写不敏感、子路径取末段；
  **与宿主 OS 无关**（即便宿主 ``mimetypes`` 对 ``.md``/``.yaml`` 返回 None 仍给出规范 MIME）；
- ``is_text_mime`` §6.4(2) 三分支：``text/*`` ∨ ``+json``/``+xml``/``+yaml`` 后缀 ∨ essence 白名单；
  二进制为负例。
"""

from __future__ import annotations

import mimetypes as _mt

import pytest

from a2c_smcp.utils.mime import FALLBACK_MIME, guess_mime, is_text_mime


class TestGuessMime:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("a.md", "text/markdown"),
            ("a.markdown", "text/markdown"),
            ("a.txt", "text/plain"),
            ("a.json", "application/json"),
            ("a.xml", "application/xml"),
            ("a.yaml", "application/yaml"),
            ("a.yml", "application/yaml"),
            ("a.toml", "application/toml"),
            ("a.rst", "text/x-rst"),
            ("a.png", "image/png"),
            ("a.pdf", "application/pdf"),
        ],
    )
    def test_baseline_table_deterministic(self, filename: str, expected: str) -> None:
        assert guess_mime(filename) == expected

    def test_case_insensitive(self) -> None:
        assert guess_mime("README.MD") == "text/markdown"
        assert guess_mime("CONFIG.YAML") == "application/yaml"

    def test_subdir_path_uses_last_suffix(self) -> None:
        assert guess_mime("resources/scenarios/file-serialize-sync.md") == "text/markdown"

    @pytest.mark.parametrize("filename", ["noext", "x.unknownext", ".skillenv", "a.tar.unknown"])
    def test_unknown_falls_back_octet_stream(self, filename: str) -> None:
        assert guess_mime(filename) == FALLBACK_MIME == "application/octet-stream"

    def test_does_not_consult_host_mimetypes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """确定性核心：即便宿主 ``mimetypes`` 对 ``.md``/``.yaml`` 返回 None（macOS / 旧 Python 实况），
        ``guess_mime`` 仍 MUST 返回规范 MIME——证明它不依赖宿主库（#105 根因修复）。"""
        monkeypatch.setattr(_mt, "guess_type", lambda *a, **k: (None, None))
        assert guess_mime("x.md") == "text/markdown"
        assert guess_mime("x.yaml") == "application/yaml"
        assert guess_mime("x.toml") == "application/toml"


class TestIsTextMime:
    @pytest.mark.parametrize(
        ("mime", "expected"),
        [
            # —— text/* 顶级类型（RFC 2046）——
            ("text/plain", True),
            ("text/markdown", True),
            ("text/x-rst", True),
            ("text/markdown; charset=utf-8", True),  # 参数被忽略
            ("TEXT/MARKDOWN", True),  # 大小写不敏感
            # —— essence 白名单 ——
            ("application/json", True),
            ("application/xml", True),
            ("application/yaml", True),
            ("application/x-yaml", True),  # 兼容别名
            ("application/toml", True),
            ("application/javascript", True),
            # —— 结构化后缀（RFC 6839 / 9512）——
            ("application/vnd.api+json", True),
            ("image/svg+xml", True),
            ("application/foo+yaml", True),
            # —— 二进制负例 ——
            ("application/octet-stream", False),
            ("image/png", False),
            ("application/pdf", False),
            ("application/zip", False),
            ("font/woff2", False),
        ],
    )
    def test_criteria(self, mime: str, expected: bool) -> None:
        assert is_text_mime(mime) is expected
