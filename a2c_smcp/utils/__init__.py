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

from .dpe_uri import DPEURI, is_dpe_uri
from .window_uri import WindowURI, is_window_uri

__all__ = [
    "DPEURI",
    "WindowURI",
    "is_dpe_uri",
    "is_window_uri",
]
