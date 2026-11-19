# -*- coding: utf-8 -*-
# filename: marketplace_identity.py
# @Time    : 2026/11/18
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Marketplace 注册身份规则（v0.5.1 #260）/ Marketplace registration identity rules.

**单一权威**：注册名派生（清单顶层 ``name``）、同源判定、注册唯一性/改名判定全部收口在本模块，CLI 与
staging 只消费、不各自实现。

协议依据 / Protocol refs: ``skill.md §1.2`` note + ``runtime-contract §5.14``（名称唯一性 / 禁静默改名由
协议管辖；派生算法 SDK 自治，见 protocol#79）。本模块不定义 wire 形态，无协议变更。

三条不变量 / Three invariants:
  1. 一个注册名在一个持久化 home 内**至多绑定一个 source**；同名不同源 MUST 失败（``validation``）。
  2. refresh 观测到清单名与 ``manifestNameBaseline`` 不符 MUST 拒绝并保留旧注册（不静默改名）。
  3. 同一 source 已以旧名注册时，以新名再注册 MUST 拒绝（诊断给旧名与处置）。

**同源判定刻意 fail-closed**：只有两侧都形如 ``{"type": "git", "url": <同一字符串>}``（仅去首尾空白）才算
同源；``ssh://`` / ``https://`` / scp-like / ``.git`` 后缀等等价写法**不归一**——协议明文「无法确定时 MUST
fail-closed」（§5.14），宁可拒绝也不静默换源。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from a2c_smcp.computer.settings.schema import is_valid_marketplace_name
from a2c_smcp.computer.skills.manifest import read_marketplace_manifest
from a2c_smcp.utils.git_url import (
    REDACTED_GIT_SOURCE,
    diagnostic_url_for_display,
    redact_git_urls_in_text,
    sanitize_text_for_diagnostics,
)
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# ``known_marketplaces.json`` 记录内的 baseline 字段名（#260 契约，与 rust-sdk#289 提案对齐）/
# Baseline field inside a known_marketplaces record (contract with rust-sdk#289).
#
# 语义：该注册**锚定**的清单顶层 ``name``。add / 首次 clone 必写（= 注册名）；legacy 记录（无该键）首次
# 成功 clone/refresh 补写观测值并保留旧注册名；已有该键时**永不改写**。``baseline != 注册键`` 即 #268 的
# legacy 判据之一。缺省键 = 旧账本，按「首次观测」处理。
MANIFEST_NAME_BASELINE_FIELD = "manifestNameBaseline"

# 机器可读原因码（结构化错误的 ``reason`` 键）/ Machine-readable reasons.
REASON_MANIFEST_NAME_INVALID = "manifest-name-invalid"
REASON_MANIFEST_NAME_CHANGED = "manifest-name-changed"
REASON_NAME_CONFLICT = "name-conflict"
REASON_SOURCE_CONFLICT = "source-conflict"
REASON_EXPECTED_NAME_MISMATCH = "expected-name-mismatch"
REASON_DANGLING_REFERENCES = "dangling-references"
REASON_UNRESOLVABLE_NAME = "unresolvable-name"
REASON_INVALID_NAME = "invalid-name"
REASON_INVALID_SOURCE = "invalid-source"


class MarketplaceIdentityError(Exception):
    """
    Marketplace 注册身份违约（结构化 ``validation``）/ A marketplace identity violation (structured validation).

    :param reason: :data:`REASON_*` 之一（机器可读，结构化 payload 的 ``reason`` 键）。
    :param details: 结构化诊断；**调用方只放脱敏值**（合法 kebab 名可回显；非法清单名只给固定文案；
        source 一律经 :func:`~a2c_smcp.utils.git_url.diagnostic_url_for_display`，账本键经
        :func:`redact_ledger_key`）。
    """

    def __init__(self, message: str, *, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.error_code = "validation"
        self.details: dict[str, Any] = dict(details or {})

    def payload_details(self) -> dict[str, Any]:
        """
        CLI / 库消费方的结构化诊断（机读键 ``reason``）/ Diagnostics for structured error payloads.

        **机读键统一用 ``reason``**（不用 ``code``）：plugin 侧 ``_err`` 的 ``code`` 是退出码保留参数，
        用 ``code`` 会让 enable 路径的改名拒绝丢失机读原因（#260 独立审查 🟡8）。
        """
        return {"reason": self.reason, **self.details}


class MarketplaceNameChangeError(MarketplaceIdentityError):
    """
    清单改名拒绝（§5.14）——**唯一**从 staging 提交点抛出的异常类型 / The single rename-rejection exception.

    单列子类是为了让调用方一眼区分「改名拒绝」（必须报 ``validation``、不得报 updated/unchanged）与普通
    降级失败；其余身份违约只在 add 的严格路径抛 :class:`MarketplaceIdentityError`。
    """

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, reason=REASON_MANIFEST_NAME_CHANGED, details=details)


# ── 清单名 / manifest name ────────────────────────────────────────────────────
def manifest_marketplace_name(manifest: Mapping[str, Any]) -> str:
    """
    取并校验清单顶层 ``name``（严格 kebab，1–64）/ Read & validate the manifest's top-level ``name``.

    :raises MarketplaceIdentityError: 缺失 / 非字符串 / 空白 / 不合严格 kebab。**错误文案不回显原值**——
        清单来自不可信仓库（#263 同口径：非法身份值只给固定文案，避免把凭据样文本写进 stdout/日志）。
    """
    raw = manifest.get("name")
    if not isinstance(raw, str) or not raw.strip():
        raise MarketplaceIdentityError(
            "marketplace manifest has no usable top-level 'name' (must be a non-empty string)",
            reason=REASON_MANIFEST_NAME_INVALID,
        )
    name = raw.strip()
    if not is_valid_marketplace_name(name):
        raise MarketplaceIdentityError(
            "marketplace manifest top-level 'name' is not strict kebab-case (1-64 chars, [a-z0-9-], "
            "no leading/trailing '-' and no '--')",
            reason=REASON_MANIFEST_NAME_INVALID,
        )
    return name


def read_manifest_marketplace_name(clone_dir: Path) -> str:
    """
    读 ``<clone>/.tfrobot-plugin/marketplace.json`` 的顶层 ``name`` / Read the manifest name from a clone.

    :raises MarketplaceIdentityError: 清单缺失 / JSON 畸形 / 无合法 ``name``（统一归 ``manifest-name-invalid``，
        与「无法在注册前确定名称」的 §5.14 语义一致）。
    """
    try:
        manifest = read_marketplace_manifest(clone_dir)
    except Exception as e:  # noqa: BLE001 - 清单不可读统一转身份违约（含路径脱敏由各字段分别处理）
        # 只记类型与固定文案：清单路径可能落在用户 home 下，且异常文本由 manifest 层决定（#263 同姿态）。
        logger.error("marketplace manifest unreadable at %s: %s", type(e).__name__, e)
        raise MarketplaceIdentityError(
            "marketplace manifest is missing or unreadable; cannot determine the registration name",
            reason=REASON_MANIFEST_NAME_INVALID,
        ) from e
    return manifest_marketplace_name(manifest)


# ── 同源判定 / same-source predicate ─────────────────────────────────────────
def source_identity(source: object) -> dict[str, object] | None:
    """
    ``source`` → 规范化身份映射（``url`` 去首尾空白）；非 ``git`` 或形状不合 → ``None``（fail-closed）/
    Source identity mapping (fail-closed).

    **全部键参与比较**（不是只挑 ``type`` / ``url``）：声明的额外字段（未来的 ``ref`` / ``sparse`` 等）若
    出现差异，同样视为「不同源」而拒绝——漏看未识别字段等于静默换源。
    """
    if not isinstance(source, Mapping):
        return None
    if source.get("type") != "git":
        return None
    url = source.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    return {**{key: value for key, value in source.items() if key != "url"}, "url": url.strip()}


def same_source(left: object, right: object) -> bool:
    """
    两个 source 是否**可证明**同一源 / Whether two sources are provably the same.

    **只做字符串级比较**（``url`` 去首尾空白，其余键逐字段相等）：``ssh://`` / ``https://`` / scp-like /
    ``.git`` 后缀等写法差异**不**归一——协议 §5.14 明许 SDK 不做等价归一，且「无法确定时 MUST fail-closed」。
    任何一侧不可识别（非 git / 缺 url / 非映射）→ ``False``（视为不同源并发起拒绝，而非静默替换）。
    """
    left_id = source_identity(left)
    right_id = source_identity(right)
    return left_id is not None and left_id == right_id


def display_source(source: object) -> str:
    """
    source 的**诊断展示值** / Display-safe source for diagnostics.

    走 :func:`~a2c_smcp.utils.git_url.diagnostic_url_for_display`（**错误 / 诊断面单一权威**，与
    ``marketplace add`` 失败文案同一判据）：去 userinfo / query / fragment，且 path 任一段含凭据样文本
    （``https://host/team/alice:PW@example.com/repo.git``）一律占位符；scp-like / 不可解析同样占位符。
    **不可**用于 re-clone。
    """
    url = source.get("url") if isinstance(source, Mapping) else None
    return diagnostic_url_for_display(url)


def redact_ledger_key(key: object) -> str:
    """
    账本键的**诊断展示值**（#261 口径的两段式）/ Display-safe ledger key for diagnostics.

    ``known_marketplaces.json`` 可被手编（``_coerce_known_marketplaces`` 不校验键），键可能是携带凭据的
    URL 或 URL 残形。**散文脱敏（``redact_git_urls_in_text``）只去 userinfo，保留 path**——路径段里的
    ``alice:PW@example.com`` 会照原样回显，故此处：

    1. URL 形态（含 ``://``）→ 走诊断面单一权威 :func:`diagnostic_url_for_display`（path 段也判）；
    2. 其余 → ``sanitize_text_for_diagnostics(redact_git_urls_in_text(key))`` 两段式（与
       ``safe_exception_text`` 同款），覆盖路径残形 / query 形态。

    已知边界：**裸 token**（如 ``glpat-…``）不可识别，属既有口径。
    """
    if not isinstance(key, str):
        return REDACTED_GIT_SOURCE
    if "://" in key:
        return diagnostic_url_for_display(key)
    return sanitize_text_for_diagnostics(redact_git_urls_in_text(key))


def registered_name_for_source(known: Mapping[str, Any], source: object) -> str | None:
    """
    账本中**已以该 source 注册**的名字（无则 ``None``）/ The registered name already bound to this source.

    :param known: ``known_marketplaces.json`` 的 ``marketplaces`` 映射。
    """
    for name, record in known.items():
        if isinstance(record, Mapping) and same_source(record.get("source"), source):
            return name
    return None


# ── 意图引用 / intent references ─────────────────────────────────────────────
def intent_reference_pids(name: str, *, env: Mapping[str, str] | None = None) -> list[str]:
    """
    指向某 marketplace 名的**意图引用**（``<plugin>@<name>``）/ Intent references bound to a marketplace name.

    数据源 = merged settings 的 ``installedPlugins`` ∪ ``enabledPlugins`` 键（user/project/local；与
    :func:`~a2c_smcp.computer.settings.reconciler.declared_installed_plugin_ids` 同一视图）。协议 §5.14
    「remove 后同名不同源 re-add」要求：仍存在这类引用时 MUST 拒绝，MUST NOT 静默重绑。

    .. note::
       已知边界：project/local 层按进程 ``cwd`` 锚定，**其他 workdir** 的声明不在本视图内（与 settings 其余
       消费面同一边界，见 ``scope.resolve_settings``）。
    """
    from a2c_smcp.computer.settings.reconciler import declared_installed_plugin_ids
    from a2c_smcp.computer.settings.schema import FIELD_ENABLED_PLUGINS
    from a2c_smcp.computer.settings.scope import resolve_settings

    declared = resolve_settings(env=env).settings
    pids = set(declared_installed_plugin_ids(declared))
    enabled = declared.get(FIELD_ENABLED_PLUGINS)
    if isinstance(enabled, Mapping):
        pids.update(key for key in enabled if isinstance(key, str))
    suffix = f"@{name}"
    return sorted(pid for pid in pids if pid.endswith(suffix) and len(pid) > len(suffix))
