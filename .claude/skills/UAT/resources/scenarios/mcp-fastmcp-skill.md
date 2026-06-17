# 场景：mcp-fastmcp-skill

## 测试目标

验证 FastMCP-style Skills Provider 经 MCP `skill://` 资源暴露的 skill，能否被 smcp-computer 的
`restage_mcp_skills` 物化注册并经 `Computer.get_skills()` 收集。守护 AS-40 的两条契约：

- **MF-01 可注册形状**：provider 暴露 `_meta.source=resources` 根 + 子资源（`SKILL.md`/`reference.md`）→
  current SDK **无需改码**即注册为 `mcp:<server>:<skill>`。
- **MF-02 裸 FastMCP 布局**：裸 `skill://<name>/SKILL.md`（无 `_meta.source` 根）当前**不注册**，
  由 provider 侧适配解决（暴露可注册形状）。SDK 应**静默跳过**，不误报 invalid/skipped。

> 依据 / Basis: AS-40 comment 13849。`_meta.source` 是 A2C 自家协议（`skill.md §3`）的物化标记，
> **不是** MCP 标准；FastMCP 的 `skill://<name>/SKILL.md`+`_manifest` 是另一套约定。两者均建于标准
> MCP `resources/list` + `resources/read` 之上。

## 类型

集成测试（gated）—— **非 CLI/tmux 可观测**，见下方局限说明。

## ⚠️ CLI / tmux 局限说明（为何不是 CLI-only 用例）

`restage_mcp_skills` 仅在 Computer `boot_up` 时触发；而 MCP server 的批准/连接发生在 boot **之后**。
故非交互 CLI（`a2c-computer skill list --source mcp`）在 boot 时尚无活跃 MCP 连接，**观测不到** live
MCP skill 注册（与 `skill-discovery.md` D-03「非交互模式返回空列表」一致）。因此本场景的可注册性验证
落在 Python 集成测试，而非 tmux 终端交互。

## 验证载体（单一真源）

| 类型 | 路径 |
|------|------|
| stdio fixture | `tests/integration_tests/computer/mcp_servers/fastmcp_skill_stdio_server.py` |
| 集成测试 | `tests/integration_tests/computer/skills/test_fastmcp_skills_integration.py` |
| UAT seed 索引 | `.claude/skills/UAT/resources/seeds/mcp/fastmcp-skill-server/README.md` |

一台 fixture 同时暴露 MF-01 可注册形状（`skill://fastmcp.demo.example/fastmcp-demo` + 子资源）与
MF-02 裸布局（`skill://bare-demo/SKILL.md`），用一次 boot 守护两个契约。

## 测试用例

### MF-01: 可注册形状被 get_skills 收集

- **优先级**: P0
- **步骤**:
  1. 经真 stdio fixture 启动 `MCPServerManager`（server 名 `fastmcp-skill-test`）
  2. 注入 Computer，调用 `await comp._restage_mcp_skills()`
  3. 读取 `comp.get_skills()`
- **预期结果**:
  - `registered` 含 `mcp:fastmcp-skill-test:fastmcp-demo`
  - `get_skills()` 名单含同名 skill；`source == "mcp:fastmcp-skill-test"`，`version == "1.0.0"`
  - 物化包根落盘 `SKILL.md` 与 `reference.md`（子资源逐个 `resources/read` 还原）

### MF-02: 裸 FastMCP 布局不注册（当前契约）

- **优先级**: P0
- **步骤**: 同 MF-01 boot；检查裸 `skill://bare-demo/SKILL.md` 的处理
- **预期结果**:
  - `registered` 与 `get_skills()` 均**不含**任何 `bare-demo` 变体
  - 裸布局仅静默跳过，**不**误报 invalid/skipped；可注册形状（MF-01）仍正常入册，未被连累

## 执行

```bash
uv run pytest tests/integration_tests/computer/skills/test_fastmcp_skills_integration.py -v
```

## 清理

集成测试使用 `tmp_path`，自动清理；fixture 仅占 stdin/stdout，astop_all 后子进程退出，无残留。
