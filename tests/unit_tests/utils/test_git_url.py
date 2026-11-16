# -*- coding: utf-8 -*-
# filename: test_git_url.py
# @Time    : 2026/11/16
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
git URL 展示脱敏单一权威单测（python-sdk#261）/ Display redaction for git URLs.

契约来源 / Contract source:
    - rust-sdk#273 维护侧决策（2026-11-16，issuecomment-6908841070）：
      已校验 source 展示面（CLI list/info/add）用 known-source display；scp-like 显示 ``host:path``，
      不保留 ``user@``；去除 userinfo/query/fragment。
    - rust-sdk#154（fail-closed）：错误 / 诊断 / 日志不得回显 git URL 的 userinfo/password/token；
      无法安全呈现 → 同一占位符 ``<redacted-git-source>``。
    - rust-sdk PR #277（``fix/273-scp-like-display``）单测字面量为逐字节对拍值。

**五形态私有向量表未在任何仓库/评论落盘**——本文件按 #273 决策评论 + PR #277 单测字面量重建，
python 与 rust 共用同一张表（非协议一致性向量，故不 vendored）。

五形态 / Five shapes::

    #  input                                                        known 展示                         strict（错误/日志）
    1  git@example.com:org/repo.git                                  example.com:org/repo.git           <redacted-git-source>
    2  ssh://git@example.com/org/repo.git                            ssh://example.com/org/repo.git     ssh://example.com/org/repo.git
    3  https://user:FAKE_TOKEN@example.com/org/repo.git?token=Q#F    https://example.com/org/repo.git   https://example.com/org/repo.git
    4  https://example.com/repo.git?x#y                              https://example.com/repo.git       https://example.com/repo.git
    5  畸形 / 不支持 scheme / 非 ASCII scp-like                        <redacted-git-source>              <redacted-git-source>

与 rust 的一处有意差异 / Deliberate stricter divergence：受支持 scheme 的 opaque 形态
（如 ``ssh:alice:PW@host``，缺 ``//``）rust PR #277 已列为既有待修泄漏点，python 侧按
「非 file scheme 缺 host → 占位符」直接 fail-closed，不复刻该泄漏。
"""

from __future__ import annotations

import pytest

from a2c_smcp.utils.git_url import (
    REDACTED_GIT_SOURCE,
    git_url_for_display,
    known_git_source_for_display,
    redact_git_urls_in_text,
    sanitize_git_text,
)

# (desc, raw, known_expected, strict_expected)
_VECTORS: list[tuple[str, str, str, str]] = [
    (
        "scp-like SSH 源：丢弃 user@",
        "git@example.com:org/repo.git",
        "example.com:org/repo.git",
        REDACTED_GIT_SOURCE,
    ),
    (
        "scp-like 带 query/fragment：按首个 ?/# 截断 path",
        "git@example.com:org/repo.git?token=QUERY#FRAGMENT",
        "example.com:org/repo.git",
        REDACTED_GIT_SOURCE,
    ),
    (
        "scp-like 部署 token 用户名",
        "deploy-token@example.com:org/repo.git",
        "example.com:org/repo.git",
        REDACTED_GIT_SOURCE,
    ),
    (
        "scp-like tilde path（合法 ascii graphic）",
        "git@example.com:~/repo.git",
        "example.com:~/repo.git",
        REDACTED_GIT_SOURCE,
    ),
    (
        "ssh URL：去除 userinfo，保留 scheme/host/path",
        "ssh://git@example.com/org/repo.git",
        "ssh://example.com/org/repo.git",
        "ssh://example.com/org/repo.git",
    ),
    (
        "https 带 userinfo/query/fragment：全去除",
        "https://cnb:FAKE_TOKEN@example.com/org/repo.git?token=QUERY#FRAGMENT",
        "https://example.com/org/repo.git",
        "https://example.com/org/repo.git",
    ),
    (
        "https 带 query/fragment（无凭据）",
        "https://example.com/repo.git?x#y",
        "https://example.com/repo.git",
        "https://example.com/repo.git",
    ),
    (
        "https 无凭据正对照：原样",
        "https://example.com/org/repo.git",
        "https://example.com/org/repo.git",
        "https://example.com/org/repo.git",
    ),
    ("git scheme", "git://host/path", "git://host/path", "git://host/path"),
    ("file scheme（无 host 合法）", "file:///abs/path/repo", "file:///abs/path/repo", "file:///abs/path/repo"),
]

# rust PR #277 `known_source_display_fails_closed_for_untrusted_shapes` 对拍集（两模式均占位符）。
_FAIL_CLOSED: list[str] = [
    "cnb:FAKE_TOKEN@example.com/org/repo.git",
    "cnb:FAKE_TOKEN@https://example.com/org/repo.git",
    "git@example.com:org/repo.git://nested",
    "git@example.com:https:/alice:PW_LEAK@example.com/repo.git",
    "git@example.com:user:pass@host/repo.git",
    "git@example.com:alice%3APW@example.com/repo.git",
    "git@example.com:org%2Frepo.git",
    "git@example.com://evil.example/repo.git",
    "用户@example.com:org/repo.git",
    "用户@例子.公司:org/repo.git",
    "alice@my_host: Permission denied (publickey).",
    "ftp://cnb:FAKE_TOKEN",
    "not a url",
    "",
    # rust 钉死的边界形态 / boundary shapes pinned by rust.
    "@example.com:org/repo.git",
    "git@:org/repo.git",
    "git@example.com:",
    "git@example.com:?token=X",
    "git@example.com:#frag",
]


@pytest.mark.parametrize(("desc", "raw", "known_expected", "strict_expected"), _VECTORS)
def test_five_shape_display_vectors(desc: str, raw: str, known_expected: str, strict_expected: str) -> None:
    """五形态逐条对拍（known 展示面 / strict 错误日志面）。"""
    assert known_git_source_for_display(raw) == known_expected, f"known: {desc}"
    assert git_url_for_display(raw) == strict_expected, f"strict: {desc}"


@pytest.mark.parametrize("raw", _FAIL_CLOSED)
def test_fail_closed_shapes(raw: str) -> None:
    """无法安全解析 / host 缺失 / 畸形 → 两模式同一占位符，不回显原串。"""
    assert git_url_for_display(raw) == REDACTED_GIT_SOURCE
    assert known_git_source_for_display(raw) == REDACTED_GIT_SOURCE


@pytest.mark.parametrize(
    "raw",
    [
        "ssh:alice:PW_LEAK@example.com/repo.git",
        "https:alice:PW_LEAK@example.com/repo.git",
        "git:alice:PW_LEAK@example.com/repo.git",
    ],
)
def test_opaque_supported_scheme_fails_closed(raw: str) -> None:
    """受支持 scheme 但缺 ``//`` 的 opaque 形态：python 侧直接占位符（比 rust 当前实现更严）。"""
    assert git_url_for_display(raw) == REDACTED_GIT_SOURCE
    assert known_git_source_for_display(raw) == REDACTED_GIT_SOURCE


def test_display_never_leaks_credentials_after_redaction() -> None:
    """双保险：任何含凭据的形态，输出里都不得出现凭据字面。"""
    secrets = ("FAKE_TOKEN", "PW_LEAK", "user:pass", "alice%3APW")
    for raw in [v[1] for v in _VECTORS] + _FAIL_CLOSED:
        shown = known_git_source_for_display(raw) + " " + git_url_for_display(raw)
        for secret in secrets:
            assert secret not in shown, f"{raw} -> {shown}"


# ── 文本脱敏（错误 / git stderr / 散文）/ text redaction ──────────────────────
def test_redact_git_urls_in_text_handles_git_stderr() -> None:
    """git stderr 中的凭据 URL：去掉 userinfo，保留 host/path（strict 展示）。"""
    text = (
        "fatal: unable to access 'https://user:FAKE_TOKEN@example.com/org/repo.git/': "
        "Could not resolve host: example.com"
    )
    out = redact_git_urls_in_text(text)
    assert "FAKE_TOKEN" not in out
    assert "https://example.com/org/repo.git/" in out
    assert "Could not resolve host" in out


def test_redact_git_urls_in_text_hides_scp_like_tokens() -> None:
    """散文中的 scp-like token 一律占位符（token 边界 = 空白，与 rust 一致，尾随标点一并吞掉）。"""
    out = redact_git_urls_in_text("could not read from remote git@example.com:org/repo.git, aborting")
    assert out == "could not read from remote <redacted-git-source> aborting"


def test_redact_git_urls_in_text_keeps_email_addresses() -> None:
    """无冒号的邮箱地址不匹配 scp-like，不得误伤。"""
    text = "contact alice@example.com for access; ssh: connect to host example.com port 22: refused"
    assert redact_git_urls_in_text(text) == text


def test_redact_git_urls_in_text_nested_and_multiline() -> None:
    """嵌套 / 多行文本：第二个 ``://`` 出现时整体占位符；file:// 正常展示。"""
    nested = redact_git_urls_in_text("see git@example.com:https:/alice:PW_LEAK@example.com/repo.git now")
    assert "PW_LEAK" not in nested
    assert REDACTED_GIT_SOURCE in nested

    multiline = "line1 https://user:FAKE_TOKEN@example.com/repo.git\nline2 file:///tmp/repo\n"
    out = redact_git_urls_in_text(multiline)
    assert "FAKE_TOKEN" not in out
    assert "https://example.com/repo.git" in out
    assert "file:///tmp/repo" in out


# ── 散文脱敏：rust PR #277 钉死的负向向量 / prose vectors pinned by rust ────────
_TEXT_VECTORS: list[tuple[str, list[str]]] = [
    (
        "x=https://public.example/a=https://user2:PW_TWO@secret.example/repo.git",
        ["user2", "PW_TWO"],
    ),
    ("key=用户@example.com:org/repo.git", ["用户"]),
    ("key=alice@my_host:org/repo.git", ["alice"]),
    ("key=用户@例子.公司:org/repo.git", ["用户", "例子.公司"]),
    ("ssh: alice@my_host: Permission denied (publickey).", ["alice", "my_host"]),
    ("ssh: 用户@例子.公司: Permission denied (publickey).", ["用户", "例子.公司"]),
    ("ssh: alice@my_host's password:", ["alice", "my_host"]),
    ("ssh: 用户@例子.公司's password:", ["用户", "例子.公司"]),
    ("path=/tmp/x=https:/alice:PW_PATH@example.com/repo.git missing", ["alice", "PW_PATH"]),
    ("x=https://example.com/r.git?token=;QUERY_SECRET", ["QUERY_SECRET"]),
    ("x=https://example.com/r.git?token=,COMMA_SECRET", ["COMMA_SECRET"]),
    ("x=https://example.com/r.git?token=QUERY_QUOTE'LEAK_SECRET", ["QUERY_QUOTE", "LEAK_SECRET"]),
    ("x=https://alice:PW_ONE'PW_TWO@example.com/repo.git", ["PW_ONE", "PW_TWO"]),
    # 外层引号：userinfo/query 自身允许引号，按第一个引号截断会泄漏后半段。
    (
        "fatal: unable to access 'https://user:TOK'EN@example.com/repo.git': failed",
        ["user", "TOK", "EN"],
    ),
]


@pytest.mark.parametrize(("text", "secrets"), _TEXT_VECTORS)
def test_redact_git_urls_in_text_never_leaks_secrets(text: str, secrets: list[str]) -> None:
    out = redact_git_urls_in_text(text)
    for secret in secrets:
        assert secret not in out, f"{text!r} -> {out!r}"


def test_redact_git_urls_in_text_keeps_outer_wrapper() -> None:
    """外层单引号闭合符保留，URL 部分严格展示。"""
    out = redact_git_urls_in_text("fatal: unable to access 'https://user:TOK@example.com/r.git': failed")
    assert out == "fatal: unable to access 'https://example.com/r.git': failed"


def test_display_encodes_non_ascii_and_control_chars() -> None:
    """path 按 url crate 序列化 percent-encode；host 走 IDNA（与 rust 对齐，不误伤合法非 ASCII 源）。"""
    assert (
        git_url_for_display("https://cnb.cool/团队/repo.git")
        == "https://cnb.cool/%E5%9B%A2%E9%98%9F/repo.git"
    )
    assert known_git_source_for_display("https://cnb.cool/团队/repo.git") == (
        "https://cnb.cool/%E5%9B%A2%E9%98%9F/repo.git"
    )
    assert git_url_for_display("https://例子.公司/repo") == "https://xn--fsqu00a.xn--55qx5d/repo"
    assert git_url_for_display("https://example.com/中文") == "https://example.com/%E4%B8%AD%E6%96%87"
    assert git_url_for_display("file:///tmp/中文/repo") == "file:///tmp/%E4%B8%AD%E6%96%87/repo"
    ansi_path = git_url_for_display("https://example.com/repo\x1b[1;31m.git")
    assert "\x1b" not in ansi_path
    assert "%1B" in ansi_path


def test_display_rejects_host_injection_chars() -> None:
    """host 中的空格 / 控制字符 → 占位符（不可安全序列化，防终端转义注入）。"""
    for raw in [
        "https://exa mple.com/repo.git",
        "https://\x1b[2Jexample.com/repo.git",
    ]:
        assert git_url_for_display(raw) == REDACTED_GIT_SOURCE, raw
        assert known_git_source_for_display(raw) == REDACTED_GIT_SOURCE, raw


# ── sanitize_git_text：已知 URL 参数 + 缩写端点 / known-arg sanitization ───────
def test_sanitize_git_text_abbreviated_scp_endpoint() -> None:
    """ssh 缩写端点（user@host: 后接空格）在 git stderr 中必须消失（rust 同款用例）。"""
    for source, stderr, secrets in [
        ("alice@my_host:org/repo.git", "alice@my_host: Permission denied (publickey).", ["alice", "my_host"]),
        (
            "用户@例子.公司:org/repo.git",
            "用户@例子.公司: Permission denied (publickey).",
            ["用户", "例子.公司"],
        ),
        ("alice@my_host:org/repo.git", "alice@my_host's password:", ["alice", "my_host"]),
    ]:
        out = sanitize_git_text(stderr, ["clone", "--", source])
        assert REDACTED_GIT_SOURCE in out, out
        for secret in secrets:
            assert secret not in out, f"{out!r} still contains {secret!r}"


def test_sanitize_git_text_argv_and_sub_delims() -> None:
    """argv 中带 RFC 3986 sub-delims 的凭据 URL（逗号/分号）整体脱敏，不留残段。"""
    raw = "https://user:123,SECRET_PASS@example.com/repo.git"
    out = sanitize_git_text(f"git clone -- {raw} failed", ["clone", "--", raw])
    assert "SECRET_PASS" not in out
    assert "user:123" not in out
    assert "https://example.com/repo.git" in out

    raw2 = "https://example.com/r.git?token=;QUERY_SECRET"
    out2 = sanitize_git_text(f"git clone -- {raw2} failed", ["clone", "--", raw2])
    assert "QUERY_SECRET" not in out2
    assert "https://example.com/r.git" in out2
