# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) versioning.

> 注：v0.3.1 / v0.3.2 发版时未单独切段（Bugfix / OAuth 收敛类），本段累积至 [0.3.0]。

## [Unreleased] — #214 房间事件失败 ack 契约（协议 v0.5.0）

> **A2C-SMCP 协议 v0.5.0 实现**：`PROTOCOL_VERSION` 由 `0.4.0` 抬至 `0.5.0`（v0.x 下 `MINOR` 严格匹配，
> 抬位后握手层才会正确拒绝 `0.4.x` 对端）。SDK 包版本（`__version__`）由发版流程另行推进。

### Breaking Changes

- **Agent 显式 `join_office` 改为等 ACK（#218）**：`AsyncSMCPAgentClient.join_office` /
  `SMCPAgentClient.join_office` 由「无 ack 的 `emit`」改为 `call`，三类后果：
  1. **可能抛**：服务端裁决为拒绝时抛既有 `SMCPProtocolError`（`.code` 可机器分流 `400/403/4101/4105/
     4106`；码不可解析 / 形状不认识时 `.code == -1`，「未获裁决」）。此前入房被拒**完全静默**
     ——包括同一连接改名被拒的 `403`（本地意图与服务端身份会静默分叉）。`403` 的异常文案追加
     「本连接会话身份已固化为 `<name>`；改名须重新建立连接」。
  2. **变阻塞调用**：ACK 等待由 `0` 变为**有界 10s**（新常量 `OFFICE_JOIN_TIMEOUT`；sync 侧还要叠加
     office 操作锁 ⇒ 最坏约 20s）。超时按 `a2c_smcp.agent.base.OFFICE_ACK_TIMEOUT_ERRORS` 捕获
     （socketio 的 `TimeoutError` **不是** builtin 子类）。
  3. **失败后本地意图去留按统一表**（显式 join 与自动回房同规则、同文案）：明确拒绝 ⇒ 意图回退到
     「已确认房」；未获裁决 / 传输层失败 ⇒ 双清空。效应只在**本次声明仍是最新**时施加（同拍已被
     断连 / 重连的会话边界抢先 ⇒ 意图保留待重放）；未连接即调用（`BadNamespaceError`，发送前）按同一
     规则 ⇒ **不保留**意图（此前会残留待重连重放）。
  4. **取锁期间被抢占 ⇒ 静默返回、不发包**（返回形如成功）：并发 `leave_office` / 换房，或断连 / 重连
     会话边界抢先时，本调用不再把 JOIN 发上线（wire 顺序恒与最后声明一致）。调用方需确认是否真的在房
     时，请以服务端事实为准。
  - 附带：新增 `_confirmed_office` / `_office_session` 状态，sync 侧新增 office 操作锁；拒绝日志由
    `a2c_smcp.utils.office` 的共享产出者统一产出（两路径同文案，不含 SID / namespace）。
- **`OFFICE_REJOIN_TIMEOUT` 硬切为 `OFFICE_JOIN_TIMEOUT`**（#218，不留别名）：Agent 两侧显式 join 与
  自动回房共用该有界等待常量；Computer 显式 join **未**纳入（仍走 socketio 默认超时）。
- **三个房间事件的失败 ack 改为 flat `ErrorPayload`**（#214，protocol#61）：
  `server:join_office` / `server:leave_office` 的**成功 = 空 ack**（`None`），失败 = 顶层含 `code` 的
  flat `ErrorPayload`；`server:list_room` 成功仍为 `ListRoomRet`，失败为 flat `ErrorPayload`。
  **`(bool, str | None)` 元组形态已废除**（该形态的 ack 载荷字节序列与 arity 均变化：由两参 ACK
  变为零参 / 单 dict）。
- **`a2c_smcp.utils.parse_join_ack` 返回类型变更**（SDK 公开 API）：由 `tuple[bool, str]` 改为
  `JoinOfficeVerdict(ok, code, message)`，便于下游按 `code` 机器分流。**刻意不再兼容**旧元组形态——
  `MINOR` 严格匹配使跨版本对端物理上不可能互联，兼容分支只会掩盖未迁移的调用方。
- **名字唯一性键空间收敛为 `(office_id, role, name)`**（#215，protocol#61 Q3）：唯一性改为**房内同 role**
  （协议 MUST NOT 施加全局唯一）。用户可感：**跨 office 同名**、**同名 Agent + Computer** 不再被 `4105`
  误拒；同房同 role 同名仍 `4105`。`client:*` 路由改为在**发起者所在房内**解析——目标只在他房 / 名字属于
  Agent 时立即回 flat `404`（与「不存在」逐字节相同，不泄露他房成员存在性），此前为抛异常致调用方挂满超时。
  未入房发起者的路由拒绝现为 flat `4103`（#216，见下条）。
  - Server 子类化 API 签名变更（不留兼容）：`get_sid_by_name(office_id, role, name)`、
    `_register_name(office_id, role, name, sid)`、`_ensure_name_registerable(office_id, role, name, sid)`；
    `_name_to_sid_map` 键改为 `(office_id, role, name)` 元组（`server.types.NAME_KEY`），新增反向索引
    `_sid_to_name_key`：`_unregister_name` 按 sid 注销、不再从可变会话字段反推（杜绝回滚后残留键致永久 4105），
    并内置归属守卫（只删本 sid 持有的键）。`enter_room` 的 Computer 房内同名扫描并入注册表闸门（判据单点）。
- **边界校验加固 + office 房名命名空间分离**（#216，protocol#61 自查 A / B + `office_id` 取值域）：
  - **`client:*` 路由的拒绝一律经 ack 可感**（此前多为抛异常 ⇒ 调用方挂满超时）：载荷畸形（缺字段 / 类型错 /
    无载荷 / 多余位置参数）⇒ `400`；未入房 ⇒ `4103`；**非 Agent 发起任一 `client:*` ⇒ `403`**（此前只有
    `tool_call` 校验角色，其余路由对 Computer 放行）；房内解析不到 ⇒ `404`。唯一保留的静默路径是「发起者会话
    已不存在」。同步版 `client:tool_call` 的载荷改按 `ToolCallReq` 校验（与 async 一致；`timeout` 等字段必填）。
  - **fire-and-forget 事件**（`server:tool_call_cancel` / `server:update_*` ×4）：载荷非法 / 角色不符 / 未入房时
    改为**告警日志 + 静默丢弃**（协议：MUST NOT 为此新增 ack），不再抛 `SMCPNamespaceError`（此前会往服务端日志灌
    traceback；线上行为不变——本就不广播、不回执）。`require_office_id` 保留导出，但本仓不再调用。
  - **出向 `notify:*` 的身份字段改取发起者会话**（events.md §房间广播类事件的目标来源 MUST）：`notify:update_*` 的
    `computer`、`notify:tool_call_cancel` 的 `agent` 不再原样转发载荷自称值（此前可用 `computer="peer"` 让房内 Agent
    去刷新**另一台** Computer）；载荷与会话不符时只告警、**按会话执行**——`tool_call_cancel` 不再因 `agent` 名不符而丢弃
    （协议 MUST NOT 拒绝）。
  - `server:join_office` 的 `office_id=""` ⇒ `400`（此前入房成功却处处被当成「未入房」）。
  - **socketio 房名改为 `office:{office_id}`**（`server.utils.office_room` / `office_id_of_room`），与 socketio
    为每个连接自动建立的私有 sid 房**无条件不相交**——此前取 `office_id = <对端 SID>` 即可进入对端私有房并向其
    投递广播。**线上载荷不变**（会话 / ack / 通知里的 `office_id` 仍是原值），但**自行 `emit(room=office_id)`、
    直接读 `manager.rooms` 或 `get_participants(namespace, office_id)` 的 `SMCPNamespace` 子类须改用 `office_room()`**；
    房间广播在基类统一经 `_emit_to_office` 发出；`rooms(sid)` 遍历只处理 `office:` 前缀房。
  - handler 签名统一为 `(sid, data=None, *_extra)`（绑定失败也落到校验层）；边界校验单点 `server.utils.parse_payload`；
    `_relay_client_call` 签名变为 `(sid, data, extra, event, req_adapter, ret_adapter, *, timeout_from_request=False)`。
    `client:*` 校验通过后**原样转发**原始载荷（async `tool_call` 此前转发校验后的 dict——会剥掉额外键、做类型强转；
    现与 sync 及其余路由一致）。
  - 公开导出 `a2c_smcp.server.office_room` / `office_id_of_room` / `OFFICE_ROOM_PREFIX`。断连清理改经可覆写钩子
    `_rooms_to_leave_on_disconnect(sid)`：`BaseNamespace` 保持原语义（离开除私有 sid 房外的全部房），`SMCPNamespace`
    覆写为只处理 `office:` 房并交出原始 office_id。
  - **Agent 侧**：`get_tools_from_computer` / `get_config_from_computer` / `get_desktop_from_computer`（sync + async）
    解码路由层 flat ErrorPayload 并抛 `SMCPProtocolError`——此前 `get_config` 把 `404` / `4103` **静默当成「零个
    server」的成功**，另两者报误导性的「req_id 不匹配」。
  - 失败 reason 不含对端 `sid` / namespace（自 #214 起已成立，本单补真实链路验收）。
- **`enter_room` / `_ensure_name_registerable` 的业务拒绝改抛领域异常**
  （`RoomFullError` / `NameConflictError` / `AlreadyInRoomError`，均为 `ValueError` 子类，携带 `code`）。

### Added

- **入房失败处理的单一权威 API**（#218，`a2c_smcp.utils`）：`resolve_join_failure`（失败效应表：
  明确拒绝 ⇒ 意图回退到已确认房；未获裁决 / 传输层失败 ⇒ 双清空）、`join_failure_message` /
  `build_join_failure_payload`（异常与日志**同一文案**；`403` 追加身份提示；未获裁决省略 `code` 键 ⇒
  `SMCPProtocolError.code == -1`）、`log_join_rejection`（两路径共用的 ERROR 产出者）、
  `OfficeMembership`（效应结果）。另新增 `a2c_smcp.agent.base.OFFICE_ACK_TIMEOUT_ERRORS`。
- `ErrorCode` 新增 `400` / `403` / `500` / `4101`–`4106`（`4102 Room Not Found` 为预留码，
  builder **构造上拒绝**产出）。载荷构造收敛到 `build_bad_request_error` / `build_internal_error` /
  `build_room_rejection_error` 三个共享 builder（sync / async 逐字节一致）。
- **`is_protocol_error_payload` 增加形状守卫**：带 `content` 的 dict（MCP `CallToolResult` 形状）
  **优先判否**。`CallToolResult` 是 `extra="allow"`，工具可携带恰好撞上协议码的顶层 `code`；
  `client:tool_call` 的 ack 直接透传该结果，误判会把真实工具结果顶替成协议错误（码集合并入
  `400/403/500` 后这一风险上升）。协议 `ErrorPayload` 从不带 `content`，判别无损。
- **载荷校验失败必回 ack**：`server:join_office` / `leave_office` / `list_room` 在载荷畸形
  （含**框架层参数绑定失败**——不带载荷 emit、多参 emit）时回 `400`，不再"静默不 ack"让调用方挂到
  自身超时（error-handling.md:102-106）。
- `server:list_room` 越权统一：无房 → `4103`；显式点名他房 → `4104`（**不再**静默不 ack、
  **不再**返回空 `sessions`）；且**不调用**会话读取器 ⇒ 目标房成员信息不可能进响应。
- 三个事件的 handler 都补齐 **catch-all**：未知内部异常一律回 `500` + 笼统文案（原文进日志），
  不再让异常逃出 handler 导致"根本不发 ACK、调用方挂到自身超时"。
- **重连回房的瞬态冲突有界退避重试（#212）**：静默断线后服务端要等自身心跳超时才回收旧会话，而客户端
  在秒级内就重连重放 ⇒ 必然撞上 `4101`（房内已有 Agent）/ `4105`（房内同名）。协议把二者定义为同一类
  **瞬态冲突**，并只对「本端刚经历传输层重连」的恢复路径放行**有界**重试（`error-handling.md`
  §建议的重试策略）。三条回放路径（Agent async / Agent sync / Computer）现在按 `1→2→4→5…` 秒退避
  重试，**默认预算 45s** = socket.io 默认最长回收窗口（`ping_interval(25) + ping_timeout(20)`），
  预算耗尽才落失败效应与错误日志（与显式入房**同一张效应表、同一句 ERROR 文案**，故 #219 的同态口径
  不受影响）。静默断线族因此从「需人工重入」变为「无感自愈」。
  - **可配置**：`AsyncSMCPAgentClient.office_rejoin_retry_budget` / `SMCPAgentClient.…` /
    `SMCPComputerClient.…`（公开实例属性，默认取 `OFFICE_REJOIN_RETRY_BUDGET`；`<= 0` 退化回单次尝试）。
    部署方调大了服务端 `ping_interval` / `ping_timeout` 时须相应调大——协议 `room-model.md` 的同名
    SHOULD 级部署约束是客户端补偿能生效的前提。
  - **只作用于回放路径**：显式 `join_office` **不重试**（首次入房撞上冲突即永久冲突）；`4106`（重连产生
    的是新会话，不可能「已在其它房」）/ `500` / 未知码 / 无码 / 传输层失败一律不重试。
    > **协议对齐说明（有意的一次解释扩张）**：协议把重试限定为「本端刚经历传输层重连」，而 Computer 侧
    > 「宿主预置 `office_id` 再 `connect()`」的**首连**也走重放通道 ⇒ 也会重试。冷启动（进程被杀后
    > 以同名重启）撞的正是**上一进程**尚未回收的僵尸会话，与协议给出的瞬态成因（「旧会话未回收」）同源，
    > 故判为落在「重试判定由客户端依自身状态做出」的自治范围内；Agent 侧无此通道（其重启走显式
    > `join_office`，不重试）。若协议要收紧该边界，两侧需同步收窄。
  - **锁语义**：预算内**逐次取/放锁**（每次尝试各取一次 office 操作锁）⇒ 退避不会占满 office 操作
    互斥；恢复耗时的上界 = 预算 + 一次有界 ACK 等待（末次尝试可在预算边界上发起）。
  - 新增单一权威纯函数 `rejoin_retry_delay` 与常量 `OFFICE_REJOIN_RETRY_BASE_DELAY` /
    `OFFICE_REJOIN_RETRY_MAX_DELAY` / `TRANSIENT_JOIN_CONFLICT_CODES`（`a2c_smcp.utils`）。
- **回放在途窗口的状态上报不再丢失（#223）**：`server:update_config` / `update_tool_list` /
  `update_desktop` / `update_skills` 的发送判据由「desired 非空 ∧ namespace 在册」收敛为「有入房意图 ∧
  **服务端已确认在房** ∧ namespace 在册」——与协议 `computer.md §2.2`「Computer SHOULD 在**成功加入
  Office 后**才发送这四个事件」对齐（rust 侧早已是同款 `has_confirmed_office()` 判据）。判据不成立时不再
  对服务端发注定被丢弃的包，而是把**事件名记入待补发集合**；成员关系重新确立（自动回房成功 / 显式入房
  成功）后**逐条补发一次**（协议同节明许「未加入 Office 时，本地变化可以被记录或合并，但不应产生跨房间
  可见通知」）。由此，**自本端感知断连起**，断连 / 退避重试窗口内改配置、改工具、改 SKILL、改桌面时，
  房内 Agent 不再漏收——工具面本就有 `notify:enter_office` 自动重拉兜底，config / skills / desktop 三类
  此前会一直漏到下一次变更（#212 把该窗口从毫秒级拉长到最长 ≈ 预算 + 一次有界 ACK 等待，放大了这一既存
  缺口）。「物理掉线 → 本端察觉」之间的残留窗口属 fire-and-forget 固有不可知（消除它必须加 ack，协议
  禁止），不在本保证范围内。
  - 规则：同一类别在窗口内改多次**合并为一次**补发（按事件名去重，域恒为 4）；补发失败的条目留待**下一次**
    成员关系确立时再补（逐条独立，一条失败不饿死其余类别）；从未入房 / 预算耗尽 / 已退房后不记录；退房
    不清空待补发集合。
  - 空转语义（写进 docstring）：条目移除 = 「已交给传输层」，**不是**「服务端已收到」——协议明文禁止给这
    四个事件新增 ack 通道（`error-handling.md` §无 ack 通道的 fire-and-forget 事件），客户端永远无法确证
    送达。
  > **跨 SDK 对齐说明（有意的一次分叉）**：本单的**判据**半与 rust 对齐；**合并补发**半是 python 先行
  > （rust 今天判据不成立即静默丢弃、无补发）⇒ 已开 rust 镜像单跟踪，由 rust 侧决定是否跟进。

### Fixed

- 失败原因不再以自由文本回传：`message` 使用协议规范文案，`details` 只含**与发起者自身相关**的
  上下文（目标房 / 自己声明的 role / 自己当前所在房），**MUST NOT** 携带任何对端会话标识（`sid` 等）。
  名字冲突的冗长诊断（含对端 sid）下沉到服务端日志。
- 未预期内部异常回 `500` + 笼统文案，原文只进日志（此前把 `str(e)` 原样回给客户端）。
- **入房失败的效应判据由「顶层有没有码」收敛为「是否校验类」（#212，`is_validation_rejection`）**：
  `room-model.md` 明写「校验必须先于副作用」⇒ 只有 `400`/`403`/`4101`/`4105`/`4106` 才保证
  「既有成员关系未被改变」，回退到「已确认房」才成立。`500` 是 handler catch-all，**可发生在成员关系
  已提交之后**（入房广播抛错 ⇒ 服务端按提交点收敛为无房）——旧判据此时会让客户端宣称仍在旧房；未知码
  同理 fail-safe。二者改按「未获裁决」处置（Agent 双清空；Computer 清 desired、保留
  `_confirmed_office_id`，与它既有的「无码」支同形）。新增常量 `JOIN_VALIDATION_REJECTION_CODES`。
  > 代价（已裁决接受）：`500` 若发生在**提交之前**，双清空会多丢一次恢复机会（旧房号不再回退）——
  > 协议未覆盖此处，按「不臆断、不撒谎」取双清空。
- **`emit_update_*` 在「回放在途」窗口内误放行（#223）**：判据只看 desired ⇒ 窗口内照发，而服务端**新会话
  尚无 `office_id`** ⇒ `require_office_id` 拒收；这四个事件是 fire-and-forget、无 ack ⇒ 客户端**无感**，
  房内 Agent 静默漏掉这几类变更。详见上面 Added 条目的判据 + 合并补发。
- **Computer 侧补上「会话纪元」守卫，堵住两条会把 `_confirmed_office_id` 写脏的路径（#223 隔离审查）**：
  「成功落账」在同一会话内刻意不受操作抢占约束（#213），但成员关系**属于会话**——跨会话的一律不得落账。
  两条实测可达的脏写：①**显式 `join_office`** 等 ACK 期间断连（断连钩子是内联同步触发的，ACK 就绪与钩子
  清账可同拍）⇒ 那条「成功」属于已销毁的会话；②**退房**在等 office 操作锁期间通知失败（`emit` 抛）⇒
  临界区之后的清理被整段跳过（现改为 `finally`，异常照旧透传）。两者都会留下「服务端会话无房、本地却
  自称已确认在房」的幻影态，使上面的判据放行一批注定被丢弃的上报**且不记入待补发**（变更真丢）。
  新增字段 `_office_session`（镜像 Agent 侧的 #217 裁决 5：`generation` 只管操作抢占，会话作废另立纪元——
  用 `generation` 守卫落账会连「同一会话内被抢占的成功」一起否掉，#213 明令不得）。

### Changed

- **`SMCPComputerClient.update_config` 与 `emit_update_config` 收敛为同一实现（#223）**：`update_config`
  此前**无守卫**（无条件发送），与其孪生 `emit_update_config` 行为分叉；现委托后者（同判据、同补发）。
  宿主若曾在「未确认在房」时依赖它把包发上线，请改用其它通道——那个包本来也会被服务端丢弃。
- **`SMCPComputerClient.leave_office` 不再因未连接而抛异常（#223）**：改为**先无条件清本地意图**
  （清 desired + 已确认房号、作废在途自动回房），再在 namespace 在册时通知服务端；不在册时记 DEBUG 并
  正常返回（服务端会话已随连接销毁，本就没有可退的成员关系）。此前「先发包后清」在断线时抛
  `BadNamespaceError` ⇒ 清理语句执行不到 ⇒ 本地意图残留 ⇒ 重连后自动回房把用户刚退掉的房又回了一遍。
  CLI `socket leave` 相应改为「desired 有值就必须真的退」（不再因「未连接 / 未在房」跳过），文案区分
  「已离开房间」与「未连接：已清除本地入房意图，重连后不再自动回房」。

## [0.4.0] - 2026-08-25

> **A2C-SMCP 协议 v0.4.0 GA 实现**。SDK 包版本 `0.4.0`，`PROTOCOL_VERSION` 同步为 `0.4.0`。

### Added
- **CLI `plugin validate` / `marketplace validate` 配置校验命令**（#193）：`a2c plugin validate <path>` /
  `a2c marketplace validate <path>` 校验 marketplace 根或 plugin 目录的配置，零副作用（只读校验，不做任何
  写入/安装），退出码 0=通过 / 非 0=有错，`--json` 输出结构化诊断，供自动化管线部署前把关。
- **`client:put_blob` 上行写入通道**（#196，protocol#12/PR#53）：Agent 侧新增 `create_put_blob_request()`
  （async + sync 双面），构建上行 blob 写入请求（`PutBlobReq`），打通 Agent → Computer 的大对象上行通道，
  与既有下行 `drain_blob` 对称。
- **A2CSkillRef.tags 加性字段透传**（#198，protocol#50/PR#55 裁决）：`client:get_skills` 返回的
  `A2CSkillRef` 新增可选 `tags`（SKILL.md frontmatter `tags` 透传，分类元数据；非 `list[str]` → 字段省略，
  纯透传不校验）。
- **动态 Socket.IO auth provider 暴露**（#200，方案 C 原生透传）：`AuthProvider` 类型面从
  `a2c_smcp.computer` 导出，`SMCPComputerClient` 支持宿主在运行期注入/更新连接鉴权 provider，不再限于
  构造期静态配置。
- **ToolMeta 三层合并：Server 声明 tags**（#199，镜像 protocol#51 / PR#57 裁决）：MCP Server 可在
  `Tool._meta["a2c_tool_meta"]` 声明默认 `tags`，合并语义升为
  `tool_meta[tool] > default_tool_meta > Server 声明`（tags 整体替换不 union；缺失/null 继承、
  `[]` 显式清除）。`available_tools` 对每个 Tool 无条件 reconcile 并校验声明，畸形声明（非对象 /
  `tags` 非 `list[str]`）丢弃 + warning 诊断（每 server 每次刷新至多一次），MUST NOT 令 `tools/list` 失败。

### Breaking Changes
- **`a2c_tool_meta` 白名单化：Server 声明仅 `tags` 生效**（#199，裁决意图）：`auto_apply` / `alias` /
  `ret_object_mapper` 等白名单外字段**不再经 Server 声明透传**——字段级过滤，reconcile 后彻底消失
  （终值恒为 Computer 写入的 canonical，全字段含 null）。下游第三方若曾依赖 `Tool._meta["a2c_tool_meta"]`
  的偶然原生透传（如 Server 自声明 `auto_apply=true`）将失效，请改由 Computer 配置侧指定。

## [0.3.3] - 2026-08-20

> **A2C-SMCP 协议 v0.3.2 GA 实现**。SDK 包版本 `0.3.3`，`PROTOCOL_VERSION` 同步为 `0.3.2`。
> 主线：PickString options 结构化 `{label, value}`（旧 `string[]` 直接拒绝）+ 实际
> start/restart 从 raw 声明重解析 input（镜像 protocol#48 / rust-sdk#187+#193）。

### Breaking Changes
- **PickString `options` 结构化：`list[str]` → `list[PickStringOption{label, value}]`**（#192，协议
  `data-structures.md` 0.3.2 破坏性变更）。
  - 旧 `"options": ["a", "b"]` 字符串数组形式以 `validation` 错误**直接拒绝**（报错指路新结构），
    **无 alias、无迁移期**；model 层与 wire TypedDict 层双收口（REPL `inputs load` / plugin 入池均生效）。
  - `options` 至少一项；`label` / `value` 非空；`label` / `value` 允许重复（MUST NOT 按 value 反推 label）；
    `default` 若存在且非 None MUST 匹配至少一个 option.value（显式 `null` 视为无默认）。
  - 交互选择表展示 `label`、注入 `value`。

### Changed
- **实际 start/restart 从 raw 重解析 input**（#192，runtime-contract §5.13）：boot / mount 不再 eager render
  （仅形状校验 + 登记 raw 声明）；每次从 stopped 实际启动（含 restart / start-all）从 raw 声明重新解析
  input 再创建 client——用户改选后重启**不再静默沿用旧渲染品**；运行中不热更新；幂等 start no-op。
  restart 解析失败 → 结构化错误并**保留仍在运行的旧进程**。
- **取值与失效语义**（§5.12）：已存值（cache / env / value store）不匹配任一 option.value → 结构化
  `InvalidSelectionError`（携带 `id` + `value`），**不回退** default / 首项；真正无用户值 → default →
  首项 value（**不反向持久化**）；command 输入仅在实际启动时执行（每次实际启动恰执行一次）。
- 交互 prompt 时机随渲染点后移：`server add` / boot 不再弹输入提示，提示发生在实际 `start` 时
  （auto-connect 流程下无感知差异）。

## [0.3.0] - 2026-07-22

> **A2C-SMCP 协议 v0.3.0 GA** 实现。SDK 包版本 `0.3.0`。
> 主线：server-name-as-identity 历史残留彻底清除——`bundle_id` 成为 MCP Server
> 唯一身份/主键/路由键，`name` 降为纯展示（允许同名合法共存）。三方共识见
> a2c-smcp-protocol Discussion #23（F1–F8 终审，无遗留裁决）。
>
> 注：v0.2.0 / v0.2.1 发版时 CHANGELOG 未单独切段，本段累积 [0.1.5a1] 以来的全部
> 变更。其中 v0.2 协议面（连接握手 `a2c_version` / `client:get_resources` /
> `window://` URI 纯标识符化 / DPE 移除）已随 v0.2.x 发布；本版本（v0.3）新增为
> BundleID 身份模型、两套 scope 层序统一、env 命名全迁、审批门档④根治、上游授权
> 错误 4006/4007 surfacing、plugin install/enable 分离。

### Breaking Changes
- **MCP inventory projection re-keyed to `bundle_id`; `managedBy` is now pure-derived** (#144, mirrors
  a2c-smcp-protocol `data-structures.md` identity-orthogonality + `runtime-contract.md` §4.8 and
  Discussion #23 F1 / F2; rust mirror tracked under rust-sdk#129).
  - `McpServerWithMetadata` (`Computer.list_mcp_servers_with_metadata()`) **gains a `bundle_id` field**
    (wire camelCase `bundleId`); `bundle_id` is the identity / primary key, `name` is demoted to pure display
    (collisions allowed, never a key). Clients (e.g. `tfrobot-client`'s MCP tab) can now correlate an inventory
    entry back to `client:get_config.servers` (bundle_id keyed) and tools (`{bundle_id}__{tool}`).
  - **join / dedup / sort are all re-keyed to `bundle_id`.** Two servers sharing a display name but with
    distinct `bundle_id` (legal coexistence, protocol §5.6) no longer collapse into one entry.
  - **`managedBy` is F1 pure-derivation**: `∃ a non-plugin-origin declaration ⇒ user, else plugin`, sourced
    from `Computer.resolve_mcp_declarations()` (origin-carrying, structurally non-plugin). A user's own server
    that shares a `bundle_id` with a plugin dependency is now `managedBy=user` (editable from the MCP tab),
    restoring §2.5 user sovereignty — it was previously mis-labeled `plugin` (read-only).
  - The `McpServerWithMetadata.assemble()` constructor gains a required `bundle_id` keyword. The type stays
    SDK-facing (not on the `client:*` wire). A structural flag-scope difference vs. the human-facing
    `cli.resolve.collect_candidates` (the core `Computer` is `--settings`-flag-less) is documented, not a defect.
- **Input env var naming: `A2C_INPUT_<ID_UPPER>` → `A2C_SMCP_<ENV_SEGMENT(id)>`, hard cut, no dual-read**
  (#155, aligned with a2c-smcp-protocol `computer-mcp-config-guide.md` §"环境变量命名规则（双端统一规范）"
  and Discussion #23 F4 / F5; rust mirror rust-sdk#140).
  - **Prefix `A2C_INPUT_` is abolished**; the id segment is **no longer upper-cased**. `A2C_INPUT_FIGMA_TOKEN`
    becomes `A2C_SMCP_figma_token`. Per F5 there is **no dual-read and no transition window** — orchestration
    layers (CI, containers) MUST update injected env var names. The headless secret error message derives the
    new name on the spot, so it is self-teaching.
  - **One `ENV_SEGMENT` normalizer for every segment** (`a2c_smcp/utils/env_segment.py`, single source of
    truth, byte-identical with rust): per-code-point, `[^A-Za-z0-9_]` → `_`, **case preserved**. Case
    preservation is what keeps `MyServer` / `myserver` — two legal, simultaneously mountable bundle_ids —
    from collapsing onto one variable name. Note `ENV_SEGMENT` **neither folds consecutive `_` nor trims
    edges**, unlike `bundle_id.normalize_name`; the two are distinct functions and MUST NOT be conflated.
  - **Env name collisions now fail fast at registration** (`EnvNameCollisionError`, raised from
    `BaseInputResolver.__init__`). `ENV_SEGMENT` is not injective (`-` and `_` both map to `_`), so
    `figma-token` and `figma_token` resolve to one variable name. Previously this silently cross-fed values
    between inputs — last writer wins, secrets included. Detection is on the **full** variable name; segments
    that collapse while full names differ are harmless and are **not** rejected. Rejected mutations leave
    `Computer` state untouched.
  - Cache / keyring / value-store keys are unchanged (`resolved_id`): the live resolution path passes a bare
    id on both SDKs, so no server context enters the key. Multi-source disambiguation continues to ride on
    prefixed plugin ids (`<plugin>@<marketplace>/<id>`), whose `@` and `/` `ENV_SEGMENT` now normalizes into a
    legal POSIX env name.
- **The two scope-layering orders are unified; `--config` → `--mcp-config`; `--inputs` removed**
  (#154 + #164, aligned with a2c-smcp-protocol `runtime-contract.md` §2.5-3 / §2.5-5 and
  Discussion #23 F6 / Discussion #32; rust mirror rust-sdk#137 / #147).
  - **One source-priority order, low → high — `settings.json` and `mcp.json` MUST agree**:
    `plugin declaration < user < project < local < embed < flag < policy`.
    Previously `settings.json` ranked `flag` **second-highest** while `mcp.json` ranked it
    **lowest** (a `--config` legacy artifact) — four positions apart. The protocol abolishes
    the latter. The order now has a single authority, `SCOPE_ORDER` in `settings/schema.py`,
    which both resolvers iterate; the two hand-written list literals that drifted are gone.
  - **`--config` → `--mcp-config`** (short `-c` kept). It is now the **flag-layer `mcp.json`**,
    forming the flag-scope *file pair* with `--settings` (the flag-layer `settings.json`),
    symmetric with every other scope.
    - **File shape hard-cut**: a bare server object / an array of server objects →
      the `mcp.json` shape `{"servers": {...}, "inputs": [...]}`. Server identity is the
      **map key**; drop the `name` field from the body (a body `name` ≠ key is rejected).
      Old-format files **fail fast with exit code 2** and a rewrite hint — deliberately
      reversing the old silently-degrading behaviour, where a typo'd `--config` path booted
      you into an empty REPL. Validation now also runs **before** any connection is opened.
    - **Precedence flips**: previously overridden by `user`/`project`/`local`; now overrides them.
    - **Now passes the approval gate** (it used to bypass scope merge, origin tracking and the
      gate entirely, mounting directly). `flag` is a trusted origin ⇒ still no prompt, so the
      common case is unchanged — but a `policy` deny-list / allow-list / `disabledMcpjsonServers`
      can now block a `--mcp-config` server. This is protocol-correct (`policy > flag`).
  - **`--inputs` (and `-i`) removed.** The `inputs` segment of the `--mcp-config` file carries
    that role; it is consumed on the same path as every other scope's `inputs`. The legacy
    `--config` schema had **no** `inputs` field, which is why a separate flag existed at all.
    The REPL `inputs` command is a different surface and is unaffected.
  - **`--settings` help text corrected**: it claimed "最低优先级" (lowest priority) while the
    implementation has always ranked it second-highest.
  - **New `embed` scope** — the embedding host's `Computer(mcp_servers=...)` constructor
    argument, a code-level explicit intent ranked between `local` and `flag`, and a trusted
    origin (no approval prompt). It still enters the gate iteration, so `policy` deny-lists and
    the generic disable switch apply to it. **Known gaps** (tracked separately, both stem from
    §5 item 10 putting plugin declarations outside the gate):
    - a pure embedded host (`Computer(...)` + `boot_up`, no REPL) has no approval gate at all,
      so `policy` deny-lists do not reach it;
    - unmounting a policy-denied `embed` server frees its `bundle_id`, after which the governance
      remount may mount a *plugin*-declared server with the same `bundle_id` — the deny-list is
      still circumvented, just by a different config. (Before this change the denied embed server
      simply kept running, so neither state is good; the gate cannot reach plugin declarations by
      protocol design.)
  - **Reclaim criterion re-anchored** (closes a #153 gap): "X is not user-declared" is now
    evaluated on the **origin-carrying** declaration set, which covers *every* non-plugin mount
    path (durable scopes + `flag` + `embed`). Servers mounted via `--mcp-config` or via the host
    constructor are consequently **never** collaterally torn down when a plugin is uninstalled.
    `mcp_json_declared_bundle_ids` → **`non_plugin_declared_bundle_ids`**.
  - **`Computer.aremove_server` now raises `McpWriteTargetError`** when the winning declaration
    lives in a **read-only scope** (`policy` / `flag` / `embed`) instead of reporting success
    while deleting nothing and resurrecting the server on the next boot. Note this **also fixes
    a pre-existing defect** for `policy`-origin servers.
- **plugin ↔ MCP Server is a dependency relation, not an ownership one** (#153, aligned
  with a2c-smcp-protocol `runtime-contract.md` §2.5 / §4.9.1 / §5.6; adjudication record
  in protocol Discussion #23 D3/F1; rust mirror rust-sdk#139 — **both SDKs share the same
  on-disk ledger format, a divergence is data corruption**).
  - **Ledger field renamed and re-typed**: `bundledMcpServers` (array of display **name**)
    → **`mcpServers` (array of `bundle_id`)**, written unconditionally (`[]` when empty).
    It records the MCP servers a plugin **declares a dependency on**. The ledger MUST NOT
    store display names (they go stale when an explicit `bundleId` server is renamed) nor
    point-in-time facts such as provenance/introduced (they rot as *other* plugins are
    uninstalled — the transitive-leak source). **Legacy records are dropped wholesale and
    rebuilt from the `installedPlugins` intent** (§4.9.1-4); no name→bundle_id migration
    mapping is written.
  - **Uninstall/disable/gc no longer reclaim unconditionally.** New criterion (§4.9.1-2,
    a pure function in `reconciler.py`): **reclaim X ⟺ no other plugin declares a
    dependency on X ∧ X is not user-declared**. Consequences: a user's own server is
    **never** collaterally removed, and a dependency shared by several plugins survives
    until its last dependent is uninstalled (no leak).
  - **`MCPServerNameConflictError` removed**, along with `_conflict_check` and the whole
    `owned` notion. Installing a plugin whose declared `bundle_id` already exists is
    **dependency satisfied** → report and install normally (exit 0); it MUST NOT be
    rejected. `plugin enable` **reuses** an already-satisfied dependency instead of
    remounting over it. Same display name with a different `bundle_id` is legal
    coexistence (§5.6). The CLI JSON error code `mcp_server_name_conflict` is gone.
  - **Injected-callback seam is now bundle_id-keyed**: `ExistingServerNames` →
    **`ExistingBundleIds`** (`install_plugin` / `enable_plugin` / `materialize_plugin`
    kwarg `existing_server_names=` → **`existing_bundle_ids=`**); `RemoveServer` and
    `gc_plugins(mcp_teardown=)` now take a **bundle_id**; `Computer.reconcile_governance`
    kwarg `existing_server_names=` → **`existing_bundle_ids=`**.
  - **Dependency prechecks and governance remount now read the runtime-authoritative
    config set** (`manager.server_configs()`), never the construction-time snapshot
    `Computer.mcp_servers` (§2.5-4) — under the CLI that snapshot is permanently empty,
    so every "dependency satisfied" was misjudged as "unsatisfied".
  - CLI output: `plugin install/list/info` field `bundledMcpServers` → **`mcpServers`**
    (values are now bundle_ids).
- **Plugin install/enable separation — install no longer activates** (#123, aligned
  with a2c-smcp-protocol **v0.3.0** `runtime-contract.md` §2.3/§2.4/§4.8; adjudication
  record in #120 / protocol#11; rust mirror rust-sdk#103).
  - **`enabledPlugins` default flipped**: an absent entry now means **not enabled**
    (only an explicit `true` activates; `false` explicitly disables and overrides a
    lower scope). Under v0.2.x, absent meant enabled.
  - New declarative intent **`installedPlugins`** (settings.json array of
    `<plugin>@<marketplace>`): the global install set. `install_plugin` now writes it
    config-first to the **user scope**, materializes (clone / manifest validation /
    MCP dependency precheck / ledger), and **does not activate** — no SKILL
    staging, no bundled-server mount, no `enabledPlugins` write → `installed_disabled`.
    Materialization failure atomically rolls the intent entry back.
  - `install_plugin` signature: **removed** `register_server=` / `remove_server=` /
    `inject_inputs=` kwargs (install mounts nothing); the precheck kwarg is kept but was
    since renamed to `existing_bundle_ids=` and demoted to report-only (see #153 above —
    it no longer rejects). New `materialize_plugin()` exposes activation-free
    materialization (reused by boot re-materialization).
  - **`enable_plugin` is now atomic**: skills and bundled servers light up together;
    on mount failure it rolls back to `installed_disabled` (unregisters newly staged
    skills, removes newly mounted servers via the new `remove_server=` kwarg, restores
    the previous `enabledPlugins` value — absent entries are deleted, not set `false`).
  - `uninstall_plugin` now also removes the `installedPlugins` entry and clears
    `enabledPlugins` entries (user always; project/local derived from recorded
    `projectPath`) once no ledger records remain.
  - **Boot recovery is intent-driven** (`recovery.py`): the install set comes from the
    merged `installedPlugins` (the ledger is a rebuildable derived cache — deleting
    `installed_plugins.json` is lossless: boot re-materializes missing entries, reported
    via the new `GovernanceRecoveryReport.rematerialized`); the active set is
    installed ∧ enabled; `installed_disabled` restores lazily (no projection).
    Orphan detection (`list_orphan_plugins`) now keys off `installedPlugins`
    (`declared_plugin_ids` **removed** in favor of `declared_installed_plugin_ids`).
  - **One-time migration** (`migrate_legacy_installs`, run by `Computer.boot_up`):
    existing ledger installs are written into `installedPlugins`, and plugins without
    any `enabledPlugins` entry get `enabledPlugins=true` in the user scope so
    pre-upgrade active plugins stay active (explicit `false` stays disabled). The
    presence of the `installedPlugins` key in user settings marks migration done
    (written even when empty), so manual intent removals are never resurrected.
  - CLI: `plugin install` prints `installed_disabled` state and mounts nothing;
    `plugin list` shows **all** installed plugins with a two-state enabled column
    (`--available` kept as a compat no-op); `plugin list`/`info` enabled now means
    explicit `true`; `plugin enable` wires `remove_server` for rollback.
- **Connection-plane auth moved from HTTP header to the Socket.IO `auth` dict**
  (#112 / Jira AS-38; Epic TFRM-153). A2C-SMCP is auth-agnostic — **no protocol
  change**; `token` is an SDK/deployment convention aligned with rust-sdk /
  tfrobot-client / TFRS Provider. The credential now travels in the connection
  `auth` dict's `token` field; **HTTP headers no longer authenticate** (routing
  headers such as `X-TF-*` are still passed through, unrelated to auth). This
  **supersedes** the [0.1.5a1] `x-api-key`→`access_token` header default.
  - Constant `DEFAULT_AUTH_HEADER_NAME` → **`DEFAULT_AUTH_FIELD_NAME`** (default
    value `access_token` → **`token`**) in `a2c_smcp.agent` and `a2c_smcp.server`;
    **removed** from `a2c_smcp.computer` (the Computer client holds no credential).
  - `DefaultAgentAuthProvider`: kwarg `api_key_header=` → **`auth_field_name=`**;
    `api_key` is now injected into the connection `auth` dict under `auth_field_name`
    (default `token`) and merged with `auth_data`; `get_connection_headers()` returns
    routing-only headers (no credential).
  - `DefaultAuthenticationProvider` / `DefaultSyncAuthenticationProvider`:
    `authenticate()` reads the credential from the connection `auth` dict
    (`api_key_name`, default `token`) instead of HTTP headers; auth failure rejects
    the connection (`ConnectionRefusedError`).
  - `a2c_smcp.computer.SMCPComputerClient`: **removed** the `auth_header_name=`
    constructor kwarg and the `auth_header_name` property. The connection `auth`
    dict is supplied by the caller via `connect(url, auth=...)` (CLI injects via
    `--auth 'token:...'`).
  - **Migration**: put the credential in the connection `auth` dict under `token`
    — Agent: `DefaultAgentAuthProvider(api_key=...)`; Computer CLI: `--auth 'token:...'`.
    To keep a custom field name, pass `auth_field_name=` (Agent) / `api_key_name=`
    (Server). JWKS/JWT verification is performed by the Server's custom
    `AuthenticationProvider` (e.g. TFRS), not by the SDK default provider.
  - Added `smcp.ConnectAuth` TypedDict (`role` required + `token` `NotRequired`) as a
    protocol-reference type. Note: default providers do **not** inject `role` at
    connect (role is established via `EnterOfficeReq`/`join_office`, consistent with
    rust-sdk); full role-at-connect wiring is a pre-existing protocol-vs-impl
    divergence deferred to a separate protocol-first effort.

### Added
- **Upstream MCP tool authorization-error surfacing (4006/4007)** (#133, implements
  a2c-smcp-protocol `error-handling.md` §4006/4007 + `security.md` and conformance §4.8;
  mirrors rust-sdk's `build_auth_error_result`, rust-sdk#120/#150). When a tool call fails
  due to **upstream** MCP authorization, the Computer returns a `CallToolResult(isError=True)`
  carrying result-level `meta`: `error_code` (4006 = authorization required / 4007 =
  authorization failed, mapped per the protocol decision table — HTTP 401→4006, 403→4007,
  OAuth token refresh/exchange failure→4007, never-configured/other OAuth flow→4006),
  `mcp_server` (the failed server's **bundle_id**, so the Agent can correlate to a specific
  server and distinguish "needs auth" from "tool is broken"), and a non-sensitive `auth_hint`
  (`{action, message}`). `a2c_smcp/computer/mcp_clients/auth_error.py` provides the pure
  `classify_auth_error` (also recognizes OAuth exceptions, walking `__cause__` +
  `BaseExceptionGroup` but deliberately not the implicit `__context__` chain) and
  `build_auth_error_result`, wired into `MCPServerManager.acall_tool`.
  **Transport-layer capture (the reactive classifier alone is unreachable for HTTP):** the
  mcp Python SDK swallows a `tools/call` 401/403 into its streamable-http task group and
  tears down the connection, so `session.call_tool` hangs (never raises) and the reactive
  classifier never sees it. `HttpMCPClient` therefore injects a custom `httpx_client_factory`
  (`_AuthWatchingClient`) that observes 401/403 at the transport layer (before mcp's
  `raise_for_status`), correlates the signal to the in-flight call by JSON-RPC id, and races
  the hung `call_tool` against the signal — on arrival it cancels the hung call and raises a
  typed `UpstreamAuthError` (→ classifier → 4006/4007), satisfying the protocol's
  "MUST NOT hang to timeout" (§可观测判据). Per-client `call_tool` is serialized (a
  concurrent-call `_request_id` race was caught in isolated review). **Coverage:** conformance
  §4.8 scenarios 1–3 (initial-response 401/403, the dominant case); scenario 4 (POST 200 +
  in-stream 401) is a documented follow-up — mcp-python surfaces stream-death opaquely, needing
  SSE body interception. A2C does not drive the upstream OAuth handshake (owned by the MCP
  library/host); proactively predicting "never authorized" before a call is out of scope.
- **Plugin lifecycle follow-ups** (#125, closing out the #123 isolated-review items;
  rust mirror evaluation via rust-sdk#103):
  - **Re-materialization scope inference** — boot recovery now infers the original
    install scope of rebuilt ledger records from per-layer `enabledPlugins` entries
    (and project/local `installedPlugins` declarations) instead of always normalizing
    to the user scope; multi-layer clues rebuild multiple records, dead stale-scope
    records are swept, and clue-less rebuilds are normalized to user scope with a
    WARN plus the new `GovernanceRecoveryReport.scope_normalized` field. This keeps
    `plugin disable`/`uninstall` writing to the correct settings layer across boots.
    `uninstall_plugin` additionally clears cwd-visible project/local `enabledPlugins`
    entries (guarded so it never creates a `.tfrobot/` dir in a bare cwd). Known
    blind spot (documented): layers of *other* project paths are not visible from
    the current cwd — precise restoration remains pin-lock territory (§4.9.2).
  - **Dangling-intent diagnosis & prune** — new `list_dangling_plugin_intents`
    (reconciler) detects `installedPlugins` entries with no live materialization that
    are statically unreachable (four reasons: `marketplace-not-added`,
    `catalog-missing`, `manifest-unreadable`, `entry-missing`; reachable-but-not-yet
    materialized intents are reported as recoverable and self-heal at next boot).
    `plugin gc` now reports them (JSON: `removed` unchanged + new `dangling`,
    `prunedIntents`, `recoverable`) and can prune via the new
    `installer.prune_plugin_intent` — REPL behind the confirm gate, non-interactive
    Typer requires the explicit `--prune-dangling` flag (pruning deletes
    authoritative intent, unlike orphan gc which only drops derived cache).
    Committable project/local `installedPlugins` declarations are never rewritten
    (WARN with the file path instead).
  - **Ledger liveness now validates bundled JSON** — `ledger_entry_materialized`
    (public in reconciler, migrated from `recovery._ledger_materialized`) treats a
    record as materialized only if the `installPath` directory exists **and**
    `load_bundled_servers` parses; a corrupt bundled server JSON now triggers
    re-materialization (repair) or keeps the plugin wholly `installed_disabled`
    (no more "skill lit, server WARN-skipped" half-state).

- **Connection protocol-version handshake** (#17 / #18). Clients (Agent + Computer,
  async + sync) auto-append `a2c_version` to the Socket.IO connect URL query;
  `a2c_smcp.PROTOCOL_VERSION` (`"0.2.0"`) is the single source. Server-side
  `a2c_smcp.server.A2CProtocolVersionASGIMiddleware` validates it: missing/invalid →
  HTTP 400; incompatible (MAJOR.MINOR mismatch) → HTTP 400 + Socket.IO `4008`.
  Incompatible clients are rejected and **never connect** (`versioning.md` §4
  loop-defense); `4008` is normalized to `a2c_smcp.exceptions.ProtocolVersionError`.
  Negotiated version is surfaced via `SessionInfo.a2c_version` through `server:list_room`.
- **`client:get_resources` event** (#14, async + sync). Transparent passthrough of
  MCP `resources/list` with caller-controlled `cursor` pagination (SDK does **not**
  auto-paginate). Flat `ErrorPayload` on `4014` (MCP Server not found) / `4015`
  (no `resources` capability). Response keys normalized camelCase→snake_case.
- `base_client.list_resources_page` single-page passthrough API (#13).

### Changed
- **`window://` URI is now a pure identifier** (v0.2). `priority` / `audience` /
  `last_modified` move to MCP `Resource.annotations`; `fullscreen` and other A2C
  extensions move to `_meta`. `WindowURI` no longer parses query; `organize_desktop`
  reads metadata from `annotations` / `_meta` (#9 / #11). Desktop aggregates
  `window://` only — non-window resources never enter the desktop.
- `Finder` removed in favor of the transparent `client:get_resources` event.
- office/role isolation invariants in `on_client_*` now raise
  `a2c_smcp.exceptions.SMCPNamespaceError` instead of `assert` (stripped under
  `python -O`); async + sync namespaces aligned (#31).

### Deprecated
- `plugin list --available` is a no-op since v0.3.0 (listing all installed plugins is
  the default) and now emits a deprecation notice (non-JSON mode; JSON mode logs a
  warning to keep stdout parseable). Planned for removal in a future release (#125).

### Tests & Docs
- `tests/e2e/test_v02_full_flow.py` (#20): real-process full chain over a real
  Uvicorn ASGI server + real protocol-version middleware + real stdio MCP
  subprocesses — covers handshake negotiation (compatible connects + `a2c_version`
  recorded; incompatible rejected), `window://` desktop aggregation, `client:get_resources`
  pagination with transparent passthrough, and the `client:tool_call` chain.
- **4008 loop-defense test mapping** (#20): no dedicated `test_v02_handshake_loop.py`
  is added — the `versioning.md` §4 infinite-reconnect-defense guarantee
  ("incompatible client never connects") is already covered by
  `tests/integration_tests/test_version_handshake_client.py`
  (`assert *.connected is False`), and the exact `4008 → ProtocolVersionError`
  normalization contract by `tests/unit_tests/test_handshake_4008_normalization.py`.
  `tests/e2e/test_v02_full_flow.py::test_v02_full_flow_incompatible_handshake_rejected`
  re-checks the guarantee at real-process level. Each layer asserts at its own
  deterministic tier (non-flaky), so a separate redundant file would add no signal.
- `README.md`: added the SDK ↔ A2C-SMCP protocol compatibility matrix and v0.2
  protocol-MUST rules. `CLAUDE.md`: event-system conventions updated with the v0.2
  events (`client:get_resources`, connection handshake) and URI/metadata changes.

### Removed
- **DPE scope removed entirely** per a2c-smcp-protocol v0.2.0 GA decision
  (see [`a2c-smcp-protocol/CHANGELOG_DPE_REMOVAL.md`](https://github.com/A2C-SMCP/a2c-smcp-protocol/blob/main/CHANGELOG_DPE_REMOVAL.md)).
  DPE moved to an independent
  [dpe-protocol](https://github.com/A2C-SMCP/dpe-protocol) repository; the A2C-SMCP
  control plane no longer routes, parses, or validates `dpe://` URIs. Although
  this DPE code (introduced in unreleased commits 675c753 / 4223515) never
  shipped in a published version, the rollback is recorded here so future
  cross-SDK protocol-history reviews can correlate Python ↔ Rust SDK timelines:
  - `a2c_smcp.utils.dpe_uri` (`DPEURI`, `is_dpe_uri`) — entire module deleted.
  - `a2c_smcp.smcp`: `GET_DPE_EVENT`; `GetDPEReq` / `GetDPERet`;
    `InlineContents` / `ExternalContents` / `ResolverContents` / `ResolverHint` /
    `ResolvedResource`; `DPEResolutionFailedCategory`; `ErrorCode` members
    `DPE_RESOLVER_NOT_CONFIGURED` (4011) / `INVALID_DPE_URI` (4012) /
    `DPE_RESOLUTION_FAILED` (4013); `ErrorPayload` fields `category` / `dpe_uri`.
- **MCPServerManager host reverse-index machinery removed.** The original
  motivation was routing `client:get_dpe` by host; with that event gone and
  protocol host-uniqueness softened to `SHOULD` (lint-style WARN, non-blocking),
  the index has no protocol-level consumer. Removed: `HostConflictError`,
  `find_server_by_host`, `aretry_pending_host_index`,
  `_host_to_servers` / `_host_index_pending` state, and the registration-time
  conflict detection inside `_astart_client` / `_astop_client` / `_clear_all`.

### Kept
- Non-DPE v0.2 protocol surface stays intact: `PROTOCOL_VERSION`,
  `GET_RESOURCES_EVENT`, `A2CResource`, `ResourceAnnotations`,
  `GetResourcesReq` / `GetResourcesRet`, `ErrorCode` members 4006 / 4007 /
  4008 / 4014 / 4015, `SessionInfo.a2c_version`.
- `WindowURI` parser, `organize_desktop` reading from
  `Resource.annotations` / `_meta`, and `base_client.list_resources_page`
  single-page passthrough (now serving only `client:get_resources`).

### References
- Protocol decision: [`a2c-smcp-protocol/CHANGELOG_DPE_REMOVAL.md`](https://github.com/A2C-SMCP/a2c-smcp-protocol/blob/main/CHANGELOG_DPE_REMOVAL.md)
- Independent DPE protocol: [A2C-SMCP/dpe-protocol](https://github.com/A2C-SMCP/dpe-protocol)
- Tracking issue: [#8](https://github.com/A2C-SMCP/python-sdk/issues/8)
  (closed sub-issues: #10 / #12 / #15 / #16; remaining v0.2 work: #14 / #17 / #18 / #19 / #20)

## [0.1.5a1] - 2026-04-23

### Breaking Changes
- Default auth HTTP header key changed from `x-api-key` to `access_token` across
  `a2c_smcp.agent`, `a2c_smcp.computer`, and `a2c_smcp.server`. A2C-SMCP is
  auth-agnostic; this aligns the SDK defaults with the TuringFocus ecosystem
  convention (Envoy is configured with `headers_with_underscores_action: ALLOW`).
  To keep the previous default, pass `api_key_header="x-api-key"` to
  `DefaultAgentAuthProvider` and/or `api_key_name="x-api-key"` to
  `DefaultAuthenticationProvider` / `DefaultSyncAuthenticationProvider`.
  No protocol change.

### Features
- `a2c_smcp.computer.SMCPComputerClient`: `namespace` and `auth_header_name` are
  now constructor-configurable (previously hardcoded to `/smcp` / `x-api-key`),
  exposed via read-only `namespace` / `auth_header_name` properties, and the
  default `emit` namespace falls back to the instance value.
- `a2c_smcp.agent.AsyncSMCPAgentClient` and `a2c_smcp.agent.SMCPAgentClient`:
  `namespace` is now a constructor kwarg that drives both handler registration
  and the default namespace for every `emit`/`call` site. `connect_to_server`
  accepts an optional `namespace` override; when provided it updates the
  instance namespace and re-registers event handlers before connecting.
- Each SDK module now exports a `DEFAULT_AUTH_HEADER_NAME = "access_token"`
  constant (`a2c_smcp.agent`, `a2c_smcp.server`, `a2c_smcp.computer`) for
  consumers who want to reference the default without hardcoding a literal.

### Fixes
- CLI: `a2c-computer run --namespace` was previously ignored by event handlers
  (only the underlying Socket.IO connect used the override). The CLI now
  propagates the namespace into `SMCPComputerClient`, so event subscriptions
  bind to the requested namespace as intended.

### References
- Aligns with Rust SDK v0.1.15 handshake configurability.
