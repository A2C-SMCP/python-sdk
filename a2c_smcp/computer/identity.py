# -*- coding: utf-8 -*-
# filename: identity.py
# @Time    : 2026/10/28
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Computer 稳定身份的**本地持久化**（协议#64；位置与格式为 SDK 自治，已与 rust-sdk 对齐）。
Local persistence of the stable Computer identity (protocol#64; location/format are SDK-autonomous
and aligned with rust-sdk — see a2c-smcp-protocol Discussion #71).

存储契约（实现约定的硬不变量）/ Storage contract (hard invariants):

- 位置：``$XDG_CONFIG_HOME/a2c/computer-id``（``XDG_CONFIG_HOME`` **仅绝对路径**生效），
  否则 ``~/.config/a2c/computer-id``；
- 内容：UTF-8 单行、小写规范 UUID + 结尾换行；
- 权限：目录 ``0700`` / 文件 ``0600``（显式 chmod，``mkdir`` 自身受 umask 影响）；
- 首启：``O_CREAT|O_EXCL`` 原子创建，多进程并发首启共用同一值；
- 空 / 半写（并发创建者尚未写完）：短重试 **≤1s**；
- 损坏（重试耗尽仍非合法形态）或不可读写：**fail-fast**，绝不静默改用临时值、绝不自动覆写。

协议依据 / Protocol: room-model.md §Computer 身份「持久化失败 MUST 以明确错误终止启动」。
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from a2c_smcp.utils.computer_id import is_canonical_computer_id

#: 目录名（XDG 配置根下的 SDK 子目录）。/ SDK subdirectory under the XDG config root.
IDENTITY_DIR_NAME: Final[str] = "a2c"
#: 身份文件名。/ Identity file name.
IDENTITY_FILE_NAME: Final[str] = "computer-id"
#: 目录权限：仅属主可访问。/ Directory mode: owner-only.
IDENTITY_DIR_MODE: Final[int] = 0o700
#: 文件权限：仅属主可读写。/ File mode: owner-only read/write.
IDENTITY_FILE_MODE: Final[int] = 0o600

#: 并发首启竞态（另一进程已建文件但尚未写完）的重试间隔与总预算。
#: Retry interval / total budget for the empty-or-half-written window of a concurrent first start.
_READ_RETRY_INTERVAL: Final[float] = 0.01
_READ_RETRY_BUDGET: Final[float] = 1.0


class ComputerIdentityError(RuntimeError):
    """身份持久化不可读写或已损坏——调用方 MUST 终止启动（CLI 非零退出），不得静默生成临时值。

    The identity store is unreadable/unwritable or corrupt: callers MUST abort startup rather
    than silently fall back to a per-run temporary value (protocol room-model §Computer 身份).
    """


def identity_store_path(env: Mapping[str, str] | None = None) -> Path:
    """
    解析身份文件路径：``$XDG_CONFIG_HOME/a2c/computer-id``（仅绝对路径），否则 ``~/.config/a2c/computer-id``。

    XDG Base Directory 规范：``XDG_CONFIG_HOME`` 为相对路径时视为未设置。本函数**只解析**，
    不创建任何目录（物化时机由调用方决定：Computer 在首次 ``connect`` 时才落盘）。
    Resolve the identity path; a relative ``XDG_CONFIG_HOME`` is ignored per the XDG spec.
    This function is side-effect free — materialization belongs to the first ``connect``.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    xdg_home = environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        candidate = Path(xdg_home).expanduser()
        if candidate.is_absolute():
            base = candidate
        else:
            base = Path.home() / ".config"
    else:
        base = Path.home() / ".config"
    return base / IDENTITY_DIR_NAME / IDENTITY_FILE_NAME


def load_or_create_computer_id(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    retry_budget: float = _READ_RETRY_BUDGET,
) -> str:
    """
    读取既有 ``computer_id``；不存在则以 ``O_CREAT|O_EXCL`` 原子创建并返回。

    并发首启：两个进程同时发现文件不存在时，恰一者创建成功，另一者读到（可能短暂读到空 / 半写，
    按 ``retry_budget`` 重试后读到完整值）。既有文件损坏（非规范 UUID、nil / max、无结尾换行）或
    不可读写 ⇒ :class:`ComputerIdentityError`（**不**自动覆写，避免把宿主的持久引用静默换掉）。
    Read the identity, atomically creating it (O_EXCL) on first start. Concurrency-safe; a corrupt
    or unreadable existing file fails fast instead of being silently overwritten.

    Args:
        path: 显式路径（测试 / 嵌入方）；``None`` ⇒ :func:`identity_store_path`。
        env: 路径解析用的环境变量映射（默认 ``os.environ``）。
        retry_budget: 空 / 半写重试预算（秒）。测试可缩短。

    Returns:
        str: 小写规范 UUID 文本。

    Raises:
        ComputerIdentityError: 目录 / 文件不可读写，或内容在重试预算内始终不是合法形态。
    """
    target = Path(path) if path is not None else identity_store_path(env)
    _ensure_private_dir(target.parent)
    deadline = time.monotonic() + max(0.0, retry_budget)

    while True:
        created = _try_create(target)
        if created is not None:
            return created

        content = _read_text(target)
        if content is not None and content.endswith("\n"):
            candidate = content[:-1]
            # 完整一行且合法 ⇒ 直接采用；完整一行但非法 ⇒ 确定性损坏，无需再等（fail-fast）。
            # A complete line: adopt it if canonical, otherwise fail fast — waiting cannot fix it.
            if is_canonical_computer_id(candidate):
                return candidate
            raise ComputerIdentityError(
                f"Computer 身份文件内容非法（{target}）: {candidate!r}；"
                f"请删除该文件以生成新身份，或显式执行 identity reset / invalid computer-id content",
            )

        # 空 / 半写（无结尾换行）：并发创建者可能尚未写完，短重试；预算耗尽 ⇒ 损坏。
        # Empty or half-written: retry briefly; exhausted budget means corruption.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ComputerIdentityError(
                f"Computer 身份文件为空或半写且重试超时（{target}）；"
                f"请检查文件权限/内容，或显式执行 identity reset / empty or half-written computer-id",
            )
        time.sleep(min(_READ_RETRY_INTERVAL, remaining))


def reset_computer_id(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """
    生成全新 ``computer_id`` 并**原子替换**持久化文件（等价于「换了一台新电脑」）。

    只做文件层替换：不判断是否有运行中的连接（独立 CLI 无法知道；REPL / SDK 的「已连接拒绝」
    由 :meth:`SMCPComputerClient.reset_identity` 承担）。新值只经**新连接**生效——Server 对同一
    sid 的身份记录不因 leave 清除，同连接改值会撞 ``403``。
    Generate a new identity and atomically replace the file. Connection-state checks belong to the
    SDK/REPL layer; the new value only takes effect on a *new* connection (same-sid change ⇒ 403).

    Args:
        path: 显式路径（测试 / 嵌入方）；``None`` ⇒ :func:`identity_store_path`。
        env: 路径解析用的环境变量映射（默认 ``os.environ``）。

    Returns:
        str: 新生成的小写规范 UUID。

    Raises:
        ComputerIdentityError: 目录 / 文件不可写。
    """
    target = Path(path) if path is not None else identity_store_path(env)
    _ensure_private_dir(target.parent)
    new_id = str(uuid.uuid4())
    payload = f"{new_id}\n".encode()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd: int | None = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, IDENTITY_FILE_MODE)
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, target)
    except OSError as e:
        raise ComputerIdentityError(f"Computer 身份重置失败（{target}）: {e}") from e
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return new_id


def _ensure_private_dir(directory: Path) -> None:
    """创建目录并**显式** chmod 0700（``mkdir`` 的 mode 会被 umask 削）。/ mkdir + explicit chmod 0700."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, IDENTITY_DIR_MODE)
    except OSError as e:
        raise ComputerIdentityError(f"无法创建 / 加固身份目录（{directory}）: {e}") from e


def _try_create(target: Path) -> str | None:
    """``O_CREAT|O_EXCL`` 原子创建；已存在返回 ``None`` 交由读取路径处理。

    Returns:
        str | None: 新写入的身份；文件已存在时为 ``None``。

    Raises:
        ComputerIdentityError: 无法创建（权限 / 路径异常）。
    """
    new_id = str(uuid.uuid4())
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, IDENTITY_FILE_MODE)
    except FileExistsError:
        return None
    except OSError as e:
        raise ComputerIdentityError(f"无法创建 Computer 身份文件（{target}）: {e}") from e
    try:
        os.write(fd, f"{new_id}\n".encode())
        os.fsync(fd)
    except OSError as e:
        raise ComputerIdentityError(f"无法写入 Computer 身份文件（{target}）: {e}") from e
    finally:
        os.close(fd)
    # O_EXCL 的 0600 已按 mode 落定（不受 umask 影响？——实际会被 umask 削），显式加固一次。
    # Explicit chmod: open()'s mode is filtered by umask.
    with contextlib.suppress(OSError):
        os.chmod(target, IDENTITY_FILE_MODE)
    return new_id


def _read_text(target: Path) -> str | None:
    """读文件文本；不存在 ⇒ ``None``（并发创建者可能刚删/替换）；其它 IO 错误 ⇒ fail-fast。"""
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise ComputerIdentityError(f"无法读取 Computer 身份文件（{target}）: {e}") from e
