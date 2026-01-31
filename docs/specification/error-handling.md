# A2C-SMCP 错误处理规范

> **状态**: 草案
>
> 本章节目前为草案状态，具体错误码与结构仍在演进中。

## 概述

A2C-SMCP 协议定义了统一的错误处理机制，确保 Agent、Server、Computer 之间能够正确传递和处理错误信息。

## 错误码定义

### 通用错误码

| 代码 | 名称 | 含义 | 典型触发场景 |
|------|------|------|-------------|
| 400 | Bad Request | 无效请求格式 | 数据结构校验失败、字段缺失或类型错误 |
| 401 | Unauthorized | 未授权 | 认证失败、Token 无效 |
| 403 | Forbidden | 权限违规 | 跨房间访问、Agent 独占冲突、未授权操作 |
| 404 | Not Found | 资源不存在 | 工具或 Computer 不存在、MCP 配置缺失 |
| 408 | Timeout | 请求超时 | 工具调用超过约定超时时间未返回 |
| 500 | Internal Error | 内部错误 | Server 或 Computer 端逻辑异常 |

### 工具调用错误码

| 代码 | 名称 | 含义 |
|------|------|------|
| 4001 | Tool Not Found | 工具不存在 |
| 4002 | Tool Disabled | 工具被禁用 |
| 4003 | Tool Execution Failed | 工具执行失败 |
| 4004 | Tool Timeout | 工具执行超时 |
| 4005 | Tool Requires Confirmation | 工具需要二次确认 |

### 房间管理错误码

| 代码 | 名称 | 含义 |
|------|------|------|
| 4101 | Room Full | 房间已有 Agent |
| 4102 | Room Not Found | 房间不存在 |
| 4103 | Not In Room | 未加入房间 |
| 4104 | Cross Room Access | 跨房间访问被拒绝 |

## 错误响应格式

### 标准错误响应

```json
{
  "error": {
    "code": 404,
    "message": "请求的工具不存在",
    "details": {
      "tool_name": "invalid-tool-name",
      "computer": "my-computer"
    }
  }
}
```

### 字段说明

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `error.code` | int | 是 | 错误码 |
| `error.message` | str | 是 | 人类可读的错误描述 |
| `error.details` | object | 否 | 结构化调试信息 |

### 安全注意事项

`details` 字段**禁止**包含以下敏感信息：

- API 密钥或 Token
- 内部 IP 地址或端口
- 数据库连接信息
- 用户密码或凭证
- 堆栈跟踪（生产环境）

## 事件级错误处理

### server:join_office 响应

```python
# 成功
(True, None)

# 失败
(False, "Room already has an agent")
```

### client:tool_call 响应

工具调用使用 MCP 的 `CallToolResult` 结构返回结果：

```python
class CallToolResult:
    content: list[TextContent | ImageContent | EmbeddedResource]
    isError: bool  # 是否为错误结果
    meta: dict     # 元数据
```

当 `isError=True` 时，`content` 中包含错误信息。

## 超时处理

### Agent 端超时

Agent 在发起工具调用时指定 `timeout`：

```python
result = await agent.emit_tool_call(
    computer="my-computer",
    tool_name="slow_tool",
    params={},
    timeout=30  # 30 秒超时
)
```

超时后，Agent 应：

1. 发送 `server:tool_call_cancel` 取消请求
2. 返回超时错误给调用方

```python
# 超时返回示例
CallToolResult(
    content=[TextContent(text="Tool call timeout")],
    isError=True,
    meta={"timeout": True}
)
```

### Server 端超时

Server 在转发请求时应设置合理的超时：

- 使用 Agent 请求中的 `timeout` 值
- 添加少量缓冲时间（如 5 秒）

### Computer 端超时

Computer 应在工具执行超时时：

1. 尝试中断工具执行
2. 返回超时错误

## 重试策略

### 建议的重试策略

| 错误类型 | 是否重试 | 策略 |
|---------|---------|------|
| 400 Bad Request | 否 | 修复请求后重试 |
| 401 Unauthorized | 否 | 重新认证后重试 |
| 403 Forbidden | 否 | 不重试 |
| 404 Not Found | 否 | 不重试 |
| 408 Timeout | 可选 | 指数退避重试 |
| 500 Internal Error | 可选 | 指数退避重试 |

### 指数退避示例

```python
async def retry_with_backoff(func, max_retries=3):
    for i in range(max_retries):
        try:
            return await func()
        except TimeoutError:
            if i == max_retries - 1:
                raise
            wait_time = (2 ** i) + random.uniform(0, 1)
            await asyncio.sleep(wait_time)
```

## 错误传播

### 工具调用错误传播链

```
MCP Server → Computer → Server → Agent
     │           │          │        │
     └───────────┴──────────┴────────┘
                错误信息保留
```

每一层应：

1. 记录错误日志
2. 将错误信息向上传播
3. 不丢失原始错误细节

### 日志记录建议

```python
# 推荐的日志格式
logger.error(
    "Tool call failed",
    extra={
        "req_id": req_id,
        "tool_name": tool_name,
        "computer": computer,
        "error_code": error.code,
        "error_message": error.message
    }
)
```

## TODO

> 以下功能尚未实现，计划在后续版本中完善：

- [ ] 统一的错误码定义文件
- [ ] 错误码国际化支持
- [ ] 错误追踪 ID（trace_id）
- [ ] 错误统计和监控接口

## 参考

- MCP CallToolResult: https://github.com/modelcontextprotocol/specification
- Socket.IO 错误处理: https://socket.io/docs/v4/handling-errors/
