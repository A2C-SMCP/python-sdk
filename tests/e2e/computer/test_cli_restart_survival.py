"""
文件名: test_cli_restart_survival.py
作者: JQQ
创建日期: 2026/07/15
版权: 2023 JQQ. All rights reserved.
依赖: pytest, pexpect
描述:
  中文: #139 ④ 组 6——durable `server add` 重启存活 e2e。REPL 显式声明一个 MCP server（durable 落
        `mcp.local.json`）→ 退出 → **全新进程**同一 cwd 重启（`--approve-all-mcp` 免 pending 提示）→ 该 server
        仍在（`status` 可见）。守护 #137/#138 双路径：用户显式声明是持久的、跨进程重启存活（对齐 rust）。
  English: #139 ④ group 6 — durable `server add` survives restart. Declare a server via REPL (durable →
        mcp.local.json) → exit → fresh process in the same cwd (`--approve-all-mcp`) → server still present.

隔离: 两次 spawn 均用 pytest ``tmp_path`` 作 cwd（durable 落 tmp/.tfrobot/），并对首个进程传
  ``cleanup_durable=False`` 令其写下的声明留存给第二个进程读到；tmp_path 由 pytest 兜底清理，绝不污染仓库。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.e2e.computer.utils import expect_prompt_stable, strip_ansi

pexpect = pytest.importorskip("pexpect", reason="e2e tests require pexpect; install with `pip install pexpect`.")

# 复用集成测试里的真实 stdio MCP server（提供 hello 工具）；用**绝对路径**（tmp cwd 下相对路径不可解析）。
_DIRECT_EXEC = Path(__file__).resolve().parents[2] / "integration_tests" / "computer" / "mcp_servers" / "direct_execution.py"


def _survivor_json() -> str:
    cfg = {
        "name": "survivor",
        "type": "stdio",
        "disabled": False,
        "forbidden_tools": [],
        "tool_meta": {},
        "server_parameters": {
            "command": sys.executable,
            "args": [str(_DIRECT_EXEC)],  # 绝对路径：tmp cwd 下仍可解析
            "env": None,
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        },
    }
    return json.dumps(cfg, ensure_ascii=False)


@pytest.mark.e2e
def test_durable_server_add_survives_restart(cli_proc_factory, tmp_path: Path) -> None:  # noqa: ANN001
    """durable REPL `server add` → 退出 → 重启（同 cwd, --approve-all-mcp）→ server 仍在 status。"""
    assert _DIRECT_EXEC.exists(), f"缺少测试 MCP server: {_DIRECT_EXEC}"
    cwd = str(tmp_path)

    # 进程 1：REPL durable `server add` → 落 tmp/.tfrobot/mcp.local.json → 退出（不清理，留给进程 2）。
    with cli_proc_factory(cwd=cwd, cleanup_durable=False) as c1:
        c1.sendline(f"server add {_survivor_json()}")
        out1 = strip_ansi(expect_prompt_stable(c1, quiet=0.5, max_wait=15.0))
        assert "survivor" in out1 or "added" in out1.lower() or "✅" in out1, f"server add 未确认成功:\n{out1}"

    # 落盘确证：进程 1 退出后，durable 声明确实留在 tmp/.tfrobot/mcp.local.json（未被清理）。
    local_mcp = tmp_path / ".tfrobot" / "mcp.local.json"
    assert local_mcp.exists(), "durable server add 应落 mcp.local.json"
    assert "survivor" in json.loads(local_mcp.read_text(encoding="utf-8"))["servers"], "声明应持久于 mcp.local.json"

    # 进程 2：全新 CLI 同一 cwd 重启（--approve-all-mcp 免 pending 审批提示）→ boot 读盘挂载 → survivor 仍在。
    with cli_proc_factory("--approve-all-mcp", cwd=cwd, cleanup_durable=False) as c2:
        c2.sendline("start all")
        expect_prompt_stable(c2, quiet=0.5, max_wait=15.0)
        c2.sendline("status")
        out2 = strip_ansi(expect_prompt_stable(c2, quiet=0.8, max_wait=12.0))
        assert "survivor" in out2, f"重启后 survivor 未存活（复活/存活守护回归）。status 输出:\n{out2}"
