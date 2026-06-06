# 场景设计指南：协议流程类

适用于涉及 Agent ↔ Server ↔ Computer 完整协议链路的 UAT 场景，如版本握手、
tool_call 路由、SKILL 通知广播、断连守卫等。

## 核心特征

- **三进程协同**：需要 Server + Computer + Agent 三个真实进程在 tmux 中运行
- **协议事件驱动**：通过 Socket.IO 事件进行通信，测试需验证事件路由和载荷格式
- **端口动态分配**：每次测试分配随机端口，通过文件传递
- **时序敏感**：连接/断连/超时等操作的时序影响测试结果

## 设计原则

### 1. 环境拓扑先于用例

编写用例前，必须先明确 tmux 环境拓扑：

```markdown
### 环境拓扑

tmux session: a2c-uat
├── window: server     →  Server 进程（端口 → /tmp/a2c-uat-port）
├── window: computer   →  a2c-computer run --url http://127.0.0.1:<PORT>
└── window: agent      →  Agent Python 脚本
```

明确每个 window 的 pane ID，后续用例直接引用。

### 2. 三端日志同步收集

协议流程场景必须同时关注三端的输出：

| 端 | 日志来源 | 关键观察点 |
| -- | -------- | ---------- |
| Server | server pane / server.log | 连接事件、路由日志、广播记录 |
| Computer | computer pane / computer.log | MCP 调用、SKILL 上报、断连检测 |
| Agent | agent pane / agent.log | 请求发出、响应接收、通知接收 |

**每个用例执行后，三个 pane 都必须 capture-pane**。只看一端的输出会导致误判。

### 3. 连接生命周期编排

协议场景的用例编排应遵循连接生命周期：

```
Phase 1: 建立连接（Server 启动 → Computer 连接 → Agent 连接）
Phase 2: 协议交互（join_office → tool_call → SKILL 通知）
Phase 3: 异常场景（断连 → 重连 → 版本不兼容）
Phase 4: 清理（断开 → kill 进程）
```

**状态复用**：Phase 1 的连接建立可被 Phase 2 的所有用例共享。

### 4. 端口传递的验证

动态端口通过文件传递是关键基础设施。必须在第一个用例中验证：

1. Server 启动后端口文件存在
2. 端口号可被 Computer/Agent 正确读取
3. 端口可连通（curl 或 socket 连接测试）

### 5. 时序等待策略

协议交互有网络延迟，用例中需要合理的等待：

| 操作 | 建议等待 | 等待方式 |
| ---- | -------- | -------- |
| Server 启动 | 2-3s | capture-pane 轮询 `SERVER_PORT=` |
| Computer 连接 | 3-5s | capture-pane 轮询 `connected` |
| Agent 连接 | 2-3s | capture-pane 轮询 `connected` |
| tool_call 响应 | 3-5s | capture-pane 轮询结果输出 |
| 断连检测 | 5-10s | capture-pane 轮询 disconnect 日志 |

**轮询参数**：间隔 1-2 秒，最多 15 秒超时。

## 验证清单补充项

除通用验证清单外，协议流程场景还需确认：

- [ ] 环境拓扑已明确（3 个 tmux window + pane ID）
- [ ] 端口动态分配 + 文件传递机制已验证
- [ ] 每个用例都收集了三端日志
- [ ] 连接生命周期编排正确（Phase 1 → 2 → 3 → 4）
- [ ] 时序等待策略合理（无硬编码 sleep，用轮询检查）
- [ ] 断连/重连场景有独立的清理和重建步骤
