# seed: `fastmcp-skill-server` — FastMCP-style Skills Provider（AS-40）

> **单一真源 / single source of truth**：本 seed **不**复制 fixture 脚本，直接指向集成测试 fixture，
> 避免两处 stdio server 实现漂移。

## 指向

| 类型 | 路径 |
|------|------|
| stdio server fixture | [`tests/integration_tests/computer/mcp_servers/fastmcp_skill_stdio_server.py`](../../../../../../tests/integration_tests/computer/mcp_servers/fastmcp_skill_stdio_server.py) |
| 集成测试（验收方法） | [`tests/integration_tests/computer/skills/test_fastmcp_skills_integration.py`](../../../../../../tests/integration_tests/computer/skills/test_fastmcp_skills_integration.py) |
| UAT 场景 | [`scenarios/mcp-fastmcp-skill.md`](../../scenarios/mcp-fastmcp-skill.md) |

## 暴露形状

一台 stdio server（名 `fastmcp-skill-test`）同时暴露：

```
# MF-01 可注册形状 / registrable（_meta.source=resources 根 + 子资源）
skill://fastmcp.demo.example/fastmcp-demo            _meta.source=resources, version=1.0.0
skill://fastmcp.demo.example/fastmcp-demo/SKILL.md
skill://fastmcp.demo.example/fastmcp-demo/reference.md

# MF-02 裸 FastMCP 布局 / bare（无 _meta.source 根，当前不注册）
skill://bare-demo/SKILL.md
```

## 验收方法（可执行）

```bash
uv run pytest tests/integration_tests/computer/skills/test_fastmcp_skills_integration.py -v
```

- MF-01 → `get_skills()` 收集 `mcp:fastmcp-skill-test:fastmcp-demo`（current SDK 无需改码）
- MF-02 → 裸布局不注册、不误报；由 provider 侧适配（暴露可注册形状）解决

## 背景

AS-40 / comment 13849：`_meta.source` 为 A2C 自家协议物化标记（非 MCP 标准）；FastMCP 的
`skill://<name>/SKILL.md`+`_manifest` 为另一套约定。结论是 **provider 侧适配到可注册形状**，
SDK 侧仅新增测试守护契约，零 src 改动。
