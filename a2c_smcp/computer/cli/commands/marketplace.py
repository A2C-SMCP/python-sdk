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

from rich.table import Table

from a2c_smcp.computer.cli.commands import McpCallbacks, files_only_non_plugin_bundle_ids, flag_value
from a2c_smcp.computer.cli.progress import clone_spinner, refresh_summary_table
from a2c_smcp.computer.cli.utils import console
from a2c_smcp.computer.settings.installer import uninstall_plugin
from a2c_smcp.computer.settings.reconciler import prune_marketplaces
from a2c_smcp.computer.settings.schema import (
    FIELD_TRUSTED_MARKETPLACES,
    SettingsScope,
    is_valid_git_url,
    is_valid_marketplace_name,
)
from a2c_smcp.computer.settings.scope import apply_write, load_settings_file, user_settings_path
from a2c_smcp.computer.settings.store import (
    atomic_write_json,
    file_lock,
    load_installed_plugins,
    load_known_marketplaces,
    update_known_marketplaces,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.sources import GITHUB_HOST, SkillSourceError, normalize_repo_shorthand
from a2c_smcp.computer.skills.staging import stage_marketplace_skills

# trust 确认回调：接收待信任源（url/name）→ 返回是否继续 / Trust confirm callback。
ConfirmFn = Callable[[str], Awaitable[bool]]

# 退出码语义（§4.6）/ Exit code semantics。
EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_NETWORK_ERROR = 2


# ── 输出辅助 / output helpers ─────────────────────────────────────────────────
def _err(msg: str, *, json_output: bool, code: int = EXIT_USER_ERROR) -> int:
    if json_output:
        console.print_json(data={"error": msg})
    else:
        console.print(f"[red]✗ {msg}[/red]")
    return code


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


def default_marketplace_name(url: str) -> str | None:
    """从 git URL 末段派生严格-kebab marketplace 名（去 ``.git``、非字母数字折叠为 ``-``）/ Derive a kebab name from URL。"""
    tail = url.rstrip("/")
    seg = re.split(r"[/:]", tail)[-1]
    if seg.endswith(".git"):
        seg = seg[:-4]
    slug = re.sub(r"[^a-z0-9]+", "-", seg.lower()).strip("-")
    return slug if slug and is_valid_marketplace_name(slug) else None


# ── trust 持久化（user scope settings.json）/ trust persistence ────────────────
# 键空间 = marketplace **name**（设计 §5.3.1 规范表：trustedMarketplaces 元素=name，与 blockedMarketplaces
# 同键空间、line 458 blocked 优先可比；#80 strict/白名单按 name 读）。**不**用 URL，避免 #80 落地时键型不一致 +
# 已写入 user settings 的迁移成本。
def _load_trusted(env: Mapping[str, str] | None) -> set[str]:
    existing, _errors = load_settings_file(user_settings_path(env), SettingsScope.USER)
    return {n for n in existing.get(FIELD_TRUSTED_MARKETPLACES, []) if isinstance(n, str)}


def _record_trust(name: str, env: Mapping[str, str] | None) -> None:
    """把 marketplace ``name`` 追加进 user ``settings.json`` 的 ``trustedMarketplaces``（持锁原子 RMW + dedup）。"""
    path = user_settings_path(env)
    with file_lock(path):
        existing, _errors = load_settings_file(path, SettingsScope.USER)
        current = [n for n in existing.get(FIELD_TRUSTED_MARKETPLACES, []) if isinstance(n, str)]
        if name not in current:
            current.append(name)
        atomic_write_json(path, apply_write(existing, {FIELD_TRUSTED_MARKETPLACES: current}))


def _revoke_trust(name: str, env: Mapping[str, str] | None) -> None:
    """从 user ``settings.json`` 的 ``trustedMarketplaces`` 移除 marketplace ``name``（remove 时调，避免只增不减）。"""
    path = user_settings_path(env)
    with file_lock(path):
        existing, _errors = load_settings_file(path, SettingsScope.USER)
        current = [n for n in existing.get(FIELD_TRUSTED_MARKETPLACES, []) if isinstance(n, str) and n != name]
        atomic_write_json(path, apply_write(existing, {FIELD_TRUSTED_MARKETPLACES: current}))


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
    """添加新 marketplace（首次 trust y/N，默认 eager clone）/ Add a marketplace (trust on first add, eager clone)。"""
    url = normalize_marketplace_url(git_url)
    if url is None:
        return _err(f"not a well-formed git url or owner/repo shorthand: {git_url!r}", json_output=json_output)
    mp_name = name or default_marketplace_name(url)
    if not mp_name or not is_valid_marketplace_name(mp_name):
        return _err(f"cannot derive a valid marketplace name from {git_url!r}; pass --name", json_output=json_output)

    if mp_name in _marketplaces(home, env):
        return _err(f"marketplace name conflict: {mp_name!r} already exists", json_output=json_output)

    # trust 门（§10.5/§11，#95 收紧）/ trust gate:
    #   - 非交互（confirm is None）：``--trust`` **无条件**强制（契约「非交互须 --trust」）。持久化的
    #     user-scope trust **不豁免**——能走到「已 trusted 但不在 known_marketplaces」的只有 desync/
    #     泄漏态（remove 未撤销 / clone 失败 orphan / 跨 A2C_SKILL_HOME；trust 落全局 user settings、
    #     与 home 解耦），陈旧 trust 不得静默授权一次全新的远端 clone+注册。
    #   - 交互（confirm 非空，REPL）：已 trusted 的 name 跳过重复 prompt；否则弹 confirm。该跳过
    #     **仅 desync 态可达**——line 160 同名拦截保证走到此处 name 必不在 known_marketplaces，故"已
    #     trusted"⇒desync；保留是为避免 clone 失败后重试同一 URL 时重复弹窗。残留：trust 按 name 键控，
    #     <新URL> 套 <旧trusted名> 会跳过 confirm 不再展示 url——交互态由在场真人把关，威胁模型有意接受
    #     （§10.5；回归守卫 test_add_pretrusted_name_interactive_skips_prompt）。
    # 门控/落盘按 **name**（§5.3.1），prompt 仍展示 url 供用户判断来源。
    if not trust:
        if confirm is None:
            return _err(f"untrusted marketplace {mp_name!r} ({url}); pass --trust to confirm non-interactively", json_output=json_output)
        if mp_name not in _load_trusted(env) and not await confirm(url):
            return _err("aborted by user (untrusted)", json_output=json_output)
    _record_trust(mp_name, env)

    if no_clone:
        # 仅注册意图（不推荐，§4.2 debug 用）：记 known_marketplaces 但不 clone/stage。
        update_known_marketplaces(
            lambda f: f["marketplaces"].__setitem__(  # type: ignore[func-returns-value]
                mp_name, {"source": {"type": "git", "url": url}, "installLocation": "", "autoUpdate": auto_update}
            )
            or f,
            home,
            env,
        )
        return _ok(f"registered marketplace intent {mp_name!r} (no clone)")

    source = {"type": "git", "url": url}
    with clone_spinner(f"Cloning {mp_name}...", enabled=not json_output):
        registered = await stage_marketplace_skills(mp_name, source, registry, home, auto_update=auto_update, env=env)

    # 成功判定：stage 失败降级（不抛、返回空 + 不写 known_marketplaces）；据物化记录是否落盘判定。
    if mp_name not in _marketplaces(home, env):
        return _err(f"clone/refresh failed for {url!r} (see logs)", json_output=json_output, code=EXIT_NETWORK_ERROR)

    if json_output:
        console.print_json(data={"added": mp_name, "url": url, "skills": len(registered)})
        return EXIT_OK
    return _ok(f"added {mp_name!r} — cloned, {len(registered)} skill(s) found")


def marketplace_list(home: Path, env: Mapping[str, str] | None, *, json_output: bool = False) -> int:
    """列出所有已知 marketplace（trusted / clone 状态 / 上次刷新 / auto_update）/ List known marketplaces。"""
    mps = _marketplaces(home, env)
    trusted = _load_trusted(env)

    if json_output:
        console.print_json(
            data=[
                {
                    "name": nm,
                    "url": _source_url(rec),
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
        url = _source_url(rec) or ""
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
    url = _source_url(rec)
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
        table.add_row(k, str(info[k]))
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
    if not keep_plugins:
        installed = load_installed_plugins(home, env).get("plugins") or {}
        victims = [pid for pid in installed if pid.endswith(f"@{name}")]
        remove_cb = mcp_cbs.remove_server if mcp_cbs is not None else None
        # 回收判据数据源：有 Computer 走其声明面（含 flag/embed）；无 Computer（非交互 ledger-only、
        # ``remove_cb is None`` ⇒ 判据结果实际未被消费）走 durable scopes。
        non_plugin_cb = mcp_cbs.non_plugin_bundle_ids if mcp_cbs is not None else files_only_non_plugin_bundle_ids(env)
        for pid in victims:
            if await uninstall_plugin(
                pid, registry, home, non_plugin_bundle_ids=non_plugin_cb, env=env, remove_server=remove_cb,
            ):
                uninstalled.append(pid)

    pruned = prune_marketplaces([name], registry, home, env=env)
    _revoke_trust(name, env)  # 撤销信任，避免 trustedMarketplaces 只增不减（user scope）
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
    """``git pull`` 失败则全量重 clone；与缓存 plugin 集合对账 + 失败汇总（§10.4）/ Refresh marketplaces。"""
    mps = _marketplaces(home, env)
    names = list(mps) if target == "all" else [target]

    rows: list[dict[str, Any]] = []
    for nm in names:
        rec = mps.get(nm)
        if rec is None:
            rows.append({"name": nm, "status": "missing"})
            continue
        before = rec.get("commitSha")
        with clone_spinner(f"Refreshing {nm}...", enabled=not json_output):
            registered = await stage_marketplace_skills(
                nm, rec.get("source") or {}, registry, home, auto_update=bool(rec.get("autoUpdate", False)), refresh=True, env=env
            )
        after = _marketplaces(home, env).get(nm) or {}
        status = "updated" if after.get("commitSha") != before else "unchanged"
        rows.append({"name": nm, "status": status, "skills": len(registered)})

    if json_output:
        console.print_json(data=rows)
        return EXIT_OK
    console.print(refresh_summary_table(rows))
    updated = sum(1 for r in rows if r["status"] == "updated")
    failed = sum(1 for r in rows if r["status"] == "missing")
    console.print(f"[dim]{len(rows)} marketplace(s) · {updated} updated · {failed} failed[/dim]")
    return EXIT_OK


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

        code = await marketplace_remove(
            registry, home, env, pos[0],
            keep_plugins="--keep-plugins" in args, confirm=_confirm,
            mcp_cbs=build_mcp_callbacks(comp), json_output=json_output,
        )
        if code == EXIT_OK:
            comp.mark_skills_dirty()
    elif sub == "refresh":
        target = pos[0] if pos else "all"
        code = await marketplace_refresh(registry, home, env, target, json_output=json_output)
        if code == EXIT_OK:
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
