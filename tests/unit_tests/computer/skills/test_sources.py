# -*- coding: utf-8 -*-
# filename: test_sources.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
marketplace plugin source 解析单元测试（v0.2.1 #61）

协议依据 / Protocol: tfrobot-marketplace docs/marketplace/protocol-v1.md §5（Plugin source 5 类
disjoint union + github/cnb 简写糖 + git-subdir）、§3.2（pluginRoot 裸名解析）、§5.3（marketplace
source vs plugin source 两套独立 schema）。

测试意图 / Test intentions:
- 相对路径（含裸名经 pluginRoot 解析）/ ``url`` / ``github`` / ``cnb`` / ``git-subdir`` 五类各解析正确
- github / cnb ``owner/repo`` 简写糖归一化到对应 host 的 https url
- 非法形态（`..` 穿越、缺必填、非简写糖、未知 discriminator、坏 sha）一律抛 SkillSourceError
- marketplace source（{type:git,url}）提取与校验
"""

import pytest

from a2c_smcp.computer.skills.sources import (
    DEFAULT_PLUGIN_ROOT,
    GitCloneSpec,
    LocalPluginSource,
    SkillSourceError,
    marketplace_clone_url,
    normalize_repo_shorthand,
    resolve_plugin_source,
)

_SHA40 = "a" * 40


# ── 相对路径 / relative-path（含裸名 pluginRoot 解析）─────────────────────────
def test_relative_dot_slash_repo_root() -> None:
    r = resolve_plugin_source("./my-plugin")
    assert r == LocalPluginSource(rel_path="my-plugin")


def test_relative_dot_slash_nested() -> None:
    r = resolve_plugin_source("./plugins/frontend-design")
    assert r == LocalPluginSource(rel_path="plugins/frontend-design")


def test_relative_bare_name_uses_default_plugin_root() -> None:
    # 裸名经 metadata.pluginRoot（默认 ./plugins）解析（协议 §3.2）
    r = resolve_plugin_source("data-toolkit")
    assert isinstance(r, LocalPluginSource)
    assert r.rel_path == "plugins/data-toolkit"
    assert DEFAULT_PLUGIN_ROOT == "./plugins"


def test_relative_bare_name_custom_plugin_root() -> None:
    r = resolve_plugin_source("formatter", plugin_root="./vendor/plugins")
    assert r == LocalPluginSource(rel_path="vendor/plugins/formatter")


def test_relative_traversal_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source("./../escape")


def test_relative_slashed_non_dotslash_rejected() -> None:
    # 非 "./" 开头又含 "/"：既非合法相对路径，也非裸名 → 拒绝
    with pytest.raises(SkillSourceError):
        resolve_plugin_source("foo/bar")


# ── url ───────────────────────────────────────────────────────────────────────
def test_url_basic() -> None:
    r = resolve_plugin_source({"source": "url", "url": "https://example.com/team/p.git"})
    assert r == GitCloneSpec(url="https://example.com/team/p.git")


def test_url_with_ref_and_sha() -> None:
    r = resolve_plugin_source({"source": "url", "url": "git@gitlab.com:team/p.git", "ref": "v1.0", "sha": _SHA40})
    assert isinstance(r, GitCloneSpec)
    assert r.url == "git@gitlab.com:team/p.git"
    assert r.ref == "v1.0"
    assert r.sha == _SHA40


def test_url_invalid_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "url", "url": "not a url"})


def test_url_missing_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "url"})


# ── github 简写糖 / github shorthand ─────────────────────────────────────────
def test_github_shorthand_normalized() -> None:
    r = resolve_plugin_source({"source": "github", "repo": "turingfocus/tfs-auth-plugin", "ref": "v0.3.1"})
    assert r == GitCloneSpec(url="https://github.com/turingfocus/tfs-auth-plugin.git", ref="v0.3.1")


def test_github_strips_trailing_git_suffix() -> None:
    r = resolve_plugin_source({"source": "github", "repo": "owner/repo.git"})
    assert isinstance(r, GitCloneSpec)
    assert r.url == "https://github.com/owner/repo.git"


def test_github_full_url_repo_rejected() -> None:
    # github 的 repo 字段只接受 owner/repo 简写，不接受完整 URL
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "github", "repo": "https://github.com/owner/repo"})


def test_github_single_segment_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "github", "repo": "owner-only"})


# ── cnb 简写糖 / cnb shorthand ───────────────────────────────────────────────
def test_cnb_shorthand_normalized() -> None:
    r = resolve_plugin_source({"source": "cnb", "repo": "turingfocus/tfs-auth-plugin"})
    assert r == GitCloneSpec(url="https://cnb.cool/turingfocus/tfs-auth-plugin.git")


# ── git-subdir ────────────────────────────────────────────────────────────────
def test_git_subdir_basic() -> None:
    r = resolve_plugin_source(
        {"source": "git-subdir", "url": "https://github.com/adobe/skills.git", "path": "plugins/creative-cloud/adobe"},
    )
    assert r == GitCloneSpec(url="https://github.com/adobe/skills.git", subdir="plugins/creative-cloud/adobe")


def test_git_subdir_url_shorthand_normalizes_to_github() -> None:
    # git-subdir 的 url 亦接受 GitHub 简写 owner/repo（协议 §5.2.3）
    r = resolve_plugin_source({"source": "git-subdir", "url": "adobe/skills", "path": "plugins/x", "sha": _SHA40})
    assert isinstance(r, GitCloneSpec)
    assert r.url == "https://github.com/adobe/skills.git"
    assert r.subdir == "plugins/x"
    assert r.sha == _SHA40


def test_git_subdir_path_traversal_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "git-subdir", "url": "https://h/r.git", "path": "../escape"})


def test_git_subdir_missing_path_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "git-subdir", "url": "https://h/r.git"})


# ── 通用错误 / generic errors ────────────────────────────────────────────────
def test_unknown_discriminator_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "npm", "package": "x"})


def test_non_str_non_mapping_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source(12345)


def test_bad_sha_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "url", "url": "https://h/r.git", "sha": "abc123"})


def test_ref_non_string_rejected() -> None:
    with pytest.raises(SkillSourceError):
        resolve_plugin_source({"source": "url", "url": "https://h/r.git", "ref": 123})


def test_relative_dot_only_empty_rejected() -> None:
    # "./." 归一化后无有效路径段（resolves to repo root）→ 拒绝
    with pytest.raises(SkillSourceError):
        resolve_plugin_source("./.")


# ── normalize_repo_shorthand 直测 / standalone ───────────────────────────────
def test_normalize_repo_shorthand_github() -> None:
    assert normalize_repo_shorthand("owner/repo", "github.com") == "https://github.com/owner/repo.git"


def test_normalize_repo_shorthand_cnb() -> None:
    assert normalize_repo_shorthand("g/p", "cnb.cool") == "https://cnb.cool/g/p.git"


def test_normalize_repo_shorthand_scp_like_rejected() -> None:
    with pytest.raises(SkillSourceError):
        normalize_repo_shorthand("git@github.com:owner/repo", "github.com")


# ── marketplace_clone_url ─────────────────────────────────────────────────────
def test_marketplace_clone_url_ok() -> None:
    assert marketplace_clone_url({"type": "git", "url": "git@github.com:team/skills.git"}) == "git@github.com:team/skills.git"


def test_marketplace_clone_url_wrong_type_rejected() -> None:
    with pytest.raises(SkillSourceError):
        marketplace_clone_url({"type": "github", "url": "owner/repo"})


def test_marketplace_clone_url_invalid_url_rejected() -> None:
    with pytest.raises(SkillSourceError):
        marketplace_clone_url({"type": "git", "url": "bogus"})


def test_marketplace_clone_url_non_mapping_rejected() -> None:
    with pytest.raises(SkillSourceError):
        marketplace_clone_url("https://h/r.git")
