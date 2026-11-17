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

import json
from collections.abc import Callable

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


# ── 构造期脱敏（#263）/ construction-time redaction ───────────────────────────
# 对拍 rust-sdk#154（PR #156 已合入）的五类向量 + 裸 token。契约：错误文本与 ``e.raw`` 不含
# userinfo / query / fragment；可解析的 scheme URL 保留 scheme/host/path，其余形态
# （scp-like / opaque / 不支持 scheme / 裸 token）一律 ``<redacted-git-source>``。
_REDACTED = "<redacted-git-source>"


def _capture_source_error(fn: Callable[[], object]) -> SkillSourceError:
    with pytest.raises(SkillSourceError) as ei:
        fn()
    return ei.value


def test_source_error_raw_and_reason_are_strings() -> None:
    e = _capture_source_error(lambda: resolve_plugin_source("./../escape"))
    assert isinstance(e.raw, str)
    assert isinstance(e.reason, str)
    # 字符串形态保留原值（无凭据可脱）——钉死"不是无条件占位符"这一语义。
    assert e.raw == "./../escape"
    assert e.reason == "path must not contain '..'"
    assert str(e) == "invalid skill source ./../escape: path must not contain '..'"


def test_vector_ftp_with_credentials_fails_closed() -> None:
    """rust#154 向量①：不支持的 scheme ⇒ 整个 URL token 占位符（含 host）。"""
    e = _capture_source_error(
        lambda: resolve_plugin_source(
            {"source": "url", "url": "ftp://cnb:FAKE_TOKEN@example.com/org/repo.git?token=QUERY#FRAGMENT"}
        )
    )
    text = str(e)
    for secret in ("cnb", "FAKE_TOKEN", "QUERY", "FRAGMENT"):
        assert secret not in text
        assert secret not in e.raw
    assert _REDACTED in text
    assert e.raw == '{"source":"url","url":"' + _REDACTED + '"}'


def test_vector_key_prefix_url_keeps_host_drops_userinfo() -> None:
    """rust#154 向量②：``key=`` 前缀 URL 保留 host/path，去掉 userinfo。"""
    e = _capture_source_error(lambda: resolve_plugin_source("key=https://user:PW_PREFIX@example.com/org/repo.git"))
    text = str(e)
    assert "PW_PREFIX" not in text
    assert "user:" not in text
    assert "https://example.com/org/repo.git" in text


def test_vector_scp_like_fails_closed() -> None:
    """rust#154 向量③：scp-like token 一律占位符（strict 模式不做 host:path 还原）。"""
    e = _capture_source_error(lambda: resolve_plugin_source({"source": "github", "repo": "git@example.com:org/repo.git"}))
    text = str(e)
    assert "git@example.com" not in text
    assert "example.com" not in text
    assert _REDACTED in text


def test_vector_name_embedded_credentials_redacted() -> None:
    """rust#154 向量④：名称内嵌凭据 URL（顶层 source 字符串）。"""
    e = _capture_source_error(lambda: resolve_plugin_source("x=https://alice:PW_NAME@example.com/repo.git"))
    text = str(e)
    assert "PW_NAME" not in text
    assert "alice:" not in text
    assert "https://example.com/repo.git" in text


def test_vector_local_path_embedded_credentials_redacted() -> None:
    """rust#154 向量⑤：本地路径内嵌凭据 URL（相对路径解析失败的 reason 路径）。"""
    e = _capture_source_error(lambda: resolve_plugin_source("./x=https://alice:PW_LOCAL@example.com/../repo.git"))
    text = str(e)
    assert "PW_LOCAL" not in text
    assert "alice:" not in text
    assert "example.com" in text  # host 保留（可解析 URL 的展示语义）


def test_valid_url_with_bad_ref_redacts_userinfo() -> None:
    """合法 URL + 非法 ref（Issue 现状描述的主路径）：raw dict 内 url 只去凭据。"""
    e = _capture_source_error(
        lambda: resolve_plugin_source({"source": "url", "url": "https://alice:PW_REF@example.com/r.git", "ref": ""})
    )
    text = str(e)
    assert "PW_REF" not in text
    assert "alice:" not in text
    assert "https://example.com/r.git" in text


def test_bare_token_in_url_field_fails_closed() -> None:
    """非 URL 形态的裸 token（如 ``glpat-...``）在 url 字段 → 占位符（strict fail-closed）。"""
    e = _capture_source_error(lambda: resolve_plugin_source({"source": "url", "url": "glpat-SECRET_TOKEN"}))
    text = str(e)
    assert "glpat-SECRET_TOKEN" not in text
    assert _REDACTED in text


def test_reason_embedded_url_is_redacted() -> None:
    """构造期兜底：raise site 若把 URL 拼进 reason，同样被脱敏。"""
    e = SkillSourceError("x", "bad source https://alice:PW_REASON@example.com/r.git")
    assert "PW_REASON" not in str(e)
    assert "https://example.com/r.git" in str(e)


def test_non_string_raw_is_displayed_as_text() -> None:
    """非 str / 非 Mapping 的 raw（int / None）也要能渲染成安全文本。"""
    e = _capture_source_error(lambda: resolve_plugin_source(123))
    assert isinstance(e.raw, str)
    assert "123" in e.raw


def test_self_referencing_raw_does_not_crash_error_construction() -> None:
    """自引用容器（公开 API 可达：``resolve_plugin_source(任意对象)``）不得让构造期脱敏抛异常。"""
    cyclic: dict[str, object] = {"source": "url", "url": "https://alice:PW_CYCLE@example.com/r.git"}
    cyclic["self"] = cyclic
    e = SkillSourceError(cyclic, "boom")
    assert "PW_CYCLE" not in str(e)
    assert "alice:" not in str(e)


def test_object_raw_rendering_stays_valid_json() -> None:
    """对象形态 raw 必须仍是合法紧凑 JSON（脱敏不得把 JSON 标点吞进 URL token）。"""
    e = _capture_source_error(
        lambda: resolve_plugin_source({"source": "url", "url": "https://alice:PW_JSON_RAW@example.com/r.git", "ref": ""})
    )
    assert json.loads(e.raw) == {"source": "url", "url": "https://example.com/r.git", "ref": ""}
    assert "PW_JSON_RAW" not in str(e)


def test_dict_keys_are_redacted_too() -> None:
    """字典**键**同属不可信回显面（键名由配置作者任意指定）——不得绕过脱敏。"""
    e = SkillSourceError({"https://alice:KEYPW@example.com/r.git": "x"}, "boom")
    assert "KEYPW" not in str(e)
    assert "alice:" not in str(e)


def test_source_field_match_is_case_insensitive() -> None:
    """字段判定大小写不敏感：``URL`` 等变体不得落回散文脱敏（裸 token 会漏）。"""
    e = SkillSourceError({"URL": "glpat-SECRET_TOKEN"}, "boom")
    assert "glpat-SECRET_TOKEN" not in str(e)
    assert _REDACTED in e.raw


def test_source_field_marker_propagates_into_containers() -> None:
    """``url`` 值被写成数组（常见手编畸形）时元素仍走 strict（裸 token → 占位符）。"""
    e = _capture_source_error(lambda: resolve_plugin_source({"source": "url", "url": ["glpat-SECRET_TOKEN"]}))
    assert "glpat-SECRET_TOKEN" not in str(e)
    assert _REDACTED in e.raw


def test_control_characters_are_escaped_in_error_text() -> None:
    """控制字符（ESC/BEL/换行）必须转义：否则可注入终端转义序列或伪造日志行。"""
    e = SkillSourceError("\x1b]52;c;ZXZpbA==\x07./x\nERROR forged line", "boom")
    text = str(e)
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "\n" not in text
    assert "\\x1b" in text


def test_nan_payload_falls_back_to_repr() -> None:
    """非有限浮点不是合法 JSON：raw 回退 repr，不产出 ``NaN`` 字面量。"""
    e = SkillSourceError({"k": float("nan")}, "boom")
    assert "NaN" not in e.raw


def test_marketplace_clone_url_vector_is_redacted() -> None:
    """rust#154 向量①的 marketplace 形态：ftp 带凭据 → url 字段整体占位符。"""
    e = _capture_source_error(lambda: marketplace_clone_url({"type": "git", "url": "ftp://cnb:FAKE_TOKEN@example.com/r.git?k=Q"}))
    assert e.raw == '{"type":"git","url":"' + _REDACTED + '"}'
    assert "FAKE_TOKEN" not in str(e)


def test_del_and_c1_characters_keep_object_raw_json_safe() -> None:
    """DEL(0x7f)/C1(0x80-0x9f) 不被 json.dumps 转义：兜底转义必须用 \\uXXXX 才是合法 JSON。"""
    e = SkillSourceError({"k": "a\x7fb\x9bc"}, "boom")
    json.loads(e.raw)  # 不抛 = 合法 JSON
    assert "\\u007f" in e.raw and "\\u009b" in e.raw
    assert "\x7f" not in e.raw and "\x9b" not in e.raw  # 原字符不得裸露（否则又不是合法 JSON）
