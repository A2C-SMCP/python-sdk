# -*- coding: utf-8 -*-
# filename: test_marketplace_identity.py
# @Time    : 2026/11/18
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Marketplace 注册身份规则单元测试（#260）/ Marketplace identity rules unit tests.

覆盖：清单名派生与校验、同源判定（fail-closed）、同源旧名索引、意图引用、注册提交点判定矩阵
（同名不同源 / 同源不同名 / 新注册名不符 / legacy grandfather / baseline 改名拒绝）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from a2c_smcp.computer.settings.scope import user_settings_path
from a2c_smcp.computer.skills.marketplace_identity import (
    MANIFEST_NAME_BASELINE_FIELD,
    MarketplaceIdentityError,
    MarketplaceNameChangeError,
    intent_reference_pids,
    manifest_marketplace_name,
    read_manifest_marketplace_name,
    registered_name_for_source,
    same_source,
)
from a2c_smcp.computer.skills.staging import registration_violation

_GIT = {"type": "git", "url": "https://example.com/org/repo.git"}


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "A2C_SKILL_HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_settings_with_references(env: dict[str, str], pids: list[str]) -> None:
    path = user_settings_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installedPlugins": pids, "enabledPlugins": {pid: True for pid in pids}}), encoding="utf-8")


# ── #296：flag / policy / cwd 轴（五层视图）/ flag & policy layers ─────────────
def _empty_user_env(tmp_path: Path) -> dict[str, str]:
    """隔离 user 层：只给 XDG_CONFIG_HOME，不继承 ambient（避免机器级 policy 干扰）。"""
    return {"XDG_CONFIG_HOME": str(tmp_path / "empty-cfg")}


def _write_user_settings(env: dict[str, str], payload: dict[str, object]) -> None:
    path = user_settings_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_ambient_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """#296：守卫默认按 ``env`` 现解析 policy → 单测固定为空，避免宿主机 MDM policy 让断言漂移。

    测 policy 层的用例在用例体内再次 monkeypatch 覆盖（后写生效）。
    """
    from a2c_smcp.computer.settings import policy as policy_mod

    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_kw: {})


# ── 清单名 / manifest name ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({"name": "acme"}, "acme"),
        ({"name": "  acme-skills  "}, "acme-skills"),  # 两侧空白 strip 后合法
    ],
)
def test_manifest_marketplace_name_accepts_strict_kebab(manifest: dict[str, object], expected: str) -> None:
    assert manifest_marketplace_name(manifest) == expected


@pytest.mark.parametrize(
    "manifest",
    [
        {},  # 缺失
        {"name": ""},
        {"name": "   "},
        {"name": 42},
        {"name": "Acme"},  # 大写
        {"name": "acme_skills"},  # 下划线
        {"name": "-acme"},  # 首 -
        {"name": "acme--skills"},  # 连续 --
        {"name": "a" * 65},  # 超长
    ],
)
def test_manifest_marketplace_name_rejects_invalid(manifest: dict[str, object]) -> None:
    with pytest.raises(MarketplaceIdentityError) as ei:
        manifest_marketplace_name(manifest)
    assert ei.value.reason == "manifest-name-invalid"
    assert ei.value.error_code == "validation"


def test_manifest_name_error_never_echoes_raw_value() -> None:
    """不可信清单：非法 name 的原值不得进入错误文本（可能内嵌凭据样文本，#263 同口径）。"""
    with pytest.raises(MarketplaceIdentityError) as ei:
        manifest_marketplace_name({"name": "alice:PW_LEAK@example.com"})
    assert "PW_LEAK" not in str(ei.value)
    assert "alice" not in str(ei.value)


def test_read_manifest_name_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(MarketplaceIdentityError) as ei:
        read_manifest_marketplace_name(tmp_path)
    assert ei.value.reason == "manifest-name-invalid"


def test_read_manifest_name_reads_top_level(tmp_path: Path) -> None:
    manifest_dir = tmp_path / ".tfrobot-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "marketplace.json").write_text(json.dumps({"name": "acme", "plugins": []}), encoding="utf-8")
    assert read_manifest_marketplace_name(tmp_path) == "acme"


# ── 同源判定 / same-source predicate（fail-closed）────────────────────────────
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (_GIT, dict(_GIT), True),
        ({"type": "git", "url": " https://example.com/org/repo.git "}, _GIT, True),  # 仅去首尾空白
        # ssh / https / .git 后缀**不**归一：无法确定即视为不同源（§5.14 fail-closed）
        ({"type": "git", "url": "git@example.com:org/repo.git"}, _GIT, False),
        ({"type": "git", "url": "https://example.com/org/repo"}, _GIT, False),
        ({"type": "hg", "url": _GIT["url"]}, _GIT, False),
        # 未识别的额外字段也参与比较：出现差异即视为不同源（漏看 = 静默换源）
        ({"type": "git", "url": _GIT["url"], "ref": "main"}, _GIT, False),
        ({"type": "git"}, _GIT, False),
        ({"type": "git", "url": ""}, _GIT, False),
        (None, _GIT, False),
        ("git", _GIT, False),
    ],
)
def test_same_source_matrix(left: object, right: object, expected: bool) -> None:
    assert same_source(left, right) is expected


def test_registered_name_for_source() -> None:
    known = {
        "acme": {"source": dict(_GIT), "installLocation": "/x"},
        "other": {"source": {"type": "git", "url": "https://example.com/o"}, "installLocation": "/y"},
    }
    assert registered_name_for_source(known, {"type": "git", "url": _GIT["url"]}) == "acme"
    assert registered_name_for_source(known, {"type": "git", "url": "https://example.com/z"}) is None


# ── 意图引用 / intent references ─────────────────────────────────────────────
def test_intent_reference_pids_scans_installed_and_enabled(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write_settings_with_references(env, ["audit@acme", "fmt@acme", "audit@other"])
    assert intent_reference_pids("acme", env=env, policy_settings={}) == ["audit@acme", "fmt@acme"]
    assert intent_reference_pids("nobody", env=env, policy_settings={}) == []


def test_intent_reference_pids_ignores_bare_name_collisions(tmp_path: Path) -> None:
    """``@acme`` 后缀匹配，不得被 ``acme-extra`` / 裸同名 plugin 误伤。"""
    env = _env(tmp_path)
    _write_settings_with_references(env, ["audit@acme-extra", "audit"])
    assert intent_reference_pids("acme", env=env, policy_settings={}) == []


def test_intent_reference_pids_includes_flag_layer(tmp_path: Path) -> None:
    """#296：``--settings`` flag 层的 ``<plugin>@<name>`` 引用必须进入视图；不传该层则不可见。"""
    env = _empty_user_env(tmp_path)
    flag = tmp_path / "flag.json"
    _write_json(flag, {"enabledPlugins": {"audit@acme": False}})

    assert intent_reference_pids("acme", env=env, flag_settings_path=flag, policy_settings={}) == ["audit@acme"]
    # 正对照：不传 flag 层 → 保持 #296 前行为（不可见）
    assert intent_reference_pids("acme", env=env, policy_settings={}) == []


@pytest.mark.parametrize("enabled_value", [True, False])
def test_intent_reference_pids_policy_layer_true_and_false_both_count(
    tmp_path: Path, enabled_value: bool
) -> None:
    """#296：policy 层可显式注入；``enabledPlugins`` 的 true / false 都算意图引用（协议 §5.14）。"""
    env = _empty_user_env(tmp_path)
    policy = {"enabledPlugins": {"audit@acme": enabled_value}}
    assert intent_reference_pids("acme", env=env, policy_settings=policy) == ["audit@acme"]


def test_intent_reference_pids_union_dedup_flag_and_policy(tmp_path: Path) -> None:
    """#296：flag ∪ policy 并集去重，同一 pid 不重复计数。

    低层引用**只放 ``enabledPlugins``**：若层合并退化为「高层整层覆盖」，user 层的 audit 会消失；
    若放进 ``installedPlugins``，数组拼接会掩盖该回归（#296 变异矩阵 M3）。
    """
    env = _empty_user_env(tmp_path)
    _write_user_settings(env, {"enabledPlugins": {"audit@acme": False}})
    flag = tmp_path / "flag.json"
    _write_json(flag, {"enabledPlugins": {"audit@acme": True, "fmt@acme": False}})
    policy = {"enabledPlugins": {"lint@acme": False}}

    assert intent_reference_pids(
        "acme", env=env, flag_settings_path=flag, policy_settings=policy
    ) == ["audit@acme", "fmt@acme", "lint@acme"]


def test_intent_reference_pids_unions_installed_and_enabled(tmp_path: Path) -> None:
    """#296 变异锁：``installedPlugins`` 与 ``enabledPlugins`` 两侧都要扫，不能只取一侧。"""
    env = _empty_user_env(tmp_path)
    _write_user_settings(env, {"installedPlugins": ["audit@acme"], "enabledPlugins": {"fmt@acme": False}})
    assert intent_reference_pids("acme", env=env, policy_settings={}) == ["audit@acme", "fmt@acme"]


def test_intent_reference_pids_project_and_local_layers_anchor_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#296：cwd 轴——project/local 真实文件经 chdir 锚定；显式 ``cwd`` 可指向别处（正对照）。"""
    env = _empty_user_env(tmp_path)
    project = tmp_path / "proj"
    _write_json(project / ".tfrobot" / "settings.json", {"installedPlugins": ["proj@acme"]})
    _write_json(project / ".tfrobot" / "settings.local.json", {"enabledPlugins": {"local@acme": False}})
    monkeypatch.chdir(project)

    assert intent_reference_pids("acme", env=env, policy_settings={}) == ["local@acme", "proj@acme"]

    other = tmp_path / "other"
    other.mkdir()
    assert intent_reference_pids("acme", env=env, cwd=other, policy_settings={}) == []


def test_intent_reference_pids_none_resolves_ambient_policy_and_explicit_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#296：``policy_settings=None`` → 按 ``env`` 现解析 ambient policy（对齐 rust#300，防库层绕过）；
    显式 mapping（含 ``{}`` = 无 policy）覆盖，供 hermetic 测试。
    """
    from a2c_smcp.computer.settings import policy as policy_mod

    monkeypatch.setattr(
        policy_mod, "resolve_policy_settings", lambda **_kw: {"enabledPlugins": {"audit@acme": True}}
    )
    env = _empty_user_env(tmp_path)
    assert intent_reference_pids("acme", env=env) == ["audit@acme"]
    assert intent_reference_pids("acme", env=env, policy_settings={}) == []


# ── 提交点判定矩阵 / registration commit predicate ───────────────────────────
def _violation(
    name: str,
    source: dict[str, str],
    prior: dict[str, object] | None,
    known: dict[str, object],
    observed: str,
    env: dict[str, str] | None = None,
) -> MarketplaceIdentityError | None:
    # 显式 ``{}``：这些矩阵用例不测 policy 层，避免机器级 ambient policy 让断言漂移（#296）。
    return registration_violation(name, source, prior, known, observed, env=env, policy_settings={})


def test_no_violation_for_fresh_registration() -> None:
    assert _violation("acme", _GIT, None, {}, "acme") is None


def test_source_conflict_when_source_registered_under_other_name() -> None:
    known = {"old": {"source": dict(_GIT), "installLocation": "/x"}}
    v = _violation("acme", _GIT, None, known, "acme")
    assert isinstance(v, MarketplaceIdentityError) and v.reason == "source-conflict"
    assert v.details["existingName"] == "old"
    assert "marketplace remove old" in v.details["remedy"]


def test_expected_name_mismatch_for_new_registration() -> None:
    """D1：新注册必须以清单名为名（声明式 key 与清单名不符同样拒绝）。"""
    v = _violation("acme", _GIT, None, {}, "acme-skills")
    assert isinstance(v, MarketplaceIdentityError) and v.reason == "expected-name-mismatch"
    assert v.details == {"manifestName": "acme-skills", "expectedName": "acme"}


def test_dangling_references_rejected_for_new_registration(tmp_path: Path) -> None:
    """协议 §5.14「remove 后同名不同源 re-add 且仍有 ``<plugin>@<name>`` 引用」→ MUST 拒绝。"""
    env = _env(tmp_path)
    _write_settings_with_references(env, ["audit@acme"])
    v = _violation("acme", _GIT, None, {}, "acme", env=env)
    assert isinstance(v, MarketplaceIdentityError) and v.reason == "dangling-references"
    assert v.details["references"] == ["audit@acme"]


def test_registration_violation_sees_flag_and_policy_references(tmp_path: Path) -> None:
    """#296：提交点守卫的引用视图必须含 flag / policy 层（两处守卫共用同一五层视图）。"""
    env = _empty_user_env(tmp_path)
    flag = tmp_path / "flag.json"
    _write_json(flag, {"enabledPlugins": {"audit@acme": False}})
    policy = {"enabledPlugins": {"fmt@acme": True}}

    v = registration_violation(
        "acme", _GIT, None, {}, "acme", env=env, flag_settings_path=flag, policy_settings=policy
    )
    assert isinstance(v, MarketplaceIdentityError) and v.reason == "dangling-references"
    assert v.details == {"name": "acme", "references": ["audit@acme", "fmt@acme"], "referenceCount": 2}

    # 正对照：不传 flag / policy 层 → 无违约（沿用 #296 前行为）
    assert registration_violation("acme", _GIT, None, {}, "acme", env=env, policy_settings={}) is None


def test_name_conflict_for_same_name_different_source() -> None:
    prior = {"source": {"type": "git", "url": "https://example.com/other.git"}, "installLocation": "/x"}
    v = _violation("acme", _GIT, prior, {"acme": prior}, "acme")
    assert isinstance(v, MarketplaceIdentityError) and v.reason == "name-conflict"
    assert v.details["name"] == "acme"
    assert "existingSource" in v.details


def test_name_conflict_does_not_leak_credentials() -> None:
    prior = {
        "source": {"type": "git", "url": "https://user:FAKE_TOKEN@example.com/org/repo.git?token=Q"},
        "installLocation": "/x",
    }
    v = _violation("acme", _GIT, prior, {"acme": prior}, "acme")
    assert isinstance(v, MarketplaceIdentityError)
    assert "FAKE_TOKEN" not in str(v)
    assert "FAKE_TOKEN" not in json.dumps(v.details)


def test_legacy_without_baseline_is_grandfathered() -> None:
    """legacy 注册（无 baseline）：首次观测不拒——由提交点补写 baseline、保留旧注册名。"""
    prior = {"source": dict(_GIT), "installLocation": "/x"}
    assert _violation("acme", _GIT, prior, {"acme": prior}, "acme-renamed") is None


def test_baseline_rename_is_rejected() -> None:
    prior = {"source": dict(_GIT), "installLocation": "/x", MANIFEST_NAME_BASELINE_FIELD: "acme"}
    v = _violation("acme", _GIT, prior, {"acme": prior}, "acme-renamed")
    assert isinstance(v, MarketplaceNameChangeError)
    assert v.reason == "manifest-name-changed"
    assert v.details["baselineManifestName"] == "acme"
    assert v.details["manifestName"] == "acme-renamed"


def test_baseline_equal_is_allowed() -> None:
    prior = {"source": dict(_GIT), "installLocation": "/x", MANIFEST_NAME_BASELINE_FIELD: "acme"}
    assert _violation("acme", _GIT, prior, {"acme": prior}, "acme") is None


def test_name_conflict_redacts_credential_like_path_in_source() -> None:
    """
    独立审查 🔴 回归锁：``existingSource`` 走**诊断面**判据——path 段含凭据样文本时必须占位符
    （authority 段 userinfo 之外的第二条泄漏面）。变异 = 改回 `known_git_source_for_display` → 转红。
    """
    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    prior = {
        "source": {"type": "git", "url": "https://host/team/alice:PW_LEAK@example.com/repo.git"},
        "installLocation": "/x",
    }
    v = _violation("acme", _GIT, prior, {"acme": prior}, "acme")

    assert isinstance(v, MarketplaceIdentityError)
    assert v.details["existingSource"] == REDACTED_GIT_SOURCE
    assert "PW_LEAK" not in str(v) and "alice" not in str(v)
    assert "PW_LEAK" not in json.dumps(v.details)


def test_source_conflict_redacts_ledger_key_in_details() -> None:
    """账本键含凭据样文本时，``existingName`` 与 remedy 也必须脱敏（与文案同一口径）。"""
    known = {"https://user:TOKEN@host/x": {"source": dict(_GIT), "installLocation": "/x"}}
    v = _violation("acme", _GIT, None, known, "acme")

    assert isinstance(v, MarketplaceIdentityError)
    assert v.reason == "source-conflict"
    assert "TOKEN" not in json.dumps(v.details)
    assert "TOKEN" not in str(v)


def test_source_conflict_redacts_credential_like_path_in_ledger_key() -> None:
    """
    终审 🔴 回归锁：账本键本身是含 **path 段凭据** 的 URL 时，`source-conflict` 的 message / `existingName`
    / `remedy` 必须全部遮蔽（散文脱敏只去 userinfo、保留 path，挡不住这一面）。
    """
    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    key = "https://host/team/alice:PW_LEAK@example.com/repo.git"
    known = {key: {"source": dict(_GIT), "installLocation": "/x"}}

    v = _violation("acme", _GIT, None, known, "acme")

    assert isinstance(v, MarketplaceIdentityError)
    assert v.reason == "source-conflict"
    assert v.details["existingName"] == REDACTED_GIT_SOURCE
    assert "marketplace remove" in v.details["remedy"]
    assert "PW_LEAK" not in json.dumps(v.details)
    assert "PW_LEAK" not in str(v) and "alice" not in str(v)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("acme-skills", "acme-skills"),  # 正常账本键原样
        ("alice:PW@example.com", "<redacted-git-source>"),  # 非 URL 的 userinfo 形态
        ("x=https:/host/alice:PW@example.com/repo.git", "<redacted-git-source>"),  # URL 残形
        ("https://host/team/alice:PW_LEAK@example.com/repo.git", "<redacted-git-source>"),  # path 段凭据
        # #281 组合锁：非 URL 键的 `?query` 尾部由 segment 漏斗截断（散文漏斗会原样保留 → 回退即转红）
        ("foo?token=SECRET", "foo<redacted-source>"),
    ],
)
def test_redact_ledger_key_matrix(key: str, expected: str) -> None:
    """终审 🟡5：账本键脱敏的 URL / 非 URL / URL 残形三支都要有向量（非 URL 支此前零覆盖）。"""
    from a2c_smcp.computer.skills.marketplace_identity import redact_ledger_key

    assert redact_ledger_key(key) == expected


def test_rename_rejection_redacts_credential_like_baseline() -> None:
    """终审 🟡2：`baselineManifestName` 来自账本（可手编）→ 回显前脱敏；`manifestName` 是清单名可原样。"""
    from a2c_smcp.utils.git_url import REDACTED_GIT_SOURCE

    prior = {
        "source": dict(_GIT),
        "installLocation": "/x",
        MANIFEST_NAME_BASELINE_FIELD: "https://host/team/alice:PW_LEAK@example.com/repo.git",
    }
    v = _violation("acme", _GIT, prior, {"acme": prior}, "acme-renamed")

    assert isinstance(v, MarketplaceNameChangeError)
    assert v.details["baselineManifestName"] == REDACTED_GIT_SOURCE
    assert "PW_LEAK" not in json.dumps(v.details)
    assert "PW_LEAK" not in str(v)
