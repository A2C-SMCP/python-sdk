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
    CODE_SKILL_NAME_INVALID,
    CODE_STRICT_CONFLICT,
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
