# Blob `drain_blob` 双端对齐建议（Rust → Python）

> 来源：Rust SDK 实现 `crates/smcp/src/utils/blob.rs`（对标 Python `a2c_smcp/utils/blob.py`）+ `/code-review` 跟进。
> 状态：**Rust + Python 双端均已实施**（Python 侧 issue #101 / 分支 `feature/blob-drain-parity`）。
> 下列两项均**非语言限制**、是设计选择，不改协议线格式、不影响 happy-path、`PROTOCOL_VERSION` 不变。
>
> 实施补记（Python `/fix-review`）：R1 落地时实测发现 **async 路径在「快 range + 慢 fatal」竞态下同样会
> 掩盖 fatal**（见 R1「现状」修正），故 sync **与** async **一并**改为「收集全部 outcome 后按
> `fatal > drift > range` 分派」，达成双端确定性 fatal-wins。

---

## R1 — sync 并行：让 `fatal > recoverable` 优先级确定性成立（永不隐藏 fatal）

### 现状（Python）

- `_drain_parallel_async`（`asyncio.TaskGroup`）：原实现首个异常即取消其余，收集异常组后经
  `_flatten_exception_group` 按 `fatal > drift > range` 分派。**曾误以为**已保证 fatal 不被 range 掩盖
  ⚠️ —— 实测**不成立**：`fetch()` 对 range **就地 raise** → TaskGroup fail-fast 取消其余在飞任务；当
  range 比 fatal **先完成**，在飞的 fatal 被取消（`CancelledError` **不进** ExceptionGroup）→ group 仅余
  range → `_RecoverableRange` → 串行 fallback → 串行撞低 offset range 即 fatal → **对外报 range，掩盖
  真实 fatal**。
- `_drain_parallel_sync`（`ThreadPoolExecutor` + `as_completed`）：原实现**遇首个错误即 `break`**
  （`first_error`），随后 `if reason == "range" → _RecoverableRange`，同样会以先完成的 range 掩盖 fatal。

### 问题

并发态下若一个 `range` 块先于一个 `fatal` 块（如 `invalid_handle` / `forbidden` / `gone`）完成，
sync 路径会以 `range` 先 `break` → `_RecoverableRange` → 串行 fallback → 串行态再遇 `range` 即 fatal →
**最终对外报 `range`，掩盖了真实的 `forbidden`/`gone`**。

即：**同一服务端状况，错误被降级成貌似瞬态的 `range`**（forbidden/gone 语义是"不可重试/无权限"，被
掩盖）。实测确认 sync（break-on-first）与 async（in-place raise + TaskGroup-cancel）**两条路径都有此掩盖**，
race 触发条件不同而已。

### 建议（sync **与** async 一并改）

**两条并行路径**均改为「收集所有已完成 outcome 后再分派」：`range`/drift **不**早退（sync 不 `break` /
async 不就地 raise，改返回 marker），**仅** fatal 早退（sync `break` / async 就地 raise 触发 TaskGroup
fail-fast）。收集结束后按 `fatal > drift > range` 优先级分派。sync 伪代码（async 同理，`fetch()` 改为返回
`("range"|"drift"|"ok", off, ret)` marker、仅 fatal raise）：

```python
# 伪代码（替换 break-on-first-error）
recoverable = False
fatal: BlobTransferError | None = None
for fut in as_completed(futures):
    try:
        ret = fut.result()
        _raise_for_blob_error(ret)
        if str(ret["sha256"]) != expected_sha or int(ret["total_size"]) != total_size:
            recoverable = True            # drift：不 break，继续收集
            continue
        results[futures[fut]] = ret
    except BlobTransferError as e:
        if e.reason == "range":
            recoverable = True            # range：不 break，继续收集（让并存 fatal 必被发现）
        else:
            fatal = fatal or e            # 仅 fatal 记录；可停止提交新任务
            break
# 分派：fatal 优先于 recoverable
if fatal is not None:
    raise fatal
if recoverable:
    raise _RecoverableRange()
```

代价：recoverable 路径会多等若干在飞块完成再回退串行（原本就要串行重读，开销有界且仅错误路径）。

### Rust 对照

Rust `drain_parallel_sync` 的 recoverable 分支（`Ok(None)` 漂移 / `ChunkErr::Range`）**不置 stop/break**，
仅 fatal 触发 `stop`+`break`；drain 完后 `fatal` 优先、其次 `recoverable → Fallback`。与 async 路径完全一致。
对应测试：`parallel_sync_fatal_beats_recoverable`、`parallel_async_fatal_beats_recoverable`。

Python 对应测试：`test_parallel_sync_fatal_beats_range`、`test_parallel_async_fatal_beats_range`，
另补 `test_parallel_sync_range_fallback` / `test_parallel_sync_drift_fallback` /
`test_parallel_sync_full_sha_mismatch_rereads` 覆盖 sync 并行 recoverable→fallback 分支。

---

## R2 — `max_retries == 0` 脚枪：夹取至 ≥ 1

### 现状（Python）

`_drain_serial_async` / `_drain_serial_sync`：`for attempt in range(max_retries): ...`。
`max_retries == 0` → **零次循环**、一个 `call` 都不发 → 直接
`raise BlobTransferError(reason="max_retries_exceeded")`。`drain_blob(..., max_retries=0)` 显式传 0
即得此反直觉结果（默认值安全，但显式 0 是合法入参）。

### 建议

入口夹取或入参校验：`effective_retries = max(1, max_retries)`（至少尝试一次），并在 docstring 注明。

### Rust 对照

`drain_blob` / `drain_blob_sync` 入口：`let max_retries = opts.max_retries.max(1);`。
对应测试：`serial_async_zero_retries_still_attempts_once`。

---

## 备注

- 两项不改变协议线格式、不影响 happy-path 行为，纯属错误路径的稳健性 / 一致性加固。
- **已落地** `a2c_smcp/utils/blob.py`（issue #101 / 分支 `feature/blob-drain-parity`）：R1 双端 marker-collect
  分派 + R2 入口 `max(1, max_retries)` 夹取，附 6 项单测；`ruff` / `mypy` 净、`drain_blob` 全测试零回归。
- 代价（双端一致）：recoverable 路径会多等若干在飞块完成再回退串行——原本就要串行重读，开销有界且仅错误路径。
