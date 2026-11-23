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
from collections.abc import Mapping
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
from a2c_smcp.computer.settings.scope import SettingsAxes, user_settings_path
from a2c_smcp.computer.settings.store import (
    known_marketplaces_path,
    load_installed_plugins,
    load_known_marketplaces,
    save_installed_plugins,
    save_known_marketplaces,
    update_known_marketplaces,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import stage_marketplace_skills
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


def _marketplace_files(name: str = "acme") -> dict[str, str]:
    # #260 / D1：注册名 = 清单顶层 `name`（本夹具默认与注册名一致；需要「仓库名 ≠ 清单名」的用例显式传参）。
    manifest = {
        "name": name,
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


@pytest.fixture(autouse=True)
def _isolate_ambient_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """#296：守卫默认按 ``env`` 现解析 policy → 单测固定为空，避免宿主机 MDM policy 让断言漂移。

    需要测 policy 层的用例在用例体内再次 monkeypatch 覆盖即可（``monkeypatch.setattr`` 后写生效）。
    """
    from a2c_smcp.computer.settings import policy as policy_mod

    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_kw: {})


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    return h


def _seed_cached_marketplace(home: Path, name: str = "acme") -> Path:
    """在 ``<home>/marketplace/<name>`` 造本地缓存（清单名 = name），供 ``--no-clone`` 定名路径。"""
    clone = home / "marketplace" / name
    manifest = {
        "name": name,
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [],
    }
    path = clone / ".tfrobot-plugin" / "marketplace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return clone


def _write_settings_file(env: Mapping[str, str], payload: dict[str, object]) -> None:
    """把 payload 写入该 env 的 user settings.json。"""
    path = user_settings_path(dict(env))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
    # 全角百分号 / encoded-全角-percent / 三重编码 / 四层 / 八层（#282：不动点解码 + 残形 `%` 即拒）
    ("https://host/alice％３ＡPW_LEAK", None),
    ("https://host/alice%EF%BC%85%EF%BC%93%EF%BC%A1PW_LEAK", None),
    ("https://host/alice%25253APW_LEAK", None),
    ("https://host/alice%2525253APW_LEAK", None),
    ("https://host/alice%252525252525253APW_LEAK", None),
    # 残形 percent 转义一律不可判定（#282 对齐 rust #285 `seg.contains('%')`）：即便解码后是干净文本
    ("https://host/repo%20name", None),
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
async def test_add_same_name_same_source_is_idempotent(tmp_path: Path) -> None:
    """#260 六景②：同名**同源**重复 add → 幂等成功，不产生第二个注册、不改既有 clone/记录。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True) == 0
    before = dict(load_known_marketplaces(home, env)["marketplaces"]["acme"])
    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True) == 0
    after = dict(load_known_marketplaces(home, env)["marketplaces"]["acme"])
    assert before == after, "幂等 re-add 不得刷新 lastUpdated/commitSha 或重建记录"
    assert list(load_known_marketplaces(home, env)["marketplaces"]) == ["acme"], "只允许一个注册"


@requires_git
@pytest.mark.asyncio
async def test_add_same_name_different_source_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#260 六景①/D2：同名**不同源** → ``validation`` 拒绝，既有注册/内容不变（不静默替换）。"""
    bare_a = _make_bare(tmp_path / "a", "acme", _marketplace_files())
    bare_b = _make_bare(tmp_path / "b", "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    assert await marketplace_add(reg, home, env, f"file://{bare_a}", trust=True) == 0
    before = dict(load_known_marketplaces(home, env)["marketplaces"]["acme"])
    capsys.readouterr()  # 清掉首次 add 的人类可读输出（下面要解析 JSON）

    assert await marketplace_add(reg, home, env, f"file://{bare_b}", trust=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation" and payload["reason"] == "name-conflict"
    assert payload["name"] == "acme" and payload["existingSource"], "结构化诊断须给旧源与处置"
    after = dict(load_known_marketplaces(home, env)["marketplaces"]["acme"])
    assert after == before, "被拒的换源不得改动既有注册"


@requires_git
@pytest.mark.asyncio
async def test_add_same_source_different_name_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#260 裁决 3：同一 source 已以旧名注册时，新名 add → ``validation``，诊断给旧名与处置。"""
    work = tmp_path / "acme-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    _write(work, _marketplace_files())  # 清单名 acme
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "acme.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(work, "remote", "add", "origin", str(bare))

    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    assert await marketplace_add(reg, home, env, f"file://{bare}", trust=True) == 0
    capsys.readouterr()  # 清掉首次 add 的人类可读输出

    # 上游把清单改名（同一 source）→ 再次 add 会解析出新名 → 与既有注册同源不同名 → 拒绝
    _write(work, {".tfrobot-plugin/marketplace.json": json.dumps({"name": "acme-renamed", "plugins": []})})
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "rename")
    _git(work, "push", "-q", "origin", "main")

    assert await marketplace_add(reg, home, env, f"file://{bare}", trust=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation" and payload["reason"] == "source-conflict"
    assert payload["existingName"] == "acme", "诊断须给旧名与处置"
    assert list(load_known_marketplaces(home, env)["marketplaces"]) == ["acme"], "不得新增同源第二注册"


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
async def test_add_interactive_always_prompts_even_if_name_trusted(tmp_path: Path) -> None:
    """
    #260 trust 裁决：交互式 add **一律**弹确认——``trustedMarketplaces`` 不再豁免 prompt。

    ``--name`` 已不命名任何东西（D6 B），拿它当免提示开关与语义不一致，且会绕过确认；``--trust`` 与
    非交互路径不变（见上一条用例）。
    """
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _record_trust("acme", env)  # 即便清单名已在 trusted

    seen: list[str] = []

    async def _confirm(target: str) -> bool:
        seen.append(target)
        return True

    assert await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=False, confirm=_confirm) == 0
    assert seen, "交互式 add 必须弹确认（trusted 不再跳过）"
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
    """``marketplace`` repl_dispatch 的最小 fake（skill_home / registry / settings axes）。"""

    def __init__(
        self,
        home: Path,
        registry: SkillRegistry,
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        flag_settings_path: Path | None = None,
        policy_settings: Mapping[str, object] | None = None,
    ) -> None:
        self.skill_home = home
        self.skill_registry = registry
        self.dirty = 0
        self.mcp_manager = None
        self.env = env
        self._cwd = cwd
        self._flag_settings_path = flag_settings_path
        self._policy_settings = policy_settings

    def _settings_axes(self) -> SettingsAxes:
        return SettingsAxes(
            env=self.env,
            cwd=self._cwd,
            flag_settings_path=self._flag_settings_path,
            policy_settings=self._policy_settings,
        )

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


# ── #296：REPL 用 Computer 实例轴（env / cwd / flag），非 ambient ──────────────
def _repl_env(tmp_path: Path, home: Path, *, tag: str) -> dict[str, str]:
    """REPL 实例 env：与 ambient 分离的 XDG，同一 A2C_SKILL_HOME。"""
    return {**os.environ, "A2C_SKILL_HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / f"{tag}-cfg")}


async def _repl_add_no_clone(comp: _ReplComp) -> int:
    before = comp.dirty
    await marketplace_mod.repl_dispatch(
        comp,
        [
            "marketplace",
            "add",
            "https://example.com/org/repo.git",
            "--name",
            "acme",
            "--trust",
            "--no-clone",
            "--json",
        ],
        session=_ReplSession(),
    )
    return comp.dirty - before


@pytest.mark.asyncio
async def test_repl_add_uses_instance_env_axis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296：REPL 用 ``comp.env``（非 ambient）——ambient 干净、实例 user 层有引用 ⇒ 拒绝。"""
    home = _home(tmp_path)
    _seed_cached_marketplace(home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-cfg"))  # ambient 干净
    instance_env = _repl_env(tmp_path, home, tag="instance")
    _write_settings_file(instance_env, {"enabledPlugins": {"audit@acme": False}})

    comp = _ReplComp(home, SkillRegistry(), env=instance_env, policy_settings={})
    assert await _repl_add_no_clone(comp) == 0, "拒绝不得标脏"
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "dangling-references"


@pytest.mark.asyncio
async def test_repl_add_ignores_ambient_environ_axes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#296 正对照：引用只在 ambient、实例层干净 ⇒ 放行（证明未偷读 ``os.environ``）。"""
    home = _home(tmp_path)
    _seed_cached_marketplace(home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-cfg"))
    _write_settings_file({**os.environ}, {"enabledPlugins": {"audit@acme": False}})
    instance_env = _repl_env(tmp_path, home, tag="instance")

    comp = _ReplComp(home, SkillRegistry(), env=instance_env, policy_settings={})
    assert await _repl_add_no_clone(comp) == 1, "放行应标脏（成功路径）"
    assert "acme" in load_known_marketplaces(home, instance_env)["marketplaces"]


@pytest.mark.asyncio
async def test_repl_add_uses_instance_cwd_axis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296：REPL 把 ``comp`` 的 cwd 传给 project/local 解析轴。"""
    home = _home(tmp_path)
    _seed_cached_marketplace(home)
    project = tmp_path / "project"
    (project / ".tfrobot").mkdir(parents=True)
    (project / ".tfrobot" / "settings.local.json").write_text(
        json.dumps({"enabledPlugins": {"audit@acme": False}}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)  # ambient cwd 无 .tfrobot（正对照面）
    comp = _ReplComp(
        home,
        SkillRegistry(),
        env=_repl_env(tmp_path, home, tag="instance"),
        cwd=project,
        policy_settings={},
    )

    assert await _repl_add_no_clone(comp) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "dangling-references"


@pytest.mark.asyncio
async def test_repl_add_uses_instance_flag_axis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296：REPL 把 ``comp`` 的 ``--settings`` flag 层传给守卫。"""
    home = _home(tmp_path)
    _seed_cached_marketplace(home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-cfg"))
    flag = tmp_path / "flag.json"
    flag.write_text(json.dumps({"enabledPlugins": {"audit@acme": False}}), encoding="utf-8")
    comp = _ReplComp(
        home,
        SkillRegistry(),
        env=_repl_env(tmp_path, home, tag="instance"),
        flag_settings_path=flag,
        policy_settings={},
    )

    assert await _repl_add_no_clone(comp) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "dangling-references"


@pytest.mark.asyncio
async def test_repl_settings_set_flows_into_marketplace_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296 跨面锁：REPL ``settings set`` 与 ``marketplace add`` 必须读写**同一实例 env**。

    注入 env 的 ``--computer-factory`` 宿主里，若 settings 分派器仍写 ambient，``settings set enabledPlugins``
    写入的引用对 marketplace 守卫不可见 ⇒ 同名不同源 re-add 被放行并被静默重绑（§5.14 MUST NOT）。
    """
    from a2c_smcp.computer.cli.commands import settings as settings_mod

    home = _home(tmp_path)
    _seed_cached_marketplace(home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-cfg"))  # ambient 干净
    instance_env = _repl_env(tmp_path, home, tag="instance")
    comp = _ReplComp(home, SkillRegistry(), env=instance_env, policy_settings={})

    await settings_mod.repl_dispatch(
        comp, ["settings", "set", "enabledPlugins", '{"audit@acme": false}'], session=None
    )
    capsys.readouterr()  # 清掉 settings set 的输出

    assert await _repl_add_no_clone(comp) == 0, "settings set 写入的引用必须被 marketplace 守卫看到"
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "dangling-references"


@pytest.mark.asyncio
async def test_repl_list_reads_trust_from_instance_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296：REPL 的账本/trust 读面同样跟随 ``comp.env``（整 env 切换，不混源）。"""
    home = _home(tmp_path)
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "acme": {
                    "source": {"type": "git", "url": "https://example.com/acme.git"},
                    "installLocation": str(home / "marketplace" / "acme"),
                }
            },
        },
        home=home,
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-cfg"))  # ambient 无 trust
    instance_env = _repl_env(tmp_path, home, tag="instance")
    _write_settings_file(instance_env, {"trustedMarketplaces": ["acme"]})

    comp = _ReplComp(home, SkillRegistry(), env=instance_env)
    await marketplace_mod.repl_dispatch(comp, ["marketplace", "list", "--json"], session=_ReplSession())

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "acme" and payload[0]["trusted"] is True


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


# ── #260 D5 no_clone：无 pending、无法定名即 validation ──────────────────────
@pytest.mark.asyncio
async def test_no_clone_new_source_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """D5：新源 ``--no-clone`` 无法在注册前确定清单名 → ``validation``、不建未定名记录。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(
        reg, home, env, "https://github.com/team/acme.git", name="acme", trust=True, no_clone=True, json_output=True
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation" and payload["reason"] == "unresolvable-name"
    assert load_known_marketplaces(home, env).get("marketplaces") in (None, {})
    assert reg.active_refs() == []


@requires_git
@pytest.mark.asyncio
async def test_no_clone_already_registered_is_idempotent(tmp_path: Path) -> None:
    """已注册同名同源 → ``--no-clone`` 幂等意图：保留既有 installLocation（不得用空值覆盖有效记录）。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    assert await marketplace_add(reg, home, env, f"file://{bare}", trust=True) == 0
    before = dict(load_known_marketplaces(home, env)["marketplaces"]["acme"])
    assert await marketplace_add(reg, home, env, f"file://{bare}", trust=True, no_clone=True) == 0
    after = dict(load_known_marketplaces(home, env)["marketplaces"]["acme"])
    assert after == before and after["installLocation"], "幂等意图不得清空 installLocation"


@requires_git
@pytest.mark.asyncio
async def test_no_clone_with_cached_clone_and_name(tmp_path: Path) -> None:
    """``--name`` + 本地缓存清单名匹配 → 允许登记意图（D5「有缓存可确定名称时允许」）。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    assert await marketplace_add(reg, home, env, f"file://{bare}", trust=True) == 0
    # 模拟「记录丢失但 clone 还在」的修复面：删账本记录，保留 clone。
    save_known_marketplaces({"version": 1, "marketplaces": {}}, home, env)

    code = await marketplace_add(reg, home, env, f"file://{bare}", name="acme", trust=True, no_clone=True)
    assert code == 0
    mps = load_known_marketplaces(home, env)["marketplaces"]
    assert mps["acme"]["manifestNameBaseline"] == "acme"


# ── #296：显式 add / --no-clone 的 flag / policy 引用守卫 ─────────────────────
@requires_git
@pytest.mark.asyncio
async def test_add_rejected_by_flag_layer_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296：显式 add（strict 提交点）受 flag 层引用守卫；不传 flag 层 → 放行（正对照）。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    flag = tmp_path / "flag.json"
    flag.write_text(json.dumps({"enabledPlugins": {"audit@acme": False}}), encoding="utf-8")

    code = await marketplace_add(
        reg, home, env, f"file://{bare}", trust=True, json_output=True, flag_settings_path=flag
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation" and payload["reason"] == "dangling-references"
    assert payload["referenceCount"] == 1
    assert load_known_marketplaces(home, env).get("marketplaces") in (None, {}), "拒绝零残留"

    # 正对照：同一源不传 flag 层 → 注册成功
    assert await marketplace_add(reg, home, env, f"file://{bare}", trust=True, json_output=True) == 0


@requires_git
@pytest.mark.asyncio
async def test_add_rejected_by_injected_policy_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296：显式 add 受显式注入的 policy 层引用守卫。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    policy = {"enabledPlugins": {"audit@acme": True}}

    code = await marketplace_add(
        reg, home, env, f"file://{bare}", trust=True, json_output=True, policy_settings=policy
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "dangling-references"

    # 正对照：显式空 policy → 放行
    assert (
        await marketplace_add(
            reg, home, env, f"file://{bare}", trust=True, json_output=True, policy_settings={}
        )
        == 0
    )


@pytest.mark.asyncio
async def test_no_clone_rejected_by_flag_layer_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296：``--no-clone`` 意图注册同样受 flag 层守卫（第二处守卫调用点）。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _seed_cached_marketplace(home)
    flag = tmp_path / "flag.json"
    flag.write_text(json.dumps({"enabledPlugins": {"audit@acme": False}}), encoding="utf-8")

    code = await marketplace_add(
        reg,
        home,
        env,
        "https://example.com/org/repo.git",
        name="acme",
        trust=True,
        no_clone=True,
        json_output=True,
        flag_settings_path=flag,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "dangling-references"
    assert load_known_marketplaces(home, env).get("marketplaces") in (None, {})

    # 正对照：不传 flag 层 → 意图注册成功
    assert (
        await marketplace_add(
            reg,
            home,
            env,
            "https://example.com/org/repo.git",
            name="acme",
            trust=True,
            no_clone=True,
            json_output=True,
        )
        == 0
    )


@requires_git
@pytest.mark.asyncio
async def test_stage_unregistered_name_commit_uses_flag_layer_guard(tmp_path: Path) -> None:
    """#296：``stage_marketplace_skills`` 的提交点（strict=False 降级路径）也接同一五层视图。

    常规调用方（reconcile / refresh / recovery）都带既有注册（prior 非 None），dangling 判据不触发；
    这里直接以「账本无记录 + clone 缺失」调公共 API，专门覆盖 stage → commit 的轴转发（否则该转发不可观测）。
    """
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    flag = tmp_path / "flag.json"
    flag.write_text(json.dumps({"enabledPlugins": {"audit@acme": False}}), encoding="utf-8")

    names = await stage_marketplace_skills(
        "acme",
        {"type": "git", "url": f"file://{bare}"},
        reg,
        home,
        env=env,
        flag_settings_path=flag,
        policy_settings={},
    )

    assert names == []
    assert "acme" not in load_known_marketplaces(home, env)["marketplaces"], "拒绝不得写账本"
    assert not (home / "marketplace" / "acme").exists(), "拒绝不得换入 clone"


@pytest.mark.asyncio
async def test_no_clone_rejected_by_injected_policy_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#296：``--no-clone`` 受显式注入的 policy 层引用守卫。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _seed_cached_marketplace(home)
    policy = {"enabledPlugins": {"audit@acme": False}}

    code = await marketplace_add(
        reg,
        home,
        env,
        "https://example.com/org/repo.git",
        name="acme",
        trust=True,
        no_clone=True,
        json_output=True,
        policy_settings=policy,
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "dangling-references"


def test_cli_root_settings_flag_rejects_no_clone_readd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#296：非交互 CLI 的**根级** ``--settings`` 必须进入守卫视图。

    ``--settings`` 在根回调收进 ``ctx.obj.flag_path``；``marketplace add`` 子命令此前从不读取。
    """
    from typer.testing import CliRunner

    from a2c_smcp.computer.cli import main as cli_main
    from a2c_smcp.computer.settings import policy as policy_mod

    env, home = _env(tmp_path), _home(tmp_path)
    monkeypatch.setenv("A2C_SKILL_HOME", env["A2C_SKILL_HOME"])
    monkeypatch.setenv("XDG_CONFIG_HOME", env["XDG_CONFIG_HOME"])
    # 隔离 ambient policy（本用例只验 flag 接线）
    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_kw: {})
    _seed_cached_marketplace(home)
    flag = tmp_path / "flag.json"
    flag.write_text(json.dumps({"enabledPlugins": {"audit@acme": False}}), encoding="utf-8")

    url = "https://example.com/org/repo.git"
    result = CliRunner().invoke(
        cli_main.app, ["--settings", str(flag), "marketplace", "add", url, "--name", "acme", "--trust", "--no-clone", "--json"]
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["reason"] == "dangling-references"

    # 正对照：不带根级 --settings → 放行
    ok = CliRunner().invoke(
        cli_main.app, ["marketplace", "add", url, "--name", "acme", "--trust", "--no-clone", "--json"]
    )
    assert ok.exit_code == 0, ok.output


def test_cli_marketplace_add_rejects_ambient_policy_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#296：非交互 CLI 路径在 ambient policy 有引用时拒绝 §5.14 re-add。

    policy 层经库层 ``policy_settings=None`` → ``resolve_policy_settings(env)`` 现解析（与 rust#300 同口径）；
    CLI 入口同时显式注入同一视图，二者一致。
    """
    from typer.testing import CliRunner

    from a2c_smcp.computer.cli import main as cli_main
    from a2c_smcp.computer.settings import policy as policy_mod

    env, home = _env(tmp_path), _home(tmp_path)
    monkeypatch.setenv("A2C_SKILL_HOME", env["A2C_SKILL_HOME"])
    monkeypatch.setenv("XDG_CONFIG_HOME", env["XDG_CONFIG_HOME"])
    _seed_cached_marketplace(home)
    monkeypatch.setattr(
        policy_mod,
        "resolve_policy_settings",
        lambda **_kw: {"enabledPlugins": {"audit@acme": True}},
    )

    result = CliRunner().invoke(
        cli_main.app,
        [
            "marketplace",
            "add",
            "https://example.com/org/repo.git",
            "--name",
            "acme",
            "--trust",
            "--no-clone",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["reason"] == "dangling-references"


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
    """
    替换 add 的注册事务与 staging：可按需模拟「成功注册」或「clone 失败（不注册）」。

    #260 起 add 的真实流程是「clone 到 staging → 读清单名 → 提交」，故同时替换
    :func:`add_marketplace_source`（否则会真连远端）；本组用例只关心**展示/脱敏**面，不关心 clone。
    """

    async def fake_add_source(
        source: object,
        home: Path,
        *,
        expected_name: str | None = None,
        auto_update: bool = False,
        env: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> str:
        if not register:
            raise marketplace_mod.SkillStagingError("clone failed")
        name = expected_name or "demo"

        def _mutate(f: dict[str, object]) -> object:
            f["marketplaces"][name] = {  # type: ignore[index]
                "source": dict(source),  # type: ignore[arg-type]
                "installLocation": "",
                "autoUpdate": auto_update,
                "manifestNameBaseline": name,
            }
            return f

        update_known_marketplaces(_mutate, home, env)
        return name

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
        return []

    monkeypatch.setattr(marketplace_mod, "add_marketplace_source", fake_add_source)
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
    """clone 失败错误：凭据 scheme URL → 去凭据展示；scp-like → 占位符；异常文本不插值。"""
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
async def test_add_manifest_name_invalid_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    #260 验收：清单 name 缺失/非法 → ``validation`` 结构化失败（``manifest-name-invalid``）。

    文案**不回显**清单里的原值（不可信仓库，可能内嵌凭据样文本）——真实 clone 路径由集成测试覆盖，
    这里钉 payload 形状与「不回显」。
    """
    from a2c_smcp.computer.skills.marketplace_identity import MarketplaceIdentityError

    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()

    async def fake_add_source(*_args: object, **_kwargs: object) -> str:
        raise MarketplaceIdentityError(
            "marketplace manifest has no usable top-level 'name' (must be a non-empty string)",
            reason="manifest-name-invalid",
        )

    monkeypatch.setattr(marketplace_mod, "add_marketplace_source", fake_add_source)
    code = await marketplace_add(reg, home, env, _CLEAN_URL, trust=True, json_output=True)
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": "validation",
        "message": "marketplace manifest has no usable top-level 'name' (must be a non-empty string)",
        "reason": "manifest-name-invalid",
    }
    assert load_known_marketplaces(home, env).get("marketplaces") in (None, {})
    assert not _load_trusted(env), "注册失败不得残留 trust"


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
    ("url", "markers"),
    [
        # 每条向量钉住它**实际走的文案分支**（#282）：只断言「凭据没回显」时，host 校验一变就会静默
        # 滑到另一条更早的失败分支（文案仍占位符）而测试照绿——回归锁失效。
        ("ssh://-/alice:PW_LEAK@example.com", ("not a well-formed git url",)),  # junk host：URL 校验失败文案
        # 凭据在 path（末段 / 中间段）、各种编码层级、尾斜杠、无 path：全部走**非交互 untrusted** 文案，
        # 展示值必须 fail-closed 成占位符。判据仍是「path 任一段含凭据样文本」——收窄为末段会让中间段
        # 三条转红（`%` / 全角 / 深编码的覆盖面同理）。
        ("https://host/alice:PW_LEAK@example.com", ("untrusted source",)),
        ("https://host/alice%EF%BC%9APW_LEAK%EF%BC%A0example.com", ("untrusted source",)),
        ("https://host/alice％３ＡPW_LEAK", ("untrusted source",)),
        ("https://host/alice%2525253APW_LEAK", ("untrusted source",)),
        ("https://host/repo%20name", ("untrusted source",)),
        ("https://host/alice:PW_LEAK@example.com/", ("untrusted source",)),
        ("https://host/team/alice:PW_LEAK@example.com/repo", ("untrusted source",)),
        ("https://host/team/alice%2525253APW_LEAK/repo", ("untrusted source",)),
        ("https://host/team/repo%20name/repo", ("untrusted source",)),
        ("https://host", ("untrusted source",)),  # 无 path：无法判定 → fail-closed
    ],
)
async def test_add_error_message_never_echoes_path_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], url: str, markers: tuple[str, ...]
) -> None:
    """
    #270/#282：add 失败文案不得回显 path 任一段里的凭据样文本。

    #260 起注册名来自清单，**「派生名失败」不再是 add 的失败面**；本组向量改由**非交互 untrusted**
    分支出口（clone 之前、对任意合法 URL 都可达），非法 URL 仍走 URL 校验分支——两条分支各自的
    marker 保持不变，避免整组静默滑到同一分支。
    """
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    code = await marketplace_add(reg, home, env, url, trust=False, json_output=True)
    assert code == 1
    out = capsys.readouterr().out
    assert "PW_LEAK" not in out and "alice" not in out
    assert REDACTED_GIT_SOURCE in out
    assert url not in out, f"失败文案不得回显原始 URL：{out}"
    for marker in markers:
        assert marker in out, f"向量应走 {marker!r} 分支，实际输出：{out}"


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


def test_error_url_display_judges_raw_source_not_quoted_display() -> None:
    """判定用**原始 source**：展示层 quote() 造出的 ``%XX`` 不得让干净 URL 被误打码（#282 审查 🟡）。

    反向保护：若把判据换回展示串（`urlsplit(git_url_for_display(raw)).path`），本条全部转红——
    这些 URL 的源码里没有 percent 转义，`%` 是展示层为了可读性自己造的。
    断言**精确展示值**（不写成 ``!= REDACTED_GIT_SOURCE``）：后者对「吞掉 path 只剩 host」的退化实现
    仍然全绿，且无法验证 CHANGELOG 声明的「按 #261 展示契约输出 `%XX` 形态」。
    """
    cases = {
        "https://host/团队/repo": "https://host/%E5%9B%A2%E9%98%9F/repo",
        "https://host/my team/repo": "https://host/my%20team/repo",
        "https://host/café/repo": "https://host/caf%C3%A9/repo",
        "https://host/repo|name": "https://host/repo%7Cname",
        # path-scoped 正对照（#282 复核 🟡）：query / fragment / userinfo 里的 `%` **不**触发
        # fail-closed——判据只看 path（展示层本就剥掉 query / fragment / userinfo，见 #261 契约）。
        # 若把判据改成对整条 raw 跑 `_credential_like`，下面两条会转红（本条即其回归锁）。
        "https://host/org/repo?redirect=https%3A%2F%2Fx": "https://host/org/repo",
        "https://alice%3APW@host/org/repo": "https://host/org/repo",
    }
    for url, expected in cases.items():
        assert marketplace_mod._error_url_display(url) == expected


@pytest.mark.asyncio
async def test_add_keeps_raw_url_and_uses_manifest_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#260：注册名来自清单（与 URL 无关）；``source.url`` 仍原样保存（raw 保真，#261 不变）。"""
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    _patch_stage(monkeypatch, register=True)
    url = "https://example.com/org/repo.git?token=FAKE_QUERY#FAKE_FRAGMENT"
    code = await marketplace_add(reg, home, env, url, trust=True, json_output=True)
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["added"] == "demo", "注册名来自清单（fake 返回 demo），不来自 URL 末段"
    assert "FAKE_QUERY" not in out and "FAKE_FRAGMENT" not in out
    assert _load_trusted(env) == {"demo"}, "trust 键 = 最终注册名"
    stored = load_known_marketplaces(home, env)["marketplaces"]["demo"]["source"]["url"]
    assert stored == url, "known_marketplaces 存储与 git argv 必须保留原始 URL（raw 保真）"


# ── #260：refresh 改名拒绝必须可判别（不得报 updated/unchanged）────────────────
@pytest.mark.asyncio
async def test_refresh_rename_reports_validation_not_updated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from a2c_smcp.computer.skills.marketplace_identity import MarketplaceNameChangeError

    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()

    def _seed(f):  # type: ignore[no-untyped-def]
        f["marketplaces"]["acme"] = {
            "source": {"type": "git", "url": "https://example.com/acme.git"},
            "installLocation": "",
            "commitSha": "deadbeef",
        }
        return f

    update_known_marketplaces(_seed, home, env)

    async def _rename(*_args: object, **_kwargs: object) -> list[str]:
        raise MarketplaceNameChangeError(
            "marketplace 'acme' was refreshed from a manifest that renames it to 'acme-renamed'",
            details={"name": "acme", "manifestName": "acme-renamed", "baselineManifestName": "acme"},
        )

    monkeypatch.setattr(marketplace_mod, "stage_marketplace_skills", _rename)

    code = await marketplace_refresh(reg, home, env, "all", json_output=True)

    assert code == 1
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "acme"
    assert row["status"] == "validation", "改名拒绝 MUST NOT 报 updated / unchanged"
    assert row["reason"] == "manifest-name-changed"
    assert row["manifestName"] == "acme-renamed"
    assert "message" in row and "error" not in row, "--json 行的人读文案在 message（与 CHANGELOG 契约一致）"


@pytest.mark.asyncio
async def test_no_clone_rejects_name_registered_from_other_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    审查 🔴2 回归锁：``--no-clone`` 的「本地缓存定名」形态**不得**绕过提交点——同名已绑定另一 source 时
    必须 ``validation``，且**不得**改写既有记录的 source。
    """
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()
    clone = home / "marketplace" / "acme"
    (clone / ".tfrobot-plugin").mkdir(parents=True)
    (clone / ".tfrobot-plugin" / "marketplace.json").write_text(json.dumps({"name": "acme", "plugins": []}), encoding="utf-8")
    old_source = {"type": "git", "url": "https://example.com/OLD.git"}
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"acme": {"source": old_source, "installLocation": str(clone), "manifestNameBaseline": "acme"}}},
        home,
        env,
    )

    code = await marketplace_add(
        reg, home, env, "https://example.com/NEW.git", name="acme", trust=True, no_clone=True, json_output=True
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "validation" and payload["reason"] == "name-conflict"
    stored = load_known_marketplaces(home, env)["marketplaces"]["acme"]
    assert stored["source"] == old_source, "不得静默改写既有注册的 source"


def test_refresh_summary_table_escapes_untrusted_text(capsys: pytest.CaptureFixture[str]) -> None:
    """审查 🟡4/5 回归锁：账本键 / 错误文案含 rich 标记时，汇总表不得抛 MarkupError。"""
    from a2c_smcp.computer.cli.progress import refresh_summary_table
    from a2c_smcp.computer.cli.utils import console

    table = refresh_summary_table([{"name": "[/]x", "status": "validation", "message": "[bold]boom[/bold]"}])
    console.print(table)  # 真实渲染路径：未转义会抛 MarkupError
    out = capsys.readouterr().out
    assert "[/]x" in out and "boom" in out


@pytest.mark.asyncio
async def test_repl_refresh_marks_dirty_on_partial_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    审查 🟡4 回归锁：`refresh` 部分源内容更新 + 部分源改名拒绝（退出码 1）时，registry 的**完整 ref**
    已变化 → 必须标脏；只比 SKILL 名字集合会漏（名字相同、version 变了）。
    """
    from a2c_smcp.computer.skills.registry import A2CSkillRef

    home = _home(tmp_path)
    reg = SkillRegistry()
    reg.register(
        A2CSkillRef(
            name="audit:lint",
            source="marketplace:acme",
            uri="skill://acme/audit/lint/SKILL.md",
            path=str(home / "marketplace" / "acme" / "audit" / "lint"),
            version="1.0.0",
        )
    )
    comp = _ReplComp(home, reg)

    async def fake_refresh(*_args: object, **_kwargs: object) -> int:
        # 模拟「A 内容更新（version 变）+ B 改名被拒」：退出码 1，但名字集合不变
        reg.register_or_update(
            A2CSkillRef(
                name="audit:lint",
                source="marketplace:acme",
                uri="skill://acme/audit/lint/SKILL.md",
                path=str(home / "marketplace" / "acme" / "audit" / "lint"),
                version="2.0.0",
            )
        )
        return 1

    monkeypatch.setattr(marketplace_mod, "marketplace_refresh", fake_refresh)

    await marketplace_mod.repl_dispatch(comp, ["marketplace", "refresh", "all"], session=_ReplSession())

    assert comp.dirty == 1, "内容更新（version 变）必须标脏，即使退出码为 1"


@requires_git
def test_add_conflict_payload_redacts_credential_like_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    审查 🔴 回归锁：`name-conflict` 的 `existingSource` 走**诊断面** URL 判据——path 段含凭据样文本时
    必须占位符（不能只挡 authority 段 userinfo）。变异 = 改回 known 展示原语 → 本条转红。
    """
    import asyncio

    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    env, home = _env(tmp_path), _home(tmp_path)
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "acme": {
                    "source": {"type": "git", "url": "https://host/team/alice:PW_LEAK@example.com/repo.git"},
                    "installLocation": "",
                }
            },
        },
        home,
        env,
    )

    bare = _make_bare(tmp_path, "acme", _marketplace_files())  # 本地源，清单名 acme → 与既有记录同名异源

    async def _run() -> int:
        return await marketplace_add(
            SkillRegistry(), home, env, f"file://{bare}", trust=True, json_output=True
        )

    assert asyncio.run(_run()) == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["error"] == "validation" and payload["reason"] == "name-conflict"
    assert payload["existingSource"] == REDACTED_GIT_SOURCE
    assert "PW_LEAK" not in out and "alice" not in out


@pytest.mark.asyncio
async def test_source_conflict_payload_redacts_path_credential_ledger_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    终审 🔴 回归锁（CLI 面）：手编账本键为含 path 段凭据的 URL 时，`--json` 的 message / `existingName` /
    `remedy` 均不得回显凭据。
    """
    from a2c_smcp.computer.skills.staging import registration_violation

    env, home = _env(tmp_path), _home(tmp_path)
    key = "https://host/team/alice:PW_LEAK@example.com/repo.git"
    source = {"type": "git", "url": "https://example.com/new.git"}
    known = {key: {"source": source, "installLocation": ""}}
    violation = registration_violation("acme", source, None, known, "acme", env=env)
    assert violation is not None

    async def _conflict(*_args: object, **_kwargs: object) -> str:
        raise violation

    monkeypatch.setattr(marketplace_mod, "add_marketplace_source", _conflict)
    code = await marketplace_add(SkillRegistry(), home, env, "https://example.com/new.git", trust=True, json_output=True)

    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["error"] == "validation" and payload["reason"] == "source-conflict"
    assert "PW_LEAK" not in out and "alice" not in out


def test_registry_fingerprints_tolerate_hostile_metadata(tmp_path: Path) -> None:
    """终审 🟡1 回归锁：frontmatter metadata 含非 str 键 / 自引用环时，指纹不得抛（旧实现用 json.dumps）。"""
    from a2c_smcp.computer.skills.registry import A2CSkillRef

    metadata: dict[object, object] = {1: "int-key", "mixed": [1, {"nested": None}]}
    ref = A2CSkillRef(
        name="audit:lint",
        source="marketplace:acme",
        uri="skill://acme/audit/lint/SKILL.md",
        path=str(tmp_path / "x"),
        description="d",
        skill_metadata=metadata,  # type: ignore[typeddict-item]
    )
    metadata["self"] = ref  # 自引用环：指纹必须靠深度封顶终止
    reg = SkillRegistry()
    assert reg.register(ref)

    fps = marketplace_mod._registry_fingerprints(reg)
    assert len(fps) == 1
    # 可选字段（skill_metadata）参与指纹：改值即可识别（避免「字段没进指纹」也照绿）
    assert reg.register_or_update(
        A2CSkillRef(
            name="audit:lint",
            source="marketplace:acme",
            uri="skill://acme/audit/lint/SKILL.md",
            path=str(tmp_path / "x"),
            description="d",
            skill_metadata={"other": 2},  # type: ignore[typeddict-item]
        )
    )
    assert marketplace_mod._registry_fingerprints(reg) != fps


def test_registry_fingerprints_track_orphan_flag(tmp_path: Path) -> None:
    """终审 🟡2 回归锁：ref 内容不变、仅 orphaned 翻转时，指纹也必须变化（否则 refresh 复活孤儿不标脏）。"""
    from a2c_smcp.computer.skills.registry import A2CSkillRef

    reg = SkillRegistry()
    reg.register(
        A2CSkillRef(
            name="audit:lint",
            source="marketplace:acme",
            uri="skill://acme/audit/lint/SKILL.md",
            path=str(tmp_path / "x"),
            description="d",
        )
    )
    before = marketplace_mod._registry_fingerprints(reg)
    assert reg.mark_orphan("audit:lint") is True
    after = marketplace_mod._registry_fingerprints(reg)
    assert before != after, "孤儿标志必须进入指纹"


def test_registry_fingerprints_track_tags_only_change(tmp_path: Path) -> None:
    """终审 🟡3 回归锁：仅 `tags`（或其它可选字段）变化也必须改变指纹。"""
    from a2c_smcp.computer.skills.registry import A2CSkillRef

    reg = SkillRegistry()
    base = dict(
        name="audit:lint",
        source="marketplace:acme",
        uri="skill://acme/audit/lint/SKILL.md",
        path=str(tmp_path / "x"),
        description="d",
        version="1.0.0",
    )
    reg.register(A2CSkillRef(**base, tags=["sec"]))  # type: ignore[typeddict-item]
    before = marketplace_mod._registry_fingerprints(reg)
    reg.register_or_update(A2CSkillRef(**base, tags=["sec", "audit"]))  # type: ignore[typeddict-item]
    assert before != marketplace_mod._registry_fingerprints(reg)


@pytest.mark.asyncio
async def test_repl_refresh_marks_dirty_on_tags_only_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """终审 🟡3：另一源改名拒绝（退出码 1）+ 本源仅 tags 变化 → 仍须标脏。"""
    from a2c_smcp.computer.skills.registry import A2CSkillRef

    home = _home(tmp_path)
    reg = SkillRegistry()
    base = dict(
        name="audit:lint",
        source="marketplace:acme",
        uri="skill://acme/audit/lint/SKILL.md",
        path=str(home / "marketplace" / "acme" / "audit" / "lint"),
        description="d",
    )
    reg.register(A2CSkillRef(**base, tags=["sec"]))  # type: ignore[typeddict-item]
    comp = _ReplComp(home, reg)

    async def fake_refresh(*_args: object, **_kwargs: object) -> int:
        reg.register_or_update(A2CSkillRef(**base, tags=["sec", "audit"]))  # type: ignore[typeddict-item]
        return 1  # 另一源改名被拒

    monkeypatch.setattr(marketplace_mod, "marketplace_refresh", fake_refresh)

    await marketplace_mod.repl_dispatch(comp, ["marketplace", "refresh", "all"], session=_ReplSession())

    assert comp.dirty == 1, "tags-only 更新也必须标脏"


# ── #260 终审 🔴：展示面（refresh/list/info）不得回显手编凭据账本键 ─────────────
_CRED_KEY = "https://host/team/alice:PW_LEAK@example.com/repo.git"


def _seed_credential_ledger(home: Path, env: dict[str, str]) -> None:
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                _CRED_KEY: {"source": {"type": "git", "url": "https://example.com/r.git"}, "installLocation": ""},
                "acme": {"source": {"type": "git", "url": "https://example.com/a.git"}, "installLocation": ""},
            },
        },
        home,
        env,
    )


@pytest.mark.asyncio
async def test_refresh_display_redacts_credential_ledger_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """终审 🔴：`refresh --json` 行与 rich 汇总表都不回显账本键里的凭据；干净 kebab 名仍可读。"""
    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    env, home = _env(tmp_path), _home(tmp_path)
    _seed_credential_ledger(home, env)

    async def _noop_stage(*_args: object, **_kwargs: object) -> list[str]:
        return []  # 脱敏与 clone 无关：替换 staging 避免真跑网络

    monkeypatch.setattr(marketplace_mod, "stage_marketplace_skills", _noop_stage)

    assert await marketplace_refresh(SkillRegistry(), home, env, "all", json_output=True) == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    names = {row["name"] for row in rows}
    assert "PW_LEAK" not in out and "alice" not in out
    assert REDACTED_GIT_SOURCE in names and "acme" in names

    await marketplace_refresh(SkillRegistry(), home, env, "all")
    out = capsys.readouterr().out
    assert "PW_LEAK" not in out and "alice" not in out
    assert "acme" in out


def test_list_and_info_redact_credential_ledger_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """终审 🔴：`list` / `info` 的 name 展示面同样走账本键脱敏。"""
    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    env, home = _env(tmp_path), _home(tmp_path)
    _seed_credential_ledger(home, env)

    marketplace_list(home, env, json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    names = {row["name"] for row in payload}
    assert "PW_LEAK" not in out and "alice" not in out
    assert REDACTED_GIT_SOURCE in names and "acme" in names

    marketplace_list(home, env)
    out = capsys.readouterr().out
    assert "PW_LEAK" not in out and "alice" not in out

    marketplace_info(home, env, _CRED_KEY, json_output=True)
    out = capsys.readouterr().out
    assert "PW_LEAK" not in out and "alice" not in out
    assert json.loads(out)["name"] == REDACTED_GIT_SOURCE

    marketplace_info(home, env, _CRED_KEY)
    out = capsys.readouterr().out
    assert "PW_LEAK" not in out and "alice" not in out


def test_stable_text_is_injective_on_known_collisions() -> None:
    """终审 🟡2 回归锁：旧 `str()` 实现的已知碰撞对必须被区分开。"""
    from a2c_smcp.computer.cli.commands.marketplace import _stable_text

    pairs = [
        (["a,b"], ["a", "b"]),
        (None, "None"),
        (1, "1"),
        ({"a": 1}, {"a": "1"}),
        ([1], ["1"]),
        ({"a"}, {"a", "b"}),
        (True, 1),
    ]
    for left, right in pairs:
        assert _stable_text(left) != _stable_text(right), f"{left!r} 与 {right!r} 不应碰撞"


def test_stable_text_bounded_on_wide_and_cyclic_metadata() -> None:
    """
    终审 🟡3 回归锁：超宽容器（alias bomb 的宽形态，如 ``[0]*200000``）与自引用环都必须在
    **节点预算**内终止、输出有界——深度封顶挡不住宽容器。
    """
    from a2c_smcp.computer.cli.commands.marketplace import _stable_text

    wide = {"metadata": list(range(200_000))}
    text = _stable_text(wide)
    assert len(text) < 200_000, f"输出必须有界，实际 {len(text)}"

    cyclic: dict[str, object] = {"x": 1}
    cyclic["self"] = cyclic
    assert len(_stable_text(cyclic)) < 10_000


@pytest.mark.asyncio
async def test_no_clone_intent_display_redacts_ledger_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """终审 🔴1：`--no-clone` 幂等意图的 JSON / rich 输出不得外泄账本键里的凭据。"""
    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    env, home = _env(tmp_path), _home(tmp_path)
    _seed_credential_ledger(home, env)
    url = "https://example.com/r.git"  # 与 seeded 凭据键的 source 同源

    code = await marketplace_add(
        SkillRegistry(), home, env, url, trust=True, no_clone=True, json_output=True
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "PW_LEAK" not in out and "alice" not in out
    assert json.loads(out)["registered"] == REDACTED_GIT_SOURCE

    code = await marketplace_add(SkillRegistry(), home, env, url, trust=True, no_clone=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "PW_LEAK" not in out and "alice" not in out


@pytest.mark.asyncio
async def test_no_clone_intent_display_escapes_markup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """终审 🔴1（rich MarkupError 面）：账本键含 `[/]` 时不得抛 MarkupError。"""
    env, home = _env(tmp_path), _home(tmp_path)
    source = {"type": "git", "url": "https://example.com/markup.git"}
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"[/]x": {"source": source, "installLocation": ""}}},
        home,
        env,
    )

    code = await marketplace_add(
        SkillRegistry(), home, env, "https://example.com/markup.git", trust=True, no_clone=True
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "[/]x" in out


def test_info_json_redacts_installed_plugin_market_segment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """终审 🔴2：`info --json` 的 `installedPlugins`（pid 的 market 段 = 账本键）同样脱敏。"""
    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    env, home = _env(tmp_path), _home(tmp_path)
    _seed_credential_ledger(home, env)
    save_installed_plugins(
        {"version": 1, "plugins": {f"audit@{_CRED_KEY}": [{"scope": "user", "installPath": str(home)}]}},
        home,
        env,
    )

    assert marketplace_info(home, env, _CRED_KEY, json_output=True) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "PW_LEAK" not in out and "alice" not in out
    assert payload["installedPlugins"] == [f"audit@{REDACTED_GIT_SOURCE}"]


def test_list_table_escapes_markup_in_name_and_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """终审 🟡4：`list` 表格的 name / url 单元格含 rich 标记时不得抛 MarkupError。"""
    env, home = _env(tmp_path), _home(tmp_path)
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "[/]x": {"source": {"type": "git", "url": "https://example.com/[/]repo.git"}, "installLocation": ""}
            },
        },
        home,
        env,
    )

    assert marketplace_list(home, env) == 0
    out = capsys.readouterr().out
    assert "[/]x" in out


@pytest.mark.asyncio
async def test_refresh_spinner_description_is_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """终审 🟡3：spinner 描述同样属展示面（pytest 下 `is_terminal=False`，需直接记录入参）。"""
    from contextlib import contextmanager

    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    env, home = _env(tmp_path), _home(tmp_path)
    _seed_credential_ledger(home, env)
    seen: list[str] = []

    @contextmanager
    def _spy(description: str, *, enabled: bool = True):  # type: ignore[no-untyped-def]
        seen.append(description)
        yield

    async def _noop_stage(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(marketplace_mod, "clone_spinner", _spy)
    monkeypatch.setattr(marketplace_mod, "stage_marketplace_skills", _noop_stage)

    await marketplace_refresh(SkillRegistry(), home, env, "all", json_output=True)
    capsys.readouterr()

    joined = " ".join(seen)
    assert REDACTED_GIT_SOURCE in joined
    assert "PW_LEAK" not in joined and "alice" not in joined


@pytest.mark.asyncio
async def test_add_commit_oserror_returns_structured_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """终审 🟡1：提交点 `os.replace` 失败（Windows 占用 / 权限）→ 结构化错误，不抛 traceback。"""
    env, home = _env(tmp_path), _home(tmp_path)

    async def _replace_fails(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("file in use")

    monkeypatch.setattr(marketplace_mod, "add_marketplace_source", _replace_fails)

    code = await marketplace_add(SkillRegistry(), home, env, "https://example.com/a.git", trust=True, json_output=True)
    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["error"] == "error"
    assert "failed to register the marketplace" in payload["message"]
    # 状态一致：失败不得写账本、不得残留 trust
    assert (load_known_marketplaces(home, env).get("marketplaces") or {}) == {}
    assert not _load_trusted(env)


def test_ok_message_escapes_markup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """终审 🔴 同类硬化：`_ok` 成功行常内插用户输入 / 账本键，rich 标记必须转义（不得 MarkupError）。"""
    env, home = _env(tmp_path), _home(tmp_path)
    save_known_marketplaces(
        {"version": 1, "marketplaces": {"[/]x": {"source": {"type": "git", "url": "https://example.com/x.git"}, "installLocation": ""}}},
        home,
        env,
    )
    assert marketplace_set(home, env, "[/]x", "auto-update", "true") == 0
    out = capsys.readouterr().out
    assert "[/]x" in out


# ── #260 七审收敛：rich 渲染面 / 双转义 / OSError 口径 ────────────────────────
def test_info_table_escapes_markup_and_non_string_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """七审 🔴1：`info` 非 JSON 表格的每个单元格都须 escape（手编 `lastUpdated`/URL path 含标记）。"""
    env, home = _env(tmp_path), _home(tmp_path)
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "acme": {
                    "source": {"type": "git", "url": "https://example.com/repo[/]src.git"},
                    "installLocation": "/tmp/[/]x",
                    "commitSha": "[/]sha",
                    "lastUpdated": "[/]ts",
                }
            },
        },
        home,
        env,
    )

    assert marketplace_info(home, env, "acme") == 0
    out = capsys.readouterr().out
    assert "[/]ts" in out and "[/]sha" in out


def test_list_table_escapes_timestamp_and_commit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """七审 🟡2：`list` 的 lastUpdated / commitSha 单元格须 escape 且容忍非 str。"""
    env, home = _env(tmp_path), _home(tmp_path)
    save_known_marketplaces(
        {
            "version": 1,
            "marketplaces": {
                "acme": {
                    "source": {"type": "git", "url": "https://example.com/a.git"},
                    "installLocation": "",
                    "lastUpdated": "[/]ts",
                    "commitSha": 12345,  # 非 str：旧实现 `sha[:10]` 会 TypeError
                }
            },
        },
        home,
        env,
    )

    assert marketplace_list(home, env) == 0
    out = capsys.readouterr().out
    assert "[/]ts" in out


@pytest.mark.asyncio
async def test_refresh_spinner_renders_markup_like_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    七审 🔴2：spinner 描述参与 rich 渲染时不得抛 `MarkupError`（`progress.py` 的 `TextColumn(markup=False)`）。

    用**真实渲染**（forced-terminal 的 Console 写 StringIO），而非不渲染的 spy。
    """
    import io as _io

    from rich.console import Console

    import a2c_smcp.computer.cli.progress as progress_mod

    sink = _io.StringIO()
    monkeypatch.setattr(progress_mod, "console", Console(file=sink, force_terminal=True, width=120))

    with progress_mod.clone_spinner("Refreshing [/b]x..."):
        pass  # 渲染发生在 enter/exit

    assert "[/b]x" in sink.getvalue()


def test_redact_pid_fails_closed_on_credential_like_plugin_segment() -> None:
    """七审 🟡7：手编 pid 的 plugin 段含凭据样文本 → 该段 fail-closed（机器写入路径两段均为严格 kebab）。"""
    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    assert marketplace_mod._redact_pid("alice:PW_LEAK@acme") == f"{REDACTED_GIT_SOURCE}@acme"
    assert marketplace_mod._redact_pid("audit@acme") == "audit@acme"


@pytest.mark.asyncio
async def test_no_clone_intent_display_not_double_escaped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """七审 🟡1：脱敏后由 `_ok` 统一转义，不得出现二次转义（`\\[`）。"""
    env, home = _env(tmp_path), _home(tmp_path)
    source = {"type": "git", "url": "https://example.com/markup.git"}
    save_known_marketplaces({"version": 1, "marketplaces": {"[/]x": {"source": source, "installLocation": ""}}}, home, env)

    assert await marketplace_add(SkillRegistry(), home, env, "https://example.com/markup.git", trust=True, no_clone=True) == 0
    out = capsys.readouterr().out
    assert "[/]x" in out
    assert "\\[" not in out, "不得二次转义"


@pytest.mark.asyncio
async def test_add_trust_persist_oserror_is_warn_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """七审 🟡3：`_record_trust` 抛 OSError（file_lock / os.replace）→ WARN，不回滚已成功注册、不 traceback。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    env, home, reg = _env(tmp_path), _home(tmp_path), SkillRegistry()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(marketplace_mod, "_record_trust", _boom)

    code = await marketplace_add(reg, home, env, f"file://{bare}", trust=True)
    captured = capsys.readouterr()
    assert code == 0
    assert "acme" in (load_known_marketplaces(home, env).get("marketplaces") or {})
    assert "failed to persist its trust entry" in captured.out + captured.err  # WARN 面可感


@pytest.mark.asyncio
async def test_intent_oserror_returns_structured_error_and_leaves_no_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """七审 🟡6：intent 路径的 OSError 分支有覆盖，且账本/trust 状态不变。"""
    env, home = _env(tmp_path), _home(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(marketplace_mod, "update_known_marketplaces", _boom)

    code = await marketplace_add(
        SkillRegistry(), home, env, "https://example.com/x.git", trust=True, no_clone=True, json_output=True
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "failed to register the marketplace intent" in json.loads(out)["message"]
    assert (load_known_marketplaces(home, env).get("marketplaces") or {}) == {}
    assert not _load_trusted(env), "失败不得残留 trust"


def test_info_table_installed_plugins_redacted_non_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """七审 🟡5：非 JSON `info` 的 installedPlugins 分支同样脱敏（此前只有 JSON 面有锁）。"""
    env, home = _env(tmp_path), _home(tmp_path)
    _seed_credential_ledger(home, env)
    save_installed_plugins(
        {"version": 1, "plugins": {f"audit@{_CRED_KEY}": [{"scope": "user", "installPath": str(home)}]}},
        home,
        env,
    )

    assert marketplace_info(home, env, _CRED_KEY) == 0
    out = capsys.readouterr().out
    assert "PW_LEAK" not in out and "alice" not in out


@pytest.mark.asyncio
async def test_intent_trust_persist_oserror_is_warn_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """七审 🟡3（intent 面）：`_record_trust` 抛 OSError → WARN，注册事实保持、不 traceback。"""
    env, home = _env(tmp_path), _home(tmp_path)
    source = {"type": "git", "url": "https://example.com/x.git"}
    save_known_marketplaces({"version": 1, "marketplaces": {"acme": {"source": source, "installLocation": str(home)}}}, home, env)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(marketplace_mod, "_record_trust", _boom)

    code = await marketplace_add(SkillRegistry(), home, env, "https://example.com/x.git", trust=True, no_clone=True)
    captured = capsys.readouterr()
    assert code == 0
    assert "acme" in (load_known_marketplaces(home, env).get("marketplaces") or {})
    assert "could not be persisted" in captured.out + captured.err  # WARN 面可感（intent 路径）
