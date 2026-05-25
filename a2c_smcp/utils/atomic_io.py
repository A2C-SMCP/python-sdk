# -*- coding: utf-8 -*-
# filename: atomic_io.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
原子文件写入工具：唯一临时文件 + ``fsync`` + 原子 ``rename``。
Atomic file-write helpers: unique tmp + ``fsync`` + atomic ``rename``.

抽取自 ``blob/toolspool`` 与 ``settings/store`` 的共用原语（S5 fix-review #58），消除两份漂移的原子写
实现。本实现带三重硬化（落盘保证 + 失败清理 + 父目录创建），迁移后 ``toolspool`` 一并获得（其原实现
无 ``fsync``、失败留垃圾）。
Extracted from the duplicated primitives in ``blob/toolspool`` and ``settings/store``. This hardened
version (fsync + on-failure cleanup + parent mkdir) is now shared, upgrading ``toolspool`` (whose old
impl had no fsync and left tmp garbage on failure).

纯 stdlib、无 a2c 内部依赖，故落在 :mod:`a2c_smcp.utils` / Pure stdlib, no a2c deps.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def unique_tmp_path(path: Path) -> Path:
    """
    生成进程唯一的临时文件名 ``<name>.<pid>.<uuid8>.tmp`` / Build a process-unique tmp filename。

    若多个进程共享目录且并发写同一目标，固定 ``<name>.tmp`` 会让两次写共用一个临时文件——第二次
    ``os.replace`` 源已被前者 rename 走 → ``FileNotFoundError``。进程 + 随机后缀彻底规避撞名。
    A fixed ``<name>.tmp`` would collide under concurrent writers (the 2nd ``os.replace`` finds its
    source already renamed → ``FileNotFoundError``); the pid + random suffix avoids this.
    """
    unique = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
    return path.with_suffix(f"{path.suffix}.{unique}.tmp")


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """
    原子写字节：父目录 ``mkdir`` + 唯一临时文件 + ``flush`` + ``fsync`` + ``os.replace``。
    Atomic bytes write (POSIX cross-kernel guarantee via unique tmp + ``fsync`` + ``os.replace``).

    rename 前 ``fsync`` 保证数据已落盘；任意失败（含 ``KeyboardInterrupt``）清理半截临时文件，目标
    文件保持原子完好（要么旧内容、要么新内容，绝无半截）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp_path(p)
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """
    原子写文本：按 ``encoding`` 编码后走 :func:`atomic_write_bytes`（二进制写，不做平台换行翻译）。
    Atomic text write: encode then delegate to :func:`atomic_write_bytes` (binary, no newline xlate).
    """
    atomic_write_bytes(path, text.encode(encoding))
