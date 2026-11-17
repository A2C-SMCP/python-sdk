# -*- coding: utf-8 -*-
# filename: test_validate.py
# @Time    : 2026/08/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``plugin validate`` / ``marketplace validate`` CLI handler 测试（#193）/ Validate CLI handler tests.

测试意图 / Test intentions:
- 退出码契约：无 error → 0（warnings 不影响）；任一 error → 1；不可识别路径 → 1；
- ``--json`` 输出结构（验收标准钉死的 CI 消费契约面：valid/mode/path/checked_files/errors/warnings + issue 四键）；
- 双入口 alias：``plugin validate`` 与 ``marketplace validate`` 两名一实现（CliRunner 走 main app 注册面）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from a2c_smcp.computer.cli import main as cli_main
from a2c_smcp.computer.cli.commands.validate import plugin_validate


def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _valid_marketplace(tmp_path: Path) -> Path:
    root = tmp_path / "mp"
    _w(root / ".tfrobot-plugin" / "marketplace.json", json.dumps({
        "name": "acme-mp",
        "owner": {"name": "Acme"},
        "plugins": [{"name": "data-toolkit", "source": "data-toolkit"}],
    }))
    _w(root / "plugins" / "data-toolkit" / ".tfrobot-plugin" / "plugin.json", json.dumps({"name": "data-toolkit"}))
    _w(root / "plugins" / "data-toolkit" / "skills" / "etl" / "SKILL.md",
       "---\nname: etl\ndescription: does etl\n---\n\nbody\n")
    _w(root / "plugins" / "data-toolkit" / "mcp-servers" / "etl.json",
       json.dumps({"name": "etl", "type": "stdio", "server_parameters": {"command": "node"}}))
    return root


def _invalid_marketplace(tmp_path: Path) -> Path:
    root = tmp_path / "mp-bad"
    _w(root / ".tfrobot-plugin" / "marketplace.json", "{ broken json")
    return root


# ── handler 直调（退出码 + 输出）/ handler direct calls ─────────────────────────
def test_valid_marketplace_exit_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = plugin_validate(_valid_marketplace(tmp_path))
    assert code == 0
    out = capsys.readouterr().out
    assert "✓ valid" in out  # 非 "valid" 子串（"invalid" 亦含之——弱断言致盲面）


def test_invalid_marketplace_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = plugin_validate(_invalid_marketplace(tmp_path))
    assert code == 1
    out = capsys.readouterr().out
    assert "json-syntax" in out  # 人类可读输出含诊断码


def test_not_a_target_exit_one(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert plugin_validate(empty) == 1
    assert plugin_validate(tmp_path / "no-such") == 1


def test_json_output_contract(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """JSON 结构 = 验收标准第 4 条钉死的契约面（顶层五键 + issue 四键）。"""
    code = plugin_validate(_invalid_marketplace(tmp_path), json_output=True)
    assert code == 1
    data: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert set(data) == {"valid", "mode", "path", "checked_files", "errors", "warnings"}
    assert data["valid"] is False
    assert data["mode"] == "marketplace"
    assert isinstance(data["path"], str)
    assert data["checked_files"] == []  # 语法坏文件未被消费
    assert len(data["errors"]) == 1
    err = data["errors"][0]
    assert set(err) == {"code", "file", "location", "message"}
    assert err["code"] == "json-syntax"
    assert err["file"] == ".tfrobot-plugin/marketplace.json"
    assert data["warnings"] == []


def test_json_output_valid_with_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """远程 source 跳过 → valid=true + warnings 非空（CI 可据 warnings 提示但不挡）。"""
    root = tmp_path / "mp-remote"
    _w(root / ".tfrobot-plugin" / "marketplace.json", json.dumps({
        "name": "acme-mp",
        "owner": {"name": "Acme"},
        "plugins": [{"name": "auth-tools", "source": {"source": "github", "repo": "a/b"}}],
    }))
    code = plugin_validate(root, json_output=True)
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["valid"] is True
    assert [w["code"] for w in data["warnings"]] == ["remote-source-skipped"]


def test_json_output_not_a_target_mode_null(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """not-a-target 路径的 --json 契约：mode 序列化为 null（隔离审查 🟡7 补钉）。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    code = plugin_validate(empty, json_output=True)
    assert code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["valid"] is False
    assert data["mode"] is None
    assert data["errors"][0]["code"] == "not-a-marketplace-or-plugin"


def test_human_output_escapes_bracketed_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """渲染保真：pydantic 诊断含 ``[type=missing, ...]`` 方括号段，不得被 rich 当样式标签吞掉（🟡2）。"""
    root = tmp_path / "mp-pyd"
    _w(root / ".tfrobot-plugin" / "marketplace.json", json.dumps({
        "name": "acme-mp",
        "owner": {"name": "Acme"},
        "plugins": [{"name": "data-toolkit", "source": "data-toolkit"}],
    }))
    # stdio 缺 server_parameters → 诊断 message 含 `[字段:类型]` 方括号段（#263 复审后不再带 input_value）。
    _w(root / "plugins" / "data-toolkit" / "mcp-servers" / "etl.json",
       json.dumps({"name": "etl", "type": "stdio"}))
    code = plugin_validate(root)
    assert code == 1
    out = capsys.readouterr().out
    assert "[StdioServerConfig.server_parameters:missing" in out  # 方括号段完整呈现（未被 rich markup 消费）


# ── 双入口 alias（#193 用户裁决）/ dual entry alias ─────────────────────────────
def test_dual_entry_plugin_validate(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_main.app, ["plugin", "validate", str(_valid_marketplace(tmp_path))])
    assert result.exit_code == 0


def test_dual_entry_marketplace_validate(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_main.app, ["marketplace", "validate", str(_invalid_marketplace(tmp_path)), "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["valid"] is False


def test_dual_entry_json_flag(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_main.app, ["plugin", "validate", str(_valid_marketplace(tmp_path)), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["valid"] is True and data["mode"] == "marketplace"


# ── #263 凭据脱敏（--json 与文本同一 message）/ credential redaction ─────────────
def _cred_marketplace(tmp_path: Path) -> Path:
    root = tmp_path / "mp-cred"
    _w(root / ".tfrobot-plugin" / "marketplace.json", json.dumps({
        "name": "acme-mp",
        "owner": {"name": "Acme"},
        "plugins": [
            {"name": "bad", "source": "key=https://alice:PW_JSON@example.com/r.git"},
            {"name": "remote", "source": {"source": "url", "url": "https://bob:PW_REMOTE@example.com/r.git"}},
        ],
    }))
    return root


def test_json_output_redacts_source_credentials(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`plugin validate --json`：错误与 warning 的 message 均不得含 userinfo。"""
    code = plugin_validate(_cred_marketplace(tmp_path), json_output=True)
    out = capsys.readouterr().out
    assert code == 1
    for secret in ("PW_JSON", "PW_REMOTE", "alice:", "bob:"):
        assert secret not in out
    data = json.loads(out)
    msgs = "\n".join(i["message"] for i in data["errors"] + data["warnings"])
    assert "https://example.com/r.git" in msgs


def test_human_output_redacts_source_credentials(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """文本路径同一口径（同一 ValidationIssue.message，仅渲染方式不同）。"""
    code = plugin_validate(_cred_marketplace(tmp_path))
    out = capsys.readouterr().out
    assert code == 1
    for secret in ("PW_JSON", "PW_REMOTE", "alice:", "bob:"):
        assert secret not in out


def test_json_file_axis_redacts_credential_source_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#263 复审：`--json` 的 `file` / `checked_files` 轴同样不得回显 source 内的凭据。"""
    bad = "x=https:.//alice:PW_FILE@example.com/repo.git"
    root = tmp_path / "mp-file"
    _w(root / ".tfrobot-plugin" / "marketplace.json", json.dumps({
        "name": "acme-mp",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [{"name": "audit", "source": f"./plugins/{bad}"}],
    }))
    from a2c_smcp.computer.skills.validator import CODE_PLUGIN_MANIFEST_MISSING  # noqa: PLC0415

    d = root / "plugins" / bad / "skills" / "etl"
    d.mkdir(parents=True)
    _w(d / "SKILL.md", "---\nname: etl\ndescription: d\n---\n\nbody\n")

    code = plugin_validate(root, json_output=True)
    out = capsys.readouterr().out
    assert code == 0  # plugin.json 缺失是 warning（不挡）
    assert "PW_FILE" not in out
    data = json.loads(out)
    assert any(i["code"] == CODE_PLUGIN_MANIFEST_MISSING for i in data["warnings"])
    assert data["checked_files"], "夹具应至少检查过文件（否则下面的否定断言恒真）"
    assert any("<redacted-source>" in i["file"] for i in data["errors"] + data["warnings"])
    for issue in data["errors"] + data["warnings"]:
        assert "PW_FILE" not in issue["file"] and "alice:" not in issue["file"]
        assert "PW_FILE" not in issue["message"] and "alice:" not in issue["message"]
    assert all("PW_FILE" not in f for f in data["checked_files"])
    assert any("<redacted-source>" in f for f in data["checked_files"])


def test_json_path_and_human_line_redact_credential_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#263 三轮复审：`--json` 的 `path` 与人类输出的 root 也不得回显凭据形态。"""
    root = tmp_path / "x=https:/TOKEN@host/mp"
    _w(root / ".tfrobot-plugin" / "marketplace.json", json.dumps({
        "name": "acme-mp",
        "owner": {"name": "Acme"},
        "plugins": [{"name": "data-toolkit", "source": "data-toolkit"}],
    }))
    _w(root / "plugins" / "data-toolkit" / "skills" / "etl" / "SKILL.md",
       "---\nname: etl\ndescription: d\n---\n\nbody\n")

    code = plugin_validate(root, json_output=True)
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert "TOKEN" not in out
    data = json.loads(out)
    assert "<redacted-source>" in data["path"]

    plugin_validate(root)
    human = capsys.readouterr().out
    assert "TOKEN" not in human
