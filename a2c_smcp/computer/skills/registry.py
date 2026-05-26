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
- **生命周期三动作**（§8 变更检测）/ Three lifecycle ops：
    - :meth:`register` —— **新增**（含 source 回归时的孤儿恢复）；同名 **active** 冲突按 §1.5 拒绝。
    - :meth:`update`   —— **刷新**已注册 SKILL 的 ref（§8.2 ResourceUpdated：description/version/sha256
      等 frontmatter 派生值变化），原地替换；name 未注册 → 拒绝（新增走 register）。
    - :meth:`mark_orphan` / :meth:`recover` —— source 消失/回归时切换孤儿态（从 `get_skills` 排除/纳入）。
- **校验失败不入册**：name 非法（lexer）/ 缺包根绝对路径 / 同名 active 冲突 → 记 ERROR、**不**写入、
  **绝不**抛出（§1.5：SKILL 通道 batch 接口对部分失败健壮）。
  Validation failures are logged ERROR and skipped — never raised.
- **读取返回深拷贝**：:meth:`resolve` / :meth:`active_refs` 返回 ref 的 **deepcopy**，调用方（响应构造、
  frontmatter 剥离等）可安全就地改写而不污染 Registry 内部状态（A2CSkillRef 运行时为可变 dict）。
  Reads return deep copies so callers may mutate freely without aliasing Registry state.

线程模型 / Threading：Computer 单进程 asyncio 单线程驱动（事件处理器串行），故本 Registry 不加锁。
Single-threaded asyncio within one Computer process — no locking.
"""

from __future__ import annotations

import copy
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

    由 staging 在物化后调用 :meth:`register` 入册、:meth:`update` 刷新；变更检测（§8）经
    :meth:`mark_orphan` / :meth:`recover` / :meth:`unregister` 维护活跃集；`client:get_skills`
    取 :meth:`active_refs`，`client:get_skill` / `client:get_blob` 经 :meth:`resolve` 拿包根
    （orphan 视为不存在）。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}

    def _validated_name(self, ref: A2CSkillRef, *, op: str) -> str | None:
        """
        共享入参校验 / Shared input validation：name 非空 + §1 lexer 合法 + path 必选且绝对。

        任一失败 → 记 ERROR、返回 ``None``（调用方据此 no-op，**不**抛）。
        """
        name = ref.get("name")
        if not name:
            logger.error("SkillRegistry.%s skipped: missing 'name' in ref=%r", op, ref)
            return None
        path = ref.get("path")
        if not path or not Path(path).is_absolute():
            logger.error("SkillRegistry.%s skipped name=%r: 'path' missing or not absolute (got %r)", op, name, path)
            return None
        try:
            parse_skill_name(name)
        except SkillNameError as e:
            logger.error("SkillRegistry.%s skipped: invalid name %r (%s)", op, name, e.reason)
            return None
        return name

    # ── 写入 / mutation ───────────────────────────────────────────────────
    def register(self, ref: A2CSkillRef) -> bool:
        """
        **新增**一个物化 SKILL / Register a newly materialized SKILL。

        校验（任一失败 → ERROR + ``False`` + no-op）见 :meth:`_validated_name`。校验通过后：
        - 同名条目不存在 → 写入活跃条目；
        - 同名 **active** 条目存在 → §1.5「拒绝第二注册者、保留先到者」→ ERROR + ``False``；
        - 同名 **orphan** 条目存在 → 视为 source 回归：用新 ref 替换并清除 orphan（恢复）→ ``True``。

        **活跃 SKILL 的内容刷新**（§8.2 ResourceUpdated）请用 :meth:`update`，**不要**用 register
        （否则命中 active 冲突分支被静默拒绝、保留旧 ref）。
        """
        name = self._validated_name(ref, op="register")
        if name is None:
            return False

        existing = self._entries.get(name)
        if existing is not None and not existing.orphaned:
            logger.error("SkillRegistry.register skipped: name %r already registered (keeping first registrant)", name)
            return False

        if existing is not None:  # 孤儿恢复 / orphan recovery
            logger.debug("SkillRegistry recovered orphaned skill via register name=%r", name)
        self._entries[name] = _RegistryEntry(ref=ref, orphaned=False)
        return True

    def update(self, ref: A2CSkillRef) -> bool:
        """
        **刷新**已注册 SKILL 的 ref / Refresh the ref of an already-registered SKILL（§8.2 ResourceUpdated）。

        原地替换同名条目的 ref（active 或 orphan 皆可，替换后置为**活跃**——ResourceUpdated 蕴含该
        SKILL 当前存在）。name **未注册** → ERROR + ``False``（更新不创建新条目；新增走 :meth:`register`）。
        校验同 :meth:`register`（失败 → ERROR + ``False``，**不**改动既有条目）。
        """
        name = self._validated_name(ref, op="update")
        if name is None:
            return False
        if name not in self._entries:
            logger.error("SkillRegistry.update skipped: name %r not registered (use register for new skills)", name)
            return False
        self._entries[name] = _RegistryEntry(ref=ref, orphaned=False)
        return True

    def register_or_update(self, ref: A2CSkillRef) -> bool:
        """
        幂等写入：已注册同名 → :meth:`update`（刷新 + 孤儿恢复），否则 :meth:`register` / Idempotent upsert。

        staging 重扫 / 跨 run 复现的统一入口（mcp 与 user 源共用），避免各处重复
        ``update(ref) if name in registry else register(ref)`` 惯用法。``name`` 缺失 / 未注册走 register
        分支，由其校验兜底（失败 → ERROR + ``False``）。
        """
        name = ref.get("name")
        return self.update(ref) if name is not None and name in self._entries else self.register(ref)

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

    # ── 读取 / lookup（返回深拷贝，调用方可安全改写）──────────────────────
    def resolve(self, name: str) -> A2CSkillRef | None:
        """
        O(1) 活跃精确匹配 / O(1) active exact lookup。

        仅返回**活跃**（非孤儿）条目 ref 的**深拷贝**；name 未注册或已孤儿 → ``None``（调用方据此回
        `4014` / `4018`）。这是 name→包根的唯一解析入口（§9.2）。返回深拷贝，调用方就地改写不影响 Registry。
        Returns a deep copy of the ref for an active (non-orphan) entry; ``None`` otherwise.
        """
        entry = self._entries.get(name)
        if entry is None or entry.orphaned:
            return None
        return copy.deepcopy(entry.ref)

    def active_refs(self) -> list[A2CSkillRef]:
        """
        活跃 SKILL 的 ref 深拷贝列表（排除孤儿）/ Deep-copied active SKILL refs。

        供 `client:get_skills`（不排序、不去重）；逐个深拷贝，调用方构造响应/剥字段不污染 Registry。
        """
        return [copy.deepcopy(entry.ref) for entry in self._entries.values() if not entry.orphaned]

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
