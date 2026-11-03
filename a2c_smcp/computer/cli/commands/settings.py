# -*- coding: utf-8 -*-
# filename: settings.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``settings`` 命令 handler（show / get / set / edit）/ Settings command handlers。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.5 / §5（S16，#69）。

与 marketplace.py 同范式：handler 取**显式资源**（``home`` / ``env`` + scope/key/value）返回退出码。读经
:func:`...scope.resolve_settings`（merged，含 policy first-source-wins）或单层 :func:`...scope.load_settings_file`；
写经 :func:`...scope.apply_write`（**数组整体替换**、``undefined`` 删键，§5.4）+ store 原子写/锁。

- **scope**：``user`` / ``project`` / ``local`` 可写；``flag`` / ``policy`` **只读**（set/edit 拒绝，退出码 1）；
  ``merged``（默认 show）= 五层合并视图。project/local 锚定进程 cwd（#116）。
- settings.json **无 version 字段**（复刻 CC passthrough，§5）；写不注 version/保护头（``header=None``）。
- ``edit`` 用 ``$EDITOR`` 打开该层文件，保存后经回调 reconcile（重跑 MCP 批准门控）。
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from a2c_smcp.computer.cli.commands import flag_value, format_settings_errors, resolved_settings_with_errors
from a2c_smcp.computer.cli.utils import console, console_err
from a2c_smcp.computer.settings.policy import resolve_policy_settings
from a2c_smcp.computer.settings.raw import (
    SettingsFileCorruptError,
    read_raw_object,
)
from a2c_smcp.computer.settings.schema import (
    FIELD_READ_ROOTS,
    POLICY_ONLY_FIELDS,
    TRUSTED_SCOPE_ONLY_FIELDS,
    SettingsScope,
    SettingsValidationError,
    validate_settings,
)
from a2c_smcp.computer.settings.scope import (
    apply_write,
    load_settings_file,
    user_settings_path,
    workdir_local_settings_path,
    workdir_project_settings_path,
)
from a2c_smcp.computer.settings.store import atomic_write_json, file_lock

EXIT_OK = 0
EXIT_USER_ERROR = 1

# 可读 scope（show/get）/ readable scopes；可写 scope（set/edit）/ writable scopes。
_READABLE = ("user", "project", "local", "flag", "policy", "merged")
_WRITABLE = {"user", "project", "local"}


def _err(msg: str, *, json_output: bool) -> int:
    if json_output:
        console.print_json(data={"error": msg})
    else:
        console.print(f"[red]✗ {msg}[/red]")
    return EXIT_USER_ERROR


def _writable_path(scope: str, env: Mapping[str, str] | None) -> tuple[Path, SettingsScope]:
    """解析可写 scope 的 settings.json 路径 + enum（project/local 锚 cwd，#116）；flag/policy/unknown → ``ValueError``。"""
    if scope == "user":
        return user_settings_path(env), SettingsScope.USER
    if scope in ("project", "local"):
        cwd = Path(os.getcwd())
        if scope == "project":
            return workdir_project_settings_path(cwd), SettingsScope.PROJECT
        return workdir_local_settings_path(cwd), SettingsScope.LOCAL
    raise ValueError(f"scope {scope!r} is read-only (writable: user|project|local)")


def _read_scope_with_errors(
    scope: str,
    home: Path,
    env: Mapping[str, str] | None,
    *,
    flag_path: Path | None,
) -> tuple[dict[str, Any], list[SettingsValidationError]] | None:
    """读单 scope（或 merged）settings dict **连同校验错误**；scope 非法 → ``None``。project/local 锚 cwd（#116）。

    #157：**每个 scope 都带出 errors**（非仅 merged）——``settings show --scope project`` 正是用户排查
    「我的 settings 莫名不生效」时最先跑的命令，若此处吞掉越权错误则诊断回路断裂（字段静默蒸发、
    ``settings get`` 还会答「not set in scope」主动误导）。协议 §3「响亮失败」；对拍 rust
    ``settings.rs::read_scope_with_errors``。呈现由调用方统一走 stderr（见 :func:`_emit_errors`）。
    Every scope carries its errors out; the callers surface them on stderr.
    """
    if scope == "merged":
        resolved_st = resolved_settings_with_errors(env, flag_path=flag_path)
        return resolved_st.settings, list(resolved_st.errors)
    if scope == "user":
        return load_settings_file(user_settings_path(env), SettingsScope.USER)
    if scope in ("project", "local"):
        cwd = Path(os.getcwd())
        path = workdir_project_settings_path(cwd) if scope == "project" else workdir_local_settings_path(cwd)
        enum = SettingsScope.PROJECT if scope == "project" else SettingsScope.LOCAL
        return load_settings_file(path, enum)
    if scope == "flag":
        if flag_path is None:
            return {}, []
        return load_settings_file(flag_path, SettingsScope.FLAG)
    if scope == "policy":
        # #161：policy 层 = resolve_policy_settings 的 first-source-wins 结果，再经 validate_settings
        # 做字段级容错校验（类型错等）。其他 scope 均经 load_settings_file → validate_settings 或
        # resolve_settings 内 validate_settings——policy 不能例外，否则 ``settings show --scope policy``
        # 对 type-error 字段零警告（企业白名单失效 = 安全事故）。对拍 rust 同构缺口（待跟修）。
        raw = resolve_policy_settings(env=env)
        return validate_settings(raw, SettingsScope.POLICY)
    return None


def _emit_errors(errors: list[SettingsValidationError]) -> None:
    """把 settings 校验错误打 **stderr**（#157）——stdout 恒为机读 JSON，``settings show | jq`` 须仍可用。

    Diagnostics on stderr; stdout stays pure JSON.
    """
    for line in format_settings_errors(errors):
        console_err.print(f"[yellow]{line}[/yellow]")


# ── handlers ──────────────────────────────────────────────────────────────────
def settings_show(
    home: Path,
    env: Mapping[str, str] | None,
    *,
    scope: str = "merged",
    flag_path: Path | None = None,
    json_output: bool = False,
) -> int:
    """展示某 scope 的 settings（默认 merged 合并视图）/ Show settings of a scope (default merged)。"""
    if scope not in _READABLE:
        return _err(f"unknown scope {scope!r} (expected {'|'.join(_READABLE)})", json_output=json_output)
    read = _read_scope_with_errors(scope, home, env, flag_path=flag_path)
    if read is None:  # 防御性：_READABLE 前置拦截后不可达（#116 起 project/local 锚 cwd 恒可读）
        return _err(f"unknown scope {scope!r}", json_output=json_output)
    data, errors = read
    _emit_errors(errors)  # #157：被 scope 越权过滤的字段在此有解释（stderr，不污染 stdout JSON）
    console.print_json(data=data)
    return EXIT_OK


def settings_get(
    home: Path,
    env: Mapping[str, str] | None,
    key: str,
    *,
    scope: str = "merged",
    flag_path: Path | None = None,
    json_output: bool = False,
) -> int:
    """读取单字段（默认 merged 视图）/ Read a single field (default merged view)。"""
    if scope not in _READABLE:
        return _err(f"unknown scope {scope!r} (expected {'|'.join(_READABLE)})", json_output=json_output)
    read = _read_scope_with_errors(scope, home, env, flag_path=flag_path)
    if read is None:  # 防御性：_READABLE 前置拦截后不可达（#116 起 project/local 锚 cwd 恒可读）
        return _err(f"unknown scope {scope!r}", json_output=json_output)
    data, errors = read
    # #157：越权字段会被过滤出 data —— 若不呈现，下面的 "not set in scope" 会**主动误导**（文件里明明写了）。
    _emit_errors(errors)
    if key not in data:
        return _err(f"key {key!r} not set in scope {scope!r}", json_output=json_output)
    console.print_json(data={key: data[key]})
    return EXIT_OK


def _warn_inert_fields(doc: Mapping[str, Any], enum: SettingsScope) -> None:
    """
    写后诊断：文件里存在**读面会被过滤掉**的键（#246 / A+，§5.6）/ Warn about fields that will be ignored。

    保留用户数据 ≠ 让它生效：project scope 的受信字段（审批门 enable 判据 / ``landingRoot`` / ``readRoots``）
    与任何可写 scope 的 policy-only 字段都会被 :func:`validate_settings` 过滤。写路径不再删除它们，因此必须
    明确告知「这些键在，但不生效」，否则用户看到 "✓ set" 会误以为已经配置成功。

    扫描**整份写后文档**而非只看本次写入的键：project settings 里长期存在一个 ``readRoots`` 时，用户改任何
    别的键都该被提醒「你那条 readRoots 是失效的」——这正是要补的配置诊断。
    Scans the whole written document, not just the key being set: a pre-existing inert field must be
    surfaced on any write, otherwise the user never learns it is dead config.

    **每次写只告警一次**（把命中的键合并成一行），走 stderr 避免污染 ``--json`` 的机读 stdout。
    At most one warning line per write; stderr only, so ``--json`` stdout stays machine-readable.
    """
    project_only = sorted(k for k in doc if enum is SettingsScope.PROJECT and k in TRUSTED_SCOPE_ONLY_FIELDS)
    policy_only = sorted(k for k in doc if k in POLICY_ONLY_FIELDS)
    if not project_only and not policy_only:
        return
    parts: list[str] = []
    if project_only:
        parts.append(
            f"{', '.join(map(repr, project_only))} (personal/trusted decision in git-tracked project settings; "
            "move it to settings.local.json or the user scope)"
        )
    if policy_only:
        parts.append(f"{', '.join(map(repr, policy_only))} (policy-only; settable only from the managed policy source)")
    console_err.print(
        f"[yellow]! present in the {enum.value} settings but will NOT take effect: {'; '.join(parts)}[/yellow]"
    )


def settings_set(
    home: Path,
    env: Mapping[str, str] | None,
    key: str,
    value: str,
    *,
    scope: str = "user",
    json_output: bool = False,
) -> int:
    """写单字段（user|project|local；flag/policy 只读）/ Write a single field (flag/policy read-only)。

    值解析：JSON 优先（``true`` / ``["a","b"]`` / ``{...}``），失败回退字面字符串。写经 ``apply_write``——
    对象递归深合并、**数组整体替换**、``null`` 删键（§5.4）。

    **raw 保真 RMW**（#246）：读**原始 JSON**（:func:`read_raw_object`，不经校验过滤视图）→ 只改目标键 →
    写回。被校验器过滤的字段（project 的 ``readRoots`` / ``landingRoot`` / ``enabledMcpjsonServers`` /
    ``enableAllProjectMcpServers``、policy-only 字段、非法 ``permissions`` 条目、未知键）**原样保留**，
    与 §5.6「单字段写、不整体重写文件」一致。目标文件 corrupt / 根非对象 → fail-closed（退出码 1，原文件不动）。
    Raw-fidelity RMW: read the raw object, touch only the target key. Corrupt targets fail closed.
    """
    if scope not in _WRITABLE:
        return _err(f"scope {scope!r} is read-only (writable: {'|'.join(sorted(_WRITABLE))})", json_output=json_output)
    try:
        path, enum = _writable_path(scope, env)
    except ValueError as e:
        return _err(str(e), json_output=json_output)

    try:
        parsed: Any = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        parsed = value  # 回退字面字符串 / fall back to a literal string

    # project scope 的 ``readRoots`` **不接收新值**（对齐 rust #253 `drop_project_read_roots`）：
    # 该键在 project scope 永不生效（读面 MUST 拒绝），写进 git-tracked 文件只会制造伪配置。
    # 注意与 A+ 的分工——**已有**的旧键不删除（不销毁用户数据），但**不写入**本次的新值。
    if enum is SettingsScope.PROJECT and key == FIELD_READ_ROOTS:
        reason = (
            "readRoots does not take effect in the project scope (personal/trusted decision; project "
            "settings.json is git-tracked) — set it in settings.local.json or the user scope"
        )
        console_err.print(f"[yellow]! {key!r} was NOT written to the project settings: {reason}[/yellow]")
        if json_output:
            console.print_json(data={"scope": scope, "key": key, "ignored": True, "reason": reason})
            return EXIT_OK
        console.print(f"[yellow]✓ ignored {key} in {scope} scope (not written)[/yellow]")
        return EXIT_OK

    try:
        with file_lock(path):
            existing = read_raw_object(path) or {}
            # 对象容器守卫（对齐 rust `ensure_object_container`）：本次要写对象、而盘上该键是非对象时
            # fail-closed——否则 `apply_write` 按「类型切换」整体替换，静默丢弃用户写的畸形值。
            if isinstance(parsed, Mapping) and key in existing and not isinstance(existing[key], Mapping):
                return _err(
                    f"{key!r} is a {type(existing[key]).__name__} in {path}, not an object; "
                    "refusing to replace it — fix or remove it first (the file was NOT modified)",
                    json_output=json_output,
                )
            updated = apply_write(existing, {key: parsed})
            atomic_write_json(path, updated)
    except SettingsFileCorruptError as e:
        return _err(str(e), json_output=json_output)

    _warn_inert_fields(updated, enum)
    if json_output:
        console.print_json(data={"scope": scope, "key": key, "value": parsed})
        return EXIT_OK
    console.print(f"[green]✓ set {key} in {scope} scope[/green]")
    return EXIT_OK


def settings_edit(
    home: Path,
    env: Mapping[str, str] | None,
    *,
    scope: str = "user",
    editor: str | None = None,
    reconcile_cb: Callable[[], Awaitable[None]] | None = None,
    json_output: bool = False,
) -> tuple[int, Callable[[], Awaitable[None]] | None]:
    """用 ``$EDITOR`` 打开该层 settings.json，保存后交回 reconcile 回调（由 dispatcher await）/ Open in $EDITOR。

    返回 ``(exit_code, post_save)``——``post_save`` 为 ``reconcile_cb``（仅编辑成功时非 None），由 async 调用方
    await（本函数同步，子进程阻塞期 REPL 暂停）。flag/policy 只读 → 退出码 1。
    """
    if scope not in _WRITABLE:
        return _err(f"scope {scope!r} is read-only (editable: {'|'.join(sorted(_WRITABLE))})", json_output=json_output), None
    try:
        path, _enum = _writable_path(scope, env)
    except ValueError as e:
        return _err(str(e), json_output=json_output), None

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{\n}\n", encoding="utf-8")  # 空骨架，便于编辑 / empty skeleton

    edit_cmd = editor or (env or os.environ).get("EDITOR") or (env or os.environ).get("VISUAL") or "vi"
    try:
        subprocess.run([*edit_cmd.split(), str(path)], check=True)  # noqa: S603 — $EDITOR 子进程（CLI 运行时，非沙箱）
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return _err(f"editor failed: {e}", json_output=json_output), None

    console.print(f"[green]✓ edited {scope} settings ({path})[/green]")
    return EXIT_OK, reconcile_cb


# ── REPL dispatcher ────────────────────────────────────────────────────────────
async def repl_dispatch(comp: Any, parts: list[str], *, session: Any) -> None:
    """``settings show|get|set|edit ...`` REPL 行解析 → 调 handler / Parse a REPL settings line。"""
    sub = parts[1].lower()
    args = parts[2:]
    home = comp.skill_home
    env = os.environ
    json_output = "--json" in args
    pos = [a for a in args if not a.startswith("--")]
    scope = flag_value(args, "--scope")

    if sub == "show":
        settings_show(home, env, scope=scope or "merged", json_output=json_output)
    elif sub == "get":
        if not pos:
            console.print("[yellow]usage: settings get <key> [--scope user|project|local|flag|policy|merged][/yellow]")
            return
        settings_get(home, env, pos[0], scope=scope or "merged", json_output=json_output)
    elif sub == "set":
        if len(pos) < 2:
            console.print("[yellow]usage: settings set <key> <value> [--scope user|project|local][/yellow]")
            return
        # value = key（parts[2]）之后、首个 --flag token 前的所有 token（重建含空格 JSON；停在**真 flag token**、
        # 非子串——修 fix-review #3：旧 _strip_scope_flag 会误截含 "--scope" 子串的 value）。usage 契约：key 在 value 前。
        key = pos[0]
        value_tokens: list[str] = []
        for tok in parts[3:]:
            if tok.startswith("--"):
                break
            value_tokens.append(tok)
        value = " ".join(value_tokens)
        code = settings_set(home, env, key, value, scope=scope or "user", json_output=json_output)
        _emit_keys = {"installedPlugins", "enabledPlugins", "enabledMcpjsonServers", "disabledMcpjsonServers", "enableAllProjectMcpServers"}
        if code == EXIT_OK and key in _emit_keys:
            comp.mark_skills_dirty()
    elif sub == "edit":
        code, post_save = settings_edit(home, env, scope=scope or "user", reconcile_cb=None, json_output=json_output)
        if code == EXIT_OK:
            comp.mark_skills_dirty()  # 编辑可能改 enabledPlugins/MCP 字段 → 触发去抖 emit
            if post_save is not None:
                await post_save()
    else:
        console.print(f"[yellow]unknown settings subcommand: {sub}[/yellow]")
