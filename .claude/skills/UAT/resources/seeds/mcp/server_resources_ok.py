# -*- coding: utf-8 -*-
"""
UAT seed: MCP stdio Server — resources mode, happy path.

Axis: MC-RES-* happy
Mode: resources
Skill served: valid-skill-pkg (derived from seeds/_common/valid-skill-pkg)

启动：python server_resources_ok.py  （stdio transport）
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

SEED_FILE = Path(__file__).resolve()
SEEDS_ROOT = SEED_FILE.parent.parent
COMMON_SRC = SEEDS_ROOT / "_common" / "valid-skill-pkg"

SKILL_HOST = "seed.mc-res-ok.example.com"
SKILL_LEAF = "valid-skill-pkg"   # 与 _common/<x>/SKILL.md frontmatter.name 一致


def build_resources(work_dir: Path) -> list[types.Resource]:
    """根 + 全部子资源（不带 _meta.source）。"""
    root_uri = f"skill://{SKILL_HOST}/{SKILL_LEAF}"
    res: list[types.Resource] = [
        types.Resource.model_validate({
            "uri": root_uri,
            "name": SKILL_LEAF,
            "mimeType": "inode/directory",
            "_meta": {"source": "resources", "version": "1.0.0"},
        }),
    ]
    # 把 work_dir 下所有文件作为子资源 enumerate（相对路径展开）
    for path in sorted(work_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(work_dir).as_posix()
        res.append(types.Resource.model_validate({
            "uri": f"{root_uri}/{rel}",
            "name": path.name,
            "mimeType": "text/plain",
            # 子资源不带 _meta.source
        }))
    return res


def serve_content(work_dir: Path, uri_path: str) -> str:
    """resources/read：把 uri 路径 (`<leaf>/<rel...>`) 还原到 work_dir 下文件。"""
    parts = uri_path.split("/", 1)
    rel = parts[1] if len(parts) > 1 else ""
    if not rel:
        raise FileNotFoundError(f"empty rel path for uri_path={uri_path!r}")
    target = work_dir / rel
    if not target.is_file():
        raise FileNotFoundError(f"resource not found: {uri_path}")
    return target.read_text(encoding="utf-8")


async def run() -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="uat-seed-resources-ok-"))
    if COMMON_SRC.is_dir():
        shutil.copytree(COMMON_SRC, work_dir, dirs_exist_ok=True)
    try:
        server = Server(name="uat-seed-resources-ok", version="0.0.1", instructions="MC-RES happy")

        @server.list_resources()
        async def _list_resources() -> list[types.Resource]:
            return build_resources(work_dir)

        @server.read_resource()
        async def _read_resource(uri: types.AnyUrl) -> str:
            rest = str(uri).split("://", 1)[-1]
            _, _, uri_path = rest.partition("/")
            return serve_content(work_dir, uri_path)

        async with stdio_server() as (read_stream, write_stream):
            init_opts = server.create_initialization_options()
            await server.run(read_stream, write_stream, init_opts)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    anyio.run(run)
