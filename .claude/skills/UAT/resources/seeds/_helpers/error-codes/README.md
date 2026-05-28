# `_helpers/error-codes`

**用途**: 供 `error-codes` UAT 场景复用的 Agent 驱动脚本 + SKILL_HOME 搭建工具

**提供**:
- `setup_skill_home.sh` — 搭建 SKILL_HOME，创建含 `.skillenv` 的 SKILL（E-06 前置）+ 标准 happy-path SKILL
- `agent_error_codes_driver.py` — 可复用的 Agent 测试驱动，自动执行 E-01~E-10 用例

**使用方式**:

1. 搭建 SKILL_HOME（Computer 启动前执行）：
   ```bash
   SEEDS_ROOT=<项目根>/.claude/skills/UAT/resources/seeds
   SKILL_HOME=/tmp/a2c-uat-skill-home-$$
   bash "$SEEDS_ROOT/_helpers/error-codes/setup_skill_home.sh" "$SKILL_HOME" "$SEEDS_ROOT"
   ```

2. 启动完整链路环境（Server → Computer → Agent），Computer 使用上面的 SKILL_HOME：
   ```bash
   A2C_SKILL_HOME=$SKILL_HOME uv run a2c-computer run \
     --url http://127.0.0.1:$PORT --approve-all-mcp --auto-connect --auto-reconnect
   ```

3. Computer 加入 office 后运行 Agent 驱动：
   ```bash
   uv run python agent_error_codes_driver.py \
     --port-file /tmp/a2c-uat-port \
     --office-id err-uat-office \
     --computer-name <computer_name> \
     --skill-with-env env-skill
   ```

**覆盖的错误码**:
- `4016` SKILL_NAME_INVALID — E-01 (path traversal name), E-02 (too many colons)
- `4014` MCP_SERVER_NOT_FOUND — E-03 (legal name but unregistered)
- `4017` SKILL_RESOURCE_NOT_ACCESSIBLE — E-04 (traversal), E-05 (absolute path), E-06 (.skillenv forbidden), E-07 (not_found)
- `4018` BLOB_NOT_ACCESSIBLE — E-08 (invalid handle), E-09 (empty handle), E-10 (cross-computer)
