# -*- coding: utf-8 -*-
"""
UAT seed harness: drive ``stage_mcp_skills`` against a single stdio MCP seed.

中文 / English:
    最小驱动 / Minimal driver. acceptance.sh 调用本脚本，把种子 stdio server 通过
    StdioServerConfig 注册到 MCPServerManager，再触发一次 ``stage_mcp_skills``，
    把执行结果（已注册 SKILL name 列表 / 异常 / Computer 日志）打到 stdout/stderr。

用法 / Usage:
    python run_staging.py --stdio-server <path-to-server.py> --home <skill-home>

接受可选环境变量：
    UAT_SEED_ARCHIVE_BASE   archive 模式种子用，传给 stdio server 子进程
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from mcp import StdioServerParameters

from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import stage_mcp_skills


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("uat-seed.run_staging")

    # 透传 archive 模式所需环境变量到子进程
    child_env: dict[str, str] | None = None
    arc = os.environ.get("UAT_SEED_ARCHIVE_BASE")
    if arc:
        child_env = {**os.environ, "UAT_SEED_ARCHIVE_BASE": arc}

    config = StdioServerConfig(
        name="seed",
        disabled=False,
        forbidden_tools=[],
        tool_meta={},
        server_parameters=StdioServerParameters(
            command=sys.executable,
            args=[str(args.stdio_server)],
            env=child_env,
            cwd=None,
        ),
    )

    manager = MCPServerManager(auto_connect=True)
    await manager.aadd_or_aupdate_server(config)

    registry = SkillRegistry()
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)

    try:
        names = await stage_mcp_skills(manager, registry, home, server_name="seed")
        log.info("STAGED_NAMES=%s", names)
        # 让 acceptance.sh 容易 grep
        print(f"STAGED_NAMES={names}", flush=True)
        # 注册到 registry 的 ref（路径 / 字段）也打一份
        for n in names:
            ref = registry.resolve(n)
            print(f"REGISTERED ref={ref}", flush=True)
        return 0
    except Exception as e:
        log.exception("staging failed: %s", e)
        return 2
    finally:
        await manager.aremove_server("seed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="UAT seed: drive stage_mcp_skills against a stdio seed server")
    ap.add_argument("--stdio-server", required=True, type=Path,
                    help="path to seed server .py")
    ap.add_argument("--home", required=True, type=Path,
                    help="SKILL Home absolute root")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(parse_args())) or 0)
