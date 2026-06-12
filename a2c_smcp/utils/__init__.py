# -*- coding: utf-8 -*-
# filename: __init__.py.py
# @Time    : 2025/9/29 10:36
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm

"""
工具模块导出
Export utilities
"""

from .atomic_io import atomic_write_bytes, atomic_write_text, unique_tmp_path
from .mime import EXT_TO_MIME, guess_mime, is_text_mime
from .path import is_within, resolve_xdg_first
from .window_uri import WindowURI, is_window_uri

__all__ = [
    "EXT_TO_MIME",
    "WindowURI",
    "atomic_write_bytes",
    "atomic_write_text",
    "guess_mime",
    "is_text_mime",
    "is_within",
    "is_window_uri",
    "resolve_xdg_first",
    "unique_tmp_path",
]
