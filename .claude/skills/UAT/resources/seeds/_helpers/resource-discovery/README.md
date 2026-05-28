# `_helpers/resource-discovery`

**用途**: 供 `resource-discovery` UAT 场景复用的 Agent 驱动脚本

**提供**:
- `agent_resource_driver.py` — 可复用的 Agent 测试驱动，自动执行 R-01~R-05 用例

**前置条件**:

Computer 需挂载两个 MCP server seed（通过 `--approve-all-mcp` 或 CLI `server add`）：

1. **window-resource-server**（有 resources 能力）:
   ```bash
   server add @seeds/mcp/server_with_window_resources.py
   ```

2. **no-resources-server**（无 resources 能力）:
   ```bash
   server add @seeds/mcp/server_no_resources_capability.py
   ```

**使用方式**:

1. 启动完整链路环境，Computer 挂载上述两个 MCP server

2. Computer 加入 office 后运行 Agent 驱动：
   ```bash
   uv run python agent_resource_driver.py \
     --port-file /tmp/a2c-uat-port \
     --office-id res-uat-office \
     --computer-name <computer_name>
   ```

**覆盖的用例**:
- R-01: get_resources 成功返回 3 个资源（window:// × 2 + config:// × 1）
- R-02: window:// 资源含 annotations（priority / audience / fullscreen）
- R-03: 不存在的 MCP Server → 4014
- R-04: 无 resources 能力的 server → 4015
- R-05: 不存在的 Computer → 路由失败

**参数说明**:
- `--window-server`: 有 resources 能力的 MCP server 名称（默认 `window-resource-server`）
- `--no-resources-server`: 无 resources 能力的 MCP server 名称（默认 `no-resources-server`）
