# 常见问题 (FAQ)

## 工具调用相关

### Q: 工具调用返回 "当前工具需要调用前进行二次确认" 错误

**问题描述**:

```json
{
  "content": [
    {
      "type": "text",
      "text": "当前工具需要调用前进行二次确认，但客户端目前没有实现二次确认回调方法"
    }
  ],
  "isError": true
}
```

**原因**: 被调用的工具在配置中启用了二次确认，但客户端未实现确认回调。

**解决方案**:

方案 A: 为特定工具关闭二次确认
```json
{
  "tool_meta": {
    "工具名": {
      "auto_apply": true
    }
  }
}
```

方案 B: 为所有工具关闭二次确认（使用默认配置）
```json
{
  "default_tool_meta": {
    "auto_apply": true
  }
}
```

### Q: 工具调用超时

**可能原因**:
- 工具执行时间过长
- Computer 不在线
- 网络连接问题

**解决方案**:
1. 增加 `timeout` 参数值
2. 检查 Computer 是否正常运行
3. 检查网络连接

### Q: 找不到工具

**可能原因**:
- MCP Server 未启动
- 工具名称错误
- 工具在 `forbidden_tools` 中

**解决方案**:
1. 使用 `tools` 命令查看可用工具列表
2. 检查 MCP Server 状态：`status`
3. 检查配置中的 `forbidden_tools`

---

## Desktop 相关

### Q: 一个 MCP Server 能暴露多少个 window:// 资源？

**答**: 没有限制。一个 MCP Server 可以暴露多个 `window://` 资源，通过 `window://{host}/{path}` 区分不同窗口。Computer 会从 `resources/list` 返回中筛选所有 `window://` 资源。

### Q: Desktop 是如何组装的？

**组装流程**:
1. 跨 MCP Server 汇总所有 `window://` 资源
2. 读取每个 window 的内容
3. 按策略排序/裁剪
4. 渲染为字符串列表

**排序规则**:
- **size 截断**: `desktop_size` 参数控制数量上限
- **server 优先级**: 最近调用过工具的 Server 优先
- **窗口排序**: 同一 server 内按 `priority` 降序
- **fullscreen 规则**: fullscreen 窗口独占该 Server 的显示

### Q: 为什么 Desktop 返回为空？

**可能原因**:
- 没有 MCP Server 暴露 `window://` 资源
- `desktop_size` 设置为 0 或负数
- MCP Server 未启动

---

## 连接相关

### Q: Socket.IO 连接失败

**可能原因**:
- Server URL 错误
- 认证失败
- 网络问题

**解决方案**:
1. 检查 Server URL 是否正确
2. 验证 API Key 等认证信息
3. 检查网络连接和防火墙

### Q: 加入房间失败

**可能原因**:
- 房间已有 Agent（Agent 独占规则）
- office_id 格式错误
- 未连接到 Server

**解决方案**:
1. 确认房间内没有其他 Agent
2. 检查 office_id 格式
3. 先执行 `socket connect`

---

## 配置相关

### Q: 占位符 ${input:xxx} 未被替换

**可能原因**:
- 未加载 inputs 定义
- input id 拼写错误

**解决方案**:
1. 使用 `inputs load @file.json` 加载定义
2. 使用 `inputs list` 检查已有定义
3. 确认 id 拼写正确

### Q: 工具名称冲突

**说明**: 当多个 MCP Server 有同名工具时会产生冲突。

**解决方案**: 使用别名区分
```json
{
  "tool_meta": {
    "read_file": {
      "alias": "local_read_file"
    }
  }
}
```

---

## 性能相关

### Q: 大量连接时性能问题

**建议**:
1. 使用 Redis 作为 Socket.IO 会话存储
2. 配置合理的连接数限制
3. 使用负载均衡

### Q: 工具执行慢

**建议**:
1. 检查 MCP Server 本身的性能
2. 考虑使用异步客户端
3. 合理设置超时时间

---

## 开发相关

### Q: 如何调试工具调用？

使用 CLI 的 `tc` 命令：
```bash
a2c> tc {"computer":"local","agent":"debug","req_id":"1","tool_name":"echo","params":{"text":"hello"},"timeout":30}
```

### Q: 如何查看事件流？

启用详细日志：
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Q: 同步客户端还是异步客户端？

- **异步客户端**: FastAPI、Sanic 等异步框架
- **同步客户端**: Flask、Django 等同步框架

---

## 其他

如有其他问题，请通过 Issue 反馈，建议附带：
- 使用场景描述
- 最小复现配置
- 日志片段
- 相关测试用例

## 参考

- [事件定义](../specification/events.md)
- [数据结构](../specification/data-structures.md)
- [CLI 使用指南](../guides/cli-guide.md)
