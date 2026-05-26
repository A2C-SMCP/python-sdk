# -*- coding: utf-8 -*-
# filename: test_manifest.py
# @Time    : 2026/05/26
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
Plugin manifest 解析单元测试（v0.2.1 #63）—— marketplace.json / plugin.json / mcp-servers/<n>.json
Plugin manifest parsing unit tests: marketplace.json / plugin.json / mcp-servers/<n>.json.

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §3.2–3.5 / §3.3。

测试意图 / Test intentions（纯解析，无 git / manager）:
- marketplace.json：present/缺失抛/非对象抛；iter/find entry；pluginRoot 默认。
- plugin.json：present/缺失→{}/损坏→{}（best-effort）。
- version 优先级 entry > plugin.json > commitSha。
- mcp-servers：枚举一级、排除 inputs.json、sorted、缺目录→[]；stdio/sse/streamable round-trip；
  文件名≠name 抛；畸形 JSON 抛；非法 config 抛；load 任一失败即抛、空→[]。
"""

import json
from pathlib import Path

import pytest

from a2c_smcp.computer.mcp_clients.model import SseServerConfig, StdioServerConfig, StreamableHttpServerConfig
from a2c_smcp.computer.skills.manifest import (
    PluginManifestError,
    enumerate_bundled_server_files,
    find_plugin_entry,
    iter_plugin_entries,
    load_bundled_servers,
    parse_bundled_server,
    plugin_root_base,
    read_marketplace_manifest,
    read_plugin_metadata,
    resolve_plugin_version,
)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _stdio(name: str, command: str = "node") -> dict:
    return {"name": name, "type": "stdio", "server_parameters": {"command": command}}


# ── marketplace.json ─────────────────────────────────────────────────────────
def test_read_marketplace_manifest_ok(tmp_path: Path) -> None:
    manifest = {
        "name": "acme",
        "owner": {"name": "X"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": "audit", "version": "1.2.0"}],
    }
    _write_json(tmp_path / ".tfrobot-plugin" / "marketplace.json", manifest)

    out = read_marketplace_manifest(tmp_path)
    assert out["name"] == "acme"
    assert plugin_root_base(out) == "./plugins"
    assert [e["name"] for e in iter_plugin_entries(out)] == ["audit"]
    entry = find_plugin_entry(out, "audit")
    assert entry is not None and entry["source"] == "audit"
    assert find_plugin_entry(out, "missing") is None


def test_read_marketplace_manifest_absent_raises(tmp_path: Path) -> None:
    with pytest.raises(PluginManifestError):
        read_marketplace_manifest(tmp_path)


def test_read_marketplace_manifest_non_object_raises(tmp_path: Path) -> None:
    _write_json(tmp_path / ".tfrobot-plugin" / "marketplace.json", ["not", "an", "object"])
    with pytest.raises(PluginManifestError):
        read_marketplace_manifest(tmp_path)


def test_plugin_root_base_defaults() -> None:
    assert plugin_root_base({}) == "./plugins"
    assert plugin_root_base({"metadata": {"pluginRoot": "  "}}) == "./plugins"  # 空白回退默认


def test_iter_plugin_entries_non_array_or_missing() -> None:
    assert iter_plugin_entries({"plugins": "oops"}) == []
    assert iter_plugin_entries({}) == []
    # 非对象条目被跳过
    assert iter_plugin_entries({"plugins": [{"name": "a"}, "junk", 42]}) == [{"name": "a"}]


# ── plugin.json ──────────────────────────────────────────────────────────────
def test_read_plugin_metadata_ok(tmp_path: Path) -> None:
    _write_json(tmp_path / ".tfrobot-plugin" / "plugin.json", {"name": "audit", "version": "1.2.0"})
    assert read_plugin_metadata(tmp_path)["version"] == "1.2.0"


def test_read_plugin_metadata_absent(tmp_path: Path) -> None:
    assert read_plugin_metadata(tmp_path) == {}


def test_read_plugin_metadata_corrupt_degrades(tmp_path: Path) -> None:
    p = tmp_path / ".tfrobot-plugin" / "plugin.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert read_plugin_metadata(tmp_path) == {}  # best-effort → {}


# ── version 优先级 ───────────────────────────────────────────────────────────
def test_resolve_plugin_version_priority() -> None:
    assert resolve_plugin_version({"version": "9.9"}, {"version": "1.0"}, "sha") == "9.9"  # entry 胜
    assert resolve_plugin_version({}, {"version": "1.0"}, "sha") == "1.0"  # plugin.json 次之
    assert resolve_plugin_version({}, {}, "sha") == "sha"  # commitSha 兜底
    assert resolve_plugin_version({}, {}, None) is None


# ── mcp-servers/ ─────────────────────────────────────────────────────────────
def test_enumerate_excludes_inputs_and_non_json_and_sorts(tmp_path: Path) -> None:
    d = tmp_path / "mcp-servers"
    d.mkdir()
    for n in ("b", "a"):
        _write_json(d / f"{n}.json", _stdio(n))
    _write_json(d / "inputs.json", {"inputs": []})  # 排除（入池归 #65）
    (d / "notes.txt").write_text("x", encoding="utf-8")  # 非 .json 排除

    files = enumerate_bundled_server_files(tmp_path)
    assert [f.name for f in files] == ["a.json", "b.json"]  # sorted、排除 inputs.json + 非 json


def test_enumerate_missing_dir(tmp_path: Path) -> None:
    assert enumerate_bundled_server_files(tmp_path) == []


def test_parse_bundled_server_stdio(tmp_path: Path) -> None:
    p = tmp_path / "mcp-servers" / "figma.json"
    _write_json(p, _stdio("figma"))
    cfg = parse_bundled_server(p)
    assert isinstance(cfg, StdioServerConfig) and cfg.name == "figma"


def test_parse_bundled_server_sse(tmp_path: Path) -> None:
    p = tmp_path / "mcp-servers" / "remote.json"
    _write_json(p, {"name": "remote", "type": "sse", "server_parameters": {"url": "https://x/sse"}})
    cfg = parse_bundled_server(p)
    assert isinstance(cfg, SseServerConfig) and cfg.name == "remote"


def test_parse_bundled_server_streamable(tmp_path: Path) -> None:
    p = tmp_path / "mcp-servers" / "http.json"
    _write_json(p, {"name": "http", "type": "streamable", "server_parameters": {"url": "https://x/mcp"}})
    cfg = parse_bundled_server(p)
    assert isinstance(cfg, StreamableHttpServerConfig) and cfg.name == "http"


def test_parse_bundled_server_stem_mismatch_raises(tmp_path: Path) -> None:
    p = tmp_path / "mcp-servers" / "wrongname.json"
    _write_json(p, _stdio("figma"))  # stem 'wrongname' != name 'figma'
    with pytest.raises(PluginManifestError, match="filename stem"):
        parse_bundled_server(p)


def test_parse_bundled_server_malformed_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "mcp-servers" / "x.json"
    p.parent.mkdir(parents=True)
    p.write_text("{bad", encoding="utf-8")
    with pytest.raises(PluginManifestError):
        parse_bundled_server(p)


def test_parse_bundled_server_invalid_config_raises(tmp_path: Path) -> None:
    p = tmp_path / "mcp-servers" / "bad.json"
    _write_json(p, {"name": "bad", "type": "stdio"})  # stdio 缺 server_parameters → ValidationError → 抛
    with pytest.raises(PluginManifestError):
        parse_bundled_server(p)


def test_load_bundled_servers_all(tmp_path: Path) -> None:
    d = tmp_path / "mcp-servers"
    d.mkdir()
    _write_json(d / "figma.json", _stdio("figma"))
    _write_json(d / "blender.json", _stdio("blender"))
    servers = load_bundled_servers(tmp_path)
    assert sorted(s.name for s in servers) == ["blender", "figma"]


def test_load_bundled_servers_any_malformed_raises(tmp_path: Path) -> None:
    d = tmp_path / "mcp-servers"
    d.mkdir()
    _write_json(d / "ok.json", _stdio("ok"))
    (d / "broken.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(PluginManifestError):
        load_bundled_servers(tmp_path)


def test_load_bundled_servers_empty(tmp_path: Path) -> None:
    assert load_bundled_servers(tmp_path) == []
