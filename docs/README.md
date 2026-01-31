# A2C-SMCP 文档中心

本目录包含 A2C-SMCP 协议及其 Python SDK 的完整文档。

## 文档结构

```
docs/
├── README.md                    # 本文件 - 文档导航
├── index.md                     # 文档入口
│
├── specification/               # 协议规范（规范性文档）
│   ├── index.md                 # 协议概述与设计目标
│   ├── architecture.md          # 协议架构（角色、通信模型）
│   ├── events.md                # 事件定义（完整事件列表）
│   ├── data-structures.md       # 数据结构定义
│   ├── room-model.md            # 房间隔离模型
│   ├── error-handling.md        # 错误处理规范
│   └── security.md              # 安全性考虑
│
├── guides/                      # 使用指南（实践性文档）
│   ├── getting-started.md       # 快速开始
│   ├── server-guide.md          # Server 模块使用指南
│   ├── agent-guide.md           # Agent 模块使用指南
│   ├── computer-guide.md        # Computer 模块使用指南
│   └── cli-guide.md             # CLI 使用指南
│
└── appendix/                    # 附录
    └── faq.md                   # 常见问题
```

## 文档类型说明

### 协议规范 (Specification)

规范性文档，定义协议的正式规则。使用 RFC 2119 风格的关键词（MUST, SHOULD, MAY 等）。适合需要实现协议或深入理解协议细节的读者。

### 使用指南 (Guides)

面向实践的教程，帮助开发者快速上手。包含代码示例和最佳实践。

## 快速导航

| 我想要... | 阅读... |
|-----------|---------|
| 了解 A2C-SMCP 是什么 | [协议概述](specification/index.md) |
| 快速开始使用 | [快速开始](guides/getting-started.md) |
| 搭建信令服务器 | [Server 使用指南](guides/server-guide.md) |
| 开发 Agent 客户端 | [Agent 使用指南](guides/agent-guide.md) |
| 管理 MCP 服务器 | [Computer 使用指南](guides/computer-guide.md) |
| 使用命令行工具 | [CLI 使用指南](guides/cli-guide.md) |
| 查看完整事件列表 | [事件定义](specification/events.md) |
| 解决常见问题 | [FAQ](appendix/faq.md) |

## 与代码版本对应

本文档对应 SDK 版本: **0.1.2-rc1**

---

*如发现文档问题，欢迎提交 Issue 或 PR。*
