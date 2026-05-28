# -*- coding: utf-8 -*-
# filename: server_<MODE>_<AXIS>.py
# UAT Seed: MCP stdio Server scaffold
#
# 用法 / Usage:
#   python server_<NAME>.py
#
# Transport: **stdio**（对齐 tests/integration_tests/computer/mcp_servers/ 现有模式）
#
# 启动协议 / Startup protocol:
#   - 仅占用 stdin/stdout（MCP JSON-RPC），不监听端口
#   - acceptance 通过 a2c-smcp MCPServerManager 的 StdioServerConfig 启动本脚本
#   - 进程退出 = stdio_server context 结束（无需信号处理）
#
# 资源契约 / Resource contract:
#   - SKILL 包根：Resource._meta = {"source": "<mounted|archive|resources>", ...}
#   - 子资源（仅 resources 模式）：不带 _meta.source
#
# 本文件是模板，复制后按 axis 改写 build_resources() 内的关键参数
# （mount_dir / archive_uri / archive_sha256 / subs 列表等）。

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

# ─── axis 配置占位符 / axis-specific placeholders ────────────────────────────
SEED_NAME = "<seed-name>"                  # 如 "server_resources_ok"
SEED_MODE = "<mounted|archive|resources>"
SEED_AXIS = "<axis id, e.g. MC-RES-01>"
SKILL_HOST = "seed.<axis>.example.com"
SKILL_LEAF = "<skill-name>"                 # 如 "valid-skill"（与 _common 派生源一致）

# 路径：种子脚本的相对位置 → seeds/_common/<x>
SEED_FILE = Path(__file__).resolve()
SEEDS_ROOT = SEED_FILE.parent.parent          # .../seeds/
COMMON_SRC = SEEDS_ROOT / "_common" / "valid-skill-pkg"


def build_resources(work_dir: Path) -> list[types.Resource]:
    """按 SEED_MODE 与 SEED_AXIS 构造 resources/list 返回。

    复制后**只改本函数**，其他装配逻辑不要动（保证种子结构一致）。
    """
    root_uri = f"skill://{SKILL_HOST}/{SKILL_LEAF}"

    if SEED_MODE == "mounted":
        # 注意 / NOTE：mounted 期望 Computer 端解链 + 拷进 staging
        # axis MC-MNT-01: 把 mount_dir 字段干掉；MC-MNT-02: mount_dir 指向不存在路径
        return [
            types.Resource.model_validate({
                "uri": root_uri,
                "name": SKILL_LEAF,
                "mimeType": "inode/directory",
                "_meta": {
                    "source": "mounted",
                    "mount_dir": str(work_dir),
                    "version": "1.0.0",
                },
            }),
        ]

    if SEED_MODE == "archive":
        # archive 模式依赖外部 HTTP 起 _archives/ 服务
        # acceptance.sh 在调用本脚本前会把 ARCHIVE_BASE 通过环境变量传入（见 mcp recipe）
        import os
        archive_base = os.environ.get("UAT_SEED_ARCHIVE_BASE", "")
        assert archive_base, "UAT_SEED_ARCHIVE_BASE env required for archive seeds"
        return [
            types.Resource.model_validate({
                "uri": root_uri,
                "name": SKILL_LEAF,
                "mimeType": "application/x-tar+gzip",
                "_meta": {
                    "source": "archive",
                    "archive_uri": f"{archive_base}/valid-1.0.0.tar.gz",
                    "archive_format": "tar.gz",
                    # axis MC-ARC-03 (bad_sha): 故意填错
                    "archive_sha256": "<expected_sha_or_intentionally_wrong>",
                    "version": "1.0.0",
                },
            }),
        ]

    if SEED_MODE == "resources":
        resources: list[types.Resource] = [
            types.Resource.model_validate({
                "uri": root_uri,
                "name": SKILL_LEAF,
                "mimeType": "inode/directory",
                "_meta": {"source": "resources", "version": "1.0.0"},
            }),
        ]
        # axis MC-RES-01 (no_subs): 不附加子资源
        # axis MC-RES-02 (path_escape): 把下面 uri 改成含 "../"
        for rel in ("SKILL.md", "scripts/run.py"):
            resources.append(types.Resource.model_validate({
                "uri": f"{root_uri}/{rel}",
                "name": rel,
                "mimeType": "text/plain",
                # 故意不带 _meta.source —— 协议契约：子资源不带 source 字段
            }))
        return resources

    raise ValueError(f"unknown SEED_MODE={SEED_MODE}")


def serve_content(work_dir: Path, uri_path: str) -> str | bytes:
    """resources/read 内容映射：把 uri path (`<leaf>/<rel>`) 还原到 work_dir 下文件。"""
    # 去掉首段 leaf
    parts = uri_path.split("/", 1)
    rel = parts[1] if len(parts) > 1 else parts[0]
    target = work_dir / rel
    if not target.is_file():
        raise FileNotFoundError(f"resource not found: {uri_path}")
    return target.read_text(encoding="utf-8")


async def run() -> None:
    """启动 stdio MCP server，按 SEED_MODE 暴露资源。"""
    # 准备工作目录（mounted: 提供给 _meta.mount_dir；resources: 作为 read 内容源）
    work_dir = Path(tempfile.mkdtemp(prefix=f"uat-seed-{SEED_NAME}-"))
    if COMMON_SRC.is_dir():
        shutil.copytree(COMMON_SRC, work_dir, dirs_exist_ok=True)

    try:
        server = Server(name=f"uat-seed-{SEED_NAME}", version="0.0.1", instructions=SEED_AXIS)

        @server.list_resources()
        async def list_resources() -> list[types.Resource]:  # type: ignore[no-redef]
            return build_resources(work_dir)

        @server.read_resource()
        async def read_resource(uri: types.AnyUrl) -> str | bytes:  # type: ignore[no-redef]
            # uri = skill://<host>/<leaf>/<rel...>
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
