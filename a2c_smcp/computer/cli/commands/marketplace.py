# -*- coding: utf-8 -*-
# filename: marketplace.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``marketplace`` 命令 handler（add / list / info / remove / refresh / set）/ Marketplace command handlers。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.2 / §10.4 / §10.5（S15，#68）。

handler 取**显式资源**（``registry`` / ``home`` / ``env``）+ flags，返回退出码（0 成功 / 1 用户错 / 2 网络错；
§4.6 另定义 3=内部错，本层暂不显式产出——内部异常由 Typer 顶层处理），便于隔离单测。包裹既有后端：克隆/对账
:func:`stage_marketplace_skills`、物化读写
:func:`load_known_marketplaces` 等、卸载级联 :func:`uninstall_plugin` + :func:`prune_marketplaces`。
Trust（§10.5）首见经 ``confirm`` 回调（REPL=session y/N；Typer 非交互无 confirm 且无 ``--trust`` → 退出码 1），
批准后持久化到 user scope ``trustedMarketplaces``（CC 模型，§16：``known_marketplaces.json`` **不**带 trusted 字段）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rich.markup import escape
from rich.table import Table

from a2c_smcp.computer.cli.commands import McpCallbacks, files_only_non_plugin_bundle_ids, flag_value
from a2c_smcp.computer.cli.progress import clone_spinner, refresh_summary_table
from a2c_smcp.computer.cli.utils import console, console_err
from a2c_smcp.computer.settings.installer import PluginInstallError, uninstall_plugin
from a2c_smcp.computer.settings.marketplace_migration import (
    ACTION_ABORTED,
    ACTION_RENAME,
    MigrationPlan,
    blocking_conflicts,
    build_plan,
    execute_plan,
    plan_exit_code,
    plan_payload,
)
from a2c_smcp.computer.settings.raw import (
    SettingsFileCorruptError,
    list_field_or_empty,
    read_raw_object,
)
from a2c_smcp.computer.settings.reconciler import prune_marketplaces
from a2c_smcp.computer.settings.schema import (
    FIELD_TRUSTED_MARKETPLACES,
    SettingsScope,
    is_scp_like_git_url,
    is_valid_git_url,
    is_valid_marketplace_name,
)
from a2c_smcp.computer.settings.scope import apply_write, load_settings_file, user_settings_path
from a2c_smcp.computer.settings.store import (
    SettingsLockError,
    atomic_write_json,
    file_lock,
    known_marketplaces_path,
    load_installed_plugins,
    load_known_marketplaces,
    update_known_marketplaces,
)
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.manifest import PluginManifestError, read_marketplace_manifest
from a2c_smcp.computer.skills.marketplace_identity import (
    REASON_INVALID_NAME,
    REASON_INVALID_SOURCE,
    REASON_NAME_CONFLICT,
    REASON_UNRESOLVABLE_NAME,
    MarketplaceIdentityError,
    MarketplaceNameChangeError,
    manifest_marketplace_name,
    read_manifest_marketplace_name,
    redact_ledger_key,
    registered_name_for_source,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.sources import (
    GITHUB_HOST,
    SkillSourceError,
    normalize_repo_shorthand,
    safe_exception_text,
)
from a2c_smcp.computer.skills.staging import (
    SkillStagingError,
    add_marketplace_source,
    registration_violation,
    stage_marketplace_skills,
)
from a2c_smcp.utils.git_url import (
    REDACTED_GIT_SOURCE,
    credential_like_text,
    diagnostic_url_for_display,
    known_git_source_for_display,
    sanitize_path_for_diagnostics,
    sanitize_text_for_diagnostics,
)
from a2c_smcp.utils.logger import get_logger

# trust 确认回调：接收待信任源（url/name）→ 返回是否继续 / Trust confirm callback。
ConfirmFn = Callable[[str], Awaitable[bool]]

logger = get_logger(__name__)

# 退出码语义（§4.6）/ Exit code semantics。
EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_NETWORK_ERROR = 2
EXIT_PARTIAL = 3  # 仅 marketplace migrate（D12）：部分完成（unresolved/blocked/keyring-abort/follow-up）


# ── 输出辅助 / output helpers ─────────────────────────────────────────────────
# ``details`` 中与 :func:`_err` 保留字段同名的键：展开前过滤，避免覆盖类别码 / 人读文案（#257 先例）。
# 注：退出码参数叫 ``exit_code``（不是 ``code``）——后者留给结构化 payload 之外的历史调用面；
# 机器码统一走 ``reason`` 键（:meth:`MarketplaceIdentityError.payload_details`），不在保留集内。
_RESERVED_DETAIL_KEYS = frozenset({"error", "message", "msg", "exit_code", "error_code"})


def _err(msg: str, *, json_output: bool, exit_code: int = EXIT_USER_ERROR, error_code: str | None = None, **extra: Any) -> int:
    """
    错误输出（结构化 ``validation`` 可选）/ Error output with an optional structured category.

    ``error_code`` 非空时输出 ``{"error": <category>, "message": …, **diagnostics}``（对齐
    ``cli/commands/plugin.py:102`` 的 #257/PR #266 先例）；为空保持既有 ``{"error": <文案>}`` 形态。
    非 JSON 输出经 ``escape()``——文案可含不可信 source 文本，rich 标记会把「结构化错误」变成 traceback（#263）。
    """
    if json_output:
        details = {key: value for key, value in extra.items() if key not in _RESERVED_DETAIL_KEYS}
        payload: dict[str, Any] = {"error": error_code or "error", "message": msg, **details}
        console.print_json(data=payload)
    else:
        console.print(f"[red]✗ {escape(msg)}[/red]")
    return exit_code


def _identity_error(e: MarketplaceIdentityError, *, json_output: bool) -> int:
    """把 :class:`MarketplaceIdentityError` 翻成结构化 CLI 错误（``validation`` + reason/诊断）/ Render it."""
    return _err(str(e), json_output=json_output, error_code=e.error_code, **e.payload_details())


def _ok(msg: str) -> int:
    # 与 `_err` 同口径转义：成功行常内插**用户输入 / 账本键**（`[/]x` 之类会让 rich 抛 MarkupError）。
    console.print(f"[green]✓ {escape(msg)}[/green]")
    return EXIT_OK


def _marketplaces(home: Path, env: Mapping[str, str] | None) -> dict[str, Any]:
    """known_marketplaces 的 marketplaces 映射（plain dict，规避 TypedDict 严格性）/ marketplaces map as a plain dict。"""
    return dict(load_known_marketplaces(home, env).get("marketplaces") or {})


def _source_url(rec: Mapping[str, Any]) -> str | None:
    """从 marketplace 记录取 git url / Extract the git url from a marketplace record。"""
    src = rec.get("source")
    return src.get("url") if isinstance(src, Mapping) else None


def _display_source_url(rec: Mapping[str, Any]) -> str | None:
    """
    marketplace 记录的**展示用** source URL（known-source 展示原语）/ Display-safe source URL.

    ``#261``：存储值可能携带 userinfo / query / fragment（含 token），展示面前一律归一；缺失 → ``None``
    （JSON ``null`` / 表格留空），畸形 → 占位符。**展示值不可用于 re-clone**。
    """
    raw = _source_url(rec)
    return None if raw is None else known_git_source_for_display(raw)


# ── URL / name 归一 / normalization ───────────────────────────────────────────
def normalize_marketplace_url(raw: str) -> str | None:
    """归一 marketplace git URL：完整 URL 原样；裸 ``owner/repo`` 简写按 GitHub 糖展开 / Normalize a marketplace git URL。"""
    raw = raw.strip()
    if is_valid_git_url(raw):
        return raw
    try:
        return normalize_repo_shorthand(raw, GITHUB_HOST)
    except SkillSourceError:
        return None


# 凭据样判据 / URL 诊断展示：**单一权威**在 :mod:`a2c_smcp.utils.git_url`（#260 上移，供注册身份诊断复用）。
# 本模块保留同名薄包装：①`default_marketplace_name` / add 失败文案既有调用面不变（#270/#282 行为与回归锁
# 保持）；②外部（测试 / 其他命令）按既有符号引用。
def _credential_like(text: str) -> bool:
    """文本是否含凭据样文本（userinfo ``@`` / 密码段 ``:`` / 残形 percent 转义）/ Credential-like text."""
    return credential_like_text(text)


def _error_url_display(raw: str) -> str:
    """
    ``marketplace add`` **失败文案**里的 URL 展示 / Display-safe URL in add-failure messages.

    委托 :func:`~a2c_smcp.utils.git_url.diagnostic_url_for_display`（去 userinfo/query/fragment +
    **path 任意段**凭据样文本 fail-closed）；判据跑在**原始 source** 上（展示层 ``quote()`` 的 ``%xx``
    不得误伤干净 URL，#282 复核）。
    """
    return diagnostic_url_for_display(raw)


def default_marketplace_name(url: str) -> str | None:
    """
    从 git URL **解析后 path 的末段**派生严格-kebab marketplace 名 / Derive a kebab name from the URL path。

    .. deprecated:: 0.5.1
       **#260 起 `marketplace add` 不再调用本函数**——注册名取清单顶层 ``name``（D1）。本函数保留仅为
       兼容既有调用方/测试；存量 URL 派生名的迁移由 #268 负责，**不要**用它推断「旧名是否合法」。

    **#270**：URL 解析式派生——userinfo / query / fragment 不参与命名
    （``https://host/repo?token=SECRET`` → ``repo``，此前是 ``repo-token-secret``；
    ``ssh:///alice:PW@example.com`` 这类 hostless 形态不合法 → ``None``）。
    ``file:`` 不要求 host，按 path 末段派生；末段含 userinfo / 密码分隔符（``@`` / ``:``，判定前做
    NFKC 归一 + percent-decode）同样视为凭据样文本 → ``None``；**#282**：末段含**残形 percent 转义**
    也一律 → ``None``（含解码后本可干净的 ``repo%20name``，口径对齐 rust-sdk#285
    ``seg.contains([':', '@', '%'])``）；无 path（``https://host``）、非法、
    hostless / junk host、末段构不成严格 kebab → ``None``（CLI 侧提示 ``--name``）。
    本函数只产出名字：存储与 git argv 仍用 :func:`normalize_marketplace_url` 的结果（原始 URL）。
    入参先 ``strip()``（与 :func:`normalize_marketplace_url` 一致；:func:`is_valid_git_url` 本身**不** strip，
    故 ``" https://host/repo "`` 在本函数可派生、在 ``is_valid_git_url`` 为 False）。
    Only the parsed path tail is used; credentials / query / fragment never enter the name.
    """
    raw = url.strip()
    if not is_valid_git_url(raw):  # hostless / junk host / 非法 → 不派生（复用单一权威，不复制 host 规则）
        return None
    if is_scp_like_git_url(raw):
        # scp-like ``user@host:path``：先截 query / fragment，再取最后一个 ``:`` 之后的 path
        # （与 rust ``rsplit_once(':')`` 对齐；scp 的 host 段不含 ``:``）
        path = re.split(r"[?#]", raw, maxsplit=1)[0].rsplit(":", 1)[1]
    else:
        path = urlsplit(raw).path  # path 天然不含 query / fragment
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return None
    tail = segments[-1]
    # 凭据样文本不进名字：末段含 userinfo 分隔符 ``@`` / 密码段分隔符 ``:``
    # （如 ``https://host/alice:PW@example.com``），或含**残形 percent 转义**（#282：``repo%20name``
    # 这类解码后本可干净的也算，对齐 rust-sdk#285）→ 交给 ``--name`` 显式命名。
    if _credential_like(tail):
        return None
    if tail.endswith(".git"):
        tail = tail[:-4]
    slug = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")
    return slug if slug and is_valid_marketplace_name(slug) else None


# ── trust 持久化（user scope settings.json）/ trust persistence ────────────────
# 键空间 = marketplace **name**（设计 §5.3.1 规范表：trustedMarketplaces 元素=name，与 blockedMarketplaces
# 同键空间、line 458 blocked 优先可比；#80 strict/白名单按 name 读）。**不**用 URL，避免 #80 落地时键型不一致 +
# 已写入 user settings 的迁移成本。
def _load_trusted(env: Mapping[str, str] | None) -> set[str]:
    existing, _errors = load_settings_file(user_settings_path(env), SettingsScope.USER)
    return {n for n in existing.get(FIELD_TRUSTED_MARKETPLACES, []) if isinstance(n, str)}


def _record_trust(name: str, env: Mapping[str, str] | None) -> None:
    """
    把 marketplace ``name`` 追加进 user ``settings.json`` 的 ``trustedMarketplaces``（持锁原子 RMW + dedup）。

    **#246 raw 保真**：读**原始 JSON**（:func:`read_raw_object`，不经校验过滤视图），只改该数组键——
    其余字段（含 policy-only 越权字段、非法 ``permissions`` 条目、未知键）与**数组内的非字符串条目**逐字保留
    （过去 ``if isinstance(n, str)`` 会顺带丢弃它们）。目标文件 corrupt / 该键非数组 → 抛
    :class:`SettingsFileCorruptError`，原文件逐字节不动。
    """
    path = user_settings_path(env)
    with file_lock(path):
        existing = read_raw_object(path) or {}
        current = list_field_or_empty(existing, FIELD_TRUSTED_MARKETPLACES, path=path)
        if name not in current:
            current.append(name)
        atomic_write_json(path, apply_write(existing, {FIELD_TRUSTED_MARKETPLACES: current}))


def _revoke_trust(name: str, env: Mapping[str, str] | None) -> None:
    """
    从 user ``settings.json`` 的 ``trustedMarketplaces`` 移除 marketplace ``name``（remove 时调，避免只增不减）。

    **#246 raw 保真**：同 :func:`_record_trust`——只删目标元素，其余字段与非字符串条目保留。
    """
    path = user_settings_path(env)
    with file_lock(path):
        existing = read_raw_object(path) or {}
        current = [n for n in list_field_or_empty(existing, FIELD_TRUSTED_MARKETPLACES, path=path) if n != name]
        atomic_write_json(path, apply_write(existing, {FIELD_TRUSTED_MARKETPLACES: current}))


def _precheck_trust_write(env: Mapping[str, str] | None) -> None:
    """
    trust 落盘前的**只读**预检（#246 破坏半径）/ Read-only pre-check before any clone happens.

    注册成功后才写 trust（#260），但「settings 损坏 ⇒ add 失败且不 clone」这条 #246 语义必须保住：
    在 clone 前把**将要写**的目标键形状验一遍，corrupt / 非数组 → :class:`SettingsFileCorruptError`
    （CLI 退出码 1、零副作用）。
    """
    path = user_settings_path(env)
    existing = read_raw_object(path) or {}
    list_field_or_empty(existing, FIELD_TRUSTED_MARKETPLACES, path=path)


def _register_intent_only(
    home: Path,
    env: Mapping[str, str] | None,
    url: str,
    name: str | None,
    *,
    auto_update: bool,
    json_output: bool,
) -> int:
    """
    ``--no-clone``：只登记意图（D5：无 pending、无临时派生名）/ Register intent only (no clone).

    允许的两种形态（其余一律 ``validation``——无法在注册前确定名称）：

    1. **已以该 source 注册**（任意名）→ 幂等意图，保留既有 ``installLocation``（绝不用空值覆盖有效记录）；
    2. ``--name N`` 且本地缓存 ``<home>/marketplace/N`` 已存在、其清单名 == ``N`` → 以缓存确定名称并登记意图。

    两种形态都在 ``update_known_marketplaces`` 的**锁内**过一遍与 add 相同的身份判定
    （:func:`~a2c_smcp.computer.skills.staging.registration_violation`）：同名不同源 / 断链引用一律
    ``validation``，**不得**绕过提交点静默改写既有注册的 source。
    """
    source = {"type": "git", "url": url}
    outcome: dict[str, Any] = {"existing": None, "registered": None}

    def _mutate(current: Any) -> None:
        known = current["marketplaces"]
        existing_name = registered_name_for_source(known, source)
        if existing_name is not None:
            outcome["existing"] = existing_name  # 幂等：不新建、不覆盖
            return None
        if name is None:
            raise _unresolvable_name_error()
        clone_dir = marketplace_skill_dir(home, name)
        observed: str | None = None
        if clone_dir.is_dir():
            try:
                observed = read_manifest_marketplace_name(clone_dir)
            except MarketplaceIdentityError:
                observed = None
        if observed != name:
            raise _unresolvable_name_error()
        prior = known.get(name)
        violation = registration_violation(name, source, prior, known, name, env=env)
        if violation is not None:
            raise violation  # 同名不同源 / 断链引用（fail-closed）
        if prior is not None:
            # 不可达：同名同源已在入口被 `registered_name_for_source` 认作幂等早返回，同名异源由上面的
            # `registration_violation` 抛出。留显式拒绝（而非静默覆盖）作为纵深防御。
            raise MarketplaceIdentityError(
                f"marketplace {name!r} is already registered; refusing to overwrite its record from --no-clone",
                reason=REASON_NAME_CONFLICT,
                details={"name": name},
            )
        known[name] = {
            "source": dict(source),
            "installLocation": str(clone_dir.resolve()),
            "autoUpdate": bool(auto_update),
            "manifestNameBaseline": name,
        }
        outcome["registered"] = name
        return None

    try:
        update_known_marketplaces(_mutate, home, env)
    except MarketplaceIdentityError as e:
        return _identity_error(e, json_output=json_output)
    except SettingsLockError:
        return _err(
            "could not acquire the marketplace ledger lock (another a2c-computer is writing it); retry in a moment",
            json_output=json_output,
        )
    except OSError as e:
        logger.error("marketplace intent registration failed: %s", safe_exception_text(e))
        return _err("failed to register the marketplace intent (see logs)", json_output=json_output)

    final_name = outcome["existing"] or outcome["registered"]
    if final_name is None:  # 防御：mutator 既没判幂等、也没登记（不应发生）
        return _err("failed to register the marketplace intent (see logs)", json_output=json_output)
    # 展示名脱敏：`final_name` 可能是 `registered_name_for_source` 返回的**账本键原值**（可被手编成携带
    # 凭据的串）。**只脱敏、不在此处 escape**——`_ok` 内部统一转义，双重转义会输出 `\[/]x`。
    # 查找 / trust 仍用原值。
    redacted = redact_ledger_key(final_name)
    record = _marketplaces(home, env).get(final_name) or {}
    try:
        _record_trust(final_name, env)
    except (SettingsFileCorruptError, OSError) as e:
        console_err.print(
            f"[yellow]! marketplace {escape(redacted)} is registered but its trust entry could not be persisted: {escape(str(e))}[/yellow]"
        )
    if json_output:
        console.print_json(
            data={
                "registered": redacted,
                "url": known_git_source_for_display(url),
                "installLocation": record.get("installLocation"),
            }
        )
        return EXIT_OK
    if outcome["existing"] is not None:
        return _ok(f"marketplace {redacted!r} is already registered (no clone needed)")
    return _ok(f"registered marketplace {redacted!r} from the local clone (no clone)")


def _unresolvable_name_error() -> MarketplaceIdentityError:
    """``--no-clone`` 无法确定名称时的结构化错误（D5）/ Structured error for an unresolvable name."""
    return MarketplaceIdentityError(
        "cannot determine the marketplace name without cloning: --no-clone only works for an already "
        "registered source, or with --name pointing at an existing local clone whose manifest declares that name. "
        "Run without --no-clone to clone and read the manifest name.",
        reason=REASON_UNRESOLVABLE_NAME,
    )


# ── handlers ──────────────────────────────────────────────────────────────────
async def marketplace_add(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    git_url: str,
    *,
    name: str | None = None,
    trust: bool = False,
    auto_update: bool = False,
    no_clone: bool = False,
    confirm: ConfirmFn | None = None,
    json_output: bool = False,
) -> int:
    """
    添加新 marketplace（D1 注册名取清单 ``name``；首次 trust y/N，默认 eager clone）/
    Add a marketplace: the registration name is the manifest's top-level ``name``.

    **D6 B**：``--name`` 只作**期望清单名**校验，不再是别名（不符即 ``validation``，不覆盖）。

    trust 门（§10.5/#95/#260）：
    - 非交互（``confirm is None``）：``--trust`` 无条件强制；
    - 交互（REPL）：**一律**弹确认——``trustedMarketplaces`` 不再豁免 prompt（#260 裁决：``--name`` 已不
      命名任何东西，拿它当免提示开关与其语义不一致，且会绕过确认）。trust 在**注册成功后**才落盘，
      注册失败零残留。
    """
    url = normalize_marketplace_url(git_url)
    if url is None:
        return _err(
            f"not a well-formed git url or owner/repo shorthand: {_error_url_display(git_url)}",
            json_output=json_output,
            error_code="validation",
            reason=REASON_INVALID_SOURCE,
        )
    if name is not None and not is_valid_marketplace_name(name):
        # ``--name`` 现在是「期望清单名」，先按同一语法契约快速拒绝（避免白跑一次 clone）。
        return _err(
            f"--name {name!r} is not a valid marketplace name (strict kebab-case, 1-64); pass the manifest's name",
            json_output=json_output,
            error_code="validation",
            reason=REASON_INVALID_NAME,
        )

    # #246：写前**只读**预检（corrupt → 退出码 1 且**不 clone**），保持既有 fail-closed 破坏半径。
    try:
        _precheck_trust_write(env)
    except SettingsFileCorruptError as e:
        return _err(str(e), json_output=json_output)

    if not trust:
        if confirm is None:
            return _err(
                f"untrusted source ({_error_url_display(url)}); pass --trust to confirm non-interactively",
                json_output=json_output,
            )
        # 确认提示是 known-source 展示面（#261）：scp-like → host:path，去 userinfo/query/fragment。
        if not await confirm(known_git_source_for_display(url)):
            return _err("aborted by user (untrusted)", json_output=json_output)

    if no_clone:
        return _register_intent_only(home, env, url, name, auto_update=auto_update, json_output=json_output)

    source = {"type": "git", "url": url}
    try:
        with clone_spinner("Cloning marketplace...", enabled=not json_output):
            final_name = await add_marketplace_source(source, home, expected_name=name, auto_update=auto_update, env=env)
    except MarketplaceIdentityError as e:
        return _identity_error(e, json_output=json_output)
    except (SkillStagingError, SkillSourceError):
        # 不插值 e：git 失败文本含 argv（原始 URL，可能是带凭据的形态），失败文案只走脱敏展示面。
        return _err(
            f"clone failed for {_error_url_display(url)} (see logs)",
            json_output=json_output,
            exit_code=EXIT_NETWORK_ERROR,
        )
    except SettingsLockError:
        # 并发写账本的锁被占满（退避约 6s）→ 干净报错（提交点已回滚，无残留）；用户稍后重试即可。
        return _err(
            "could not acquire the marketplace ledger lock (another a2c-computer is writing it); retry in a moment",
            json_output=json_output,
        )
    except OSError as e:
        # `os.replace` 失败（Windows 文件占用 / 权限 / 磁盘异常）→ 结构化错误，不抛 traceback。提交点自身
        # 已尽力回滚（新树删除、旧树复位），状态与提交前一致。文案保持泛化：OSSerror 也可能来自 clone 前的
        # 目录操作，不宜断言「commit 失败」。
        logger.error("marketplace registration failed: %s", safe_exception_text(e))
        return _err(
            f"failed to register the marketplace (see logs): {_error_url_display(url)}",
            json_output=json_output,
        )

    # 注册已提交 → 落 trust（失败仅 WARN，不回滚已成功的注册）。`file_lock` / `os.replace` 也可能抛
    # OSError（只读 / 磁盘满 / 权限），一并降级为 WARN 而不是 traceback（#260 七审 🟡3）。
    try:
        _record_trust(final_name, env)
    except (SettingsFileCorruptError, OSError) as e:
        console_err.print(
            f"[yellow]! added {escape(final_name)!r} but failed to persist its trust entry: {escape(str(e))}[/yellow]"
        )

    try:
        with clone_spinner(f"Staging {final_name}...", enabled=not json_output):
            registered = await stage_marketplace_skills(final_name, source, registry, home, auto_update=auto_update, env=env)
    except MarketplaceNameChangeError as e:
        # 防御：提交后的复用路径不观测清单名，此处理论不可达；真发生也不得 traceback（注册已提交）。
        return _identity_error(e, json_output=json_output)

    # 成功判定：注册提交点已写账本；此处再确认一次，避免 stage 的并发 remove 把记录抹掉后误报成功。
    if final_name not in _marketplaces(home, env):
        return _err(
            f"clone/refresh failed for {_error_url_display(url)} (see logs)",
            json_output=json_output,
            exit_code=EXIT_NETWORK_ERROR,
        )

    if json_output:
        console.print_json(
            data={"added": final_name, "url": known_git_source_for_display(url), "skills": len(registered)}
        )
        return EXIT_OK
    return _ok(f"added {final_name!r} — cloned, {len(registered)} skill(s) found")


def _redact_pid(pid: str) -> str:
    """
    plugin id 的展示值：只脱敏 ``<marketplace>`` 段，保留 ``<plugin>@`` 前缀 / Display-safe plugin id.

    pid 形如 ``<plugin>@<marketplace>``；market 段就是账本键（可被手编成携带凭据的串）。整条 pid 交给
    :func:`redact_ledger_key` 会因含 ``://`` 被判成 URL 而整串占位（丢 plugin 名），故按**第一个** ``@``
    拆分（plugin 名是严格 kebab，不含 ``@``；market 段可能自带 ``@``，用 rpartition 会切错）后只脱敏 market 段。
    """
    plugin, sep, market = pid.partition("@")
    if not sep:
        return redact_ledger_key(pid)
    # 机器写入路径两段均为严格 kebab；**手编**账本可把凭据样文本塞进 plugin 段 → 同样 fail-closed。
    shown_plugin = REDACTED_GIT_SOURCE if credential_like_text(plugin) else plugin
    return f"{shown_plugin}@{redact_ledger_key(market)}"


def marketplace_list(home: Path, env: Mapping[str, str] | None, *, json_output: bool = False) -> int:
    """列出所有已知 marketplace（trusted / clone 状态 / 上次刷新 / auto_update）/ List known marketplaces。"""
    mps = _marketplaces(home, env)
    trusted = _load_trusted(env)

    if json_output:
        console.print_json(
            data=[
                {
                    "name": redact_ledger_key(nm),
                    "url": _display_source_url(rec),
                    "trusted": nm in trusted,  # 信任按 name 判定（§5.3.1）
                    "autoUpdate": bool(rec.get("autoUpdate", False)),
                    "lastUpdated": rec.get("lastUpdated"),
                    "commitSha": rec.get("commitSha"),
                }
                for nm, rec in mps.items()
            ]
        )
        return EXIT_OK

    if not mps:
        console.print("[dim]No marketplaces. Add one: marketplace add <git-url>[/dim]")
        return EXIT_OK
    table = Table(title="Marketplaces", header_style="bold magenta")
    for col in ("Name", "URL", "Trusted", "Auto-update", "Last updated", "Commit"):
        table.add_column(col)
    for nm, rec in mps.items():
        url = _display_source_url(rec) or ""
        # 手编账本可把 `commitSha` 写成非 str → 先 `str()` 再截断（旧实现 `sha[:10]` 会 TypeError）。
        sha = str(rec.get("commitSha") or "")
        table.add_row(
            escape(redact_ledger_key(nm)),
            escape(url),
            "✓" if nm in trusted else "—",
            "on" if rec.get("autoUpdate") else "off",
            escape(str(rec.get("lastUpdated") or "—")),
            escape(sha[:10]) if sha else "—",
        )
    console.print(table)
    return EXIT_OK


def _manifest_name_for_display(home: Path, name: str) -> tuple[str | None, bool]:
    """
    ``marketplace info`` 的清单名展示值（§10.4，Q9）/ Manifest name for display (read-only, redacted).

    清单内容来自 git 源、属不可信输入：只有**合法严格 kebab** 才原样展示；清单不可读 → ``(None, False)``；
    可读但名字非法（可能携带凭据样文本）→ ``(None, True)``，**绝不回显原值**（演示规则同 ``redact_ledger_key``）。
    """
    clone_dir = marketplace_skill_dir(home, name)
    if not clone_dir.is_dir() or clone_dir.is_symlink():
        return None, False
    try:
        manifest = read_marketplace_manifest(clone_dir)
    except PluginManifestError:
        return None, False
    try:
        return manifest_marketplace_name(manifest), False
    except MarketplaceIdentityError:
        return None, True


def marketplace_info(home: Path, env: Mapping[str, str] | None, name: str, *, json_output: bool = False) -> int:
    """marketplace 详情：URL / clone 路径 / commit / plugins[]（含 installed 状态）/ auto_update / trusted。"""
    rec = _marketplaces(home, env).get(name)
    if rec is None:
        return _err(f"unknown marketplace: {name!r}", json_output=json_output)

    installed = load_installed_plugins(home, env).get("plugins") or {}
    plugins = [pid for pid in installed if pid.endswith(f"@{name}")]
    url = _display_source_url(rec)
    manifest_name, manifest_name_invalid = _manifest_name_for_display(home, name)
    info: dict[str, Any] = {
        "name": redact_ledger_key(name),
        "url": url,
        "installLocation": rec.get("installLocation"),
        "commitSha": rec.get("commitSha"),
        "autoUpdate": bool(rec.get("autoUpdate", False)),
        "trusted": name in _load_trusted(env),  # 信任按 name 判定（§5.3.1）
        "lastUpdated": rec.get("lastUpdated"),
        # §10.4 / Q9：只读派生，供自查 legacy；非法/不可读清单名绝不回显原值。
        "manifestName": manifest_name,
        "manifestNameInvalid": manifest_name_invalid,
        "legacyName": manifest_name is not None and manifest_name != name,
        # pid = `<plugin>@<marketplace>`：market 段就是账本键，可被手编成携带凭据的串 → 展示面脱敏。
        "installedPlugins": [_redact_pid(pid) for pid in plugins],
    }
    if json_output:
        console.print_json(data=info)
        return EXIT_OK
    table = Table(title=f"Marketplace · {escape(redact_ledger_key(name))}", show_header=False)
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")
    for k in (
        "url",
        "installLocation",
        "commitSha",
        "autoUpdate",
        "trusted",
        "lastUpdated",
        "manifestName",
        "legacyName",
    ):
        value: Any = info[k]
        if k == "manifestName" and info["manifestNameInvalid"]:
            value = "<invalid>"
        # 缺失字段留空（与 docstring / JSON null 语义一致），不渲染字面 "None"（#261 复审 🟡）。
        # 值可能来自**手编**账本（或 URL path）→ rich 单元格必须 escape（#260 七审 🔴1）。
        table.add_row(k, "" if value is None else escape(str(value)))
    table.add_row("installedPlugins", ", ".join(escape(_redact_pid(pid)) for pid in plugins) if plugins else "—")
    console.print(table)
    return EXIT_OK


def _display_conflict_name(name: str) -> str:
    """冲突名展示：pid 走 ``_redact_pid``（只脱敏 marketplace 段），其余走 ``redact_ledger_key``。"""
    return _redact_pid(name) if "@" in name else redact_ledger_key(name)


def _probe_concurrent_writer(home: Path, env: Mapping[str, str] | None) -> bool:
    """
    启动时的**只读**并发探测（#268 §7.5）/ Read-only best-effort lock probe.

    对既有旁车 ``.lock`` 尝试非阻塞 ``flock``：**不创建**锁文件（dry-run 必须零写入），因此
    「锁文件存在」本身不被当作活性信号——能拿到锁就说明没有进程在写。命中 → 只提示、不阻断。
    win32 跳过（best-effort，与 ``file_lock`` 的 win32 姿态一致）。
    """
    import sys

    if sys.platform == "win32":  # pragma: no cover - POSIX 是 CI/主用平台
        return False
    import fcntl

    lock_path = known_marketplaces_path(home, env).with_name(known_marketplaces_path(home, env).name + ".lock")
    if not lock_path.exists():
        return False
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:  # pragma: no cover - 权限等
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _flag_values(args: Sequence[str], flag: str) -> list[str]:
    """解析可重复 flag（``--name a --name=b``）/ Parse a repeatable flag."""
    values: list[str] = []
    for idx, token in enumerate(args):
        if token == flag and idx + 1 < len(args):
            values.append(args[idx + 1])
        elif token.startswith(f"{flag}="):
            values.append(token.split("=", 1)[1])
    return values


def _display_actions(item: Any) -> dict[str, Any]:
    """人读表格的 actions 抑制口径（与 ``serializable()`` 同判据）/ Actions shown in the human table."""
    return item.actions if item.action in {ACTION_RENAME, ACTION_ABORTED} else {}


def _render_migration(plan: MigrationPlan, *, applied: bool) -> None:
    """人读形态（§6）/ Human-readable migration report."""
    if plan.corrupt:
        console_err.print("[red]✗ aborted: corrupt state file(s); nothing was written[/red]")
        for entry in plan.corrupt:
            console_err.print(
                f"  [red]{escape(sanitize_path_for_diagnostics(str(entry.get('path', ''))))}[/red]: "
                f"{escape(sanitize_text_for_diagnostics(str(entry.get('reason', ''))))}"
            )
        return
    if plan.errors:
        for message in plan.errors:
            console_err.print(f"[red]✗ {escape(sanitize_text_for_diagnostics(message))}[/red]")
        return
    if plan.corrupt or plan.errors or blocking_conflicts(plan):
        title = "Marketplace name migration aborted (nothing was written)"
    elif applied:
        title = "Marketplace name migration (applied)"
    else:
        title = "Marketplace name migration plan (dry run; run with --apply to execute)"
    table = Table(title=title, header_style="bold magenta")
    for column in ("Old", "New", "Action", "Plugins", "Trust", "Blocked", "Inputs", "Catalog", "Follow-ups"):
        table.add_column(column)
    for item in plan.items:
        actions = _display_actions(item)
        table.add_row(
            escape(redact_ledger_key(item.old)),
            escape(redact_ledger_key(item.new)) if item.new else "—",
            escape(item.action + (f" ({item.reason})" if item.reason else "")),
            str(actions.get("installedPlugins", "—")),
            str(actions.get("trustedMarketplaces", "—")),
            str(actions.get("blockedMarketplaces", "—")),
            str(actions.get("inputValues", "—")),
            str(actions.get("catalog", "—")),
            str(len(item.follow_ups)),
        )
    console.print(table)
    if plan.conflicts:
        if blocking_conflicts(plan):
            console.print("[red]Conflicts (command aborts; nothing is written):[/red]")
        else:
            console.print("[yellow]Conflicts reported (only between unselected items; not blocking):[/yellow]")
        for conflict in plan.conflicts:
            names = ", ".join(_display_conflict_name(str(name)) for name in conflict.get("names", []))
            console.print(
                f"  [red]{escape(str(conflict.get('code', 'conflict')))}[/red]: "
                f"{escape(names)} → {escape(_display_conflict_name(str(conflict.get('target', ''))))}"
            )
    for item in plan.items:
        for warning in item.warnings:
            console_err.print(
                f"[yellow]![/yellow] {escape(redact_ledger_key(item.old))}: "
                f"{escape(sanitize_text_for_diagnostics(warning))}"
            )
        for follow in item.follow_ups:
            console_err.print(
                f"[yellow]![/yellow] {escape(sanitize_path_for_diagnostics(str(follow.get('file', ''))))} "
                f"{escape(sanitize_text_for_diagnostics(str(follow.get('pointer', ''))))}: "
                f"{escape(str(follow.get('effect', '')))} — "
                f"{escape(sanitize_text_for_diagnostics(str(follow.get('suggested', ''))))}"
            )
    code = plan_exit_code(plan)
    migrating = [item for item in plan.items if item.selected and item.action == ACTION_RENAME]
    if code == EXIT_OK:
        if plan.applied and migrating:
            console.print(f"[green]Migrated {len(migrating)} marketplace(s).[/green]")
        elif migrating:
            console.print(
                f"[green]{len(migrating)} marketplace(s) can be migrated; run with --apply to write.[/green]"
            )
        else:
            console.print("[green]No marketplace name migration needed.[/green]")
    elif code == EXIT_USER_ERROR:
        console_err.print("[red]Aborted by conflict; nothing was written. Fix the conflicts and rerun.[/red]")
    elif code == EXIT_PARTIAL:
        console_err.print(
            "[yellow]Partial: some items need attention (see warnings/follow-ups). Rerun after fixing.[/yellow]"
        )


async def marketplace_migrate(
    home: Path,
    env: Mapping[str, str] | None,
    *,
    names: Sequence[str] | None = None,
    apply: bool = False,
    update_declarations: bool = False,
    json_output: bool = False,
) -> int:
    """
    ``marketplace migrate`` handler（#268 / D12）/ Legacy registration-name migration.

    默认 dry-run（零写入）：检测、计划、项级预检全部走 strict 只读面；``apply=True`` 才写盘。
    冲突涉及任一选中项 → 整体 abort、零写入、退出码 1；项级失败 → 其余继续、退出码 3。
    """
    if _probe_concurrent_writer(home, env):
        console_err.print(
            "[yellow]! another a2c-computer process appears to be writing the marketplace ledger; "
            "stop other instances before running --apply[/yellow]"
        )
    plan = build_plan(home, env, names=names, update_declarations=update_declarations)
    plan.apply_requested = apply
    if apply and not plan.corrupt and not plan.errors and not blocking_conflicts(plan):
        await execute_plan(plan, home, env)
    if json_output:
        console.print_json(data=plan_payload(plan))
    else:
        _render_migration(plan, applied=apply)
    return plan_exit_code(plan)


async def marketplace_remove(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    name: str,
    *,
    keep_plugins: bool = False,
    confirm: ConfirmFn | None = None,
    mcp_cbs: McpCallbacks | None = None,
    json_output: bool = False,
) -> int:
    """移除 marketplace。默认级联卸载其下 installed plugin（含 MCP server）；``--keep-plugins`` 仅 prune clone（记录留存=孤儿）。"""
    if name not in _marketplaces(home, env):
        return _err(f"unknown marketplace: {name!r}", json_output=json_output)

    if confirm is not None and not await confirm(name):
        return _err("aborted by user", json_output=json_output)

    uninstalled: list[str] = []
    failed: list[str] = []
    if not keep_plugins:
        installed = load_installed_plugins(home, env).get("plugins") or {}
        victims = [pid for pid in installed if pid.endswith(f"@{name}")]
        remove_cb = mcp_cbs.remove_server if mcp_cbs is not None else None
        # 回收判据数据源：有 Computer 走其声明面（含 flag/embed）；无 Computer（非交互 ledger-only、
        # ``remove_cb is None`` ⇒ 判据结果实际未被消费）走 durable scopes。
        non_plugin_cb = mcp_cbs.non_plugin_bundle_ids if mcp_cbs is not None else files_only_non_plugin_bundle_ids(env)
        for pid in victims:
            # 按 victim 捕获（#246 审查 🔴A）：settings fail-closed 后 uninstall 可抛 `PluginInstallError`，
            # 不捕获会在卸掉部分 plugin 后整条命令崩掉、其余 victim 一个都不处理。
            try:
                if await uninstall_plugin(
                    pid, registry, home, non_plugin_bundle_ids=non_plugin_cb, env=env, remove_server=remove_cb,
                ):
                    uninstalled.append(pid)
            except PluginInstallError as e:
                failed.append(pid)
                console_err.print(f"[yellow]! failed to uninstall {pid!r}: {e}[/yellow]")

    pruned = prune_marketplaces([name], registry, home, env=env)
    if name not in pruned:
        # clone / 外部树未能清空（软链 / OSError）→ prune 保留 known 条目；marketplace 仍存在，不得撤销信任。
        return _err(
            f"failed to remove marketplace {name!r}: clone tree could not be removed",
            json_output=json_output,
        )
    # 撤销信任，避免 trustedMarketplaces 只增不减（user scope）。此时移除**已完成**，revoke 只是清理残留，
    # 故 fail-closed 的写失败降级为 WARN（不吞、不覆盖、不回滚已完成的开除动作）——#246。
    try:
        _revoke_trust(name, env)
    except SettingsFileCorruptError as e:
        console_err.print(f"[yellow]! removed {name!r} but failed to revoke its trust entry: {e}[/yellow]")
    if failed:
        # 部分失败必须可感（退出码 1）：脚本无法从 stderr 的 WARN 判断成败。
        return _err(
            f"removed {name!r} but failed to uninstall {len(failed)} plugin(s): {', '.join(failed)}",
            json_output=json_output,
        )
    if json_output:
        console.print_json(data={"removed": name, "pruned": pruned, "uninstalledPlugins": uninstalled, "keptPlugins": keep_plugins})
        return EXIT_OK
    detail = f" (uninstalled {len(uninstalled)} plugin(s))" if uninstalled else (" (plugins kept as orphans)" if keep_plugins else "")
    return _ok(f"removed marketplace {name!r}{detail}")


async def marketplace_refresh(
    registry: SkillRegistry,
    home: Path,
    env: Mapping[str, str] | None,
    target: str = "all",
    *,
    json_output: bool = False,
) -> int:
    """
    重新 clone 并原子换入（拒绝时旧内容保留）；与缓存 plugin 集合对账 + 失败汇总（§10.4）/
    Refresh marketplaces by re-cloning into staging and atomically swapping.

    **#260 / §5.14**：清单改名 → 该行 ``status="validation"``（结构化、带 reason 与诊断），**不得**报
    ``updated`` / ``unchanged``；旧内容、账本与既有运行时能力保持不动。其余 marketplace 继续刷新。
    """
    mps = _marketplaces(home, env)
    names = list(mps) if target == "all" else [target]

    rows: list[dict[str, Any]] = []
    exit_code = EXIT_OK
    for nm in names:
        # 展示名统一脱敏（账本键可被手编成携带凭据的串）；内部查找 / staging 仍用原值。
        shown = redact_ledger_key(nm)
        rec = mps.get(nm)
        if rec is None:
            rows.append({"name": shown, "status": "missing"})
            continue
        before = rec.get("commitSha")
        try:
            with clone_spinner(f"Refreshing {shown}...", enabled=not json_output):
                registered = await stage_marketplace_skills(
                    nm, rec.get("source") or {}, registry, home, auto_update=bool(rec.get("autoUpdate", False)), refresh=True, env=env
                )
        except MarketplaceNameChangeError as e:
            # 改名拒绝：结构化 outcome，非 updated/unchanged；旧注册与旧能力原样保留。
            rows.append({"name": shown, "status": "validation", "message": str(e), **e.payload_details()})
            exit_code = EXIT_USER_ERROR
            continue
        after = _marketplaces(home, env).get(nm) or {}
        status = "updated" if after.get("commitSha") != before else "unchanged"
        rows.append({"name": shown, "status": status, "skills": len(registered)})

    if json_output:
        console.print_json(data=rows)
        return exit_code
    console.print(refresh_summary_table(rows))
    updated = sum(1 for r in rows if r["status"] == "updated")
    failed = sum(1 for r in rows if r["status"] == "missing")
    rejected = sum(1 for r in rows if r["status"] == "validation")
    console.print(f"[dim]{len(rows)} marketplace(s) · {updated} updated · {failed} failed · {rejected} validation[/dim]")
    return exit_code


def marketplace_set(
    home: Path, env: Mapping[str, str] | None, name: str, key: str, value: str, *, json_output: bool = False
) -> int:
    """设置 per-source 旗，目前仅 ``auto-update=<bool>`` / Set a per-source flag (auto-update only for v0.2.1)。"""
    if key != "auto-update":
        return _err(f"unknown key {key!r} (only 'auto-update' supported)", json_output=json_output)
    val = value.strip().lower() in {"true", "1", "yes", "on"}
    if name not in _marketplaces(home, env):
        return _err(f"unknown marketplace: {name!r}", json_output=json_output)

    def _mutate(f: Any) -> Any:
        f["marketplaces"][name]["autoUpdate"] = val
        return f

    update_known_marketplaces(_mutate, home, env)
    if json_output:
        console.print_json(data={"name": name, "autoUpdate": val})
        return EXIT_OK
    return _ok(f"{name!r} auto-update set to {val}")


# 指纹覆盖 `A2CSkillRef` **全部**协议字段（含可选），逐项显式取值 + 安全定型——不序列化整个 ref：
# frontmatter 的 `skill_metadata` / `tags` 是不可信结构（YAML 允许非 str 键 → `json.dumps(sort_keys=True)`
# 抛 TypeError；自引用环 → Circular reference），在 refresh 之前抛会把整个 REPL 命令打挂
# （#260 终审 🟡1 / 🟡3）。
_FINGERPRINT_FIELDS = (
    "name",
    "source",
    "uri",
    "path",
    "description",
    "license",
    "compatibility",
    "allowed_tools",
    "tags",
    "version",
    "skill_metadata",
)

_STABLE_MAX_DEPTH = 6
# 单字段的**节点预算**：`skill_metadata` 是 frontmatter 原样透传，YAML alias 可把结构放大到指数级
# （#260 终审 🟡3：261B 输入 → 1.42MB/0.09s，分支 50 时节点数 ~1.5e10）。预算耗尽即截断，保证
# `_registry_fingerprints` 在 async 处理器里的同步开销有界。
_STABLE_NODE_BUDGET = 512
_TRUNCATED = "<truncated>"


class _StableBudget:
    """一次深度遍历的共享预算 + 环检测（同一对象在多分支出现属 DAG，允许复用）。"""

    __slots__ = ("remaining", "active")

    def __init__(self) -> None:
        self.remaining = _STABLE_NODE_BUDGET
        self.active: set[int] = set()

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _stable_text(value: Any, budget: _StableBudget | None = None, _depth: int = 0) -> str:
    """
    任意 JSON-ish 值的**稳定且可判别**字符串化 / Stable, discriminating text for arbitrary values.

    #260 终审 🟡2–3：早期实现用 ``str()`` 序列化容器，**非单射**——``["a,b"]`` 与 ``["a","b"]``、
    ``None`` 与 ``"None"``、``1`` 与 ``"1"``、``{"a":1}`` 与 ``{"a":"1"}`` 都会碰撞（漏标脏）。现在：

    - 标量带**类型前缀**（``s`` + ``repr`` / ``int:`` / ``float:`` / ``bool:`` / ``null``）；
    - 容器带**类型标签 + 元素数 + 定界符**，映射键按序列化结果排序（非 str 键安全）；
    - 深度与**节点预算**双封顶（:data:`_STABLE_MAX_DEPTH` / :data:`_STABLE_NODE_BUDGET`），并用
      ``id()`` 活动集做环检测 → alias bomb / 自引用都有界；
    - 集合按序列化结果排序（无序容器也稳定）。
    """
    budget = budget if budget is not None else _StableBudget()
    if _depth >= _STABLE_MAX_DEPTH or not budget.take():
        return _TRUNCATED
    if isinstance(value, str):
        return "s" + repr(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool:1" if value else "bool:0"
    if isinstance(value, int):
        return "int:" + str(value)
    if isinstance(value, float):
        return "float:" + repr(value)
    if isinstance(value, Mapping):
        if id(value) in budget.active:
            return "<cycle>"
        budget.active.add(id(value))
        try:
            rendered: list[str] = []
            truncated = False
            for key, item in value.items():
                if budget.remaining <= 0:
                    truncated = True
                    break
                rendered.append(
                    _stable_text(key, budget, _depth + 1) + "=" + _stable_text(item, budget, _depth + 1)
                )
        finally:
            budget.active.discard(id(value))
        body = ";".join(sorted(rendered))
        if truncated:
            body = f"{body};{_TRUNCATED}" if body else _TRUNCATED
        return f"map[{len(value)}]{{{body}}}"
    if isinstance(value, (list, tuple)):
        if id(value) in budget.active:
            return "<cycle>"
        budget.active.add(id(value))
        try:
            rendered = []
            truncated = False
            for item in value:
                if budget.remaining <= 0:
                    truncated = True
                    break
                rendered.append(_stable_text(item, budget, _depth + 1))
        finally:
            budget.active.discard(id(value))
        body = ";".join(rendered)
        if truncated:
            body = f"{body};{_TRUNCATED}" if body else _TRUNCATED
        tag = "list" if isinstance(value, list) else "tuple"
        return f"{tag}[{len(value)}]{{{body}}}"
    if isinstance(value, (set, frozenset)):
        if id(value) in budget.active:
            return "<cycle>"
        budget.active.add(id(value))
        try:
            rendered = []
            truncated = False
            for item in value:
                if budget.remaining <= 0:
                    truncated = True
                    break
                rendered.append(_stable_text(item, budget, _depth + 1))
        finally:
            budget.active.discard(id(value))
        body = ";".join(sorted(rendered))
        if truncated:
            body = f"{body};{_TRUNCATED}" if body else _TRUNCATED
        tag = "set" if isinstance(value, set) else "frozenset"
        return f"{tag}[{len(value)}]{{{body}}}"
    return "obj:" + type(value).__name__


def _registry_fingerprints(registry: SkillRegistry) -> frozenset[tuple[tuple[str, str], ...]]:
    """
    registry 条目的**完整指纹**（`A2CSkillRef` 全字段 + 孤儿标志）/ Full fingerprints of the registry refs.

    #260 独立审查 🟡4 / 终审 🟡1–3：REPL `marketplace refresh` 原先只比 SKILL **名字集合**——「A 内容更新
    （version/uri/tags 变）+ B 改名被拒」时名字集相同，退出码为 1 但不会标脏，已连接 Agent 继续看到旧投影。

    指纹 = 全部协议字段（经 :func:`_stable_text` 定型）的 ``(键, 值)`` 元组 + **``orphaned`` 标志**：
    `plugin disable` 置孤儿后 refresh 重扫会把 ``orphaned`` 翻回 False 而 ref 内容不变——不纳入孤儿标志
    就同样漏标脏。
    """
    return frozenset(
        tuple((field, _stable_text(ref.get(field))) for field in _FINGERPRINT_FIELDS) + (("orphaned", str(orphaned)),)
        for ref, orphaned in registry.all_refs()
    )


# ── REPL dispatcher（绑定 Computer 的活跃 registry / home / session）/ REPL adapter ──
async def repl_dispatch(comp: Any, parts: list[str], *, session: Any) -> None:
    """把 ``marketplace ...`` REPL 行解析为 flags → 调 handler；变更后触发去抖 emit / Parse a REPL line and dispatch。"""
    sub = parts[1].lower()
    args = parts[2:]
    registry = comp.skill_registry
    home = comp.skill_home
    env = os.environ

    async def _confirm(target: str) -> bool:
        prompt = f"⚠ Untrusted source: {target}\n  Trust this source and continue? [y/N]: "
        ans = (await session.prompt_async(prompt)).strip().lower()
        return ans in {"y", "yes"}

    json_output = "--json" in args
    pos = [a for a in args if not a.startswith("--")]

    if sub == "add":
        if not pos:
            console.print("[yellow]usage: marketplace add <git-url> [--name N] [--trust] [--auto-update] [--no-clone][/yellow]")
            return
        name = flag_value(args, "--name")
        code = await marketplace_add(
            registry, home, env, pos[0],
            name=name, trust="--trust" in args, auto_update="--auto-update" in args,
            no_clone="--no-clone" in args, confirm=_confirm, json_output=json_output,
        )
        if code == EXIT_OK:  # 仅成功才标脏，避免失败/拒绝触发无意义 emit
            comp.mark_skills_dirty()
    elif sub == "list":
        marketplace_list(home, env, json_output=json_output)
    elif sub == "info":
        if not pos:
            console.print("[yellow]usage: marketplace info <name>[/yellow]")
            return
        marketplace_info(home, env, pos[0], json_output=json_output)
    elif sub in {"remove", "rm"}:
        if not pos:
            console.print("[yellow]usage: marketplace remove <name> [--keep-plugins][/yellow]")
            return
        from a2c_smcp.computer.cli.commands import build_mcp_callbacks

        name = pos[0]
        code = await marketplace_remove(
            registry, home, env, name,
            keep_plugins="--keep-plugins" in args, confirm=_confirm,
            mcp_cbs=build_mcp_callbacks(comp), json_output=json_output,
        )
        # 部分失败（uninstall 抛错）时返回非 EXIT_OK，但 marketplace 可能已被 prune 移除——按「是否仍在册」
        # 标脏，否则已连接 Agent 继续看到已删除的 SKILL（#258 复审四轮 🟡1）。
        if code == EXIT_OK or name not in _marketplaces(home, env):
            comp.mark_skills_dirty()
    elif sub == "refresh":
        target = pos[0] if pos else "all"
        # #260：部分源刷新成功 + 部分 validation 时退出码为 1，但 registry 已变——不能只看退出码，
        # 否则已连接 Agent 继续看到旧投影。
        before_state = _registry_fingerprints(comp.skill_registry)
        code = await marketplace_refresh(registry, home, env, target, json_output=json_output)
        after_state = _registry_fingerprints(comp.skill_registry)
        if code == EXIT_OK or before_state != after_state:
            comp.mark_skills_dirty()
    elif sub == "migrate":
        # 事务性迁移不进 REPL（D12）：只允许 dry-run；--apply 提示到 shell 执行。
        if "--apply" in args:
            console.print(
                "[yellow]marketplace migrate --apply must be run in a shell: "
                "a2c-computer marketplace migrate --apply[/yellow]"
            )
            return
        await marketplace_migrate(
            home,
            env,
            names=_flag_values(args, "--name") or None,
            apply=False,
            update_declarations="--update-declarations" in args,
            json_output=json_output,
        )
    elif sub == "set":
        # marketplace set <name> auto-update=<bool>
        if len(pos) < 2 or "=" not in pos[1]:
            console.print("[yellow]usage: marketplace set <name> auto-update=<bool>[/yellow]")
            return
        key, _, value = pos[1].partition("=")
        marketplace_set(home, env, pos[0], key, value, json_output=json_output)
    else:
        console.print(f"[yellow]unknown marketplace subcommand: {sub}[/yellow]")
