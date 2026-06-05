# Acceptance 设计原则

> Acceptance = 证明"种子本身按设计行为"的可执行/可手动跑的脚本/清单。它是种子库
> 不腐烂的**唯一**保证。本文是 acceptance 编写的设计契约。

## 1. 独立性（最重要）

Acceptance **必须脱离任何 UAT scenario** 独立可跑。

- 不依赖某个 scenario 已经准备的 tmux session
- 不依赖某个 UAT 报告的中间状态
- 不依赖前一条 acceptance 的副作用

**怎么做到独立**：

- 自己起进程（mcp 种子启动自己的 Python server）
- 自己起隔离环境（`A2C_SKILL_HOME=$(mktemp -d)`）
- 自己起依赖资源（`_helpers/init_bare_repo.sh` 自己 git init）
- 自己清理（trap EXIT + rm -rf）

## 2. 幂等

跑 N 次结果一致。**绝对**不在 acceptance 里：

- 写到 `$HOME/.a2c/` 或任何真实用户目录
- 改 git 全局/系统 config
- 占用固定端口（用 `--port 0` + 临时文件传递实际端口）
- 留下 `/tmp/a2c-uat-seed-*` 残留（即使失败也要清）

## 3. 期望对齐协议

每条 acceptance 的"PASS 判据"必须能映射到 `failure-axes.md` 一条记录或
`skill.md` 一处条款。**禁止**出现"我觉得应该这样"的 PASS。

判据写法：

```bash
# 正确（明确引用）
# Axis MC-ARC-03: skill.md §3 B archive_sha256 完整性校验
# 期望日志：archive sha256 mismatch
grep -q "archive sha256 mismatch" "$LOG_FILE" || fail "expected sha mismatch log"

# 错误（含糊）
grep -q "error" "$LOG_FILE"     # 太宽，任何 error 都通过；模糊
```

## 4. 三类 acceptance 形态

| 形态 | 适用 | 文件 |
|---|---|---|
| **`acceptance.sh`**（全自动） | mcp / marketplace 大部分种子 | 单文件 bash，trap EXIT 清理；退出码 0=PASS / 1=FAIL |
| **`acceptance.md` + 自动片段** | user 源（涉及 SKILL Home 路径，常需 audit 者确认） | markdown 内嵌 bash code fence；audit 时可全自动跑 or 交互式逐步 |
| **`acceptance.md`（纯静态）** | `_common/` 原料 | 仅静态校验：yaml 解析、目录结构 assert；可由通用脚本 batch 跑 |

## 5. 标准 `acceptance.sh` 骨架

```bash
#!/usr/bin/env bash
# Acceptance for seeds/mcp/server_archive_bad_sha.py
# Axis: MC-ARC-03 (skill.md §3 B archive_sha256)
# Expected: Computer ERROR log contains "archive sha256 mismatch", SKILL not registered.
set -Eeuo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
SEED_NAME="$(basename "$0" .acceptance.sh)"
TMPDIR="$(mktemp -d -t "a2c-seed-${SEED_NAME}.XXXXXX")"
LOG="$TMPDIR/computer.log"
SERVER_PORT_FILE="$TMPDIR/server.port"
HTTP_PORT_FILE="$TMPDIR/http.port"
PIDS=()

cleanup() {
  local code=$?
  for pid in "${PIDS[@]:-}"; do kill -9 "$pid" 2>/dev/null || true; done
  rm -rf "$TMPDIR"
  exit "$code"
}
trap cleanup EXIT INT TERM

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. 启动 archive HTTP fixture（serve seeds/mcp/_archives/）
python "$SEED_DIR/_http_fixture.py" --port-file "$HTTP_PORT_FILE" &
PIDS+=($!)
for _ in {1..20}; do [[ -s "$HTTP_PORT_FILE" ]] && break; sleep 0.1; done
[[ -s "$HTTP_PORT_FILE" ]] || fail "http fixture did not become ready"
HTTP_PORT="$(cat "$HTTP_PORT_FILE")"

# 2. 启动种子 MCP Server
python "$SEED_DIR/${SEED_NAME}.py" \
  --port-file "$SERVER_PORT_FILE" \
  --archive-base "http://127.0.0.1:$HTTP_PORT" &
PIDS+=($!)
for _ in {1..20}; do [[ -s "$SERVER_PORT_FILE" ]] && break; sleep 0.1; done
[[ -s "$SERVER_PORT_FILE" ]] || fail "seed server did not become ready"
SERVER_PORT="$(cat "$SERVER_PORT_FILE")"

# 3. 用 a2c-computer + 临时 SKILL_HOME，对该 server 触发 staging
export A2C_SKILL_HOME="$TMPDIR/skill-home"
mkdir -p "$A2C_SKILL_HOME"

# 通过最简方式直接调 staging（见 _helpers/run_staging.py）
python "$SEED_DIR/../_helpers/run_staging.py" \
  --server-url "http://127.0.0.1:$SERVER_PORT/mcp" \
  --home "$A2C_SKILL_HOME" \
  > "$LOG" 2>&1 || true   # 期望失败，所以不 set -e 终止

# 4. PASS 判据：日志含期望错误关键字 + SKILL 未在 home 物化
grep -q "archive sha256 mismatch" "$LOG" || fail "expected 'archive sha256 mismatch' in log, got:
$(tail -40 "$LOG")"

[[ ! -d "$A2C_SKILL_HOME/mcp" ]] || \
  [[ -z "$(ls -A "$A2C_SKILL_HOME/mcp" 2>/dev/null)" ]] || \
  fail "expected no SKILL materialized, but found: $(ls -R "$A2C_SKILL_HOME/mcp")"

echo "PASS: seed ${SEED_NAME}"
```

要点：

- `set -Eeuo pipefail` + 在"期望失败"步骤显式 `|| true`
- `trap cleanup EXIT INT TERM` 兜底清理
- `mktemp -d` + `A2C_SKILL_HOME=$TMPDIR/...` 隔离
- 端口由子进程写入文件、父进程读取（避免硬编码 / 抢占）
- 失败 PASS 判据**两条**：必有期望日志 + 必无非期望产物（避免"日志对了但 SKILL 也注册成功"漏判）

## 6. happy path acceptance 的额外项

happy 种子除了"成功落盘"，还要验证：

- 注册到 `SkillRegistry` 的 `A2CSkillRef` 字段齐全（name/source/path/description/version 等）
- `path` 指向的目录存在且包含 `SKILL.md`
- `SKILL.md` 的 frontmatter `name` 与目录 basename 一致（协议 §4）
- 复跑（同 server 同 SKILL）走 `update` 而非 `register`（缓存命中场景）—— 看
  ResourceListChanged 路径时

## 7. user 源 acceptance 的特殊处理

user 源**不复制进 SKILL Home**——它就地发现。所以 acceptance.md 必须：

- 给出**两种**拷贝路径之一：
  - 拷进 `$A2C_SKILL_HOME/user/<skill>/`（全局个人）
  - 拷进 `<workdir>/.tfrobot/skills/<skill>/`（工作目录）
- 列出"哪个 workdir 已登记 + 登记序"作为前置；用 `acceptance.sh` 里
  `a2c-computer workdir register <path>` 显式构造
- 期望生效优先级：`failure-axes.md US-OVR-01`

## 8. marketplace 源 acceptance 的特殊处理

每个 marketplace 种子目录是一份**仓库工作树**，不带 `.git/`。acceptance 自己 init：

```bash
SEED_REPO_WT="$SEED_DIR/$SEED_NAME"
BARE="$TMPDIR/$SEED_NAME.git"
git init --bare "$BARE" >/dev/null
git -C "$SEED_REPO_WT" -c init.defaultBranch=main init >/dev/null 2>&1 || true
git -C "$SEED_REPO_WT" add -A
git -C "$SEED_REPO_WT" -c user.email=seed@uat -c user.name=seed commit -m "seed snapshot" >/dev/null
git -C "$SEED_REPO_WT" push "$BARE" HEAD:refs/heads/main >/dev/null
# 之后 a2c-computer marketplace add file://$BARE --trust
```

上面这段封装进 `seeds/marketplace/_helpers/init_bare_repo.sh`，acceptance 调用即可。

## 9. `_common/` acceptance 的特殊处理

`_common/` 是静态原料，acceptance.md 只做静态校验：

```bash
# 静态校验脚本（_common 通用）
python - <<'PY'
import sys, yaml
from pathlib import Path
d = Path(sys.argv[1])
assert (d / "SKILL.md").is_file(), "SKILL.md missing"
text = (d / "SKILL.md").read_text(encoding="utf-8")
assert text.startswith("---\n"), "frontmatter not found"
fm = yaml.safe_load(text.split("---\n", 2)[1])
# happy: name + description 都有
# invalid-missing-desc: 期望 description 缺
# ...（按 _common/<name>/acceptance.md 的具体期望分支）
PY
```

## 10. 反模式（**不要**这么写 acceptance）

| 反模式 | 为什么差 |
|---|---|
| 跑 pytest（依赖 conftest） | 破坏独立性 |
| sleep 5 等待进程 | 时序脆弱，CI/本地差异大；用 polling 文件信号 |
| 检查"无 error 输出" | 期望应是**正向断言**（log 含 X / 目录无 Y），不是"否定全集" |
| grep 模糊关键字 | 期望必须对齐协议条款的精确文本 |
| 把端口写死 | 抢占；用 `--port-file` |
| 让 `set -e` 失效 | 漏掉错误；显式 `|| true` 标注期望失败行 |

## 11. audit 调用契约

`/uat-seed audit ...` 调用 acceptance 时遵守：

| 调用 | 参数 |
|---|---|
| `acceptance.sh` | `bash <path>` —— 退出码 0=PASS / ≠0=FAIL；stderr 是 fail 原因 |
| `acceptance.md` 自动片段 | 提取 ` ```bash` ... ``` ` 代码块，按顺序执行；遇到错误终止 |
| `acceptance.md` 手动项 | 显示给用户逐项确认 |

每次 audit 在 `/tmp/a2c-uat-seed-audit-<timestamp>/` 留日志，方便复盘。

## 12. CI 集成（informational）

将来可加 `.github/workflows/seed-audit.yml`：

```yaml
- name: Audit all seeds
  run: |
    for sh in .claude/skills/UAT/resources/seeds/*/*.acceptance.sh \
              .claude/skills/UAT/resources/seeds/*/*/acceptance.sh; do
      [[ -f "$sh" ]] || continue
      bash "$sh" || exit 1
    done
```

短期手动跑（`/uat-seed audit --all`）即可；积累一定量后再做 CI。
