# -*- coding: utf-8 -*-
# filename: env.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
环境变量解析工具 / Environment-variable parsing helpers。

统一布尔环境变量的**真值集**，避免各处内联 ``... .lower() in (...)`` 解析因集合不一致而漂移
（如某处认 ``"on"`` 而另一处不认）。
Unifies the canonical truth set for boolean env vars so inline parses don't drift across modules.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# 规范真值集 / Canonical truthy set（解析前统一 ``strip().lower()``，故此处只列小写裸值）。
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def env_truthy(key: str, *, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    """
    判断布尔型环境变量是否为真 / Whether a boolean-style env var is truthy。

    取 ``env[key]``（缺省 ``os.environ``）后 ``strip().lower()``，命中 :data:`_TRUTHY`
    （``1`` / ``true`` / ``yes`` / ``on``）为真；**未设置或空白** → ``default``；其它值（如 ``0`` /
    ``false`` / ``off``）→ ``False``。

    :param key: 环境变量名 / env var name.
    :param default: 未设置或空白时的回退值 / fallback when unset or blank.
    :param env: 环境映射，便于测试注入 / env mapping, defaults to ``os.environ``.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    raw = environ.get(key)
    if raw is None:
        return default
    norm = raw.strip().lower()
    if not norm:
        return default
    return norm in _TRUTHY
