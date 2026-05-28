# `mcp/` — 可执行 MCP Server 种子（stdio）

> 每条种子是一个 Python 脚本，通过 **stdio** transport（与仓库现有
> `tests/integration_tests/computer/mcp_servers/` 同源）暴露 `skill://` 资源，模拟
> mounted / archive / resources 三模式 × 各类失败维度。

## 启动协议

```bash
python server_<name>.py
```

仅占用 stdin/stdout（MCP JSON-RPC），不监听端口。acceptance 通过
`a2c_smcp.computer.mcp_clients.manager.MCPServerManager` 的 `StdioServerConfig`
启动本脚本（见 `_helpers/run_staging.py`）。

archive 模式额外通过环境变量 `UAT_SEED_ARCHIVE_BASE` 接收 HTTP fixture 的 URL。

## 子目录与文件

```
mcp/
├── _archives/       ← archive 模式归档库（build.sh / manifest.json / *.tar.gz｜不入 git）
├── _helpers/
│   └── run_staging.py   ← stdio 模式驱动 stage_mcp_skills 的最小 driver
├── server_*.py      ← 种子脚本（snake_case，文件名 = 种子 name）
└── server_*.acceptance.sh  ← 与种子同名，配对存在
```

## 索引

参见上级 [`seeds/README.md`](../README.md) `mcp/` 节。

## 二进制归档策略（强制）

- `.tar.gz` / `.zip` **不**入 git（`.gitignore`）
- 由 `_archives/build.sh` 重建；CI / 本地 audit 时比对 `_archives/manifest.json` sha256
- 攻击面归档由代码合成，**不**留二进制黑盒

## 详细规范

[`uat-seed/resources/recipes/mcp.md`](../../../../uat-seed/resources/recipes/mcp.md)
