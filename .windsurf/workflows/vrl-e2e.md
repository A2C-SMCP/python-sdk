---
description: VRL模块E2E测试
---

现在A2C协议可以触发MCP协议的工具，对其进行调用

但在使用过程中，工具返回结果的数据结构可能并不满足用例使用场景，比如在前端渲染的时候，不仅仅需要字符串给到LLM，更需要一个结构化的表单，方便前端进行渲染。而我们不能一旦遇到这类结构问题就改造MCP，因此在A2C协议中有了VRL封装，利用VRL-Python提供的VRL运行能力来解决结构问题。

相关实现在：
1. 工具调用实现：a2c_smcp/computer/mcp_clients/manager.py::acall_tool
2. VRL脚本定义位置在 a2c_smcp/computer/mcp_clients/model.py::BaseMCPServerConfig 根VRL属性
3. 现在对VRL的相关功能测试主要在：tests/unit_tests/computer/mcp_clients/test_vrl_integration.py 你可以通过这组用例了解目前的使用方式

我们需要补充一些e2e测试，位置放到：tests/e2e/computer/mcp_clients/test_manager.py

我提出要求，比如使用某个MCP封装（这个MCP是生产环境提供的开源方案，可以放心使用）
你初始化一个Manager，管理这个MCP实现，然后按测试要求对某个工具进行调用，并且验证返回结果被VRL正确重构。