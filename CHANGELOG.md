# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) versioning.

> 注：v0.3.1 / v0.3.2 发版时未单独切段（Bugfix / OAuth 收敛类），本段累积至 [0.3.0]。

## [Unreleased] — #214 房间事件失败 ack 契约（协议 v0.5.0）

> **A2C-SMCP 协议 v0.5.0 实现**：`PROTOCOL_VERSION` 由 `0.4.0` 抬至 `0.5.0`（v0.x 下 `MINOR` 严格匹配，
> 抬位后握手层才会正确拒绝 `0.4.x` 对端）。SDK 包版本（`__version__`）由发版流程另行推进。

### Upgrade Notes（0.5.0 → 0.5.1：marketplace catalog 子树恢复）

- **0.5.0 上卸载过相对 source 插件的环境需要手工恢复（#267，镜像 rust-sdk#278）**：0.5.0 的
  `plugin uninstall` / `plugin gc` 会删除插件的安装目录；相对 source（`"source": "./plugin"`）插件落在
  marketplace 的**共享** clone 内（`<A2C_SKILL_HOME>/marketplace/<mp>/<rel_path>`），连带删掉父子 / 同源 / 多 scope
  的文件，其后再安装报 `plugin root not found after resolve`。0.5.1 起卸载不再删除该共享子树，但**已损坏的本地
  clone 不会自愈**。
- 恢复路径：`a2c-computer marketplace remove <name>`（默认级联卸载其下所有已安装插件并撤销 trust）→
  `a2c-computer marketplace add <git-url> --trust` → 逐个 `plugin install <plugin>@<name>` + `plugin enable <plugin>@<name>`。
- **不要使用 `marketplace remove --keep-plugins`**：它保留 installed 状态与账本，留下指向已删 clone 的孤儿记录。
- `marketplace refresh`（`refresh=true`）**会**还原上游仍存在的被删文件（#260 起为「重新 clone 到 staging + 原子换入」，
  不再原地 `git pull`）；但上游本身已删的路径、以及本地 clone 与上游一致时仍无变化。`plugin install` 与 boot 重物化
  都复用既有 clone。
- 详见 [`docs/upgrade-0.5.1-marketplace-catalog-recovery.md`](docs/upgrade-0.5.1-marketplace-catalog-recovery.md)。

### Breaking Changes

- **marketplace 注册名改为清单 `name`（#260，镜像 rust-sdk#272）**：协议 `runtime-contract §5.14` 只约束
  「名称唯一性 / 禁静默改名」（派生算法属 SDK 自治，protocol#79）。行为变更：
  - **`marketplace add` 的注册名取 `marketplace.json` 顶层 `name`**，不再从 URL 末段派生，也与 `--name` 无关。
    `<plugin>@<marketplace>` 中的 `<marketplace>` 随之取清单名——仓库名与清单名不同时，插件 id 会改变。
  - **`--name` 不再是别名**（D6 B）：只作「期望清单名」校验，与清单不符即 `validation` 失败。
  - **`--no-clone` 对新源一律 `validation`**（D5：无 pending、无临时派生名）；仅对已注册源或可确认的本地
    缓存生效。
  - **交互式 `marketplace add` 一律弹 trust 确认**：`trustedMarketplaces` 不再豁免 prompt；trust 在**注册成功后**
    才写入 user settings（注册失败零残留，settings 损坏仍在 clone 前 fail-closed）。非交互恒需 `--trust`（不变）。
  - **同名不同源 → `validation` 拒绝**（不替换、不自动后缀）；同名同源重复 add 幂等；同一 source 已以旧名注册时
    新名 add → `validation`（诊断给旧名与处置）；remove（含 `--keep-plugins`）后 re-add 且仍有
    `<plugin>@<name>` 意图引用 → 拒绝（登记已删、无法证同源，同源 re-add 亦 fail-closed）。
  - **`marketplace refresh` 遇清单改名 → `validation` 结构化失败**（不再报 updated/unchanged），旧内容 / 账本 /
    运行时能力保留；legacy 注册（账本无 `manifestNameBaseline`）首次成功 clone/refresh 记 baseline、保留旧名、
    不报改名拒绝，仅 baseline 之后再改名才拒绝。refresh 改为「重新 clone + 原子换入」，不再原地 `git pull`。
  - **declared 换源拒绝**：`extraKnownMarketplaces` 中同名 source 变更 → `validation`，**不** wipe / 重 clone；
    换源需显式 remove / re-add。
  - **`--json` 错误载荷形状变更（breaking）**：此前为 `{"error": "<人读文案>"}`；现在统一为
    `{"error": "<类别>", "message": "<人读文案>", …诊断}`，机读原因在 **`reason`** 键（`error` 为
    `validation` / `error`）。类别与 reason 取值：`validation` × {`manifest-name-invalid` /
    `manifest-name-changed` / `name-conflict` / `source-conflict` / `expected-name-mismatch` /
    `dangling-references` / `unresolvable-name` / `invalid-name` / `invalid-source`}。
    消费方需改为读 `message`/`reason`（旧的「整个 `error` 就是文案」读法不再成立）。
  - **存量迁移不在本版**：已有 URL 派生名 / `--name` 别名的注册**不追溯改名**，由 #268 的
    `marketplace migrate` 处理（账本字段 `manifestNameBaseline` 已就位供其消费）。
- **flag / policy 层的 `<plugin>@<name>` 引用纳入 §5.14 re-add 守卫（#296，镜像 rust-sdk#267 item 3）**：
  此前 `intent_reference_pids` 只读 user/project/local，`--settings` flag 层、policy 层与实例 cwd 轴不在视图内
  ⇒「policy/flag 有引用 → `marketplace remove` → 同名不同源 re-add」可绕过 §5.14 的 MUST 拒绝。现在：
  - 非交互 CLI 的**根级** `--settings`（flag 层）参与守卫；policy 层由库层按 `env` 现解析。REPL 的
    marketplace / settings / plugin 三个分派器统一经 `settings_axes_of(comp)` 跟随运行中 Computer 实例的
    env / cwd / flag / policy（settings 的 `show/get/set/edit` 也读取实例 `--settings`、实例 cwd 与实例
    policy），不再各自读 ambient `os.environ` / 进程 cwd。
  - **行为变化**：此前能成功的同名不同源 re-add，在 flag/policy 仍存在引用时现在以 `validation` +
    `reason: dangling-references` 失败（`details` 仍为 `{"name","references","referenceCount"}`，不新增层来源）。
  - **库层默认对齐 rust#300**：`policy_settings=None` 时按 `env` 现解析 ambient policy（`resolve_policy_settings`）
    ——`add_marketplace_source` / `commit_marketplace_registration` 的公开调用方即使不显式接线也不会漏掉 policy 层；
    显式传入 mapping 覆盖，`{}` = 显式「无 policy」（测试与嵌入式宿主的 hermetic 开关）。
  - 范围：显式 `marketplace add` 与 `--no-clone` 两处守卫完成 cwd / flag / policy 接线；复用路径
    （`_record_known_marketplace`）与 reconcile / recovery / installer 的 **cwd / flag 接线**、错误 details
    层来源为后续跟进。注意 `policy_settings=None` 的库层默认值使**所有**提交路径都会读到 policy 层。
    迁移（#268）不绕过守卫，也不追溯改写 flag/policy 旧引用——见
    [`docs/upgrade-0.5.1-marketplace-name-migration.md`](docs/upgrade-0.5.1-marketplace-name-migration.md) §6.5。
- **一房至多一台 Computer：「每 role 一席」（#230，protocol#66）**：房间收紧为一房 ≤1 Agent、≤1 Computer
  ⇒ 一个 Agent 同一时刻至多连一台 Computer。无新事件、无能力字段（随 0.5 MINOR 握手生效）。
  - **第二台 Computer 入房被拒 `4101`**（此前允许任意多台）：不替换、不驱逐已在房的 Computer；同名与否都一样。
    换绑 = 旧 Computer `leave_office` → 新 Computer `join_office`（Agent 观察到 `notify:leave_office` →
    `notify:enter_office`）。
  - **`4101` 泛化为「本 role 席位已占」**，ack 载荷字节级对齐协议示例：
    `{"code":4101,"message":"Room already has an agent"|"Room already has a computer","details":{"office_id":…,"role":…}}`
    （服务端载荷中 `details.role` = 发起者自己声明的 role，即被占席位；客户端 `SMCPProtocolError`
    同时带出 `details`——`4101` `{office_id, role}`、`4106` `{office_id}`，按 `code` + `details` 分流，
    见 #234）。`build_room_rejection_error(4101, …)` 要求
    `declared_role` 必填；`RoomFullError` 构造改为 `RoomFullError(role)`。
  - **`4105` 收回为预留码**：服务端不再产出（同 role 同名先撞席位 ⇒ `4101`），`build_room_rejection_error`
    构造上拒绝 `4105`（与 `4102` 同款）；**`NameConflictError` 删除**（未发布即收回，不留别名）。客户端收到
    `4102` / `4105` ⇒ 记协议违规告警、按非校验类拒绝处理、**不重试**。
  - **重连回房的瞬态重试集合由 `{4101, 4105}` 收敛为 `{4101}`**（`TRANSIENT_JOIN_CONFLICT_CODES`；
    `JOIN_VALIDATION_REJECTION_CODES` 同步去掉 `4105`）。
  - **服务端准入改由席位表原子判定**：新增 `_seat_to_sid: (office_id, role) → sid`（`server.types.SEAT_KEY`）
    与反向索引 `_sid_to_seats`。`enter_room` 在**任何副作用之前**（含 Computer 换房的「自动离开旧房」）原子
    「检查 + 占席」（async 零挂起点 / sync 注册表锁内）⇒ 并发加入空房恰一者成功，输家在动旧房之前即被拒
    （此前「竞争输家短暂入房、换房者已先退旧房」的已知边界随之消失）；失败即回滚新占席位，退房 / 断连 /
    relay 自愈时释放。名字注册表只承担路由解析：键被他人持有 ⇒ `RegistryInvariantError`（`500`），不伪装成协议码。
    Server 子类化 API：`_ensure_name_registerable` 删除，改为 `_claim_seat(office_id, role, sid)` /
    `_release_seats(sid, office_id=None)`；Agent 占用判定不再扫描 socketio 房成员。
  - **幂等重入统一**：会话已在目标房再次 join（Agent / Computer 同）⇒ 空 ack、不重复广播 `notify:enter_office`。
  - **Agent API**：新增 `get_computer_in_office(office_id) -> SessionInfo | None`（async / sync）；房内列出多于
    一台 Computer ⇒ 判服务端协议违规抛 `ValueError`（不挑其中一台）。**`get_computers_in_office` 已废弃**：
    仍可用（委托新方法，返回 `[c]` / `[]`）并发 `DeprecationWarning`，将在后续版本移除——**使用方请迁移到
    `get_computer_in_office`**。rust-sdk 侧同步新增同名单数方法（rust-sdk#232）。
  - 一致性：新增 `tests/integration_tests/server/test_single_computer_room_conformance.py`，对 async / sync 两种
    服务端线上覆盖 room-model §一致性测试场景全部 9 条。
- **Agent 显式 `join_office` 改为等 ACK（#218）**：`AsyncSMCPAgentClient.join_office` /
  `SMCPAgentClient.join_office` 由「无 ack 的 `emit`」改为 `call`，三类后果：
  1. **可能抛**：服务端裁决为拒绝时抛既有 `SMCPProtocolError`（`.code` 可机器分流 `400/403/4101/
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
  自动回房共用该有界等待常量；Computer 显式 join 起初未纳入，本版审查收尾时已纳入（见 Changed）。
- **三个房间事件的失败 ack 改为 flat `ErrorPayload`**（#214，protocol#61）：
  `server:join_office` / `server:leave_office` 的**成功 = 空 ack**（`None`），失败 = 顶层含 `code` 的
  flat `ErrorPayload`；`server:list_room` 成功仍为 `ListRoomRet`，失败为 flat `ErrorPayload`。
  **`(bool, str | None)` 元组形态已废除**（该形态的 ack 载荷字节序列与 arity 均变化：由两参 ACK
  变为零参 / 单 dict）。
- **`a2c_smcp.utils.parse_join_ack` 返回类型变更**（SDK 公开 API）：由 `tuple[bool, str]` 改为
  `JoinOfficeVerdict(ok, code, message)`，便于下游按 `code` 机器分流。**刻意不再兼容**旧元组形态——
  `MINOR` 严格匹配使跨版本对端物理上不可能互联，兼容分支只会掩盖未迁移的调用方。
- **名字唯一性键空间收敛为 `(office_id, role, name)`**（#215，protocol#61 Q3）：唯一性改为**房内同 role**
  （协议 MUST NOT 施加全局唯一）。用户可感：**跨 office 同名**、**同名 Agent + Computer** 不再被误拒；
  同房同 role 同名由「每 role 一席」先拦为 `4101`（#230，见上条）。`client:*` 路由改为在**发起者所在房内**解析——目标只在他房 / 名字属于
  Agent 时立即回 flat `404`（与「不存在」逐字节相同，不泄露他房成员存在性），此前为抛异常致调用方挂满超时。
  未入房发起者的路由拒绝现为 flat `4103`（#216，见下条）。
  - Server 子类化 API 签名变更（不留兼容）：`get_sid_by_name(office_id, role, name)`、
    `_register_name(office_id, role, name, sid)`（`_ensure_name_registerable` 已由 #230 删除，见上条）；
    `_name_to_sid_map` 键改为 `(office_id, role, name)` 元组（`server.types.NAME_KEY`），新增反向索引
    `_sid_to_name_key`：`_unregister_name` 按 sid 注销、不再从可变会话字段反推（杜绝回滚后残留键永久占用），
    并内置归属守卫（只删本 sid 持有的键）。
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
- **`join_office` 身份声明一致性补齐 name 半（#221）**：Server（async + sync）的身份判据由「role 不符」扩为
  「role **或** name 不符」⇒ flat `ErrorPayload(403)`（`events.md:610`，与 rust 对齐），仍在任何会话写入与
  房间副作用之前。**同一连接上改名再入房不再可能**——改身份须新连接（`faq.md:162`「重连或换 sid」）。
  - 空名归一单点同源：`server/utils.py::default_session_name`（两处 `enter_room` 与两处身份判据共用）。
  - CLI `socket join <office> <name>` 与本连接已尝试过的名字不同 ⇒ 透明重连四步（离开旧房 → 断开 → 以新名
    重连 → 入房）；建连失败回滚 `comp.name`，入房被拒保留那条连接。
  - `403` 文案泛化为 `"Role or name mismatch with existing session"`；`ConnectionArgs.namespace` 收敛为必填
    `str`（存生效值，避免重连时显式传 `None` 覆盖默认命名空间）。

### Added

- **CLI 治理恢复分三类展示（#294）**：`a2c-computer` REPL 启动期的 `run_governance_remount` 此前只打印
  `failed_marketplaces`，另外两类信号只在日志里（日志级别调高 / `A2C_SMCP_LOG_SILENT` 时不可见）。现在
  **只消费 `GovernanceRecoveryReport` 字段**（不重新检测、不重新解析 settings），分三类各打一行，文案锚点
  各不相同、不合并计数：
  - **声明错误**（配置错误，**未参与**恢复）：`⚠ settings.json[<scope>]: <field> — <reason> (declaration ignored; existing registration unchanged)`；
  - **源冲突**（声明源 ≠ 已注册源，仍按**旧源**恢复）：`⚠ marketplace '<name>': declared source differs from, or cannot be proven equivalent to, the registered source; …`；
  - **降级**（`failed_marketplaces`）：`⚠ marketplace '<name>' degraded during governance recovery (skills/servers not restored)`（沿用 #287 起的既有文案）。
  - 冲突与降级**只展示脱敏后的市场名**（name-only，两侧 source 明细仍只在 recovery 的逐条 `DEBUG` 里）。
    文案与顺序均对拍 rust `cli/commands/mod.rs`：**降级 → 源冲突 → 声明错误**，文本逐字一致（仅引号风格不同：
    python `!r` 给 `'acme'`、rust `{:?}` 给 `"acme"`）。rust 只有前两条是同名纯函数（降级行内联），python 侧
    三条都提成纯函数以便单测钉措辞。
  - **每条记录一行**（0 条不打、n 条 n 行），三类不合并计数。
  - 行文本一律经 `rich.markup.escape()`。这不是纯粹的防崩溃：rich 会把 `settings.json[<scope>]` 里的
    `[<scope>]` 当样式标签**吞掉**（实测未转义时只剩 `settings.json:`，scope 信息丢失），`field` 里的
    `[/]` / `[bold]` 同样会消失；形状不巧时还会抛 `MarkupError`。可达的注入点是**声明行的 `field`**——
    校验器用原始 JSON 键拼点路径，而 `sanitize_marketplace_name` 保留 `[`/`]` ⇒ 手编 settings 键即可注入。
    **既有降级行此前没有转义，本版一并修掉**。
  - **不提供 `--json`**：`run_governance_remount` 是 REPL 启动路径的内部函数（非 Typer 子命令），root `--json`
    不覆盖该路径，输出仅面向人类。
  - 同一次启动可能出现多条同义提示——`WARN` 是库契约，本版不回收（用户 2026-11-22 裁决接受）。常见 2–3 条
    （boot `WARN` + 重挂 `WARN` + 新 CLI 行）；当 `.tfrobot/mcp.json` 解析出 ≥1 个 server 时还会多一条
    `run_mcp_approval` 的 `format_settings_errors` 行（**第 4 条**，其措辞与脱敏由
    [#295](https://github.com/A2C-SMCP/python-sdk/issues/295) 接管），合计约 4 条（见设计文档 §7 已知边界）。
- **`marketplace migrate`：存量 marketplace 注册名迁移（#268 / D3）**：把 0.5.1 之前以 URL 派生名 /
  显式 `--name` 别名注册的 marketplace 迁移到清单顶层 `name`，并同步改写全部引用面：
  - 权威面（user scope）：`installedPlugins` / `enabledPlugins`（含显式 `false`）/ `trustedMarketplaces` /
    `blockedMarketplaces` / plugin-scoped input 值（`input-values.json`）/ keyring secret；`extraKnownMarketplaces`
    默认只报告，`--update-declarations` 才改写 user scope。project/local 一律**只报告不改**（输出文件、JSON 指针、
    建议值与后果）。
  - 派生面：`known_marketplaces.json` 新记录（`installLocation` 重算、`manifestNameBaseline` 重置为新名）、
    catalog clone 与 `.plugins` 外树搬迁、账本记录按新 pid 重建（重建成功才删旧记录）。
  - **默认 dry-run、零写入**（`--apply` 才写盘）；迁移全程离线。
  - 退出码：`0` 成功 / `1` 冲突**整体 abort**（零写入）/ `3` 部分完成（unresolved / blocked /
    keyring 不可用 / project-local 手动引用）。REPL 只允许 dry-run。
  - 幂等可续（无 journal / 备份 / 回滚）：崩溃后重跑会自动认领「`known[new]` 已写、目录未搬」与
    「目录已搬、旧键未删」两个窗口；无法归因的孤儿目录不会被认领。
  - 详见 [`docs/upgrade-0.5.1-marketplace-name-migration.md`](docs/upgrade-0.5.1-marketplace-name-migration.md)
    与 [`docs/design-0.5.1-marketplace-name-migration.md`](docs/design-0.5.1-marketplace-name-migration.md)。
- **`marketplace info` 新增 `manifestName` / `manifestNameInvalid` / `legacyName`（#268 / §10.4）**：
  自查注册名是否为 legacy；清单名属不可信输入，非法 / 疑似凭据一律不原值回显。
- **boot 治理恢复诊断声明源变化（#287，语义镜像 rust-sdk#295；检测范围按 2026-11-20 用户裁决收窄）**：
  此前 boot 只按 `known_marketplaces.json` 的已注册源恢复，用户改了
  `extraKnownMarketplaces[name].source` 后**静默沿用旧源**、无诊断。现在：
  - `GovernanceRecoveryReport` 新增 additive 字段 `marketplace_source_conflicts：
    list[MarketplaceSourceConflict]`，条目含 `marketplace` / `declared_source_display` /
    `known_source_display`（三者均为**展示值**：账本键走 `redact_ledger_key`，两侧 source 走
    `display_source`，不可用于再克隆）。
  - 检测范围 = **`installedPlugins` 意图涉及**的 marketplace（无安装意图的市场不检测，空意图早退行为不变）；
    无声明条目、或无**可用** known 记录（缺失 / 记录非映射 / `source` 非映射）的市场不在此报告——后者由
    stage 循环记入 `failed_marketplaces`，但**全惰性市场**（`installed_disabled` ∧ 账本完好）在 stage 循环
    里零动作 `continue`，因此两处报告都不出现。
  - 同源判据 = `same_source`（SDK 单一权威，与 `reconcile` 同一函数）：**只做字符串级比较**，
    `ssh://` / `https://` / scp-like / `.git` 后缀等等价写法**不归一**，任一侧面目不可识别即视为不同源
    ——协议 §5.14「无法确定时 MUST fail-closed」。
  - 可见面：recovery `WARNING` 汇总（冲突数 + **脱敏后的市场名列表**，不含 URL）+ 逐条 `DEBUG`（两侧脱敏
    URL）；`Computer.boot_up` 的恢复汇总行纳入冲突数。CLI `run_governance_remount` 的展示当时留 follow-up
    [#294](https://github.com/A2C-SMCP/python-sdk/issues/294)——**已由 #294 实现**（见同段 `### Added`；
    这是与 rust-sdk#295 的两处有意差异之一，见设计文档 §6）。
  - **只诊断、不换源**：恢复仍用已注册源 stage，旧能力保留，`failed_marketplaces` 与降级语义**不变**。
    boot 自动换源即 §5.14 禁止的「静默替换」；换源只经显式 `marketplace remove` → `add`。与 #268 的迁移
    模型互不替代——该迁移**仅由显式 `marketplace migrate` 触发**（boot 不执行它，也不跑 `reconciler.reconcile`），
    且不改 `extraKnownMarketplaces`。
  - **已知边界（已由 [#293](https://github.com/A2C-SMCP/python-sdk/issues/293) 收口）**：当时若声明本身
    **不合法**（`source` 缺失 / 非 `git` / URL 畸形，如少写 scheme），settings 校验会把该声明整条丢弃，
    boot 只看到「无声明」⇒ 仍静默用旧源、无诊断。#293 起这类声明进入
    `GovernanceRecoveryReport.marketplace_declaration_errors` 并打 WARN 汇总（见 `### Fixed`）。
    详见 [`docs/design-0.5.1-boot-recovery-source-diagnostics.md`](docs/design-0.5.1-boot-recovery-source-diagnostics.md)。
- **按路径读取 `client:resolve_path` + `readRoots` 授权（#236，protocol#65）**：新增事件
  `client:resolve_path`（`ResolvePathReq` / `ResolvePathRet`）与错误码 `4020 Path Not Accessible`
  （`details.reason ∈ {invalid_path, forbidden, not_found, not_a_file}`，开放枚举；resolve 成功后
  拉取期的撤权 / 删除仍走 `4018 forbidden` / `gone`，**`4018` 枚举不变**）。
  - **授权边界 = settings `readRoots`**（默认空 = 不可读，fail-closed）：仅受信 scope 可设，
    `project` scope 提供该键**整字段拒绝**；跨 scope 按数组语义拼接去重；逐项须为绝对路径且通过
    `normalize_path`（宿主平台）形态校验，非法项**逐条**过滤并记配置诊断。（0.5.1 起改为
    `normalize_path` **单一判据**，见 `### Fixed（#304 item 3 …）`。）
  - **授权每请求现读、无进程内缓存**：拉取期 `get_blob` 按**当前** `readRoots` 与**当前文件系统状态**
    重施围栏（覆盖 resolve 后的撤权、root 符号链接换指向）；正确性不依赖宿主调用任何失效入口。
  - **固定拒绝集**（即便在 `readRoots` 内也拒绝）：`.skillenv`、SDK 配置/状态/凭据目录
    （含 `--settings` / `--mcp-config` / `.tfrobot` / SKILL Home / blob 缓存根 / keyring 文件后端）、
    未定稿 `.part`。
  - `resolve`（对全量字节算 sha256）与 `get_blob` 的 path 分支均 offload 到 worker 线程，
    避免阻塞 socket.io 事件循环；path 句柄在 `finally` 中关闭（fd 泄漏守卫）。
  - Agent 侧：`SMCPAgentClient.resolve_path` / `AsyncSMCPAgentClient.resolve_path`；Server 侧走统一
    `_relay_client_call` 路由（与其它 `client:*` 一致）。
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
  在秒级内就重连重放 ⇒ 必然撞上 `4101`（旧会话仍占着本 role 的席位；#230 起 `4105` 为预留码、不再重试）。
  协议把这种成因定义为**瞬态冲突**，并只对「本端刚经历传输层重连」的恢复路径放行**有界**重试（`error-handling.md`
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
- **MCP 启动并发门（#208）**：单个启动 / 批量启动（`start all`）/ Plugin 治理恢复共享同一个 Computer 级
  并发门（`a2c_smcp.computer.mcp_clients.start_gate.McpStartGate`，FIFO、关闸即令排队者失败，语义对齐 rust
  `mcp_start_gate.rs`）。
  - 新增 CLI 参数 `--concurrency N`（root callback 与 `run` 均可用；宿主运行时策略、不落盘；缺省 = 保持
    串行的既有行为，`0` 按 `1` 处理）。
  - 新增 `Computer.with_mcp_start_concurrency(n)` / `MCPServerManager.with_mcp_start_concurrency(n)`：
    **构造期策略**，须在 `boot_up` 与任何启动 / 挂载尝试之前安装，之后调用抛 `RuntimeError`（fail-closed）。
  - 新增 `MCPServerManager.astart_clients_batch(bundle_ids) -> list[StartOutcome]`（逐项结果，全部尝试）；
    `ainitialize` 改为「先收集再批量启动」，失败时报首错（对齐 rust `start_all_mcp_clients`）。
  - manager 锁层次重构为单向三层（门 → per-bundle 生命周期锁 → 状态锁）；更新 / 重启 / 停止 / 移除与同
    bundle 的启动事务全序化。
- **Agent `emit_tool_call` 支持宿主取消信号 + 三态分类（#209，protocol#58）**：`emit_tool_call(..., cancel=None)`
  末位追加参数，只投递 `server:tool_call_cancel` 信号、仍以原 ack 为终态（不本地合成 `a2c_cancelled`）。
  新增公开面 `CancelSignal`（只要求 `is_set()`）、`ToolCallOutcome` + `classify_tool_call_outcome`（与 rust
  逐条一致：cancelled > timed_out > isError > completed）、`TOOL_CALL_TIMEOUT_ERRORS`。
- **`tools/list_changed` 推进 `Computer.capability_revision`（#197）**：manager 以含 description /
  inputSchema / annotations / meta 的全字段投影指纹判定「投影是否真实变化」，`arefresh_tools()` 返回该
  bool；失败 bundle 两侧对称剔除（临时故障不读成移除、恢复不抖出虚假 revision）。对齐 rust PR #199。
- **自动重连后恢复 Office 成员关系（#203，Computer + Agent async/sync）**：房间成员关系属于会话，断线重连换
  新 SID 后客户端自动重放 `server:join_office`（desired + generation 模型，镜像 rust-sdk#204）。手工断开 /
  服务端踢出 / 重连彻底放弃清空回房意图；新增 `a2c_smcp/utils/office.py`（入房 ACK 判定与有界超时的单一
  权威）。
- **Computer 稳定唯一身份 `computer_id`（#235，protocol#64）**：Computer 首次连接生成随机 UUID v4，持久化到
  `<config>/a2c/computer-id`（`$XDG_CONFIG_HOME` 优先，否则注入 env 的 `HOME/.config`；文件 0600、新建目录
  0700；并发首启原子；损坏 / 不可读写 ⇒ fail-fast，不静默临时值），跨重连 / 重启复用。
  - **wire**：`EnterOfficeReq.computer_id`（`role="computer"` MUST 携带、Agent MUST NOT 出现该键）；
    `notify:enter_office` / `notify:leave_office` 与 `server:list_room` 的 `SessionInfo` 对 Computer 回显，
    Agent 省略该键（不得为 `null`）。形态：小写规范 UUID，整串且恰 36 字符，拒绝 nil / max。
  - **Server**：`400`（缺失 / 非法 / Agent 携带）、`403`（同一 sid 改值）；身份三元组 `(role, name,
    computer_id)` 在首次校验通过时原子记录，`4101`/`4106`/`500` 与 `leave_office` 均不回滚。
  - **重置**：SDK `SMCPComputerClient.reset_identity()`（已连接 / 有入房意图时拒绝）、REPL `identity reset`、
    独立 CLI `a2c-computer identity reset`（原子替换 + 重启提示）；新值只经新连接生效。
  - 新增包级导出 `ComputerIdentityError`（持久化不可读写 / 损坏 ⇒ 明确报错终止启动，CLI 非零退出）。
  - **破坏性**：v0.5.0 起 Computer 入房不携带 `computer_id` 将被 `400` 拒绝。

### Fixed

- **enable 失败回滚未恢复「操作前状态」，且失败不结构化（#289，对齐 [protocol#83](https://github.com/A2C-SMCP/a2c-smcp-protocol/issues/83) / [Discussion #82](https://github.com/A2C-SMCP/a2c-smcp-protocol/discussions/82) 裁决 C，镜像 rust-sdk#306）**：
  `plugin enable` 失败时此前只保证恢复本 scope 的 `enabledPlugins` raw 值，不判定合并视图、不清理 register
  失败的 server、且 register / staging 阶段的裸异常会以 traceback 逃出 CLI。现按 runtime-contract §2.4 对齐：
  - **预检失败零写**：被写 scope 的 raw pre-state 保持（absent 不写 `false`、`false` / `true` 精确保持）；
    有效 prior-true 时既有 skills / server / 能力不动。
  - **写后失败**：本 scope `enabledPlugins` 精确恢复调用前原值（absent → 删键）；本次 stage 的 SKILL 撤销、
    操作前既有 orphan / 活跃条目保持 pre-state；只清理**本次 attempt 新挂**的 server（含 register 失败者
    ——manager 先登记 config 再启动、启动失败不回滚登记，此前会留下「未启用却出现在运行期权威配置集」的
    半态），按 §4.9.1-2 判据分类（可回收 / 只移除本 plugin 贡献；用户声明与他 plugin 依赖**折叠同档**，与 rust 一致）记日志。
  - **结构化诊断**：回滚**前 ∧ 回滚后**合并视图（`user < project < local`）均 enabled → 抛
    `EnableFailedRetainedError`（消息带 `existing enabled state retained`；`--json` 增
    `enabledStateRetained: true`；`reason` 保留 #296 的源 reason 如 `manifest-name-changed`，源无自有 reason
    或为通用归一值时给 `enable-failed-retained`）。其余 enable 失败统一为 `PluginInstallError`——裸异常
    （register / staging 回调、宿主 `existing_bundle_ids` 等）归一时 `--json` 的 `reason` 为
    **`enable-failed`**（新机读键值）；原异常保留在 `__cause__`，CLI 不再吐裸 traceback。异常文本先脱敏
    （pydantic `ValidationError` 只报字段/条数，不透出 `input_value`——渲染后配置可能含已解析 secret）。
  - **顺带修复 enable 挂载失败链路上的既有 secret 日志泄露**（不在本单原始范围，复审发现）：MCP client
    连接/保活失败日志此前原样输出**渲染后**的 `server_parameters`（`env` / `headers` 值、`command` / `args`、
    URL 的 userinfo/query/**path 段**都可能含已解析 `${input:secret}`）；`Computer` 渲染/校验失败、boot 初始化
    失败、治理重挂失败、REPL 启动展示与**分发兜底**（`server add` / `inputs` 预校验）、`settings` 校验错误的
    `reason`、plugin input 畸形告警、输入 resolver 失败，此前都可能原样带出 pydantic `ValidationError` 的
    `input_value` / traceback。现统一走新增的 `a2c_smcp.utils.diagnostics`（`loggable_params` fail-closed
    打码：保留字段名与 args 条数、URL 只留 `scheme://host[:port]`；`pydantic_error_text` / `exception_text`
    只报字段/条数、自身不抛；REPL 文案另经 rich 转义），各落点均有 caplog / 断言回归锁。连接失败日志同时携带
    **非 secret 的运行期身份**（bundle_id），避免打码后无法定位 server；DEBUG 级参数渲染改为惰性守卫。
  - **回滚健壮性加固**：写 `enabledPlugins=true` 若在 `os.replace` 成功后收尾抛错，现无条件 best-effort 恢复
    raw pre-state 再判定 retained；SKILL 回滚改为**逐条** best-effort（单条失败不放弃其余条目）。
  - **retained 判定纳入 ambient policy**：库层缺省 provider 在 `user < project < local` 之上加进程级 policy
    （admin 强停 `enabledPlugins: false` 时不再把「有效 disabled」反向误报成 `enabledStateRetained: true`）；
    flag 层仍由宿主 / CLI 注入。一次性 CLI 与 REPL 共用 `commands.make_settings_provider`（单一实现，避免
    #296 同款层组合漂移）。
  - conformance：`conformance-tests.md` §5 的预检 ①②③ 与写后 ④/④b 四景新增 Python 映射用例（含跨 scope
    `user=true / project absent` 失败后 bundled server verdict 回 `ENABLED`，并配 project-only `PENDING` 正对照）。
- **非法 marketplace 声明被静默丢弃（#293）**：`extraKnownMarketplaces` 的声明若不合 schema（漏 scheme、
  `owner/repo` 简写、`source.type` 非 `git`、缺 `source`），`validate_settings` 会把它**整条丢弃**，而
  `Computer` 从不读 `ResolvedSettings.errors` ⇒ 用户把声明 URL 改出 typo 后重启，boot 只看到「无声明」，
  既不报冲突也不报错，仍静默按旧源恢复。
  - 新增 `GovernanceRecoveryReport.marketplace_declaration_errors: list[MarketplaceDeclarationError]`
    （字段 `scope` / `marketplace` / `field` / `reason` / `source_path`，**逐条 + scope**，不聚合
    「该市场无声明」；**含无安装意图的市场与 flag 层**），镜像 rust 同名 DTO。
  - 与 `marketplace_source_conflicts` **正交、不重复计数**：被丢弃的声明不在 `declared` 视图里，冲突检测
    看不到它；恢复源 / `failed_marketplaces` / 降级语义 / 账本**完全不变**。
  - 可见面：recovery `WARNING` 汇总（条数 + **脱敏后的市场名列表**）+ 逐条 `DEBUG`；
    `Computer.boot_up` 汇总行新增 `M marketplace declaration errors`，且该计数**单独即可触发**汇总行
    （「零恢复 + 有错误」不再静默）。
  - **显式 `declared=` 契约**：`Computer.reconcile_governance(declared=...)` 现接受
    `ResolvedSettings`（携带 `.errors`，并取其 `.per_scope_layers`）与裸 `Mapping`（遗留形态、
    无错误、无 per-scope 层，docstring 已注明）；CLI `run_governance_remount` 改传整对象并**返回**
    report（additive），供 [#294](https://github.com/A2C-SMCP/python-sdk/issues/294) 的三类展示消费。
  - **行为变化（修正）**：显式 `declared=ResolvedSettings` 路径此前丢弃 `.per_scope_layers` ⇒
    `enabled_origin` 恒 `None`，project-scope 激活的 bundled MCP server **绕过** #159 Option B 的
    PENDING 审批门被自动挂载。现与 `declared=None` 路径同门（与 rust 一致）：这类 server 落
    `pending_bundled_servers` 等用户批准，不再静默挂载。裸 `Mapping` 调用方无 per-scope 信息，
    行为不变。**已知边界**：CLI 目前对 bundled PENDING 只打 INFO 日志、**没有批准入口**
    （`run_mcp_approval` 不覆盖 bundled 声明面）——审批由 client / 宿主承担，批准 UI 另开单跟踪
    （见设计文档 §7）。
  - **脱敏**：`field` 的市场名段走 `redact_ledger_key`（段漏斗，比散文脱敏更严），`reason` 走散文脱敏，
    两者再经控制字符 / 方向覆写（U+202E 等 Cf 族）清洗；`source_path` 的判据是「**能否排除 userinfo**」——
    不能就整值 fail-closed，能则走路径轴脱敏保留结构。判据 = 剥掉 Windows 盘符后「最后一个 `@` 在第一个
    `:` 之后」，在原文与每一轮 `percent-decode → NFKC` 形态上各判一次（覆盖控制字符穿插、`#`/`?` 夹在 `:` 与
    `@` 之间、段内空白、凭据内字面 `/` 与 `\`、percent-encoded、全角、多层编码）。负对照：Windows
    `alice@corp`、纯 `100%25done`、`?` 尾部照常展示（详见设计文档 §6.1）。
    `settings show` 的凭据回显仍是 [#295](https://github.com/A2C-SMCP/python-sdk/issues/295)，本版不改其行为。
  - 与 rust 的实现口径差异（脱敏落点 / flag 过滤 / 名字脱敏强度 / `source_path` 脱敏强度）见
    [`docs/design-0.5.1-boot-recovery-source-diagnostics.md`](docs/design-0.5.1-boot-recovery-source-diagnostics.md) §6.1。
- **Computer 停机卡死 + 外部取消信号被吞（#211）**：`_keep_alive_task` 拆除窗口内服务端消息到达时
  `aclose()` 抛 `ExceptionGroup`，跳过关闭信号 ⇒ `Computer.shutdown()` 永不返回。关闭信号与状态清理改为
  无条件执行，断开等待加上界（超时强杀子进程）。
  - **行为变化**：新增 `a2c_smcp.utils.cancellation.restores_cancellation`，装饰 `Computer.boot_up` /
    `shutdown` 与 `MCPServerManager.aclose` / `astart_all` / `astart_client` / `astop_all` /
    `astop_client`——下游（如 transitions `process_context`）吞掉的外部取消会在最外层入口**补抛
    `CancelledError`**。宿主 `wait_for(computer.shutdown(), t)` 现在能如实拿到超时信号；此前会「正常返回」。
- **资源变更刷新离开 MCP 接收循环（#210）**：`ResourceListChanged` / `ResourceUpdated(skill://)` 的刷新含
  对同一会话的 RPC，在接收循环内联执行会自阻塞，使该 server 永久失能。改为后台任务 + 按动作分设三个脏位；
  资源刷新改为「本地状态先行」（未加入 office 也推进本地 Registry）。
- **被拒的 `server:join_office` 不再真入房（#213）**：`enter_room` 拆「校验阶段 → 生效阶段」，校验全部
  先于任何成员关系变更（`room-model.md`「校验必须先于副作用」）；生效阶段抛错按提交点收敛；回滚改为字段级
  （只回 `role` / `name`）。Computer 新增 `_confirmed_office_id`，被明确拒绝时回退到已确认房。
- **房间广播目标只取服务端权威状态（跨房间隔离泄漏）**：`server:update_config` / `update_tool_list` /
  `update_desktop` / `update_skills` / `tool_call_cancel` 在发起者未入房时曾以 `room=None` 广播给**整个
  命名空间**；`server:leave_office` 曾把客户端载荷里的 `office_id` 当广播目标（可向任意房注入
  `notify:leave_office`）。统一收敛到 `server/utils.py::require_office_id`（未入房即 raise）；leave 房号
  只取服务端会话，无房可退时幂等成功。

- 失败原因不再以自由文本回传：`message` 使用协议规范文案，`details` 只含**与发起者自身相关**的
  上下文（目标房 / 自己声明的 role / 自己当前所在房），**MUST NOT** 携带任何对端会话标识（`sid` 等）。
  名字冲突的冗长诊断（含对端 sid）下沉到服务端日志。
- 未预期内部异常回 `500` + 笼统文案，原文只进日志（此前把 `str(e)` 原样回给客户端）。
- **入房失败的效应判据由「顶层有没有码」收敛为「是否校验类」（#212，`is_validation_rejection`）**：
  `room-model.md` 明写「校验必须先于副作用」⇒ 只有 `400`/`403`/`4101`/`4106` 才保证
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

- **`enable_plugin` 注入回调成组 + 异常类型统一（#289，嵌入式宿主升级面）**：`register_server` 现要求同时提供
  `existing_bundle_ids` **与** `remove_server`（写后失败必须能运行期摘除本次 attempt 新挂的 server；无摘除需求
  请传 no-op），缺任一即 `PluginInstallError`；enable 阶段异常统一为 `PluginInstallError`（此前可能原样抛
  `RuntimeError` / `SkillSourceError` / 回调自定义异常），按类型捕获的宿主需改捕 `PluginInstallError` 并从
  `__cause__` 取原始异常。
- **`enable_plugin` 的 `remove_server` 回调契约收窄为「运行期-only + 覆盖全部 attempt」（#289）**：写后失败时
  对**本次 attempt** 的全部 server（含 register 失败者）调用，不再只对 §4.9.1-2 判定的可回收者；实现
  **MUST** 仅做运行期 unmount，**MUST NOT** 删 durable 声明（`mcp.json` / settings / 账本）——用户声明或他
  plugin 依赖的条目「只移除本 plugin 贡献、不回收条目本身」。仓内参考实现
  `Computer.aunmount_server_by_id` 本就运行期-only；外部宿主若把该回调接到会删声明的实现，须改用运行期
  unmount。`disable` / `uninstall` / `gc` 的回收调用语义不变。
- **`SMCPComputerClient.update_config` 与 `emit_update_config` 收敛为同一实现（#223）**：`update_config`
  此前**无守卫**（无条件发送），与其孪生 `emit_update_config` 行为分叉；现委托后者（同判据、同补发）。
  宿主若曾在「未确认在房」时依赖它把包发上线，请改用其它通道——那个包本来也会被服务端丢弃。
- **`SMCPComputerClient.leave_office` 不再因未连接而抛异常（#223）**：改为**先无条件清本地意图**
  （清 desired + 已确认房号、作废在途自动回房），再在 namespace 在册时通知服务端；不在册时记 DEBUG 并
  正常返回（服务端会话已随连接销毁，本就没有可退的成员关系）。此前「先发包后清」在断线时抛
  `BadNamespaceError` ⇒ 清理语句执行不到 ⇒ 本地意图残留 ⇒ 重连后自动回房把用户刚退掉的房又回了一遍。
  CLI `socket leave` 相应改为「desired 有值就必须真的退」（不再因「未连接 / 未在房」跳过），文案区分
  「已离开房间」与「未连接：已清除本地入房意图，重连后不再自动回房」。
- **工具列表按 bundle 缓存 + 刷新移出状态锁（#222）**：结构性变更（启动 / 更新 / 移除 / 停止）只重读自身
  失效的 bundle，批量启动 N 个的 `list_tools` 次数由 N(N+1)/2 收敛为 N（实测 210 → 20 次）；状态锁不再跨越
  任何 RPC，另设 `tool_refresh_lock` 串行化整轮刷新。公开入口 `arefresh_tools()` 语义不变（恒全量重读）。
- **`leave_office` 改为等 ACK（Agent async/sync + Computer，v0.5.0 跨节点审查）**：由 fire-and-forget `emit`
  改为 `call(..., timeout=OFFICE_JOIN_TIMEOUT)`（协议 `events.md` 规定 `server:leave_office` 有 ack）。本地意图
  与已确认房号仍**先于发包**清掉（#223 顺序不变）；服务端拒绝时 Agent 抛 `SMCPProtocolError`、Computer 抛
  `RuntimeError`；超时按 `OFFICE_ACK_TIMEOUT_ERRORS` 捕获。新增 `a2c_smcp.utils.parse_leave_ack`（与
  `parse_join_ack` 同一 ack 契约）。Agent 在未连接时调用仍抛 `BadNamespaceError`（意图已先清）。
- **Computer 显式 `join_office` 的 ACK 等待由 socketio 默认 60s 改为 `OFFICE_JOIN_TIMEOUT`（10s）**：此前断线后
  op 锁会被占住 60s，重连回房与 leave 一并卡住。
- **Agent `emit` / `call` 缺省命名空间 = 客户端实例命名空间**：`join_office(office, name)` 等不传 `namespace` 的
  调用此前落到 `/` 命名空间 ⇒ 抛 `BadNamespaceError`（文档示例即此写法），现与 Computer 同口径回退。
- **`OFFICE_ACK_TIMEOUT_ERRORS` / `TOOL_CALL_TIMEOUT_ERRORS` 从 `a2c_smcp.agent` 导出**。
- **sync Agent 的 office 操作锁改为 FIFO**：`threading.Lock` 不公平，后发的 `join` 可能抢在先发的 `leave` 之前，
  最终 LEAVE 把新房退掉而本地仍以为在房。
- **MCP 管理层（v0.5.0 跨节点审查）**：
  - `client:get_tools` 直接服务**已提交的**工具投影（`available_tools()` 读 `_tool_list_cache`），每个 bundle
    只发一次 `list_tools`；单个 bundle 失败不再拖垮整个调用。强刷提交的投影变化经 Computer 单一出口推进
    `capability_revision`（此前被丢弃，紧随其后的 `list_changed` 比对为「未变」⇒ revision 永久少一次）。
  - 公开的 `astop_client` / `aremove_server` / `astop_all` 纳入启动门的生命周期票据（不占并发上限、计入
    `drain()`）：`aclose` 不再在子进程未停下时返回，`aremove_server` 与 `aclose` 并发不再抛 `KeyError`。
    **关闸后调用改抛 `McpStartGateClosed`**（对齐 rust `mcp_lifecycle_gate`）。
  - `_commit_active_client` 的冲突裁决按**声明身份**：用已被替换的声明构造的 client 一律让位（新来者被拒 /
    旧活跃者被退役），两方都是当前声明时先到者保留、后到者断开退役（此前 OAuth 后台连接可覆盖并泄漏，且可能让
    旧配置的进程顶替 restart 后的新声明）。
  - 资源 / 工具刷新一轮失败后不再丢弃本轮工作与期间到达的通知。

### Fixed（v0.5.0 跨节点审查）

- **重连窗口内 `disconnect()` 无效（Computer + Agent async/sync）**：socketio 的 `disconnect()` 在自动重连
  窗口内是空操作 ⇒ 意图不清、重连不停，约 1 秒后自动回到旧房。三个客户端覆写 `disconnect()`：先清意图，
  再中止重连并等待重连任务结束（`a2c_smcp.utils.reconnect`；等待上界 10s，到点记 WARNING 返回，中止位保持置位）。CLI `socket connect` / 改名重连因此不再留下
  CLI 管不到的「幽灵 Computer」。
- **自动重连撞上 `4008`**：Computer 侧 `ProtocolVersionError` 逃出重连任务（任务死掉、意图永久残留）；Agent
  侧被当作普通连接错误**无限重试**。现两端一律停止重连、记 ERROR、经既有钩子清空意图。
- **Agent 工具调用超时分支的取消广播可抛**：断链后等到超时，`emit` 抛 `BadNamespaceError` 逃出
  `emit_tool_call`，`a2c_timeout` 标记丢失。广播改为 best-effort（超时分支与 watcher 共用）。
- **sync Agent `join_office` 入口分两次取锁**：与断连钩子交错时可能静默不发包却残留意图，或复活已清空的意图；
  现单次取锁，与 async 同构。
- **Server 名字注册表原子化**：sync 服务端每个事件一个线程，入房注册与断连清理交错会留下指向死 sid 的条目
  ⇒ 同名重连永久被拒。注册改为一步完成「sid 存活校验 + 判定 + 写入」（sync 加
  `RLock`，async 无 await 段），并发同名 join 只会一个成功；断连改为先广播 leave 再注销；relay 与断连守卫
  统一以「仍解析到同一 sid」复查，目标会话已不存在时回 `404` 并自愈，不再让 Agent 干等超时。断连清理包在
  `try/finally` 里：leave 广播抛错（pubsub publish 失败等）也照常注销名字、唤醒在途调用。
  > 原「并发竞争的输家短暂进入目标房、换房的 Computer 已先退旧房」的已知边界已由 #230 的阶段 1 原子占席消除
  > （见 Breaking Changes）；名字注册本身仍留在生效阶段（前移会让 relay 误报注册表损坏，#213）。

### Fixed（#224 打包依赖 —— 干净安装可用性）

- **`pyyaml` 未声明为运行时依赖，干净安装下 Computer CLI 导入即崩**（#224）：`pip install a2c-smcp`
  后执行 `a2c-computer --help` 在导入阶段抛 `ModuleNotFoundError: No module named 'yaml'`
  （`computer/skills/staging.py` 的模块级 `import yaml`），Computer CLI 完全不可用。
  - **根因**：该 `import` 自 v0.2.1（commit `21e6016`，SKILL 子系统）引入起就是**运行期硬需求**，
    但 `pyyaml` 从未写进 `[project] dependencies`。同一提交只给 mypy 加了 `yaml` / `yaml.*` 的
    `ignore_missing_imports` —— 把「库无类型存根」误当成「库可不安装」，让静态检查对「未安装」主动沉默。
    dev / CI 环境因 `poethepoet`（dev 组）传递依赖 pyyaml 而**恒能 import**，故本地与 CI 从不复现；
    坏包形状自 v0.2.1 起未变，一路发布至 v0.4.0 才由使用者在干净 pipx 环境暴露。
  - **修复**：`pyyaml (>=6.0.2,<7.0.0)` 落入 **`[project] dependencies`（core，非 extra）**——SKILL 是
    协议核心通道（非可选能力），`parse_skill_frontmatter` 由 `Computer.boot_up()` 的**库路径**调用，
    与 CLI 无关，塞进 `cli` extra 只能修一半。对称的 rust-sdk 同样把 `serde_yaml_ng` 声明在
    `crates/smcp-computer` 的依赖里。
    > 未走「自写极简 frontmatter 解析器」路线：frontmatter 非扁平结构，`tags`（`list[str]`）/
    > `allowed-tools`（`list`）/ `metadata`（`dict`）均需覆盖，且偏离协议「YAML frontmatter」措辞。
  - **回归守卫**：新增 `tests/unit_tests/test_packaging_dependencies.py`——断言源码中每个第三方顶层
    import 都有一条**声明路径**（先按 import 名直配声明面，未中再按发行包映射回落，以兼容
    `yaml → pyyaml` 这类改名；只用后者会把「已声明但当前环境未装」的可选依赖如 `keyring` 误判违规）。
    含正对照（防「扫不到东西」的假绿）、marker 求值不变量（PEP 508 marker 必须以 `extra=""` 求值，
    否则会顺着 `mcp → starlette → "pyyaml ; extra == 'full'"` 误判可达，对本 bug 假绿），
    以及 `pyyaml` 必须落 **core** 的单点回归锁（通用守卫不区分 core / extra，无法阻止它被挪进 extra）。
    另有一条「已声明但当前环境未装」的回归锁：CI 的 `uv sync --group dev --group test --extra cli`
    只装 cli extra，`keyring` 等其它 extra 成员**并不在场**，故判据必须是「声明面直配」而非
    「已在环境中安装」——否则守卫会在 CI 恒红并把维护者引向错误的修法。上述每条断言均以变异验证
    （逐个反向改动后确认其真的会红）。
  - **CI 纵深**：`tests.yml` 新增 `clean-install-smoke` job（空 venv 只装制品声明的依赖后跑
    `a2c-computer --help` + 断言导入来源为制品而非仓库源码）；`publish.yml` 在**上传制品前**调用同款冒烟
    ——此前 publish job 只 `uv build`（隔离环境里连包本体都不安装），制品从未被验证过，
    正是「坏包直达 PyPI」的通道。两处共用 `scripts/clean_install_smoke.sh` 单一来源，防止漂移。

### Fixed（#234 入房/退房失败详情）

- **Agent 入房/退房被拒时丢弃协议 `details`**（#234）：`4101` 的 `details.office_id` / `details.role`、
  `4106` 的 `details.office_id`（会话**当前**所在房）此前恒为空——宿主无法从 `SMCPProtocolError` 得知
  当前房号与席位角色，与 rust-sdk 行为不对称。现 `JoinOfficeVerdict` 携带 `details`，显式 `join_office` /
  `leave_office`（async / sync）与自动回房共用同一裁决与日志产出者：
  - `SMCPProtocolError.details` 原样带出 ack 的 `details`（其余码同样透传，不按码特判；无 `details` 仍为 `{}`）；
  - **非 mapping 的 `details` 一律归一为空映射**（新增单一权威 `smcp.normalize_details`，`4014`–`4020`
    全族共用，含 `get_resources` / `get_skill[s]` / `get_blob` / `put_blob`）——对端违规不再把可感的协议
    拒绝升级成 `ValueError` / `AttributeError`，与 rust `SmcpProtocolError::from_value`
    （`as_object` + `unwrap_or_default`）对齐；
  - `details.reason` 仅接受字符串，非字符串视同未提供（与 rust `as_str` 对齐）；
  - 重放路径没有异常通道，拒绝日志（`log_join_rejection`）现同时携带 `details`。

### Fixed（#245 macOS 拒绝集大小写）

- **固定拒绝集在 macOS 上未按大小写不敏感匹配**（#245，protocol#76）：协议要求 Windows 与 macOS 上
  拒绝集的名称判定（`.skillenv` / `.part`）与目录判定 MUST 使用 Unicode casefold（其他平台 MAY）。
  `_is_protected` 此前仅在 `platform == "windows"` 时折叠，而 macOS 的路径形态是 `posix`：`.skillenv`
  与 protected 目录名的大小写变体只在第 8 步 final-path 复核（协议 SHOULD，且依赖 `/proc` 或
  `F_GETPATH` 可用）时才被兜住；**`.part` 名称与 landing 目录的大小写变体没有任何兜底，实测可被读取**。
  现新增单一宿主映射 `_rejection_case_insensitive`（Windows 或宿主 `sys.platform` 为 `win32` /
  `darwin`），`.skillenv` 名称、`.part` 名称与 protected 目录前缀（如 `.tfrobot`）三处一并折叠，
  step 6 / step 8 / 拉取期重跑结构上一致；Linux 及其他平台保持码点（协议 MAY，避免误拒真实存在的
  `.SKILLENV`）。**已知边界**：大小写不敏感但平台串不在上述集合的挂载（如 WSL DrvFs `/mnt/c`、
  Cygwin/MSYS）仍按码点——协议 MAY 不违规，覆盖需卷能力探测，另行评估。readRoots 词法 / realpath
  围栏语义不变（协议要求区分大小写）。rust 侧目录折叠缺口见 rust-sdk#256。

### Fixed（#226 纯 core 导入契约）

- **纯 core 安装（不装 `[cli]` extra）下 `import a2c_smcp.computer` 崩溃**（#226）：`rich` / `prompt_toolkit`
  此前在 `computer.py`、`inputs/resolver.py`、`inputs/cli_io.py`、`utils/console.py` 被模块级硬引用，
  与 #187 的 extra 惰性契约相悖。现改为惰性导入：
  - `import a2c_smcp.computer` 不再要求 cli extra（类基类 / 注解用 `TYPE_CHECKING` + 运行时占位求值）；
  - `utils/console.py` 的 `console` / `console_err` 改为首次访问时创建（PEP 562），保留 #160 的
    实例身份与 `set_no_color` 就地 mutate 契约；`set_no_color` 本身不触发导入；
  - `utils/console` 新增 `console_available()` 谓词与 `__dir__`：缺 extra 时 `hasattr` /
    `getattr(..., default)` 会抛 `ImportError`（PEP 562 语义），可选 UI 降级须先查该谓词；
  - 交互式输入 / 控制台渲染在依赖缺失时抛携带 `pip install "a2c-smcp[cli]"` 的 `ImportError`；
  - 新增子进程回归锁（阻断 rich / prompt_toolkit）与 `clean_install_smoke.sh none` 制品级闸门（CI 双跑）。
  - 注：`a2c-computer` 命令行入口本身仍需要 `a2c-smcp[cli]`（typer）。

### Fixed（#261 marketplace URL 展示脱敏）

- **marketplace CLI 原样展示 `source.url`，userinfo / token 可能外显**（#261，安全）：新增单一展示权威
  `a2c_smcp/utils/git_url.py`（与 rust-sdk 同一展示契约，非镜像）：
  - `list` / `info` / `add` 的表格、JSON、确认提示中的 `url` 改为**展示值**——去除 userinfo（用户名与
    密码 / token）、query、fragment；scp-like `user@host:path` 显示 `host:path`。展示值不可用于 re-clone，
    `known_marketplaces.json` 存储与 git clone/pull 入参保持原始 URL。
  - 错误 / 诊断 / 日志走严格 fail-closed：不可解析 / host 缺失 / scp-like → `<redacted-git-source>`；
    `_run_git` 异常（argv + git stderr）、SSH→HTTPS 回退日志、marketplace source 非法与 clone/refresh
    失败日志均做 token 级脱敏，不再泄漏 git stderr 中的凭据。
  - 与 rust-sdk#273 决策（2026-11-16）对齐；rust#154 fail-closed 边界不变。

### Fixed（#248 可选依赖惰性导入契约统一）

- **两套 extra 惰性导入实现合流为单一 helper**（#248）：新增 `a2c_smcp/utils/optional_deps.py`
  的 `require_extra(module, hint)`——`a2c_smcp.computer`（`[cli]`）与 `a2c_smcp.testing`（`[server]`）
  共用同一套异常粒度与文案格式 `"{hint} (missing: {module})"`。旧路径
  `a2c_smcp/computer/utils/optional_deps.py` 保留为 re-export shim（`require_cli_dependency` /
  `CLI_EXTRA_HINT` 均可用，v0.5.0 已发布面不破）。
  - **对下游可见的行为变化**：`a2c_smcp.testing` 的 runner 在**已装** `[server]` extra 但传递依赖
    损坏（如 `click` / `markupsafe` 缺失）时，改为原样抛出 `ModuleNotFoundError`，不再一律改写成
    「请安装 `a2c-smcp[server]`」——后者把坏安装指成缺 extra，按提示重装依然失败。真·未装 extra 时
    的提示不变，仅追加 `(missing: <模块名>)` 便于定位。
  - `[cli]` 侧文案与行为保持不变。`secret_store.py` 的 keyring 探测降级语义不在本单范围。
  - 打包守卫补位：`uvicorn` 转为动态导入后失去 AST 静态见证，新增「`server` extra 恰含
    werkzeug + uvicorn」单点断言（`tests/unit_tests/test_packaging_dependencies.py`）。

### Fixed（#263 plugin source / validator 错误与日志脱敏）

- **plugin source 解析 / `plugin validate` 诊断 / plugin staging 失败日志 / CLI 未捕获路径回显凭据 URL**
  （#263，安全；#261 未覆盖的同类面）：
  - `SkillSourceError` 改为**构造期脱敏**（对齐 rust `SkillSourceError::new`）：`raw` 与 `reason` 存已脱敏
    文本，`str(e)` / traceback / 日志 / `plugin validate`（文本与 `--json`）自动安全。`raw` 口径改为
    紧凑 JSON（对象形态）/ 原字符串（字符串形态），与 rust 同形；全仓无 `.raw` 读取方，无兼容负担。
  - `url` / `repo` 字段走 `git_url_for_display` 严格展示（可解析 scheme URL 保留 scheme/host/path，去
    userinfo / query / fragment；scp-like / opaque / ftp / 裸 token → `<redacted-git-source>`）；其余
    name / entry / 本地路径按 `redact_git_urls_in_text` 脱敏（合法 kebab 名逐字保留）。
  - `plugin install` 遇 marketplace source 非法：由 `SkillSourceError` 未捕获（traceback）改为 installer
    边界包装成 `PluginInstallError`，CLI 走既有结构化错误输出（红字 + 非零退出码；`--json` 同 payload），
    回滚语义不变。
  - 存储与 git 入参保持原始 URL（`marketplace add` 落盘断言 + `plugin clone` argv 断言）。
  - 对拍 rust-sdk#154 五向量（ftp 带凭据 / `key=` 前缀 URL / scp-like / name 内嵌凭据 / 本地路径内嵌凭据）；
    另覆盖字典**键**脱敏、字段名大小写不敏感、`url` 值被写成数组、控制字符（ESC/BEL/换行）转义。
  - CLI 错误输出对 rich 标记转义（`[/red]` 这类不可信文本不再把结构化错误变成 `MarkupError` traceback）。
  - 复审补口（同一 PR，七轮）：`plugin install` 的 `SkillSourceError` 分支与 boot 重物化 WARN 改走
    `safe_exception_text`（无 scheme 的 `x/r.git?token=…` 不再漏）；`UnicodeDecodeError` 统一归一为
    `<undecodable content (non-UTF-8)>`（不回显原始字节 / 解码细节）；staging 的 `skill_dir.name` /
    `name` / `skill_md` 回显分支补齐路径脱敏。已知降级（本单接受）：对**构造期已脱敏 JSON 回显**再做
    token 级路径脱敏时，值里的 `?`/`#` 会截断尾部——仅影响诊断可读性，不降低脱敏强度（JSON 感知的保真
    处理另立 follow-up）。
  - 复审补口（同一 PR，六轮）：新增 `safe_exception_text` 统一异常文本出口——**裸**异常文本（OSError /
    git / manifest）先散文脱敏再 token 级路径脱敏（scp-like `user@host:path` 只有散文规则能识别，
    上一轮去掉 R 属真实回退），`SkillSourceError` 的 JSON 回显只做 token 级；SKILL.md 读取点的
    `UnicodeDecodeError` 归一（GBK/UTF-16 不再 traceback；mcp 源单 SKILL 失败隔离，不中止整个 run）；
    covered-root DEBUG 的 uri / coverer 与其余 root_uri 分支统一脱敏。
  - 复审补口（同一 PR，五轮）：plugin 源 SKILL.md 不可读分支的 `OSError` 全文与 mcp 源各跳过分支的
    `root_uri` 统一走脱敏漏斗；`_read_json_object` / validator 的 JSON 读取并入 `UnicodeDecodeError`
    （非 UTF-8 manifest 归一为诊断/`PluginManifestError`，不再 traceback）；消费端不再对**构造期已脱敏**的
    文本二次 `redact_git_urls_in_text`（对象源 JSON 回显曾被改写成 percent-encoded 乱码）；散文脱敏只放行
    空格 / `\n` / `\t`（CR、VT、FS、NEL 等空白类 Cc 转义，防日志行伪造）；authority 判据改用 scheme 尾形态
    （`[A-Za-z][A-Za-z0-9+.-]+:`），Windows 盘符 `C:` 不再被当作 scheme。
  - 复审补口（同一 PR，四轮）：路径轴在 `?`/`#` 截断**之前**先判凭据（`alice:pw@host?token=x`
    不再把 userinfo 留在截断前缀里）；authority 判据取代「有 `@` 就遮」，`file:///home/alice@corp/repo.git`
    与 `repo@2.git` 保留；散文 token 一律转义 Cc/Cs（换行/缩进保留），无 scheme 的 `./x/r.git?token=…`
    也路由到路径脱敏；`plugin install` 预检阶段（catalog `marketplace.json` 损坏）同样归一为结构化错误。
  - 复审补口（同一 PR，三轮）：scp-like 判据只看 `@` **之前**是否含 `://`（`alice:PW@host,https://…`
    这类 `@` 在前的 token 仍是凭据）；路径轴把 PAT / username-only userinfo（URL 语境下的 `TOKEN@host`）
    与 percent-encoded 冒号一并按凭据处理，同时不误伤 `alice@corp` 与 Windows 盘符；`plugin install` 把
    `SkillStagingError` / `PluginManifestError` 也归一为结构化错误（仍脱敏）；manifest 的 pydantic 诊断
    改为「字段:类型 + 条数」（不带 `input_value` / URL），stem / name 回显脱敏；`plugin validate` 的
    `--json path` 与人类输出同一口径；message 轴改为**逐 token** 路径遮蔽（保留可读前缀与换行）。
  - 复审补口（同一 PR，二轮）：validator 的 `message` 轴同样过路径级遮蔽（`_Collector.add()` 单一漏斗）；
    `sanitize_path_for_diagnostics` 修正——`/` 与 `\` 都算段边界、query/fragment 从首个 `?`/`#` 起遮蔽余下整条
    路径、仅遮蔽 userinfo 形态（`user:secret@`）不再误伤 `alice@corp`、控制字符先转义；
    散文脱敏跳过已含 `://` 的 token（`file:///home/alice@corp/repo.git` 这类展示安全 URL 不再被吞）；
    `PluginManifestError` 消息与 `stage_mcp_skills` 物化失败分支（原 `exc_info=True`）同口径脱敏。
  - 复审补口（同一 PR，一轮）：`ValidationIssue.file` / `checked` 两条路径轴在 `_Collector` **单一漏斗**脱敏
    （`sanitize_path_for_diagnostics` 逐段遮蔽 `@` / `?` / `#` 片段）；消息一律用**归一化前**的原始 source
    回显（`://` 被路径归一折叠成 `:/` 后 URL 规则会失效，query / fragment 因而漏出）；staging 兜底
    `except Exception` 的 traceback 改为「格式化后整体脱敏」输出（`exc_info=True` 绕不过 logging）；
    非对象 plugin entry 只记类型名；manifest loader 侧 override / ignore 日志同口径脱敏；`raw` 对象形态的
    DEL / C1 兜底转义改用 `\uXXXX`（`\xNN` 不是合法 JSON 转义）。

### Fixed（#272 plugin entry name 路径穿越）

- **`marketplace.json` 的 plugin `name` 未校验即作外部 clone 路径段 → 可删除任意目录**（#272，安全，高危；
  既有缺陷，非 #263 引入）：`_entry_plugin_name` 仅判非空，`external_plugin_dir` 直接把 `entry.name` 拼进
  `<home>/marketplace/.plugins/<marketplace>/<plugin>`，而 clone 落盘前会 `shutil.rmtree(dest)`；`marketplace
  add` / `refresh` / boot recovery / reconciler 均以 `plugin_filter=None` 全量 stage ⇒ 恶意或被投毒
  marketplace.json 写 `name: "../../../x"`（或绝对路径）即可删除任意目录并写入 clone。
  - 修法：`_entry_plugin_name` 按协议 §4.1 严格 kebab 校验（`is_valid_marketplace_name`，固定错误文案、
    不回显原值，对齐 rust `entry_plugin_name`）；`locate_plugin_root` 在 clone/rmtree 之前补
    `is_within(ext_dir.resolve(), home.resolve())` 纵深守卫。显式 `plugin install` 路径本已校验，不受影响。

### Fixed（#281 source 脱敏收尾）

- **`SkillSourceError` 字符串形态的构造期脱敏不完整**（#281，安全；#263/#272 终审 4🟡 收口）：
  字符串 `raw` 只做散文 token 级 `redact_git_urls_in_text`，没有 path/query 轴 ⇒
  `SkillSourceError("x/r.git?token=SECRET#f", …)` 的 `.raw` / `str(e)` 仍带凭据。类 docstring「消费者无需
  打补丁」的承诺因此不成立——仓内 sink 全靠 `safe_exception_text` 兜住，新增消费点会重新开洞。
  - 修法：新增单一漏斗 **`utils.git_url.sanitize_value_for_diagnostics()`**（`escape_unsafe_chars` →
    `redact_git_urls_in_text` → `sanitize_text_for_diagnostics`），**单个不可信值**（非散文）走它。
    本单迁移的站点：字符串 `raw` / `reason`、对象形态 raw 的**每个字符串值与字典键**（`{"path":
    "../x/r.git?token=…"}` 可达；`json_safe` 口径保证 JSON 合法）、`stage_user_skills` 的目录名与
    `reason`、`_build_user_ref` 的 `SKILL.md` 路径与 `frontmatter.name`、发现期 DEBUG 的根/深层路径、
    marketplace clone 的 skill 目录名与 `reason`。
    `escape_unsafe_chars` **必须最先**：散文口径按设计**保留**换行与空白（单值里的换行可伪造日志行），
    且不先转义时 `alice:pw\n@host` 会被切成两半而两半都不命中任何规则（漏遮）；先转义后整串退回单
    token，散文轴（scp-like / `scheme:` opaque）与 path 轴均按整体判定。合法路径 / kebab 名
    （`./../escape`、`https://example.com/r.git`、`file:///home/alice@corp/repo.git`）逐字不变。
  - **单值模式的 token 边界不是空白**：`alice:pw @host`（ASCII 空格 / NBSP `U+00A0` / `U+2028` /
    全角空格 `U+3000`，含 percent-encoded 冒号/空格如 `alice%3A pw@host`、`alice:my pass@host`）会被
    散文轴劈成两半而两半都不命中规则 ⇒ 命中 `user:secret<空白>…@host` 形态时整值 fail-closed 为
    `<redacted-git-source>`（判定前先 `unquote`；不放宽 path 谓词，避免误伤 `note: see @team` 这类散文）。
  - **路径段 / 名字走「单值漏斗 + path 轴」复合**（`sanitize_segment_for_diagnostics`）：basename 天然不含
    `/`，单值漏斗的 path-token 谓词不会触发 `?`/`#` 遮蔽（`foo?token=…` 会原样回显）——故 segment 漏斗
    在单值漏斗之后再补 path 轴。user 源非法目录名与 marketplace 的 skill 目录名（4 处落点）均用它。
  - **非法目录名日志保留定位**：只把不可信的 **basename** 过单值漏斗，parent 走 path 轴（段级遮蔽）——
    既不让 `deploy-token@example.com:org` 这类名称外泄，也不让 `…/build:2024/alice@corp/…` 整行失去落点。
  - **取舍（有意，安全优先）**：`_build_user_ref` 两处**整条路径** sink（`SKILL.md` 路径）走单值漏斗，
    含「冒号早于 `@`」的普通路径（`…/build:2024/alice@corp/…`、Windows `C:\Users\alice@corp\…`）会被整串
    遮为 `<redacted-git-source>`——该行仍有同行的 `%r` 名称标识。
  - **未迁移（既有面，follow-up）**：`validator._Collector.rel()` 的 `issue.file`/`checked` 与 manifest
    override 路径仍是 path-only 轴（scp-like 段不识别）；改它们会变更 `plugin validate` 的既有诊断
    形状，不属本单。home / watcher 的 DEBUG 路径同理。
  - `stage_user_skills` 非法目录名 ERROR 不再裸传 `Path`：scp-like 目录名（`deploy-token@example.com:org`）
    与换行/TAB 形态均被遮蔽或转义（`sanitize_path_for_diagnostics` 不识别 scp-like，故统一走上述漏斗）。
  - `parse_skill_frontmatter` 的 PyYAML 错误改走 `safe_exception_text`：其错误文本会回显出错行片段
    （远端 SKILL.md 的凭据 URL）。`plugin validate` 与 staging 两条路径同时受益。
  - 测试加固：decode 诊断三处恒真断言（`assert "\xff" not in text`——`UnicodeDecodeError.__str__` 只含
    ASCII `0xff`）改为 `<undecodable content (non-UTF-8)>` 精确文案；validator 三处 decode 用例补固定文案
    正向断言与「好 SKILL 仍被 check」正对照；user 源六处脱敏补真实凭据用例（userinfo 与 scp-like 两种
    形态、换行/TAB 控制字符、DEBUG 路径），并统一补**占位符正向断言**（只断言「不存在」时，删掉脱敏参数
    仍会绿）。
  - **空白的取舍（有意，安全优先）**：判据无法区分 `alice: pw@host`（被空白劈开的 userinfo）与
    `contact: alice@example.com`（普通散文），故一律 fail-closed（整值遮）；`note: see @team`
    （`@` 前有空白）仍逐字保留，已入反例向量。
  - 已知边界（不变）：无法按 URL/路径形态识别的 token 原样保留——裸 token（`glpat-…`）与**无分隔符的
    query 形态**（`url?token=…`，如 `{"source": "url?token=…"}` 这类**对象值**；basename 落点不在其内，
    已由 segment 漏斗的 path 轴覆盖）。放宽 path 谓词会误伤 `why?` 这类散文。
  - 已知边界（既有，**非本单引入**，另案）：**percent-encoded 分隔符**把 userinfo 劈开时仍原样保留
    （如 `alice:pw%2Fmore @host` / `alice:pw%5Cmore @host`：`unquote` 后含 `/`/`\`，被段判据排除）；
    `frontmatter.name` 的 DEBUG 回显同理——该 sink 走单值漏斗，`name: foo?token=…` 形态无分隔符、
    path 谓词不触发（改为 segment 漏斗会动这一行的行为面，留待另案）。
  - JSON 感知的保真脱敏（`?`/`#` 截断影响可读性）仍属另案。

### Fixed（#270 hostless git URL 与 marketplace 名称派生）

- **hostless ssh/git/http(s) URL 通过校验，凭据被 slug 进 marketplace 注册名**（#270，安全）：注册名会进入
  CLI 文案 / `--json`、`trustedMarketplaces`、`known_marketplaces.json` 键、`marketplace list` 与 staging
  日志，并经 `A2CSkillRef.source = marketplace:<name>` 下发到 Agent。镜像 rust-sdk#285。
  - **公共 API 收紧**（`settings.is_valid_git_url`，行为变更）：`ssh` / `git` / `http(s)` 必须有**形如主机名
    的 host**——拒绝 `ssh:///alice:PW@example.com`、`git:///x`、`https:///x`、`ssh://user@:22/path`，
    以及把 host 槽位填成非主机占位串的变体（空白 / `-` / `_` / `%20` / 纯点，如 `ssh:// /alice:PW@x`）；
    `file:` 一律不要求 host（`file:///abs/path`、`file://host/path` 均合法）；scp-like 形态不变。
    非法端口 / 畸形 netloc 一并 fail-closed（`file:` 除外——file 全例外，`file://C:\repos\repo` 保持合法；
    比展示层 `git_url_for_display` 更严：展示层仍可展示 `ssh://-/x`，校验不认它）。percent-encoded host
    （`https://ex%61mple.com/x`）有意拒绝；下划线 host、子域、IDN（IDNA 归一）、尾点 FQDN
    （`example.com.`）、IPv6 字面量（含 zone-id）仍合法。既有消费者（`validate_settings`、plugin source
    解析、`marketplace_clone_url`）随之 fail-closed。新增公开谓词 `settings.is_scp_like_git_url()`
    （仅形态分类，合法性仍走 `is_valid_git_url`）；scp-like 形态改按整串匹配判定（尾随换行不再视为合法）。
  - **派生名改解析式**（`marketplace.default_marketplace_name`）：只取解析后 **path 的末段**——userinfo /
    query / fragment 不再参与命名（`https://host/repo?token=SECRET` → `repo`，此前 `repo-token-secret`；
    `git@host:o/r?token=SECRET` → `r`）。末段含 userinfo/密码分隔符（`@` / `:`，如
    `https://host/alice:PW@example.com`）同样不派生，且判定前做 NFKC 归一 + percent-decode
    （`alice%3APW%40host`、全角 `：`/`＠` 等价变体一并挡）——凭据样文本交给 `--name` 显式命名。
    无 path（`https://host`；`https://github.com` 此前派生 `github-com`）、hostless / junk host、非法、
    末段构不成严格 kebab → `None`，CLI 提示 `--name`。scp-like 多冒号取最后一个 `:`（与 rust 一致）。
    判定前迭代「percent-decode → NFKC」至**不动点**，且**残形 percent 转义一律 fail-closed**
    （#282 修正：原固定 3 轮会被四层编码 `alice%2525253APW_LEAK` 旁路；口径对齐 rust-sdk#285
    `seg.contains([':', '@', '%'])`）。故双重 / 三重 / 四层及更深编码、percent-encoded 全角分隔符
    （`%EF%BC%9A` / `%EF%BC%A0`）、**全角百分号**（`alice％３ＡPW`），乃至解码后本可干净的
    `repo%20name`（**行为变更**：此前派生 `repo-20name`），一律不派生名字 —— CLI 提示 `--name`
    显式命名。`_MAX_DECODE_ROUNDS=10` 只保证解码循环终止，**不**承担覆盖面，不存在「N 层以上漏判」的
    深度残余。
  - **`marketplace add` 失败文案不再回显 path 段凭据**：在 #261 展示脱敏之上再挡一层，按 path **任意段**
    判定（含尾斜杠、中间段、编码变体、残形 `%` 与上述不动点解码），命中 → `<redacted-git-source>`。
    判定对象是**原始 source** 的 path 段（渲染才用展示串）：展示层会把空格 / 非 ASCII `quote()` 成 `%XX`，
    若拿展示串当判据，`https://host/团队/repo`、`https://host/my team/repo` 这类干净 URL 会被误整条打码。
    现在它们**不再整体占位符**，按 #261 展示契约输出（`团队` → `%E5%9B%A2%E9%98%9F`、`my team` → `my%20team`，
    即源里本就存在的 percent 转义仍按 `%` 规则 fail-closed，展示层自己 quote 出来的不算）。
    **无 path 时同样整体占位符**（#282 补记，行为自 #270 起即如此；无法判定即为 fail-closed）：
    `marketplace add https://host` 的文案是
    `cannot derive a valid marketplace name from <redacted-git-source>; pass --name`，不展示主机部分。
    **范围仅限 add 失败文案**：
    `marketplace list` / `info` / confirm 提示 / 成功 `--json` 的 `url` 仍按 #261 known-source 契约保留
    path（path 内凭据的外显属 #261 显示契约面，未在本单扩大）。
  - **只防新增**：存量已落盘的凭据 slug 不自动删除 / 改名（迁移归 #268 / #260）；`marketplace remove`
    与 `--name` 显式命名不受影响；`--name` 不能绕过 URL 校验（校验先于命名）。
  - 存储与 git argv 保持原始 URL（raw 保真），展示脱敏（#261）不变。
  - **合并顺序（已满足）**：本改动让 hostless 的 plugin source 新走 fail-closed，该路径的原始 URL 回显面由
    #263 / PR #276 修复，且 **#276 已先合入 develop（`319847c`）**——`SkillSourceError` 构造期脱敏已覆盖
    本单新增的输入，无需另等。

### Fixed（#275 legacy 账本 scoped 卸载残留 + uninstall 失败序）

- **legacy 账本记录的 scoped 卸载不再留下 skills / clone 残留**：库层 `uninstall_plugin(..., scope=...)`（含嵌入
  宿主经公开 API 的调用；CLI 不暴露 `--scope`，见下条触发面）的「存活记录」判据改与落盘视图同源——
  含 `bundledMcpServers` 的 v0.2.x 旧格式记录按协议 §4.9.1-4 会被整条 sweep，
  故**不再算作**「其余 scope 仍安装」的证据。此前该场景会留下账本/意图/enabled 全清、而同进程 registry
  仍注册其 skills（可硬挡换源 enable）、plugin 独占外部 clone 无人引用（后续 `uninstall` 已 no-op、`gc`
  只遍历账本 pid ⇒ 持久磁盘泄漏）的自相矛盾终态。判据以 `store.is_legacy_ledger_record` 单点导出，
  sweep 本体与 clone 门控 / server 回收 / skills 注销共用，避免两处谓词漂移。
- **`uninstall` 失败序：skills 注销后置到 `remove_server` 之后**（`§5.5`）：停摘 server 是本函数唯一可达的
  可抛外部调用（`McpStartGateClosed` 关闸 / 状态机 / 传输异常）。原顺序下它一抛就是「账本说已安装、技能已丢」
  半态；新顺序失败时账本、意图、技能一律原样保留，重试即可收敛（已删 clone 的重试按「缺失即视为已删除」）。
  **非 best-effort**：吞掉停摘失败再提交账本会把「plugin 已卸但 server 仍可调用」写死。
- 触发面：**库层 / 嵌入宿主的 scoped uninstall**——CLI 的 `plugin uninstall` / `marketplace remove` 均不传
  `scope`；v0.2.x 账本在首次成功 boot 的治理恢复中通常已被整体重物化，窗口为「升级后未成功 boot + 库层 scoped
  卸载」。`gc_plugins` 的同款失败序不在本单范围（另案）。

### Fixed（#271 enable 复审残留）

- **identity collapse 的结构化细节改用 `bundleId` 键**：`plugin enable` 预检发现同一 `bundle_id` 对应不同安装根
  （或同根不同配置）时报 `error_code=validation`，`details` 由 `{"conflictWith": <bundle_id>}` 改为
  `{"bundleId": <bundle_id>}`。`conflictWith` 恢复其既有值域——只承载 `<plugin>@<marketplace>` pid（跨
  marketplace 同名冲突）。**按 `conflictWith` 解析 pid 的调用方**若曾把身份坍缩一并处理，需改读 `bundleId`。
- **身份坍缩文案分档**：同根异配置现在明确报 `different configs under the same install root` 并点出两个
  **server 名**（此前只报 `(/a vs /a)` 两段相同路径，无从判断差异）；异根档文案不变。两档均**不**回显
  `command` / `args` / `env` / `headers` 等配置体。
- **`plugin enable` 的 SKILL 零注册兜底文案不再绝对化**：`enabledPlugins` 的回滚写是 best-effort（写失败只记
  `enable rollback: failed to restore ...` WARNING），故消息由「was restored to its previous value」改为
  「a restore ... was attempted but may not have succeeded」。
- **声明态判据单点**：`skills/manifest.py` 新增 `entry_declares_component(entry, key)`（镜像 rust 同名函数），
  `manifest_declared_components` 与 installer 的 SKILL 声明判据共用；JSON 真值函数保持私有、不对外导出。
  对 **JSON 解码值**逐值等价（纯结构收口）；唯一差异在非 JSON falsy 类型（如 `set()` / 自定义对象）——
  现按「已声明」处理（fail-closed），而旧的 Python 真值按「未声明」。本仓调用方全部读 `json.loads` 结果，
  该差异不可达；嵌入宿主若自行构造非 JSON 值需注意。

### Fixed（#278 install 失败的恢复指引 + 迁移文档口径）

- **损坏 catalog 上 `plugin install` 失败不再只丢一句根因**：`install_plugin` 把 `SkillStagingError` 归一为
  `PluginInstallError` 时追加与 `plugin enable` 路径同源的 `Recovery:` 指引——`marketplace remove <name>` →
  `marketplace add <source>` → `plugin install <plugin>@<name>` →（install 不激活）`plugin enable`。此前 PR #276
  已消灭 traceback 并给出退出码 1 / `--json` 结构化错误，但用户仍需自行猜到这条恢复路径。
  **该指引只给「相对源插件根缺失」**（0.5.0 旧卸载删共享 catalog 子树这一种损坏）：`marketplace remove` 会级联
  卸载该 marketplace 的全部插件并撤销 trust，对独占 clone（`url` / `github` / `cnb` / `git-subdir`）的
  根缺失、clone 失败或相对源越界声明既修不好又有损失，故这些情形**一律不给**恢复指引（与 rust-sdk 同单一致）；
  `SkillSourceError` / `PluginManifestError` 分支同样不附。
  不新增机读码（`--json` 仍是 `{"error": "error", "message": …}`）。
- **迁移文档 §2 口径订正**：`docs/upgrade-0.5.1-marketplace-catalog-recovery.md` 不再写「可能以未处理异常、
  带堆栈的形式打印」，改为 0.5.1 的实际行为（结构化错误 + 恢复指引，`--json` 形态），保留 remove/add/重装步骤。

### Fixed（#296 marketplace 引用守卫纳入 flag/policy 层）

- `intent_reference_pids` / `registration_violation` / `commit_marketplace_registration` /
  `add_marketplace_source` / `stage_marketplace_skills` 增可选原始轴 `cwd` / `flag_settings_path` /
  `policy_settings`，与 `env` 一起透传给 `resolve_settings`。`policy_settings=None` 时由
  `intent_reference_pids` 按 `env` 现解析 ambient policy（与 rust#300 / `Computer._settings_axes` 同口径）；
  显式 mapping 覆盖，`{}` = 显式无 policy（hermetic 测试与嵌入式宿主的开关）。
- **两处守卫调用点共用同一五层视图**：注册提交点（`marketplace add` / clone 缺失修复）与 `--no-clone`
  意图注册（`_register_intent_only`）。
- 新增 `Computer._settings_axes()`（`SettingsAxes` 扁平原始轴），`_resolve_declared_settings` 与 REPL
  marketplace 守卫同源。
- **REPL 三面同源（marketplace / settings / plugin）**：统一经 `settings_axes_of(comp)` 取轴（优先
  `Computer._settings_axes()`，缺失时退化为 env/cwd/flag）——所有 marketplace 子命令（add/list/info/
  remove/refresh/migrate/set）与 settings 的 `show/get/set/edit` 均用**同一**实例 env；`settings show/get`
  的 project/local cwd、flag 层与 policy 层也取实例轴（policy 直接消费 `axes.policy_settings`，不再从 env
  重解析），plugin 的 project/local `project_path` 跟随实例 cwd。仅改 `marketplace` 一面会让
  「`settings set enabledPlugins` 写 ambient、守卫读实例」在注入 env 的 `--computer-factory` 宿主里互相
  看不见，引用可被静默重绑；三面同源后该缺口关闭。
- **子进程环境与 ambient 合并**：实例 env 可为部分映射（只注入 `XDG_CONFIG_HOME` / `HOME` 等）——git 子进程
  （`_git_env`）与 `settings edit` 的 `$EDITOR` 子进程改用 `{**os.environ, **instance_env}`，避免丢
  `PATH` / `SSH_AUTH_SOCK` / `HTTPS_PROXY` / `EDITOR`；settings 解析仍只用实例 env。合并后**剥除 ambient 的
  `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_OBJECT_DIRECTORY` /
  `GIT_ALTERNATE_OBJECT_DIRECTORIES` / `GIT_NAMESPACE`**（实例 env 显式提供才保留），否则会覆盖
  `git -C` / clone 落点；ambient `GIT_SSH_COMMAND` 缺 `-oBatchMode=yes` 时自动追加，避免挂 tty 等待口令。
- 引用语义不变：`installedPlugins` ∪ `enabledPlugins` 键、`@<name>` 后缀匹配、true/false 均算、层并集去重；
  不新增错误码。

### Fixed（#304 item 3 settings 路径判据收尾）

- **`readRoots` 改为单一判据（配置期 == 运行期）**：`_validate_read_roots` 去掉前置的「POSIX `/` 或
  Windows 盘符」语法预判，只留 `normalize_path`（与运行期 `build_read_roots` 同源），并拆出
  `_validate_read_roots_for(..., platform)` 平台显式接缝（与 rust `validate_read_roots_for` 同名同构，
  CI 只有 Linux）。行为变化：**Windows verbatim 前缀 `\\?\C:\...` 在配置期不再被误拒**——
  `normalize_path` 按协议向量 `win-verbatim-prefix` 接受并归一，此前「运行期读得了、配置期写不进」；
  drive-relative `C:repos\` 两侧一致拒绝。相对路径项的诊断文案由 `must be an absolute path` 变为
  `invalid path: …`（`settings show/get` 可见）。
- **`landingRoot` 与 rust 对齐 `trim()`**：先按 Unicode `White_Space` trim（逐字节等价 rust
  `str::trim()`——**不**含 U+001C–U+001F 四个信息分隔符，Python `strip()` 会多剥），trim 后为空 /
  非绝对 → 整字段判废，通过则**存 trim 后的值**。升级面（前导 / 尾随不对称，都**不是**简单的
  「从判废变为生效」）：
  - **前导**空白：此前整字段判废（`startswith("/")` 失败、静默失效），现在 trim 后生效；
  - **尾随**空白：此前**原样生效**——POSIX 上写入根就是带空格的目录（其中产物不会迁移），现在写入根
    **静默改为 trim 后的路径**；Windows 上 Win32 本就会剥掉非 verbatim 路径分量末尾的空格/点（真实差异
    是 `"C:\\x "` 此前因 `normalize_path` 拒绝空格结尾而丢掉 `.part` 拒绝集条目，trim 后该缺口一并修复）。
- **判据边界文档化（不做谓词合并，2026-11-23 用户裁决）**：`landingRoot` / `permissions.additionalDirectories`
  保持**语法**判据、不按宿主平台过滤，也不改用 `normalize_path`。由此的「外来平台形态」边界：
  `readRoots` 运行期由 `build_read_roots` 丢弃外来形态；`additionalDirectories` 运行期无消费者；
  **`landingRoot` 运行期不丢弃**——`Computer.landing_root` 直接 `Path(raw)`（POSIX 上 `D:\...` 是
  相对 cwd 的路径、Windows 上 `/home/...` 是 current-drive-rooted），该**实际落盘目录**仍进
  `.part` 拒绝集（见下条）。
- **拒绝集先绝对化再归一（镜像 rust `std::path::absolute`）**：`build_protected_paths` 对每个候选
  （含 `extra_paths` 与 `landingRoot`）先按宿主 cwd `abspath` 再 `normalize_path`。修复外来平台
  `landingRoot`（如 POSIX 宿主配 `D:\uploads`）此前因 normalize 失败**静默退出** `.part` 拒绝集、
  令在途上传可被 `client:resolve_path` 读出的缺口；相对注入项也不再被静默丢弃。
- **`build_read_roots` 按配置形态去重**（镜像 rust）：verbatim `\\?\C:\data` 与 canonical
  `/C:/data` 归一同根，只保留首次出现。
- `build_read_roots` 增 keyword-only `platform` 注入接缝（缺省宿主平台），供「配置期/运行期同源」测试
  在任意 OS 上对拍。

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
