# `marketplace/plugin-with-bundled-mcp`

**Axis**: MK-BMC-01 (plugin 捆绑 MCP server)

**形态**: marketplace 工作树，plugin 含 `mcp-servers/*.json` 捆绑配置

**用途**: 供 `plugin-management` UAT 场景复用

**提供**:
- marketplace 名: `mp-bundled-mcp`
- plugin `foo`，含 1 个 skill `valid-skill-pkg`（派生自 `_common`）+ 1 个捆绑 MCP server `figma-mcp`
- 捆绑 MCP server 配置: `plugins/foo/mcp-servers/figma-mcp.json`

**期望被测行为**:
- `plugin install foo@mp-bundled-mcp` 成功
- `plugin info` 显示 `bundledMcpServers` 含 `figma-mcp`
- `plugin uninstall foo@mp-bundled-mcp` 级联移除捆绑 MCP server
