# Recipe: `mcp/` — 可执行 MCP Server 种子

> 每条种子是一个**独立可执行**的 Python 脚本，启动后通过 `skill://` 资源暴露一个/多
> 个 SKILL，模拟 MCP source 的三种模式（mounted / archive / resources）+ 各类失败维度。

## 目录形态

```
seeds/mcp/
├── _archives/                       ← archive 模式的归档库
│   ├── build.sh                     ← 重建脚本（从 _common/<x> 打包）
│   ├── manifest.json                ← {name → {sha256, source, axis}}
│   └── *.tar.gz / *.zip
├── _http_fixture.py                 ← 本地 HTTP 服务，serve _archives/
├── _helpers/run_staging.py          ← 直接驱动 stage_mcp_skills 的最小 harness
├── server_<mode>_<axis>.py          ← 种子脚本
└── server_<mode>_<axis>.acceptance.sh
```

## 种子脚本骨架

每个种子脚本基于 `mcp` 官方 Python SDK 的 **lowlevel + stdio** API（与仓库
`tests/integration_tests/computer/mcp_servers/` 现有 stdio servers 同源）。模板在
`resources/templates/mcp-server-scaffold.py`，核心约定：

```python
# 启动协议
# $ python server_<name>.py
#
# - **stdio** transport：仅占用 stdin/stdout（MCP JSON-RPC），不监听端口
# - acceptance 通过 a2c-smcp MCPServerManager 的 StdioServerConfig 启动本脚本
# - 进程退出 = stdio_server context 结束，无需 SIGTERM 处理
# - archive 模式额外通过环境变量 UAT_SEED_ARCHIVE_BASE 传入 HTTP fixture URL
```

种子在 `resources/list` 返回的资源**必须**遵守：

- **SKILL 包根**：`_meta = {"source": "<mounted|archive|resources>", ...}`
- **子资源（仅 resources 模式才有）**：**不要**带 `_meta.source`
- URI 用 `skill://<host>/<path>` 形式；`<host>` 推荐 `seed.<axis>.example.com`，
  `<path>` = SKILL.md frontmatter.name

## 三模式核心实现要点

### mounted 模式

```python
# 1) 在种子启动时把 _common/<src> 拷贝到 self._mount_dir = tempfile.mkdtemp()
# 2) 在 resources/list 暴露：
Resource(
    uri="skill://seed.mounted.example.com/valid-skill",
    name="valid-skill",
    _meta={
        "source": "mounted",
        "mount_dir": self._mount_dir,      # 绝对路径
        "version": "1.0.0",
    },
)
# 3) 失败种子的差异：
#    - mounted_missing_dir: 把 mount_dir 字段干掉
#    - mounted_nonexistent: mount_dir = "/nonexistent/path"
#    - mounted_symlink_in_tree: 在 _mount_dir 里建一个 symlink，看 staging 是否解链
```

### archive 模式

```python
# 1) 启动时依赖 --archive-base 知道 _http_fixture.py 的根 URL
# 2) 在 resources/list 暴露：
Resource(
    uri="skill://seed.archive.example.com/valid-skill",
    name="valid-skill",
    _meta={
        "source": "archive",
        "archive_uri": f"{self._archive_base}/valid-1.0.0.tar.gz",
        "archive_format": "tar.gz",
        "archive_sha256": "<expected sha256>",  # 与 _archives/manifest.json 一致
        "version": "1.0.0",
    },
)
# 3) 失败种子的差异：
#    - archive_bad_sha:        archive_sha256 故意填错值
#    - archive_bomb:           archive_uri 指向 tar-bomb.tar.gz
#    - archive_path_traversal: archive_uri 指向 path-traversal.tar.gz
#    - archive_uri_unreachable: archive_uri = "http://127.0.0.1:1/nonexistent.tgz"
```

### resources 模式

```python
# 1) 启动时把 _common/<src> 拷贝到 self._serve_root = tempfile.mkdtemp()
# 2) 在 resources/list 暴露 N+1 条资源：
#    - 1 条 SKILL 包根（带 _meta.source = "resources"）
#    - N 条子资源（无 _meta.source）
resources = [
    Resource(
        uri="skill://seed.resources.example.com/valid-skill",
        name="valid-skill",
        _meta={"source": "resources", "version": "1.0.0"},
    ),
    # 子资源 —— 不带 _meta.source
    Resource(uri="skill://seed.resources.example.com/valid-skill/SKILL.md", name="SKILL.md", mimeType="text/markdown"),
    Resource(uri="skill://seed.resources.example.com/valid-skill/scripts/run.py", name="run.py", mimeType="text/x-python"),
]
# 3) resources/read 按 uri 路径转 self._serve_root + relative path 返内容
# 4) 失败种子的差异：
#    - resources_no_subs:     只暴露根，不暴露子（list 时不附子资源）
#    - resources_path_escape: 暴露子资源 uri = "skill://.../valid-skill/../escape.txt"
#    - resources_subs_carry_source_meta: 在子资源 _meta 也加 source 字段（违反协议）
```

## stdio 启动约定

stdio transport 没有端口/就绪信号：父进程通过 MCPServerManager 的
`StdioServerConfig` 用 subprocess + JSON-RPC over pipe 起种子；进程自然结束即可。

```python
# 模板里关键代码（lowlevel + stdio）
async def run():
    work_dir = prepare_workdir()              # 从 _common/<x> 拷贝
    try:
        server = Server(name=..., version="0.0.1", instructions=SEED_AXIS)
        @server.list_resources()
        async def list_resources(): return build_resources(work_dir)
        @server.read_resource()
        async def read_resource(uri): return serve_content(work_dir, uri.path)
        async with stdio_server() as (rs, ws):
            await server.run(rs, ws, server.create_initialization_options())
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

if __name__ == "__main__":
    anyio.run(run)
```

## 标准 acceptance.sh

完整骨架见 `guides/acceptance-design.md §5`。mcp 种子的 acceptance 必备：

1. （仅 archive 模式）起 `_http_fixture.py` serve `_archives/`
2. 用 `_helpers/run_staging.py` 通过 stdio 起种子 server，触发 `stage_mcp_skills`
3. 比对日志关键字 + staging 目录形态
4. trap EXIT 清理所有子进程 + tmpdir

`_helpers/run_staging.py` 是个**约 40 行**的最小驱动（stdio）：

```python
# seeds/mcp/_helpers/run_staging.py
"""通过 StdioServerConfig 起种子 + 驱动 stage_mcp_skills。"""
import argparse, asyncio, logging, sys
from pathlib import Path
from mcp import StdioServerParameters
from a2c_smcp.computer.mcp_clients.manager import MCPServerManager
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.staging import stage_mcp_skills

async def amain(args):
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
    manager = MCPServerManager()
    config = StdioServerConfig(
        name="seed",
        disabled=False,
        forbidden_tools=[],
        tool_meta={},
        server_parameters=StdioServerParameters(
            command=sys.executable,
            args=[str(args.stdio_server)],
            env=None,
            cwd=None,
        ),
    )
    await manager.aadd_or_aupdate_server(config)
    registry = SkillRegistry()
    home = Path(args.home); home.mkdir(parents=True, exist_ok=True)
    names = await stage_mcp_skills(manager, registry, home, server_name="seed")
    logging.info("registered: %s", names)
    await manager.aremove_server("seed")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio-server", required=True, type=Path)
    ap.add_argument("--home", required=True)
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)) or 0)
```

## `_archives/build.sh` & `manifest.json`

```bash
#!/usr/bin/env bash
# seeds/mcp/_archives/build.sh — 从 _common/<x> 重建所有归档
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
COMMON="$(cd "$HERE/../../_common" && pwd)"
OUT="$HERE"
manifest="$OUT/manifest.json"
declare -A SOURCES=(
  [valid-1.0.0.tar.gz]=valid-skill-pkg
  [valid-1.0.0.zip]=valid-skill-pkg
  # bad-sha 等"内容 = valid，但 sha 故意错"在种子脚本里 fake，归档本身不动
  [tar-bomb.tar.gz]=__synthesized__
  [tar-too-many-members.tar.gz]=__synthesized__
  [path-traversal.tar.gz]=__synthesized__
  [symlink-escape.tar.gz]=__synthesized__
)
# valid 类：直接 tar 打包
( cd "$COMMON/valid-skill-pkg" && tar czf "$OUT/valid-1.0.0.tar.gz" . )
( cd "$COMMON/valid-skill-pkg" && zip -r -q "$OUT/valid-1.0.0.zip" . )
# 攻击面类：用 Python 代码合成（不留二进制黑盒）
python "$HERE/_synthesize_attacks.py" --out "$OUT"
# 写 manifest sha256
python "$HERE/_write_manifest.py" --out "$OUT" --manifest "$manifest"
```

`manifest.json` 形如：

```json
{
  "version": 1,
  "items": {
    "valid-1.0.0.tar.gz": {
      "sha256": "<hex>",
      "source": "_common/valid-skill-pkg",
      "axis": "happy"
    },
    "tar-bomb.tar.gz": {
      "sha256": "<hex>",
      "source": "__synthesized__",
      "axis": "MC-ARC-04"
    }
  }
}
```

**审查规则**：所有 `_archives/*.tar.gz` / `*.zip` **不入 git**（`.gitignore`）；
入库的只有 `build.sh` / `_synthesize_attacks.py` / `_write_manifest.py` /
`manifest.json`（manifest 是文本，可 review）。CI / 本地 audit：`build.sh` → 重建
→ 比对 sha256。

## 命名一览（典型种子）

| name | mode | axis |
|---|---|---|
| `server_mounted_ok` | mounted | happy |
| `server_mounted_missing_dir` | mounted | MC-MNT-01 |
| `server_mounted_nonexistent` | mounted | MC-MNT-02 |
| `server_archive_ok` | archive | happy |
| `server_archive_bad_sha` | archive | MC-ARC-03 |
| `server_archive_bomb` | archive | MC-ARC-04 |
| `server_archive_path_traversal` | archive | MC-ARC-06 |
| `server_archive_uri_unreachable` | archive | MC-ARC-09 |
| `server_resources_ok` | resources | happy |
| `server_resources_no_subs` | resources | MC-RES-01 |
| `server_resources_path_escape` | resources | MC-RES-02 |
| `server_mixed_modes` | mixed | happy（三模式各 1 个 SKILL） |
| `server_cursor_paginated` | resources（或任意） | MC-GEN-02 |
| `server_no_resources_cap` | N/A | MC-GEN-01 |
| `server_name_collision` | 任意 | MC-GEN-04 |

## 创建检查清单

- [ ] 种子脚本接受 `--port-file` 并在 ready 后写入端口；接受 SIGTERM 干净退出
- [ ] `resources/list` 输出严格遵守"根带 `_meta.source` / 子资源不带" 契约
- [ ] failure 种子的违规点**只有一处**（与 axis 对齐）
- [ ] acceptance.sh 完整：起 fixture → 起 server → 跑 staging → 断言 → 清理
- [ ] acceptance 在 happy 种子上检查"成功落盘 + 注册 + SKILL.md 存在"；在 failure
      种子上检查"特定日志关键字 + 未注册产物"
- [ ] 若依赖 `_archives/`：build.sh 能重建 + manifest.json sha256 匹配
- [ ] `seeds/README.md` 索引登记
- [ ] 不抢占固定端口、不留 `/tmp` 残留
