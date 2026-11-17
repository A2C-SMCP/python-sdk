# -*- coding: utf-8 -*-
# filename: test_validator.py
# @Time    : 2026/08/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``skills.validator`` 单元测试（#193）/ Validator unit tests.

设计依据 / Design: python-sdk #193（用户裁决：全量收集 + 远程跳过 warning + 契约面含 SKILL 深检）；
协议依据 tfrobot-marketplace protocol.md §3/§4/§5/§6 + loading-behavior.md §1/§2。

测试意图 / Test intentions:
- 双模式识别（marketplace.json 在 → marketplace；plugin 形态 → plugin；都不像 → not-a-target）；
- 静态契约补位（§3.1/§4.1/§6.1 必填、类型、kebab 命名、owner.name）；
- 静默吞错面显式报出（plugins[] 畸形条目、损坏 plugin.json、同名 entry）；
- severity 对齐装载姿态（plugin.json 缺失=warning；strict 冲突=path-missing=error）；
- collect-all：单文件语法错不终止 run（好 plugin 的文件仍进 checked）；
- SKILL 深检四诊断 + 容器覆写（缺失→error、越界→error、重名→error、无容器→warning）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.skills.validator import (
    CODE_DUPLICATE_PLUGIN_NAME,
    CODE_DUPLICATE_SKILL,
    CODE_INVALID_MCP_SERVER,
    CODE_INVALID_NAME,
    CODE_INVALID_SOURCE,
    CODE_INVALID_TYPE,
    CODE_JSON_SYNTAX,
    CODE_MISSING_FIELD,
    CODE_NO_SKILL_CONTAINER,
    CODE_NOT_A_TARGET,
    CODE_PATH_ESCAPE,
    CODE_PATH_MISSING,
    CODE_PLUGIN_MANIFEST_MISSING,
    CODE_REMOTE_SOURCE_SKIPPED,
    CODE_SKILL_DESC_MISSING,
    CODE_SKILL_MD_MISSING,
    CODE_SKILL_MD_UNREADABLE,
    CODE_SKILL_NAME_INVALID,
    CODE_STRICT_CONFLICT,
    CODE_UNREADABLE,
    validate_path,
)


# ── 夹具构造 / fixture builders ────────────────────────────────────────────────
def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _wj(path: Path, data: Any) -> Path:
    return _w(path, json.dumps(data, ensure_ascii=False, indent=2))


def _skill_md(desc: str = "does the thing") -> str:
    return f"---\nname: demo\ndescription: {desc}\n---\n\nbody\n"


def _plugin(
    root: Path,
    name: str = "data-toolkit",
    *,
    with_pjson: bool = True,
    pjson: dict[str, Any] | None = None,
    skills: tuple[str, ...] = ("etl",),
    servers: tuple[str, ...] = ("etl",),
) -> Path:
    """建一个最小合法 plugin。``with_pjson=True`` 且 ``pjson=None`` → 写默认合法 plugin.json；``pjson`` 非
    ``None`` → 写给定内容；``with_pjson=False`` → 不写（plugin.json 缺失用例）。"""
    p = root / "plugins" / name
    if with_pjson:
        manifest = pjson if pjson is not None else {"name": name, "description": f"{name} plugin", "version": "1.0.0"}
        _wj(p / ".tfrobot-plugin" / "plugin.json", manifest)
    for s in skills:
        _w(p / "skills" / s / "SKILL.md", _skill_md())
    for s in servers:
        _wj(p / "mcp-servers" / f"{s}.json", {"name": s, "type": "stdio", "server_parameters": {"command": "node"}})
    return p


def _marketplace(tmp_path: Path, manifest: Any, *, plugins: tuple[str, ...] = ("data-toolkit",)) -> Path:
    """建 marketplace 根：manifest 传 dict（合法写盘）或 str（原样写盘，供语法错测试）。"""
    root = tmp_path / "mp"
    if isinstance(manifest, str):
        _w(root / ".tfrobot-plugin" / "marketplace.json", manifest)
    else:
        _wj(root / ".tfrobot-plugin" / "marketplace.json", manifest)
    for name in plugins:
        _plugin(root, name)
    return root


def _mp_manifest(**over: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "name": "acme-mp",
        "owner": {"name": "Acme Team", "email": "t@acme.io"},
        "plugins": [{"name": "data-toolkit", "source": "data-toolkit"}],
    }
    m.update(over)
    return m


def _codes(result: Any, severity: str = "error") -> list[str]:
    return [i.code for i in result.issues if i.severity == severity]


# ── 模式识别 / mode detection ─────────────────────────────────────────────────
def test_marketplace_happy_path(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest())
    r = validate_path(root)
    assert r.mode == "marketplace"
    assert r.valid, [i.message for i in r.issues]
    assert r.issues == []
    assert ".tfrobot-plugin/marketplace.json" in r.checked
    assert "plugins/data-toolkit/.tfrobot-plugin/plugin.json" in r.checked
    assert "plugins/data-toolkit/mcp-servers/etl.json" in r.checked
    assert "plugins/data-toolkit/skills/etl/SKILL.md" in r.checked


def test_standalone_plugin_mode(tmp_path: Path) -> None:
    p = _plugin(tmp_path / "solo")
    r = validate_path(p)
    assert r.mode == "plugin"
    assert r.valid, [i.message for i in r.issues]
    assert r.issues == []
    # plugin 模式下相对路径以 plugin 根为轴。
    assert "skills/etl/SKILL.md" in r.checked


def test_plugin_mode_without_plugin_json(tmp_path: Path) -> None:
    # 仅 skills/ 存在 → plugin 模式 + plugin.json 缺失 warning（loading-behavior §1 全组合合法）。
    p = _plugin(tmp_path / "solo", with_pjson=False, servers=())
    r = validate_path(p)
    assert r.mode == "plugin"
    assert r.valid  # warning 不影响 valid
    assert _codes(r, "warning") == [CODE_PLUGIN_MANIFEST_MISSING]


def test_not_a_target(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    for target in (empty, tmp_path / "no-such-dir"):
        r = validate_path(target)
        assert r.mode is None
        assert not r.valid
        assert _codes(r) == [CODE_NOT_A_TARGET]


# ── collect-all：单文件失败不终止 / one bad file does not stop the run ──────────
def test_collect_all_across_files(tmp_path: Path) -> None:
    # plugin.json 语法坏 + 另一 entry 完好：坏文件只出自己一条，好 plugin 照常深检进 checked。
    root = tmp_path / "mp"
    _wj(root / ".tfrobot-plugin" / "marketplace.json", _mp_manifest(plugins=[
        {"name": "data-toolkit", "source": "data-toolkit"},
        {"name": "good-toolkit", "source": "good-toolkit"},
    ]))
    _plugin(root, "good-toolkit")
    bad = _plugin(root, "data-toolkit")
    _w(bad / ".tfrobot-plugin" / "plugin.json", "{ not json")
    r = validate_path(root)
    assert not r.valid
    syntax = [i for i in r.errors if i.code == CODE_JSON_SYNTAX]
    assert len(syntax) == 1
    assert syntax[0].file == "plugins/data-toolkit/.tfrobot-plugin/plugin.json"
    # 好 plugin 的文件仍被检查（含其 SKILL.md / mcp server）。
    assert "plugins/good-toolkit/.tfrobot-plugin/plugin.json" in r.checked
    assert "plugins/good-toolkit/skills/etl/SKILL.md" in r.checked


def test_marketplace_manifest_syntax_error(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, "{ broken")
    r = validate_path(root)
    assert r.mode == "marketplace"
    assert not r.valid
    assert _codes(r) == [CODE_JSON_SYNTAX]


def test_root_not_object(tmp_path: Path) -> None:
    root = tmp_path / "mp"
    _w(root / ".tfrobot-plugin" / "marketplace.json", "[1, 2]")
    r = validate_path(root)
    assert [i.code for i in r.errors] == ["json-root-not-object"]


# ── 静态契约补位（§3/§4/§6）/ static contract checks ───────────────────────────
def test_marketplace_required_fields(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, {})
    r = validate_path(root)
    locs = {(i.code, i.location) for i in r.errors}
    assert (CODE_MISSING_FIELD, "name") in locs
    assert (CODE_MISSING_FIELD, "owner") in locs
    assert (CODE_MISSING_FIELD, "plugins") in locs


def test_owner_name_required(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(owner={}))
    r = validate_path(root)
    errs = [(i.code, i.location) for i in r.errors]
    assert (CODE_MISSING_FIELD, "owner.name") in errs


def test_invalid_kebab_names(tmp_path: Path) -> None:
    # marketplace 名 / entry 名 / plugin.json 名三处 kebab 全查，文件轴分别落在各自 manifest。
    root = _marketplace(tmp_path, _mp_manifest())
    _plugin(root, "data-toolkit", pjson={"name": "nope!"})
    _wj(root / ".tfrobot-plugin" / "marketplace.json", _mp_manifest(
        name="Bad_Name",
        plugins=[{"name": "Also_Bad", "source": "data-toolkit"}],
    ))
    r = validate_path(root)
    triples = {(i.code, i.file, i.location) for i in r.errors}
    assert (CODE_INVALID_NAME, ".tfrobot-plugin/marketplace.json", "name") in triples
    assert (CODE_INVALID_NAME, ".tfrobot-plugin/marketplace.json", "plugins[0].name") in triples
    assert (CODE_INVALID_NAME, "plugins/data-toolkit/.tfrobot-plugin/plugin.json", "name") in triples


def test_optional_field_type_checks(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(
        description=7,
        metadata={"pluginRoot": 5},
        plugins=[{"name": "data-toolkit", "source": "data-toolkit", "strict": "yes", "version": []}],
    ))
    r = validate_path(root)
    locs = {(i.code, i.location) for i in r.errors}
    assert (CODE_INVALID_TYPE, "description") in locs
    assert (CODE_INVALID_TYPE, "metadata.pluginRoot") in locs
    assert (CODE_INVALID_TYPE, "plugins[0].strict") in locs
    assert (CODE_INVALID_TYPE, "plugins[0].version") in locs


def test_plugins_must_be_array(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(plugins={"a": 1}))
    r = validate_path(root)
    assert (CODE_INVALID_TYPE, "plugins") in {(i.code, i.location) for i in r.errors}


# ── 静默吞错面 / silently-tolerated-at-install surfaces ────────────────────────
def test_malformed_plugin_entry_surfaced(tmp_path: Path) -> None:
    # install 的 iter_plugin_entries 静默跳过非对象条目——validator 显式报出（#193 核心价值）。
    root = _marketplace(tmp_path, _mp_manifest(plugins=[
        "just-a-string",
        {"name": "data-toolkit", "source": "data-toolkit"},
    ]))
    r = validate_path(root)
    errs = [(i.code, i.location) for i in r.errors]
    assert (CODE_INVALID_TYPE, "plugins[0]") in errs
    assert r.valid is False


def test_broken_plugin_json_surfaced(tmp_path: Path) -> None:
    # install 的 read_plugin_metadata WARN 后按 {} 兜底——validator 报 json-syntax（作者须知晓）。
    root = _marketplace(tmp_path, _mp_manifest())
    p = _plugin(root, "data-toolkit")
    _w(p / ".tfrobot-plugin" / "plugin.json", "/// not json")
    r = validate_path(root)
    assert CODE_JSON_SYNTAX in _codes(r)


def test_empty_plugin_json_requires_name(tmp_path: Path) -> None:
    # 空 {} 是合法 JSON object：不能折叠成「缺失合法」——§6.1 name 必填仍须报（隔离审查 🔴1 回归）。
    root = _marketplace(tmp_path, _mp_manifest())
    p = _plugin(root, "data-toolkit", pjson={})
    r = validate_path(root)
    assert not r.valid
    errs = [(i.code, i.file, i.location) for i in r.errors]
    assert (CODE_MISSING_FIELD, "plugins/data-toolkit/.tfrobot-plugin/plugin.json", "name") in errs
    assert p


def test_duplicate_plugin_names_warned(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(plugins=[
        {"name": "data-toolkit", "source": "data-toolkit"},
        {"name": "data-toolkit", "source": "data-toolkit"},
    ]))
    r = validate_path(root)
    assert r.valid  # warning 不致 invalid
    warns = [(i.code, i.location) for i in r.warnings]
    assert (CODE_DUPLICATE_PLUGIN_NAME, "plugins[1].name") in warns


# ── source（§5）/ plugin source ────────────────────────────────────────────────
def test_source_missing_and_invalid(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(plugins=[
        {"name": "data-toolkit"},  # 缺 source
        {"name": "x-toolkit", "source": {"source": "npm", "repo": "a/b"}},  # 未知 discriminator
    ]))
    r = validate_path(root)
    errs = [(i.code, i.location) for i in r.errors]
    assert (CODE_MISSING_FIELD, "plugins[0].source") in errs
    assert (CODE_INVALID_SOURCE, "plugins[1].source") in errs


def test_remote_source_skipped_with_warning(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(plugins=[
        {"name": "auth-tools", "source": {"source": "github", "repo": "turingfocus/tfs-auth-plugin", "ref": "v0.3.1"}},
    ]), plugins=())
    r = validate_path(root)
    assert r.valid
    assert _codes(r, "warning") == [CODE_REMOTE_SOURCE_SKIPPED]


def test_local_source_path_missing(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "ghost", "source": "./plugins/ghost"}]), plugins=())
    r = validate_path(root)
    assert [(i.code, i.location) for i in r.errors] == [(CODE_PATH_MISSING, "plugins[0].source")]


# ── strict 冲突（§4.4）/ strict-mode conflict ──────────────────────────────────
def test_strict_conflict(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "data-toolkit", "source": "data-toolkit", "strict": False}]))
    p = _plugin(root, "data-toolkit", pjson={"name": "data-toolkit", "skills": ["assets"]})
    (p / "assets").mkdir()
    r = validate_path(root)
    errs = [(i.code, i.file, i.location) for i in r.errors]
    assert (CODE_STRICT_CONFLICT, ".tfrobot-plugin/marketplace.json", "plugins[0].strict") in errs


# ── mcp-servers（§8）/ bundled MCP servers ─────────────────────────────────────
def test_mcp_server_invalid_and_name_mismatch(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest())
    p = _plugin(root, "data-toolkit", servers=())
    _wj(p / "mcp-servers" / "broken.json", {"name": "broken", "type": "stdio"})  # 缺 server_parameters
    _wj(p / "mcp-servers" / "renamed.json", {"name": "other-name", "type": "stdio", "server_parameters": {"command": "node"}})
    r = validate_path(root)
    mcp_errs = [i for i in r.errors if i.code == CODE_INVALID_MCP_SERVER]
    assert len(mcp_errs) == 2
    assert {i.file for i in mcp_errs} == {"plugins/data-toolkit/mcp-servers/broken.json", "plugins/data-toolkit/mcp-servers/renamed.json"}
    assert any("other-name" in i.message for i in mcp_errs)  # 文件名≠name 的原因可读


def test_inputs_json_syntax_checked(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest())
    p = _plugin(root, "data-toolkit", servers=())
    _w(p / "mcp-servers" / "inputs.json", "{ nope")
    r = validate_path(root)
    assert [(i.code, i.file) for i in r.errors] == [(CODE_JSON_SYNTAX, "plugins/data-toolkit/mcp-servers/inputs.json")]


# ── SKILL 深检 / SKILL deep checks ─────────────────────────────────────────────
def test_skill_dir_without_skill_md_warns(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest())
    p = _plugin(root, "data-toolkit")
    (p / "skills" / "aux").mkdir()  # 无 SKILL.md：装载静默跳过 → warning
    r = validate_path(root)
    assert r.valid
    assert (CODE_SKILL_MD_MISSING, "plugins/data-toolkit/skills/aux") in {(i.code, i.file) for i in r.warnings}


def test_skill_missing_description_is_error(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest())
    p = _plugin(root, "data-toolkit", skills=())
    _w(p / "skills" / "etl" / "SKILL.md", "---\nname: etl\n---\n\nbody\n")  # 无 description
    r = validate_path(root)
    assert not r.valid
    assert [(i.code, i.location) for i in r.errors] == [(CODE_SKILL_DESC_MISSING, "frontmatter.description")]


def test_skill_bad_basename_is_error(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest())
    _plugin(root, "data-toolkit", skills=())
    _w(root / "plugins" / "data-toolkit" / "skills" / "Bad_Name" / "SKILL.md", _skill_md())
    r = validate_path(root)
    assert [(i.code, i.file) for i in r.errors] == [(CODE_SKILL_NAME_INVALID, "plugins/data-toolkit/skills/Bad_Name")]


def test_skill_duplicate_across_containers(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "data-toolkit", "source": "data-toolkit", "skills": "extra-skills"}]))
    p = _plugin(root, "data-toolkit", skills=())
    _w(p / "skills" / "etl" / "SKILL.md", _skill_md())
    _w(p / "extra-skills" / "etl" / "SKILL.md", _skill_md())  # 同 basename → 合成名 data-toolkit:etl 重名
    r = validate_path(root)
    assert [(i.code) for i in r.errors] == [CODE_DUPLICATE_SKILL]


def test_no_skill_container_warns(tmp_path: Path) -> None:
    # MCP-only plugin：无 skills 容器合法（装载 WARNING）→ warning。
    root = _marketplace(tmp_path, _mp_manifest(), plugins=())
    _plugin(root, "data-toolkit", skills=())
    r = validate_path(root)
    assert r.valid
    assert CODE_NO_SKILL_CONTAINER in _codes(r, "warning")


# ── skills 覆写路径（§4.3）/ skills override paths ─────────────────────────────
def test_override_path_missing_is_error(tmp_path: Path) -> None:
    # 装载 resolve_skill_override_dirs WARNING 跳过——作者显式声明 → error。
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "data-toolkit", "source": "data-toolkit", "skills": "no-such-dir"}]))
    _plugin(root, "data-toolkit", skills=())
    r = validate_path(root)
    assert [(i.code, i.location) for i in r.errors] == [(CODE_PATH_MISSING, "plugins[0].skills")]


def test_override_path_escape_is_error(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "data-toolkit", "source": "data-toolkit", "skills": "../escape"}]))
    _plugin(root, "data-toolkit", skills=())
    r = validate_path(root)
    assert [(i.code, i.location) for i in r.errors] == [(CODE_PATH_ESCAPE, "plugins[0].skills")]


def test_override_from_plugin_json_strict_true(tmp_path: Path) -> None:
    # strict=true（默认）：plugin.json.skills 覆写生效（entry.skills 空）。
    root = _marketplace(tmp_path, _mp_manifest())
    p = _plugin(root, "data-toolkit", skills=(), pjson={"name": "data-toolkit", "skills": "alt-skills"})
    _w(p / "alt-skills" / "report" / "SKILL.md", _skill_md())
    r = validate_path(root)
    assert r.valid, [i.message for i in r.issues]
    assert "plugins/data-toolkit/alt-skills/report/SKILL.md" in r.checked


# ── 诊断脱敏（#263）/ diagnostic redaction ─────────────────────────────────────
# 校验结果的 message 会同时进 CLI 文本与 `--json`（validate.py 直接渲染 issue.message），
# 因此断言 message 本身：不得含 userinfo / query / fragment 或内嵌凭据的名称。
def _messages(result: Any) -> str:
    return "\n".join(i.message for i in result.issues)


def test_invalid_marketplace_name_with_credentials_is_redacted(tmp_path: Path) -> None:
    root = _marketplace(tmp_path, {"name": "x=https://alice:PW_NAME@example.com/repo.git", "owner": {"name": "A"}, "plugins": []})
    r = validate_path(root)
    assert CODE_INVALID_NAME in _codes(r)
    text = _messages(r)
    assert "PW_NAME" not in text
    assert "alice:" not in text
    assert "https://example.com/repo.git" in text


def test_invalid_plugin_source_with_credentials_is_redacted(tmp_path: Path) -> None:
    root = _marketplace(
        tmp_path,
        _mp_manifest(plugins=[{"name": "bad", "source": "key=https://alice:PW_SRC@example.com/r.git"}]),
        plugins=(),
    )
    r = validate_path(root)
    assert CODE_INVALID_SOURCE in _codes(r)
    text = _messages(r)
    assert "PW_SRC" not in text
    assert "alice:" not in text
    assert "https://example.com/r.git" in text


def test_remote_source_warning_redacts_url_credentials(tmp_path: Path) -> None:
    root = _marketplace(
        tmp_path,
        _mp_manifest(plugins=[{"name": "remote", "source": {"source": "url", "url": "https://alice:PW_REMOTE@example.com/r.git"}}]),
        plugins=(),
    )
    r = validate_path(root)
    assert CODE_REMOTE_SOURCE_SKIPPED in _codes(r, "warning")
    text = _messages(r)
    assert "PW_REMOTE" not in text
    assert "alice:" not in text
    # strict 展示语义：保留 scheme/host/path
    assert "https://example.com/r.git" in text


def test_missing_local_source_redacts_embedded_credentials(tmp_path: Path) -> None:
    """相对源路径内嵌凭据 URL（rust#154 向量⑤）：rel_path 与解析结果都不得回显凭据。"""
    root = _marketplace(
        tmp_path,
        _mp_manifest(plugins=[{"name": "ghost", "source": "./x=https://alice:PW_LOCAL@example.com/repo.git"}]),
        plugins=(),
    )
    r = validate_path(root)
    assert CODE_PATH_MISSING in _codes(r)
    text = _messages(r)
    assert "PW_LOCAL" not in text
    assert "alice:" not in text
    assert "https://example.com/repo.git" in text


# ── 复审 delta（#263 第二轮）：路径轴 / query-fragment / file 轴 ─────────────────
def test_query_and_fragment_not_echoed_for_normalized_source(tmp_path: Path) -> None:
    """source 归一后 ``://`` 坍缩成 ``:/``：query / fragment 不得借路径泄漏（用原始 source 回显）。"""
    root = _marketplace(
        tmp_path,
        # 关键：不含 userinfo —— scp-like 兜底规则抓不到，只有原始 source 的 URL 规则才认得 query/fragment。
        _mp_manifest(plugins=[{"name": "ghost", "source": "./x=https://example.com/r.git?token=QUERY#frag"}]),
        plugins=(),
    )
    r = validate_path(root)
    assert CODE_PATH_MISSING in _codes(r)
    text = _messages(r)
    for secret in ("token=QUERY", "#frag"):
        assert secret not in text


def test_issue_file_axis_redacts_credential_path(tmp_path: Path) -> None:
    """`ValidationIssue.file`（--json 的 file 轴）：凭据形态的 source 目录名不得原样进 file。"""
    bad = "x=https:.//alice:PW_FILE@example.com/repo.git"
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "audit", "source": f"./plugins/{bad}"}]), plugins=())
    # 造出深检分支需要的目录（触发 plugin.json 缺失等以该路径为 file 的诊断）
    (root / "plugins" / bad / "skills" / "etl").mkdir(parents=True)
    (root / "plugins" / bad / "skills" / "etl" / "SKILL.md").write_text(_skill_md(), encoding="utf-8")
    r = validate_path(root)
    assert r.issues, "夹具应至少产出一条诊断"
    for issue in r.issues:
        assert "PW_FILE" not in issue.file
        assert "alice:" not in issue.file
        assert "PW_FILE" not in issue.message
    for checked in r.checked:
        assert "PW_FILE" not in checked


# ── 复审第二轮：message 轴 + 正对照 ─────────────────────────────────────────────
def test_issue_message_axis_redacts_path_from_deep_check(tmp_path: Path) -> None:
    """深检报错（bundled server / plugin.json）的 message 内嵌路径时也不得带 query / fragment。"""
    bad = "x=https:/h/r.git?token=SECRET#frag"
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "audit", "source": f"./plugins/{bad}"}]), plugins=())
    p = root / "plugins" / bad
    (p / ".tfrobot-plugin").mkdir(parents=True)
    (p / ".tfrobot-plugin" / "plugin.json").write_text('{"name":"audit"}', encoding="utf-8")
    (p / "mcp-servers").mkdir()
    (p / "mcp-servers" / "bad.json").write_text("{ broken", encoding="utf-8")
    (p / "skills" / "etl").mkdir(parents=True)
    (p / "skills" / "etl" / "SKILL.md").write_text(_skill_md(), encoding="utf-8")

    r = validate_path(root)
    text = _messages(r)
    assert "token=SECRET" not in text
    assert "#frag" not in text
    # 正对照：确实产出了深检诊断（否则以上否定断言恒真）
    assert any(i.code == CODE_INVALID_MCP_SERVER for i in r.issues), [i.code for i in r.issues]


def test_file_and_checked_axis_positive_control(tmp_path: Path) -> None:
    """正对照：file / checked 轴确实拿到了遮蔽值（否定断言不能靠「轴为空」蒙混）。"""
    bad = "x=https:.//alice:PW_POS@example.com/repo.git"
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "audit", "source": f"./plugins/{bad}"}]), plugins=())
    p = root / "plugins" / bad
    (p / "skills" / "etl").mkdir(parents=True)
    (p / "skills" / "etl" / "SKILL.md").write_text(_skill_md(), encoding="utf-8")

    r = validate_path(root)
    assert r.checked, "夹具应至少检查过文件"
    assert any("<redacted-source>" in i.file for i in r.issues), [i.file for i in r.issues]
    assert any("<redacted-source>" in f for f in r.checked), list(r.checked)


def test_collector_add_sanitizes_message_and_file(tmp_path: Path) -> None:
    """`_Collector.add()` 是诊断三轴的兜底出口：新增诊断点即使直接塞归一化路径也不会泄漏。"""
    from a2c_smcp.computer.skills.validator import _Collector

    c = _Collector(tmp_path)
    bad = "plugins/x=https:/h/r.git?token=SECRET#frag"
    c.add("x-code", bad, bad, f"cannot read {bad}: boom")
    issue = c.result.issues[0]
    assert "token=SECRET" not in issue.message
    assert "token=SECRET" not in issue.file
    assert "token=SECRET" not in issue.location


def test_manifest_error_message_redacts_credential_path(tmp_path: Path) -> None:
    """manifest 层（PluginManifestError）自身的 message 也要遮蔽路径里的 query / fragment。"""
    from a2c_smcp.computer.skills.manifest import PluginManifestError, parse_bundled_server

    bad_dir = tmp_path / "x=https:/h/r.git?token=SECRET#frag"
    bad_dir.mkdir(parents=True)
    bad = bad_dir / "srv.json"
    bad.write_text("{ broken", encoding="utf-8")
    with pytest.raises(PluginManifestError) as ei:
        parse_bundled_server(bad)
    msg = str(ei.value)
    assert "token=SECRET" not in msg
    assert "#frag" not in msg


def test_manifest_validation_message_hides_input_values(tmp_path: Path) -> None:
    """#263 三轮复审：pydantic 诊断不得带 `input_value`（env / headers 里可能是 token）。"""
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "audit", "source": "audit"}]), plugins=())
    p = root / "plugins" / "audit" / "mcp-servers"
    p.mkdir(parents=True)
    (p / "evil.json").write_text(
        json.dumps({
            "name": "evil",
            "type": "stdio",
            "server_parameters": {"command": "node", "env": {"TOKEN": "ghp_SUPERSECRET"}},
            "unexpected_field": "ghp_SUPERSECRET",
        }),
        encoding="utf-8",
    )
    r = validate_path(root)
    text = _messages(r)
    assert "ghp_SUPERSECRET" not in text
    assert "input_value" not in text


def test_collector_add_preserves_prose_around_masked_path(tmp_path: Path) -> None:
    """#263 三轮复审：message 轴只遮蔽路径 token——可读前缀、问号散文、换行必须保留。"""
    from a2c_smcp.computer.skills.validator import _Collector

    c = _Collector(tmp_path)
    bad = "plugins/x=https:/h/r.git?token=SECRET#frag"
    c.add("x-code", bad, ".", f"cannot read {bad}: why? oops\nsecond line")
    msg = c.result.issues[0].message
    assert "token=SECRET" not in msg
    # 正对照：前缀 / prose 的 `?` / 换行都还在（不得整段吞掉或转义成 \x0a）
    assert msg.startswith("cannot read plugins/x=https:/h/r.git")
    assert msg.endswith("why? oops\nsecond line")


def test_checked_axis_masks_userinfo_with_query(tmp_path: Path) -> None:
    """#263 四轮：`file` / `checked` 轴在 userinfo + `?` 同段时也不得漏凭据头。"""
    bad = "x=https:/alice:pw@host?token=SECRET"
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "audit", "source": f"./plugins/{bad}"}]), plugins=())
    p = root / "plugins" / bad
    (p / "skills" / "etl").mkdir(parents=True)
    (p / "skills" / "etl" / "SKILL.md").write_text(_skill_md(), encoding="utf-8")

    r = validate_path(root)
    assert r.checked
    blob = "\n".join([*(i.file for i in r.issues), *r.checked])
    assert "pw@" not in blob and "token=SECRET" not in blob
    assert "<redacted-source>" in blob


# ── #263 五轮复审：非 UTF-8 / 对象源回显不再二次散文化 ──────────────────────────
def test_non_utf8_manifest_is_reported_not_raised(tmp_path: Path) -> None:
    """非 UTF-8 的 marketplace.json（UnicodeDecodeError）→ 记诊断，不得 traceback。"""
    root = tmp_path / "mp-bytes"
    path = root / ".tfrobot-plugin" / "marketplace.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe not utf-8")
    r = validate_path(root)
    assert any(i.code == CODE_UNREADABLE for i in r.issues), [i.code for i in r.issues]


def test_object_source_message_stays_readable(tmp_path: Path) -> None:
    """对象源诊断不得被二次散文脱敏改写成 percent-encoded 乱码。"""
    root = _marketplace(
        tmp_path,
        _mp_manifest(plugins=[{"name": "audit", "source": {"source": "url", "url": "https://alice:PW_OBJ@example.com/r.git", "ref": ""}}]),
        plugins=(),
    )
    r = validate_path(root)
    text = _messages(r)
    assert "PW_OBJ" not in text
    assert "%22" not in text and "%2C" not in text  # 没有 percent-encoded 的 JSON 标点
    assert '"ref":""' in text  # 结构仍可读


def test_manifest_reader_reports_non_utf8_as_manifest_error(tmp_path: Path) -> None:
    """manifest 层 `_read_json_object` 也必须把非 UTF-8 归一成 PluginManifestError（install 预检同路径）。"""
    from a2c_smcp.computer.skills.manifest import PluginManifestError, read_marketplace_manifest

    root = tmp_path / "mp"
    path = root / ".tfrobot-plugin" / "marketplace.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(PluginManifestError) as ei:
        read_marketplace_manifest(root)
    # 断言确实命中 UnicodeDecodeError 归一（不是别的分支）——避免「unreadable or invalid」近似恒真。
    assert isinstance(ei.value.__cause__, UnicodeDecodeError)
    assert "marketplace manifest" in str(ei.value)


def test_non_utf8_skill_md_reported_as_unreadable(tmp_path: Path) -> None:
    """#263 七轮：非 UTF-8 SKILL.md 深检归一为 skill-md-unreadable（不得 traceback、不得泄漏解码细节）。"""
    root = _marketplace(tmp_path, _mp_manifest(plugins=[{"name": "audit", "source": "audit"}]), plugins=())
    p = root / "plugins" / "audit" / "skills" / "bad"
    p.mkdir(parents=True)
    (p / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
    r = validate_path(root)
    assert any(i.code == CODE_SKILL_MD_UNREADABLE for i in r.issues), [i.code for i in r.issues]
    msg = next(i.message for i in r.issues if i.code == CODE_SKILL_MD_UNREADABLE)
    assert "codec" not in msg
