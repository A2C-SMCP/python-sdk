# 在使用A2C python-sdk 过程中常见问题及解法

## 1. “当前工具需要调用前进行二次确认，但客户端目前没有实现二次确认回调方法。请联系用户反馈此问题”

一般而言如果遇到：

{
  "meta": null,
  "content": [
    {
      "type": "text",
      "text": "当前工具需要调用前进行二次确认，但客户端目前没有实现二次确认回调方法。请联系用户反馈此问题",
      "annotations": null,
      "meta": null
    }
  ],
  "structuredContent": null,
  "isError": true
}

说明当前被调用的工具在添加服务时启用了二次确认能力，如果在cli测试过程中遇到此问题，想要关闭，有两个方法：

a. 可以在配置文件中 tool_meta.{工具名} 设置 auto_apply: true
b. 可以在配置文件中 default_tool_meta.auto_apply: true，关闭当前服务所有未显式声明打开的工具的二次确认

## 2. 补充：`window://` 数量与桌面组装规则（现状）

- **一个 MCP Server 是否只能暴露一个 `window://`？**
  不是。当前约定与实现均支持一个 MCP Server 暴露多个 `window://` 资源（通过 `window://{host}/{path...}` 区分不同窗口）。Computer 侧会从 `resources/list` 的返回中筛选出所有 `window://` 资源，并逐个进行订阅。

- **Computer 如何用这些 window 资源组装 Desktop？**
  Desktop 的生成是“跨 MCP Server 汇总 window 资源 → 读取每个 window 的内容 → 按策略排序/裁剪 → 渲染为字符串列表”的流程，对外通过 `client:get_desktop` 返回。

- **桌面组装的主要原则（当前策略实现）**
  - **size 截断**：若传入 `desktop_size`，则全局按数量上限截断；`desktop_size<=0` 直接返回空。
  - **server 优先级**：优先展示最近发生工具调用的 MCP Server（按工具调用历史倒序去重），其余 server 再按名称稳定排序。
  - **server 内窗口排序**：同一 server 内按 `WindowURI.priority` 降序；未提供 priority 默认为 0。
  - **fullscreen 规则**：若某 server 存在 `fullscreen=true` 的窗口，则该 server 仅展示一个 fullscreen 窗口（多个 fullscreen 时仅第一个生效），然后进入下一个 server。
