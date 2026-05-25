# -*- coding: utf-8 -*-
# filename: store.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
物化层文件 store：原子写 + 文件锁 + 损坏恢复（v0.2.1）
Materialized-file store: atomic write + file lock + corruption recovery (v0.2.1).

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §6.1 / §6.2 / §6.3。

本模块是物化层（CLI 自动维护、**不可手编**）的持久化 I/O 底座，供 reconciler（#62）/ plugin
manager（#63）调用。承载两个文件：``known_marketplaces.json``（§6.1）与 ``installed_plugins.json``
（§6.2）；二者**带 ``version`` 字段**（区别于 settings.json 意图层 —— 后者无 version，见
:mod:`a2c_smcp.computer.settings.schema`）。
The durable I/O backbone of the materialized layer (CLI-maintained, hand-edit-forbidden), consumed
by the reconciler / plugin manager. Holds two files, both carrying a ``version`` field (unlike the
human-edited settings.json intent layer).

三条「优于 Claude Code 现状」的可靠性保证 / Three reliability guarantees (improving on CC):

- **原子写**：唯一临时文件 + ``fsync`` + atomic ``os.replace``——进程中途死不留半截 JSON。CC 用
  ``writeFileSync``（非原子、无锁，CC 开发者自评"反面教材"），A2C 不抄。/ Atomic write via unique
  tmp + ``fsync`` + ``os.replace`` (CC uses non-atomic ``writeFileSync``).
- **文件锁**：旁车 ``<path>.lock`` 排他锁（POSIX ``fcntl`` / win32 ``msvcrt``）防同用户多实例撕裂；
  退避重试仍拿不到 → 抛 :class:`SettingsLockError`（fail-fast，**绝不无锁写**）。POSIX 路径有单测；
  win32 ``msvcrt`` 路径为 best-effort、**未经 CI 验证**（无 Windows lane，对齐 policy.py win32 姿态）。
  读-改-写须用 :func:`update_known_marketplaces` / :func:`update_installed_plugins`（单把锁内 RMW），
  **不可**在持锁上下文内再调 ``save_*``。/ Sidecar exclusive lock; POSIX tested, win32 best-effort
  (not CI-verified). Use the ``update_*`` helpers for read-modify-write; never call ``save_*`` while
  holding the lock.
- **损坏恢复**：load 失败先备份 ``.corrupt-<ts>.bak`` **再**降级空配置（CC 静默重置无备份，紧接 save
  永久覆盖损坏数据）。/ On corruption, back up before degrading (CC silently resets without backup).

写保护：物化文件顶端写一行 JSONC 注释 :data:`WRITE_PROTECTION_HEADER`；读时容错剥离前导 ``//`` 行
再解析。发现手编痕迹（version 不符 / 未知顶层键）→ WARN + 用 in-memory 解析、下次 save 重写覆盖。
Write protection: a leading JSONC ``//`` header is written and tolerantly stripped on read; hand-edit
traces (version mismatch / unknown top-level keys) → WARN + in-memory parse + rewrite on next save.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from a2c_smcp.computer.settings.schema import GitSource
from a2c_smcp.computer.skills.home import resolve_skill_home
from a2c_smcp.utils.atomic_io import atomic_write_text
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 常量 / Constants
# ---------------------------------------------------------------------------
# 物化文件顶端写保护注释（JSONC 单行；读时被 _strip_jsonc_header 剥离）/ Write-protection header line.
WRITE_PROTECTION_HEADER = "// Maintained automatically by a2c-computer. DO NOT EDIT."

# 物化文件 schema 版本（区别于无 version 的 settings.json 意图层）/ Materialized-file schema version.
MATERIALIZED_VERSION = 1

KNOWN_MARKETPLACES_FILENAME = "known_marketplaces.json"
INSTALLED_PLUGINS_FILENAME = "installed_plugins.json"

# 文件锁退避默认参数（指数退避，封顶）/ Default lock backoff knobs (exponential, capped).
DEFAULT_LOCK_RETRIES = 10
DEFAULT_LOCK_BACKOFF_BASE = 0.05  # 秒 / seconds
DEFAULT_LOCK_BACKOFF_MAX = 1.0  # 秒 / seconds


class SettingsLockError(RuntimeError):
    """文件锁退避重试后仍无法获取 / The file lock could not be acquired after backoff retries (§6.3)."""


# ---------------------------------------------------------------------------
# 物化文件结构（IDE / 文档用；运行时容错见 load_* 的 _coerce_*）/ Materialized-file shapes
# ---------------------------------------------------------------------------
class MarketplaceRecord(TypedDict):
    """``known_marketplaces.json`` 单条 marketplace 物化记录（§6.1）/ a materialized marketplace record。"""

    source: GitSource
    installLocation: str  # 绝对路径 / absolute path
    lastUpdated: NotRequired[str]  # ISO-8601 UTC
    commitSha: NotRequired[str]
    autoUpdate: NotRequired[bool]


class KnownMarketplacesFile(TypedDict):
    """``known_marketplaces.json`` 整文件结构（§6.1）/ the full known_marketplaces.json shape。"""

    version: int
    marketplaces: dict[str, MarketplaceRecord]


class InstalledPluginRecord(TypedDict):
    """
    ``installed_plugins.json`` 单条安装记录（§6.2，对齐 CC V2 schema）/ a single plugin install record。

    数组化按 scope 维度（``scope`` + ``projectPath`` 精确匹配），为「user + workspace 同时装、不同版本」
    预留；``bundledMcpServers`` 为 A2C 扩展字段（CC 无），供 uninstall 级联精准清理 bundled MCP server。
    """

    scope: str  # managed | user | project | local
    installPath: str
    projectPath: NotRequired[str]  # project / local scope 必填 / required for project/local
    version: NotRequired[str]
    commitSha: NotRequired[str]
    installedAt: NotRequired[str]
    lastUpdated: NotRequired[str]
    bundledMcpServers: NotRequired[list[str]]  # A2C 扩展 / A2C extension


class InstalledPluginsFile(TypedDict):
    """``installed_plugins.json`` 整文件结构（§6.2）/ the full installed_plugins.json shape。"""

    version: int
    plugins: dict[str, list[InstalledPluginRecord]]


# ---------------------------------------------------------------------------
# 原子写 JSON / Atomic JSON write
# ---------------------------------------------------------------------------
# 原子写文本原语（唯一临时文件 + fsync + rename + 失败清理）抽到 a2c_smcp.utils.atomic_io，
# 与 blob/toolspool 共用（见 fix-review #58 §6）；此处仅做 JSON + JSONC 写保护头的封装。
# The atomic text-write primitive lives in a2c_smcp.utils.atomic_io (shared with blob/toolspool);
# here we only wrap JSON serialization + the JSONC write-protection header.
def atomic_write_json(path: Path, obj: Mapping[str, Any], *, header: str | None = None) -> None:
    """
    原子写 JSON（可选 JSONC 顶部写保护注释）/ Atomic JSON write with an optional JSONC header line。

    ``indent=2`` + ``ensure_ascii=False``（可读、保留非 ASCII）；末尾补换行。``header`` 写在首行
    （形如 ``// ...``），与 :func:`read_jsonc_with_recovery` 的前导 ``//`` 剥离对称。落盘经
    :func:`a2c_smcp.utils.atomic_io.atomic_write_text`（唯一临时文件 + ``fsync`` + ``os.replace``）。
    """
    body = json.dumps(obj, ensure_ascii=False, indent=2)
    text = f"{header}\n{body}\n" if header else f"{body}\n"
    atomic_write_text(path, text)


# ---------------------------------------------------------------------------
# 文件锁（旁车 .lock，跨平台）/ File lock (sidecar .lock, cross-platform)
# ---------------------------------------------------------------------------
def _lock_fd(fd: int) -> None:
    """
    对 fd 加排他非阻塞锁（POSIX ``fcntl.flock`` / win32 ``msvcrt.locking``）；被占抛 OSError。

    .. note::
       win32 分支为 best-effort、**未经 CI 验证**（无 Windows lane），与 policy.py 的 win32 注册表
       读取同姿态；POSIX ``fcntl`` 分支有单测覆盖。``msvcrt.locking`` 锁的是「当前位置起 N 字节」，
       对 0 字节锁文件在 offset 0 加锁偏脆弱——故先占位 1 字节再 ``seek(0)`` 规避。
    """
    if sys.platform == "win32":
        # 非 win32 上 mypy 据 sys.platform 收窄为不可达，故 msvcrt import 不被检查（无需 type:ignore）。
        import msvcrt

        # 确保锁文件 ≥1 字节并把位置归零，再锁 [0,1)；规避 0 字节文件 offset 0 加锁的脆弱惯用法。
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    """释放 fd 上的锁（best-effort，释放失败仅记 debug）/ Release the lock (best-effort)."""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:  # pragma: no cover - 释放失败极罕见，关闭 fd 时内核仍会释放
        logger.debug("Lock release on fd %d failed (ignored): %s", fd, exc)


@contextmanager
def file_lock(
    path: Path,
    *,
    retries: int = DEFAULT_LOCK_RETRIES,
    backoff_base: float = DEFAULT_LOCK_BACKOFF_BASE,
    backoff_max: float = DEFAULT_LOCK_BACKOFF_MAX,
) -> Iterator[None]:
    """
    获取 ``<path>.lock`` 旁车排他文件锁（§6.3）/ Acquire an exclusive lock on the sidecar ``<path>.lock``。

    锁加在**旁车** ``.lock`` 文件而非数据文件本身——数据文件每次写都被 ``os.replace`` 换掉，持有其
    inode 上的锁会随 rename 失效。被占时按**指数退避**重试 ``retries`` 次（每次 WARN，延迟封顶
    ``backoff_max``）；仍拿不到 → 抛 :class:`SettingsLockError`（fail-fast，绝不无锁写）。
    Locks a sidecar ``.lock`` (the data file is swapped by ``os.replace`` each write, so an inode
    lock on it would be lost). On contention, exponential backoff for ``retries`` attempts, then
    raises :class:`SettingsLockError`.

    :raises SettingsLockError: 退避重试后仍无法获取锁 / lock still unavailable after retries.
    """
    p = Path(path)
    lock_path = p.with_name(f"{p.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        delay = backoff_base
        last_exc: OSError | None = None
        for attempt in range(retries + 1):
            try:
                _lock_fd(fd)
                break
            except OSError as exc:  # 锁被其他进程占用 / held by another process
                last_exc = exc
                if attempt >= retries:
                    raise SettingsLockError(
                        f"could not acquire lock {lock_path} after {retries} retries",
                    ) from last_exc
                logger.warning(
                    "Lock %s busy (attempt %d/%d), backing off %.0fms",
                    lock_path,
                    attempt + 1,
                    retries,
                    delay * 1000,
                )
                time.sleep(delay)
                delay = min(delay * 2, backoff_max)
        try:
            yield
        finally:
            _unlock_fd(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# JSONC 读取 + 损坏恢复 / JSONC read + corruption recovery
# ---------------------------------------------------------------------------
def _strip_jsonc_header(text: str) -> str:
    """
    剥离**前导**整行 ``//`` 注释（写保护头）与前导空行 / Strip the leading ``//`` header + blank lines。

    仅处理文件顶部连续的整行注释 / 空行，遇首个内容行即停——不是通用 JSONC（不处理行尾 ``//``、块
    注释、字符串内 ``//``），够用且对数据零误伤。Only the file-top run of whole-line comments / blanks.
    """
    lines = text.splitlines()
    idx = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped == "" or stripped.startswith("//"):
            idx += 1
            continue
        break
    return "\n".join(lines[idx:])


def _backup_corrupt(path: Path) -> Path | None:
    """
    把损坏文件**移走**为 ``<name>.corrupt-<ts>.bak``（§6.3）/ Move a corrupt file aside to a ``.bak``。

    用 ``os.replace`` 整体移动（保留损坏内容到 .bak），原路径腾空——下次 save 重建全新文件，损坏数据
    不被静默永久覆盖。备份本身失败仅记 ERROR、返回 ``None``（不阻断降级）。
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.corrupt-{ts}.bak")
    try:
        os.replace(path, backup)
    except OSError as exc:
        logger.error("Failed to back up corrupt file %s → %s: %s", path, backup, exc)
        return None
    logger.warning("Corrupt materialized file %s backed up to %s, degrading to empty config", path, backup)
    return backup


def read_jsonc_with_recovery(path: Path) -> dict[str, Any] | None:
    """
    读取 JSONC 物化文件（容错前导 ``//`` 头）+ 损坏恢复 / Read a JSONC materialized file with recovery。

    - 文件缺失 → ``None``（调用方按空配置处理）/ missing → ``None``.
    - IO 读取错误 → ``None``、**不**备份（文件可能仍完好、只是暂时不可读）/ IO error → ``None`` (no backup).
    - JSON 解析失败 / 根非对象（损坏）→ 备份 ``.corrupt-<ts>.bak`` **再**返回 ``None``（§6.3）/ corrupt →
      back up then ``None``.
    - 成功 → 原始 dict（version / shape 校验交给上层 ``load_*``）/ ok → the raw dict.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Materialized file %s unreadable (kept on disk): %s", p, exc)
        return None
    try:
        data = json.loads(_strip_jsonc_header(raw))
    except json.JSONDecodeError as exc:
        logger.warning("Materialized file %s corrupt (%s), backing up before degrade", p, exc)
        _backup_corrupt(p)
        return None
    if not isinstance(data, dict):
        logger.warning("Materialized file %s root is not an object, backing up before degrade", p)
        _backup_corrupt(p)
        return None
    return data


# ---------------------------------------------------------------------------
# 路径解析 / Path resolution（基于 $A2C_SKILL_HOME）
# ---------------------------------------------------------------------------
def known_marketplaces_path(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """``$A2C_SKILL_HOME/known_marketplaces.json`` 路径 / Path to known_marketplaces.json。"""
    return (home or resolve_skill_home(env)) / KNOWN_MARKETPLACES_FILENAME


def installed_plugins_path(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """``$A2C_SKILL_HOME/installed_plugins.json`` 路径 / Path to installed_plugins.json。"""
    return (home or resolve_skill_home(env)) / INSTALLED_PLUGINS_FILENAME


# ---------------------------------------------------------------------------
# 手编痕迹容错 / Hand-edit tolerance（§6.3：WARN + in-memory + 下次 save 重写）
# ---------------------------------------------------------------------------
def _warn_hand_edit(path: Path, version: Any, known_keys: set[str], data: Mapping[str, Any]) -> None:
    """检测手编痕迹（version 不符 / 未知顶层键）并 WARN（不阻断、不丢已知字段）/ Warn on hand-edit traces。"""
    if version != MATERIALIZED_VERSION:
        logger.warning(
            "Materialized file %s has unexpected version %r (expected %d); using in-memory, will rewrite on next save",
            path,
            version,
            MATERIALIZED_VERSION,
        )
    unknown = set(data) - known_keys
    if unknown:
        logger.warning(
            "Materialized file %s has unexpected top-level keys %s (hand-edited?); ignoring them",
            path,
            sorted(unknown),
        )


def _coerce_known_marketplaces(data: Mapping[str, Any], path: Path) -> KnownMarketplacesFile:
    """把读到的 dict 规整为 :class:`KnownMarketplacesFile`（容错 + 手编 WARN）/ Coerce to the typed shape。"""
    _warn_hand_edit(path, data.get("version"), {"version", "marketplaces"}, data)
    marketplaces = data.get("marketplaces")
    if not isinstance(marketplaces, dict):
        if marketplaces is not None:
            logger.warning("Materialized file %s field 'marketplaces' is not an object; treating as empty", path)
        marketplaces = {}
    return {"version": MATERIALIZED_VERSION, "marketplaces": marketplaces}


def _coerce_installed_plugins(data: Mapping[str, Any], path: Path) -> InstalledPluginsFile:
    """把读到的 dict 规整为 :class:`InstalledPluginsFile`（容错 + 手编 WARN）/ Coerce to the typed shape。"""
    _warn_hand_edit(path, data.get("version"), {"version", "plugins"}, data)
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        if plugins is not None:
            logger.warning("Materialized file %s field 'plugins' is not an object; treating as empty", path)
        plugins = {}
    return {"version": MATERIALIZED_VERSION, "plugins": plugins}


# ---------------------------------------------------------------------------
# 高层读写 / High-level load & save
# ---------------------------------------------------------------------------
def empty_known_marketplaces() -> KnownMarketplacesFile:
    """空的 known_marketplaces 物化结构 / An empty known_marketplaces file。"""
    return {"version": MATERIALIZED_VERSION, "marketplaces": {}}


def empty_installed_plugins() -> InstalledPluginsFile:
    """空的 installed_plugins 物化结构 / An empty installed_plugins file。"""
    return {"version": MATERIALIZED_VERSION, "plugins": {}}


def load_known_marketplaces(home: Path | None = None, env: Mapping[str, str] | None = None) -> KnownMarketplacesFile:
    """
    加载 ``known_marketplaces.json``（缺失 / 损坏 → 空配置）/ Load known_marketplaces.json (missing/corrupt → empty)。

    损坏文件先备份 ``.corrupt-<ts>.bak`` 再降级（见 :func:`read_jsonc_with_recovery`）。
    """
    path = known_marketplaces_path(home, env)
    data = read_jsonc_with_recovery(path)
    if data is None:
        return empty_known_marketplaces()
    return _coerce_known_marketplaces(data, path)


def load_installed_plugins(home: Path | None = None, env: Mapping[str, str] | None = None) -> InstalledPluginsFile:
    """加载 ``installed_plugins.json``（缺失 / 损坏 → 空配置）/ Load installed_plugins.json (missing/corrupt → empty)。"""
    path = installed_plugins_path(home, env)
    data = read_jsonc_with_recovery(path)
    if data is None:
        return empty_installed_plugins()
    return _coerce_installed_plugins(data, path)


def save_known_marketplaces(
    data: Mapping[str, Any],
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """
    加锁 + 原子写 ``known_marketplaces.json``（带写保护头 + ``version``）/ Locked atomic write。

    缺省补齐 ``version`` 字段（物化文件强制带 version，§6）；返回写入路径。锁不可得 → 抛
    :class:`SettingsLockError`（不无锁写）。

    ⚠️ **不可**在已持有 :func:`file_lock` 的上下文内调用（同进程新 fd 的 flock 互斥 →
    :class:`SettingsLockError`）；读-改-写请改用 :func:`update_known_marketplaces`。
    ⚠️ Do not call inside a held :func:`file_lock` (same-process flock is mutually exclusive); use
    :func:`update_known_marketplaces` for read-modify-write.
    """
    path = known_marketplaces_path(home, env)
    payload: dict[str, Any] = dict(data)
    payload.setdefault("version", MATERIALIZED_VERSION)
    with file_lock(path):
        atomic_write_json(path, payload, header=WRITE_PROTECTION_HEADER)
    return path


def save_installed_plugins(
    data: Mapping[str, Any],
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """
    加锁 + 原子写 ``installed_plugins.json``（带写保护头 + ``version``）/ Locked atomic write。

    ⚠️ **不可**在已持有 :func:`file_lock` 的上下文内调用（同 :func:`save_known_marketplaces`）；
    读-改-写请改用 :func:`update_installed_plugins`。
    """
    path = installed_plugins_path(home, env)
    payload: dict[str, Any] = dict(data)
    payload.setdefault("version", MATERIALIZED_VERSION)
    with file_lock(path):
        atomic_write_json(path, payload, header=WRITE_PROTECTION_HEADER)
    return path


# ---------------------------------------------------------------------------
# 持锁原子读-改-写 / Locked atomic read-modify-write（供 reconciler #62 等）
# ---------------------------------------------------------------------------
def update_known_marketplaces(
    mutator: Callable[[KnownMarketplacesFile], KnownMarketplacesFile | None],
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> KnownMarketplacesFile:
    """
    持锁原子读-改-写 ``known_marketplaces.json``（§6.3）/ Locked atomic read-modify-write。

    **单把锁**覆盖 load→mutate→save，杜绝并发进程间的丢更新窗口——供 reconciler（#62）等 RMW 场景。
    ``mutator`` 收到当前（已规整；缺失 / 损坏则为空）结构，可**就地改并返回 ``None``**，或返回新结构；
    二者皆补齐 ``version`` 后原子落盘。返回最终写入的结构。
    A single lock spans load→mutate→save (no lost-update window) — for the reconciler etc. The
    mutator may mutate in place and return ``None``, or return a new structure; either way
    ``version`` is backfilled and atomically written. Returns the final written structure.

    这是「持锁 RMW」的推荐入口；**不要**自行 ``with file_lock(p): ...; save_*(...)``（save_* 会再次
    加锁 → :class:`SettingsLockError`，见 :func:`save_known_marketplaces`）。
    """
    path = known_marketplaces_path(home, env)
    with file_lock(path):
        raw = read_jsonc_with_recovery(path)
        current = empty_known_marketplaces() if raw is None else _coerce_known_marketplaces(raw, path)
        result = mutator(current)
        to_save: dict[str, Any] = dict(result if result is not None else current)
        to_save.setdefault("version", MATERIALIZED_VERSION)
        atomic_write_json(path, to_save, header=WRITE_PROTECTION_HEADER)
    return _coerce_known_marketplaces(to_save, path)


def update_installed_plugins(
    mutator: Callable[[InstalledPluginsFile], InstalledPluginsFile | None],
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> InstalledPluginsFile:
    """持锁原子读-改-写 ``installed_plugins.json``（语义同 :func:`update_known_marketplaces`）/ Locked atomic RMW。"""
    path = installed_plugins_path(home, env)
    with file_lock(path):
        raw = read_jsonc_with_recovery(path)
        current = empty_installed_plugins() if raw is None else _coerce_installed_plugins(raw, path)
        result = mutator(current)
        to_save: dict[str, Any] = dict(result if result is not None else current)
        to_save.setdefault("version", MATERIALIZED_VERSION)
        atomic_write_json(path, to_save, header=WRITE_PROTECTION_HEADER)
    return _coerce_installed_plugins(to_save, path)
