# `_helpers/blob-resources`

**用途**: 供 `blob-transfer` UAT 场景复用的二进制测试资源 + Agent 驱动脚本

**提供**:
- `generate.sh` — 生成测试文件（small.txt 100B + large.txt 64KB）+ 输出 SHA256
- `agent_blob_driver.py` — 可复用的 Agent 测试驱动，自动执行 B-01~B-04 用例

**使用方式**:

1. 生成测试资源：
   ```bash
   bash generate.sh /path/to/skill-dir
   ```

2. 运行 Agent 驱动（在完整链路环境就绪后）：
   ```bash
   uv run python agent_blob_driver.py \
     --port-file /tmp/a2c-uat-port \
     --office-id blob-uat-office \
     --computer-name blob-comp-001
   ```

3. B-04 需要先在 Computer 上挂载 binary MCP server：
   ```bash
   # 在 Computer CLI 中执行
   server add @seeds/mcp/binary_image_tool_server_config.json
   ```
   > 注意：config 中的 `<PROJECT_ROOT>` 需替换为实际项目根路径。
   > 加 `--skip-b04` 可跳过 B-04 测试。

**确定性 SHA256**:
- `small.txt` (100B): `d82c6aa133a0fc25b087f46ad7ed2a3042772e612e015571e61753ff55ba6da8`
- `large.txt` (64KB): `fee47b1f0d7685a226fd5f2b9dd8f525038bbb05fe9d89a5d75c249edac868e3`
- `big_image` (32768B raw → ~43.7KB base64): `a06fa47c2671def27679fe048a287aeb2823c07a1e15d6395e02b3cec681c73d`
