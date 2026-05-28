# `_helpers/full-protocol-supplement`

**用途**: 供 `full-protocol` UAT 场景补测 F-03 / F-06 / F-12 三个需 Computer 侧配合或
特殊时序的用例。补充 `full-protocol` 主驱动（仅覆盖 F-01/02/04/05/07/08/09/10/11）。

**提供**:
- `f03_notify_broadcast.py` — Agent 监听 `notify:update_config`，验证广播链路
- `f12_tool_call_cancel.py` — Agent 发送 `slow_echo` tool_call 后立即 cancel
- `f06_disconnect_reconnect.py` — Agent 发送 tool_call，Computer 被 kill 后重连
- `slow_tool_server.py` — 慢速 MCP Server（`slow_echo` 工具，10 秒延迟），供 F-06/F-12 使用

**前置依赖**:
- `seeds/mcp/binary_image_tool_server.py` — F-06 Phase 2 使用 `big_image` 工具
- `seeds/mcp/server_with_window_resources.py` — 如需 F-09 desktop 数据
- 运行中的 Server + Computer 环境（端口写入 `/tmp/a2c-uat-port`）
- Computer 已加入 office，且挂载含 `slow_tool_server.py` 的 MCP server（需 `default_tool_meta: {auto_apply: true}`）

**使用方式**:

```bash
# F-03: 通知广播（Computer CLI 执行 `notify update` 触发）
uv run python f03_notify_broadcast.py --port-file /tmp/a2c-uat-port --office-id test-office-001

# F-12: tool_call_cancel
uv run python f12_tool_call_cancel.py --port-file /tmp/a2c-uat-port --office-id test-office-001 --computer-name proto-comp-001

# F-06: 断连重连（需在 Agent 打印 KILL_SIGNAL_SENT 后立即 kill Computer）
uv run python f06_disconnect_reconnect.py --port-file /tmp/a2c-uat-port --office-id test-office-001 --computer-name proto-comp-001
```

**slow_tool_server.py MCP 配置**（需 `auto_apply` 跳过二次确认）:
```json
{
  "name": "uat-slow-tool",
  "type": "stdio",
  "disabled": false,
  "default_tool_meta": {"auto_apply": true},
  "server_parameters": {
    "command": "uv",
    "args": ["run", "python", "<path>/slow_tool_server.py"],
    "encoding": "utf-8", "encoding_error_handler": "strict"
  }
}
```

**覆盖的用例**:
- F-03: `notify:update_config` 广播验证（注：场景要求 `notify:update_skills`，但无 marketplace 时用 `notify:update_config` 替代）
- F-06: Computer 断连后重连（Phase 1 飞行中断连 + Phase 2 重连后调用）
- F-12: `tool_call_cancel` 取消机制验证

**注意事项**:
- F-06 需手动在 `KILL_SIGNAL_SENT` 后 kill Computer 进程（`kill -9 <pid>`）
- F-06 Phase 2 需重启 Computer（`--auto-reconnect`）并在 15 秒等待内完成
- F-12 当前 cancel 返回 None（SUT bug，已提 GitHub Issue #96）
