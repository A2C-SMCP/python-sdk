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
  ``merged``（默认 show）= 六层合并视图。project/local 需 active workdir。
- settings.json **无 version 字段**（复刻 CC passthrough，§5）；写不注 version/保护头（``header=None``）。
- ``edit`` 用 ``$EDITOR`` 打开该层文件，保存后经回调 reconcile（能力层对账 + 重跑 MCP 批准门控）。
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from a2c_smcp.computer.cli.commands import flag_value, resolved_settings
from a2c_smcp.computer.cli.utils import console
from a2c_smcp.computer.settings.policy import resolve_policy_settings
from a2c_smcp.computer.settings.schema import SettingsScope
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


def _writable_path(scope: str, active_workdir: Path | None, env: Mapping[str, str] | None) -> tuple[Path, SettingsScope]:
    """解析可写 scope 的 settings.json 路径 + enum；flag/policy/unknown → ``ValueError``。"""
    if scope == "user":
        return user_settings_path(env), SettingsScope.USER
    if scope in ("project", "local"):
        if active_workdir is None:
            raise ValueError(f"scope {scope!r} requires an active workdir (use --add-dir at startup)")
        if scope == "project":
            return workdir_project_settings_path(active_workdir), SettingsScope.PROJECT
        return workdir_local_settings_path(active_workdir), SettingsScope.LOCAL
    raise ValueError(f"scope {scope!r} is read-only (writable: user|project|local)")


def _read_scope(
    scope: str,
    home: Path,
    env: Mapping[str, str] | None,
    *,
    registered_workdirs: tuple[Path, ...],
    active_workdir: Path | None,
    flag_path: Path | None,
) -> dict[str, Any] | None:
    """读单 scope（或 merged）settings dict；scope 非法或缺 active workdir → ``None``（调用方报错）。"""
    if scope == "merged":
        return resolved_settings(registered_workdirs, active_workdir, env, flag_path=flag_path)
    if scope == "user":
        return load_settings_file(user_settings_path(env), SettingsScope.USER)[0]
    if scope in ("project", "local"):
        if active_workdir is None:
            return None
        path = workdir_project_settings_path(active_workdir) if scope == "project" else workdir_local_settings_path(active_workdir)
        enum = SettingsScope.PROJECT if scope == "project" else SettingsScope.LOCAL
        return load_settings_file(path, enum)[0]
    if scope == "flag":
        if flag_path is None:
            return {}
        return load_settings_file(flag_path, SettingsScope.FLAG)[0]
    if scope == "policy":
        return resolve_policy_settings(env=env)
    return None


# ── handlers ──────────────────────────────────────────────────────────────────
def settings_show(
    home: Path,
    env: Mapping[str, str] | None,
    *,
    scope: str = "merged",
    registered_workdirs: tuple[Path, ...] = (),
    active_workdir: Path | None = None,
    flag_path: Path | None = None,
    json_output: bool = False,
) -> int:
    """展示某 scope 的 settings（默认 merged 合并视图）/ Show settings of a scope (default merged)。"""
    if scope not in _READABLE:
        return _err(f"unknown scope {scope!r} (expected {'|'.join(_READABLE)})", json_output=json_output)
    data = _read_scope(scope, home, env, registered_workdirs=registered_workdirs, active_workdir=active_workdir, flag_path=flag_path)
    if data is None:
        return _err(f"scope {scope!r} requires an active workdir (use --add-dir at startup)", json_output=json_output)
    console.print_json(data=data)
    return EXIT_OK


def settings_get(
    home: Path,
    env: Mapping[str, str] | None,
    key: str,
    *,
    scope: str = "merged",
    registered_workdirs: tuple[Path, ...] = (),
    active_workdir: Path | None = None,
    flag_path: Path | None = None,
    json_output: bool = False,
) -> int:
    """读取单字段（默认 merged 视图）/ Read a single field (default merged view)。"""
    if scope not in _READABLE:
        return _err(f"unknown scope {scope!r} (expected {'|'.join(_READABLE)})", json_output=json_output)
    data = _read_scope(scope, home, env, registered_workdirs=registered_workdirs, active_workdir=active_workdir, flag_path=flag_path)
    if data is None:
        return _err(f"scope {scope!r} requires an active workdir (use --add-dir at startup)", json_output=json_output)
    if key not in data:
        return _err(f"key {key!r} not set in scope {scope!r}", json_output=json_output)
    console.print_json(data={key: data[key]})
    return EXIT_OK


def settings_set(
    home: Path,
    env: Mapping[str, str] | None,
    key: str,
    value: str,
    *,
    scope: str = "user",
    active_workdir: Path | None = None,
    json_output: bool = False,
) -> int:
    """写单字段（user|project|local；flag/policy 只读）/ Write a single field (flag/policy read-only)。

    值解析：JSON 优先（``true`` / ``["a","b"]`` / ``{...}``），失败回退字面字符串。写经 ``apply_write``——
    对象递归深合并、**数组整体替换**、``null`` 删键（§5.4）。
    """
    if scope not in _WRITABLE:
        return _err(f"scope {scope!r} is read-only (writable: {'|'.join(sorted(_WRITABLE))})", json_output=json_output)
    try:
        path, enum = _writable_path(scope, active_workdir, env)
    except ValueError as e:
        return _err(str(e), json_output=json_output)

    try:
        parsed: Any = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        parsed = value  # 回退字面字符串 / fall back to a literal string

    with file_lock(path):
        existing, _errors = load_settings_file(path, enum)
        updated = apply_write(existing, {key: parsed})
        atomic_write_json(path, updated)

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
    active_workdir: Path | None = None,
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
        path, _enum = _writable_path(scope, active_workdir, env)
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
    registered = comp._registered_workdirs
    aw = comp.active_workdir

    if sub == "show":
        settings_show(
            home, env, scope=scope or "merged",
            registered_workdirs=registered, active_workdir=aw, json_output=json_output,
        )
    elif sub == "get":
        if not pos:
            console.print("[yellow]usage: settings get <key> [--scope user|project|local|flag|policy|merged][/yellow]")
            return
        settings_get(
            home, env, pos[0], scope=scope or "merged",
            registered_workdirs=registered, active_workdir=aw, json_output=json_output,
        )
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
        code = settings_set(home, env, key, value, scope=scope or "user", active_workdir=aw, json_output=json_output)
        _emit_keys = {"enabledPlugins", "enabledMcpjsonServers", "disabledMcpjsonServers", "enableAllProjectMcpServers"}
        if code == EXIT_OK and key in _emit_keys:
            comp.mark_skills_dirty()
    elif sub == "edit":
        code, post_save = settings_edit(home, env, scope=scope or "user", active_workdir=aw, reconcile_cb=None, json_output=json_output)
        if code == EXIT_OK:
            comp.mark_skills_dirty()  # 编辑可能改 enabledPlugins/MCP 字段 → 触发去抖 emit
            if post_save is not None:
                await post_save()
    else:
        console.print(f"[yellow]unknown settings subcommand: {sub}[/yellow]")
