# `_helpers/full-protocol`

**用途**: 供 `full-protocol` UAT 场景复用的 Agent 驱动脚本

**提供**:
- `agent_protocol_driver.py` — 可复用的 Agent 测试驱动，自动执行 F-01~F-12 用例

**使用方式**:

1. 启动完整链路环境（Server → Computer → Agent），Computer 至少挂载一个 MCP server

2. Computer 加入 office 后运行 Agent 驱动：
   ```bash
   uv run python agent_protocol_driver.py \
     --port-file /tmp/a2c-uat-port \
     --office-id proto-uat-office \
     --computer-name <computer_name>
   ```

**覆盖的用例**:
- F-01: Agent joins office
- F-02: tool_call（自动发现可用工具并调用）
- F-04: Version handshake (implicit via connection)
- F-05: Version incompatibility rejection (a2c_version=99.0.0)
- F-07: get_config
- F-08: get_tools
- F-09: get_desktop（如无 window 资源则 skip）
- F-10: list_room
- F-11: leave_office
- F-03 / F-06 / F-12: 需要 Computer 侧触发，自动标记 SKIPPED

**注意事项**:
- F-02 会自动从 `get_tools` 发现的第一个工具发起调用
- F-05 会创建一个独立的 Socket.IO 客户端携带 `a2c_version=99.0.0`
- F-03（SKILL 通知）、F-06（断连重连）、F-12（tool_call_cancel）需要 Computer 侧配合触发，
  本驱动会标记为 SKIPPED，需在 UAT 执行期间由 Claude 手动触发后补验证
