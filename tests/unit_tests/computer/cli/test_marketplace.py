# -*- coding: utf-8 -*-
# filename: test_marketplace.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``marketplace`` 命令 handler 单元测试（v0.2.1 #68）/ Marketplace command handler unit tests。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §4.2 / §10.5。

测试意图 / Test intentions（本地 ``git init --bare`` 裸仓作 file:// 源，无网络；tmp home + XDG 重定向隔离）:
- URL / name 归一（纯函数，无需 git）；
- add：trust 缺 flag 且无 confirm → 退出码 1、不落盘；confirm=yes → clone + 注册 + 落 known_marketplaces；
  confirm=no → 退出码 1；同名冲突 → 退出码 1；--trust 直通；
- list / info / set(auto-update) / remove（级联 prune）；
- ``--json`` 输出形态。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a2c_smcp.computer.cli.commands import marketplace as marketplace_mod
from a2c_smcp.computer.cli.commands.marketplace import (
    _load_trusted,
    _record_trust,
    default_marketplace_name,
    marketplace_add,
    marketplace_info,
    marketplace_list,
    marketplace_refresh,
    marketplace_remove,
    marketplace_set,
    normalize_marketplace_url,
)
from a2c_smcp.computer.settings.installer import PluginInstallError
from a2c_smcp.computer.settings.scope import user_settings_path
from a2c_smcp.computer.settings.store import (
    known_marketplaces_path,
    load_installed_plugins,
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
    update_known_marketplaces,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

_GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]


# ── 本地裸仓 + 环境隔离辅助 / local bare-repo + env-isolation helpers ──────────
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: a skill\nlicense: MIT\n---\n# {name}\nbody\n"


def _marketplace_files() -> dict[str, str]:
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": "audit", "version": "1.0.0"}],
    }
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugins/audit/skills/audit-code/SKILL.md": _skill_md("audit-code"),
        "plugins/audit/skills/lint/SKILL.md": _skill_md("lint"),
    }


def _make_bare(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    work = tmp_path / f"{name}-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    _write(work, files)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / f"{name}.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def _env(tmp_path: Path) -> dict[str, str]:
    """A2C_SKILL_HOME → 物化根；XDG_CONFIG_HOME → user settings（trust 落盘）/ isolate home + user settings。"""
    return {**os.environ, "A2C_SKILL_HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    return h


async def _yes(_target: str) -> bool:
    return True


async def _no(_target: str) -> bool:
    return False


# ── 纯函数：URL / name 归一 / pure normalization ──────────────────────────────
def test_normalize_marketplace_url() -> None:
    assert normalize_marketplace_url("https://github.com/team/skills.git") == "https://github.com/team/skills.git"
    assert normalize_marketplace_url("git@github.com:team/skills.git") == "git@github.com:team/skills.git"
    # owner/repo 简写 → GitHub https 糖
    assert normalize_marketplace_url("team/skills") == "https://github.com/team/skills.git"
    # 非法 → None
    assert normalize_marketplace_url("not a url at all !!") is None
    # #270：hostless 既非合法 URL、也不是 owner/repo 简写 → None（此前原样放行）
    assert normalize_marketplace_url("ssh:///alice:PW_LEAK@example.com") is None
    assert normalize_marketplace_url("ssh://user@:22/path") is None


# #270：名称只取**解析后 path 的末段**——userinfo / query / fragment 不参与派生
_NAME_VECTORS: list[tuple[str, str | None]] = [
    # 既有正例（行为不变）
    ("https://github.com/team/My_Skills.git", "my-skills"),
    ("git@github.com:team/acme-skills.git", "acme-skills"),
    ("file:///tmp/foo.git", "foo"),
    # query / fragment 剥离（此前折叠进名字：repo-token-secret / repo-git-frag）
    ("https://host/repo?token=SECRET", "repo"),
    ("https://host/repo.git#frag", "repo"),
    ("git@host:o/r?token=SECRET", "r"),
    ("git@host:o/r#frag", "r"),
    ("https://host/repo.git?", "repo"),
    # scp-like 的 query / fragment 里出现 ``://`` 不得把整串当 URL 解析（曾得 git-host-repo）
    ("git@host:repo?x=://y", "repo"),
    ("git@host:repo#frag://y", "repo"),
    ("alice@host:repo?redirect=https://x", "repo"),
    # scp-like 多冒号取最后一个 ``:``（与 rust rsplit_once 及旧实现一致）
    ("git@host:a:b", "b"),
    ("git@host:o/r", "r"),
    # userinfo 不参与派生
    ("https://user:FAKE_TOKEN@host/o/repo.git", "repo"),
    ("ssh://alice:PW@host/o/repo.git", "repo"),
    # file: 不要求 host；带 host 形态同样按 path 末段派生
    ("file://host/path/repo.git", "repo"),
    ("file:///abs/path", "path"),
    # 尾斜杠 / 大小写 / 多段 path
    ("https://host/repo/", "repo"),
    ("https://HOST/Repo", "repo"),
    ("ssh://host/a/b/c.git", "c"),
    # hostless / 非法 / 无法判定 → None
    ("ssh:///alice:PW_LEAK@example.com", None),
    ("git:///x", None),
    ("https:///x", None),
    ("ssh://user@:22/path", None),
    ("file:alice@host", None),
    ("ssh://", None),
    ("file://[bad", None),
    # host 槽位的非主机占位串（空白 / ``-`` / ``_`` / ``%20`` / 纯点）→ 不派生
    ("ssh:// /alice:PW_LEAK@example.com", None),
    ("ssh://-/alice:PW_LEAK@example.com", None),
    ("ssh://_/alice:PW_LEAK@example.com", None),
    ("ssh://%20/alice:PW_LEAK@example.com", None),
    ("ssh://./alice:PW_LEAK@example.com", None),
    # 凭据样文本落在 **path 末段**（不在 userinfo）→ 同样不派生（交给 --name）
    ("https://example.com/alice:PW@example.com", None),
    ("git@host:alice:PW_LEAK@example.com", None),
    ("https://host/repo:tag", None),
    # 等价变体：percent-encoded / 全角分隔符 / 双重编码 / percent-encoded 全角 同样挡
    ("https://host/alice%3APW%40example.com", None),
    ("git@host:alice%3APW%40example.com", None),
    ("https://host/alice：PW＠example.com", None),
    ("https://host/alice%EF%BC%9APW_LEAK%EF%BC%A0example.com", None),
    ("https://host/alice%253APW_LEAK%2540example.com", None),
    # 全角百分号 / encoded-全角-percent / 三重编码（有界 3 轮覆盖）
    ("https://host/alice％３ＡPW_LEAK", None),
    ("https://host/alice%EF%BC%85%EF%BC%93%EF%BC%A1PW_LEAK", None),
    ("https://host/alice%25253APW_LEAK", None),
    # 入参先 strip（与 normalize_marketplace_url 一致；is_valid_git_url 本身不 strip）
    ("  https://host/repo  ", "repo"),
    # 尾点 FQDN 正常派生
    ("https://example.com./team/repo.git", "repo"),
    # 无 path（只有 host）→ None（用户 2026-11-17 拍板，不再派生 host 名）
    ("https://host", None),
    ("https://host/", None),
    ("https://user:pw@host", None),
    # 末段都构不成合法 kebab
    ("https://example.com/@@@", None),
    ("not a url", None),
    ("", None),
]


@pytest.mark.parametrize(("url", "expected"), _NAME_VECTORS)
def test_default_marketplace_name(url: str, expected: str | None) -> None:
    assert default_marketplace_name(url) == expected


def test_default_marketplace_name_never_leaks_credentials() -> None:
    """派生名会进 trust / 账本键 / clone 目录段 / ``A2CSkillRef.source``：#270 起不得含凭据片段。"""
    # 正对照：合法 URL 必须**仍能**派生出干净名字——否则「恒返回 None」的退化实现也会全绿。
    assert default_marketplace_name("https://user:FAKE_TOKEN@host/org/repo.git?token=FAKE_QUERY") == "repo"
    for url in (
        "ssh:///alice:PW_LEAK@example.com",
        "ssh:// /alice:PW_LEAK@example.com",
        "ssh://-/alice:PW_LEAK@example.com",
        "https://example.com/alice:PW@example.com",
        "git@host:alice:PW_LEAK@example.com",
    ):
        assert default_marketplace_name(url) is None


# ── add：trust 门 / add: trust gate ───────────────────────────────────────────
@requires_git
@pytest.mark.asyncio
async def test_add_untrusted_no_confirm_no_flag_errors(tmp_path: Path) -> None:
    """非交互（confirm=None）且无 --trust → 退出码 1、不落盘 / refuse + no record。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=None)
    assert code == 1
    assert "acme" not in (load_known_marketplaces(home, env).get("marketplaces") or {})


@requires_git
@pytest.mark.asyncio
async def test_add_confirm_yes_clones_and_records(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=_yes)
    assert code == 0
    assert "acme" in (load_known_marketplaces(home, env).get("marketplaces") or {})
    # plugin audit 的 skills 已注册（marketplace 2 段名 <plugin>:<skill>）
    names = {str(r.get("name")) for r in reg.active_refs()}
    assert "audit:audit-code" in names and "audit:lint" in names


@requires_git
@pytest.mark.asyncio
async def test_add_confirm_no_aborts(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=_no)
    assert code == 1
    assert "acme" not in (load_known_marketplaces(home, env).get("marketplaces") or {})


@requires_git
@pytest.mark.asyncio
async def test_add_trust_flag_skips_prompt(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True, confirm=None)
    assert code == 0


@requires_git
@pytest.mark.asyncio
async def test_add_name_conflict_errors(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True) == 0
    # 同名再 add → 冲突退出码 1
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True) == 1


@requires_git
@pytest.mark.asyncio
async def test_add_pretrusted_name_noninteractive_still_requires_trust(tmp_path: Path) -> None:
    """#95：非交互（confirm=None）即便 name 已在 trustedMarketplaces 也须 --trust → 退出码 1、不落盘。

    能走到「已 trusted 但不在 known_marketplaces」的只有 desync/泄漏态（remove 未撤销 / clone 失败
    orphan / 跨 A2C_SKILL_HOME；trust 落全局 user settings、与 home 解耦）。该陈旧全局 trust 不得
    静默授权一次全新的非交互远端 clone+注册——契约「非交互须 --trust」无条件成立。
    """
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _record_trust("acme", env)  # 预置泄漏态：name=acme 已在全局 trustedMarketplaces，但不在 known_marketplaces
    # 非交互（confirm=None）+ 无 --trust：旧实现直通（bug），新实现必须拒绝
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=None) == 1
    assert "acme" not in (load_known_marketplaces(home, env).get("marketplaces") or {})
    # 显式 --trust 仍直通（契约提供的非交互旁路）
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True, confirm=None) == 0


@requires_git
@pytest.mark.asyncio
async def test_add_pretrusted_name_interactive_skips_prompt(tmp_path: Path) -> None:
    """回归守卫：交互（confirm 非空）下，已 trusted 的 name 仍跳过重复 prompt（#95 收紧不破坏 REPL UX）。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _record_trust("acme", env)  # 预信任 name=acme

    called = False

    async def _must_not_be_called(_target: str) -> bool:
        nonlocal called
        called = True
        return True

    # confirm 非空（模拟 REPL）但 name 已 trusted → 直接 clone，不触发 confirm prompt
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=_must_not_be_called) == 0
    assert called is False
    assert "acme" in (load_known_marketplaces(home, env).get("marketplaces") or {})


# ── list / info / set / remove ───────────────────────────────────────────────
@requires_git
@pytest.mark.asyncio
async def test_list_and_set_auto_update(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True)
    assert marketplace_list(home, env) == 0
    assert marketplace_set(home, env, "acme", "auto-update", "true") == 0
    assert (load_known_marketplaces(home, env).get("marketplaces") or {})["acme"].get("autoUpdate") is True
    # 未知 key → 1
    assert marketplace_set(home, env, "acme", "bogus", "x") == 1


@requires_git
@pytest.mark.asyncio
async def test_info_and_remove(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True)
    assert marketplace_info(home, env, "acme") == 0
    assert marketplace_info(home, env, "nope") == 1
    # remove（无 installed plugin → 仅 prune clone）
    assert await marketplace_remove(reg, home, env, "acme", confirm=_yes) == 0
    assert "acme" not in (load_known_marketplaces(home, env).get("marketplaces") or {})


@pytest.mark.asyncio
async def test_remove_fails_when_clone_is_symlink_258(tmp_path: Path) -> None:
    """#258 复审 🔴1：clone 为软链、prune 无法清空 → 退出码 1，known 条目保留（残留树仍可被 orphan 发现）。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    real = home / "marketplace" / "real"
    (real / "plugins").mkdir(parents=True)
    (real / "plugins" / "keep").write_text("x", encoding="utf-8")
    link = home / "marketplace" / "acme"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": {"type": "git", "url": "file:///x"}, "installLocation": str(real)}}},
        home=home,
    )
    _record_trust("acme", env)

    code = await marketplace_remove(reg, home, env, "acme", confirm=_yes)

    assert code == 1
    assert "acme" in load_known_marketplaces(home, env)["marketplaces"]
    assert "acme" in _load_trusted(env), "移除失败不得撤销信任"
    assert link.is_symlink() and (real / "plugins" / "keep").exists()


@pytest.mark.asyncio
async def test_remove_reports_uninstall_delete_failure_258(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#258 复审四轮 🟡2：uninstall 删除失败 + prune 成功 → exit 1、账本/意图/trust 保留、stderr 可感。"""
    monkeypatch.chdir(tmp_path)  # cwd 隔离：_precheck_settings_writes 读 cwd 锚定的 project/local 层
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    clone = home / "marketplace" / "acme"
    (clone / "plugins").mkdir(parents=True)
    (clone / "plugins" / "x").write_text("x", encoding="utf-8")
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": {"type": "git", "url": "file:///x"}, "installLocation": str(clone)}}},
        home=home,
    )
    settings = user_settings_path(env)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"installedPlugins": ["audit@acme"], "enabledPlugins": {"audit@acme": True}, "trustedMarketplaces": ["acme"]}),
        encoding="utf-8",
    )
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("x", encoding="utf-8")
    ext = home / "marketplace" / ".plugins" / "acme" / "audit"
    ext.parent.mkdir(parents=True)
    try:
        ext.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    save_installed_plugins(
        {"version": 1, "plugins": {"audit@acme": [{"scope": "user", "installPath": str(ext)}]}},
        home=home,
    )
    capsys.readouterr()

    code = await marketplace_remove(reg, home, env, "acme", confirm=_yes)

    assert code == 1, "uninstall 删除失败必须可感，不得因 prune 成功而 exit 0"
    err = capsys.readouterr().err
    assert "failed to uninstall 'audit@acme'" in err, "stderr 须点名失败 victim"
    ledger = load_installed_plugins(home, env)["plugins"]
    assert "audit@acme" in ledger and ledger["audit@acme"][0]["installPath"] == str(ext), "账本与 installPath 保留"
    user = json.loads(settings.read_text(encoding="utf-8"))
    assert user["installedPlugins"] == ["audit@acme"], "安装意图保留"
    assert user["enabledPlugins"]["audit@acme"] is True, "enabled 保留"
    # marketplace 本体已被 prune 移除 → 撤销信任属正确清理（避免同名 later re-add 继承旧信任）；
    # 「移除真正失败（prune 未清空）」时信任保留，由 test_remove_fails_when_clone_is_symlink_258 锁定。
    assert "acme" not in user["trustedMarketplaces"]
    assert "acme" not in load_known_marketplaces(home, env)["marketplaces"], "prune 本身成功（场景前提）"
    assert (target / "keep").exists(), "拒删：链接目标完好"


class _ReplComp:
    """``marketplace`` repl_dispatch 的最小 fake（仅需 skill_home / skill_registry / mark_skills_dirty）。"""

    def __init__(self, home: Path, registry: SkillRegistry) -> None:
        self.skill_home = home
        self.skill_registry = registry
        self.dirty = 0
        self.mcp_manager = None

    def mark_skills_dirty(self) -> None:
        self.dirty += 1


class _ReplSession:
    """REPL confirm 最小替身：恒答 y。"""

    async def prompt_async(self, _prompt: str) -> str:
        return "y"


@pytest.mark.asyncio
async def test_repl_remove_partial_failure_marks_dirty_258(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#258 复审四轮 🟡1：一 victim 成功、一 victim 失败但 marketplace 已被 prune → REPL 必须标脏。"""
    env, home = _env(tmp_path), _home(tmp_path)
    monkeypatch.setenv("A2C_SKILL_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    clone = home / "marketplace" / "acme"
    (clone / "plugins").mkdir(parents=True)
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": {"type": "git", "url": "file:///x"}, "installLocation": str(clone)}}},
        home=home,
    )
    save_installed_plugins(
        {
            "version": 1,
            "plugins": {
                "ok@acme": [{"scope": "user", "installPath": str(clone / "plugins" / "ok")}],
                "bad@acme": [{"scope": "user", "installPath": str(clone / "plugins" / "bad")}],
            },
        },
        home=home,
    )

    async def _uninstall(pid: str, *_a: object, **_kw: object) -> bool:
        if pid == "bad@acme":
            raise PluginInstallError("simulated removal failure")
        return True

    monkeypatch.setattr(marketplace_mod, "uninstall_plugin", _uninstall)
    comp = _ReplComp(home, SkillRegistry())

    await marketplace_mod.repl_dispatch(comp, ["marketplace", "remove", "acme"], session=_ReplSession())

    assert comp.dirty == 1, "prune 已移除 marketplace ⇒ 即使 exit 1 也必须标脏（否则 Agent 看不到已删除 SKILL）"
    assert "acme" not in load_known_marketplaces(home, env)["marketplaces"]


@requires_git
@pytest.mark.asyncio
async def test_json_output_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True, json_output=True)
    capsys.readouterr()  # 清空 add 的输出
    marketplace_list(home, env, json_output=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list) and data[0]["name"] == "acme" and data[0]["trusted"] is True


# ── #6 trust 撤销 / revoke trust on remove ────────────────────────────────────
@requires_git
@pytest.mark.asyncio
async def test_remove_revokes_trust(tmp_path: Path) -> None:
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True)
    assert "acme" in _load_trusted(env)
    await marketplace_remove(reg, home, env, "acme", confirm=_yes)
    assert "acme" not in _load_trusted(env)  # remove 撤销信任，避免只增不减


# ── #2 no_clone 仅注册意图 / no_clone registers intent only ───────────────────
@pytest.mark.asyncio
async def test_no_clone_registers_intent(tmp_path: Path) -> None:
    """no_clone=True → 记 known_marketplaces（installLocation 空）但不 clone/stage skill。无需 git。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, "https://github.com/team/acme.git", name="acme", trust=True, no_clone=True)
    assert code == 0
    mps = load_known_marketplaces(home, env).get("marketplaces") or {}
    assert "acme" in mps and mps["acme"].get("installLocation") == ""
    assert reg.active_refs() == []  # 未 clone → 无 skill 注册


# ── #2 refresh 三态 / refresh updated|unchanged|missing ───────────────────────
def _commit_push_skill(work: Path, skill: str) -> None:
    """向 work 工作仓加一个 skill 并 push 到 origin(bare)（供 refresh 拉取，造 updated 态）。"""
    _write(work, {f"plugins/audit/skills/{skill}/SKILL.md": _skill_md(skill)})
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", f"add {skill}")
    _git(work, "push", "-q", "origin", "main")


@requires_git
@pytest.mark.asyncio
async def test_refresh_states(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # work + bare（work origin=bare，便于 push 造 updated 态）
    work = tmp_path / "acme-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    _write(work, _marketplace_files())
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "acme.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(work, "remote", "add", "origin", str(bare))

    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True)
    capsys.readouterr()  # 清空 add 的成功行，避免污染后续 json 读取

    # unchanged：add 后立即 refresh，sha 未变
    await marketplace_refresh(reg, home, env, "all", json_output=True)
    assert json.loads(capsys.readouterr().out)[0]["status"] == "unchanged"

    # updated：push 新 skill 后 refresh 拉取
    _commit_push_skill(work, "newskill")
    await marketplace_refresh(reg, home, env, "all", json_output=True)
    assert json.loads(capsys.readouterr().out)[0]["status"] == "updated"
    assert "audit:newskill" in {str(r.get("name")) for r in reg.active_refs()}

    # missing：未知 name
    await marketplace_refresh(reg, home, env, "ghost", json_output=True)
    assert json.loads(capsys.readouterr().out)[0]["status"] == "missing"


# ── #261 展示脱敏：known-source 展示面 / display redaction ─────────────────────
_CRED_URL = "https://user:FAKE_TOKEN@example.com/org/repo.git?token=FAKE_QUERY#FAKE_FRAGMENT"
_SCP_URL = "git@example.com:org/repo.git"
_SSH_URL = "ssh://git@example.com/org/repo.git"
_QUERY_URL = "https://example.com/repo.git?x#y"
_CLEAN_URL = "https://example.com/org/repo.git"

# (input, expected display, forbidden literals in output)
_LIST_INFO_VECTORS: list[tuple[str, str, tuple[str, ...]]] = [
    (_SCP_URL, "example.com:org/repo.git", ("git@",)),
    (_SSH_URL, "ssh://example.com/org/repo.git", ("git@",)),
    (_CRED_URL, "https://example.com/org/repo.git", ("FAKE_TOKEN", "FAKE_QUERY", "FAKE_FRAGMENT", "user:")),
    (_QUERY_URL, "https://example.com/repo.git", ("?x", "#y")),
    (_CLEAN_URL, "https://example.com/org/repo.git", ()),
]


def _flat(text: str) -> str:
    """去全部空白后比较（Rich 表格在窄终端会折行）。"""
    return "".join(text.split())


@pytest.fixture
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rich 表格在窄终端会截断 URL（``…``）；测试用宽控制台保证断言整串。"""
    from rich.console import Console

    monkeypatch.setattr(marketplace_mod, "console", Console(width=200))


def _seed_marketplace(home: Path, env: dict[str, str], name: str, url: str | None) -> None:
    rec: dict[str, object] = {"installLocation": ""}
    if url is not None:
        rec["source"] = {"type": "git", "url": url}
    save_known_marketplaces({"version": 1, "marketplaces": {name: rec}}, home, env)


@pytest.mark.parametrize(("feed_url", "expected", "forbidden"), _LIST_INFO_VECTORS)
def test_list_display_redacts_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    wide_console: None,
    feed_url: str,
    expected: str,
    forbidden: tuple[str, ...],
) -> None:
    """list 表格 / JSON 均走 known-source 展示：无 userinfo/query/fragment。"""
    env, home = _env(tmp_path), _home(tmp_path)
    _seed_marketplace(home, env, "demo", feed_url)

    marketplace_list(home, env, json_output=True)
    row = json.loads(capsys.readouterr().out)[0]
    assert row["url"] == expected
    assert "FAKE_TOKEN" not in json.dumps(row) and "FAKE_QUERY" not in json.dumps(row)

    marketplace_list(home, env)
    out = capsys.readouterr().out
    assert _flat(expected) in _flat(out)
    if feed_url != expected:
        assert _flat(feed_url) not in _flat(out), "表格不得回退为原始 URL"
    for literal in forbidden:
        assert literal not in out, f"表格输出不得含 {literal!r}"


@pytest.mark.parametrize(("feed_url", "expected", "forbidden"), _LIST_INFO_VECTORS)
def test_info_display_redacts_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    wide_console: None,
    feed_url: str,
    expected: str,
    forbidden: tuple[str, ...],
) -> None:
    """info 表格 / JSON 均走 known-source 展示。"""
    env, home = _env(tmp_path), _home(tmp_path)
    _seed_marketplace(home, env, "demo", feed_url)

    code = marketplace_info(home, env, "demo", json_output=True)
    assert code == 0
    info = json.loads(capsys.readouterr().out)
    assert info["url"] == expected
    assert "FAKE_TOKEN" not in json.dumps(info) and "FAKE_QUERY" not in json.dumps(info)

    code = marketplace_info(home, env, "demo")
    assert code == 0
    out = capsys.readouterr().out
    assert _flat(expected) in _flat(out)
    if feed_url != expected:
        assert _flat(feed_url) not in _flat(out), "表格不得回退为原始 URL"
    for literal in forbidden:
        assert literal not in out, f"表格输出不得含 {literal!r}"


def test_info_missing_source_url_stays_null(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], wide_console: None
) -> None:
    """手编 / 历史记录缺 source.url：JSON 保持 null、表格留空（不渲染字面 None）。"""
    env, home = _env(tmp_path), _home(tmp_path)
    _seed_marketplace(home, env, "demo", None)

    assert marketplace_info(home, env, "demo", json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["url"] is None

    assert marketplace_list(home, env, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)[0]["url"] is None

    assert marketplace_info(home, env, "demo") == 0
    out = capsys.readouterr().out
    assert "None" not in out, "缺失字段应留空，不得渲染字面 None"
    url_row = next(line for line in out.splitlines() if "url" in line)
    assert url_row.split("│")[2].strip() == "", "url 行 value 单元格必须为空白"


def test_surrogate_url_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], wide_console: None
) -> None:
    """手编 ledger 的孤立代理项 URL：展示层 fail-closed，不崩溃（#261 复审 🔴）。"""
    env, home = _env(tmp_path), _home(tmp_path)
    path = known_marketplaces_path(home, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "marketplaces": {
                    "demo": {
                        "source": {"type": "git", "url": "https://example.com/\ud800"},
                        "installLocation": "",
                    }
                },
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    assert marketplace_list(home, env, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)[0]["url"] == REDACTED_GIT_SOURCE

    assert marketplace_list(home, env) == 0
    assert REDACTED_GIT_SOURCE in capsys.readouterr().out

    assert marketplace_info(home, env, "demo", json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["url"] == REDACTED_GIT_SOURCE


def _patch_stage(monkeypatch: pytest.MonkeyPatch, *, register: bool) -> None:
    """替换 staging：可按需模拟「成功注册」或「clone 失败（不注册）」。"""

    async def fake_stage(
        name: str,
        source: object,
        registry: object,
        home: Path,
        *,
        auto_update: bool = False,
        env: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> list[str]:
        if register:

            def _mutate(f: dict[str, object]) -> object:
                f["marketplaces"][name] = {  # type: ignore[index]
                    "source": dict(source),  # type: ignore[arg-type]
                    "installLocation": "",
                    "autoUpdate": auto_update,
                }
                return f

            update_known_marketplaces(_mutate, home, env)
        return []

    monkeypatch.setattr(marketplace_mod, "stage_marketplace_skills", fake_stage)


@requires_git
@pytest.mark.asyncio
async def test_add_json_uses_known_display(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """add 成功 JSON 的 url 为 known-source 展示值（不可 re-clone）；存储仍是原始 URL。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _patch_stage(monkeypatch, register=True)

    code = await marketplace_add(reg, home, env, _CRED_URL, name="demo", trust=True, json_output=True)
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["url"] == "https://example.com/org/repo.git"
    assert "FAKE_TOKEN" not in json.dumps(data)

    stored = load_known_marketplaces(home, env)["marketplaces"]["demo"]["source"]["url"]
    assert stored == _CRED_URL, "known_marketplaces 必须保留原始 URL"


@pytest.mark.asyncio
async def test_add_confirm_prompt_uses_known_display(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """交互确认提示展示 known-source（scp-like → host:path，不保留 user@）。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _patch_stage(monkeypatch, register=True)
    seen: list[str] = []

    async def confirm(target: str) -> bool:
        seen.append(target)
        return True

    code = await marketplace_add(reg, home, env, _SCP_URL, confirm=confirm)
    assert code == 0
    assert seen == ["example.com:org/repo.git"]


@pytest.mark.asyncio
async def test_add_invalid_url_error_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """非法 URL 错误 fail-closed：无法解析 → 占位符，不回显原始输入。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, "not a url", trust=True, json_output=True)
    assert code == 1
    out = capsys.readouterr().out
    assert "not a url" not in out
    assert REDACTED_GIT_SOURCE in out


@pytest.mark.asyncio
async def test_add_untrusted_noninteractive_error_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """非交互 untrusted 错误走 strict：凭据 URL 去 userinfo/query/fragment（不泄漏 token）。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, _CRED_URL, trust=False, json_output=True)
    assert code == 1
    out = capsys.readouterr().out
    assert "FAKE_TOKEN" not in out and "FAKE_QUERY" not in out
    assert "https://example.com/org/repo.git" in out


@pytest.mark.asyncio
async def test_add_clone_failure_error_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """clone/refresh 失败错误：凭据 scheme URL → 去凭据展示；scp-like → 占位符。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _patch_stage(monkeypatch, register=False)

    assert await marketplace_add(reg, home, env, _CRED_URL, name="demo", trust=True, json_output=True) == 2
    out = capsys.readouterr().out
    assert "FAKE_TOKEN" not in out and "FAKE_QUERY" not in out
    assert "https://example.com/org/repo.git" in out

    assert await marketplace_add(reg, home, env, _CLEAN_URL, name="demo2", trust=True, json_output=True) == 2
    out = capsys.readouterr().out
    assert "https://example.com/org/repo.git" in out

    assert await marketplace_add(reg, home, env, _SCP_URL, name="demo3", trust=True, json_output=True) == 2
    out = capsys.readouterr().out
    assert "git@example.com" not in out
    assert REDACTED_GIT_SOURCE in out


@pytest.mark.asyncio
async def test_add_name_derivation_error_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """派生名失败错误同样去凭据：尾段 ``@@@`` 命中凭据样判据 → 整体占位符（#270）。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, "https://user:FAKE_TOKEN@example.com/@@@", trust=True, json_output=True)
    assert code == 1
    out = capsys.readouterr().out
    assert "FAKE_TOKEN" not in out
    assert REDACTED_GIT_SOURCE in out
    assert "example.com" not in out


# ── #270 hostless URL / 派生名入账 / hostless + derived-name flows ─────────────
@pytest.mark.asyncio
async def test_add_hostless_url_rejected_even_with_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """hostless URL 在 URL 校验处即拒绝：``--name`` 不能绕过，零 trust / 零账本写入。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(
        reg,
        home,
        env,
        "ssh:///alice:PW_LEAK@example.com",
        name="evil",
        trust=True,
        no_clone=True,
        json_output=True,
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "PW_LEAK" not in out and "alice" not in out
    assert REDACTED_GIT_SOURCE in out
    assert "evil" not in _load_trusted(env), "URL 校验失败不得写 trust"
    assert load_known_marketplaces(home, env).get("marketplaces") in (None, {}), "不得写 known_marketplaces"
    assert not user_settings_path(env).exists(), "user settings（trust 落盘）不得被创建"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "ssh://-/alice:PW_LEAK@example.com",  # junk host：URL 校验失败文案
        "https://host/alice:PW_LEAK@example.com",  # 合法 host、凭据在末段：派生名失败文案
        "https://host/alice%EF%BC%9APW_LEAK%EF%BC%A0example.com",  # percent-encoded 全角分隔符
        "https://host/alice％３ＡPW_LEAK",  # 全角百分号编码（NFKC 后才成 %3A）
        "https://host/alice:PW_LEAK@example.com/",  # 尾斜杠：末段为空，不得漏判
        "https://host/team/alice:PW_LEAK@example.com/repo",  # 凭据在**中间段**：untrusted 文案
        "https://host",  # 无 path：无法判定 → fail-closed（派生名失败文案）
    ],
)
async def test_add_error_message_never_echoes_path_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], url: str
) -> None:
    """#270：add 失败文案不得回显 path 任一段里的凭据样文本（展示层保留 path，须再挡一次）。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, url, trust=False, no_clone=True, json_output=True)
    assert code == 1
    out = capsys.readouterr().out
    assert "PW_LEAK" not in out and "alice" not in out
    assert REDACTED_GIT_SOURCE in out


def test_error_url_display_keeps_clean_urls_readable() -> None:
    """正对照：干净 URL 必须原样可读——否则「一律占位符」的退化实现也会全绿。"""
    assert (
        marketplace_mod._error_url_display("https://example.com/org/repo.git")
        == "https://example.com/org/repo.git"
    )
    # query / fragment / userinfo 由 #261 展示层剥掉，path 保留
    assert (
        marketplace_mod._error_url_display("https://user:FAKE_TOKEN@example.com/org/repo.git?token=Q#f")
        == "https://example.com/org/repo.git"
    )


@pytest.mark.asyncio
async def test_add_query_url_derives_clean_name_and_keeps_raw_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """query / fragment / userinfo 不进派生名；账本键干净、``source.url`` 仍原样（raw 保真）。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    seen: list[str] = []

    async def fake_stage(
        name: str,
        source: object,
        registry: object,
        home_: Path,
        *,
        auto_update: bool = False,
        env: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> list[str]:
        seen.append(name)

        def _mutate(f: dict[str, object]) -> object:
            f["marketplaces"][name] = {  # type: ignore[index]
                "source": dict(source),  # type: ignore[arg-type]
                "installLocation": "",
                "autoUpdate": auto_update,
            }
            return f

        update_known_marketplaces(_mutate, home_, env)
        return []

    monkeypatch.setattr(marketplace_mod, "stage_marketplace_skills", fake_stage)
    url = "https://example.com/org/repo.git?token=FAKE_QUERY#FAKE_FRAGMENT"
    code = await marketplace_add(reg, home, env, url, trust=True, json_output=True)
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["added"] == "repo", "派生名只取 path 末段"
    assert "FAKE_QUERY" not in out and "FAKE_FRAGMENT" not in out
    assert seen == ["repo"], "进入 staging 的 name 必须干净（A2CSkillRef.source = marketplace:repo）"
    assert _load_trusted(env) == {"repo"}, "trust 键同样只能是干净名"
    stored = load_known_marketplaces(home, env)["marketplaces"]["repo"]["source"]["url"]
    assert stored == url, "known_marketplaces 存储与 git argv 必须保留原始 URL（raw 保真）"
