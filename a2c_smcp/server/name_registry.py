"""
* 文件名: name_registry
* 作者: JQQ
* 创建日期: 2026/9/27
* 最后修改日期: 2026/9/27
* 版权: 2023 JQQ. All rights reserved.
* 依赖: 无
* 描述: 名字注册表的纯数据原语（sync / async Namespace 共用）/ Pure name-registry primitives shared by both namespaces
"""

from a2c_smcp.exceptions import NameConflictError, RoomFullError
from a2c_smcp.server.types import NAME_KEY, OFFICE_ID, SID


class SessionGoneError(RuntimeError):
    """注册时发现 sid 已不在连接中（断连收尾已开始）——拒绝注册，由 ``enter_room`` 的失败收敛兜底。

    不是协议错误码：发起者已断开，ack 无人接收；存在意义只是让「死 sid 占名」在构造上不可能
    （否则注册表残留指向死 sid 的键 ⇒ 同名重连永久 ``4105``，v0.5.0 审查 🔴7）。
    Raised when the registering sid is no longer connected; never surfaces as a protocol code.
    """


def reserve_name(
    name_map: dict[NAME_KEY, SID],
    reverse: dict[SID, NAME_KEY],
    key: NAME_KEY,
    sid: SID,
    *,
    connected: bool,
) -> None:
    """**原子**「校验 + 写入」：一次调用内完成 Agent 席位（``4101``）、同名（``4105``）与存活校验并落账。

    本函数**无 await、不取锁**——原子性由调用方提供：sync 端在注册表锁内调用，async 端依赖「零挂起点」
    （把它写成同步函数正是为了让 async 的原子性成为**结构保证**而非碰巧）。
    Atomic check-and-set; the caller provides atomicity (a lock on sync, zero suspension points on async).

    Agent 席位判据取注册表（不扫 socketio 成员）：Agent 的「在房」恒经注册落账、退房恒先注销，
    故「房内存在另一 Agent 键」与「房内已有 Agent」等价，且与写入同处一个原子步内（S1）。
    The agent-slot check reads the registry so it shares the atomic step with the write (S1).

    Raises:
        SessionGoneError: ``connected`` 为假（sid 已进入断连收尾）。
        RoomFullError: ``4101``，目标房已有另一 Agent。
        NameConflictError: ``4105``，键已被其它 sid 持有。
    """
    if not connected:
        raise SessionGoneError(f"sid {sid!r} is no longer connected")
    office_id, role, _name = key
    if role == "agent" and any(k[0] == office_id and k[1] == "agent" and holder != sid for k, holder in name_map.items()):
        raise RoomFullError()
    holder = name_map.get(key)
    if holder is not None and holder != sid:
        raise NameConflictError()
    if holder == sid:
        return  # 同 sid 重复注册：幂等 / idempotent re-registration
    # 一个 sid 至多持有一个键：先释放它此前持有的（正常路径已由 leave_room 释放，此处兜住收敛失败的残留）
    # One key per sid: release any key it still holds.
    release_name(name_map, reverse, sid)
    name_map[key] = sid
    reverse[sid] = key


def release_name(
    name_map: dict[NAME_KEY, SID],
    reverse: dict[SID, NAME_KEY],
    sid: SID,
    office_id: OFFICE_ID | None = None,
) -> NAME_KEY | None:
    """注销 sid 持有的键（按反向索引定位；**归属守卫**：只删确由本 sid 持有的键）。

    ``office_id`` 非空时只注销**该房**的键（``leave_room`` 用：退 A 房不得顺手注销同 sid 并发登记的 B 房键）。
    Release the key held by ``sid``; with ``office_id`` only a key of that office is released.

    Returns:
        实际从正向表删除的键；未持有 / 归属他人 / 不属该房时为 ``None``。
    """
    key = reverse.get(sid)
    if key is None or (office_id is not None and key[0] != office_id):
        return None
    del reverse[sid]
    if name_map.get(key) == sid:
        del name_map[key]
        return key
    return None
