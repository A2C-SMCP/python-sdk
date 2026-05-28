# `mcp/` — 可执行 MCP Server 种子

> 每条种子是一个 Python 脚本，启动后通过 MCP `resources/list` 暴露 `skill://` 资源，
> 模拟 mounted / archive / resources 三模式 × 各类失败维度。

## 启动协议（所有脚本共同遵守）

```bash
python server_<name>.py --port-file <path> [--archive-base http://127.0.0.1:<P>]
```

- 端口绑定成功后立即将 port 数字写入 `--port-file`
- stdout 打印 `MCP_READY port=<P>` 并 flush
- SIGTERM / SIGINT 时清理临时目录、关闭 sockets

## 子目录与文件

```
mcp/
├── _archives/       ← archive 模式归档库（build.sh / manifest.json / *.tar.gz｜不入 git）
├── _http_fixture.py ← 本地 HTTP serve _archives/
├── _helpers/        ← acceptance 共享的最小驱动（run_staging.py 等）
├── server_*.py      ← 种子脚本（snake_case，文件名 = 种子 name）
└── server_*.acceptance.sh  ← 与种子同名，配对存在
```

## 索引

参见上级 [`seeds/README.md`](../README.md) `mcp/` 节。

## 二进制归档策略（强制）

- `.tar.gz` / `.zip` **不**入 git（`.gitignore`）
- 由 `_archives/build.sh` 重建；CI / 本地 audit 时比对 `_archives/manifest.json` sha256
- 攻击面归档（path-traversal / tar-bomb / symlink-escape）由
  `_synthesize_attacks.py` 用代码合成，**不**留二进制黑盒

## 详细规范

[`uat-seed/resources/recipes/mcp.md`](../../../uat-seed/resources/recipes/mcp.md)
