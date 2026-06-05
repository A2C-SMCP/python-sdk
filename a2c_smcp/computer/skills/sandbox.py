# -*- coding: utf-8 -*-
# filename: sandbox.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 包根沙箱 / SKILL package-root sandbox (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §9 安全模型；
                      error-handling.md §4017（Skill Resource Not Accessible）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §5 / §5.3。

职责 / Responsibility:
- ``safe_join`` + ``realpath`` 包根内校验：``rel_path`` 必须相对、无 ``..``、解析符号链接后仍落在
  包根内；绝对路径 / ``..`` / symlink 逃逸 → ``traversal``。
- ``.skillenv`` 等敏感凭证文件**任何 ``rel_path`` 下**命中即拒（``forbidden``），且**不泄漏存在性**
  （存在与不存在返回同一 reason，命中判定纯词法、不 ``stat``）。
- **name 寻址防越权**：本模块只收**包根绝对路径**（由 Registry 经 name 解析得到），**绝不**接收
  SKILL name、**绝不**从 name / ``rel_path`` 推导 FS 路径——结构上杜绝越权寻址（skill.md §9.2 安全铁律）。

所有违规经 :class:`SkillSandboxError` 表达，``reason`` 直映协议 4017 ``details.reason`` 开放枚举
（``traversal`` / ``forbidden`` / ``not_found`` / ``too_large``）；调用方（``client:get_skill`` 处理器 /
blob ``kind=skill`` resolver）据此构造 flat ``ErrorPayload`` 的 ``details``。

This module is deliberately ``name``-free: it accepts an already-resolved package **root** path
(obtained by the caller from the Skill Registry via name) plus a relative ``rel_path``, and never
derives a filesystem path from a SKILL name — structurally enforcing the §9.2 addressing invariant.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Literal

from a2c_smcp.utils.path import is_within

# rel_path 缺省 = 包根入口 SKILL.md / default rel_path = package-root entry SKILL.md（skill.md §6 / §9）。
DEFAULT_SKILL_FILE = "SKILL.md"

# 敏感凭证文件默认黑集 / default sensitive-credential basenames。
# 协议只明文命名 ``.skillenv``；"及 Computer 判定的凭证类文件"（error-handling.md §4017）留给调用方
# 经 ``forbidden_names`` 注入扩展，避免在 SDK 内硬编码猜测性策略（对齐 no-overengineering 约定）。
# Protocol names only ``.skillenv``; the open-ended "and Computer-judged credential files" is left to
# callers via ``forbidden_names`` rather than guessing a broader built-in set.
FORBIDDEN_SKILL_FILES: frozenset[str] = frozenset({".skillenv"})

# 4017 ``details.reason`` 开放枚举 / open enum for protocol 4017 ``details.reason``。
SkillSandboxReason = Literal["traversal", "forbidden", "not_found", "too_large"]


class SkillSandboxError(Exception):
    """
    SKILL 包内资源不可访问 / SKILL in-package resource not accessible.

    映射协议 ``4017 Skill Resource Not Accessible``（error-handling.md §4017）。``reason`` 为开放枚举，
    ``rel_path`` 回显请求路径，``total_size`` 仅 ``too_large`` 携带——三者直填 4017 ``details``。
    Maps to protocol ``4017``; ``reason`` is an open enum, ``rel_path`` echoes the request, and
    ``total_size`` is set only for ``too_large`` — all three populate the 4017 ``details`` object.

    本异常**不**自带协议码常量（保持 sandbox 模块对 ``a2c_smcp.smcp`` 零依赖，对齐 ``naming.SkillNameError``）；
    由调用方按需映射到 :data:`a2c_smcp.smcp.SMCPErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE`。
    This exception embeds no protocol-code constant (keeps the module dependency-free of
    ``a2c_smcp.smcp``, mirroring ``naming.SkillNameError``); callers map it as appropriate.
    """

    def __init__(self, reason: SkillSandboxReason, *, rel_path: str = "", total_size: int | None = None) -> None:
        self.reason: SkillSandboxReason = reason
        self.rel_path = rel_path
        self.total_size = total_size
        super().__init__(f"skill resource not accessible (reason={reason}, rel_path={rel_path!r})")


def _guard_lexical(rel: str) -> PurePosixPath:
    """
    触 FS 前的纯词法 traversal 守卫 / Pre-FS lexical traversal guard。

    拒绝：反斜杠（非 POSIX 分隔符，防 Windows 路径走私）/ 绝对路径 / 含 ``..`` 段 / 段内含 ``:``
    （盘符 / scheme 走私）——任一命中 → :class:`SkillSandboxError` ``traversal``。返回解析后的
    :class:`PurePosixPath`（``.`` 段已被 PurePosixPath 规整掉）。
    """
    # 反斜杠不是 POSIX 分隔符；存在即视为非法（防 ``..\`` / ``C:\`` 等 Windows 形态绕过词法检查）。
    if "\\" in rel:
        raise SkillSandboxError("traversal", rel_path=rel)
    pure = PurePosixPath(rel)
    if pure.is_absolute():
        raise SkillSandboxError("traversal", rel_path=rel)
    for part in pure.parts:
        if part == ".." or ":" in part:
            raise SkillSandboxError("traversal", rel_path=rel)
    return pure


def resolve_skill_resource(
    root: Path,
    rel_path: str | None = None,
    *,
    forbidden_names: Collection[str] = FORBIDDEN_SKILL_FILES,
) -> Path:
    """
    在包根 ``root`` 内安全解析 ``rel_path``，返回已 ``resolve()`` 的资源绝对路径。
    Safely resolve ``rel_path`` within package root ``root``; returns the ``resolve()``-d absolute path.

    判定顺序（顺序保证「forbidden 不泄漏存在性」与「触 FS 前先挡 traversal」）/ Decision order:
      1. ``rel_path`` 缺省 / 空 → :data:`DEFAULT_SKILL_FILE`（``SKILL.md``）。
      2. 词法 traversal 守卫（绝对 / ``..`` / 反斜杠 / 盘符，**触 FS 前**）→ ``traversal``。
      3. **forbidden 先于存在性**：目标 basename（大小写不敏感）命中 ``forbidden_names`` → ``forbidden``，
         **不 ``stat``、不泄漏存在性**（存在与不存在同一 reason）。
      4. ``realpath`` 容器校验：``(root/rel).resolve()`` 必须仍在 ``root.resolve()`` 内，否则 ``traversal``
         （捕 symlink 逃逸）；对 realpath 后的 basename 复检 forbidden（symlink → ``.skillenv``）。
      5. 目标须为既存普通文件，否则 ``not_found``（含不存在 / 目录）。

    :param root: SKILL 包根绝对路径，**仅**由 Registry 经 name 解析得到（本函数不接收 name）。
    :param rel_path: 包根 POSIX 相对路径；``None`` / ``""`` 取 ``SKILL.md``。
    :param forbidden_names: 敏感文件 basename 集合（大小写不敏感比较，防大小写不敏感 FS 绕过）。
    :raises SkillSandboxError: ``reason`` ∈ ``traversal`` / ``forbidden`` / ``not_found``。
    """
    rel = DEFAULT_SKILL_FILE if not rel_path else rel_path

    # ② 词法守卫（触 FS 前） / lexical guard before touching the filesystem.
    pure = _guard_lexical(rel)

    # 大小写不敏感比较：macOS/APFS、Windows 默认大小写不敏感，``.SKILLENV`` 会落到同一文件，
    # 故按 lower 比较以堵死大小写绕过 / case-insensitive to close case-folding FS bypass.
    forbidden_lower = {n.lower() for n in forbidden_names}

    # ③ forbidden 先于任何 FS 访问（纯词法，不泄漏存在性） / forbidden before any FS access.
    if pure.name.lower() in forbidden_lower:
        raise SkillSandboxError("forbidden", rel_path=rel)

    # ④ realpath 容器校验（捕 symlink 逃逸） / realpath containment (catches symlink escape).
    root_real = root.resolve()
    target_real = (root_real / pure).resolve()
    if not is_within(target_real, root_real):
        raise SkillSandboxError("traversal", rel_path=rel)
    # symlink 指向 ``.skillenv`` 等：realpath 后 basename 复检 / re-check after symlink resolution.
    if target_real.name.lower() in forbidden_lower:
        raise SkillSandboxError("forbidden", rel_path=rel)

    # ⑤ 既存普通文件 / must be an existing regular file（不存在 / 目录 → not_found）。
    if not target_real.is_file():
        raise SkillSandboxError("not_found", rel_path=rel)

    return target_real


def ensure_within_size_cap(path: Path, cap: int, *, rel_path: str = "") -> int:
    """
    绝对上限校验 / Absolute size-cap check：``path`` 字节数 > ``cap`` → ``4017 too_large``（不铸句柄）。
    Returns the file size when within cap.

    协议依据 / Protocol: skill.md §9「铸造期决断 too_large，不铸句柄、零字节传输（防 DoS）」。触发点由
    ``client:get_skill`` 铸造边界（#66）调用——本函数只负责 size 判定，``cap`` 为 SDK 可配上限。
    The minting boundary decides ``too_large``; this helper only performs the size comparison.

    TOCTOU：本函数的 ``stat`` 与 :func:`resolve_skill_resource` 的 ``is_file()`` 各一次，二者间存在窗口；
    属可接受——协议 §9 要求每次 ``client:get_blob`` 解析时**重跑整套沙箱**，铸造期单点 TOCTOU 不构成持久漏洞。
    The race between this ``stat`` and the resolver's ``is_file()`` is tolerated: §9 mandates re-running
    the full sandbox on every ``client:get_blob`` resolution, so a momentary mint-time race is not durable.

    :param path: 已经 :func:`resolve_skill_resource` 通过沙箱的资源路径。
    :param cap: SDK 可配的内联 / 句柄绝对字节上限。
    :param rel_path: 回显到异常的请求路径（便于 4017 ``details.rel_path``）。
    :raises SkillSandboxError: ``reason="too_large"``，携带 ``total_size``。
    """
    size = path.stat().st_size
    if size > cap:
        raise SkillSandboxError("too_large", rel_path=rel_path, total_size=size)
    return size
