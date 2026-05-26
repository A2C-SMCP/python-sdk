# -*- coding: utf-8 -*-
# filename: sources.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL marketplace source 解析：plugin source 5 类 + 简写糖归一化（v0.2.1）
SKILL marketplace source resolution: plugin source (5 kinds) + shorthand normalization (v0.2.1).

协议依据 / Protocol: tfrobot-marketplace docs/marketplace/protocol-v1.md §5（Plugin source 字段规范，
                      5 类 disjoint union + github/cnb 简写糖 + git-subdir sparse clone）、§3.2（``metadata.
                      pluginRoot`` 使裸名 ``"x"`` 等价 ``"./<pluginRoot>/x"``）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §3.2 / §5.3。

本模块**纯解析、零 I/O、零 git**：把 ``marketplace.json`` 中每个 plugin 条目的 ``source`` 字段
（``str`` 相对路径 / 5 类 source 对象之一）归一化为可供 staging（#61）执行的克隆/定位规格：
- :class:`LocalPluginSource` —— 相对路径（含裸名经 ``pluginRoot`` 解析），指向 **marketplace clone 内**子目录；
- :class:`GitCloneSpec` —— 需独立 ``git clone`` 的源（``url`` / ``github`` / ``cnb`` / ``git-subdir``），
  ``github`` / ``cnb`` 的 ``owner/repo`` 简写糖归一化为 ``https://<host>/<repo>.git``，``git-subdir`` 额外
  携带 ``subdir`` 供 sparse clone（plugin 根 = ``<clone>/<subdir>``）。
This module is pure parsing (no I/O, no git): it normalizes a plugin entry's ``source`` field into a
:class:`LocalPluginSource` (within the marketplace clone) or a :class:`GitCloneSpec` (needs its own
clone). ``github`` / ``cnb`` shorthand is normalized to an ``https://`` URL; ``git-subdir`` carries a
``subdir`` for sparse checkout.

Marketplace source（用户添加 marketplace 的 ``{type:"git", url}``，与 plugin source **两套独立 schema**，
disjoint union，协议 §5.3）的克隆 url 提取见 :func:`marketplace_clone_url`。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from a2c_smcp.computer.settings.schema import is_valid_git_url
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# 简写糖归一化目标 host / Shorthand normalization hosts（协议 §5.2.1 / §5.2.4）。
GITHUB_HOST = "github.com"
CNB_HOST = "cnb.cool"

# 裸名经 metadata.pluginRoot 解析的默认前缀（协议 §3.2）/ Default pluginRoot for bare-name plugin sources.
DEFAULT_PLUGIN_ROOT = "./plugins"

# ``owner/repo`` 简写糖：恰两段，段内无空白 / 无 ``/``（协议 §5.2 ``owner/repo`` / ``group/project``）。
_REPO_SHORTHAND_RE = re.compile(r"^[^\s/]+/[^\s/]+$")

# 完整 40 字符 hex commit SHA（协议 §5.2 ``sha`` 字段）/ Full 40-char hex commit SHA.
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class SkillSourceError(ValueError):
    """
    plugin / marketplace source 形态非法 / Malformed plugin or marketplace source.

    由 staging（:func:`~a2c_smcp.computer.skills.staging.stage_marketplace_skills`）捕获并按失败降级
    铁律处理（记 ERROR、该源不入 Registry、不阻断其余、不向 Agent 硬报错）。本异常**不**自带协议码
    （marketplace source 解析失败属本地配置问题，非协议错误码语义）。
    Caught by staging and handled per the failure-isolation rule (logged ERROR, not registered, does
    not block other sources). Carries no protocol code.
    """

    def __init__(self, raw: object, reason: str) -> None:
        self.raw = raw
        self.reason = reason
        super().__init__(f"invalid skill source {raw!r}: {reason}")


@dataclass(frozen=True, slots=True)
class GitCloneSpec:
    """
    需独立 ``git clone`` 的 plugin 源规格 / Spec for a plugin source requiring its own ``git clone``。

    :ivar url: 归一化后的 git URL（``github`` / ``cnb`` 简写糖已展开）/ normalized git URL.
    :ivar ref: 分支或 tag（可选）/ branch or tag (optional).
    :ivar sha: 完整 40 字符 commit SHA（可选，精确锁版本）/ full 40-char commit SHA (optional, version lock).
    :ivar subdir: 非 ``None`` → ``git-subdir`` sparse clone，plugin 根 = ``<clone>/<subdir>``（仓库相对，
        已校验无 ``..`` / 非绝对）/ when set, sparse-clone subdir; plugin root = ``<clone>/<subdir>``.
    """

    url: str
    ref: str | None = None
    sha: str | None = None
    subdir: str | None = None


@dataclass(frozen=True, slots=True)
class LocalPluginSource:
    """
    相对路径 plugin 源 / Relative-path plugin source（指向 **marketplace clone 内**子目录，无需独立 clone）。

    :ivar rel_path: 归一化的仓库相对路径（去前导 ``./``、POSIX 分隔、无 ``..``）；裸名形态已由
        :data:`DEFAULT_PLUGIN_ROOT` / ``metadata.pluginRoot`` 前缀展开（协议 §3.2）。
        Normalized repo-relative path (leading ``./`` stripped, POSIX-separated, no ``..``); bare-name
        form is already expanded under ``pluginRoot``.
    """

    rel_path: str


# plugin source 解析结果（disjoint union，协议 §5.3）/ Resolved plugin source (disjoint union).
ResolvedPluginSource = GitCloneSpec | LocalPluginSource


# ── 内部辅助 / internal helpers ──────────────────────────────────────────────
def _require_str(d: Mapping[str, Any], key: str) -> str:
    """取必填字符串字段（缺失 / 非字符串 / 空 → 抛）/ Require a non-empty string field。"""
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise SkillSourceError(dict(d), f"required field {key!r} missing or not a non-empty string")
    return v.strip()


def _extract_ref_sha(d: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """提取可选 ``ref`` / ``sha``（``sha`` 须为完整 40 字符 hex）/ Extract optional ``ref`` / ``sha``。"""
    ref = d.get("ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise SkillSourceError(dict(d), "'ref' must be a non-empty string when present")
    sha = d.get("sha")
    if sha is not None and (not isinstance(sha, str) or _FULL_SHA_RE.match(sha.strip()) is None):
        raise SkillSourceError(dict(d), "'sha' must be a full 40-char hex commit SHA when present")
    return (
        ref.strip() if isinstance(ref, str) else None,
        sha.strip().lower() if isinstance(sha, str) else None,
    )


def _is_repo_shorthand(s: str) -> bool:
    """是否为 ``owner/repo`` 两段简写糖（非完整 URL / 非 scp-like）/ Whether ``s`` is an ``owner/repo`` shorthand。"""
    return "://" not in s and "@" not in s and _REPO_SHORTHAND_RE.match(s) is not None


def normalize_repo_shorthand(repo: str, host: str) -> str:
    """
    ``owner/repo`` 简写糖归一化为 ``https://<host>/<repo>.git`` / Normalize ``owner/repo`` shorthand。

    用于 ``github``（host=:data:`GITHUB_HOST`）与 ``cnb``（host=:data:`CNB_HOST`）两类（协议 §5.2.1 /
    §5.2.4，归一化逻辑同构）。``repo`` 非 ``owner/repo`` 两段简写（如携带 scheme / scp-like ``@``）→ 抛。
    Used by ``github`` / ``cnb`` (same normalization). Raises if ``repo`` is not a two-segment shorthand.
    """
    if not isinstance(repo, str) or not _is_repo_shorthand(repo.strip()):
        raise SkillSourceError(repo, f"'repo' must be an '<owner>/<repo>' shorthand, not a full URL (got {repo!r})")
    r = repo.strip().removesuffix(".git")
    return f"https://{host}/{r}.git"


def _norm_rel_segments(rel: str, *, raw: object) -> str:
    """归一化仓库相对路径：POSIX 分隔、去 ``.``、禁 ``..`` / 绝对、非空 / Normalize a repo-relative path。"""
    parts = [seg for seg in rel.replace("\\", "/").split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise SkillSourceError(raw, "path must not contain '..'")
    if not parts:
        raise SkillSourceError(raw, "path is empty / resolves to repo root")
    return "/".join(parts)


def _resolve_relative(raw: str, plugin_root: str) -> LocalPluginSource:
    """
    解析相对路径 plugin 源（含裸名经 ``pluginRoot`` 解析）/ Resolve a relative-path (or bare-name) source。

    - ``"./x/y"`` → 仓库根相对 ``x/y``（协议 §5.1：必须以 ``./`` 开头、禁 ``..``）；
    - 裸名 ``"x"``（无 ``/``、无前导 ``./``）→ ``<pluginRoot>/x``（协议 §3.2 ``metadata.pluginRoot``）。
    """
    s = raw.strip()
    if s.startswith("./"):
        rel = s[2:]
    elif "/" not in s and s not in ("", ".", ".."):
        # 裸名：经 metadata.pluginRoot 前缀展开（pluginRoot 自身可带前导 ./）。
        rel = f"{_norm_rel_segments(plugin_root, raw=raw)}/{s}"
    else:
        raise SkillSourceError(raw, "relative plugin source must start with './' or be a bare plugin name")
    return LocalPluginSource(rel_path=_norm_rel_segments(rel, raw=raw))


# ── 公开 API / public API ────────────────────────────────────────────────────
def resolve_plugin_source(raw: object, *, plugin_root: str = DEFAULT_PLUGIN_ROOT) -> ResolvedPluginSource:
    """
    解析 ``marketplace.json`` plugin 条目的 ``source``（5 类 disjoint union）/ Resolve a plugin entry ``source``。

    协议 §5.1 五类：相对路径（``str``）/ ``url`` / ``github`` / ``git-subdir`` / ``cnb``。对象形态靠内层
    ``source`` 字段作 union discriminator（协议 §5.3，**非**递归嵌套）。
    Five kinds per §5.1: relative path (``str``) / ``url`` / ``github`` / ``git-subdir`` / ``cnb``; object
    forms are discriminated by the inner ``source`` field (§5.3, not recursive nesting).

    :param raw: plugin 条目的 ``source`` 值（``str`` 相对路径或 source 对象）。
    :param plugin_root: 裸名相对路径的 ``pluginRoot`` 前缀（来自 ``metadata.pluginRoot``，默认
        :data:`DEFAULT_PLUGIN_ROOT`）。
    :raises SkillSourceError: 形态非法 / 缺必填字段 / discriminator 未知。
    """
    if isinstance(raw, str):
        return _resolve_relative(raw, plugin_root)
    if not isinstance(raw, Mapping):
        raise SkillSourceError(raw, "plugin source must be a relative-path string or a source object")

    kind = raw.get("source")
    if kind == "url":
        url = _require_str(raw, "url")
        if not is_valid_git_url(url):
            raise SkillSourceError(dict(raw), f"'url' is not a well-formed git url (got {url!r})")
        ref, sha = _extract_ref_sha(raw)
        return GitCloneSpec(url=url, ref=ref, sha=sha)
    if kind == "github":
        url = normalize_repo_shorthand(_require_str(raw, "repo"), GITHUB_HOST)
        ref, sha = _extract_ref_sha(raw)
        return GitCloneSpec(url=url, ref=ref, sha=sha)
    if kind == "cnb":
        url = normalize_repo_shorthand(_require_str(raw, "repo"), CNB_HOST)
        ref, sha = _extract_ref_sha(raw)
        return GitCloneSpec(url=url, ref=ref, sha=sha)
    if kind == "git-subdir":
        raw_url = _require_str(raw, "url")
        # url 亦接受 GitHub 简写 owner/repo 或 SSH URL（协议 §5.2.3）。
        url = raw_url if is_valid_git_url(raw_url) else normalize_repo_shorthand(raw_url, GITHUB_HOST)
        subdir = _norm_rel_segments(_require_str(raw, "path"), raw=dict(raw))
        ref, sha = _extract_ref_sha(raw)
        return GitCloneSpec(url=url, ref=ref, sha=sha, subdir=subdir)

    raise SkillSourceError(dict(raw), f"unknown plugin source discriminator {kind!r} (expect url|github|git-subdir|cnb)")


def marketplace_clone_url(source: object) -> str:
    """
    从 marketplace source（``{type:"git", url}``）取克隆 URL / Extract the clone URL from a marketplace source。

    SDK settings 的 marketplace source 仅 git url（协议 §5.3：marketplace 侧支持 url/github/git/cnb，本 SDK
    的 :class:`~a2c_smcp.computer.settings.schema.GitSource` 收敛为 ``{type:"git", url}``；不支持 ``ref``/
    ``sha``，与 plugin source 的 5 类**两套独立 schema**）。形态非法 → 抛 :class:`SkillSourceError`。
    The SDK marketplace source is a plain git url (``{type:"git", url}``); no ``ref``/``sha`` and disjoint
    from the plugin-source schema. Raises on malformed input.
    """
    if not isinstance(source, Mapping):
        raise SkillSourceError(source, "marketplace source must be an object")
    stype = source.get("type")
    if stype != "git":
        raise SkillSourceError(dict(source), f"marketplace source 'type' must be 'git' (got {stype!r})")
    url = source.get("url")
    if not isinstance(url, str) or not is_valid_git_url(url):
        raise SkillSourceError(dict(source), f"marketplace source 'url' is not a well-formed git url (got {url!r})")
    return url
