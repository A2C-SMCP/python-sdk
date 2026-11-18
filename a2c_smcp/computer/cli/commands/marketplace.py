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
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from unicodedata import normalize
from urllib.parse import unquote, urlsplit

from rich.markup import escape
from rich.table import Table

from a2c_smcp.computer.cli.commands import McpCallbacks, files_only_non_plugin_bundle_ids, flag_value
from a2c_smcp.computer.cli.progress import clone_spinner, refresh_summary_table
from a2c_smcp.computer.cli.utils import console, console_err
from a2c_smcp.computer.settings.installer import PluginInstallError, uninstall_plugin
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
    load_installed_plugins,
    load_known_marketplaces,
    update_known_marketplaces,
)
from a2c_smcp.computer.skills.home import marketplace_skill_dir
from a2c_smcp.computer.skills.marketplace_identity import (
    MANIFEST_NAME_BASELINE_FIELD,
    REASON_UNRESOLVABLE_NAME,
    MarketplaceIdentityError,
    MarketplaceNameChangeError,
    read_manifest_marketplace_name,
    registered_name_for_source,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.sources import GITHUB_HOST, SkillSourceError, normalize_repo_shorthand
from a2c_smcp.computer.skills.staging import (
    SkillStagingError,
    add_marketplace_source,
    registration_violation,
    stage_marketplace_skills,
)
from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE, git_url_for_display, known_git_source_for_display

# trust 确认回调：接收待信任源（url/name）→ 返回是否继续 / Trust confirm callback。
ConfirmFn = Callable[[str], Awaitable[bool]]

# 退出码语义（§4.6）/ Exit code semantics。
EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_NETWORK_ERROR = 2


# ── 输出辅助 / output helpers ─────────────────────────────────────────────────
# ``details`` 中与 :func:`_err` 保留字段同名的键：展开前过滤，避免覆盖类别码 / 人读文案（#257 先例）。
# 注：``code``（退出码）**不在**此集——它是签名参数，``**extra`` 撞名会在调用点 TypeError，根本到不了过滤；
# 而结构化 payload 的机器码键就叫 ``code``（:meth:`MarketplaceIdentityError.payload_details`），须放行。
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
    console.print(f"[green]✓ {msg}[/green]")
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


# 凭据样判据的**硬上界**解码轮数（每轮 = 判残形 ``%`` → percent-decode → NFKC）/ Hard cap on decode rounds.
# **#282**：由固定 3 轮改为**不动点循环**（见 :func:`_credential_like`）。轮数**不承担**多层编码的覆盖面
# ——那由「残形 ``%`` 即 fail-closed」保证（与 rust #285 同口径），故 ``%25253A`` / ``%2525253A`` 之流
# 在**第一轮**就返回 True，与层数无关。轮数真正承担的是「NFKC 归一后再看 ``%`` / ``：`` / ``＠``」：
# 全角 ``％３Ａ`` 要**两轮**才现出 ASCII ``%``（故上界不可退化为 1），其余输入一轮即达不动点。
# 上界本身只保证终止。
_MAX_DECODE_ROUNDS = 10


def _credential_like(text: str) -> bool:
    """
    文本是否含凭据样文本（userinfo ``@`` / 密码段 ``:`` / 残形 percent 转义）/
    Whether text carries credential-like text.

    **#270**：做「percent-decode → NFKC」迭代——挡住 ``alice%3APW%40host``、percent-encoded 全角
    ``%EF%BC%9A``/``%EF%BC%A0``、双重/三重编码 ``%253A``/``%25253A``，以及**全角百分号**
    ``alice％３ＡPW``（NFKC 会先把它归一成 ``%3A``，下一轮才 decode；单轮「decode 后归一」会漏）。

    **#282**：① **残形 percent 转义一律 fail-closed**——任一解码轮里出现 ``%`` 即判可疑
    （对齐 rust-sdk#285 ``seg.contains([':', '@', '%'])``）：percent 转义本身说明来源被编码过（还能再解
    一层，或本就不该出现在注册名 / 来源里），故四层 ``alice%2525253APW_LEAK`` 与解码后本可干净的
    ``repo%20name`` 一并拒。**这一条让覆盖面不依赖解码深度**（>N 层不再有漏判残余）。
    ② 解码为**不动点循环**（本轮结果与上轮相同即提前退出）：轮次仍承担①——全角 ``％`` 要经一轮 NFKC
    才现出 ASCII ``%``；``@`` / ``:`` 也在 NFKC 归一（全角 ``：`` / ``＠``）+ decode 之后判定。
    ``_MAX_DECODE_ROUNDS`` **只保证终止**（``unquote`` 只减不增，NFKC 幂等，有变化的轮次有限；
    **不**宣称「每轮严格缩短」——NFKC 存在 1→2 兼容展开，如 ``ﬁ``→``fi``）。轮次实际承担的是
    「NFKC 归一后再看 ``%``」：全角 ``％３Ａ`` 类输入要**两轮**（第一轮才把 ``％`` 归一成 ``%``），
    故上界不能退化为 1；更深的编码由 ① 的 ``%`` 判据兜住，与轮数无关。
    """
    probe = text
    for _ in range(_MAX_DECODE_ROUNDS):
        # ① 残形 percent 转义 ⇒ 不可判定（含上一轮 NFKC 从全角 ``％`` 归一出来的 ``%``）
        if "%" in probe:
            return True
        decoded = normalize("NFKC", unquote(probe))
        if decoded == probe:  # 不动点：再迭代也不变
            break
        probe = decoded
    return "@" in probe or ":" in probe


def _error_url_display(raw: str) -> str:
    """
    ``marketplace add`` **失败文案**里的 URL 展示 / Display-safe URL in add-failure messages.

    在 #261 展示脱敏（去 userinfo / query / fragment）之上，再挡住 **path 任意段**携带的凭据样文本——
    ``ssh://-/alice:PW_LEAK@example.com``、``https://host/team/alice:PW@example.com/repo``（中间段）、
    ``.../alice:PW@example.com/``（尾斜杠）的展示值都仍保留 path，直接回显会把凭据写进 stdout /
    ``--json``。空 path 或解析失败同样 fail-closed。
    判定对象是**原始 source** 的 path 段（渲染才用展示串）：展示层会把空格 / 非 ASCII ``quote()`` 成
    ``%XX``，「残形 ``%`` 即拒」若跑在展示串上会把干净 URL 误打码。
    **范围**：仅 add 失败文案；``list`` / ``info`` / confirm 提示 / 成功 ``--json`` 的 ``url`` 仍按 #261
    的 known-source 契约保留 path（见 CHANGELOG 的范围说明）。
    """
    shown = git_url_for_display(raw)
    if shown == REDACTED_GIT_SOURCE:
        return shown
    # 判据取**原始 source** 的 path 段，不取展示串：展示层会对空格 / 非 ASCII / ``|`` / ``\`` 等做
    # ``quote()``（``my team``→``my%20team``、``团队``→``%E5%9B%A2%E9%98%9F``），拿展示串跑「残形 ``%``
    # 即拒」会把**源里根本没有编码**的干净 URL 整条打码（#282 审查 🟡）。
    try:
        path = urlsplit(raw).path
    except ValueError:
        return REDACTED_GIT_SOURCE
    segments = [seg for seg in path.split("/") if seg]
    if not segments:  # 无 path：无法判定来源内容 → fail-closed
        return REDACTED_GIT_SOURCE
    if any(_credential_like(seg) for seg in segments):
        return REDACTED_GIT_SOURCE
    return shown


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
            raise violation  # 同名不同源 / 断链引用 / baseline 冲突（fail-closed）
        prior_record: Mapping[str, Any] = prior if isinstance(prior, Mapping) else {}
        known[name] = {
            "source": dict(source),
            "installLocation": str(clone_dir.resolve()),
            "autoUpdate": bool(auto_update or prior_record.get("autoUpdate")),
            "manifestNameBaseline": prior_record.get(MANIFEST_NAME_BASELINE_FIELD) or name,
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

    final_name = outcome["existing"] or outcome["registered"]
    if final_name is None:  # 防御：mutator 既没判幂等、也没登记（不应发生）
        return _err("failed to register the marketplace intent (see logs)", json_output=json_output)
    record = _marketplaces(home, env).get(final_name) or {}
    try:
        _record_trust(final_name, env)
    except SettingsFileCorruptError as e:
        console_err.print(
            f"[yellow]! marketplace {final_name!r} is registered but its trust entry could not be persisted: {e}[/yellow]"
        )
    if json_output:
        console.print_json(
            data={
                "registered": final_name,
                "url": known_git_source_for_display(url),
                "installLocation": record.get("installLocation"),
            }
        )
        return EXIT_OK
    if outcome["existing"] is not None:
        return _ok(f"marketplace {final_name!r} is already registered (no clone needed)")
    return _ok(f"registered marketplace {final_name!r} from the local clone (no clone)")


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
            code="invalid-source",
        )
    if name is not None and not is_valid_marketplace_name(name):
        # ``--name`` 现在是「期望清单名」，先按同一语法契约快速拒绝（避免白跑一次 clone）。
        return _err(
            f"--name {name!r} is not a valid marketplace name (strict kebab-case, 1-64); pass the manifest's name",
            json_output=json_output,
            error_code="validation",
            code="invalid-name",
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

    # 注册已提交 → 落 trust（失败仅 WARN，不回滚已成功的注册）。
    try:
        _record_trust(final_name, env)
    except SettingsFileCorruptError as e:
        console_err.print(f"[yellow]! added {final_name!r} but failed to persist its trust entry: {e}[/yellow]")

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


def marketplace_list(home: Path, env: Mapping[str, str] | None, *, json_output: bool = False) -> int:
    """列出所有已知 marketplace（trusted / clone 状态 / 上次刷新 / auto_update）/ List known marketplaces。"""
    mps = _marketplaces(home, env)
    trusted = _load_trusted(env)

    if json_output:
        console.print_json(
            data=[
                {
                    "name": nm,
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
        sha = rec.get("commitSha") or ""
        table.add_row(
            nm,
            url,
            "✓" if nm in trusted else "—",
            "on" if rec.get("autoUpdate") else "off",
            str(rec.get("lastUpdated") or "—"),
            sha[:10] if sha else "—",
        )
    console.print(table)
    return EXIT_OK


def marketplace_info(home: Path, env: Mapping[str, str] | None, name: str, *, json_output: bool = False) -> int:
    """marketplace 详情：URL / clone 路径 / commit / plugins[]（含 installed 状态）/ auto_update / trusted。"""
    rec = _marketplaces(home, env).get(name)
    if rec is None:
        return _err(f"unknown marketplace: {name!r}", json_output=json_output)

    installed = load_installed_plugins(home, env).get("plugins") or {}
    plugins = [pid for pid in installed if pid.endswith(f"@{name}")]
    url = _display_source_url(rec)
    info: dict[str, Any] = {
        "name": name,
        "url": url,
        "installLocation": rec.get("installLocation"),
        "commitSha": rec.get("commitSha"),
        "autoUpdate": bool(rec.get("autoUpdate", False)),
        "trusted": name in _load_trusted(env),  # 信任按 name 判定（§5.3.1）
        "lastUpdated": rec.get("lastUpdated"),
        "installedPlugins": plugins,
    }
    if json_output:
        console.print_json(data=info)
        return EXIT_OK
    table = Table(title=f"Marketplace · {name}", show_header=False)
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")
    for k in ("url", "installLocation", "commitSha", "autoUpdate", "trusted", "lastUpdated"):
        # 缺失字段留空（与 docstring / JSON null 语义一致），不渲染字面 "None"（#261 复审 🟡）。
        table.add_row(k, "" if info[k] is None else str(info[k]))
    table.add_row("installedPlugins", ", ".join(plugins) if plugins else "—")
    console.print(table)
    return EXIT_OK


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
        rec = mps.get(nm)
        if rec is None:
            rows.append({"name": nm, "status": "missing"})
            continue
        before = rec.get("commitSha")
        try:
            with clone_spinner(f"Refreshing {nm}...", enabled=not json_output):
                registered = await stage_marketplace_skills(
                    nm, rec.get("source") or {}, registry, home, auto_update=bool(rec.get("autoUpdate", False)), refresh=True, env=env
                )
        except MarketplaceNameChangeError as e:
            # 改名拒绝：结构化 outcome，非 updated/unchanged；旧注册与旧能力原样保留。
            rows.append({"name": nm, "status": "validation", "error": str(e), **e.payload_details()})
            exit_code = EXIT_USER_ERROR
            continue
        after = _marketplaces(home, env).get(nm) or {}
        status = "updated" if after.get("commitSha") != before else "unchanged"
        rows.append({"name": nm, "status": status, "skills": len(registered)})

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
        before_skills = {ref.get("name") for ref, _orphaned in comp.skill_registry.all_refs()}
        code = await marketplace_refresh(registry, home, env, target, json_output=json_output)
        after_skills = {ref.get("name") for ref, _orphaned in comp.skill_registry.all_refs()}
        if code == EXIT_OK or before_skills != after_skills:
            comp.mark_skills_dirty()
    elif sub == "set":
        # marketplace set <name> auto-update=<bool>
        if len(pos) < 2 or "=" not in pos[1]:
            console.print("[yellow]usage: marketplace set <name> auto-update=<bool>[/yellow]")
            return
        key, _, value = pos[1].partition("=")
        marketplace_set(home, env, pos[0], key, value, json_output=json_output)
    else:
        console.print(f"[yellow]unknown marketplace subcommand: {sub}[/yellow]")
