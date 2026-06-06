# -*- coding: utf-8 -*-
"""
UAT seed harness: drive ``stage_marketplace_skills`` against a local bare repo.

用法 / Usage:
    python run_marketplace_stage.py --name <mp-name> --bare <path-to.git> --home <skill-home>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import stage_marketplace_skills


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
    log = logging.getLogger("uat-seed.run_marketplace_stage")

    registry = SkillRegistry()
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "A2C_SKILL_HOME": str(home)}
    src = {"type": "git", "url": f"file://{args.bare}"}

    try:
        names = await stage_marketplace_skills(
            args.name, src, registry, home, env=env,
        )
        log.info("STAGED_NAMES=%s", names)
        print(f"STAGED_NAMES={names}", flush=True)
        for n in names:
            ref = registry.resolve(n)
            print(f"REGISTERED ref={ref}", flush=True)
        return 0
    except Exception as e:
        log.exception("marketplace staging failed: %s", e)
        return 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="UAT seed: drive stage_marketplace_skills")
    ap.add_argument("--name", required=True, help="marketplace name (kebab-case)")
    ap.add_argument("--bare", required=True, type=Path, help="local bare repo path")
    ap.add_argument("--home", required=True, type=Path, help="SKILL Home root")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(parse_args())) or 0)
