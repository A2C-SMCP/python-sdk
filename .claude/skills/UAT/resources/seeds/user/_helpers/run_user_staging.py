# -*- coding: utf-8 -*-
"""
UAT seed harness: drive ``stage_user_skills`` against a SKILL Home.

用法 / Usage:
    python run_user_staging.py --home <skill-home> [--workdirs <wd1> <wd2> ...]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import stage_user_skills


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
    log = logging.getLogger("uat-seed.run_user_staging")

    ap = argparse.ArgumentParser(description="UAT seed: drive stage_user_skills")
    ap.add_argument("--home", required=True, type=Path, help="SKILL Home root")
    ap.add_argument("--workdirs", nargs="*", type=Path, default=[],
                    help="Registered workdirs in registration order")
    args = ap.parse_args(argv)

    registry = SkillRegistry()
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)

    names = stage_user_skills(registry, home, workdirs=tuple(args.workdirs))
    log.info("STAGED_NAMES=%s", names)
    print(f"STAGED_NAMES={names}", flush=True)
    for n in names:
        ref = registry.resolve(n)
        print(f"REGISTERED ref={ref}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
