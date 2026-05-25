# -*- coding: utf-8 -*-
# filename: registry.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Skill Registry：name → A2CSkillRef 物化索引（v0.2.1）
Skill Registry: name → A2CSkillRef materialized index (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §1.5（校验失败不入册）、
                      §6（A2CSkillRef）、§8（变更检测 / 孤儿）、§9.2（name 寻址防越权）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §5（registry）。

职责 / Responsibilities：
- **`name → A2CSkillRef` O(1) 精确匹配**：Registry 是 name→包根路径的**唯一**映射来源（§9.2
  「name 寻址防越权」——禁止从 name 推导 FS 路径；包根仅由本 Registry 经 name 解析）。
  The single source of truth for name→package-root resolution (no FS-path derivation from name).
- **孤儿标记 / 恢复**：source 消失时把对应 SKILL 标为 orphan（从 `get_skills` 排除，但保留以便
  source 回归时恢复）；orphan 对 `get_skill` / `get_blob` 等同「不存在」（协议复用 `4014` / `4018`）。
  Orphan marking/recovery: source-gone SKILLs are excluded from `get_skills` yet retained for recovery.
- **校验失败不入册**：name 非法（lexer）/ 缺包根绝对路径 / 同名 active 冲突 → 记 ERROR、**不**注册、
  **绝不**抛出（§1.5：SKILL 通道 batch 接口对部分失败健壮）。
  Validation failures are logged ERROR and skipped — never raised.

线程模型 / Threading：Computer 单进程 asyncio 单线程驱动（事件处理器串行），故本 Registry 不加锁。
Single-threaded asyncio within one Computer process — no locking.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from a2c_smcp.computer.skills.naming import SkillNameError, parse_skill_name
from a2c_smcp.smcp import A2CSkillRef
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class _RegistryEntry:
    """Registry 内部条目 / Internal registry entry：物化 ref + 孤儿标记。"""

    ref: A2CSkillRef
    orphaned: bool = False


class SkillRegistry:
    """
    name → A2CSkillRef 的内存物化索引 / In-memory materialized index of name → A2CSkillRef。

    由 staging 在物化后调用 :meth:`register` 入册；变更检测（§8）经 :meth:`mark_orphan` /
    :meth:`recover` / :meth:`unregister` 维护活跃集；`client:get_skills` 取 :meth:`active_refs`，
    `client:get_skill` / `client:get_blob` 经 :meth:`resolve` 拿包根（orphan 视为不存在）。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}

    # ── 写入 / mutation ───────────────────────────────────────────────────
    def register(self, ref: A2CSkillRef) -> bool:
        """
        入册一个物化 SKILL / Register a materialized SKILL。

        校验（任一失败 → 记 ERROR、返回 ``False``、**不**抛）/ Validation (any failure → ERROR + False):
        1. ``name`` 经 §1 lexer（:func:`parse_skill_name`）；
        2. ``path`` 必选且为**绝对**路径（name 寻址唯一来源 / S2 沙箱包根）；
        3. 同名**活跃**条目已存在 → §1.5「拒绝第二注册者、保留先到者」。

        同名**孤儿**条目存在 → 视为 source 回归：用新 ref 替换并清除 orphan 标记（恢复），返回 ``True``。

        :return: 是否成功入册 / whether registered (or recovered).
        """
        name = ref.get("name")
        if not name:
            logger.error("SkillRegistry.register skipped: missing 'name' in ref=%r", ref)
            return False

        path = ref.get("path")
        if not path or not Path(path).is_absolute():
            logger.error("SkillRegistry.register skipped name=%r: 'path' missing or not absolute (got %r)", name, path)
            return False

        try:
            parse_skill_name(name)
        except SkillNameError as e:
            logger.error("SkillRegistry.register skipped: invalid name %r (%s)", name, e.reason)
            return False

        existing = self._entries.get(name)
        if existing is not None and not existing.orphaned:
            logger.error("SkillRegistry.register skipped: name %r already registered (keeping first registrant)", name)
            return False

        if existing is not None:  # 孤儿恢复 / orphan recovery
            logger.debug("SkillRegistry recovered orphaned skill name=%r", name)
        self._entries[name] = _RegistryEntry(ref=ref, orphaned=False)
        return True

    def mark_orphan(self, name: str) -> bool:
        """把已注册 SKILL 标为孤儿（从活跃集排除，保留以便恢复）/ Mark a registered SKILL orphaned."""
        entry = self._entries.get(name)
        if entry is None:
            return False
        if not entry.orphaned:
            entry.orphaned = True
            logger.debug("SkillRegistry marked orphan name=%r", name)
        return True

    def recover(self, name: str) -> bool:
        """恢复孤儿 SKILL（重新纳入活跃集）/ Recover an orphaned SKILL into the active set."""
        entry = self._entries.get(name)
        if entry is None or not entry.orphaned:
            return False
        entry.orphaned = False
        logger.debug("SkillRegistry recovered orphan name=%r", name)
        return True

    def unregister(self, name: str) -> bool:
        """彻底移除一个 SKILL（孤儿与活跃皆可）/ Remove a SKILL entirely (active or orphaned)."""
        return self._entries.pop(name, None) is not None

    # ── 读取 / lookup ─────────────────────────────────────────────────────
    def resolve(self, name: str) -> A2CSkillRef | None:
        """
        O(1) 活跃精确匹配 / O(1) active exact lookup。

        仅返回**活跃**（非孤儿）条目的 ref；name 未注册或已孤儿 → ``None``（调用方据此回 `4014` /
        `4018`）。这是 name→包根的唯一解析入口（§9.2）。
        Returns the ref only for an active (non-orphan) entry; ``None`` otherwise.
        """
        entry = self._entries.get(name)
        return entry.ref if entry is not None and not entry.orphaned else None

    def active_refs(self) -> list[A2CSkillRef]:
        """活跃 SKILL 的 ref 列表（排除孤儿）/ Active SKILL refs（`client:get_skills`；不排序、不去重）。"""
        return [entry.ref for entry in self._entries.values() if not entry.orphaned]

    def is_orphan(self, name: str) -> bool:
        """name 是否为已注册的孤儿条目 / Whether name is a registered-but-orphaned entry。"""
        entry = self._entries.get(name)
        return entry is not None and entry.orphaned

    def __contains__(self, name: object) -> bool:
        """name 是否已注册（含孤儿）/ Whether name is registered (active or orphaned)。"""
        return name in self._entries

    def __len__(self) -> int:
        """已注册条目总数（含孤儿）/ Total registered entries (active + orphaned)。"""
        return len(self._entries)
